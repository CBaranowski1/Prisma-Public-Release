# routes/query_routes.py
"""Visual query builder routes — /api/queries/*.

Builds, previews, runs, and saves queries over allowlisted internal tables.
Run/build/save are available to any authenticated user; results respect the
catalog allowlist and are always parameterized + SELECT-only.
"""
import json
import uuid
from typing import Any, Dict, Optional

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

from core.database import SessionLocal, get_db_session
from core.querybuilder_models import SavedQuery, QueryRelationship
from src.auth_helpers import require_user, require_privilege
from src.query_catalog import iter_catalog, QueryValidationError, validate_definition_against_catalog
from src.query_compiler import compile_query, execute_query
from src.query_external import build_resolver, build_owner_clause, query_touches_external


class RunRequest(BaseModel):
    definition: Dict[str, Any]


class SavedQueryCreate(BaseModel):
    name: str
    description: Optional[str] = None
    definition: Dict[str, Any]


class SavedQueryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[Dict[str, Any]] = None


# A saved definition is the user's own JSON, but cap it so a single row can't be
# inflated to megabytes (storage abuse / slow reads).
_MAX_DEFINITION_BYTES = 256 * 1024


def _check_definition_size(definition: Any) -> None:
    try:
        size = len(json.dumps(definition))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid query definition")
    if size > _MAX_DEFINITION_BYTES:
        raise HTTPException(400, "Query definition is too large")


def _reject_if_unknown_table(stale: Dict[str, Any]) -> None:
    """QB-16/D-15: raise a clean 400 naming any table reference that resolves
    to NEITHER an internal-allowlisted table NOR a known external
    `CatalogTable` id. Must run BEFORE the privilege gate -- an unprivileged
    caller must not be able to distinguish "this name maps to no real table"
    from any other malformed-query 400, so this never reveals whether a
    genuine external table exists (no existence oracle). A table that
    resolves (internal or a real external id) is a no-op here; whether it
    ALSO needs `can_manage_data_sources` is decided separately, after this
    check, by the unchanged `query_touches_external`/`require_privilege`
    gate (D-17 -- the privilege POLICY is unchanged, only the check order)."""
    if stale["missing_tables"]:
        raise HTTPException(400, "Unknown or forbidden table: " + ", ".join(stale["missing_tables"]))


def _reject_if_stale_fields(stale: Dict[str, Any]) -> None:
    """QB-07/D-14: raise a structured 400 naming the missing field(s) when
    `definition` references a field the live catalog no longer has --
    distinct from the generic could-not-execute/could-not-compile message a
    stale reference would otherwise fall through to. A table-level miss is
    handled earlier by `_reject_if_unknown_table`, before the privilege gate;
    this only covers the field-level half, run AFTER the gate. A valid
    definition (every field ref resolves) is a no-op."""
    if not stale["missing_fields"]:
        return
    raise HTTPException(400, "Query references a stale schema: missing field(s): "
                              + ", ".join(stale["missing_fields"]) + ".")


def _enforce_owner_for(request: Request, user: str) -> Optional[str]:
    """The owner value to scope query results to, or None for no scoping.

    Non-admin users only see their own rows (cross-user data isolation).
    None is returned for admins (they see everything) and for the
    single-user / auth-disabled mode where `user` is empty (no other users
    to isolate from)."""
    if not user:
        return None
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    try:
        if auth_mgr is not None and auth_mgr.is_admin(user):
            return None
    except (AttributeError, TypeError) as e:
        # Falls through to owner-scoping (non-admin) below — visibility
        # only, no control-flow change (D-10).
        logger.warning("is_admin check failed for %s: %s", user, e)
    return user


