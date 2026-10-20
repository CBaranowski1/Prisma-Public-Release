# tests/test_query_routes.py
"""Query routes: catalog, run (real in-memory data via resolver), validation."""
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.query_routes as mod


def _req(user="alice", *, is_admin=False):
    auth_mgr = SimpleNamespace(get_privileges=lambda u: {}, is_configured=True,
                               is_admin=lambda u: is_admin)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.5"),
    )


def _handler(method, fragment):
    for route in mod.setup_query_routes().routes:
        if fragment in getattr(route, "path", "") and method in (getattr(route, "methods", None) or set()):
            return route.endpoint
    raise KeyError(fragment)


def test_catalog_lists_internal_tables(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(mod, "iter_catalog", lambda: [{"table": "sessions", "columns": []}])

    @contextlib.contextmanager
    def _db():
        s = MagicMock()
        s.query.return_value.filter.return_value.all.return_value = []
        s.query.return_value.all.return_value = []
        yield s
    monkeypatch.setattr(mod, "get_db_session", lambda: _db())

    catalog = _handler("GET", "/catalog")
    out = catalog(request=_req())
    assert any(t["table"] == "sessions" for t in out["tables"])


def test_run_invalid_ast_returns_400(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from src.query_catalog import QueryValidationError

    def _boom(db, ast, resolver, enforce_owner=None, owner_clause_fn=None):
        raise QueryValidationError("nope")
    monkeypatch.setattr(mod, "execute_query", _boom)
    monkeypatch.setattr(mod, "SessionLocal", lambda: MagicMock())

    run = _handler("POST", "/run")
    # Use an internal (allowlisted) table so this stays an internal,
    # non-external-touch query — the point of this test is the
    # QueryValidationError -> 400 translation, not the fail_closed privilege
    # gate (D-08: internal owner-scoped queries stay open to any
    # authenticated user). A non-allowlisted table name here would
    # (correctly, per the SEC-04 fix) trip the external-touch privilege gate
    # before ever reaching the mocked execute_query.
    body = mod.RunRequest(definition={"tables": [{"id": "t1", "table": "sessions"}]})
    with pytest.raises(HTTPException) as exc:
        run(request=_req(), body=body)
    assert exc.value.status_code == 400


def test_run_returns_rows(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(mod, "execute_query",
                        lambda db, ast, resolver, enforce_owner=None, owner_clause_fn=None: {
                            "columns": ["n"], "rows": [{"n": "Alpha"}],
                            "limit": 100, "offset": 0})
    monkeypatch.setattr(mod, "SessionLocal", lambda: MagicMock())
    run = _handler("POST", "/run")
    body = mod.RunRequest(definition={"tables": [{"id": "t1", "table": "sessions"}],
                                      "fields": [{"table": "t1", "field": "name", "alias": "n"}]})
    out = run(request=_req(), body=body)
    assert out["rows"] == [{"n": "Alpha"}]


def test_run_scopes_to_owner_for_non_admin(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    captured = {}
    monkeypatch.setattr(mod, "execute_query",
                        lambda db, ast, resolver, enforce_owner=None, owner_clause_fn=None: captured.update(
                            enforce_owner=enforce_owner) or {"columns": [], "rows": [],
                                                             "limit": 100, "offset": 0})
    monkeypatch.setattr(mod, "SessionLocal", lambda: MagicMock())
    run = _handler("POST", "/run")
    body = mod.RunRequest(definition={"tables": [{"id": "t1", "table": "sessions"}],
                                      "fields": [{"table": "t1", "field": "name"}]})
    run(request=_req("alice", is_admin=False), body=body)
    assert captured["enforce_owner"] == "alice"


def test_run_no_owner_scope_for_admin(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    captured = {}
    monkeypatch.setattr(mod, "execute_query",
                        lambda db, ast, resolver, enforce_owner=None, owner_clause_fn=None: captured.update(
                            enforce_owner=enforce_owner) or {"columns": [], "rows": [],
                                                             "limit": 100, "offset": 0})
    monkeypatch.setattr(mod, "SessionLocal", lambda: MagicMock())
    run = _handler("POST", "/run")
    body = mod.RunRequest(definition={"tables": [{"id": "t1", "table": "sessions"}],
                                      "fields": [{"table": "t1", "field": "name"}]})
    run(request=_req("rootadmin", is_admin=True), body=body)
    assert captured["enforce_owner"] is None


def _priv_req(user="carol"):
    """A request whose auth_manager grants can_manage_data_sources, so the
    external-touch privilege gate (fail_closed=True) passes and execution
    reaches the QB-07 stale-reference validator."""
    auth_mgr = SimpleNamespace(get_privileges=lambda u: {"can_manage_data_sources": True},
                               is_configured=True, is_admin=lambda u: False)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.7"),
    )


def test_run_saved_query_stale_reference_error(monkeypatch, tmp_path):
    """QB-07/D-14: a query definition referencing a table/field no longer in
    the live catalog gets a structured 400 naming the missing table/field --
    not the generic 'Query could not be executed' message."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base
    import core.querybuilder_models as qm
    from src.query_catalog import validate_definition_against_catalog

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=eng)
    Session = sessionmaker(bind=eng)
    db = Session()
    ct = qm.CatalogTable(id="ct1", source_id="src1", name="customers",
                         display_name="src: customers", is_internal=False,
                         physical_table_name="ext_src1_customers")
    db.add(ct)
    db.flush()
    # Only 'id' is registered -- 'email' simulates a column the source
    # dropped (or a stale saved query referencing one that was never there).
    db.add(qm.CatalogField(id="f1", table_id=ct.id, name="id", data_type="VARCHAR"))
    db.commit()

    definition = {
        "tables": [{"id": "t1", "table": "ct1"}],
        "fields": [{"table": "t1", "field": "email"}],
    }

    # Unit: the validator itself reports the missing field, naming the
    # catalog table's logical name (not the opaque CatalogTable.id).
    result = validate_definition_against_catalog(db, definition)
    assert result["missing_tables"] == []
    assert result["missing_fields"] == ["customers.email"]

    # Route: /api/queries/run surfaces a specific 400, distinct from the
    # generic could-not-execute message, naming the stale reference.
    monkeypatch.setattr(mod, "SessionLocal", lambda: db)
    run = _handler("POST", "/run")
    body = mod.RunRequest(definition=definition)
    with pytest.raises(HTTPException) as exc:
        run(request=_priv_req(), body=body)
    assert exc.value.status_code == 400
    assert "customers.email" in str(exc.value.detail)
    assert "could not be executed" not in str(exc.value.detail).lower()

    db.close()


def test_run_valid_query_unaffected_by_stale_check(monkeypatch, tmp_path):
    """A valid query (all refs resolve) is unaffected by the QB-07 validator
    -- no false positive."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base
    import core.querybuilder_models as qm

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=eng)
    Session = sessionmaker(bind=eng)
    db = Session()
    ct = qm.CatalogTable(id="ct1", source_id="src1", name="customers",
                         display_name="src: customers", is_internal=False,
                         physical_table_name="ext_src1_customers")
    db.add(ct)
    db.flush()
    db.add(qm.CatalogField(id="f1", table_id=ct.id, name="id", data_type="VARCHAR"))
    db.commit()

    monkeypatch.setattr(mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(mod, "execute_query",
                        lambda db, ast, resolver, enforce_owner=None, owner_clause_fn=None: {
                            "columns": ["id"], "rows": [{"id": "c1"}], "limit": 100, "offset": 0})
    run = _handler("POST", "/run")
    body = mod.RunRequest(definition={"tables": [{"id": "t1", "table": "ct1"}],
                                      "fields": [{"table": "t1", "field": "id"}]})
    out = run(request=_priv_req(), body=body)
    assert out["rows"] == [{"id": "c1"}]

    db.close()
