# src/settings.py
"""Centralized settings and features management.

Single source of truth for reading/writing data/settings.json and data/features.json.
All modules should import from here instead of accessing files directly.
"""

import json
import os
import time
import logging
from typing import Any, Optional

from src.constants import SETTINGS_FILE, FEATURES_FILE

logger = logging.getLogger(__name__)

# Tiny TTL cache for settings/features. get_setting() is called on hot paths
# (every chat, every preprocess); without this it re-parses the JSON each call.
# Picks up edits within _CACHE_TTL seconds, which is fine for human-edited config.
_CACHE_TTL = 2.0
_settings_cache: tuple[float, dict] | None = None
_features_cache: tuple[float, dict] | None = None

def _invalidate_caches():
    global _settings_cache, _features_cache
    _settings_cache = None
    _features_cache = None

# ── Default values ──

# Seeded council (TOOL-04/D-02): named specialist seats that declare tool/skill
# ownership per the roadmap SC-2 mapping, plus a chief synthesizer that
# `consumes` every specialist so it composes their outputs (closes A9). Seats
# that don't own a tool/skill simply omit those keys (empty defaults).
# endpoint_id/model are intentionally empty — the Council settings UI prompts the
# user to assign a model to each seat (mirrors how Compare starts unconfigured).
# This refreshed default only reaches users with NO saved council config —
# existing saved configs keep empty tools/skills and are non-regressive
# (Runtime State A4; no migration this phase, per Open Question 1 resolution).
DEFAULT_COUNCIL = {
    "name": "The Council",
    "enabled": True,
    "default_council_id": "panel",
    "councils": [
        {
            "id": "panel",
            "name": "The Council",
            "seats": [
                {
                    "id": "code_reviewer", "name": "Code Reviewer", "role": "panelist",
                    "endpoint_id": "", "model": "",
                    "persona": "You review code changes for bugs, security issues, and quality.",
                    "color": "#5b9dff", "temperature": 0.4,
                    "consumes": [], "skills": ["code-review"], "enabled": True,
                },
                {
                    "id": "tester", "name": "Tester", "role": "panelist",
                    "endpoint_id": "", "model": "",
                    "persona": "You write and drive tests, favoring test-first development.",
                    "color": "#41c980", "temperature": 0.4,
                    "consumes": [], "skills": ["tdd"], "enabled": True,
                },
                {
                    "id": "debugger", "name": "Debugger", "role": "panelist",
                    "endpoint_id": "", "model": "",
                    "persona": "You systematically diagnose bugs and unexpected behavior.",
                    "color": "#ff8a5b", "temperature": 0.3,
                    "consumes": [], "skills": ["diagnosing-bugs"], "enabled": True,
                },
                {
                    "id": "architect", "name": "Architect", "role": "panelist",
                    "endpoint_id": "", "model": "",
                    "persona": "You design deep modules and evaluate codebase architecture.",
                    "color": "#c97cff", "temperature": 0.3,
                    "consumes": [], "skills": ["codebase-design", "improve-codebase-architecture"],
                    "enabled": True,
                },
                {
                    "id": "planner", "name": "Planner", "role": "panelist",
                    "endpoint_id": "", "model": "",
                    "persona": "You turn requirements into PRDs and actionable issues.",
                    "color": "#ffd45b", "temperature": 0.4,
                    "consumes": [], "skills": ["to-prd", "to-issues"], "enabled": True,
                },
                {
                    "id": "chief", "name": "Synthesizer", "role": "synthesizer",
                    "endpoint_id": "", "model": "",
                    "persona": "You reconcile the panel into one clear final answer.",
                    "color": "#9b7cff", "temperature": 0.3,
                    "consumes": [
                        "code_reviewer", "tester", "debugger", "architect",
                        "planner",
                    ],
                    "enabled": True,
                },
            ],
        }
    ],
}

