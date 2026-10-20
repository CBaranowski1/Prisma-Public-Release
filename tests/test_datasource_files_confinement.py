"""Security: CSV/Excel FileConnector must confine file_path to the upload dir
(DATA_DIR/qb_sources/...). A data source pointing at an arbitrary server file
(e.g. data/.app_key, /etc/passwd) must be rejected — otherwise a coder with
can_manage_data_sources could read any server file as a queryable table."""
import json
import os

import pytest

import src.datasource_files as files
import core.constants as constants
from core.querybuilder_models import DataSource


def _ds(path, kind="csv"):
    return DataSource(id="s1", name="f", source_type=kind,
                      options=json.dumps({"file_path": str(path), "kind": kind}))


def test_arbitrary_path_outside_upload_dir_is_rejected(tmp_path, monkeypatch):
    # Point the app data dir at a temp dir; an upload dir exists under it.
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(files, "DATA_DIR", str(tmp_path), raising=False)
    # A "secret" file outside qb_sources/ that a coder must NOT be able to read.
    secret = tmp_path / ".app_key"
    secret.write_text("super-secret-fernet-key", encoding="utf-8")
    with pytest.raises(files.ConnectorError):
        files.FileConnector(_ds(secret))


def test_path_inside_upload_dir_is_allowed(tmp_path, monkeypatch):
    pytest.importorskip("pandas")
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(files, "DATA_DIR", str(tmp_path), raising=False)
    up = tmp_path / "qb_sources" / "s1"
    up.mkdir(parents=True)
    f = up / "data.csv"
    f.write_text("id,name\n1,a\n", encoding="utf-8")
    conn = files.FileConnector(_ds(f))  # must NOT raise
    assert conn.list_tables() == ["data"]


def test_traversal_out_of_upload_dir_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(files, "DATA_DIR", str(tmp_path), raising=False)
    (tmp_path / "qb_sources").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    sneaky = os.path.join(str(tmp_path), "qb_sources", "..", "secret.txt")
    with pytest.raises(files.ConnectorError):
        files.FileConnector(_ds(sneaky))
