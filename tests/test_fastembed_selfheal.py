"""
tests/test_fastembed_selfheal.py

Hermetic test for the Windows FastEmbed cache self-heal (GRAY-BRN-15,
threat T-32-rmtree). Structural analog: tests/test_hf_symlink_guard.py --
monkeypatch + tmp_path only, platform-gated via the module's platform helper,
NEVER a real fastembed download/import or a process-wide `os.name` mutation.

`src.embeddings` is safe to import at module scope without fastembed
installed: `FastEmbedClient.__init__` imports `TextEmbedding` lazily inside
the constructor (guarded by try/except ImportError), and
`_windows_selfheal_and_build` is a plain module-level function with no
fastembed dependency at all -- it takes a caller-supplied `build_embedder`
callable instead of importing/constructing TextEmbedding itself.
"""

import os

import pytest

import core.file_locks as file_locks
import src.embeddings as emb


def _norm(path) -> str:
    """Normalize a path for robust cross-platform string comparison
    (case-insensitive on Windows, normalized separators)."""
    return os.path.normcase(os.path.normpath(str(path)))


def _spy_get_lock(monkeypatch):
    """Patch core.file_locks.get_lock with a call-counting spy that still
    returns the real RLock, so `with get_lock(...):` keeps working. The
    self-heal function does `from core.file_locks import get_lock` INSIDE
    the function body, so it re-resolves this module attribute on every
    call -- patching the module attribute (not `emb.get_lock`, which does
    not exist as a module-level name) is what actually intercepts it.
    """
    calls = {"n": 0}
    real_get_lock = file_locks.get_lock

    def spy(path):
        calls["n"] += 1
        return real_get_lock(path)

    monkeypatch.setattr(file_locks, "get_lock", spy)
    return calls


def _make_cache_with_broken_symlink(tmp_path, monkeypatch):
    """Build a fake HF-cache layout under tmp_path with ONE dangling
    `models--foo` symlink. Tries a real OS symlink first; Windows dev boxes
    frequently lack SeCreateSymbolicLinkPrivilege, so on any OSError this
    falls back to a plain placeholder file with os.path.islink/exists
    monkeypatched ONLY for that exact onnx path -- every other path check
    still hits the real filesystem, so quarantine/restore/commit logic is
    exercised against real directories regardless of symlink privilege.

    Returns (cache_dir: str, root: str) where `root` is the models--foo dir.
    """
    cache_dir = tmp_path / "cache"
    root = cache_dir / "models--foo"
    snap_dir = root / "snapshots" / "rev"
    snap_dir.mkdir(parents=True)
    onnx_path = snap_dir / "model.onnx"

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


def test_success_commits_and_deletes_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_is_windows_platform", lambda: True)
    lock_calls = _spy_get_lock(monkeypatch)
    cache_dir, root = _make_cache_with_broken_symlink(tmp_path, monkeypatch)

    sentinel = object()
    build_calls = {"n": 0}

    def fake_build():
        build_calls["n"] += 1
        return sentinel

    result = emb._windows_selfheal_and_build(cache_dir, "m", fake_build)

    assert result is sentinel
    assert build_calls["n"] == 1, "build_embedder should be called exactly once"
    assert lock_calls["n"] == 1, "critical section must run under get_lock"
    # The broken original dir is gone (renamed away), AND no quarantine
    # sibling is left behind either -- it was deleted only after the
    # rebuild proved successful.
    leftovers = list(tmp_path.joinpath("cache").glob("models--foo*"))
    assert leftovers == [], f"expected no models--foo* leftovers, found: {leftovers}"


def test_failure_restores_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_is_windows_platform", lambda: True)
    lock_calls = _spy_get_lock(monkeypatch)
    cache_dir, root = _make_cache_with_broken_symlink(tmp_path, monkeypatch)

    def fake_build():
        raise RuntimeError("simulated re-download failure")

    with pytest.raises(RuntimeError, match="simulated re-download failure"):
        emb._windows_selfheal_and_build(cache_dir, "m", fake_build)

    assert lock_calls["n"] == 1
    # The only known-good copy must be restored to its original path --
    # never left destroyed after a failed rebuild. Use os.listdir (not the
    # possibly-monkeypatched os.path.exists) for a genuine filesystem check.
    assert os.path.isdir(root), "original models--foo dir must be restored on failure"
    snap_dir = os.path.join(root, "snapshots", "rev")
    assert os.path.isdir(snap_dir)
    assert "model.onnx" in os.listdir(snap_dir), "quarantined data must not be lost"
    # No quarantine sibling left dangling after restore.
    leftovers = [
        p for p in tmp_path.joinpath("cache").glob("models--foo*")
        if p.name != "models--foo"
    ]
    assert leftovers == [], f"expected no leftover quarantine dirs, found: {leftovers}"


def test_no_broken_symlink_is_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_is_windows_platform", lambda: True)
    lock_calls = _spy_get_lock(monkeypatch)

    cache_dir = tmp_path / "clean_cache"
    snap_dir = cache_dir / "models--foo" / "snapshots" / "rev"
    snap_dir.mkdir(parents=True)
    (snap_dir / "model.onnx").write_bytes(b"real onnx bytes, not a symlink")

    sentinel = object()
    build_calls = {"n": 0}

    def fake_build():
        build_calls["n"] += 1
        return sentinel

    result = emb._windows_selfheal_and_build(str(cache_dir), "m", fake_build)

    assert result is sentinel
    assert build_calls["n"] == 1
    # Clean cache must be left completely untouched -- no rename occurred.
    assert os.path.isdir(str(cache_dir / "models--foo"))
    assert "model.onnx" in os.listdir(str(snap_dir))


def test_non_windows_is_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_is_windows_platform", lambda: False)
    lock_calls = _spy_get_lock(monkeypatch)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    sentinel = object()
    build_calls = {"n": 0}

    def fake_build():
        build_calls["n"] += 1
        return sentinel

    result = emb._windows_selfheal_and_build(str(cache_dir), "m", fake_build)

    assert result is sentinel
    assert build_calls["n"] == 1
    assert lock_calls["n"] == 0, "non-Windows fast path must not touch the lock at all"
