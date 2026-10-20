"""Phase-37 automated re-encoding of Phase 32's deferred live smoke (32-08).

Per 32-VERIFICATION.md, 32-08 was a human-only live smoke covering five
properties this repo could not (at the time) prove in-process. This file
proves the SAME properties (SMOKE-01) on the assembled-app / real-source
paths, hermetically:

  1. `scripts.reindex_brain.main()` rejects `--nomic` honestly -- non-zero
     return, NO `.env`/`os.environ` mutation before the return.
  2. `src.model_discovery` guards a non-dict `/v1/models` body (no raise)
     and `discover_models()`/`get_providers()` returns an additive
     `status='none_found'` + `remediation` on a fresh install with no
     reachable endpoint and no OpenAI key -- `hosts`/`items` always present.
  3. `src.llm_core._get_cache_key` is owner-salted: two owners never
     collide; a single/no-owner caller stays on one backward-compatible
     bucket.
  4. `PersonalDocsManager`'s incremental (mtime, size) cache is proven
     through the ASSEMBLED `POST /api/personal/reload` route (not just the
     manager method directly): an unchanged file is reused (not
     re-extracted), and `unextractable_count` is additive and distinct from
     empty once a genuinely-unextractable file is present.
  5. `src.embeddings._windows_selfheal_and_build`'s quarantine-rename
     self-heal: the suspect cache dir is renamed aside BEFORE any rebuild
     attempt, deleted only after a successful rebuild, and restored (with
     its original contents intact) when the rebuild keeps failing -- all
     under `core.file_locks.get_lock`.

HONEST RESIDUAL (see the docstring above the Task-3 tests below): the real
Windows FastEmbed cache directory self-heal against an actual WinError-1463
symlinked cache on a real Windows filesystem is NOT reproduced in-process
here -- a `tmp_path` cache dir + monkeypatched build failure is the faithful
LOGIC proxy. The real-OS confirmation is routed to Phase 38/39
reconciliation, not faked here.

Hermetic: no live network, fastembed download, Chroma, or LLM call is
touched. `DO NOT merely duplicate tests/test_phase32_integration.py` --
this file elevates the SAME headline claims onto the assembled-app paths
(the personal-docs reload route, the real `_windows_selfheal_and_build`
quarantine/restore cycle) that phase 32's hermetic SC-5 checker did not
cover, alongside the reindex/discovery/cache claims it did.
"""

import os
import sys
from pathlib import Path

import pytest

import core.file_locks as file_locks
import scripts.reindex_brain as rb
import src.embeddings as emb
import src.llm_core as lc
import src.model_discovery as model_discovery
import src.personal_docs as pd
from routes.personal_routes import setup_personal_routes


# ---------------------------------------------------------------------------
# Task 1a -- reindex --nomic is rejected honestly, pre-mutation.
# ---------------------------------------------------------------------------


def _fake_build_index(calls, dim=384):
    def _build_index(vault, out, *, batch=256, progress=None):
        calls.append({"vault": vault, "out": out, "batch": batch})
        return (3, dim)
    return _build_index


def test_reindex_nomic_rejected_before_any_env_mutation(tmp_path, monkeypatch, capsys):
    # The dead helper a prior phase removed must stay gone.
    assert not hasattr(rb, "_nomic_live")

    build_calls = []
    monkeypatch.setattr("src.brain_index.build_index", _fake_build_index(build_calls))

    env_file = tmp_path / ".env"
    monkeypatch.setattr(rb, "ENV", str(env_file))
    monkeypatch.setattr(rb, "_vault", lambda: str(tmp_path / "vault"))

    set_env_calls = []
    monkeypatch.setattr(rb, "_set_env", lambda kv: set_env_calls.append(dict(kv)))
    monkeypatch.setattr(sys, "argv", ["reindex_brain.py", "--nomic"])

    # Spy directly on os.environ (not just _set_env) so a future refactor
    # that writes os.environ directly (bypassing _set_env) is still caught.
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_URL", raising=False)

    rc = rb.main()

    assert rc != 0, "--nomic must be rejected, not silently accepted"
    assert build_calls == [], "build_index must never be invoked on the rejected --nomic path"
    assert set_env_calls == [], "--nomic must write NO env at all (no nomic EMBEDDING_MODEL write)"
    assert "EMBEDDING_BACKEND" not in os.environ, "--nomic must not mutate os.environ before returning"
    assert "EMBEDDING_URL" not in os.environ, "--nomic must not mutate os.environ before returning"
    assert not env_file.exists(), "--nomic must not write the .env file at all"

    out = capsys.readouterr().out
    assert "supported" in out or "FastEmbed" in out, (
        "the rejection message must convey honesty (unsupported/FastEmbed-only), "
        "not a false nomic/768-d rebuild claim"
    )
    assert "768" not in out, "must never repeat a false dimension claim"


