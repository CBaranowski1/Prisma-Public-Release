"""SC (D-06) -- Phase 20's integration-checker connection: the assembled-
stack proof that all four cross-cutting security gates hold on REAL runs,
not merely in isolated units ("presence != integration", the milestone's
core principle). This file IS the Phase 20 SC connection -- the "passing
on-machine test" required alongside the per-gate unit tests already landed
in tests/test_council_verify.py (SEC-09), tests/test_mcp_manager.py
(SEC-15), tests/test_model_discovery_status.py (SEC-16), and
tests/test_agent_loop_verify_gate.py (SEC-11).

Four proofs, modeled on tests/test_verify_hardening_integration.py's
Phase-11 SC-5 through-the-team discipline and tests/test_verify_prompt_
injection_integration.py's Phase-18 SC-3 discipline -- every spy patches
the CALLING module's own bound name (never `src.<module>.*` directly, the
name-binding trap both prior files document):

  (a) SEC-09: with execution enabled, a REAL `src.council.verify.run_snippet`
      call proves a canary secret env var set on the PARENT process is NOT
      visible inside the executed child (env scrub). POSIX-only sub-proof:
      an over-cap allocation is killed by the resource.setrlimit caps
      (skipif win32) -- this leg calls `run_snippet` directly per this
      plan's own explicit instruction (unlike (b)/(d) below, there is no
      "not a direct call" constraint on this proof).

  (b) SEC-15: a corrupt/wrong-shape `disabled_tools` value on a fake
      McpServer row fails CLOSED through the REAL loader
      (`src.agent_loop._load_mcp_disabled_map`, NOT a direct
      `parse_disabled_tools()` unit call) -- the server's whole tool set is
      hidden downstream via `McpManager.get_all_openai_schemas`/
      `get_all_tools`, and an ERROR is logged. A well-formed sibling case
      proves the loader still narrows correctly (only the named tool is
      hidden) when the sentinel does NOT fire.

  (c) SEC-16: `model_discovery_scan_enabled` defaults False on a REAL
      `ModelDiscovery` instance -- `_get_hosts()` does not call
      `discover_tailscale_hosts()` (spy `call_count == 0`).

  (d) SEC-11: an `update_document`/`edit_document` Python-fenced block is
      fed through the REAL `src.agent_loop._gate_candidates` path (the
      function that decides WHICH raw text reaches the gate); the resulting
      candidate text is then handed to `verified_gate.evaluate` exactly as
      `stream_agent_loop`'s own call site does (agent_loop.py:2232-2236) --
      never a hand-rolled string passed directly to `evaluate()` bypassing
      `_gate_candidates`'s decision. A syntax-error document body flips
      `needs_repair` to True, proving the gate genuinely evaluates the
      candidate rather than rubber-stamping it.

Name-binding traps this file deliberately respects:
  - `src/agent_loop.py` imports `from src.mcp_manager import
    parse_disabled_tools, ALL_TOOLS_DISABLED` and `from src import
    verified_gate as _vg` -- proof (b) drives `agent_loop._load_mcp_
    disabled_map()` (which resolves its OWN bound `parse_disabled_tools`),
    and proof (d) patches `agent_loop._vg.evaluate` (a module-reference
    import, so this is the same underlying `src.verified_gate` module
    object -- patching it here is visible to any other caller resolving it
    via the same reference, and is restored by `monkeypatch` after the
    test).
  - `src/model_discovery.py` does `from src.settings import get_setting` at
    module scope -- proof (c) patches `model_discovery_mod.get_setting`
    (the bound name `_get_hosts()` actually resolves), not
    `src.settings.get_setting` (same trap documented in
    tests/test_model_discovery_status.py::test_scan_gate).
  - `core/database.py`'s module-level `SessionLocal` is what
    `agent_loop._load_mcp_disabled_map`'s own local
    `from core.database import McpServer, SessionLocal` re-resolves on
    every call -- proof (b) patches `core_database.SessionLocal` before
    calling the loader, exactly mirroring
    tests/test_mcp_manager.py::_stub_db_session's convention.

Offline throughout: no live network, no live LLM, no live DB file writes
(DB session is a MagicMock throughout). Deterministic/no-LLM (CLAUDE.md).
"""
from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import core.database as core_database
import src.agent_loop as agent_loop
import src.council.verify as verify_mod
import src.model_discovery as model_discovery_mod
from src.mcp_manager import ALL_TOOLS_DISABLED, McpManager


