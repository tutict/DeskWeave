//! DeskWeave desktop shell.
//!
//! Responsibilities, in order: window/system integration (Tauri), and the
//! lifetime of the Python core runtime child process. No business logic lives
//! here — workflows, permissions and adapters stay in Python.

mod bridge;
mod process;

use std::path::PathBuf;
use std::sync::Mutex;

use process::RuntimeProcess;
use tauri::Manager;

pub struct AppState {
    pub bridge: bridge::RuntimeBridge,
    pub process: Mutex<Option<RuntimeProcess>>,
}

fn resolve_data_dir(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .app_data_dir()
        .unwrap_or_else(|_| PathBuf::from(".deskweave"))
}

pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let data_dir = resolve_data_dir(app.handle());
            if let Err(err) = std::fs::create_dir_all(&data_dir) {
                eprintln!(
                    "[deskweave] could not create data dir {}: {err}",
                    data_dir.display()
                );
            }

            let runtime = RuntimeProcess::start(data_dir);
            let bridge = bridge::RuntimeBridge::new(
                runtime.base_url.clone(),
                runtime.token.clone(),
                runtime.error.clone(),
            );
            app.manage(AppState {
                bridge,
                process: Mutex::new(Some(runtime)),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            bridge::runtime_request,
            bridge::runtime_status
        ])
        .build(tauri::generate_context!())
        .expect("failed to build the DeskWeave desktop shell");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(state) = app_handle.try_state::<AppState>() {
                if let Ok(mut guard) = state.process.lock() {
                    if let Some(process) = guard.as_mut() {
                        process.stop();
                    }
                }
            }
        }
    });
}
