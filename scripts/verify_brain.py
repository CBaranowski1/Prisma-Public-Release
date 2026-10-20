"""Verify what actually made it into the brain: counts by category + a LIVE recall
probe (proves the brain retrieves the nodes, not just that files exist on disk).

  python scripts/verify_brain.py
"""
from __future__ import annotations
import json
import os
import sys
import glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _vault():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bj = os.path.join(base, "brains", "llm-council", "brain.json")
    try:
        vp = (json.load(open(bj, encoding="utf-8")).get("vault_path") or "").strip()
        if vp:
            return os.path.normpath(os.path.join(os.path.dirname(bj), vp))
    except Exception:
        pass
    return os.path.join(base, "brains", "llm-council", "vault")


def _count(d):
    return len(glob.glob(os.path.join(d, "**", "*.md"), recursive=True))


def main():
    v = _vault()
    print("vault:", v)
    if not os.path.isdir(v):
        print("[!] vault folder not found - check brain.json vault_path."); return 2
    total = _count(v)
    algos = _count(os.path.join(v, "knowledge", "algorithms"))
    sols = _count(os.path.join(v, "knowledge", "solutions"))
    runs = _count(os.path.join(v, "knowledge", "training_runs"))
    gaps = _count(os.path.join(v, "knowledge", "gaps"))
    remed = _count(os.path.join(v, "knowledge", "remediation"))
    print(f"  total .md notes       : {total}")
    print(f"  algorithm references  : {algos}   (expect ~79 incl. index)")
    print(f"  remediation concepts  : {remed}")
    print(f"  verified solutions    : {sols}")
    print(f"  gap nodes (open fails): {gaps}")
    print(f"  training-run summaries: {runs}")

    print("\nLIVE RECALL PROBE (does the brain actually return these?):")
    try:
        from src.brains import build_brain_providers
        import inspect
        import asyncio
        import concurrent.futures
        def _resolve(x):
            if inspect.iscoroutine(x):
                try: asyncio.get_running_loop()
                except RuntimeError: return asyncio.run(x)
                with concurrent.futures.ThreadPoolExecutor(1) as ex:
                    return ex.submit(lambda: asyncio.new_event_loop().run_until_complete(x)).result()
            return x
        provs = build_brain_providers()
        print(f"  providers active: {len(provs)}")
        def _ident(h):
            m = getattr(h, "memory", None)
            # .source is the PROVIDER id (brain:llm-council) - skip it. The note
            # identifier is in .id (slug/path); fall back to the text/title.
            for attr in ("id", "path", "title", "name"):
                v = getattr(m, attr, "") or ""
                if v and not str(v).startswith("brain:"):
                    return os.path.basename(str(v))
            txt = (getattr(m, "text", "") or "")
            for line in txt.splitlines():
                if line.startswith("# "):
                    return "title:" + line[2:].strip()[:40]
            flat = " ".join(txt.split())
            return ("text:" + flat[:50]) if flat else "?"
        hit_algo = 0
        for q in ["dijkstra shortest path", "modular inverse prime", "knapsack dynamic programming", "two pointers sliding window"]:
            ids=[]
            for p in provs:
                for h in (_resolve(p.recall(q, owner=None, top_k=3)) or []):
                    ids.append(_ident(h))
            algo = any(("title:" in i) or any(k in i.lower() for k in ("dijkstra","modular","knapsack","pointer","algorithm")) for i in ids[:3])
            hit_algo += algo
            print(f"  '{q}' -> {ids[:3] or 'NO HITS'}")
        print(f"\n  algorithm notes surfaced on {hit_algo}/4 probes "
              + ("(recall is finding the toolkit - good)" if hit_algo>=2 else "(toolkit NOT surfacing - retrieval needs a look)"))
    except Exception as e:
        print("  [!] recall probe failed:", str(e)[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
