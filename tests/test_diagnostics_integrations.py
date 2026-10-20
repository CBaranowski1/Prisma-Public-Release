"""Unit tests for src/provider_diagnostics.py (GRAY-INT-01): per-provider
configured/reachable resolution, snapshot secret-safety, and the
run_probe()/get_cached_snapshot() cache lifecycle.

All DB/CLI/network access is monkeypatched via the module's seams
(_enabled_model_endpoints, _email_configured, _calendar_configured_count,
_vault_cli_present, _probe_llm_endpoint, preflight_llm_host) -- no live DB,
no live CLI, no real network call.
"""
import json
from types import SimpleNamespace

import pytest

import src.provider_diagnostics as pd


@pytest.fixture(autouse=True)
def _stub_seams(monkeypatch):
    """Stub every side-channel seam to a safe, deterministic default so
    build_provider_snapshot() never touches a live DB, live CLI, the local
    prefs file, or the network. Individual tests override the seam(s) they
    exercise."""
    monkeypatch.setattr(pd, "_enabled_model_endpoints", lambda: [])
    monkeypatch.setattr(pd, "_email_configured", lambda: (False, "none"))
    monkeypatch.setattr(pd, "_calendar_configured_count", lambda: 0)
    monkeypatch.setattr(pd, "_vault_cli_present", lambda: False)
    monkeypatch.setattr(
        pd, "_probe_llm_endpoint",
        lambda base_url, timeout=3.0: {"reachable": False, "error": "stub"},
    )
    monkeypatch.setattr(
        pd, "preflight_llm_host",
        lambda base_url, timeout=3.0: {"reachable": False, "error": "stub"},
    )
    pd._snapshot = None
    yield
    pd._snapshot = None


def test_llm_provider_configured_from_db(monkeypatch):
    endpoint = SimpleNamespace(base_url="https://api.anthropic.com/v1", api_key="SECRET123")
    monkeypatch.setattr(pd, "_enabled_model_endpoints", lambda: [endpoint])
    snap = pd.build_provider_snapshot()
    assert snap["providers"]["anthropic"]["configured"] is True
    assert snap["providers"]["anthropic"]["credential_source"] == "db"


def test_llm_provider_unconfigured():
    snap = pd.build_provider_snapshot()
    assert snap["providers"]["anthropic"]["configured"] is False
    assert snap["providers"]["anthropic"]["credential_source"] == "none"
    assert snap["providers"]["anthropic"]["reachable"] is None


def test_email_configured_via_env(monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "user@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "hunter2")
    monkeypatch.setattr(pd, "_email_configured", lambda: (True, "env"))
    snap = pd.build_provider_snapshot()
    assert snap["providers"]["email"]["configured"] is True
    assert snap["providers"]["email"]["credential_source"] == "env"
    assert snap["providers"]["email"]["reachable"] is None


def test_vault_absent_is_not_error():
    snap = pd.build_provider_snapshot()
    assert snap["providers"]["vault"]["configured"] is False
    assert snap["providers"]["vault"]["reachable"] is None


def test_snapshot_never_contains_raw_key(monkeypatch):
    endpoint = SimpleNamespace(base_url="https://api.anthropic.com/v1", api_key="SECRET123")
    monkeypatch.setattr(pd, "_enabled_model_endpoints", lambda: [endpoint])
    snap = pd.build_provider_snapshot()
    assert "SECRET123" not in json.dumps(snap)


def test_snapshot_has_all_provider_keys_plus_llm_host():
    snap = pd.build_provider_snapshot()
    expected = set(pd.PROVIDER_INFO.keys()) | {"llm_host"}
    assert set(snap["providers"].keys()) == expected
    assert len(pd.PROVIDER_INFO) == 8


async def test_cache_lifecycle():
    assert pd.get_cached_snapshot() is None
    snap = await pd.run_probe()
    assert "providers" in snap
    cached = pd.get_cached_snapshot()
    assert cached is not None
    assert "providers" in cached


def test_llm_host_unconfigured_when_no_env_set(monkeypatch):
    """IN-03: with no OLLAMA_BASE_URL/LLM_HOST/LLM_HOSTS set, llm_host must
    report configured:false (not-applicable-to-this-deployment) rather than
    always-true, which previously read as an active misconfiguration for
    pure-cloud-provider deployments."""
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_HOST", raising=False)
    snap = pd.build_provider_snapshot()
    assert snap["providers"]["llm_host"]["configured"] is False


def test_llm_host_configured_when_ollama_base_url_set(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    snap = pd.build_provider_snapshot()
    assert snap["providers"]["llm_host"]["configured"] is True


def test_dgx_host_probe_uses_env_credential_without_exposing_it(monkeypatch):
    calls = []

    def _preflight(base_url, timeout=3.0, api_key=None):
        calls.append((base_url, api_key))
        return {"reachable": True, "error": None}

    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_HOST", raising=False)
    monkeypatch.setenv("DGX_SPARK_BASE_URL", "https://spark.internal:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    monkeypatch.setenv("DGX_SPARK_MODEL", "spark-chat")
    monkeypatch.setattr(pd, "preflight_llm_host", _preflight)

    snap = pd.build_provider_snapshot()
    llm_host = snap["providers"]["llm_host"]

    assert calls[0] == ("https://spark.internal:8000/v1", "spark-secret")
    assert llm_host["configured"] is True
    assert llm_host["reachable"] is True
    assert llm_host["credential_source"] == "env"
    assert "spark-secret" not in json.dumps(snap)


def test_llm_provider_reachable_with_401_auth_challenge(monkeypatch):
    """CR-02: a stubbed 401 (the auth-challenge a real, correctly-configured
    cloud provider returns to an unauthenticated probe) must report
    reachable:true through the snapshot builder -- network path confirmed,
    credential not verified, NOT the same as a connection failure."""
    endpoint = SimpleNamespace(base_url="https://api.anthropic.com/v1", api_key="SECRET123")
    monkeypatch.setattr(pd, "_enabled_model_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        pd, "_probe_llm_endpoint",
        lambda base_url, timeout=3.0: {
            "reachable": True,
            "status_code": 401,
            "error": "responded with HTTP 401 — network path OK, credential not verified (no live-auth call made)",
        },
    )
    snap = pd.build_provider_snapshot()
    anthropic = snap["providers"]["anthropic"]
    assert anthropic["configured"] is True
    assert anthropic["reachable"] is True
    assert "401" in anthropic["detail"]
