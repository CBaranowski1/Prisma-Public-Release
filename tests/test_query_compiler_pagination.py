# tests/test_query_compiler_pagination.py
"""QB-13: boundary suite for execute_query's has_more limit+1 probe.

Covers the exactly-limit boundary, the limit+1 boundary, and the MAX_LIMIT
clamp trap (RESEARCH Pitfall 2) where a naive limit+1 probe fed back through
compile_query's own `_clamp_limit_offset` would silently re-clamp 1001 rows
down to 1000 and lose the probe row at the ceiling — reporting has_more as
False when it should be True.
"""
import pytest
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, insert

import src.query_compiler as query_compiler
from src.query_compiler import execute_query
from src.query_catalog import QueryValidationError


def _make_db(n_rows: int):
    """A fresh in-memory SQLite db with `n_rows` rows in a single `items`
    table — a fixture table sized per-test so boundary counts are exact."""
    engine = create_engine("sqlite:///:memory:")
    md = MetaData()
    items = Table("items", md,
                  Column("id", Integer, primary_key=True),
                  Column("name", String))
    md.create_all(engine)
    conn = engine.connect()
    conn.execute(insert(items), [{"id": i, "name": f"item{i}"} for i in range(n_rows)])
    conn.commit()

    def resolve(name):
        if name != "items":
            raise QueryValidationError(f"forbidden: {name}")
        return items

    return conn, resolve


def _ast(limit=None):
    ast = {"tables": [{"id": "t1", "table": "items"}],
           "fields": [{"table": "t1", "field": "name", "alias": "n"}],
           "sorts": [{"table": "t1", "field": "id", "dir": "asc"}]}
    if limit is not None:
        ast["limit"] = limit
    return ast


def test_fewer_rows_than_limit_has_more_false():
    conn, resolve = _make_db(3)
    out = execute_query(conn, _ast(limit=10), resolve)
    assert out["has_more"] is False
    assert len(out["rows"]) == 3
    conn.close()


def test_exactly_limit_rows_has_more_false():
    conn, resolve = _make_db(5)
    out = execute_query(conn, _ast(limit=5), resolve)
    assert out["has_more"] is False
    assert len(out["rows"]) == 5
    conn.close()


def test_limit_plus_one_rows_has_more_true_and_trimmed():
    conn, resolve = _make_db(6)
    out = execute_query(conn, _ast(limit=5), resolve)
    assert out["has_more"] is True
    # The probe's extra row must be trimmed, never leaked to the client.
    assert len(out["rows"]) == 5
    assert [r["n"] for r in out["rows"]] == [f"item{i}" for i in range(5)]
    conn.close()


def test_max_limit_ceiling_exact_not_truncated(monkeypatch):
    # Pitfall 2 (ceiling case): patch MAX_LIMIT small so we don't need to seed
    # 1001 real rows to exercise the clamp-trap boundary.
    monkeypatch.setattr(query_compiler, "MAX_LIMIT", 3)
    conn, resolve = _make_db(3)
    out = execute_query(conn, _ast(limit=1000), resolve)  # requested limit clamps to patched ceiling
    assert out["limit"] == 3
    assert out["has_more"] is False
    assert len(out["rows"]) == 3
    conn.close()


def test_max_limit_ceiling_plus_one_reports_has_more_true(monkeypatch):
    # The tell-tale case: one MORE row than the ceiling must still be detected
    # as has_more=True — proving the probe reached ceiling+1 and was NOT
    # silently re-clamped back down to the ceiling.
    monkeypatch.setattr(query_compiler, "MAX_LIMIT", 3)
    conn, resolve = _make_db(4)
    out = execute_query(conn, _ast(limit=1000), resolve)
    assert out["limit"] == 3
    assert out["has_more"] is True
    assert len(out["rows"]) == 3
    conn.close()


def _ast_with_offset(limit, offset):
    ast = _ast(limit=limit)
    ast["offset"] = offset
    return ast


