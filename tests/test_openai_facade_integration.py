"""Live integration smoke test for the OpenAI-compatible facade.

Unlike test_openai_facade_models / _chat (which stub `_visible_model_ids` and
`_resolve_model`), this exercises the REAL collection + resolution machinery:

  * real `routes.openai_compat_routes._visible_model_ids`
  * real `src.ai_interaction._resolve_model`
  * real `routes.model_routes` helpers (`_cached_model_ids`, `_visible_models`,
    `_is_chat_model`)

Only the DB session layer is faked (real-shaped `ModelEndpoint` rows — the same
`cached_models` JSON-string shape the real ORM stores) and the two outbound HTTP
calls are mocked (the resolver's model-list probe and the upstream chat stream).
This is what catches wrong field assumptions in `_visible_model_ids` end-to-end.
"""
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AUTH = {"Authorization": "Bearer test-tok"}


# --- A minimal fake DB shaped like the real ModelEndpoint query chain --------

class _FakeColumn:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return ("eq", self.name, value)


class _FakeModelEndpoint:
    is_enabled = _FakeColumn("is_enabled")
    name = _FakeColumn("name")


class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *conditions):
        for cond in conditions:
            if isinstance(cond, tuple) and cond[0] == "eq":
                _, field, value = cond
                self.rows = [r for r in self.rows if getattr(r, field) == value]
        return self

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def query(self, _model):
        return _FakeQuery(self.rows)

    def close(self):
        pass


def _endpoint(name, base, models):
    """A ModelEndpoint row with the real field shape (cached_models = JSON str)."""
    return SimpleNamespace(
        name=name,
        base_url=base,
        api_key="sk-test",
        cached_models=json.dumps(models),
        hidden_models=None,
        pinned_models=None,
        model_type="llm",
        is_enabled=True,
    )


@pytest.fixture
def integration_client(monkeypatch):
    # One enabled OpenAI-compatible endpoint exposing a chat model + an
    # embedding model (which must be filtered out of /v1/models).
    rows = [_endpoint("local", "http://localhost:11434",
                      ["llama-3.1-8b", "text-embedding-3-small"])]
    fake_db = lambda: _FakeDb(rows)

    # Patch the DB session layer in BOTH modules the real code imports from:
    #   _visible_model_ids -> `from core.database import SessionLocal, ModelEndpoint`
    #   _resolve_model     -> `from src.database import SessionLocal, ModelEndpoint`
    import core.database as core_db
    import src.database as src_db
    for mod in (core_db, src_db):
        monkeypatch.setattr(mod, "SessionLocal", fake_db, raising=False)
        monkeypatch.setattr(mod, "ModelEndpoint", _FakeModelEndpoint, raising=False)

    # Mock the resolver's outbound model-list probe (httpx.get).
    import httpx

    def _fake_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"data": [{"id": "llama-3.1-8b"}]}
        return resp

    monkeypatch.setattr(httpx, "get", _fake_get)

    # Fresh facade module with a known token; keep the REAL helpers.
    monkeypatch.delitem(sys.modules, "routes.openai_compat_routes", raising=False)
    import routes.openai_compat_routes as facade
    monkeypatch.setattr(facade, "_PRISM_TOKEN", "test-tok")

    app = FastAPI()
    app.include_router(facade.setup_openai_compat_routes())
    return TestClient(app), facade


def test_models_lists_real_chat_ids_only(integration_client):
    """Real _visible_model_ids flattens cached_models and drops the embedding id."""
    client, _ = integration_client
    r = client.get("/v1/models", headers=_AUTH)
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["llama-3.1-8b"]


def test_chat_resolves_real_upstream_and_passes_through(integration_client, monkeypatch):
    """Real _resolve_model picks the upstream + canonical model id; only the
    upstream stream is mocked, and we assert what it was handed."""
    client, facade = integration_client

    captured = {}

    async def _fake_stream(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(facade, "_proxy_stream", _fake_stream)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "llama-3.1-8b",
              "messages": [{"role": "user", "content": "hi"}],
              "stream": True},
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert "[DONE]" in r.text
    # Real resolver built the upstream chat URL and canonicalized the model id.
    assert captured["url"] == "http://localhost:11434/chat/completions"
    assert captured["body"]["model"] == "llama-3.1-8b"


def test_chat_unknown_model_404(integration_client):
    """A model id not present on any endpoint surfaces as a 404 (ValueError)."""
    client, _ = integration_client
    r = client.post(
        "/v1/chat/completions",
        json={"model": "no-such-model", "messages": []},
        headers=_AUTH,
    )
    assert r.status_code == 404
    assert "error" in r.json()
