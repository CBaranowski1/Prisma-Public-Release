"""Phase 37 automated re-encoding of Phase 30's deferred live smoke (30-07
Task 3), per
.planning/phases/30-brain-store-write-integrity/30-VERIFICATION.md's "Human
Verification Required" section.

Proves, automatically and hermetically, the same properties that plan's
`checkpoint:human-verify` deferred (SMOKE-01): a crash mid-write on the
brain's paired learned.npy/learned.json leaves vector<->note aligned on
reload with the committed note surviving and a WARNING logged; concurrent
same-process writers to BOTH the brain and memory stores drop no entries;
and opt-in retention (`brain_retention_enabled`) never evicts pinned
entries while default-OFF stays a byte-identical append-forever no-op.

This elevates above the existing hermetic `tests/test_phase30_integration.py`
(which proves the crash and concurrency claims only) by ALSO covering Task
3's third sub-property -- opt-in pinned-safe retention -- in one
self-contained assembled-store module, so no headline Phase 30 property is
left only human-verified.

Crash simulation: a real process kill (SIGKILL) mid-fsync cannot be
reproduced in-process. This module uses EXCEPTION INJECTION (monkeypatching
the second of `append_learned`'s two paired writes to raise) as a faithful
proxy -- it produces the exact same on-disk artifact a real kill would
(learned.npy committed, learned.json not yet written -- "vectors ahead of
meta") and exercises the exact same reload self-heal code path
(`BrainIndex._ensure_learned`). The real OS SIGKILL case is a documented,
accepted residual -- see the RESIDUALS block near the bottom of this file.

Hermetic: a module-local `_FakeEmbedder` (fixed-size, deterministic encode)
is passed via `append_learned`'s `embedder=` param so NO fastembed is
required -- this file must NOT be gated behind
pytest.importorskip('fastembed'). No live Chroma, DB, or network is touched.
"""
import concurrent.futures
import json
import logging

import numpy as np
import pytest

import src.brain_index as bi
from src.memory import MemoryManager


