# src/council/orchestrator.py
"""Run a council over one prompt, streaming progress as event dicts.

Condensed from llm-council's `council/dag.py` + `orchestrator.py`, minus the
code-review specifics (triage, static gate, grounding, refinement). Pure
orchestration: derive layers from the seat graph, run each layer's seats in
parallel under a per-seat timeout (a slow/broken seat fails gracefully and never
kills the run), feed upstream answers downstream, and stream the synthesizer's
final answer token-by-token.

`run_council` is an async generator of plain dict events; the route turns those
into SSE frames. Keeping it transport-agnostic mirrors llm-council's design.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from src.council.schemas import CouncilConfig, Seat, SeatResult
from src.council.seat_runner import run_seat, stream_seat, _build_messages
from src.council import seat_runner as _seat_runner
from src import verified_gate as _vg
from src.council import fresh_context as _fresh
from src.llm_core import normalize_model_id
from core.constants import ESCALATE_MODEL_MAX_LEN, ESCALATION_RESOLVE_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

SEAT_TIMEOUT_S = 180.0
# GRAY-AGT-06: synthesizer stream_seat wall-clock — mirrors the batch-seat
# SEAT_TIMEOUT_S budget above, but for the STREAMING synthesizer path (both
# the initial-synthesis and verify-repair stream_seat consumption sites). A
# single deadline is computed once per drain and the shrinking remainder is
# passed to each asyncio.wait_for call — never a fixed per-chunk timeout,
# which would never trip on a slow-but-steady trickle stream (29-RESEARCH.md
# Pitfall 5).
SYNTH_TIMEOUT_S = 180.0
# GRAY-AGT-06: overall council-run wall-clock budget, checked at each layer
# boundary, so a wedged seat/synth cannot pin the whole run indefinitely.
# Comfortably >= SEAT_TIMEOUT_S * 2 so a normal multi-layer council never
# spuriously breaches it (29-RESEARCH.md Open Question #1).
COUNCIL_RUN_TIMEOUT_S = 600.0
MAX_CONCURRENCY = 4
# Mirrors src/answer_pipeline.py's history sub-cap divisor so context_window
# stays the single tuning knob (D-12) — used to bound the verification
# repair-prompt `detail` field below.
_HISTORY_CAP_DIVISOR = 4

# Gap closure 08-05 (IN-01/WR-02): fixed allowance for the context_fit budget
# covering the deterministic template text `_build_messages` always adds —
# the longest `_ROLE_FRAMING` string (synthesizer, 273 chars measured) plus
# "User prompt:\n" (13) plus the "Here are the contributions..." header (58)
# plus join-separator margin. A small, fixed constant -- NOT inflated to make
# the signal vacuous (mirrors answer_pipeline.py's _STATIC_OVERHEAD).
_FRAMING_STATIC = 350
# Fixed per-item label overhead: "### {label}\n" (4 + label + 1) plus the
# "\n\n" block-join separator (2) -- the label TEXT length itself is measured
# per-call (below) the same way answer_pipeline.py measures max_source_len,
# so this constant covers only the surrounding punctuation, not the label.
_LABEL_OVERHEAD_FIXED = 7


def _seat_meta(s: Seat) -> dict:
    return {"id": s.id, "name": s.name or s.id, "role": s.role, "color": s.color}


def _seat_runtime_kwargs(registry, reasoning_effort: str | None) -> dict:
    """Build seat-call kwargs while preserving the legacy ``None`` call shape.

    Several integrations patch the seat boundary with its original signature.
    Omitting ``reasoning_effort`` when no value was requested keeps those
    callers byte-for-byte compatible while explicit per-request choices flow
    through every model call.
    """
    kwargs = {"registry": registry}
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


def _upstream_for(seat: Seat, results: dict[str, SeatResult]) -> list[SeatResult]:
    return [results[c] for c in seat.consumes if c in results]


def _curate_upstream(seat: Seat, results: dict[str, SeatResult], win: int | None
                     ) -> "tuple[list[SeatResult] | _fresh.CuratedList, dict | None]":
    """Select a seat's upstream — curated to `win` chars via `curate_for` when
    `win` is truthy, or the raw `_upstream_for` assembly otherwise — and build
    the `context_curated` event when anything was dropped.

    Shared by both the batch-seat loop and the synthesizer loop in
    `run_council` so a future change to curation/event semantics can't drift
    between the two copies (WR-01). Not a generator itself (mixing event/value
    yields in one generator is confusing): returns `(upstream, event_or_None)`
    and each call site does `if event: yield event`.
    """
    upstream = _fresh.curate_for(seat, results, win) if win else _upstream_for(seat, results)
    dropped_count = getattr(upstream, "dropped_count", 0)
    event = None
    if dropped_count > 0:
        dropped_ids = list(getattr(upstream, "dropped_ids", []))
        logger.warning(
            "context_curated: seat=%s dropped=%d dropped_ids=%s window=%s",
            seat.id, dropped_count, dropped_ids, win,
        )
        event = {"type": "context_curated", "seat_id": seat.id,
                 "dropped": dropped_count, "dropped_ids": dropped_ids, "window": win}
    return upstream, event


def _context_fit_event(seat: Seat, prompt: str, upstream: "list[SeatResult] | _fresh.CuratedList",
                       win: int) -> dict:
    """Shared `context_fit` measurement for BOTH emit sites (mirrors
    `_curate_upstream` being shared so the two loops can't drift, WR-01/IN-01).

    Measures the ACTUAL assembled `_build_messages(seat, prompt, upstream)`
    call -- system persona+framing + user prompt + labeled upstream -- against
    a budget structurally consistent with the Answer path's (src/answer_
    pipeline.py's TARGET): the curated-upstream ceiling (`win`, matching
    curate_for's own content-sum guarantee) plus a BOUNDED allowance for the
    caller-controlled `prompt` (capped at `win`, not its raw unbounded length,
    so a pathological prompt still overflows -- WR-02) plus the seat's own
    persona text plus a per-item label allowance (the deterministic "###
    {label}\n" header + join separator each upstream contribution costs,
    bounded because curate()/curate_for() already cap live item count) plus a
    fixed allowance for the deterministic framing/label template text.

    The optional best-effort brain-note block (`build_messages_with_brain`,
    top_k-bounded) is intentionally EXCLUDED from the measured size/budget --
    it is a separately-bounded, registry-dependent, non-deterministic
    addition; measuring the deterministic `_build_messages` core keeps this
    signal deterministic/no-LLM, consistent with curation excluding it too.
    Publishes `budget` as the EXACT value `fits` is compared against, so
    `fits == (size <= budget)` for any consumer that recomputes it (WR-02).
    """
    messages = _build_messages(seat, prompt, upstream)
    size = sum(len((m or {}).get("content", "") or "") for m in messages)
    ok_items = [r for r in upstream if getattr(r, "ok", False) and (getattr(r, "content", "") or "").strip()]
    n_items = len(ok_items)
    max_label_len = max((len(r.name or r.seat_id or "") for r in ok_items), default=0)
    persona_len = len((seat.persona or "").strip())
    prompt_allowance = min(len(prompt or ""), win)
    label_overhead = n_items * (max_label_len + _LABEL_OVERHEAD_FIXED)
    budget = win + prompt_allowance + persona_len + label_overhead + _FRAMING_STATIC
    return {"type": "context_fit", "seat_id": seat.id,
            "fits": size <= budget, "size": size, "budget": budget, "window": win}


async def run_council(prompt: str, cfg: CouncilConfig,
                      owner: str | None = None,
                      registry=None,
                      verify: bool = True,
                      verify_execute: bool = False,
                      verify_repair: bool = True,
                      fresh_context: bool = True,
                      context_window: int = _fresh.DEFAULT_WINDOW,
                      escalate_model: str | None = None,
                      reasoning_effort: str | None = None) -> AsyncIterator[dict]:
    """Yield event dicts as the council deliberates. See module docstring.

    Verification gate (anti-hallucination): once the synthesizer produces the
    final answer, any code in it is checked (`verify_answer`). `verify` toggles
    the gate (syntax always; `verify_execute` also RUNS the code in a sandbox).
    On failure, `verify_repair` gives the synthesizer ONE more round with the
    real error before emitting — and the `done` event carries a `verified` flag
    so callers never present unverified code as if it were correct.

    Fresh-context (GSD-ported): with `fresh_context=True` (the default, Phase 8
    D-01), each seat's upstream context is curated/bounded to `context_window`
    chars before the call, instead of dumping every upstream answer in full —
    this fights context rot as the DAG deepens. Callers may still pass
    `fresh_context=False` to opt out and reach the raw, uncurated legacy path
    (D-09/D-10). A falsy `context_window` (e.g. 0) disables curation even with
    the flag on — mirrors the old `_build_messages`-level `if window:`
    truthiness gate, so `fresh_context=True, context_window=0` falls through to
    the raw upstream assembly instead of curating everything away.

    Escalation (RMEAS-02): `escalate_model` is a per-request, opt-in weak->
    strong synthesizer-model swap on the EXISTING verify-gate repair round —
    it never adds a round. When falsy (the default `None`, or `""`), this is
    completely inert: `normalize_model_id` is never called and no escalation
    events are emitted (byte-for-byte parity with today's repair path, the
    same opt-out contract `fresh_context` uses). When `needs_repair` fires
    and `escalate_model` resolves against the synthesizer endpoint's own
    model list (GRAY-AGT-04 validate-before-swap), the already-happening
    repair call runs on the resolved model instead and an `escalation_fired`
    event is emitted; an unresolvable target fails safe (no swap, an
    `escalation_failed` event, a logged warning) — an arbitrary caller-
    supplied model string can never be dispatched.

    Reasoning effort is a per-request compute budget. When supplied, the same
    value is forwarded to every panelist/aggregator, the streaming synthesizer,
    and the verification repair call. ``None`` preserves the endpoint default.
    """
    win = int(context_window) if fresh_context and context_window else None
    # GRAY-AGT-07: a cyclic or missing-dependency DAG must surface as the same
    # {"type": "error", ...} shape the empty-seats path below already uses —
    # not raise out of the generator. This is the ONLY caller-side change; the
    # detection logic in CouncilConfig.layers() itself is unchanged (correct
    # and already tested).
    try:
        layers = cfg.layers()
    except ValueError as e:
        yield {"type": "error", "error": str(e)}
        return
    if not layers:
        yield {"type": "error", "error": "council has no enabled seats"}
        return

    yield {"type": "run_started", "council": cfg.name,
           "seats": [_seat_meta(s) for layer in layers for s in layer]}

    results: dict[str, SeatResult] = {}
    final_content = ""
    last_synth: Seat | None = None          # for the verification repair round
    last_synth_upstream: list[SeatResult] = []
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    # GRAY-AGT-06: overall run wall-clock, computed once; checked at each
    # layer boundary below. On breach we `break` (not an early `return`) so
    # the existing fallback/verify/done code still runs and a time-boxed run
    # still emits an honest partial `done` event.
    _run_deadline = asyncio.get_running_loop().time() + COUNCIL_RUN_TIMEOUT_S

    for layer in layers:
        if asyncio.get_running_loop().time() >= _run_deadline:
            yield {"type": "error",
                   "error": f"council run exceeded wall-clock budget of {COUNCIL_RUN_TIMEOUT_S}s"}
            break
        stream_seats = [s for s in layer if s.role == "synthesizer"]
        batch_seats = [s for s in layer if s.role != "synthesizer"]

        # ── non-streaming seats: run concurrently, each guarded by a timeout ──
        async def _run_one(seat: Seat, upstream: list[SeatResult]) -> SeatResult:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        run_seat(
                            seat, prompt, upstream, owner,
                            **_seat_runtime_kwargs(registry, reasoning_effort),
                        ),
                        timeout=SEAT_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    return SeatResult(seat_id=seat.id, name=seat.name, role=seat.role,
                                      ok=False, error="timeout")

        # Holds a plain list[SeatResult] on the flag-off path, or a
        # _fresh.CuratedList (list subclass carrying .dropped_count/.dropped_ids)
        # when fresh_context curation is active.
        curated_by_seat: dict[str, "list[SeatResult] | _fresh.CuratedList"] = {}
        for s in batch_seats:
            upstream, event = _curate_upstream(s, results, win)
            curated_by_seat[s.id] = upstream
            if event:
                yield event
            if win:
                yield _context_fit_event(s, prompt, upstream, win)
            yield {"type": "seat_status", "seat_id": s.id, "status": "running"}
        if batch_seats:
            done = await asyncio.gather(*(_run_one(s, curated_by_seat[s.id]) for s in batch_seats))
            for res in done:
                results[res.seat_id] = res
                if res.ok:
                    yield {"type": "seat_result", "seat_id": res.seat_id,
                           "content": res.content}
                    yield {"type": "seat_status", "seat_id": res.seat_id, "status": "done"}
                else:
                    yield {"type": "seat_status", "seat_id": res.seat_id,
                           "status": "error", "error": res.error}

        # ── synthesizer seats: stream their answer live ──
        for s in stream_seats:
            upstream, event = _curate_upstream(s, results, win)
            if event:
                yield event
            if win:
                yield _context_fit_event(s, prompt, upstream, win)
            yield {"type": "seat_status", "seat_id": s.id, "status": "running"}
            yield {"type": "synthesis_start", "seat_id": s.id, "name": s.name or s.id}
            buf: list[str] = []
            # GRAY-AGT-06: deadline-bounded drain — mirrors agent_loop.py's
            # TOOL_WALL_CLOCK_TIMEOUT_SECONDS idiom (a single deadline computed
            # once, the shrinking remainder passed to each wait_for; never a
            # fixed per-chunk timeout — Pitfall 5).
            gen = stream_seat(
                s, prompt, upstream, owner,
                **_seat_runtime_kwargs(registry, reasoning_effort),
            )
            _loop = asyncio.get_running_loop()
            _deadline = _loop.time() + SYNTH_TIMEOUT_S
            _synth_ok = False
            try:
                while True:
                    _remaining = _deadline - _loop.time()
                    if _remaining <= 0:
                        raise asyncio.TimeoutError
                    delta = await asyncio.wait_for(gen.__anext__(), timeout=_remaining)
                    buf.append(delta)
                    yield {"type": "synthesis_delta", "seat_id": s.id, "delta": delta}
            except StopAsyncIteration:
                _synth_ok = True
            except asyncio.TimeoutError:
                yield {"type": "seat_status", "seat_id": s.id, "status": "error",
                       "error": "timeout"}
                results[s.id] = SeatResult(seat_id=s.id, name=s.name, role=s.role,
                                           ok=False, error="timeout")
            except Exception as e:
                yield {"type": "seat_status", "seat_id": s.id, "status": "error",
                       "error": f"{type(e).__name__}: {e}"}
                results[s.id] = SeatResult(seat_id=s.id, name=s.name, role=s.role,
                                           ok=False, error=str(e))
            if not _synth_ok:
                continue
            content = "".join(buf)
            results[s.id] = SeatResult(seat_id=s.id, name=s.name, role=s.role,
                                       ok=True, content=content)
            final_content = content or final_content
            last_synth, last_synth_upstream = s, upstream
            yield {"type": "seat_result", "seat_id": s.id, "content": content}
            yield {"type": "seat_status", "seat_id": s.id, "status": "done"}

    # Fall back to the last successful seat if there was no synthesizer.
    if not final_content:
        for res in reversed(list(results.values())):
            if res.ok and res.content.strip():
                final_content = res.content
                break

    # ── Verification gate: only emit code that clears (syntax, and optionally a
    # sandbox run). This is the anti-hallucination step — the whole point of the
    # council pipeline. On failure, give the synthesizer ONE grounded repair round.
    verdict = {"passed": True, "has_code": False}
    if verify and final_content.strip():
        verdict, needs_repair, why = _vg.evaluate(final_content, execute=verify_execute)
        yield {"type": "verification", "stage": "initial", **verdict}
        if needs_repair and verify_repair and last_synth is not None:
            # D-12: bound the sandbox/syntax-check detail before it re-enters
            # a model call — a pathologically long or injection-shaped error
            # string must not be replayed at full length (also defense-in-depth
            # against the detail field being an untrusted, code-derived string).
            repair_detail = verdict.get('detail', '')
            if win:
                repair_detail = _fresh.distill(repair_detail, win // _HISTORY_CAP_DIVISOR)
            repair_prompt = (
                prompt
                + "\n\n[VERIFICATION FAILED] The code in your previous answer "
                + why + f": {repair_detail}\n"
                + "Return a corrected answer whose code is valid"
                + (" and runs cleanly." if verify_execute else ".")
            )
            # RMEAS-02: per-request weak->strong swap of THIS repair call's
            # model only — rides the single existing repair round (no new
            # round, no retry loop; T-40-02). Inert by construction when
            # escalate_model is falsy: normalize_model_id is never reached
            # (T-40-04 flag-off parity).
            repair_seat = last_synth
            escalate_target = (escalate_model or "").strip()
            # 40-REVIEW IN-01: bound the untrusted, per-request escalate_model
            # string before it can reach normalize_model_id. An implausibly
            # long value is treated as no-escalation (fail-safe), never a hard
            # rejection.
            if len(escalate_target) > ESCALATE_MODEL_MAX_LEN:
                escalate_target = ""
            if escalate_target and escalate_target != last_synth.model:
                resolved = None
                # 40-REVIEW WR-04: fail safe -- the resolve/normalize sequence
                # must never raise out of this async generator. Any exception
                # here is treated exactly like "did not resolve": keep
                # repair_seat on the existing (un-escalated) model, emit
                # escalation_failed, log a warning, and let the repair round
                # proceed normally.
                try:
                    # 40-REVIEW WR-01: _resolve opens a synchronous SQLAlchemy
                    # session inline; thread-wrap it the same way
                    # normalize_model_id is thread-wrapped below so it never
                    # blocks the event loop.
                    synth_endpoint = await asyncio.to_thread(_seat_runner._resolve, last_synth, owner)
                    if synth_endpoint:
                        synth_url = synth_endpoint[0]
                        # GRAY-AGT-04 validate-before-swap: the untrusted,
                        # per-request escalate_target is only ever dispatched if
                        # it resolves against the synthesizer endpoint's own
                        # (DB-cached-first) available-model list (T-40-01).
                        resolved = await asyncio.to_thread(
                            normalize_model_id, synth_url, escalate_target,
                            ESCALATION_RESOLVE_TIMEOUT_SECONDS,
                        )
                except Exception as e:
                    logger.warning(
                        "council escalation: resolve/normalize raised for "
                        "seat %s target %r (%s: %s); staying on %r",
                        last_synth.id, escalate_target, type(e).__name__, e,
                        last_synth.model,
                    )
                    yield {"type": "escalation_failed", "seat_id": last_synth.id,
                           "target": escalate_target,
                           "reason": "escalation target did not resolve"}
                else:
                    if resolved:
                        repair_seat = last_synth.model_copy(update={"model": resolved})
                        yield {"type": "escalation_fired", "seat_id": last_synth.id,
                               "from": last_synth.model, "to": resolved,
                               "reason": why}
                    else:
                        logger.warning(
                            "council escalation: target %r did not resolve for "
                            "seat %s; staying on %r",
                            escalate_target, last_synth.id, last_synth.model,
                        )
                        yield {"type": "escalation_failed", "seat_id": last_synth.id,
                               "target": escalate_target,
                               "reason": "escalation target did not resolve"}
            yield {"type": "seat_status", "seat_id": last_synth.id, "status": "running"}
            yield {"type": "synthesis_start", "seat_id": last_synth.id,
                   "name": (last_synth.name or last_synth.id) + " (repair)"}
            buf2: list[str] = []
            # GRAY-AGT-06: same deadline-bounded drain idiom as the initial
            # synthesis site above.
            gen2 = stream_seat(
                repair_seat, repair_prompt, last_synth_upstream, owner,
                **_seat_runtime_kwargs(registry, reasoning_effort),
            )
            _loop = asyncio.get_running_loop()
            _deadline2 = _loop.time() + SYNTH_TIMEOUT_S
            _repair_ok = False
            try:
                while True:
                    _remaining = _deadline2 - _loop.time()
                    if _remaining <= 0:
                        raise asyncio.TimeoutError
                    delta = await asyncio.wait_for(gen2.__anext__(), timeout=_remaining)
                    buf2.append(delta)
                    yield {"type": "synthesis_delta", "seat_id": last_synth.id, "delta": delta}
            except StopAsyncIteration:
                _repair_ok = True
            except asyncio.TimeoutError:
                yield {"type": "seat_status", "seat_id": last_synth.id,
                       "status": "error", "error": "timeout"}
            except Exception as e:
                yield {"type": "seat_status", "seat_id": last_synth.id,
                       "status": "error", "error": f"{type(e).__name__}: {e}"}
            if _repair_ok:
                fixed = "".join(buf2)
                v2, _, _ = _vg.evaluate(fixed, execute=verify_execute)
                yield {"type": "verification", "stage": "repair", **v2}
                accepted, ftext, fverdict = _vg.resolve(verdict, fixed, v2)
                if accepted:
                    final_content, verdict = ftext, fverdict
                yield {"type": "seat_status", "seat_id": last_synth.id, "status": "done"}

    # SEC-08 reporting honesty (Plan 11-04): read the HONEST `verified` key
    # here, not `passed` -- `passed` is now non-blocking (true for both
    # genuinely-passed AND unverified code, e.g. a lone ```sql/```js block),
    # so a user/API-facing "verified" claim sourced from `passed` would
    # silently reintroduce the exact false-assurance bug SEC-08 exists to
    # fix, one layer up. Scope note (D-06): Chat's separate entry point
    # (src/agent_loop.py) has its OWN independent gate-entry trigger
    # ("```py" in cleaned_round or a create_document block) that this
    # widened verdict does not reach -- Chat is deliberately out of scope
    # for SEC-08 this phase; its trigger is asserted unchanged by
    # tests/test_verify_reporting_honesty.py.
    yield {"type": "done", "final": final_content,
           "verified": bool(verdict.get("verified")), "verification": verdict}
