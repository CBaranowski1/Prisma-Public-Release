"""Unit tests for src.provider_diagnostics.preflight_llm_host (GRAY-INT-10).

Uses the closed-socket technique for a deterministic "connection refused":
bind a socket to an ephemeral localhost port, read the assigned port, close
it immediately -- no Ollama/vLLM install required, no real network call.
"""
import socket

import src.provider_diagnostics as diagnostics
from src.provider_diagnostics import preflight_llm_host


def _closed_port_url() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def test_closed_port_reports_unreachable():
    url = _closed_port_url()
    result = preflight_llm_host(url)
    assert result["reachable"] is False


def test_actionable_message_present():
    url = _closed_port_url()
    result = preflight_llm_host(url)
    assert "is Ollama/vLLM running" in result["error"]
    assert "LLM_HOST" in result["error"]


def test_cached_dead_host_short_circuits():
    url = _closed_port_url()
    # _HOST_FAIL_THRESHOLD is 2 -- two consecutive failures actually cool
    # the host in src.llm_core's shared circuit breaker.
    preflight_llm_host(url)
    preflight_llm_host(url)
    # A further call short-circuits via the cooled-host branch instead of
    # attempting a fresh connection.
    result = preflight_llm_host(url)
    assert result["reachable"] is False
    assert "cooled" in result["error"]


def test_managed_dgx_probes_authenticated_models_resource(monkeypatch):
    monkeypatch.setenv("DGX_SPARK_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("DGX_SPARK_API_KEY", "spark-secret")
    calls = []

    class Response:
        status_code = 200
        is_success = True

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(diagnostics.httpx, "get", fake_get)
    result = preflight_llm_host(
        "http://vllm:8000/v1",
        api_key="spark-secret",
    )

    assert result["reachable"] is True
    assert calls[0][0] == "http://vllm:8000/v1/models"
    assert calls[0][1]["headers"] == {
        "Authorization": "Bearer spark-secret"
    }
