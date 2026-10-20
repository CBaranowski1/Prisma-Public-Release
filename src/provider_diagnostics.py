"""Unified provider diagnostics (GRAY-INT-01) + Ollama/vLLM preflight (GRAY-INT-10).

Single source of truth for "is each provider configured and reachable" —
one metadata table (PROVIDER_INFO) driving a snapshot builder, a
module-level cache the diagnostics route reads, and a non-fatal LLM-host
preflight that reuses the existing live-chat circuit breaker.

`credential_source` uses FOUR values: "env" | "settings" | "db" | "none".
This is an intentional, documented deviation from CONTEXT.md's illustrative
three-value enum ("env"|"settings"|"none") — the 5 cloud LLM providers
(anthropic/gemini/groq/xai/openrouter) store credentials exclusively in
encrypted `ModelEndpoint` DB rows (`core.database`), not env vars or
settings.json, so a 4th value ("db") is required to describe them honestly.

This module aggregates and thin-routes existing, already-hardened
primitives (reachability probing, circuit breaking) — it does not invent
new HTTP/probe logic. Cloud-LLM-provider reachability probes are strictly
read-only: they MUST NOT call `_mark_host_dead`, which is reserved for the
Ollama/vLLM preflight (`preflight_llm_host`) so diagnostics refreshes never
pollute the live chat dispatch circuit breaker (see src/llm_core.py).
"""

import asyncio
import httpx
import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import NamedTuple, Optional

from core.constants import DEFAULT_HOST, LLM_HOSTS
from src.llm_core import (
    DEAD_HOST_COOLDOWN,
    _host_key,
    _host_match,
    _is_host_dead,
    _mark_host_dead,
)

logger = logging.getLogger(__name__)


class ProviderInfo(NamedTuple):
    label: str
    kind: str                    # "llm" | "credential"
    needs_key: bool
    host_match: Optional[str]    # domain for src.llm_core._host_match, LLM providers only
    env_var: Optional[str]       # Always None today (WR-03): the 5 LLM providers have no
                                  # single per-provider env var (DB-only credential), and
                                  # credential-kind providers (email/calendar/vault) each
                                  # resolve their own env var(s) in a dedicated seam function
                                  # below (_email_configured/_calendar_configured_count/
                                  # _vault_cli_present) since email needs a 3-var trio, not
                                  # one name -- this field is reserved, not wired to any
                                  # lookup; do not add logic that reads it without also
                                  # updating those seam functions.
    probe: Optional[str]         # "ping" | None (credential-only, no probe target)


# NOTE: gemini/xai host_match domains are defined independently here.
# src.llm_core._detect_provider does NOT recognize gemini or xai hostnames
# today (it falls through to the generic openai-compatible default for
# both) — extending _detect_provider is explicitly out of scope for this
# phase (it drives live chat header-building/dispatch, a hot-path change).
PROVIDER_INFO: dict[str, ProviderInfo] = {
    "anthropic": ProviderInfo("Anthropic", "llm", True, "anthropic.com", None, "ping"),
    "gemini": ProviderInfo("Gemini", "llm", True, "generativelanguage.googleapis.com", None, "ping"),
    "groq": ProviderInfo("Groq", "llm", True, "groq.com", None, "ping"),
    "xai": ProviderInfo("xAI", "llm", True, "x.ai", None, "ping"),
    "openrouter": ProviderInfo("OpenRouter", "llm", True, "openrouter.ai", None, "ping"),
    "email": ProviderInfo("Email (IMAP/SMTP)", "credential", True, None, None, None),
    "calendar": ProviderInfo("Calendar (CalDAV)", "credential", True, None, None, None),
    "vault": ProviderInfo("Vault (Bitwarden CLI)", "credential", False, None, None, None),
}

# Module-level snapshot cache — mirrors src/llm_core.py's _dead_hosts + lock
# idiom. A plain dict is fine (writes are infrequent: one at startup,
# occasional on-demand refreshes) but is lock-guarded since FastAPI's event
# loop can interleave an in-flight refresh with a concurrent GET.
_snapshot: Optional[dict] = None
_snapshot_lock = threading.Lock()


