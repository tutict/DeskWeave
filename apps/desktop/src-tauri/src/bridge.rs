//! The only bridge between the webview and the local control API.
//!
//! The webview cannot address arbitrary hosts: [`RuntimeBridge::request`] only
//! accepts a fixed set of read-only paths, and the auth token never leaves this
//! process. This keeps a compromised renderer from turning the desktop shell into
//! a general-purpose local proxy.

use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

/// Paths the UI is allowed to read. Side-effecting routes are deliberately absent.
pub const ALLOWED_PATHS: &[&str] = &["/health", "/adapters", "/tasks", "/events"];

const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone)]
pub struct RuntimeBridge {
    inner: Arc<BridgeInner>,
}

struct BridgeInner {
    base_url: String,
    token: String,
    startup_error: Option<String>,
}

#[derive(Serialize)]
pub struct RuntimeStatus {
    pub running: bool,
    pub base_url: String,
    pub error: Option<String>,
}

impl RuntimeBridge {
    pub fn new(base_url: String, token: String, startup_error: Option<String>) -> Self {
        Self {
            inner: Arc::new(BridgeInner {
                base_url,
                token,
                startup_error,
            }),
        }
    }

    pub fn startup_error(&self) -> Option<String> {
        self.inner.startup_error.clone()
    }

    pub fn base_url(&self) -> &str {
        &self.inner.base_url
    }

    fn url_for(&self, path: &str) -> Result<String, String> {
        if let Some(error) = &self.inner.startup_error {
            return Err(error.clone());
        }
        if !path.starts_with('/') || path.contains('?') || path.contains('#') {
            return Err(format!("invalid runtime path {path:?}"));
        }
        let normalised = path.trim_end_matches('/');
        let normalised = if normalised.is_empty() { "/" } else { normalised };
        if !ALLOWED_PATHS.contains(&normalised) {
            return Err(format!(
                "runtime path {path:?} is not allowed; permitted: {ALLOWED_PATHS:?}"
            ));
        }
        Ok(format!("{}{}", self.inner.base_url, path))
    }

    /// Blocking GET against the loopback control API. Call from a blocking pool.
    pub fn request(&self, path: &str) -> Result<Value, String> {
        let url = self.url_for(path)?;
        let response = ureq::get(&url)
            .set("Authorization", &format!("Bearer {}", self.inner.token))
            .set("Accept", "application/json")
            .timeout(REQUEST_TIMEOUT)
            .call();

        match response {
            Ok(response) => {
                let body = response
                    .into_string()
                    .map_err(|err| format!("could not read the runtime response: {err}"))?;
                serde_json::from_str(&body)
                    .map_err(|err| format!("the runtime returned invalid JSON: {err}"))
            }
            Err(ureq::Error::Status(code, response)) => {
                let body = response.into_string().unwrap_or_default();
                Err(format!("runtime returned HTTP {code}: {body}"))
            }
            Err(ureq::Error::Transport(transport)) => {
                Err(format!("could not reach the runtime at {url}: {transport}"))
            }
        }
    }
}

#[tauri::command]
pub async fn runtime_request(
    state: tauri::State<'_, crate::AppState>,
    path: String,
) -> Result<Value, String> {
    let bridge = state.bridge.clone();
    tauri::async_runtime::spawn_blocking(move || bridge.request(&path))
        .await
        .map_err(|err| format!("runtime request task failed: {err}"))?
}

#[tauri::command]
pub fn runtime_status(state: tauri::State<'_, crate::AppState>) -> RuntimeStatus {
    let running = state
        .process
        .lock()
        .map(|mut guard| guard.as_mut().map(|process| process.is_running()).unwrap_or(false))
        .unwrap_or(false);
    RuntimeStatus {
        running,
        base_url: state.bridge.base_url().to_string(),
        error: state.bridge.startup_error(),
    }
}
