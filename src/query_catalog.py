"""Allowlist of internal tables/columns the visual query builder may query.

Only safe, non-credential tables/columns are exposed. The query compiler
resolves every table reference through this module and receives a RESTRICTED
Core Table that exposes ONLY the allowlisted columns (same table/column names
as the real table, so generated SQL is identical) — a column absent from the
allowlist therefore can never appear in a generated query, and identifiers are
never taken from raw input.

Each table also declares how to owner-scope it (`owner`), so the compiler can
restrict non-admin callers to their own rows even for tables (like
chat_messages) that have no `owner` column of their own and are owned via a
parent row. A table with no `owner` declaration cannot be scoped and is
rejected for non-admin callers (fail closed).
"""
from typing import Any, Dict, List

from sqlalchemy import Table, MetaData, Column, select, literal

from core.database import (
    Session, ChatMessage, Document, Memory, GalleryImage,
    ScheduledTask, CrewMember,
)


class QueryValidationError(Exception):
    """Raised when an AST references a table/column/operator not on the allowlist."""


# logical name -> metadata.
#   columns: explicit exposed-column allowlist
#   owner:   how to scope rows to a user. Either {"column": "<col>"} for a
#            direct owner column, or {"via": {...}} to scope through a parent
#            row via a correlated EXISTS. A table with no `owner` entry cannot
#            be owner-scoped and is rejected for non-admin callers.
INTERNAL_TABLES: Dict[str, dict] = {
    "sessions": {
        "display": "Chat Sessions",
        "model": Session,
        "columns": ("id", "name", "model", "owner", "archived", "folder",
                    "mode", "message_count", "created_at", "updated_at",
                    "last_message_at", "crew_member_id"),
        "owner": {"column": "owner"},
    },
    "chat_messages": {
        "display": "Chat Messages",
        "model": ChatMessage,
        "columns": ("id", "session_id", "role", "content", "timestamp"),
        # No own `owner` column — owned via the parent session row.
        "owner": {"via": {"parent": "sessions", "local_field": "session_id",
                          "parent_key": "id", "parent_owner": "owner"}},
    },
    "documents": {
        "display": "Documents",
        "model": Document,
        "columns": ("id", "session_id", "title", "language", "owner",
                    "archived", "version_count", "created_at", "updated_at"),
        "owner": {"column": "owner"},
    },
    "memories": {
        "display": "Memories",
        "model": Memory,
        "columns": ("id", "text", "category", "source", "owner",
                    "session_id", "timestamp"),
        "owner": {"column": "owner"},
    },
    "gallery_images": {
        "display": "Gallery Images",
        "model": GalleryImage,
        "columns": ("id", "filename", "prompt", "model", "owner", "album_id",
                    "favorite", "created_at"),
        "owner": {"column": "owner"},
    },
    "scheduled_tasks": {
        "display": "Scheduled Tasks",
        # NOTE: `prompt`, `endpoint_url`, and `webhook_token` exist on the model
        # but are deliberately excluded — they carry instructions/secrets. The
        # restricted table built below means they are not even queryable.
        "model": ScheduledTask,
        "columns": ("id", "name", "owner", "task_type", "schedule", "status",
                    "next_run", "last_run", "run_count", "created_at"),
        "owner": {"column": "owner"},
    },
    "crew_members": {
        "display": "Crew Members",
        "model": CrewMember,
        "columns": ("id", "name", "owner", "model", "is_active",
                    "sort_order", "created_at"),
        "owner": {"column": "owner"},
    },
}

# Restricted Core Tables (only allowlisted columns), built lazily + cached.
_restricted_md = MetaData()
_restricted_cache: Dict[str, Table] = {}


def is_allowed_table(name: str) -> bool:
    return name in INTERNAL_TABLES


def resolve_table(name: str):
    """Return a RESTRICTED Core Table for an allowlisted internal table.

    The returned table carries the same name and the allowlisted columns only
    (re-declared with their original types), so generated SQL is identical to
    the real table but excluded columns are physically absent from ``.c`` and
    cannot be referenced. Raises for non-allowlisted tables."""
    if name not in INTERNAL_TABLES:
        raise QueryValidationError(f"Unknown or forbidden table: {name!r}")
    if name not in _restricted_cache:
        full = INTERNAL_TABLES[name]["model"].__table__
        cols = [Column(c, full.c[c].type) for c in INTERNAL_TABLES[name]["columns"]]
        _restricted_cache[name] = Table(name, _restricted_md, *cols)
    return _restricted_cache[name]


def resolve_column(name: str, column: str):
    if name not in INTERNAL_TABLES:
        raise QueryValidationError(f"Unknown or forbidden table: {name!r}")
    if column not in INTERNAL_TABLES[name]["columns"]:
        raise QueryValidationError(f"Unknown or forbidden column: {name}.{column}")
    return resolve_table(name).c[column]


def owner_clause(name: str, aliased, user: str):
    """Build a WHERE predicate restricting ``aliased`` (an alias of table
    ``name``) to rows owned by ``user``.

    Direct-owner tables compare their `owner` column; parent-owned tables
    (e.g. chat_messages) emit a correlated ``EXISTS`` against the parent. A
    table with no declared ownership raises — the compiler fails closed rather
    than returning unscoped rows."""
    spec = INTERNAL_TABLES.get(name)
    if not spec:
        raise QueryValidationError(f"Unknown or forbidden table: {name!r}")
    own = spec.get("owner")
    if not own:
        raise QueryValidationError(f"Table {name!r} cannot be owner-scoped")
    if "column" in own:
        return aliased.c[own["column"]] == user
    via = own["via"]
    parent = resolve_table(via["parent"]).alias()
    return (
        select(literal(1)).select_from(parent)
        .where(parent.c[via["parent_key"]] == aliased.c[via["local_field"]])
        .where(parent.c[via["parent_owner"]] == user)
        .exists()
    )


