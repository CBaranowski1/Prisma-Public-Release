"""SEC-28 (D-04) — standing UNTRUSTED_CONTEXT_POLICY broadcast coverage
(18-04-PLAN.md).

Proves the standing policy system message is present exactly once on each
of the three paths that lacked it -- agent_loop (`stream_agent_loop`,
covering task_scheduler/teacher_escalation/bg_monitor/skills_routes callers
that bypass chat_processor), Council (`seat_runner`), and Answer
(`answer_pipeline.wire_defaults`) -- and that the already-covered
chat_routes.py path (via `chat_processor.py:236`) is NOT duplicated.

Offline, stub-driven -- no real LLM / brain / network.
"""
from __future__ import annotations

import asyncio

import httpx

import src.agent_loop as agent_loop
import src.answer_pipeline as answer_pipeline
from src.council import seat_runner
from src.council.schemas import Seat
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY


def _policy_message_count(messages: list[dict]) -> int:
    marker = "Prompt-safety policy:"
    return sum(
        1 for m in messages
        if m.get("role") == "system" and marker in (m.get("content") or "")
    )


# ---------------------------------------------------------------------------
# agent_loop / stream_agent_loop (`-k agent_loop`)
# ---------------------------------------------------------------------------

def test_agent_loop_policy_inserted_once_when_absent():
    # Simulates a non-chat_routes caller (task_scheduler/teacher_escalation/
    # bg_monitor/skills_routes) that builds its own messages list with no
    # policy at all.
    messages = [{"role": "user", "content": "hi"}]
    agent_loop._ensure_untrusted_context_policy(messages)

    assert messages[0] == {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY}
    assert len(messages) == 2
    assert _policy_message_count(messages) == 1


def test_agent_loop_policy_not_duplicated_on_chat_routes_path():
    # Simulates the chat_routes.py path: chat_processor.py:236 already
    # injected the policy verbatim into ctx.messages before stream_agent_loop
    # runs. Calling the idempotent guard again must NOT add a second copy
    # (18-RESEARCH.md Pitfall 4).
    messages = [
        {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY},
        {"role": "user", "content": "hi"},
    ]
    agent_loop._ensure_untrusted_context_policy(messages)

    assert len(messages) == 2  # unchanged length -- proves no duplicate insert
    assert _policy_message_count(messages) == 1


def test_agent_loop_policy_guard_keys_on_substring_not_exact_equality():
    # The guard must key on a stable SUBSTRING of the policy (its first
    # words), not exact string equality, per the plan's <behavior> spec.
    messages = [
        {"role": "system", "content": f"(preface) {UNTRUSTED_CONTEXT_POLICY}"},
    ]
    agent_loop._ensure_untrusted_context_policy(messages)

    assert len(messages) == 1
    assert _policy_message_count(messages) == 1


# ---------------------------------------------------------------------------
# Council (seat_runner)
# ---------------------------------------------------------------------------

def test_council_policy_present_no_registry_no_skills():
    # Appended (not inserted at index 1) so the pre-existing pinned test
    # tests/test_fresh_context.py::test_build_messages_default_is_unchanged
    # (which indexes _build_messages(...)[1] expecting the USER message)
    # and tests/test_council_brain_injection.py::test_no_registry_is_plain_build
    # (build_messages_with_brain(registry=None) == _build_messages(...)) both
    # stay green -- see seat_runner._ensure_untrusted_context_policy's
    # docstring.
    seat = Seat(id="a", name="A", role="panelist")
    msgs = asyncio.run(seat_runner.build_messages_with_brain(seat, "hi", [], None))

    assert msgs[0]["role"] == "system"  # persona
    assert msgs[1]["role"] == "user"    # user prompt, undisturbed (pinned contract)
    assert msgs[-1] == {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY}
    assert _policy_message_count(msgs) == 1


def test_council_policy_idempotent_across_two_independent_builds():
    seat = Seat(id="a", name="A", role="panelist")
    msgs_1 = asyncio.run(seat_runner.build_messages_with_brain(seat, "hi", [], None))
    msgs_2 = asyncio.run(seat_runner.build_messages_with_brain(seat, "hi", [], None))

    assert _policy_message_count(msgs_1) == 1
    assert _policy_message_count(msgs_2) == 1


class _FakeReg:
    def active(self):
        class P:
            provider_id = "test-provider"

            async def recall(self, q, *, owner=None, top_k=3):
                from src.memory_provider import MemoryRecord, MemorySearchHit
                return [MemorySearchHit(
                    memory=MemoryRecord(id="1", text="recalled brain note"),
                    provider_id="test-provider",
                    score=1.0,
                )]
        return [P()]


def test_council_policy_present_exactly_once_with_registry_and_skills():
    seat = Seat(id="a", name="A", role="panelist", skills=["code-review"])
    msgs = asyncio.run(seat_runner.build_messages_with_brain(
        seat, "hi", [], _FakeReg()))

    assert msgs[0]["role"] == "system"  # persona
    assert _policy_message_count(msgs) == 1
    assert msgs[-1] == {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY}

    # Brain-note/skill CONTENT inserts remain role="user"/fenced (unchanged).
    fenced = [m for m in msgs if m.get("role") == "user" and m.get("metadata", {}).get("trusted") is False]
    assert fenced, "expected at least one fenced brain-note/skill insert"


# ---------------------------------------------------------------------------
# Answer pipeline (wire_defaults)
# ---------------------------------------------------------------------------

class _FakeHttpResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_answer_wire_defaults_model_fn_prepends_policy(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["messages"] = json["messages"]
        return _FakeHttpResponse("an answer")

    monkeypatch.setattr(httpx, "post", fake_post)

    fns = answer_pipeline.wire_defaults("test-model", registry=None)
    out = fns["model_fn"]("What is the capital of France?")

    assert out == "an answer"
    assert captured["messages"][0] == {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY}
    assert _policy_message_count(captured["messages"]) == 1


def test_answer_wire_defaults_author_fn_gets_policy_without_duplicating_its_own_system_message(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["messages"] = json["messages"]
        return _FakeHttpResponse("a thorough answer")

    monkeypatch.setattr(httpx, "post", fake_post)

    fns = answer_pipeline.wire_defaults("test-model", registry=None)
    fns["author_fn"]("some draft prompt")

    messages = captured["messages"]
    # Policy is prepended ahead of author_fn's own system prompt -- exactly
    # one message carries the policy marker; author_fn's own `sysp` (which
    # does not contain the marker) is untouched, just pushed to index 1.
    assert messages[0] == {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY}
    assert messages[1]["role"] == "system"
    assert "Prompt-safety policy:" not in messages[1]["content"]
    assert _policy_message_count(messages) == 1
