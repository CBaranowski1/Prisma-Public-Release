"""Audit regression tests for the query compiler (2026-06-29 full audit).

Covers join-direction independence (A3) and clean errors for malformed field
specs (A4), so a valid visual join never fails on click-order and bad input
never escapes as a 500.
"""
import pytest

from src.query_compiler import compile_query
from src.query_catalog import resolve_table, QueryValidationError


def _two_table_join(left, right):
    """tables added in order t1=sessions, t2=chat_messages; the join is authored
    with the given left/right instance ids (simulating either handle-click order)."""
    return {
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "chat_messages"}],
        "joins": [{"left": left, "left_field": "id" if left == "t1" else "session_id",
                   "right": right, "right_field": "id" if right == "t1" else "session_id",
                   "type": "inner"}],
        "fields": [{"table": "t1", "field": "id", "alias": "id"}],
    }


def test_join_left_is_first_table():
    stmt = compile_query(_two_table_join("t1", "t2"), resolve_table)
    assert "JOIN" in str(stmt).upper()


def test_join_left_is_second_table():
    # A3: clicking the later table's handle first makes left=t2; must still compile.
    stmt = compile_query(_two_table_join("t2", "t1"), resolve_table)
    assert "JOIN" in str(stmt).upper()


def test_unjoined_second_table_still_errors():
    ast = {
        "tables": [{"id": "t1", "table": "sessions"},
                   {"id": "t2", "table": "chat_messages"}],
        "joins": [],
        "fields": [{"table": "t1", "field": "id", "alias": "id"}],
    }
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve_table)


def test_nonstring_field_is_clean_validation_error():
    # A4: a dict where a column name is expected must be a 400-class error.
    ast = {"tables": [{"id": "t1", "table": "sessions"}],
           "fields": [{"table": "t1", "field": {"evil": 1}, "alias": "a"}]}
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve_table)


def test_query_touches_external_tolerates_malformed_tables():
    # A6: a non-dict table entry must not raise (would be a 500 in the route).
    from src.query_external import query_touches_external
    assert query_touches_external(None, {"tables": ["not-a-dict", 5, None]}) is False
    assert query_touches_external(None, {"tables": "nope"}) is False
    assert query_touches_external(None, "not-a-dict") is False
    # A real internal table is still recognised as non-external.
    assert query_touches_external(None, {"tables": [{"table": "sessions"}]}) is False
