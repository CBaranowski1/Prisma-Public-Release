"""services/research _extract_sources must gate low-quality findings.

The handler filters findings whose summary is junk boilerplate (via
research_utils.is_low_quality) before listing them as cited sources, so
"the page does not contain relevant information" URLs never show up as
sources, and a junk finding seen first cannot suppress the good title for
the same URL. services/research/research_handler.py is now a compatibility
shim over the canonical src/research_handler.py, so these tests exercise
the single live implementation.
"""

import importlib.util
import sys
import types

import pytest


@pytest.fixture
def handler_cls(monkeypatch):
    """Load services.research.research_handler from its file path so the
    heavy services/__init__.py (httpx etc.) is never imported."""
    pkg = types.ModuleType("services")
    pkg.__path__ = []
    sub = types.ModuleType("services.research")
    sub.__path__ = []
    monkeypatch.setitem(sys.modules, "services", pkg)
    monkeypatch.setitem(sys.modules, "services.research", sub)
    name = "services.research.research_handler"
    monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        name, "services/research/research_handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod.ResearchHandler


JUNK = "The page does not contain relevant information"


def test_low_quality_summary_is_not_a_source(handler_cls):
    out = handler_cls._extract_sources([{"url": "http://a", "title": "T", "summary": JUNK}])
    assert out == []


def test_good_summary_is_kept(handler_cls):
    out = handler_cls._extract_sources(
        [{"url": "http://a", "title": "T", "summary": "Detailed statistics about the topic"}]
    )
    assert out == [{"url": "http://a", "title": "T"}]


def test_junk_first_no_longer_suppresses_the_good_finding(handler_cls):
    out = handler_cls._extract_sources(
        [
            {"url": "http://a", "title": "Bad", "summary": JUNK},
            {"url": "http://a", "title": "Good", "summary": "Real data about the topic"},
        ]
    )
    assert out == [{"url": "http://a", "title": "Good"}]


def test_evidence_is_checked_when_summary_missing(handler_cls):
    out = handler_cls._extract_sources(
        [{"url": "http://a", "title": "T", "evidence": "Concrete evidence text"}]
    )
    assert out == [{"url": "http://a", "title": "T"}]


def test_report_is_markdown_only_sources_handled_elsewhere(handler_cls):
    """The live handler no longer embeds a Sources section in the report —
    sources are extracted via _extract_sources (gated above) and rendered by
    the frontend. The old ``findings=`` kwarg is gone from the formatter."""
    h = object.__new__(handler_cls)
    report = h._format_research_report("q", "full report", {}, 1.0)
    assert "full report" in report
    assert "### Sources" not in report
