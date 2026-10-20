"""Learning loop — let the shared brain grow from real conversations.

The goal of the brain is to be something the models reference *and learn from*.
After a normal exchange, this distills at most one reusable lesson from the
question+answer, runs it through the quality gate (`brain_filter.acceptable`),
and — if it passes — writes it as a note into the target brain's
`knowledge/lessons/` and appends its embedding to that brain's vector index, so
the very next recall can surface it.

Policy: shared auto-learning is private/opt-out by default — `is_enabled()`
defaults false, and the whole loop is silent until an admin explicitly sets
`brain_learning_enabled` true. Once enabled, `user_opted_out()` still lets an
admin opt individuals back out via a managed list. Incognito/compare
exchanges never learn.

Wiring: `run_post_response_tasks` fires `learn_from_exchange(...)` as a
background task, gated on the policy above. The actual model call mirrors the
memory extractor: `await llm_call_async(url, model, messages, ...)`.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

_LESSON_CATEGORY = "lessons"
_DISTILL_SYSTEM = (
    "You distill durable, reusable lessons from a single user/assistant "
    "exchange for a shared team knowledge base. A good lesson is a specific, "
    "generalizable fact, decision, convention, or gotcha that would help answer "
    "future questions — NOT a restatement of this one conversation, not "
    "pleasantries, not transient context.\n\n"
    "Reply with EXACTLY one of:\n"
    "  NO LESSON\n"
    "      (when nothing in the exchange is worth remembering)\n"
    "  TITLE: <a short noun-phrase title>\n"
    "  <one short paragraph stating the lesson, self-contained>\n\n"
    "Be conservative: most casual exchanges have NO LESSON."
)


def is_enabled() -> bool:
    """Automatic learning is OFF for everyone by default (private-by-default);
    an admin turns the whole feature on via the `brain_learning_enabled`
    setting. Reversible via this single named flag."""
    try:
        from src.settings import get_setting
        return bool(get_setting("brain_learning_enabled", False))
    except Exception:
        return False


def user_opted_out(user: Optional[str]) -> bool:
    """True if the admin has opted this user out (`brain_learning_optout` list)."""
    if not user:
        return False
    try:
        from src.settings import get_setting
        optout = get_setting("brain_learning_optout", []) or []
        return user in {str(u) for u in optout}
    except Exception:
        return False


def learning_target() -> Optional[Dict]:
    """Pick the brain that learnings are written into.

    If the `learning_target` setting names a brain id explicitly, that brain
    is used *instead of* first-sorted discovery — but only after re-applying
    the same inside-brains_root safety check: an explicit target that doesn't
    resolve to a brain, or that resolves *outside* brains_root (an external
    vault), is treated as broken and None is returned (never silently written
    into, and never silently falls back to a different brain — the caller is
    expected to surface this loudly).

    With no explicit target configured, falls back to the original behavior:
    the first discovered brain whose knowledge root lives *inside*
    brains_root. We never write into an external vault a brain merely points
    at (e.g. a read-only snapshot) — only into a brain Prisma actually owns
    on disk."""
    try:
        from src.brains import discover_brains, brains_root, resolve_brain
    except Exception:
        return None
    try:
        root = brains_root().resolve()
    except Exception:
        return None

    try:
        from src.settings import get_setting
        explicit = (get_setting("learning_target", "") or "").strip()
    except Exception:
        explicit = ""

    if explicit:
        b = resolve_brain(explicit)
        if b:
            try:
                p = Path(b["path"]).resolve()
            except Exception:
                return None
            if p == root or root in p.parents:
                return b
        # Configured but unresolvable or outside brains_root: broken. Do NOT
        # fall back to first-sorted discovery -- the caller must see this as
        # a failure (WARNING), not a silent switch to a different brain.
        return None

    for b in discover_brains():
        try:
            p = Path(b["path"]).resolve()
        except Exception:
            continue
        if p == root or root in p.parents:
            return b
    return None


def _existing_titles(lessons_dir: Path, index_dir: Path) -> List[str]:
    """Titles already learned — from lesson files + the appended index meta —
    so the filter can reject duplicates."""
    titles: List[str] = []
    if lessons_dir.is_dir():
        for f in lessons_dir.glob("*.md"):
            try:
                text = f.read_text("utf-8", "replace")
            except OSError:
                continue
            m = re.search(r"title:\s*(.+)", text)
            titles.append(m.group(1).strip() if m else f.stem.replace("-", " "))
    lj = index_dir / "learned.json"
    if lj.is_file():
        try:
            import json
            for row in json.loads(lj.read_text("utf-8")):
                t = row.get("title")
                if t:
                    titles.append(t)
        except Exception:
            pass
    return titles


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s[:60] or "lesson")


def add_learning(brain: Dict, title: str, body: str, source: str = "") -> bool:
    """Filter a candidate lesson and, if accepted, persist it into `brain`:
    write knowledge/lessons/<slug>.md and append it to the brain's vector index.
    Returns True if the lesson was added."""
    from src.brain_filter import acceptable
    from src.brain_index import append_learned
    from src.brain_privacy import contains_pii_or_secret

    knowledge_root = Path(brain["path"]) / "knowledge"
    lessons_dir = knowledge_root / _LESSON_CATEGORY
    index_dir = Path(brain.get("dir") or brain["path"]) / "index"

    ok, reason = acceptable(title, body, _existing_titles(lessons_dir, index_dir))
    if not ok:
        logger.debug("[brain-learn] rejected (%s): %r", reason, title)
        return False

    flagged, why = contains_pii_or_secret(f"{title}\n{body}")
    if flagged:
        # Fail-closed WITHHOLD: never write, never redact-and-continue.
        logger.warning("[brain-learn] withheld from shared brain (%s): %r", why, title)
        return False

    title = title.strip()
    body = body.strip()
    lessons_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{int(time.time())}-{_slugify(title)}"
    fm = [f"title: {title}", f"category: {_LESSON_CATEGORY}", "origin: learned"]
    if source:
        fm.append(f"source: {source}")
    note_text = "---\n" + "\n".join(fm) + "\n---\n\n" + body + "\n"
    note_path = lessons_dir / f"{slug}.md"
    try:
        note_path.write_text(note_text, encoding="utf-8")
    except OSError as e:
        logger.warning("[brain-learn] could not write lesson %s: %s", note_path, e)
        return False

    try:
        append_learned(index_dir, {
            "id": slug, "category": _LESSON_CATEGORY,
            "title": title, "excerpt": " ".join(body.split())[:400], "body": body,
        })
    except Exception as e:
        # The note is on disk and will be picked up on the next full reindex even
        # if live append fails (no embedder, etc.) — don't lose the lesson.
        logger.warning("[brain-learn] indexed write failed for %s: %s", slug, e)

    logger.info("[brain-learn] learned: %r -> %s", title, brain.get("id", "?"))
    return True


def _parse_lesson(raw: str) -> Optional[tuple[str, str]]:
    """Parse the model reply into (title, body), or None for NO LESSON / junk."""
    text = (raw or "").strip()
    if not text:
        return None
    if text[:40].upper().lstrip().startswith("NO LESSON"):
        return None
    m = re.search(r"^\s*TITLE:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if m:
        title = m.group(1).strip()
        body = text[m.end():].strip()
        if not body:
            body = title
        return title, body
    # No explicit TITLE: use first line as title, the rest as body.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    title = lines[0][:120]
    body = "\n".join(lines[1:]).strip() or text
    return title, body


async def learn_from_exchange(
    question: str,
    answer: str,
    *,
    endpoint_url: str,
    model: str,
    headers: Optional[Dict] = None,
    owner: Optional[str] = None,
) -> bool:
    """Distill one reusable lesson from a question/answer pair and add it to the
    target brain. Best-effort and safe to fire-and-forget: any failure is logged
    and swallowed. Returns True if a lesson was added."""
    if not is_enabled() or user_opted_out(owner):
        return False
    if not (question and answer):
        return False
    brain = learning_target()
    if not brain:
        explicit_target = ""
        try:
            from src.settings import get_setting
            explicit_target = (get_setting("learning_target", "") or "").strip()
        except Exception:
            explicit_target = ""
        if explicit_target:
            # A configured target that didn't resolve is a misconfiguration,
            # not routine "nothing to write to" -- surface it loudly.
            logger.warning(
                "[brain-learn] configured learning_target did not resolve to "
                "a writable in-root brain; skipping"
            )
        else:
            logger.debug("[brain-learn] no writable target brain; skipping")
        return False

    from src.llm_core import llm_call_async
    convo = (
        "User asked:\n" + question.strip()[:4000]
        + "\n\nAssistant answered:\n" + answer.strip()[:4000]
    )
    messages = [
        {"role": "system", "content": _DISTILL_SYSTEM},
        {"role": "user", "content": convo},
    ]
    try:
        raw = await llm_call_async(
            endpoint_url, model, messages,
            temperature=0.2, max_tokens=300, headers=headers,
            owner=owner,
        )
    except Exception as e:
        logger.warning("[brain-learn] distill call failed: %s", e)
        return False

    parsed = _parse_lesson(raw)
    if not parsed:
        logger.debug("[brain-learn] no lesson from exchange")
        return False
    title, body = parsed
    return add_learning(brain, title, body, source=f"chat:{owner or 'unknown'}")
