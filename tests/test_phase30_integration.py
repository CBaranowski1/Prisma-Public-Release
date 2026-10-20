"""Phase 30 integration-checker connection (SC-5).

Proves the phase's headline write-integrity behaviors on the paths the app
actually assembles -- not only on the literally-named helpers -- converting
GRAY-BRN-06/07/08 from "presence" (per-file unit tests in
tests/test_brain_atomic_write.py and tests/test_memory_write_locking.py) to
"integration": one hermetic checker that drives BOTH real production stores
(brain + memory) at once, mirroring tests/test_phase28_integration.py's and
tests/test_phase29_integration.py's SC-5 discipline for this phase:

  - claim (a): a simulated crash BETWEEN the two paired brain writes (the
    learned.json write is monkeypatched to raise) leaves vector<->note
    ALIGNED on a fresh BrainIndex reload -- the previously committed note
    survives, the uncommitted extra vector self-heals away, and a WARNING is
    logged (GRAY-BRN-07, exercising 30-04's atomic ordered pair + load-side
    self-heal);

  - claim (b): N concurrent SAME-PROCESS writers -- threads calling
    append_learned into one brain AND threads calling
    MemoryManager.add_and_save into one memory store, running concurrently
    against each other -- drop no entries in either store, and the brain
    stays vector<->note aligned (GRAY-BRN-08, exercising 30-04's per-brain
    file lock + 30-03's MemoryManager RLock funnel).

Hermetic: a module-local `_FakeEmbedder` (fixed-size, deterministic encode)
is passed via `append_learned`'s `embedder=` param so NO fastembed is
required -- this file must NOT be gated behind
pytest.importorskip('fastembed') (30-RESEARCH.md Pitfall 6, mirrors
tests/test_brain_atomic_write.py). No live Chroma, DB, or network is
touched. The concurrency drive is bounded via `Future.result(timeout=15)` --
this phase's writers are synchronous (thread-based, not asyncio), so the
"outer asyncio.wait_for ceiling" convention from Phase 28/29 is expressed
here as an equivalent bounded-wait on every future so a deadlock/hang
regression fails LOUD instead of wedging the suite.

OUT OF SC-5 SCOPE (documented, not tested-as-fixed): the cross-process
mcp_servers/memory_server.py subprocess race against memory.json
(30-RESEARCH.md Pitfall 4 / Open Question 1). This checker proves the
IN-PROCESS race only, matching this plan's `must_haves.prohibitions`.

The live on-machine confirmation (a real server killed mid brain-learn
write, real concurrent chat sessions remembering facts at once, and the
opt-in retention cap keeping pinned memories) is proven by this plan's
Task 3 human smoke, not by this hermetic suite.
"""
import concurrent.futures
import json
import logging

import numpy as np

import src.brain_index as bi
from src.memory import MemoryManager


class _FakeEmbedder:
    """Deterministic, fixed-size fake embedder -- no fastembed required.

    Mirrors tests/test_brain_atomic_write.py's _FakeEmbedder exactly so this
    checker's crash/concurrency drives exercise the SAME production
    append_learned code path with the SAME embedder-identity shape.
    """

    url = "fake://test"
    model = "fake-model"

    def encode(self, texts, normalize_embeddings=True):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _note(note_id: str) -> dict:
    return {
        "id": note_id,
        "category": "code",
        "title": f"Note {note_id}",
        "excerpt": f"excerpt {note_id}",
        "body": f"body text for {note_id}",
    }


# ---------------------------------------------------------------------------
# Claim (a) -- crash-mid-write: vector<->note stays aligned on reload
# (GRAY-BRN-07).
# ---------------------------------------------------------------------------
def test_crash_mid_write_reload_aligned_and_survivor_sc5(tmp_path, monkeypatch, caplog):
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()

    # Seed a committed learned note via the REAL append_learned.
    bi.append_learned(out_dir, _note("l1"), embedder=emb)

    # Simulate a crash BETWEEN the two paired writes: learned.npy commits
    # (vectors-ahead-of-meta), learned.json's atomic write raises.
    def _raise_on_meta_write(path, data, **kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(bi, "atomic_write_json", _raise_on_meta_write)

    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="simulated crash mid-write"):
        bi.append_learned(out_dir, _note("l2"), embedder=emb)

    # No live fastembed lookup should interfere with this fake-embedder-
    # stamped identity on reload.
    import src.embeddings as embeddings_mod
    monkeypatch.setattr(embeddings_mod, "get_embedding_client", lambda **kwargs: None)

    # Fresh BrainIndex reload (a real process restart, hermetically): must
    # self-heal to the aligned prefix and keep the committed note.
    idx = bi.BrainIndex(out_dir)
    with caplog.at_level(logging.WARNING):
        idx._ensure_learned()

    assert len(idx._learned_vecs) == len(idx._learned_meta) == 1, (
        "crash-mid-write reload must be aligned (len(vectors) == len(meta)), "
        f"got vectors={len(idx._learned_vecs)} meta={len(idx._learned_meta)}"
    )
    assert idx._learned_meta[0]["id"] == "l1", "the previously committed note must survive"
    assert any("length mismatch" in r.message.lower() for r in caplog.records), (
        "a torn write must emit a WARNING naming the length mismatch"
    )

    # Independently confirm the on-disk pair itself is aligned (not only the
    # in-memory reload view).
    arr = np.load(out_dir / "learned.npy")
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == 2, "the uncommitted vector row IS on disk (vectors-ahead-of-meta)"
    assert len(doc["meta"]) == 1, "learned.json is untouched by the raised write"


