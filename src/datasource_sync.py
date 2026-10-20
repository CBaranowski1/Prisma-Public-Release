"""Sync (ingest) selected external tables into the app's own store.

Full-refresh model: each synced table is dropped + recreated locally as
`ext_<sourceid>_<table>` with the same column names/types reflected from the
source, then all rows are copied in. The local copy is registered as an
external CatalogTable (+CatalogField rows) so the Phase 1 compiler can query it
as an ordinary local table. Joins therefore run as local parameterized SQL.

SQLAlchemy 2.0 note: `metadata.bind` / `table.bind` are removed in SA 2.0.
The sync engine now uses `connector.fetch_table()` which returns a `SyncTable`
(columns + row iterator). `materialize_synctable` then creates the local mirror
table and copies rows from the iterator without needing the source engine directly.
"""
import json as _json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from sqlalchemy import MetaData, Table, Column, insert, inspect, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import Integer, Float, Boolean, String, DateTime

from core.database import engine as app_engine_default, utcnow_naive
from core.querybuilder_models import CatalogTable, CatalogField, DataSource, _uuid
from src.datasource_connectors import get_connector, SyncTable, ConnectorError
from src.secret_scrub import scrub_dsn

logger = logging.getLogger(__name__)

_BATCH = 1000
MAX_SYNC_ROWS = 1_000_000  # disk-DoS guard: stop materializing past this many rows
_SYNC_LOCK_TTL_MINUTES = 30  # QB-06: bounded, generous vs MAX_SYNC_ROWS-sized syncs

# QB-18: single source of truth (D-15/D-17, Pitfall 3) -- names safe to DROP,
# word chars only (no raw uuid4 hyphens, Pitfall 2). routes/datasource_routes.py
# imports this constant rather than defining its own copy, so the two DROP
# call sites (delete_source, sweep_orphan_ext_tables) can never drift apart.
_SAFE_PHYSICAL_TABLE = re.compile(r"^ext_[A-Za-z0-9_]+$")

# HG-01: in-process registry of in-flight physical table names -- i.e.
# CREATEd (replace mode: the fresh staging table; upsert mode: `phys` itself
# on a table's first-ever sync) but not yet pointed at by a committed
# CatalogTable.physical_table_name. Such a table is INDISTINGUISHABLE by
# shape alone from a settled, already-swapped-in mirror (both match
# `_SAFE_PHYSICAL_TABLE`), so sweep_orphan_ext_tables cannot tell the two
# apart from names/DB state alone. This registry is the chosen rule, ONE
# consistent ownership model covering BOTH sync modes: `sync_source` (the
# caller) reserves the relevant name for the WHOLE window between that
# table's CREATE and the catalog-pointer-swap commit (or the best-effort
# DROP on failure) -- released in the per-table loop's `finally` clause,
# never inside `materialize_synctable` itself -- and
# `sweep_orphan_ext_tables` subtracts a snapshot of it from its drop
# candidates, in addition to the committed-pointer "live" set. A bare
# in-process set is sufficient for compatibility callers that invoke syncs
# concurrently across different DataSources as threads within this same
# process (D-12) -- the exact surface this guards.
# T-22-18-DROP gap-closure (post-22-05 audit): the original HG-01 fix only
# reserved replace-mode staging names; upsert-mode's structurally identical
# first-sync CREATE window was unreserved until this fix extended the SAME
# registry/ownership model to that branch too (see the per-table loop below).
_staging_reservation_lock = threading.Lock()
_reserved_staging_tables: set = set()


def _reserve_staging_table(name: str) -> None:
    with _staging_reservation_lock:
        _reserved_staging_tables.add(name)


def _release_staging_table(name: str) -> None:
    with _staging_reservation_lock:
        _reserved_staging_tables.discard(name)


def _reserved_staging_tables_snapshot() -> set:
    with _staging_reservation_lock:
        return set(_reserved_staging_tables)


def physical_name(source_id: str, table: str) -> str:
    """Return the local mirror table name for an external table."""
    safe_sid = "".join(ch for ch in source_id if ch.isalnum())
    safe_tbl = "".join(ch if ch.isalnum() else "_" for ch in table)
    return f"ext_{safe_sid}_{safe_tbl}"


