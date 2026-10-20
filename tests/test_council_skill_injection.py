"""TOOL-02 — skill-body loader + seat skill injection.

Two groups (per 10-03-PLAN.md Task 3):

- `-k loader`: `load_skill_instructions` resolves a real skill directory's
  SKILL.md body and returns "" for a missing directory (Task 1 unit proof).
- injection: a real council `_run` (harness copied — not imported — from
  tests/test_unified_pipeline_e2e.py, per the repo's copy-don't-import
  convention) with a seat declaring `skills=["code-review"]` proves the
  skill body genuinely reaches that seat's assembled messages, and
  that a seat with empty `skills` gets no such block (non-regression).

SEC-10 (Phase 11 Plan 2): the skill-guidance block is now fenced as
UNTRUSTED content (role='user', metadata.trusted=False) via
`src.prompt_security.untrusted_context_message` rather than injected as a
raw system-role message — the assertion below was updated to search across
ALL assembled messages (not just role=='system') accordingly.
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.council import seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat
from src.skill_loader import load_skill_instructions

PROMPT = "Please help."


# ---------------------------------------------------------------------------
# Council harness (copied from tests/test_unified_pipeline_e2e.py:107-154,
# copy-don't-import convention — Pitfall 3)
# ---------------------------------------------------------------------------

def _make_stubs(monkeypatch, scripted: dict[str, list[str]] | None = None,
                default: str = "canned answer, short and simple."):
    captured: dict[str, list[list[dict]]] = {}
    scripted = {k: list(v) for k, v in (scripted or {}).items()}

    def _next(model: str) -> str:
        queue = scripted.get(model)
        if queue:
            return queue.pop(0)
        return default

    def fake_resolve(seat, owner):
        return (f"http://fake/{seat.id}", seat.id, {})

    async def fake_llm_call_async(url, model, messages, temperature=1.0, headers=None, **kw):
        captured.setdefault(model, []).append(messages)
        return _next(model)

    async def fake_stream_llm(url, model, messages, temperature=1.0, headers=None, **kw):
        captured.setdefault(model, []).append(messages)
        content = _next(model)
        yield f"data: {json.dumps({'delta': content})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(seat_runner, "_resolve", fake_resolve)
    import src.llm_core as llm_core_mod
    monkeypatch.setattr(llm_core_mod, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr(llm_core_mod, "stream_llm", fake_stream_llm)
    return captured


async def _run(cfg: CouncilConfig, prompt: str = PROMPT, **kw) -> list[dict]:
    events = []
    async for ev in run_council(prompt, cfg, **kw):
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# loader group (-k loader)
# ---------------------------------------------------------------------------

def test_loader_returns_real_skill_body():
    body = load_skill_instructions("code-review")
    assert body
    assert "front-matter" not in body.split("\n")[0]  # sanity: not the raw file


def test_loader_missing_directory_returns_empty():
    assert load_skill_instructions("does-not-exist") == ""


# ---------------------------------------------------------------------------
# injection group (through a real council run)
# ---------------------------------------------------------------------------

def test_seat_with_skill_gets_skill_guidance_block(monkeypatch):
    captured = _make_stubs(monkeypatch)
    cfg = CouncilConfig(seats=[
        Seat(id="code_reviewer", role="panelist", skills=["code-review"], consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["code_reviewer"]),
    ])

    import asyncio
    asyncio.run(_run(cfg))

    seat_messages = captured["code_reviewer"][0]
    joined = " ".join(m["content"] for m in seat_messages)
    assert "Skill guidance (code-review)" in joined
    body = load_skill_instructions("code-review")
    assert body[:80] in joined

    # SEC-10: the skill block is fenced as untrusted (role='user'), not
    # inserted with SYSTEM authority.
    skill_msg = next(m for m in seat_messages if "Skill guidance (code-review)" in m["content"])
    assert skill_msg["role"] == "user"
    assert skill_msg["metadata"]["trusted"] is False


def test_seat_with_empty_skills_gets_no_skill_block(monkeypatch):
    captured = _make_stubs(monkeypatch)
    cfg = CouncilConfig(seats=[
        Seat(id="plain", role="panelist", consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["plain"]),
    ])

    import asyncio
    asyncio.run(_run(cfg))

    seat_messages = captured["plain"][0]
    for m in seat_messages:
        if m["role"] == "system":
            assert "Skill guidance" not in m["content"]


def test_unreadable_skill_does_not_raise_out_of_build_messages(monkeypatch, caplog):
    """WR-03: `build_messages_with_brain` docstring claims "never raises", but
    `load_skill_instructions` does a real file read (`Path.read_text`) that
    can raise OSError (permissions/encoding) even when the skill directory
    exists. A bad skill file must degrade (skip that skill, log, keep going)
    rather than raise out of this function -- which would otherwise escape
    `run_seat`/`stream_seat`'s error isolation (they call
    `build_messages_with_brain` before their own try/except) and abort the
    whole council run via `asyncio.gather`."""

    def _broken_loader(skill_name):
        raise OSError("simulated unreadable skill file")

    monkeypatch.setattr(seat_runner, "load_skill_instructions", _broken_loader)

    seat = Seat(id="a", name="A", role="panelist", skills=["code-review"])

    with caplog.at_level(logging.WARNING):
        # Must not raise.
        msgs = asyncio.run(seat_runner.build_messages_with_brain(
            seat, "any prompt", [], None))

    assert msgs
    assert msgs[0]["role"] == "system"
    assert any(
        record.levelno == logging.WARNING and "code-review" in record.getMessage()
        for record in caplog.records
    )