def _collect_field_refs(definition: dict) -> List[dict]:
    """Collect every {table, field} reference in a query definition's
    select/calculated/filters/group_by/sorts.

    Mirrors query_compiler.py's own plain dict/list AST walk (`_resolve_
    instances`/`_col`/`_build_filters`) rather than introducing a new
    visitor abstraction -- the AST is small and fully owned by this codebase
    (RESEARCH Don't-Hand-Roll)."""
    refs: List[dict] = []
    for spec in (definition.get("fields") or []):
        if (isinstance(spec, dict) and isinstance(spec.get("table"), str)
                and isinstance(spec.get("field"), str)):
            refs.append(spec)
    for spec in (definition.get("calculated") or []):
        if not isinstance(spec, dict):
            continue
        for side in ("left", "right"):
            operand = spec.get(side)
            if (isinstance(operand, dict) and isinstance(operand.get("table"), str)
                    and isinstance(operand.get("field"), str)):
                refs.append(operand)

    def _walk_filters(node):
        if not isinstance(node, dict):
            return
        for cond in (node.get("conditions") or []):
            if not isinstance(cond, dict):
                continue
            if "conditions" in cond:
                _walk_filters(cond)
            elif isinstance(cond.get("table"), str) and isinstance(cond.get("field"), str):
                refs.append(cond)

    _walk_filters(definition.get("filters") or {})
    for spec in (definition.get("group_by") or []):
        if (isinstance(spec, dict) and isinstance(spec.get("table"), str)
                and isinstance(spec.get("field"), str)):
            refs.append(spec)
    for spec in (definition.get("sorts") or []):
        if (isinstance(spec, dict) and isinstance(spec.get("table"), str)
                and isinstance(spec.get("field"), str)):
            refs.append(spec)
    return refs


def validate_definition_against_catalog(db, definition: dict) -> dict:
    """Walk a query `definition`'s table/field references against the LIVE
    catalog and report any that no longer resolve (QB-07 / D-14).

    Resolves every table reference through the SAME allowlist the compiler
    itself uses -- `is_allowed_table`/`INTERNAL_TABLES` for internal tables,
    parameterized `CatalogTable`/`CatalogField` ORM lookups for synced
    external tables -- never a new SQL path (V5).

    Detection + a clear structured error only -- this never auto-migrates or
    rewrites the definition (D-14, Deferred Ideas).

    Returns {"missing_tables": [...], "missing_fields": ["<table>.<field>", ...]}
    -- both empty for a valid definition, so a genuinely valid query is
    unaffected (no false positives)."""
    from core.querybuilder_models import CatalogTable, CatalogField

    if not isinstance(definition, dict):
        return {"missing_tables": [], "missing_fields": []}

    missing_tables: List[str] = []
    # inst_id -> allowed column-name set, or None if the table itself didn't resolve.
    table_cols: Dict[str, Any] = {}
    # inst_id -> a human-readable table name for the error message.
    names: Dict[str, str] = {}

    for t in (definition.get("tables") or []):
        if not isinstance(t, dict):
            continue
        ref_name = t.get("table")
        inst_id = t.get("id")
        if not ref_name or not inst_id:
            continue
        if is_allowed_table(ref_name):
            table_cols[inst_id] = set(INTERNAL_TABLES[ref_name]["columns"])
            names[inst_id] = ref_name
            continue
        # IN-01: use the SAME predicate the compiler's resolver enforces
        # (src/query_external.py build_resolver) -- an external CatalogTable row
        # must be non-internal AND carry a physical_table_name. Otherwise an
        # internal-synced or physical-name-less row would pass this "known table"
        # gate but then fail in the resolver, routing through a different error
        # path than this validation intends (D-13: same allowlist the compiler uses).
        ct = (db.query(CatalogTable)
              .filter(CatalogTable.id == ref_name,
                      CatalogTable.is_internal == False)  # noqa: E712
              .first())
        if ct is None or not ct.physical_table_name:
            missing_tables.append(ref_name)
            table_cols[inst_id] = None
            names[inst_id] = ref_name
            continue
        table_cols[inst_id] = {f.name for f in
                               db.query(CatalogField).filter(CatalogField.table_id == ct.id)}
        names[inst_id] = ct.name or ref_name

    missing_fields: List[str] = []
    for ref in _collect_field_refs(definition):
        inst_id = ref.get("table")
        field = ref.get("field")
        if inst_id not in table_cols:
            continue  # unknown instance id -- the compiler's own validation catches this
        cols = table_cols[inst_id]
        if cols is None:
            continue  # the table itself is already reported missing; don't double-report its fields
        if field not in cols:
            missing_fields.append(f"{names.get(inst_id, inst_id)}.{field}")

    return {"missing_tables": sorted(set(missing_tables)),
            "missing_fields": sorted(set(missing_fields))}


def iter_catalog() -> List[dict]:
    out: List[dict] = []
    for name, meta in INTERNAL_TABLES.items():
        table = meta["model"].__table__
        cols = []
        for col in meta["columns"]:
            c = table.c[col]
            cols.append({"name": col, "type": str(c.type)})
        out.append({"table": name, "display": meta["display"],
                    "is_internal": True, "source_id": None, "columns": cols})
    return out
