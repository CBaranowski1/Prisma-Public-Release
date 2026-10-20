import json
import sys
from runners import score

def load(p): return [json.loads(l) for l in open(p) if l.strip()]

def check(path, sql_backend):
    probs = load(path); ok=0; bad=0
    for p in probs:
        # 1) reference must PASS
        passed, detail = score(p["reference"], p, sql_backend=sql_backend)
        # 2) a broken candidate must FAIL (empty + a wrong-but-valid answer)
        wrong = "SELECT 1 AS x" if p["language"] in ("postgres","sql") else "def %s(*a); nil; end" % p.get("entry_point","f") if p["language"]=="ruby" else "pass"
        wfail, _ = score(wrong, p, sql_backend=sql_backend)
        status = "PASS" if passed else "FAIL"
        disc  = "ok" if not wfail else "LEAK"
        if passed: ok+=1
        else: bad+=1
        flag = "" if (passed and not wfail) else "   <-- PROBLEM"
        print(f"  [{status}] ref  [{disc}] wrong  {p['task_id']}{flag}")
        if not passed: print(f"        detail: {detail[:180]}")
    print(f"  => {ok}/{len(probs)} references pass, {bad} broken\n")
    return bad==0

_dsets = sys.argv[1:] or ["datasets/ruby.jsonl", "datasets/postgres.jsonl"]
_ok = True
for _d in _dsets:
    print(f"=== {_d} (sqlite backend for grader check) ===")
    _ok &= check(_d, "sqlite")
sys.exit(0 if _ok else 1)
