//! Persisted thin-client config: the backend URL the app connects to.
use std::path::{Path, PathBuf};

/// %APPDATA%\Prism\client.json (falls back to the temp dir if APPDATA is unset).
pub fn config_path() -> PathBuf {
    let base = std::env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir());
    base.join("Prism").join("client.json")
}

pub fn load_url(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    let url = v.get("url")?.as_str()?.trim().to_string();
    if url.is_empty() {
        None
    } else {
        Some(url)
    }
}

/// Optional client-mode override. `false` is used when a loopback URL points
/// at an SSH tunnel to the Spark rather than a backend spawned on this PC.
pub fn load_spawn_sidecar(path: &Path) -> Option<bool> {
    let text = std::fs::read_to_string(path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get("spawn_sidecar")?.as_bool()
}

pub fn save_url(path: &Path, url: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let body = serde_json::json!({ "url": url }).to_string();
    std::fs::write(path, body)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_then_load_roundtrips() {
        let dir = std::env::temp_dir().join(format!("prism-cfg-{}", std::process::id()));
        let path = dir.join("client.json");
        save_url(&path, "http://192.168.1.20:7000").unwrap();
        assert_eq!(load_url(&path).as_deref(), Some("http://192.168.1.20:7000"));
        assert_eq!(load_spawn_sidecar(&path), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tunnel_mode_disables_sidecar() {
        let dir = std::env::temp_dir().join(format!("prism-tunnel-{}", std::process::id()));
        let path = dir.join("client.json");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            &path,
            r#"{"url":"http://127.0.0.1:7000","spawn_sidecar":false}"#,
        )
        .unwrap();
        assert_eq!(load_url(&path).as_deref(), Some("http://127.0.0.1:7000"));
        assert_eq!(load_spawn_sidecar(&path), Some(false));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_missing_is_none() {
        let path = std::env::temp_dir().join("prism-nope").join("client.json");
        assert_eq!(load_url(&path), None);
    }

    #[test]
    fn load_blank_url_is_none() {
        let dir = std::env::temp_dir().join(format!("prism-blank-{}", std::process::id()));
        let path = dir.join("client.json");
        save_url(&path, "   ").unwrap();
        assert_eq!(load_url(&path), None);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
