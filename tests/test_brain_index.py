"""Tests for src/brain_index.py — hermetic fixture-brain build + search.

Per-test fastembed skip (D-10, BRAIN-04): tests that build/search a real
index need fastembed and SKIP individually with a visible reason when it's
absent. This is a PER-TEST skipif (not a module-level importorskip) so that
the GRAY-BRN-07 length-mismatch self-heal tests below -- which write
learned.npy/learned.json directly and need no embedder at all -- always run,
independent of fastembed availability (30-RESEARCH.md Pitfall 6).
"""
import json
import logging

import pytest

try:
    import fastembed  # noqa: F401
    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False

requires_fastembed = pytest.mark.skipif(
    not _HAS_FASTEMBED, reason="brain recall requires fastembed; pip install fastembed")

import src.brain_index as bi


def _fixture_brain(tmp_path):
    vault = tmp_path / "vault"
    (vault / "knowledge" / "code").mkdir(parents=True)
    (vault / "knowledge" / "code" / "short-note.md").write_text(
        "---\ntitle: Short Note\n---\n\nA short reusable fact about retries.\n",
        encoding="utf-8")
    (vault / "knowledge" / "code" / "long-note.md").write_text(
        "---\ntitle: Long Note\n---\n\n" + ("Detailed idempotent retry design. " * 40),
        encoding="utf-8")
    return vault


@requires_fastembed
def test_fastembed_importable_or_skips():
    import fastembed
    assert hasattr(fastembed, "TextEmbedding")


@requires_fastembed
def test_fixture_index_search_returns_hits(tmp_path):
    vault = _fixture_brain(tmp_path)
    out_dir = tmp_path / "index"
    bi.build_index(vault, out_dir)
    idx = bi.BrainIndex(out_dir)
    hits = idx.search("idempotent retry design", top_k=3)
    assert len(hits) >= 1


@requires_fastembed
def test_build_index_stamps_embedder_identity(tmp_path):
    """GRAY-BRN-01 fresh-stamp: a freshly-built meta.json carries a populated
    'embedder' block sourced from the live embedding client."""
    vault = _fixture_brain(tmp_path)
    out_dir = tmp_path / "index"
    bi.build_index(vault, out_dir)
    doc = json.loads((out_dir / "meta.json").read_text("utf-8"))
    assert "embedder" in doc
    embedder = doc["embedder"]
    assert isinstance(embedder, dict)
    assert "backend" in embedder
    assert embedder["backend"] == "fastembed"


@requires_fastembed
def test_legacy_meta_json_is_verified_and_stamped(tmp_path, caplog):
    """A legacy index is stamped only after its stored vectors reproduce."""
    vault = _fixture_brain(tmp_path)
    out_dir = tmp_path / "index"
    bi.build_index(vault, out_dir)
    doc = json.loads((out_dir / "meta.json").read_text("utf-8"))
    legacy_doc = {"dim": doc["dim"], "meta": doc["meta"]}
    assert "embedder" not in legacy_doc
    (out_dir / "meta.json").write_text(json.dumps(legacy_doc), encoding="utf-8")

    idx = bi.BrainIndex(out_dir)
    with caplog.at_level(logging.WARNING):
        hits = idx.search("idempotent retry design", top_k=3)
    assert len(hits) >= 1
    migrated = json.loads((out_dir / "meta.json").read_text("utf-8"))
    assert migrated["embedder"]["backend"] == "fastembed"
    assert not any(
        "no embedder identity stamp" in r.message.lower()
        for r in caplog.records
    )


@requires_fastembed
def test_legacy_meta_with_unverifiable_vectors_requires_reindex(tmp_path):
    """Same-width vectors from an unknown space must never be auto-stamped."""
    import numpy as np

    vault = _fixture_brain(tmp_path)
    out_dir = tmp_path / "index"
    bi.build_index(vault, out_dir)
    doc = json.loads((out_dir / "meta.json").read_text("utf-8"))
    (out_dir / "meta.json").write_text(
        json.dumps({"dim": doc["dim"], "meta": doc["meta"]}),
        encoding="utf-8",
    )
    vectors = np.load(out_dir / "vectors.npy")
    np.save(out_dir / "vectors.npy", -vectors)

    with pytest.raises(RuntimeError, match="reindex needed:"):
        bi.BrainIndex(out_dir).search("idempotent retry design", top_k=3)


