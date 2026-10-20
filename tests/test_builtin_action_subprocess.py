"""Security regressions for built-in action subprocess execution."""

from types import SimpleNamespace

import pytest

import src.builtin_actions as actions


@pytest.mark.asyncio
async def test_run_subprocess_never_enables_python_shell(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    output, success = await actions._run_subprocess(
        ["cmd.exe", "/d", "/s", "/c", "echo ok"]
    )

    assert (output, success) == ("ok", True)
    assert calls[0][1]["shell"] is False


def test_windows_shell_argv_is_explicit_when_bash_is_unavailable(monkeypatch):
    monkeypatch.setattr(actions, "IS_WINDOWS", True)
    monkeypatch.setattr(actions, "find_bash", lambda: None)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    assert actions._local_shell_argv("echo hello") == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
        "echo hello",
    ]


def test_posix_shell_argv_is_explicit(monkeypatch):
    monkeypatch.setattr(actions, "IS_WINDOWS", False)

    assert actions._local_shell_argv("printf ok") == ["bash", "-c", "printf ok"]
