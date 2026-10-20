"""Visual query builder models — catalog, relationships, saved queries.

Kept out of the ~1,950-line core/database.py to stay focused; models attach to
the shared Base, so init_db()'s create_all() picks them up once this module is
imported (done inside init_db before create_all to avoid an import cycle).
"""
import json
import uuid
from typing import Any, Dict
from urllib.parse import quote_plus

from sqlalchemy import Column, String, Text, Boolean, DateTime, ForeignKey, Integer

from core.database import Base, TimestampMixin, EncryptedText


def _uuid() -> str:
    return str(uuid.uuid4())


_DRIVER_URLS = {
    "postgres": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
}


class DataSource(TimestampMixin, Base):
    """A registered external database the query builder can sync from."""
    __tablename__ = "qb_data_sources"

    id = Column(String, primary_key=True, index=True, default=_uuid)
    name = Column(String, nullable=False)
    source_type = Column(String, nullable=False)          # postgres | mysql
    host = Column(String, nullable=True)
    port = Column(Integer, nullable=True)
    database = Column(String, nullable=True)
    username = Column(String, nullable=True)
    password = Column(EncryptedText, nullable=True)        # Fernet at rest
    options = Column(Text, nullable=True)                  # JSON: {"connect_args": {<vetted TLS keys>}}
    enabled = Column(Boolean, default=True, nullable=False)
    owner = Column(String, nullable=True, index=True)      # registering admin/coder
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String, nullable=True)       # ok | error | running
    last_sync_error = Column(Text, nullable=True)
    # QB-06: measures how long a "running" lock has been held, independent of
    # last_synced_at (which only updates once a sync finishes). Internal guard
    # field only -- not exposed via to_dict().
    sync_started_at = Column(DateTime, nullable=True)

    def sqlalchemy_url(self) -> str:
        driver = _DRIVER_URLS.get(self.source_type)
        if not driver:
            raise ValueError(f"Unsupported source_type: {self.source_type!r}")
        user = quote_plus(self.username or "")
        pw = quote_plus(self.password or "")
        auth = f"{user}:{pw}@" if user else ""
        # Omit the port when blank so the driver falls back to its default
        # (5432 / 3306); "host:None" makes SQLAlchemy raise on int("None").
        host = self.host or ""
        host_port = f"{host}:{self.port}" if self.port else host
        return f"{driver}://{auth}{host_port}/{self.database}"

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "source_type": self.source_type,
            "host": self.host, "port": self.port, "database": self.database,
            "username": self.username, "enabled": self.enabled, "owner": self.owner,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "last_sync_status": self.last_sync_status,
            "last_sync_error": self.last_sync_error,
            "has_password": bool(self.password),
        }


class CatalogTable(TimestampMixin, Base):
    __tablename__ = "qb_catalog_tables"

    id = Column(String, primary_key=True, index=True, default=_uuid)
    source_id = Column(String, nullable=True, index=True)   # null = internal
    name = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    is_internal = Column(Boolean, default=True, nullable=False)
    physical_table_name = Column(String, nullable=True)      # ext_* for synced data
    last_synced_at = Column(DateTime, nullable=True)
    owner = Column(String, nullable=True, index=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "source_id": self.source_id, "name": self.name,
            "display_name": self.display_name, "is_internal": self.is_internal,
            "physical_table_name": self.physical_table_name,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
        }


class CatalogField(Base):
    __tablename__ = "qb_catalog_fields"

    id = Column(String, primary_key=True, index=True, default=_uuid)
    table_id = Column(String, ForeignKey("qb_catalog_tables.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    name = Column(String, nullable=False)
    data_type = Column(String, nullable=True)
    nullable = Column(Boolean, default=True)
    is_pk = Column(Boolean, default=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "table_id": self.table_id, "name": self.name,
                "data_type": self.data_type, "nullable": self.nullable, "is_pk": self.is_pk}


class QueryRelationship(TimestampMixin, Base):
    __tablename__ = "qb_relationships"

    id = Column(String, primary_key=True, index=True, default=_uuid)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="one_to_many")  # one_to_one|one_to_many|many_to_many
    left_table_id = Column(String, nullable=False)
    left_field = Column(String, nullable=False)
    right_table_id = Column(String, nullable=False)
    right_field = Column(String, nullable=False)
    # many-to-many join table (optional)
    join_table_id = Column(String, nullable=True)
    join_left_field = Column(String, nullable=True)
    join_right_field = Column(String, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "name": self.name, "kind": self.kind,
            "left_table_id": self.left_table_id, "left_field": self.left_field,
            "right_table_id": self.right_table_id, "right_field": self.right_field,
            "join_table_id": self.join_table_id,
            "join_left_field": self.join_left_field, "join_right_field": self.join_right_field,
        }


class SavedQuery(TimestampMixin, Base):
    __tablename__ = "qb_saved_queries"

    id = Column(String, primary_key=True, index=True, default=_uuid)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    definition_json = Column("definition", Text, nullable=False, default="{}")

    @property
    def definition(self) -> Dict[str, Any]:
        try:
            return json.loads(self.definition_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    @definition.setter
    def definition(self, value: Dict[str, Any]):
        self.definition_json = json.dumps(value or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "name": self.name,
            "description": self.description, "definition": self.definition,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def seed_internal_catalog(db) -> int:
    """Idempotently create CatalogTable/CatalogField rows for allowlisted internal
    tables. Returns the number of CatalogTable rows created this call."""
    # Imported lazily (not at module top) to avoid a circular import: this
    # module is imported by core.database.init_db() during core.database's own
    # load, while src.query_catalog imports FROM core.database — importing
    # query_catalog at module top here deadlocks when query_catalog is the
    # entry point. By the time this function runs, query_catalog is importable.
    from src.query_catalog import iter_catalog
    created = 0
    for entry in iter_catalog():
        existing = (db.query(CatalogTable)
                    .filter(CatalogTable.is_internal == True,  # noqa: E712
                            CatalogTable.name == entry["table"])
                    .first())
        if existing:
            continue
        ct = CatalogTable(id=_uuid(), source_id=None, name=entry["table"],
                          display_name=entry["display"], is_internal=True)
        db.add(ct)
        db.flush()  # assign ct.id for the field FK
        for col in entry["columns"]:
            db.add(CatalogField(id=_uuid(), table_id=ct.id, name=col["name"],
                                data_type=col["type"]))
        created += 1
    db.commit()
    return created
