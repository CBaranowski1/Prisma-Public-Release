"""External-table resolution, owner-exemption, and external-touch detection."""
import uuid
import pytest
from sqlalchemy import create_engine, MetaData, Table, Column, String, true
from sqlalchemy.orm import sessionmaker

from core.database import Base
import core.querybuilder_models as qm
from src.query_catalog import QueryValidationError
import src.query_external as qx


@pytest.fixture
def db_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'app.db'}")
    Base.metadata.create_all(bind=eng)
    # A materialized external table + its catalog row.
    md = MetaData()
    Table("ext_src1_customers", md, Column("id", String), Column("name", String)).create(eng)
    db = sessionmaker(bind=eng)()
    ct = qm.CatalogTable(id="ctid-1", source_id="src1", name="customers",
                         is_internal=False, physical_table_name="ext_src1_customers")
    db.add(ct); db.commit()
    return db, eng


def test_resolver_internal_and_external(db_engine):
    db, eng = db_engine
    resolve = qx.build_resolver(db, app_engine=eng)
    assert resolve("sessions").name == "sessions"            # internal allowlist
    assert resolve("ctid-1").name == "ext_src1_customers"    # external by catalog id


def test_resolver_rejects_unknown(db_engine):
    db, eng = db_engine
    resolve = qx.build_resolver(db, app_engine=eng)
    with pytest.raises(QueryValidationError):
        resolve("does-not-exist")


def test_owner_clause_exempts_external(db_engine):
    db, eng = db_engine
    oc = qx.build_owner_clause(db)
    ext_alias = qx.build_resolver(db, app_engine=eng)("ctid-1").alias("t1")
    clause = oc("ctid-1", ext_alias, "alice")
    assert clause.compare(true())                            # no-op exemption


def test_query_touches_external(db_engine):
    db, eng = db_engine
    assert qx.query_touches_external(db, {"tables": [{"id": "t1", "table": "ctid-1"}]}) is True
    assert qx.query_touches_external(db, {"tables": [{"id": "t1", "table": "sessions"}]}) is False
