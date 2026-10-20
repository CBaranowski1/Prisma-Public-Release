#!/usr/bin/env python3
"""Bake a brain's galaxy graph to a static file so a big vault loads instantly.

    python tools/bake_brain_graph.py <brain-id>
    python tools/bake_brain_graph.py --all

Reads the brain's knowledge/<cat>/*.md (+ config/ agents + events) via the same
builder the live /api/brain-graph endpoint uses, and writes {nodes,links,events,
stats} to brains/<id>/graph.json. The endpoint serves that baked file when
present (no live globbing of tens of thousands of notes). The output is local
(gitignored) — re-run after the vault changes.
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brains import discover_brains, resolve_brain  # noqa: E402
from routes.brain_graph_routes import _build_graph, _visual_graph  # noqa: E402


def bake(b: dict) -> None:
    know = Path(b["path"])
    out = Path(b.get("dir") or b["path"]) / "graph.json"
    t0 = time.time()
    print(f"baking '{b['id']}' from {know} ...")
    full = _build_graph(know)
    data = _visual_graph(full)
    out.write_text(json.dumps(data), encoding="utf-8")
    n = len(data.get("nodes", []))
    a = sum(1 for x in data.get("nodes", []) if x.get("type") == "agent")
    total = (data.get("stats") or {}).get("nodes", n)
    mb = out.stat().st_size / 1e6
    print(
        f"  -> {out}  ({n} visible / {total} indexed nodes, "
        f"{a} agents, {mb:.1f} MB, {time.time()-t0:.1f}s)"
    )


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("available brains:", ", ".join(b["id"] for b in discover_brains()) or "(none)")
        return 1
    if args[0] in ("--all", "-a"):
        brains = discover_brains()
        if not brains:
            print("no brains found"); return 1
        for b in brains:
            bake(b)
        return 0
    b = resolve_brain(args[0])
    if not b:
        print(f"unknown brain '{args[0]}'. available:",
              ", ".join(x["id"] for x in discover_brains()) or "(none)")
        return 1
    bake(b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
