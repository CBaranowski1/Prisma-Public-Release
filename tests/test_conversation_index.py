import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import src.brain_index as brain_index
import src.conversation_index as conversation_index
from routes import chat_helpers


def _brain(tmp_path):
    (tmp_path / "knowledge" / "conversations").mkdir(parents=True)
    return {
        "id": "conversation-evals",
        "path": str(tmp_path),
        "dir": str(tmp_path),
    }


def test_pasted_critique_is_classified_and_rules_are_extracted():
    prompt = """USER: Review this benchmark.
ASSISTANT: The code is unsafe.
CRITIC: Database queries must use parameterized inputs.
The script should validate untrusted input before calling the API.
"""
    assert (
        conversation_index.classify_exchange(prompt, "Evaluation result: failed")
        == "code_benchmark"
    )
    rules = conversation_index.extract_code_rules(prompt)
    assert any("parameterized" in rule for rule in rules)
    assert any("validate" in rule for rule in rules)


def test_rule_extraction_ignores_code_and_serialized_model_payloads():
    text = """The API must validate untrusted input before returning output.
<untrusted_code>
def validate(value):
    return value
</untrusted_code>
```python
def ensure_output():
    return True
```
{"summary":"Code must validate input","replacement":"def output(): pass"}
"""
    assert conversation_index.extract_code_rules(text) == [
        "The API must validate untrusted input before returning output."
    ]


def test_exchange_is_redacted_written_and_indexed(tmp_path, monkeypatch):
    brain = _brain(tmp_path)
    appended = []
    monkeypatch.setattr(conversation_index, "resolve_brain", lambda _brain_id: brain)

    def fake_append(index_dir, note, embedder=None):
        appended.append(note)
        Path(index_dir).mkdir(parents=True, exist_ok=True)
        (Path(index_dir) / "learned.json").write_text(
            json.dumps({"meta": [{"id": note["id"]}]}),
            encoding="utf-8",
        )

    monkeypatch.setattr(brain_index, "append_learned", fake_append)
    result = conversation_index.index_exchange(
        "Use sk-FAKE1234567890ABCDEFGH1234 and contact dev@example.com",
        "The code must validate API input before returning output.",
        session_id="session-1",
        owner="alice",
        model="qwen",
    )
    assert result["indexed"] is True
    notes = list((tmp_path / "knowledge" / "conversations").glob("*.md"))
    assert len(notes) == 1
    content = notes[0].read_text("utf-8")
    assert "sk-FAKE" not in content
    assert "dev@example.com" not in content
    assert "[REDACTED_SECRET]" in content
    assert "[REDACTED_EMAIL]" in content
    assert len(appended) == 1

    second = conversation_index.index_exchange(
        "Use sk-FAKE1234567890ABCDEFGH1234 and contact dev@example.com",
        "The code must validate API input before returning output.",
        session_id="session-1",
        owner="alice",
        model="qwen",
    )
    assert second["indexed"] is True
    assert len(appended) == 1


def test_compare_response_is_dispatched_as_benchmark(monkeypatch):
    captured = {}

    async def fake_index(prompt, response, **kwargs):
        captured.update(prompt=prompt, response=response, **kwargs)

    monkeypatch.setattr(conversation_index, "index_exchange_async", fake_index)
    scheduled = []
    monkeypatch.setattr(chat_helpers.asyncio, "create_task", scheduled.append)
    session = SimpleNamespace(
        history=[],
        model="qwen",
        name="Existing session name",
        endpoint_url="http://vllm:8000/v1",
        headers={},
    )

    chat_helpers.run_post_response_tasks(
        session,
        SimpleNamespace(),
        "session-compare",
        "Evaluate this code benchmark.",
        "The implementation passes.",
        None,
        {"auto_memory": False, "auto_skills": False},
        None,
        None,
        None,
        compare_mode=True,
        owner="alice",
    )

    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert captured["compare_mode"] is True
    assert captured["session_id"] == "session-compare"
