"""A/B lift report: compare two code_trainer result files (baseline vs with-brain).

  python scripts/ab_report.py --baseline cc_baseline.jsonl --withbrain cc_withbrain.jsonl

Prints per-arm pass@1, paired flips (fail->pass = brain helped, pass->fail = brain
hurt), net delta, and a McNemar exact-test p-value so "reliable" is quantified.
"""
from __future__ import annotations
import argparse
import json
import math


def _load(path):
    d = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            r = json.loads(line)
            d[r["task_id"]] = bool(r.get("passed"))
    return d


def _mcnemar_p(b, c):
    """Exact two-sided binomial p over the n=b+c discordant pairs (p=0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--withbrain", required=True)
    args = ap.parse_args()
    base, brain = _load(args.baseline), _load(args.withbrain)
    common = sorted(set(base) & set(brain))
    n = len(common)
    bp = sum(base[t] for t in common)
    rp = sum(brain[t] for t in common)
    helped = [t for t in common if not base[t] and brain[t]]   # fail -> pass
    hurt = [t for t in common if base[t] and not brain[t]]      # pass -> fail
    p = _mcnemar_p(len(helped), len(hurt))

    print(f"paired problems      : {n}")
    print(f"baseline pass@1      : {bp}/{n} = {100*bp/max(1,n):.1f}%")
    print(f"with-brain pass@1    : {rp}/{n} = {100*rp/max(1,n):.1f}%")
    print(f"net delta            : {rp-bp:+d}  ({100*(rp-bp)/max(1,n):+.1f} pts)")
    print(f"brain HELPED (F->P)  : {len(helped)}  {helped[:8]}")
    print(f"brain HURT   (P->F)  : {len(hurt)}  {hurt[:8]}")
    print(f"McNemar p-value      : {p:.3f}  ->  {'RELIABLE (p<0.05)' if p < 0.05 else 'NOT yet reliable (need bigger gap or more n)'}")
    if n < 60:
        print(f"NOTE: n={n} is small; run the full set for a trustworthy result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
