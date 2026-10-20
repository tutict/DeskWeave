//! Spawns and supervises the Python core runtime as a child process.
//!
//! Contract with the UI:
//! * the shell picks a free loopback port and a per-run token, so two instances
//!   never collide and the token never lives in a committed file;
//! * the token stays in the Rust process — the webview only sees
//!   [`crate::bridge::runtime_request`], never the token itself;
//! * if the interpreter or the runtime fails to start, the error is surfaced to
//!   the UI instead of being swallowed.
//!
//! Shutdown is a *sequence*, not a `kill()`: ask the runtime to close over HTTP
//! (so adapters can close Chromium/Blender and SQLite can flush), wait for it to
//! exit, and only then force-kill. A Windows Job Object as a backstop makes sure
//! nothing the runtime spawned outlives this process.

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use crate::job::ChildJob;

/// How long we wait for `GET /health` to answer before declaring startup failed.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);
const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(150);

/// How long the runtime gets to close its own resources after `POST /shutdown`.
pub const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);
const SHUTDOWN_POLL_INTERVAL: Duration = Duration::from_millis(50);
const SHUTDOWN_REQUEST_TIMEOUT: Duration = Duration::from_secs(2);

/// What actually happened when the runtime was stopped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopOutcome {
    /// The child had already exited before we asked.
    AlreadyExited,
    /// The runtime accepted `POST /shutdown` and exited on its own.
    Graceful,
    /// The runtime did not exit within the grace period and was terminated.
    Forced,
}

/// A running (or failed) Python runtime process.
pub struct RuntimeProcess {
    child: Option<Child>,
    /// Present on Windows when the child was successfully placed in a job.
    job: Option<ChildJob>,
    pub base_url: String,
    pub token: String,
    pub error: Option<String>,
    /// False when job supervision is unavailable (logged, not fatal).
    pub supervised: bool,
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
            .env("PYTHONIOENCODING", "utf-8")
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

        // Assign to the job immediately: the shorter the window between spawn and
        // assignment, the smaller the chance a grandchild escapes supervision.
        let mut supervised = false;
        let job = match ChildJob::create() {
            Ok(job) => match job.assign(&child) {
                Ok(()) => {
                    supervised = true;
                    Some(job)
                }
                Err(err) => {
                    eprintln!("[deskweave] could not put the runtime into a job object: {err}");
                    None
                }
            },
            Err(err) => {
                eprintln!("[deskweave] job object supervision unavailable: {err}");
                None
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
        let mut process = Self {
            child: Some(child),
            job,
            base_url: base_url.clone(),
            token: token.clone(),
            error: None,
            supervised,
        };

        if let Err(err) = wait_until_healthy(&base_url, &token) {
            let outcome = process.stop_with_grace(GRACEFUL_SHUTDOWN_TIMEOUT);
            eprintln!("[deskweave] startup failed, runtime stop: {outcome:?}");
            return Self::failed(err);
        }
        process
    }

    fn failed(error: String) -> Self {
        eprintln!("[deskweave] runtime unavailable: {error}");
        Self {
            child: None,
            job: None,
            base_url: String::new(),
            token: String::new(),
            error: Some(error),
            supervised: false,
        }
    }

    pub fn is_running(&mut self) -> bool {
        match self.child.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        }
    }

    /// Stop the runtime: ask politely, wait, then force. Safe to call repeatedly.
    pub fn stop(&mut self) {
        let outcome = self.stop_with_grace(GRACEFUL_SHUTDOWN_TIMEOUT);
        eprintln!("[deskweave] runtime stop: {outcome:?}");
    }

    /// Stop with an explicit grace period. Returns what actually happened, so the
    /// caller (and tests) never have to assume the child is gone.
    pub fn stop_with_grace(&mut self, grace: Duration) -> StopOutcome {
        let Some(mut child) = self.child.take() else {
            self.release_job();
            return StopOutcome::AlreadyExited;
        };

        if matches!(child.try_wait(), Ok(Some(_))) {
            self.release_job();
            return StopOutcome::AlreadyExited;
        }

        // Ask the runtime to close: this runs its `finally` blocks, closes the
        // browser context, terminates a Blender subprocess and flushes SQLite.
        if let Err(err) = request_shutdown(&self.base_url, &self.token) {
            eprintln!("[deskweave] graceful shutdown request failed ({err}); will force-stop");
        }

        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => {
                    self.release_job();
                    return StopOutcome::Graceful;
                }
                Ok(None) => thread::sleep(SHUTDOWN_POLL_INTERVAL),
                Err(err) => {
                    eprintln!("[deskweave] could not poll the runtime process: {err}");
                    break;
                }
            }
        }

        kill_tree(&mut child);
        let _ = child.wait();
        self.release_job();
        StopOutcome::Forced
    }

    /// Closing the job terminates anything the runtime left behind.
    fn release_job(&mut self) {
        self.job.take();
    }
}

