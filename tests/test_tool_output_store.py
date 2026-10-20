"""GRAY-AGT-02: full-output store round-trip, ownership scoping, and the
bash/python truncation-indicator handoff.

The store keeps the FULL tool output on disk (scoped to session_id/owner) so
truncated bash/python output is retrievable in full, while a cross-session
lookup is rejected (Information Disclosure control).
"""
import re

from src import tool_output_store


def test_put_get_round_trip_preserves_full_text():
    full = "X" * 50_000
    oid = tool_output_store.put(full, session_id="s1", owner="u1")
    assert oid and oid.isalnum()
    got = tool_output_store.get(oid, session_id="s1", owner="u1")
    assert got == full  # exact, untruncated


def test_get_rejects_cross_owner_and_cross_session():
    oid = tool_output_store.put("secret output", session_id="s1", owner="u1")
    assert tool_output_store.get(oid, session_id="s1", owner="u2") is None  # wrong owner
    assert tool_output_store.get(oid, session_id="s2", owner="u1") is None  # wrong session
    assert tool_output_store.get(oid, session_id="s1", owner="u1") == "secret output"


def test_get_unknown_or_invalid_id_returns_none():
    assert tool_output_store.get("deadbeef" * 4, session_id="s1", owner="u1") is None
    assert tool_output_store.get("", session_id=None, owner=None) is None
    assert tool_output_store.get("../etc/passwd", session_id=None, owner=None) is None


def test_ids_are_unique_and_non_guessable():
    ids = {tool_output_store.put("x", session_id="s", owner="u") for _ in range(20)}
    assert len(ids) == 20
    assert all(len(i) >= 16 for i in ids)


def test_truncate_with_indicator_persists_and_is_retrievable():
    from src.tool_execution import _truncate_with_indicator, MAX_OUTPUT_CHARS

    full = "line\n" * (MAX_OUTPUT_CHARS)  # well over the limit
    shown = _truncate_with_indicator(full, MAX_OUTPUT_CHARS, session_id="s9", owner="u9")

    assert len(shown) < len(full)
    m = re.search(r"full output: ([0-9a-f]+)", shown)
    assert m, f"no retrieval id in indicator: {shown[-120:]!r}"
    oid = m.group(1)
    # The full, untruncated output is retrievable for the owner...
    assert tool_output_store.get(oid, session_id="s9", owner="u9") == full
    # ...and NOT for a different owner.
    assert tool_output_store.get(oid, session_id="s9", owner="rogue") is None


def test_truncate_with_indicator_under_limit_is_unchanged():
    from src.tool_execution import _truncate_with_indicator

    small = "just a little output"
    assert _truncate_with_indicator(small, 10_000, session_id="s", owner="u") == small
    # No 'full output' indicator for under-limit text.
    assert "full output:" not in _truncate_with_indicator(small, 10_000)


def test_store_growth_is_bounded_by_retention_cap(tmp_path, monkeypatch):
    # T-28-07 (DoS/disk): the store prunes oldest records beyond the cap so a
    # flood of over-limit tool calls cannot grow DATA_DIR/tool_output unbounded.
    import os

    monkeypatch.setattr(tool_output_store, "_STORE_DIR", tmp_path)
    oids = [tool_output_store.put(f"output-{i}", session_id="s", owner="u") for i in range(12)]
    # Under the default cap (500), put() prunes nothing — all 12 survive.
    assert len(list(tmp_path.glob("*.json"))) == 12
    # Stamp strictly increasing mtimes so "oldest" is deterministic (avoids
    # coarse-filesystem-mtime flakiness on Windows): oids[0] oldest → oids[-1] newest.
    for i, oid in enumerate(oids):
        os.utime(tmp_path / f"{oid}.json", (1_000_000 + i, 1_000_000 + i))

    tool_output_store._prune_store(keep=4)

    remaining = {p.stem for p in tmp_path.glob("*.json")}
    assert len(remaining) == 4
    # The 4 newest survive; the 8 oldest were evicted.
    assert remaining == set(oids[-4:])
    assert tool_output_store.get(oids[-1], session_id="s", owner="u") == "output-11"
    assert tool_output_store.get(oids[0], session_id="s", owner="u") is None
