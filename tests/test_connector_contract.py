"""The generalized connector contract: fetch_table returns columns + row dicts,
and the sync engine materializes that into a local table — independent of source
type. A SQLite-backed SQL connector exercises the SQL path."""
import pytest
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, insert, text

import src.datasource_connectors as dc
import src.datasource_sync as sync


@pytest.fixture
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'src.db'}")
    md = MetaData()
    t = Table("widgets", md, Column("id", Integer, primary_key=True), Column("name", String))
    md.create_all(eng)
    with eng.begin() as c:
        c.execute(insert(t), [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return eng
    return _Shim()


def test_fetch_table_returns_columns_and_rows(sqlite_conn):
    st = sqlite_conn.fetch_table("widgets")
    assert {c.name for c in st.columns} == {"id", "name"}
    rows = list(st.rows)
    assert {r["name"] for r in rows} == {"a", "b"}


def test_materialize_synctable(tmp_path, sqlite_conn):
    app_engine = create_engine(f"sqlite:///{tmp_path/'app.db'}")
    st = sqlite_conn.fetch_table("widgets")
    # replace mode (default) is now crash-safe (QB-05): it stages into a fresh
    # table and returns (rows, staging_name); the caller performs the pointer
    # swap, so the live `ext_x_widgets` is untouched by this call.
    n, staging = sync.materialize_synctable(app_engine, st, "ext_x_widgets")
    assert n == 2
    assert staging != "ext_x_widgets"
    with app_engine.connect() as c:
        got = {r[0] for r in c.execute(text(f"SELECT name FROM {staging}"))}
    assert got == {"a", "b"}