# ---------------------------------------------------------------------------
# Task 1b -- model discovery guards a non-dict /v1/models body and returns
# an additive none_found + remediation on a fresh install.
# ---------------------------------------------------------------------------


class _FakeModelsResponse:
    """Minimal httpx.Response stand-in -- no real network."""

    def __init__(self, json_body, is_success=True):
        self._json_body = json_body
        self.is_success = is_success

    def json(self):
        return self._json_body


def test_discovery_check_port_guards_non_dict_models_body(monkeypatch, caplog):
    """A bare-list /v1/models body must be shape-rejected (logged), not
    raise and not be mistaken for a valid empty/host-down result."""
    monkeypatch.setattr(
        model_discovery.httpx, "get",
        lambda *a, **k: _FakeModelsResponse(["unexpected", "list", "shape"]),
    )

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1")
    with caplog.at_level("WARNING"):
        result = md._check_port("127.0.0.1", 8000)  # must not raise

    assert result is None
    assert any("non-dict" in rec.message for rec in caplog.records)


def _clear_llm_discovery_env(monkeypatch):
    for env_name in ("LLM_HOSTS", "OLLAMA_BASE_URL", "OLLAMA_URL", "LM_STUDIO_URL"):
        monkeypatch.delenv(env_name, raising=False)


def test_discovery_none_found_additive_with_hosts_items_present(monkeypatch):
    """Fresh install: no reachable endpoint, no OpenAI key -> additive
    status='none_found' + remediation, while hosts/items keys are ALWAYS
    present (never removed/replaced -- existing UI consumers depend on the
    shape)."""
    _clear_llm_discovery_env(monkeypatch)
    monkeypatch.setattr(
        model_discovery, "get_setting",
        lambda key, default=None: False, raising=False,
    )
    monkeypatch.setattr(
        model_discovery.ModelDiscovery, "_check_port",
        lambda self, h, p: None,
    )

    md = model_discovery.ModelDiscovery(default_host="127.0.0.1")
    result = md.discover_models()

    assert "hosts" in result and "items" in result, "hosts/items keys must always be present"
    assert result["items"] == []
    assert result["status"] == "none_found"
    assert result["remediation"], "remediation must be non-empty guidance text"

    providers = md.get_providers()
    assert providers["providers"][0]["provider"] == "vllm"
    assert providers["providers"][0]["items"] == []


# ---------------------------------------------------------------------------
# Task 2a -- owner-salted response cache: no cross-owner collision, single-
# owner (no owner) stays on the pre-salt backward-compatible bucket.
# ---------------------------------------------------------------------------


class _FakeChatResponse:
    """Minimal stand-in for an httpx.Response, OpenAI-compatible shape."""

    def __init__(self, content: str):
        self.is_success = True
        self.status_code = 200
        self.text = ""
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


_SAME_MESSAGES = [{"role": "user", "content": "same text, cache-key smoke"}]
_FAKE_LLM_URL = "http://fake-llm.test/v1/chat/completions"


def test_cache_key_owner_salted_no_cross_owner_collision(monkeypatch):
    lc._response_cache.clear()
    responses = ["A", "B"]
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        idx = calls["n"]
        calls["n"] += 1
        return _FakeChatResponse(responses[idx])

    monkeypatch.setattr(lc.httpx, "post", fake_post)

    alice_result = lc.llm_call(_FAKE_LLM_URL, "m", _SAME_MESSAGES, owner="alice")
    bob_result = lc.llm_call(_FAKE_LLM_URL, "m", _SAME_MESSAGES, owner="bob")

    assert alice_result == "A"
    assert bob_result == "B", "bob must never see alice's cached response"
    assert calls["n"] == 2, "each owner must trigger its own upstream call"
    assert len(lc._response_cache) == 2, "two distinct owner-salted cache entries must exist"

    key_alice = lc._get_cache_key(_FAKE_LLM_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="alice")
    key_bob = lc._get_cache_key(_FAKE_LLM_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="bob")
    assert key_alice != key_bob


