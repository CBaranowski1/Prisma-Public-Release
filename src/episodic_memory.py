"""Episodic / procedural memory write-back — let the agents LEARN across turns.

Reflexion-style self-improvement: after a NON-TRIVIAL SUCCESS the assistant
distills what actually worked into a compact, reusable lesson and stores it via
the EXISTING native-memory write path (tagged source/category="episodic"), so the
regular recall pipeline surfaces it on similar future turns as untrusted grounding.

Design constraints (mirrors GuardedProvider's fail-safe philosophy):
  * `extract_episode` is DETERMINISTIC — no LLM call. It decides, from cheap
    signals, whether a turn is worth remembering and distills the lesson.
  * Gated OFF by default behind the `agent_episodic_memory` setting; wiring sites
    swallow every error so a mis-write never breaks the user-facing turn.
  * We store the DISTILLED lesson (a few hundred chars), never the raw transcript.

The record shape:
    {title, trigger, procedure, source_turn_id, ts, source: "episodic"}
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

# Kept distinguishable from user facts/preferences so recall + the Brain tab can
# label episodic lessons as agent-learned procedure, not user-stated fact.
EPISODIC_SOURCE = "episodic"
EPISODIC_CATEGORY = "episodic"

# Effectful/coding tools whose successful use marks a turn as a real "did work"
# episode worth learning from. Mirrors agent_loop._VERIFIER_EFFECTFUL_TOOLS.
_EFFECTFUL_TOOLS = frozenset({
    "bash", "python", "write_file", "edit_file",
    "create_document", "update_document", "edit_document",
})

_MAX_TITLE = 90
_MAX_TRIGGER = 200
_MAX_PROCEDURE = 320
_MIN_ANSWER_CHARS = 40      # a "final answer" must be more than a one-liner ack
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with",
    "this", "that", "it", "is", "are", "was", "were", "be", "how", "do", "i",
    "my", "me", "you", "your", "can", "please", "help", "want", "need", "get",
})


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _keywords(text: str, k: int = 8) -> List[str]:
    """Cheap task-shape keywords for the trigger (when this lesson would help
    again). Deterministic: first-seen order, deduped, stopwords dropped."""
    out: List[str] = []
    for tok in re.findall(r"[A-Za-z0-9_.+\-/]{3,}", (text or "").lower()):
        if tok in _STOPWORDS or tok in out:
            continue
        out.append(tok)
        if len(out) >= k:
            break
    return out


def _effectful_tools_used(tool_events: List[Dict[str, Any]]) -> List[str]:
    """Names of effectful tools that ran WITHOUT a nonzero exit code."""
    used: List[str] = []
    for ev in tool_events or []:
        if not isinstance(ev, dict):
            continue
        tool = ev.get("tool", "")
        if tool not in _EFFECTFUL_TOOLS:
            continue
        rc = ev.get("exit_code")
        if rc not in (None, 0):
            continue  # a failed effectful call is not a "what worked" step
        used.append(tool)
    return used


def _distill_procedure(tool_events: List[Dict[str, Any]]) -> str:
    """One compact line per successful effectful step: `[tool] command`.

    Stores the distilled steps/commands that WORKED — not tool output, not the
    raw transcript. Head-of-command only, capped hard."""
    steps: List[str] = []
    for ev in tool_events or []:
        if not isinstance(ev, dict):
            continue
        tool = ev.get("tool", "")
        if tool not in _EFFECTFUL_TOOLS:
            continue
        if ev.get("exit_code") not in (None, 0):
            continue
        cmd = " ".join((ev.get("command") or "").split())
        if not cmd:
            # Document tools carry a title rather than a shell command.
            cmd = " ".join((ev.get("doc_title") or "").split())
        line = f"[{tool}] {cmd}".rstrip()
        if line not in steps:
            steps.append(line)
    return _clip(" ; ".join(steps), _MAX_PROCEDURE)


def extract_episode(
    question: str,
    final_answer: str,
    transcript: Any,
    outcome: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Deterministically decide if this turn is worth remembering, and if so
    distill a compact episodic record. Returns None for trivial turns.

    NO LLM call. Heuristics for a non-trivial success:
      (agent_loop path)   effectful/coding tools ran AND a real final answer was
                          reached WITHOUT the loop-breaker firing; or
      (answer_pipeline)   the reviewer APPROVED after >= 1 revision round.

    `transcript` is either the agent_loop `tool_events` list (dicts with
    tool/command/exit_code) or the answer_pipeline `transcript` list of role
    entries. `outcome` carries mode-specific signals:
      {"mode": "agent", "loop_broke": bool, "source_turn_id": str}
      {"mode": "pipeline", "approved": bool, "rounds": int, "source_turn_id": str}
    """
    outcome = outcome or {}
    mode = outcome.get("mode", "agent")
    answer = (final_answer or "").strip()
    source_turn_id = str(outcome.get("source_turn_id") or "")
    ts = int(time.time())

    if mode == "pipeline":
        # Non-trivial success: reviewer approved AND it took >= 1 revision round.
        # A first-round approval is trivial (the fast draft was already right).
        if not outcome.get("approved"):
            return None
        if int(outcome.get("rounds") or 0) < 2:
            return None
        if len(answer) < _MIN_ANSWER_CHARS:
            return None
        procedure = _clip(answer, _MAX_PROCEDURE)
        title = _clip(question or answer, _MAX_TITLE)
        trigger = _clip(" ".join(_keywords(question)), _MAX_TRIGGER)
        return {
            "title": title,
            "trigger": trigger,
            "procedure": procedure,
            "source_turn_id": source_turn_id,
            "ts": ts,
            "source": EPISODIC_SOURCE,
        }

    # agent_loop path -----------------------------------------------------
    if outcome.get("loop_broke"):
        return None  # the loop-breaker fired — this is a stall, not a success
    if len(answer) < _MIN_ANSWER_CHARS:
        return None  # no real final answer (bare ack / empty)

    tool_events = transcript if isinstance(transcript, list) else []
    used = _effectful_tools_used(tool_events)
    if not used:
        return None  # trivial prose turn — nothing procedural to learn

    procedure = _distill_procedure(tool_events)
    if not procedure:
        return None

    title = _clip(question or answer, _MAX_TITLE)
    # Trigger = task-shape keywords from the question plus the tools involved, so
    # a similar future request recalls this lesson.
    trig_words = _keywords(question) + sorted(set(used))
    trigger = _clip(" ".join(dict.fromkeys(trig_words)), _MAX_TRIGGER)
    return {
        "title": title,
        "trigger": trigger,
        "procedure": procedure,
        "source_turn_id": source_turn_id,
        "ts": ts,
        "source": EPISODIC_SOURCE,
    }


