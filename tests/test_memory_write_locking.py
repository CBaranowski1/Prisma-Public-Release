"""GRAY-BRN-08: MemoryManager's guarded funnel methods must not lose updates
under concurrent same-process writers.

Models tests/test_api_key_manager_concurrent_save.py's ThreadPoolExecutor
N-writer pattern: N distinct writes performed concurrently via
add_and_save() must all persist -- a lock scoped only to save() would still
lose entries here, because the race is between one caller's load_all() and
another caller's save() (30-RESEARCH.md Pitfall 1), not inside save() itself.

Fully offline: MemoryManager only touches memory.json on disk; no Chroma, no
fastembed, no network.
"""
import concurrent.futures

from src.memory import MemoryManager

N_WRITERS = 50


def test_concurrent_add_and_save_retains_all_entries(tmp_path):
    mgr = MemoryManager(str(tmp_path))

    def write(idx: int):
        return mgr.add_and_save(f"memory-{idx}", source="user", category="fact")

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        results = list(pool.map(write, range(N_WRITERS)))

    assert len(results) == N_WRITERS

    stored = mgr.load_all()
    texts = {e["text"] for e in stored}
    expected = {f"memory-{i}" for i in range(N_WRITERS)}
    missing = expected - texts
    assert not missing, f"Concurrent add_and_save lost {len(missing)}/{N_WRITERS} entries: {missing}"
    assert len(stored) == N_WRITERS, (
        f"Expected exactly {N_WRITERS} entries, got {len(stored)} "
        "(duplicate or lost-update corruption)"
    )
    # All ids must be unique -- no entry silently overwrote another's id slot.
    ids = [e["id"] for e in stored]
    assert len(ids) == len(set(ids))


def test_concurrent_mixed_add_and_delete_stays_consistent(tmp_path):
    mgr = MemoryManager(str(tmp_path))

    # Seed N entries first (sequentially, so we know their ids for deletion).
    seeded = [mgr.add_and_save(f"seed-{i}") for i in range(N_WRITERS)]
    to_delete = [e["id"] for e in seeded[: N_WRITERS // 2]]

    def add_op(idx: int):
        mgr.add_and_save(f"added-{idx}")

    def delete_op(memory_id: str):
        mgr.delete_by_id(memory_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        futures = []
        for i in range(N_WRITERS // 2):
            futures.append(pool.submit(add_op, i))
        for memory_id in to_delete:
            futures.append(pool.submit(delete_op, memory_id))
        for f in futures:
            f.result()

    stored = mgr.load_all()
    ids = [e["id"] for e in stored]
    # No duplicate ids -- internal consistency held under interleaved
    # add/delete traffic.
    assert len(ids) == len(set(ids))
    # Count matches: N seeded - N/2 deleted + N/2 added == N.
    assert len(stored) == N_WRITERS, (
        f"Expected {N_WRITERS} surviving entries after mixed concurrent "
        f"add/delete, got {len(stored)}"
    )
    # None of the deleted ids survived.
    surviving_ids = set(ids)
    assert not (set(to_delete) & surviving_ids), "A deleted entry survived the concurrent batch"