def test_cache_key_no_owner_stays_backward_compatible_single_bucket(monkeypatch):
    """Omitting owner (None) and owner="" both collapse to the SAME single
    bucket -- byte-identical key -- matching pre-salt behavior. A repeat
    call with no owner is still a cache hit (only one upstream call)."""
    key_none = lc._get_cache_key(_FAKE_LLM_URL, "m", _SAME_MESSAGES, 1.0, 100, owner=None)
    key_empty = lc._get_cache_key(_FAKE_LLM_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="")
    key_default = lc._get_cache_key(_FAKE_LLM_URL, "m", _SAME_MESSAGES, 1.0, 100)
    assert key_none == key_empty == key_default, "no-owner callers must share ONE byte-identical bucket"

    lc._response_cache.clear()
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeChatResponse("A")

    monkeypatch.setattr(lc.httpx, "post", fake_post)

    first = lc.llm_call(_FAKE_LLM_URL, "m", _SAME_MESSAGES)
    second = lc.llm_call(_FAKE_LLM_URL, "m", _SAME_MESSAGES)

    assert first == "A"
    assert second == "A"
    assert calls["n"] == 1, "no-owner bucket must still collapse identical repeats (backward compat)"


# ---------------------------------------------------------------------------
# Task 2b -- personal-docs incremental cache via the ASSEMBLED
# POST /api/personal/reload route (not just PersonalDocsManager directly).
# ---------------------------------------------------------------------------


def _route(router, path: str, method: str):
    """Locate a registered route's endpoint callable by (path, method),
    mirroring tests/test_research_owner_scope_routes.py's synthetic-
    authenticated-request pattern: call the assembled route handler
    directly with explicit owner/_admin kwargs, bypassing FastAPI's
    Depends() resolution (which only triggers through the ASGI app), since
    driving full auth middleware through TestClient is heavy for this
    hermetic smoke."""
    for route in router.routes:
        if getattr(route, "path", "") != path:
            continue
        if method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def test_personal_reload_route_reuses_unchanged_files_and_surfaces_unextractable(tmp_path, monkeypatch):
    personal_dir = tmp_path / "personal"
    personal_dir.mkdir()
    note = personal_dir / "note.txt"
    note.write_text("hello incremental cache world", encoding="utf-8")

    # PersonalDocsManager.__init__ calls refresh_index() once already, which
    # performs the FIRST extraction of note.txt via read_text_file. We only
    # start counting AFTER construction so the assertion below isolates the
    # incremental-reuse behavior on the SECOND (route-triggered) refresh.
    manager = pd.PersonalDocsManager(str(personal_dir))
    assert len(manager.index) == 1
    assert manager.get_stats()["unextractable_count"] == 0

    read_calls = {"n": 0}
    real_read_text_file = pd.read_text_file

    def counting_read_text_file(path):
        read_calls["n"] += 1
        return real_read_text_file(path)

    monkeypatch.setattr(pd, "read_text_file", counting_read_text_file)

    router = setup_personal_routes(manager, None, False)
    reload_route = _route(router, "/api/personal/reload", "POST")

    # First reload through the assembled route: note.txt's (mtime, size) is
    # UNCHANGED since construction -> must be reused from cache, not
    # re-extracted (read_text_file must NOT be called again).
    result = reload_route(owner="test-owner", _admin=None)
    assert result["ok"] is True
    assert result["count"] == 1
    assert "unextractable_count" in result, "unextractable_count must be additive on the reload response"
    assert result["unextractable_count"] == 0
    assert read_calls["n"] == 0, (
        "an unchanged (mtime, size) file must be reused from the incremental cache, "
        "not re-extracted, on the assembled reload route"
    )

    # Now introduce a genuinely-unextractable file (malformed PDF bytes) and
    # reload again: unextractable_count must go from 0 -> 1 (distinct from
    # empty), while note.txt is STILL reused (still 0 read_text_file calls).
    bad_pdf = personal_dir / "corrupt.pdf"
    bad_pdf.write_bytes(b"this is not a real pdf file, extraction must fail")

    result2 = reload_route(owner="test-owner", _admin=None)
    assert result2["ok"] is True
    assert result2["count"] == 2
    assert result2["unextractable_count"] == 1, "a genuinely-unextractable file must surface distinct from empty"
    assert read_calls["n"] == 0, "note.txt must still be reused from cache on the second reload"


