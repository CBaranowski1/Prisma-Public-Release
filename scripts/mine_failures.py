"""Backfill: turn a PAST run's failures into brain gap nodes (retroactive adaptation).

  python scripts/mine_failures.py --fails mbpp_exam.jsonl.fails.json
Auto-detects the dataset from task ids (Mbpp/*, HumanEval/*) to attach the problem
statement; for other ids pass --problems <jsonl>. Writes knowledge/gaps/gap-*.md.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE); sys.path.insert(0, os.path.join(BASE, "scripts"))
import code_trainer as ct


def _statements(fails, args):
    ids = {f["task_id"] for f in fails}
    out = {}
    if args.problems and os.path.exists(args.problems):
        for l in open(args.problems, encoding="utf-8"):
            if l.strip():
                r = json.loads(l); out[r.get("task_id", "")] = r.get("prompt", "")
        return out
    pref = next(iter(ids), "").split("/")[0].lower()
    try:
        if pref == "mbpp":
            from evalplus.data import get_mbpp_plus as g
        elif pref == "humaneval":
            from evalplus.data import get_human_eval_plus as g
        else:
            return out
        for tid, p in g().items():
            out[tid] = p.get("prompt", "")
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fails", required=True)
    ap.add_argument("--problems", default="")
    ap.add_argument("--vault", default=ct._default_vault())
    args = ap.parse_args()
    fails = json.load(open(args.fails, encoding="utf-8"))
    stmts = _statements(fails, args)
    n = 0
    for f in fails:
        tid = f["task_id"]
        prob = {"task_id": tid, "prompt": stmts.get(tid, ""),
                "kind": "stdio" if tid.lower().startswith("codecontests") else "function"}
        try:
            ct.store_gap_note(prob, f.get("detail", ""), args.vault); n += 1
        except Exception as e:
            print("  [!]", tid, e)
    print(f"wrote {n} gap nodes from {args.fails} -> {args.vault}/knowledge/gaps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
