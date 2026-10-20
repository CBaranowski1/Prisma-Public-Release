"""SEC-27c / D-07c: get_auth_manager() process-level singleton accessor.

Covers the four behaviors locked in 17-04-PLAN.md:
  1. Same-instance identity across repeated calls.
  2. reset_auth_manager_cache() forces a new instance on next call.
  3. A monkeypatch of core.auth.AuthManager (set before the first call, while
     the cache is empty) is honored -- the returned instance is the stub.
  4. Double-checked locking: concurrent construction yields a single instance.
"""
import tempfile
import threading

import core.auth as auth_mod
from core.auth import get_auth_manager, reset_auth_manager_cache


def _tmp_auth_path():
    return tempfile.NamedTemporaryFile(suffix=".json", delete=False).name


def test_same_instance_across_repeated_calls():
    reset_auth_manager_cache()
    path = _tmp_auth_path()
    a = get_auth_manager(path)
    b = get_auth_manager(path)
    assert a is b


def test_reset_forces_new_instance():
    reset_auth_manager_cache()
    path = _tmp_auth_path()
    a = get_auth_manager(path)
    reset_auth_manager_cache()
    b = get_auth_manager(path)
    assert a is not b


def test_monkeypatch_honored_when_cache_empty(monkeypatch):
    reset_auth_manager_cache()

    class _StubAuth:
        def __init__(self, *_args, **_kwargs):
            self.is_stub = True

    monkeypatch.setattr(auth_mod, "AuthManager", _StubAuth)
    result = get_auth_manager()
    assert isinstance(result, _StubAuth)
    assert result.is_stub is True
    # Clean up so the stub doesn't leak into any test run in the same process
    # before the autouse fixture's post-test reset fires.
    reset_auth_manager_cache()


def test_concurrent_construction_yields_single_instance():
    reset_auth_manager_cache()
    path = _tmp_auth_path()
    results = []
    barrier = threading.Barrier(8)

    def _worker():
        barrier.wait()
        results.append(get_auth_manager(path))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    first = results[0]
    assert all(r is first for r in results)
