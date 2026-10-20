"""Phase 32 integration-checker connection (SC-5).

Proves the phase's headline honesty + isolation behaviors on the paths the
app actually assembles -- not only on the literally-named helpers -- mirroring
tests/test_phase28_integration.py .. tests/test_phase31_integration.py's
hermetic SC-5 discipline for this phase:

  - claim (a) (GRAY-BRN-04, from 32-01): invoking the REAL
    scripts.reindex_brain.main() with `--nomic` on `sys.argv` rejects the
    flag honestly -- non-zero return code, no nomic env write (via a spy on
    the real `_set_env`), `src.brain_index.build_index` never called, and no
    dead `_nomic_live` helper survives. The default (no-flag) path calls the
    REAL `main()` -> the (monkeypatched-at-source) `build_index` and reports
    the dim `build_index` actually returned, never a hardcoded/false literal
    -- advertised == actual in both directions.

  - claim (b) (GRAY-BRN-11, from 32-03 + 32-06): the SAME message text under
    two distinct owners, run through the REAL `src.llm_core.llm_call` (with
    `httpx.post` monkeypatched to a counting fake), yields two DISTINCT
    `_response_cache` entries and two distinct upstream responses -- no
    cross-owner leak -- and `_get_cache_key` itself produces different keys
    per owner. A representative real call site
    (`services/memory/skill_extractor.py::maybe_extract_skill`, fixed in
    32-06) forwards a real, non-fabricated owner into `llm_call_async`.

Hermetic: no live fastembed, Chroma, DB, LLM, or network is touched -- this
file must NOT be gated behind pytest.importorskip('fastembed'). Claim (a)
monkeypatches `src.brain_index.build_index` at its import source (mirrors
tests/test_reindex_backend.py's technique, since `main()` imports it lazily)
and points `scripts.reindex_brain.ENV`/`_vault` at tmp_path. Claim (b)
monkeypatches `src.llm_core.httpx.post` (mirrors
tests/test_response_cache_isolation.py) and `src.llm_core.llm_call_async`
for the representative call site (mirrors
tests/test_response_cache_callsites.py). Assertions never touch a raw
secret/dimension literal -- only return codes, distinct cache keys/entries,
absence of env writes, and the forwarded owner kwarg.

OUT OF SC-5 SCOPE (documented, not tested-as-fixed here): the live
real-server/real-endpoint/real-reindex confirmation is this plan's next
plan (32-08)'s human smoke, not this hermetic suite.
"""
import asyncio

import scripts.reindex_brain as rb
import src.llm_core as lc


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Claim (a) -- reindex --nomic is honest; default path reports the actual dim
# (GRAY-BRN-04).
# ---------------------------------------------------------------------------

def _fake_build_index(calls, dim=384):
    def _build_index(vault, out, *, batch=256, progress=None):
        calls.append({"vault": vault, "out": out, "batch": batch})
        return (3, dim)
    return _build_index


def test_reindex_nomic_flag_is_honest_sc5(tmp_path, monkeypatch, capsys):
    # The dead helper this plan's 32-01 predecessor removed must stay gone.
    assert not hasattr(rb, "_nomic_live")

    calls = []
    monkeypatch.setattr("src.brain_index.build_index", _fake_build_index(calls))

    env_file = tmp_path / ".env"
    monkeypatch.setattr(rb, "ENV", str(env_file))
    monkeypatch.setattr(rb, "_vault", lambda: str(tmp_path / "vault"))

    set_env_calls = []
    monkeypatch.setattr(rb, "_set_env", lambda kv: set_env_calls.append(dict(kv)))
    monkeypatch.setattr("sys.argv", ["reindex_brain.py", "--nomic"])

    rc = rb.main()

    assert rc != 0, "--nomic must be rejected, not silently accepted"
    assert calls == [], "build_index must never be invoked on the rejected --nomic path"
    assert set_env_calls == [], "--nomic must write NO env at all (no nomic EMBEDDING_MODEL write)"

    out = capsys.readouterr().out
    assert "supported" in out or "FastEmbed" in out, (
        "the rejection message must convey honesty (unsupported/FastEmbed-only), "
        "not a false nomic/768-d rebuild claim"
    )
    assert "768" not in out, "must never repeat the old false dimension claim"


