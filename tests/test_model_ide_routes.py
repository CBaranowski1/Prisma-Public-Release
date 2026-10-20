"""Security and shape tests for the integrated Model IDE database surface."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.model_ide_routes as mod


def _request(user="alice", *, allowed=True, registry=None):
    auth_manager = SimpleNamespace(
        get_privileges=lambda _user: {"can_manage_data_sources": allowed},
        is_configured=True,
    )
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user, api_token=False),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=auth_manager,
                memory_provider_registry=registry,
            )
        ),
        client=SimpleNamespace(host="10.0.0.5"),
    )


def _handler(method, path):
    for route in mod.setup_model_ide_routes().routes:
        if route.path == path and method in (route.methods or set()):
            return route.endpoint
    raise KeyError(path)


@contextlib.contextmanager
def _db_context(session):
    yield session


def test_database_list_requires_data_source_privilege():
    handler = _handler("GET", "/api/model-ide/databases")
    with pytest.raises(HTTPException) as exc:
        handler(request=_request(allowed=False))
    assert exc.value.status_code == 403


def test_completion_keeps_endpoint_credentials_server_side(monkeypatch):
    import routes.openai_compat_routes as openai_compat

    captured = {}
    monkeypatch.setattr(
        openai_compat,
        "_resolve_model",
        lambda model, owner: (
            "http://vllm:8000/v1",
            model,
            {"Authorization": "Bearer upstream-secret"},
        ),
    )
    monkeypatch.setattr(openai_compat, "_detect_provider", lambda _base: "openai")

    async def fake_proxy(url, headers, body, **options):
        captured.update(url=url, headers=headers, body=body, options=options)
        return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}

    monkeypatch.setattr(openai_compat, "_proxy_json", fake_proxy)
    payload = mod.ModelIdeCompletionRequest(
        model="qwen",
        messages=[{"role": "user", "content": "Review this file"}],
    )
    result = asyncio.run(
        _handler("POST", "/api/model-ide/complete")(
            request=_request(),
            payload=payload,
        )
    )

    proposal = json.loads(result["choices"][0]["message"]["content"])
    assert proposal == {
        "summary": "ok",
        "replacement": None,
        "migration_sql": None,
        "migration_name": None,
    }
    assert result["prisma_gate"]["passed"] is True
    assert "publication contract" in captured["body"]["messages"][0]["content"]
    assert captured["url"] == "http://vllm:8000/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer upstream-secret"
    assert captured["options"]["timeout"] == 120
    assert "chat_template_kwargs" not in captured["body"]
    assert "upstream-secret" not in str(result)


def test_completion_resolves_virtual_dgx_spark_model(monkeypatch):
    import routes.openai_compat_routes as openai_compat

    monkeypatch.setattr(
        openai_compat,
        "_resolve_model",
        lambda *_args: (_ for _ in ()).throw(ValueError("No enabled endpoints found")),
    )
    monkeypatch.setattr(openai_compat, "_detect_provider", lambda _base: "openai")
    monkeypatch.setattr(
        mod,
        "load_dgx_spark_config",
        lambda **_kwargs: SimpleNamespace(
            base_url="http://vllm:8000/v1",
            model="nvidia/Qwen",
            headers={"Authorization": "Bearer deployment-secret"},
        ),
    )
    captured = {}

    async def fake_proxy(url, headers, body, **options):
        captured.update(url=url, headers=headers, body=body, options=options)
        return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}

    monkeypatch.setattr(openai_compat, "_proxy_json", fake_proxy)
    payload = mod.ModelIdeCompletionRequest(
        model="nvidia/Qwen",
        messages=[{"role": "user", "content": "Review this file"}],
    )
    asyncio.run(
        _handler("POST", "/api/model-ide/complete")(
            request=_request(),
            payload=payload,
        )
    )

    assert captured["url"] == "http://vllm:8000/v1/chat/completions"
    assert captured["body"]["model"] == "nvidia/Qwen"
    assert captured["body"]["chat_template_kwargs"] == {
        "enable_thinking": False,
    }
    assert captured["headers"]["Authorization"] == "Bearer deployment-secret"


def test_completion_handles_reasoning_only_truncation(monkeypatch):
    import routes.openai_compat_routes as openai_compat

    monkeypatch.setattr(
        openai_compat,
        "_resolve_model",
        lambda model, owner: ("http://vllm:8000/v1", model, {}),
    )
    monkeypatch.setattr(openai_compat, "_detect_provider", lambda _base: "openai")
    responses = []

    async def fake_proxy(*_args, **_kwargs):
        response = {
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "content": None,
                    "reasoning": "private reasoning that must not be returned",
                    "role": "assistant",
                },
            }],
        }
        responses.append(response)
        return response

    monkeypatch.setattr(openai_compat, "_proxy_json", fake_proxy)
    payload = mod.ModelIdeCompletionRequest(
        model="qwen",
        messages=[{"role": "user", "content": "Review this file"}],
        max_tokens=32,
    )

    result = asyncio.run(
        _handler("POST", "/api/model-ide/complete")(
            request=_request(),
            payload=payload,
        )
    )

    proposal = json.loads(result["choices"][0]["message"]["content"])
    assert proposal["replacement"] is None
    assert result["prisma_gate"]["passed"] is False
    assert result["prisma_gate"]["repair_attempts"] == 1
    assert "private reasoning" not in str(result)
    assert len(responses) == 2


def test_completion_repairs_invalid_python_once(monkeypatch):
    import routes.openai_compat_routes as openai_compat

    monkeypatch.setattr(
        openai_compat,
        "_resolve_model",
        lambda model, owner: ("http://vllm:8000/v1", model, {}),
    )
    monkeypatch.setattr(openai_compat, "_detect_provider", lambda _base: "openai")
    responses = [
        {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "summary": "first",
                        "replacement": "def broken(:\n",
                        "migration_sql": None,
                        "migration_name": None,
                    })
                }
            }]
        },
        {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "summary": "fixed",
                        "replacement": "def fixed():\n    return True\n",
                        "migration_sql": None,
                        "migration_name": None,
                    })
                }
            }]
        },
    ]

    async def fake_proxy(*_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(openai_compat, "_proxy_json", fake_proxy)
    payload = mod.ModelIdeCompletionRequest(
        model="qwen",
        workspace_path="src/example.py",
        language="python",
        messages=[{
            "role": "user",
            "content": (
                "Fix the function.\n<untrusted_code>\n"
                "def old():\n    return False\n"
                "</untrusted_code>"
            ),
        }],
    )
    result = asyncio.run(
        _handler("POST", "/api/model-ide/complete")(
            request=_request(),
            payload=payload,
        )
    )

    proposal = json.loads(result["choices"][0]["message"]["content"])
    assert proposal["replacement"] == "def fixed():\n    return True\n"
    assert result["prisma_gate"]["verified"] is True
    assert result["prisma_gate"]["repair_attempts"] == 1


def test_completion_injects_brain_critique_and_scores_stick_rate(monkeypatch):
    import routes.openai_compat_routes as openai_compat
    import src.conversation_index as conversation_module
    from src.memory_provider import MemoryRecord, MemorySearchHit

    class Provider:
        provider_id = "brain:conversation-evals"
        display_name = "Conversation evaluations"
        enabled = True

        async def recall(self, *_args, **_kwargs):
            return [
                MemorySearchHit(
                    memory=MemoryRecord(
                        id="rule-1",
                        text="Database queries must use parameterized inputs.",
                    ),
                    provider_id=self.provider_id,
                    score=0.9,
                )
            ]

    class Registry:
        def active(self):
            return [Provider()]

    async def no_index(*_args, **_kwargs):
        return {"indexed": True}

    monkeypatch.setattr(conversation_module, "index_exchange_async", no_index)
    monkeypatch.setattr(
        openai_compat,
        "_resolve_model",
        lambda model, owner: ("http://vllm:8000/v1", model, {}),
    )
    monkeypatch.setattr(openai_compat, "_detect_provider", lambda _base: "openai")
    captured = {}

    async def fake_proxy(_url, _headers, body, **_kwargs):
        captured["messages"] = body["messages"]
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "summary": "parameterized",
                        "replacement": (
                            'cursor.execute("SELECT * FROM users WHERE id = ?", '
                            "(user_id,))\n"
                        ),
                        "migration_sql": None,
                        "migration_name": None,
                    })
                }
            }]
        }

    monkeypatch.setattr(openai_compat, "_proxy_json", fake_proxy)
    payload = mod.ModelIdeCompletionRequest(
        model="qwen",
        workspace_path="src/query.py",
        language="python",
        messages=[{
            "role": "user",
            "content": (
                "Fix the query.\n<untrusted_code>\n"
                'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")\n'
                "</untrusted_code>"
            ),
        }],
    )
    result = asyncio.run(
        _handler("POST", "/api/model-ide/complete")(
            request=_request(registry=Registry()),
            payload=payload,
        )
    )

    assert any(
        "parameterized inputs" in message["content"]
        for message in captured["messages"]
    )
    assert result["brain_used"][0]["id"] == "rule-1"
    assert result["prisma_gate"]["critique_stick_rate"] == 1.0


def test_completion_rejects_aggregate_prompt_over_limit():
    payload = mod.ModelIdeCompletionRequest(
        model="qwen",
        messages=[
            {"role": "system", "content": "x" * 50_000},
            {"role": "user", "content": "y" * 31_000},
        ],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _handler("POST", "/api/model-ide/complete")(
                request=_request(),
                payload=payload,
            )
        )
    assert exc.value.status_code == 413


def test_database_list_is_metadata_only(monkeypatch):
    source = SimpleNamespace(
        id="source-1",
        name="Production",
        source_type="postgres",
        host="db.internal",
        port=5432,
        database="app",
        enabled=True,
        username="developer",
        password="must-not-leak",
        last_sync_status="ok",
    )
    query = MagicMock()
    query.all.return_value = [source]
    session = MagicMock()
    session.query.return_value = query
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_context(session))
    monkeypatch.setattr(mod, "owner_filter", lambda value, *_a, **_k: value)

    result = _handler("GET", "/api/model-ide/databases")(
        request=_request(allowed=True)
    )

    assert result["mode"] == "read_only"
    assert result["items"][0]["id"] == mod.INTERNAL_DATABASE_ID
    external = result["items"][1]
    assert external["id"] == "source-1"
    assert external["has_credentials"] is True
    assert "password" not in external
    assert "username" not in external


def test_cross_owner_database_is_hidden_as_not_found(monkeypatch):
    query = MagicMock()
    query.first.return_value = None
    session = MagicMock()
    session.query.return_value.filter.return_value = query
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_context(session))
    monkeypatch.setattr(mod, "owner_filter", lambda value, *_a, **_k: value)

    handler = _handler(
        "GET",
        "/api/model-ide/databases/{source_id}/tables",
    )
    with pytest.raises(HTTPException) as exc:
        handler(request=_request(), source_id="not-owned")
    assert exc.value.status_code == 404


def test_routes_do_not_offer_sql_execution():
    paths = {route.path for route in mod.setup_model_ide_routes().routes}
    assert not any("execute" in path or "run" in path for path in paths)
