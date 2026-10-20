"""Tests for src/brains.py modular-brain discovery."""
import json
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("brains_mod", pathlib.Path("src/brains.py"))
brains = importlib.util.module_from_spec(spec); spec.loader.exec_module(brains)


def _mk(root, name, manifest=None, with_knowledge=True):
    d = root / name
    (d / "knowledge" / "code").mkdir(parents=True, exist_ok=True) if with_knowledge else d.mkdir(parents=True, exist_ok=True)
    if with_knowledge:
        (d / "knowledge" / "code" / "x.md").write_text("# X\n", encoding="utf-8")
    if manifest is not None:
        (d / "brain.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def test_discover_reads_manifest_and_defaults(tmp_path):
    _mk(tmp_path, "precision-audit", {"name": "Precision Audit Brain", "color": "#8c7cf8"})
    _mk(tmp_path, "finance_notes")  # no manifest -> defaults
    found = {b["id"]: b for b in brains.discover_brains(tmp_path)}
    assert "precision-audit" in found and found["precision-audit"]["name"] == "Precision Audit Brain"
    assert found["precision-audit"]["color"] == "#8c7cf8"
    # default id is the slugified folder name (underscores preserved); default
    # name is title-cased from the folder name
    assert "finance_notes" in found
    assert found["finance_notes"]["name"] == "Finance Notes"


def test_disabled_brain_is_skipped(tmp_path):
    _mk(tmp_path, "on", {"name": "On"})
    _mk(tmp_path, "off", {"name": "Off", "enabled": False})
    ids = {b["id"] for b in brains.discover_brains(tmp_path)}
    assert "on" in ids and "off" not in ids


def test_resolve_brain(tmp_path):
    _mk(tmp_path, "alpha", {"name": "Alpha"})
    assert brains.resolve_brain("alpha", tmp_path)["name"] == "Alpha"
    assert brains.resolve_brain("nope", tmp_path) is None


def test_missing_root_is_safe(tmp_path):
    # A non-existent root yields no folder brains (no crash). Ignore the legacy
    # bridge, which may surface a PRISMA_BRAIN_VAULT set by a sibling test module.
    folder_brains = [b for b in brains.discover_brains(tmp_path / "does-not-exist")
                     if b["id"] != "legacy"]
    assert folder_brains == []
