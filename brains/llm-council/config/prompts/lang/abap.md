# ABAP — language grounding

When reviewing ABAP, weigh these domain specifics (note: base models are WEAKEST
here — be conservative and cite the code):

- **SELECT performance**: avoid `SELECT *`; select only needed fields; avoid
  `SELECT ... ENDSELECT` loops and nested SELECTs — use `FOR ALL ENTRIES` (guard
  against an empty driver table → full scan) or CDS/joins. Watch the buffer.
- **`FOR ALL ENTRIES`**: empty internal table selects EVERYTHING; always check
  `IF itab IS NOT INITIAL`. Duplicates are removed — beware lost rows.
- **Internal tables**: prefer `READ TABLE ... BINARY SEARCH` (sorted) or hashed
  tables over linear reads; `LOOP` work adds up.
- **Authority checks**: `AUTHORITY-CHECK` present for protected operations.
- **Units of work**: explicit `COMMIT WORK` / rollback; avoid commits inside
  LUW-spanning loops.
- **Obsolete statements**: flag `MOVE`/header-line usage and other deprecated
  constructs.

> Execution caveat: true ABAP needs an SAP test system; DuckDB/SQLite cannot run
> it. Scope executable checks accordingly and LABEL anything "emulated". ABAP is
> the weakest lane until RAG over the company's code lands.

## Worked example (calibrate your findings to this bar)

Given:
```abap
SELECT * FROM mara INTO TABLE lt_mara FOR ALL ENTRIES IN lt_keys
  WHERE matnr = lt_keys-matnr.
LOOP AT lt_mara INTO ls_mara.
  SELECT SINGLE * FROM makt INTO ls_makt WHERE matnr = ls_mara-matnr.
ENDLOOP.
```
A good review produces findings like:
- **critical · line 1 · `FOR ALL ENTRIES IN lt_keys`** — no `IF lt_keys IS NOT
  INITIAL` guard; an empty driver table selects the ENTIRE table. Add the check.
- **high · line 1 · `SELECT *`** — selects all fields; name only the needed
  columns to cut buffer/transport cost.
- **high · line 3 · `SELECT SINGLE` inside `LOOP`** — N+1 database round-trips;
  pre-fetch `makt` once with `FOR ALL ENTRIES` (guarded) and read from an
  internal table.

Each finding has a severity, a real line, a one-line root cause, and a concrete
fix. Be conservative and cite the code — do not invent issues.
