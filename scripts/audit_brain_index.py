#!/usr/bin/env python3
"""Audit a modular brain's semantic coverage and optionally repair missing notes.

Usage:
    python scripts/audit_brain_index.py llm-council
    python scripts/audit_brain_index.py llm-council --repair

Repair appends only missing notes to the cheap learned partition. It never
rewrites the large immutable main index or removes any vector.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.brain_index import _docs, _excerpt, append_learned  # noqa: E402
from src.brains import resolve_brain  # noqa: E402


def _index_keys(index_dir: Path) -> tuple[set[tuple[str, str]], list[dict]]:
    entries: list[dict] = []
    for name in ("meta.json", "learned.json"):
        path = index_dir / name
        if not path.is_file():
            continue
        document = json.loads(path.read_text("utf-8"))
        rows = document.get("meta", []) if isinstance(document, dict) else document
        entries.extend(row for row in rows if isinstance(row, dict))
    return {
        (str(row.get("category", "")), str(row.get("id", "")))
        for row in entries
    }, entries


def audit_brain(brain: dict) -> dict:
    vault = Path(brain["path"])
    index_dir = Path(brain.get("dir") or brain["path"]) / "index"
    notes = [
        {
            "id": slug,
            "category": category,
            "title": title,
            "body": body,
        }
        for slug, category, title, body in _docs(vault)
    ]
    expected = {(note["category"], note["id"]) for note in notes}
    indexed, entries = _index_keys(index_dir)
    seen: set[tuple[str, str]] = set()
    duplicate_entries = 0
    for row in entries:
        key = (str(row.get("category", "")), str(row.get("id", "")))
        if key in seen:
            duplicate_entries += 1
        seen.add(key)
    missing_keys = expected - indexed
    extra_count = len(indexed - expected)
    return {
        "brain": brain["id"],
        "fresh": not missing_keys and not extra_count and not duplicate_entries,
        "vault_notes": len(expected),
        "indexed_notes": len(indexed & expected),
        "missing_count": len(missing_keys),
        "extra_count": extra_count,
        "duplicate_index_entries": duplicate_entries,
        "missing": [
            note
            for note in notes
            if (note["category"], note["id"]) in missing_keys
        ],
        "index_dir": str(index_dir),
    }


def repair_missing(report: dict) -> int:
    index_dir = Path(report["index_dir"])
    repaired = 0
    for note in report["missing"]:
        append_learned(
            index_dir,
            {
                **note,
                "excerpt": _excerpt(note["body"]),
            },
        )
        repaired += 1
    return repaired


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("brain_id")
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    brain = resolve_brain(args.brain_id)
    if brain is None:
        parser.error(f"unknown brain: {args.brain_id}")

    report = audit_brain(brain)
    if args.repair and report["missing_count"]:
        repaired = repair_missing(report)
        report = audit_brain(brain)
        report["repaired"] = repaired

    printable = {key: value for key, value in report.items() if key != "missing"}
    print(json.dumps(printable, indent=2))
    return 1 if report["missing_count"] or report["duplicate_index_entries"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
