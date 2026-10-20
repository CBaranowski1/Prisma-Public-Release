"""DataSource model: encrypted creds, safe to_dict, URL building."""
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
import core.querybuilder_models as qm


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_to_dict_never_leaks_password():
    db = _session()
    ds = qm.DataSource(id=str(uuid.uuid4()), name="Prod", source_type="postgres",
                       host="db.internal", port=5432, database="app",
                       username="reader", password="s3cret", owner="alice")
    db.add(ds)
    db.commit()
    d = ds.to_dict()
    assert "password" not in d
    assert d["host"] == "db.internal"
    assert d["source_type"] == "postgres"


def test_sqlalchemy_url_postgres_and_mysql():
    pg = qm.DataSource(id="1", name="p", source_type="postgres", host="h",
                       port=5432, database="d", username="u", password="p@ss/word")
    assert pg.sqlalchemy_url().startswith("postgresql+psycopg://u:")
    assert "@h:5432/d" in pg.sqlalchemy_url()
    my = qm.DataSource(id="2", name="m", source_type="mysql", host="h2",
                       port=3306, database="d2", username="u2", password="pw")
    assert my.sqlalchemy_url().startswith("mysql+pymysql://u2:")
