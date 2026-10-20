"""Hermetic coverage for the credential-safe GLM/OpenAI eval transport."""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_POLYGLOT = os.path.join(_REPO, "scripts", "polyglot_ab")
_HARNESS = os.path.join(_POLYGLOT, "harness.py")


def _harness():
    if _POLYGLOT not in sys.path:
        sys.path.insert(0, _POLYGLOT)
    spec = importlib.util.spec_from_file_location("polyglot_openai_harness", _HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://spark:1919", "http://spark:1919/v1/chat/completions"),
        ("http://spark:1919/v1", "http://spark:1919/v1/chat/completions"),
        ("http://spark:1919/v1/chat/completions",
         "http://spark:1919/v1/chat/completions"),
    ],
)
def test_openai_url_normalization(base, expected):
    harness = _harness()
    assert harness.openai_chat_url(base) == expected


def test_openai_call_sends_reasoning_and_env_only_auth(monkeypatch):
    harness = _harness()
    captured = {}
    monkeypatch.setenv("TEST_GLM_KEY", "top-secret-token")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response({"choices": [{"message": {"content": "```sql\nSELECT 1\n```"}}]})

    monkeypatch.setattr(harness.urllib.request, "urlopen", fake_urlopen)
    result = harness.call_openai(
        "dealignai/GLM-5.3-Flash-UNCENSORED-NVFP4",
        "return one",
        0.2,
        base_url="http://spark:1919/v1",
        reasoning_effort="high",
        api_key_env="TEST_GLM_KEY",
    )

    assert result.startswith("```sql")
    assert captured["url"] == "http://spark:1919/v1/chat/completions"
    assert captured["payload"]["reasoning_effort"] == "high"
    assert captured["payload"]["model"].startswith("dealignai/")
    assert captured["headers"]["Authorization"] == "Bearer top-secret-token"


def test_openai_rejects_url_credentials_and_invalid_key_env():
    harness = _harness()
    with pytest.raises(ValueError, match="without credentials"):
        harness.openai_chat_url("http://user:secret@spark:1919/v1")
    with pytest.raises(ValueError, match="variable name"):
        harness._api_key_from_env("literal secret")


def test_run_resume_is_fingerprinted_and_never_serializes_secret(tmp_path, monkeypatch):
    harness = _harness()
    dataset = tmp_path / "postgres.jsonl"
    output = tmp_path / "results.jsonl"
    dataset.write_text(json.dumps({
        "task_id": "postgres/one",
        "language": "postgres",
        "prompt": "Return one.",
        "reference": "SELECT 1",
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("TEST_GLM_KEY", "never-write-this-token")
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(kwargs.get("reasoning_effort"))
        return "```sql\nSELECT 1\n```"

    monkeypatch.setattr(harness, "call_model", fake_call)
    monkeypatch.setattr(harness, "score", lambda *args, **kwargs: (True, ""))
    common = dict(
        dataset=str(dataset), backend="openai", model="glm", arm="baseline",
        out_path=str(output), temperature=0.2, sql_backend="sqlite", limit=0,
        base_url="http://spark:1919/v1", api_key_env="TEST_GLM_KEY",
    )

    harness.run(**common, reasoning_effort="low")
    harness.run(**common, reasoning_effort="low")
    assert calls == ["low"], "an identical configuration should resume"

    harness.run(**common, reasoning_effort="max")
    assert calls == ["low", "max"], "a changed reasoning level must re-score"
    raw = output.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 1
    assert rows[0]["reasoning_effort"] == "max"
    assert "never-write-this-token" not in raw
    assert "spark:1919" not in raw
