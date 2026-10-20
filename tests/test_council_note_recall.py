"""TOOL-03 — brain-note relevance/recall proof through build_messages_with_brain.

`build_messages_with_brain` (D-05) already recalls notes for any registry —
this is a relevance verification, not new retrieval infra: a security-flavored
prompt must surface the `awesome-ai-security` note, and an agent/skill prompt
must surface the `awesome-agent-skills` note. Uses the real
`MemoryProviderRegistry` + `PrecisionVaultProvider` over an index-less fixture
vault (`index_dir=None` forces the deterministic stdlib lexical ranker — the
Phase 9 P4 pattern, tests/test_verify_brain_notes.py), so no embedding index
is required. Offline: never `import app` (bs4/DEBT-03).

SEC-10 (Phase 11 Plan 2): notes and skill bodies are now fenced as UNTRUSTED
content via `src.prompt_security.untrusted_context_message` (role='user',
metadata.trusted=False) rather than injected as SYSTEM role. The shape
assertions below (mirroring
tests/test_security_regressions.py::test_untrusted_context_message_is_not_system_role)
prove the wrapper is genuinely applied — the pre-existing relevance tests
above pass with or without the wrapper and are not proof by themselves.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import pathlib

from src.council import seat_runner
from src.council.schemas import Seat
from src.memory_provider import MemoryProviderRegistry

# Dynamic load (providers/ predates being a normal package in some call sites
# -- same idiom as tests/test_verify_brain_notes.py / test_verify_integration_connections.py)
_spec = importlib.util.spec_from_file_location(
    "precision_vault", pathlib.Path("providers/precision_vault.py"))
_pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pv)

# Distinctly-worded fixture notes (unlike the shared-intro fixture in
# test_verify_brain_notes.py) so each prompt's lexical overlap picks out its
# own note specifically, proving RELEVANCE, not just presence.
_SECURITY_NOTE = """---
title: Awesome AI Security — curated frameworks, standards & tools
tags: [moc, knowledge/security, reference, ai-security]
---

A curated list of awesome AI security frameworks, guardrails, threat models,
red teaming tooling, and prompt-injection defenses for agentic AI systems.
"""

_AGENT_SKILLS_NOTE = """---
title: Awesome Agent Skills — curated agent-skill resources
tags: [moc, knowledge/agentsdk, reference, agent-skills]
---