def _format_episode_text(episode: Dict[str, Any]) -> str:
    """Compact single-string body stored in the memory record. Kept short and
    self-labeling so it reads clearly when recalled as grounding."""
    title = (episode.get("title") or "").strip()
    trigger = (episode.get("trigger") or "").strip()
    procedure = (episode.get("procedure") or "").strip()
    parts = [f"Learned procedure: {title}" if title else "Learned procedure"]
    if trigger:
        parts.append(f"Use when: {trigger}")
    if procedure:
        parts.append(f"What worked: {procedure}")
    return " | ".join(parts)


async def write_episode(providers_or_manager, episode, owner=None):
    """Persist an episode via the EXISTING native-memory write path.

    `providers_or_manager` may be a MemoryProviderRegistry, a single
    MemoryProvider, or a raw MemoryManager. We reuse each of their supported
    write mechanisms (provider.remember(...) — the same contract manage_memory /
    NativeMemoryProvider.remember use). Tagged source/category="episodic" so it's
    distinguishable and recall can surface + label it.

    Returns the stored record (or True) on success, None on failure/no-op. Never
    raises — callers are fail-safe.
    """
    if not episode:
        return None
    text = _format_episode_text(episode)
    if not text.strip():
        return None

    metadata = {
        "trigger": episode.get("trigger", ""),
        "source_turn_id": episode.get("source_turn_id", ""),
        "episodic": True,
    }

    # 1) Registry: write through the FIRST provider that accepts a native-style
    #    remember() (the built-in native provider). CombinedBrainProvider raises
    #    NotImplementedError (read-only), which we skip past.
    active = None
    if hasattr(providers_or_manager, "active"):
        try:
            active = list(providers_or_manager.active())
        except Exception:
            active = None
    if active is None and hasattr(providers_or_manager, "all"):
        try:
            active = list(providers_or_manager.all())
        except Exception:
            active = None

    if active is not None:
        for provider in active:
            remember = getattr(provider, "remember", None)
            if remember is None:
                continue
            if not getattr(provider, "writable", True):
                continue  # read-only provider — skip before ever calling remember()
            try:
                return await remember(
                    text,
                    owner=owner,
                    category=EPISODIC_CATEGORY,
                    source=EPISODIC_SOURCE,
                    metadata=metadata,
                )
            except NotImplementedError:
                continue  # read-only provider (e.g. combined brain) — try next
            except Exception:
                continue
        return None

    # 2) A single provider with an async remember().
    remember = getattr(providers_or_manager, "remember", None)
    if remember is not None:
        try:
            return await remember(
                text,
                owner=owner,
                category=EPISODIC_CATEGORY,
                source=EPISODIC_SOURCE,
                metadata=metadata,
            )
        except Exception:
            return None

    # 3) A raw MemoryManager (add_entry + save), matching do_manage_memory.
    add_entry = getattr(providers_or_manager, "add_entry", None)
    if add_entry is not None:
        try:
            entry = add_entry(
                text, source=EPISODIC_SOURCE, category=EPISODIC_CATEGORY, owner=owner,
            )
            # GRAY-BRN-08: prefer the guarded add_and_save() funnel (locked
            # load->append->save) when the manager exposes it; fall back to
            # the older load_all/append/save shape for legacy/mock managers
            # (e.g. test doubles) that don't implement it.
            add_and_save = getattr(providers_or_manager, "add_and_save", None)
            if add_and_save is not None:
                return add_and_save(entry=entry)
            memories = providers_or_manager.load_all()
            memories.append(entry)
            providers_or_manager.save(memories)
            return entry
        except Exception:
            return None

    return None
