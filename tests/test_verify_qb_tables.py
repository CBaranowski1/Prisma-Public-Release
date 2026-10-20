"""core.database.init_db() creates the 5 qb_* query-builder tables on a fresh
temp SQLite DB, without ever touching the real data/app.db."""
import sys
from unittest.mock import MagicMock

import sqlalchemy

QB_TABLE_NAMES = (
    "qb_data_sources",
    "qb_catalog_tables",
    "qb_catalog_fields",
    "qb_relationships",
    "qb_saved_queries",
)


def _heal_polluted_modules() -> None:
    """Sibling unit tests stub model modules with MagicMock at module scope,
    leaving core.database/core.querybuilder_models bound to mocks in
    sys.modules. Drop only the modules that are ACTUALLY mocks (never a real
    ORM module — SQLAlchemy would re-declare tables). Mirrors the healing
    idiom in tests/test_query_routes_registered.py."""
    for name in ("core.database", "core.querybuilder_models", "core.models"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)


def test_init_db_creates_qb_tables(tmp_path, monkeypatch) -> None:
    _heal_polluted_modules()
    import os
    import core.database as db
    from core.constants import DATA_DIR

    real_db_path = os.path.join(DATA_DIR, "app.db")
    real_db_existed_before = os.path.exists(real_db_path)
    real_db_mtime_before = os.path.getmtime(real_db_path) if real_db_existed_before else None

    tmp_db_path = tmp_path / "qb.db"
    tmp_db_url = "sqlite:///" + str(tmp_db_path).replace(chr(92), "/")
    tmp_engine = sqlalchemy.create_engine(tmp_db_url, connect_args={"check_same_thread": False})

    # Isolate BOTH the engine and DATABASE_URL: init_db()'s create_all() uses
    # `db.engine`, but its post-create_all migration helpers (_migrate_*)
    # independently recompute a raw sqlite3 connection from the module-level
    # `DATABASE_URL` global — patching engine alone would leave those helpers
    # pointed at the real data/app.db path (T-09-02).
    monkeypatch.setattr(db, "engine", tmp_engine)
    monkeypatch.setattr(db, "DATABASE_URL", tmp_db_url)
    monkeypatch.setattr(db, "SessionLocal", sqlalchemy.orm.sessionmaker(
        autocommit=False, autoflush=False, bind=tmp_engine))

    db.init_db()

    table_names = set(sqlalchemy.inspect(tmp_engine).get_table_names())
    for name in QB_TABLE_NAMES:
        assert name in table_names

    # The real production DB must never be touched by this test (T-09-02).
    if real_db_existed_before:
        assert os.path.getmtime(real_db_path) == real_db_mtime_before
    else:
        assert not os.path.exists(real_db_path)
