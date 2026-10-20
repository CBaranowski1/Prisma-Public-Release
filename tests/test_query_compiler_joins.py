# tests/test_query_compiler_joins.py
"""Join-graph correctness for the compiler:

- WR-01: a LEFT join preserves the side the user DREW as the join's `left`,
  independent of the order tables were added to the canvas.
- CR-01: a fully-connected join graph compiles regardless of insertion order
  (a hub-last star schema must not raise "not joined").
- CR-02: two same-named columns from different joined tables yield DISTINCT
  result keys instead of collapsing/clashing.
"""
import pytest
from sqlalchemy import (create_engine, MetaData, Table, Column, String, insert)

from src.query_catalog import QueryValidationError
from src.query_compiler import compile_query, execute_query


@pytest.fixture
def db_and_resolver():
    engine = create_engine("sqlite:///:memory:")
    md = MetaData()
    # `documents` has a row whose owning session is MISSING, so an outer join
    # that preserves documents keeps it (session columns NULL) while an outer
    # join that preserves sessions would drop it — the tell-tale for direction.
    sessions = Table("sessions", md,
                     Column("id", String, primary_key=True),
                     Column("name", String))
    documents = Table("documents", md,
                      Column("id", String, primary_key=True),
                      Column("title", String),
                      Column("session_id", String))
    # A hub table joined by two spokes, to exercise hub-last insertion order.
    tags = Table("tags", md,
                 Column("id", String, primary_key=True),
                 Column("doc_id", String))
    md.create_all(engine)
    conn = engine.connect()
    conn.execute(insert(sessions), [
        {"id": "s1", "name": "Alpha"},
    ])
    conn.execute(insert(documents), [
        {"id": "d1", "title": "Doc One", "session_id": "s1"},
        {"id": "d2", "title": "Orphan", "session_id": "s_missing"},
    ])
    conn.execute(insert(tags), [
        {"id": "g1", "doc_id": "d1"},
    ])
    conn.commit()
    tables = {"sessions": sessions, "documents": documents, "tags": tags}

    def resolve(name):
        if name not in tables:
            raise QueryValidationError(f"forbidden: {name}")
        return tables[name]

    yield conn, resolve
    conn.close()


def test_left_join_preserves_drawn_left_side_regardless_of_add_order(db_and_resolver):
    """WR-01: sessions (t1) is added FIRST, but the user draws a LEFT join
    starting from documents (t2): `documents LEFT JOIN sessions` — "all
    documents, plus their session if any". The orphan document (no matching
    session) must survive; the compiled FROM must preserve documents, NOT the
    first-added sessions."""
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "documents"}],
        # Drawn from documents (left) -> sessions (right), so documents rows
        # are the preserved side.
        "joins": [{"left": "t2", "left_field": "session_id",
                   "right": "t1", "right_field": "id", "type": "left"}],
        "fields": [{"table": "t2", "field": "title", "alias": "title"},
                   {"table": "t1", "field": "name", "alias": "sname"}],
        "sorts": [{"table": "t2", "field": "id", "dir": "asc"}],
    }
    out = execute_query(conn, ast, resolve)
    titles = [r["title"] for r in out["rows"]]
    assert "Orphan" in titles, "orphan document must be preserved by the LEFT join"
    by_title = {r["title"]: r["sname"] for r in out["rows"]}
    assert by_title["Doc One"] == "Alpha"
    assert by_title["Orphan"] is None


def test_left_join_direction_visible_in_compiled_sql(db_and_resolver):
    """The compiled SQL must place documents on the outer-preserved (left) side
    of the OUTER JOIN even though sessions was added first."""
    _conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "documents"}],
        "joins": [{"left": "t2", "left_field": "session_id",
                   "right": "t1", "right_field": "id", "type": "left"}],
        "fields": [{"table": "t2", "field": "title", "alias": "title"}],
    }
    sql = str(compile_query(ast, resolve).compile())
    assert "LEFT OUTER JOIN" in sql
    # documents (t2) must appear before sessions (t1) around the OUTER JOIN so
    # the preserved side is documents.
    pre = sql.split("LEFT OUTER JOIN", 1)[0]
    assert "documents" in pre and "sessions" not in pre


def test_hub_last_star_schema_compiles(db_and_resolver):
    """CR-01: tables added hub-last (the connecting `documents` hub appears
    LAST in insertion order) must still compile — the attach loop iterates to a
    fixed point rather than a single insertion-order pass."""
    _conn, resolve = db_and_resolver
    ast = {
        # Insertion order: spoke, spoke, hub — hub (documents) is last.
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "tags"},
                   {"id": "t3", "table": "documents"}],
        "joins": [
            {"left": "t3", "left_field": "session_id", "right": "t1",
             "right_field": "id", "type": "inner"},
            {"left": "t3", "left_field": "id", "right": "t2",
             "right_field": "doc_id", "type": "inner"},
        ],
        "fields": [{"table": "t3", "field": "title", "alias": "title"}],
    }
    # Must not raise "not joined".
    stmt = compile_query(ast, resolve)
    assert stmt is not None


