"""Close the learning loop: evolve the competitive-programming brain from failures.

  python scripts/evolve_brain.py --fails run.jsonl.fails.json

Reads one or more `*.fails.json` files (the failing-task records code_trainer writes
with --show-fails), buckets each failure by algorithm tag using code_trainer's own
`_detect_techniques`, then DETERMINISTICALLY (regex/frequency over the error text — NO
LLM, safe to run offline in the training loop) synthesizes up to 3 NEW, specific,
generalizable "pitfall" bullets per technique and APPENDS them under the
"## Pitfalls for generated code" section of the matching
<vault>/knowledge/cc_algos/algo-<tag>.md.

Bullets go under a dated `<!-- auto-learned YYYY-MM-DD -->` subheading so runs are
idempotent and traceable; a bullet whose normalized text already exists anywhere in
the note is skipped, so re-running never duplicates. Human content is never rewritten,
only appended to. Every run also appends one line to
knowledge/cc_algos/_evolution_log.md (which links [[cc-solutions-index]] to stay
graph-connected), and emits a JSON + human summary of what was learned.

  --dry-run   print what WOULD be learned/appended without writing anything.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
import code_trainer as ct  # reuse _detect_techniques + _default_vault (single source of truth)


# ---------------------------------------------------------------------------
# 1. Load failing records (tolerant to the two shapes seen in the wild):
#    - code_trainer's fails.json: {"task_id", "detail"}  (detail = last error text)
#    - richer exports:            {"task_id", "prompt", "error"/"feedback", "code"/"completion"}
# ---------------------------------------------------------------------------
def _err_text(rec: dict) -> str:
    for k in ("detail", "error", "feedback", "stderr", "traceback", "message"):
        v = rec.get(k)
        if v:
            return str(v)
    return ""


def _prompt_text(rec: dict) -> str:
    for k in ("prompt", "statement", "problem"):
        v = rec.get(k)
        if v:
            return str(v)
    return ""


def load_fails(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[!] skipping {p}: {e}")
            continue
        # accept a bare list, or {"fails": [...]} / {"failures": [...]}
        if isinstance(data, dict):
            data = data.get("fails") or data.get("failures") or [data]
        for rec in data:
            if isinstance(rec, dict) and rec.get("task_id"):
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# 2. Deterministic error-signature classifier. Each rule: (name, regex, template).
#    The template turns a recurring signature into a specific, generalizable bullet.
#    NO LLM — pure regex + frequency counting, so it runs in the offline loop.
# ---------------------------------------------------------------------------
_SIGNATURES: list[tuple[str, re.Pattern, str]] = [
    ("recursion", re.compile(r"recursion|maximum recursion depth|recursionerror", re.I),
     "Deep recursion blew the stack ({n} failure(s)): convert DFS/recursion to an "
     "explicit iterative stack (or raise sys.setrecursionlimit early) — default depth "
     "~1000 is far below competitive-graph/tree sizes."),
    ("index", re.compile(r"indexerror|list index out of range|string index out of range", re.I),
     "IndexError recurred ({n} failure(s)): guard boundaries and 1-indexed vs 0-indexed "
     "access — size arrays as n+1 and stay consistent, and clamp neighbor/child indices "
     "before dereferencing adjacency or DP tables."),
    ("keyerror", re.compile(r"keyerror", re.I),
     "KeyError recurred ({n} failure(s)): a lookup key was never inserted — use "
     "dict.get / defaultdict / setdefault for adjacency, memo, or frequency maps instead "
     "of bare subscripting."),
    ("tle", re.compile(r"\btime\s*limit|\btimeout\b|timelimitexceeded|\btle\b|took too long", re.I),
     "Time-limit / timeout recurred ({n} failure(s)): the reached-for approach is too "
     "slow for the input size — target an O(n log n) or O(n) method (fast sys.stdin I/O, "
     "prefix sums, two pointers, or a better data structure) rather than the O(n^2)/"
     "brute-force default."),
    ("modulo", re.compile(r"\bmod(?:ulo)?\b|1000000007|998244353|10\^9\s*\+\s*7|10\*\*9\s*\+\s*7|overflow", re.I),
     "Modular-arithmetic mistake recurred ({n} failure(s)): take the answer mod "
     "1_000_000_007 (or 998244353) after EVERY add/multiply, keep intermediates reduced, "
     "and use pow(a, m-2, m) for modular inverse — never plain division."),
    ("wrong_answer", re.compile(r"\bgot\b.*\bwant\b|assertionerror|wrong answer|mismatch|expected .* but", re.I),
     "Wrong-answer on the samples recurred ({n} failure(s)): re-derive the edge cases "
     "(empty input, n=1, all-equal, negatives/overflow) and validate the output FORMAT "
     "(trailing newline, spacing, count line) against the statement before submitting."),
    ("valueerror", re.compile(r"valueerror|invalid literal|not enough values to unpack|too many values to unpack", re.I),
     "Input-parsing ValueError recurred ({n} failure(s)): the number of tokens/lines read "
     "did not match the statement — read all of stdin once and consume tokens explicitly "
     "(handle the leading T test-case count and multi-value lines)."),
    ("typeerror", re.compile(r"typeerror", re.I),
     "TypeError recurred ({n} failure(s)): a value had the wrong type (str vs int from "
     "unparsed input, or None from a function with a missing return) — cast parsed tokens "
     "with int()/map(int, ...) and ensure every branch returns a value."),
    ("zerodiv", re.compile(r"zerodivisionerror|division by zero", re.I),
     "Division-by-zero recurred ({n} failure(s)): guard the zero denominator (empty set, "
     "n=0, gcd degenerate case) before dividing or taking a modulus."),
    ("timeout_gen", re.compile(r"\btimeout\b", re.I),  # fallback (rarely reached; tle above wins)
     "Execution timed out ({n} failure(s)): reduce the algorithmic complexity or the "
     "constant factor (buffered I/O, avoid rebuilding structures in the inner loop)."),
    ("memory", re.compile(r"memoryerror|out of memory|\bmle\b", re.I),
     "Memory-limit recurred ({n} failure(s)): shrink the working set — avoid materializing "
     "O(n^2) tables, use arrays/bytearray over dict-of-dict, and stream input."),
]


def classify_error(text: str) -> list[str]:
    """Return the signature names matched by this one error string (deterministic)."""
    hits = []
    for name, rx, _tmpl in _SIGNATURES:
        if rx.search(text or ""):
            hits.append(name)
    return hits


_TMPL = {name: tmpl for name, _rx, tmpl in _SIGNATURES}


def synthesize_bullets(sig_counts: dict[str, int], max_bullets: int = 3) -> list[str]:
    """Turn per-technique signature frequencies into up to `max_bullets` bullets,
    most-frequent first (ties broken by _SIGNATURES declaration order for determinism)."""
    order = {name: i for i, (name, _r, _t) in enumerate(_SIGNATURES)}
    ranked = sorted(sig_counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))
    bullets = []
    for name, n in ranked:
        if n <= 0:
            continue
        bullets.append(_TMPL[name].format(n=n))
        if len(bullets) >= max_bullets:
            break
    return bullets


# ---------------------------------------------------------------------------
# 3. Note appending + idempotency.
# ---------------------------------------------------------------------------
_PITFALL_HDR = "## Pitfalls for generated code"


def _normalize(s: str) -> str:
    """Loose normalization for dedupe: lowercase, strip markdown/count noise, collapse ws."""
    s = s.lower()
    s = re.sub(r"\(\s*\d+\s*failure\(s\)\s*\)", "", s)  # drop the "(N failure(s))" count
    s = re.sub(r"[`*_>#\-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _existing_normalized_bullets(note_text: str) -> set[str]:
    out = set()
    for line in note_text.splitlines():
        st = line.strip()
        if st.startswith("- "):
            out.add(_normalize(st[2:]))
    return out


def note_path(vault: str, tag: str) -> str:
    return os.path.join(vault, "knowledge", "cc_algos", f"algo-{tag}.md")


def append_bullets(vault: str, tag: str, bullets: list[str], today: str,
                   dry_run: bool) -> tuple[list[str], str]:
    """Append `bullets` under the Pitfalls section of algo-<tag>.md, skipping any whose
    normalized text already exists. Returns (bullets_actually_added, status)."""
    path = note_path(vault, tag)
    if not os.path.exists(path):
        return [], "no-note"
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if _PITFALL_HDR not in text:
        return [], "no-pitfall-section"

    existing = _existing_normalized_bullets(text)
    fresh = []
    for b in bullets:
        nb = _normalize(b)
        if nb and nb not in existing:
            fresh.append(b)
            existing.add(nb)  # guard against intra-batch dupes too
    if not fresh:
        return [], "nothing-new"

    marker = f"<!-- auto-learned {today} -->"
    block = "\n".join(f"- {b}" for b in fresh)

    if marker in text:
        # Append to today's existing auto-learned block (keeps one dated subheading/day).
        insert_at = text.index(marker) + len(marker)
        # place after the marker line
        nl = text.find("\n", insert_at)
        nl = nl if nl != -1 else len(text)
        new_text = text[:nl] + "\n" + block + text[nl:]
    else:
        # Insert a fresh dated subheading at the END of the Pitfalls section (before the
        # next "## " header or EOF) so it stays inside the right section.
        hdr_at = text.index(_PITFALL_HDR) + len(_PITFALL_HDR)
        nxt = text.find("\n## ", hdr_at)
        sec_end = nxt if nxt != -1 else len(text)
        addition = f"\n\n{marker}\n{block}\n"
        new_text = text[:sec_end].rstrip() + "\n" + addition + text[sec_end:]

    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    return fresh, "appended"


# ---------------------------------------------------------------------------
# 4. Provenance ledger.
# ---------------------------------------------------------------------------
_LOG_NAME = "_evolution_log.md"


def append_log(vault: str, fails_paths: list[str], n_fails: int,
               added_per_tag: dict[str, int], today: str, dry_run: bool) -> str:
    path = os.path.join(vault, "knowledge", "cc_algos", _LOG_NAME)
    header = ("---\ntype: evolution-log\ntags: log,evolution,codegen\n---\n\n"
              "# Brain Evolution Log\n\n"
              "Deterministic (no-LLM) provenance for `evolve_brain.py`: one line per run — "
              "date, fails processed, and pitfall bullets added per algorithm tag. "
              "Part of [[cc-solutions-index]].\n\n")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    srcs = ", ".join(os.path.basename(p) for p in fails_paths) or "(none)"
    added = ", ".join(f"{t}:{c}" for t, c in sorted(added_per_tag.items()) if c) or "none"
    line = f"- {ts} | fails={n_fails} | sources={srcs} | added: {added}\n"
    if dry_run:
        return line
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if not exists:
            f.write(header)
        f.write(line)
    return line


# ---------------------------------------------------------------------------
# 5. Orchestration.
# ---------------------------------------------------------------------------
def evolve(fails_paths: list[str], vault: str, today: str, dry_run: bool,
           max_bullets: int = 3) -> dict:
    fails = load_fails(fails_paths)

    # Bucket failures by technique tag; collect error-signature frequencies per tag.
    tag_sig: dict[str, dict[str, int]] = {}
    tag_examples: dict[str, list[str]] = {}
    unclassified = 0
    for rec in fails:
        prob = {"prompt": _prompt_text(rec), "task_id": rec.get("task_id", "")}
        tags = ct._detect_techniques(prob)
        sigs = classify_error(_err_text(rec))
        if not sigs:
            unclassified += 1
            continue
        for tag in tags:
            sc = tag_sig.setdefault(tag, {})
            for s in sigs:
                sc[s] = sc.get(s, 0) + 1
            tag_examples.setdefault(tag, []).append(rec.get("task_id", ""))

    report = {"date": today, "fails_processed": len(fails), "sources": fails_paths,
              "unclassified": unclassified, "dry_run": dry_run, "techniques": {}}
    added_per_tag: dict[str, int] = {}

    for tag in sorted(tag_sig):
        bullets = synthesize_bullets(tag_sig[tag], max_bullets)
        added, status = append_bullets(vault, tag, bullets, today, dry_run)
        added_per_tag[tag] = len(added)
        report["techniques"][tag] = {
            "failures": sum(tag_sig[tag].values()),
            "signatures": dict(sorted(tag_sig[tag].items(), key=lambda kv: -kv[1])),
            "task_ids": sorted(set(tag_examples.get(tag, []))),
            "candidate_bullets": bullets,
            "bullets_added": added,
            "status": status,
        }

    log_line = append_log(vault, fails_paths, len(fails), added_per_tag, today, dry_run)
    report["log_line"] = log_line.strip()
    report["total_bullets_added"] = sum(added_per_tag.values())
    return report


def print_human(report: dict) -> None:
    mode = "DRY-RUN (no files written)" if report["dry_run"] else "WROTE"
    print(f"\n=== evolve_brain [{mode}] {report['date']} ===")
    print(f"failures processed: {report['fails_processed']}  "
          f"(unclassified errors skipped: {report['unclassified']})")
    if not report["techniques"]:
        print("no classifiable failures bucketed to any technique — nothing to learn.")
    for tag, info in report["techniques"].items():
        print(f"\n[{tag}]  {info['failures']} failure-signals  "
              f"status={info['status']}  tasks={info['task_ids']}")
        print(f"  signatures: {info['signatures']}")
        added = info["bullets_added"]
        if added:
            print(f"  + added {len(added)} bullet(s):")
            for b in added:
                print(f"    - {b}")
        else:
            cand = info["candidate_bullets"]
            if cand and info["status"] == "nothing-new":
                print("  (all candidate bullets already present — idempotent, skipped)")
            elif info["status"] in ("no-note", "no-pitfall-section"):
                print(f"  (skipped: {info['status']})")
    print(f"\ntotal bullets added: {report['total_bullets_added']}")
    print(f"ledger line: {report['log_line']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Evolve the CP brain from failure records (deterministic, no LLM).")
    ap.add_argument("--fails", nargs="+", required=True, help="one or more *.fails.json files")
    ap.add_argument("--vault", default=ct._default_vault(), help="vault root (contains knowledge/cc_algos/)")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    ap.add_argument("--max-bullets", type=int, default=3, help="max new bullets per technique (default 3)")
    ap.add_argument("--json", action="store_true", help="also print the machine-readable JSON report")
    ap.add_argument("--date", default=datetime.date.today().isoformat(),
                    help="override the auto-learned date (YYYY-MM-DD); default = today")
    args = ap.parse_args()

    report = evolve(args.fails, args.vault, args.date, args.dry_run, args.max_bullets)
    print_human(report)
    if args.json:
        print("\n--- JSON ---")
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
