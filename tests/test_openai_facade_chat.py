"""Tests for /v1/chat/completions passthrough proxy."""
import sys
from unittest.mock import MagicMock
import pytest
import httpx
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
    app = FastAPI()
    app.include_router(facade.setup_openai_compat_routes())
    return TestClient(app), facade


_AUTH = {"Authorization": "Bearer test-tok"}


def test_chat_requires_bearer(facade_client):
    client, _ = facade_client
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 401


def test_chat_unknown_model_404(facade_client, monkeypatch):
    client, facade = facade_client
    def _raise(spec, owner=None):
        raise ValueError("model not found")
    monkeypatch.setattr(facade, "_resolve_model", _raise)
    r = client.post("/v1/chat/completions", json={"model": "nope", "messages": []}, headers=_AUTH)
    assert r.status_code == 404


def test_chat_non_openai_upstream_400(facade_client, monkeypatch):
    client, facade = facade_client
    monkeypatch.setattr(facade, "_resolve_model",
                        lambda spec, owner=None: ("https://api.anthropic.com", "claude-3", {}))
    monkeypatch.setattr(facade, "_detect_provider", lambda base: "anthropic")
    r = client.post("/v1/chat/completions", json={"model": "claude-3", "messages": []}, headers=_AUTH)
    assert r.status_code == 400


def test_chat_passthrough_streams(facade_client, monkeypatch):
    client, facade = facade_client
    monkeypatch.setattr(facade, "_resolve_model",
                        lambda spec, owner=None: ("http://localhost:11434", "llama-3.1-8b", {"Authorization": "Bearer up"}))
    monkeypatch.setattr(facade, "_detect_provider", lambda base: "ollama")

    # Stub the upstream streamer to yield two SSE chunks.
    async def _fake_proxy(url, headers, body):
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b'data: [DONE]\n\n'
    monkeypatch.setattr(facade, "_proxy_stream", _fake_proxy)

    r = client.post("/v1/chat/completions",
                    json={"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                    headers=_AUTH)
    assert r.status_code == 200
    text = r.text
    assert "hi" in text and "[DONE]" in text


# ---------------------------------------------------------------------------
# Task 3 — new tests
# ---------------------------------------------------------------------------

def test_error_body_is_openai_shaped(facade_client):
    """All /v1/* errors must carry {"error": {"message": ..., "type": ...}}."""
    client, _ = facade_client
    # No auth → 401
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 401
    body = r.json()
    assert "error" in body
    assert "message" in body["error"]
    assert "type" in body["error"]
    assert body["error"]["type"] == "authentication_error"


def test_error_shape_404(facade_client, monkeypatch):
    """404 errors carry invalid_request_error type."""
    client, facade = facade_client
    def _raise(spec, owner=None):
        raise ValueError("not found")
    monkeypatch.setattr(facade, "_resolve_model", _raise)
    r = client.post("/v1/chat/completions", json={"model": "ghost", "messages": []}, headers=_AUTH)
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "not found" in body["error"]["message"]


def test_error_shape_400(facade_client, monkeypatch):
    """400 errors carry invalid_request_error type."""
    client, facade = facade_client
    monkeypatch.setattr(facade, "_resolve_model",
                        lambda spec, owner=None: ("https://api.anthropic.com", "claude-3", {}))
    monkeypatch.setattr(facade, "_detect_provider", lambda base: "anthropic")
    r = client.post("/v1/chat/completions", json={"model": "claude-3", "messages": []}, headers=_AUTH)
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"


def test_error_shape_503(monkeypatch):
    """503 (PRISM_TOKEN unset) returns server_error type."""
    monkeypatch.delitem(sys.modules, "routes.openai_compat_routes", raising=False)
    for mod in ("core.database", "core.models", "src.auth_helpers"):
        if mod not in sys.modules:
            monkeypatch.setitem(sys.modules, mod, MagicMock())
    import routes.openai_compat_routes as facade
    monkeypatch.setattr(facade, "_PRISM_TOKEN", None)
    app = FastAPI()
    app.include_router(facade.setup_openai_compat_routes())
    client = TestClient(app)
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": []},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["type"] == "server_error"


def test_non_stream_returns_json(facade_client, monkeypatch):
    """Non-streaming path relays the upstream JSON response verbatim."""
    client, facade = facade_client
    monkeypatch.setattr(facade, "_resolve_model",
                        lambda spec, owner=None: ("http://localhost:11434", "llama-3.1-8b", {}))
    monkeypatch.setattr(facade, "_detect_provider", lambda base: "ollama")

    _upstream_response = {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}],
    }

    async def _fake_proxy_json(url, headers, body):
        return _upstream_response

    monkeypatch.setattr(facade, "_proxy_json", _fake_proxy_json)

    r = client.post("/v1/chat/completions",
                    json={"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}]},
                    headers=_AUTH)
    assert r.status_code == 200
    assert r.json() == _upstream_response


def test_non_stream_upstream_error_is_oai_shaped(facade_client, monkeypatch):
    """Carry-forward #3: upstream non-2xx becomes a 502 OpenAI-shaped error, not a bare 500."""
    client, facade = facade_client
    monkeypatch.setattr(facade, "_resolve_model",
                        lambda spec, owner=None: ("http://localhost:11434", "llama-3.1-8b", {}))
    monkeypatch.setattr(facade, "_detect_provider", lambda base: "ollama")

    async def _bad_proxy_json(url, headers, body):
        # Simulate httpx.HTTPStatusError from a 500 upstream
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"error": {"message": "internal upstream failure"}}
        raise httpx.HTTPStatusError("upstream 500", request=MagicMock(), response=mock_response)

    monkeypatch.setattr(facade, "_proxy_json", _bad_proxy_json)

    r = client.post("/v1/chat/completions",
                    json={"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}]},
                    headers=_AUTH)
    assert r.status_code == 502
    body = r.json()
    assert "error" in body
    assert "message" in body["error"]
    assert body["error"]["type"] == "server_error"
    assert "internal upstream failure" in body["error"]["message"]


def test_missing_model_field_returns_400(facade_client):
    """Carry-forward #4: body without 'model' returns clean 400 OpenAI-shaped error."""
    client, _ = facade_client
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    headers=_AUTH)
    assert r.status_code == 400
    body = r.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"
    assert "model" in body["error"]["message"]
