//! The only bridge between the webview and the local control API.
//!
//! The webview cannot address arbitrary hosts. [`RuntimeBridge::request`] only
//! accepts an explicit allowlist of method/path shapes, and the auth token never
//! leaves this process. This keeps a compromised renderer from turning the
//! desktop shell into a general-purpose local proxy.
//!
//! The allowlist is structural rather than a flat string list because task and
//! artifact ids appear in the path. Ids are constrained to the same alphabet the
//! runtime uses (`[A-Za-z0-9_-]`), so a crafted path cannot escape the route
//! space with `..`, `%2f` or a scheme.

use std::io::Read;
use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

/// Largest request body forwarded to the runtime. Every body is a small JSON object.
const MAX_BODY_BYTES: usize = 64 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
/// Confirmation may launch Blender, which is allowed to take minutes.
const CONFIRM_TIMEOUT: Duration = Duration::from_secs(240);

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
    /// True when the runtime child is inside a Windows Job Object, i.e. a shell
    /// crash cannot orphan it. Reported so the state is observable, not assumed.
    pub supervised: bool,
    pub base_url: String,
    pub error: Option<String>,
}

/// A structured failure the UI can branch on without parsing prose.
#[derive(Serialize, Debug)]
pub struct BridgeError {
    pub status: u16,
    pub code: String,
    pub message: String,
    pub details: Value,
}

impl BridgeError {
    fn local(message: impl Into<String>) -> Self {
        Self {
            status: 0,
            code: "bridge_error".to_string(),
            message: message.into(),
            details: Value::Null,
        }
    }
}

/// Returns true when `method` + `path` is one of the routes the UI may use.
pub fn is_allowed(method: &str, path: &str) -> bool {
    let path = path.split('?').next().unwrap_or("");
    let segments: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();

    match (method, segments.as_slice()) {
        ("GET", ["health"] | ["adapters"] | ["events"] | ["tasks"]) => true,
        ("GET", ["tasks", id]) => is_id(id),
        ("GET", ["artifacts", id]) => is_id(id),
        ("POST", ["tasks"]) => true,
        ("POST", ["tasks", id, "confirm" | "cancel"]) => is_id(id),
        // "Show in folder" / "Open in Blender": the runtime resolves the artifact
        // and performs the launch, the webview only names an id and an action.
        ("POST", ["artifacts", id, "open"]) => is_id(id),
        _ => false,
    }
}

fn is_id(candidate: &str) -> bool {
    !candidate.is_empty()
        && candidate.len() <= 64
        && candidate
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
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

    fn url_for(&self, path: &str) -> Result<String, BridgeError> {
        if let Some(error) = &self.inner.startup_error {
            return Err(BridgeError::local(error.clone()));
        }
        if !path.starts_with('/') {
            return Err(BridgeError::local(format!("invalid runtime path {path:?}")));
        }
        // Query strings are allowed only as a status filter on the task list.
        if let Some((_, query)) = path.split_once('?') {
            if !query.split('&').all(|pair| pair.starts_with("status=")) {
                return Err(BridgeError::local(format!(
                    "runtime path {path:?} carries an unsupported query"
                )));
            }
        }
        Ok(format!("{}{}", self.inner.base_url, path))
    }

    /// Blocking call against the loopback control API. Call from a blocking pool.
    pub fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Value, BridgeError> {
        if !is_allowed(method, path) {
            return Err(BridgeError::local(format!(
                "runtime route {method} {path:?} is not allowed"
            )));
        }
        let url = self.url_for(path)?;
        let timeout = if path.contains("/confirm") {
            CONFIRM_TIMEOUT
        } else {
            REQUEST_TIMEOUT
        };
        let authorization = format!("Bearer {}", self.inner.token);

        let response = match method {
            "GET" => ureq::get(&url)
                .set("Authorization", &authorization)
                .set("Accept", "application/json")
                .timeout(timeout)
                .call(),
            "POST" => {
                let payload = body.cloned().unwrap_or(Value::Object(Default::default()));
                let encoded = serde_json::to_vec(&payload).map_err(|err| {
                    BridgeError::local(format!("could not encode the body: {err}"))
                })?;
                if encoded.len() > MAX_BODY_BYTES {
                    return Err(BridgeError::local(format!(
                        "request body exceeds {MAX_BODY_BYTES} bytes"
                    )));
                }
                ureq::post(&url)
                    .set("Authorization", &authorization)
                    .set("Accept", "application/json")
                    .set("Content-Type", "application/json")
                    .timeout(timeout)
                    .send_bytes(&encoded)
            }
            other => {
                return Err(BridgeError::local(format!(
                    "method {other:?} is not allowed"
                )))
            }
        };

        match response {
            Ok(response) => {
                let text = response.into_string().map_err(|err| {
                    BridgeError::local(format!("could not read the response: {err}"))
                })?;
                serde_json::from_str(&text).map_err(|err| {
                    BridgeError::local(format!("the runtime returned invalid JSON: {err}"))
                })
            }
            Err(ureq::Error::Status(code, response)) => {
                let text = response.into_string().unwrap_or_default();
                Err(parse_error(code, &text))
            }
            Err(ureq::Error::Transport(transport)) => Err(BridgeError {
                status: 0,
                code: "runtime_unreachable".to_string(),
                message: format!("could not reach the runtime at {url}: {transport}"),
                details: Value::Null,
            }),
        }
    }

    /// Fetch an artifact preview as a data URL.
    ///
    /// The token must not reach the webview, so the bytes are fetched here and
    /// handed over inline. Preview size is bounded by the runtime (8 MiB).
    pub fn preview_data_url(&self, artifact_id: &str) -> Result<String, BridgeError> {
        if !is_id(artifact_id) {
            return Err(BridgeError::local("invalid artifact id"));
        }
        let url = format!("{}/artifacts/{artifact_id}/preview", self.inner.base_url);
        let response = ureq::get(&url)
            .set("Authorization", &format!("Bearer {}", self.inner.token))
            .timeout(REQUEST_TIMEOUT)
            .call();
        match response {
            Ok(response) => {
                let media_type = response
                    .header("content-type")
                    .unwrap_or("application/octet-stream")
                    .to_string();
                let mut bytes = Vec::new();
                response
                    .into_reader()
                    .read_to_end(&mut bytes)
                    .map_err(|err| {
                        BridgeError::local(format!("could not read the preview: {err}"))
                    })?;
                Ok(format!(
                    "data:{media_type};base64,{}",
                    base64_encode(&bytes)
                ))
            }
            Err(ureq::Error::Status(code, response)) => {
                let text = response.into_string().unwrap_or_default();
                Err(parse_error(code, &text))
            }
            Err(ureq::Error::Transport(transport)) => Err(BridgeError {
                status: 0,
                code: "runtime_unreachable".to_string(),
                message: format!("could not reach the runtime at {url}: {transport}"),
                details: Value::Null,
            }),
        }
    }
}

