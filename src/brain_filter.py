"""Quality gate for notes that want to enter the shared brain.

The people using Prisma aren't AI experts, so they don't hand-pick what the
brain learns — the system does, automatically. This is the gate: a candidate
learning is accepted only if it carries real signal and isn't a near-duplicate
of something already in the brain. Rule-based now (one pure function, the same
"model-later" seam idea as format_note); swap the body for a model later.
"""
from __future__ import annotations

import re

_MIN_WORDS = 8           # below this it's not a real lesson
_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{2,}")


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def acceptable(title: str, body: str, existing_titles) -> tuple[bool, str]:
    """Return (ok, reason). Rejects empties, too-short, model 'no lesson'
    sentinels, and titles that duplicate an existing note (case/punct-insensitive)."""
    body = (body or "").strip()
    title = (title or "").strip()
    if not title or not body:
        return False, "empty"
    low = body.lower()
    if "no lesson" in low[:40] or low in ("none", "n/a", "nothing"):
        return False, "no-lesson sentinel"
    if len(_WORD.findall(body)) < _MIN_WORDS:
        return False, "too short"
    nt = _norm_title(title)
    if nt and nt in {_norm_title(t) for t in (existing_titles or [])}:
        return False, "duplicate title"
    return True, "ok"