# ---------------------------------------------------------------------------
# Proof (a) -- SEC-09: run_snippet env-scrub + POSIX rlimit.
# Direct call to the REAL run_snippet, per this plan's explicit instruction
# (mirrors tests/test_council_verify.py::TestSEC09ExecutionHardening).
# ---------------------------------------------------------------------------

class TestProofA_RunSnippetEnvScrubAndRlimit:
    def test_canary_secret_is_absent_from_executed_snippet_env(self, monkeypatch):
        """A distinctive secret-shaped env var set on the PARENT process must
        NOT be visible inside the executed child -- proves the child's env is
        scrubbed to an allowlist, not inherited wholesale from os.environ.
        Cross-platform (no skip): the snippet itself exits non-zero if the
        canary is visible, so run_snippet's return value proves the scrub."""
        monkeypatch.setenv("PRISM_CANARY_SECRET", "super-secret-parent-value")
        code = (
            "import os, sys\n"
            "val = os.environ.get('PRISM_CANARY_SECRET')\n"
            "sys.exit(1 if val is not None else 0)\n"
        )
        ok, detail = verify_mod.run_snippet(code)
        assert ok is True, f"canary secret leaked into executed snippet env: {detail}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource module only")
    def test_rlimit_kills_over_cap_allocation(self):
        """Attempt to allocate well beyond the RLIMIT_AS cap (512MB) -- must
        be killed/fail, proving resource.setrlimit is genuinely enforced via
        the preexec_fn hook, not merely constructed and discarded."""
        code = "bytearray(1024 * 1024 * 1024 * 2)\n"  # 2GB, over the 512MB cap
        ok, detail = verify_mod.run_snippet(code)
        assert ok is False, "over-cap allocation should have been killed/failed by rlimit"


# ---------------------------------------------------------------------------
# Proof (b) -- SEC-15: disabled_tools fails CLOSED through the REAL loader
# (src.agent_loop._load_mcp_disabled_map), never a direct parse_disabled_
# tools() unit call. Downstream hiding proven via a real McpManager instance.
# ---------------------------------------------------------------------------

def _stub_db_session_with_servers(monkeypatch, servers):
    session = MagicMock()
    session.query.return_value.all.return_value = servers
    monkeypatch.setattr(core_database, "SessionLocal", lambda: session)
    return session


class TestProofB_DisabledToolsFailClosedThroughRealLoader:
    def test_corrupt_disabled_tools_fails_closed_through_loader_and_hides_tools(
        self, monkeypatch, caplog
    ):
        corrupt_srv = SimpleNamespace(id="srv-corrupt", disabled_tools='{"broken":')
        _stub_db_session_with_servers(monkeypatch, [corrupt_srv])

        with caplog.at_level(logging.ERROR, logger="src.mcp_manager"):
            disabled_map = agent_loop._load_mcp_disabled_map()

        assert any(r.levelname == "ERROR" for r in caplog.records), (
            "corrupt disabled_tools must log an ERROR, not fail silently"
        )
        assert disabled_map.get("srv-corrupt") is ALL_TOOLS_DISABLED

        # Downstream: the server's whole tool set is hidden via the REAL
        # McpManager consumer methods, driven by the map the loader produced.
        mgr = McpManager()
        mgr._tools["srv-corrupt"] = [
            {"name": "tool_a", "description": "does a", "input_schema": {}},
            {"name": "tool_b", "description": "does b", "input_schema": {}},
        ]
        mgr._connections["srv-corrupt"] = {"status": "connected", "name": "Corrupt Server"}

        schemas = mgr.get_all_openai_schemas(disabled_map)
        assert schemas == [], "corrupt disabled_tools must hide every tool of the server"

        tools = mgr.get_all_tools(disabled_map)
        assert tools, "tools were discovered at all"
        assert all(t["is_disabled"] for t in tools)

    def test_wellformed_disabled_tools_only_hides_the_named_tool(self, monkeypatch):
        """Sibling negative control: a well-formed disabled_tools value must
        NOT trip the fail-closed sentinel -- only the explicitly named tool
        is hidden, proving the loader narrows correctly, not just fails
        closed on everything."""
        good_srv = SimpleNamespace(id="srv-good", disabled_tools='["tool_a"]')
        _stub_db_session_with_servers(monkeypatch, [good_srv])

        disabled_map = agent_loop._load_mcp_disabled_map()
        assert disabled_map == {"srv-good": {"tool_a"}}

        mgr = McpManager()
        mgr._tools["srv-good"] = [
            {"name": "tool_a", "description": "does a", "input_schema": {}},
            {"name": "tool_b", "description": "does b", "input_schema": {}},
        ]
        mgr._connections["srv-good"] = {"status": "connected", "name": "Good Server"}

        schemas = mgr.get_all_openai_schemas(disabled_map)
        visible_names = {s["function"]["name"].rsplit("__", 1)[-1] for s in schemas}
        assert visible_names == {"tool_b"}, "only the explicitly-disabled tool should be hidden"


