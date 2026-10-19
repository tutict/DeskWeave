//! Spawns and supervises the Python core runtime as a child process.
//!
//! Contract with the UI:
//! * the shell picks a free loopback port and a per-run token, so two instances
//!   never collide and the token never lives in a committed file;
//! * the token stays in the Rust process — the webview only sees
//!   [`crate::bridge::runtime_request`], never the token itself;
//! * if the interpreter or the runtime fails to start, the error is surfaced to
//!   the UI instead of being swallowed.

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

/// How long we wait for `GET /health` to answer before declaring startup failed.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);
const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(150);

/// A running (or failed) Python runtime process.
pub struct RuntimeProcess {
    child: Option<Child>,
    pub base_url: String,
    pub token: String,
    pub error: Option<String>,
}

impl RuntimeProcess {
    /// Start the runtime, returning a handle even when startup fails so the UI can
    /// render the real reason.
    pub fn start(data_dir: PathBuf) -> Self {
        let token = uuid::Uuid::new_v4().to_string();
        let port = match free_loopback_port() {
            Some(port) => port,
            None => {
                return Self::failed("could not reserve a loopback port for the runtime".into());
            }
        };

        let python = match resolve_python() {
            Some(python) => python,
            None => {
                return Self::failed(
                    "no Python interpreter found: set DESKWEAVE_PYTHON or create .venv \
                     (see docs/development/getting-started.md)"
                        .into(),
                );
            }
        };

        let mut command = Command::new(&python);
        command
            .args(["-m", "deskweave_runtime.service"])
            .env("DESKWEAVE_HOST", "127.0.0.1")
            .env("DESKWEAVE_PORT", port.to_string())
            .env("DESKWEAVE_DATA_DIR", &data_dir)
            .env("DESKWEAVE_TOKEN", &token)
            .env("DESKWEAVE_LOG_LEVEL", "INFO")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(err) => {
                return Self::failed(format!(
                    "failed to spawn {} -m deskweave_runtime.service: {err}",
                    python.display()
                ));
            }
        };

        // Forward the runtime's stderr into this process so logs stay visible in
        // the terminal that launched the desktop app.
        if let Some(stderr) = child.stderr.take() {
            thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    eprintln!("[runtime] {line}");
                }
            });
        }

        let base_url = format!("http://127.0.0.1:{port}");
        let process = Self {
            child: Some(child),
            base_url: base_url.clone(),
            token: token.clone(),
            error: None,
        };

        if let Err(err) = wait_until_healthy(&base_url, &token) {
            let mut process = process;
            process.stop();
            return Self::failed(err);
        }
        process
    }

    fn failed(error: String) -> Self {
        eprintln!("[deskweave] runtime unavailable: {error}");
        Self {
            child: None,
            base_url: String::new(),
            token: String::new(),
            error: Some(error),
        }
    }

    pub fn is_running(&mut self) -> bool {
        match self.child.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        }
    }

    /// Terminate the child process. Safe to call more than once.
    pub fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for RuntimeProcess {
    fn drop(&mut self) {
        self.stop();
    }
}

fn free_loopback_port() -> Option<u16> {
    // Binding to port 0 asks the OS for a free port. The listener is dropped
    // immediately, so there is a small race between this call and the runtime
    // binding the same port; the health check below turns that race into a
    // visible startup error instead of a silent failure.
    let listener = TcpListener::bind(("127.0.0.1", 0)).ok()?;
    listener.local_addr().ok().map(|addr| addr.port())
}

/// Resolve the interpreter that has the deskweave packages installed.
fn resolve_python() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("DESKWEAVE_PYTHON") {
        if !explicit.trim().is_empty() {
            let path = PathBuf::from(explicit);
            if path.is_file() {
                return Some(path);
            }
        }
    }

    // Development layout: <repo>/.venv/{Scripts,bin}/python[.exe]
    let mut dir = std::env::current_dir().ok()?;
    for _ in 0..4 {
        for candidate in venv_candidates(&dir) {
            if candidate.is_file() {
                return Some(candidate);
            }
        }
        if !dir.pop() {
            break;
        }
    }

    // Fall back to the interpreter on PATH.
    for name in ["python3", "python"] {
        if Command::new(name)
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
        {
            return Some(PathBuf::from(name));
        }
    }
    None
}

fn venv_candidates(dir: &std::path::Path) -> [PathBuf; 2] {
    [
        dir.join(".venv").join("Scripts").join("python.exe"),
        dir.join(".venv").join("bin").join("python"),
    ]
}

fn wait_until_healthy(base_url: &str, token: &str) -> Result<(), String> {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    let mut last_error = String::from("no response");
    while Instant::now() < deadline {
        match probe_health(base_url, token) {
            Ok(()) => return Ok(()),
            Err(err) => last_error = err,
        }
        thread::sleep(STARTUP_POLL_INTERVAL);
    }
    Err(format!(
        "the Python runtime did not become healthy at {base_url} within {}s (last error: {last_error})",
        STARTUP_TIMEOUT.as_secs()
    ))
}

fn probe_health(base_url: &str, token: &str) -> Result<(), String> {
    let response = ureq::get(&format!("{base_url}/health"))
        .set("Authorization", &format!("Bearer {token}"))
        .timeout(Duration::from_secs(2))
        .call();
    match response {
        Ok(_) => Ok(()),
        Err(err) => Err(err.to_string()),
    }
}