DEFAULT_SETTINGS = {
    "image_gen_enabled": True,
    "image_model": "",
    "image_quality": "medium",
    "vision_model": "",
    "vision_enabled": True,
    # Ordered fallback chain for the Vision model (image analysis, OCR, tagging).
    "vision_model_fallbacks": [],
    # Public base URL used to build clickable deep-links in outgoing alerts
    # (e.g., urgency alert email). Example: "https://chat.example.com"
    "app_public_url": "",
    "tts_enabled": True,
    "tts_provider": "disabled",
    "tts_model": "tts-1",
    "tts_voice": "alloy",
    "tts_speed": "1",
    "stt_enabled": False,
    "stt_provider": "disabled",
    "stt_model": "base",
    "stt_language": "",
    "search_provider": "searxng",
    # Default fallback chain — when the primary provider fails or
    # rate-limits, we try DuckDuckGo next. Free, no API key required, so
    # safe to ship on by default for every user.
    "search_fallback_chain": ["duckduckgo"],
    "search_url": "",
    "search_result_count": 5,
    # SafeSearch level applied to every provider that exposes one.
    # "strict"   — block adult / explicit results (default; matches what users
    #              expect from a research tool and avoids unrelated NSFW URLs
    #              bleeding in via provider "related" / spam recommendations)
    # "moderate" — provider-default behavior (filter explicit but allow
    #              suggestive content)
    # "off"      — disable filtering entirely (advanced users only)
    #
    # Providers that honor this setting (translated to each provider's native
    # param in src/search/providers.py:_safesearch_for):
    #     SearXNG       safesearch=0/1/2 (JSON API, HTML scrape, news fallback)
    #     Brave Search  safesearch=off/moderate/strict
    #     DuckDuckGo    safesearch=off/moderate/on (library + HTML kp param)
    #     Google PSE    safe=active (omitted for "off"; PSE has no middle tier)
    #     Serper.dev    safe=active (omitted for "off"; proxies Google's `safe`)
    # Providers NOT touched: Tavily (no SafeSearch knob; filters at index time)
    # and any custom backend reached via search_url — they keep whatever the
    # backend itself decides, so operators stay in control of self-hosted /
    # niche search instances.
    "search_safesearch": "strict",
    "brave_api_key": "",
    "google_pse_key": "",
    "google_pse_cx": "",
    "tavily_api_key": "",
    "serper_api_key": "",
    "research_endpoint_id": "",
    "research_model": "",
    "research_search_provider": "",
    "research_max_tokens": 16384,
    "research_extraction_timeout_seconds": 90,
    # Lightweight planning/query LLM calls happen before any search starts.
    # Keep them separately tunable so slow local backends are not capped by
    # the old 30s/60s per-call defaults.
    "research_planning_timeout_seconds": 90,
    "research_query_timeout_seconds": 90,
    "research_extraction_concurrency": 3,
    # Hard wall-clock cap on a single deep-research run. The previous 600s
    # (10 min) default cut off slow local / edge LLMs mid-synthesis; 1800s
    # (30 min) is comfortable for most local setups while still bounding
    # runaway jobs. Set to 0 to disable the cap entirely (unlimited) — only
    # for very long deep-research runs, since a stalled job then runs an
    # unbounded model/API bill. Other values are bounded to [60, 86400].
    # Tune via Settings or by editing data/settings.json.
    "research_run_timeout_seconds": 1800,
    "agent_max_tool_calls": 0,
    # Episodic/procedural memory write-back (reflexion-style learning). When ON,
    # after a NON-TRIVIAL SUCCESS the assistant distills what worked into a
    # compact lesson and stores it via the native memory path (source="episodic"),
    # so recall surfaces it on similar future turns. Deterministic — no LLM call.
    # OFF by default: enabling it lets the agents learn across turns.
    "agent_episodic_memory": False,
    "agent_input_token_budget": 6000,
    # Ceiling on the *auto-derived* input budget that #1230 introduced. Has
    # no effect when `agent_input_token_budget` is explicitly set (the user's
    # value is honoured regardless). Default matches
    # `src.context_budget.DEFAULT_HARD_MAX`; lower this for cost-paranoid
    # setups, raise it on premium APIs with very large windows that you
    # want to actually use (e.g. 900_000 to fill a 1M-context model). See
    # `compute_input_token_budget` in src/context_budget.py.
    "agent_input_token_hard_max": 200_000,
    "agent_stream_timeout_seconds": 300,
    # Extra directory roots that read_file / write_file may access, in
    # addition to the built-in project data/ and system temp dirs. Each
    # entry is an absolute path. Sensitive subpaths (.ssh, .gnupg, shell
    # rc files, SSH key files) are always blocked regardless of roots.
    "tool_path_extra_roots": [],
    "task_endpoint_id": "",
    "task_model": "",
    "default_endpoint_id": "",
    "default_model": "",
    # Ordered fallback chain for the default chat model. Each entry is
    # {"endpoint_id": "...", "model": "..."}. If the primary model fails
    # before producing output (endpoint offline / errors), the chat
    # dispatch retries the next entry in order.
    "default_model_fallbacks": [],
    "utility_endpoint_id": "",
    "utility_model": "",
    # Ordered fallback chain for the Utility model (summarization, naming,
    # tidy actions, etc.).
    "utility_model_fallbacks": [],
    "teacher_model": "",
    "teacher_enabled": False,
    # SEC-16: model discovery's Tailscale-peer sweep + broad port scan
    # (src/model_discovery.py) is an opt-in-only reconnaissance feature —
    # default OFF, mirroring teacher_enabled. With it off, _get_hosts()
    # returns only the configured/default host (LLM_HOSTS override always
    # still wins). When on, discover_models() consults
    # model_discovery_allowed_hosts (empty = no additional filter).
    "model_discovery_scan_enabled": False,
    "model_discovery_allowed_hosts": [],
    # Skills: minimum self-reported confidence for an auto-written (LLM-authored)
    # DRAFT skill to be injected into the agent prompt. Published skills always
    # qualify. Keeps low-confidence auto-skills out of context until they're
    # vetted/published. 0 disables the gate.
    "skill_autosave_min_confidence": 0.85,
    # Max relevant skills injected into the prompt for one request. The skills
    # library can grow beyond this; cleanup/retirement is an explicit review flow.
    "skill_max_injected": 3,
    # Reminders
    "reminder_channel": "browser",   # "browser" | "email" | "ntfy"
    "reminder_llm_synthesis": False,
    "reminder_ntfy_topic": "Reminders",
    "reminder_email_to": "",
    # Email triage scanner rules. Running/paused state and schedule live in
    # Tasks via the built-in `check_email_urgency` task.
    "urgent_email_prompt": (
        "Flag as urgent: explicit deadlines, time-sensitive requests, "
        "work-blocking issues, messages from people I report to, or anything "
        "where a delayed reply costs money/trust. Someone waiting outside, "
        "at the door, locked out, or unable to get in is urgent now. "
        "Newsletters, marketing, automated digests, and FYI-only updates are "
        "NOT urgent."
    ),
    # Council — modular multi-model deliberation. A council is a list of named
    # "seats"; each seat references a ModelEndpoint (endpoint_id + model) and a
    # persona. Seeded with two panelists + a synthesizer whose models are unset
    # until the user assigns them in Council settings (same flow as Compare).
    # Per-user overridable (see _PER_USER_KEYS) so each account can keep its own.
    "council": DEFAULT_COUNCIL,
    # Brain learning — after a normal exchange, distill a reusable lesson into
    # the shared brain so the models grow smarter from real use. Private/
    # opt-out by default: shared auto-distillation is OFF until this single
    # named flag is explicitly set True, so a private chat is never silently
    # distilled into a multi-user-readable brain. While the default is off,
    # `brain_learning_optout` is a no-op for any user who hasn't been opted
    # back in — that's expected, not a bug, on new installs.
    "brain_learning_enabled": False,
    "brain_learning_optout": [],
    # Explicit destination brain id for learned notes (GRAY-BRN-09). When
    # unset (""), learning_target() falls back to the first-sorted
    # in-brains_root brain (existing behavior). When set, it must resolve to
    # a brain whose path is inside brains_root; an unresolvable or
    # out-of-root value is treated as broken (see src/brain_learn.py).
    "learning_target": "",
    # Keyboard shortcuts (action: key combination)
    "keybinds": {
        "search": "ctrl+k",
        "toggle_sidebar": "ctrl+b",
        "new_session": "ctrl+alt+n",
        "star_session": "ctrl+alt+s",
        "delete_session": "ctrl+alt+d",
        "admin_panel": "ctrl+shift+u",
        "cancel": "escape",
    },
}

