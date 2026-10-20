# src/app_initializer.py
"""Initialize all application components and dependencies."""
import os
import logging
from typing import Dict, Any

from src.constants import (
    BASE_DIR, DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR,
    SESSIONS_FILE, DEFAULT_HOST, OPENAI_API_KEY
)
from src.memory import MemoryManager
from src.memory_provider import MemoryProviderRegistry, NativeMemoryProvider
from services.memory.skills import SkillsManager
from core.session_manager import SessionManager
from core.models import set_session_manager
from src.personal_docs import PersonalDocsManager
from src.api_key_manager import APIKeyManager
from src.preset_manager import PresetManager
from src.chat_processor import ChatProcessor
from src.model_discovery import ModelDiscovery
from src.chat_handler import ChatHandler
from src.research_handler import ResearchHandler
from src.upload_handler import UploadHandler
from src.search import update_search_config

logger = logging.getLogger(__name__)

def create_directories():
    """Create necessary directories if they don't exist."""
    for directory in (DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR):
        os.makedirs(directory, exist_ok=True)
        
def initialize_managers(base_dir: str, rag_manager=None) -> Dict[str, Any]:
    """
    Initialize all manager and handler instances.

    Args:
        base_dir: Base directory path
        rag_manager: RAG manager instance (optional)
    Returns:
        Dictionary containing all initialized components
    """
    # Create directories first
    create_directories()

    # Initialize core managers
    memory_manager = MemoryManager(DATA_DIR)
    skills_manager = SkillsManager(DATA_DIR)
    session_manager = SessionManager(SESSIONS_FILE)
    set_session_manager(session_manager)  # Enable Session.add_message() persistence
    upload_handler = UploadHandler(base_dir, UPLOAD_DIR)
    personal_docs_manager = PersonalDocsManager(PERSONAL_DIR, rag_manager)
    api_key_manager = APIKeyManager(DATA_DIR)
    preset_manager = PresetManager(DATA_DIR)

    # Initialize memory vector store (share embedding model with RAG if available)
    memory_vector = None
    try:
        from src.memory_vector import MemoryVectorStore
        embedding_model = getattr(rag_manager, '_model', None) if rag_manager else None
        memory_vector = MemoryVectorStore(DATA_DIR, embedding_model=embedding_model)
        if memory_vector.healthy:
            # Rebuild index from existing memories if empty
            if memory_vector.count() == 0:
                existing = memory_manager.load()
                if existing:
                    memory_vector.rebuild(existing)
                    logger.info(f"Rebuilt memory vector index from {len(existing)} existing entries")
            logger.info("MemoryVectorStore initialized")
        else:
            logger.warning("MemoryVectorStore DEGRADED: ChromaDB vector memory unavailable")
            memory_vector = None
    except Exception as e:
        logger.warning(f"MemoryVectorStore DEGRADED: {e}")
        memory_vector = None

    from src.brain_provider import discover_providers, GuardedProvider
    from src.brains import build_brain_providers
    _providers = [NativeMemoryProvider(memory_manager, memory_vector)]
    for _p in discover_providers(os.path.join(BASE_DIR, "providers")):
        # The file-vault provider (precision-audit) is now instantiated once per
        # brain folder by build_brain_providers(); skip the bare auto-discovered
        # singleton so it doesn't duplicate / shadow the modular brains.
        if getattr(_p, "provider_id", "") == "precision-audit":
            continue
        _providers.append(GuardedProvider(_p))
    # Modular brains: one provider per brains/<id> folder + a combined provider.
    try:
        _providers.extend(build_brain_providers())
    except Exception as _e:
        logger.warning("brain discovery failed: %s", _e)
    memory_provider_registry = MemoryProviderRegistry(_providers)

    # Warm each brain's vector index in the background so the FIRST recall is
    # instant (loading a big index — ~100MB — on the first query otherwise adds
    # a one-time few-second stall). Best-effort, daemon thread, never blocks boot.
    def _warm_brain_indexes():
        for _p in _providers:
            inner = getattr(_p, "_inner", _p)
            try:
                idx = inner._vector_index() if hasattr(inner, "_vector_index") else None
                if idx is not None and idx.available:
                    idx._ensure_main()
                    idx._ensure_learned()
                    logger.info("warmed brain index: %s", getattr(_p, "provider_id", "?"))
            except Exception as exc:
                logger.error(
                    "brain index warm failed for %s: %s",
                    getattr(_p, "provider_id", "?"),
                    exc,
                    exc_info=True,
                )
    try:
        import threading
        threading.Thread(target=_warm_brain_indexes, daemon=True).start()
    except Exception:
        pass

    # Initialize processors
    chat_processor = ChatProcessor(memory_manager, personal_docs_manager, memory_vector=memory_vector, skills_manager=skills_manager, provider_registry=memory_provider_registry)
    research_handler = ResearchHandler()
    
    # Initialize chat handler with all dependencies
    chat_handler = ChatHandler(
        session_manager=session_manager,
        memory_manager=memory_manager,
        chat_processor=chat_processor,
        research_handler=research_handler,
        preset_manager=preset_manager,
        upload_handler=upload_handler,
    )
    
    # Initialize model discovery
    model_discovery = ModelDiscovery(DEFAULT_HOST, OPENAI_API_KEY)
    
    # Load and apply saved API keys
    saved_keys = api_key_manager.load()
    if "brave" in saved_keys:
        update_search_config(api_key=saved_keys["brave"])
        logger.info("Loaded Brave API key from saved configuration")
    
    return {
        "memory_manager": memory_manager,
        "memory_vector": memory_vector,
        "memory_provider_registry": memory_provider_registry,
        "skills_manager": skills_manager,
        "session_manager": session_manager,
        "upload_handler": upload_handler,
        "personal_docs_manager": personal_docs_manager,
        "api_key_manager": api_key_manager,
        "preset_manager": preset_manager,
        "chat_processor": chat_processor,
        "research_handler": research_handler,
        "chat_handler": chat_handler,
        "model_discovery": model_discovery,
        "current_presets": preset_manager.presets,
        "PERSONAL_INDEX": personal_docs_manager.index
    }