A curated list of awesome agent skills, tool definitions, and resources for
building Claude Code and agentic AI skill ecosystems.
"""


def _fixture_vault(tmp_path: pathlib.Path) -> pathlib.Path:
    vault = tmp_path / "vault"
    security_dir = vault / "knowledge" / "security"
    agentsdk_dir = vault / "knowledge" / "agentsdk"
    security_dir.mkdir(parents=True)
    agentsdk_dir.mkdir(parents=True)
    (security_dir / "awesome-ai-security.md").write_text(_SECURITY_NOTE, encoding="utf-8")
    (agentsdk_dir / "awesome-agent-skills.md").write_text(_AGENT_SKILLS_NOTE, encoding="utf-8")
    return vault


def _fixture_registry(tmp_path: pathlib.Path) -> MemoryProviderRegistry:
    vault = _fixture_vault(tmp_path)
    # index_dir=None forces the index-less lexical recall path (Phase 9 P4) —
    # no embedding index required.
    provider = _pv.PrecisionVaultProvider(vault_path=str(vault), index_dir=None)
    return MemoryProviderRegistry(providers=[provider])


def test_security_prompt_recalls_awesome_ai_security(tmp_path):
    registry = _fixture_registry(tmp_path)
    seat = Seat(id="a", name="A", role="panelist")
    msgs = asyncio.run(seat_runner.build_messages_with_brain(
        seat, "What are the best AI security guardrails and red teaming tools?",
        [], registry))
    joined = " ".join(m["content"] for m in msgs)
    assert "Awesome AI Security" in joined


def test_agent_skill_prompt_recalls_awesome_agent_skills(tmp_path):
    registry = _fixture_registry(tmp_path)
    seat = Seat(id="a", name="A", role="panelist")
    msgs = asyncio.run(seat_runner.build_messages_with_brain(
        seat, "What agent skill tool definitions exist for Claude Code?",
        [], registry))
    joined = " ".join(m["content"] for m in msgs)
    assert "Awesome Agent Skills" in joined


# ── SEC-10 shape assertions: recalled notes/skills must be UNTRUSTED ────────
#
# The two relevance tests above pass with OR without the untrusted-context
# wrapper (they only check the note text is *somewhere* in the joined
# content) — they are not proof the wrapper is applied. These new tests
# assert the actual message shape, mirroring
# tests/test_security_regressions.py::test_untrusted_context_message_is_not_system_role.

def test_security_note_is_wrapped_untrusted_not_system_role(tmp_path):
    registry = _fixture_registry(tmp_path)
    seat = Seat(id="a", name="A", role="panelist")
    msgs = asyncio.run(seat_runner.build_messages_with_brain(
        seat, "What are the best AI security guardrails and red teaming tools?",
        [], registry))

    note_msg = next(m for m in msgs if "Awesome AI Security" in m["content"])
    assert note_msg["role"] == "user"
    assert note_msg["metadata"]["trusted"] is False

    # The seat's own persona/system prompt (index 0) must stay trusted.
    assert msgs[0]["role"] == "system"


def test_skill_block_is_wrapped_untrusted_not_system_role(tmp_path):
    """A seat with skills gets its skill-guidance block wrapped the same way
    as brain notes (SEC-10 covers both inserts identically)."""
    registry = _fixture_registry(tmp_path)
    seat = Seat(id="a", name="A", role="panelist", skills=["code-review"])
    msgs = asyncio.run(seat_runner.build_messages_with_brain(
        seat, "irrelevant prompt with no brain match at all zzzqqq",
        [], registry))

    skill_msg = next(m for m in msgs if "Skill guidance (code-review)" in m["content"])
    assert skill_msg["role"] == "user"
    assert skill_msg["metadata"]["trusted"] is False


def test_recall_provider_error_logs_warning_and_continues(tmp_path, caplog):
    """A provider whose .recall raises must no longer fail silently — a
    WARNING naming the provider is logged, and the run still returns (the
    never-raise contract is preserved)."""

    class _ExplodingProvider:
        provider_id = "exploding-provider"
        enabled = True

        async def recall(self, prompt, owner=None, top_k=3):
            raise RuntimeError("simulated recall failure")

        def __repr__(self):
            return f"<ExplodingProvider {self.provider_id!r}>"

    registry = MemoryProviderRegistry(providers=[_ExplodingProvider()])
    seat = Seat(id="a", name="A", role="panelist")

    with caplog.at_level(logging.WARNING):
        msgs = asyncio.run(seat_runner.build_messages_with_brain(
            seat, "any prompt", [], registry))

    assert any(
        record.levelno == logging.WARNING and "exploding-provider" in record.getMessage()
        for record in caplog.records
    )
    # The run still returns a valid message list (no exception propagated,
    # and the failed provider's hits are treated as empty).
    assert msgs
    assert msgs[0]["role"] == "system"


def test_malformed_hit_memory_does_not_raise_out_of_build_messages(tmp_path):
    """WR-03: `build_messages_with_brain` docstring claims "never raises", but
    the note-extraction step only guarded `getattr(h, "memory", None)` being
    truthy -- not that `.memory` actually has a usable `.text` attribute. A
    provider returning a malformed hit (memory object missing `.text`) must
    degrade (skip that hit) rather than raise AttributeError out of this
    function, which would otherwise propagate out of `asyncio.gather` in
    `run_seat`/`stream_seat` and abort the entire council run."""

    class _BrokenMemory:
        """No `.text` attribute at all."""

    class _MalformedHit:
        def __init__(self):
            self.memory = _BrokenMemory()

    class _MalformedHitProvider:
        provider_id = "malformed-hit-provider"
        enabled = True

        async def recall(self, prompt, owner=None, top_k=3):
            return [_MalformedHit()]

        def __repr__(self):
            return f"<MalformedHitProvider {self.provider_id!r}>"

    registry = MemoryProviderRegistry(providers=[_MalformedHitProvider()])
    seat = Seat(id="a", name="A", role="panelist")

    # Must not raise.
    msgs = asyncio.run(seat_runner.build_messages_with_brain(
        seat, "any prompt", [], registry))

    assert msgs
    assert msgs[0]["role"] == "system"
