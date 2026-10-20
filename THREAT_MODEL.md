# Threat Model

> **Current branch:** single-user/no-login. `app.py` forces authentication off; the login and multi-user policy described in some legacy sections below is not active. Do not expose this service directly to the internet. See [runtime boundary](PUBLIC-RUNTIME-NOTES.md).


Prisma is a **self-hosted AI workspace with privileged local access**. This document states the trust boundary so contributors can reason about security decisions without reading through the full auth and middleware stack.

## Trust Boundary

Prisma is designed for **trusted users on a private network**, not public exposure. The README describes it as "treat it like an admin console" — that framing is accurate. A logged-in admin can execute shell commands, read and write files, send email, and control model serving. This is intentional. The threat model does not try to prevent admins from doing these things. It does try to prevent:

- Unauthenticated access
- Non-admins reaching admin-only capabilities
- The AI agent acting on instructions injected through untrusted content (web results, emails, fetched pages, memories)
- Internal services (ChromaDB, Ollama, SearXNG, etc.) being reachable from outside the host

## Roles and Capabilities

| Capability | Admin | Non-admin (default) |
|---|---|---|
| Chat with agent | ✓ | ✓ |
| Browser tool | ✓ | ✓ |
| Documents | ✓ | ✓ |
| Research mode | ✓ | ✓ |
| Image generation | ✓ | ✓ |
| Memory management | ✓ | ✓ |
| Shell / Python execution | ✓ | ✗ |
| File read / write | ✓ | ✗ |
| Email send / read | ✓ | ✗ |
| MCP tools | ✓ | ✗ |
| Calendar management | ✓ | ✗ |
| Token / webhook management | ✓ | ✗ |
| Model serving | ✓ | ✗ |
| Vault | ✓ | ✗ |
| Settings | ✓ | ✗ |

Non-admin defaults are in `core/auth.py:DEFAULT_PRIVILEGES`. Tool enforcement is in `src/tool_security.py:NON_ADMIN_BLOCKED_TOOLS`. Any tool whose name starts with `mcp__` is also blocked for non-admins. Admins always get full access regardless of stored privilege values.

## Authentication

- **Sessions:** bcrypt passwords, 7-day session tokens stored atomically in `data/sessions.json` via `core/atomic_io.py`.
- **2FA:** TOTP with 8 single-use backup codes. Verified after password check, before session issuance.
- **Reserved usernames:** `internal-tool`, `api`, `demo`, `system` cannot be registered or renamed into. Defined in `core/auth.py:RESERVED_USERNAMES`.
  - `internal-tool` is security-critical: `core/middleware.py:require_admin` treats any request where `request.state.current_user == "internal-tool"` as the in-process tool loopback and grants admin unconditionally. A real account with that name would silently pass every `require_admin` check.
- **Orphan sessions:** `validate_token` re-checks that the user record still exists on every call. A deleted user's cookie is dropped on next request rather than continuing to authenticate.

## Internal Tool Loopback

Agent tool calls reach admin-gated HTTP routes over an in-process HTTP loopback. The mechanism:

1. At app startup, `core/middleware.py` generates a random `INTERNAL_TOOL_TOKEN` via `secrets.token_hex(32)`. It is never persisted and never sent to clients.
2. Loopback requests carry `X-Prisma-Internal-Token: <token>` or have `request.state.current_user` already set to `"internal-tool"` by the auth middleware.
3. `require_admin` recognises either signal and grants access without checking the session user.

The agent may be running in a non-admin user's session, but tool dispatch first calls `src/tool_security.py:owner_is_admin_or_single_user` to verify the session owner is an admin before issuing any loopback call. Non-admin users cannot invoke admin tools even via the agent.

## Prompt-Injection Hardening

External content that reaches the LLM is treated as untrusted via `src/prompt_security.py`:

- `untrusted_context_message(label, content)` wraps the content in a `user`-role message with a header block instructing the model not to follow instructions inside it. Content goes in as data, not as a system instruction.
- `UNTRUSTED_CONTEXT_POLICY` is a system-prompt preamble that states the same policy at the top of every session where untrusted data may appear.

**Untrusted surfaces that must go through this wrapper:** web search results, fetched URLs, emails (read), saved memories, skill text, notes, and any tool output sourced from outside the server. Injecting untrusted content directly into the system role is a security bug.

## Security Headers

`core/middleware.py:SecurityHeadersMiddleware` sets headers on every response:

- `X-Frame-Options: DENY` + `frame-ancestors 'none'` on all routes except tool-render iframes (which are sandboxed at the HTML level).
- `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` everywhere.
- **CSP:** nonce-based `script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net`. `style-src 'unsafe-inline'` is intentionally kept — `static/index.html` ships inline `<style>` blocks and JS modules set `style=""` attributes at runtime. Inline styles do not execute script so the risk is visual-only. Removing this requires templating the HTML files and auditing all JS-set style attributes.

## Known Gaps

These are open, acknowledged, and contributor help is welcome:

1. **No full shell/filesystem sandbox for `bash`/`python`.** The agent `bash` and
   `python` tools spawn a shell/interpreter as the app-process user with no network
   egress filtering or filesystem confinement, so a prompt-injection reaching a
   shell-enabled **admin** session can read local files or reach internal services.
   Note `read_file`/`write_file` are NOT affected — they are already path-confined
   (allowlist + sensitive-file policy; `tests/test_tool_path_confinement.py`), and
   `bash`/`python` are blocked for non-admins (`src/tool_security.py`). Interim
   mitigation shipped: `PRISM_AGENT_SHELL_DISABLED` (default off) is an operator
   kill-switch for the shell tools. Full sandbox design in `SANDBOX_DESIGN.md`; see #1058.

2. **Token scopes do not sub-scope a browser session.** There is no way to hand a
   logged-in session a subset of the owning user's privileges. Bearer API tokens,
   however, are fail-closed: they authenticate as the reserved `api` sentinel user
   (`app.py` AuthMiddleware), so they cannot pass `require_admin`/`require_privilege`
   at all, and the routes that DO accept tokens now enforce per-capability scopes
   through the shared `require_scope()` chokepoint (`src/auth_helpers.py`; scope
   vocabulary in `routes/api_token_routes.py:ALLOWED_SCOPES`). Remaining work:
   per-capability sub-scoping of interactive sessions.

## Recently Resolved

Closed by the security-audit pass (with regression tests):

- **SSRF via `/api/v1/chat` `base_url`** — the token-supplied `base_url` is now run
  through `validate_public_http_url()` (`src/url_security.py`), which rejects
  loopback, private ranges, and the cloud-metadata IP. (Was Known Gap #2.)
- **`src/search/` drift** — `analytics`, `cache`, `content`, `query`, and `ranking`
  are now compatibility re-export shims to the canonical `services.search.*`, so the
  two copies can no longer diverge. (Was Known Gap #3.)
- **Session tokens plaintext at rest** — `data/sessions.json` is now keyed by
  `sha256(token)`; the raw bearer token never touches disk (`core/auth.py`).
- **Webhook DNS-rebinding** — delivery pins the connection to the vetted IP
  (`resolve_pinned_target`, `src/webhook_manager.py`).
- **Mermaid `securityLevel: 'loose'`** over untrusted chat markdown → `'strict'`
  (`static/js/markdown.js`).
- **`PRISM_DATA_DIR` split-brain** — `src/constants.py` now re-exports
  `core.constants`, so all data paths honor the override.
- **Missing `can_manage_brain` gate** on `/api/brain/config` + brain-graph/note reads,
  and **missing `can_use_documents` gate** on `/api/personal/upload`.
