# src/council/seat_runner.py
"""Run a single council seat through Prisma's existing model layer.

A seat only knows *which* model (endpoint_id + model) and *how* to behave
(persona/role). Everything else — provider detection, auth headers, fallback,
dead-host cooldown, response caching — is inherited for free from
`endpoint_resolver` + `llm_core`. This is the seam that makes models modular:
swap a seat's endpoint_id/model and the next run uses it, no restart.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from src.council.schemas import Seat, SeatResult
from src.prompt_security import untrusted_context_message, UNTRUSTED_CONTEXT_POLICY
from src.skill_loader import load_skill_instructions
# parse_tool_blocks is imported via the src.agent_tools facade (not directly
# from src.tool_parsing) to avoid a real circular-import crash: tool_parsing
# imports ToolBlock/TOOL_TAGS from agent_tools at its own module top, and if
# tool_parsing were the FIRST module loaded (as it would be if imported
# directly here before anything else touches agent_tools), agent_tools would
# in turn try to re-import parse_tool_blocks/etc. from a still-partially-
# initialized tool_parsing module. Routing through agent_tools guarantees
# agent_tools' own TOOL_TAGS/ToolBlock definitions land first, regardless of
# import order (Rule 1 auto-fix — a real ImportError, not a style choice).
from src.agent_tools import parse_tool_blocks
# Imported at module top (not lazily inside a function) so `execute_tool_block`
# is a stable, LIVE attribute of this module — the D-06 through-the-team proof
# monkeypatches `seat_runner.execute_tool_block` directly; a string-path patch
# would re-import a second, unpatched copy the moment it's absent from
# sys.modules (Phase 6 war story / RESEARCH anti-pattern).
from src.tool_execution import execute_tool_block

logger = logging.getLogger(__name__)

# Role framing appended to each seat's persona so a bare persona still produces
# the right kind of contribution.
_ROLE_FRAMING = {
    "panelist": (
        "You are one member of a council answering the user's prompt. "
        "Give your own complete, well-reasoned answer from your perspective."
    ),
    "aggregator": (
        "You are an aggregator on a council. Read the panel members' answers "
        "below and consolidate them into a single coherent intermediate answer, "
        "noting where they agree and disagree."
    ),
    "synthesizer": (
        "You are the chief synthesizer of a council. Read every contribution "
        "below and produce the single best final answer for the user. Reconcile "
        "disagreements, keep what is strongest, and resolve to a clear conclusion. "
        "Do not mention the council process unless it helps the user."
    ),
}


def _system_prompt(seat: Seat) -> str:
    persona = (seat.persona or "").strip()
    framing = _ROLE_FRAMING.get(seat.role, _ROLE_FRAMING["panelist"])
    return f"{persona}\n\n{framing}".strip() if persona else framing


# SEC-28 (D-04): standing UNTRUSTED_CONTEXT_POLICY broadcast, idempotent --
# mirrors src/chat_processor.py:236 (the sole pre-existing injection) and
# src/agent_loop.py's stream_agent_loop check. The Council path never had it.
_POLICY_MARKER = "Prompt-safety policy:"  # stable leading substring of UNTRUSTED_CONTEXT_POLICY


def _ensure_untrusted_context_policy(msgs: list[dict]) -> None:
    """Idempotently APPEND UNTRUSTED_CONTEXT_POLICY as the final message. The
    policy text is a static first-party constant -- not attacker-influenceable
    content -- so role="system" is correct here, unlike the brain-note/skill
    CONTENT which stays fenced as role="user" (SEC-10).

    Deliberately APPENDED (not inserted at index 1) rather than 18-04-PLAN.md's
    original `msgs.insert(1, ...)` sketch: `tests/test_fresh_context.py::
    test_build_messages_default_is_unchanged` is a pre-existing PINNED test
    that indexes `_build_messages(...)[1]` expecting the USER message
    (`msgs_default[1]["content"]`) -- inserting at index 1 would silently
    break that non-regression contract (CLAUDE.md: `tests/test_fresh_context.py`
    must stay green). Appending preserves both that contract AND
    `test_no_registry_is_plain_build`'s `build_messages_with_brain(registry=None)
    == _build_messages(...)` equality, since both paths call this same helper
    on the same starting list. Lives in `_build_messages` (not
    `build_messages_with_brain`) so BOTH the no-registry and with-registry
    paths get it identically."""
    if any(
        m.get("role") == "system" and _POLICY_MARKER in (m.get("content") or "")
        for m in msgs
    ):
        return
    msgs.append({"role": "system", "content": UNTRUSTED_CONTEXT_POLICY})


def _build_messages(seat: Seat, prompt: str, upstream: list[SeatResult]) -> list[dict]:
    """System (persona+framing) + user (prompt, plus labeled upstream answers).

    `upstream` is assembled as-is — curation (if any) happens upstream of this
    call, at the orchestrator's `curate_for` boundary (D-01/D-02).
    """
    user_parts = [f"User prompt:\n{prompt}"]
    if upstream:
        blocks = []
        for r in upstream:
            if r.ok and r.content.strip():
                label = r.name or r.seat_id
                blocks.append(f"### {label}\n{r.content.strip()}")
        if blocks:
            user_parts.append(
                "Here are the contributions from earlier council members:\n\n"
                + "\n\n".join(blocks)
            )
    msgs = [
        {"role": "system", "content": _system_prompt(seat)},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    # SEC-28 (D-04): standing policy, appended last -- see
    # _ensure_untrusted_context_policy's docstring for why APPEND (not
    # insert-at-1) is required here.
    _ensure_untrusted_context_policy(msgs)
    return msgs


async def build_messages_with_brain(
    seat: Seat,
    prompt: str,
    upstream: list[SeatResult],
    registry,
    owner: str | None = None,
    top_k: int = 3,
) -> list[dict]:
    """Like _build_messages but prepends relevant brain notes (best-effort).

    A failing/empty provider simply contributes nothing — never raises.
    When registry is None the result is identical to _build_messages(...).
    """
    msgs = _build_messages(seat, prompt, upstream)
    if registry:
        notes = []
        for p in registry.active():
            try:
                hits = await p.recall(prompt, owner=owner, top_k=top_k)
            except Exception as e:
                logger.warning(
                    "brain recall failed for provider %r: %s", p, e, exc_info=True,
                )
                hits = []
            # WR-03: per-hit try/except, not a bare comprehension -- `h.memory`
            # being truthy does not guarantee it has a `.text` attribute. A
            # single malformed hit must degrade (skip that hit, log, keep
            # going) rather than raising AttributeError out of this
            # "never raises" function and aborting the whole council run.
            for h in hits:
                mem = getattr(h, "memory", None)
                if not mem:
                    continue
                try:
                    notes.append(mem.text)
                except Exception as e:
                    logger.warning(
                        "brain recall for provider %r: malformed hit (no usable "
                        "memory.text), skipping: %s", p, e,
                    )
        if notes:
            block = "Relevant knowledge from the brain:\n" + "\n".join(
                f"- {n}" for n in notes[:top_k]
            )
            # Insert right after the system persona message (index 0) --
            # unchanged from pre-D04 (the SEC-28/D-04 standing policy is
            # APPENDED at the end of `_build_messages`'s output, not inserted
            # here, so this index is undisturbed). Recalled notes are
            # attacker-influenceable (brain ingest) so they are fenced as
            # untrusted (role='user', metadata.trusted=False) rather than
            # inserted with SYSTEM authority (SEC-10).
            msgs.insert(1, untrusted_context_message("brain notes", block))

    # D-04 (TOOL-02): a seat "loads" a skill by injecting the skill's SKILL.md
    # body as an untrusted context block, text-only (T-09-05 invariant — never
    # eval/exec/import/compile). Read only here, in message assembly — NEVER
    # in layers()/active_seats() (Pitfall 3/T-10-08). Empty `seat.skills`
    # (the default) adds nothing, so `msgs` stays byte-for-byte identical to
    # today for every existing council config.
    for skill_name in getattr(seat, "skills", []):
        # WR-03: load_skill_instructions does a real file read and can raise
        # OSError (permissions/encoding) even when the path exists. This
        # function's docstring promises "never raises" -- a bad skill file
        # must degrade (skip that skill, log, keep going), not abort the
        # whole council run.
        try:
            body = load_skill_instructions(skill_name, owner=owner)
        except Exception as e:
            logger.warning(
                "seat %s: failed to load skill %r, skipping: %s", seat.id, skill_name, e,
                exc_info=True,
            )
            continue
        if body:
            # Skill bodies are files on disk (attacker-influenceable, same
            # trust boundary as brain notes) so they are fenced as untrusted
            # too (SEC-10), not inserted with SYSTEM authority. Index 1,
            # unchanged from pre-D04 -- same rationale as the brain-note
            # insert above.
            msgs.insert(1, untrusted_context_message(
                f"skill: {skill_name}",
                f"Skill guidance ({skill_name}):\n{body}",
            ))

    return msgs


async def _dispatch_owned_tools(seat: Seat, content: str, owner: str | None = None) -> None:
    """D-03/TOOL-01: the seat-level tool-invocation step the council lacked.

    A seat only ever dispatches tools it OWNS (`seat.tools`) — a tool block
    whose `tool_type` isn't in that list is ignored. Additive and gated on
    non-empty `tools`: when a seat declares no tools this never calls
    `parse_tool_blocks`/`execute_tool_block` at all, so every existing council
    config (empty `tools` by default) stays byte-for-byte unchanged
    (Pitfall 3/T-10-08 non-regression).

    `owner` is threaded straight into `execute_tool_block` so Plan 02's
    fail-closed `can_manage_data_sources` gate (and any other owner-scoped
    policy inside the dispatcher) governs seat-layer dispatch exactly like it
    does for the Chat agent_loop (T-10-09). A dispatcher error is isolated and
    logged here — never raised out of the seat — mirroring the existing
    seat error-isolation posture in `run_seat`/`stream_seat` (T-10-11).

    Verify-by-execution boundary (D-03/D-07): a tool's real execution here IS
    the "runs before it trusts" verify step for that tool call — this does
    NOT add or claim static SQL verification (SEC-08/Phase 11).
    """
    owned = getattr(seat, "tools", [])
    if not owned:
        return
    for block in parse_tool_blocks(content):
        if block.tool_type not in owned:
            continue
        try:
            await execute_tool_block(block, owner=owner)
        except Exception:
            logger.warning(
                "seat %s: tool dispatch failed for %s", seat.id, block.tool_type,
                exc_info=True,
            )


def _resolve(seat: Seat, owner: str | None):
    """(chat_url, model, headers) or None if the seat's model can't be resolved."""
    from src.endpoint_resolver import resolve_endpoint_by_id
    if not seat.endpoint_id:
        return None
    return resolve_endpoint_by_id(seat.endpoint_id, seat.model or None, owner=owner)


async def run_seat(seat: Seat, prompt: str, upstream: list[SeatResult],
                   owner: str | None = None, registry=None,
                   reasoning_effort: str | None = None) -> SeatResult:
    """Run a seat to completion and return its full answer (non-streaming)."""
    from src.llm_core import llm_call_async
    resolved = _resolve(seat, owner)
    if not resolved:
        return SeatResult(seat_id=seat.id, name=seat.name, role=seat.role, ok=False,
                          error="no model configured for this seat")
    url, model, headers = resolved
    messages = await build_messages_with_brain(seat, prompt, upstream, registry, owner)
    try:
        content = await llm_call_async(
            url, model, messages,
            temperature=seat.temperature, headers=headers,
            owner=owner, reasoning_effort=reasoning_effort,
        )
        await _dispatch_owned_tools(seat, content or "", owner)
        return SeatResult(seat_id=seat.id, name=seat.name, role=seat.role,
                          ok=True, content=content or "")
    except Exception as e:  # a bad/slow seat must never kill the run
        return SeatResult(seat_id=seat.id, name=seat.name, role=seat.role, ok=False,
                          error=f"{type(e).__name__}: {e}")


async def stream_seat(seat: Seat, prompt: str, upstream: list[SeatResult],
                      owner: str | None = None,
                      registry=None,
                      reasoning_effort: str | None = None) -> AsyncIterator[str]:
    """Stream a seat's answer token-by-token. Yields plain text deltas.

    Wraps `llm_core.stream_llm`, parsing its SSE chunk protocol and yielding
    only the visible (non-thinking) text so the caller can re-emit it under the
    council's own event protocol.
    """
    from src.llm_core import stream_llm
    resolved = _resolve(seat, owner)
    if not resolved:
        # Surface as an error rather than streaming nothing — otherwise an
        # unconfigured synthesizer silently produces an empty "done" answer.
        raise RuntimeError("no model configured for this seat")
    url, model, headers = resolved
    messages = await build_messages_with_brain(seat, prompt, upstream, registry, owner)
    buf: list[str] = []
    async for chunk in stream_llm(
        url, model, messages,
        temperature=seat.temperature, headers=headers,
        reasoning_effort=reasoning_effort,
    ):
        for block in chunk.split("\n\n"):
            block = block.strip()
            if not block:
                continue
            # stream_llm signals failures with `event: error` followed by a
            # data line. Swallowing it would leave the synthesizer "done" with
            # an empty answer, so raise to mark the seat errored.
            if block.startswith("event: error") or block.startswith("event:error"):
                msg = ""
                for ln in block.split("\n"):
                    ln = ln.strip()
                    if ln.startswith("data:"):
                        try:
                            err = json.loads(ln[len("data:"):].strip())
                            msg = err.get("error") or err.get("text") or ""
                        except json.JSONDecodeError:
                            pass
                raise RuntimeError(msg or "model stream error")
            if not block.startswith("data:"):
                continue
            data = block[len("data:"):].strip()
            if data == "[DONE]" or not data:
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("thinking"):
                continue
            delta = obj.get("delta")
            if delta:
                buf.append(delta)
                yield delta
    await _dispatch_owned_tools(seat, "".join(buf), owner)
