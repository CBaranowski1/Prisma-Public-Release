"""Regression coverage for the retired Query Builder API surface."""

import importlib.util
import sys
from unittest.mock import MagicMock

import pytest


def _heal_polluted_modules():
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def app_instance():
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    stubbed_bs4 = False
    if "bs4" not in sys.modules and importlib.util.find_spec("bs4") is None:
        sys.modules["bs4"] = MagicMock()
        stubbed_bs4 = True
    try:
        import app as app_module
    finally:
        if stubbed_bs4:
            sys.modules.pop("bs4", None)
    return app_module.app


def test_query_builder_routes_are_not_registered(app_instance):
    paths = set(app_instance.openapi()["paths"])
    retired_prefixes = (
        "/api/queries",
        "/api/relationships",
        "/api/datasources",
    )
    assert not any(
        path.startswith(prefix)
        for path in paths
        for prefix in retired_prefixes
    )
