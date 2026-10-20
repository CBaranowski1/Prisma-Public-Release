"""Incremental 'upsert' sync mode: preserves existing rows across syncs and
updates rows by key, vs the default crash-safe 'replace' mode (QB-05) which
stages the full new row set into a fresh table for the caller to swap in."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.types import String, Integer

import src.datasource_connectors as dc
import src.datasource_sync as sync


def _st(rows):
    cols = [dc.SyncColumn("id", Integer()), dc.SyncColumn("name", String())]
    return dc.SyncTable(columns=cols, rows=rows)


def test_replace_mode_drops_and_recreates(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'a.db'}")
    # replace mode (QB-05) stages the full new row set into a fresh table and
    # returns (rows, staging_name); the live `ext_t` is untouched by this call
    # (the caller swaps the catalog pointer). Full-replace semantics still hold
    # within the staging table: only the new fetch's rows are present.
    sync.materialize_synctable(eng, _st([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]), "ext_t")
    n, staging = sync.materialize_synctable(eng, _st([{"id": 1, "name": "a2"}]), "ext_t")
    assert n == 1
    assert staging != "ext_t"
    with eng.connect() as c:
        got = {r[0]: r[1] for r in c.execute(text(f"SELECT id, name FROM {staging}"))}
    assert got == {1: "a2"}  # only the new fetch's row — full replace


def test_upsert_mode_preserves_and_updates(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'b.db'}")
    sync.materialize_synctable(eng, _st([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]),
                               "ext_t", mode="upsert", key="id")
    n = sync.materialize_synctable(eng, _st([{"id": 1, "name": "a2"}, {"id": 3, "name": "c"}]),
                                   "ext_t", mode="upsert", key="id")
    assert n == 2
    with eng.connect() as c:
        got = {r[0]: r[1] for r in c.execute(text("SELECT id, name FROM ext_t ORDER BY id"))}
    assert got == {1: "a2", 2: "b", 3: "c"}  # 1 updated, 2 preserved, 3 inserted


def test_upsert_requires_valid_key(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'c.db'}")
    with pytest.raises(ValueError):
        sync.materialize_synctable(eng, _st([{"id": 1, "name": "a"}]), "ext_t",
                                   mode="upsert", key="missing")
