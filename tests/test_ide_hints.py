import pytest


def test_detects_absolute_unix_path():
    from src.ide_hints import extract_ide_hint
    result = extract_ide_hint("See /home/user/project/main.py for details")
    assert result is not None
    assert result["path"] == "/home/user/project/main.py"


def test_detects_path_with_line_number():
    from src.ide_hints import extract_ide_hint
    result = extract_ide_hint("Error in /project/app.py:42 — syntax error")
    assert result["path"] == "/project/app.py"
    assert result["line"] == 42


def test_detects_windows_path():
    from src.ide_hints import extract_ide_hint
    result = extract_ide_hint(r"Open C:\Users\user\project\main.py")
    assert result is not None
    assert "main.py" in result["path"]


def test_returns_none_for_plain_text():
    from src.ide_hints import extract_ide_hint
    assert extract_ide_hint("No file paths here, just text.") is None


def test_returns_none_for_url():
    from src.ide_hints import extract_ide_hint
    assert extract_ide_hint("See https://example.com/page") is None


def test_prefers_first_match():
    from src.ide_hints import extract_ide_hint
    result = extract_ide_hint("In /a/first.py and /b/second.py")
    assert result["path"] == "/a/first.py"
