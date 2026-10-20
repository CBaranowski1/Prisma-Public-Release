use crate::health::{wait_for_health, ReqwestProbe};
use std::process::Command;
use std::time::Duration;

use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::Mutex;

use crate::config::PrismConfig;

/// Locate a bundled prism-server binary: PRISM_SERVER_BIN if it exists on disk,
/// else the conventional staged path under the desktop project. None => dev mode.
pub fn resolve_server_bin(repo_root: &Path) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("PRISM_SERVER_BIN") {
        let pb = PathBuf::from(p);
        if pb.exists() {
            return Some(pb);
        }
    }
    let bundled = repo_root
        .join("desktop")
        .join("src-tauri")
        .join("binaries")
        .join("prism-server")
        .join("prism-server.exe");
    if bundled.exists() {
        Some(bundled)
    } else {
        None
    }
}

/// Pure: (program, args, env) for the backend. `Some(bin)` => the bundled
/// PyInstaller exe; `None` => the dev `python -m uvicorn` fallback. Injects
/// PRISM_TOKEN always and PRISM_DATA_DIR when provided.
pub fn backend_command(
    server_bin: Option<&Path>,
    token: &str,
    data_dir: Option<&str>,
) -> (String, Vec<String>, Vec<(String, String)>) {
    let tail = ["--host", "127.0.0.1", "--port", "7000"];
    let (program, args): (String, Vec<String>) = match server_bin {
        Some(bin) => (
            bin.to_string_lossy().into_owned(),
            tail.iter().map(|s| s.to_string()).collect(),
        ),
        None => (
            "python".to_string(),
            ["-m", "uvicorn", "app:app"]
                .iter()
                .chain(tail.iter())
                .map(|s| s.to_string())
                .collect(),
        ),
    };
    let mut env = vec![
        ("PRISM_TOKEN".to_string(), token.to_string()),
        // This is a single-user, loopback-only sidecar. Desktop users should
        // enter the application directly without a browser login ceremony.
        ("AUTH_ENABLED".to_string(), "false".to_string()),
        ("LOCALHOST_BYPASS".to_string(), "false".to_string()),
        ("PRISMA_DESKTOP_MODE".to_string(), "true".to_string()),
    ];
    if let Some(dir) = data_dir.filter(|s| !s.trim().is_empty()) {
        env.push(("PRISM_DATA_DIR".to_string(), dir.to_string()));
    }
    (program, args, env)
}

pub struct AppState {
    pub token: String,
    pub config: PrismConfig,
    pub backend: Mutex<Option<Child>>,
    pub ide_workspace: Mutex<Option<PathBuf>>,
    pub repo_root: PathBuf,
}

/// Spawn the backend child (cwd = repo_root) with PRISM_TOKEN injected.
pub fn start_backend(state: &AppState) -> std::io::Result<()> {
    let server_bin = resolve_server_bin(&state.repo_root);
    let data_dir = std::env::var("PRISM_DATA_DIR").ok();
    let (program, args, env) =
        backend_command(server_bin.as_deref(), &state.token, data_dir.as_deref());
    let mut cmd = Command::new(program);
    cmd.args(&args).current_dir(&state.repo_root);
    for (k, v) in env {
        cmd.env(k, v);
    }
    let child = cmd.spawn()?;
    *state.backend.lock().unwrap() = Some(child);
    Ok(())
}

/// Block until the backend answers /api/health (500ms x 60 = 30s) or give up.
pub fn wait_until_ready(url: &str) -> bool {
    let probe = ReqwestProbe::new();
    wait_for_health(
        &probe,
        url,
        60,
        Duration::from_millis(500),
        &std::thread::sleep,
    )
}

/// Restart the backend up to 3 times if it has exited. Returns true if a live
/// (or freshly restarted) backend is present.
pub fn ensure_backend_alive(state: &AppState) -> bool {
    let mut guard = state.backend.lock().unwrap();
    let exited = match guard.as_mut() {
        Some(child) => matches!(child.try_wait(), Ok(Some(_))),
        None => true,
    };
    drop(guard);
    if !exited {
        return true;
    }
    for attempt in 1..=3 {
        if start_backend(state).is_ok() && wait_until_ready(&state.config.url) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500 * attempt));
    }
    false
}

/// Kill the managed local backend. Best-effort; ignores already-exited.
pub fn shutdown(state: &AppState) {
    if let Some(mut child) = state.backend.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backend_command_dev_fallback_uses_uvicorn() {
        let (program, args, env) = backend_command(None, "deadbeef", None);
        assert_eq!(program, "python");
        assert_eq!(
            args,
            vec![
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "7000"
            ]
        );
        assert!(env
            .iter()
            .any(|(k, v)| k == "PRISM_TOKEN" && v == "deadbeef"));
        assert!(env.iter().any(|(k, v)| k == "AUTH_ENABLED" && v == "false"));
        assert!(env
            .iter()
            .any(|(k, v)| k == "LOCALHOST_BYPASS" && v == "false"));
        assert!(env
            .iter()
            .any(|(k, v)| k == "PRISMA_DESKTOP_MODE" && v == "true"));
        assert!(!env.iter().any(|(k, _)| k == "PRISM_DATA_DIR"));
    }

    #[test]
    fn backend_command_bundled_uses_exe_and_data_dir() {
        let bin = PathBuf::from("/opt/prism-server/prism-server.exe");
        let (program, args, env) = backend_command(Some(&bin), "tok", Some("/data/Prism"));
        assert!(program.ends_with("prism-server.exe"));
        assert_eq!(args, vec!["--host", "127.0.0.1", "--port", "7000"]);
        assert!(env.iter().any(|(k, v)| k == "PRISM_TOKEN" && v == "tok"));
        assert!(env
            .iter()
            .any(|(k, v)| k == "PRISM_DATA_DIR" && v == "/data/Prism"));
    }
}
