"""Tests for src/brain_learn.py::learning_target's explicit-setting override
(GRAY-BRN-09) and the WARNING-vs-debug distinction in learn_from_exchange
when a configured target is broken vs. when no target is configured at all.

Offline: src.brains.brains_root / discover_brains / resolve_brain and
src.settings.get_setting are monkeypatched -- no real filesystem brain
discovery, no LLM, no fastembed. No module-level pytest.importorskip.
"""
import asyncio
import logging

import src.brain_learn as bl


def _brain(path, bid="t"):
    (path / "knowledge" / "code").mkdir(parents=True, exist_ok=True)
    return {"id": bid, "path": str(path), "dir": str(path)}


# ---- learning_target() ----------------------------------------------------

def test_learning_target_unset_falls_back_to_first_sorted(tmp_path, monkeypatch):
    import src.brains as brains_mod

    root = tmp_path / "root"
    root.mkdir()
    b_alpha = _brain(root / "alpha", "alpha")
    b_beta = _brain(root / "beta", "beta")
    monkeypatch.setattr(brains_mod, "brains_root", lambda: root)
    monkeypatch.setattr(brains_mod, "discover_brains", lambda r=None: [b_alpha, b_beta])

    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: d)  # no explicit target

    result = bl.learning_target()
    assert result is not None
    assert result["id"] == "alpha"


def test_learning_target_explicit_override_wins_over_first_sorted(tmp_path, monkeypatch):
    import src.brains as brains_mod

    root = tmp_path / "root"
    root.mkdir()
    b_alpha = _brain(root / "alpha", "alpha")
    b_beta = _brain(root / "beta", "beta")
    monkeypatch.setattr(brains_mod, "brains_root", lambda: root)
    monkeypatch.setattr(brains_mod, "discover_brains", lambda r=None: [b_alpha, b_beta])
    monkeypatch.setattr(brains_mod, "resolve_brain",
                        lambda bid, r=None: b_beta if bid == "beta" else None)

    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: "beta" if k == "learning_target" else d)

    result = bl.learning_target()
    assert result is not None
    assert result["id"] == "beta"


def test_learning_target_out_of_root_is_broken(tmp_path, monkeypatch):
    import src.brains as brains_mod

    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "external-vault"
    ext_brain = _brain(external, "ext")
    monkeypatch.setattr(brains_mod, "brains_root", lambda: root)
    monkeypatch.setattr(brains_mod, "discover_brains", lambda r=None: [])
    monkeypatch.setattr(brains_mod, "resolve_brain",
                        lambda bid, r=None: ext_brain if bid == "ext" else None)

    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: "ext" if k == "learning_target" else d)

    # Broken: outside brains_root -- must NOT be returned (never silently
    # written into an external vault), and must NOT fall back to first-sorted.
    assert bl.learning_target() is None


def test_learning_target_nonexistent_id_is_broken(tmp_path, monkeypatch):
    import src.brains as brains_mod

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(brains_mod, "brains_root", lambda: root)
    monkeypatch.setattr(brains_mod, "discover_brains", lambda r=None: [])
    monkeypatch.setattr(brains_mod, "resolve_brain", lambda bid, r=None: None)

    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: "ghost" if k == "learning_target" else d)

    assert bl.learning_target() is None


# ---- learn_from_exchange(): warning-vs-debug on missing target ------------

def test_learn_from_exchange_warns_on_broken_explicit_target(monkeypatch, caplog):
    monkeypatch.setattr(bl, "is_enabled", lambda: True)
    monkeypatch.setattr(bl, "user_opted_out", lambda owner: False)
    monkeypatch.setattr(bl, "learning_target", lambda: None)

    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: "ghost" if k == "learning_target" else d)

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            bl.learn_from_exchange("q", "a", endpoint_url="http://x", model="m")
        )
    assert result is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING when an explicit learning_target is configured but broken"
    assert any("learning_target" in r.message.lower() for r in warnings)


def test_learn_from_exchange_stays_quiet_when_no_target_configured(monkeypatch, caplog):
    monkeypatch.setattr(bl, "is_enabled", lambda: True)
    monkeypatch.setattr(bl, "user_opted_out", lambda owner: False)
    monkeypatch.setattr(bl, "learning_target", lambda: None)

    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: d)  # nothing configured

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            bl.learn_from_exchange("q", "a", endpoint_url="http://x", model="m")
        )
    assert result is False
    assert not any(r.levelno == logging.WARNING for r in caplog.records)
