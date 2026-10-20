#!/usr/bin/env python3
"""Build a brain's semantic vector index so recall is fast at scale.

    python tools/index_brain.py <brain-id>
    python tools/index_brain.py --all

Embeds every note (meaning -> vector) using the configured embedder
(src.embeddings: HTTP embedder -> local FastEmbed fallback) and writes
brains/<id>/index/{vectors.npy, meta.json}. The output is local (gitignored) —
re-run after the vault changes. Recall (chat + council) loads it automatically
and does a millisecond cosine top-k instead of a lexical scan of every note.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brains import discover_brains, resolve_brain   # noqa: E402
from src.brain_index import build_index                  # noqa: E402


def _index(b: dict) -> None:
    vault = Path(b["path"])
    out = Path(b.get("dir") or b["path"]) / "index"
    t0 = time.time()
    print(f"indexing '{b['id']}' from {vault} ...")

    def progress(done, total):
        if done % 2048 == 0 or done == total:
            print(f"  embedded {done}/{total}", end="\r", flush=True)

    n, dim = build_index(vault, out, progress=progress)
    print(f"\n  -> {out}  ({n} notes, dim {dim}, {time.time()-t0:.1f}s)")


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
            try:
                _index(b)
            except Exception as e:
                print(f"  ! skipped '{b['id']}': {e}")
        return 0
    b = resolve_brain(args[0])
    if not b:
        print(f"unknown brain '{args[0]}'. available:",
              ", ".join(x["id"] for x in discover_brains()) or "(none)")
        return 1
    _index(b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
