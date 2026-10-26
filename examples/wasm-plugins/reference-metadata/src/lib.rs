//! Reference-metadata plugin for DeskWeave.
//!
//! This is the "hello world" of the WASM plugin ABI, and it is deliberately
//! boring: it reads a list of reference-image records, asks the host to validate
//! each one through `reference.metadata.validate`, and reports which records were
//! accepted. It never touches the filesystem, the network, or Blender — it
//! cannot, because the sandbox gives it no such import.
//!
//! The ABI is defined in `packages/plugin-wasm/src/deskweave_plugin_wasm/abi.py`;
//! nothing here is Rust-specific, which is the point. Any language that can emit
//! `wasm32-wasip1`, export four functions and speak length-prefixed JSON can be
//! dropped in as a replacement.
//!
//! Build:  cargo build --release --target wasm32-wasip1
//! Output: target/wasm32-wasip1/release/deskweave_plugin_reference_metadata.wasm

use serde::Deserialize;
use serde_json::{json, Value};

/// Must match `ABI_VERSION` in the host. The host refuses to load anything else.
const ABI_VERSION: i32 = 1;

/// Negative codes mirror `HostCallStatus` in the host package.
const DENIED_NOT_DECLARED: i32 = -1;
const DENIED_NO_GRANT: i32 = -2;
const DENIED_UNKNOWN_CAPABILITY: i32 = -3;
const INVALID_ARGUMENT: i32 = -4;
const BUFFER_TOO_SMALL: i32 = -5;

/// 32 KiB is far more than this plugin needs; the host enforces the real cap.
const HOST_OUT_CAPACITY: usize = 32 * 1024;

#[link(wasm_import_module = "env")]
extern "C" {
    fn dw_host_call(req_ptr: *const u8, req_len: u32, out_ptr: *mut u8, out_cap: u32) -> i32;
}

#[derive(Debug, Deserialize)]
struct Input {
    images: Vec<Value>,
}

// -- ABI surface -----------------------------------------------------------

#[no_mangle]
pub extern "C" fn dw_abi_version() -> i32 {
    ABI_VERSION
}

#[no_mangle]
pub extern "C" fn dw_alloc(len: u32) -> u32 {
    if len == 0 {
        return 0;
    }
    let mut buf: Vec<u8> = Vec::with_capacity(len as usize);
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr as u32
}

#[no_mangle]
pub extern "C" fn dw_free(ptr: u32, len: u32) {
    if ptr == 0 || len == 0 {
        return;
    }
    unsafe {
        drop(Vec::from_raw_parts(ptr as *mut u8, 0, len as usize));
    }
}

#[no_mangle]
pub extern "C" fn dw_invoke(ptr: u32, len: u32) -> u32 {
    let input = unsafe { std::slice::from_raw_parts(ptr as *const u8, len as usize) };
    let response = match run(input) {
        Ok(value) => value,
        Err(message) => json!({ "error": message }),
    };
    write_frame(&response)
}

// -- implementation --------------------------------------------------------

fn run(input: &[u8]) -> Result<Value, String> {
    let parsed: Input =
        serde_json::from_slice(input).map_err(|e| format!("input is not a valid request: {e}"))?;

    let mut accepted: Vec<Value> = Vec::new();
    let mut rejected: Vec<Value> = Vec::new();

    for (index, image) in parsed.images.iter().enumerate() {
        let artifact_id = image
            .get("artifact_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();

        let request = json!({ "capability": "reference.metadata.validate", "payload": { "image": image } });

        match host_call(&request) {
            Ok(verdict) => {
                let valid = verdict.get("valid").and_then(Value::as_bool).unwrap_or(false);
                if valid {
                    accepted.push(json!({ "index": index, "artifact_id": artifact_id }));
                } else {
                    rejected.push(json!({
                        "index": index,
                        "artifact_id": artifact_id,
                        "problems": verdict.get("problems").cloned().unwrap_or(json!([])),
                    }));
                }
            }
            Err(HostError::Status(code)) => {
                // Never paper over a denial. Returning a "successful" summary here
                // would let a plugin claim work it was not allowed to do; the host
                // would reject the call anyway, but the honest answer is an error.
                let reason = match code {
                    DENIED_NOT_DECLARED => "capability was not declared in the manifest",
                    DENIED_NO_GRANT => "capability was declared but not authorised",
                    DENIED_UNKNOWN_CAPABILITY => "capability is not offered by the host",
                    INVALID_ARGUMENT => "capability rejected the arguments",
                    BUFFER_TOO_SMALL => "host response did not fit the buffer",
                    _ => "host call failed",
                };
                return Err(format!("{reason} (status {code})"));
            }
            Err(HostError::Decode) => return Err("host response was not valid JSON".to_string()),
        }
    }

    Ok(json!({
        "checked": parsed.images.len(),
        "accepted": accepted,
        "rejected": rejected,
    }))
}

enum HostError {
    Status(i32),
    Decode,
}

fn host_call(request: &Value) -> Result<Value, HostError> {
    let bytes = serde_json::to_vec(request).map_err(|_| HostError::Decode)?;
    let mut out = vec![0u8; HOST_OUT_CAPACITY];

    let written = unsafe {
        dw_host_call(
            bytes.as_ptr(),
            bytes.len() as u32,
            out.as_mut_ptr(),
            out.len() as u32,
        )
    };

    if written < 0 {
        return Err(HostError::Status(written));
    }

    let written = written as usize;
    if written > out.len() {
        return Err(HostError::Decode);
    }
    out.truncate(written);
    serde_json::from_slice(&out).map_err(|_| HostError::Decode)
}

fn write_frame(value: &Value) -> u32 {
    let body = match serde_json::to_vec(value) {
        Ok(bytes) => bytes,
        Err(_) => b"{\"error\":\"could not serialise response\"}".to_vec(),
    };

    let total = 4 + body.len();
    let mut frame: Vec<u8> = Vec::with_capacity(total);
    frame.extend_from_slice(&(body.len() as u32).to_le_bytes());
    frame.extend_from_slice(&body);

    let ptr = frame.as_ptr() as u32;
    std::mem::forget(frame);
    ptr
}
