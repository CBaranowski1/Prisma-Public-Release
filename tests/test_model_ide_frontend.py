"""Static contracts for Prisma Desktop's integrated Model IDE."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IDE_JS = (ROOT / "static" / "js" / "ide.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CAPABILITY = (
    ROOT / "desktop" / "src-tauri" / "capabilities" / "default.json"
).read_text(encoding="utf-8")
WORKSPACE_RS = (
    ROOT / "desktop" / "src-tauri" / "src" / "ide_workspace.rs"
).read_text(encoding="utf-8")


def test_sidebar_opens_integrated_model_ide_not_missing_zed_binary():
    assert "Model IDE" in INDEX
    assert "ide_choose_workspace" in IDE_JS
    assert "focus_ide_window" not in IDE_JS


def test_model_edits_are_reviewed_before_native_save():
    assert "/api/model-ide/complete" in IDE_JS
    assert "buildLineDiff" in IDE_JS
    assert "Apply to editor" in IDE_JS
    assert "expectedRevision" in IDE_JS


def test_secret_bearing_files_are_not_sent_to_model():
    assert "SECRET_PATH" in IDE_JS
    assert r"\.env(?:\.[^/]*)?" in IDE_JS
    assert "Secret-bearing files are never sent to a model" in IDE_JS


def test_database_surface_is_read_only_and_migration_drafts_are_files():
    assert "/api/model-ide/databases" in IDE_JS
    assert "never executes their SQL automatically" in IDE_JS
    assert "Save reviewed migration file" in IDE_JS


def test_native_commands_are_available_to_loopback_spark_tunnel_origin():
    assert "http://localhost:7000" in CAPABILITY
    assert "http://[::1]:7000" not in CAPABILITY
    assert "ide_read_file" in WORKSPACE_RS
    assert "ide_write_file" in WORKSPACE_RS


def test_native_workspace_rejects_parent_traversal_and_symlink_entries():
    assert "Component::ParentDir" in WORKSPACE_RS
    assert "file_type.is_symlink()" in WORKSPACE_RS
    assert "starts_with(root)" in WORKSPACE_RS
