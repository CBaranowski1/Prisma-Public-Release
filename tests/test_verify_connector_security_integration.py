"""The connector management HTTP surface is retired from the assembled app.

Low-level connector security remains covered by the datasource connector,
confinement, SSRF, TLS, and credential-scrubbing unit suites.
"""

from pathlib import Path


def test_datasource_connector_routes_are_not_mounted():
    app_source = Path("app.py").read_text(encoding="utf-8")
    assert "setup_datasource_routes" not in app_source
    assert "routes.datasource_routes" not in app_source
