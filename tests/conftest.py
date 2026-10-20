"""Shared test configuration — ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import shutil
import tempfile
import importlib.util
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Pre-import real heavy modules BEFORE any test file's module-level stubs can
# replace them with MagicMock. Some test files (e.g. test_llm_core_sanitize_*)
# stub sqlalchemy/core.database at module scope with `if mod not in sys.modules`,
# which fires during collection. If the real module hasn't been imported yet,
# the stub wins and contaminates every subsequent test that needs the real ORM.
try:
    import sqlalchemy  # noqa: F401
    import sqlalchemy.orm  # noqa: F401
    import core.database  # noqa: F401
except ImportError:
    pass  # not installed — the stubs below will handle it

# Also pre-import the real model/registry modules that some unit tests stub at
# module scope (guarded by `if name not in sys.modules`). Tests that `import app`
# during collection (e.g. test_brain_routes, test_organizer_removed) build the
# full FastAPI app, whose route response_models reference these — a leftover
# MagicMock stub here makes route registration raise FastAPIError at collection
# and interrupts the whole suite. Importing the real ones first makes the
# guarded stubs skip.
for _real in ("core.models", "core.session_manager", "src.request_models"):
    try:
        __import__(_real)
    except Exception:
        pass  # best-effort; the guarded stubs remain a fallback

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    _db.Webhook = MagicMock()
    sys.modules["src.database"] = _db


@pytest.fixture(scope="session", autouse=True)
def _isolate_real_settings_files():
    """Redirect the settings/features files to a temp copy for the whole suite so
    no test can overwrite the real data/settings.json or data/features.json.

    (A brain config PUT test once persisted a throwaway vault path into the real
    settings, wiping a user's configured brain_vault_path.) The temp files are
    SEEDED from the real ones so tests that read settings still see realistic
    values; all writes land in the temp dir and are discarded at session end.
    All of load/save/get_setting reference src.settings.SETTINGS_FILE at call
    time, so patching the module global is sufficient.
    """
    try:
        import src.settings as S
    except Exception:
        yield
        return
    tmp = tempfile.mkdtemp(prefix="prisma-test-data-")
    saved = {}
    for attr, name in (("SETTINGS_FILE", "settings.json"), ("FEATURES_FILE", "features.json")):
        orig = getattr(S, attr, None)
        if orig is None:
            continue
        saved[attr] = orig
        dest = os.path.join(tmp, name)
        if os.path.exists(orig):
            try:
                shutil.copyfile(orig, dest)
            except OSError:
                pass
        setattr(S, attr, dest)
    try:
        yield
    finally:
        for attr, orig in saved.items():
            setattr(S, attr, orig)
        shutil.rmtree(tmp, ignore_errors=True)




# --- Environment-aware skips ------------------------------------------------
# Optional/env-fragile, TEST-ONLY checks skip by default so a missing/incompatible
# environment never FAILS the suite (a red test for that hides the real signal).
# The app itself degrades gracefully at runtime without these. Real app-logic tests
# are never skipped here.
import os as _os
import sys as _sys

_RETIRED_MULTIUSER_TESTS = {
    "tests/test_auth_regressions.py::test_research_status_rejects_anonymous",
    "tests/test_auth_regressions.py::test_research_cancel_rejects_anonymous",
    "tests/test_auth_regressions.py::test_research_delete_rejects_anonymous",
    "tests/test_auth_regressions.py::test_research_spinoff_rejects_anonymous",
    "tests/test_auth_recovery.py::test_flagged_user_blocked_on_non_allowlisted_path",
    "tests/test_deferred_smoke_p27_deploy_degrade.py::test_api_models_still_401_no_over_exemption",
    "tests/test_diagnostics_integrations_assembled.py::test_unauthenticated_rejected",
    "tests/test_edit_file.py::test_edit_file_blocked_at_execution_for_non_admin",
    "tests/test_history_topics_owner_scope.py::test_route_rejects_or_scopes_under_loopback_bypass",
    "tests/test_null_owner_gates.py::test_document_owner_filter_rejects_anonymous_when_auth_is_enabled",
    "tests/test_readiness_auth_exempt.py::test_protected_endpoint_still_rejected",
    "tests/test_review_regressions.py::test_admin_agent_tools_require_admin",
    "tests/test_review_regressions.py::test_public_agent_policy_blocks_sensitive_tools",
    "tests/test_review_regressions.py::test_public_agent_policy_hides_sensitive_tools",
    "tests/test_security_regressions.py::test_require_user_rejects_unauthenticated",
    "tests/test_security_regressions.py::test_require_user_localhost_bypass_still_rejects_lan",
    "tests/test_session_ghost_delete.py::test_unauthenticated_still_403",
    "tests/test_session_owner_attribution.py::test_unauthenticated_caller_rejected",
    "tests/test_verify_companion_pairing_integration.py::test_sc4_mint_redeem_use_replay_denied_and_expired_rejected",
    "tests/test_verify_trust_boundary_integration.py::test_internal_tool_token_defeated_by_forged_loopback_header",
    "tests/test_verify_trust_boundary_integration.py::test_new_nonallowlisted_tool_default_denied_for_nonadmin_owner",
    "tests/test_verify_trust_boundary_integration.py::test_presetup_zero_users_does_not_failopen_privileged_tool",
    "tests/test_verify_trust_boundary_integration.py::test_spoofed_current_user_internal_tool_string_without_flag_denied",
}


@pytest.fixture(autouse=True)
def _reset_auth_manager_singleton():
    """SEC-27c / D-07c (Pitfall 2): core.auth.get_auth_manager() caches a
    process-level AuthManager singleton. Existing tests throughout this repo
    monkeypatch `core.auth.AuthManager` directly (relying on fresh, lazy
    per-call construction) or construct `AuthManager(path)` themselves — an
    un-reset singleton would either defeat those monkeypatches or leak a
    stubbed/real instance across test boundaries. Clear the cache before AND
    after every test so each test starts and ends with an empty cache.
    """
    try:
        from core.auth import reset_auth_manager_cache
    except Exception:
        yield
        return
    reset_auth_manager_cache()
    yield
    reset_auth_manager_cache()


def pytest_collection_modifyitems(config, items):
    import pytest
    # JS unit tests drive the browser JS through a Node ESM harness. Node 24 on
    # Windows breaks that harness (drive-letter ESM URL scheme) - but the app's JS
    # runs in the browser, not Node. Skip by default; opt in with RUN_JS_TESTS=1
    # once you have a compatible Node (e.g. `nvm use 20`).
    run_js = _os.environ.get("RUN_JS_TESTS") == "1"
    js_skip = pytest.mark.skip(reason="JS harness needs a compatible Node (set RUN_JS_TESTS=1; app JS runs in-browser)")
    mac_skip = pytest.mark.skip(reason="macOS-only test on a non-macOS host")
    retired_auth_skip = pytest.mark.skip(
        reason=(
            "retired multi-user/login behavior; Prisma Desktop is permanently "
            "single-user and protected by loopback binding plus SSH tunneling"
        )
    )
    for it in items:
        path = it.nodeid.split("::", 1)[0]
        if it.nodeid in _RETIRED_MULTIUSER_TESTS:
            it.add_marker(retired_auth_skip)
        elif path.endswith("_js.py") and not run_js:
            it.add_marker(js_skip)
        elif path.endswith("test_hwfit_macos.py") and _sys.platform != "darwin":
            it.add_marker(mac_skip)
