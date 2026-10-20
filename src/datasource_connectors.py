"""Pluggable external-data-source connectors for the query builder.

Each connector wraps a SQLAlchemy Engine for one external database and exposes
the operations the sync engine needs: test, list tables, reflect a table. The
DB drivers (psycopg / PyMySQL) are optional and lazy-imported via SQLAlchemy's
dialect loading, so importing this module never requires them; a missing driver
surfaces as MissingDriverError with an install hint at connect time.
"""
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List

from sqlalchemy import create_engine, event, inspect, MetaData, Table, select, text
from sqlalchemy.exc import NoSuchModuleError, OperationalError, SQLAlchemyError
from sqlalchemy.types import TypeEngine

from src.secret_scrub import scrub_dsn

logger = logging.getLogger(__name__)


@dataclass
class SyncColumn:
    name: str
    type_: TypeEngine


@dataclass
class SyncTable:
    columns: list  # list[SyncColumn]
    rows: Iterable  # Iterable[dict]
    truncated: bool = False  # QB-17: set True when a row/byte budget cap fired (MD-01 reuse)


class ConnectorError(Exception):
    """A data-source connector could not perform an operation."""


class MissingDriverError(ConnectorError):
    """The DB driver for this source type is not installed."""


_INSTALL_HINT = {
    "postgresql+psycopg": "pip install 'psycopg[binary]'",
    "mysql+pymysql": "pip install PyMySQL",
}

# Vetted per-dialect connect_args TLS allowlists (D-06). Off-allowlist keys —
# including any host/hostaddr a user tried to inject — are silently dropped;
# see _vetted_connect_args and get_connector's Pitfall-5 comment.
_PG_TLS_KEYS = {"sslmode", "sslrootcert", "sslcert", "sslkey"}
# NOTE: `ssl_mode` is deliberately ABSENT — PyMySQL 1.2.0 has no such keyword
# and raises TypeError if forwarded (RESEARCH Pitfall 1). Use `ssl`/`ssl_verify_*`
# to escalate, mirroring Postgres's sslmode=verify-full escalation path.
_MYSQL_TLS_KEYS = {
    "ssl", "ssl_ca", "ssl_cert", "ssl_key", "ssl_key_password",
    "ssl_verify_cert", "ssl_verify_identity", "ssl_disabled",
}
_TLS_ALLOWLISTS = {"postgres": _PG_TLS_KEYS, "mysql": _MYSQL_TLS_KEYS}

# QB-08 (D-03): generous engine-timeout defaults, safe vs a legitimate
# MAX_SYNC_ROWS-sized sync. Overridable ONLY via the narrow numeric
# `options.timeouts` key parsed by _timeout_overrides() below — this is
# deliberately a SIBLING allowlist, never a widening of _TLS_ALLOWLISTS
# (see _timeout_overrides docstring / RESEARCH Pitfall 1).
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_POOL_TIMEOUT_S = 30

# QB-17: byte half of the sync streaming budget (row half is MAX_SYNC_ROWS in
# datasource_sync.py); shared here so both File/REST connectors (Plans 04/05)
# and the sync orchestrator import a single source of truth.
MAX_SYNC_BYTES = 50 * 1024 * 1024  # ~50MB

