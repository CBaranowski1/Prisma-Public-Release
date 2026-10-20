"""Tests for the GSD-ported fresh-context discipline (src/council/fresh_context.py)
and its opt-in wiring into the council seat message builder."""
from dataclasses import dataclass

from src.council import fresh_context as fc
from src.council.seat_runner import _build_messages
from src.council.schemas import Seat, SeatResult


def test_distill_bounds_and_keeps_head_tail():
    long = "HEAD-" + ("x" * 5000) + "-TAIL"
    d = fc.distill(long, 400)
    assert len(d) <= 400
    assert d.startswith("HEAD")
    assert d.endswith("TAIL")
    # already-short text is returned unchanged
    assert fc.distill("short", 400) == "short"


def test_curate_keeps_only_live_and_bounds_total():
    ups = [
        SeatResult(seat_id="a", content="A" * 3000, ok=True),
        SeatResult(seat_id="b", content="B" * 3000, ok=True),
        SeatResult(seat_id="c", content="dead", ok=False),
        SeatResult(seat_id="d", content="", ok=True),
    ]
    cur = fc.curate(ups, window=1200)
    assert len(cur) == 2                                  # only the two live, non-empty
    assert sum(len(r.content) for r in cur) <= 1300       # bounded to ~window
    # originals are never mutated
    assert len(ups[0].content) == 3000


def test_curate_bounds_total_past_max_items():
    # Proven reproduction case: today's per_item = max(_MIN_PER_ITEM, window // len(live))
    # formula overflows the window by ~12x here (12,000 chars against a 1000-char window).
    ups = [SeatResult(seat_id=str(i), content="z" * 2000, ok=True) for i in range(50)]
    cur = fc.curate(ups, window=1000)
    assert fc.fits("".join(r.content for r in cur), 1000)
    assert sum(len(r.content) for r in cur) <= 1000


def test_curate_output_always_fits():
    for n_items, window in [(5, 4000), (16, 4000), (17, 4000), (50, 1000), (1, 240), (100, 4000)]:
        ups = [SeatResult(seat_id=str(i), content="z" * 2000, ok=True) for i in range(n_items)]
        cur = fc.curate(ups, window=window)
        assert fc.fits("".join(r.content for r in cur), window), (n_items, window)


def test_curate_reports_dropped_count():
    window = 1000
    ups = [SeatResult(seat_id=str(i), content="z" * 2000, ok=True) for i in range(50)]
    cur = fc.curate(ups, window=window)
    max_items = window // fc._MIN_PER_ITEM
    assert cur.dropped_count == len(ups) - max_items


def test_curate_tiny_window_never_raises():
    ups = [SeatResult(seat_id="only", content="z" * 2000, ok=True)]
    cur = fc.curate(ups, window=100)   # window < _MIN_PER_ITEM -> max_items == 0
    assert cur == []
    assert cur.dropped_count == 1


def test_curate_is_deterministic():
    ups = [SeatResult(seat_id=str(i), content=f"content-{i}-" + ("z" * 500), ok=True) for i in range(20)]
    first = fc.curate(ups, window=1500)
    second = fc.curate(ups, window=1500)
    assert [r.content for r in first] == [r.content for r in second]


def test_distill_is_deterministic():
    big_text = "HEAD-" + ("x" * 5000) + "-TAIL"
    first = fc.distill(big_text, 500)
    second = fc.distill(big_text, 500)
    third = fc.distill(big_text, 500)
    assert first == second == third


def test_fits_guard():
    msgs = [{"role": "system", "content": "x" * 100}, {"role": "user", "content": "y" * 100}]
    assert fc.fits(msgs, 500) is True
    assert fc.fits(msgs, 150) is False
    assert fc.fits("a" * 300, 200) is False


def test_empty_upstream_returns_empty():
    assert fc.curate([], 4000) == []
    assert fc.curate(None, 4000) == []


def test_build_messages_default_is_unchanged():
    seat = Seat(id="chief", role="synthesizer")
    ups = [SeatResult(seat_id="p1", name="Panelist", content="C" * 3000, ok=True)]
    msgs_default = _build_messages(seat, "the prompt", ups)          # window=None
    # full upstream content present when not curating
    assert "C" * 3000 in msgs_default[1]["content"]


def test_build_messages_fresh_context_curates():
    # D-03: curation now happens at the curate_for/orchestrator boundary, not
    # inside _build_messages (which no longer accepts a window param post-D-02).
    results = {"p1": SeatResult(seat_id="p1", name="Panelist", content="C" * 3000, ok=True)}
    seat = Seat(id="chief", role="synthesizer", consumes=["p1"])
    curated = fc.curate_for(seat, results, 800)
    body = curated[0].content
    assert "C" * 3000 not in body                 # full dump was curated away
    assert "context trimmed for a fresh window" in body
    assert len(body) < 3000                        # bounded


def test_curate_for_selects_only_consumed():
    results = {
        "a": SeatResult(seat_id="a", content="A" * 500, ok=True),
        "b": SeatResult(seat_id="b", content="B" * 500, ok=True),
        "x": SeatResult(seat_id="x", content="X" * 500, ok=True),
    }
    seat = Seat(id="chief", role="synthesizer", consumes=["a", "b", "missing"])
    cur = fc.curate_for(seat, results)
    ids = {r.seat_id for r in cur}
    assert ids == {"a", "b"}          # only consumed ids are present
    assert "x" not in ids             # present-but-not-consumed is dropped
    # a consumed id absent from results is skipped, not a KeyError


def test_curate_for_excludes_dead_or_empty_consumed():
    results = {
        "a": SeatResult(seat_id="a", content="A" * 500, ok=True),
        "b": SeatResult(seat_id="b", content="B" * 500, ok=False),   # dead
        "c": SeatResult(seat_id="c", content="", ok=True),           # empty
    }
    seat = Seat(id="chief", role="synthesizer", consumes=["a", "b", "c"])
    cur = fc.curate_for(seat, results)
    ids = {r.seat_id for r in cur}
    assert ids == {"a"}               # curate()'s live-filter composes through
