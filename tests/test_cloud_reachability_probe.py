"""Unit tests for src.provider_diagnostics._probe_llm_endpoint reachability
semantics (CR-02 review-fix).

Every cloud LLM provider in PROVIDER_INFO has needs_key=True, so an
unauthenticated probe against a real, correctly-configured, fully-reachable
endpoint routinely gets a 401/403 auth-challenge response -- that response
still proves the network path/TLS/DNS resolved. This pins the fix: ANY
completed HTTP round-trip (including 401/403/4xx/5xx) is `reachable: True`,
distinct from "authenticated successfully" (no live-auth/billable call is
ever made here); only a genuine connection-level failure (timeout, DNS
failure, connection refused -- surfaced as status_code=None) stays
`reachable: False`.

Stubs routes.model_routes._ping_endpoint -- no real network call.

`routes.model_routes` is imported freshly INSIDE each test (not once at
module-collection time): sibling assembled-app tests
(tests/test_diagnostics_integrations_assembled.py) rebuild the app and pop+
reimport every `routes.*` module during their fixture, which would leave a
module-collection-time `import routes.model_routes as model_routes` binding
to a stale module object -- monkeypatching that stale object would silently
no-op and this file's `_probe_llm_endpoint(...)` calls would hit the real
(un-stubbed) `_ping_endpoint`, making a real network call. Importing inside
each test always resolves against the CURRENT `sys.modules` entry.
"""
from src.provider_diagnostics import _probe_llm_endpoint


def test_401_response_is_reachable(monkeypatch):
    """The expected response from a real, unauthenticated cloud API call
    must be treated as reachable, not folded into 'unreachable' alongside
    a hard connection failure."""
    import routes.model_routes as model_routes

    monkeypatch.setattr(
        model_routes, "_ping_endpoint",
        lambda base_url, timeout=3.0: {"reachable": False, "status_code": 401, "error": "HTTP 401"},
    )
    result = _probe_llm_endpoint("https://api.anthropic.com/v1")
    assert result["reachable"] is True
    assert result["status_code"] == 401


def test_403_response_is_reachable(monkeypatch):
    import routes.model_routes as model_routes

    monkeypatch.setattr(
        model_routes, "_ping_endpoint",
        lambda base_url, timeout=3.0: {"reachable": False, "status_code": 403, "error": "HTTP 403"},
    )
    result = _probe_llm_endpoint("https://api.anthropic.com/v1")
    assert result["reachable"] is True
    assert result["status_code"] == 403


def test_connection_failure_is_unreachable(monkeypatch):
    """A genuine connection-level failure (no HTTP response at all) must
    stay reachable: False."""
    import routes.model_routes as model_routes

    monkeypatch.setattr(
        model_routes, "_ping_endpoint",
        lambda base_url, timeout=3.0: {"reachable": False, "status_code": None, "error": "Connection refused"},
    )
    result = _probe_llm_endpoint("https://api.anthropic.com/v1")
    assert result["reachable"] is False
    assert result["status_code"] is None


def test_2xx_response_stays_reachable(monkeypatch):
    import routes.model_routes as model_routes

    monkeypatch.setattr(
        model_routes, "_ping_endpoint",
        lambda base_url, timeout=3.0: {"reachable": True, "status_code": 200, "error": None},
    )
    result = _probe_llm_endpoint("https://api.anthropic.com/v1")
    assert result["reachable"] is True
    assert result["status_code"] == 200
    assert result["error"] is None