DEFAULT_FEATURES = {
    "web_search": True,
    "web_fetch": True,
    "deep_research": False,
    "memory": True,
    "document_editor": True,
    "rag": True,
    "sensitive_filter": True,
    "gallery": True,
}


# ── Settings (data/settings.json) ──

def load_settings() -> dict:
    """Load settings merged with defaults. Always returns a complete dict."""
    global _settings_cache
    now = time.monotonic()
    if _settings_cache and (now - _settings_cache[0]) < _CACHE_TTL:
        return _settings_cache[1]
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("settings must be an object")
        merged = {**DEFAULT_SETTINGS, **saved}
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, ValueError):
        merged = dict(DEFAULT_SETTINGS)
    _settings_cache = (now, merged)
    return merged


def save_settings(settings: dict):
    """Persist settings to disk (atomic; see core.atomic_io)."""
    from core.atomic_io import atomic_write_json
    atomic_write_json(SETTINGS_FILE, settings, indent=2)
    _invalidate_caches()


def get_setting(key: str, default: Any = None) -> Any:
    """Read a single setting value."""
    return load_settings().get(key, default)


def is_setting_overridden(key: str) -> bool:
    """True if ``key`` is explicitly present in the saved settings file.

    ``load_settings`` merges DEFAULT_SETTINGS with the saved file, so a value
    equal to its default is indistinguishable from "never set" via get_setting.
    Callers that need to treat an explicit user choice differently from the
    default (e.g. adaptive budgets) use this to read the raw saved file.
    """
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        return isinstance(saved, dict) and key in saved
    except (FileNotFoundError, json.JSONDecodeError):
        return False