def _coerce(v):
    if isinstance(v, (dict, list)):
        return _json.dumps(v, ensure_ascii=False)
    return v


# QB-10: static type-name allowlist for `resolve_effective_types()` (D-12).
# Maps BOTH the reflection-formatted UPPER names `str(Integer())` etc. produce
# (`INTEGER`/`FLOAT`/`BOOLEAN`/`VARCHAR`/`DATETIME` -- the shape
# `CatalogField.data_type` is persisted in, per `_upsert_catalog`'s
# `data_type=str(col.type_)` write) AND the CamelCase SQLAlchemy class names a
# user would write in `options.type_overrides` (`Integer`/`Float`/`Boolean`/
# `String`/`DateTime`) to the five generic SQLAlchemy type classes. A STATIC
# dict-literal lookup ONLY -- NEVER `getattr(sqlalchemy.types, name)` on
# user-controlled input (T-23-07; an unknown/invalid name must fall through
# to prior/inferred, never resolve to an arbitrary attribute).
_TYPE_BY_NAME = {
    "INTEGER": Integer, "Integer": Integer,
    "FLOAT": Float, "Float": Float,
    "BOOLEAN": Boolean, "Boolean": Boolean,
    "VARCHAR": String, "String": String,
    "DATETIME": DateTime, "DateTime": DateTime,
}


def resolve_effective_types(existing_fields, sync_table: SyncTable, type_overrides: dict) -> None:
    """Mutate `sync_table.columns[i].type_` in place per D-11 precedence:
    override > prior `CatalogField.data_type` > freshly inferred.

    Must be called BETWEEN `connector.fetch_table()` and
    `materialize_synctable()` (never after) so both the physical mirror DDL
    (`Column(c.name, c.type_)`) and the catalog write (`data_type=str(col.
    type_)`) see the identical resolved type -- no split-brain (D-11).

    `existing_fields`: the PRE-delete `CatalogField` snapshot for this
    table's `CatalogTable`, queried ONCE by the caller (`sync_source`) and
    shared with `_upsert_catalog` (Pitfall 6) -- this function never queries
    the database itself. Empty list on a table's first-ever sync (no prior
    row to preserve -- keep the freshly inferred type).

    `type_overrides`: `{"<column>": "<SA type name>"}` for THIS table, read
    from `ds.options.type_overrides` by the caller. An override name absent
    from `_TYPE_BY_NAME` is ignored (logged) and falls through to prior/
    inferred -- never fatal (D-12).
    """
    prior_by_name = {f.name: f.data_type for f in existing_fields}
    overrides = type_overrides or {}
    for col in sync_table.columns:
        override_name = overrides.get(col.name)
        if override_name:
            if override_name in _TYPE_BY_NAME:
                col.type_ = _TYPE_BY_NAME[override_name]()
                continue
            logger.warning(
                "Ignoring unknown type_overrides value %r for column %r "
                "(not in the allowlist %s); falling back to prior/inferred type",
                override_name, col.name, sorted(_TYPE_BY_NAME))
        prior = prior_by_name.get(col.name)
        if prior and prior in _TYPE_BY_NAME:
            col.type_ = _TYPE_BY_NAME[prior]()
        # else: no (valid) override, no (valid) prior -- a brand-new column,
        # or a prior data_type outside the allowlist -- keep the freshly
        # inferred type_ as-is.


def _new_staging_name(phys: str) -> str:
    """Return a unique per-sync staging table name derived from `phys`.

    Word-chars only (matches `_SAFE_PHYSICAL_TABLE` -- no raw uuid4 hyphens,
    Pitfall 2), so a successful swap can safely point physical_table_name at
    it. The suffix is unique PER CALL: after a swap, physical_table_name
    points AT this table, so a fixed suffix would make the NEXT sync's
    staging.drop() empty the live mirror -- reintroducing the exact QB-05 bug
    (Pitfall 1).
    """
    return f"{phys}__{uuid.uuid4().hex[:8]}"


