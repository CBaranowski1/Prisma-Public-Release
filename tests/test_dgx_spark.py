import pytest
import asyncio
from types import SimpleNamespace

from src.dgx_spark import (
    DGX_SPARK_ENDPOINT_ID,
    is_dgx_spark_endpoint_id,
    is_dgx_spark_url,
    load_dgx_spark_config,
    normalize_openai_base,
)


def test_normalizes_openai_base_without_doubling_v1():
    assert (
        normalize_openai_base("https://spark.internal:8000/v1/models")
        == "https://spark.internal:8000/v1"
    )
    assert (
        normalize_openai_base("http://spark.internal:8000")
        == "http://spark.internal:8000/v1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://spark.internal/v1",
        "https://user:secret@spark.internal/v1",
        "https://spark.internal/v1?token=secret",
        "https://spark.internal/v1#fragment",
    ],
)
def test_rejects_unsafe_dgx_urls(url):
    with pytest.raises(ValueError):
        normalize_openai_base(url)


def test_authentication_is_required_by_default():
    with pytest.raises(ValueError, match="DGX_SPARK_API_KEY"):
        load_dgx_spark_config({"DGX_SPARK_BASE_URL": "https://spark/v1"})


def test_builds_authenticated_chat_discovery_and_embedding_targets():
    config = load_dgx_spark_config(
        {
            "DGX_SPARK_BASE_URL": "https://spark.internal:8000",
            "DGX_SPARK_API_KEY": "test-secret",
            "DGX_SPARK_MODEL": "open-model",
            "DGX_SPARK_MAX_MODEL_LEN": "32768",
            "DGX_SPARK_EMBEDDING_MODEL": "embed-model",
        },
        require_model=True,
    )

    assert config is not None
    assert config.chat_url == "https://spark.internal:8000/v1/chat/completions"
    assert config.models_url == "https://spark.internal:8000/v1/models"
    assert config.resolved_embedding_url == (
        "https://spark.internal:8000/v1/embeddings"
    )
    assert config.headers == {"Authorization": "Bearer test-secret"}
    assert config.max_model_len == 32768


def test_rejects_invalid_serving_context_length():
    with pytest.raises(ValueError, match="DGX_SPARK_MAX_MODEL_LEN"):
        load_dgx_spark_config(
            {
                "DGX_SPARK_BASE_URL": "https://spark.internal:8000",
                "DGX_SPARK_API_KEY": "test-secret",
                "DGX_SPARK_MAX_MODEL_LEN": "not-an-integer",
            }
        )


def test_no_auth_requires_explicit_operator_opt_out():
    config = load_dgx_spark_config(
        {
            "DGX_SPARK_BASE_URL": "http://127.0.0.1:8000/v1",
            "DGX_SPARK_ALLOW_INSECURE_NO_AUTH": "true",
        }
    )

    assert config is not None
    assert config.headers == {}


def test_virtual_endpoint_matches_only_the_configured_spark_service():
    config = load_dgx_spark_config(
        {
            "DGX_SPARK_BASE_URL": "http://vllm:8000/v1",
            "DGX_SPARK_API_KEY": "spark-secret",
            "DGX_SPARK_MODEL": "Qwen/current-model",
        },
        require_model=True,
    )

    assert is_dgx_spark_endpoint_id(DGX_SPARK_ENDPOINT_ID)
    assert not is_dgx_spark_endpoint_id("database-endpoint")
    assert is_dgx_spark_url(
        "http://vllm:8000/v1/chat/completions",
        config,
    )
    assert not is_dgx_spark_url(
        "http://other-host:8000/v1/chat/completions",
        config,
    )


