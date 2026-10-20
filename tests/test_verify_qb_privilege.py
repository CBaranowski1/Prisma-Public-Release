"""VERIFY-03: prove `can_manage_data_sources` is genuinely enforced on the
query-builder source/relationship MANAGEMENT routes.

`require_privilege` (src/auth_helpers.py:91-116) is FAIL-OPEN: a missing
privilege key defaults to permitted (`privs.get(key, True)`). Every deny
assertion in this file supplies an EXPLICIT ``False`` value for
``can_manage_data_sources`` -- never a missing key -- or the 403 path would
never fire and the test would prove nothing (T-09-04).

Two halves:
  1. Direct-endpoint (this section) -- resolves the route's endpoint
     function and calls it with a fake ``request`` SimpleNamespace, mirroring
     the repo's established idiom in tests/test_datasource_routes.py.
  2. Assembled-app TestClient (below) -- drives the routes through the real,
     assembled FastAPI ``app`` object per D-01, with a test-only auth shim
     confined to this file (T-09-03): it monkeypatches the live
     ``auth_manager`` object the middleware and routes both resolve, and
     auto-reverts at teardown. It is never written into shipped source.
"""
import contextlib
import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

import routes.datasource_routes as ds_mod
import routes.relationship_routes as rel_mod


def _req(user="alice", *, priv_ok):
    """Request shim carrying an auth_manager whose get_privileges returns an
    EXPLICIT can_manage_data_sources value (never a missing key -- see
    module docstring on the fail-open gate)."""
    privs = {"can_manage_data_sources": priv_ok}
    auth_mgr = SimpleNamespace(get_privileges=lambda u: privs, is_configured=True,
                               is_admin=lambda u: False)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.9"),
    )


def _handler(router_factory, method, fragment):
    for r in router_factory().routes:
        if fragment in getattr(r, "path", "") and method in (getattr(r, "methods", None) or set()):
            return r.endpoint
    raise KeyError(fragment)


@contextlib.contextmanager
def _db_ctx(session):
    yield session


# ---------------------------------------------------------------------------
# Direct-endpoint half -- datasource management route
# ---------------------------------------------------------------------------

def test_datasource_list_denies_explicit_false_privilege():
    list_sources = _handler(ds_mod.setup_datasource_routes, "GET", "/datasources")
    with pytest.raises(HTTPException) as exc:
        list_sources(request=_req("bob", priv_ok=False))
    assert exc.value.status_code == 403


def test_datasource_list_allows_true_privilege(monkeypatch):
    session = MagicMock()
    session.query.return_value.all.return_value = []
    monkeypatch.setattr(ds_mod, "get_db_session", lambda: _db_ctx(session))
    list_sources = _handler(ds_mod.setup_datasource_routes, "GET", "/datasources")
    result = list_sources(request=_req("alice", priv_ok=True))
    assert result == []


# ---------------------------------------------------------------------------
# Direct-endpoint half -- relationship management route
# ---------------------------------------------------------------------------

def _relationship_body():
    # Real allowlisted internal tables/fields (sessions.id, chat_messages.session_id)
    # so this resolves against the D-13 create-time catalog validation (24-05) --
    # this file only exercises the privilege gate, not catalog validation itself.
    return rel_mod.RelationshipCreate(
        name="session_to_messages", kind="one_to_many",
        left_table_id="sessions", left_field="id",
        right_table_id="chat_messages", right_field="session_id",
    )


def test_relationship_create_denies_explicit_false_privilege():
    create_relationship = _handler(rel_mod.setup_relationship_routes, "POST", "/relationships")
    with pytest.raises(HTTPException) as exc:
        create_relationship(request=_req("bob", priv_ok=False), body=_relationship_body())
    assert exc.value.status_code == 403