def test_duplicate_field_names_across_tables_yield_distinct_keys(db_and_resolver):
    """CR-02: selecting `id` from two different joined tables must produce two
    DISTINCT result keys, not collapse into one column / raise ambiguous."""
    conn, resolve = db_and_resolver
    ast = {
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "documents"}],
        "joins": [{"left": "t1", "left_field": "id", "right": "t2",
                   "right_field": "session_id", "type": "inner"}],
        # Both selected with the compiler-default dedup: no explicit aliases,
        # so the backstop must disambiguate them.
        "fields": [{"table": "t1", "field": "id"},
                   {"table": "t2", "field": "id"}],
    }
    out = execute_query(conn, ast, resolve)
    assert len(out["columns"]) == 2
    assert len(set(out["columns"])) == 2, "two `id` columns must have distinct keys"


# --- WR-02: owner-scoping must not demote a LEFT join to INNER -----------------

@pytest.fixture
def owner_scoped_db():
    """`docs` LEFT JOIN `sessions`, both owner-scoped, with three of alice's docs:
    one matching her own session, one pointing at BOB's session, and one orphan
    (no session). Proves the LEFT join preserves alice's orphan/unmatched docs
    while never surfacing bob's session data."""
    engine = create_engine("sqlite:///:memory:")
    md = MetaData()
    sessions = Table("sessions", md,
                     Column("id", String, primary_key=True),
                     Column("name", String),
                     Column("owner", String))
    docs = Table("docs", md,
                 Column("id", String, primary_key=True),
                 Column("title", String),
                 Column("session_id", String),
                 Column("owner", String))
    md.create_all(engine)
    conn = engine.connect()
    conn.execute(insert(sessions), [
        {"id": "s1", "name": "alice-session", "owner": "alice"},
        {"id": "s2", "name": "bob-session", "owner": "bob"},
    ])
    conn.execute(insert(docs), [
        {"id": "d1", "title": "own-session", "session_id": "s1", "owner": "alice"},
        {"id": "d2", "title": "points-at-bob", "session_id": "s2", "owner": "alice"},
        {"id": "d3", "title": "orphan", "session_id": "s_missing", "owner": "alice"},
    ])
    conn.commit()
    tables = {"sessions": sessions, "docs": docs}

    def resolve(name):
        if name not in tables:
            raise QueryValidationError(f"forbidden: {name}")
        return tables[name]

    def owner_clause(name, aliased, user):
        return aliased.c["owner"] == user

    yield conn, resolve, owner_clause
    conn.close()


def _owner_left_join_ast(docs_first: bool):
    """`docs LEFT JOIN sessions` (docs preserved), with table-add order toggled
    so both the default and the flip attach branch are exercised."""
    tables = ([{"id": "t1", "table": "docs"}, {"id": "t2", "table": "sessions"}]
              if docs_first else
              [{"id": "t1", "table": "sessions"}, {"id": "t2", "table": "docs"}])
    docs_id, sess_id = ("t1", "t2") if docs_first else ("t2", "t1")
    return {
        "tables": tables,
        "joins": [{"left": docs_id, "left_field": "session_id",
                   "right": sess_id, "right_field": "id", "type": "left"}],
        "fields": [{"table": docs_id, "field": "title", "alias": "title"},
                   {"table": sess_id, "field": "name", "alias": "sname"}],
        "sorts": [{"table": docs_id, "field": "id", "dir": "asc"}],
    }


@pytest.mark.parametrize("docs_first", [True, False])
def test_left_join_owner_scoping_preserves_orphans_and_hides_other_owner(owner_scoped_db, docs_first):
    """WR-02: a non-admin LEFT-join query must (a) preserve the base table's
    orphan/unmatched rows and (b) never expose another user's non-NULL data on
    the nullable side. Runs with both table-add orders so both the default and
    the operand-flip attach branch push the nullable-side owner predicate into
    the ON clause."""
    conn, resolve, owner_clause = owner_scoped_db
    out = execute_query(conn, _owner_left_join_ast(docs_first), resolve,
                        enforce_owner="alice", owner_clause_fn=owner_clause)
    by_title = {r["title"]: r["sname"] for r in out["rows"]}
    # (a) All three of alice's docs survive — the LEFT join is NOT demoted to
    # INNER by the owner predicate.
    assert set(by_title) == {"own-session", "points-at-bob", "orphan"}
    # Alice's doc joined to her OWN session shows the session name.
    assert by_title["own-session"] == "alice-session"
    # (b) The orphan and the doc pointing at BOB's session both surface with a
    # NULL session — bob's session name must never appear.
    assert by_title["orphan"] is None
    assert by_title["points-at-bob"] is None
    assert "bob-session" not in [r["sname"] for r in out["rows"]]


def test_left_join_owner_scoping_old_behavior_would_drop_rows(owner_scoped_db):
    """Guard the direction of the fix: base-table owner predicate stays in WHERE
    (docs is the preserved side), so a doc owned by BOB is still filtered out —
    the fix preserves orphans WITHOUT weakening access control."""
    conn, resolve, owner_clause = owner_scoped_db
    conn.execute(insert(resolve("docs")), [
        {"id": "d4", "title": "bob-doc", "session_id": "s2", "owner": "bob"},
    ])
    conn.commit()
    out = execute_query(conn, _owner_left_join_ast(True), resolve,
                        enforce_owner="alice", owner_clause_fn=owner_clause)
    titles = [r["title"] for r in out["rows"]]
    assert "bob-doc" not in titles, "base-table owner scoping must still exclude bob's rows"
    assert set(titles) == {"own-session", "points-at-bob", "orphan"}
