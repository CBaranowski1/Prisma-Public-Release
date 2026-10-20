# tests/test_query_compiler_ui_key.py
"""The compiler must ignore UI-only keys (e.g. `ui`) embedded in a saved query's
definition, so the front end can persist canvas layout alongside the query."""
from src.query_compiler import compile_query
from src.query_catalog import resolve_table


def _base_ast():
    return {
        "tables": [{"id": "t1", "table": "sessions"}],
        "fields": [{"table": "t1", "field": "id", "alias": "id"}],
    }


def test_ui_key_does_not_change_compiled_sql():
    plain = compile_query(_base_ast(), resolve_table)
    with_ui = dict(_base_ast())
    with_ui["ui"] = {"positions": {"t1": {"x": 10, "y": 20}},
                     "grid": [{"table": "t1", "field": "id", "show": True}]}
    decorated = compile_query(with_ui, resolve_table)
    assert str(plain) == str(decorated)


def test_unknown_top_level_key_is_ignored():
    ast = _base_ast()
    ast["totally_unknown"] = {"anything": [1, 2, 3]}
    # Should compile without raising.
    compile_query(ast, resolve_table)
