"""DSN credential scrubbing for connector error text / logs.

Deliberately dependency-light (stdlib ``re`` only) so it can be imported and
unit-tested without dragging in the SQLAlchemy/httpx import chain. Applied at
the three D-08 sinks in ``datasource_connectors.py``/``datasource_sync.py``
before ``str(e)`` (or a hand-built DSN string) reaches a log line or an
``HTTPException`` body.
"""
import re

_DSN_CREDS = re.compile(r"(://[^:/@\s]+):([^@/\s]+)@")


def scrub_dsn(text: str) -> str:
    """Redact the password in any ``scheme://user:password@host`` substring,
    preserving user/host/db/port for debuggability. Safe to call on arbitrary
    text (DBAPI exception messages, hand-built DSN strings) — a no-op if no
    ``user:password@`` pattern is present.
    """
    if not text:
        return text
    return _DSN_CREDS.sub(r"\1:***@", text)