def test_reindex_default_path_reports_actual_dim_sc5(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("src.brain_index.build_index", _fake_build_index(calls, dim=384))

    env_file = tmp_path / ".env"
    monkeypatch.setattr(rb, "ENV", str(env_file))
    monkeypatch.setattr(rb, "_vault", lambda: str(tmp_path / "vault"))

    set_env_calls = []
    real_set_env = rb._set_env

    def _spy_set_env(kv):
        set_env_calls.append(dict(kv))
        real_set_env(kv)

    monkeypatch.setattr(rb, "_set_env", _spy_set_env)
    monkeypatch.setattr("sys.argv", ["reindex_brain.py"])

    rc = rb.main()

    assert rc == 0
    assert len(calls) == 1, "the default path must build the index exactly once"
    assert any(kv.get("EMBEDDING_BACKEND") == "fastembed" for kv in set_env_calls)

    out = capsys.readouterr().out
    assert "384" in out, "the printed dim must equal build_index's ACTUAL returned dim"


# ---------------------------------------------------------------------------
# Claim (b) -- cross-owner cache isolation + a real call site forwards owner
# (GRAY-BRN-11).
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for an httpx.Response, OpenAI-compatible shape."""

    def __init__(self, content: str):
        self.is_success = True
        self.status_code = 200
        self.text = ""
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


_SAME_MESSAGES = [{"role": "user", "content": "same text, two owners"}]
_FAKE_URL = "http://fake-llm.test/v1/chat/completions"


def test_cache_no_cross_owner_collision_sc5(monkeypatch):
    """Same message text, two owners, through the REAL llm_call: no leak,
    two distinct _response_cache entries, and _get_cache_key itself differs
    per owner."""
    lc._response_cache.clear()
    responses = ["A", "B"]
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        idx = calls["n"]
        calls["n"] += 1
        return _FakeResponse(responses[idx])

    monkeypatch.setattr(lc.httpx, "post", fake_post)

    alice_result = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES, owner="alice")
    bob_result = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES, owner="bob")

    assert alice_result == "A"
    assert bob_result == "B", "bob must never see alice's cached response"
    assert calls["n"] == 2, "each owner must trigger its own upstream call"
    assert len(lc._response_cache) == 2, "two distinct owner-salted cache entries must exist"

    key_alice = lc._get_cache_key(_FAKE_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="alice")
    key_bob = lc._get_cache_key(_FAKE_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="bob")
    assert key_alice != key_bob


class _FakeSession:
    """Minimal stand-in for a chat Session -- only get_context_messages() is used."""

    def get_context_messages(self):
        return [
            {"role": "user", "content": "please fix the bug"},
            {"role": "assistant", "content": "done, ran 3 shell commands"},
        ]


def test_representative_callsite_forwards_owner_sc5(monkeypatch):
    """services/memory/skill_extractor.py::maybe_extract_skill (fixed in
    32-06) must forward its REAL owner into llm_call_async -- not fabricate,
    not drop it."""
    from services.memory.skill_extractor import maybe_extract_skill

    recorded = {}

    async def fake_llm_call_async(*args, **kwargs):
        recorded["owner"] = kwargs.get("owner")
        recorded["called"] = True
        return "null"  # "no reusable skill" -> maybe_extract_skill returns early

    monkeypatch.setattr(lc, "llm_call_async", fake_llm_call_async)

    result = run(
        maybe_extract_skill(
            session=_FakeSession(),
            skills_manager=None,
            endpoint_url="http://fake-llm.test/v1/chat/completions",
            model="fake-model",
            headers={},
            round_count=2,
            tool_count=3,
            owner="carol-real-owner",
        )
    )

    assert recorded.get("called") is True
    assert recorded.get("owner") == "carol-real-owner"
    assert recorded.get("owner") not in (None, "")
    assert result is None
