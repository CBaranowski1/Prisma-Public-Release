"""GRAY-BRN-14: incremental mtime+size cache + failure-vs-empty extraction signal.

Hermetic — stubs the extractors (`read_text_file` / `convert_to_markdown`), no
live markitdown/pypdf/network calls.
"""
import os
import time

from src import personal_docs
from src.personal_docs import PersonalDocsManager, load_personal_index


def test_unchanged_file_not_reextracted(tmp_path, monkeypatch):
    """A second load with a warm cache must not re-extract an unchanged file."""
    original_read_text_file = personal_docs.read_text_file
    calls = []

    def spy(path):
        calls.append(path)
        return original_read_text_file(path)

    monkeypatch.setattr(personal_docs, "read_text_file", spy)

    note = tmp_path / "note.txt"
    note.write_text("hello world", encoding="utf-8")

    # Cold load: warms the cache, one extract call.
    files = load_personal_index(str(tmp_path), cache={})
    assert len(calls) == 1
    assert files[0]["chunks"] == ["hello world"]

    warm_cache = {os.path.abspath(f["path"]): f for f in files}
    calls.clear()

    # Second load with the warm cache: unchanged file must not be re-extracted.
    files2 = load_personal_index(str(tmp_path), cache=warm_cache)
    assert len(calls) == 0
    assert files2[0]["chunks"] == ["hello world"]
    assert files2[0]["extract_failed"] is False


def test_changed_file_is_reextracted(tmp_path, monkeypatch):
    """A file whose mtime changed since the cache was built must be re-extracted."""
    original_read_text_file = personal_docs.read_text_file
    calls = []

    def spy(path):
        calls.append(path)
        return original_read_text_file(path)

    monkeypatch.setattr(personal_docs, "read_text_file", spy)

    note = tmp_path / "note.txt"
    note.write_text("hello world", encoding="utf-8")

    files = load_personal_index(str(tmp_path), cache={})
    assert len(calls) == 1
    warm_cache = {os.path.abspath(f["path"]): f for f in files}
    calls.clear()

    # Bump mtime without changing size/content.
    new_time = time.time() + 5
    os.utime(note, (new_time, new_time))

    files2 = load_personal_index(str(tmp_path), cache=warm_cache)
    assert len(calls) == 1
    assert files2[0]["chunks"] == ["hello world"]


def test_failed_extraction_counted_distinctly(tmp_path, monkeypatch):
    """A raw None from markitdown is extract_failed=True; a legit '' is not."""
    from src import markitdown_runtime

    def fake_convert(path):
        name = os.path.basename(path)
        if name == "fails.docx":
            return None
        if name == "empty.docx":
            return ""
        return "unexpected"

    monkeypatch.setattr(markitdown_runtime, "convert_to_markdown", fake_convert)

    (tmp_path / "fails.docx").write_bytes(b"not a real docx")
    (tmp_path / "empty.docx").write_bytes(b"not a real docx either")

    files = load_personal_index(str(tmp_path), cache={})
    by_name = {f["name"]: f for f in files}

    assert by_name["fails.docx"]["extract_failed"] is True
    assert by_name["fails.docx"]["chunks"] == []

    assert by_name["empty.docx"]["extract_failed"] is False
    assert by_name["empty.docx"]["chunks"] == []

    # Full manager path: get_stats() sums the failures into unextractable_count.
    mgr = PersonalDocsManager(str(tmp_path))
    stats = mgr.get_stats()
    assert stats["unextractable_count"] == 1


def test_reload_route_shape(tmp_path, monkeypatch):
    """The /api/personal/reload response shape gains unextractable_count additively."""
    from src import markitdown_runtime

    def fake_convert(path):
        return None if os.path.basename(path) == "broken.docx" else "content"

    monkeypatch.setattr(markitdown_runtime, "convert_to_markdown", fake_convert)

    (tmp_path / "broken.docx").write_bytes(b"junk")
    (tmp_path / "ok.docx").write_bytes(b"junk")

    mgr = PersonalDocsManager(str(tmp_path))
    mgr.refresh_index()

    # Mirror routes/personal_routes.py::api_personal_reload's response shape.
    unextractable_count = sum(1 for f in mgr.index if f.get("extract_failed"))
    response = {"ok": True, "count": len(mgr.index), "unextractable_count": unextractable_count}

    assert response["ok"] is True
    assert response["count"] == len(mgr.index)
    assert response["unextractable_count"] == 1
    assert mgr.get_stats()["unextractable_count"] == 1
