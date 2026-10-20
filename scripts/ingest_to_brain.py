"""Capture activity into the Second Brain — conversations, docs, notes.

Distills (or stores raw) a piece of text as a brain note, deduped by content
hash so re-ingesting the same thing is a no-op. Use this to feed conversations
and other app activity into the brain WITHOUT raw-dumping noise: prefer --distill
so only the durable lesson/summary is stored, not the full transcript.

Usage:
  python scripts/ingest_to_brain.py --file chat.txt --kind conversation --distill
  echo "some text" | python scripts/ingest_to_brain.py --kind note
"""
from __future__ import annotations
import argparse
import hashlib
import os
import re
import sys
import datetime


def distill(text: str, args) -> str:
    if os.getenv("INGEST_MOCK") == "1":
        return "DISTILLED: " + text.strip()[:200]
    import httpx
    sys_msg = ("Distill the following into a durable, reusable note: the key facts, "
               "decisions, and lessons worth remembering. Be concise; drop chit-chat, "
               "secrets, and PII. Output markdown.")
    r = httpx.post(args.ollama_url.rstrip("/") + "/chat/completions",
                   json={"model": args.model, "temperature": 0.2,
                         "messages": [{"role": "system", "content": sys_msg},
                                      {"role": "user", "content": text[:12000]}]},
                   timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--kind", default="note")  # conversation | note | doc | lesson
    ap.add_argument("--title", default="")
    ap.add_argument("--distill", action="store_true")
    ap.add_argument("--model", default="vera-reasoner")
    ap.add_argument("--ollama-url", default=os.getenv("OLLAMA_OPENAI_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--vault", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                   "brains", "llm-council", "vault"))
    args = ap.parse_args()

    text = args.text or (open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read())
    if not text.strip():
        print("[!] no input text"); return 2
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    cat = os.path.join(args.vault, "knowledge", args.kind + "s")
    os.makedirs(cat, exist_ok=True)
    # dedup: skip if a note already carries this content hash
    for f in os.listdir(cat):
        if f.endswith(".md"):
            try:
                if f"hash: {h}" in open(os.path.join(cat, f), encoding="utf-8").read(2000):
                    print(f"[skip] already ingested (hash {h})"); return 0
            except OSError:
                pass
    body_text = distill(text, args) if args.distill else text.strip()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    title = args.title or f"{args.kind} {ts}"
    slug = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")[:80] or args.kind
    note = (f"---\ntype: {args.kind}\ntags: {args.kind},captured\nhash: {h}\n---\n\n"
            f"# {title}\n\n{body_text}\n")
    path = os.path.join(cat, f"{slug}-{h}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(note)
    print(f"[ok] stored {args.kind} note: {os.path.relpath(path, args.vault)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
