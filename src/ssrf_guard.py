"""SSRF guard for user-configured outbound connections (REST + DB data sources).

A data source is configured by a `can_manage_data_sources` user (coder/admin),
who supplies a URL or host the server will then connect to. Without a guard a
coder could point a source at the cloud-metadata endpoint
(http://169.254.169.254/…) and sync IAM credentials into a queryable table, or
probe link-local/reserved addresses.

Policy (deliberately conservative so it does NOT break legitimate self-hosted
use): block link-local (incl. the 169.254.169.254 metadata IP), multicast,
reserved, and unspecified addresses. Loopback and RFC1918 private ranges are
ALLOWED — a self-hosted Prism legitimately syncs from a localhost or
LAN database / internal REST API.

Note: this resolves DNS at check time; a determined DNS-rebinding attacker could
still bypass it. It closes the direct-IP and static-hostname metadata vector,
which is the realistic risk here.

An operator may additionally opt into a strict `DATASOURCE_ALLOWED_HOSTS`
allowlist (comma-separated hostnames, case-insensitive, whitespace-trimmed).
It is OFF by default — unset means today's allow-LAN/block-metadata behavior,
unchanged. When set, it is deny-by-default: only listed hosts are permitted.
"""
import ipaddress
import os
import socket
from urllib.parse import urlsplit


class SsrfBlocked(Exception):
    """Raised when a configured host/URL resolves to a blocked address."""


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (addr.is_link_local or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified)


def _allowlist_hosts() -> set:
    """Lowercased/stripped DATASOURCE_ALLOWED_HOSTS entries, or an empty set
    when the env var is unset/empty (allowlist inert — today's behavior)."""
    raw = os.getenv("DATASOURCE_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _check_allowlist(host: str) -> None:
    """No-op when DATASOURCE_ALLOWED_HOSTS is unset; deny-by-default (raise for
    anything not listed) when it is set."""
    allowlist = _allowlist_hosts()
    if allowlist and host.strip().lower() not in allowlist:
        raise SsrfBlocked(f"host {host!r} is not in DATASOURCE_ALLOWED_HOSTS")


def _resolve_and_validate(host: str) -> str:
    """Validate `host` against SSRF policy and return the validated IP.

    Shared by assert_host_allowed and resolve_and_pin_host so the two can never
    classify a host differently (both route every check through
    _is_blocked_ip and _check_allowlist). A bare IP literal is checked
    directly and returned unchanged if allowed. A hostname is resolved via DNS
    and every returned address is checked; resolution failure fails CLOSED
    (uncertainty is denied, never silently let through).
    """
    if not host:
        raise SsrfBlocked("missing host")
    _check_allowlist(host)
    # A bare IP literal is checked directly (covers http://169.254.169.254/).
    # NOTE: `ipaddress.ip_address()` itself is what raises ValueError for a
    # non-IP hostname — _is_blocked_ip() swallows that internally and returns
    # False, so checking the try/except around _is_blocked_ip() (as before)
    # never falls through to hostname resolution below; it always returned
    # early. Parsing the IP literal directly here is what makes the fallthrough
    # real.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(host):
            raise SsrfBlocked(f"host {host!r} is a blocked address")
        return host  # it parsed as an IP and is allowed
    # Hostname: resolve and check every address it maps to.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable — fail CLOSED: uncertainty is denied, not silently let
        # through. Was: `return` (fail-open).
        raise SsrfBlocked(f"host {host!r} could not be resolved") from None
    validated_ip = None
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            raise SsrfBlocked(f"host {host!r} resolves to a blocked address ({ip})")
        if validated_ip is None:
            validated_ip = ip
    return validated_ip


def assert_host_allowed(host: str) -> None:
    _resolve_and_validate(host)


def resolve_and_pin_host(host: str) -> str:
    """Resolve `host` ONCE, validate it against SSRF policy, and return the
    validated IP string for the caller to pin for the actual connection —
    closes the DNS-rebind TOCTOU window between check-time and connect-time
    (the resolution used for validation is the same one used to connect)."""
    return _resolve_and_validate(host)


def assert_url_allowed(url: str) -> None:
    netloc = urlsplit(url).netloc
    # strip credentials and port: user:pass@host:port
    host = netloc.split("@")[-1]
    if host.startswith("["):  # IPv6 literal [::1]:port
        host = host[1:].split("]")[0]
    else:
        host = host.split(":")[0]
    assert_host_allowed(host)
