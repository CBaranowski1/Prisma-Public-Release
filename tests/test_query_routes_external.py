# tests/test_query_routes_external.py
"""Run route requires can_manage_data_sources when a query touches an external
table; internal-only queries are unaffected."""
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.query_routes as mod


def _req(user="bob", *, priv_ok=False, is_admin=False):
    privs = {"can_manage_data_sources": priv_ok}
    auth_mgr = SimpleNamespace(get_privileges=lambda u: privs, is_configured=True,
                               is_admin=lambda u: is_admin)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.9"),
    )


def _handler(method, fragment):
    for r in mod.setup_query_routes().routes:
        if fragment in getattr(r, "path", "") and method in (getattr(r, "methods", None) or set()):
            return r.endpoint
    raise KeyError(fragment)


def test_run_external_requires_privilege(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(mod, "query_touches_external", lambda db, ast: True)
    monkeypatch.setattr(mod, "build_resolver", lambda db: (lambda n: None))
    monkeypatch.setattr(mod, "build_owner_clause", lambda db: (lambda *a: None))
    monkeypatch.setattr(mod, "SessionLocal", lambda: MagicMock())
    run = _handler("POST", "/run")
    body = mod.RunRequest(definition={"tables": [{"id": "t1", "table": "ctid-1"}]})
    with pytest.raises(HTTPException) as exc:
        run(request=_req("bob", priv_ok=False), body=body)
    assert exc.value.status_code == 403


def test_run_external_allowed_with_privilege(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(mod, "query_touches_external", lambda db, ast: True)
    monkeypatch.setattr(mod, "build_resolver", lambda db: (lambda n: None))
    monkeypatch.setattr(mod, "build_owner_clause", lambda db: (lambda *a: None))
    monkeypatch.setattr(mod, "execute_query",
                        lambda db, ast, resolver, enforce_owner=None, owner_clause_fn=None: {
                            "columns": [], "rows": [], "limit": 100, "offset": 0})
    monkeypatch.setattr(mod, "SessionLocal", lambda: MagicMock())
    run = _handler("POST", "/run")
    body = mod.RunRequest(definition={"tables": [{"id": "t1", "table": "ctid-1"}]})
    out = run(request=_req("carol", priv_ok=True), body=body)
    assert out["rows"] == []
