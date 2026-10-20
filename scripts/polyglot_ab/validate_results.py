"""Refuse to trust a results file that shows signs of a broken run.

Guards against the 'looks-like-a-number-but-isn't' failure modes:
  - empty file (model/endpoint never produced anything)
  - row count != expected (run died partway / dataset mismatch)
  - too many model-call errors (endpoint down => all-fail masquerading as low skill)
Prints per-language pass@1 only when the run is trustworthy. Exit 1 on any fail.
"""
import argparse
import json
import sys
import collections

ap = argparse.ArgumentParser()
ap.add_argument("--results", required=True)
ap.add_argument("--expect", type=int, default=0, help="expected row count (0=skip)")
ap.add_argument("--dataset", default="", help="derive expected count from this dataset")
ap.add_argument("--max-error-rate", type=float, default=0.25)
a = ap.parse_args()

try:
    rows = [json.loads(l) for l in open(a.results) if l.strip()]
except FileNotFoundError:
    print(f"FAIL: results file missing: {a.results}"); sys.exit(1)

if a.dataset:
    try: a.expect=sum(1 for l in open(a.dataset) if l.strip())
    except FileNotFoundError: pass
n = len(rows)
if n == 0:
    print(f"FAIL: {a.results} is empty — the model produced no scored rows."); sys.exit(1)
if a.expect and n != a.expect:
    print(f"FAIL: {a.results} has {n} rows, expected {a.expect} — run incomplete/mismatched."); sys.exit(1)

nerr = sum(1 for r in rows if r.get("model_error"))
rate = nerr / n
if rate > a.max_error_rate:
    print(f"FAIL: {nerr}/{n} ({rate:.0%}) model-call errors > {a.max_error_rate:.0%} "
          f"— endpoint/hiccup, the pass rate is NOT a real proficiency read.")
    ex = next((r["model_error"] for r in rows if r.get("model_error")), "")
    print(f"      first error: {ex}"); sys.exit(1)

by = collections.defaultdict(lambda: [0, 0])
for r in rows:
    by[r["language"]][0] += 1 if r.get("passed") else 0
    by[r["language"]][1] += 1
print(f"OK: {a.results} — {n} rows, {nerr} model errors ({rate:.0%})")
for lang, (p, t) in sorted(by.items()):
    print(f"    {lang:9}: {p}/{t} = {100*p//t}% pass@1")
sys.exit(0)