def materialize_synctable(app_engine, sync_table: SyncTable, phys: str,
                          *, mode: str = "replace", key: str = None,
                          staging_name: str = None):
    """Materialize a SyncTable locally.

    mode="upsert": create `phys` if absent (preserving existing data), then for
    each batch delete rows whose `key` matches and re-insert — so changed rows
    are updated, new rows added, and rows not present in this fetch are KEPT.
    `key` must be a column name in the SyncTable. Returns the int row count
    written (unchanged behavior -- D-03).

    mode="replace" (default): full refresh, crash-safe (QB-05). Materializes
    into a FRESH, uniquely-named staging table -- never the live `phys` table
    -- so a mid-fill connector/driver failure never touches the current
    mirror. `staging_name`, if given, is used verbatim (HG-01: the caller
    pre-generates and reserves it via `_reserve_staging_table` BEFORE this
    call, so the reservation covers the table from its CREATE onward -- not
    just from whenever this function happened to pick a name); if omitted, a
    fresh name is generated internally (back-compat for direct callers that
    don't need the HG-01 registry, e.g. unit tests exercising this function in
    isolation). On success returns `(rows, staging_name)`; the caller performs
    the atomic catalog-pointer swap (D-04). On ANY failure the staging table
    is best-effort dropped and the exception re-raised: the old mirror +
    pointer are left fully intact (D-01, D-02).
    """
    cols_by_name = {c.name for c in sync_table.columns}
    if mode == "upsert" and (not key or key not in cols_by_name):
        raise ValueError(f"upsert mode requires a 'key' column present in the table; got {key!r}")
    md = MetaData()
    cols = [Column(c.name, c.type_) for c in sync_table.columns]

    if mode == "upsert":
        local = Table(phys, md, *cols)
        local.create(app_engine, checkfirst=True)  # keep existing rows

        def _flush(dst, batch):
            if not batch:
                return 0
            dst.execute(local.delete().where(local.c[key].in_([r[key] for r in batch])))
            dst.execute(insert(local), batch)
            return len(batch)

        total = 0
        batch = []
        try:
            with app_engine.begin() as dst:
                for row in sync_table.rows:
                    # Keep only known columns; JSON-stringify nested values for TEXT cols.
                    batch.append({c.name: _coerce(row.get(c.name)) for c in sync_table.columns})
                    if len(batch) >= _BATCH:
                        total += _flush(dst, batch)
                        batch = []
                        if total >= MAX_SYNC_ROWS:  # bound disk usage from a runaway source
                            break
                total += _flush(dst, batch)
        finally:
            # WR-04 (LOW): explicitly close the row generator on every exit
            # path (early MAX_SYNC_ROWS break, an exception, or normal
            # exhaustion) so resources it may be holding open (an httpx.
            # Client, engine.connect(), an openpyxl workbook) are released
            # deterministically rather than relying on GC/refcounting once
            # this loop stops consuming it early. No-op (via getattr's
            # default) if `rows` is a plain list/iterator without .close().
            getattr(sync_table.rows, "close", lambda: None)()
        return total

    # mode == "replace": stage into a fresh, uniquely-named table -- never the
    # live mirror (QB-05 / D-01). Table.create/Table.drop auto-commit
    # immediately (SA 2.0 DDL semantics, Pitfall 1) -- atomicity belongs on
    # the catalog-pointer commit the caller performs after this returns, not
    # on this DDL.
    staging_name = staging_name or _new_staging_name(phys)
    staging = Table(staging_name, md, *cols)
    staging.drop(app_engine, checkfirst=True)  # clear leftover from a prior crash (unique name -- normally a no-op)
    staging.create(app_engine)

    def _flush(dst, batch):
        if not batch:
            return 0
        dst.execute(insert(staging), batch)
        return len(batch)

    try:
        total = 0
        batch = []
        with app_engine.begin() as dst:
            for row in sync_table.rows:
                batch.append({c.name: _coerce(row.get(c.name)) for c in sync_table.columns})
                if len(batch) >= _BATCH:
                    total += _flush(dst, batch)
                    batch = []
                    if total >= MAX_SYNC_ROWS:
                        break
            total += _flush(dst, batch)
    except Exception:
        staging.drop(app_engine, checkfirst=True)  # best-effort; old mirror + pointer untouched
        raise
    finally:
        # WR-04 (LOW): same explicit-close guarantee as the upsert branch
        # above, on every exit path (early MAX_SYNC_ROWS break, exception,
        # or normal exhaustion).
        getattr(sync_table.rows, "close", lambda: None)()
    return total, staging_name