def preflight_llm_host(
    base_url: str,
    timeout: float = 3.0,
    api_key: Optional[str] = None,
) -> dict:
    """Non-fatal listener probe with an actionable message (GRAY-INT-10).

    Reuses the SAME circuit breaker as live chat dispatch
    (src.llm_core._is_host_dead/_mark_host_dead/DEAD_HOST_COOLDOWN) rather
    than a parallel cooldown mechanism — this is the one caller in this
    module explicitly permitted to mark a host dead (cloud-provider probes
    in build_provider_snapshot() must not).
    """
    if _is_host_dead(base_url):
        return {
            "reachable": False,
            "status_code": None,
            "error": (
                f"Cannot reach LLM host at {_host_key(base_url)} "
                f"(cooled for up to {DEAD_HOST_COOLDOWN:.0f}s) — "
                f"is Ollama/vLLM running? Set LLM_HOST / OLLAMA_BASE_URL."
            ),
        }
    from routes.model_routes import _ping_endpoint  # lazy import — avoid routes->src circular import (Pitfall 4)

    # vLLM commonly returns 404 at its OpenAI API root even while the model is
    # healthy. For the managed Spark target, probe the authenticated /models
    # resource instead of treating that expected root response as downtime.
    try:
        from src.dgx_spark import is_dgx_spark_url, load_dgx_spark_config

        dgx = load_dgx_spark_config()
        if dgx is not None and is_dgx_spark_url(base_url, dgx):
            response = httpx.get(
                dgx.models_url,
                headers=dgx.headers,
                timeout=timeout,
            )
            if response.is_success:
                return {
                    "reachable": True,
                    "status_code": response.status_code,
                    "error": None,
                }
            return {
                "reachable": False,
                "status_code": response.status_code,
                "error": (
                    "DGX Spark model API rejected the health probe "
                    f"(HTTP {response.status_code})"
                ),
            }
    except (httpx.HTTPError, ValueError):
        # Fall through to the standard connectivity probe and actionable
        # circuit-breaker message.
        pass

    result = _ping_endpoint(base_url, api_key, timeout=timeout)
    if not result.get("reachable"):
        _mark_host_dead(base_url)
        result["error"] = (
            f"Cannot reach LLM host at {_host_key(base_url)} — "
            f"is Ollama/vLLM running? Set LLM_HOST / OLLAMA_BASE_URL. "
            f"({result.get('error') or 'connection failed'})"
        )
    return result


def _probe_llm_endpoint(base_url: str, timeout: float = 3.0) -> dict:
    """Read-only cloud-LLM reachability probe (T-25-03).

    Deliberately separate from preflight_llm_host: this NEVER touches the
    _is_host_dead/_mark_host_dead circuit breaker, so a diagnostics refresh
    against a misconfigured cloud endpoint can never cool a host that a
    concurrent live chat request is also trying to use. Split out as its
    own seam (beyond the plan's four named seams) so tests can stub cloud
    reachability without a real network call.

    Reachability semantics (CR-02): every cloud LLM provider in
    PROVIDER_INFO has needs_key=True, so an unauthenticated probe against a
    real, fully-reachable endpoint routinely gets a 401/403/404 response --
    that response still proves the network path/TLS/DNS resolved. Treat ANY
    completed HTTP round-trip (whatever the status code) as `reachable:
    True`, distinct from "authenticated successfully" (this probe never
    attempts a live-auth/billable call and sends no API key). Only a
    genuine connection-level failure -- timeout, DNS failure, connection
    refused, surfaced by `_ping_endpoint` as `status_code: None` -- is
    `reachable: False`.
    """
    from routes.model_routes import _ping_endpoint  # lazy import (Pitfall 4)

    result = _ping_endpoint(base_url, timeout=timeout)
    status_code = result.get("status_code")
    if status_code is None:
        # No HTTP response at all -- connection error/timeout/DNS failure.
        return result
    if 200 <= status_code < 300:
        return result
    return {
        "reachable": True,
        "status_code": status_code,
        "error": (
            f"responded with HTTP {status_code} — network path OK, "
            f"credential not verified (no live-auth call made)"
        ),
    }


# ── Seams — DB/CLI/settings access, monkeypatchable without a live DB ──

def _enabled_model_endpoints() -> list:
    """Enabled ModelEndpoint rows across ALL owners (global existence check —
    Open Question 2: no request-scoped owner exists for the startup probe).

    Returns plain SimpleNamespace objects exposing base_url + api_key,
    detached from the SQLAlchemy session before it closes.
    """
    try:
        from core.database import ModelEndpoint, get_db_session

        with get_db_session() as db:
            rows = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()  # noqa: E712
            return [SimpleNamespace(base_url=r.base_url, api_key=r.api_key) for r in rows]
    except Exception as e:
        logger.debug(f"provider_diagnostics: model endpoint lookup failed: {e}")
        return []