/// Turn a runtime error body into a [`BridgeError`], keeping the runtime's code.
fn parse_error(status: u16, body: &str) -> BridgeError {
    if let Ok(value) = serde_json::from_str::<Value>(body) {
        if let Some(error) = value.get("error") {
            return BridgeError {
                status,
                code: error
                    .get("code")
                    .and_then(Value::as_str)
                    .unwrap_or("runtime_error")
                    .to_string(),
                message: error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("the runtime rejected the request")
                    .to_string(),
                details: error.get("details").cloned().unwrap_or(Value::Null),
            };
        }
    }
    BridgeError {
        status,
        code: "runtime_error".to_string(),
        message: if body.is_empty() {
            format!("runtime returned HTTP {status}")
        } else {
            body.to_string()
        },
        details: Value::Null,
    }
}

/// Minimal base64 encoder (standard alphabet, padded).
fn base64_encode(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(triple >> 18) as usize & 0x3F] as char);
        out.push(ALPHABET[(triple >> 12) as usize & 0x3F] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(triple >> 6) as usize & 0x3F] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[triple as usize & 0x3F] as char
        } else {
            '='
        });
    }
    out
}

#[tauri::command]
pub async fn runtime_request(
    state: tauri::State<'_, crate::AppState>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<Value, BridgeError> {
    let bridge = state.bridge.clone();
    tauri::async_runtime::spawn_blocking(move || bridge.request(&method, &path, body.as_ref()))
        .await
        .map_err(|err| BridgeError::local(format!("runtime request task failed: {err}")))?
}

#[tauri::command]
pub async fn artifact_preview(
    state: tauri::State<'_, crate::AppState>,
    artifact_id: String,
) -> Result<String, BridgeError> {
    let bridge = state.bridge.clone();
    tauri::async_runtime::spawn_blocking(move || bridge.preview_data_url(&artifact_id))
        .await
        .map_err(|err| BridgeError::local(format!("preview task failed: {err}")))?
}

#[tauri::command]
pub fn runtime_status(state: tauri::State<'_, crate::AppState>) -> RuntimeStatus {
    let (running, supervised) = state
        .process
        .lock()
        .map(|mut guard| match guard.as_mut() {
            Some(process) => (process.is_running(), process.supervised),
            None => (false, false),
        })
        .unwrap_or((false, false));
    RuntimeStatus {
        running,
        supervised,
        base_url: state.bridge.base_url().to_string(),
        error: state.bridge.startup_error(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowed_routes() {
        assert!(is_allowed("GET", "/health"));
        assert!(is_allowed("GET", "/tasks"));
        assert!(is_allowed("GET", "/tasks?status=failed"));
        assert!(is_allowed("GET", "/tasks/task_abc123"));
        assert!(is_allowed("GET", "/artifacts/art_abc123"));
        assert!(is_allowed("POST", "/tasks"));
        assert!(is_allowed("POST", "/tasks/task_abc123/confirm"));
        assert!(is_allowed("POST", "/tasks/task_abc123/cancel"));
        assert!(is_allowed("POST", "/artifacts/art_abc123/open"));
    }

    #[test]
    fn rejected_routes() {
        assert!(!is_allowed("GET", "/shutdown"));
        assert!(!is_allowed("POST", "/shutdown"));
        assert!(!is_allowed("GET", "/artifacts/art_1/preview"));
        assert!(!is_allowed("GET", "/artifacts/art_abc123/open"));
        assert!(!is_allowed("POST", "/artifacts/art_abc123"));
        assert!(!is_allowed("POST", "/artifacts/../open"));
        assert!(!is_allowed("POST", "/artifacts/art_abc123/delete"));
        assert!(!is_allowed("POST", "/health"));
        assert!(!is_allowed("DELETE", "/tasks/task_abc"));
        assert!(!is_allowed("GET", "/tasks/..%2f..%2fetc"));
        assert!(!is_allowed("GET", "/tasks/../../etc/passwd"));
        assert!(!is_allowed("GET", "/tasks/task_abc/confirm"));
        assert!(!is_allowed("GET", "http://evil.example/health"));
        assert!(!is_allowed("POST", "/tasks/task_abc/run"));
    }

    #[test]
    fn base64_matches_reference_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }
}