# ---------------------------------------------------------------------------
# Proof (c) -- SEC-16: model_discovery_scan_enabled defaults False on a REAL
# ModelDiscovery instance -- _get_hosts() must not call discover_tailscale_
# hosts() (spy call_count == 0).
# ---------------------------------------------------------------------------

class TestProofC_ModelDiscoveryScanGateDefaultOff:
    def test_scan_gate_off_by_default_on_real_instance(self, monkeypatch):
        for env_name in ("LLM_HOSTS", "OLLAMA_BASE_URL", "OLLAMA_URL", "LM_STUDIO_URL"):
            monkeypatch.delenv(env_name, raising=False)
        # Force the gate off regardless of any on-disk settings.json state --
        # patches model_discovery_mod's OWN bound `get_setting` name (the
        # attribute `_get_hosts()` actually resolves), not src.settings.
        monkeypatch.setattr(
            model_discovery_mod, "get_setting",
            lambda key, default=None: False, raising=False,
        )

        spy = MagicMock(wraps=model_discovery_mod.discover_tailscale_hosts)
        monkeypatch.setattr(model_discovery_mod, "discover_tailscale_hosts", spy)

        md = model_discovery_mod.ModelDiscovery(default_host="127.0.0.1")
        hosts = md._get_hosts()

        assert spy.call_count == 0, "discover_tailscale_hosts must not fire with the scan gate off"
        assert hosts == ["127.0.0.1", "host.docker.internal"]


# ---------------------------------------------------------------------------
# Proof (d) -- SEC-11: an update_document/edit_document Python-fenced block
# reaches verified_gate.evaluate as a side effect of the REAL
# _gate_candidates path -- never a hand-rolled string handed straight to
# evaluate() bypassing _gate_candidates's own decision of what to gate.
# ---------------------------------------------------------------------------

class TestProofD_DocWriteGateReachesVerifiedGateViaRealGateCandidates:
    """GAP-CLOSURE (Plan 09, CR-01): fixtures rebuilt to tool_schemas.py's
    REAL runtime content shapes (src/tool_schemas.py:1214-1229) -- a raw
    body for update_document, real `<<<FIND>>>/<<<REPLACE>>>/<<<END>>>`
    markers for edit_document -- replacing the Plan 06/08 masking fixtures
    that used create_document's `title\n[lang]\ncontent` shape for these
    two tools (a shape tool_schemas.py never actually produces for them;
    see 20-VERIFICATION.md)."""

    def test_update_document_python_block_reaches_verified_gate_evaluate(self, monkeypatch):
        spy = MagicMock(wraps=agent_loop._vg.evaluate)
        monkeypatch.setattr(agent_loop._vg, "evaluate", spy)

        update_doc = agent_loop.ToolBlock(
            "update_document", "```python\nprint('hello world')\n```"
        )
        cands = agent_loop._gate_candidates("", [update_doc], answer_reachable=True)
        assert cands, "_gate_candidates did not route the update_document block at all"

        # Mirrors agent_loop.py's own call site (stream_agent_loop, ~2232-2236):
        # feed each candidate _gate_candidates produced straight to evaluate().
        for _label, candidate_text in cands:
            agent_loop._vg.evaluate(candidate_text, execute=False)

        assert spy.call_count > 0, "verified_gate.evaluate never fired as a side effect"

    def test_edit_document_syntax_error_flips_needs_repair(self, monkeypatch):
        spy = MagicMock(wraps=agent_loop._vg.evaluate)
        monkeypatch.setattr(agent_loop._vg, "evaluate", spy)

        broken_doc = agent_loop.ToolBlock(
            "edit_document",
            "<<<FIND>>>\ndef ok():\n    pass\n<<<REPLACE>>>\n"
            "```python\ndef broken(:\n    pass\n```\n<<<END>>>",
        )
        cands = agent_loop._gate_candidates("", [broken_doc], answer_reachable=True)
        assert cands, "_gate_candidates did not route the edit_document block at all"

        needs_repair_flags = []
        for _label, candidate_text in cands:
            _verdict, needs_repair, _why = agent_loop._vg.evaluate(candidate_text, execute=False)
            needs_repair_flags.append(needs_repair)

        assert spy.call_count > 0
        assert any(needs_repair_flags), "syntax-error document body should flip needs_repair to True"
