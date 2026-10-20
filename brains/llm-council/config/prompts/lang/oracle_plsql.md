# Oracle PL/SQL — language grounding

When reviewing Oracle PL/SQL, weigh these domain specifics:

- **Implicit type conversion**: comparing a NUMBER column to a quoted string
  (`employee_id = '00123'`) risks `ORA-01722`. Bind types must match.
- **NULL semantics**: `x = NULL` is never TRUE — use `IS NULL`. `NVL`/`COALESCE`
  for defaults. Three-valued logic in `WHERE`/`CASE`.
- **`SELECT ... INTO`** raises `NO_DATA_FOUND` (none) or `TOO_MANY_ROWS`
  (duplicates) — require an `EXCEPTION` block or a cursor.
- **Dates**: never compare a `DATE` to a string literal; use
  `TO_DATE('...', 'YYYY-MM-DD')` to avoid NLS-format dependence.
- **Set ops**: `UNION` adds an implicit DISTINCT (sort) — prefer `UNION ALL`
  unless dedupe is required.
- **Cursors / transactions**: explicit cursors closed; `COMMIT`/`ROLLBACK`
  placement; avoid commits inside loops over large sets.
- **Injection**: dynamic SQL via concatenation is exploitable — use bind
  variables / `DBMS_ASSERT`.

> Execution caveat: DuckDB/SQLite "pretend tables" do NOT run real PL/SQL. Label
> any executed checks "emulated"; true PL/SQL needs Oracle XE.

## Worked example (calibrate your findings to this bar)

Given:
```sql
SELECT salary INTO v_sal FROM employees
WHERE employee_id = '00123' AND hire_date > '2020-01-01';
IF v_rating = NULL THEN v_bonus := 0; END IF;
```
A good review produces findings like:
- **critical · line 1 · `employee_id = '00123'`** — NUMBER column compared to a
  string; throws `ORA-01722` on non-numeric/zero-padded input. Use `123` (or
  `TO_NUMBER`).
- **high · line 2 · `hire_date > '2020-01-01'`** — DATE vs string literal; depends
  on session NLS format. Use `TO_DATE('2020-01-01','YYYY-MM-DD')`.
- **critical · line 1 · `SELECT ... INTO`** — unhandled `NO_DATA_FOUND` /
  `TOO_MANY_ROWS`. Add an `EXCEPTION` block.
- **high · line 3 · `v_rating = NULL`** — never TRUE; use `IS NULL`.

Note the shape: each finding has a severity, a real line, a one-line root cause,
and a concrete fix. Do not pad with generic style advice.
