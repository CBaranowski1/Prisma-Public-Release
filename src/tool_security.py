"""Server-side tool safety policy."""

from __future__ import annotations

import logging
from typing import Optional, Set

logger = logging.getLogger(__name__)


# Explicit registry of tools ANY authenticated (or single-user/no-auth) owner
# may run directly. Everything NOT listed here -- including any future tool
# and any mcp__* tool -- is admin-only by default (SEC-27a). This is the
# ENFORCEMENT source of truth; see `is_public_blocked_tool` below.
#
# Derived as the deliberate, reviewed complement of the old
# NON_ADMIN_BLOCKED_TOOLS denylist, MINUS the tools agent_loop.py's
# prompt-assembly `_ADMIN_TOOLS` (src/agent_loop.py:993-998) already signals
# are admin-only in intent but were never enforced at execution time
# (create_session, list_sessions, send_to_session, pipeline, manage_session,
# list_models, ask_teacher -- 17-RESEARCH.md Summary finding #1). Those 7
# tools are intentionally NOT included here, so they now default-deny.
#
# `run_query` is intentionally OMITTED -- it has its own dedicated
# `owner_can_manage_data_sources` gate below and must never be granted by
# blanket allowlist membership.
#
# Email (send_email/read_email/reply_to_email/list_emails/...) and contact
# (resolve_contact/manage_contact) tools are intentionally EXCLUDED, deviating
# from 17-RESEARCH.md Pattern 4's example list: `do_resolve_contact` /
# `do_manage_contact` (src/tool_implementations.py) accept `owner` but never
# filter by it (they hit a deployment-wide CardDAV/contacts endpoint with no
# owner scoping), and the email-tool names have no working dispatch handler
# in tool_execution.py at all today -- neither satisfies RESEARCH's own
# Assumption A1/Open Question 2 "confirm each candidate's handler
# owner-scopes internally... treat any tool whose handler does NOT
# owner-scope internally as a candidate for exclusion." An existing
# regression test (tests/test_review_regressions.py::
# test_public_agent_policy_blocks_sensitive_tools) also already asserts
# send_email/read_file/mcp__email__send_email stay blocked for non-admins.
# Per this plan's own action text ("when in doubt, exclude -- default-deny
# is the safe direction"), both families are left off the allowlist.
PUBLIC_TOOL_ALLOWLIST: Set[str] = {
    "web_search",
    "web_fetch",
    "create_document",
    "edit_document",
    "suggest_document",
    "update_document",
    "chat_with_model",
    "manage_notes",       # do_manage_notes: filters by Note.owner == owner
    "manage_calendar",    # do_manage_calendar: filters by CalendarCal.owner == owner
    "edit_image",
    "generate_image",
    "trigger_research",
    "ui_control",
}


# Presentation-only hide-set: tools disabled/hidden in the UI/prompt for
# non-admin owners via `blocked_tools_for_owner`. This is NOT the enforcement
# source of truth anymore -- `PUBLIC_TOOL_ALLOWLIST` / `is_public_blocked_tool`
# is (SEC-27a). Kept under its historical name because several existing tests
# import it directly. Contents are the old denylist PLUS the 7 agent_loop
# prompt-assembly-only admin tools, so the UI still hides them even though
# MCP tools are already hidden wholesale for public users at
# agent_loop.py:1485.
NON_ADMIN_BLOCKED_TOOLS = {
    "bash",
    "python",
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "glob",
    "ls",
    "search_chats",
    "manage_memory",
    "manage_skills",
    "manage_tasks",
    "manage_endpoints",
    "manage_mcp",
    "manage_webhooks",
    "manage_tokens",
    "manage_documents",
    "manage_settings",
    "api_call",
    "app_api",
    "send_email",
    "reply_to_email",
    "list_emails",
    "read_email",
    "resolve_contact",
    "manage_contact",
    "manage_calendar",
    "vault_search",
    "vault_get",
    "vault_unlock",
    "download_model",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "cancel_download",
    "adopt_served_model",
    # The 7 tools agent_loop.py's prompt-assembly _ADMIN_TOOLS already
    # signaled as admin-only but that were never enforced at execution time
    # (17-RESEARCH.md Summary finding #1). Presentation-only membership here;
    # enforcement is via PUBLIC_TOOL_ALLOWLIST exclusion (SEC-27a).
    "create_session",
    "list_sessions",
    "send_to_session",
    "pipeline",
    "manage_session",
    "list_models",
    "ask_teacher",
}


def is_public_blocked_tool(tool_name: Optional[str]) -> bool:
    """Return True when a non-admin/public user must not execute this tool.

    Fail-closed allowlist (SEC-27a): only an exact `PUBLIC_TOOL_ALLOWLIST`
    match is public. Anything else -- an unknown/new tool, any `mcp__*` tool,
    or a malformed non-string identifier -- is blocked for non-admins.
    ``None`` / empty string mean there is no tool to gate.
    """
    if tool_name is None or tool_name == "":
        return False
    if not isinstance(tool_name, str):
        return True
    return tool_name not in PUBLIC_TOOL_ALLOWLIST


def owner_is_admin_or_single_user(owner: Optional[str]) -> bool:
    """Return True for admins, or when auth is explicitly disabled (SEC-27b).

    Grants ONLY when `_auth_disabled()` is True (the operator explicitly set
    AUTH_ENABLED=false -- intentional no-auth mode) or `owner` is an admin.
    Pre-setup (auth enabled, zero users) now DENIES: `owner` is falsy or
    `auth.is_admin(owner)` is False for every username while `self.users` is
    empty, so this closes the previous unconfigured-auth fail-open bare
    grant without touching the intentional no-auth path.
    """
    try:
        from src.auth_helpers import _auth_disabled

        if _auth_disabled():
            return True
        from core.auth import get_auth_manager

        auth = get_auth_manager()
        return bool(owner and auth.is_admin(owner))
    except Exception as exc:
        logger.warning("Unable to evaluate owner admin status: %s", exc)
        return False


def blocked_tools_for_owner(owner: Optional[str]) -> Set[str]:
    """Tools to hide/disable for this owner under public-user policy."""
    if owner_is_admin_or_single_user(owner):
        return set()
    return set(NON_ADMIN_BLOCKED_TOOLS)


def owner_can_manage_data_sources(owner: Optional[str]) -> bool:
    """Fail-closed gate for the run_query tool (D-03 / T-10-01).

    Returns True only when `owner` holds the can_manage_data_sources
    privilege (admin, or coder role via `_CODER_PRIVILEGES`). Deliberately
    independent of NON_ADMIN_BLOCKED_TOOLS/_ADMIN_TOOLS so it stays
    forward-compatible with the SEC-27 allowlist flip (Phase 17).

    Fail-closed matrix:
      - unconfigured/single-user auth -> True (matches route semantics)
      - owner is falsy on configured auth -> False
      - can_manage_data_sources missing or False -> False
      - AuthManager construction/lookup raises -> False

    The broad `except Exception -> False` is the one deliberate exception to
    narrow-except discipline here: this is a security gate, so any failure
    to determine the privilege must deny, never silently allow.
    """
    try:
        from core.auth import get_auth_manager

        auth = get_auth_manager()
        if not auth.is_configured:
            return True
        if not owner:
            return False
        return bool((auth.get_privileges(owner) or {}).get("can_manage_data_sources", False))
    except Exception as exc:
        logger.warning("Unable to evaluate can_manage_data_sources for owner=%r: %s", owner, exc)
        return False
