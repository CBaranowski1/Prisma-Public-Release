"""Phase 41 (Real-Model Three-Arm Measurement) — Wave-0 test for the NEW
dual-model polyglot screener `scripts/polyglot_ab/screen_polyglot.py`
(41-03, RMEAS-01).

Lazily imports the not-yet-existing module via `importlib.util.spec_from_
file_location` (mirrors tests/test_ab_harness_integration.py's `_h()`
collection-safety convention) so `pytest --co` stays usable regardless of
whether Task 2 has landed yet -- the FileNotFoundError/ModuleNotFoundError
happens at TEST-BODY runtime (inside `_screener()`), never at module
collection time. `pytest tests/test_screen_polyglot.py -q --co` must exit 0
before scripts/polyglot_ab/screen_polyglot.py exists.

Offline throughout: fakes `harness.call_ollama` and `runners.score` on the
loaded screener module's own `harness`/`runners` submodule references (the
screener imports both as MODULE OBJECTS, never name-bound, so patching
`screener.harness.call_ollama` / `screener.runners.score` is the correct,
non-silently-no-op target -- same name-binding discipline this repo's other
harness tests use for seat_runner.untrusted_context_message). No live
Ollama, no live ruby/sql execution, no network.
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_SCREENER_PATH = os.path.join(_REPO_ROOT, "scripts", "polyglot_ab", "screen_polyglot.py")


def _screener():
    """Lazily import the not-yet-existing scripts/polyglot_ab/screen_polyglot.py
    contract (41-03-T2). Raises FileNotFoundError/ModuleNotFoundError until
    that task lands -- the intended Wave-0 RED state."""
    spec = importlib.util.spec_from_file_location("screen_polyglot", _SCREENER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_RUBY_PROBLEM = {
    "task_id": "ruby/borderline-0",
    "language": "ruby",
    "prompt": "Define add(a, b) that returns a + b.",
    "entry_point": "add",
    "reference": "\ndef add(a, b)\n  a + b\nend\n",
    "test": 'raise "fail" unless add(1, 2) == 3',
    "timeout": 5,
}


# ---------------------------------------------------------------------------
# (1) Dual-model selection: weak band AND strong-saturation ceiling
# ---------------------------------------------------------------------------

def test_select_keeps_only_rows_with_both_model_headroom():
    """RMEAS-01: a row is kept only when weak_rate is inside [band_lo,
    band_hi] AND strong_rate is strictly below saturation_ceiling. A row
    the strong model has SATURATED (strong_rate >= ceiling) must be
    excluded even though its weak_rate sits dead-center in the borderline
    band -- headroom for BOTH models, not just the weak one, is the whole
    point of the dual-model criterion (vs. Phase 12's single-model
    screen_borderline.py)."""
    s = _screener()
    scored = [
        {"task_id": "keep-mid", "weak_rate": 0.5, "strong_rate": 0.5},        # both in range -> KEEP
        {"task_id": "keep-lo-edge", "weak_rate": 0.2, "strong_rate": 0.1},    # band edge inclusive -> KEEP
        {"task_id": "keep-hi-edge", "weak_rate": 0.8, "strong_rate": 0.3},    # band edge inclusive -> KEEP
        {"task_id": "drop-weak-too-easy", "weak_rate": 0.9, "strong_rate": 0.2},   # weak outside band
        {"task_id": "drop-weak-too-hard", "weak_rate": 0.1, "strong_rate": 0.2},   # weak outside band
        {"task_id": "drop-strong-saturated", "weak_rate": 0.5, "strong_rate": 0.95},  # weak fine, strong saturated
    ]
    # target_n small enough (and max_widen=0.0) that no widening is needed --
    # proves the base selection predicate in isolation.
    selected = s.select_polyglot_band(scored, target_n=3, band_lo=0.20, band_hi=0.80,
                                       saturation_ceiling=0.9, widen_step=0.10, max_widen=0.0)
    ids = {row["task_id"] for row in selected}
    assert ids == {"keep-mid", "keep-lo-edge", "keep-hi-edge"}
    assert "drop-strong-saturated" not in ids, \
        "a strong-saturated row must be excluded even with a borderline weak_rate"


# ---------------------------------------------------------------------------
# (2) Adaptive widening reaches target_n -- WEAK band only, ceiling fixed
# ---------------------------------------------------------------------------

def test_select_widens_weak_band_but_never_the_saturation_ceiling():
    """The adaptive-widening loop widens ONLY the weak band symmetrically
    (mirrors screen_borderline.py's proven _widen_to_target shape); the
    strong-saturation ceiling is a HARD CUTOFF that never widens -- rows
    that saturate the strong model stay excluded no matter how wide the
    weak band grows."""
    s = _screener()
    # A tight [0.4, 0.6] band alone selects only the 5 "mid" rows below;
    # adaptive widening (up to +/-0.20) must reach the "near" rows (weak_rate
    # 0.3/0.7) to hit target_n=12 -- but must NEVER pull in the "saturated"
    # rows even though their weak_rate (0.5) is dead-center in the band.
    scored = (
        [{"task_id": f"mid-{i}", "weak_rate": 0.5, "strong_rate": 0.5} for i in range(5)]
        + [{"task_id": f"near-lo-{i}", "weak_rate": 0.3, "strong_rate": 0.4} for i in range(5)]
        + [{"task_id": f"near-hi-{i}", "weak_rate": 0.7, "strong_rate": 0.4} for i in range(5)]
        + [{"task_id": f"saturated-{i}", "weak_rate": 0.5, "strong_rate": 0.95} for i in range(20)]
    )
    selected = s.select_polyglot_band(scored, target_n=12, band_lo=0.40, band_hi=0.60,
                                       saturation_ceiling=0.9, widen_step=0.10, max_widen=0.20)
    assert len(selected) >= 12
    ids = {row["task_id"] for row in selected}
    assert not any(tid.startswith("saturated-") for tid in ids), \
        "widening must never rescue a strong-saturated row"


# ---------------------------------------------------------------------------
# (3) Fail-loud: BandUnreachableError with a pass_rate diagnostic
# ---------------------------------------------------------------------------

def test_select_raises_band_unreachable_with_diagnostic_when_target_n_impossible():
    """When target_n is unreachable even at the widening cap (here: the
    strong model is saturated on EVERY row, so no amount of weak-band
    widening can rescue anything), select_polyglot_band must FAIL LOUD --
    never silently ship a tiny n. The exception message must carry a
    pass_rate diagnostic (a histogram) an operator can act on."""
    s = _screener()
    scored = [{"task_id": f"t{i}", "weak_rate": 0.5, "strong_rate": 0.99} for i in range(5)]
    with pytest.raises(Exception) as exc_info:
        s.select_polyglot_band(scored, target_n=10, band_lo=0.40, band_hi=0.60,
                                saturation_ceiling=0.9, widen_step=0.10, max_widen=0.20)
    assert isinstance(exc_info.value, s.BandUnreachableError)
    msg = str(exc_info.value)
    assert "rate" in msg.lower(), "the fail-loud message must include a pass_rate diagnostic"
    assert "10" in msg  # target_n surfaced in the diagnostic


# ---------------------------------------------------------------------------
# (4) Per-problem driver: all k samples, temperature-diversity ladder
# ---------------------------------------------------------------------------

def test_screen_pass_rate_runs_all_k_samples_no_shortcircuit(monkeypatch):
    """Pitfall 4: screen_pass_rate must run ALL k samples -- unlike a best-
    of-N sampler that short-circuits on the first passer -- because a pass
    RATE across k samples, not best-of-N, is what borderline-band
    screening needs. Faking call_ollama to ALWAYS pass proves the driver
    still burns all k calls rather than stopping at sample 1, and that it
    samples across more than one temperature (Pitfall 8)."""
    s = _screener()
    calls = {"n": 0, "temps": []}

    def fake_call_ollama(model, prompt, temperature):
        calls["n"] += 1
        calls["temps"].append(temperature)
        return "```ruby\ndef add(a, b)\n  a + b\nend\n```"

    def fake_score(text, problem, sql_backend="sqlite"):
        return True, ""

    monkeypatch.setattr(s.harness, "call_ollama", fake_call_ollama)
    monkeypatch.setattr(s.runners, "score", fake_score)

    rate = s.screen_pass_rate(_RUBY_PROBLEM, k=5, model="fake-weak-model")

    assert calls["n"] == 5, "screen_pass_rate must run ALL k samples, no short-circuit"
    assert rate == 1.0
    assert len(set(calls["temps"])) > 1, \
        "screening must sample across a temperature-diversity ladder, never a single pinned temperature"


def test_screen_pass_rate_degrades_a_flaky_sample_to_a_fail(monkeypatch):
    """A per-sample exception (model call OR scoring) must degrade THAT
    sample to a fail rather than aborting the whole screen -- mirrors
    screen_borderline.screen_pass_rate's flaky-sample tolerance."""
    s = _screener()
    calls = {"n": 0}

    def flaky_call_ollama(model, prompt, temperature):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated transient model error")
        return "```ruby\ndef add(a, b)\n  a + b\nend\n```"

    def fake_score(text, problem, sql_backend="sqlite"):
        return True, ""

    monkeypatch.setattr(s.harness, "call_ollama", flaky_call_ollama)
    monkeypatch.setattr(s.runners, "score", fake_score)

    rate = s.screen_pass_rate(_RUBY_PROBLEM, k=5, model="fake-weak-model")

    assert calls["n"] == 5, "a flaky sample must not abort the remaining k-1 samples"
    assert rate == 0.8  # 4/5 pass, one degraded to a fail


def test_screen_pass_rate_uses_prompt_tmpl_and_sql_backend(monkeypatch):
    """screen_pass_rate must drive harness.PROMPT_TMPL (the SAME Ruby/
    Postgres-aware template the live 3-arm run uses) and pass sql_backend
    through to runners.score (D-02: sqlite default, no local Postgres
    server)."""
    s = _screener()
    seen = {}

    def fake_call_ollama(model, prompt, temperature):
        seen["prompt"] = prompt
        return "```ruby\ndef add(a, b)\n  a + b\nend\n```"

    def fake_score(text, problem, sql_backend="postgres"):
        seen["sql_backend"] = sql_backend
        return True, ""

    monkeypatch.setattr(s.harness, "call_ollama", fake_call_ollama)
    monkeypatch.setattr(s.runners, "score", fake_score)

    s.screen_pass_rate(_RUBY_PROBLEM, k=1, model="fake-weak-model", sql_backend="sqlite")

    assert _RUBY_PROBLEM["prompt"] in seen["prompt"]
    assert seen["sql_backend"] == "sqlite"


# ---------------------------------------------------------------------------
# (5) Resumable _score_all: incremental cache survives a kill (41-07 hardening)
# ---------------------------------------------------------------------------

_RESUME_PROBLEMS = [
    {"task_id": "p0", "language": "ruby", "prompt": "add"},
    {"task_id": "p1", "language": "ruby", "prompt": "sub"},
    {"task_id": "p2", "language": "ruby", "prompt": "mul"},
    {"task_id": "p3", "language": "ruby", "prompt": "div"},
]


def test_score_all_resumes_from_partial_cache_skips_cached_ids(tmp_path, monkeypatch):
    """A pre-populated cache covering a SUBSET of task_ids (p0, p2) must be
    reused verbatim -- screen_pass_rate must NEVER be called for those
    task_ids (a kill mid-run must not force re-scoring already-cached
    problems, which is the whole point of resumability). The remaining
    task_ids (p1, p3) must be scored AND appended to the cache file so a
    SUBSEQUENT kill loses at most the in-flight problem."""
    s = _screener()
    cache_path = tmp_path / "cache.jsonl"
    cached_rows = {
        "p0": {"task_id": "p0", "weak_rate": 0.11, "strong_rate": 0.22},
        "p2": {"task_id": "p2", "weak_rate": 0.33, "strong_rate": 0.44},
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        for row in cached_rows.values():
            f.write(json.dumps(row) + "\n")

    calls: dict[str, list[str]] = {}

    def spy(problem, k, model, temperature_ladder=None, sql_backend="sqlite"):
        tid = problem["task_id"]
        if tid in cached_rows:
            raise AssertionError(f"screen_pass_rate must not be called for cached task_id {tid}")
        calls.setdefault(tid, []).append(model)
        return 0.42

    monkeypatch.setattr(s, "screen_pass_rate", spy)

    scored = s._score_all(_RESUME_PROBLEMS, "weak-model", "strong-model", k=3,
                           cache_path=str(cache_path))

    # order-preserving over problems
    assert [row["task_id"] for row in scored] == ["p0", "p1", "p2", "p3"]

    # cached rows reused verbatim, never re-scored
    assert scored[0] == {"task_id": "p0", "weak_rate": 0.11, "strong_rate": 0.22}
    assert scored[2] == {"task_id": "p2", "weak_rate": 0.33, "strong_rate": 0.44}
    assert calls.get("p0") is None
    assert calls.get("p2") is None

    # non-cached rows freshly scored via the spy
    assert scored[1] == {"task_id": "p1", "weak_rate": 0.42, "strong_rate": 0.42}
    assert scored[3] == {"task_id": "p3", "weak_rate": 0.42, "strong_rate": 0.42}
    assert calls["p1"] == ["weak-model", "strong-model"]
    assert calls["p3"] == ["weak-model", "strong-model"]

    # freshly-scored rows must be appended (flushed) to the cache file
    with open(cache_path, encoding="utf-8") as f:
        persisted = {json.loads(line)["task_id"]: json.loads(line) for line in f if line.strip()}
    assert persisted.keys() == {"p0", "p1", "p2", "p3"}
    assert persisted["p1"] == {"task_id": "p1", "weak_rate": 0.42, "strong_rate": 0.42}
    assert persisted["p3"] == {"task_id": "p3", "weak_rate": 0.42, "strong_rate": 0.42}


def test_score_all_second_run_over_full_cache_makes_zero_scoring_calls(tmp_path, monkeypatch):
    """Once the cache is FULLY populated (e.g. after a first run completed,
    or a resumed run finished), a second `_score_all` pass over the same
    problems must make ZERO scoring calls -- full resume, not just partial."""
    s = _screener()
    cache_path = tmp_path / "cache.jsonl"
    with open(cache_path, "w", encoding="utf-8") as f:
        for problem in _RESUME_PROBLEMS:
            f.write(json.dumps({"task_id": problem["task_id"], "weak_rate": 0.5,
                                 "strong_rate": 0.6}) + "\n")

    def never_call(problem, k, model, temperature_ladder=None, sql_backend="sqlite"):
        raise AssertionError("screen_pass_rate must not be called when the cache is fully populated")

    monkeypatch.setattr(s, "screen_pass_rate", never_call)

    scored = s._score_all(_RESUME_PROBLEMS, "weak-model", "strong-model", k=3,
                           cache_path=str(cache_path))

    assert [row["task_id"] for row in scored] == ["p0", "p1", "p2", "p3"]
    assert all(row["weak_rate"] == 0.5 and row["strong_rate"] == 0.6 for row in scored)


def test_glm_screen_cache_requires_matching_reasoning_fingerprint(tmp_path, monkeypatch):
    """An old model/reasoning cache row must never contaminate a GLM run."""
    s = _screener()
    cache_path = tmp_path / "glm-cache.jsonl"
    cache_path.write_text(json.dumps({
        "task_id": "p0", "weak_rate": 0.99, "strong_rate": 0.99,
        "screen_fingerprint": "different-run",
    }) + "\n", encoding="utf-8")
    calls = []

    def spy(problem, k, model, temperature_ladder=None, sql_backend="sqlite", **kwargs):
        calls.append((model, kwargs["reasoning_effort"]))
        return 0.4 if kwargs["reasoning_effort"] == "low" else 0.7

    monkeypatch.setattr(s, "screen_pass_rate", spy)
    problems = [_RESUME_PROBLEMS[0]]
    options = dict(
        backend="openai", base_url="http://spark:1919/v1",
        weak_reasoning="low", strong_reasoning="max",
        cache_path=str(cache_path),
    )
    scored = s._score_all(problems, "glm", "glm", k=3, **options)
    assert scored == [{"task_id": "p0", "weak_rate": 0.4, "strong_rate": 0.7}]
    assert calls == [("glm", "low"), ("glm", "max")]

    persisted = [json.loads(line) for line in cache_path.read_text(
        encoding="utf-8").splitlines()]
    fresh = persisted[-1]
    assert fresh["screen_fingerprint"] != "different-run"
    assert fresh["weak_reasoning"] == "low"
    assert fresh["strong_reasoning"] == "max"
    assert "spark" not in json.dumps(fresh)

    calls.clear()
    s._score_all(problems, "glm", "glm", k=3, **options)
    assert calls == [], "the exact GLM reasoning configuration should resume"


def test_module_import_makes_no_live_call():
    """Importing screen_polyglot.py must never make a live model call --
    live calls happen only under main() / __main__ (task done-criterion).
    If _screener() itself made a live network call this would hang/err
    under a hermetic test run; reaching these assertions at all is proof it
    didn't."""
    s = _screener()
    assert hasattr(s, "select_polyglot_band")
    assert hasattr(s, "screen_pass_rate")
    assert hasattr(s, "BandUnreachableError")
    assert callable(s.main)
