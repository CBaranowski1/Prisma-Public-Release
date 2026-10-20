import core.hf_env_guard as hf_env_guard


_ENV_VARS = ("HF_HUB_DISABLE_SYMLINKS", "HF_HUB_DISABLE_SYMLINKS_WARNING")


def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_guard_sets_env_vars_when_network_detected(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(hf_env_guard, "_is_network_path", lambda path: True)

    hf_env_guard.ensure_hf_symlinks_disabled_if_networked("//server/share")

    assert hf_env_guard.os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"
    assert hf_env_guard.os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1"


def test_guard_is_noop_when_not_network_detected(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(hf_env_guard, "_is_network_path", lambda path: False)

    hf_env_guard.ensure_hf_symlinks_disabled_if_networked("/local/data")

    for var in _ENV_VARS:
        assert var not in hf_env_guard.os.environ


def test_guard_does_not_override_existing_value(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS", "0")
    monkeypatch.setattr(hf_env_guard, "_is_network_path", lambda path: True)

    hf_env_guard.ensure_hf_symlinks_disabled_if_networked("//server/share")

    assert hf_env_guard.os.environ["HF_HUB_DISABLE_SYMLINKS"] == "0"
    # The warning var was unset, so setdefault should still set it.
    assert hf_env_guard.os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1"


def test_is_network_path_never_raises_on_garbage_path():
    # A NUL byte is invalid in paths on every OS and would normally raise
    # ValueError from os.path.splitdrive / open() — this proves the fail-safe
    # try/except wraps ALL detection logic, not just the platform branches.
    assert hf_env_guard._is_network_path("bad\x00path") is False


def test_guard_is_idempotent(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(hf_env_guard, "_is_network_path", lambda path: True)

    hf_env_guard.ensure_hf_symlinks_disabled_if_networked("//server/share")
    first = dict(hf_env_guard.os.environ)
    hf_env_guard.ensure_hf_symlinks_disabled_if_networked("//server/share")
    second = dict(hf_env_guard.os.environ)

    assert first == second
    assert hf_env_guard.os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"
