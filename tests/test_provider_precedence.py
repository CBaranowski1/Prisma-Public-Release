"""Pins the GRAY-INT-07 uniform env -> settings -> default precedence resolver.

`resolve_config_value` (src/settings.py) is the single shared helper all
provider config reads should route through. These tests pin its three-tier
precedence order so it cannot silently drift. No disk I/O — an explicit
`settings=` dict is always passed.
"""
from src.settings import resolve_config_value


def test_env_wins_over_settings(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "env-value")
    val, source = resolve_config_value(
        "SOME_VAR", "some_key", default="d",
        settings={"some_key": "settings-value"},
    )
    assert (val, source) == ("env-value", "env")


def test_settings_wins_over_default(monkeypatch):
    monkeypatch.delenv("SOME_VAR", raising=False)
    val, source = resolve_config_value(
        "SOME_VAR", "some_key", default="d",
        settings={"some_key": "settings-value"},
    )
    assert (val, source) == ("settings-value", "settings")


def test_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("SOME_VAR", raising=False)
    val, source = resolve_config_value(
        "SOME_VAR", "some_key", default="d", settings={},
    )
    assert (val, source) == ("d", "default")


def test_empty_env_string_falls_through(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "")
    val, source = resolve_config_value(
        "SOME_VAR", "some_key", default="d",
        settings={"some_key": "settings-value"},
    )
    assert (val, source) == ("settings-value", "settings")


def test_none_env_var_arg_skips_env_tier(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "env-value")
    val, source = resolve_config_value(
        None, "some_key", default="d",
        settings={"some_key": "settings-value"},
    )
    assert (val, source) == ("settings-value", "settings")


def test_whitespace_only_env_string_falls_through(monkeypatch):
    """IN-02: a whitespace-only env var must not count as "set", matching
    the settings tier's own stripping behavior."""
    monkeypatch.setenv("SOME_VAR", "   ")
    val, source = resolve_config_value(
        "SOME_VAR", "some_key", default="d",
        settings={"some_key": "settings-value"},
    )
    assert (val, source) == ("settings-value", "settings")


def test_env_value_is_stripped(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "  env-value  ")
    val, source = resolve_config_value(
        "SOME_VAR", "some_key", default="d",
        settings={"some_key": "settings-value"},
    )
    assert (val, source) == ("env-value", "env")
