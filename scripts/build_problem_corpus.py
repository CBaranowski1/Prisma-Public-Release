"""Ingest open coding-problem datasets (LeetCode mirror, basic exercises, MBPP+)
into the vault as wikilinked notes. Legal datasets, NOT live-site scraping.

  python scripts/build_problem_corpus.py --source leetcode --vault <vault>
  python scripts/build_problem_corpus.py --source exercises --limit 8000 --skip 0 --vault <vault>
  python scripts/build_problem_corpus.py --source mbppplus --vault <vault>
"""
from __future__ import annotations
import argparse
import os
import re

SOURCES = {
    "leetcode": (
        "greengerong/leetcode", "train", None,
        "00f2d466dc0f00f65a0b6938c4c11a57f721db81",
    ),
    "exercises": (
        "jinaai/code_exercises", "train", None,
        "998698fc883c9885d966481e4a2dc7eaf7a85738",
    ),
    "mbppplus": (
        "evalplus/mbppplus", "test", None,
        "b2d74c91837c3f2a20c1299ae98133cbe7cfa077",
    ),
    "leetcode_cpp": (
        "greengerong/leetcode", "train", None,
        "00f2d466dc0f00f65a0b6938c4c11a57f721db81",
    ),
    "go_qa": (
        "ExAi/Code-Golang-QA-2k", "train", None,
        "7f60e9fa9b5a76ded5e6cd2cf52c841382dbca2b",
    ),
    "python_lc": (
        "newfacade/LeetCodeDataset", "train", None,
        "215604aeed660029df7de2fea5a4d7b6ed476a08",
    ),
    "rust": (
        "HuggingFaceTB/stack-edu", "train", "Rust",
        "eeec5caac5cc3758a18f1d3ba4416837a9ba814c",
    ),
}


def _slug(s, i):
    return (re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")[:80]) or f"p{i}"


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _code(v):
    if not v:
        return ""
    m = re.search(r"```(?:python)?\s*(.*?)```", v, re.DOTALL)
    return (m.group(1) if m else v).strip()


def note_leetcode(r, i):
    diff = (r.get("difficulty") or "unknown").strip().lower()
    title = r.get("title") or r.get("slug") or f"problem{i}"
    py = _code(r.get("python"))
    body = (f"---\ntype: leetcode\ntags: leetcode,problem,solution,codegen,{diff}\n"
            f"difficulty: {diff}\n---\n\n# {title}\n\n"
            f"Difficulty: [[difficulty-{diff}]]  ·  See [[leetcode-index]]\n\n"
            f"## Problem\n{(r.get('content') or '').strip()[:1500]}\n\n"
            f"## Python solution\n```python\n{py}\n```\n")
    return "leetcode", _slug(r.get("slug") or title, i), body


def note_exercises(r, i):
    prob = (r.get("problem") or "").strip()
    sol = (r.get("solution") or "").strip()
    name = re.search(r"def\s+([a-zA-Z_]\w*)", prob)
    title = name.group(1) if name else f"exercise-{i}"
    body = (f"---\ntype: exercise\ntags: python,basic,problem,solution,codegen\n---\n\n"
            f"# {title}\n\nSee [[basic-exercises]]\n\n## Problem\n```python\n{prob[:1200]}\n```\n\n"
            f"## Solution\n```python\n{sol[:1500]}\n```\n")
    return "exercises", _slug(title, i), body


def note_mbppplus(r, i):
    tid = r.get("task_id") or f"mbpp-{i}"
    body = (f"---\ntype: mbpp\ntags: python,basic,problem,solution,codegen\ntask: {tid}\n---\n\n"
            f"# {tid}\n\nSee [[basic-exercises]]\n\n## Problem\n{(r.get('prompt') or '').strip()[:1000]}\n\n"
            f"## Reference solution\n```python\n{(r.get('canonical_solution') or '').strip()}\n```\n")
    return "mbpp", _slug(str(tid), i), body


def note_leetcode_cpp(r, i):
    cpp = _code(r.get("c++"))
    diff = (r.get("difficulty") or "unknown").strip().lower()
    title = r.get("title") or r.get("slug") or f"p{i}"
    body = (f"---\ntype: leetcode-cpp\ntags: leetcode,cpp,c++,solution,codegen,{diff}\ndifficulty: {diff}\n---\n\n"
            f"# {title} (C++)\n\nDifficulty: [[difficulty-{diff}]] · [[leetcode-index]] · lang [[lang-cpp]]\n\n"
            f"## Problem\n{(r.get('content') or '').strip()[:1200]}\n\n## C++ solution\n```cpp\n{cpp}\n```\n")
    return "leetcode_cpp", _slug(r.get("slug") or title, i), body


def note_go_qa(r, i):
    q = (r.get("question") or "").strip(); a = (r.get("answer") or "").strip()
    body = (f"---\ntype: go-qa\ntags: go,golang,code,solution,codegen\n---\n\n"
            f"# {q[:80]}\n\nLanguage: [[lang-go]] · See [[go-qa]]\n\n## Question\n{q}\n\n## Answer\n{a}\n")
    return "go_qa", _slug(q, i), body


def note_python_lc(r, i):
    sol = (r.get("completion") or "").strip()
    tid = r.get("task_id") or f"lc{i}"; diff = (r.get("difficulty") or "").strip().lower()
    tags = r.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,;]", tags) if t.strip()]
    taglinks = " ".join(f"[[pytag-{_slug(t,0)}]]" for t in tags) or "(untagged)"
    body = (f"---\ntype: leetcode-python\ntags: leetcode,python,solution,codegen,{diff}\ndifficulty: {diff}\n---\n\n"
            f"# {tid} (Python)\n\nDifficulty: [[difficulty-{diff}]] · lang [[lang-python]] · {taglinks}\n\n"
            f"## Problem\n{(r.get('problem_description') or '').strip()[:1400]}\n\n## Python solution\n```python\n{sol}\n```\n")
    return "python_lc", _slug(tid, i), body