@requires_fastembed
def test_mismatched_embedder_raises_with_reindex_message(tmp_path):
    """GRAY-BRN-01 mismatch-raise: a meta.json whose stored embedder identity
    genuinely differs from the live one must RAISE RuntimeError with an
    actionable 'reindex needed:' message — no auto-rebuild."""
    vault = _fixture_brain(tmp_path)
    out_dir = tmp_path / "index"
    bi.build_index(vault, out_dir)
    doc = json.loads((out_dir / "meta.json").read_text("utf-8"))
    doc["embedder"] = {"backend": "http", "model": "other-model", "url": "http://elsewhere"}
    (out_dir / "meta.json").write_text(json.dumps(doc), encoding="utf-8")

    idx = bi.BrainIndex(out_dir)
    with pytest.raises(RuntimeError, match="reindex needed:"):
        idx.search("idempotent retry design", top_k=3)


# ---------------------------------------------------------------------------
# WR-02: the `learned` partition (learned.npy/learned.json) must carry and
# validate the SAME embedder-identity three-way compare as the main index --
# previously it carried no identity at all, and a mismatched-dim or
# mismatched-model learned partition would crash/silently corrupt `search`.
# ---------------------------------------------------------------------------

@requires_fastembed
def test_append_learned_stamps_embedder_identity(tmp_path):
    """A freshly-appended learned note stamps learned.json with a populated
    'embedder' block, mirroring build_index's meta.json stamp (WR-02)."""
    out_dir = tmp_path / "index"
    bi.append_learned(out_dir, {"id": "l1", "category": "code", "title": "Learned Fact",
                                 "excerpt": "a learned fact", "body": "a learned fact about retries"})
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert "embedder" in doc
    embedder = doc["embedder"]
    assert isinstance(embedder, dict)
    assert "backend" in embedder
    assert embedder["backend"] == "fastembed"
    assert doc["meta"][0]["id"] == "l1"


@requires_fastembed
def test_legacy_learned_json_bare_list_warns_and_accepts(tmp_path, caplog):
    """The pre-WR-02 on-disk shape for learned.json is a bare list (no
    embedder identity, no wrapping dict). Loading it must WARN once and
    ACCEPT (never raise), matching the main index's legacy-accept behavior."""
    vault = _fixture_brain(tmp_path)
    out_dir = tmp_path / "index"
    bi.build_index(vault, out_dir)

    legacy_learned = [{"id": "l1", "category": "code", "title": "Learned Fact",
                        "excerpt": "a learned fact", "body": "a learned fact about retries"}]
    (out_dir / "learned.json").write_text(json.dumps(legacy_learned), encoding="utf-8")
    import numpy as np
    main_vecs = np.load(out_dir / "vectors.npy")
    np.save(out_dir / "learned.npy", main_vecs[:1])  # dim-compatible dummy vector

    idx = bi.BrainIndex(out_dir)
    with caplog.at_level(logging.WARNING):
        hits = idx.search("idempotent retry design", top_k=3)
    assert len(hits) >= 1
    assert any("legacy" in r.message.lower() and "learned partition" in r.message.lower()
               for r in caplog.records)


@requires_fastembed
def test_mismatched_learned_embedder_raises_with_remedy_message(tmp_path):
    """WR-02 mismatch-raise: a learned.json whose stored embedder identity
    genuinely differs from the live one must RAISE RuntimeError with an
    actionable remedy -- the same protection the main index already had,
    now extended to the learned partition (the GRAY-BRN-01 coverage hole).
    Without this fix, a same-dim/different-model learned partition would
    silently combine into `search`'s np.vstack and return wrong scores; a
    different-dim one would crash with an unhandled ValueError."""
    out_dir = tmp_path / "index"
    bi.append_learned(out_dir, {"id": "l1", "category": "code", "title": "Learned Fact",
                                 "excerpt": "a learned fact", "body": "a learned fact about retries"})
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    doc["embedder"] = {"backend": "http", "model": "other-model", "url": "http://elsewhere"}
    (out_dir / "learned.json").write_text(json.dumps(doc), encoding="utf-8")

    idx = bi.BrainIndex(out_dir)
    with pytest.raises(RuntimeError, match="learned partition"):
        idx.search("a learned fact", top_k=3)


# ---------------------------------------------------------------------------
# GRAY-BRN-07 (30-04): BrainIndex._ensure_learned load-side length self-heal.
# A crash between append_learned's two paired writes (learned.npy committed,
# learned.json not yet, or vice versa) can leave len(vectors) != len(meta).
# These tests write the mismatched pair DIRECTLY (numpy + json, no embedder
# involved) so they run even without fastembed -- NOT gated by
# requires_fastembed / pytest.importorskip("fastembed").
# ---------------------------------------------------------------------------

