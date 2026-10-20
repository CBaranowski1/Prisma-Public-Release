# src/context_curation.py
"""Council-agnostic context curation/budgeting primitives.

Context rot is quality decay as a model's window fills with accumulated state.
GSD's answer is "lean, curated context per phase": each call sees only what
it needs, bounded to fit a fresh window, verified before the call runs.

This module curates upstream contributions to a token/char budget WITHOUT an LLM:
keep each contribution's head and tail (the framing + the conclusion, which is
what a synthesizer actually needs) and elide the middle, then bound the total so
the assembled prompt is verified to fit before the call runs.

Deterministic. No network. Never raises — a bad input just yields less context.
"""
from __future__ import annotations

import copy as _copy
from dataclasses import is_dataclass as _is_dataclass, replace as _dc_replace

# Rough char budget for one call's assembled upstream context. ~4 chars/token,
# so 4000 chars ≈ 1000 tokens — lean enough to leave the model room to reason,
# generous enough to carry several distilled contributions.
DEFAULT_WINDOW = 4000
_MIN_PER_ITEM = 240   # never distill an item below this (keeps it meaningful)
_ELISION = "\n… [context trimmed for a fresh window] …\n"


class CuratedList(list):
    """list subclass carrying `.dropped_count` and `.dropped_ids` — the checkable
    "not silently overflowed" signal (D-03/SPINE-03). Behaves identically to a plain
    list for every existing consumer (isinstance, iteration, len, == comparison all
    work unchanged) because list.__eq__ compares by content, not type."""

    def __init__(self, *args):
        super().__init__(*args)
        self.dropped_count = 0
        self.dropped_ids = []


def distill(text: str, cap: int) -> str:
    """Bound `text` to ~cap chars, keeping the HEAD and TAIL (approach + result)
    and eliding the middle. Whole text returned unchanged when already under cap."""
    text = (text or "").strip()
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    if cap <= len(_ELISION) + 40:
        return text[:cap].rstrip()
    keep = cap - len(_ELISION)
    head = int(keep * 0.6)          # bias toward the opening framing
    tail = keep - head
    return text[:head].rstrip() + _ELISION + text[-tail:].lstrip()


def curate(upstream: list, window: int = DEFAULT_WINDOW) -> "CuratedList":
    """Return a bounded copy of the upstream results — total content is guaranteed
    to fit `window` (cap-then-resplit), never just each item individually.

    Only OK, non-empty contributions are kept, in declaration order (author
    priority). If more items are live than the window can afford at
    `_MIN_PER_ITEM` chars each, the overflow is dropped (not distilled to
    near-nothing) and the drop is surfaced via `.dropped_count`/`.dropped_ids`
    on the returned `CuratedList` — never a silent discard. The remaining items
    are `distill`ed to an even share of the window, so the combined context is
    verified to fit a fresh window before the call runs. Objects are COPIED —
    the originals are never mutated. Never raises, even when `window <
    _MIN_PER_ITEM` (max_items == 0): returns an empty CuratedList with
    dropped_count == len(live).
    """
    live = [r for r in (upstream or []) if getattr(r, "ok", False) and (getattr(r, "content", "") or "").strip()]
    if not live:
        return CuratedList([])
    max_items = window // _MIN_PER_ITEM
    if len(live) > max_items:
        kept = live[:max_items]          # declaration order = author priority (D-02)
        dropped = live[max_items:]
        dropped_count = len(dropped)
    else:
        kept = live
        dropped = []
        dropped_count = 0
    if not kept:
        # window < _MIN_PER_ITEM -> max_items == 0. Guard the window // len(kept)
        # division below (Pitfall 2) and preserve the never-raises contract.
        result = CuratedList([])
        result.dropped_count = dropped_count
        result.dropped_ids = [getattr(r, "seat_id", "") for r in dropped]
        return result
    per_item = window // len(kept)       # provably >= _MIN_PER_ITEM by construction
    out = CuratedList()
    for r in kept:
        trimmed = distill(r.content, per_item)
        out.append(_with_content(r, trimmed))
    out.dropped_count = dropped_count
    out.dropped_ids = [getattr(r, "seat_id", "") for r in dropped]
    return out


def _with_content(r, trimmed: str):
    """Return a COPY of r with content replaced — never mutate the original.
    Handles pydantic models (model_copy), dataclasses (replace), and plain objs."""
    if hasattr(r, "model_copy"):                      # pydantic v2 (SeatResult)
        try:
            return r.model_copy(update={"content": trimmed})
        except Exception:
            pass
    if _is_dataclass(r):
        try:
            return _dc_replace(r, content=trimmed)
        except Exception:
            pass
    try:
        c = _copy.copy(r)
        c.content = trimmed
        return c
    except Exception:
        return r


def fits(messages_or_text, window: int = DEFAULT_WINDOW) -> bool:
    """True if the total char count is <= `window` (GSD's 'plan fits a fresh
    context' check). Accepts a raw string, or a message list — in which case
    it sums EVERY message's 'content', system prompt included.

    This is a whole-payload measure, not an upstream-only one. To check
    D-08's "curated upstream fits a fresh window" guarantee specifically,
    measure just the upstream-derived content (e.g. the curated list's own
    text, or the isolated user-message block built from it) — passing a full
    assembled message list only proves the guarantee if the system persona
    (and any future brain-note block) happens to be small/empty, which is
    incidental, not something this function checks or bounds.
    """
    if isinstance(messages_or_text, str):
        total = len(messages_or_text)
    else:
        total = sum(len((m or {}).get("content", "")) for m in (messages_or_text or []))
    return total <= window
