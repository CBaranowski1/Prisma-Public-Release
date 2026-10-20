# src/constants.py
"""Compatibility shim — re-exports the canonical constants from core.constants.

HISTORICAL SPLIT-BRAIN (audit P1): this module used to define its own copy of
BASE_DIR/DATA_DIR/… with DATA_DIR hardcoded to "<repo>/data". core.constants,
meanwhile, honors PRISM_DATA_DIR and the frozen-app platform appdata dir. Any
module importing DATA_DIR (and SETTINGS_FILE/FEATURES_FILE/UPLOAD_DIR/…) from
here therefore read/wrote a DIFFERENT data root than the rest of the app under a
custom PRISM_DATA_DIR or a packaged build — splitting secrets, settings,
uploads, OAuth artifacts, mail DBs and the admin-wipe target across two dirs.

To keep a single source of truth, this file now re-exports core.constants so
`from src.constants import X` resolves to the exact same object as
`from core.constants import X`. Do not reintroduce standalone definitions here.
"""

from core.constants import *  # noqa: F401,F403
from core.constants import (  # explicit re-exports for IDE/type-checker visibility
    APP_VERSION,
    BASE_DIR,
    STATIC_DIR,
    DATA_DIR,
    SESSIONS_FILE,
    MEMORY_FILE,
    MEMORY_DOC,
    PERSONAL_DIR,
    RUNBOOK_DIR,
    UPLOAD_DIR,
    FEATURES_FILE,
    SETTINGS_FILE,
    MAX_CONTEXT_MESSAGES,
    REQUEST_TIMEOUT,
    OPENAI_COMPAT_PATH,
    DEFAULT_HOST,
    LLM_HOSTS,
    OPENAI_API_KEY,
    SEARXNG_INSTANCE,
    CLEANUP_ENABLED,
    CLEANUP_INTERVAL_HOURS,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
)
