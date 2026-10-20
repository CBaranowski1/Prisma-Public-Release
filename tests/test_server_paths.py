import os
import sys

import core.constants as constants


def test_data_dir_env_override_wins(monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", os.path.join("X:", "custom"))
    assert constants._default_data_dir() == os.path.join("X:", "custom")


def test_data_dir_dev_default_is_repo_data(monkeypatch):
    monkeypatch.delenv("PRISM_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    expected = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(constants.__file__))), "data"
    )
    assert constants._default_data_dir() == expected


def test_data_dir_frozen_windows_uses_appdata(monkeypatch):
    monkeypatch.delenv("PRISM_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(constants, "_is_windows_platform", lambda: True)
    monkeypatch.setenv("APPDATA", os.path.join("C:", "Users", "x", "AppData", "Roaming"))
    assert constants._default_data_dir() == os.path.join(
        "C:", "Users", "x", "AppData", "Roaming", "Prism"
    )


def test_asset_base_dir_frozen_uses_meipass(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", os.path.join("C:", "payload"), raising=False)
    base = constants._asset_base_dir()
    assert base.startswith(os.path.join("C:", "payload"))


def test_asset_base_dir_dev_is_repo_root(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    expected = os.path.dirname(os.path.dirname(os.path.abspath(constants.__file__))) + "/"
    assert constants._asset_base_dir() == expected


def test_default_database_url_uses_forward_slashes():
    # The SQLite URL must use forward slashes (a backslash Windows path like
    # sqlite:///C:\... is mishandled) and be absolute / cwd-independent.
    import core.database as database
    url = database._default_database_url(r"C:\data\Prism")
    assert url == "sqlite:///C:/data/Prism/app.db"
