# tests/test_query_compiler_coercion.py
"""QB-11: filter values are coerced to their column's resolved Python type
immediately before binding (D-01/D-02/D-03/D-04).

Mirrors the `db_and_resolver` fixture pattern and bound-param/dialect-compile
assertion style from tests/test_query_compiler.py.
"""
import datetime
import decimal

import pytest
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Integer,
                        Float, Numeric, Boolean, DateTime, Date, insert)
from sqlalchemy.dialects import postgresql, mysql, sqlite

from src.query_catalog import QueryValidationError
from src.query_compiler import compile_query, execute_query


@pytest.fixture
def db_and_resolver():
    engine = create_engine("sqlite:///:memory:")
    md = MetaData()
    widgets = Table("widgets", md,
                    Column("id", String, primary_key=True),
                    Column("name", String),
                    Column("count", Integer),
                    Column("price", Float),
                    Column("weight", Numeric),
                    Column("active", Boolean),
                    Column("created_at", DateTime),
                    Column("due_on", Date))
    md.create_all(engine)
    conn = engine.connect()
    conn.execute(insert(widgets), [
        {"id": "w1", "name": "Alpha", "count": 5, "price": 1.5, "weight": 2.25,
         "active": True, "created_at": datetime.datetime(2026, 1, 1, 12, 0, 0),
         "due_on": datetime.date(2026, 1, 1)},
        {"id": "w2", "name": "Beta", "count": 10, "price": 3.5, "weight": 4.5,
         "active": False, "created_at": datetime.datetime(2026, 2, 1, 12, 0, 0),
         "due_on": datetime.date(2026, 2, 1)},
    ])
    conn.commit()
    tables = {"widgets": widgets}

    def resolve(name):
        if name not in tables:
            raise QueryValidationError(f"forbidden: {name}")
        return tables[name]

    yield conn, resolve
    conn.close()


def _filter_ast(field, operator, value):
    return {
        "tables": [{"id": "t1", "table": "widgets"}],
        "fields": [{"table": "t1", "field": "name", "alias": "n"}],
        "filters": {"op": "and", "conditions": [
            {"table": "t1", "field": field, "operator": operator, "value": value}]},
    }


# ---------------------------------------------------------------------------
# int coercion
# ---------------------------------------------------------------------------

def test_int_eq_coerces_numeric_string(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "eq", "5")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    assert 5 in compiled.params.values()
    assert "5" not in [str(v) for v in compiled.params.values() if isinstance(v, str)]


def test_int_gt_coerces_numeric_string(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "gt", "7")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    values = list(compiled.params.values())
    assert 7 in values
    assert all(not isinstance(v, str) for v in values if v == 7)


# ---------------------------------------------------------------------------
# float / decimal coercion
# ---------------------------------------------------------------------------

