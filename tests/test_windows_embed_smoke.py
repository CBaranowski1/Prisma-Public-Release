"""Phase 27 embed smoke — proves the HF symlink workaround does not break the
FastEmbed load/encode path.

WR-02: this used to be gated by BOTH `skipif(sys.platform != "win32")` AND
`importorskip("fastembed")`, so on any non-Windows CI *and* on any box without
fastembed it silently skipped — the phase's headline behavior got zero real
coverage while the suite still went green.

Now the meaningful assertion (FastEmbed actually loads and encodes with
HF_HUB_DISABLE_SYMLINKS=1) runs on EVERY platform whenever fastembed is
importable; it only skips — with an explicit, visible reason — when fastembed
is genuinely absent. The Windows-only piece (that the guard *detects* a UNC
path and sets the env var) stays platform-gated because that detection is
inherently Windows-specific.
"""
import importlib.util
import os
import sys

import pytest

from core.hf_env_guard import ensure_hf_symlinks_disabled_if_networked

# Explicit, visible skip reason (not a silent no-op) when the phase behavior
# genuinely cannot be exercised on this box.
_HAS_FASTEMBED = importlib.util.find_spec("fastembed") is not None


@pytest.mark.skipif(
    not _HAS_FASTEMBED,
    reason="fastembed not installed — embed load/encode smoke cannot run here "
    "(install fastembed to exercise the HF symlink workaround)",
)
def test_fastembed_loads_and_encodes_with_symlinks_disabled(tmp_path, monkeypatch):
    """Cross-platform headline: with HF_HUB_DISABLE_SYMLINKS=1 active,
    FastEmbedClient must still load its ONNX model and produce a real vector.
    Runs wherever fastembed is importable — Windows, Linux, or this dev box —
    so a regression in the symlink workaround / load path is actually caught.
    """
    from src.embeddings import FastEmbedClient

    # Force the workaround on (what the guard does on a network-backed dir),
    # so we prove loading succeeds WITH symlinks disabled — the exact condition
    # the phase ships for network/UNC data dirs.
    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))

    client = FastEmbedClient()
    vecs = client.encode(["hello world"])

    assert vecs.size > 0
    assert vecs.shape[0] == 1
    assert all(isinstance(x, float) for x in vecs[0].tolist())


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="UNC/mapped-drive network detection is Windows-specific; the "
    "cross-platform load/encode behavior is covered by the test above",
)
def test_guard_sets_symlink_env_on_windows_unc_path(monkeypatch):
    """Windows-only: the guard must detect a UNC-style network share and set
    HF_HUB_DISABLE_SYMLINKS so huggingface_hub copies instead of symlinking.
    """
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)

    ensure_hf_symlinks_disabled_if_networked("//simulated-network-share/data")

    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"