def _diff_catalog_columns(existing_fields, incoming_columns) -> dict:
    """Name-only column diff between the PRE-delete CatalogField snapshot for
    a table and its incoming SyncTable.columns (QB-07 / D-13).

    Deliberately diffs NAMES only -- not `data_type` -- because
    `str(col.type_)` reflection formatting can vary subtly across separate
    reflect calls even when a column is genuinely unchanged, which would
    manufacture false "drift" on every sync (Pitfall 4). Name presence/
    absence is the stable, useful signal.
    """
    existing_names = {f.name for f in existing_fields}
    incoming_names = {c.name for c in incoming_columns}
    return {
        "added": sorted(incoming_names - existing_names),
        "removed": sorted(existing_names - incoming_names),
    }


def _upsert_catalog(db, ds, table: str, phys: str, sync_table: SyncTable, existing_fields):
    """Create or update the external CatalogTable + CatalogField rows.

    Sets `ct.physical_table_name = phys` (the new mirror -- for replace mode
    this is the freshly-filled staging table; for upsert mode it is the same
    deterministic name as always, D-03 unchanged). The single `db.commit()`
    the caller issues right after this call is the atomic pointer-swap step
    (D-04). Returns `(prior_phys, drift)`:
      - `prior_phys`: the PRIOR `physical_table_name` (None on a table's
        first sync) so the caller can best-effort DROP the table it just
        replaced -- strictly AFTER that commit succeeds.
      - `drift`: `{"added": [...], "removed": [...]}` (QB-07 / D-13),
        name-only diff of the PRE-delete CatalogField snapshot against the
        incoming columns; `{"added": [], "removed": []}` on a table's
        first-ever sync (nothing to diff against yet).
    This function never drops anything and never blocks the sync on drift
    (D-13) -- reporting only.

    `existing_fields`: the PRE-delete CatalogField snapshot for this table,
    queried ONCE by the caller (`sync_source`, before `materialize_synctable`)
    and shared with `resolve_effective_types()` (QB-10 / Pitfall 6) -- this
    function no longer queries CatalogField itself, so the drift diff and the
    delete/re-add below see the exact same snapshot `resolve_effective_types`
    used to resolve types, never a second, potentially-inconsistent read.
    """
    ct = (db.query(CatalogTable)
          .filter(CatalogTable.source_id == ds.id, CatalogTable.name == table)
          .first())
    prior_phys = None
    drift = {"added": [], "removed": []}
    if ct is None:
        ct = CatalogTable(id=_uuid(), source_id=ds.id, name=table,
                          display_name=f"{ds.name}: {table}", is_internal=False,
                          physical_table_name=phys)
        db.add(ct)
        db.flush()
    else:
        prior_phys = ct.physical_table_name
        ct.physical_table_name = phys
        ct.display_name = f"{ds.name}: {table}"
        drift = _diff_catalog_columns(existing_fields, sync_table.columns)
        db.query(CatalogField).filter(CatalogField.table_id == ct.id).delete()
    ct.last_synced_at = utcnow_naive()
    for col in sync_table.columns:
        db.add(CatalogField(id=_uuid(), table_id=ct.id, name=col.name,
                            data_type=str(col.type_)))
    return prior_phys, drift


