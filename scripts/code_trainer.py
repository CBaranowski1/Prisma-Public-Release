"""Prism code-training harness: generate -> sandbox-run -> score -> store.

Execution kinds: function (HumanEval/MBPP/BigCodeBench) and stdio (CodeContests/
LiveCodeBench). Self-repair (--attempts), test-verified best-of-N sampling
(--samples: temperature-diverse candidates, first passer wins), held-out baseline
(--no-brain), failure report (--show-fails), and brain-assisted generation. --hint-mode picks what gets
injected: 'techniques' (dense algo notes from knowledge/cc_algos, no providers
needed), 'solutions' (legacy --use-brain embedding recall of related VERIFIED
solutions, excluding the problem's own stored answer so the brain can't trivially
leak it), or 'mixed' (default: technique notes + at most one recalled solution).
Captures verified solutions + a per-run summary into the brain the app actually reads.

NOT weight fine-tuning. SAFETY: runs generated code in a subprocess with a timeout;
for large unattended runs use Docker/WSL.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import re
import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root for 


def _default_vault() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bj = os.path.join(base, "brains", "llm-council", "brain.json")
    try:
        vp = (json.load(open(bj, encoding="utf-8")).get("vault_path") or "").strip()
        if vp:
            return os.path.normpath(os.path.join(os.path.dirname(bj), vp))
    except Exception:
        pass
    return os.path.join(base, "brains", "llm-council", "vault")


def _norm(rec: dict) -> dict:
    rec.setdefault("kind", "stdio" if rec.get("io") else "function")
    return rec


def _slice(seq, args):
    """Apply --skip offset then --limit, so a run can use later, unseen problems."""
    sk = getattr(args, "skip", 0) or 0
    seq = seq[sk:]
    return seq[: args.limit or None]


def load_problems(args):
    if args.problems:
        with open(args.problems, encoding="utf-8") as f:
            return _slice([_norm(json.loads(l)) for l in f if l.strip()], args)
    name = (args.dataset or "humaneval").lower()
    if name == "bigcodebench":
        try:
            from bigcodebench.data import get_bigcodebench
            out = []
            for tid, p in get_bigcodebench().items():
                out.append(_norm({"task_id": tid, "prompt": p.get("complete_prompt") or p["prompt"],
                                  "test": p["test"], "entry_point": p["entry_point"],
                                  "canonical_solution": p.get("canonical_solution", "")}))
            return _slice(out, args)
        except Exception as e:
            print(f"[!] bigcodebench load failed ({e}). pip install bigcodebench, or use --problems."); sys.exit(2)
    try:
        if name == "humaneval":
            from evalplus.data import get_human_eval_plus as gp
            out = []
            for tid, p in gp().items():
                out.append(_norm({"task_id": tid, "prompt": p["prompt"], "test": p["test"],
                                  "entry_point": p["entry_point"], "kind": "function",
                                  "canonical_solution": p.get("canonical_solution", "")}))
            return _slice(out, args)
        else:
            # MBPP+ has no check(candidate) wrapper; its tests live in "assertion".
            from evalplus.data import get_mbpp_plus as gp
            out = []
            for tid, p in gp().items():
                out.append(_norm({"task_id": tid, "prompt": p["prompt"],
                                  "test": p.get("test") or p.get("assertion", ""),
                                  "entry_point": p["entry_point"], "kind": "assert",
                                  "canonical_solution": p.get("canonical_solution", "")}))
            return _slice(out, args)
    except Exception as e:
        print(f"[!] couldn't load '{name}' ({e}). Install evalplus/bigcodebench or pass --problems."); sys.exit(2)


def _recall_brain(providers, query, k):
    import inspect
    import asyncio
    import concurrent.futures
    def _resolve(v):
        if inspect.iscoroutine(v):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(v)
            with concurrent.futures.ThreadPoolExecutor(1) as ex:
                return ex.submit(lambda: asyncio.new_event_loop().run_until_complete(v)).result()
        return v
    out = []
    for p in providers:
        try:
            for h in (_resolve(p.recall(query, owner=None, top_k=k)) or []):
                m = getattr(h, "memory", None)
                out.append((getattr(m, "source", "") or getattr(h, "provider_id", ""),
                            getattr(m, "text", "")))
        except Exception:
            pass
    return out


def _brain_hints(problem, providers, k=3, cap=400):
    """Related verified solutions as hints; EXCLUDES the problem's own stored answer."""
    if not providers:
        return ""
    hits = _recall_brain(providers, (problem.get("prompt", "") or "")[:500], k + 4)
    tid = problem["task_id"].lower()
    tid_slug = re.sub(r"[^a-z0-9-]+", "-", tid).strip("-")  # matches stored filename slug
    lines = []
    for src, text in hits:
        low = (src or "").lower()
        if (tid and tid in low) or (tid_slug and tid_slug in low):
            continue
        lines.append(f"[{src}] {(text or '')[:cap]}")
        if len(lines) >= k:
            break
    return "\n".join(lines)