def _email_configured() -> tuple:
    """(configured, credential_source) for the email provider.

    Coarse "any user configured" semantic (Pitfall 3 / Open Question 1) —
    the diagnostics endpoint asks "is this feature usable at all", counting
    ALL enabled EmailAccount rows across every owner. Falls back to the
    legacy shared IMAP_HOST/IMAP_USER/IMAP_PASSWORD env trio.
    """
    try:
        from core.database import EmailAccount, get_db_session

        with get_db_session() as db:
            count = db.query(EmailAccount).filter(EmailAccount.enabled == True).count()  # noqa: E712
        if count > 0:
            return True, "db"
    except Exception as e:
        logger.debug(f"provider_diagnostics: email account lookup failed: {e}")
    import os

    if os.environ.get("IMAP_HOST") and os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASSWORD"):
        return True, "env"
    return False, "none"


def _calendar_configured_count() -> int:
    """Count users with a fully-populated CalDAV prefs block (url+username+
    password). Coarse per-instance signal, mirroring the email check's
    "any user configured" semantic (Pitfall 3 / Open Question 1)."""
    try:
        from routes.prefs_routes import _load

        all_prefs = _load()
        users = all_prefs.get("_users") if isinstance(all_prefs, dict) else None
        blocks = list(users.values()) if isinstance(users, dict) else [all_prefs]
        count = 0
        for prefs in blocks:
            cfg = (prefs or {}).get("caldav") or {}
            if cfg.get("url") and cfg.get("username") and cfg.get("password"):
                count += 1
        return count
    except Exception as e:
        logger.debug(f"provider_diagnostics: calendar prefs lookup failed: {e}")
        return 0


def _vault_cli_present() -> bool:
    """True iff the bw CLI binary actually resolves on disk/PATH.

    routes.vault_routes._find_bw() falls back to the bare string "bw" when
    nothing is found — that only fails at actual subprocess-invocation time
    (FileNotFoundError), so a bare resolve is not itself proof of presence;
    verify the path exists or is on PATH before reporting configured=True.
    """
    try:
        import os
        import shutil

        from routes.vault_routes import _find_bw

        path = _find_bw()
        return bool(path and (os.path.isfile(path) or shutil.which(path)))
    except Exception as e:
        logger.debug(f"provider_diagnostics: vault CLI lookup failed: {e}")
        return False


# ── Snapshot builder ──

def _snapshot_for_llm_provider(pid: str, info: ProviderInfo, endpoints: list) -> dict:
    match = next(
        (
            ep for ep in endpoints
            if _host_match(getattr(ep, "base_url", "") or "", info.host_match)
            and (getattr(ep, "api_key", "") or "")
        ),
        None,
    )
    configured = match is not None
    reachable = None
    detail = (
        f"{info.label} endpoint configured." if configured
        else f"No enabled {info.label} endpoint with a credential found."
    )
    if configured:
        try:
            ping = _probe_llm_endpoint(match.base_url)
            reachable = bool(ping.get("reachable"))
            if reachable and ping.get("error"):
                # Completed HTTP round-trip with a non-2xx status (e.g. an
                # auth-challenge 401/403) -- network path confirmed, but
                # surface the distinguishing detail rather than the generic
                # "configured" message (CR-02).
                detail = f"{info.label} endpoint configured; {ping['error']}"
            elif not reachable:
                detail = (
                    f"{info.label} endpoint configured but unreachable: "
                    f"{ping.get('error') or 'connection failed'}"
                )
        except Exception as e:
            reachable = False
            detail = f"{info.label} reachability probe failed: {e}"
    return {
        "provider": pid,
        "configured": configured,
        "reachable": reachable,
        "credential_source": "db" if configured else "none",
        "detail": detail,
    }


def _snapshot_for_email() -> dict:
    configured, source = _email_configured()
    return {
        "provider": "email",
        "configured": configured,
        "reachable": None,
        "credential_source": source,
        "detail": (
            "Email account configured." if configured
            else "No email account configured (DB or IMAP_HOST/IMAP_USER/IMAP_PASSWORD env)."
        ),
    }


def _snapshot_for_calendar() -> dict:
    count = _calendar_configured_count()
    configured = count > 0
    return {
        "provider": "calendar",
        "configured": configured,
        "reachable": None,
        "credential_source": "settings" if configured else "none",
        "detail": (
            f"{count} user(s) have a CalDAV calendar configured." if configured
            else "No user has a CalDAV calendar configured."
        ),
    }


def _snapshot_for_vault() -> dict:
    present = _vault_cli_present()
    return {
        "provider": "vault",
        "configured": present,
        "reachable": None,
        "credential_source": "none",
        "detail": "bw CLI found." if present else "bw CLI not found on this machine.",
    }