def test_float_column_coerces_numeric_string(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("price", "eq", "1.5")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    assert 1.5 in compiled.params.values()


def test_numeric_column_coerces_to_decimal(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("weight", "eq", "2.25")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    values = list(compiled.params.values())
    assert any(isinstance(v, decimal.Decimal) and v == decimal.Decimal("2.25") for v in values)


# ---------------------------------------------------------------------------
# boolean-string mapping (Pitfall 3)
# ---------------------------------------------------------------------------

def test_boolean_false_token_coerces_to_false_not_truthy(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = _filter_ast("active", "eq", "false")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    values = list(compiled.params.values())
    assert False in values
    assert True not in values
    out = execute_query(conn, ast, resolve)
    assert [r["n"] for r in out["rows"]] == ["Beta"]


def test_boolean_true_token_coerces_to_true(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = _filter_ast("active", "eq", "true")
    out = execute_query(conn, ast, resolve)
    assert [r["n"] for r in out["rows"]] == ["Alpha"]


def test_boolean_1_and_0_tokens_accepted(db_and_resolver):
    conn, resolve = db_and_resolver
    out_true = execute_query(conn, _filter_ast("active", "eq", "1"), resolve)
    assert [r["n"] for r in out_true["rows"]] == ["Alpha"]
    out_false = execute_query(conn, _filter_ast("active", "eq", "0"), resolve)
    assert [r["n"] for r in out_false["rows"]] == ["Beta"]


def test_boolean_invalid_token_raises_validation_error(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("active", "eq", "maybe")
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve)


# ---------------------------------------------------------------------------
# datetime / date ISO coercion
# ---------------------------------------------------------------------------

def test_datetime_column_parses_iso_string(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("created_at", "eq", "2026-01-01T12:00:00")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    values = list(compiled.params.values())
    assert any(isinstance(v, datetime.datetime) for v in values)


def test_date_column_parses_iso_string(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("due_on", "eq", "2026-01-01")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    values = list(compiled.params.values())
    assert any(type(v) is datetime.date for v in values)


def test_datetime_column_non_iso_string_raises_validation_error(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("created_at", "eq", "not-a-date")
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve)


# ---------------------------------------------------------------------------
# str passthrough
# ---------------------------------------------------------------------------

def test_varchar_column_value_passes_through_unchanged(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("name", "eq", "Alpha")
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    assert "Alpha" in compiled.params.values()


# ---------------------------------------------------------------------------
# `in` per-element coercion (D-03)
# ---------------------------------------------------------------------------

def _flatten_params(compiled):
    """SQLAlchemy compiles `in_()` as a single expanding bindparam whose value
    is a list, not one bindparam per element — flatten so per-element
    coercion can be asserted uniformly."""
    flat = []
    for v in compiled.params.values():
        if isinstance(v, (list, tuple)):
            flat.extend(v)
        else:
            flat.append(v)
    return flat


def test_in_operator_coerces_every_list_element(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "in", ["5", "10", "15"])
    stmt = compile_query(ast, resolve)
    compiled = stmt.compile()
    values = _flatten_params(compiled)
    ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    assert set(ints) >= {5, 10, 15}
    assert not any(isinstance(v, str) and v in ("5", "10", "15") for v in values)


def test_in_operator_executes_with_coerced_ints(db_and_resolver):
    conn, resolve = db_and_resolver
    ast = _filter_ast("count", "in", ["5", "10"])
    out = execute_query(conn, ast, resolve)
    assert sorted(r["n"] for r in out["rows"]) == ["Alpha", "Beta"]


# ---------------------------------------------------------------------------
# coercion failure -> clean error (D-04)
# ---------------------------------------------------------------------------

def test_int_column_bad_value_raises_validation_error_naming_column(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "eq", "abc")
    with pytest.raises(QueryValidationError) as exc_info:
        compile_query(ast, resolve)
    assert "count" in str(exc_info.value)


def test_in_operator_bad_element_raises_validation_error(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "in", ["5", "not-a-number"])
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve)


# ---------------------------------------------------------------------------
# WR-02 reject paths: bool-to-numeric and non-integral-float-to-int
# ---------------------------------------------------------------------------

def test_int_column_rejects_boolean_value(db_and_resolver):
    """WR-02: bool is an int subclass in Python, so an unguarded int() fast-path
    would silently bind True/False (1/0) to a numeric column. Reject it."""
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "eq", True)
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve)


def test_float_column_rejects_boolean_value(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("price", "eq", False)
    with pytest.raises(QueryValidationError):
        compile_query(ast, resolve)


def test_int_column_rejects_non_integral_float(db_and_resolver):
    """WR-02: `count == 3.7` must NOT silently truncate to 3 (which would return
    the wrong rows) — it raises a clean validation error naming the column."""
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "eq", 3.7)
    with pytest.raises(QueryValidationError) as exc_info:
        compile_query(ast, resolve)
    assert "count" in str(exc_info.value)


def test_int_column_accepts_integral_float(db_and_resolver):
    """The reject path must not over-reach: an integral float (3.0) is a valid
    integer value and coerces cleanly to 3."""
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "eq", 3.0)
    compiled = compile_query(ast, resolve).compile()
    values = list(compiled.params.values())
    assert 3 in values
    assert not any(isinstance(v, float) for v in values)


# ---------------------------------------------------------------------------
# IN-03 LIKE ESCAPE: `%`/`_` in a "contains" search match literally
# ---------------------------------------------------------------------------

def test_like_filter_matches_percent_literally_not_as_wildcard():
    """IN-03: a "contains" search for "50%" (escaped by the frontend, wrapped as
    `%50\\%%`) must match only bodies containing a literal "50%", NOT any body
    with "50" followed by anything — proving the compiler's `ESCAPE '\\'` clause
    honors the backslash-escaped metacharacter."""
    engine = create_engine("sqlite:///:memory:")
    md = MetaData()
    notes = Table("notes", md,
                  Column("id", Integer, primary_key=True),
                  Column("body", String))
    md.create_all(engine)
    conn = engine.connect()
    conn.execute(insert(notes), [
        {"id": 1, "body": "50% off"},
        {"id": 2, "body": "500 items"},
    ])
    conn.commit()

    def resolve(name):
        if name != "notes":
            raise QueryValidationError(f"forbidden: {name}")
        return notes

    # Exactly what state.js emits for a user typing "50%": metacharacters
    # backslash-escaped, then wrapped in the outer %…% wildcards.
    value = "%50\\%%"
    ast = {"tables": [{"id": "t1", "table": "notes"}],
           "fields": [{"table": "t1", "field": "body", "alias": "b"}],
           "filters": {"op": "and", "conditions": [
               {"table": "t1", "field": "body", "operator": "like", "value": value}]}}
    out = execute_query(conn, ast, resolve)
    bodies = [r["b"] for r in out["rows"]]
    assert bodies == ["50% off"], "the literal % must not act as a wildcard"
    conn.close()


def test_like_compiles_with_explicit_escape_clause(db_and_resolver):
    """The emitted SQL must carry an explicit ESCAPE clause so the backslash is
    treated as the escape character across dialects."""
    _conn, resolve = db_and_resolver
    ast = _filter_ast("name", "like", "%50\\%%")
    sql = str(compile_query(ast, resolve).compile())
    assert "ESCAPE" in sql


# ---------------------------------------------------------------------------
# is_null / is_not_null never reach coercion
# ---------------------------------------------------------------------------

def test_is_null_operator_ignores_value(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "is_null", None)
    stmt = compile_query(ast, resolve)
    assert stmt is not None


# ---------------------------------------------------------------------------
# cross-dialect proof (QB-11 explicit)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,operator,value,expected", [
    ("count", "eq", "5", 5),
    ("count", "gt", "7", 7),
])
def test_cross_dialect_eq_gt_bind_coerced_value(db_and_resolver, field, operator, value, expected):
    _conn, resolve = db_and_resolver
    ast = _filter_ast(field, operator, value)
    stmt = compile_query(ast, resolve)
    for dialect_mod in (sqlite.dialect(), postgresql.dialect(), mysql.dialect()):
        compiled = stmt.compile(dialect=dialect_mod)
        values = list(compiled.params.values())
        assert expected in values
        assert "DROP TABLE" not in str(compiled)
        assert not any(isinstance(v, str) and v == value for v in values)


def test_cross_dialect_in_bind_coerces_every_element(db_and_resolver):
    _conn, resolve = db_and_resolver
    ast = _filter_ast("count", "in", ["5", "10", "15"])
    stmt = compile_query(ast, resolve)
    for dialect_mod in (sqlite.dialect(), postgresql.dialect(), mysql.dialect()):
        compiled = stmt.compile(dialect=dialect_mod)
        values = _flatten_params(compiled)
        ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
        assert set(ints) >= {5, 10, 15}
        assert not any(isinstance(v, str) and v in ("5", "10", "15") for v in values)
