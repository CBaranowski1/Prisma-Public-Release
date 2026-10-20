"""External data-source management — /api/datasources/*.

All endpoints require the can_manage_data_sources privilege. Credentials are
stored via the DataSource.password EncryptedText column (Fernet at rest).
"""
import json
import logging
import os
import uuid
from pathlib import PurePath
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from core.constants import DATA_DIR
from core.database import get_db_session, engine
from core.querybuilder_models import DataSource, CatalogTable, CatalogField
from src.auth_helpers import require_privilege
from src.datasource_connectors import get_connector, ConnectorError
from src.datasource_sync import sync_source, _SAFE_PHYSICAL_TABLE

logger = logging.getLogger(__name__)

VALID_TYPES = {"postgres", "mysql", "csv", "excel", "rest"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB cap on data-source file uploads
# _SAFE_PHYSICAL_TABLE is defined ONCE in src/datasource_sync.py (QB-18 / D-15,
# D-17, Pitfall 3) and imported here -- this route's delete_source DROP guard
# and the sweep's DROP guard now share one source of truth.


class DataSourceCreate(BaseModel):
    name: str
    source_type: str
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    options: Optional[dict] = None


class SyncRequest(BaseModel):
    tables: list[str]


def setup_datasource_routes() -> APIRouter:
    router = APIRouter(prefix="/api/datasources", tags=["data_sources"])

    @router.get("")
    def list_sources(request: Request):
        require_privilege(request, "can_manage_data_sources", fail_closed=True)
        with get_db_session() as db:
            return [d.to_dict() for d in db.query(DataSource).all()]

    @router.post("")
    def create_source(request: Request, body: DataSourceCreate):
        user = require_privilege(request, "can_manage_data_sources", fail_closed=True)
        if body.source_type not in VALID_TYPES:
            raise HTTPException(400, f"Unsupported source_type: {body.source_type}")
        with get_db_session() as db:
            ds = DataSource(
                id=str(uuid.uuid4()), name=body.name, source_type=body.source_type,
                host=body.host, port=body.port, database=body.database,
                username=body.username, password=body.password,
                options=json.dumps(body.options) if body.options else None,
                owner=user or None, enabled=True,
            )
            db.add(ds)
            db.flush()
            return ds.to_dict()

    @router.post("/{ds_id}/test")
    def test_source(request: Request, ds_id: str):
        require_privilege(request, "can_manage_data_sources", fail_closed=True)
        with get_db_session() as db:
            ds = db.query(DataSource).filter(DataSource.id == ds_id).first()
            if not ds:
                raise HTTPException(404, "Data source not found")
            try:
                get_connector(ds).test_connection()
            except ConnectorError as e:
                raise HTTPException(400, str(e))
        return {"ok": True}

    @router.get("/{ds_id}/tables")
    def list_source_tables(request: Request, ds_id: str):
        require_privilege(request, "can_manage_data_sources", fail_closed=True)
        with get_db_session() as db:
            ds = db.query(DataSource).filter(DataSource.id == ds_id).first()
            if not ds:
                raise HTTPException(404, "Data source not found")
            try:
                return {"tables": get_connector(ds).list_tables()}
            except ConnectorError as e:
                raise HTTPException(400, str(e))

    @router.post("/{ds_id}/sync")
    def sync_now(request: Request, ds_id: str, body: SyncRequest):
        require_privilege(request, "can_manage_data_sources", fail_closed=True)
        with get_db_session() as db:
            ds = db.query(DataSource).filter(DataSource.id == ds_id).first()
            if not ds:
                raise HTTPException(404, "Data source not found")
            try:
                return sync_source(db, ds, body.tables)
            except ConnectorError as e:
                raise HTTPException(400, str(e))

    @router.post("/{ds_id}/upload")
    async def upload_file(request: Request, ds_id: str, file: UploadFile = File(...)):
        require_privilege(request, "can_manage_data_sources", fail_closed=True)
        safe = PurePath(file.filename or "upload").name  # strip any path components
        if not safe:
            raise HTTPException(400, "Invalid filename")
        # Read at most the cap + 1 byte so an oversized upload can't exhaust memory.
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Uploaded file is too large (max 50 MB)")
        with get_db_session() as db:
            ds = db.query(DataSource).filter(DataSource.id == ds_id).first()
            if not ds:
                raise HTTPException(404, "Data source not found")
            # Build the destination from the VALIDATED stored id (a UUID), never
            # the raw `ds_id` path param — otherwise a crafted ds_id ("../..")
            # could traverse outside DATA_DIR/qb_sources before this check.
            dest_dir = os.path.join(DATA_DIR, "qb_sources", ds.id)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, safe)
            with open(dest, "wb") as f:
                f.write(data)
            kind = "excel" if safe.lower().endswith((".xlsx", ".xls")) else "csv"
            ds.options = json.dumps({"file_path": dest, "kind": kind})
            db.flush()
        try:
            with get_db_session() as db:
                ds = db.query(DataSource).filter(DataSource.id == ds_id).first()
                tables = get_connector(ds).list_tables()
        except ConnectorError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "tables": tables}

    @router.delete("/{ds_id}")
    def delete_source(request: Request, ds_id: str):
        require_privilege(request, "can_manage_data_sources", fail_closed=True)
        with get_db_session() as db:
            ds = db.query(DataSource).filter(DataSource.id == ds_id).first()
            if not ds:
                raise HTTPException(404, "Data source not found")
            # Drop materialized ext_* tables + their catalog rows.
            cats = db.query(CatalogTable).filter(CatalogTable.source_id == ds_id).all()
            for ct in cats:
                if ct.physical_table_name:
                    if not _SAFE_PHYSICAL_TABLE.match(ct.physical_table_name):
                        logger.warning(
                            "Refusing DROP for unexpected physical_table_name shape: %r",
                            ct.physical_table_name,
                        )
                    else:
                        try:
                            with engine.begin() as conn:
                                from sqlalchemy import text
                                conn.execute(text(f'DROP TABLE IF EXISTS "{ct.physical_table_name}"'))
                        except SQLAlchemyError as e:
                            logger.warning(
                                "Failed to drop orphaned table %r: %s", ct.physical_table_name, e
                            )
                db.query(CatalogField).filter(CatalogField.table_id == ct.id).delete()
                db.delete(ct)
            db.delete(ds)  # unconditional -- cleanup must not be blocked by a failed DROP (D-11)
        return {"status": "deleted"}

    return router
