"""Persistent, model-free indexing of completed conversations and evaluations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_text
from core.file_locks import get_lock
from src.brain_privacy import redact_pii_and_secrets
from src.brains import resolve_brain

logger = logging.getLogger(__name__)

CONVERSATION_BRAIN_ID = "conversation-evals"
_ROLE_EXPORT = re.compile(
    r"(?im)^\s*(?:system|user|assistant|developer|critic|reviewer)\s*:"
)
_BENCHMARK_TERMS = re.compile(
    r"\b(?:benchmark|evaluation|eval|pass@|test case|expected output|"
    r"code critique|review finding|judge|rubric)\b",
    re.IGNORECASE,
)
_CRITIQUE_TERMS = re.compile(
    r"\b(?:critique|review finding|bug|vulnerabilit|incorrect|regression)\b",
    re.IGNORECASE,
)
_RULE_TERMS = re.compile(
    r"\b(?:must|should|never|avoid|ensure|required?|do not|don't|"
    r"cannot|can't|correct|validate|sanitize|escape|parameteriz)\b",
    re.IGNORECASE,
)
_CODE_TERMS = re.compile(
    r"\b(?:code|script|function|class|method|api|sql|query|database|"
    r"exception|error|input|output|secret|token|test|import|type|return)\b",
    re.IGNORECASE,
)


def classify_exchange(prompt: str, response: str, *, compare_mode: bool = False) -> str:
    combined = f"{prompt}\n{response}"
    if compare_mode or _BENCHMARK_TERMS.search(combined):
        return "code_benchmark"
    if _CRITIQUE_TERMS.search(combined) and (
        "```" in combined or _CODE_TERMS.search(combined)
    ):
        return "code_critique"
    if len(prompt) >= 1_000 and len(_ROLE_EXPORT.findall(prompt)) >= 2:
        return "imported_conversation"
    return "conversation"


def extract_code_rules(text: str, *, limit: int = 16) -> list[str]:
    """Extract explicit, test-oriented coding constraints without an LLM call."""
    rules: list[str] = []
    seen: set[str] = set()
    prose = re.sub(
        r"(?is)<untrusted_code>.*?</untrusted_code>|```.*?```",
        " ",
        text or "",
    )
    for part in re.split(r"(?<=[.!?])\s+|\r?\n+", prose):
        cleaned = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", part).strip()
        if not 12 <= len(cleaned) <= 500:
            continue
        if cleaned.startswith(("{", "[", "<")):
            continue
        if not _RULE_TERMS.search(cleaned) or not _CODE_TERMS.search(cleaned):
            continue
        key = " ".join(cleaned.lower().split())
        if key in seen:
            continue
        seen.add(key)
        rules.append(cleaned)
        if len(rules) >= limit:
            break
    return rules


def _owner_hash(owner: str | None) -> str:
    if not owner:
        return ""
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


def _record_id(
    session_id: str,
    prompt: str,
    response: str,
    model: str,
    surface: str,
) -> str:
    value = "\0".join((session_id, prompt, response, model, surface))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _already_indexed(index_dir: Path, record_id: str) -> bool:
    path = index_dir / "learned.json"
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text("utf-8"))
        meta = document.get("meta", []) if isinstance(document, dict) else document
        return any(
            item.get("id") == record_id
            for item in meta
            if isinstance(item, dict)
        )
    except Exception:
        return False


def index_exchange(
    prompt: str,
    response: str,
    *,
    session_id: str,
    owner: str | None = None,
    model: str = "",
    surface: str = "chat",
    compare_mode: bool = False,
    evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one idempotent exchange note and append its semantic embedding."""
    if not (prompt or "").strip() or not (response or "").strip():
        return {"indexed": False, "reason": "empty_exchange"}

    brain = resolve_brain(CONVERSATION_BRAIN_ID)
    if brain is None:
        return {"indexed": False, "reason": "conversation_brain_missing"}

    safe_prompt, prompt_redactions = redact_pii_and_secrets(prompt)
    safe_response, response_redactions = redact_pii_and_secrets(response)
    redactions = sorted(set(prompt_redactions + response_redactions))
    kind = classify_exchange(safe_prompt, safe_response, compare_mode=compare_mode)
    rules = extract_code_rules(f"{safe_prompt}\n{safe_response}")
    record_id = _record_id(session_id, safe_prompt, safe_response, model, surface)
    created_at = datetime.now(timezone.utc).isoformat()

    brain_dir = Path(brain["dir"])
    note_dir = Path(brain["path"]) / "knowledge" / "conversations"
    note_path = note_dir / f"{record_id}.md"
    index_dir = brain_dir / "index"
    title = f"{kind.replace('_', ' ').title()} {created_at[:19]}"
    frontmatter = {
        "id": record_id,
        "title": title,
        "kind": kind,
        "surface": surface,
        "session_id": session_id,
        "owner_hash": _owner_hash(owner),
        "model": model,
        "created_at": created_at,
        "redactions": redactions,
        "code_rules": rules,
        "evaluation": evaluation or {},
    }
    body = (
        "---\n"
        + "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in frontmatter.items()
        )
        + "\n---\n\n"
        + "## User\n\n"
        + safe_prompt.strip()
        + "\n\n## Assistant\n\n"
        + safe_response.strip()
        + "\n"
    )
    excerpt = " ".join(f"{safe_prompt}\n{safe_response}".split())[:800]

    with get_lock(brain_dir):
        note_dir.mkdir(parents=True, exist_ok=True)
        if not note_path.is_file():
            atomic_write_text(str(note_path), body)
        if not _already_indexed(index_dir, record_id):
            from src.brain_index import append_learned

            append_learned(
                index_dir,
                {
                    "id": record_id,
                    "category": "conversations",
                    "title": title,
                    "excerpt": excerpt,
                    "body": body,
                },
            )

    logger.info(
        "conversation_indexed id=%s surface=%s kind=%s rules=%d redaction_classes=%d",
        record_id,
        surface,
        kind,
        len(rules),
        len(redactions),
    )
    return {
        "indexed": True,
        "id": record_id,
        "kind": kind,
        "rules": len(rules),
        "redactions": redactions,
    }


async def index_exchange_async(*args, **kwargs) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(index_exchange, *args, **kwargs)
    except Exception as exc:
        logger.warning("conversation_index_failed: %s", exc)
        return {"indexed": False, "reason": type(exc).__name__}
