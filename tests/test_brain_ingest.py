import importlib
import logging
import sys

import src.brain_ingest as brain_ingest
from src.brain_ingest import format_note, KNOWN_CATEGORIES, slugify


def test_known_categories_includes_imported_and_security():
    assert "imported" in KNOWN_CATEGORIES
    assert "security" in KNOWN_CATEGORIES


def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  SQL   Injection  ") == "sql-injection"
    assert slugify("../../etc") == "etc"            # separators stripped


def test_title_from_heading_and_frontmatter_injected():
    cat, slug, md = format_note("# My Note\n\nBody here.\n", "whatever.md")
    assert slug == "my-note"
    assert md.startswith("---")
    assert "title: My Note" in md
    assert "category: " + cat in md
    assert "Body here." in md            # body preserved


def test_explicit_category_wins_over_frontmatter():
    raw = "---\ncategory: python\n---\n# X\nbody"
    cat, slug, md = format_note(raw, "x.md", category="security")
    assert cat == "security"


def test_frontmatter_category_used_when_no_explicit():
    raw = "---\ncategory: oracle\n---\n# X\nbody"
    cat, _, _ = format_note(raw, "x.md")
    assert cat == "oracle"


def test_heuristic_categorizes_obvious_security_note():
    cat, _, _ = format_note("# Auth Bug\nThis is a security vulnerability / CVE in auth.\n", "n.md")
    assert cat == "security"


def test_unknown_falls_back_to_imported():
    cat, _, _ = format_note("# Random\nNothing identifiable here zzz.\n", "n.md")
    assert cat == "imported"


def test_heuristic_word_boundary_avoids_false_positives():
    # "author" must NOT trigger 'security' via the "auth" substring
    cat, _, _ = format_note("# Bio\nThe author writes about gardening.\n", "n.md")
    assert cat != "security"


def test_body_gets_h1_when_missing_so_graph_shows_title():
    # no heading in the body -> filename-derived title; an H1 is injected so the
    # graph (which reads the first '# heading') shows the real title, not the slug.
    cat, slug, md = format_note("just some scraped prose, no heading.\n", "Cool Topic.md")
    body = md.split("---", 2)[-1]          # text after the frontmatter block
    assert "# Cool Topic" in body
    # a note that already has an H1 is not doubled
    _, _, md2 = format_note("# Real Title\nbody\n", "x.md")
    assert md2.count("# Real Title") == 1


def test_invalid_explicit_category_is_ignored():
    cat, _, _ = format_note("# X\nbody", "x.md", category="../evil")
    assert cat in KNOWN_CATEGORIES           # never the bad value


def test_import_failure_logs_error_and_falls_back_to_vendored_categories(monkeypatch, caplog):
    """GRAY-BRN-12: an import failure of routes.brain_graph_routes._KNOWLEDGE must
    log loudly (ERROR + exc_info) and degrade to the vendored multi-category
    _FALLBACK_CATEGORIES set -- never collapse to frozenset({"imported"})."""
    # `sys.modules[name] = None` is the documented way to force the next
    # `import name` statement to raise ImportError, without touching the
    # real routes.brain_graph_routes module object for other tests.
    monkeypatch.setitem(sys.modules, "routes.brain_graph_routes", None)
    try:
        with caplog.at_level(logging.ERROR, logger="src.brain_ingest"):
            importlib.reload(brain_ingest)

        # Fail-loud: an ERROR record was emitted mentioning the fallback.
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "expected an ERROR log record on import failure"
        assert any("falling back" in r.getMessage().lower() for r in error_records)

        # Fallback, not collapse: KNOWN_CATEGORIES equals the vendored set,
        # a many-entry set, never the single-bucket {"imported"}.
        assert brain_ingest.KNOWN_CATEGORIES == brain_ingest._FALLBACK_CATEGORIES
        assert len(brain_ingest.KNOWN_CATEGORIES) > 40
        assert brain_ingest.KNOWN_CATEGORIES != frozenset({"imported"})

        # Membership still holds for real tags and rejects invalid ones.
        assert "security" in brain_ingest.KNOWN_CATEGORIES
        assert "frontend" in brain_ingest.KNOWN_CATEGORIES
        assert "not-a-category" not in brain_ingest.KNOWN_CATEGORIES
    finally:
        # Restore real import behavior and reload so later tests (and any
        # other module holding a reference) see the normal happy-path state.
        monkeypatch.undo()
        importlib.reload(brain_ingest)
