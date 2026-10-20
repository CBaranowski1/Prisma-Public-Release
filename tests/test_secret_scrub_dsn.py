"""Contract for scrub_dsn: redact DSN passwords, preserve user/host/db/port."""
from src.secret_scrub import scrub_dsn


def test_redact_preserves_user_host_port_db():
    result = scrub_dsn("connection to postgresql://u:secretpw@h:5432/d failed")
    assert "secretpw" not in result
    assert "://u:***@h:5432/d" in result
    assert "u" in result
    assert "h" in result
    assert "5432" in result
    assert "d" in result


def test_redact_quote_plus_encoded_password():
    result = scrub_dsn("mysql+pymysql://user:p%40ss%3Aword@host/db")
    assert "p%40ss%3Aword" not in result
    assert "user" in result
    assert "host" in result
    assert "db" in result


def test_no_op_on_plain_text():
    text = "plain error text, no creds"
    assert scrub_dsn(text) == text


def test_empty_string_returns_empty():
    assert scrub_dsn("") == ""


def test_passwordless_auth_unchanged():
    text = "postgresql://u@h/d"
    assert scrub_dsn(text) == text


def test_multi_occurrence_both_redacted():
    text = "first postgresql://a:pw1@x/db1 then mysql://b:pw2@y/db2"
    result = scrub_dsn(text)
    assert "pw1" not in result
    assert "pw2" not in result
    assert "://a:***@x/db1" in result
    assert "://b:***@y/db2" in result
