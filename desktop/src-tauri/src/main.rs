#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::Mutex;

use prism_desktop_lib::config::resolve_config;
use prism_desktop_lib::ide_workspace;
use prism_desktop_lib::orchestrator::{self, AppState};
use prism_desktop_lib::token::generate_token;

fn repo_root() -> PathBuf {
    // Dev: desktop/src-tauri -> repo root is two levels up.
    std::env::var("PRISM_REPO_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            let mut p = std::env::current_dir().expect("cwd");
            // When run via `tauri dev`, cwd is desktop/src-tauri.
            if p.ends_with("src-tauri") {
                p.pop();
                p.pop();
            }
            p
        })
}

fn main() {
    let token = std::env::var("PRISM_TOKEN")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(generate_token);
    use prism_desktop_lib::client_config;
    let client_config_path = client_config::config_path();
    let config = resolve_config(
        std::env::var("PRISM_URL").ok(),
        client_config::load_url(&client_config_path),
        std::env::var("PRISM_SPAWN_SIDECAR")
            .ok()
            .and_then(|raw| match raw.trim().to_ascii_lowercase().as_str() {
                "1" | "true" | "yes" => Some(true),
                "0" | "false" | "no" => Some(false),
                _ => None,
            })
            .or_else(|| client_config::load_spawn_sidecar(&client_config_path)),
    );
    let root = repo_root();

    let state = AppState {
        token: token.clone(),
        config: config.clone(),
        backend: Mutex::new(None),
        ide_workspace: Mutex::new(ide_workspace::load_saved_workspace()),
        repo_root: root,
    };

    if state.config.spawn_sidecar {
        orchestrator::start_backend(&state).expect("spawn backend");
        let ready = orchestrator::wait_until_ready(&state.config.url);
        if !ready {
            eprintln!(
                "Prism backend failed to become healthy within 30s at {}",
                state.config.url
            );
        }
    }

    // Central mode: give a clear message if the shared backend isn't reachable,
    // rather than a blank/error webview. (Local mode already health-gates above.)
    if !state.config.spawn_sidecar {
        use prism_desktop_lib::health::{HealthProbe, ReqwestProbe};
        if !ReqwestProbe::new().is_healthy(&state.config.url) {
            eprintln!("Prism server not reachable at {}", state.config.url);
            // Non-fatal: still open the window (it will retry on reload), but log loudly.
        }
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            use tauri::Manager;
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
        }))
        .manage(state)
        .setup(|app| {
            use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
            let url = {
                let st = app.state::<AppState>();
                prism_desktop_lib::config::backend_window_url(&st.config.url)
                    .expect("invalid backend URL")
            };
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title("Prisma Desktop")
                .inner_size(1280.0, 820.0)
                .min_inner_size(960.0, 640.0)
                .resizable(true)
                .build()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            ide_workspace::ide_get_workspace,
            ide_workspace::ide_choose_workspace,
            ide_workspace::ide_list_tree,
            ide_workspace::ide_read_file,
            ide_workspace::ide_stat_file,
            ide_workspace::ide_write_file,
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                use tauri::Manager;
                let state = window.state::<AppState>();
                orchestrator::shutdown(&state);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Prism desktop shell");
}
