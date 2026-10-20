"""SSRF guard: block cloud-metadata / link-local, allow legit private hosts."""
import socket

import pytest

from src.ssrf_guard import assert_url_allowed, assert_host_allowed, SsrfBlocked


def test_blocks_cloud_metadata_ip():
    with pytest.raises(SsrfBlocked):
        assert_url_allowed("http://169.254.169.254/latest/meta-data/")


def test_blocks_link_local_host_only():
    with pytest.raises(SsrfBlocked):
        assert_host_allowed("169.254.169.254")


def test_allows_loopback():
    # Self-hosted internal API on localhost is legitimate.
    assert_url_allowed("http://127.0.0.1:8000/api")


def test_allows_private_lan():
    assert_url_allowed("http://10.1.2.3:5432/")
    assert_url_allowed("http://192.168.1.50/data")


def test_allows_public_host(monkeypatch):
    # api.example.com does not have a real DNS record (example.com is an IANA
    # reserved documentation domain with no wildcard) -- mock resolution to a
    # public IP so this test is hermetic and doesn't depend on live DNS/network.
    def _fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    assert_url_allowed("https://api.example.com/v1/users")


def test_blocks_ipv6_link_local():
    with pytest.raises(SsrfBlocked):
        assert_url_allowed("http://[fe80::1]:80/")


def test_rest_connector_blocks_metadata_base_url():
    # End-to-end: a REST source pointed at the cloud-metadata IP is refused as a
    # ConnectorError (→ 400 at the route), not actually fetched. As of 13-04's
    # D-03 IP-pinning wiring, RestConnector.__init__ itself now resolves +
    # validates the base_url host (to pin it) -- so a blocked metadata host is
    # refused at CONSTRUCTION time, earlier than the previous test_connection()/
    # fetch_table()-only check this test originally asserted.
    from src.datasource_rest import RestConnector
    from src.datasource_connectors import ConnectorError

    class _DS:
        options = '{"base_url": "http://169.254.169.254", "endpoints": {"creds": {"path": "/latest/meta-data/"}}}'
        password = None
        name = "evil"

    with pytest.raises(ConnectorError):
        RestConnector(_DS())


# --- Task 1: fail-closed on unresolvable host (D-02) ---

def test_gaierror_unresolvable_host_fails_closed(monkeypatch):
    def _raise(*a, **k):
        raise socket.gaierror("mock: name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    with pytest.raises(SsrfBlocked):
        assert_host_allowed("nonexistent.invalid")


def test_allowed_hostname_resolving_to_private_ip_no_regression(monkeypatch):
    def _resolve(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    assert assert_host_allowed("db.internal") is None


def test_hostname_resolving_to_metadata_ip_is_blocked(monkeypatch):
    # Proves the hostname-resolution path actually fires and blocks a DNS
    # answer that lands on the cloud-metadata IP (not just bare IP literals).
    def _resolve(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    with pytest.raises(SsrfBlocked):
        assert_host_allowed("evil-dns.example")


def test_bare_blocked_ip_literal_still_blocked():
    with pytest.raises(SsrfBlocked):
        assert_host_allowed("169.254.169.254")


# --- Task 2: resolve_and_pin_host resolves once, returns validated IP (D-03) ---

def test_resolve_and_pin_host_returns_validated_ip(monkeypatch):
    from src.ssrf_guard import resolve_and_pin_host

    def _resolve(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    assert resolve_and_pin_host("some-private.example") == "10.0.0.5"


def test_resolve_and_pin_host_blocks_metadata_resolution(monkeypatch):
    from src.ssrf_guard import resolve_and_pin_host

    def _resolve(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    with pytest.raises(SsrfBlocked):
        resolve_and_pin_host("evil-dns.example")


def test_resolve_and_pin_host_fails_closed_on_gaierror(monkeypatch):
    from src.ssrf_guard import resolve_and_pin_host

    def _raise(*a, **k):
        raise socket.gaierror("mock")
    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    with pytest.raises(SsrfBlocked):
        resolve_and_pin_host("nonexistent.invalid")


def test_resolve_and_pin_host_returns_bare_ip_literal_unchanged():
    from src.ssrf_guard import resolve_and_pin_host

    assert resolve_and_pin_host("10.1.2.3") == "10.1.2.3"


def test_assert_and_pin_agree_on_blocked_host():
    from src.ssrf_guard import resolve_and_pin_host

    with pytest.raises(SsrfBlocked):
        assert_host_allowed("169.254.169.254")
    with pytest.raises(SsrfBlocked):
        resolve_and_pin_host("169.254.169.254")


# --- Task 3: optional DATASOURCE_ALLOWED_HOSTS env allowlist (D-04, off by default) ---

def _mock_dns_to(ip):
    def _resolve(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return _resolve


def test_allowlist_unset_is_inert(monkeypatch):
    monkeypatch.delenv("DATASOURCE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _mock_dns_to("10.0.0.9"))
    assert_host_allowed("db.internal")  # must not raise -- allowlist inert


def test_allowlist_set_denies_unlisted_host(monkeypatch):
    monkeypatch.setenv("DATASOURCE_ALLOWED_HOSTS", "db.internal,db2.internal")
    monkeypatch.setattr(socket, "getaddrinfo", _mock_dns_to("10.0.0.9"))
    assert_host_allowed("db.internal")  # listed -> allowed
    with pytest.raises(SsrfBlocked):
        assert_host_allowed("evil.example")  # unlisted -> denied (deny-by-default)


def test_allowlist_case_insensitive_and_trimmed(monkeypatch):
    monkeypatch.setenv("DATASOURCE_ALLOWED_HOSTS", " DB.Internal , db2.internal ")
    monkeypatch.setattr(socket, "getaddrinfo", _mock_dns_to("10.0.0.9"))
    assert_host_allowed("db.internal")  # case-insensitive + trimmed match


def test_allowlist_applies_to_resolve_and_pin_host(monkeypatch):
    from src.ssrf_guard import resolve_and_pin_host

    monkeypatch.setenv("DATASOURCE_ALLOWED_HOSTS", "db.internal")
    monkeypatch.setattr(socket, "getaddrinfo", _mock_dns_to("10.0.0.9"))
    assert resolve_and_pin_host("db.internal") == "10.0.0.9"
    with pytest.raises(SsrfBlocked):
        resolve_and_pin_host("evil.example")