def test_picker_item_marks_current_spark_model_without_exposing_key(monkeypatch):
    from routes.model_routes import _dgx_spark_model_item

    monkeypatch.setenv("DGX_SPARK_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    monkeypatch.setenv("DGX_SPARK_MODEL", "Qwen/current-model")

    item = _dgx_spark_model_item()

    assert item["endpoint_id"] == DGX_SPARK_ENDPOINT_ID
    assert item["deployment_default"] is True
    assert item["current_model"] == "Qwen/current-model"
    assert item["models"] == ["Qwen/current-model"]
    assert item["url"] == "http://vllm:8000/v1/chat/completions"
    assert "spark-secret" not in repr(item)


def test_virtual_selection_rejects_any_model_except_active_spark_model(
    monkeypatch,
):
    from fastapi import HTTPException
    from routes.session_routes import _resolve_dgx_spark_selection

    monkeypatch.setenv("DGX_SPARK_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    monkeypatch.setenv("DGX_SPARK_MODEL", "Qwen/current-model")

    config = _resolve_dgx_spark_selection(
        DGX_SPARK_ENDPOINT_ID,
        "Qwen/current-model",
    )
    assert config.model == "Qwen/current-model"
    assert config.headers == {"Authorization": "Bearer spark-secret"}

    with pytest.raises(HTTPException, match="not the active DGX Spark model"):
        _resolve_dgx_spark_selection(
            DGX_SPARK_ENDPOINT_ID,
            "Qwen/not-loaded",
        )


def test_spark_session_is_not_treated_as_deleted_database_endpoint(monkeypatch):
    from types import SimpleNamespace

    from routes.chat_routes import _clear_orphaned_session_endpoint

    monkeypatch.setenv("DGX_SPARK_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    monkeypatch.setenv("DGX_SPARK_MODEL", "Qwen/current-model")
    session = SimpleNamespace(
        endpoint_url="http://vllm:8000/v1/chat/completions",
    )

    assert _clear_orphaned_session_endpoint(session) is False


def test_embedding_client_uses_dgx_target_and_bearer(monkeypatch):
    from src.embeddings import EmbeddingClient

    monkeypatch.delenv("EMBEDDING_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("DGX_SPARK_BASE_URL", "https://spark.internal:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    monkeypatch.setenv("DGX_SPARK_EMBEDDING_MODEL", "spark-embed")

    client = EmbeddingClient()

    assert client.url == "https://spark.internal:8000/v1/embeddings"
    assert client.model == "spark-embed"
    assert client._headers == {"Authorization": "Bearer spark-secret"}
    client._client.close()


def test_model_discovery_probes_exact_dgx_url_with_auth(monkeypatch):
    import src.model_discovery as model_discovery

    calls = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "spark-chat"}]}

    def _get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setenv("DGX_SPARK_BASE_URL", "https://spark.internal:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    monkeypatch.setattr(model_discovery.httpx, "get", _get)

    result = model_discovery.ModelDiscovery("localhost")._discover_dgx_spark()

    assert result["provider"] == "dgx-spark"
    assert result["url"] == "https://spark.internal:8000/v1/chat/completions"
    assert calls[0][0] == "https://spark.internal:8000/v1/models"
    assert calls[0][1]["headers"] == {
        "Authorization": "Bearer spark-secret"
    }
    assert "verify" in calls[0][1]


def test_strict_answer_defaults_to_active_dgx_model(monkeypatch):
    import src.answer_pipeline as pipeline
    from routes.answer_routes import AnswerRequest, router

    monkeypatch.setenv("DGX_SPARK_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    monkeypatch.setenv("DGX_SPARK_MODEL", "nvidia/Qwen")
    captured = {}

    def fake_wire(model, registry, **kwargs):
        captured.update(model=model, registry=registry, **kwargs)
        return {"model_fn": object(), "retrieve_fn": object(), "review_fn": object()}

    monkeypatch.setattr(pipeline, "wire_defaults", fake_wire)
    monkeypatch.setattr(
        pipeline,
        "run_pipeline",
        lambda *_args, **_kwargs: SimpleNamespace(
            answer="ok",
            approved=True,
            rounds=1,
            grounded=True,
            sources=["brain"],
            transcript=[],
            orchestration={},
            provenance={},
        ),
    )
    handler = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/answer"
    )
    registry = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(memory_provider_registry=registry)
        ),
        state=SimpleNamespace(current_user="alice"),
    )
    result = asyncio.run(handler(request, AnswerRequest(prompt="question")))

    assert result["answer"] == "ok"
    assert captured["model"] == "nvidia/Qwen"
    assert captured["ollama_url"] == "http://vllm:8000/v1"
    assert captured["request_headers"] == {
        "Authorization": "Bearer spark-secret"
    }
