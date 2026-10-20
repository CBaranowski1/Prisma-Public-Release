# tests/test_query_compiler_validation.py
"""QB-12 compiler validation suite.

Covers the two additive `compile_query` validations D-07 asks for:
  1. Aggregate-without-group_by: a bare (non-aggregated) selected field
     alongside an aggregated field, with no covering `group_by`, must be
     rejected with a clean QueryValidationError — never silently compiled
     into SQL that only strict dialects (Postgres/MySQL) reject at runtime.
  2. Unsupported join `type`: an unknown/unsupported join type (e.g. "right",
     "full") must be rejected with a clean QueryValidationError, never
     silently downgraded to an inner join (the pre-existing `type:"right"` ->
     inner-join fallback bug at query_compiler.py:212-213).

Also proves owner-scoping is preserved when aggregation/grouping is used —
a cross-owner aggregate must never count another user's rows.
"""
import pytest
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Integer,
                        Boolean, insert, true)

from src.query_catalog import QueryValidationError
from src.query_compiler import compile_query, execute_query


@pytest.fixture
def db_and_resolver():
    engine = create_engine("sqlite:///:memory:")
    md = MetaData()
    sessions = Table("sessions", md,
                     Column("id", String, primary_key=True),
                     Column("name", String),
                     Column("owner", String),
                     Column("archived", Boolean))
    messages = Table("chat_messages", md,
                     Column("id", String, primary_key=True),
                     Column("session_id", String),
                     Column("role", String))
    md.create_all(engine)
    conn = engine.connect()
    conn.execute(insert(sessions), [
        {"id": "s1", "name": "Alpha", "owner": "alice", "archived": False},
        {"id": "s2", "name": "Beta", "owner": "bob", "archived": True},
    ])
    conn.execute(insert(messages), [
        {"id": "m1", "session_id": "s1", "role": "user"},
        {"id": "m2", "session_id": "s1", "role": "assistant"},
        {"id": "m3", "session_id": "s2", "role": "user"},
    ])
    conn.commit()
    tables = {"sessions": sessions, "chat_messages": messages}

    def resolve(name):
        if name not in tables:
            raise QueryValidationError(f"forbidden: {name}")
        return tables[name]

    yield conn, resolve
    conn.close()


# ---------------------------------------------------------------------------
# Aggregate-without-group_by
# ---------------------------------------------------------------------------

def test_bare_field_with_aggregate_and_no_group_by_rejected(db_and_resolver):
    """A bare field alongside an aggregate, with NO group_by at all, is invalid."""
    _conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"}],
        "fields": [
            {"table": "t1", "field": "name"},
            {"table": "t1", "field": "id", "agg": "count", "alias": "cnt"},
        ],
    }
    with pytest.raises(QueryValidationError, match="name"):
        compile_query(ast, resolve)


def test_bare_field_uncovered_by_group_by_on_different_field_rejected(db_and_resolver):
    """group_by covers a DIFFERENT field, leaving the bare field uncovered."""
    _conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"}],
        "fields": [
            {"table": "t1", "field": "name"},
            {"table": "t1", "field": "id", "agg": "count", "alias": "cnt"},
        ],
        "group_by": [{"table": "t1", "field": "archived"}],
    }
    with pytest.raises(QueryValidationError, match="name"):
        compile_query(ast, resolve)


def test_bare_field_covered_by_group_by_accepted(db_and_resolver):
    """A bare field whose (table, field) IS in group_by compiles cleanly."""
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"}],
        "fields": [
            {"table": "t1", "field": "name", "alias": "name"},
            {"table": "t1", "field": "id", "agg": "count", "alias": "cnt"},
        ],
        "group_by": [{"table": "t1", "field": "name"}],
        "sorts": [{"table": "t1", "field": "name", "dir": "asc"}],
    }
    out = execute_query(conn, ast, resolve)
    by_name = {r["name"]: r["cnt"] for r in out["rows"]}
    assert by_name == {"Alpha": 1, "Beta": 1}


def test_aggregate_only_query_accepted(db_and_resolver):
    """An aggregate-only query (a single COUNT, no bare field, no group_by) is
    valid SQL and must NOT be rejected."""
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"}],
        "fields": [{"table": "t1", "field": "id", "agg": "count", "alias": "cnt"}],
    }
    out = execute_query(conn, ast, resolve)
    assert out["rows"] == [{"cnt": 2}]


# ---------------------------------------------------------------------------
# Unsupported join type
# ---------------------------------------------------------------------------

def _join_ast(join_type):
    return {
        "tables": [{"id": "t1", "table": "sessions"}, {"id": "t2", "table": "chat_messages"}],
        "joins": [{"left": "t1", "left_field": "id", "right": "t2",
                   "right_field": "session_id", "type": join_type}],
        "fields": [{"table": "t1", "field": "name", "alias": "name"}],
    }


def test_join_type_right_rejected(db_and_resolver):
    _conn, resolve = db_and_resolver
    with pytest.raises(QueryValidationError, match="right"):
        compile_query(_join_ast("right"), resolve)


def test_join_type_full_rejected(db_and_resolver):
    _conn, resolve = db_and_resolver
    with pytest.raises(QueryValidationError, match="full"):
        compile_query(_join_ast("full"), resolve)


def test_join_type_unknown_string_rejected(db_and_resolver):
    _conn, resolve = db_and_resolver
    with pytest.raises(QueryValidationError):
        compile_query(_join_ast("cross"), resolve)


def test_join_type_inner_and_left_accepted(db_and_resolver):
    """Regression guard: inner/left joins must keep compiling unchanged."""
    conn, resolve = db_and_resolver
    inner_out = execute_query(conn, _join_ast("inner"), resolve)
    assert sorted(r["name"] for r in inner_out["rows"]) == ["Alpha", "Alpha", "Beta"]
    left_out = execute_query(conn, _join_ast("left"), resolve)
    assert sorted(r["name"] for r in left_out["rows"]) == ["Alpha", "Alpha", "Beta"]


# ---------------------------------------------------------------------------
# Owner-scoping preserved through aggregation/grouping
# ---------------------------------------------------------------------------

def test_cross_owner_aggregate_isolation(db_and_resolver):
    """A cross-owner aggregate must never count another user's rows — grouping
    does not drop owner-scoping. sessions.owner scopes the base table; the
    joined chat_messages rows are transitively scoped through that join."""
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"}, {"id": "t2", "table": "chat_messages"}],
        "joins": [{"left": "t1", "left_field": "id", "right": "t2",
                   "right_field": "session_id", "type": "inner"}],
        "fields": [{"table": "t2", "field": "id", "agg": "count", "alias": "cnt"}],
    }

    def owner_fn(name, aliased, user):
        if name == "sessions":
            return aliased.c["owner"] == user
        # chat_messages is scoped transitively via the sessions join; no
        # direct owner column, so contribute an always-true clause here.
        return true()

    stmt = compile_query(ast, resolve, enforce_owner="alice", owner_clause_fn=owner_fn)
    compiled = stmt.compile()
    # The owner predicate's bound value must be present in the compiled SQL.
    assert "alice" in compiled.params.values()

    out = execute_query(conn, ast, resolve, enforce_owner="alice", owner_clause_fn=owner_fn)
    # alice's session s1 has 2 messages; bob's s2 (1 message) must never be
    # counted, even though this is an aggregate/group-less query.
    assert out["rows"] == [{"cnt": 2}]
