from routes.chat_helpers import request_bool


def test_false_form_values_do_not_enable_features():
    for value in ("false", "0", "off", "no", "", None, False):
        assert request_bool(value) is False


def test_true_form_values_enable_features():
    for value in ("true", "1", "on", "yes", True):
        assert request_bool(value) is True


def test_default_is_honored_for_missing_value():
    assert request_bool(None, default=True) is True