def test_relationship_create_allows_true_privilege(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(rel_mod, "get_db_session", lambda: _db_ctx(session))
    create_relationship = _handler(rel_mod.setup_relationship_routes, "POST", "/relationships")
    result = create_relationship(request=_req("alice", priv_ok=True), body=_relationship_body())
    assert result["name"] == "session_to_messages"
    assert result["owner"] == "alice"


# ---------------------------------------------------------------------------
# Assembled-app TestClient half (D-01) -- drives the routes through the real,
# composed FastAPI `app` object, not an isolated router import.
# ---------------------------------------------------------------------------

def _heal_polluted_modules():
    """Sibling unit tests stub model modules (src.request_models, core.models)
    with MagicMock and import route modules against them, leaving routes.*
    bound to mocks in sys.modules. That makes app route-registration raise
    FastAPIError when we build the full app. Drop mock-contaminated model
    modules, then force app + all routes.* to re-import so they rebind
    against the real models. (Replicated from
    tests/test_query_routes_registered.py:9-24 -- see that file for the full
    rationale; kept inline here since importing a fixture across test modules
    is not reliable.)"""
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def app_module_instance():
    """Build the assembled app with AUTH_ENABLED forced on, so the real
    AuthMiddleware (and hence require_privilege) is on the request path
    instead of bypassed -- otherwise the privilege gate would be a no-op and
    this half would prove nothing (mirrors T-09-04 for the assembled-app
    claim)."""
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    # Environment gap (DEBT-03, out of Phase-9 scope): bs4 is a documented
    # dependency (requirements.txt) but not installed in this venv. app.py's
    # import chain (app -> services -> services.search -> providers)
    # hard-imports it. Stub it ONLY for the duration of building the
    # assembled app here, and ONLY if genuinely absent, then remove the stub
    # immediately after -- a conftest-wide stub would make sibling files'
    # `pytest.importorskip("bs4")` calls stop skipping and run against a
    # fake BeautifulSoup, which is out of this plan's scope to fix.
    _stubbed_bs4 = False
    if "bs4" not in sys.modules and importlib.util.find_spec("bs4") is None:
        sys.modules["bs4"] = MagicMock()
        _stubbed_bs4 = True
    saved_auth_enabled = os.environ.get("AUTH_ENABLED")
    os.environ["AUTH_ENABLED"] = "true"
    try:
        import app as app_module
    finally:
        if saved_auth_enabled is None:
            os.environ.pop("AUTH_ENABLED", None)
        else:
            os.environ["AUTH_ENABLED"] = saved_auth_enabled
        if _stubbed_bs4:
            sys.modules.pop("bs4", None)
    return app_module


def _fake_auth_manager(*, priv_ok, user):
    """Test-only auth shim (T-09-03): a stand-in auth_manager object, NOT a
    modification of shipped source. It is monkeypatched onto BOTH the
    module-global `auth_manager` app.py's AuthMiddleware closes over, and
    `app.state.auth_manager` that require_privilege reads -- both must agree
    so the middleware authenticates the TestClient call the same way the
    route handler sees it. monkeypatch reverts both at test teardown. The
    REAL require_privilege gate still runs unmodified; only the privilege
    data it reads is faked."""
    return SimpleNamespace(
        is_configured=True,
        validate_token=lambda token: True,
        get_username_for_token=lambda token: user,
        get_privileges=lambda u: {"can_manage_data_sources": priv_ok},
        is_admin=lambda u: False,
    )


def _patch_auth(monkeypatch, app_module_instance, *, priv_ok, user):
    fake_auth = _fake_auth_manager(priv_ok=priv_ok, user=user)
    monkeypatch.setattr(app_module_instance, "auth_manager", fake_auth)
    monkeypatch.setattr(app_module_instance.app.state, "auth_manager", fake_auth)


def test_testclient_datasource_list_denies_explicit_false_privilege(app_module_instance, monkeypatch):
    _patch_auth(monkeypatch, app_module_instance, priv_ok=False, user="bob")
    client = TestClient(app_module_instance.app)
    response = client.get("/api/datasources")
    assert response.status_code == 404


def test_testclient_datasource_list_allows_true_privilege(app_module_instance, monkeypatch):
    _patch_auth(monkeypatch, app_module_instance, priv_ok=True, user="alice")
    client = TestClient(app_module_instance.app)
    response = client.get("/api/datasources")
    assert response.status_code == 404
