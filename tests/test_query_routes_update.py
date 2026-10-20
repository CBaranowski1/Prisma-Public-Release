"""Update/rename endpoint for saved queries (PUT /api/queries/{qid})."""
import contextlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.query_routes as mod
from core.database import Base


@pytest.fixture
def client(monkeypatch):
    # Build an isolated in-memory SQLite DB for this test.
    # StaticPool is required: plain sqlite:///:memory: creates a NEW empty DB
    # per connection, so tables created by create_all() are invisible to the
    # session opened on the next connection. StaticPool reuses one connection.
    # Mirrors the pattern in test_brain_graph_route.py: minimal FastAPI app +
    # monkeypatched dependencies, no full-app import needed.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    @contextlib.contextmanager
    def _fake_db_session():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    # Single-user / auth-disabled mode: require_user returns "" so owner == None.
    monkeypatch.setattr(mod, "get_db_session", _fake_db_session)
    monkeypatch.setattr(mod, "require_user", lambda r: "")

    app = FastAPI()
    app.include_router(mod.setup_query_routes())
    with TestClient(app) as c:
        yield c


def _make(client, name="orig"):
    r = client.post("/api/queries", json={
        "name": name,
        "definition": {"tables": [{"id": "t1", "table": "sessions"}],
                       "fields": [{"table": "t1", "field": "id", "alias": "id"}]},
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_put_renames_saved_query(client):
    qid = _make(client)
    r = client.put(f"/api/queries/{qid}", json={"name": "renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "renamed"
    # Persisted.
    assert client.get(f"/api/queries/{qid}").json()["name"] == "renamed"


def test_put_updates_definition(client):
    qid = _make(client)
    new_def = {"tables": [{"id": "t1", "table": "memory_entries"}],
               "fields": [{"table": "t1", "field": "id", "alias": "id"}],
               "ui": {"positions": {"t1": {"x": 40, "y": 20}}}}
    r = client.put(f"/api/queries/{qid}", json={"definition": new_def})
    assert r.status_code == 200, r.text
    assert r.json()["definition"]["ui"]["positions"]["t1"]["x"] == 40


def test_put_rejects_blank_name(client):
    qid = _make(client)
    r = client.put(f"/api/queries/{qid}", json={"name": "   "})
    assert r.status_code == 400


def test_create_rejects_oversized_definition(client):
    # A5: a multi-megabyte definition must be refused, not stored.
    big = {"tables": [{"id": "t1", "table": "sessions"}], "blob": "x" * (300 * 1024)}
    r = client.post("/api/queries", json={"name": "huge", "definition": big})
    assert r.status_code == 400


def test_put_rejects_oversized_definition(client):
    qid = _make(client)
    big = {"tables": [{"id": "t1", "table": "sessions"}], "blob": "x" * (300 * 1024)}
    r = client.put(f"/api/queries/{qid}", json={"definition": big})
    assert r.status_code == 400


def test_put_missing_query_is_404(client):
    r = client.put(f"/api/queries/{uuid.uuid4()}", json={"name": "x"})
    assert r.status_code == 404
