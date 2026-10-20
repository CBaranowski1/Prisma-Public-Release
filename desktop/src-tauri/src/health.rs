use std::time::Duration;

pub trait HealthProbe {
    fn is_healthy(&self, url: &str) -> bool;
}

/// Real probe: a 200 from `{url}/api/health` means healthy.
pub struct ReqwestProbe {
    client: reqwest::blocking::Client,
}

impl ReqwestProbe {
    pub fn new() -> Self {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_millis(400))
            .build()
            .expect("build reqwest client");
        Self { client }
    }
}

impl Default for ReqwestProbe {
    fn default() -> Self {
        Self::new()
    }
}

impl HealthProbe for ReqwestProbe {
    fn is_healthy(&self, url: &str) -> bool {
        let endpoint = format!("{}/api/health", url.trim_end_matches('/'));
        matches!(self.client.get(&endpoint).send(), Ok(r) if r.status().is_success())
    }
}

/// Poll until healthy or `attempts` exhausted. Sleeps `interval` between tries
/// using the injected `sleep` fn (real impl passes `std::thread::sleep`).
pub fn wait_for_health(
    probe: &dyn HealthProbe,
    url: &str,
    attempts: u32,
    interval: Duration,
    sleep: &dyn Fn(Duration),
) -> bool {
    for i in 0..attempts {
        if probe.is_healthy(url) {
            return true;
        }
        if i + 1 < attempts {
            sleep(interval);
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    struct Fake {
        healthy_after: u32,
        calls: Cell<u32>,
    }
    impl HealthProbe for Fake {
        fn is_healthy(&self, _url: &str) -> bool {
            let n = self.calls.get();
            self.calls.set(n + 1);
            n + 1 >= self.healthy_after
        }
    }

    #[test]
    fn returns_true_once_healthy() {
        let probe = Fake {
            healthy_after: 3,
            calls: Cell::new(0),
        };
        let ok = wait_for_health(&probe, "http://x", 60, Duration::from_millis(500), &|_| {});
        assert!(ok);
        assert_eq!(probe.calls.get(), 3);
    }

    #[test]
    fn gives_up_after_attempts() {
        let probe = Fake {
            healthy_after: 999,
            calls: Cell::new(0),
        };
        let ok = wait_for_health(&probe, "http://x", 5, Duration::from_millis(500), &|_| {});
        assert!(!ok);
        assert_eq!(probe.calls.get(), 5);
    }
}