# ---------------------------------------------------------------------------
# Task 3 -- Windows FastEmbed cache quarantine-rename self-heal (in-process
# PROXY against a tmp_path cache dir + monkeypatched build failure).
#
# RESIDUAL (honest triage, per 37-CONTEXT.md): the real Windows FastEmbed
# cache directory self-heal against an ACTUAL WinError-1463 broken-symlink
# condition on a real Windows filesystem/HuggingFace-hub cache is NOT
# reproduced in-process by this test -- creating a genuine dangling symlink
# requires SeCreateSymbolicLinkPrivilege, which a CI/dev box frequently
# lacks, and even when available, a real fastembed re-download is
# explicitly out of scope for a hermetic suite. The tests below drive the
# REAL `src.embeddings._windows_selfheal_and_build` function (no
# reimplementation) against a `tmp_path` cache dir with the SAME
# broken-symlink-detection contract simulated (a real directory tree, a
# `model.onnx` path that deliberately does not exist so `os.path.exists`
# is naturally False, with `os.path.islink` patched true for ONLY that
# exact path) and a `build_embedder` callable that raises-then-succeeds.
# This is the faithful LOGIC proxy for the quarantine-rename /
# delete-on-success / restore-on-failure / under-get_lock contract; the
# real-OS WinError-1463 confirmation is routed to Phase 38/39
# reconciliation, not faked here.
# ---------------------------------------------------------------------------


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _make_cache_with_broken_symlink(tmp_path, monkeypatch, marker_contents: bytes):
    """Build a fake HF-hub cache layout under tmp_path with one dangling
    `models--foo` symlink, plus a live-cache marker file so a
    failed-then-restored cycle's data preservation can be verified by
    content, not just existence. Tries a real OS symlink first; falls back
    to a placeholder file with os.path.islink/exists monkeypatched ONLY for
    that exact path when the host lacks symlink privilege."""
    cache_dir = tmp_path / "cache"
    root = cache_dir / "models--foo"
    snap_dir = root / "snapshots" / "rev"
    snap_dir.mkdir(parents=True)
    onnx_path = snap_dir / "model.onnx"
    (root / "live-marker.bin").write_bytes(marker_contents)

    real_symlink_created = True
    try:
        onnx_path.symlink_to(cache_dir / "blobs" / "deadbeef-nonexistent")
    except (OSError, NotImplementedError):
        real_symlink_created = False
        onnx_path.write_bytes(b"placeholder-not-a-real-symlink")

    if not real_symlink_created:
        real_islink = os.path.islink
        real_exists = os.path.exists
        target_norm = _norm(onnx_path)

        def fake_islink(path):
            if _norm(path) == target_norm:
                return True
            return real_islink(path)

        def fake_exists(path):
            if _norm(path) == target_norm:
                return False
            return real_exists(path)

        monkeypatch.setattr(os.path, "islink", fake_islink)
        monkeypatch.setattr(os.path, "exists", fake_exists)

    return str(cache_dir), str(root)


def _spy_get_lock(monkeypatch):
    """Patch core.file_locks.get_lock with a call-counting spy that still
    returns the real RLock. `_windows_selfheal_and_build` does
    `from core.file_locks import get_lock` INSIDE the function body, so it
    re-resolves this module attribute on every call -- patching the module
    attribute is what actually intercepts it."""
    calls = {"n": 0}
    real_get_lock = file_locks.get_lock

    def spy(path):
        calls["n"] += 1
        return real_get_lock(path)

    monkeypatch.setattr(file_locks, "get_lock", spy)
    return calls


