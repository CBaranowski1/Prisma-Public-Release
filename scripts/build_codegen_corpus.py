"""Grow the brain with REAL verified competitive solutions (not synthetic noise).

Ingests CodeContests (HuggingFace) problems + their reference solutions into the
vault as densely-wikilinked notes:
  knowledge/cc_solutions/<slug>-<i>.md   (problem + a correct Python solution)
  knowledge/cc_algos/algo-<tag>.md       (one hub note per algorithm tag)
Each solution note wikilinks to its algorithm tags ([[algo-greedy]], [[algo-dp]]),
so the graph gets dense and recall can pull "similar algorithm" exemplars.

  pip install datasets
  python scripts/build_codegen_corpus.py --limit 2000 --per-problem 3

SCALE WARNING: aiming for 100k notes means 100k files. On OneDrive this is slow
and lock-prone. Strongly prefer a vault OUTSIDE OneDrive (pass --vault C:\\dev\\vault).
"""
from __future__ import annotations
import argparse
import hashlib
import os
import re
import sys

_PY_LANGS = {1, 3}  # CodeContests language enum: 1=PYTHON, 3=PYTHON3
CODE_CONTESTS_REVISION = "802411c3010cb00d1b05bad57ca77365a3c699d6"


def _is_python(code: str, lang) -> bool:
    if lang in _PY_LANGS:
        return True
    if "#include" in code or "public static void main" in code:
        return False
    return ("def " in code) or ("input(" in code) or ("print(" in code)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")[:80] or "p"


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def build(args):
    from datasets import load_dataset
    ds = load_dataset(
        "deepmind/code_contests",
        split=args.split,
        streaming=True,
        revision=CODE_CONTESTS_REVISION,
    )
    sol_dir = os.path.join(args.vault, "knowledge", "cc_solutions")
    algo_dir = os.path.join(args.vault, "knowledge", "cc_algos")
    os.makedirs(sol_dir, exist_ok=True); os.makedirs(algo_dir, exist_ok=True)
    seen_tags, n_sol = set(), 0
    skip = getattr(args, 'skip', 0)
    for i, row in enumerate(ds):
        if i < skip:
            continue
        if args.limit and i >= skip + args.limit:
            break
        tags = [t for t in (row.get("cf_tags") or []) if t]
        for t in tags:
            ts = _slug(t)
            if ts not in seen_tags:
                seen_tags.add(ts)
                _write(os.path.join(algo_dir, f"algo-{ts}.md"),
                        f"---\ntype: algo-tag\ntags: algorithm,{ts}\n---\n\n# Algorithm: {t}\n\n"
                        f"Hub note for the **{t}** technique. Solutions tagged here link back via [[algo-{ts}]].\n")
        sols = row.get("solutions") or {}
        langs = sols.get("language") or []
        codes = sols.get("solution") or []
        kept = 0
        for lang, code in zip(langs, codes):
            if kept >= args.per_problem:
                break
            if not code or not _is_python(code, lang):
                continue
            name = row.get("name", f"p{i}")
            slug = _slug(name)
            taglinks = " ".join(f"[[algo-{_slug(t)}]]" for t in tags) or "(untagged)"
            desc = re.sub(r"\s+", " ", (row.get("description") or ""))[:600]
            body = (f"---\ntype: cc-solution\ntags: solution,verified,codegen,competitive\n"
                    f"problem: {name}\ndifficulty: {row.get('difficulty','?')}\n---\n\n"
                    f"# {name}\n\nAlgorithms: {taglinks}\n\n## Problem\n{desc}\n\n"
                    f"## Reference solution (Python)\n```python\n{code.strip()}\n```\n")
            _write(os.path.join(sol_dir, f"{slug}-{kept}.md"), body)
            kept += 1; n_sol += 1
    print(f"wrote {n_sol} solution notes + {len(seen_tags)} algorithm hub notes")
    print(f"-> {sol_dir}")
    print("Run Reindex Brain.bat next so recall can use them.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2000, help="problems to scan")
    ap.add_argument("--skip", type=int, default=0, help="skip first N problems (pagination)")
    ap.add_argument("--per-problem", type=int, default=3, help="python solutions per problem")
    ap.add_argument("--split", default="train")
    ap.add_argument("--vault", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "brains", "llm-council", "vault"))
    args = ap.parse_args()
    try:
        build(args)
    except Exception as e:
        print(f"[!] corpus build failed ({e}). pip install datasets; check network/disk."); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
