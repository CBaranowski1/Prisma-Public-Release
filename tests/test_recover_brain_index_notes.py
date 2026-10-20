import json
from pathlib import Path

import tools.recover_brain_index_notes as recovery


def test_recovers_missing_index_excerpt_without_overwriting(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    vault = brain_dir / "vault"
    knowledge = vault / "knowledge"
    existing = knowledge / "code" / "existing.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# Current version\n", encoding="utf-8")
    index = brain_dir / "index"
    index.mkdir()
    (index / "meta.json").write_text(
        json.dumps(
            {
                "meta": [
                    {
                        "id": "existing",
                        "category": "code",
                        "title": "Old copy",
                        "excerpt": "# Old copy",
                    },
                    {
                        "id": "recovered",
                        "category": "gaps",
                        "title": "Recovered",
                        "excerpt": "# Recovered\nSee [[existing]].",
                    },
                    {
                        "id": "../escape",
                        "category": "gaps",
                        "title": "Unsafe",
                        "excerpt": "unsafe",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        recovery,
        "resolve_brain",
        lambda _brain_id: {"dir": str(brain_dir), "path": str(vault)},
    )

    report = recovery.recover("test", apply=True)

    assert report["recovered"] == 1
    assert report["unsafe_rows_skipped"] == 1
    assert existing.read_text(encoding="utf-8") == "# Current version\n"
    restored = knowledge / "gaps" / "recovered.md"
    assert "semantic-index-metadata" in restored.read_text(encoding="utf-8")
    assert not (vault / "escape.md").exists()
