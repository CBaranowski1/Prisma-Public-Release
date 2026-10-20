#!/usr/bin/env python3
"""Recover missing brain notes from preserved semantic-index metadata.

The vector index stores a title and excerpt for every indexed note. This tool
compares those `(category, id)` pairs to the current vault and can materialize
clearly labeled excerpt-only recovery notes when no Markdown source survives.

Dry run:
    python tools/recover_brain_index_notes.py llm-council

Apply:
    python tools/recover_brain_index_notes.py llm-council --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brains import resolve_brain

_SAFE_CATEGORY = re.compile(r"[A-Za-z0-9._-]{1,100}")
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")


def recover(brain_id: str, *, apply: bool = False) -> dict:
    brain = resolve_brain(brain_id)
    if not brain:
        raise SystemExit(f"Unknown or disabled brain: {brain_id}")

    brain_dir = Path(brain["dir"])
    knowledge = (Path(brain["path"]) / "knowledge").resolve()
    meta_path = brain_dir / "index" / "meta.json"
    if not meta_path.is_file():
        raise SystemExit(f"Semantic index metadata is missing: {meta_path}")
    if not knowledge.is_dir():
        raise SystemExit(f"Knowledge directory is missing: {knowledge}")

    document = json.loads(meta_path.read_text("utf-8"))
    rows = document.get("meta", []) if isinstance(document, dict) else []
    missing: list[tuple[Path, dict]] = []
    unsafe = 0
    seen: set[tuple[str, str]] = set()

    for row in rows:
        category = str(row.get("category", "")).strip()
        note_id = str(row.get("id", "")).strip()
        key = (category.casefold(), note_id.casefold())
        if key in seen:
            continue
        seen.add(key)
        if not _SAFE_CATEGORY.fullmatch(category) or not _SAFE_ID.fullmatch(note_id):
            unsafe += 1
            continue
        path = (knowledge / category / f"{note_id}.md").resolve()
        if not path.is_relative_to(knowledge):
            unsafe += 1
            continue
        if not path.is_file():
            missing.append((path, row))

    recovered = 0
    failures: list[str] = []
    if apply:
        for path, row in missing:
            title = str(row.get("title") or row.get("id") or "Recovered note").strip()
            excerpt = str(row.get("excerpt") or "").strip()
            body = (
                "<!-- recovered-from: semantic-index-metadata; "
                "full-original-unavailable -->\n\n"
                "> Recovery note: the full Markdown source was absent from every "
                "located vault copy. The content below is the preserved semantic "
                "index excerpt.\n\n"
                f"{excerpt or '# ' + title}\n"
            )
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
                recovered += 1
            except OSError as exc:
                failures.append(f"{path}: {exc}")

    categories = Counter(path.parent.name for path, _ in missing)
    return {
        "brain": brain_id,
        "index_rows": len(rows),
        "unique_index_pairs": len(seen),
        "missing_before": len(missing),
        "recovered": recovered,
        "dry_run": not apply,
        "unsafe_rows_skipped": unsafe,
        "failures": failures,
        "missing_by_category": dict(sorted(categories.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brain_id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = recover(args.brain_id, apply=args.apply)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
