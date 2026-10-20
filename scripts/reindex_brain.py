"""One-time rebuild of the brain's SEMANTIC index over ALL notes.

Why (not the old per-file reindex): lexical recall reads new files live and needs
no index. But the VECTOR tier only knows notes that were embedded - and the index
had 76k vectors while the vault has ~115k. This rebuilds it so semantic recall
covers the new toolkit + verified solutions, and pins the SAME embedder for index
and queries (no 404, no dimension mismatch).

Always pins local FastEmbed (MiniLM 384-d) - this repo's brain index only ever
builds and queries with the fastembed backend (a deliberate, documented
invariant in src/embeddings.py / src/brain_index.py). `--nomic` is not
supported: passing it is rejected honestly rather than silently building a
fastembed index while claiming a nomic/768-d rebuild happened.

  python scripts/reindex_brain.py
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(BASE, ".env")


def _vault():
    bj = os.path.join(BASE, "brains", "llm-council", "brain.json")
    vp = (json.load(open(bj, encoding="utf-8")).get("vault_path") or "").strip()
    return os.path.normpath(os.path.join(os.path.dirname(bj), vp)) if vp else \
        os.path.join(BASE, "brains", "llm-council", "vault")


def _set_env(kv: dict):
    lines = open(ENV, encoding="utf-8").read().splitlines() if os.path.exists(ENV) else []
    kept = [l for l in lines if l.split("=", 1)[0].strip() not in kv]
    for k, v in kv.items():
        kept.append(f"{k}={v}")
    open(ENV, "w", encoding="utf-8", newline="").write("\n".join(kept) + "\n")


def main() -> int:
    if "--nomic" in sys.argv:
        print("[embedder] --nomic is not supported: this repo's brain index always builds "
              "and queries with local FastEmbed (a deliberate, documented invariant), so a "
              "nomic index would silently mismatch at query time. Re-run without --nomic.")
        return 1

    # DEFAULT (and only path): local FastEmbed (MiniLM 384). CPU-only, always available, no VRAM
    # contention with the 30B, no 404 - so the vector fast-path always works and
    # query dim always matches the index. The robust choice for a single-GPU rig.
    _set_env({"EMBEDDING_BACKEND": "fastembed", "EMBEDDING_URL": "local"})
    os.environ["EMBEDDING_BACKEND"] = "fastembed"; os.environ["EMBEDDING_URL"] = "local"
    print("[embedder] pinned local FastEmbed (MiniLM 384-d) - CPU, always available, no 404, no VRAM fight.")

    from src.brain_index import build_index
    v = _vault(); out = os.path.join(BASE, "brains", "llm-council", "index")
    print(f"[index] vault={v}\n[index] out={out}\nembedding all notes (this is the slow part)...")
    def prog(done, total):
        if done % 2560 == 0 or done == total:
            print(f"  {done}/{total}")
    n, dim = build_index(Path(v), Path(out), progress=prog)
    print(f"\n[done] indexed {n} notes at dim {dim}. Query + index now use the SAME embedder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
