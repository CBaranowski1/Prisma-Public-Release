"""Attack-surface regression for the retired Query Builder HTTP endpoints."""

from pathlib import Path


def test_query_and_relationship_routes_are_not_mounted():
    app_source = Path("app.py").read_text(encoding="utf-8")
    assert "setup_query_routes" not in app_source
    assert "setup_relationship_routes" not in app_source
    assert "routes.query_routes" not in app_source
    assert "routes.relationship_routes" not in app_source