# --hint-mode techniques/mixed: keyword/regex -> algorithm-tag classifier table.
# Order matters: specific tags first, and earlier entries win score ties. Extend by
# adding patterns to a tag or a new "<tag>": [patterns] entry (a matching note must
# exist at <vault>/knowledge/cc_algos/algo-<tag>.md). "greedy" and "implementation"
# are fallbacks inside _detect_techniques, so they are not listed here.
_TECH_KEYWORDS = {
    "shortest-paths": [r"shortest\s+path", r"minimum\s+(?:total\s+)?distance", r"edge\s+weights?",
                       r"weighted\s+(?:directed\s+|undirected\s+)?graph"],
    "dsu": [r"connected\s+components?", r"merge\s+(?:the\s+)?(?:sets|components|queries)",
            r"union[\s-]find", r"disjoint\s+set"],
    "trees": [r"tree\s+with\s+\S+\s+vertices", r"rooted\s+(?:tree|at)", r"\bsubtree\b",
              r"\bancestors?\b", r"\bleaf\b|\bleaves\b"],
    "graphs": [r"\bgraph\b", r"\bvertices\b", r"\bedges\b", r"adjacen"],
    "binary-search": [r"minimi[sz]e\s+the\s+maximum", r"maximi[sz]e\s+the\s+minimum", r"monotonic",
                      r"binary\s+search", r"(?:smallest|largest)\s+\w+\s+such\s+that"],
    "combinatorics": [r"number\s+of\s+ways.{0,120}(?:mod(?:ulo)?\b|\bmod\b).{0,20}(?:10\^\{?9\}?\s*\+\s*7|1000000007|998244353)",
                      r"998244353", r"10\^\{?9\}?\s*\+\s*7|1000000007"],
    "dp": [r"number\s+of\s+ways", r"count\s+(?:the\s+)?(?:number\s+of\s+)?ways",
           r"(?:maximum|minimum|maximal|minimal)\s+(?:possible\s+)?(?:total\s+)?(?:sum|value|cost|score|profit|length|beauty)",
           r"expected\s+(?:value|number|cost|time)"],
    "games": [r"first\s+player\s+wins?", r"plays?\s+optimally", r"optimal\s+play", r"\bwinner\b",
              r"alice\s+and\s+bob"],
    "two-pointers": [r"sub-?array", r"substring", r"\bwindow\b",
                     r"contiguous\s+(?:segment|subsequence|part)"],
    "strings": [r"string\s+s\b", r"palindrom", r"\bprefix", r"\bsuffix",
                r"lowercase\s+(?:english\s+)?letters"],
    "number-theory": [r"divisible", r"\bdivisors?\b", r"\bprimes?\b", r"\bgcd\b",
                      r"greatest\s+common\s+divisor", r"\bmodulo\b", r"\bremainder\b"],
    "geometry": [r"points?\s+(?:on|in)\s+(?:the\s+|a\s+)?plane", r"coordinates", r"polygon",
                 r"euclidean", r"segments?\s+intersect"],
}


def _detect_techniques(problem) -> list[str]:
    """Classify the problem statement into at most 2 algorithm tags (best-first)
    via _TECH_KEYWORDS: score = number of matched patterns, ties go to earlier
    (more specific) entries. Falls back to greedy (if the problem optimizes
    something) and finally implementation."""
    text = " ".join((problem.get("prompt", "") or "").lower().split())
    scored = []
    for pri, (tag, pats) in enumerate(_TECH_KEYWORDS.items()):
        n = sum(1 for pat in pats if re.search(pat, text))
        if n:
            scored.append((-n, pri, tag))
    scored.sort()
    tags = [t for _, _, t in scored[:2]]
    if not tags:
        tags = ["greedy"] if re.search(r"minimi[sz]e|maximi[sz]e", text) else ["implementation"]
    return tags