def _try_acquire_sync_lock(db, ds_id: str, ttl_minutes: int = _SYNC_LOCK_TTL_MINUTES) -> bool:
    """Atomically claim the per-source sync lock (QB-06 / D-08).

    A single UPDATE...WHERE + rowcount check -- the read-check and the claim
    are the SAME statement, so two concurrent callers can never both pass a
    separate read-check before either commits (Pitfall 5). NULL-safe: a
    never-synced or pre-migration row (sync_started_at IS NULL) is treated as
    immediately reclaimable, not permanently wedged (Pitfall 7).

    Commits any transaction already open on `db` before issuing the CAS
    UPDATE. Callers (the `/sync` route, `run_due_syncs`) typically load `ds`
    on this same session first; ending that read transaction here guarantees
    the UPDATE begins its own fresh transaction against current data rather
    than depending on how long the caller's own earlier read has been open --
    cheap defensive hygiene for the guard that IS the correctness contract.
    """
    db.commit()
    now = utcnow_naive()
    cutoff = now - timedelta(minutes=ttl_minutes)
    result = db.execute(
        update(DataSource)
        .where(DataSource.id == ds_id)
        .where(
            (DataSource.last_sync_status != "running")
            | (DataSource.sync_started_at == None)  # noqa: E711 -- NULL-safe reclaim (Pitfall 7)
            | (DataSource.sync_started_at < cutoff)
        )
        .values(last_sync_status="running", sync_started_at=now)
    )
    db.commit()
    return result.rowcount == 1


def sweep_orphan_ext_tables(db, app_engine=None) -> List[str]:
    """Best-effort DROP of every `_SAFE_PHYSICAL_TABLE`-shaped physical table
    with no CURRENT `CatalogTable.physical_table_name` pointer (QB-18 / D-15).

    Reuses `routes/datasource_routes.py::delete_source`'s guard-then-DROP
    idiom: enumerate physical tables via `sqlalchemy.inspect(...)
    .get_table_names()` (dialect-agnostic -- never hand-roll a
    `sqlite_master` query, D-19/Pitfall 5), filter to `_SAFE_PHYSICAL_TABLE`-
    shaped names, subtract (a) every non-null `CatalogTable.physical_table_name`
    across ALL sources (a live mirror -- or a table a concurrent sync is
    mid-swap on -- is never dropped, D-15) AND (b) every name currently held
    in the `_reserved_staging_tables` in-process registry (HG-01: a DIFFERENT,
    concurrently-syncing DataSource's staging table that has been CREATEd but
    not yet pointed at by a committed catalog row -- indistinguishable from a
    settled mirror by name/shape alone, since the swap never renames the
    table). Then DROPs each remaining name. Never raises: an enumeration or
    DROP failure is logged (scrubbed) and skipped, matching the narrowed
    `except SQLAlchemyError` idiom this phase preserves verbatim (D-17).
    Returns the sorted list of table names actually dropped.
    """
    engine_ = app_engine or app_engine_default
    try:
        all_tables = inspect(engine_).get_table_names()
    except SQLAlchemyError as e:
        logger.warning("Orphan sweep failed to enumerate tables: %s", scrub_dsn(str(e)))
        return []
    candidates = {t for t in all_tables if _SAFE_PHYSICAL_TABLE.match(t)}
    live = {
        row[0] for row in
        db.query(CatalogTable.physical_table_name)
          .filter(CatalogTable.physical_table_name.isnot(None))
          .all()
    }
    reserved = _reserved_staging_tables_snapshot()  # HG-01: in-flight, not-yet-pointed staging tables
    candidates -= reserved
    dropped = []
    for name in sorted(candidates - live):
        try:
            with engine_.begin() as conn:
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
            dropped.append(name)
        except SQLAlchemyError as e:
            logger.warning("Orphan sweep failed to drop %r: %s", name, scrub_dsn(str(e)))
    return dropped


