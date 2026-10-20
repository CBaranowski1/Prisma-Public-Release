"""Secret-scrubbing for settings exposed to non-admin / unauthenticated callers.

Deliberately dependency-light (stdlib only) and separate from
``routes/auth_routes.py`` so it can be imported and unit-tested without dragging
in the FastAPI app / auth / database import chain.

``/api/auth/settings`` is auth-exempt — the frontend (and the pre-login page)
read it for keybinds + TTS prefs, so non-admin and unauthenticated callers get a
*scrubbed* copy. Secrets (provider API keys, IMAP/SMTP passwords, OAuth tokens)
must NOT leak to them — load-bearing when the app is reachable over a Cloudflare
tunnel / reverse proxy. Scrubbing is deep (recurses nested dicts/lists) and keyed
on secret-shaped names.
"""

_SECRET_KEY_PATTERNS = (
    "_api_key", "_apikey", "_password", "_passwd", "_pass", "_pwd",
    "_secret", "_client_secret", "_token", "_access_token", "_refresh_token",
    "_credential", "_credentials", "_key",
)
_SECRET_KEY_ALLOW = ("google_pse_cx",)  # public identifiers, not secrets


def is_secret_key(name: str) -> bool:
    n = (name or "").lower()
    if n in _SECRET_KEY_ALLOW:
        return False
    return any(n.endswith(p) or n == p.lstrip("_") for p in _SECRET_KEY_PATTERNS)


def _scrub_value(key, value):
    """Mask secret-shaped leaves, recursing into nested dicts/lists so a secret
    stored under a non-secret parent key (e.g.
    ``{"email_account": {"smtp_password": "..."}}``) is still blanked. Only
    non-empty *string* values are blanked; presence is preserved."""
    if isinstance(value, dict):
        return {
            k: ("" if (is_secret_key(k) and isinstance(v, str) and v)
                else _scrub_value(k, v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(key, item) for item in value]
    if is_secret_key(key) and isinstance(value, str) and value:
        return ""
    return value


def scrub_settings(settings: dict) -> dict:
    """Return a copy of ``settings`` with secret-shaped values masked (deep)."""
    if not isinstance(settings, dict):
        return {}
    return {k: _scrub_value(k, v) for k, v in (settings or {}).items()}

# Keys the pre-login page / non-admin frontend actually reads from
# ``/api/auth/settings`` (keybinds, TTS/STT prefs, a few UI toggles) plus
# clearly-non-secret neighbours. Anonymous and non-admin callers get ONLY
# these keys — an allowlist, so a future settings key that doesn't match the
# secret-suffix patterns above (URLs with embedded creds, prompt text with
# secrets pasted in, …) can't leak pre-auth by default. Consumers:
# static/js/keyboard-shortcuts.js + slashCommands.js (keybinds),
# static/app.js + tts-ai.js (tts_*), static/js/search.js (search_provider),
# static/js/emailLibrary.js (reminder_channel), static/js/tasks.js
# (urgent_email_prompt).
PUBLIC_SETTINGS_KEYS = (
    "keybinds",
    "tts_enabled", "tts_provider", "tts_model", "tts_voice", "tts_speed",
    "stt_enabled", "stt_provider", "stt_model", "stt_language",
    "search_provider", "search_result_count", "search_safesearch",
    "image_gen_enabled", "image_quality", "vision_enabled",
    "reminder_channel", "reminder_llm_synthesis", "reminder_ntfy_topic",
    "urgent_email_prompt",
)


def public_settings(settings: dict) -> dict:
    """Allowlisted + scrubbed view of ``settings`` for anonymous / non-admin
    callers. Belt-and-braces: only ``PUBLIC_SETTINGS_KEYS`` survive, and the
    surviving values still pass through :func:`scrub_settings` in case a
    nested secret ever lands under an allowlisted key."""
    if not isinstance(settings, dict):
        return {}
    return scrub_settings({k: v for k, v in settings.items() if k in PUBLIC_SETTINGS_KEYS})
