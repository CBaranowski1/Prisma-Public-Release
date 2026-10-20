"""Query-builder ORM models + internal-catalog seeding (real in-memory SQLite)."""
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
import core.querybuilder_models as qm


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_saved_query_roundtrips_definition_json():
    db = _session()
    sq = qm.SavedQuery(id=str(uuid.uuid4()), owner="alice", name="My query")
    sq.definition = {"tables": [{"id": "t1", "table": "sessions"}], "fields": []}
    db.add(sq)
    db.commit()
    loaded = db.query(qm.SavedQuery).first()
    assert loaded.definition["tables"][0]["table"] == "sessions"
    assert loaded.to_dict()["name"] == "My query"


def test_seed_internal_catalog_is_idempotent():
    db = _session()
    first = qm.seed_internal_catalog(db)
    assert first > 0
    again = qm.seed_internal_catalog(db)
    assert again == 0
    tables = db.query(qm.CatalogTable).filter(qm.CatalogTable.is_internal == True).all()  # noqa: E712
    names = {t.name for t in tables}
    assert "sessions" in names
    fields = db.query(qm.CatalogField).all()
    assert any(f.name == "name" for f in fields)


def test_relationship_to_dict():
    db = _session()
    rel = qm.QueryRelationship(
        id=str(uuid.uuid4()), owner="alice", name="session→messages",
        kind="one_to_many", left_table_id="ct1", left_field="id",
        right_table_id="ct2", right_field="session_id",
    )
    db.add(rel)
    db.commit()
    d = rel.to_dict()
    assert d["kind"] == "one_to_many"
    assert d["left_field"] == "id"
