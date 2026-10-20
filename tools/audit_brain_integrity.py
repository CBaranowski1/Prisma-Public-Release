#!/usr/bin/env python3
"""Audit a modular Prisma brain's node and wikilink integrity.

Examples:
    python tools/audit_brain_integrity.py llm-council
    python tools/audit_brain_integrity.py llm-council --json report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes.brain_graph_routes import _build_graph, _extract_wikilinks, _knowledge_folders
from src.brains import resolve_brain


def _links(path: Path) -> list[str]:
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return []
    return _extract_wikilinks(text)


def audit(brain_id: str, workers: int = 12) -> dict:
    brain = resolve_brain(brain_id)
    if not brain:
        raise SystemExit(f"Unknown or disabled brain: {brain_id}")

    root = Path(brain["path"])
    knowledge = root / "knowledge"
    if not knowledge.is_dir():
        raise SystemExit(f"Knowledge directory is missing: {knowledge}")

    files = sorted(path for path in knowledge.rglob("*.md") if path.is_file())
    note_files: list[tuple[Path, str, str]] = []
    for folder, node_type in _knowledge_folders(root):
        if not folder.is_dir():
            continue
        note_files.extend(
            (path, node_type, folder.name)
            for path in sorted(folder.glob("*.md"))
            if not path.stem.startswith("_")
        )
    slug_counts = Counter(path.stem.casefold() for path, _, _ in note_files)
    note_ids = {
        (category.casefold(), path.stem.casefold()): (
            path.stem
            if slug_counts[path.stem.casefold()] == 1
            else f"{node_type}:{path.stem}"
        )
        for path, node_type, category in note_files
    }
    canonical_ids: dict[str, str] = {}
    for path, _, category in note_files:
        slug = path.stem.casefold()
        canonical_ids.setdefault(slug, note_ids[(category.casefold(), slug)])
    known = set(note_ids.values())
    categories = Counter(category for _, _, category in note_files)
    inbound: Counter[str] = Counter()
    outbound: Counter[str] = Counter()
    dangling: Counter[str] = Counter()
    ignored_non_wiki_patterns: Counter[str] = Counter()
    total_links = 0
    resolved_links = 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        targets_by_file = pool.map(
            _links,
            (path for path, _, _ in note_files),
        )
        for (path, _, category), targets in zip(note_files, targets_by_file):
            slug = path.stem.casefold()
            source = note_ids[(category.casefold(), slug)]
            for target in targets:
                total_links += 1
                resolved = (
                    note_ids.get((category.casefold(), target))
                    or canonical_ids.get(target)
                )
                if resolved:
                    resolved_links += 1
                    outbound[source] += 1
                    inbound[resolved] += 1
                elif target.isdigit() or target == "difficulty-unknown":
                    ignored_non_wiki_patterns[target] += 1
                    total_links -= 1
                else:
                    dangling[target] += 1

    orphans = sorted(
        slug for slug in known if not inbound[slug] and not outbound[slug]
    )
    graph = _build_graph(root)
    graph_ids = [node["id"] for node in graph["nodes"]]
    graph_id_counts = Counter(graph_ids)
    duplicates = [
        {"id": node_id, "entries": count}
        for node_id, count in graph_id_counts.items()
        if count > 1
    ]
    graph_known = set(graph_ids)
    broken_graph_links = sum(
        1
        for link in graph["links"]
        if link["source"] not in graph_known or link["target"] not in graph_known
    )
    return {
        "brain": brain_id,
        "path": str(root),
        "markdown_files": len(files),
        "graph_eligible_nodes": len(note_files),
        "unique_node_ids": len(known),
        "excluded_nested_or_index_nodes": len(files) - len(note_files),
        "categories": dict(sorted(categories.items())),
        "wikilinks": {
            "total": total_links,
            "resolved": resolved_links,
            "dangling": total_links - resolved_links,
            "distinct_dangling_targets": len(dangling),
            "top_dangling_targets": [
                {"target": target, "references": count}
                for target, count in dangling.most_common(25)
            ],
            "ignored_non_wiki_patterns": sum(
                ignored_non_wiki_patterns.values()
            ),
        },
        "graph": {
            "nodes": len(graph_ids),
            "links": len(graph["links"]),
            "events": len(graph.get("events", [])),
            "broken_links": broken_graph_links,
        },
        "duplicate_node_ids": duplicates[:100],
        "duplicate_node_id_count": len(duplicates),
        "orphan_node_count": len(orphans),
        "orphan_node_sample": orphans[:100],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brain_id")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--json", type=Path, dest="json_path")
    args = parser.parse_args()

    report = audit(args.brain_id, workers=args.workers)
    output = json.dumps(report, indent=2, ensure_ascii=False)
    print(output)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
