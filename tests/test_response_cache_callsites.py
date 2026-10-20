"""GRAY-BRN-11 (T-32-xowner): representative call-site owner-forwarding test.

Plan 32-03 salted `_get_cache_key`/`llm_call`/`llm_call_async` with an
`owner` parameter (closing the requirement "on paper" per RESEARCH.md
Pitfall 3). That primitive alone does nothing unless the highest-risk
per-user call sites actually pass a real owner through. This test proves
one representative call site (`services/memory/skill_extractor.py`'s
`maybe_extract_skill`, which already carries a real `owner` parameter in
its own signature) forwards that owner into `llm_call_async` — hermetic,
no live LLM/network call.
"""

import asyncio

import src.llm_core as lc
from services.memory.skill_extractor import maybe_extract_skill


class _FakeSession:
    """Minimal stand-in for a chat Session — only get_context_messages() is used."""

    def get_context_messages(self):
        return [
            {"role": "user", "content": "please fix the bug"},
            {"role": "assistant", "content": "done, ran 3 shell commands"},
        ]


def _run(coro):
    return asyncio.run(coro)


def test_skill_extractor_forwards_real_owner(monkeypatch):
    """maybe_extract_skill's llm_call_async call must forward the REAL owner
    passed into its own `owner=` parameter -- not None, not empty, not a
    fabricated value."""
    recorded = {}

    async def fake_llm_call_async(*args, **kwargs):
        recorded["owner"] = kwargs.get("owner")
        recorded["called"] = True
        return "null"  # skill_extractor treats this as "no reusable skill" and returns early

    monkeypatch.setattr(lc, "llm_call_async", fake_llm_call_async)

    result = _run(
        maybe_extract_skill(
            session=_FakeSession(),
            skills_manager=None,
            endpoint_url="http://fake-llm.test/v1/chat/completions",
            model="fake-model",
            headers={},
            round_count=2,
            tool_count=3,
            owner="alice-real-owner",
        )
    )

    assert recorded.get("called") is True
    assert recorded.get("owner") == "alice-real-owner"
    assert recorded.get("owner") not in (None, "")
    assert result is None  # "null" response -> no skill extracted, confirms the call actually ran


def test_skill_extractor_forwards_owner_none_stays_none(monkeypatch):
    """When no real owner is available (owner=None, the default), the call
    site must NOT fabricate one -- it should forward None/"" honestly, same
    as the backward-compatible bucket in llm_core."""
    recorded = {}

    async def fake_llm_call_async(*args, **kwargs):
        recorded["owner"] = kwargs.get("owner")
        return "null"

    monkeypatch.setattr(lc, "llm_call_async", fake_llm_call_async)

    _run(
        maybe_extract_skill(
            session=_FakeSession(),
            skills_manager=None,
            endpoint_url="http://fake-llm.test/v1/chat/completions",
            model="fake-model",
            headers={},
            round_count=2,
            tool_count=3,
            owner=None,
        )
    )

    assert recorded.get("owner") is None  # honest passthrough, not fabricated
