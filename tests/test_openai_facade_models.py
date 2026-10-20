# tests/test_openai_facade_models.py
"""Tests for the /v1/models OpenAI-compatible endpoint."""
import sys
from unittest.mock import MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def facade_client(monkeypatch):
    for mod in ("core.database", "core.models", "src.auth_helpers"):
        if mod not in sys.modules:
            monkeypatch.setitem(sys.modules, mod, MagicMock())
    monkeypatch.delitem(sys.modules, "routes.openai_compat_routes", raising=False)
    import routes.openai_compat_routes as facade
    monkeypatch.setattr(facade, "_PRISM_TOKEN", "test-tok")
    # Stub the model-id collector so the test doesn't need a real DB.
    monkeypatch.setattr(facade, "_visible_model_ids", lambda owner: ["llama-3.1-8b", "qwen2.5-coder"])
    app = FastAPI()
    app.include_router(facade.setup_openai_compat_routes())
    return TestClient(app), facade


def test_models_requires_bearer(facade_client):
    client, _ = facade_client
    assert client.get("/v1/models").status_code == 401


def test_models_wrong_token(facade_client):
    client, _ = facade_client
    r = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_models_openai_shape(facade_client):
    client, _ = facade_client
    r = client.get("/v1/models", headers={"Authorization": "Bearer test-tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == ["llama-3.1-8b", "qwen2.5-coder"]
    assert all(m["object"] == "model" for m in body["data"])


def test_models_503_when_token_unset(monkeypatch):
    monkeypatch.delitem(sys.modules, "routes.openai_compat_routes", raising=False)
    import routes.openai_compat_routes as facade
    monkeypatch.setattr(facade, "_PRISM_TOKEN", None)
    app = FastAPI()
    app.include_router(facade.setup_openai_compat_routes())
    client = TestClient(app)
    assert client.get("/v1/models", headers={"Authorization": "Bearer x"}).status_code == 503
