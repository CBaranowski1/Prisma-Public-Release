"""Polyglot execution scorers for the A/B harness (python / ruby / postgres).

Each runner takes a candidate solution string + a problem dict and returns
(passed: bool, detail: str). Scoring is language-appropriate and sandboxed with
a subprocess/connection timeout. The SQL runner is backend-pluggable:
  - 'postgres' (default, real runs): psycopg3 against a live Postgres (DSN in
    POLYGLOT_PG_DSN, default postgresql:///polyglot_ab).
  - 'sqlite'   (local verification): Python stdlib, no server needed.
Both the candidate query and the reference query execute on the SAME backend at
score time, so the comparison is always dialect-consistent.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile


# ---- code fence extraction (shared) -------------------------------------
def extract_code(text: str, lang: str) -> str:
    """Pull the first fenced code block; fall back to the whole text."""
    if not text:
        return ""
    # language-tagged fence first, then any fence
    for pat in (rf"```(?:{lang}|sql|pgsql)?\s*(.*?)```", r"```(.*?)```"):
        m = re.findall(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            return m[0].strip()
    return text.strip()


# ---- python -------------------------------------------------------------
def run_python(candidate: str, problem: dict) -> tuple[bool, str]:
    timeout = int(problem.get("timeout", 10))
    program = candidate + "\n\n" + problem["test"] + "\nprint('__OK__')\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(program); path = f.name
    try:
        p = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=timeout)
        ok = p.returncode == 0 and "__OK__" in p.stdout
        return ok, (p.stderr or p.stdout)[-500:]
    except subprocess.TimeoutExpired:
        return False, f"timeout>{timeout}s"
    finally:
        os.unlink(path)


# ---- ruby ---------------------------------------------------------------
def run_ruby(candidate: str, problem: dict) -> tuple[bool, str]:
    timeout = int(problem.get("timeout", 10))
    program = candidate + "\n\n" + problem["test"] + "\nputs '__OK__'\n"
    with tempfile.NamedTemporaryFile("w", suffix=".rb", delete=False) as f:
        f.write(program); path = f.name
    try:
        p = subprocess.run(["ruby", path], capture_output=True, text=True, timeout=timeout)
        ok = p.returncode == 0 and "__OK__" in p.stdout
        return ok, (p.stderr or p.stdout)[-500:]
    except FileNotFoundError:
        return False, "ruby not found on PATH — install Ruby (https://rubyinstaller.org)"
    except subprocess.TimeoutExpired:
        return False, f"timeout>{timeout}s"
    finally:
        os.unlink(path)


# ---- sql (postgres default, sqlite for local verification) --------------
def _norm_rows(rows, order_matters):
    out = [tuple(("" if v is None else v) for v in r) for r in rows]
    return out if order_matters else sorted(out, key=lambda t: [str(x) for x in t])


def _sql_sqlite(schema, query, ref):
    import sqlite3
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(schema)
        cand = con.execute(query).fetchall()
        want = con.execute(ref).fetchall()
        return cand, want
    finally:
        con.close()


def _sql_postgres(schema, query, ref, timeout):
    import psycopg
    dsn = os.getenv("POLYGLOT_PG_DSN", "postgresql:///polyglot_ab")
    with psycopg.connect(dsn, autocommit=False, connect_timeout=timeout) as con:
        con.execute(f"SET statement_timeout = {timeout * 1000}")
        with con.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS ab_tmp; SET search_path TO ab_tmp;")
            cur.execute(schema)
            cur.execute(query); cand = cur.fetchall()
            cur.execute(ref); want = cur.fetchall()
        con.rollback()  # never persist fixtures
    return cand, want


def run_sql(candidate: str, problem: dict, backend: str = "postgres") -> tuple[bool, str]:
    timeout = int(problem.get("timeout", 15))
    schema = problem["schema"]
    ref = problem["reference"]
    order_matters = bool(problem.get("order_matters", False))
    q = candidate.strip().rstrip(";")
    if not q:
        return False, "empty query"
    try:
        if backend == "sqlite":
            cand, want = _sql_sqlite(schema, q, ref)
        else:
            cand, want = _sql_postgres(schema, q, ref, timeout)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:400]}"
    ok = _norm_rows(cand, order_matters) == _norm_rows(want, order_matters)
    return ok, "" if ok else f"got {cand[:3]}... want {want[:3]}..."


RUNNERS = {"python": run_python, "ruby": run_ruby, "postgres": run_sql, "sql": run_sql}


def score(candidate: str, problem: dict, sql_backend: str = "postgres") -> tuple[bool, str]:
    lang = problem["language"]
    code = extract_code(candidate, lang)
    if lang in ("postgres", "sql"):
        return run_sql(code, problem, backend=sql_backend)
    return RUNNERS[lang](code, problem)