def test_selfheal_quarantines_before_rebuild_restores_on_failure_deletes_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_is_windows_platform", lambda: True)
    lock_calls = _spy_get_lock(monkeypatch)
    marker_contents = b"original-live-cache-bytes-must-survive-a-failed-rebuild"
    cache_dir, root = _make_cache_with_broken_symlink(tmp_path, monkeypatch, marker_contents)

    def _no_quarantine_leftovers():
        return [
            p for p in Path(cache_dir).glob("models--foo*")
            if p.name != "models--foo"
        ]

    # --- Cycle 1: build_embedder FAILS. Must quarantine BEFORE the call
    # (asserted from INSIDE build_embedder, proving ordering, not just the
    # end state), then RESTORE the original dir + its contents on failure.
    def fake_build_fails():
        assert not os.path.isdir(root), (
            "the suspect cache dir must already be renamed away BEFORE build_embedder runs"
        )
        quarantines = _no_quarantine_leftovers()
        assert len(quarantines) == 1, "exactly one quarantine dir must exist during the rebuild attempt"
        raise RuntimeError("simulated corrupt/locked cache build failure")

    with pytest.raises(RuntimeError, match="simulated corrupt/locked cache build failure"):
        emb._windows_selfheal_and_build(cache_dir, "m", fake_build_fails)

    assert lock_calls["n"] == 1, "the critical section must run under core.file_locks.get_lock"
    assert os.path.isdir(root), "original cache dir must be restored on failure -- never left destroyed"
    restored_marker = Path(root) / "live-marker.bin"
    assert restored_marker.read_bytes() == marker_contents, (
        "the live cache's contents must survive a failed-then-restored cycle byte-for-byte "
        "(no unconditional pre-build rmtree of a healthy cache)"
    )
    assert _no_quarantine_leftovers() == [], "no quarantine dir may be left dangling after a restore"

    # --- Cycle 2: build_embedder SUCCEEDS. The (still-broken-per-our-proxy)
    # cache is quarantined again, the rebuild succeeds, and ONLY THEN is the
    # quarantine deleted -- never deleted before success is proven.
    sentinel = object()

    def fake_build_succeeds():
        assert not os.path.isdir(root), "the cache dir must be renamed away before this rebuild too"
        return sentinel

    result = emb._windows_selfheal_and_build(cache_dir, "m", fake_build_succeeds)

    assert result is sentinel
    assert lock_calls["n"] == 2
    assert list(Path(cache_dir).glob("models--foo*")) == [], (
        "the quarantine must be deleted only AFTER a successful rebuild -- no leftovers of any kind remain"
    )


def test_selfheal_clean_cache_untouched_on_windows(tmp_path, monkeypatch):
    """Guard rail around the headline claim: a CLEAN cache (no broken
    symlink) is never touched -- build_embedder still runs under get_lock
    (the detect step always acquires it), but no rename ever happens."""
    monkeypatch.setattr(emb, "_is_windows_platform", lambda: True)
    lock_calls = _spy_get_lock(monkeypatch)

    clean_cache = tmp_path / "clean_cache"
    snap_dir = clean_cache / "models--foo" / "snapshots" / "rev"
    snap_dir.mkdir(parents=True)
    (snap_dir / "model.onnx").write_bytes(b"real onnx bytes, not a broken symlink")

    sentinel = object()
    build_calls = {"n": 0}

    def fake_build():
        build_calls["n"] += 1
        return sentinel

    result = emb._windows_selfheal_and_build(str(clean_cache), "m", fake_build)

    assert result is sentinel
    assert build_calls["n"] == 1
    assert os.path.isdir(str(clean_cache / "models--foo")), "a clean cache must be left completely untouched"
    assert lock_calls["n"] == 1, "the detect step runs under get_lock even for a clean cache"


def test_selfheal_is_pure_passthrough_on_non_windows(tmp_path, monkeypatch):
    """On non-Windows, the function must be a pure passthrough: no lock,
    no rename, build_embedder called directly. Platform emulation patches the
    module helper instead of mutating process-wide os.name, which pytest and
    pathlib also depend on."""
    lock_calls = _spy_get_lock(monkeypatch)

    posix_cache = tmp_path / "posix_cache"
    posix_cache.mkdir()

    sentinel = object()
    build_calls = {"n": 0}

    def fake_build():
        build_calls["n"] += 1
        return sentinel

    monkeypatch.setattr(emb, "_is_windows_platform", lambda: False)
    result = emb._windows_selfheal_and_build(str(posix_cache), "m", fake_build)

    assert result is sentinel
    assert build_calls["n"] == 1
    assert lock_calls["n"] == 0, "non-Windows must never touch get_lock -- pure passthrough"
