import httpx

from src import model_discovery


def test_parse_tailscale_status_rejects_wrong_shapes():
    assert model_discovery._parse_tailscale_status("{bad") == {}
    assert model_discovery._parse_tailscale_status("[]") == {}
    assert model_discovery._parse_tailscale_status('{"Self": {}}') == {"Self": {}}


def test_discovery_ignores_invalid_peer_rows(monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"Self":{"TailscaleIPs":["100.1.1.1"]},"Peer":{"bad":"row","ok":{"Online":true,"HostName":"box","OS":"linux","TailscaleIPs":["100.1.1.2"]}}}'

    monkeypatch.setattr(model_discovery.subprocess, "run", lambda *a, **k: Result())
    model_discovery._hosts_cache = []
    model_discovery._hosts_cache_time = 0

    assert model_discovery.discover_tailscale_hosts() == ["100.1.1.1", "100.1.1.2"]


def test_discovery_ignores_invalid_tailscale_ip_shapes(monkeypatch):
    class Result:
        returncode = 0
        stdout = (
            '{"Self":{"TailscaleIPs":"100.1.1.1"},'
            '"Peer":{'
            '"string_ips":{"Online":true,"HostName":"bad","OS":"linux","TailscaleIPs":"100.1.1.2"},'
            '"mixed_ips":{"Online":true,"HostName":"ok","OS":"linux","TailscaleIPs":[null,123,"100.1.1.3"]}'
            '}}'
        )

    monkeypatch.setattr(model_discovery.subprocess, "run", lambda *a, **k: Result())
    model_discovery._hosts_cache = []
    model_discovery._hosts_cache_time = 0

    assert model_discovery.discover_tailscale_hosts() == ["100.1.1.3"]


def _clear_llm_env(monkeypatch):
    for env_name in ("LLM_HOSTS", "OLLAMA_BASE_URL", "OLLAMA_URL", "LM_STUDIO_URL"):
        monkeypatch.delenv(env_name, raising=False)


def test_scan_gate(monkeypatch):
    """SEC-16: with model_discovery_scan_enabled at its False default, _get_hosts
    must NOT call discover_tailscale_hosts (no Tailscale sweep) and must return
    only the configured/default host list."""
    _clear_llm_env(monkeypatch)
    # Force the gate off regardless of any on-disk settings.json state (once
    # src/model_discovery.py imports get_setting, this patches the bound name
    # in THIS module — same name-binding trap as seat_runner.untrusted_context_message).
    monkeypatch.setattr(
        model_discovery, "get_setting",
        lambda key, default=None: False, raising=False,
    )

    call_count = {"n": 0}

    def _spy():
        call_count["n"] += 1
        return ["100.9.9.9"]

    monkeypatch.setattr(model_discovery, "discover_tailscale_hosts", _spy)

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1")
    hosts = md._get_hosts()

    assert call_count["n"] == 0, "discover_tailscale_hosts must not be called with the scan gate off"
    assert hosts == ["127.0.0.1", "host.docker.internal"]
    assert "100.9.9.9" not in hosts


def test_llm_hosts_override_with_gate_off(monkeypatch):
    """Pitfall 4 regression guard: an explicit LLM_HOSTS override must still be
    honored even though model_discovery_scan_enabled is at its False default."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_HOSTS", "192.168.1.50")
    monkeypatch.setattr(
        model_discovery, "get_setting",
        lambda key, default=None: False, raising=False,
    )

    call_count = {"n": 0}

    def _spy():
        call_count["n"] += 1
        return ["100.9.9.9"]

    monkeypatch.setattr(model_discovery, "discover_tailscale_hosts", _spy)

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1")
    hosts = md._get_hosts()

    assert "192.168.1.50" in hosts
    assert call_count["n"] == 0, "explicit LLM_HOSTS override must never trigger the Tailscale sweep"


def test_scan_enabled_reaches_tailscale_and_caps_workers(monkeypatch):
    """When the setting is ON, the Tailscale sweep IS reachable and the
    discover_models executor is concurrency-capped (max_workers <= 10)."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setattr(
        model_discovery, "get_setting",
        lambda key, default=None: (
            True if key == "model_discovery_scan_enabled" else default
        ),
        raising=False,
    )

    call_count = {"n": 0}

    def _spy():
        call_count["n"] += 1
        return ["100.2.2.2"]

    monkeypatch.setattr(model_discovery, "discover_tailscale_hosts", _spy)

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1")
    hosts = md._get_hosts()

    assert call_count["n"] == 1
    assert "100.2.2.2" in hosts

    real_executor = model_discovery.ThreadPoolExecutor
    captured = {}

    class SpyExecutor(real_executor):
        def __init__(self, max_workers=None, *a, **k):
            captured["max_workers"] = max_workers
            super().__init__(max_workers=max_workers, *a, **k)

    monkeypatch.setattr(model_discovery, "ThreadPoolExecutor", SpyExecutor)
    monkeypatch.setattr(model_discovery.ModelDiscovery, "_check_port", lambda self, h, p: None)

    md.discover_models()

    assert captured["max_workers"] is not None
    assert captured["max_workers"] <= 10


