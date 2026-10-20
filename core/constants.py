# src/constants.py
"""Application-wide constants and configuration values."""
import os
import sys

APP_VERSION = "0.9.1"


def _is_windows_platform() -> bool:
    """Isolate the platform check so tests never mutate the process-wide os.name."""
    return os.name == "nt"


def _repo_base_dir() -> str:
    # Two levels up from core/constants.py == repo root. Historical BASE_DIR
    # ended with a literal "/", so keep that exact suffix for dev fidelity.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


def _asset_base_dir() -> str:
    """Read-only asset root. Frozen PyInstaller builds extract bundled data
    (static/, brains/, config/) under sys._MEIPASS; dev runs from the repo."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if not meipass:
            raise RuntimeError(
                "Frozen build is missing sys._MEIPASS — cannot locate the asset root."
            )
        return meipass if meipass.endswith(os.sep) else meipass + os.sep
    return _repo_base_dir()


def _default_data_dir() -> str:
    """Writable data dir. Precedence: PRISM_DATA_DIR > (frozen) platform appdata
    > (dev) repo-relative data/. Keeps dev behavior identical to before."""
    override = os.getenv("PRISM_DATA_DIR")
    if override and override.strip():
        return override.strip()
    if getattr(sys, "frozen", False):
        if _is_windows_platform():
            base = os.getenv("APPDATA") or os.path.expanduser("~")
            return os.path.join(base, "Prism")
        if sys.platform == "darwin":
            return os.path.expanduser(os.path.join("~", "Library", "Application Support", "Prism"))
        xdg = os.getenv("XDG_DATA_HOME") or os.path.expanduser(os.path.join("~", ".local", "share"))
        return os.path.join(xdg, "Prism")
    return os.path.join(_repo_base_dir().rstrip("/\\"), "data")


# Base paths
BASE_DIR = _asset_base_dir()
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = _default_data_dir()

# Data file paths
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
MEMORY_DOC = os.path.join(DATA_DIR, "memory_doc.md")
PERSONAL_DIR = os.path.join(DATA_DIR, "personal_docs")
RUNBOOK_DIR = os.path.join(PERSONAL_DIR, "runbook")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
FEATURES_FILE = os.path.join(DATA_DIR, "features.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# API Configuration
MAX_CONTEXT_MESSAGES = 90
REQUEST_TIMEOUT = 20
OPENAI_COMPAT_PATH = "/v1/chat/completions"

# Environment variables with defaults
DEFAULT_HOST = os.getenv("LLM_HOST", "localhost")
LLM_HOSTS = [h.strip() for h in os.getenv("LLM_HOSTS", "").split(",") if h.strip()]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# IDE bridge — set by Tauri at launch; None when running standalone
PRISM_TOKEN: str | None = os.getenv("PRISM_TOKEN")
SEARXNG_INSTANCE = os.getenv('SEARXNG_INSTANCE', 'http://localhost:8080')


# Cleanup configuration
CLEANUP_ENABLED = os.getenv("CLEANUP_ENABLED", "True").lower() == "true"
CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))

# Default parameters
DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_TOKENS = 0

# Agent loop / tool execution limits
# Single source of truth for these limits (GRAY-AGT-02). MUST NOT be
# redefined in src/agent_tools.py, src/tool_execution.py, or
# src/tool_implementations.py — those modules import from here.
MAX_OUTPUT_CHARS = 10_000  # tool stdout/stderr feedback cap
MAX_READ_CHARS = 20_000  # read_file cap
MAX_AGENT_ROUNDS = 20  # default per-turn round budget
# Per-tool in-loop wall-clock backstop. MUST be >= DEFAULT_BASH_TIMEOUT /
# DEFAULT_PYTHON_TIMEOUT (3600s, src/tool_execution.py) so bash/python's own
# subprocess timeout still governs those tools; this closes the infinite-hang
# gap for every OTHER tool type (MCP, do_*, app_api) which today has none.
TOOL_WALL_CLOCK_TIMEOUT_SECONDS = 3660
CONTINUATION_ROUND_BATCH = 10  # rounds added per continuation extension
MAX_CONTINUATION_BATCHES = 3  # hard ceiling on continuation extensions (DoS bound)
MID_STREAM_RETRY_MAX_ATTEMPTS = 2  # bounded post-content retry attempts
MID_STREAM_RETRY_BASE_BACKOFF_SECONDS = 0.5  # exponential-backoff base
# GRAY-AGT-02 / T-28-07: bound the full-output store's disk footprint. put()
# opportunistically prunes the oldest records beyond this count so a flood of
# over-limit tool calls cannot grow DATA_DIR/tool_output without limit.
MAX_TOOL_OUTPUT_STORE_FILES = 500
# GRAY-BRN-06: opt-in eviction cap for a single brain's learned partition
# (learned.npy/learned.json pair). A learned partition is per-brain and
# small, so 5000 is a generous default -- no real brain's learned notes
# should hit this on upgrade before an operator explicitly enables retention
# (brain_retention_enabled, default OFF; see 30-05/30-06's prune_learned).
MAX_LEARNED_ENTRIES_PER_BRAIN = 5000
# GRAY-BRN-06: opt-in eviction cap for memory.json (native + episodic
# entries). Consumed by 30-05/30-06's prune_memory; pinned entries are
# ALWAYS exempt from this cap (never counted toward eviction, never
# evicted) so a user's identity/pinned facts can't be pruned away. 10000
# is a generous, round default so no real store is pruned before an
# operator opts in.
MAX_MEMORY_ENTRIES = 10000
# GRAY-BRN-13: native recall's lexical-fallback path (src/memory.py::
# get_relevant_memories) used a bare 0.05 Jaccard threshold that over-injects
# weak/noisy matches -- exactly when the vector store/embedder is down and
# recall is already degraded. Raised to a named, greppable constant so weak
# sub-threshold hits are dropped while genuinely relevant (high-overlap or
# boosted) memories still clear the bar.
LEXICAL_FALLBACK_SIMILARITY_THRESHOLD = 0.2
# GRAY-BRN-13: get_relevant_memories' identity-memory injection block adds
# EVERY identity-classified memory at a fixed 0.9 score whenever a query
# trips the broad identity keyword set, bypassing the threshold entirely.
# This fixed, small cap (independent of max_items) bounds that block so
# identity memories cannot flood recall/context when the embedder is down.
IDENTITY_MEMORY_INJECTION_CAP = 3
# GRAY-AGT-04: short hot-path budget for the escalation-target resolve check
# (src/agent_loop.py::_escalate, src/answer_pipeline.py::wire_defaults().author_fn).
# Deliberately shorter than src/llm_core.py's LLMConfig.DEFAULT_TIMEOUT (30s) so a
# slow/dead endpoint cannot pin the escalation swap decision on the hot path.
ESCALATION_RESOLVE_TIMEOUT_SECONDS = 3
# 40-REVIEW IN-01: bound the per-request escalate_model string before it ever
# reaches normalize_model_id / the resolve block. An implausibly long value is
# treated as no-escalation (fail-safe), never rejected with an error.
ESCALATE_MODEL_MAX_LEN = 256