def note_rust(r, i):
    code = (r.get("text") or r.get("content") or "").strip()
    title = os.path.basename(str(r.get("path") or f"rust-{i}"))
    body = (f"---\ntype: rust-code\ntags: rust,code,codegen\n---\n\n# {title}\n\n"
            f"Language: [[lang-rust]] · See [[rust-code]]\n\n```rust\n{code[:3000]}\n```\n")
    return "rust", _slug(title, i), body


NOTERS = {"leetcode": note_leetcode, "exercises": note_exercises, "mbppplus": note_mbppplus,
          "leetcode_cpp": note_leetcode_cpp, "go_qa": note_go_qa,
          "python_lc": note_python_lc, "rust": note_rust}


def build(args):
    from datasets import load_dataset
    spec = SOURCES[args.source]
    name, split = spec[0], spec[1]
    cfg, revision = spec[2], spec[3]
    ds = load_dataset(
        name,
        split=split,
        streaming=True,
        name=cfg,
        revision=revision,
    )
    noter = NOTERS[args.source]
    diffs, n = set(), 0
    for i, row in enumerate(ds):
        if i < args.skip:
            continue
        if args.limit and i >= args.skip + args.limit:
            break
        cat, slug, body = noter(row, i)
        d = os.path.join(args.vault, "knowledge", cat); os.makedirs(d, exist_ok=True)
        _write(os.path.join(d, slug + ".md"), body)
        n += 1
        diff = (row.get("difficulty") or "").strip().lower() if args.source == "leetcode" else ""
        if diff and diff not in diffs:
            diffs.add(diff)
            hub = os.path.join(args.vault, "knowledge", "leetcode")
            _write(os.path.join(hub, f"difficulty-{diff}.md"),
                    f"---\ntype: difficulty-hub\ntags: leetcode,{diff}\n---\n\n# {diff.title()} problems\n\nHub for {diff} LeetCode problems.\n")
    print(f"wrote {n} {args.source} notes -> {os.path.join(args.vault,'knowledge')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=list(SOURCES))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--vault", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "brains", "llm-council", "vault"))
    args = ap.parse_args()
    try:
        build(args)
    except Exception as e:
        print(f"[!] failed ({e})"); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