def test_learned_length_mismatch_self_heals(tmp_path, caplog):
    """A learned.npy with 3 rows but learned.json with only 2 meta entries
    (vectors-ahead-of-meta, the shape a crash between append_learned's two
    paired writes always leaves) must self-heal to the aligned prefix (2)
    and emit a WARNING naming both counts -- never serve a misaligned pair."""
    import numpy as np
    out_dir = tmp_path / "index"
    out_dir.mkdir(parents=True)
    dim = 4
    arr = np.zeros((3, dim), dtype=np.float32)
    np.save(out_dir / "learned.npy", arr)
    meta = [
        {"id": "l1", "category": "code", "title": "Note 1", "excerpt": "", "body": ""},
        {"id": "l2", "category": "code", "title": "Note 2", "excerpt": "", "body": ""},
    ]
    (out_dir / "learned.json").write_text(
        json.dumps({"dim": dim, "embedder": None, "meta": meta}), encoding="utf-8")

    idx = bi.BrainIndex(out_dir)
    with caplog.at_level(logging.WARNING):
        idx._ensure_learned()

    assert len(idx._learned_vecs) == 2
    assert len(idx._learned_meta) == 2
    assert idx._learned_meta[0]["id"] == "l1"
    assert idx._learned_meta[1]["id"] == "l2"
    assert any("length mismatch" in r.message.lower() for r in caplog.records)


def test_learned_equal_length_pair_loads_fully_no_mismatch_warning(tmp_path, caplog):
    """An aligned pair (len(vectors) == len(meta)) loads both fully and never
    emits a length-mismatch warning."""
    import numpy as np
    out_dir = tmp_path / "index"
    out_dir.mkdir(parents=True)
    dim = 4
    arr = np.zeros((2, dim), dtype=np.float32)
    np.save(out_dir / "learned.npy", arr)
    meta = [
        {"id": "l1", "category": "code", "title": "Note 1", "excerpt": "", "body": ""},
        {"id": "l2", "category": "code", "title": "Note 2", "excerpt": "", "body": ""},
    ]
    (out_dir / "learned.json").write_text(
        json.dumps({"dim": dim, "embedder": None, "meta": meta}), encoding="utf-8")

    idx = bi.BrainIndex(out_dir)
    with caplog.at_level(logging.WARNING):
        idx._ensure_learned()

    assert len(idx._learned_vecs) == 2
    assert len(idx._learned_meta) == 2
    assert not any("length mismatch" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 33-07 (captured item): build_index's vectors.npy/meta.json writes now route
# through atomic_write_npy/atomic_write_json instead of a non-atomic
# np.save(path-string)/.write_text -- out_dir is the exact live directory
# PrecisionVaultProvider.recall reads at runtime, so a crash mid-write during
# a manual re-index must never leave a truncated/corrupt vectors.npy there.
# Hermetic: a fake embedder (mirrors tests/test_brain_atomic_write.py) means
# this test runs regardless of fastembed availability -- NOT gated by
# requires_fastembed.
# ---------------------------------------------------------------------------

class _FakeEmbedder:
    """Deterministic, fixed-size fake embedder -- no fastembed required."""

    url = "fake://test"
    model = "fake-model"

    def encode(self, texts, normalize_embeddings=True):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def test_build_index_atomic_write_survives_crash_mid_write(tmp_path, monkeypatch):
    """build_index routes vectors.npy/meta.json through atomic_write_npy/
    atomic_write_json (temp-file + fsync + os.replace). Simulate a crash at
    the exact commit point (os.replace raises AFTER the temp file has been
    fully written+fsynced) and confirm the previously-committed vectors.npy/
    meta.json at out_dir survive byte-for-byte -- an interrupted rebuild can
    never leave a truncated/partial file at the path recall reads."""
    vault = _fixture_brain(tmp_path)
    out_dir = tmp_path / "index"

    import src.embeddings as embeddings_mod
    monkeypatch.setattr(embeddings_mod, "get_embedding_client", lambda **kwargs: _FakeEmbedder())

    # Baseline: a successful build establishes a valid, committed pair.
    note_count, dim = bi.build_index(vault, out_dir)
    assert note_count == 2
    baseline_vectors = (out_dir / "vectors.npy").read_bytes()
    baseline_meta = (out_dir / "meta.json").read_text("utf-8")

    # Simulate a crash mid-write: os.replace (the atomic commit step inside
    # atomic_write_npy/atomic_write_json) raises. The temp file has already
    # been fully written+fsynced by this point -- only the final rename is
    # interrupted -- so the live path must be left completely untouched.
    def _raise_on_replace(src, dst):
        raise OSError("simulated crash mid-write (before os.replace commit)")

    monkeypatch.setattr("os.replace", _raise_on_replace)

    with pytest.raises(OSError, match="simulated crash mid-write"):
        bi.build_index(vault, out_dir)

    assert (out_dir / "vectors.npy").read_bytes() == baseline_vectors, (
        "a crash during os.replace must never leave a truncated/partial vectors.npy"
    )
    assert (out_dir / "meta.json").read_text("utf-8") == baseline_meta, (
        "a crash during os.replace must never leave a truncated/partial meta.json"
    )
