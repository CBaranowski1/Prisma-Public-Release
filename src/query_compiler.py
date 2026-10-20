# src/query_compiler.py
"""Translate a visual-query JSON AST into a parameterized, SELECT-only
SQLAlchemy Core Select. Identifiers are resolved through an allowlist resolver;
values are always bound parameters. No string SQL is ever constructed.

QB-11: every filter value is coerced to its column's resolved Python type
(`col.type.python_type`) immediately before it is bound — see `_coerce_value`/
`_coerce_scalar` below, wired into `_build_filters`.
"""
import datetime
from typing import Any, Callable, Dict, List

from sqlalchemy import select, and_, or_, func, asc, desc

from src.query_catalog import QueryValidationError

MAX_LIMIT = 1000
DEFAULT_LIMIT = 100

_AGGREGATES = {
    "count": func.count, "sum": func.sum, "avg": func.avg,
    "min": func.min, "max": func.max,
}

# QB-12/D-07: only inner/left are compiler-supported. RIGHT/FULL are not
# implemented — a join `type` outside this set must clean-400, never silently
# fall back to inner (the old behavior at the `is_outer = ... == "left"` line).
_VALID_JOIN_TYPES = {"inner", "left"}

# Each operator maps to a callable(column, value) -> ColumnElement. Values are
# passed straight to SQLAlchemy comparison operators, which bind them as params.
_OPERATORS: Dict[str, Callable] = {
    "eq": lambda c, v: c == v,
    "ne": lambda c, v: c != v,
    "lt": lambda c, v: c < v,
    "lte": lambda c, v: c <= v,
    "gt": lambda c, v: c > v,
    "gte": lambda c, v: c >= v,
    # IN-03: declare backslash as the LIKE escape character so a value the
    # frontend has escaped (e.g. `%50\%%` for a literal "50%" substring search)
    # is matched literally. Backward-compatible for callers that use neither a
    # backslash nor wildcards; `%`/`_` remain wildcards unless backslash-escaped.
    "like": lambda c, v: c.like(v, escape="\\"),
    "ilike": lambda c, v: c.ilike(v, escape="\\"),
    "in": lambda c, v: c.in_(v if isinstance(v, (list, tuple)) else [v]),
    "is_null": lambda c, v: c.is_(None),
    "is_not_null": lambda c, v: c.isnot(None),
}

_CALC_OPS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
    "concat": lambda a, b: a.concat(b),
}


