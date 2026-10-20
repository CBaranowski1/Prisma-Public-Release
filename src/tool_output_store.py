"""tool_output_store.py — server-side full-output store for truncated tool output.

GRAY-AGT-02: bash/python tool output longer than MAX_OUTPUT_CHARS is fed back
to the model truncated (token-budget safety), but the FULL text must not be
silently destroyed. This store persists the full output keyed by a
non-guessable id, scoped to the owning (session_id, owner), so it is
retrievable in full server-side (for the SC-5 proof + human debugging).

Retrieval is RETRIEVAL-ONLY this phase — no model-visible retrieval tool is
wired into TOOL_TAGS/FUNCTION_TOOL_SCHEMAS (deferred). Access is scoped: a
`get()` whose (session_id, owner) does not match what was stored returns None,
so one session cannot fish another session's tool output by guessing an id.

Modeled directly on src/bg_jobs.py's "full content on disk, atomic write,
retrieve by id" precedent (uuid.uuid4().hex id convention, atomic_write_json,
DATA_DIR-rooted). File-backed so entries survive a server restart.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from core.atomic_io import atomic_write_json
from core.constants import DATA_DIR, MAX_TOOL_OUTPUT_STORE_FILES

logger = logging.getLogger(__name__)

_STORE_DIR = Path(DATA_DIR) / "tool_output"


def _record_path(output_id: str) -> Path:
    # output_id is a uuid hex (validated below) so it can never traverse.
    return _STORE_DIR / f"{output_id}.json"


def _is_valid_id(output_id: str) -> bool:
    return bool(output_id) and output_id.isalnum() and len(output_id) <= 64


def _prune_store(keep: int = MAX_TOOL_OUTPUT_STORE_FILES) -> None:
    """T-28-07 DoS (disk) bound: keep at most `keep` most-recent records under
    _STORE_DIR, deleting the oldest by mtime. Opportunistic + best-effort — any
    error is swallowed so pruning never breaks the tool result the model needs.
    A flood of over-limit tool calls therefore cannot grow the store without
    limit (footprint is bounded to ~keep records)."""
    try:
        records = sorted(
            _STORE_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        excess = len(records) - keep
        for path in records[:max(0, excess)]:
            try:
                path.unlink()
            except OSError:
                pass
    except Exception as e:
        logger.debug("tool_output_store prune skipped: %s", e, exc_info=True)


def put(text: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> str:
    """Persist the FULL `text`, scoped to (session_id, owner). Returns a
    non-guessable id used to retrieve it in full via `get()`."""
    output_id = uuid.uuid4().hex
    record = {
        "id": output_id,
        "session_id": session_id,
        "owner": owner,
        "text": text if isinstance(text, str) else ("" if text is None else str(text)),
    }
    try:
        atomic_write_json(str(_record_path(output_id)), record)
        # T-28-07: bound disk growth — prune oldest records beyond the cap.
        _prune_store()
    except Exception as e:
        # Never let a store failure break the tool result the model needs.
        logger.warning("tool_output_store.put failed for %s: %s", output_id, e, exc_info=True)
    return output_id


def get(output_id: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Optional[str]:
    """Return the full stored text ONLY when the caller's (session_id, owner)
    matches what was stored at put() time. Cross-session / cross-owner lookups
    and unknown ids return None (no raise)."""
    if not _is_valid_id(output_id):
        return None
    path = _record_path(output_id)
    try:
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("tool_output_store.get failed for %s: %s", output_id, e, exc_info=True)
        return None
    if not isinstance(record, dict):
        return None
    # Ownership scoping (Information Disclosure control): both must match.
    if record.get("session_id") != session_id or record.get("owner") != owner:
        logger.warning(
            "tool_output_store.get denied: id=%s requested by (session=%r, owner=%r) "
            "does not match stored scope", output_id, session_id, owner,
        )
        return None
    return record.get("text")
