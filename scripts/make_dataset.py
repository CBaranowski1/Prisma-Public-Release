"""Convert public coding datasets into the harness's JSONL format.

Function-style: {task_id, prompt, test, entry_point}
Stdio-style:    {task_id, prompt, io:[{input,output}, ...]}

Sources:
  --source codecontests   deepmind/code_contests (HuggingFace) -> stdio jsonl
  --source jsonl --in X    normalize/validate an existing jsonl

Needs:  pip install datasets   (for HuggingFace sources)
NOTE: HuggingFace schemas drift — run with --limit 3 first and eyeball the output.
"""
from __future__ import annotations
import argparse
import json
import sys

CODE_CONTESTS_REVISION = "802411c3010cb00d1b05bad57ca77365a3c699d6"


def from_codecontests(limit, split, max_cases=25, skip=0):
    from datasets import load_dataset
    ds = load_dataset(
        "deepmind/code_contests",
        split=split,
        streaming=True,
        revision=CODE_CONTESTS_REVISION,
    )
    out = []
    for i, row in enumerate(ds):
        if i < skip:
            continue
        if limit and (i - skip) >= limit:
            break
        io = []
        # public = sample; private + generated = the REAL hidden judge. Include all
        # (capped) so a stored "verified" solution actually passes the full suite.
        for key in ("public_tests", "private_tests", "generated_tests"):
            t = row.get(key) or {}
            for a, b in zip(t.get("input") or [], t.get("output") or []):
                io.append({"input": a, "output": b})
                if len(io) >= max_cases:
                    break
            if len(io) >= max_cases:
                break
        if not io:
            continue
        out.append({"task_id": "codecontests/" + str(row.get("name", i)),
                    "prompt": row.get("description", ""), "io": io, "kind": "stdio"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=["codecontests", "jsonl"])
    ap.add_argument("--in", dest="infile", default="")
    ap.add_argument("--out", default="problems.jsonl")
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--max-cases", type=int, default=25, help="max test cases per problem")
    ap.add_argument("--skip", type=int, default=0, help="skip the first N rows (use later, unseen problems)")
    args = ap.parse_args()

    if args.source == "codecontests":
        try:
            recs = from_codecontests(args.limit, args.split, args.max_cases, args.skip)
        except Exception as e:
            print(f"[!] codecontests load failed ({e}). pip install datasets; check network/schema."); return 2
    else:
        if not args.infile:
            print("[!] --in required for jsonl source"); return 2
        recs = [json.loads(l) for l in open(args.infile, encoding="utf-8") if l.strip()][: args.limit or None]

    with open(args.out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    kinds = {}
    for r in recs:
        k = r.get("kind", "function"); kinds[k] = kinds.get(k, 0) + 1
    print(f"wrote {len(recs)} problems {kinds} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
