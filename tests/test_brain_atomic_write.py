"""Tests for src/brain_index.py::append_learned's atomic ordered paired write
+ per-brain lock (GRAY-BRN-07/08, 30-04).

Uses a fake embedder so these tests run identically regardless of fastembed
availability -- this file MUST NOT be gated behind
pytest.importorskip('fastembed') (30-RESEARCH.md Pitfall 6).
"""
import concurrent.futures
import json

import numpy as np
import pytest

import src.brain_index as bi
import src.embeddings as embeddings_mod


class _FakeEmbedder:
    """Deterministic, fixed-size fake embedder -- no fastembed required."""

    url = "fake://test"
    model = "fake-model"

    def encode(self, texts, normalize_embeddings=True):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _note(note_id: str) -> dict:
    return {"id": note_id, "category": "code", "title": f"Note {note_id}",
            "excerpt": f"excerpt {note_id}", "body": f"body text for {note_id}"}


def test_append_learned_round_trip_readable_and_aligned(tmp_path):
    out_dir = tmp_path / "index"
    bi.append_learned(out_dir, _note("l1"), embedder=_FakeEmbedder())

    arr = np.load(out_dir / "learned.npy")
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == 1
    assert len(doc["meta"]) == 1
    assert doc["meta"][0]["id"] == "l1"


def test_append_learned_two_sequential_appends_yield_two_aligned_rows(tmp_path):
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    bi.append_learned(out_dir, _note("l1"), embedder=emb)
    bi.append_learned(out_dir, _note("l2"), embedder=emb)

    arr = np.load(out_dir / "learned.npy")
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == 2
    assert len(doc["meta"]) == 2
    assert doc["meta"][0]["id"] == "l1"
    assert doc["meta"][1]["id"] == "l2"


def test_crash_between_paired_writes_never_leaves_meta_ahead_or_partial(tmp_path, monkeypatch):
    """Monkeypatch the SECOND write (learned.json's atomic_write_json) to
    raise -- learned.npy commits first (vectors-ahead-of-meta), but
    learned.json must be left exactly as it was before this call: never
    meta-ahead, never a torn/partial file."""
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    bi.append_learned(out_dir, _note("l1"), embedder=emb)  # committed pair, 1 row

    meta_before = (out_dir / "learned.json").read_text("utf-8")

    def _raise_on_learned_json(path, data, **kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(bi, "atomic_write_json", _raise_on_learned_json)

    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        bi.append_learned(out_dir, _note("l2"), embedder=emb)

    # learned.npy committed the SECOND write's vector (vectors-ahead-of-meta);
    # learned.json is untouched -- byte-identical to before this call, never
    # meta-ahead and never a torn/partial file.
    arr = np.load(out_dir / "learned.npy")
    assert len(arr) == 2
    assert (out_dir / "learned.json").read_text("utf-8") == meta_before
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert len(doc["meta"]) == 1
    assert doc["meta"][0]["id"] == "l1"


def test_concurrent_append_learned_retains_all_notes(tmp_path):
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    n_writers = 20

    def write(i: int) -> None:
        bi.append_learned(out_dir, _note(f"n{i}"), embedder=emb)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_writers) as pool:
        list(pool.map(write, range(n_writers)))

    arr = np.load(out_dir / "learned.npy")
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == n_writers
    assert len(doc["meta"]) == n_writers
    ids = {m["id"] for m in doc["meta"]}
    assert ids == {f"n{i}" for i in range(n_writers)}


# ---------------------------------------------------------------------------
# Reload-side coverage (Task 3): the above tests inspect on-disk file state
# directly; these confirm the SAME guarantees hold when read back through a
# fresh BrainIndex (i.e. Task 1's atomic pair + Task 2's load-side self-heal
# working together end to end).
# ---------------------------------------------------------------------------

def _no_live_embedder(monkeypatch) -> None:
    """Force _check_embedder_identity's live-embedder lookup to return None,
    so these tests' fake-embedder-stamped identity never collides with a
    REAL fastembed client that happens to be importable in this dev shell
    (30-RESEARCH.md Assumptions Log A4: the target environment has no
    fastembed, so this is the representative/hermetic case)."""
    monkeypatch.setattr(embeddings_mod, "get_embedding_client", lambda **kwargs: None)


def test_append_learned_fake_embedder_round_trip_via_reload(tmp_path, monkeypatch):
    """Append two notes with a fake embedder, then reload a FRESH BrainIndex
    on the same dir -- both notes present and aligned."""
    _no_live_embedder(monkeypatch)
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    bi.append_learned(out_dir, _note("l1"), embedder=emb)
    bi.append_learned(out_dir, _note("l2"), embedder=emb)

    idx = bi.BrainIndex(out_dir)
    idx._ensure_learned()
    assert len(idx._learned_vecs) == 2
    assert len(idx._learned_meta) == 2
    assert {m["id"] for m in idx._learned_meta} == {"l1", "l2"}


def test_crash_mid_write_reload_self_heals_and_keeps_committed_notes(tmp_path, monkeypatch):
    """Monkeypatch learned.json's write to raise on a second append -- on
    reload, a fresh BrainIndex must self-heal to the aligned prefix (drop the
    uncommitted extra vector) while the previously committed note survives."""
    _no_live_embedder(monkeypatch)
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    bi.append_learned(out_dir, _note("l1"), embedder=emb)  # committed pair, 1 row

    def _raise(path, data, **kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(bi, "atomic_write_json", _raise)
    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        bi.append_learned(out_dir, _note("l2"), embedder=emb)

    idx = bi.BrainIndex(out_dir)
    idx._ensure_learned()
    assert len(idx._learned_vecs) == len(idx._learned_meta) == 1
    assert idx._learned_meta[0]["id"] == "l1"


def test_concurrent_append_learned_reload_shows_all_notes_aligned(tmp_path, monkeypatch):
    _no_live_embedder(monkeypatch)
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    n_writers = 20

    def write(i: int) -> None:
        bi.append_learned(out_dir, _note(f"n{i}"), embedder=emb)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_writers) as pool:
        list(pool.map(write, range(n_writers)))

    idx = bi.BrainIndex(out_dir)
    idx._ensure_learned()
    assert len(idx._learned_vecs) == n_writers
    assert len(idx._learned_meta) == n_writers
    assert {m["id"] for m in idx._learned_meta} == {f"n{i}" for i in range(n_writers)}
