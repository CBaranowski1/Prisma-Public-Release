#[derive(Debug, Clone)]
pub struct PrismConfig {
    pub url: String,
    pub spawn_sidecar: bool,
}

// The packaged desktop application is a thin client for the private Spark
// tunnel. `localhost` resolves to IPv6 loopback first on Windows and is also a
// valid, narrowly-scoped Tauri remote-capability origin (the URLPattern parser
// used by Tauri currently rejects bracketed IPv6 literals).
const DEFAULT_URL: &str = "http://localhost:7000";

/// True when `url`'s host is loopback (so we should spawn a local sidecar).
pub fn is_local_url(url: &str) -> bool {
    // Strip scheme.
    let after_scheme = url.split("://").nth(1).unwrap_or(url);
    // Host is up to the first '/', and we trim a trailing ':port'.
    let host_port = after_scheme.split('/').next().unwrap_or(after_scheme);
    let host = if let Some(stripped) = host_port.strip_prefix('[') {
        // IPv6 literal: [::1]:7000
        stripped.split(']').next().unwrap_or(stripped)
    } else {
        host_port
            .rsplit_once(':')
            .map(|(h, _)| h)
            .unwrap_or(host_port)
    };
    matches!(host, "localhost" | "127.0.0.1" | "::1")
}

/// Parse + validate the backend URL the main window should load. Must be an
/// http(s) URL with a host; anything else is a configuration error.
pub fn backend_window_url(raw: &str) -> Result<url::Url, String> {
    let parsed = url::Url::parse(raw.trim()).map_err(|e| format!("invalid URL {raw:?}: {e}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(format!("URL must be http/https, got {:?}", parsed.scheme()));
    }
    if parsed.host_str().map(|h| h.is_empty()).unwrap_or(true) {
        return Err(format!("URL has no host: {raw:?}"));
    }
    Ok(parsed)
}

/// Resolve effective config. URL precedence: env var > config file > built-in
/// default. `spawn_sidecar_override` lets a desktop client use a loopback SSH
/// tunnel without accidentally starting a second local Prisma backend.
pub fn resolve_config(
    env_url: Option<String>,
    conf_file_url: Option<String>,
    spawn_sidecar_override: Option<bool>,
) -> PrismConfig {
    let env_url = env_url.filter(|s| !s.trim().is_empty());
    let conf_file_url = conf_file_url.filter(|s| !s.trim().is_empty());
    let has_explicit_url = env_url.is_some() || conf_file_url.is_some();
    let url = env_url
        .or(conf_file_url)
        .unwrap_or_else(|| DEFAULT_URL.to_string());
    // A packaged install does not contain the development Python environment.
    // With no explicit configuration, open the standard Spark tunnel and never
    // attempt `python -m uvicorn`. Developers can opt into the sidecar with
    // PRISM_URL plus PRISM_SPAWN_SIDECAR=true.
    let spawn_sidecar =
        spawn_sidecar_override.unwrap_or_else(|| has_explicit_url && is_local_url(&url));
    PrismConfig { url, spawn_sidecar }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unset_defaults_to_private_spark_tunnel_without_spawning() {
        let c = resolve_config(None, None, None);
        assert_eq!(c.url, "http://localhost:7000");
        assert!(!c.spawn_sidecar);
    }

    #[test]
    fn env_url_overrides_conf_file() {
        let c = resolve_config(
            Some("http://127.0.0.1:9000".into()),
            Some("http://x:1".into()),
            None,
        );
        assert_eq!(c.url, "http://127.0.0.1:9000");
        assert!(c.spawn_sidecar);
    }

    #[test]
    fn conf_file_used_when_env_absent() {
        let c = resolve_config(None, Some("http://127.0.0.1:9000".into()), None);
        assert_eq!(c.url, "http://127.0.0.1:9000");
        assert!(c.spawn_sidecar);
    }

    #[test]
    fn remote_host_is_central_mode_no_sidecar() {
        let c = resolve_config(Some("https://prism.company.internal".into()), None, None);
        assert_eq!(c.url, "https://prism.company.internal");
        assert!(!c.spawn_sidecar);
    }

    #[test]
    fn loopback_tunnel_can_disable_local_sidecar() {
        let c = resolve_config(Some("http://127.0.0.1:7000".into()), None, Some(false));
        assert_eq!(c.url, "http://127.0.0.1:7000");
        assert!(!c.spawn_sidecar);
    }

    #[test]
    fn localhost_variants_are_local() {
        assert!(is_local_url("http://localhost:7000"));
        assert!(is_local_url("http://127.0.0.1:7000"));
        assert!(is_local_url("http://[::1]:7000"));
        assert!(!is_local_url("https://prism.company.internal"));
        assert!(!is_local_url("http://192.168.1.20:7000"));
    }

    #[test]
    fn backend_window_url_accepts_http_and_https() {
        assert_eq!(
            backend_window_url("http://127.0.0.1:7000")
                .unwrap()
                .as_str(),
            "http://127.0.0.1:7000/"
        );
        assert!(backend_window_url("https://prism.company.internal").is_ok());
    }

    #[test]
    fn backend_window_url_rejects_garbage() {
        assert!(backend_window_url("not a url").is_err());
        assert!(backend_window_url("ftp://x").is_err());
        assert!(backend_window_url("http://").is_err());
    }
}