def _technique_hints(problem, vault, max_notes=2, budget=1600):
    """Dense technique notes for the detected algorithm tags. Reads
    <vault>/knowledge/cc_algos/algo-<tag>.md, strips YAML frontmatter and the
    exemplar/link sections, and keeps the sections that help at generation time
    ("Complexity & limits" only if it still fits the per-note budget). Missing
    note files are skipped silently."""
    keep = ("When to reach for it", "Core patterns", "Pitfalls for generated code")
    parts = []
    for tag in _detect_techniques(problem)[:max_notes]:
        path = os.path.join(vault, "knowledge", "cc_algos", f"algo-{tag}.md")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
        secs = {m.group(1).strip(): m.group(2).strip()
                for m in re.finditer(r"(?ms)^##\s+(.+?)\s*\n(.*?)(?=^##\s|\Z)", text)}
        note = "\n\n".join(f"#### {h}\n{secs[h]}" for h in keep if h in secs)
        if not note:
            continue
        comp = secs.get("Complexity & limits", "")
        if comp and len(note) + len(comp) + 30 <= budget:
            note += "\n\n#### Complexity & limits\n" + comp
        parts.append(f"### Technique: {tag}\n" + note[:budget])
    return "\n\n".join(parts)


def _assemble_hints(problem, args, providers):
    """Build the full hint block (framing included) per --hint-mode. 'techniques'
    needs no providers; 'solutions' is the legacy recall-only behavior; 'mixed'
    adds at most ONE recalled solution (700 chars) under the technique notes."""
    mode = getattr(args, "hint_mode", "mixed")
    parts = []
    if mode in ("techniques", "mixed"):
        th = _technique_hints(problem, args.vault)
        if th:
            parts.append("Technique playbook (from your verified memory) — apply what fits, "
                         "do not copy blindly:\n" + th)
    if mode == "solutions" and providers:
        sh = _brain_hints(problem, providers)
        if sh:
            parts.append("Related VERIFIED solutions from your memory (reference, do NOT copy blindly):\n" + sh)
    elif mode == "mixed" and providers:
        # Inject ONE full verified solution — not a 700-char fragment. A truncated
        # solution is a broken, misleading snippet (the original reason brain hints
        # HURT); a whole verified program is something the model can actually adapt.
        sh = _brain_hints(problem, providers, k=1, cap=4000)
        if sh:
            parts.append("One related verified solution (reference only, adapt — do NOT copy blindly):\n" + sh)
    return "\n\n".join(parts)


# Error-signature -> targeted fix, so a FAILED attempt is repaired with a real
# lesson instead of just re-seeing its own traceback. This is the agentic part:
# observe the actual test result, diagnose the error class, retrieve the matching
# technique's pitfalls, and feed a concrete correction into the next attempt.
_ERROR_FIXES = [
    ("tle_gen", r"gen: timed out|timed out",
     "The generation/run TIMED OUT: the approach is too slow for the input size. "
     "Switch to O(n log n) or O(n) — fast sys.stdin I/O, prefix sums, two pointers, "
     "a heap, or a better data structure — not the brute-force default."),
    ("tle", r"time limit|timeout",
     "TIME LIMIT exceeded: reduce complexity or constant factor (buffered I/O with "
     "sys.stdin.buffer, avoid rebuilding structures in the inner loop, memoize)."),
    ("index", r"IndexError|index out of range",
     "IndexError: re-check every boundary and 1-indexed vs 0-indexed access; guard "
     "empty containers; make sure you read exactly as many tokens as the format gives."),
    ("parse", r"invalid literal for int|not enough values to unpack|EOFError|unpack",
     "INPUT PARSING bug: read the input EXACTLY as the statement specifies. Read all "
     "of stdin once (data = sys.stdin.buffer.read().split()) and index a cursor; do "
     "not assume line counts. The first token is often T (number of test cases)."),
    ("recursion", r"RecursionError|maximum recursion depth",
     "RecursionError: convert the recursion to an explicit stack/iteration, or set "
     "sys.setrecursionlimit high AND read input iteratively — Python recursion dies "
     "near depth 1000."),
    ("key", r"KeyError",
     "KeyError: use dict.get / collections.defaultdict, or check membership before access."),
    ("type", r"TypeError",
     "TypeError: check what each function returns and the types you unpack; a helper "
     "returning None or an int where a tuple/list is expected is the usual cause."),
    ("wrong", r"got '.*' want '.*'|case \d+:",
     "WRONG ANSWER on the sample: re-derive the small cases by hand (empty, n=1, all "
     "equal, negatives, overflow) and match the OUTPUT FORMAT exactly — spacing, "
     "newline vs space separators, the count line, trailing newline."),
]


