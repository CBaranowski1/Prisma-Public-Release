"""Initialization shared by the packaged server entrypoint and setup.py.

Creates the writable data-dir tree and the SQLite schema WITHOUT creating an
admin user — so a fresh install reaches the existing web first-run setup screen
(static/login.html -> POST /api/auth/setup) instead of needing a terminal.
"""
import os

# Subdirs the app expects under the data dir (mirrors setup.py:DIRS).
_DATA_SUBDIRS = [
    "uploads",
    "personal_docs",
    "personal_uploads",
    "tts_cache",
    "generated_images",
    "deep_research",
    "chroma",
    "rag",
    "memory_vectors",
    "logs",
]


def ensure_data_dirs(data_dir: str) -> None:
    """Create the data dir and its standard subdirs. Idempotent."""
    os.makedirs(data_dir, exist_ok=True)
    for sub in _DATA_SUBDIRS:
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)


def init_database(data_dir: str) -> None:
    """Create all SQLAlchemy tables. Mirrors setup.py:init_database."""
    from core.database import _default_database_url
    os.environ.setdefault("DATABASE_URL", _default_database_url(data_dir))
    from core.database import Base, engine
    Base.metadata.create_all(bind=engine)


def bootstrap() -> None:
    """Full first-run init: data dirs + DB schema (no admin user)."""
    from core.constants import DATA_DIR
    ensure_data_dirs(DATA_DIR)
    init_database(DATA_DIR)
