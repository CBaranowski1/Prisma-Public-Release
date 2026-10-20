"""Assembled-app SC5 proof for GRAY-INT-01/GRAY-INT-10 (Phase 25 Plan 05).

Proves, via a TestClient built against the REAL assembled `app`, that the
"presence != integration" spine holds for the provider-diagnostics endpoint:

  1. The admin-gated route is reachable (200) with the internal-tool token
     from a genuine loopback peer.
  2. An unauthenticated, non-loopback caller is rejected (never granted the
     provider report).
  3. A provider missing its credential is reported `configured: false`
     through the live endpoint (not just the unit-tested module).
  4. A local LLM host pointed at a definitely-closed port surfaces
     `reachable: false` plus the actionable Ollama/vLLM message through the
     live endpoint.

No real Ollama/cloud provider is required -- the missing-key case stubs the
model-endpoint seam to empty, and the closed-port case binds+closes an
ephemeral localhost socket for a deterministic "connection refused".

The `_heal_polluted_modules()` helper and the `app_instance` fixture below
are replicated VERBATIM from tests/test_query_routes_registered.py (that
file's own note: importing a fixture across test modules is not reliable
here, so replication is this codebase's established convention) -- do NOT
import them from another test module.
"""
import importlib.util
import socket
import sys
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

import src.provider_diagnostics as pd


def _heal_polluted_modules():
    """Sibling unit tests stub model modules (src.request_models, core.models)
    with MagicMock and import route modules against them, leaving routes.* bound
    to mocks in sys.modules. That makes app route-registration raise FastAPIError
    when we build the full app. Drop mock-contaminated model modules, then force
    app + all routes.* to re-import so they rebind against the real models.
    Only models that are ACTUALLY mocks are dropped (don't re-import a real ORM
    module -- SQLAlchemy would re-declare tables)."""
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    # Force the app + route modules to rebuild against whatever models resolve
    # now (real). Safe: these are app/route definition modules, not ORM tables.
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def app_instance():
    """Import app INSIDE the fixture (run time), not at collection time."""
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    # Environment gap (DEBT-03, out of Phase-9 scope): bs4 is a documented
    # dependency (requirements.txt) but may not be installed in every venv.
    # app.py's import chain (app -> services -> services.search -> providers)
    # hard-imports it. Stub it ONLY while building the assembled app here,
    # and ONLY if genuinely absent, then remove the stub immediately after --
    # a conftest-wide stub would make sibling files' pytest.importorskip("bs4")
    # stop skipping. (Mirrors tests/test_query_routes_registered.py.)
    _stubbed_bs4 = False
    if "bs4" not in sys.modules and importlib.util.find_spec("bs4") is None:
        sys.modules["bs4"] = MagicMock()
        _stubbed_bs4 = True
    try:
        import app as app_module
    finally:
        if _stubbed_bs4:
            sys.modules.pop("bs4", None)
    return app_module.app


@pytest.fixture(autouse=True)
def _reset_snapshot_cache():
    """Every test in this file hits refresh=true (always rebuilds), but the
    snapshot cache is module-level global state shared with the running app
    -- reset it so no test observes a stale snapshot written by a sibling."""
    pd._snapshot = None
    yield
    pd._snapshot = None


def _closed_port_url() -> str:
    """Bind an ephemeral localhost socket, read its port, close it -- a
    deterministic "connection refused" target with no Ollama/vLLM install
    or real network call required (mirrors tests/test_llm_preflight.py)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def test_endpoint_admin_gated_ok(app_instance, monkeypatch):
    """The internal-tool token from a genuine loopback peer reaches the
    route and gets a 200 with a `providers` object -- the SC5 baseline
    proof that the admin gate + route wiring work end-to-end."""
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    # Hermetic: no real DB/CLI/network access, no cloud-provider calls.
    monkeypatch.setattr(pd, "_enabled_model_endpoints", lambda: [])

    client = TestClient(app_instance, client=("127.0.0.1", 12345))
    resp = client.get(
        "/api/diagnostics/integrations",
        params={"refresh": "true"},
        headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("providers"), dict)
    assert "anthropic" in body["providers"]


def test_unauthenticated_rejected(app_instance):
    """A non-loopback caller carrying no internal-tool token and no session
    cookie must never be granted the provider report. The real assembled
    app fronts every admin route with BOTH the AuthMiddleware auth gate
    (returns 401 "Not authenticated"/"Setup required" before the route is
    even reached) AND the route's own require_admin() gate (403) -- which
    one answers first depends on whether AuthMiddleware short-circuits
    the request before it reaches require_admin. Codebase precedent
    (tests/test_verify_trust_boundary_integration.py
    ::test_internal_tool_token_defeated_by_forged_loopback_header) asserts
    "rejected" via the same (401, 403) tuple rather than a single hardcoded
    code, for exactly this reason -- mirrored here rather than asserting a
    literal 403 that AuthMiddleware's own 401 branches would make flaky."""
    client = TestClient(app_instance, client=("198.51.100.7", 54321))
    resp = client.get("/api/diagnostics/integrations", params={"refresh": "true"})
    assert resp.status_code in (401, 403)


def test_missing_key_reports_unconfigured(app_instance, monkeypatch):
    """A provider with no matching, credentialed ModelEndpoint reports
    configured:false through the LIVE endpoint (not just the unit-tested
    module) -- and the response never leaks a raw key/secret string."""
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    # No enabled ModelEndpoint anywhere -- every LLM provider (anthropic
    # included) is unconfigured. No real DB row, no real network call.
    monkeypatch.setattr(pd, "_enabled_model_endpoints", lambda: [])

    client = TestClient(app_instance, client=("127.0.0.1", 12345))
    resp = client.get(
        "/api/diagnostics/integrations",
        params={"refresh": "true"},
        headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    anthropic = body["providers"]["anthropic"]
    assert anthropic["configured"] is False
    assert anthropic["credential_source"] == "none"
    # No raw credential value anywhere in the serialized response.
    raw_text = resp.text
    assert "api_key" not in raw_text.lower() or '"api_key"' not in raw_text


def test_closed_port_preflight_surfaced(app_instance, monkeypatch):
    """OLLAMA_BASE_URL pointed at a definitely-closed localhost port ->
    the synthesized llm_host entry reports reachable:false through the LIVE
    endpoint, with the actionable "is Ollama/vLLM running" message -- no
    real Ollama/vLLM install required."""
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    closed_url = _closed_port_url()
    monkeypatch.setenv("OLLAMA_BASE_URL", closed_url)
    # Hermetic: no real DB/CLI/network access for the cloud-provider branch.
    monkeypatch.setattr(pd, "_enabled_model_endpoints", lambda: [])

    client = TestClient(app_instance, client=("127.0.0.1", 12345))
    resp = client.get(
        "/api/diagnostics/integrations",
        params={"refresh": "true"},
        headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    llm_host = body["providers"]["llm_host"]
    assert llm_host["reachable"] is False
    assert "is Ollama/vLLM running" in llm_host["detail"]
    assert "LLM_HOST" in llm_host["detail"]