def test_offset_clamped_so_window_never_exceeds_max_limit(monkeypatch):
    """WR-01: a raw API caller whose window would extend past the cap must have
    its TAIL PAGE TRUNCATED (not its offset shifted) so the effective window END
    stays within MAX_LIMIT — the requested window START is preserved."""
    monkeypatch.setattr(query_compiler, "MAX_LIMIT", 10)
    conn, resolve = _make_db(30)
    # offset=6, limit=10 would (unclamped) return rows 6-15, past the 10-row cap.
    # The clamp must KEEP offset at 6 and shrink limit to MAX_LIMIT - 6 == 4, so
    # the window is rows 6-9 — the tail is trimmed, the start is NOT moved.
    out = execute_query(conn, _ast_with_offset(limit=10, offset=6), resolve)
    assert out["offset"] == 6          # start preserved, never shifted
    assert out["limit"] == 4           # tail page truncated to fit the cap
    assert out["offset"] + out["limit"] <= query_compiler.MAX_LIMIT
    # Rows returned are exactly item6..item9 — no rows below offset, none at/over cap.
    assert [r["n"] for r in out["rows"]] == [f"item{i}" for i in range(6, 10)]
    conn.close()


def test_offset_at_or_past_cap_yields_empty_page(monkeypatch):
    """WR-01: an offset at/beyond MAX_LIMIT must yield an EMPTY page (limit
    truncated to 0), never a silently shifted window of earlier rows."""
    monkeypatch.setattr(query_compiler, "MAX_LIMIT", 10)
    conn, resolve = _make_db(30)
    out = execute_query(conn, _ast_with_offset(limit=10, offset=10), resolve)
    assert out["offset"] == 10         # start preserved (capped at MAX_LIMIT)
    assert out["limit"] == 0           # nothing left within the cap → empty page
    assert out["rows"] == []
    conn.close()


def test_offset_far_past_cap_clamped_to_cap_not_shifted(monkeypatch):
    """A wildly out-of-range offset (5000) must be capped at MAX_LIMIT (not
    shifted to a valid earlier window) and return an empty page."""
    monkeypatch.setattr(query_compiler, "MAX_LIMIT", 10)
    conn, resolve = _make_db(30)
    out = execute_query(conn, _ast_with_offset(limit=1000, offset=5000), resolve)
    assert out["offset"] == 10         # capped at MAX_LIMIT, not moved to 0
    assert out["limit"] == 0           # empty page — no earlier rows re-served
    assert out["rows"] == []
    assert out["offset"] + out["limit"] <= query_compiler.MAX_LIMIT
    conn.close()


def test_non_divisor_page_size_truncates_tail_not_shifts_window(monkeypatch):
    """WR-01 regression: for a page size that does NOT divide MAX_LIMIT, the cap
    must TRUNCATE the tail page rather than SHIFT the window. limit=300,
    offset=900, MAX_LIMIT=1000 requests rows [900, 1200); the cap trims this to
    rows [900, 1000) (exactly 100 rows). It must NOT pull offset back to 700
    (which would re-show rows 700-899 from the previous page) and no returned row
    may reach or exceed MAX_LIMIT."""
    monkeypatch.setattr(query_compiler, "MAX_LIMIT", 1000)
    conn, resolve = _make_db(1000)  # ids 0..999
    out = execute_query(conn, _ast_with_offset(limit=300, offset=900), resolve)
    assert out["offset"] == 900, "window start must be unchanged, not shifted"
    assert out["limit"] == 100, "tail page truncated to fit the cap (1000-900)"
    assert out["offset"] + out["limit"] <= query_compiler.MAX_LIMIT
    ns = [int(r["n"].replace("item", "")) for r in out["rows"]]
    assert ns == list(range(900, 1000)), "exactly rows 900-999, no duplication of prior page"
    assert min(ns) >= 900, "no row from before the requested offset (no shift)"
    assert max(ns) < query_compiler.MAX_LIMIT, "no row at/over the cap"
    conn.close()


def test_legitimate_in_cap_pagination_offset_preserved(monkeypatch):
    """The clamp must NOT disturb a legitimate in-cap page: limit=100, the last
    valid offset (900) leaves the window ending exactly at the cap."""
    monkeypatch.setattr(query_compiler, "MAX_LIMIT", 1000)
    conn, resolve = _make_db(0)  # rows irrelevant; we assert the metadata clamp
    out = execute_query(conn, _ast_with_offset(limit=100, offset=900), resolve)
    assert out["limit"] == 100
    assert out["offset"] == 900        # untouched — 900 + 100 == 1000 == cap
    conn.close()


def test_additive_safety_existing_keys_and_return_stmt_unaffected():
    conn, resolve = _make_db(2)
    out = execute_query(conn, _ast(limit=10), resolve)
    assert {"columns", "rows", "limit", "offset", "has_more"} <= set(out.keys())
    assert out["limit"] == 10
    assert out["offset"] == 0
    assert isinstance(out["has_more"], bool)

    out2 = execute_query(conn, _ast(limit=10), resolve, return_stmt=True)
    assert "stmt" in out2
    conn.close()
