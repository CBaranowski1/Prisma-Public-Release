"""Relationship metadata routes — /api/relationships/*.

Relationships drive default joins in the query builder. Defining them is gated
by the can_manage_data_sources privilege; everyone may read them.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import get_db_session
from core.querybuilder_models import QueryRelationship
from src.auth_helpers import require_user, require_privilege

VALID_KINDS = {"one_to_one", "one_to_many", "many_to_many"}


def _resolve_table_columns(db, table_id: str):
    """Return the allowed-column-name set for `table_id`, or None if it does
    not resolve to any known catalog table.

    Mirrors src/query_catalog.py's validate_definition_against_catalog
    resolution convention (internal allowlist first, then a CatalogTable id
    lookup for synced external tables) -- the SAME allowlist the compiler
    itself uses, never a new resolution path (D-13, Don't Hand-Roll)."""
    from src.query_catalog import is_allowed_table, INTERNAL_TABLES
    if is_allowed_table(table_id):
        return set(INTERNAL_TABLES[table_id]["columns"])
    from core.querybuilder_models import CatalogTable, CatalogField
    # IN-01: match the compiler resolver's predicate (non-internal + a real
    # physical_table_name) so this advisory validation agrees with the path the
    # compiler actually uses (D-13), rather than accepting a row the resolver
    # would later reject.
    ct = (db.query(CatalogTable)
          .filter(CatalogTable.id == table_id,
                  CatalogTable.is_internal == False)  # noqa: E712
          .first())
    if ct is None or not ct.physical_table_name:
        return None
    return {f.name for f in db.query(CatalogField).filter(CatalogField.table_id == ct.id)}


def _validate_ref(db, table_id: str, field: str) -> None:
    """Raise HTTPException(400) unless `table_id`/`field` resolve against the
    live catalog. Advisory-only validation at create time (D-13) -- never
    touches the compiler, which still reads joins solely from ast.joins (D-14)."""
    cols = _resolve_table_columns(db, table_id)
    if cols is None:
        raise HTTPException(400, f"Unknown or forbidden table: {table_id!r}")
    if field not in cols:
        raise HTTPException(400, f"Unknown or forbidden column: {table_id}.{field}")


class RelationshipCreate(BaseModel):
    name: str
    kind: str = "one_to_many"
    left_table_id: str
    left_field: str
    right_table_id: str
    right_field: str
    join_table_id: Optional[str] = None
    join_left_field: Optional[str] = None
    join_right_field: Optional[str] = None


def setup_relationship_routes() -> APIRouter:
    router = APIRouter(prefix="/api/relationships", tags=["query_relationships"])

    @router.get("")
    def list_relationships(request: Request):
        user = require_user(request)
        with get_db_session() as db:
            q = db.query(QueryRelationship)
            if user:
                q = q.filter((QueryRelationship.owner == user) | (QueryRelationship.owner == None))  # noqa: E711
            return [r.to_dict() for r in q.all()]

    @router.post("")
    def create_relationship(request: Request, body: RelationshipCreate):
        user = require_privilege(request, "can_manage_data_sources")
        if body.kind not in VALID_KINDS:
            raise HTTPException(400, f"Invalid relationship kind: {body.kind}")
        if body.kind == "many_to_many" and not (body.join_table_id and body.join_left_field and body.join_right_field):
            raise HTTPException(400, "many_to_many requires join_table_id, join_left_field, join_right_field")
        with get_db_session() as db:
            _validate_ref(db, body.left_table_id, body.left_field)
            _validate_ref(db, body.right_table_id, body.right_field)
            if body.kind == "many_to_many":
                _validate_ref(db, body.join_table_id, body.join_left_field)
                _validate_ref(db, body.join_table_id, body.join_right_field)
            rel = QueryRelationship(
                id=str(uuid.uuid4()), owner=user or None, name=body.name, kind=body.kind,
                left_table_id=body.left_table_id, left_field=body.left_field,
                right_table_id=body.right_table_id, right_field=body.right_field,
                join_table_id=body.join_table_id,
                join_left_field=body.join_left_field, join_right_field=body.join_right_field,
            )
            db.add(rel)
            db.flush()
            return rel.to_dict()

    @router.delete("/{rel_id}")
    def delete_relationship(request: Request, rel_id: str):
        user = require_privilege(request, "can_manage_data_sources")
        with get_db_session() as db:
            q = db.query(QueryRelationship).filter(QueryRelationship.id == rel_id)
            if user:
                q = q.filter((QueryRelationship.owner == user) | (QueryRelationship.owner == None))  # noqa: E711
            deleted = q.delete()
            if not deleted:
                raise HTTPException(404, "Relationship not found")
        return {"status": "deleted"}

    return router
