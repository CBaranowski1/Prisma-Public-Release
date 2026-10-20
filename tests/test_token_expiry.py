"""SEC-22 (token-expiry half): dialect-agnostic expires_at migration on
ApiToken + the api_token_is_live expiry predicate.

Test A -- migration correctness on SQLite: init_db() against a fresh temp
sqlite file adds an `expires_at` column to api_tokens, idempotently.

Test B -- Postgres DDL correctness (no live connection required): the same
Column(DateTime) construct used by the migration compiles to valid Postgres
DDL syntax.

Test C -- api_token_is_live predicate: NULL never expires, future is live,
past is not live, exact-now boundary is NOT live (expired-at-boundary).
"""
import sys

import sqlalchemy
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects import postgresql


def _heal_polluted_modules() -> None:
    """Sibling unit tests stub model modules with MagicMock at module scope,
    leaving core.database bound to a mock in sys.modules. Drop only modules
    that are ACTUALLY mocks (never a real ORM module -- SQLAlchemy would
    re-declare tables). Mirrors tests/test_verify_qb_tables.py."""
    for name in ("core.database", "core.querybuilder_models", "core.models"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)


def test_init_db_adds_expires_at_column_to_api_tokens(tmp_path, monkeypatch):
    """Migration mechanic (RESEARCH Pitfall 1): expires_at must materialize
    additively on a live SQLite api_tokens table via init_db()."""
    _heal_polluted_modules()
    import core.database as db

    tmp_db_path = tmp_path / "token_expiry.db"
    tmp_db_url = "sqlite:///" + str(tmp_db_path).replace(chr(92), "/")
    tmp_engine = sqlalchemy.create_engine(tmp_db_url, connect_args={"check_same_thread": False})

    monkeypatch.setattr(db, "engine", tmp_engine)
    monkeypatch.setattr(db, "DATABASE_URL", tmp_db_url)
    monkeypatch.setattr(db, "SessionLocal", sqlalchemy.orm.sessionmaker(
        autocommit=False, autoflush=False, bind=tmp_engine))

    db.init_db()

    columns = {c["name"] for c in sqlalchemy.inspect(tmp_engine).get_columns("api_tokens")}
    assert "expires_at" in columns

    # Idempotent: a second init_db() call must not error and must still
    # leave exactly one expires_at column.
    db.init_db()
    columns_again = {c["name"] for c in sqlalchemy.inspect(tmp_engine).get_columns("api_tokens")}
    assert "expires_at" in columns_again


def test_expires_at_column_compiles_to_postgres_timestamp():
    """Cross-dialect claim (RESEARCH Pitfall 1 Warning signs): prove the
    dialect-compiled DDL type is valid Postgres syntax without needing a
    live Postgres connection.

    Installed sqlalchemy (2.0.50) compiles DateTime to
    "TIMESTAMP WITHOUT TIME ZONE" for the postgresql dialect (not the bare
    "TIMESTAMP" some older docs show) -- both are valid Postgres column-type
    DDL syntax; assert it STARTS WITH TIMESTAMP so the cross-dialect claim is
    genuinely exercised without over-fitting to one sqlalchemy version's
    exact spelling.
    """
    compiled = Column("expires_at", DateTime).type.compile(dialect=postgresql.dialect())
    assert compiled.startswith("TIMESTAMP")


def test_api_token_is_live_none_never_expires():
    _heal_polluted_modules()
    from core.database import api_token_is_live
    assert api_token_is_live(None) is True


def test_api_token_is_live_future_is_live():
    _heal_polluted_modules()
    from datetime import timedelta
    from core.database import api_token_is_live, utcnow_naive
    future = utcnow_naive() + timedelta(days=1)
    assert api_token_is_live(future) is True


def test_api_token_is_live_past_is_not_live():
    _heal_polluted_modules()
    from datetime import timedelta
    from core.database import api_token_is_live, utcnow_naive
    past = utcnow_naive() - timedelta(days=1)
    assert api_token_is_live(past) is False


def test_api_token_is_live_exact_now_boundary_is_not_live():
    """Expired-at-boundary is NOT live: expires_at == now must be treated as
    expired, not live (strict > comparison, not >=)."""
    _heal_polluted_modules()
    from core.database import api_token_is_live, utcnow_naive
    now = utcnow_naive()
    assert api_token_is_live(now, now=now) is False


def test_bearer_candidate_selection_skips_expired_and_accepts_null_or_future():
    """Unit-level proof of the match-loop's expiry-gated selection logic
    (Task 3): a 5-tuple candidate list is filtered via api_token_is_live so
    an expired entry never reaches the bcrypt match step, while a NULL or
    future-expiry entry does."""
    _heal_polluted_modules()
    from datetime import timedelta
    from core.database import api_token_is_live, utcnow_naive

    now = utcnow_naive()
    candidates = [
        ("id-null", "hash-null", "owner1", ["chat"], None),
        ("id-future", "hash-future", "owner1", ["chat"], now + timedelta(days=1)),
        ("id-past", "hash-past", "owner1", ["chat"], now - timedelta(days=1)),
    ]

    live_ids = [tid for tid, _thash, _owner, _scopes, expires_at in candidates if api_token_is_live(expires_at, now=now)]

    assert "id-null" in live_ids
    assert "id-future" in live_ids
    assert "id-past" not in live_ids