def _pitfalls_for(tag, vault):
    """Pull just the 'Pitfalls for generated code' bullets from a technique note."""
    path = os.path.join(vault, "knowledge", "cc_algos", f"algo-{tag}.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r"(?ms)^##\s+Pitfalls for generated code\s*\n(.*?)(?=^##\s|\Z)", text)
    return (m.group(1).strip() if m else "")[:900]


def _repair_hint(problem, detail, args):
    """Diagnose the actual failure and assemble a targeted correction: the matching
    error-class fix PLUS the detected technique's pitfalls. Grounded in the real
    test output — this is retrieve-reason-adapt, not blind re-prompting."""
    if not detail:
        return ""
    bits = []
    for _name, pat, fix in _ERROR_FIXES:
        if re.search(pat, detail):
            bits.append(fix)
            break
    tags = _detect_techniques(problem)
    pit = _pitfalls_for(tags[0], args.vault) if tags else ""
    if pit:
        bits.append(f"Pitfalls for {tags[0]} problems:\n{pit}")
    if not bits:
        return ""
    return "# Diagnosis of the previous failure — fix THIS before retrying:\n" + "\n\n".join(bits) + "\n\n"


def generate(problem, args, feedback: str = "", model_override: str | None = None, deep: bool = False,
             temperature: float | None = None) -> str:
    if os.getenv("CODE_TRAINER_MOCK") == "1":
        return problem.get("canonical_solution", "")
    import httpx
    if problem.get("kind") == "stdio":
        sys_msg = ("You are an expert competitive programmer. Write a COMPLETE Python "
                   "program that reads ALL of stdin and prints answers to stdout. "
                   "IMPORTANT: the first line is often T = the number of test cases; "
                   "if so, loop T times and print one answer per case in order. "
                   "Read input with sys.stdin. Return ONLY a Python code block.")
    else:
        sys_msg = ("You are an expert Python engineer. Complete the function. "
                   "Return ONLY a Python code block, no prose.")
    if deep:
        sys_msg += (" This is a HARD problem the fast model could not solve. Reason step by step "
                    "about the algorithm, time/space complexity, and EVERY edge case before coding, "
                    "then output the complete, correct solution. Return ONLY a Python code block.")
    user = problem["prompt"]
    hints = getattr(args, "_hints", "") or ""
    if hints:  # framing text is already part of the block (see _assemble_hints)
        user = hints + "\n\n" + user
    if feedback:
        user += "\n\n# Previous attempt FAILED with:\n# " + feedback.replace("\n", "\n# ")
    r = httpx.post(args.ollama_url.rstrip("/") + "/chat/completions",
                   json={"model": model_override or args.model,
                         "temperature": temperature if temperature is not None else (0.1 if deep else 0.2),
                         "messages": [{"role": "system", "content": sys_msg},
                                      {"role": "user", "content": user}]},
                   timeout=getattr(args, "gen_timeout", 120))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def extract_code(text: str) -> str:
    m = re.findall(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return (m[0] if m else text).strip("\n")


def _exec(program, timeout, stdin="", expect=None):
    with tempfile.TemporaryDirectory() as d:
        fp = os.path.join(d, "cand.py")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(program)
        try:
            p = subprocess.run([sys.executable, fp], input=stdin, capture_output=True,
                               text=True, timeout=timeout, cwd=d)
        except subprocess.TimeoutExpired:
            return (False, "timeout")
        except Exception as e:
            return (False, str(e)[:200])
        if expect is None:
            return (p.returncode == 0, (p.stderr or "")[-300:])
        if p.returncode != 0:
            return (False, (p.stderr or "")[-200:])
        return (p.stdout.strip() == expect.strip(),
                f"got {p.stdout.strip()[:60]!r} want {expect.strip()[:60]!r}")


def _alias_entrypoint(code: str, ep: str) -> str:
    """If the model named the function with different casing than expected (e.g.
    idiomatic snake_case vs MBPP's odd capitalization like opposite_Signs), alias
    the expected name to what it actually defined so the tests can call it. Only
    aliases an EXACT case-insensitive match - never masks a wrong/absent function."""
    if not ep:
        return ""
    defs = re.findall(r"(?m)^\s*def\s+([A-Za-z_]\w*)\s*\(", code)
    if ep in defs:
        return ""
    for d in defs:
        if d.lower() == ep.lower():
            return f"\n{ep} = {d}\n"
    # MBPP/HumanEval are single-function tasks. If the model defined exactly ONE
    # top-level function under a different name (the dataset often mis-spells them,
    # e.g. "count_occurance", "lateralsuface_cylinder"), alias the expected name to
    # it - the model solved the right task, just named it sensibly. Only when there
    # is exactly one top-level def, so a helper-plus-main solution is never mis-aliased.
    tops = re.findall(r"(?m)^def\s+([A-Za-z_]\w*)\s*\(", code)
    if len(set(tops)) == 1 and tops[0] != ep:
        return f"\n{ep} = {tops[0]}\n"
    return ""


def _run_function(problem, completion, timeout):
    ep = problem["entry_point"]
    # Always include the prompt: HumanEval problems often define helper functions
    # (e.g. encode_cyclic) and imports there. If the model also redefines the target,
    # the later definition simply wins.
    head = problem["prompt"] + "\n" + completion
    program = head + _alias_entrypoint(head, ep) + "\n" + problem["test"] + f"\n\ncheck({ep})\n"
    return _exec(program, timeout)


def _run_stdio(problem, completion, timeout):
    for i, case in enumerate(problem.get("io", [])):
        ok, detail = _exec(completion, timeout, stdin=case.get("input", ""),
                           expect=str(case.get("output", "")))
        if not ok:
            return (False, f"case {i}: {detail}")
    return (True, "")


def _run_assert(problem, completion, timeout):
    # MBPP+ style: the completion defines the function, then plain `assert f(...)==...`
    # statements call it directly. No check() wrapper, no entry_point call.
    alias = _alias_entrypoint(completion, problem.get("entry_point", ""))
    program = completion + alias + "\n\n" + problem["test"]
    return _exec(program, timeout)


def run_tests(problem, completion, timeout):
    k = problem.get("kind")
    if k == "stdio":
        return _run_stdio(problem, completion, timeout)
    if k == "assert":
        return _run_assert(problem, completion, timeout)
    return _run_function(problem, completion, timeout)


def _sample_temps(n: int) -> list[float | None]:
    """Temperature ladder for --samples: candidate 1 keeps generate()'s configured
    temp (None), later candidates spread across 0.5-0.8 for diversity."""
    if n <= 1:
        return [None]
    return [None] + [round(0.5 + 0.3 * i / max(1, n - 2), 2) for i in range(n - 1)]


def sample_solutions(problem, args):
    """Test-verified best-of-N (--samples): generate candidates one at a time with
    temperature diversity, sandbox-run EACH against the problem's tests, and
    short-circuit on the first passer (remaining candidates are never generated).
    If none pass, keep the candidate with the SHORTEST failure output (heuristic:
    closest to working) so the --attempts repair loop can continue from its
    feedback. Returns (ok, code, detail, samples_used, sample_passed)."""
    used, best = 0, None  # best = (len(detail), code, detail)
    for idx, temp in enumerate(_sample_temps(args.samples)):
        try:
            code = extract_code(generate(problem, args, temperature=temp))
        except Exception:
            if best is None:
                raise
            break  # keep the best failing candidate instead of losing it to a flaky call
        used += 1
        ok, detail = run_tests(problem, code, args.timeout)
        if ok:
            return True, code, detail, used, idx
        print(f"    [sample {used}/{args.samples}] fail: "
              f"{(detail.splitlines()[-1] if detail else '?')[:80]}")
        if best is None or len(detail) < best[0]:
            best = (len(detail), code, detail)
    return False, best[1], best[2], used, -1


def store_solution(problem, completion, vault_dir, attempts):
    cat = os.path.join(vault_dir, "knowledge", "solutions"); os.makedirs(cat, exist_ok=True)
    slug = re.sub(r"[^a-z0-9-]+", "-", problem["task_id"].lower()).strip("-")[:80] or "sol"
    body = ("---\ntype: verified-solution\ntags: solution,verified,code,"
            + problem.get("kind", "function") + "\ntask: " + problem["task_id"]
            + "\nattempts: " + str(attempts) + "\n---\n\n# Verified solution: " + problem["task_id"]
            + "\n\nPassed all tests (in " + str(attempts) + " attempt(s)). See [[solutions-index]].\n\n"
            + "```python\n" + completion + "\n```\n")
    with open(os.path.join(cat, slug + ".md"), "w", encoding="utf-8") as f:
        f.write(body)


def store_gap_note(problem, detail, vault_dir):
    """On failure: immediately write a node capturing the unsolved problem + error, so
    the brain remembers this weak spot and recall surfaces the right technique next time.
    Retired automatically if a later side-solve passes."""
    cat = os.path.join(vault_dir, "knowledge", "gaps"); os.makedirs(cat, exist_ok=True)
    slug = re.sub(r"[^a-z0-9-]+", "-", problem["task_id"].lower()).strip("-")[:80] or "gap"
    err = (((detail or "").strip().splitlines() or ["unknown"])[-1])[:200]
    stmt = " ".join((problem.get("prompt", "") or "").split())[:300]
    body = ("---\ntype: gap\ntags: gap,unsolved,code," + problem.get("kind", "function")
            + "\ntask: " + problem["task_id"] + "\n---\n\n# Unsolved: " + problem["task_id"]
            + "\n\nThe model FAILED this. Last error: `" + err + "`\n\nProblem: " + stmt
            + "\n\nThe technique needed lives in [[algorithms-index]] / [[remediation-index]]; "
              "recall these when a similar problem appears.\n")
    path = os.path.join(cat, "gap-" + slug + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def store_run_summary(vault_dir, meta):
    cat = os.path.join(vault_dir, "knowledge", "training_runs"); os.makedirs(cat, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = ["---\ntype: training-run\ntags: training,eval,code\n---\n", f"# Training run {ts}", "",
             f"- dataset: {meta['dataset']}", f"- model: {meta['model']}",
             f"- brain-assisted: {meta['use_brain']}", f"- hint-mode: {meta.get('hint_mode', 'solutions')}",
             f"- attempts/problem: {meta['attempts']}", f"- samples/problem: {meta.get('samples', 1)}",
             f"- pass@1: {meta['passed']}/{meta['total']} = {meta['rate']:.1f}%",
             f"- escalated (written by strong model): {meta.get('escalated', 0)}", "",
             f"PASSED: {', '.join(meta['passed_ids']) or '(none)'}", "",
             f"FAILED: {', '.join(meta['failed_ids']) or '(none)'}", "", "Builds on [[solutions-index]]."]
    with open(os.path.join(cat, f"run-{ts}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def team_review(problem, code, args, detail=None):
    """An EXECUTION-GROUNDED reviewer teammate (the 'reasoner' role). It critiques
    the draft with the REAL test result in hand — a blind reviewer just guesses, a
    real teammate knows what actually failed. Pass `detail` (the failure output from
    a run already done) to avoid re-executing; otherwise it runs the tests itself and
    returns LGTM cheaply if the draft already passes."""
    if detail is None:
        ok, detail = run_tests(problem, code, args.timeout)
        if ok:
            return "LGTM"
    if os.getenv("CODE_TRAINER_MOCK") == "1":
        return os.getenv("CODE_TRAINER_MOCK_REVIEW", "")
    import httpx
    sysp = ("You are a senior code reviewer on a coding team. The SOLUTION was RUN and "
            "FAILED with the RESULT shown. Explain the root cause and give the concrete "
            "fix as a few short bullets. Focus on the exact failure, not style.")
    rmodel = (getattr(args, "reviewer_model", "") or args.model)
    diag = _repair_hint(problem, detail, args)
    user = ("PROBLEM:\n" + (problem.get("prompt", "") or "")[:1500]
            + "\n\nSOLUTION:\n" + code
            + "\n\nTEST RESULT (failure):\n" + (detail or "")[:600]
            + ("\n\n" + diag if diag else ""))
    r = httpx.post(args.ollama_url.rstrip("/") + "/chat/completions",
                   json={"model": rmodel, "temperature": 0.2,
                         "messages": [{"role": "system", "content": sysp},
                                      {"role": "user", "content": user}]},
                   timeout=getattr(args, "gen_timeout", 120))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="humaneval")
    ap.add_argument("--problems", default="")
    ap.add_argument("--model", default="vera-coder")
    ap.add_argument("--ollama-url", default=os.getenv("OLLAMA_OPENAI_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip", type=int, default=0, help="skip the first N problems (use later, unseen questions)")
    ap.add_argument("--attempts", type=int, default=1)
    ap.add_argument("--samples", type=int, default=1,
                    help="test-verified best-of-N: generate up to N candidates per problem with "
                         "temperature diversity, run each against the tests, accept the FIRST "
                         "passer (short-circuit); --attempts repair starts only after all N fail, "
                         "seeded with the best candidate's feedback (1 = off)")
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--gen-timeout", type=int, default=120, help="seconds for each model generation call (the 30B is slow; avoids premature cut-offs)")
    ap.add_argument("--out", default="code_train_results.jsonl")
    ap.add_argument("--store-brain", action="store_true")
    ap.add_argument("--no-brain", action="store_true")
    ap.add_argument("--show-fails", action="store_true")
    ap.add_argument("--use-brain", action="store_true",
                    help="enable embedding recall of related verified solutions (feeds --hint-mode "
                         "solutions/mixed; --hint-mode techniques works WITHOUT this)")
    ap.add_argument("--hint-mode", choices=["solutions", "techniques", "mixed"], default="mixed",
                    help="what to inject as hints: 'solutions'=legacy recalled solutions (needs "
                         "--use-brain), 'techniques'=dense algo notes from <vault>/knowledge/cc_algos "
                         "(no providers needed), 'mixed'=techniques + at most 1 recalled solution (default)")
    ap.add_argument("--team", action="store_true",
                    help="coding team: a reviewer agent critiques the draft, then the coder revises, before testing")
    ap.add_argument("--reviewer-model", default="",
                    help="reviewer model for --team (default: same as --model, to avoid VRAM swap)")
    ap.add_argument("--escalate-model", default="",
                    help="strong model (e.g. the 30B) that AUTHORS a final deep-reasoning attempt when the solver fails")
    ap.add_argument("--adapt", action="store_true",
                    help="continuous self-adaptation: on EVERY failure, immediately write a gap node "
                         "to the brain and try a brain-assisted deep side-solve; store it if it passes")
    ap.add_argument("--vault", default=_default_vault())
    args = ap.parse_args()
    store = args.store_brain and not args.no_brain

    providers = None
    if args.use_brain:
        try:
            from src.brains import build_brain_providers
            providers = build_brain_providers()
            print(f"brain-assisted generation: {len(providers)} providers")
        except Exception as e:
            print(f"[!] --use-brain unavailable ({e}); running raw")

    probs = load_problems(args)
    kinds = {}
    for p in probs:
        kinds[p.get("kind", "function")] = kinds.get(p.get("kind", "function"), 0) + 1
    print(f"loaded {len(probs)} problems {kinds}; model={args.model}; attempts={args.attempts}; "
          f"samples={args.samples}; use_brain={bool(providers)}; hint_mode={args.hint_mode}; "
          f"store={'on' if store else 'off'}")

    passed_ids, failed_ids, fails = [], [], []
    esc_pass = 0
    adapt_solved = 0
    t0 = time.time()
    with open(args.out, "w", encoding="utf-8") as out:
        for i, p in enumerate(probs, 1):
            args._hints = _assemble_hints(p, args, providers)
            if args._hints:
                tags = ",".join(_detect_techniques(p)) if args.hint_mode != "solutions" else "(recall)"
                print(f"    [hint] mode={args.hint_mode} techniques={tags} chars={len(args._hints)}")
            ok, detail, used, feedback = False, "", 0, ""
            samples_used, sample_passed = 0, -1
            for attempt in range(1, max(1, args.attempts) + 1):
                used = attempt
                # Best-of-N replaces the FIRST draft only; --attempts repair kicks in
                # after all samples fail, seeded with the best candidate's feedback.
                sampled = attempt == 1 and args.samples > 1
                try:
                    if sampled:
                        # Coder role: best-of-N drafts, first test-passer wins.
                        ok, code, detail, samples_used, sample_passed = sample_solutions(p, args)
                    else:
                        # Coder role: write/revise from the reasoner's last feedback.
                        code = extract_code(generate(p, args, feedback))
                except Exception as e:
                    detail = f"gen: {e}"; break
                if not sampled:
                    # Tester role (real tool): EXECUTE the draft — ground truth.
                    ok, detail = run_tests(p, code, args.timeout)
                    if attempt == 1:
                        samples_used, sample_passed = 1, (0 if ok else -1)
                if ok:
                    if store:
                        try: store_solution(p, code, args.vault, attempt)
                        except Exception: pass
                    break
                # --- Real multi-agent dialogue on failure, grounded in the ACTUAL
                # test result. Deterministic diagnosis (error class + brain technique
                # pitfalls) is always fed forward; when --team, a distinct reasoner
                # model additionally reasons about THIS failure and proposes a fix.
                # This fires every round (not just attempt 1), so the coder and
                # reasoner genuinely iterate toward a passing solution.
                feedback = _repair_hint(p, detail, args)
                if args.team and attempt < args.attempts:
                    try:
                        crit = team_review(p, code, args, detail=detail)
                        if crit and "lgtm" not in crit.lower():
                            feedback = ("Reviewer teammate (ran your code, saw the real failure):\n"
                                        + crit + "\n\n" + feedback)
                    except Exception:
                        pass
                feedback += detail
            # Escalation: hand the unsolved problem to the strong model for one
            # final deep-reasoning attempt — the 30B WRITES the hard ones.
            escalated = False
            if not ok and args.escalate_model and args.escalate_model.lower() != args.model.lower():
                escalated = True
                try:
                    code = extract_code(generate(p, args, feedback, model_override=args.escalate_model, deep=True))
                    ok, detail = run_tests(p, code, args.timeout)
                    if ok and store:
                        try: store_solution(p, code, args.vault, used + 1)
                        except Exception: pass
                except Exception as e:
                    detail = f"escalate: {e}"
                if ok:
                    esc_pass += 1
            # Continuous adaptation: turn every remaining failure into a brain node NOW,
            # then try to solve it on the side using the accumulated toolkit + remediation.
            adapted = False
            if not ok and args.adapt and not args.no_brain:
                try: _gap = store_gap_note(p, detail, args.vault)
                except Exception: _gap = None
                try:
                    args._hints = _assemble_hints(p, args, providers) or args._hints
                    code = extract_code(generate(p, args,
                        "This problem was MISSED. Use your reference knowledge (formulas, edge "
                        "cases, idioms) to solve it correctly.\n" + feedback, deep=True))
                    ok, detail = run_tests(p, code, args.timeout)
                    if ok:
                        adapted = True; adapt_solved += 1
                        if store:
                            try: store_solution(p, code, args.vault, used + 2)
                            except Exception: pass
                        if _gap:
                            try: os.remove(_gap)   # solved now -> retire the gap node
                            except Exception: pass
                except Exception as e:
                    detail = f"adapt: {e}"
            (passed_ids if ok else failed_ids).append(p["task_id"])
            if not ok:
                fails.append({"task_id": p["task_id"], "detail": detail})
            out.write(json.dumps({"task_id": p["task_id"], "passed": ok, "attempts": used,
                                  "samples_used": samples_used, "sample_passed": sample_passed,
                                  "escalated": escalated, "adapted": adapted, "detail": detail}) + "\n")
            tag = ("PASS [adapted]" if adapted else "PASS [30B]" if escalated else "PASS") if ok else "FAIL"
            print(f"  {i}/{len(probs)} {p['task_id']}: {tag}" + (f" (attempt {used})" if used > 1 else "")
                  + ((f" (sample {sample_passed + 1}/{samples_used})" if sample_passed >= 0
                      else f" (samples {samples_used})") if args.samples > 1 else ""))

    n = len(probs); pc = len(passed_ids); rate = 100 * pc / max(1, n)
    print(f"\npass@1: {pc}/{n} = {rate:.1f}%  ({time.time()-t0:.0f}s)  brain-assisted={bool(providers)}"
          + (f"  (+{esc_pass} written by {args.escalate_model})" if esc_pass else "")
          + (f"  (+{adapt_solved} solved on-the-side via adaptation)" if adapt_solved else ""))
    if args.show_fails and fails:
        print("FAILURES:")
        for f in fails:
            print(f"  - {f['task_id']}: {(f['detail'].splitlines()[-1] if f['detail'] else '?')[:100]}")
        with open(args.out + ".fails.json", "w", encoding="utf-8") as ff:
            json.dump(fails, ff, indent=2)
    if store:
        try:
            store_run_summary(args.vault, {"dataset": args.problems or args.dataset, "model": args.model,
                "use_brain": bool(providers), "hint_mode": args.hint_mode,
                "attempts": args.attempts, "samples": args.samples, "passed": pc, "total": n,
                "rate": rate, "passed_ids": passed_ids, "failed_ids": failed_ids, "escalated": esc_pass, "adapted": adapt_solved})
            print("[brain] stored verified solutions + a training-run summary.")
        except Exception as e:
            print(f"[brain] summary store failed: {e}")
    print(f"results -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
