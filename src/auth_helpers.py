"""Shared auth helpers used by all route files."""

import logging
import os
from typing import Optional
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)


def get_current_user(request: Request) -> Optional[str]:
    """Get current username from request state (set by auth middleware)."""
    return getattr(request.state, 'current_user', None)


def require_scope(request: Request, *needed: str, require_all: bool = False) -> None:
    """Enforce API-token scope on a route (audit gap 4).

    Bearer tokens authenticate as the reserved "api" sentinel user (see app.py
    AuthMiddleware), so they are already fail-closed on every require_admin /
    require_privilege route. This helper is the shared, audited chokepoint for
    the routes that DO opt into serving tokens (they read api_token_owner): it
    gives them uniform per-capability enforcement instead of ad-hoc
    `set(request.state.api_token_scopes)` checks duplicated per route.

    Semantics:
      * Non-token requests (cookie sessions, the in-process internal-tool
        loopback) are a NO-OP — scopes constrain tokens, not logged-in users.
      * A token request must carry at least one of `needed` (or all of them
        when require_all=True), else 403. This makes a token's scopes a hard
        ceiling: a "chat"-only companion token cannot invoke an email:send or
        memory:write route even though its owner could from a browser.
    """
    if not needed:
        return
    if not getattr(request.state, "api_token", False):
        return
    scopes = set(getattr(request.state, "api_token_scopes", []) or [])
    ok = all(s in scopes for s in needed) if require_all else any(s in scopes for s in needed)
    if not ok:
        joiner = " and " if require_all else " or "
        raise HTTPException(403, f"API token is not scoped for: {joiner.join(needed)}")


def effective_user(request: Request):
    """The real human behind the request, for ownership/attribution.

    Cookie sessions resolve to the logged-in username. Bearer ``ody_`` callers
    come through as the sandboxed pseudo-user "api" so they can't wander into
    cookie/user routes by default, but their token was minted by, and belongs
    to, a real owner stamped on ``request.state.api_token_owner``. Routes that
    should attribute a token's actions to that owner (sessions, chat history)
    call this instead of :func:`get_current_user`, so a paired client sees and
    creates the SAME data as the owner's desktop UI rather than a separate
    "api"-owned silo.

    For cookie sessions this is identical to :func:`get_current_user`, so
    swapping a route over is a no-op for browser users. A bearer token with no
    owner falls back to :func:`get_current_user` (the "api" pseudo-user), so it
    never escalates.
    """
    if getattr(request.state, "api_token", False):
        owner = getattr(request.state, "api_token_owner", None)
        if owner:
            return owner
    return get_current_user(request)


def _auth_disabled() -> bool:
    """Return the permanent security mode for the desktop application.

    Prisma Desktop deliberately has no login surface.  The process is secured
    by a loopback-only bind and the desktop SSH tunnel, not by mutable request-
    time environment state.  Returning a constant also prevents an old
    ``AUTH_ENABLED=true`` value (or a test changing the environment after app
    import) from partially restoring the retired login gate.
    """
    return True


def require_user(request: Request) -> str:
    """FastAPI dependency: reject unauthenticated callers when the upstream
    auth middleware was bypassed unexpectedly (e.g. SSRF from a sibling
    service). Returns the resolved username, or "" in single-user / anonymous
    modes where no username is available.

    The three "" cases are:
      1. AUTH_ENABLED=false — the operator explicitly turned auth off.
         The full /login flow is skipped (issue #622), so route-level
         require_user must let the request through too instead of 401-ing
         and forcing the browser to /login.
      2. Unconfigured first-run + loopback caller — pre-setup access from
         localhost so the operator can hit the SPA before creating the
         first admin.
      3. LOCALHOST_BYPASS=true + loopback caller — documented dev bypass.

    Use this on routes that touch user data so middleware misconfig can't
    open them up.
    """
    u = get_current_user(request)
    if u:
        return u
    # Operator-disabled auth: honor it at the route layer too. Without this,
    # routes that depend on require_user 401, the front-end fetch wrapper
    # redirects to /login, and the user sees a login page despite
    # AUTH_ENABLED=false (issue #622). Docker / reverse-proxy deployments
    # hit this because requests arrive from a non-loopback client.host, so
    # the loopback fall-through below never fires.
    if _auth_disabled():
        return ""
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    is_loopback = host in ("127.0.0.1", "::1", "localhost")
    # LOCALHOST_BYPASS=true is the dev-only "I'm on loopback, skip auth"
    # switch. Mirror the middleware so routes don't 401 the same caller
    # the middleware just let through.
    if is_loopback and os.getenv("LOCALHOST_BYPASS", "false").lower() == "true":
        return ""
    if auth_mgr is not None and getattr(auth_mgr, "is_configured", False):
        raise HTTPException(401, "Not authenticated")
    # Unconfigured / first-run mode: only allow loopback callers.
    if is_loopback:
        return ""
    raise HTTPException(401, "Not authenticated")


def require_privilege(request: Request, key: str, *, fail_closed: bool = False) -> str:
    """Reject callers whose `auth.json` privilege flag for `key` is False.
    Returns the username so the route handler can keep using it.

    Admins always have every privilege via `auth_manager.get_privileges`
    (which returns ADMIN_PRIVILEGES wholesale), so this is a no-op for
    them. In unauthenticated single-user mode (`require_user` returns ""),
    privileges aren't enforced — that early return is UNCHANGED in both
    modes below (a sole self-hosted owner stays explicitly permitted).

    `fail_closed` (default False, keyword-only, opt-in): the legacy
    behavior for this helper's 5 non-query-builder callers
    (document/gallery/memory/relationship/research routes) is fail-open —
    a privilege-lookup exception or a privileges dict missing `key`
    defaults to PERMITTED (`privs.get(key, True)`). That default is
    preserved byte-for-byte here. Only `routes/query_routes.py`'s two
    external-touch call sites (`run_query`, `preview_sql`) opt into
    `fail_closed=True`, under which the same two branches instead DENY
    (403) — closing SEC-04's fail-open gap on the query run/preview path
    without changing the shared helper's default for any other caller.

    Scope note (D-12): this opt-in flag is the full extent of Phase 14's
    "tighten privilege granularity" scope for the query builder. Broader
    API-token scope granularity (e.g. dedicated `datasources:read`/
    `:manage` token scopes) is deferred to the auth hardening phases
    (Phases 15-17), not introduced here.
    """
    user = require_user(request)
    if not user:
        return user
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if auth_mgr is None:
        return user
    try:
        privs = auth_mgr.get_privileges(user) or {}
    except Exception:
        if fail_closed:
            logger.warning(
                "privilege lookup failed for %s; denying %s (fail-closed)", user, key
            )
            raise HTTPException(403, f"Your account is not allowed to {key.replace('_', ' ')}.")
        return user
    if not isinstance(privs, dict):
        privs = {}
    # True = permitted; missing key defaults to permitted (unknown privileges
    # fail open — the UI gates display-side) UNLESS fail_closed, in which
    # case a missing key defaults to denied.
    default = False if fail_closed else True
    if not privs.get(key, default):
        raise HTTPException(403, f"Your account is not allowed to {key.replace('_', ' ')}.")
    return user


def owner_filter(query, model_cls, user: str, *, include_shared: bool = True):
    """Filter `query` so only rows owned by `user` (and optionally null-owner
    'shared' rows) come through. No-op when `user` is empty (single-user
    mode). Returns the modified query."""
    if not user:
        return query
    if include_shared:
        return query.filter((model_cls.owner == user) | (model_cls.owner == None))  # noqa: E711
    return query.filter(model_cls.owner == user)