def test_scan_enabled_respects_allowed_hosts_allowlist(monkeypatch):
    """When the setting is ON and model_discovery_allowed_hosts is non-empty,
    discover_models only probes allowlisted hosts."""
    _clear_llm_env(monkeypatch)

    def _fake_get_setting(key, default=None):
        if key == "model_discovery_scan_enabled":
            return True
        if key == "model_discovery_allowed_hosts":
            return ["100.9.9.9"]
        return default

    monkeypatch.setattr(model_discovery, "get_setting", _fake_get_setting, raising=False)
    monkeypatch.setattr(
        model_discovery, "discover_tailscale_hosts",
        lambda: ["100.9.9.9", "100.1.1.1"],
    )

    seen_hosts = set()

    def _fake_check_port(self, h, p):
        seen_hosts.add(h)
        return None

    monkeypatch.setattr(model_discovery.ModelDiscovery, "_check_port", _fake_check_port)

    md = model_discovery.ModelDiscovery(default_host="100.1.1.1")
    md.discover_models()

    assert seen_hosts == {"100.9.9.9"}


class _FakeResponse:
    """Minimal httpx.Response stand-in for hermetic mocking (no real network)."""

    def __init__(self, json_body, status_code=200, is_success=True):
        self._json_body = json_body
        self.status_code = status_code
        self.is_success = is_success

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if not self.is_success:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self,
            )


def test_check_port_rejects_nondict_shape(monkeypatch, caplog):
    """A bare-list /v1/models body must be shape-rejected (logged distinctly),
    not silently swallowed as if the host were unreachable."""
    monkeypatch.setattr(
        model_discovery.httpx, "get",
        lambda *a, **k: _FakeResponse([{"id": "x"}]),
    )

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1")
    with caplog.at_level("WARNING"):
        result = md._check_port("127.0.0.1", 8000)

    assert result is None
    assert any("non-dict" in rec.message for rec in caplog.records)


def test_get_providers_uses_live_openai_list(monkeypatch):
    """When the live /v1/models fetch succeeds, the openai provider's models
    come from the live list, not the hardcoded fallback."""
    monkeypatch.setattr(
        model_discovery, "_fetch_openai_models",
        lambda api_key, **k: ["live-model-a", "live-model-b"],
    )
    monkeypatch.setattr(
        model_discovery.ModelDiscovery, "discover_models",
        lambda self: {"hosts": [], "items": []},
    )

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1", openai_api_key="sk-test")
    result = md.get_providers()

    openai_provider = next(p for p in result["providers"] if p["provider"] == "openai")
    assert openai_provider["items"][0]["models"] == ["live-model-a", "live-model-b"]
    assert result["providers"][0]["provider"] == "vllm"


def test_get_providers_falls_back_when_live_fetch_fails(monkeypatch):
    """When the live /v1/models fetch fails (returns None), the openai provider
    falls back to the curated fallback list."""
    monkeypatch.setattr(
        model_discovery, "_fetch_openai_models",
        lambda api_key, **k: None,
    )
    monkeypatch.setattr(
        model_discovery.ModelDiscovery, "discover_models",
        lambda self: {"hosts": [], "items": []},
    )

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1", openai_api_key="sk-test")
    result = md.get_providers()

    openai_provider = next(p for p in result["providers"] if p["provider"] == "openai")
    assert openai_provider["items"][0]["models"] == model_discovery._FALLBACK_OPENAI_MODELS


def test_discover_models_none_found_is_structured_and_additive(monkeypatch):
    """On a fresh install with no reachable models and no API key, discover_models()
    must return an additive status='none_found' + remediation, while still keeping
    hosts/items present (Pitfall 2: additive only)."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setattr(
        model_discovery, "get_setting",
        lambda key, default=None: False, raising=False,
    )
    monkeypatch.setattr(
        model_discovery.ModelDiscovery, "_check_port",
        lambda self, h, p: None,
    )

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1")
    result = md.discover_models()

    assert result["status"] == "none_found"
    assert result["remediation"]
    assert "hosts" in result
    assert result["items"] == []
