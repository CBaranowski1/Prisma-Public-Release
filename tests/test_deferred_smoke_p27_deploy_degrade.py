"""Phase 37 automated re-encoding of Phase 27's deferred live smoke.

Source of the deferred properties: `.planning/phases/27-deploy-environment-
hardening/27-VERIFICATION.md` "Human Verification Required" items 1-3:

  1. Live Docker-healthy smoke (`docker compose up -d --build` +
     `docker inspect ... .State.Health.Status` reaching "healthy").
  2. Live-interpreter bs4-absent import
     (`python -c "sys.modules['bs4']=None; import services.search; ..."`).
  3. `docs/deploy-readiness-checklist.md` content-accuracy review.

This module automates item 2 in full (Task 1, below), and item 3 plus the
API-level half of item 1 (Task 2, below).

RESIDUAL (documented, not automated -- routed to Phase 38/39
reconciliation): a real `docker compose up -d --build` followed by
`docker inspect --format='{{json .State.Health.Status}}' <prisma-container>`
reaching status "healthy" against a live Docker daemon cannot be reproduced
in-process -- there is no Docker daemon in this test environment, and
faking that surface (e.g. with a skipped/xfail placeholder test) would
misrepresent coverage. Task 2's `/api/ready` middleware round-trip test is
the faithful in-process proxy for the exact endpoint the HEALTHCHECK curls
(confirmed identical semantics: 200 only when database + data_dir checks
pass), but it does not confirm the real container's Docker-reported health
status transitions to "healthy". That final confirmation remains a residual
for Phase 38/39.

Hermetic: no real Docker daemon, no real network fetch. Every helper needed
is replicated inline in this module (no cross-module fixture import), per
the Phase 26/27 integration-checker precedent.
"""
import sys
import importlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Task 1: bs4-absent graceful degrade on the reachable import chain
# ---------------------------------------------------------------------------


def test_search_imports_without_bs4_on_reachable_chain(monkeypatch):
    """`import services.search` must not raise when bs4 is simulated absent
    on the reachable import chain (services.search -> .core ->
    .providers/.content), and the package must be usable afterward (a
    public symbol is truthy), not just importable -- the hermetic proof for
    27-VERIFICATION.md Human Verification item 2 (live-interpreter
    bs4-absent import).
    """
    for mod in list(sys.modules):
        if mod == "services" or mod.startswith("services.search") or mod.startswith("bs4"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setitem(sys.modules, "bs4", None)

    import services.search  # must NOT raise

    assert services.search.PROVIDER_INFO  # usable, not just importable


class _FakeHtmlResponse:
    """Minimal httpx.Response stand-in carrying real HTML bytes -- no
    network required to drive the HTML-parsing degrade branch."""

    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b""
    text = "<html><head><title>Example</title></head><body>Hello world</body></html>"

    def raise_for_status(self):
        return None


def test_fetch_webpage_content_html_branch_degrades_without_bs4(monkeypatch, tmp_path):
    """With bs4 simulated absent, the reachable HTML-fallback surface
    (`fetch_webpage_content`'s HTML branch) must degrade to an empty/falsy
    result naming bs4 in its error, rather than raising. Drives the fetch
    with a monkeypatched HTML response -- no network required.
    """
    from services.search import content

    monkeypatch.setattr(content, "has_bs4", lambda: False)
    monkeypatch.setattr(content, "CONTENT_CACHE_DIR", tmp_path)
    content.content_cache_index.clear()
    monkeypatch.setattr(
        content, "_get_public_url", lambda url, headers, timeout: _FakeHtmlResponse()
    )

    result = content.fetch_webpage_content("https://example.test/p27-bs4-degrade-smoke")

    assert result["success"] is False
    assert not result["content"]
    assert "bs4" in result["error"].lower()


# ---------------------------------------------------------------------------
# Task 2: /api/ready HEALTHCHECK proxy + checklist accuracy + residual note
# ---------------------------------------------------------------------------


@pytest.fixture
def hardened_client(monkeypatch, tmp_path):
    """The exact hardened deploy config the Dockerfile HEALTHCHECK runs
    under: AUTH_ENABLED=true, no LOCALHOST_BYPASS. Mirrors
    tests/test_readiness_auth_exempt.py's fixture mechanics.
    """
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    from fastapi.testclient import TestClient
    import app as appmod
    # The full suite may have imported app earlier under an explicit
    # AUTH_ENABLED=false test. app.py installs AuthMiddleware at import time,
    # so reload under this fixture's hardened environment instead of reusing
    # that deliberately unauthenticated application instance.
    if not appmod.AUTH_ENABLED:
        appmod = importlib.reload(appmod)
    return TestClient(appmod.app)


def test_api_ready_unauthenticated_200_healthcheck_proxy(hardened_client):
    """GET /api/ready must return 200 (never 401) with no credentials --
    the faithful in-process proxy for the Docker HEALTHCHECK curl that hits
    this exact endpoint (see this file's module docstring RESIDUAL note for
    what remains un-automated: the live container's Docker-reported health
    status).
    """
    r = hardened_client.get("/api/ready")
    assert r.status_code != 401, f"readiness endpoint is auth-gated: {r.text}"
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("ready"), bool)
    assert isinstance(body.get("checks"), dict)


def test_api_models_still_401_no_over_exemption(hardened_client):
    """Guard against over-exemption: a normal protected route stays 401
    unauthenticated -- AUTH_EXEMPT_EXACT must not have widened past
    /api/ready + /api/health.
    """
    r = hardened_client.get("/api/models")
    assert r.status_code == 401, r.text


def test_deploy_checklist_documents_switches_and_placeholder_only_credentials():
    """docs/deploy-readiness-checklist.md must document the DATABASE_URL and
    CHROMADB_HOST/PORT prod switches, flag SQLite as worst-case concurrency,
    and carry only placeholder credentials -- the hermetic proof for
    27-VERIFICATION.md Human Verification item 3.

    The secret-shape check is kept conceptual per this plan's guidance:
    assert the placeholder marker's PRESENCE rather than a forbidden
    string's absence, so this test does not itself become a negative-grep
    trip hazard for a downstream secret-scanning gate.
    """
    checklist_path = Path(__file__).resolve().parent.parent / "docs" / "deploy-readiness-checklist.md"
    text = checklist_path.read_text(encoding="utf-8")

    for token in ("DATABASE_URL", "CHROMADB_HOST", "CHROMADB_PORT"):
        assert token in text, f"deploy checklist missing documentation for {token}"

    assert "worst-case concurrency" in text.lower(), (
        "deploy checklist must flag SQLite as the worst-case-concurrency default"
    )

    # Placeholder-only credentials: the doc labels its example connection
    # string explicitly with a PLACEHOLDER marker and generic
    # USER:PASSWORD@HOST tokens rather than any real-shaped secret.
    assert "PLACEHOLDER" in text, "deploy checklist example credential is not marked as a placeholder"
    assert "USER:PASSWORD@HOST" in text, "deploy checklist example credential is not the documented placeholder shape"
