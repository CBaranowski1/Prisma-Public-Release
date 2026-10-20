"""Regression tests for SEC-07 narrow-log-preserve-fallback fixes applied to
``src/tool_implementations.py`` (Phase 20 Plan 04, cluster 1 of 3).

Enumerated silent sites this plan edits (pre-fix line numbers, function):
  -  165  ``_sniff_doc_language``                      -- json.loads probe for JSON-shaped doc text
  - 1200  ``do_manage_mcp`` (delete action)             -- mcp.disconnect_server best-effort
  - 1422  ``do_manage_documents`` (``_rel`` closure)     -- relative-timestamp formatting fallback
  - 1622  ``do_manage_settings`` (``_endpoint_model_from_cache`` closure) -- cached_models JSON parse
  - 1805  ``do_api_call``                                -- line-based body JSON parse fallback
  - 1885  ``do_manage_notes`` (list action)               -- checklist items JSON parse for display
  - 1925  ``do_manage_notes`` (add action)                -- natural-language due_date parse fallback
  - 1986  ``do_manage_notes`` (update action)             -- natural-language due_date parse fallback
  - 2507  ``_cookbook_servers``                           -- cookbook state fetch fallback
  - 2841  ``do_app_api``                                  -- generic API proxy response JSON parse fallback
  - 3207  ``_cookbook_kill_session``                      -- shell/exec response JSON parse fallback
  - 3393  ``do_adopt_served_model`` (health-check probe)  -- best-effort httpx health check
  - 3449  ``do_adopt_served_model`` (endpoint registration) -- self-import fallback
  - 3666  ``do_list_cached_models``                       -- cookbook state fallback lookup

Deliberately EXCLUDED from this plan's edit set (not silently skipped -- a
documented scope decision, not an oversight):
  - 2926  ``_scan_running_model_processes`` -- the per-PID
    ``except (OSError, PermissionError): continue`` while reading
    ``/proc/<pid>/cmdline`` is an expected, high-frequency race/permission
    condition when scanning arbitrary other-user processes (not a hidden
    failure); the enclosing function already has a broader
    ``except Exception as e: logger.debug(...)`` at line 2963 covering any
    unusual failure. Adding a per-iteration log here would be log-spam
    noise proportional to the number of processes on the host, not an
    improvement in observability.

Two representative sites are exercised end-to-end below (``except_narrowing``
cases): each (a) monkeypatches the site's inner call to raise the triggering
exception, (b) asserts the enclosing function still returns its EXACT
pre-change fallback value/shape, and (c) asserts a caplog record was emitted
for that site. Both cases are RED against the pre-fix source (the sites are
currently silent -- no log call exists yet).

Phase 42 Plan 01 (SEC-07 residual, TAIL-01) extends the site list above
~3666 with the 6 sites missed by Phase 20 Plan 04 (16-20 independent audit
finding, ``.planning/todos/pending/sec-07-residual-silent-swallows.md``):
  - 3903  ``do_resolve_contact`` (CardDAV block)          -- contact-lookup best-effort
  - 3915  ``do_resolve_contact`` (email-history block)    -- contact-lookup best-effort
  - 4008  ``_load_vault_config``                          -- malformed vault.json JSON parse
  - 4117  ``do_vault_get`` (audit-log write)               -- assistant-log best-effort (T-42-01, repudiation)
  - 4164  ``do_vault_unlock`` (reload-existing-cfg)        -- malformed vault.json JSON parse
  - 4173  ``do_vault_unlock`` (chmod 0600)                 -- session-file perms best-effort (T-42-02)

All 6 are exercised end-to-end below with the same ``except_narrowing``
convention as the two representative sites above.
"""

import asyncio
from importlib import import_module
import json
import logging

import pytest

from src import tool_implementations as tools


# ---------------------------------------------------------------------------
# Site 165 -- _sniff_doc_language: json.loads probe on JSON-shaped text
# ---------------------------------------------------------------------------

