import json
from pathlib import Path

import scripts.audit_brain_index as audit


def _brain(tmp_path):
    knowledge = tmp_path / "knowledge" / "code"
    knowledge.mkdir(parents=True)
    (knowledge / "one.md").write_text("# One\nFirst note body", "utf-8")
    (knowledge / "two.md").write_text("# Two\nSecond note body", "utf-8")
    index = tmp_path / "index"
    index.mkdir()
    (index / "meta.json").write_text(
        json.dumps({
            "meta": [{
                "id": "one",
                "category": "code",
                "title": "One",
                "excerpt": "First",
            }]
        }),
        "utf-8",
    )
    return {
        "id": "test",
        "path": str(tmp_path),
        "dir": str(tmp_path),
    }


def test_audit_identifies_and_repairs_only_missing_notes(tmp_path, monkeypatch):
    brain = _brain(tmp_path)
    first = audit.audit_brain(brain)
    assert first["fresh"] is False
    assert first["vault_notes"] == 2
    assert first["missing_count"] == 1
    assert first["missing"][0]["id"] == "two"

    def fake_append(index_dir, note):
        path = Path(index_dir) / "learned.json"
        path.write_text(json.dumps({"meta": [note]}), "utf-8")

    monkeypatch.setattr(audit, "append_learned", fake_append)
    assert audit.repair_missing(first) == 1
    second = audit.audit_brain(brain)
    assert second["fresh"] is True
    assert second["missing_count"] == 0
    assert second["duplicate_index_entries"] == 0
