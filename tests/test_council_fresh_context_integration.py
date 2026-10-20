"""Integration/parity harness for run_council's fresh-context wiring (Phase 2 Plan 2).

Drives `run_council` through its real orchestrator -> seat_runner -> assembled-messages
path, stubbing ONLY the model boundary (`src.llm_core.llm_call_async` / `stream_llm`,
the seam `run_seat`/`stream_seat` late-import at call time) and the DB resolver
(`src.council.seat_runner._resolve`, so no real `SessionLocal()` is ever opened) —
RESEARCH Patterns 3/4/5, CONTEXT D-04/D-05.

Proves (per the plan's must_haves):
  - flag-off assembled synth messages == the raw `_upstream_for` assembly, byte-for-byte
    (SPINE-04)
  - flag-on assembled synth messages == the `curate_for`-curated assembly, and differ
    from the raw dump when upstream exceeds the window (SPINE-04)
  - `context_curated` fires with {type, seat_id, dropped, dropped_ids, window} only when
    dropped_count > 0, and is silent when nothing is dropped or the flag is off (D-06)
  - the curated synthesizer message fits the window, upstream-only scope (D-08)
  - the verification repair round reuses the SAME stored curated upstream, with no
    re-curate and no re-emit of context_curated (D-09)
"""
from __future__ import annotations

import json

from src.council import fresh_context as fc
from src.council import seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat, SeatResult

PROMPT = "What should we do?"


def _two_layer_cfg(persona: str = "") -> CouncilConfig:
    """Minimal 2-layer council: one panelist consumed by one synthesizer."""
    return CouncilConfig(seats=[
        Seat(id="p1", role="panelist", persona=persona, consumes=[]),
        Seat(id="chief", role="synthesizer", persona=persona, consumes=["p1"]),
    ])


def _fan_in_cfg(n: int, persona: str = "") -> CouncilConfig:
    """A synthesizer consuming `n` independent panelists — used to force a drop
    (Task 2/3) when `n` exceeds the curated window's max_items."""
    panelists = [Seat(id=f"p{i}", role="panelist", persona=persona, consumes=[])
                 for i in range(n)]
    chief = Seat(id="chief", role="synthesizer", persona=persona,
                 consumes=[p.id for p in panelists])
    return CouncilConfig(seats=panelists + [chief])


def _aggregator_cfg(n: int, persona: str = "") -> CouncilConfig:
    """3-layer council: `n` panelists -> an aggregator (batch_seats, role !=
    "synthesizer", non-empty consumes) -> a synthesizer. WR-03: exercises the
    batch-loop's curate_for/context_curated branch with real upstream content
    — every other fixture in this file gives panelists (the only prior
    batch_seats member) `consumes=[]`, so this is the first fixture to prove
    the batch-loop copy of _curate_upstream, not just the synthesizer's."""
    panelists = [Seat(id=f"p{i}", role="panelist", persona=persona, consumes=[])
                 for i in range(n)]
    agg = Seat(id="agg", role="aggregator", persona=persona,
               consumes=[p.id for p in panelists])
    chief = Seat(id="chief", role="synthesizer", persona=persona, consumes=["agg"])
    return CouncilConfig(seats=panelists + [agg, chief])


