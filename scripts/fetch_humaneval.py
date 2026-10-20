#!/usr/bin/env python3
"""Fetch HumanEval into the code_trainer --problems format (no evalplus needed).

Uses the `datasets` library (already present — it's what pulls CodeContests) to
download the canonical openai_humaneval set and emit one JSON object per line in
exactly the shape load_problems() expects for a "function"-kind task:
    {task_id, prompt, test, entry_point, kind:"function", canonical_solution}

Usage:
    python scripts/fetch_humaneval.py --out data/evals/humaneval.jsonl
Then:
    python scripts/code_trainer.py --problems data/evals/humaneval.jsonl --limit 100 ...
"""
from __future__ import annotations
import argparse
import json
import os
import sys

HUMANEVAL_REVISION = "7dce6050a7d6d172f3cc5c32aa97f52fa1a2e544"


def _load_humaneval_rows(dataset_id: str, revision: str):
    """Return HumanEval rows (dicts with task_id/prompt/test/entry_point/
    canonical_solution). Tries the `datasets` library first; on ANY failure
    (e.g. the dill `Pickler._batch_setitems` incompatibility under Python 3.14)
    falls back to OpenAI's canonical HumanEval.jsonl.gz — the authoritative
    source, with identical field names. Returns None if both paths fail."""
    try:
        from datasets import load_dataset
        return list(load_dataset(dataset_id, revision=revision)["test"])
    except Exception as e:  # pragma: no cover - environment dependent
        print(f"[!] `datasets` load failed ({e}); falling back to canonical HumanEval.jsonl.gz")

    if "humaneval" not in dataset_id.lower():
        print(f"[!] no canonical fallback for non-HumanEval dataset '{dataset_id}'.")
        return None

    import gzip
    import urllib.request
    url = "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        # URL is a fixed HTTPS OpenAI GitHub asset, never caller-controlled.
        raw = urllib.request.urlopen(req, timeout=60).read()  # nosec B310
        text = gzip.decompress(raw).decode("utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except Exception as e:  # pragma: no cover - network dependent
        print(f"[!] canonical fallback download failed ({e}). Check network / HF access.")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/evals/humaneval.jsonl")
    ap.add_argument("--dataset", default="openai_humaneval",
                    help="HF dataset id (default openai_humaneval)")
    ap.add_argument(
        "--revision",
        default=HUMANEVAL_REVISION,
        help="immutable Hugging Face dataset commit",
    )
    args = ap.parse_args()

    ds = _load_humaneval_rows(args.dataset, args.revision)
    if ds is None:
        return 2

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    n = 0
    with open(args.out, "w", encoding="utf-8") as f:
        for r in ds:
            rec = {
                # normalize the id slug so brain-hint self-exclusion matches
                "task_id": str(r["task_id"]).replace("HumanEval/", "humaneval/"),
                "prompt": r["prompt"],
                "test": r["test"],            # defines check(candidate); harness appends check(<entry_point>)
                "entry_point": r["entry_point"],
                "kind": "function",
                "canonical_solution": r.get("canonical_solution", ""),
            }
            f.write(json.dumps(rec) + "\n")
            n += 1
    print(f"wrote {n} HumanEval problems -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