def sync_source(db, ds, tables: List[str], app_engine=None) -> Dict:
    """Sync each named table from `ds` into the app store.

    Returns {"synced": [...], "rows": {...}, "errors": {...}, "drift": {...},
    "truncated": bool, "rows_truncated": {...}}, plus {"already_running": True}
    instead of doing any work if a concurrent sync for the same DataSource
    already holds the lock (QB-06). `drift` maps table name ->
    {"added": [...], "removed": [...]} ONLY for tables whose incoming columns
    differ from their prior CatalogField snapshot (QB-07 / D-13) -- drift is
    reporting only and never blocks the sync; the mirror still updates to the
    live shape.

    MD-01: `truncated` is True and `rows_truncated[table]` is True for any
    table whose row count hit `MAX_SYNC_ROWS` -- the disk-DoS guard silently
    stops materializing past that many rows, and without this flag the
    truncated mirror looked identical to a fully-synced one (`out["synced"]`
    /`out["rows"]` are set the same either way; only this flag distinguishes
    a complete sync from a truncated one). When ANY table truncates and there
    were no other errors, `last_sync_status` is set to "ok_truncated" (rather
    than a bare "ok") so this is visible on the DataSource row itself, not
    only in this per-call return value.

    Updates ds.last_synced_at / last_sync_status / last_sync_error, then
    best-effort sweeps orphaned ext_* tables (QB-18) -- a sweep failure is
    logged and never propagates.
    """
    app_engine = app_engine or app_engine_default
    out: Dict = {"synced": [], "rows": {}, "errors": {}, "drift": {},
                 "truncated": False, "rows_truncated": {}}
    if not _try_acquire_sync_lock(db, ds.id):
        out["already_running"] = True
        return out

    # CR-01/LW-01: everything from here on -- connector construction, the
    # per-table loop, and the post-loop status commit -- is wrapped so the
    # QB-06 lock acquired above is RELEASED on every exit path, not just the
    # per-table failures the inner loop already handles. Before this fix,
    # get_connector(ds) ran unguarded: a deterministic ConnectorError (an
    # SSRF-blocked host, or an unsupported source_type) propagated straight
    # out of sync_source, skipping the post-loop status update entirely and
    # leaving the DataSource wedged in last_sync_status="running" for the
    # full _SYNC_LOCK_TTL_MINUTES -- every retry then saw a false
    # already_running instead of the real, persistent error.
    try:
        connector = get_connector(ds)
        # Per-source sync mode/keys live in options.sync: {"mode": "replace"|"upsert",
        # "keys": {"<table>": "<key_column>"}}. Default is full-refresh "replace".
        # QB-10: options.type_overrides = {"<table>": {"<column>": "<SA type name>"}}
        # -- parsed from the SAME options blob, mirroring the options.sync read.
        try:
            _opts_parsed = _json.loads(ds.options or "{}") or {}
        except (ValueError, TypeError):
            _opts_parsed = {}
        _sync_cfg = _opts_parsed.get("sync") or {}
        _mode = _sync_cfg.get("mode", "replace")
        _keys = _sync_cfg.get("keys") or {}
        _type_overrides = _opts_parsed.get("type_overrides") or {}

        for table in tables:
            _reserved_staging = None  # HG-01: staging name reserved for this iteration, if any
            try:
                st = connector.fetch_table(table)
                phys = physical_name(ds.id, table)

                # QB-10 / Pitfall 6: hoist the CatalogField snapshot ONCE,
                # here -- BEFORE materialize_synctable -- and share it with
                # BOTH resolve_effective_types() (below) and _upsert_catalog()
                # (after materialize) so the mirror DDL and the catalog write
                # can never disagree on a column's type (no split-brain,
                # D-11). Empty list on a table's first-ever sync (no
                # CatalogTable yet) -- resolution keeps the freshly inferred
                # types in that case.
                _ct_for_types = (db.query(CatalogTable)
                                 .filter(CatalogTable.source_id == ds.id, CatalogTable.name == table)
                                 .first())
                existing_fields = (
                    db.query(CatalogField).filter(CatalogField.table_id == _ct_for_types.id).all()
                    if _ct_for_types is not None else []
                )
                resolve_effective_types(existing_fields, st, _type_overrides.get(table) or {})

                if _mode == "upsert":
                    # T-22-18-DROP gap-closure: `phys` is the SAME name
                    # materialize_synctable's upsert branch creates via
                    # `local.create(checkfirst=True)`. On a table's first-ever
                    # upsert sync, no CatalogTable row exists yet (that only
                    # happens in `_upsert_catalog`, called below, AFTER
                    # materialize_synctable returns) -- so for the whole
                    # fetch/insert loop `phys` is `_SAFE_PHYSICAL_TABLE`-shaped,
                    # absent from the "live" CatalogTable.physical_table_name
                    # set, AND (before this fix) absent from the HG-01
                    # registry: indistinguishable from a genuine orphan to a
                    # DIFFERENT, concurrently-syncing source's
                    # sweep_orphan_ext_tables. Reserve it here, BEFORE
                    # materialize_synctable's `local.create(...)` runs, mirroring
                    # exactly how the replace-mode branch below reserves its
                    # staging name before that mode's CREATE. Released in the
                    # same `finally` as the replace-mode reservation --  ONE
                    # consistent ownership model: `sync_source` (the caller)
                    # owns reservation/release for BOTH modes;
                    # materialize_synctable never reserves anything itself.
                    _reserved_staging = phys
                    _reserve_staging_table(_reserved_staging)
                    materialized = materialize_synctable(app_engine, st, phys,
                                                         mode=_mode, key=_keys.get(table))
                else:
                    # HG-01: reserve the staging name BEFORE materialize_synctable
                    # CREATEs it, so a DIFFERENT, concurrently-syncing source's
                    # sweep_orphan_ext_tables can never classify it as an orphan
                    # for the whole window between CREATE and the catalog-pointer
                    # commit below (released in the `finally` clause of this loop).
                    _reserved_staging = _new_staging_name(phys)
                    _reserve_staging_table(_reserved_staging)
                    materialized = materialize_synctable(app_engine, st, phys,
                                                         mode=_mode, key=_keys.get(table),
                                                         staging_name=_reserved_staging)
                # replace mode (QB-05): materialize_synctable stages into a fresh,
                # uniquely-named table and returns it for the caller to swap the
                # pointer to. upsert mode (D-03, unchanged): same deterministic
                # name as always -- no swap, no old-table drop below.
                if _mode == "upsert":
                    count, phys_new = materialized, phys
                else:
                    count, phys_new = materialized
                prior_phys, drift = _upsert_catalog(db, ds, table, phys_new, st, existing_fields)
                db.commit()  # the atomic pointer swap (D-04) -- nothing before this is visible
                out["synced"].append(table)
                out["rows"][table] = count
                if drift["added"] or drift["removed"]:  # QB-07/D-13: reporting only, never noisy
                    out["drift"][table] = drift
                if count >= MAX_SYNC_ROWS or getattr(st, "truncated", False):
                    # MD-01: the row-cap guard (MAX_SYNC_ROWS) silently
                    # stopped materializing early, OR the connector itself
                    # signalled a byte-cap truncation (QB-17/D-16 -- File/
                    # REST's streaming budget, `SyncTable.truncated`) --
                    # EITHER firing means a truncated mirror must never look
                    # like a complete sync (Open Question 3: reuse the SAME
                    # boolean verbatim for both caps).
                    out["truncated"] = True
                    out["rows_truncated"][table] = True

                # Best-effort DROP of the table just replaced -- only AFTER the
                # commit above has repointed the catalog (D-02, D-04). Never
                # fires for upsert mode (phys_new == prior_phys, same physical
                # table) or on a table's first-ever sync (prior_phys is None). A
                # failed drop is logged, not fatal -- it becomes a QB-18 orphan.
                if prior_phys and prior_phys != phys_new and _SAFE_PHYSICAL_TABLE.match(prior_phys):
                    try:
                        with app_engine.begin() as conn:
                            conn.execute(text(f'DROP TABLE IF EXISTS "{prior_phys}"'))
                    except SQLAlchemyError as e:
                        logger.warning("Failed to drop old mirror table %r: %s",
                                      prior_phys, scrub_dsn(str(e)))
            except (ConnectorError, SQLAlchemyError) as e:  # one bad table must not abort the rest
                db.rollback()
                out["errors"][table] = scrub_dsn(str(e))
                logger.warning("sync of %s.%s failed: %s", ds.name, table, scrub_dsn(str(e)))
            except Exception as e:  # noqa: BLE001 -- WR-04: untyped failures (e.g. the
                # upsert-without-key ValueError from materialize_synctable, or a
                # driver-native/_coerce error) must not abort the remaining tables
                # or bypass credential scrubbing (SEC-05/D-10). Typed branch above
                # stays explicit for telemetry.
                db.rollback()
                out["errors"][table] = scrub_dsn(str(e))
                logger.warning("sync of %s.%s failed with an unexpected error: %s",
                               ds.name, table, scrub_dsn(str(e)))
            finally:
                # HG-01 (covers BOTH modes -- T-22-18-DROP gap-closure):
                # release the reservation once this iteration ends -- on
                # success the catalog-pointer commit above already made the
                # DB-derived "live" set cover it. On failure: replace mode's
                # materialize_synctable has already best-effort dropped the
                # staging table; upsert mode leaves `phys` as-is (preserving
                # whatever rows it holds, by design -- upsert never drops on
                # failure). Either way the table no longer needs registry
                # protection from a concurrent sweep once this iteration ends.
                if _reserved_staging is not None:
                    _release_staging_table(_reserved_staging)

        ds.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if out["errors"]:
            ds.last_sync_status = "error"
        elif out["truncated"]:  # MD-01: visible on the DataSource row, not just this return value
            ds.last_sync_status = "ok_truncated"
        else:
            ds.last_sync_status = "ok"
        ds.last_sync_error = "; ".join(f"{t}: {e}" for t, e in out["errors"].items()) or None
        ds.sync_started_at = None  # LW-01: release the TTL clock along with the lock
        db.commit()
    except Exception as e:  # noqa: BLE001 -- CR-01: the ONLY things that can reach
        # this except are failures OUTSIDE the per-table loop's own handling --
        # concretely, get_connector(ds) raising ConnectorError (SSRF-blocked
        # host / unsupported source_type). The per-table loop above already
        # catches and records everything else, so this never double-reports a
        # per-table error. Must release the lock (status="error",
        # sync_started_at=None) and MUST re-raise -- never swallow -- so the
        # real error still reaches the /sync route's ConnectorError -> 400
        # handler and run_due_syncs' own scrub/log.
        db.rollback()
        ds.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
        ds.last_sync_status = "error"
        ds.last_sync_error = scrub_dsn(str(e))
        ds.sync_started_at = None  # LW-01: release the TTL clock along with the lock
        db.commit()
        raise

    # QB-18: best-effort orphan-table reclaim, once per sync_source
    # invocation (never per-table/in the hot loop above). Its own try/except
    # -- separate from the per-table typed/trailing excepts -- so a sweep
    # failure can NEVER abort or retroactively fail an already-committed sync
    # (T-22-18-SWEEPFAIL / D-15).
    try:
        sweep_orphan_ext_tables(db, app_engine)
    except Exception as e:  # noqa: BLE001 -- maintenance sweep must never fail a real sync
        logger.warning("Orphan sweep errored during sync_source: %s", scrub_dsn(str(e)))

    return out


