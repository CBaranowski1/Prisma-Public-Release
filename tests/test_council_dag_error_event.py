"""tests/test_council_dag_error_event.py — GRAY-AGT-07 (Phase 29 Plan 02).

Proves a cyclic CouncilConfig makes run_council yield {"type": "error",
"error": <str>} as its FIRST event and return cleanly, with NO exception
escaping the async generator — driven DIRECTLY against run_council (never
through routes/council_routes.py's blanket try/except, which would mask this
bug for its one caller — 29-RESEARCH.md Pitfall 3).

Also proves the non-regression case: a valid (acyclic) config still yields
run_started as its first event.

Note on "missing dependency" (deviation from the plan's literal wording,
discovered during test-writing): `CouncilConfig.active_seats()` (schemas.py,
untouched by this plan) prunes every seat's `consumes` list down to ids that
are actually in the enabled seat set BEFORE `layers()` ever runs. A dependency
pointing outside that set is therefore silently dropped, never seen by the
Kahn's-algorithm-style layering loop — by the algorithm's own invariant, a
"no ready seats" deadlock over a graph whose edges are already restricted to
its own vertex set is ALWAYS a genuine cycle, never a bare dangling
reference. So a seat consuming a nonexistent id, on its own, cannot trigger
`layers()`'s ValueError — it is safely pruned and the run proceeds normally.
`test_missing_dependency_is_pruned_not_errored` proves this directly; the
error-path tests below use genuine cycles (the only reachable trigger).
"""
from __future__ import annotations

from src.council.schemas import CouncilConfig, Seat
from tests.test_council_fresh_context_integration import PROMPT, _make_stubs, _run


def _cyclic_cfg() -> CouncilConfig:
    """Two seats that consume each other — a direct 2-cycle."""
    return CouncilConfig(seats=[
        Seat(id="a", role="panelist", consumes=["b"]),
        Seat(id="b", role="synthesizer", consumes=["a"]),
    ])


def _self_cycle_cfg() -> CouncilConfig:
    """A seat that consumes itself — a 1-node cycle (a distinct shape from
    the mutual 2-cycle above, still reachable through the public
    CouncilConfig constructor)."""
    return CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", consumes=["chief"]),
    ])


def _missing_dep_cfg() -> CouncilConfig:
    """A synthesizer consuming a seat id that doesn't exist — pruned by
    active_seats(), NOT a cycle; see module docstring."""
    return CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", consumes=["ghost"]),
    ])


def _valid_cfg() -> CouncilConfig:
    return CouncilConfig(seats=[
        Seat(id="p1", role="panelist", consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["p1"]),
    ])


async def test_cyclic_dag_yields_error_event_first_no_exception(monkeypatch):
    _make_stubs(monkeypatch)
    events = await _run(_cyclic_cfg(), PROMPT)
    assert events, "run_council must yield at least one event"
    assert events[0]["type"] == "error"
    err = events[0]["error"].lower()
    assert "cycle" in err or "missing dependency" in err
    # Generator ends cleanly right after the error event — no run_started.
    assert len(events) == 1


async def test_self_cycle_dag_yields_error_event_first_no_exception(monkeypatch):
    _make_stubs(monkeypatch)
    events = await _run(_self_cycle_cfg(), PROMPT)
    assert events, "run_council must yield at least one event"
    assert events[0]["type"] == "error"
    err = events[0]["error"].lower()
    assert "cycle" in err or "missing dependency" in err
    assert len(events) == 1


async def test_missing_dependency_is_pruned_not_errored(monkeypatch):
    """A bare non-existent `consumes` id is pruned by active_seats() before
    layers() runs — the run proceeds normally, it does not error. Documents
    the actual (correct, untouched) behavior of schemas.py's pruning."""
    _make_stubs(monkeypatch)
    events = await _run(_missing_dep_cfg(), PROMPT)
    assert events[0]["type"] == "run_started"
    assert not any(e["type"] == "error" for e in events)


async def test_valid_config_still_yields_run_started(monkeypatch):
    """Non-regression: the try/except is transparent on the happy path."""
    _make_stubs(monkeypatch)
    events = await _run(_valid_cfg(), PROMPT)
    assert events[0]["type"] == "run_started"
