"""Idempotently set the embeddings backend in prism-UI/.env and verify it.

  python scripts/set_embedder.py --url http://localhost:11434/v1/embeddings --model nomic-embed-text --verify
"""
from __future__ import annotations
import argparse
import os
import sys


def set_env(env_path: str, kv: dict):
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    keys = set(kv)
    kept = [l for l in lines if l.split("=", 1)[0].strip() not in keys]
    for k, v in kv.items():
        kept.append(f"{k}={v}")
    with open(env_path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(kept) + "\n")


def verify(url: str, model: str) -> bool:
    try:
        import httpx
        r = httpx.post(url, json={"model": model, "input": "hello"}, timeout=15)
        if r.status_code == 200 and (r.json().get("data") or r.json().get("embedding")):
            print(f"[ok] embeddings endpoint live ({url}, model={model})")
            return True
        print(f"[!] endpoint returned {r.status_code}: {r.text[:160]}")
    except Exception as e:
        print(f"[!] verify failed: {e}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:11434/v1/embeddings")
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--env", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    set_env(args.env, {"EMBEDDING_URL": args.url, "EMBEDDING_MODEL": args.model})
    print(f"[ok] wrote EMBEDDING_URL + EMBEDDING_MODEL to {args.env}")
    if args.verify and not verify(args.url, args.model):
        print("    (it'll fall back to FastEmbed until this resolves)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
