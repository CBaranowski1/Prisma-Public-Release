# src/council/fresh_context.py
"""Fresh-context discipline for the council — ported from GSD Core's core idea.

The council-agnostic curation/budgeting primitives (curate, distill, fits,
CuratedList, _with_content, and the DEFAULT_WINDOW/_MIN_PER_ITEM/_ELISION
constants) now live in `src.context_curation` (GUARD-01: extracted so
`answer_pipeline` and other non-council callers can reuse them without any
council coupling). This module re-exports them explicitly (see `__all__`
below) and keeps only the council-shaped glue: `_consumed` and `curate_for`.

Deterministic. No network. Never raises — a bad input just yields less context.
"""
from __future__ import annotations

from src.context_curation import (
    curate,
    distill,
    fits,
    CuratedList,
    DEFAULT_WINDOW,
    _MIN_PER_ITEM,
    _ELISION,
    _with_content,
)

__all__ = [
    "curate",
    "distill",
    "fits",
    "CuratedList",
    "DEFAULT_WINDOW",
    "_MIN_PER_ITEM",
    "_ELISION",
    "_with_content",
    "curate_for",
    "_consumed",
]


def _consumed(seat, results: dict) -> list:
    """Local reimplementation of orchestrator._upstream_for's one-line selection —
    NOT an import of it (importing from src.council.orchestrator would be circular:
    orchestrator -> fresh_context -> orchestrator, since orchestrator.py already
    imports this module at module scope). Declaration order in `seat.consumes` is
    preserved; ids absent from `results` are skipped, never a KeyError."""
    return [results[c] for c in seat.consumes if c in results]


def curate_for(seat, results: dict, window: int = DEFAULT_WINDOW) -> "CuratedList":
    """curate_for(seat, results, window) == curate(_upstream_for(seat, results), window)
    (D-07) — a standalone, orchestrator-independent entry point so SPINE-01 ("curate
    selects only the inputs a seat consumes and drops the rest") is unit-testable
    without spinning up run_council. Selection is done locally via `_consumed`; all
    size-bounding is delegated to `curate()`."""
    return curate(_consumed(seat, results), window)
