"""Relationship routes: privilege gating + create/list using the endpoint-
extraction style (no real DB)."""
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.relationship_routes as mod


def _req(user, *, priv_ok):
    privs = {"can_manage_data_sources": priv_ok}
    auth_mgr = SimpleNamespace(get_privileges=lambda u: privs, is_configured=True)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.5"),
    )


def _handler(method, fragment):
    for route in mod.setup_relationship_routes().routes:
        if fragment in getattr(route, "path", "") and method in (getattr(route, "methods", None) or set()):
            return route.endpoint
    raise KeyError(fragment)


@contextlib.contextmanager
def _db_ctx(session):
    yield session


def test_create_requires_privilege(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    create = _handler("POST", "/relationships")
    body = mod.RelationshipCreate(
        name="r", kind="one_to_many", left_table_id="a", left_field="id",
        right_table_id="b", right_field="a_id")
    with pytest.raises(HTTPException) as exc:
        create(request=_req("bob", priv_ok=False), body=body)
    assert exc.value.status_code == 403


def test_create_persists_with_owner(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    captured = {}

    class _Rel:
        def __init__(self, **kw):
            captured.update(kw)
            self.__dict__.update(kw)
        def to_dict(self):
            return {"id": captured.get("id"), "owner": captured.get("owner"),
                    "name": captured.get("name"), "kind": captured.get("kind")}

    session = MagicMock()
    monkeypatch.setattr(mod, "QueryRelationship", _Rel)
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))

    create = _handler("POST", "/relationships")
    # Real allowlisted internal tables/fields (sessions.id, chat_messages.session_id)
    # so the D-13 create-time catalog validation resolves them -- this test stays
    # scoped to owner-persistence, not to validation itself (RESEARCH Pitfall 5b).
    body = mod.RelationshipCreate(
        name="session→messages", kind="one_to_many", left_table_id="sessions",
        left_field="id", right_table_id="chat_messages", right_field="session_id")
    resp = create(request=_req("alice", priv_ok=True), body=body)
    assert resp["owner"] == "alice"
    assert resp["kind"] == "one_to_many"
    assert captured["name"] == "session→messages"


def test_create_rejects_unknown_table_ref(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    session = MagicMock()
    # "does-not-exist" isn't internal-allowlisted, so validation falls through to a
    # CatalogTable id lookup -- force that lookup to miss (None), same as a real,
    # never-synced/deleted external table id would.
    session.query.return_value.filter.return_value.first.return_value = None
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))

    create = _handler("POST", "/relationships")
    body = mod.RelationshipCreate(
        name="bad-ref", kind="one_to_many", left_table_id="does-not-exist",
        left_field="id", right_table_id="chat_messages", right_field="session_id")
    with pytest.raises(HTTPException) as exc:
        create(request=_req("alice", priv_ok=True), body=body)
    assert exc.value.status_code == 400


def test_create_rejects_unknown_field_ref(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    session = MagicMock()
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))

    create = _handler("POST", "/relationships")
    body = mod.RelationshipCreate(
        name="bad-field", kind="one_to_many", left_table_id="sessions",
        left_field="not_a_real_column", right_table_id="chat_messages",
        right_field="session_id")
    with pytest.raises(HTTPException) as exc:
        create(request=_req("alice", priv_ok=True), body=body)
    assert exc.value.status_code == 400