def _snapshot_for_llm_host() -> dict:
    """Synthesized local-LLM (Ollama/vLLM) preflight entry — not one of the
    8 SC1 providers. Scope decision (Pitfall 6): probe OLLAMA_BASE_URL (if
    set) else DEFAULT_HOST, plus every entry in LLM_HOSTS — the same
    non-scan-mode host list model discovery uses, WITHOUT the opt-in
    Tailscale-peer full port scan (SEC-16-gated, out of scope here).

    `configured` (IN-03) reflects whether the deployer actually set
    OLLAMA_BASE_URL / LLM_HOST / LLM_HOSTS, not just whether a probe target
    exists — DEFAULT_HOST itself always resolves to a value (defaulting to
    "localhost" via core.constants) even when nothing was configured, so a
    pure-cloud-provider deployment with no local Ollama/vLLM would otherwise
    always read as an active misconfiguration (`reachable: false`) instead
    of "not applicable to this deployment."
    """
    import os

    ollama_base = os.environ.get("OLLAMA_BASE_URL")
    llm_host_explicit = os.environ.get("LLM_HOST")
    dgx_raw = os.environ.get("DGX_SPARK_BASE_URL")
    dgx_config = None
    dgx_error = None
    if dgx_raw:
        try:
            from src.dgx_spark import load_dgx_spark_config

            dgx_config = load_dgx_spark_config()
        except ValueError as exc:
            dgx_error = str(exc)
    configured = bool(ollama_base or llm_host_explicit or LLM_HOSTS or dgx_raw)

    targets = []
    if dgx_config is not None:
        targets.append((dgx_config.base_url, dgx_config.api_key))
    targets.extend(
        (target, None)
        for target in (
            [ollama_base] if ollama_base else [f"http://{DEFAULT_HOST}:11434"]
        )
    )
    targets.extend((f"http://{h}:11434", None) for h in LLM_HOSTS)

    reachable = False
    detail = f"Invalid DGX Spark configuration: {dgx_error}" if dgx_error else None
    for target, api_key in targets:
        result = preflight_llm_host(target, api_key=api_key) if api_key else (
            preflight_llm_host(target)
        )
        if result.get("reachable"):
            reachable = True
            detail = f"Reachable at {target}."
            break
        if detail is None:
            detail = result.get("error") or f"Unreachable at {target}."
    if not configured:
        detail = (
            "No local LLM host configured (using default localhost:11434 as "
            "a courtesy probe). Set OLLAMA_BASE_URL / LLM_HOST / LLM_HOSTS to "
            "configure one."
        )
    return {
        "provider": "llm_host",
        "configured": configured,
        "reachable": reachable,
        "credential_source": (
            "env" if dgx_config is not None and dgx_config.api_key else "none"
        ),
        "detail": detail or "No LLM host targets configured.",
    }


def build_provider_snapshot() -> dict:
    """Build one snapshot object per provider (GRAY-INT-01), plus a
    synthesized "llm_host" entry (GRAY-INT-10). Never includes a raw
    api_key/password value — only booleans, a source tag, and a
    human-readable detail string (T-25-01)."""
    providers: dict = {}
    endpoints: Optional[list] = None

    for pid, info in PROVIDER_INFO.items():
        if info.kind == "llm":
            if endpoints is None:
                endpoints = _enabled_model_endpoints()
            providers[pid] = _snapshot_for_llm_provider(pid, info, endpoints)
        elif pid == "email":
            providers[pid] = _snapshot_for_email()
        elif pid == "calendar":
            providers[pid] = _snapshot_for_calendar()
        elif pid == "vault":
            providers[pid] = _snapshot_for_vault()

    providers["llm_host"] = _snapshot_for_llm_host()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }


async def run_probe() -> dict:
    """Fire-and-forget-safe probe runner. Never raises into its caller
    (matches app.py's _warmup_endpoints try/except discipline) and never
    blocks the event loop (offloads the sync snapshot build to a thread)."""
    global _snapshot
    try:
        snap = await asyncio.to_thread(build_provider_snapshot)
    except Exception as e:
        logger.debug(f"provider_diagnostics: run_probe failed: {e}")
        with _snapshot_lock:
            return _snapshot or {}
    with _snapshot_lock:
        _snapshot = snap
    return snap


def get_cached_snapshot() -> Optional[dict]:
    """Return the last cached snapshot, or None before the first probe."""
    with _snapshot_lock:
        return _snapshot
