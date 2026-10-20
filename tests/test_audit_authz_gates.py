"""Functional regression guards for the audit authorization fixes.

P3: /api/brain/config (GET+PUT) and /api/brain-graph, /api/brain-note must
    require can_manage_brain — previously any authenticated user could mutate
    global brain settings / read the vault.
P4: /api/personal/upload must require can_use_documents.

These build a minimal app, inject request.state.current_user via middleware,
and stub app.state.auth_manager so require_privilege() resolves real privileges
without a full login stack.
"""

import os
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware


_REPO = Path(__file__).resolve().parent.parent


class _FakeAuth:
    is_configured = True

    def __init__(self, privs_by_user):
        self._privs = privs_by_user

    def get_privileges(self, user):
        return self._privs.get(user, {})


def _make_app(monkeypatch):
    # Ensure auth is ENFORCED (not the AUTH_ENABLED=false bypass path).
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    monkeypatch.setattr("src.settings.load_settings", lambda: {"brain_vault_path": "/x"}, raising=False)
    monkeypatch.setattr("src.settings.save_settings", lambda s: None, raising=False)

    import importlib
    import routes.brain_routes as br
    importlib.reload(br)

    app = FastAPI()

    class _InjectUser(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.current_user = request.headers.get("X-Test-User") or None
            return await call_next(request)

    app.add_middleware(_InjectUser)
    app.state.auth_manager = _FakeAuth({
        "alice_admin": {"can_manage_brain": True},
        "bob_user": {"can_manage_brain": False},
    })

    class _Registry:
        def all(self):
            return []
    app.include_router(br.setup_brain_routes(_Registry()))
    return app


def test_brain_config_put_denied_for_non_brain_user(monkeypatch):
    client = TestClient(_make_app(monkeypatch))
    r = client.put(
        "/api/brain/config",
        json={"vault_path": "/tmp/attacker"},
        headers={"X-Test-User": "bob_user"},
    )
    assert r.status_code == 403, r.text


def test_brain_config_put_allowed_for_brain_admin(monkeypatch):
    client = TestClient(_make_app(monkeypatch))
    r = client.put(
        "/api/brain/config",
        json={"vault_path": "/tmp/ok"},
        headers={"X-Test-User": "alice_admin"},
    )
    assert r.status_code == 200, r.text


def test_brain_config_get_denied_for_non_brain_user(monkeypatch):
    client = TestClient(_make_app(monkeypatch))
    r = client.get("/api/brain/config", headers={"X-Test-User": "bob_user"})
    assert r.status_code == 403, r.text


def test_personal_upload_source_is_privilege_gated():
    # Source-level guard: the upload handler must gate on can_use_documents
    # rather than the old bare get_current_user() read.
    src = (_REPO / "routes" / "personal_routes.py").read_text(encoding="utf-8")
    assert 'require_privilege(request, "can_use_documents"' in src


def test_brain_graph_source_is_privilege_gated():
    src = (_REPO / "routes" / "brain_graph_routes.py").read_text(encoding="utf-8")
    assert src.count('require_privilege(request, "can_manage_brain"') >= 2


def _direct_request(
    *,
    user="bob_user",
    privs=None,
    api_token=False,
    scopes=None,
):
    return SimpleNamespace(
        state=SimpleNamespace(
            current_user=user,
            api_token=api_token,
            api_token_scopes=scopes or [],
            api_token_owner=user if api_token else None,
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(auth_manager=_FakeAuth({user: privs or {}}))
        ),
    )


def _route_endpoint(router, path, method):
    return next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == path and method in getattr(r, "methods", set())
    )


def test_document_read_denied_when_documents_privilege_revoked(monkeypatch):
    import routes.document_routes as dr

    monkeypatch.setattr(
        dr,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("DB should not be touched")),
    )
    router = dr.setup_document_routes(None, None)
    target = _route_endpoint(router, "/api/document/{doc_id}", "GET")
    req = _direct_request(privs={"can_use_documents": False})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(req, "doc-1"))
    assert exc.value.status_code == 403


def test_document_write_denied_for_read_only_token(monkeypatch):
    import routes.document_routes as dr

    monkeypatch.setattr(
        dr,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("DB should not be touched")),
    )
    router = dr.setup_document_routes(None, None)
    target = _route_endpoint(router, "/api/document/{doc_id}/archive", "POST")
    req = _direct_request(
        user="api",
        privs={"can_use_documents": True},
        api_token=True,
        scopes=["documents:read"],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(req, "doc-1", archived=True))
    assert exc.value.status_code == 403


def test_model_management_uses_can_manage_models_gate():
    import routes.model_routes as mr
    import routes.cookbook_routes as cr

    allowed = _direct_request(user="coder", privs={"can_manage_models": True})
    denied = _direct_request(user="user", privs={"can_manage_models": False})

    assert mr._require_model_manager(allowed) == "coder"
    assert cr._require_model_manager(allowed) == "coder"
    with pytest.raises(HTTPException) as exc:
        mr._require_model_manager(denied)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        cr._require_model_manager(denied)
    assert exc.value.status_code == 403


# --- gap 4: API-token scope enforcement (require_scope) ---

def _tok_request(api_token, scopes):
    from types import SimpleNamespace
    return SimpleNamespace(
        state=SimpleNamespace(api_token=api_token, api_token_scopes=scopes)
    )


def test_require_scope_noop_for_session_requests():
    from src.auth_helpers import require_scope
    # A logged-in browser session (api_token False) is never constrained by scopes.
    require_scope(_tok_request(False, []), "chat", "email:send")


def test_require_scope_blocks_token_missing_scope():
    from fastapi import HTTPException
    from src.auth_helpers import require_scope
    with pytest.raises(HTTPException) as exc:
        require_scope(_tok_request(True, ["chat"]), "email:send")
    assert exc.value.status_code == 403


def test_require_scope_allows_token_with_scope():
    from src.auth_helpers import require_scope
    require_scope(_tok_request(True, ["chat", "email:send"]), "email:send")


def test_require_scope_require_all_semantics():
    from fastapi import HTTPException
    from src.auth_helpers import require_scope
    with pytest.raises(HTTPException):
        require_scope(
            _tok_request(True, ["email:read"]),
            "email:read", "email:send", require_all=True,
        )
    # Both present → passes.
    require_scope(
        _tok_request(True, ["email:read", "email:send"]),
        "email:read", "email:send", require_all=True,
    )