def _make_stubs(monkeypatch, scripted: dict[str, list[str]] | None = None,
                default: str = "canned answer, short and simple."):
    """Patch the model seam (`_resolve` + `llm_call_async` + `stream_llm`).

    `scripted` maps model (== seat.id, since the fake resolver keys model by
    seat id) to a list of canned answers consumed IN ORDER across successive
    calls to that model — this is what lets Task 3's repair-round test script
    two different synthesizer answers. Once a model's list is exhausted (or it
    was never scripted), `default` is used, so a no-op patch never silently
    hits the real network — every call is captured.

    Returns `captured`: model -> list of the exact `messages` arg each call
    received, in call order.
    """
    captured: dict[str, list[list[dict]]] = {}
    scripted = {k: list(v) for k, v in (scripted or {}).items()}

    def _next(model: str) -> str:
        queue = scripted.get(model)
        if queue:
            return queue.pop(0)
        return default

    def fake_resolve(seat, owner):
        return (f"http://fake/{seat.id}", seat.id, {})

    async def fake_llm_call_async(url, model, messages, temperature=1.0, headers=None, **kw):
        captured.setdefault(model, []).append(messages)
        return _next(model)

    async def fake_stream_llm(url, model, messages, temperature=1.0, headers=None, **kw):
        captured.setdefault(model, []).append(messages)
        content = _next(model)
        yield f"data: {json.dumps({'delta': content})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(seat_runner, "_resolve", fake_resolve)
    import src.llm_core as llm_core_mod
    monkeypatch.setattr(llm_core_mod, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr(llm_core_mod, "stream_llm", fake_stream_llm)
    return captured


async def _run(cfg: CouncilConfig, prompt: str = PROMPT, **kw) -> list[dict]:
    """Drain run_council's async generator into a plain list of events."""
    events = []
    async for ev in run_council(prompt, cfg, **kw):
        events.append(ev)
    return events


def _user_content(messages: list[dict]) -> str:
    return next(m["content"] for m in messages if m["role"] == "user")


class TestFlagParity:
    """Task 1: flag-off byte-for-byte parity vs. flag-on curate_for parity (SPINE-04)."""

    async def test_flag_off_matches_raw_upstream_assembly(self, monkeypatch):
        cfg = _two_layer_cfg()
        p1_answer = "p1's raw answer, short and simple."
        captured = _make_stubs(
            monkeypatch,
            scripted={"p1": [p1_answer], "chief": ["final synth answer"]},
        )

        events = await _run(cfg, fresh_context=False)

        # The stub was actually invoked (fail loud on a no-op patch).
        assert captured
        assert "chief" in captured
        chief_messages = captured["chief"][0]

        p1_result = SeatResult(seat_id="p1", name="", role="panelist",
                               ok=True, content=p1_answer)
        expected = seat_runner._build_messages(cfg.seat("chief"), PROMPT, [p1_result])
        assert chief_messages == expected

        done = next(e for e in events if e["type"] == "done")
        assert done["final"].strip() != ""

    async def test_flag_on_matches_curate_for_assembly(self, monkeypatch):
        cfg = _two_layer_cfg()
        window = 800
        p1_answer = "X" * 5000   # well over the window so distill() actually trims
        captured = _make_stubs(
            monkeypatch,
            scripted={"p1": [p1_answer], "chief": ["final synth answer"]},
        )

        await _run(cfg, fresh_context=True, context_window=window)

        chief_messages = captured["chief"][0]

        p1_result = SeatResult(seat_id="p1", name="", role="panelist",
                               ok=True, content=p1_answer)
        results = {"p1": p1_result}
        curated = fc.curate_for(cfg.seat("chief"), results, window)
        expected = seat_runner._build_messages(cfg.seat("chief"), PROMPT, list(curated))
        assert chief_messages == expected

        # Differs from the raw (flag-off) assembly: full content absent, elision
        # marker present — proves flag-on is the curated assembly, not the raw dump.
        raw_expected = seat_runner._build_messages(cfg.seat("chief"), PROMPT, [p1_result])
        assert chief_messages != raw_expected
        user_body = _user_content(chief_messages)
        assert p1_answer not in user_body
        assert "context trimmed for a fresh window" in user_body

    async def test_flag_on_matches_curate_for_assembly_for_batch_aggregator_seat(self, monkeypatch):
        # WR-03: the same parity proof as above, but for a batch_seats member
        # (role="aggregator", non-empty consumes) — the structurally-duplicated
        # (pre-WR-01) / shared (post-WR-01) curation branch that every other
        # fixture in this file only ever exercises with consumes=[].
        cfg = _aggregator_cfg(1)
        window = 800
        p0_answer = "X" * 5000   # well over the window so distill() actually trims
        captured = _make_stubs(
            monkeypatch,
            scripted={"p0": [p0_answer], "agg": ["agg answer"],
                     "chief": ["final synth answer"]},
        )

        await _run(cfg, fresh_context=True, context_window=window)

        agg_messages = captured["agg"][0]

        p0_result = SeatResult(seat_id="p0", name="", role="panelist",
                               ok=True, content=p0_answer)
        results = {"p0": p0_result}
        curated = fc.curate_for(cfg.seat("agg"), results, window)
        expected = seat_runner._build_messages(cfg.seat("agg"), PROMPT, list(curated))
        assert agg_messages == expected

        raw_expected = seat_runner._build_messages(cfg.seat("agg"), PROMPT, [p0_result])
        assert agg_messages != raw_expected
        user_body = _user_content(agg_messages)
        assert p0_answer not in user_body
        assert "context trimmed for a fresh window" in user_body


class TestDropSignalAndFit:
    """Task 2: context_curated fires only on drops (D-06), and the curated
    synthesizer message fits its window, upstream-only scope (D-08)."""

    async def test_context_curated_fires_only_on_drop(self, monkeypatch):
        # Mirrors test_fresh_context.py's 50-items/1000-window drop fixture ratio:
        # max_items = window // _MIN_PER_ITEM (1000 // 240 == 4), so N=10 panelists
        # guarantees an overflow drop for the synthesizer's curate_for call.
        n = 10
        window = 1000
        cfg = _fan_in_cfg(n)
        _make_stubs(monkeypatch, default="panelist answer, moderately sized content.")

        events = await _run(cfg, fresh_context=True, context_window=window)

        curated_events = [e for e in events if e["type"] == "context_curated"]
        assert len(curated_events) == 1
        ev = curated_events[0]
        assert ev["seat_id"] == "chief"
        assert isinstance(ev["dropped"], int) and ev["dropped"] > 0
        assert isinstance(ev["dropped_ids"], list) and len(ev["dropped_ids"]) == ev["dropped"]
        assert ev["window"] == window

    async def test_context_curated_fires_for_batch_aggregator_seat(self, monkeypatch):
        # WR-03: proves the drop signal on a batch_seats member (role !=
        # "synthesizer"), not just the synthesizer — the branch this event
        # fires from is now shared (_curate_upstream, WR-01) but was
        # previously a separate, untested copy in the batch loop.
        n = 10
        window = 1000
        cfg = _aggregator_cfg(n)
        _make_stubs(monkeypatch, default="panelist answer, moderately sized content.")

        events = await _run(cfg, fresh_context=True, context_window=window)

        curated_events = [e for e in events
                          if e["type"] == "context_curated" and e["seat_id"] == "agg"]
        assert len(curated_events) == 1
        ev = curated_events[0]
        assert isinstance(ev["dropped"], int) and ev["dropped"] > 0
        assert isinstance(ev["dropped_ids"], list) and len(ev["dropped_ids"]) == ev["dropped"]
        assert ev["window"] == window

    async def test_no_context_curated_when_nothing_dropped(self, monkeypatch):
        cfg = _two_layer_cfg()
        _make_stubs(monkeypatch, default="short")

        # Generous default window (4000), tiny panelist content -> nothing dropped.
        events_on = await _run(cfg, fresh_context=True)
        assert not any(e["type"] == "context_curated" for e in events_on)

        # Flag off never curates at all, so the plain-list path must stay silent
        # too — guards the getattr(upstream, "dropped_count", 0) guard.
        events_off = await _run(cfg, fresh_context=False)
        assert not any(e["type"] == "context_curated" for e in events_off)

    async def test_zero_window_falls_back_to_raw_upstream(self, monkeypatch):
        # WR-02: a falsy context_window (e.g. 0) must disable curation even
        # with fresh_context=True — mirrors the pre-refactor `if window:`
        # truthiness gate. Without this fallback, curate_for(seat, results, 0)
        # would curate every live contribution away (max_items == 0).
        cfg = _two_layer_cfg()
        p1_answer = "p1's raw answer, short and simple."
        captured = _make_stubs(
            monkeypatch,
            scripted={"p1": [p1_answer], "chief": ["final synth answer"]},
        )

        events = await _run(cfg, fresh_context=True, context_window=0)

        chief_messages = captured["chief"][0]
        p1_result = SeatResult(seat_id="p1", name="", role="panelist",
                               ok=True, content=p1_answer)
        expected = seat_runner._build_messages(cfg.seat("chief"), PROMPT, [p1_result])
        assert chief_messages == expected

        assert not any(e["type"] == "context_curated" for e in events)

    async def test_curated_synth_message_fits_window(self, monkeypatch):
        # D-08 scopes the "fits window" guarantee to upstream contributions
        # only — whole-prompt budgeting (system persona + a future brain-note
        # block) is deferred to Phase 4/Phase 8 (FRESH-02). IN-01: fc.fits()
        # itself sums a WHOLE message list (system content included) when
        # given one, so asserting fc.fits(chief_messages, window) would only
        # incidentally prove the upstream-only guarantee (because persona is
        # empty here) rather than actually testing it. Isolate the
        # upstream-derived user-message block first so this assertion tests
        # exactly what D-08 promises, honestly.
        cfg = _two_layer_cfg(persona="")
        window = 4000
        p1_answer = "Y" * 1500
        captured = _make_stubs(
            monkeypatch,
            scripted={"p1": [p1_answer]},
            default="short synth answer",
        )

        await _run(cfg, fresh_context=True, context_window=window)

        chief_messages = captured["chief"][0]
        upstream_block = _upstream_block(_user_content(chief_messages))
        assert fc.fits(upstream_block, window)


class TestHardenedFitSignal:
    """Gap closure 08-05 (WR-01/WR-02/IN-01): the context_fit signal must
    measure the ACTUAL assembled `_build_messages` call (system persona+
    framing + user prompt + labeled upstream), publish an honest `budget` key
    that `fits` is compared against, and be able to report both True and
    False for real inputs. RED until Task 3 replaces the upstream-only
    measurement with the assembled-call measurement."""

    async def test_context_fit_measures_assembled_message_not_upstream_only(self, monkeypatch):
        cfg = _two_layer_cfg(persona="")
        window = 4000
        p1_answer = "Y" * 1500
        captured = _make_stubs(
            monkeypatch,
            scripted={"p1": [p1_answer]},
            default="short synth answer",
        )

        events = await _run(cfg, fresh_context=True, context_window=window)

        fit_events = [e for e in events if e["type"] == "context_fit"]
        chief_fit = next(e for e in fit_events if e["seat_id"] == "chief")
        assert isinstance(chief_fit["budget"], int), (
            f"context_fit event missing an int 'budget' key: {chief_fit}")

        chief_messages = captured["chief"][0]
        assembled_size = sum(len(m.get("content", "")) for m in chief_messages)
        assert chief_fit["size"] == assembled_size, (
            "size must reflect the assembled _build_messages call (system + "
            f"user), not the upstream-only concatenation: {chief_fit}")

        upstream_block = _upstream_block(_user_content(chief_messages))
        assert chief_fit["size"] > len(upstream_block), (
            "size must be strictly larger than the upstream-only block -- "
            "it must also cover persona/framing/'User prompt:' text"
        )

    async def test_context_fit_consistent_and_can_report_false(self, monkeypatch):
        cfg = _two_layer_cfg()
        window = 200  # small window
        huge_prompt = "P" * 8000  # far larger than the window -- forces overflow
        _make_stubs(monkeypatch, default="short answer, nothing special.")

        events = await _run(cfg, huge_prompt, fresh_context=True, context_window=window)

        fit_events = [e for e in events if e["type"] == "context_fit"]
        assert fit_events
        assert any(e["fits"] is False for e in fit_events), (
            "an over-budget assembled call (huge prompt at a small window) "
            f"should make at least one context_fit event report fits=False: {fit_events}")
        for e in fit_events:
            assert isinstance(e["budget"], int)
            assert e["fits"] == (e["size"] <= e["budget"]), (
                f"fits must equal size <= budget: {e}")

    async def test_context_fit_true_for_realistic_council_call(self, monkeypatch):
        cfg = _two_layer_cfg(persona="You are a helpful, concise council member.")
        window = 4000
        p1_answer = "A reasonably sized panelist answer with some detail in it."
        _make_stubs(
            monkeypatch,
            scripted={"p1": [p1_answer]},
            default="A reasonably sized synthesizer answer with some detail.",
        )

        events = await _run(cfg, fresh_context=True, context_window=window)

        fit_events = [e for e in events if e["type"] == "context_fit"]
        assert fit_events
        for e in fit_events:
            assert isinstance(e["budget"], int), (
                f"context_fit event missing an int 'budget' key: {e}")
            assert e["fits"] is True, f"realistic council call should fit: {e}"


def _upstream_block(user_content: str) -> str:
    """The labeled "contributions from earlier council members" tail of a
    synthesizer's user message — the part actually built from `upstream`,
    isolated from the leading prompt text (which differs between the initial
    call and the repair call's `[VERIFICATION FAILED]`-prefixed prompt)."""
    marker = "Here are the contributions from earlier council members:\n\n"
    assert marker in user_content
    return user_content.split(marker, 1)[1]


class TestRepairRoundReuse:
    """Task 3: the verification repair round reuses the stored curated
    synthesizer upstream verbatim — no re-curate, no re-emit (D-09)."""

    async def test_repair_round_reuses_curated_upstream(self, monkeypatch):
        # Reuse the drop-forcing fan-in fixture so there IS a curated (dropped)
        # synthesizer upstream worth proving is reused across the repair round.
        n = 10
        window = 1000
        cfg = _fan_in_cfg(n)
        bad_answer = "Here is my answer:\n```python\ndef broken(:\n    pass\n```"
        good_answer = "The corrected answer, now with no code at all."
        captured = _make_stubs(
            monkeypatch,
            scripted={"chief": [bad_answer, good_answer]},
            default="panelist answer, moderately sized content here.",
        )

        events = await _run(
            cfg, fresh_context=True, context_window=window,
            verify=True, verify_repair=True, verify_execute=False,
        )

        # Initial synth call + repair call, both to the same synthesizer model.
        assert len(captured["chief"]) == 2

        initial_upstream = _upstream_block(_user_content(captured["chief"][0]))
        repair_upstream = _upstream_block(_user_content(captured["chief"][1]))
        assert initial_upstream == repair_upstream

        # Only ONE context_curated event for the synthesizer across the whole
        # run — the repair round did NOT re-curate or re-emit.
        curated_events = [e for e in events
                          if e["type"] == "context_curated" and e["seat_id"] == "chief"]
        assert len(curated_events) == 1

        # Both verification stages ran, confirming the repair branch executed.
        verification_events = [e for e in events if e["type"] == "verification"]
        stages = {e["stage"] for e in verification_events}
        assert stages == {"initial", "repair"}
        initial_v = next(e for e in verification_events if e["stage"] == "initial")
        repair_v = next(e for e in verification_events if e["stage"] == "repair")
        assert initial_v["passed"] is False
        assert repair_v["passed"] is True

        done = next(e for e in events if e["type"] == "done")
        assert done["verified"] is True
        assert done["final"] == good_answer


class TestSharedCoreUsage:
    """Task 2 (UNIFY-03): proves `run_council`'s repair round genuinely
    INVOKES `src.verified_gate.evaluate`, not merely imports it. The
    orchestrator reaches the shared core via a qualified module reference
    (`from src import verified_gate as _vg`, then `_vg.evaluate(...)` at call
    time -- see orchestrator.py's fix commit) so patching the attribute on
    the `verified_gate` module object is the same object the orchestrator's
    call site reads from at call time, not a stale name-bound copy."""

    async def test_council_repair_uses_shared_core(self, monkeypatch):
        # Same forced-repair fixture shape as TestRepairRoundReuse: a
        # bad-then-good scripted synthesizer answer forces exactly one
        # repair round (initial evaluate() + repair-recheck evaluate()).
        n = 10
        window = 1000
        cfg = _fan_in_cfg(n)
        bad_answer = "Here is my answer:\n```python\ndef broken(:\n    pass\n```"
        good_answer = "The corrected answer, now with no code at all."
        _make_stubs(
            monkeypatch,
            scripted={"chief": [bad_answer, good_answer]},
            default="panelist answer, moderately sized content here.",
        )

        from src import verified_gate

        real_evaluate = verified_gate.evaluate
        calls = {"count": 0}

        def spy_evaluate(candidate, *, execute=False):
            calls["count"] += 1
            return real_evaluate(candidate, execute=execute)

        monkeypatch.setattr(verified_gate, "evaluate", spy_evaluate)

        events = await _run(
            cfg, fresh_context=True, context_window=window,
            verify=True, verify_repair=True, verify_execute=False,
        )

        # Concrete evidence the shared core is genuinely invoked (used, not
        # just imported): initial evaluate() + repair-recheck evaluate().
        assert calls["count"] >= 2

        # Behavior unchanged: both verification stages ran, and the
        # done/verified result is exactly what TestRepairRoundReuse expects.
        verification_events = [e for e in events if e["type"] == "verification"]
        stages = {e["stage"] for e in verification_events}
        assert stages == {"initial", "repair"}

        done = next(e for e in events if e["type"] == "done")
        assert done["verified"] is True
        assert done["final"] == good_answer


class TestDefaultOnAndFit:
    """Plan 08-02: Council default-on (D-01/FRESH-01), the always-on
    `context_fit` signal (D-05/06/07/FRESH-02), and the bounded verification
    repair `detail` (D-12). RED at Task 1 — none of this behavior exists yet
    (fresh_context still defaults False, context_fit is not emitted, and
    verdict['detail'] is embedded in the repair prompt unbounded)."""

    async def test_default_on_emits_context_fit_with_zero_kwargs(self, monkeypatch):
        # Zero fresh-context kwargs: if the production default is truly
        # curation-on (D-01), the run must curate and emit context_fit
        # without the caller asking for it at all.
        cfg = _two_layer_cfg()
        _make_stubs(monkeypatch, default="short answer, nothing special.")

        events = await _run(cfg)

        assert any(e["type"] == "context_fit" for e in events), (
            "expected a context_fit event with ZERO fresh-context kwargs — "
            "fresh_context must default to True in both RunRequest and "
            "run_council (D-01); today it defaults False so no curation "
            "(and no context_fit) ever runs."
        )

    async def test_context_fit_fires_once_per_seat_processed(self, monkeypatch):
        cfg = _two_layer_cfg()
        window = 800
        _make_stubs(monkeypatch, default="short answer, nothing special.")

        events = await _run(cfg, fresh_context=True, context_window=window)

        fit_events = [e for e in events if e["type"] == "context_fit"]
        # Two seats processed: panelist p1 (batch loop) + synthesizer chief.
        assert len(fit_events) == 2, (
            f"expected one context_fit per seat call (p1 + chief), got "
            f"{len(fit_events)} — context_fit is not implemented yet."
        )
        seat_ids = {e["seat_id"] for e in fit_events}
        assert seat_ids == {"p1", "chief"}
        for e in fit_events:
            assert set(e.keys()) >= {"type", "seat_id", "fits", "size", "window"}
            assert isinstance(e["fits"], bool)
            assert isinstance(e["size"], int)
            assert e["window"] == window

    async def test_bounded_detail_not_replayed_verbatim_in_repair_prompt(self, monkeypatch):
        # Force a repair round via a monkeypatched (module-object, per the
        # Phase 5/6 qualified-reference war story) src.council.verify.verify_answer:
        # oversized `detail` on the first call, passing verdict on the second.
        cfg = _two_layer_cfg()
        window = 800
        bad_answer = "some initial synth answer"
        good_answer = "the corrected synth answer"
        captured = _make_stubs(
            monkeypatch,
            scripted={"p1": ["p1 answer, short and simple."],
                     "chief": [bad_answer, good_answer]},
        )

        oversized_detail = "E" * 3000  # built here, never hardcoded elsewhere
        calls = {"n": 0}

        def fake_verify_answer(text, execute=False, timeout=8):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"has_code": True, "syntax_ok": False, "ran": False,
                        "passed": False, "detail": oversized_detail, "blocks": 1}
            return {"has_code": False, "syntax_ok": True, "ran": False,
                    "passed": True, "detail": "", "blocks": 0}

        from src.council import verify as _verify_mod
        monkeypatch.setattr(_verify_mod, "verify_answer", fake_verify_answer)

        await _run(
            cfg, fresh_context=True, context_window=window,
            verify=True, verify_repair=True, verify_execute=False,
        )

        # Initial synth call + repair call, both to the same synthesizer model.
        assert len(captured["chief"]) == 2, (
            "expected the repair round to fire a second call to chief"
        )
        repair_messages = captured["chief"][1]
        repair_user_content = _user_content(repair_messages)
        assert oversized_detail not in repair_user_content, (
            "the oversized verdict['detail'] string was replayed verbatim "
            "into the repair prompt — it must be distilled first (D-12)."
        )