def _clamp_limit_offset(ast: dict):
    """Clamp the AST's limit/offset to safe bounds. Shared by compile_query (for
    the SQL) and execute_query (for the response metadata) so they never drift."""
    limit = ast.get("limit", DEFAULT_LIMIT)
    try:
        limit = min(int(limit), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    offset = ast.get("offset", 0)
    try:
        # WR-01: cap the offset at MAX_LIMIT WITHOUT shifting the window, then
        # truncate the effective limit so the window END stays within the
        # advertised MAX_LIMIT-row cap. The earlier `offset = MAX_LIMIT - limit`
        # clamp *moved* the requested window for page sizes that do not divide
        # MAX_LIMIT: e.g. limit=300, offset=900, MAX_LIMIT=1000 pulled offset
        # down to 700 and returned rows 700-999 — re-showing rows 700-899 the
        # prior page already displayed (silent duplication) instead of the
        # requested rows 900-999. Keeping the offset fixed (floored at 0, capped
        # at the cap) and shrinking the tail page returns exactly the rows the
        # caller asked for, and yields an EMPTY page once offset >= MAX_LIMIT —
        # never a shifted one. Divisor page sizes (the UI's limit=100) are
        # unaffected: 900+100 == 1000 == cap stays intact.
        offset = min(max(int(offset), 0), MAX_LIMIT)
    except (TypeError, ValueError):
        offset = 0
    # Trim the tail page instead of moving it: `offset + limit <= MAX_LIMIT`.
    limit = max(0, min(limit, MAX_LIMIT - offset))
    return limit, offset


def _req(d, key, ctx="spec"):
    """Return d[key], raising QueryValidationError if d isn't a dict or the key
    is missing. Turns malformed-AST KeyErrors/AttributeErrors into clean 400s."""
    if not isinstance(d, dict):
        raise QueryValidationError(f"Malformed {ctx}: expected an object")
    if key not in d:
        raise QueryValidationError(f"Malformed {ctx}: missing {key!r}")
    return d[key]


def _as_list(value, ctx):
    if value is None:
        return []
    if not isinstance(value, list):
        raise QueryValidationError(f"'{ctx}' must be a list")
    return value


def _resolve_instances(ast: dict, resolve_table_fn):
    """Map each AST table-instance id to its (aliased) Core Table, plus a
    parallel id->logical-name map (needed for owner-scoping)."""
    instances: Dict[str, Any] = {}
    names: Dict[str, str] = {}
    for t in _as_list(ast.get("tables"), "tables"):
        if not isinstance(t, dict):
            raise QueryValidationError("Each table must be an object")
        inst_id = t.get("id")
        name = t.get("table")
        if not inst_id or not name:
            raise QueryValidationError("Each table needs an 'id' and 'table' name")
        tbl = resolve_table_fn(name)            # raises if not allowlisted
        instances[inst_id] = tbl.alias(inst_id)
        names[inst_id] = name
    if not instances:
        raise QueryValidationError("Query must reference at least one table")
    return instances, names


def _col(instances, table_id: str, field: str):
    # Identifiers must be plain strings; a dict/list here would otherwise blow up
    # deep inside SQLAlchemy (e.g. TypeError: unhashable type) as a 500 instead
    # of a clean validation error.
    if not isinstance(table_id, str) or not isinstance(field, str):
        raise QueryValidationError("Table and field names must be strings")
    if table_id not in instances:
        raise QueryValidationError(f"Unknown table instance: {table_id!r}")
    aliased = instances[table_id]
    if field not in aliased.c:
        raise QueryValidationError(f"Unknown column: {table_id}.{field}")
    return aliased.c[field]


def _field_expr(instances, spec: dict):
    col = _col(instances, _req(spec, "table", "field"), _req(spec, "field", "field"))
    agg = spec.get("agg")
    if agg:
        if agg not in _AGGREGATES:
            raise QueryValidationError(f"Unknown aggregate: {agg!r}")
        col = _AGGREGATES[agg](col)
    return col


def _operand(instances, spec: dict):
    if not isinstance(spec, dict):
        raise QueryValidationError("Malformed calculated-field operand")
    if "value" in spec:
        return spec["value"]
    return _field_expr(instances, spec)


_NO_VALUE_OPERATORS = {"is_null", "is_not_null"}


def _coerce_scalar(col, value):
    """Coerce a single filter `value` to `col`'s resolved Python type
    (`col.type.python_type`, D-02 — the live SQLAlchemy column type, never the
    free-form catalog `data_type` string). Raises QueryValidationError with a
    field-named message on failure (D-04). Values remain plain Python objects
    handed to the SAME operator lambda — the binding mechanism never changes
    (D-01/D-16); only the VALUE does."""
    py_type = getattr(col.type, "python_type", str)
    if py_type is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
        raise QueryValidationError(
            f"Column {col.name!r} expects a boolean (true/false or 1/0) — got {value!r}")
    if py_type in (int, float) or getattr(py_type, "__name__", "") == "Decimal":
        # WR-02: a bool is an int subclass in Python, so an unguarded
        # isinstance/int() fast-path would silently bind True/False (1/0) to a
        # numeric column. Reject booleans outright.
        if isinstance(value, bool):
            raise QueryValidationError(
                f"Column {col.name!r} expects a number — got {value!r}")
        if isinstance(value, py_type):
            return value
        try:
            # WR-02: never silently truncate a non-integral float for an integer
            # column (`count == 3.7` must not become `count == 3`); surface a
            # clean validation error instead of returning the wrong rows.
            if py_type is int and isinstance(value, float) and not value.is_integer():
                raise ValueError
            return py_type(value)
        except (TypeError, ValueError):
            raise QueryValidationError(
                f"Column {col.name!r} expects a number — got {value!r}")
    if py_type in (datetime.datetime, datetime.date):
        if isinstance(value, py_type):
            return value
        try:
            parsed = datetime.datetime.fromisoformat(str(value))
        except ValueError:
            raise QueryValidationError(
                f"Column {col.name!r} expects an ISO date/datetime — got {value!r}")
        return parsed if py_type is datetime.datetime else parsed.date()
    return value  # str / passthrough for unmapped or unknown column types


def _coerce_value(col, operator: str, value):
    """Coerce `value` (or, for `in`, every element of it) to `col`'s resolved
    Python type before it is bound. `is_null`/`is_not_null` carry no value and
    must never reach this — guarded by the caller (`_build_filters`)."""
    if operator == "in":
        if not isinstance(value, (list, tuple)):
            value = [value]
        return [_coerce_scalar(col, v) for v in value]
    return _coerce_scalar(col, value)


def _push_owner_into_on(on, inst_ids, owner_preds, owner_in_on):
    """AND each not-yet-placed ownership predicate for the given (now
    nullable-side) instances into a join's ON clause, marking them handled so
    they are NOT also emitted in the top-level WHERE (WR-02). Instances with no
    predicate (owner scoping off, or already placed) are skipped."""
    for i in inst_ids:
        if i in owner_preds and i not in owner_in_on:
            on = and_(on, owner_preds[i])
            owner_in_on.add(i)
    return on


def _build_filters(instances, node: dict):
    if not node:
        return None
    if not isinstance(node, dict):
        raise QueryValidationError("Malformed filter group")
    op = node.get("op", "and")
    if op not in ("and", "or"):
        raise QueryValidationError(f"Unknown boolean op: {op!r}")
    clauses = []
    for cond in _as_list(node.get("conditions"), "conditions"):
        if not isinstance(cond, dict):
            raise QueryValidationError("Each filter condition must be an object")
        if "op" in cond and "conditions" in cond:
            sub = _build_filters(instances, cond)
            if sub is not None:
                clauses.append(sub)
            continue
        operator = cond.get("operator")
        if operator not in _OPERATORS:
            raise QueryValidationError(f"Unknown operator: {operator!r}")
        col = _col(instances, _req(cond, "table", "filter"), _req(cond, "field", "filter"))
        value = cond.get("value")
        if operator not in _NO_VALUE_OPERATORS:
            value = _coerce_value(col, operator, value)
        clauses.append(_OPERATORS[operator](col, value))
    if not clauses:
        return None
    return and_(*clauses) if op == "and" else or_(*clauses)


def compile_query(ast: dict, resolve_table_fn, enforce_owner: str | None = None,
                  owner_clause_fn=None) -> Any:
    instances, names = _resolve_instances(ast, resolve_table_fn)

    field_specs = _as_list(ast.get("fields"), "fields")
    columns = []
    for spec in field_specs:
        expr = _field_expr(instances, spec)
        if spec.get("alias"):
            expr = expr.label(spec["alias"])
        columns.append(expr)
    # QB-12/D-07: a bare (non-aggregated) selected field alongside an
    # aggregated field must be covered by group_by — otherwise this compiles
    # into SQL that only strict dialects (Postgres/MySQL) reject at execution
    # time. An aggregate-ONLY query (no bare field at all) is valid SQL and
    # must NOT be rejected here.
    if any(spec.get("agg") for spec in field_specs):
        grouped = {(g.get("table"), g.get("field"))
                   for g in _as_list(ast.get("group_by"), "group_by")}
        for spec in field_specs:
            if not spec.get("agg") and (spec.get("table"), spec.get("field")) not in grouped:
                raise QueryValidationError(
                    f"Field {spec.get('table')}.{spec.get('field')} must be "
                    f"aggregated or included in group_by")
    for spec in _as_list(ast.get("calculated"), "calculated"):
        if not isinstance(spec, dict):
            raise QueryValidationError("Each calculated field must be an object")
        op = spec.get("op")
        if op not in _CALC_OPS:
            raise QueryValidationError(f"Unknown calculated op: {op!r}")
        left = _operand(instances, _req(spec, "left", "calculated"))
        right = _operand(instances, _req(spec, "right", "calculated"))
        expr = _CALC_OPS[op](left, right)
        columns.append(expr.label(spec.get("alias", "calc")))
    if not columns:
        raise QueryValidationError("Query must select at least one field")

    # CR-02 defense-in-depth: guarantee every emitted column has a UNIQUE result
    # label so two same-named columns (e.g. sessions.id + chat_messages.id, even
    # when a caller supplies no/duplicate aliases) don't collapse into one key or
    # raise an ambiguous-column error in `result.mappings()`. First occurrence
    # keeps its name; each subsequent duplicate is suffixed `_2`, `_3`, …
    seen: Dict[str, int] = {}
    deduped = []
    for col in columns:
        base = getattr(col, "name", None) or "col"
        count = seen.get(base, 0)
        if count:
            candidate = f"{base}_{count + 1}"
            # Skip forward past any suffix that would itself collide.
            while candidate in seen:
                count += 1
                candidate = f"{base}_{count + 1}"
            col = col.label(candidate)
            seen[candidate] = 1
        seen[base] = count + 1
        deduped.append(col)
    columns = deduped

    stmt = select(*columns)

    # FROM + joins. Start from the first table instance and attach the rest.
    order = list(instances.keys())
    joins_raw = []
    for j in _as_list(ast.get("joins"), "joins"):
        if not isinstance(j, dict):
            raise QueryValidationError("Each join must be an object")
        # QB-12/D-07: reject an unknown/unsupported join type HERE, before the
        # attach loop's `is_outer = match.get("type") == "left"` treats
        # anything non-"left" as an inner join — that silent fallback let
        # `type:"right"` compile as an (incorrect) inner join.
        jtype = j.get("type", "inner")
        if jtype not in _VALID_JOIN_TYPES:
            raise QueryValidationError(
                f"Unsupported join type: {jtype!r} (only inner/left are supported)")
        joins_raw.append(j)

    # Attach each remaining table via a join that connects it to a table already
    # in the FROM — in EITHER direction. The canvas sets a join's left/right from
    # the order the user clicks the field handles, which is independent of the
    # order tables were added, so we must not assume the new table is always the
    # join's `right` side (that produced a spurious "not joined" error).
    #
    # CR-01: iterate to a FIXED POINT rather than a single insertion-order pass.
    # A single pass rejects a fully-connected join graph whenever a table's only
    # join(s) connect it to instances that appear LATER in insertion order (e.g.
    # a star schema built hub-last). Repeatedly sweeping the remaining tables and
    # attaching any that touch the already-attached set — until no more progress
    # is made — attaches every table reachable through the connected join graph,
    # regardless of table-add order.
    # WR-02: owner-scoping must not silently demote a LEFT join to INNER.
    # Precompute every instance's ownership predicate up front so a predicate on
    # the NULLABLE (null-producing) side of an outer join can be pushed into that
    # join's ON clause instead of the top-level WHERE. A predicate left in WHERE
    # evaluates to NULL/false for an unmatched outer-join row and drops it —
    # turning `A LEFT JOIN B` into an effective INNER JOIN whenever B is
    # owner-scoped, and silently dropping A's orphan rows. Moving the nullable
    # side's predicate into ON preserves those orphans (unmatched/unowned rows
    # surface with NULL nullable-side columns) while staying fail-closed: another
    # user's data can never appear as NON-NULL, because that predicate still
    # gates whether the nullable side matches at all. The base table and
    # inner-joined tables keep their predicate in WHERE (unchanged). The same
    # fail-closed raise for un-scopable tables is preserved, just computed here.
    owner_preds: Dict[str, Any] = {}
    if enforce_owner:
        for inst_id, aliased in instances.items():
            if owner_clause_fn is not None:
                owner_preds[inst_id] = owner_clause_fn(names[inst_id], aliased, enforce_owner)
            elif "owner" in aliased.c:
                owner_preds[inst_id] = aliased.c["owner"] == enforce_owner
            else:
                raise QueryValidationError(
                    f"Table {names[inst_id]!r} cannot be owner-scoped")
    owner_in_on: set = set()  # instances whose predicate was pushed into a JOIN ON

    attached = {order[0]}
    joined = instances[order[0]]
    remaining = set(order[1:])
    progress = True
    while remaining and progress:
        progress = False
        for inst_id in list(remaining):
            match = None
            for j in joins_raw:
                left, right = j.get("left"), j.get("right")
                if (inst_id == right and left in attached) or (inst_id == left and right in attached):
                    match = j
                    break
            if match is None:
                continue
            # The ON clause is symmetric, so left/right order doesn't affect it.
            left_col = _col(instances, _req(match, "left", "join"), _req(match, "left_field", "join"))
            right_col = _col(instances, _req(match, "right", "join"), _req(match, "right_field", "join"))
            is_outer = match.get("type") == "left"
            new_tbl = instances[inst_id]
            on = left_col == right_col
            # WR-01: a LEFT join must preserve the rows of the side the USER drew
            # as the join's `left`, independent of table-add order. SQLAlchemy's
            # `A.join(B, on, isouter=True)` emits `A LEFT OUTER JOIN B`, preserving
            # A (the left operand). When the table being attached (`inst_id`) is
            # itself the join's designated `left` (preserved) side — because the
            # OTHER endpoint happened to be added first and is already in the FROM —
            # attaching it as `joined.join(new, isouter=True)` would preserve
            # `joined` (the nullable `right` side): the WRONG rows, silently. Flip
            # the operands in that case so the new (preserved) table wraps the
            # accumulated FROM (`new LEFT OUTER JOIN joined`), keeping the
            # user-intended side preserved. Inner joins are symmetric, so operand
            # order is irrelevant for them and the default attach is used.
            if is_outer and inst_id == match.get("left"):
                # Flip case: `new LEFT OUTER JOIN joined` preserves new_tbl; every
                # instance already in the FROM (`attached`) becomes the nullable
                # side. WR-02: push their still-unplaced ownership predicates into
                # THIS ON clause so those preserved orphan rows survive.
                on = _push_owner_into_on(on, attached, owner_preds, owner_in_on)
                joined = new_tbl.join(joined, on, isouter=True)
            else:
                if is_outer:
                    # `joined LEFT OUTER JOIN new_tbl` preserves the FROM; new_tbl
                    # (inst_id) is the nullable side. WR-02: its ownership
                    # predicate belongs in the ON clause, not the WHERE.
                    on = _push_owner_into_on(on, {inst_id}, owner_preds, owner_in_on)
                joined = joined.join(new_tbl, on, isouter=is_outer)
            attached.add(inst_id)
            remaining.discard(inst_id)
            progress = True
    if remaining:
        raise QueryValidationError(
            f"Table {next(iter(remaining))!r} is not joined to the query")
    stmt = stmt.select_from(joined)

    where = _build_filters(instances, ast.get("filters"))
    clauses = []
    if where is not None:
        clauses.append(where)
    # Owner-scoping guard (fail closed): for non-admin callers, EVERY table
    # instance must contribute an ownership predicate, so no table can leak
    # other users' rows — not even a standalone owner-less table reached
    # without a join. Predicates for nullable-side instances of a LEFT join were
    # already pushed into their join's ON clause above (WR-02, `owner_in_on`) to
    # preserve orphan rows, so they are skipped here; base/inner-side predicates
    # go into WHERE. An empty `enforce_owner` string means "no scope"
    # (admins / single-user).
    if enforce_owner:
        for inst_id in instances:
            if inst_id in owner_in_on:
                continue
            clauses.append(owner_preds[inst_id])
    if clauses:
        stmt = stmt.where(and_(*clauses))

    for spec in _as_list(ast.get("group_by"), "group_by"):
        stmt = stmt.group_by(_col(instances, _req(spec, "table", "group_by"), _req(spec, "field", "group_by")))

    for spec in _as_list(ast.get("sorts"), "sorts"):
        col = _col(instances, _req(spec, "table", "sort"), _req(spec, "field", "sort"))
        stmt = stmt.order_by(desc(col) if spec.get("dir") == "desc" else asc(col))

    limit, offset = _clamp_limit_offset(ast)
    stmt = stmt.limit(limit).offset(offset)
    return stmt


def execute_query(db, ast: dict, resolve_table_fn, enforce_owner: str | None = None,
                  owner_clause_fn=None, return_stmt: bool = False) -> Dict[str, Any]:
    limit, offset = _clamp_limit_offset(ast)
    stmt = compile_query(ast, resolve_table_fn, enforce_owner=enforce_owner,
                         owner_clause_fn=owner_clause_fn)
    # QB-13/D-09: probe for `limit + 1` rows so `has_more` is an exact,
    # server-driven truncation signal instead of a client-side `>=limit`
    # heuristic. `compile_query` already applied its OWN clamped `.limit()`
    # above (via its internal `_clamp_limit_offset` call) — re-feeding a
    # `limit + 1`-mutated AST through `compile_query` again would just
    # re-clamp 1001 back down to MAX_LIMIT (1000) and silently lose the probe
    # row right at the ceiling (Pitfall 2). Instead, override the LIMIT
    # clause directly on the already-compiled `stmt`: `Select.limit()` is
    # generative and REPLACES the prior limit clause rather than stacking, so
    # this cleanly bypasses the clamp for this one internal probe call
    # without re-running `_resolve_instances`/the external-table resolver a
    # second time (which a second `compile_query` call would do) and without
    # mutating the caller's `ast`.
    probe_limit = min(limit + 1, MAX_LIMIT + 1)
    probe_stmt = stmt.limit(probe_limit)
    result = db.execute(probe_stmt)
    rows = [dict(m) for m in result.mappings().all()]
    columns: List[str] = list(result.keys())
    has_more = len(rows) > limit
    rows = rows[:limit]  # trim the probe's extra row — never leak it to the client
    out = {"columns": columns, "rows": rows, "limit": limit, "offset": offset,
           "has_more": has_more}
    if return_stmt:
        # WR-02: let callers that also need the compiled statement (e.g. to
        # derive verify-gate SQL text) reuse THIS compile instead of paying
        # for a second, separate compile_query call — each referenced
        # external table's resolver issues its own DB lookup per compile
        # (src/query_external.py:26-29), so a second compile doubles that
        # round-trip on every run_query execution.
        out["stmt"] = stmt
    return out
