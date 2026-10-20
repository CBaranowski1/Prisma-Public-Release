"""Internal catalog allowlist: only safe tables/columns resolve."""
import pytest
from sqlalchemy.sql.selectable import Exists
from src.query_catalog import (
    INTERNAL_TABLES, is_allowed_table, resolve_table, resolve_column,
    iter_catalog, owner_clause, QueryValidationError,
)


def test_known_table_allowed():
    assert is_allowed_table("sessions") is True
    tbl = resolve_table("sessions")
    assert tbl.name == "sessions"


def test_credential_tables_excluded():
    for forbidden in ("api_tokens", "email_accounts", "signatures",
                      "model_endpoints", "webhooks", "mcp_servers"):
        assert is_allowed_table(forbidden) is False
        with pytest.raises(QueryValidationError):
            resolve_table(forbidden)


def test_unknown_table_rejected():
    assert is_allowed_table("sqlite_master") is False
    with pytest.raises(QueryValidationError):
        resolve_table("sqlite_master")


def test_resolve_column_rejects_unlisted_column():
    # 'name' is exposed on sessions; a fabricated column is not.
    assert resolve_column("sessions", "name") is not None
    with pytest.raises(QueryValidationError):
        resolve_column("sessions", "password_hash")


def test_iter_catalog_shape():
    cat = iter_catalog()
    names = {entry["table"] for entry in cat}
    assert "sessions" in names
    entry = next(e for e in cat if e["table"] == "sessions")
    assert all("name" in c and "type" in c for c in entry["columns"])


def test_resolve_table_exposes_only_allowlisted_columns():
    # The restricted table must carry allowlisted columns and NOT the excluded,
    # sensitive ones (e.g. sessions.headers, which can hold auth headers).
    cols = set(resolve_table("sessions").c.keys())
    assert "name" in cols and "owner" in cols
    assert "headers" not in cols
    # scheduled_tasks must not expose its secret/instruction columns.
    tcols = set(resolve_table("scheduled_tasks").c.keys())
    assert "webhook_token" not in tcols
    assert "prompt" not in tcols and "endpoint_url" not in tcols


def test_owner_clause_direct_column():
    aliased = resolve_table("sessions").alias("t1")
    clause = owner_clause("sessions", aliased, "alice")
    # Direct-owner predicate, value bound (not inlined).
    compiled = clause.compile()
    assert "owner" in str(compiled)
    assert "alice" in compiled.params.values()


def test_owner_clause_via_parent_is_correlated_exists():
    aliased = resolve_table("chat_messages").alias("t1")
    clause = owner_clause("chat_messages", aliased, "alice")
    assert isinstance(clause, Exists)
    sql = str(clause.compile())
    # Scopes through the parent sessions row, value bound.
    assert "EXISTS" in sql.upper()
    assert "sessions" in sql
    assert "alice" in clause.compile().params.values()