def run_due_syncs(db, app_engine=None, now=None) -> list:
    """Sync every DataSource whose options.sync says it's enabled and due.
    Returns the ids synced. Best-effort: one source's failure is logged and
    does not stop the others."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    synced = []
    for ds in db.query(DataSource).filter(DataSource.enabled == True).all():  # noqa: E712
        try:
            opts = _json.loads(ds.options or "{}")
        except (ValueError, TypeError):
            continue
        cfg = (opts or {}).get("sync") or {}
        if not cfg.get("enabled"):
            continue
        interval = int(cfg.get("interval_minutes", 60) or 60)
        last = ds.last_synced_at
        if last is not None and (last + timedelta(minutes=interval)) > now:
            continue  # not due yet
        tables = cfg.get("tables") or []
        try:
            sync_source(db, ds, tables, app_engine=app_engine)
            synced.append(ds.id)
        except (ConnectorError, SQLAlchemyError) as e:
            logger.warning("scheduled sync of %s failed: %s", ds.name, scrub_dsn(str(e)))
        except Exception as e:  # noqa: BLE001 -- WR-04: an untyped failure (e.g.
            # a driver-native or _coerce/json error) must not abort the
            # remaining due sources or bypass credential scrubbing
            # (SEC-05/D-10). Typed branch above stays explicit for telemetry.
            logger.warning("scheduled sync of %s failed with an unexpected error: %s",
                           ds.name, scrub_dsn(str(e)))
    return synced
