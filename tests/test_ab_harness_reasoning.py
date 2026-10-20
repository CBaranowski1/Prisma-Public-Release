"""Offline checks for evaluation-harness reasoning propagation."""

from __future__ import annotations

from types import SimpleNamespace

import scripts.ab_harness as harness


async def test_baseline_forwards_reasoning_effort(monkeypatch):
    captured = {}

    async def fake_llm(url, model, messages, **kwargs):
        captured.update(kwargs)
        return "answer"

    monkeypatch.setattr(harness, "llm_call_async", fake_llm)
    result = await harness.run_baseline(
        {"prompt": "solve"}, reasoning_effort="max")
    assert result == "answer"
    assert captured["reasoning_effort"] == "max"


async def test_real_arm_forwards_reasoning_effort(monkeypatch):
    captured = {}

    async def fake_council(prompt, cfg, **kwargs):
        captured.update(kwargs)
        yield {"type": "done", "final": "answer", "verified": True}

    monkeypatch.setattr(harness, "run_council", fake_council)
    events, result = await harness.run_real_arm(
        {"prompt": "solve"}, registry=SimpleNamespace(), cfg=SimpleNamespace(),
        reasoning_effort="high",
    )
    assert result == "answer"
    assert events[-1]["verified"] is True
    assert captured["reasoning_effort"] == "high"
