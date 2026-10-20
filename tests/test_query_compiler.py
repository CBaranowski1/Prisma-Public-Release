# tests/test_query_compiler.py
"""QueryCompiler: AST -> parameterized, SELECT-only SQLAlchemy Select.

Uses a real in-memory SQLite schema (two tables) so generated SQL is actually
executed, and asserts identifiers are allowlisted and values are bound params.
"""
import pytest
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Integer,
                        Boolean, insert)

from src.query_catalog import QueryValidationError
from src.query_compiler import compile_query, execute_query, MAX_LIMIT


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


def test_simple_select_with_filter(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"}],
        "fields": [{"table": "t1", "field": "name", "alias": "n"}],
        "filters": {"op": "and", "conditions": [
            {"table": "t1", "field": "archived", "operator": "eq", "value": False}]},
        "sorts": [{"table": "t1", "field": "name", "dir": "asc"}],
    }
    out = execute_query(conn, ast, resolve)
    assert out["columns"] == ["n"]
    assert [r["n"] for r in out["rows"]] == ["Alpha"]


def test_join_with_group_and_aggregate(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "chat_messages"}],
        "joins": [{"left": "t1", "left_field": "id", "right": "t2",
                   "right_field": "session_id", "type": "inner"}],
        "fields": [{"table": "t1", "field": "name", "alias": "name"},
                   {"table": "t2", "field": "id", "agg": "count", "alias": "cnt"}],
        "group_by": [{"table": "t1", "field": "name"}],
        "sorts": [{"table": "t1", "field": "name", "dir": "asc"}],
    }
    out = execute_query(conn, ast, resolve)
    by_name = {r["name"]: r["cnt"] for r in out["rows"]}
    assert by_name == {"Alpha": 2, "Beta": 1}


def test_calculated_field(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "chat_messages"}],
        "joins": [{"left": "t1", "left_field": "id", "right": "t2",
                   "right_field": "session_id", "type": "inner"}],
        "fields": [{"table": "t1", "field": "name", "alias": "name"}],
        "calculated": [{"alias": "dbl", "op": "*",
                        "left": {"table": "t2", "field": "id", "agg": "count"},
                        "right": {"value": 2}}],
        "group_by": [{"table": "t1", "field": "name"}],
    }
    out = execute_query(conn, ast, resolve)
    by_name = {r["name"]: r["dbl"] for r in out["rows"]}
    assert by_name == {"Alpha": 4, "Beta": 2}


def test_unknown_table_rejected(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = {"tables": [{"id": "t1", "table": "sqlite_master"}],
           "fields": [{"table": "t1", "field": "name"}]}
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve)


def test_unknown_operator_rejected(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = {"tables": [{"id": "t1", "table": "sessions"}],
           "fields": [{"table": "t1", "field": "name"}],
           "filters": {"op": "and", "conditions": [
               {"table": "t1", "field": "name", "operator": "DROP", "value": "x"}]}}
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve)


def test_values_are_bound_parameters_not_inlined(db_and_resolver):
    conn, resolve = db_and_resolver
    evil = "x'; DROP TABLE sessions;--"
    ast = {"tables": [{"id": "t1", "table": "sessions"}],
           "fields": [{"table": "t1", "field": "name"}],
           "filters": {"op": "and", "conditions": [
               {"table": "t1", "field": "name", "operator": "eq", "value": evil}]}}
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    # The literal must NOT appear inline; it lives in bound params.
    assert "DROP TABLE" not in str(compiled)
    assert evil in compiled.params.values()
    # And it executes harmlessly (table still there afterwards).
    out = execute_query(conn, ast, resolve)
    assert out["rows"] == []


def test_limit_is_capped(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = {"tables": [{"id": "t1", "table": "sessions"}],
           "fields": [{"table": "t1", "field": "name"}],
           "limit": 999999}
    out = execute_query(conn, ast, resolve)
    assert out["limit"] == MAX_LIMIT


def test_enforce_owner_filters_other_users(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = {"tables": [{"id": "t1", "table": "sessions"}],
           "fields": [{"table": "t1", "field": "name", "alias": "n"}],
           "sorts": [{"table": "t1", "field": "name", "dir": "asc"}]}
    # Without enforce_owner: both rows (alice's Alpha + bob's Beta).
    everyone = execute_query(conn, ast, resolve)
    assert [r["n"] for r in everyone["rows"]] == ["Alpha", "Beta"]
    # With enforce_owner="alice": only alice's row, no AST filter needed.
    scoped = execute_query(conn, ast, resolve, enforce_owner="alice")
    assert [r["n"] for r in scoped["rows"]] == ["Alpha"]


def test_enforce_owner_fails_closed_on_owner_less_table(db_and_resolver):
    # Without an owner_clause_fn, a table that has no `owner` column cannot be
    # scoped — the compiler MUST refuse rather than return unscoped rows. This
    # is the fix for the standalone-chat_messages cross-user leak: previously
    # such a table was silently left unfiltered.
    conn, resolve = db_and_resolver
    ast = {"tables": [{"id": "t1", "table": "chat_messages"}],
           "fields": [{"table": "t1", "field": "role"}]}
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve, enforce_owner="alice")


def test_enforce_owner_uses_owner_clause_fn(db_and_resolver):
    # When an owner_clause_fn is supplied (production passes the catalog's
    # owner_clause), the compiler applies its predicate to every table instance
    # — including owner-less ones — so they ARE scoped.
    conn, resolve = db_and_resolver

    def owner_fn(name, aliased, user):
        assert user == "alice"
        # Toy scope: only session s1's messages belong to "alice".
        return aliased.c["session_id"] == "s1"

    ast = {"tables": [{"id": "t1", "table": "chat_messages"}],
           "fields": [{"table": "t1", "field": "id", "alias": "id"}],
           "sorts": [{"table": "t1", "field": "id", "dir": "asc"}]}
    out = execute_query(conn, ast, resolve, enforce_owner="alice", owner_clause_fn=owner_fn)
    assert [r["id"] for r in out["rows"]] == ["m1", "m2"]


@pytest.mark.parametrize("bad_ast", [
    {"tables": [{"id": "t1", "table": "sessions"}], "fields": [{"table": "t1"}]},          # field missing 'field'
    {"tables": [{"id": "t1", "table": "sessions"}], "fields": [{"field": "name"}]},         # field missing 'table'
    {"tables": [{"id": "t1", "table": "sessions"}], "fields": [{"table": "t1", "field": "name"}],
     "filters": {"op": "and", "conditions": [{"table": "t1", "operator": "eq", "value": 1}]}},  # filter missing 'field'
    {"tables": [{"id": "t1", "table": "sessions"}, {"id": "t2", "table": "chat_messages"}],
     "joins": [{"left": "t1"}], "fields": [{"table": "t1", "field": "name"}]},               # join missing keys
    {"tables": "sessions", "fields": []},                                                     # tables not a list
    {"tables": [{"id": "t1", "table": "sessions"}], "fields": [{"table": "t1", "field": "name"}],
     "filters": {"op": "and", "conditions": ["oops"]}},                                       # condition not an object
    {"tables": [{"id": "t1", "table": "sessions"}], "fields": [{"table": "t1", "field": "name"}],
     "group_by": [{"table": "t1"}]},                                                          # group_by missing 'field'
])
def test_malformed_ast_raises_validation_error(db_and_resolver, bad_ast):
    """A malformed query definition must raise QueryValidationError (→ HTTP 400),
    never an uncaught KeyError/AttributeError (which would be a 500)."""
    _conn, resolve = db_and_resolver
    with pytest.raises(QueryValidationError):
        compile_query(bad_ast, resolve)
