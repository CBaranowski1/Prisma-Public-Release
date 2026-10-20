"""The data-source management privilege exists with correct role defaults."""
from core.auth import DEFAULT_PRIVILEGES, ADMIN_PRIVILEGES, _CODER_PRIVILEGES


def test_default_privilege_present_and_false():
    assert DEFAULT_PRIVILEGES.get("can_manage_data_sources") is False


def test_admin_gets_data_source_privilege():
    assert ADMIN_PRIVILEGES.get("can_manage_data_sources") is True


def test_coder_gets_data_source_privilege():
    assert _CODER_PRIVILEGES.get("can_manage_data_sources") is True
