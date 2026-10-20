"""
Basic tests for prisma-ui application structure
"""
import pytest
import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAppStructure:
    """Test that required modules and files exist"""

    def test_app_file_exists(self):
        """Test that app.py exists"""
        app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")
        assert os.path.exists(app_path), "app.py should exist"

    def test_static_directory_exists(self):
        """Test that static directory exists"""
        static_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        assert os.path.exists(static_path), "static directory should exist"

    def test_routes_directory_exists(self):
        """Test that routes directory exists"""
        routes_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "routes")
        assert os.path.exists(routes_path), "routes directory should exist"

    def test_src_directory_exists(self):
        """Test that src directory exists"""
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
        assert os.path.exists(src_path), "src directory should exist"

    def test_env_file_is_optional_and_ignored(self):
        """A fresh checkout should not require a private .env file."""
        root = os.path.dirname(os.path.dirname(__file__))
        gitignore_path = os.path.join(root, ".gitignore")
        with open(gitignore_path, encoding="utf-8") as fh:
            ignored = {line.strip() for line in fh}
        assert ".env" in ignored, ".env should stay local and ignored"

    def test_env_example_exists(self):
        """Test that .env.example exists"""
        env_example_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.example")
        assert os.path.exists(env_example_path), ".env.example file should exist"


class TestImports:
    """Test that key modules can be imported"""

    def test_constants_importable(self):
        """Test that constants module is importable"""
        from src.constants import BASE_DIR, STATIC_DIR, SESSIONS_FILE, MEMORY_FILE
        assert BASE_DIR is not None
        assert STATIC_DIR is not None

    def test_app_helpers_importable(self):
        """Test that app_helpers module is importable"""
        from src.app_helpers import abs_join
        assert callable(abs_join)

    def test_exceptions_importable(self):
        """Test that exceptions module is importable"""
        from src.exceptions import (
            SessionNotFoundError,
            InvalidFileUploadError,
            LLMServiceError,
            WebSearchError,
        )
        # These should be exception classes
        assert issubclass(SessionNotFoundError, Exception)


class TestRouteFiles:
    """Test that route files exist and have proper structure"""

    def test_auth_routes_exist(self):
        """Test auth_routes.py exists"""
        routes_path = os.path.dirname(os.path.dirname(__file__))
        auth_routes = os.path.join(routes_path, "routes", "auth_routes.py")
        assert os.path.exists(auth_routes), "auth_routes.py should exist"

    def test_chat_routes_exist(self):
        """Test chat_routes.py exists"""
        routes_path = os.path.dirname(os.path.dirname(__file__))
        chat_routes = os.path.join(routes_path, "routes", "chat_routes.py")
        assert os.path.exists(chat_routes), "chat_routes.py should exist"

    def test_memory_routes_exist(self):
        """Test memory_routes.py exists"""
        routes_path = os.path.dirname(os.path.dirname(__file__))
        mem_routes = os.path.join(routes_path, "routes", "memory_routes.py")
        assert os.path.exists(mem_routes), "memory_routes.py should exist"


def test_integrated_model_ide_routes_registered_in_app():
    """The native Model IDE schema API is mounted; the Zed bridge is retired."""
    root = os.path.dirname(os.path.dirname(__file__))
    source = open(os.path.join(root, "app.py"), encoding="utf-8").read()
    assert "app.include_router(setup_model_ide_routes())" in source
    assert "setup_ide_routes" not in source


def test_openai_facade_registered_in_app():
    """Smoke: /v1/models reachable (401 without bearer) when PRISM_TOKEN set.

    A 404 would mean the OpenAI-compat facade router was never registered.
    A 401 confirms it is registered and correctly requires a bearer token.

    Uses the same hermetic harness as test_ide_routes_registered_in_app:
    evict first-party modules, set env vars, fresh-import app, then restore
    everything in a finally block to avoid cross-test pollution.
    """
    import os, sys

    _FIRST_PARTY = ("app", "routes", "core", "src", "services")

    def _is_first_party(name):
        return any(name == p or name.startswith(p + ".") for p in _FIRST_PARTY)

    saved_modules = {k: v for k, v in sys.modules.items() if _is_first_party(k)}
    saved_env = {k: os.environ.get(k) for k in ("PRISM_TOKEN", "AUTH_ENABLED")}
    try:
        os.environ["PRISM_TOKEN"] = "smoke-test-token"
        # Disable auth middleware so the test client doesn't need a session cookie.
        os.environ["AUTH_ENABLED"] = "false"
        # Evict first-party modules so the fresh import binds real
        # implementations and re-reads os.getenv("PRISM_TOKEN").
        for name in list(sys.modules.keys()):
            if _is_first_party(name):
                del sys.modules[name]
        from fastapi.testclient import TestClient
        import app as app_mod
        client = TestClient(app_mod.app)
        resp = client.get("/v1/models")
        # 401 → registered, but requires a bearer token (no Authorization header sent)
        # 404 → router was never registered (fail)
        assert resp.status_code == 401, (
            f"Expected 401 (facade registered, needs bearer) but got {resp.status_code}. "
            "A 404 means the OpenAI facade router is not registered in app.py."
        )
    finally:
        # Restore env vars to their prior values (or remove if unset before).
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # Restore sys.modules: drop anything our import loaded, then put the
        # originals back so downstream tests see the state they expect.
        for name in list(sys.modules.keys()):
            if _is_first_party(name) and name not in saved_modules:
                del sys.modules[name]
        sys.modules.update(saved_modules)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
