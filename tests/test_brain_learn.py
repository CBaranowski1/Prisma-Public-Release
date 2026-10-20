"""Tests for src/brain_learn.py — the conversation->brain learning loop.

Covers the pure pieces (lesson parsing, policy gates) and add_learning's
filter + file-write behavior, with the embedding append stubbed so the test
needs no model backend.
"""
import importlib
import logging

import src.brain_learn as bl


# ---- _parse_lesson -------------------------------------------------------

def test_parse_no_lesson_sentinel():
    assert bl._parse_lesson("NO LESSON") is None
    assert bl._parse_lesson("  no lesson here  ") is None
    assert bl._parse_lesson("") is None


def test_parse_explicit_title():
    t, b = bl._parse_lesson("TITLE: Use atomic writes\nAlways write JSON atomically to avoid corruption.")
    assert t == "Use atomic writes"
    assert "atomically" in b


def test_parse_first_line_fallback():
    t, b = bl._parse_lesson("Prefer venv python\nThe py launcher is not on PATH here.")
    assert t == "Prefer venv python"
    assert "launcher" in b


# ---- policy gates --------------------------------------------------------

def test_is_enabled_default_false(monkeypatch):
    # is_enabled() imports get_setting from src.settings at call time; stub it
    # to return the supplied default so the flipped default (False,
    # private-by-default) is exercised.
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: d)
    assert bl.is_enabled() is False


def test_is_enabled_true_when_explicitly_set(monkeypatch):
    # The default remains reversible via the single brain_learning_enabled flag.
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: True if k == "brain_learning_enabled" else d)
    assert bl.is_enabled() is True


def test_user_opted_out(monkeypatch):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: ["alice"] if k == "brain_learning_optout" else d)
    assert bl.user_opted_out("alice") is True
    assert bl.user_opted_out("bob") is False
    assert bl.user_opted_out(None) is False


# ---- add_learning --------------------------------------------------------

def _brain(tmp_path):
    (tmp_path / "knowledge" / "code").mkdir(parents=True)
    return {"id": "t", "path": str(tmp_path), "dir": str(tmp_path)}


def test_add_learning_writes_note_and_indexes(tmp_path, monkeypatch):
    appended = {}

    def fake_append(index_dir, note, embedder=None):
        appended["note"] = note

    import src.brain_index as bi
    monkeypatch.setattr(bi, "append_learned", fake_append)

    brain = _brain(tmp_path)
    ok = bl.add_learning(brain, "Atomic writes",
                         "Always write settings JSON atomically to avoid corruption on crash.")
    assert ok is True
    lessons = list((tmp_path / "knowledge" / "lessons").glob("*.md"))
    assert len(lessons) == 1
    assert "Atomic writes" in lessons[0].read_text("utf-8")
    assert appended["note"]["title"] == "Atomic writes"


def test_add_learning_rejects_too_short(tmp_path, monkeypatch):
    import src.brain_index as bi
    monkeypatch.setattr(bi, "append_learned", lambda *a, **k: None)
    brain = _brain(tmp_path)
    assert bl.add_learning(brain, "Tiny", "too short") is False
    assert not (tmp_path / "knowledge" / "lessons").exists()


def test_add_learning_rejects_duplicate_title(tmp_path, monkeypatch):
    import src.brain_index as bi
    monkeypatch.setattr(bi, "append_learned", lambda *a, **k: None)
    brain = _brain(tmp_path)
    body = "Always write settings JSON atomically to avoid corruption on crash."
    assert bl.add_learning(brain, "Atomic writes", body) is True
    # Same title again -> rejected as duplicate.
    assert bl.add_learning(brain, "atomic   writes", body) is False
    lessons = list((tmp_path / "knowledge" / "lessons").glob("*.md"))
    assert len(lessons) == 1


def test_add_learning_withholds_secret_bearing_lesson(tmp_path, monkeypatch, caplog):
    appended = {}

    def fake_append(index_dir, note, embedder=None):
        appended["called"] = True

    import src.brain_index as bi
    monkeypatch.setattr(bi, "append_learned", fake_append)

    brain = _brain(tmp_path)
    # Synthetic, obviously-fake OpenAI-shaped key -- passes the acceptable()
    # quality gate (>=8 words, non-duplicate title) but must be withheld by
    # the fail-closed PII/secret gate before any write.
    body = (
        "Our deployment script uses the key "
        "sk-FAKE1234567890ABCDEFGH1234 for testing purposes only."
    )
    with caplog.at_level(logging.WARNING):
        ok = bl.add_learning(brain, "Deployment key note", body)
    assert ok is False
    assert not (tmp_path / "knowledge" / "lessons").exists()
    assert "called" not in appended
    assert any("withheld" in r.message.lower() for r in caplog.records)
