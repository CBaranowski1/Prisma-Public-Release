import os

from src.server_bootstrap import _DATA_SUBDIRS, ensure_data_dirs


def test_ensure_data_dirs_creates_expected_tree(tmp_path):
    data_dir = str(tmp_path / "Prism")
    ensure_data_dirs(data_dir)
    assert os.path.isdir(data_dir)
    for sub in _DATA_SUBDIRS:
        assert os.path.isdir(os.path.join(data_dir, sub)), f"missing {sub!r}"


def test_ensure_data_dirs_is_idempotent(tmp_path):
    data_dir = str(tmp_path / "Prism")
    ensure_data_dirs(data_dir)
    ensure_data_dirs(data_dir)  # second call must not raise
    assert os.path.isdir(data_dir)
