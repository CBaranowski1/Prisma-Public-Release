"""Regression guard for the agent shell/python kill-switch (audit gap 1).

PRISM_AGENT_SHELL_DISABLED (default off) must deny the bash/python tools without
spawning a process, and must be a no-op when unset so existing setups still run.
"""

import asyncio

import pytest

import src.tool_execution as te


def _run(tool, content):
    return asyncio.new_event_loop().run_until_complete(te._direct_fallback(tool, content))


def test_default_is_off(monkeypatch):
    monkeypatch.delenv("PRISM_AGENT_SHELL_DISABLED", raising=False)
    assert te._agent_shell_disabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_flag_parses_truthy(monkeypatch, val):
    monkeypatch.setenv("PRISM_AGENT_SHELL_DISABLED", val)
    assert te._agent_shell_disabled() is True


def test_bash_denied_when_disabled(monkeypatch):
    monkeypatch.setenv("PRISM_AGENT_SHELL_DISABLED", "true")
    r = _run("bash", "echo SHOULD_NOT_RUN")
    assert r.get("exit_code") == 126
    assert "disabled by operator policy" in r.get("error", "")


def test_python_denied_when_disabled(monkeypatch):
    monkeypatch.setenv("PRISM_AGENT_SHELL_DISABLED", "true")
    r = _run("python", "print('SHOULD_NOT_RUN')")
    assert r.get("exit_code") == 126


def test_bash_runs_when_enabled(monkeypatch):
    monkeypatch.setenv("PRISM_AGENT_SHELL_DISABLED", "false")
    r = _run("bash", "echo hello_from_shell")
    assert "hello_from_shell" in (r.get("output") or "")
