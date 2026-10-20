"""Security regression tests for query-builder owner scoping.

These use the REAL catalog (`src.query_catalog.resolve_table` + `owner_clause`)
against a live in-memory SQLite DB — the same wiring the /api/queries/run route
uses — to prove the two findings from the 2026-06-27 security review are closed:

  1. A standalone query over an owner-less table (chat_messages) must NOT leak
     other users' rows; it is scoped through the parent session via EXISTS.
  2. Columns excluded from the allowlist (e.g. sessions.headers) cannot be
     selected even via a hand-crafted AST.
"""
import pytest
from sqlalchemy import create_engine, MetaData, Table, Column, String, insert

from src import query_catalog
from src.query_compiler import execute_query, compile_query
from src.query_catalog import QueryValidationError


@pytest.fixture
def conn():
    engine = create_engine("sqlite:///:memory:")
    md = MetaData()
    sessions = Table("sessions", md,
                     Column("id", String, primary_key=True),
                     Column("owner", String))
    messages = Table("chat_messages", md,
                     Column("id", String, primary_key=True),
                     Column("session_id", String),
                     Column("content", String))
    md.create_all(engine)
    c = engine.connect()
    c.execute(insert(sessions), [
        {"id": "s1", "owner": "alice"},
        {"id": "s2", "owner": "bob"},
    ])
    c.execute(insert(messages), [
        {"id": "m1", "session_id": "s1", "content": "alice-secret"},
        {"id": "m2", "session_id": "s2", "content": "bob-secret"},
    ])
    c.commit()
    yield c
    c.close()


def test_standalone_chat_messages_scoped_via_parent(conn):
    # The exploit: query chat_messages directly, no join to sessions.
    ast = {"tables": [{"id": "t1", "table": "chat_messages"}],
           "fields": [{"table": "t1", "field": "content", "alias": "content"}]}
    out = execute_query(conn, ast, query_catalog.resolve_table,
                        enforce_owner="alice",
                        owner_clause_fn=query_catalog.owner_clause)
    # alice must see ONLY her own message, never bob's.
    assert [r["content"] for r in out["rows"]] == ["alice-secret"]


def test_admin_no_scope_sees_all(conn):
    ast = {"tables": [{"id": "t1", "table": "chat_messages"}],
           "fields": [{"table": "t1", "field": "content", "alias": "content"}],
           "sorts": [{"table": "t1", "field": "id", "dir": "asc"}]}
    # enforce_owner=None (admin / single-user) → no scoping.
    out = execute_query(conn, ast, query_catalog.resolve_table,
                        enforce_owner=None, owner_clause_fn=query_catalog.owner_clause)
    assert [r["content"] for r in out["rows"]] == ["alice-secret", "bob-secret"]


def test_excluded_column_cannot_be_selected():
    # `headers` exists on the Session model but is NOT allowlisted; selecting it
    # via a hand-crafted AST must be rejected at compile time.
    ast = {"tables": [{"id": "t1", "table": "sessions"}],
           "fields": [{"table": "t1", "field": "headers"}]}
    with pytest.raises(QueryValidationError):
        compile_query(ast, query_catalog.resolve_table)
