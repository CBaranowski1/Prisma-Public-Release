"""GRAY-BRN-06: bounded, opt-in, pinned-safe retention/eviction for memory.json.

`prune_memory` is a pure list function modeled on
src/tool_output_store.py::_prune_store (oldest-first, best-effort eviction),
extended with a pinned-entry exclusion rule (30-RESEARCH.md Pitfall 5):
pinned entries are NEVER placed in the eviction pool, even if that means the
total surviving count exceeds `keep`.

`MemoryManager.compact()` is the guarded, flag-gated (`brain_retention_enabled`,
default OFF) trigger that persists a pruned list under the manager's lock
(30-03) and returns the evicted ids. It must be a strict no-op when the flag
is off/unset, and it must never raise (best-effort, mirrors
tool_output_store's `_prune_store` exception-swallowing shape).

Fully offline: MemoryManager only touches memory.json on a tmp dir; no
Chroma, no fastembed, no network.
"""
from src.memory import MemoryManager, prune_memory


def _entry(id_, timestamp, pinned=False):
    return {
        "id": id_,
        "text": f"entry-{id_}",
        "timestamp": timestamp,
        "source": "user",
        "category": "fact",
        "uses": 0,
        "pinned": pinned,
    }


# ---------------------------------------------------------------------------
# prune_memory (pure function)
# ---------------------------------------------------------------------------

def test_prune_memory_under_cap_returns_input_unchanged():
    entries = [_entry(str(i), i) for i in range(5)]
    result = prune_memory(entries, keep=10)
    assert result == entries


def test_prune_memory_evicts_oldest_unpinned_first_and_never_evicts_pinned():
    # 3 pinned (old timestamps) + 7 unpinned (mixed timestamps), keep=5.
    pinned = [_entry(f"pin-{i}", timestamp=i, pinned=True) for i in range(3)]
    unpinned = [_entry(f"un-{i}", timestamp=100 + i) for i in range(7)]
    entries = pinned + unpinned

    result = prune_memory(entries, keep=5)
    result_ids = {e["id"] for e in result}

    # Every pinned entry survives, no matter what.
    for p in pinned:
        assert p["id"] in result_ids

    # Cap respected: at most `keep` unpinned survivors (3 pinned + up to 2 unpinned == 5).
    survivors_unpinned = [e for e in result if not e.get("pinned")]
    assert len(survivors_unpinned) == 2

    # Oldest unpinned dropped first: un-0..un-4 (timestamps 100-104) evicted,
    # un-5/un-6 (newest, timestamps 105/106) survive.
    survivors_unpinned_ids = {e["id"] for e in survivors_unpinned}
    assert survivors_unpinned_ids == {"un-5", "un-6"}


def test_prune_memory_all_pinned_can_exceed_keep():
    # Pitfall 5: pinned entries are NEVER in the drop pool, even if the total
    # then exceeds `keep`.
    entries = [_entry(f"pin-{i}", timestamp=i, pinned=True) for i in range(8)]
    result = prune_memory(entries, keep=5)
    assert len(result) == 8
    assert {e["id"] for e in result} == {e["id"] for e in entries}


# ---------------------------------------------------------------------------
# MemoryManager.compact (guarded, flag-gated)
# ---------------------------------------------------------------------------

def test_compact_is_noop_when_flag_unset(tmp_path, monkeypatch):
    mgr = MemoryManager(str(tmp_path))
    entries = [_entry(str(i), i) for i in range(20)]
    mgr.save(entries)

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)

    evicted = mgr.compact(keep=5)

    assert evicted == []
    assert mgr.load_all() == entries


def test_compact_is_noop_when_flag_explicitly_false(tmp_path, monkeypatch):
    mgr = MemoryManager(str(tmp_path))
    entries = [_entry(str(i), i) for i in range(20)]
    mgr.save(entries)

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "brain_retention_enabled" else default,
    )

    evicted = mgr.compact(keep=5)

    assert evicted == []
    assert mgr.load_all() == entries


def test_compact_prunes_and_returns_evicted_ids_when_flag_enabled(tmp_path, monkeypatch):
    mgr = MemoryManager(str(tmp_path))
    pinned = [_entry(f"pin-{i}", timestamp=i, pinned=True) for i in range(2)]
    unpinned = [_entry(f"un-{i}", timestamp=100 + i) for i in range(8)]
    mgr.save(pinned + unpinned)

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    evicted = mgr.compact(keep=5)

    # 2 pinned + 8 unpinned = 10 total, keep=5 -> 3 unpinned survive, 5 evicted.
    assert len(evicted) == 5
    on_disk = mgr.load_all()
    on_disk_ids = {e["id"] for e in on_disk}

    # All pinned survive on disk.
    for p in pinned:
        assert p["id"] in on_disk_ids
    # Evicted ids are gone from disk, and are exactly the oldest unpinned.
    assert on_disk_ids.isdisjoint(set(evicted))
    assert set(evicted) == {"un-0", "un-1", "un-2", "un-3", "un-4"}
    assert len(on_disk) == 5


def test_compact_never_raises_on_internal_error(tmp_path, monkeypatch):
    mgr = MemoryManager(str(tmp_path))
    mgr.save([_entry(str(i), i) for i in range(20)])

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: True if key == "brain_retention_enabled" else default,
    )

    def _boom(entries):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(mgr, "save", _boom)

    evicted = mgr.compact(keep=5)

    assert evicted == []
