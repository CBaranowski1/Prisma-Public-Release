"""
Verify that the personal-organizer routes (Email, Calendar, Notes, Contacts)
and the Codex bridge are no longer mounted in the app.

These routers were disabled in app.py as part of the Prisma redesign
(see docs/superpowers/specs/2026-06-10-prisma-design.md). The route files
remain on disk but are not registered, so all paths must be absent from the
app's route table.

We introspect app.routes directly instead of making HTTP requests so the
test is not affected by auth middleware returning 401 vs 404.
"""

import sys
from unittest.mock import MagicMock
import pytest


@pytest.fixture(scope="module")
def mounted_paths():
    """Return a set of all route path strings registered in the app.

    Import app INSIDE the fixture (run time), not at module/collection scope:
    sibling test modules temporarily stub core.database / src.request_models at
    their own collection, and importing app during collection can race those
    stubs and fail route registration (FastAPIError). By run time they're
    restored.
    """
    # conftest stubs src.database with only SessionLocal + ModelEndpoint;
    # app.py's import chain (src.webhook_manager) also needs Webhook.
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    # Sibling unit tests stub model modules with MagicMock and import routes
    # against them, leaving routes.* bound to mocks — which makes app route
    # registration raise FastAPIError. Drop mock-contaminated models, then force
    # app + routes.* to re-import so they rebind against the real models.
    for _name in ("src.request_models", "core.models", "core.session_manager"):
        _m = sys.modules.get(_name)
        if _m is not None and type(_m).__name__ == "MagicMock":
            sys.modules.pop(_name, None)
    for _name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(_name, None)
    import app as app_module
    paths = set()
    for route in app_module.app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
    return paths


def test_organizer_routes_absent(mounted_paths):
    # Real GET paths confirmed from each organizer router (prefix + path):
    #   /api/calendar/calendars  — calendar_routes.py GET "/calendars"
    #   /api/notes               — note_routes.py    GET ""  (list notes)
    #   /api/email/list          — email_routes.py   GET "/list"
    #   /api/contacts/list       — contacts_routes.py GET "/list"
    organizer_paths = [
        "/api/calendar/calendars",
        "/api/notes",
        "/api/email/list",
        "/api/contacts/list",
    ]
    for path in organizer_paths:
        assert path not in mounted_paths, f"{path} is still registered in app.routes"