def test_sniff_doc_language_except_narrowing_json_parse_failure(caplog):
    """Malformed but curly-brace-prefixed text falls through the json.loads
    probe to markdown detection -- fallback value must stay 'markdown' AND
    the parse failure must now be logged (it was silently `pass`-ed before)."""
    text = "{not really json, still curly}"

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = tools._sniff_doc_language(text)

    # Fallback value/control-flow preserved byte-for-byte.
    assert result == "markdown"
    # The formerly-silent site must now be observable.
    assert any(
        "json" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed json.loads failure, got: {[r.getMessage() for r in caplog.records]}"


def test_sniff_doc_language_valid_json_still_detected():
    """Non-regression: valid JSON-shaped text is still detected as 'json'
    (the try branch's success path is untouched by this plan)."""
    assert tools._sniff_doc_language('{"a": 1}') == "json"


# ---------------------------------------------------------------------------
# Site 1805 -- do_api_call: line-based body JSON parse fallback
# ---------------------------------------------------------------------------

def test_do_api_call_except_narrowing_body_json_parse_failure(monkeypatch, caplog):
    """When the top-level content isn't JSON, do_api_call falls back to a
    line-based format (integration / 'METHOD path' / body). If the body
    line itself isn't valid JSON, the pre-fix code silently drops it
    (`except json.JSONDecodeError: pass`) and `body` is simply absent from
    args. That fallback (body=None reaching execute_api_call) must be
    preserved byte-for-byte AND the parse failure must now be logged."""
    integrations = [{"id": "myintegration", "name": "MyIntegration", "enabled": True}]
    calls = {}

    async def fake_execute_api_call(integration_id, method, path, params=None, body=None, extra_headers=None):
        calls["integration_id"] = integration_id
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {"output": "ok", "exit_code": 0}

    # Full-suite isolation tests may reload ``src`` modules. Patch the module
    # object currently registered for the lazy imports in ``do_api_call``.
    active_integrations = import_module("src.integrations")
    monkeypatch.setattr(active_integrations, "load_integrations", lambda: integrations)
    monkeypatch.setattr(active_integrations, "execute_api_call", fake_execute_api_call)

    content = "myintegration\nPOST /widgets\nnot valid json body"

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = asyncio.run(tools.do_api_call(content))

    assert result == {"output": "ok", "exit_code": 0}
    # Fallback control flow preserved: unparseable body line -> body stays
    # absent from args -> None reaches execute_api_call, exactly as before.
    assert calls["method"] == "POST"
    assert calls["path"] == "/widgets"
    assert calls["body"] is None
    assert any(
        "body" in r.getMessage().lower() or "json" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed body json.loads failure, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Phase 42 Plan 01 (SEC-07 residual, TAIL-01) -- the 6 sites missed by
# Phase 20 Plan 04 (see module docstring above for the full site list).
# ---------------------------------------------------------------------------

class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in: async context manager with a
    single .get() coroutine whose behavior is injected per-test."""

    def __init__(self, get_impl, *_a, **_kw):
        self._get_impl = get_impl

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, *a, **kw):
        return await self._get_impl(*a, **kw)


def _make_fake_async_client(get_impl):
    """Return a class usable as a drop-in for httpx.AsyncClient in monkeypatch.setattr,
    where get_impl is an async callable implementing .get()."""

    def factory(*a, **kw):
        return _FakeAsyncClient(get_impl, *a, **kw)

    return factory


# ---------------------------------------------------------------------------
# Site 3903 -- do_resolve_contact: CardDAV lookup best-effort
# ---------------------------------------------------------------------------

def test_do_resolve_contact_except_narrowing_carddav_lookup_failure(monkeypatch, caplog):
    """CardDAV lookup failing (site ~3903) must not break contact resolution --
    it silently falls through to the email-history lookup, `contacts` stays
    unpopulated from that source exactly as before. The swallowed failure
    must now be logged."""

    def boom():
        raise RuntimeError("carddav down")

    monkeypatch.setattr("routes.contacts_routes._fetch_contacts", boom)

    async def fake_get(*_a, **_kw):
        class _Resp:
            status_code = 200

            def json(self):
                return {"contacts": []}

        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _make_fake_async_client(fake_get))

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = asyncio.run(tools.do_resolve_contact(json.dumps({"name": "Alice"})))

    # Fallback control flow preserved: CardDAV source contributes nothing,
    # email-history source (mocked empty) contributes nothing -> "no contacts".
    assert result == {"output": "No contacts found matching 'Alice'.", "exit_code": 0}
    assert any(
        "carddav" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed CardDAV lookup failure, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Site 3915 -- do_resolve_contact: email-history lookup best-effort
# ---------------------------------------------------------------------------

def test_do_resolve_contact_except_narrowing_email_history_lookup_failure(monkeypatch, caplog):
    """Email-history lookup failing (site ~3915) must not break contact
    resolution -- `contacts` stays unpopulated from that source exactly as
    before. The swallowed failure must now be logged."""

    monkeypatch.setattr("routes.contacts_routes._fetch_contacts", lambda: [])

    async def fake_get(*_a, **_kw):
        raise RuntimeError("email history unreachable")

    monkeypatch.setattr("httpx.AsyncClient", _make_fake_async_client(fake_get))

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = asyncio.run(tools.do_resolve_contact(json.dumps({"name": "Bob"})))

    assert result == {"output": "No contacts found matching 'Bob'.", "exit_code": 0}
    assert any(
        "email-history" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed email-history lookup failure, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Site 4008 -- _load_vault_config: malformed vault.json JSON parse
# ---------------------------------------------------------------------------

def test_load_vault_config_except_narrowing_malformed_json(monkeypatch, tmp_path, caplog):
    """Malformed vault.json (site ~4008) must not crash config lookup --
    `_load_vault_config` returns `{}` exactly as before. The swallowed JSON
    parse failure must now be logged."""

    (tmp_path / "vault.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("core.constants.DATA_DIR", str(tmp_path))

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = tools._load_vault_config()

    assert result == {}
    assert any(
        "_load_vault_config" in r.getMessage().lower() or "malformed vault.json" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed vault.json parse failure, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Site 4117 -- do_vault_get: audit-log write to assistant chat (T-42-01)
# ---------------------------------------------------------------------------

def test_do_vault_get_except_narrowing_audit_log_write_failure(monkeypatch, caplog):
    """The vault-get audit-log write to assistant chat (site ~4117, T-42-01)
    failing must not prevent the retrieved vault item from being returned --
    `output` stays intact exactly as before. The swallowed audit-write
    failure must now be logged (closes a repudiation gap: a failed record of
    a password-retrieval action is no longer silently lost)."""

    monkeypatch.setattr(tools, "_load_vault_config", lambda: {"session": "sess-token"})

    async def fake_run_bw(args, session=None, input_text=None):
        item = {
            "name": "Example Site",
            "login": {"username": "alice", "password": "hunter2"},
        }
        return json.dumps(item), "", 0

    monkeypatch.setattr(tools, "_run_bw", fake_run_bw)

    def boom_log(*_a, **_kw):
        raise RuntimeError("assistant log unavailable")

    monkeypatch.setattr("src.assistant_log.log_to_assistant", boom_log)

    content = json.dumps({"item_id": "item-1", "reason": "testing"})

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = asyncio.run(tools.do_vault_get(content, owner="alice"))

    expected_output = "Vault item: Example Site\nUsername: alice\nPassword: hunter2"
    assert result == {"output": expected_output, "exit_code": 0}
    assert any(
        "audit-log" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed audit-log write failure, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Site 4164 -- do_vault_unlock: reload-existing-cfg malformed JSON
# ---------------------------------------------------------------------------

def test_do_vault_unlock_except_narrowing_malformed_existing_config(monkeypatch, tmp_path, caplog):
    """If the on-disk vault.json is malformed when `do_vault_unlock` reloads
    it (site ~4164) before writing the new session, `cfg` silently resets to
    `{}` and the session is still persisted -- exactly as before. The
    swallowed parse failure must now be logged."""

    vault_file = tmp_path / "vault.json"
    vault_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("core.constants.DATA_DIR", str(tmp_path))

    async def fake_run_bw(args, session=None, input_text=None):
        return "new-session-token", "", 0

    monkeypatch.setattr(tools, "_run_bw", fake_run_bw)

    content = json.dumps({"master_password": "correct-horse"})

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = asyncio.run(tools.do_vault_unlock(content))

    assert result == {"output": "Vault unlocked. Session saved.", "exit_code": 0}
    saved = json.loads(vault_file.read_text(encoding="utf-8"))
    assert saved["session"] == "new-session-token"
    assert "unlocked_at" in saved
    assert any(
        "do_vault_unlock" in r.getMessage().lower() and "reload" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed vault.json reload failure, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Site 4173 -- do_vault_unlock: chmod 0600 on vault.json (T-42-02)
# ---------------------------------------------------------------------------

def test_do_vault_unlock_except_narrowing_chmod_failure(monkeypatch, tmp_path, caplog):
    """If chmod 0600 on the freshly-written vault.json fails (site ~4173,
    T-42-02), `do_vault_unlock` still reports success with its exact prior
    return value -- perms hardening stays best-effort by design (residual
    accepted per the threat register), but the failure must now be logged so
    a group/world-readable vault.json is observable."""

    monkeypatch.setattr("core.constants.DATA_DIR", str(tmp_path))

    async def fake_run_bw(args, session=None, input_text=None):
        return "new-session-token", "", 0

    monkeypatch.setattr(tools, "_run_bw", fake_run_bw)

    def boom_chmod(*_a, **_kw):
        raise OSError("chmod not permitted")

    monkeypatch.setattr("os.chmod", boom_chmod)

    content = json.dumps({"master_password": "correct-horse"})

    with caplog.at_level(logging.DEBUG, logger="src.tool_implementations"):
        result = asyncio.run(tools.do_vault_unlock(content))

    assert result == {"output": "Vault unlocked. Session saved.", "exit_code": 0}
    assert any(
        "chmod" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a log record naming the swallowed chmod failure, got: {[r.getMessage() for r in caplog.records]}"