def setup_query_routes() -> APIRouter:
    router = APIRouter(prefix="/api/queries", tags=["queries"])

    @router.get("/catalog")
    def get_catalog(request: Request):
        user = require_user(request)
        tables = iter_catalog()
        with get_db_session() as db:
            q = db.query(QueryRelationship)
            if user:
                q = q.filter((QueryRelationship.owner == user) | (QueryRelationship.owner == None))  # noqa: E711
            rels = [r.to_dict() for r in q.all()]
            # External tables are admin/coder-only.
            privs = {}
            auth_mgr = getattr(request.app.state, "auth_manager", None)
            if user and auth_mgr is not None:
                try:
                    privs = auth_mgr.get_privileges(user) or {}
                except (AttributeError, TypeError, KeyError) as e:
                    # Read-only catalog display path, not a gate — preserve
                    # the {} fallback, just add visibility (D-10).
                    logger.warning("privilege lookup failed for %s: %s", user, e)
                    privs = {}
            can_ext = (not user) or privs.get("can_manage_data_sources", False) or (
                auth_mgr.is_admin(user) if (user and auth_mgr) else False)
            if can_ext:
                from core.querybuilder_models import CatalogTable, CatalogField
                for ct in db.query(CatalogTable).filter(CatalogTable.is_internal == False).all():  # noqa: E712
                    cols = [{"name": f.name, "type": f.data_type}
                            for f in db.query(CatalogField).filter(CatalogField.table_id == ct.id).all()]
                    tables.append({"table": ct.id, "display": ct.display_name or ct.name,
                                   "is_internal": False, "source_id": ct.source_id, "columns": cols})
        return {"tables": tables, "relationships": rels}

    @router.post("/run")
    def run_query(request: Request, body: RunRequest):
        user = require_user(request)
        db = SessionLocal()
        try:
            # QB-16/D-15: classify table existence ONCE (single catalog walk,
            # split across the two checks below) BEFORE the privilege gate.
            stale = validate_definition_against_catalog(db, body.definition)
            _reject_if_unknown_table(stale)
            if query_touches_external(db, body.definition):
                require_privilege(request, "can_manage_data_sources", fail_closed=True)
            _reject_if_stale_fields(stale)
            enforce_owner = _enforce_owner_for(request, user)
            return execute_query(db, body.definition, build_resolver(db),
                                 enforce_owner=enforce_owner,
                                 owner_clause_fn=build_owner_clause(db))
        except QueryValidationError as e:
            raise HTTPException(400, str(e))
        except SQLAlchemyError as e:
            # A bad-but-allowlisted query (or an external source error) — a user
            # error, not a server fault. Don't leak raw DB internals.
            logger.warning("query run failed: %s", e)
            raise HTTPException(400, "Query could not be executed (check fields, joins, and the data source).")
        finally:
            db.close()

    @router.post("/preview-sql")
    def preview_sql(request: Request, body: RunRequest):
        user = require_user(request)
        db = SessionLocal()
        try:
            # QB-16/D-15: classify table existence ONCE (single catalog walk,
            # split across the two checks below) BEFORE the privilege gate.
            stale = validate_definition_against_catalog(db, body.definition)
            _reject_if_unknown_table(stale)
            if query_touches_external(db, body.definition):
                require_privilege(request, "can_manage_data_sources", fail_closed=True)
            _reject_if_stale_fields(stale)
            enforce_owner = _enforce_owner_for(request, user)
            try:
                stmt = compile_query(body.definition, build_resolver(db),
                                     enforce_owner=enforce_owner,
                                     owner_clause_fn=build_owner_clause(db))
            except QueryValidationError as e:
                raise HTTPException(400, str(e))
            except SQLAlchemyError as e:
                logger.warning("query preview failed: %s", e)
                raise HTTPException(400, "Query could not be compiled (check fields, joins, and the data source).")
            return {"sql": str(stmt.compile(compile_kwargs={"literal_binds": False}))}
        finally:
            db.close()

    @router.get("")
    def list_saved(request: Request):
        user = require_user(request)
        with get_db_session() as db:
            q = db.query(SavedQuery)
            if user:
                q = q.filter(SavedQuery.owner == user)
            return [s.to_dict() for s in q.order_by(SavedQuery.updated_at.desc()).all()]

    @router.post("")
    def create_saved(request: Request, body: SavedQueryCreate):
        user = require_user(request)
        if not body.name.strip():
            raise HTTPException(400, "Query name is required")
        _check_definition_size(body.definition)
        with get_db_session() as db:
            sq = SavedQuery(id=str(uuid.uuid4()), owner=user or None,
                            name=body.name.strip(), description=body.description)
            sq.definition = body.definition
            db.add(sq)
            db.flush()
            return sq.to_dict()

    @router.get("/{qid}")
    def get_saved(request: Request, qid: str):
        user = require_user(request)
        with get_db_session() as db:
            sq = db.query(SavedQuery).filter(SavedQuery.id == qid).first()
            if not sq or (user and sq.owner != user):
                raise HTTPException(404, "Query not found")
            return sq.to_dict()

    @router.put("/{qid}")
    def update_saved(request: Request, qid: str, body: SavedQueryUpdate):
        user = require_user(request)
        with get_db_session() as db:
            sq = db.query(SavedQuery).filter(SavedQuery.id == qid).first()
            if not sq or (user and sq.owner != user):
                raise HTTPException(404, "Query not found")
            if body.name is not None:
                if not body.name.strip():
                    raise HTTPException(400, "Query name is required")
                sq.name = body.name.strip()
            if body.description is not None:
                sq.description = body.description
            if body.definition is not None:
                _check_definition_size(body.definition)
                sq.definition = body.definition
            db.flush()
            return sq.to_dict()

    @router.delete("/{qid}")
    def delete_saved(request: Request, qid: str):
        user = require_user(request)
        with get_db_session() as db:
            sq = db.query(SavedQuery).filter(SavedQuery.id == qid).first()
            if not sq or (user and sq.owner != user):
                raise HTTPException(404, "Query not found")
            db.delete(sq)
        return {"status": "deleted"}

    return router
