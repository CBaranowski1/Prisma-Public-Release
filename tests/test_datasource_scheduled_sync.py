"""run_due_syncs: only enabled + due sources are synced."""
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
import core.querybuilder_models as qm
import src.datasource_sync as sync


def _db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng)()


def _src(db, name, *, enabled, interval, last, tables=("t",)):
    ds = qm.DataSource(
        id=name, name=name, source_type="postgres", host="h", port=1, database="d",
        username="u", enabled=True, last_synced_at=last,
        options=json.dumps({"sync": {"enabled": enabled, "interval_minutes": interval,
                                     "tables": list(tables)}}))
    db.add(ds); db.commit(); return ds


def test_only_enabled_and_due_sources_sync(monkeypatch):
    db = _db()
    now = datetime(2026, 1, 1, 12, 0, 0)
    _src(db, "due", enabled=True, interval=60, last=now - timedelta(minutes=90))   # due
    _src(db, "fresh", enabled=True, interval=60, last=now - timedelta(minutes=10)) # not due
    _src(db, "off", enabled=False, interval=60, last=None)                          # disabled
    _src(db, "never", enabled=True, interval=60, last=None)                         # due (never synced)

    called = []
    monkeypatch.setattr(sync, "sync_source",
                        lambda db, ds, tables, app_engine=None: called.append(ds.id) or {"synced": tables})

    done = sync.run_due_syncs(db, now=now)
    assert set(done) == {"due", "never"}
    assert set(called) == {"due", "never"}


def test_no_sync_block_is_skipped(monkeypatch):
    db = _db()
    ds = qm.DataSource(id="plain", name="plain", source_type="postgres", enabled=True,
                       options=None)
    db.add(ds); db.commit()
    monkeypatch.setattr(sync, "sync_source",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not sync")))
    assert sync.run_due_syncs(db, now=datetime(2026, 1, 1)) == []
