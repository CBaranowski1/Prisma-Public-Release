"""GRAY-BRN-06: forget-by-id + opt-in bounded eviction for a brain's learned
partition (learned.npy/learned.json).

`forget_learned(index_dir, note_id)` removes ONE note's paired vector row +
meta entry, keeping the pair aligned; it reuses 30-04's atomic ordered
(vector-first/meta-second) write and the same length self-heal used by
`BrainIndex._ensure_learned`.

`prune_learned(index_dir, keep)` is an opt-in (`brain_retention_enabled`,
default OFF), best-effort bounded cap on the same pair -- modeled on
`src/tool_output_store.py::_prune_store`'s oldest-first, exception-swallowing
shape (also the precedent used by 30-05's `MemoryManager.compact`).

Uses a fake embedder so these tests run identically regardless of fastembed
availability -- this file MUST NOT be gated behind
pytest.importorskip('fastembed') (30-RESEARCH.md Pitfall 6).
"""
import json

import numpy as np

import src.brain_index as bi


class _FakeEmbedder:
    """Deterministic, fixed-size fake embedder -- no fastembed required."""

    url = "fake://test"
    model = "fake-model"

    def encode(self, texts, normalize_embeddings=True):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _note(note_id: str) -> dict:
    return {"id": note_id, "category": "code", "title": f"Note {note_id}",
            "excerpt": f"excerpt {note_id}", "body": f"body text for {note_id}"}


def _seed(out_dir, ids):
    emb = _FakeEmbedder()
    for note_id in ids:
        bi.append_learned(out_dir, _note(note_id), embedder=emb)


def _assert_aligned(out_dir):
    """Alignment invariant: the learned partition's vector-row count must
    always equal its meta-entry count after any mutation."""
    arr = np.load(out_dir / "learned.npy")
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == len(doc["meta"])
    return arr, doc["meta"]


# ---------------------------------------------------------------------------
# forget_learned
# ---------------------------------------------------------------------------

def test_forget_learned_removes_paired_row_keeping_alignment(tmp_path):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2", "l3"])

    result = bi.forget_learned(out_dir, "l2")

    assert result is True
    arr, meta = _assert_aligned(out_dir)
    assert len(arr) == 2
    assert len(meta) == 2
    ids = [m["id"] for m in meta]
    assert "l2" not in ids
    assert ids == ["l1", "l3"]


def test_forget_learned_absent_id_returns_false_and_leaves_pair_unchanged(tmp_path):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2"])
    before_arr = np.load(out_dir / "learned.npy").copy()
    before_meta = json.loads((out_dir / "learned.json").read_text("utf-8"))

    result = bi.forget_learned(out_dir, "does-not-exist")

    assert result is False
    _assert_aligned(out_dir)
    after_arr = np.load(out_dir / "learned.npy")
    after_meta = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert np.array_equal(before_arr, after_arr)
    assert before_meta == after_meta


def test_forget_learned_missing_files_returns_false_safe_noop(tmp_path):
    out_dir = tmp_path / "index"  # never seeded -- no learned.npy/.json at all
    out_dir.mkdir(parents=True, exist_ok=True)

    result = bi.forget_learned(out_dir, "l1")

    assert result is False
    assert not (out_dir / "learned.npy").is_file()
    assert not (out_dir / "learned.json").is_file()


def test_forget_learned_self_heals_mismatched_pair_before_filtering(tmp_path):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2", "l3"])

    # Simulate a pre-existing crash-mid-write mismatch: learned.npy has an
    # extra uncommitted row beyond learned.json's meta (the ONLY direction
    # append_learned's ordering can produce -- vectors-ahead-of-meta).
    arr = np.load(out_dir / "learned.npy")
    extra = np.vstack([arr, arr[-1:]])
    np.save(out_dir / "learned.npy", extra)
    assert len(np.load(out_dir / "learned.npy")) == 4  # mismatched: 4 vectors, 3 meta

    result = bi.forget_learned(out_dir, "l1")

    assert result is True
    arr, meta = _assert_aligned(out_dir)
    # Self-heal truncates to the aligned 3-row prefix FIRST, then l1 is removed.
    assert len(meta) == 2
    ids = [m["id"] for m in meta]
    assert "l1" not in ids
    assert ids == ["l2", "l3"]


# ---------------------------------------------------------------------------
# prune_learned (opt-in, best-effort, bounded eviction)
# ---------------------------------------------------------------------------

def test_prune_learned_is_noop_when_flag_unset(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2", "l3", "l4", "l5"])
    before_arr = np.load(out_dir / "learned.npy").copy()
    before_meta = json.loads((out_dir / "learned.json").read_text("utf-8"))

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)

    dropped = bi.prune_learned(out_dir, keep=2)

    assert dropped == []
    after_arr = np.load(out_dir / "learned.npy")
    after_meta = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert np.array_equal(before_arr, after_arr)
    assert before_meta == after_meta


def test_prune_learned_is_noop_when_flag_explicitly_false(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2", "l3"])

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "brain_retention_enabled" else default,
    )

    dropped = bi.prune_learned(out_dir, keep=1)

    assert dropped == []
    _assert_aligned(out_dir)
    assert len(json.loads((out_dir / "learned.json").read_text("utf-8"))["meta"]) == 3


def test_prune_learned_flag_on_keeps_newest_drops_oldest(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2", "l3", "l4", "l5"])

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    dropped = bi.prune_learned(out_dir, keep=2)

    assert set(dropped) == {"l1", "l2", "l3"}
    arr, meta = _assert_aligned(out_dir)
    assert len(meta) == 2
    ids = [m["id"] for m in meta]
    assert ids == ["l4", "l5"]  # newest 2 survive, in original order


def test_prune_learned_under_cap_is_noop(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2"])

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    dropped = bi.prune_learned(out_dir, keep=5)

    assert dropped == []
    _assert_aligned(out_dir)


def test_prune_learned_missing_files_is_noop(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    out_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    dropped = bi.prune_learned(out_dir, keep=2)

    assert dropped == []
    assert not (out_dir / "learned.npy").is_file()


def test_prune_learned_never_raises_on_internal_error(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    _seed(out_dir, ["l1", "l2", "l3"])

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(bi, "atomic_write_npy", _boom)

    dropped = bi.prune_learned(out_dir, keep=1)

    assert dropped == []


def test_append_learned_opportunistically_prunes_when_flag_enabled(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    for note_id in ["l1", "l2", "l3"]:
        bi.append_learned(out_dir, _note(note_id), embedder=emb)

    # MAX_LEARNED_ENTRIES_PER_BRAIN default (5000) is far above 3 -- appending
    # 3 notes with the flag on must NOT trim anything, and must not deadlock
    # (RLock re-entrancy: append_learned's own get_lock is held while its
    # opportunistic prune_learned(d) call re-acquires the same lock).
    arr, meta = _assert_aligned(out_dir)
    assert len(meta) == 3


def test_append_learned_still_writes_normally_when_flag_disabled(tmp_path, monkeypatch):
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)

    bi.append_learned(out_dir, _note("l1"), embedder=emb)

    arr, meta = _assert_aligned(out_dir)
    assert len(meta) == 1