# Per-user settings (user prefs override the global admin default). Used for
# keys that a user is allowed to choose individually — currently the vision
# model + image-generation model. The owner argument is the authed username
# resolved by FastAPI deps; an empty/None owner falls through to the global.
_PER_USER_KEYS = {
    "vision_model", "vision_enabled", "vision_model_fallbacks",
    "image_model", "image_gen_enabled", "image_quality",
    # Default chat endpoint / model — without per-user resolution every new
    # account inherited whatever the most-recent admin picked, which then
    # got injected into the chat composer on first open.
    "default_endpoint_id", "default_model", "default_model_fallbacks",
    "utility_endpoint_id", "utility_model", "utility_model_fallbacks",
    "research_endpoint_id", "research_model",
    # Council config (seats/models/personas/colors) is per-user so each account
    # can curate its own panel without clobbering the admin default.
    "council",
}


def get_user_setting(key: str, owner: str = "", default: Any = None) -> Any:
    """Resolve `key` from the caller's per-user prefs first, falling back to
    the global setting. Only the small whitelist in `_PER_USER_KEYS` is
    eligible — for any other key this is equivalent to `get_setting(key)`.

    Falls back gracefully if the prefs module can't be imported (cycle/early
    boot) — admin-global settings keep working.
    """
    if owner and key in _PER_USER_KEYS:
        try:
            from routes.prefs_routes import _load_for_user
            prefs = _load_for_user(owner) or {}
            if key in prefs and prefs[key] not in (None, ""):
                return prefs[key]
        except Exception:
            pass
    return get_setting(key, default)


# ── Features (data/features.json) ──

def load_features() -> dict:
    """Load feature flags merged with defaults."""
    global _features_cache
    now = time.monotonic()
    if _features_cache and (now - _features_cache[0]) < _CACHE_TTL:
        return _features_cache[1]
    try:
        with open(FEATURES_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("features must be an object")
        merged = {**DEFAULT_FEATURES, **saved}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        merged = dict(DEFAULT_FEATURES)
    _features_cache = (now, merged)
    return merged


def save_features(features: dict):
    """Persist feature flags to disk (atomic). Central write path for features.json."""
    from core.atomic_io import atomic_write_json
    atomic_write_json(FEATURES_FILE, features, indent=2)
    _invalidate_caches()


# ── Uniform provider config precedence (GRAY-INT-07) ──

def resolve_config_value(
    env_var: Optional[str],
    settings_key: Optional[str],
    default: Any = None,
    settings: Optional[dict] = None,
) -> tuple:
    """Uniform env -> settings -> default precedence resolver (GRAY-INT-07).

    Returns a ``(value, source)`` tuple where ``source`` is one of the string
    literals ``"env"``, ``"settings"``, or ``"default"``.

    Precedence, in order:
      1. If ``env_var`` is truthy, read ``os.environ.get(env_var)``. Stripped
         before the truthiness check, same as the settings tier (IN-02) — an
         empty or whitespace-only string does not count as set and falls
         through to the next tier.
      2. If ``settings_key`` is truthy, read it from the passed ``settings``
         dict (when not None) else from ``load_settings()``. String values are
         stripped before the truthiness check.
      3. Otherwise return ``(default, "default")``.

    This resolver does NOT cover the encrypted ``ModelEndpoint`` DB tier used
    for LLM chat endpoints (``src/api_key_manager.py`` / ``src/endpoint_resolver.py``)
    — that remains its own explicit tier, documented but not migrated here
    (see CONTEXT.md Deferred Ideas for Phase 25).
    """
    if env_var:
        val = os.environ.get(env_var)
        if val is not None:
            val = val.strip()
        if val:
            return val, "env"
    if settings_key:
        s = settings if settings is not None else load_settings()
        val = s.get(settings_key)
        if isinstance(val, str):
            val = val.strip()
        if val:
            return val, "settings"
    return default, "default"