# ---------------------------------------------------------------------------
# Claim (b) -- concurrent same-process writers to BOTH stores drop no
# entries (GRAY-BRN-08).
# ---------------------------------------------------------------------------
def test_concurrent_writers_both_stores_drop_no_entries_sc5(tmp_path, monkeypatch):
    import src.embeddings as embeddings_mod
    monkeypatch.setattr(embeddings_mod, "get_embedding_client", lambda **kwargs: None)

    brain_dir = tmp_path / "index"
    mem_dir = tmp_path / "memdata"
    mem_dir.mkdir()
    emb = _FakeEmbedder()
    mgr = MemoryManager(str(mem_dir))

    n_brain_writers = 20
    n_mem_writers = 20

    def write_brain(i: int) -> None:
        bi.append_learned(brain_dir, _note(f"n{i}"), embedder=emb)

    def write_memory(i: int) -> None:
        mgr.add_and_save(f"memory-{i}", source="user", category="fact")

    # Interleave BOTH stores' writers in ONE pool so the in-process races
    # against each other's locks are actually exercised concurrently (a
    # sequential "brain pool then memory pool" run would not prove the two
    # independent locks (core.file_locks per-brain lock vs MemoryManager
    # RLock) coexist safely under real thread interleaving).
    total_writers = n_brain_writers + n_mem_writers
    # Outer ceiling: a deadlock/hang regression must fail LOUD, never wedge CI.
    with concurrent.futures.ThreadPoolExecutor(max_workers=total_writers) as pool:
        futures = (
            [pool.submit(write_brain, i) for i in range(n_brain_writers)]
            + [pool.submit(write_memory, i) for i in range(n_mem_writers)]
        )
        for f in futures:
            f.result(timeout=15)

    # Brain store: all N notes present, vector<->note aligned.
    arr = np.load(brain_dir / "learned.npy")
    doc = json.loads((brain_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == len(doc["meta"]) == n_brain_writers, (
        f"brain store must retain all {n_brain_writers} entries aligned, "
        f"got vectors={len(arr)} meta={len(doc['meta'])}"
    )
    brain_ids = {m["id"] for m in doc["meta"]}
    assert brain_ids == {f"n{i}" for i in range(n_brain_writers)}, (
        f"brain store lost entries under concurrency: missing "
        f"{({f'n{i}' for i in range(n_brain_writers)} - brain_ids)}"
    )

    # Reload through a fresh BrainIndex too (Task 1's atomic pair + Task 2's
    # load-side self-heal working together end to end, not just raw files).
    idx = bi.BrainIndex(brain_dir)
    idx._ensure_learned()
    assert len(idx._learned_vecs) == len(idx._learned_meta) == n_brain_writers

    # Memory store: all N entries present, no lost update.
    stored = mgr.load_all()
    assert len(stored) == n_mem_writers, (
        f"memory store must retain all {n_mem_writers} entries, got {len(stored)} "
        "(lost-update corruption)"
    )
    mem_texts = {e["text"] for e in stored}
    assert mem_texts == {f"memory-{i}" for i in range(n_mem_writers)}
    ids = [e["id"] for e in stored]
    assert len(ids) == len(set(ids)), "duplicate memory ids -- a race overwrote another entry's slot"

    # NOTE (out of SC-5 scope, T-30-xproc/accepted): this proves the
    # SAME-PROCESS race only. The separate mcp_servers/memory_server.py
    # subprocess opening its own MemoryManager on the same memory.json file
    # is a cross-process writer NOT serialized by this in-process RLock --
    # that gap is a documented, accepted residual limitation, not something
    # this (or any) hermetic in-process test can assert closed.