class _FakeEmbedder:
    """Deterministic, fixed-size fake embedder -- no fastembed required.

    Mirrors tests/test_phase30_integration.py's _FakeEmbedder exactly so
    this file's drives exercise the SAME production append_learned code
    path with the SAME embedder-identity shape.
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
# Crash-mid-write: vector<->note stays aligned + committed note survives +
# a WARNING is logged (30-07 Task 3 sub-property 1, GRAY-BRN-07).
# ---------------------------------------------------------------------------
def test_crash_mid_write_self_heals_aligned_survivor_and_warns(tmp_path, monkeypatch, caplog):
    out_dir = tmp_path / "index"
    emb = _FakeEmbedder()

    # Seed a committed learned note via the REAL append_learned.
    bi.append_learned(out_dir, _note("l1"), embedder=emb)

    # Simulate a crash BETWEEN the two paired writes: learned.npy commits
    # (vectors-ahead-of-meta), learned.json's atomic write raises -- the
    # exception-injection proxy for a real process kill (see module
    # docstring / RESIDUALS block).
    def _raise_on_meta_write(path, data, **kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(bi, "atomic_write_json", _raise_on_meta_write)

    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
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

    # Independently confirm the on-disk pair itself is torn as expected (not
    # only the in-memory reload view): vectors-ahead-of-meta.
    arr = np.load(out_dir / "learned.npy")
    doc = json.loads((out_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == 2, "the uncommitted vector row IS on disk (vectors-ahead-of-meta)"
    assert len(doc["meta"]) == 1, "learned.json is untouched by the raised write"


# ---------------------------------------------------------------------------
# Concurrent same-process writers to BOTH stores drop no entries and stay
# aligned (30-07 Task 3 sub-property 2, GRAY-BRN-08).
# ---------------------------------------------------------------------------
def test_concurrent_writers_both_stores_drop_no_entries_and_stay_aligned(tmp_path, monkeypatch):
    import src.embeddings as embeddings_mod
    monkeypatch.setattr(embeddings_mod, "get_embedding_client", lambda **kwargs: None)

    brain_dir = tmp_path / "index"
    mem_dir = tmp_path / "memdata"
    mem_dir.mkdir()
    emb = _FakeEmbedder()
    mgr = MemoryManager(str(mem_dir))

    n_brain_writers = 12
    n_mem_writers = 12

    def write_brain(i: int) -> None:
        bi.append_learned(brain_dir, _note(f"n{i}"), embedder=emb)

    def write_memory(i: int) -> None:
        mgr.add_and_save(f"memory-{i}", source="user", category="fact")

    # Interleave BOTH stores' writers in ONE pool so the in-process races
    # against each other's locks are actually exercised concurrently -- a
    # sequential "brain pool then memory pool" run would not prove the two
    # independent locks (core.file_locks per-brain lock vs MemoryManager
    # RLock) coexist safely under real thread interleaving.
    total_writers = n_brain_writers + n_mem_writers
    with concurrent.futures.ThreadPoolExecutor(max_workers=total_writers) as pool:
        futures = (
            [pool.submit(write_brain, i) for i in range(n_brain_writers)]
            + [pool.submit(write_memory, i) for i in range(n_mem_writers)]
        )
        # Bounded wait on every future: a deadlock/hang regression must fail
        # LOUD, never wedge the suite.
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

    # Reload through a fresh BrainIndex too (crash-mid-write self-heal +
    # concurrent-writer safety working together end to end, not just raw
    # files).
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


# ---------------------------------------------------------------------------
# Opt-in retention: default OFF is a byte-identical no-op; enabled, eviction
# occurs but pinned entries are NEVER evicted (30-07 Task 3 sub-property 3,
# GRAY-BRN-06).
# ---------------------------------------------------------------------------
def test_retention_default_off_is_byte_identical_noop(tmp_path, monkeypatch):
    # Memory side: MemoryManager.compact.
    mem_dir = tmp_path / "memdata"
    mem_dir.mkdir()
    mgr = MemoryManager(str(mem_dir))
    mem_entries = [
        {"id": f"m{i}", "text": f"memory-{i}", "timestamp": i, "source": "user",
         "category": "fact", "uses": 0, "pinned": False}
        for i in range(10)
    ]
    mgr.save(mem_entries)
    mem_bytes_before = (mem_dir / "memory.json").read_bytes()

    # Brain side: prune_learned.
    brain_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    for note_id in ["l1", "l2", "l3", "l4", "l5"]:
        bi.append_learned(brain_dir, _note(note_id), embedder=emb)
    brain_npy_before = (brain_dir / "learned.npy").read_bytes()
    brain_json_before = (brain_dir / "learned.json").read_bytes()

    # Default OFF (flag absent from settings entirely).
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)

    evicted = mgr.compact(keep=3)
    dropped = bi.prune_learned(brain_dir, keep=2)

    assert evicted == [], "compact must be a strict no-op when brain_retention_enabled is unset"
    assert dropped == [], "prune_learned must be a strict no-op when brain_retention_enabled is unset"
    assert (mem_dir / "memory.json").read_bytes() == mem_bytes_before, (
        "memory.json must be byte-identical (append-forever) when retention is OFF"
    )
    assert (brain_dir / "learned.npy").read_bytes() == brain_npy_before, (
        "learned.npy must be byte-identical (append-forever) when retention is OFF"
    )
    assert (brain_dir / "learned.json").read_bytes() == brain_json_before, (
        "learned.json must be byte-identical (append-forever) when retention is OFF"
    )


def test_retention_enabled_evicts_over_cap_but_never_evicts_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    # Memory side: pinned entries must survive eviction even when the store
    # is over the cap; only the oldest UNPINNED entries are dropped.
    mem_dir = tmp_path / "memdata"
    mem_dir.mkdir()
    mgr = MemoryManager(str(mem_dir))
    pinned = [
        {"id": f"pin-{i}", "text": f"pinned-{i}", "timestamp": i, "source": "user",
         "category": "fact", "uses": 0, "pinned": True}
        for i in range(2)
    ]
    unpinned = [
        {"id": f"un-{i}", "text": f"unpinned-{i}", "timestamp": 100 + i, "source": "user",
         "category": "fact", "uses": 0, "pinned": False}
        for i in range(8)
    ]
    mgr.save(pinned + unpinned)

    evicted = mgr.compact(keep=5)

    assert len(evicted) == 5, f"expected 5 unpinned evictions (10 total, keep=5, 2 pinned), got {evicted}"
    on_disk = mgr.load_all()
    on_disk_ids = {e["id"] for e in on_disk}
    for p in pinned:
        assert p["id"] in on_disk_ids, f"pinned entry {p['id']} was evicted -- pinned-safe guarantee violated"
    assert on_disk_ids.isdisjoint(set(evicted))
    assert set(evicted) == {"un-0", "un-1", "un-2", "un-3", "un-4"}, (
        "eviction must drop the OLDEST unpinned entries first"
    )

    # Brain side: the learned partition has no pinned concept (strictly
    # append-ordered notes) -- prune_learned's cap still drops the oldest
    # and keeps the newest, exercising the same opt-in flag path.
    brain_dir = tmp_path / "index"
    emb = _FakeEmbedder()
    for note_id in ["l1", "l2", "l3", "l4", "l5"]:
        bi.append_learned(brain_dir, _note(note_id), embedder=emb)

    dropped = bi.prune_learned(brain_dir, keep=2)

    assert set(dropped) == {"l1", "l2", "l3"}
    arr = np.load(brain_dir / "learned.npy")
    doc = json.loads((brain_dir / "learned.json").read_text("utf-8"))
    assert len(arr) == len(doc["meta"]) == 2, "brain partition must stay vector<->note aligned after eviction"
    assert [m["id"] for m in doc["meta"]] == ["l4", "l5"], "newest entries survive, oldest dropped"


# ---------------------------------------------------------------------------
# RESIDUALS (documented, not tested-as-fixed; 30-07 Task 3's non-blocking
# deferral, closed out here per 37-CONTEXT.md SMOKE-01):
#
#   1. A real OS process SIGKILL mid-fsync is NOT reproduced by this suite.
#      Exception injection (see the crash test above and the module
#      docstring) is the faithful in-process proxy: it produces the
#      identical on-disk artifact ("vectors ahead of meta") and drives the
#      identical self-heal code path (BrainIndex._ensure_learned /
#      _self_heal_learned_pair) a real kill -9 mid-write would. A true
#      process kill on a live machine remains unautomatable in a hermetic
#      test session; this is an accepted, non-blocking residual (matches
#      this same milestone's established Phase 28 (28-06)/Phase 29 (29-04)
#      precedent).
#
#   2. The cross-process mcp_servers/memory_server.py subprocess race
#      against memory.json (30-RESEARCH.md Pitfall 4 / Open Question 1) is
#      explicitly OUT OF SCOPE. This suite (like
#      tests/test_phase30_integration.py before it) proves the IN-PROCESS
#      race only -- core.file_locks and MemoryManager._lock are
#      process-local locks and do not serialize against a separate OS
#      process opening the same memory.json/brain index files.
# ---------------------------------------------------------------------------
