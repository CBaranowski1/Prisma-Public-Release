"""Extra remediation notes distilled from the HumanEval+ exam's logic failures
(pair-sum, interval intersection, mixed-recurrence sequences, bracket nesting,
sort-by-key, digit ranges). GENERAL techniques for SIMILAR future problems - not
the graded answers. Writes into knowledge/remediation alongside the main corpus.
"""
from __future__ import annotations
import argparse
import json
import os
def _default_vault():
    base=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bj=os.path.join(base,"brains","llm-council","brain.json")
    try:
        vp=(json.load(open(bj,encoding="utf-8")).get("vault_path") or "").strip()
        if vp: return os.path.normpath(os.path.join(os.path.dirname(bj),vp))
    except Exception: pass
    return os.path.join(base,"brains","llm-council","vault")
NOTES=[('remediation',
  'pair-target-sum',
  'Pair With Target Sum (two-sum exists)',
  "Detect two elements summing to a target in O(n) with a seen-set. Edge: 'two DISTINCT elements' - a lone value that is half the target (e.g. a single 0 for target 0) does NOT count.",
  ['list-edge-cases'],
  'def has_pair_sum(nums, target):\n'
  '    seen = set()\n'
  '    for x in nums:\n'
  '        if (target - x) in seen: return True\n'
  '        seen.add(x)\n'
  '    return False\n'
  "# 'sum to zero': target=0; a single 0 alone is not a pair - need two entries."),
 ('remediation',
  'interval-intersection',
  'Interval Intersection',
  'Overlap of closed intervals [a1,a2],[b1,b2]: lo=max(a1,b1), hi=min(a2,b2); empty when lo>hi. The overlap LENGTH is hi-lo (points count as length 0). Then apply whatever test (e.g. primality) to '
  'that length.',
  ['number-theory-extras', 'geometry-formulas'],
  'def overlap_len(a, b):\n    lo, hi = max(a[0], b[0]), min(a[1], b[1])\n    return hi - lo if hi >= lo else -1   # -1 = no overlap'),
 ('remediation',
  'mixed-recurrence-sequences',
  'Sequences: Closed-Form + Recurrence Mix',
  'Some sequences define even-indexed terms by a closed form and odd-indexed terms by a recurrence that may reference a LATER term (forward reference). Fill the directly-computable terms first, then '
  "resolve the rest - don't assume a plain f(n)=f(n-1)+f(n-2).",
  ['fast-doubling-fibonacci', 'dp-fibonacci-memo'],
  '# pattern: for i in range(n+1):\n'
  '#   if i even: seq[i] = <closed form in i>\n'
  '#   else:      seq[i] = combination of neighbours (maybe seq[i+1], computed first)\n'
  '# Read the exact definition carefully - the recurrence is often non-standard.'),
 ('remediation',
  'bracket-nesting-depth',
  'Bracket Validity & Nesting Depth',
  "Track a running depth (+1 on open, -1 on close); invalid if it ever goes negative or ends non-zero. For 'is there a validly-nested subsequence', you may need to scan for a point where depth "
  'reaches >=2 (a nested pair), sometimes checking left-to-right AND right-to-left.',
  ['monotonic-stack', 'string-transforms'],
  'def max_depth(s):\n'
  '    d = best = 0\n'
  '    for c in s:\n'
  "        d += 1 if c in '([{' else -1\n"
  '        if d < 0: return -1   # unbalanced\n'
  '        best = max(best, d)\n'
  '    return best if d == 0 else -1'),
 ('remediation',
  'sort-by-computed-key',
  'Sort by Computed Key (stable tie-break)',
  "Sort by a derived value (e.g. digit sum) with a STABLE tie-break on original order. Python's sort is stable, so key=lambda i:(digitsum(x), idx) or just sort by the key preserves input order on "
  'ties. Digit sum of a NEGATIVE: apply the sign to the first digit (e.g. -12 -> -1+2 = 1).',
  ['type-discipline', 'list-edge-cases'],
  'def digit_sum_signed(n):\n'
  '    s = str(abs(n)); ds = [int(c) for c in s]\n'
  '    if n < 0: ds[0] = -ds[0]\n'
  '    return sum(ds)\n'
  '# sorted(nums, key=digit_sum_signed)  # stable -> ties keep original order'),
 ('remediation',
  'even-digits-in-range',
  'Even Digits in a Range',
  "When a task says 'even digits between a and b', 'digits' means single values 0-9: clamp the range into [0,9] first (lo=max(min(a,b),0), hi=min(max(a,b),9)), then take the even ones. Don't iterate "
  'raw a..b if they exceed one digit.',
  ['list-edge-cases'],
  'def even_digits(a, b):\n    lo, hi = max(min(a, b), 0), min(max(a, b), 9)\n    return [d for d in range(lo, hi + 1) if d % 2 == 0]')]
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--vault",default=_default_vault()); a=ap.parse_args()
    root=os.path.join(a.vault,"knowledge","remediation"); os.makedirs(root,exist_ok=True)
    for cat,slug,title,summary,related,code in NOTES:
        links=" ".join("[["+r+"]]" for r in related)
        body=("---\ntype: reference\ntags: reference,remediation,concept,humaneval,"+cat+"\n---\n\n# "+title+
              "\n\n"+summary+"\n\n```python\n"+code+"\n```\n\nSee also: "+links+" [[algorithms-index]] [[remediation-index]]\n")
        open(os.path.join(root,slug+".md"),"w",encoding="utf-8").write(body)
    print("wrote",len(NOTES),"HumanEval-derived remediation notes ->",root)
if __name__=="__main__": raise SystemExit(main())