# (key in options.timeouts, default, ceiling) — numeric-only, clamped.
_TIMEOUT_SPEC = (
    ("statement_timeout_ms", DEFAULT_STATEMENT_TIMEOUT_MS, 600_000),
    ("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S, 120),
    ("pool_timeout_s", DEFAULT_POOL_TIMEOUT_S, 300),
)


def _timeout_overrides(options) -> dict:
    """Parse a DataSource's `options` JSON (tolerating None/invalid input) and
    return the three timeout values, each read from the narrow
    `options.timeouts.<key>` numeric key and clamped to a sane ceiling; any
    absent/non-int/zero/negative/over-ceiling value falls back to its module
    default. This is a NEW, narrow sibling to `_vetted_connect_args` —
    deliberately NOT a widening of `_TLS_ALLOWLISTS` (Pitfall 1): it never
    accepts a raw string, only individually-validated integers."""
    try:
        parsed = json.loads(options) if options else {}
    except (TypeError, ValueError):
        parsed = {}
    t = parsed.get("timeouts") if isinstance(parsed, dict) else None
    if not isinstance(t, dict):
        t = {}
    out = {}
    for key, default, ceiling in _TIMEOUT_SPEC:
        v = t.get(key, default)
        out[key] = v if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= ceiling else default
    return out


def _vetted_connect_args(source_type: str, options) -> dict:
    """Parse a DataSource's `options` JSON (tolerating None/invalid input) and
    return only the `connect_args` keys on the dialect's TLS allowlist,
    silently dropping everything else — including any `host`/`hostaddr` a user
    tried to inject (those two keys are guard-computed only, never user input;
    see get_connector's Pitfall-5 comment)."""
    allowlist = _TLS_ALLOWLISTS.get(source_type)
    if not allowlist:
        return {}
    try:
        parsed = json.loads(options) if options else {}
    except (TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        return {}
    raw = parsed.get("connect_args") or {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in allowlist}


class DataSourceConnector(ABC):
    """Base connector. Subclasses (or the SQLite test shim) supply make_engine()."""

    @abstractmethod
    def make_engine(self):
        ...

    def test_connection(self) -> None:
        try:
            engine = self.make_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except NoSuchModuleError as e:
            raise MissingDriverError(str(e)) from e
        except (OperationalError, SQLAlchemyError) as e:
            raise ConnectorError(scrub_dsn(str(e))) from e

    def list_tables(self) -> List[str]:
        try:
            return list(inspect(self.make_engine()).get_table_names())
        except NoSuchModuleError as e:
            raise MissingDriverError(str(e)) from e
        except SQLAlchemyError as e:
            raise ConnectorError(scrub_dsn(str(e))) from e

    def reflect_table(self, name: str) -> Table:
        try:
            return Table(name, MetaData(), autoload_with=self.make_engine())
        except NoSuchModuleError as e:
            raise MissingDriverError(str(e)) from e
        except SQLAlchemyError as e:
            raise ConnectorError(scrub_dsn(f"Could not reflect table {name!r}: {e}")) from e

    def fetch_table(self, name: str) -> "SyncTable":
        """Default SQL implementation: reflect the table, then stream its rows.
        Non-SQL connectors (CSV/Excel/REST) override this and need not implement
        make_engine/reflect_table."""
        table = self.reflect_table(name)
        columns = [SyncColumn(c.name, c.type) for c in table.columns]

        def _row_iter():
            engine = self.make_engine()
            with engine.connect() as conn:
                result = conn.execution_options(stream_results=True).execute(select(table))
                for r in result.mappings():
                    yield dict(r)

        return SyncTable(columns=columns, rows=_row_iter())


class _UrlConnector(DataSourceConnector):
    def __init__(self, url: str, connect_args: dict | None = None,
                 pool_timeout: int = DEFAULT_POOL_TIMEOUT_S,
                 statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS):
        self._url = url
        # Computed ONCE by get_connector (pinned IP + vetted TLS subset) and
        # reused verbatim on every make_engine() call — never re-resolved
        # (Pinned-IP lifetime pitfall, RESEARCH Pattern 1).
        self._connect_args = connect_args or {}
        # QB-08 (D-01/D-02): pool-checkout bound threaded to create_engine();
        # statement_timeout_ms is also needed here (not just connect_args) for
        # the optional MySQL post-connect kill-switch listener below.
        self._pool_timeout = pool_timeout
        self._statement_timeout_ms = statement_timeout_ms

    def make_engine(self):
        try:
            engine = create_engine(
                self._url, connect_args=self._connect_args,
                pool_timeout=self._pool_timeout, pool_pre_ping=True,
            )
        except NoSuchModuleError as e:
            hint = ""
            for prefix, h in _INSTALL_HINT.items():
                if self._url.startswith(prefix):
                    hint = f" — install the driver: {h}"
                    break
            raise MissingDriverError(f"DB driver not installed{hint}") from e
        if self._url.startswith("mysql"):
            self._register_mysql_stmt_kill(engine)
        return engine

    def _register_mysql_stmt_kill(self, engine) -> None:
        """QB-08 (D-02) optional defense-in-depth: kill a runaway MySQL SELECT
        server-side via SET SESSION max_execution_time. Registered as a guarded
        post-connect event listener (NEVER PyMySQL's `init_command` — that
        fails the whole handshake non-recoverably on MariaDB, which has no
        such variable; RESEARCH Pitfall 2). The try/except wraps BOTH the
        registration itself (a non-Engine target, e.g. a test spy MagicMock,
        raises at registration time) and the per-connection SET SESSION call
        (fails harmlessly on MariaDB / older MySQL)."""
        stmt_ms = self._statement_timeout_ms
        try:
            @event.listens_for(engine, "connect")
            def _set_mysql_stmt_kill(dbapi_conn, _record, _ms=stmt_ms):
                try:
                    cur = dbapi_conn.cursor()
                    cur.execute(f"SET SESSION max_execution_time={_ms}")
                    cur.close()
                except Exception:
                    logger.debug(
                        "server-side max_execution_time unsupported (MariaDB "
                        "or old MySQL); relying on read_timeout only", exc_info=True)
        except Exception:
            logger.debug("could not register mysql statement-kill listener", exc_info=True)


_SUPPORTED = {"postgres", "mysql", "csv", "excel", "rest"}


def get_connector(ds) -> DataSourceConnector:
    """Build a connector for a DataSource row. Raises ConnectorError for an
    unsupported source_type."""
    if ds.source_type in ("postgres", "mysql"):
        # Block cloud-metadata / link-local hosts (defense-in-depth; loopback +
        # private LAN remain allowed for legitimate self-hosted databases).
        # Resolved ONCE here and pinned for the connector's whole lifetime —
        # closes the DNS-rebind TOCTOU window between check-time and every
        # subsequent make_engine() call (test/list/reflect/fetch all reuse it;
        # RESEARCH Pattern 1's "Pinned-IP lifetime pitfall").
        from src.ssrf_guard import resolve_and_pin_host, SsrfBlocked
        try:
            pinned_ip = resolve_and_pin_host(ds.host or "")
        except SsrfBlocked as e:
            raise ConnectorError(f"This database host is not allowed: {e}")
        vetted = _vetted_connect_args(ds.source_type, ds.options)
        # QB-08 (D-01/D-02/D-03): narrow numeric-only timeout override channel,
        # a SIBLING to _vetted_connect_args — never a raw options passthrough
        # (Pitfall 1). Applied unconditionally (generous defaults if absent).
        timeouts = _timeout_overrides(ds.options)
        if ds.source_type == "postgres":
            # Guard-computed hostaddr applied LAST so a user-supplied
            # connect_args key can never shadow it (Pitfall 5). `host` (from
            # the URL) is left unchanged so TLS SNI / cert hostname
            # verification still target the real hostname. The Postgres
            # statement-timeout `options` string is built INTERNALLY from the
            # validated integer above — never from user JSON (Pitfall 1).
            connect_args = {
                **vetted,
                "connect_timeout": timeouts["connect_timeout_s"],
                "options": f"-c statement_timeout={timeouts['statement_timeout_ms']}",
                "hostaddr": pinned_ip,
            }
        else:
            # MySQL: accepted residual, NOT IP-pinned (RESEARCH Pattern 3 /
            # Pitfall 3). PyMySQL has no host/hostaddr split — it hard-codes
            # `server_hostname=self.host` for TLS identity verification, so
            # pinning `host` to an IP would silently break
            # ssl_verify_identity. The pre-connect resolve_and_pin_host above
            # still blocks direct-IP/static-hostname metadata access.
            # read_timeout bounds each individual socket read (ms -> s).
            connect_args = {
                **vetted,
                "connect_timeout": timeouts["connect_timeout_s"],
                "read_timeout": timeouts["statement_timeout_ms"] / 1000,
            }
        return _UrlConnector(
            ds.sqlalchemy_url(), connect_args=connect_args,
            pool_timeout=timeouts["pool_timeout_s"],
            statement_timeout_ms=timeouts["statement_timeout_ms"],
        )
    if ds.source_type in ("csv", "excel"):
        from src.datasource_files import FileConnector
        return FileConnector(ds)
    if ds.source_type == "rest":
        from src.datasource_rest import RestConnector
        return RestConnector(ds)
    raise ConnectorError(f"Unsupported source_type: {ds.source_type!r}")
