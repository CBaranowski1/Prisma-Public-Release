"""Regression coverage for removal of the Query Builder from Chat."""

from pathlib import Path


def test_query_builder_ui_is_retired():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    style_css = Path("static/style.css").read_text(encoding="utf-8")

    assert 'id="tool-query-btn"' not in index_html
    assert ">Query</span>" not in index_html
    assert "tool-query-btn" not in app_js
    assert "queryBuilder.js" not in app_js
    assert "openQueryBuilder" not in app_js
    assert "/* === Prism Query ===" not in style_css
    assert not Path("static/js/queryBuilder.js").exists()
    assert not any(Path("static/js/queryBuilder").glob("*.js"))
