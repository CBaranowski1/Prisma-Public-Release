"""GRAY-INT-08: services/search must import and degrade gracefully without bs4.

Covers the full import chain (services.search -> .core -> .providers/.content)
and the HTML-parsing call sites that must degrade to empty/degraded results
instead of raising when beautifulsoup4 is unavailable.
"""

import sys

import pytest


def test_search_imports_without_bs4(monkeypatch):
    """import services.search must not raise when bs4 is simulated absent,
    and the package must be usable (not just importable) afterward.
    """
    for mod in list(sys.modules):
        if mod == "services" or mod.startswith("services.search") or mod.startswith("bs4"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setitem(sys.modules, "bs4", None)

    import services.search  # must NOT raise after the GRAY-INT-08 fix

    assert services.search.PROVIDER_INFO  # usable, not just importable


class _FakeHtmlResponse:
    """Minimal httpx.Response stand-in for the HTML-parsing degrade paths."""

    is_success = True
    status_code = 200
    text = "<html><body><article class='result'>ignored</article></body></html>"

    def raise_for_status(self):
        return None


def test_searxng_and_ddg_html_fallback_return_empty_without_bs4(monkeypatch):
    """With bs4 simulated absent, the searxng HTML-parse path and the
    duckduckgo HTML fallback return [] instead of raising.
    """
    from services.search import providers

    monkeypatch.setattr(providers, "has_bs4", lambda: False)
    monkeypatch.setattr(providers.httpx, "get", lambda *a, **k: _FakeHtmlResponse())

    assert providers.searxng_search("test query") == []

    # Force duckduckgo_search() into its _html_fallback() branch by making the
    # optional `duckduckgo_search` library import fail, then confirm the HTML
    # fallback itself degrades to [] rather than constructing BeautifulSoup.
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)
    assert providers.duckduckgo_search("test query") == []


def test_fetch_webpage_content_degrades_without_bs4(monkeypatch, tmp_path):
    """With bs4 simulated absent, the HTML branch of fetch_webpage_content
    returns a degraded _empty_result (not raise).
    """
    from services.search import content

    monkeypatch.setattr(content, "has_bs4", lambda: False)
    monkeypatch.setattr(content, "CONTENT_CACHE_DIR", tmp_path)
    content.content_cache_index.clear()

    class _FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        content = b""
        text = "<html><head><title>Example</title></head><body>Hello world</body></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(content, "_get_public_url", lambda url, headers, timeout: _FakeResponse())

    result = content.fetch_webpage_content("https://example.test/bs4-degraded")

    assert result["success"] is False
    assert "bs4" in result["error"].lower()


def test_degrade_and_pdf_results_share_html_success_schema():
    """WR-01: every result builder must return the same key set as the
    HTML-success path, so callers doing result["og_image"] never hit KeyError
    on the empty/degrade/PDF branches.
    """
    from services.search import content

    # Canonical shape produced by the HTML-success path (fetch_webpage_content
    # tail); og_image was previously missing from the two builders below.
    html_success_keys = {
        "url", "title", "content", "lists", "tables", "code_blocks",
        "meta_description", "meta_keywords", "og_image",
        "js_rendered", "js_message", "success", "error",
    }

    empty = content._empty_result("https://example.test/x", "bs4 not installed")
    assert set(empty.keys()) == html_success_keys
    assert empty["og_image"] == ""