impl Drop for RuntimeProcess {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Terminate the child and everything it started.
///
/// On Windows a venv `python.exe` can be a launcher that re-execs the real
/// interpreter, so `Child::kill` alone would leave the interpreter running.
/// `taskkill /T` is scoped to this process id — never to a process *name*, so it
/// cannot touch an unrelated Python or Blender.
#[cfg(windows)]
fn kill_tree(child: &mut Child) {
    let pid = child.id().to_string();
    let killed = Command::new("taskkill")
        .args(["/PID", &pid, "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false);
    if !killed {
        let _ = child.kill();
    }
}

#[cfg(not(windows))]
fn kill_tree(child: &mut Child) {
    let _ = child.kill();
}

fn request_shutdown(base_url: &str, token: &str) -> Result<(), String> {
    if base_url.is_empty() || token.is_empty() {
        return Err("the runtime never started".to_string());
    }
    ureq::post(&format!("{base_url}/shutdown"))
        .set("Authorization", &format!("Bearer {token}"))
        .timeout(SHUTDOWN_REQUEST_TIMEOUT)
        .call()
        .map(|_| ())
        .map_err(|err| err.to_string())
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

#[cfg(test)]
mod tests {
    use std::process::{Child, Command, Stdio};
    use std::time::Duration;

    use super::{RuntimeProcess, StopOutcome};
    /// A process that is still alive when the grace period expires.
    ///
    /// The obvious `ping -n 60 127.0.0.1` is Windows-only syntax: on Unix `-n`
    /// means "numeric output" and `60` is parsed as the host, so ping exits
    /// immediately. That turned the escalation test into a race — it passed on
    /// Windows and reported `Graceful` on Linux. Whatever is spawned here has to
    /// outlive the grace period on every platform CI runs on.
    ///
    /// `cfg!` rather than `#[cfg]` on purpose: both branches are type-checked on
    /// every platform, so a mistake in the branch you cannot run here still fails
    /// the local build instead of waiting for CI.
    fn sleeping_child() -> Child {
        let mut command = if cfg!(windows) {
            let mut command = Command::new("ping");
            command.args(["-n", "60", "127.0.0.1"]);
            command
        } else {
            // `sh` is guaranteed by POSIX; naming `sleep` alone would depend on
            // how the runner's PATH is assembled.
            let mut command = Command::new("sh");
            command.args(["-c", "sleep 60"]);
            command
        };
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("could not spawn the test child")
    }

    /// A stand-in for the runtime: alive, but not reachable over HTTP.
    fn unreachable_process() -> RuntimeProcess {
        RuntimeProcess {
            child: Some(sleeping_child()),
            job: None,
            // Port 9 (discard) is closed on a default install, so the graceful
            // request fails immediately instead of hanging.
            base_url: "http://127.0.0.1:9".to_string(),
            token: "test-token".to_string(),
            error: None,
            supervised: false,
        }
    }

    #[test]
    fn stop_escalates_to_kill_when_the_endpoint_is_unreachable() {
        let mut process = unreachable_process();
        let outcome = process.stop_with_grace(Duration::from_millis(300));
        assert_eq!(outcome, StopOutcome::Forced);
        assert!(!process.is_running(), "the child is still running");
    }

    #[test]
    fn stop_is_idempotent() {
        let mut process = unreachable_process();
        assert_eq!(
            process.stop_with_grace(Duration::from_millis(100)),
            StopOutcome::Forced
        );
        assert_eq!(
            process.stop_with_grace(Duration::from_millis(100)),
            StopOutcome::AlreadyExited
        );
    }

    /// A venv `python.exe` can re-exec the interpreter. Killing only the launcher
    /// would orphan the real process, so `kill_tree` must take the whole tree.
    #[cfg(windows)]
    #[test]
    fn kill_tree_terminates_the_launcher_and_its_child() {
        use std::thread::sleep;
        use std::time::Instant;

        use super::kill_tree;

        let Some(python) = which_python() else {
            eprintln!("skipping: no python on PATH");
            return;
        };
        let pid_file =
            std::env::temp_dir().join(format!("deskweave-kill-tree-{}.pid", std::process::id()));
        let _ = std::fs::remove_file(&pid_file);
        let inner = format!(
            "import os,pathlib,time; pathlib.Path({:?}).write_text(str(os.getpid())); time.sleep(60)",
            pid_file.display().to_string()
        );
        let outer = format!(
            "import subprocess,sys; subprocess.run([sys.executable,'-c',{:?}])",
            inner
        );

        let mut child = Command::new(&python)
            .args(["-c", &outer])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("could not spawn the test launcher");

        let deadline = Instant::now() + Duration::from_secs(20);
        while !pid_file.exists() && Instant::now() < deadline {
            sleep(Duration::from_millis(50));
        }
        let grandchild: u32 = std::fs::read_to_string(&pid_file)
            .expect("the grandchild never wrote its pid")
            .trim()
            .parse()
            .expect("the pid file did not contain a number");
        assert!(is_alive(grandchild), "the grandchild should be running");
        assert_ne!(grandchild, child.id(), "the test needs a real grandchild");

        kill_tree(&mut child);
        let _ = child.wait();

        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if !is_alive(grandchild) {
                let _ = std::fs::remove_file(&pid_file);
                return;
            }
            sleep(Duration::from_millis(50));
        }
        panic!("the re-exec'd interpreter (pid {grandchild}) survived the tree kill");
    }

    #[cfg(windows)]
    fn which_python() -> Option<std::path::PathBuf> {
        for name in ["python", "python3"] {
            if Command::new(name)
                .arg("--version")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|status| status.success())
                .unwrap_or(false)
            {
                return Some(std::path::PathBuf::from(name));
            }
        }
        None
    }

    /// `OpenProcess` with query rights is the cheapest "is this pid alive" probe.
    #[cfg(windows)]
    fn is_alive(pid: u32) -> bool {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        // SAFETY: no inheritance, no special rights; the handle is closed when
        // non-null, and a null handle simply means "no such process".
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return false;
        }
        unsafe { CloseHandle(handle) };
        true
    }
}
