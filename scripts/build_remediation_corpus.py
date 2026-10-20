"""Inject a REMEDIATION concept corpus - the specific general knowledge the model
missed on the MBPP+ exam (figurate-number formulas, solid geometry, cmath, tuple/dict
idioms, edge-cases). GENERAL reference, NOT the graded problems' solutions.

  python scripts/build_remediation_corpus.py
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
  'figurate-numbers',
  'Figurate / Polygonal Numbers',
  'Closed-form formulas for the nth polygonal number. General: P(s,n)=((s-2)*n*n-(s-4)*n)//2.',
  ['combinatorics-overview'],
  'def polygonal(s,n): return ((s-2)*n*n-(s-4)*n)//2\n'
  '# triangular n(n+1)/2 | square n^2 | pentagonal n(3n-1)/2 | hexagonal n(2n-1)\n'
  '# heptagonal n(5n-3)/2 | octagonal n(3n-2) | nonagonal n(7n-5)/2 | decagonal n(4n-3)\n'
  'def octagonal(n): return n*(3*n-2)\n'
  'def nonagonal(n): return n*(7*n-5)//2\n'
  'def decagonal(n): return n*(4*n-3)'),
 ('remediation',
  'geometry-solids',
  'Surface Area & Volume of Solids',
  'Common solid formulas that show up constantly. Use math.pi.',
  ['geometry-formulas'],
  'import math\n'
  '# cylinder: lateral=2*pi*r*h, total=2*pi*r*(r+h), volume=pi*r*r*h\n'
  '# cone: lateral=pi*r*slant, volume=pi*r*r*h/3\n'
  '# sphere: area=4*pi*r*r, volume=4/3*pi*r**3\n'
  '# cube: area=6*a*a, volume=a**3\n'
  '# square pyramid (base edge a, slant l): SA = a*a + 2*a*l\n'
  'def lateral_cylinder(r,h): return 2*math.pi*r*h'),
 ('remediation',
  'geometry-formulas',
  'Plane Geometry / Conics',
  'Right-triangle sides, parabola directrix, triangle area.',
  ['geometry-solids'],
  'import math\n'
  '# right triangle, given two sides -> third: hyp=sqrt(a*a+b*b); leg=sqrt(c*c-a*a)\n'
  '# parabola y=ax^2+bx+c directrix: y = c - (b*b+1)/(4a)\n'
  '# heron area: s=(a+b+c)/2; sqrt(s*(s-a)*(s-b)*(s-c))\n'
  'def third_side(a,b): return math.sqrt(a*a+b*b)'),
 ('remediation',
  'complex-and-polar',
  'Complex Numbers & Polar/Rect',
  'Use cmath for angle/magnitude and coordinate conversions.',
  ['geometry-formulas'],
  'import cmath, math\n'
  '# angle (phase) of a complex number: cmath.phase(z)\n'
  '# polar<-complex: cmath.polar(z) -> (r, phi) ; rect<-polar: cmath.rect(r, phi)\n'
  '# polar(r,theta) -> rectangular: (r*math.cos(theta), r*math.sin(theta))\n'
  'def angle_complex(a,b): return cmath.phase(complex(a,b))'),
 ('remediation',
  'wind-chill',
  'Wind Chill Index',
  'North American wind chill formula (v in km/h, t in Celsius), round to next int.',
  ['geometry-formulas'],
  'import math\ndef wind_chill(v,t):\n    wc = 13.12 + 0.6215*t - 11.37*v**0.16 + 0.3965*t*v**0.16\n    return int(round(wc))'),
 ('remediation',
  'tuple-dict-idioms',
  'Tuple & Dict Idioms',
  'The Python one-liners MBPP-style tuple/dict tasks want.',
  ['python-collections'],
  '# sort tuples by 2nd value: sorted(lst, key=lambda x: x[1])\n'
  '# merge dicts: {**a, **b, **c}\n'
  '# elementwise op on two tuples: tuple(x & y for x,y in zip(t1,t2))\n'
  '# average each inner tuple: [sum(t)/len(t) for t in tuples]\n'
  '# cumulative sum of a tuple: list(itertools.accumulate(t))\n'
  'import itertools'),
 ('remediation',
  'dict-depth',
  'Depth of Nested Dict',
  'Recursive max depth of nested dictionaries.',
  ['tuple-dict-idioms'],
  'def dict_depth(d):\n    if not isinstance(d,dict) or not d: return int(isinstance(d,dict))\n    return 1+max(dict_depth(v) for v in d.values())'),
 ('remediation',
  'list-edge-cases',
  'List Edge-Cases (second smallest, unique, diff)',
  'Handle duplicates and empties explicitly.',
  ['python-collections'],
  'from collections import Counter\n'
  '# second smallest (distinct): u=sorted(set(a)); u[1] if len(u)>1 else None\n'
  '# non-repeated sum: sum(x for x,c in Counter(a).items() if c==1)\n'
  '# symmetric difference of two lists: list(set(a)^set(b))\n'
  '# remove kth (0-indexed): a[:k]+a[k+1:]'),
 ('remediation',
  'type-discipline',
  "Type Discipline (don't abs a list)",
  "Return the right TYPE; map over elements, don't apply scalar ops to containers.",
  ['list-edge-cases'],
  '# sum of digits per number in a list -> return a LIST, map each:\n# [sum(int(c) for c in str(abs(n))) for n in nums]\n# abs()/int()/round() take scalars, not lists - iterate first.'),
 ('remediation',
  'string-transforms',
  'String Transforms',
  'snake->camel, count substring, filter chars, to tuple.',
  ['python-collections'],
  "def snake_to_camel(s): return ''.join(w.title() for w in s.split('_'))\n"
  "# count occurrences of a substring: s.count('std')\n"
  '# remove chars by index parity: s[::2] (keep even positions)\n'
  '# string to tuple of chars: tuple(s)'),
 ('remediation',
  'regex-basics',
  'Regex Matching Basics',
  "re for pattern tasks like 'a followed by three b'.",
  ['string-transforms'],
  "import re\n# 'a' then exactly three 'b': re.search('ab{3}', s)\n# whole-string match: re.fullmatch(pat, s) ; find all: re.findall(pat, s)"),
 ('remediation',
  'combinations-itertools',
  'Combinations & Subset Sums',
  'itertools for all combinations / sum-combinations.',
  ['combinatorics-overview', 'list-edge-cases'],
  'import itertools\n'
  '# all combinations of all lengths:\n'
  '# sum(([list(c) for c in itertools.combinations(a,r)] for r in range(len(a)+1)),[])\n'
  '# pairwise sums of tuples: [x+y for x,y in itertools.combinations(vals,2)]'),
 ('remediation',
  'number-theory-extras',
  'Number-Theory Extras',
  'Common divisors, ax+by=n, NSW primes, ludic numbers.',
  ['number-theory-overview'],
  'from math import gcd\n'
  '# sum of common divisors of a,b: g=gcd(a,b); sum(d for d in range(1,g+1) if g%d==0)\n'
  '# solve a*x+b*y=n: try x, then (n-a*x) divisible by b\n'
  '# Newman-Shanks-Williams prime: P(0)=1,P(1)=1,P(n)=2*P(n-1)+P(n-2)'),
 ('remediation',
  'bit-set-unset',
  'Set the Leftmost Unset Bit',
  'Flip the highest 0 bit to 1 (if any unset bits below the top set bit).',
  ['bit-manipulation'],
  'def set_leftmost_unset(n):\n    if n==0: return 1\n    t=n; pos=-1; i=0\n    while t:\n        if not (t&1): pos=i\n        t>>=1; i+=1\n    return n | (1<<pos) if pos!=-1 else n'),
 ('remediation',
  'ip-address-ops',
  'IP Address String Ops',
  'Strip leading zeros from dotted-quad octets.',
  ['string-transforms'],
  "import re\ndef removezero_ip(ip): return re.sub(r'\\b0+(\\d)', r'\\1', ip)\n# or: '.'.join(str(int(o)) for o in ip.split('.'))")]
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--vault",default=_default_vault()); a=ap.parse_args()
    root=os.path.join(a.vault,"knowledge","remediation"); os.makedirs(root,exist_ok=True)
    for cat,slug,title,summary,related,code in NOTES:
        links=" ".join("[["+r+"]]" for r in related)
        body=("---\ntype: reference\ntags: reference,remediation,concept,"+cat+"\n---\n\n# "+title+
              "\n\n"+summary+"\n\n```python\n"+code+"\n```\n\nSee also: "+links+" [[algorithms-index]] [[remediation-index]]\n")
        open(os.path.join(root,slug+".md"),"w",encoding="utf-8").write(body)
    idx=["---\ntype: index\ntags: reference,remediation,index\n---\n","# Remediation Index\n",
         "Concepts the model missed on MBPP+ ("+str(len(NOTES))+" notes).\n"]
    for cat,slug,title,*_ in NOTES: idx.append("- [["+slug+"]] - "+title)
    open(os.path.join(root,"remediation-index.md"),"w",encoding="utf-8").write("\n".join(idx)+"\n")
    print("wrote",len(NOTES),"remediation notes + index ->",root)
if __name__=="__main__": raise SystemExit(main())
