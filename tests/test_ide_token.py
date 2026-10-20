import importlib


def _reload_constants(monkeypatch, token_value):
    """Reload core.constants with a patched env var.

    Uses importlib.reload() to mutate the existing module object in place,
    so sys.modules["core.constants"] and core.__dict__["constants"] can
    never diverge (no new module object is ever created). Every attribute
    the reload could have changed is registered with monkeypatch.setattr so
    pytest's own teardown restores the pre-test values.
    """
    monkeypatch.setenv("PRISM_TOKEN", token_value) if token_value else monkeypatch.delenv("PRISM_TOKEN", raising=False)
    import core.constants

    before = dict(vars(core.constants))
    importlib.reload(core.constants)
    after = dict(vars(core.constants))
    changed = {k: v for k, v in before.items() if after.get(k) != v}
    for k, v in changed.items():
        monkeypatch.setattr(core.constants, k, v, raising=False)
    importlib.reload(core.constants)
    return core.constants


def test_prism_token_read_from_env(monkeypatch):
    mod = _reload_constants(monkeypatch, "my-secret")
    assert mod.PRISM_TOKEN == "my-secret"


def test_prism_token_none_when_unset(monkeypatch):
    mod = _reload_constants(monkeypatch, None)
    assert mod.PRISM_TOKEN is None
