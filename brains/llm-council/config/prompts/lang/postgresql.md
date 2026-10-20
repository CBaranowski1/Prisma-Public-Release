# PostgreSQL — language grounding

When reviewing PostgreSQL (SQL + PL/pgSQL), weigh these domain specifics:

- **NULL semantics**: `x = NULL` is never TRUE — use `IS NULL`. `COALESCE` /
  `NULLIF` for defaults. Aggregates skip NULLs.
- **Set ops**: `UNION` adds an implicit DISTINCT (sort) — prefer `UNION ALL`
  unless dedupe is required.
- **Injection**: never concatenate input into dynamic SQL; use parameterized
  queries or `format(... %L/%I)` / `quote_literal` / `quote_ident` in PL/pgSQL.
- **Indexes**: a function or cast on a filter/join column (`WHERE lower(x)=...`)
  bypasses a plain index — use an expression index or rewrite. Watch implicit
  casts (`text` vs `int`).
- **PL/pgSQL**: `SELECT ... INTO` sets `NOT FOUND` (not an exception) — check it;
  guard divide-by-zero; keep transactions tight (no long loops holding locks).
- **Types**: prefer `text`/`varchar(n)` deliberately; `timestamptz` over
  `timestamp` for instants; `numeric` for money, never `float`.
- **Performance**: avoid `SELECT *`; beware N+1 in app code; use `EXISTS` over
  `IN (subquery)` for large sets; `LIMIT` without `ORDER BY` is non-deterministic.

> Execution: the SqlRunner uses DuckDB "pretend tables" — good for generic SQL
> logic checks. True PL/pgSQL behavior needs a real Postgres (see docker-compose).
