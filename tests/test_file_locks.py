"""Tests for ``core.file_locks``'s per-canonical-path RLock registry (GRAY-BRN-08).

``get_lock(path)`` must return the SAME ``threading.RLock`` object for the
same canonical (resolved) path, however it is spelled (relative vs absolute),
and distinct lock objects for distinct paths. It must never mint a duplicate
lock for one canonical path under concurrent access, and the returned object
must be re-entrant (a thread may acquire it twice without deadlocking).

Offline, no external deps — stdlib threading + concurrent.futures only.
"""
import concurrent.futures
import os
import threading

from core.file_locks import get_lock

N_WRITERS = 20


def test_get_lock_same_path_different_spellings_returns_identical_object(tmp_path):
    target = tmp_path / "a" / "b.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")

    abs_spelling = str(target.resolve())
    rel_style = os.path.join(str(target.parent), "..", target.parent.name, target.name)

    lock1 = get_lock(abs_spelling)
    lock2 = get_lock(rel_style)

    assert lock1 is lock2


def test_get_lock_distinct_paths_returns_distinct_objects(tmp_path):
    path_a = tmp_path / "learned_a.json"
    path_b = tmp_path / "learned_b.json"

    lock_a = get_lock(str(path_a))
    lock_b = get_lock(str(path_b))

    assert lock_a is not lock_b


def test_get_lock_returns_reentrant_rlock(tmp_path):
    path = tmp_path / "reentrant.json"
    lock = get_lock(str(path))

    assert isinstance(lock, type(threading.RLock()))

    # Re-entrant: acquiring twice from the SAME thread must not deadlock.
    acquired_twice = False
    with lock:
        with lock:
            acquired_twice = True
    assert acquired_twice


def test_get_lock_concurrent_calls_never_mint_duplicate_lock_for_same_path(tmp_path):
    path = tmp_path / "concurrent.json"
    target = str(path)

    def fetch(_idx: int):
        return get_lock(target)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        locks = list(pool.map(fetch, range(N_WRITERS)))

    # Every thread must observe the exact same lock object -- no duplicate
    # RLock ever created for this one canonical path.
    assert len({id(lock) for lock in locks}) == 1
