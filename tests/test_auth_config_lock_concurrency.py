"""Real-thread concurrency proof for AuthManager config-lock serialization (SEC-17).

Drives many real threading.Thread workers against a single shared AuthManager
to prove that `_config_lock` (added in this phase) serializes every
read-modify-`_save()` critical section — no lost updates, and no torn/partial
on-disk `auth.json` snapshot.

Guard comment: if either test in this file ever HANGS instead of failing an
assertion, lock-ordering between `_config_lock` and `_sessions_lock` (Pitfall 3
in 15-RESEARCH.md) is the first suspect — check that no code path acquires
`_sessions_lock` while already holding `_config_lock` (or vice versa).
"""

import json
import threading

import pytest

import core.auth as auth_mod


def _make_manager(tmp_path, monkeypatch):
    # Monkeypatch to fast fake hashing so a many-thread test isn't dominated
    # by bcrypt's ~100ms-per-call cost. Using monkeypatch.setattr (instead of
    # raw attribute assignment) guarantees pytest reverts both functions on
    # this test's teardown, so the real bcrypt-backed hashing is never
    # permanently replaced process-wide (see 33-RESEARCH.md Pattern 2).
    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")
    monkeypatch.setattr(
        auth_mod, "_verify_password", lambda password, hashed: hashed == f"hash:{password}"
    )
    auth_path = tmp_path / "auth.json"
    mgr = auth_mod.AuthManager(str(auth_path))
    return mgr


class _ThreadCollector:
    """Runs target functions in real threads and collects any raised exceptions
    (thread exceptions do not otherwise propagate to the main thread/pytest)."""

    def __init__(self):
        self._errors = []
        self._lock = threading.Lock()
        self._threads = []

    def spawn(self, fn, *args, **kwargs):
        def _wrapped():
            try:
                fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - intentionally broad, this is a test harness
                with self._lock:
                    self._errors.append(e)

        t = threading.Thread(target=_wrapped)
        self._threads.append(t)
        return t

    def start_all(self):
        for t in self._threads:
            t.start()

    def join_all(self, timeout=30):
        for t in self._threads:
            t.join(timeout=timeout)

    @property
    def errors(self):
        return list(self._errors)


def test_concurrent_create_user_no_lost_updates(tmp_path, monkeypatch):
    """~50 real threads each creating a distinct user against one shared
    AuthManager must all succeed with no lost update and no torn on-disk
    snapshot."""
    mgr = _make_manager(tmp_path, monkeypatch)
    names = [f"user{i}" for i in range(50)]

    collector = _ThreadCollector()
    for name in names:
        collector.spawn(mgr.create_user, name, "pw12345", is_admin=False)

    collector.start_all()
    collector.join_all()

    assert collector.errors == [], f"Unexpected exceptions from worker threads: {collector.errors}"

    # No lost updates in memory.
    assert set(names).issubset(mgr.users.keys())

    # No torn/partial on-disk snapshot: the file must be valid, parseable
    # JSON reflecting every committed user.
    auth_path = tmp_path / "auth.json"
    on_disk = json.loads(auth_path.read_text())
    assert set(names).issubset(on_disk["users"].keys())


def test_concurrent_mixed_mutators_no_lost_update_no_torn_write(tmp_path, monkeypatch):
    """Mixed-mutator stress: concurrently running create_user + set_role +
    change_password against a seeded population must complete without
    raising RuntimeError("dictionary changed size during iteration") and
    leave internally-consistent final state (in-memory AND on-disk)."""
    mgr = _make_manager(tmp_path, monkeypatch)

    # Seed an admin (to authorize set_role calls) plus a population of
    # existing users that will be mutated concurrently.
    assert mgr.create_user("admin", "adminpw", is_admin=True)
    seed_names = [f"seed{i}" for i in range(30)]
    for name in seed_names:
        assert mgr.create_user(name, "origpw", is_admin=False)

    new_names = [f"new{i}" for i in range(30)]

    collector = _ThreadCollector()

    # Bucket A: create brand-new users concurrently.
    for name in new_names:
        collector.spawn(mgr.create_user, name, "pw12345", is_admin=False)

    # Bucket B: promote/demote existing seeded users concurrently.
    for i, name in enumerate(seed_names):
        role = "coder" if i % 2 == 0 else "user"
        collector.spawn(mgr.set_role, name, role, "admin")

    # Bucket C: change passwords on existing seeded users concurrently.
    for name in seed_names:
        collector.spawn(mgr.change_password, name, "origpw", "newpw")

    collector.start_all()
    collector.join_all()

    assert collector.errors == [], (
        "Unexpected exceptions from worker threads (a "
        "'dictionary changed size during iteration' RuntimeError here means "
        "_config_lock is missing or not spanning the full read-modify-_save() "
        "body): " + repr(collector.errors)
    )

    # No lost updates in memory: every new user present, every seed user
    # still present with its role/privileges intact.
    assert set(new_names).issubset(mgr.users.keys())
    assert set(seed_names).issubset(mgr.users.keys())
    assert "admin" in mgr.users

    # On-disk snapshot must be valid, parseable JSON reflecting the same
    # committed state — proves _save() never wrote a torn/partial file even
    # under concurrent mutation.
    auth_path = tmp_path / "auth.json"
    on_disk = json.loads(auth_path.read_text())
    assert set(new_names).issubset(on_disk["users"].keys())
    assert set(seed_names).issubset(on_disk["users"].keys())
    assert "admin" in on_disk["users"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
