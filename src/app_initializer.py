# src/app_initializer.py
"""Initialize all application components and dependencies."""
import os
import logging
import sqlite3
from typing import Dict, Any

from src.constants import (
    DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR,
    SESSIONS_FILE, DEFAULT_HOST, OPENAI_API_KEY, FM_DB_PATH
)
from src.memory import MemoryManager
from src.memory_provider import MemoryProviderRegistry, NativeMemoryProvider
from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_scope import chat_workspace
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
from src.tool_utils import set_upload_handler
from src.search import update_search_config

logger = logging.getLogger(__name__)

def create_directories():
    """Create necessary directories if they don't exist."""
    for directory in (DATA_DIR, PERSONAL_DIR, RUNBOOK_DIR, UPLOAD_DIR):
        os.makedirs(directory, exist_ok=True)


def prepare_frankenmemory_database() -> str:
    """Create/read the durable DB identity inherited by every fm-mcp child."""
    os.makedirs(os.path.dirname(FM_DB_PATH), exist_ok=True)
    with sqlite3.connect(FM_DB_PATH) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS fm_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO fm_meta(key,value) VALUES ('database_id', lower(hex(randomblob(16))))"
        )
        database_id = connection.execute(
            "SELECT value FROM fm_meta WHERE key='database_id'"
        ).fetchone()[0]
    expected = os.environ.get("FM_DB_ID", "").strip()
    if expected and expected != database_id:
        raise RuntimeError(
            f"frankenmemory database identity mismatch: expected {expected}, opened {database_id}"
        )
    os.environ["FM_DB_ID"] = database_id
    return database_id
        
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
    session_manager.upload_handler = upload_handler
    set_upload_handler(upload_handler)
    personal_docs_manager = PersonalDocsManager(PERSONAL_DIR, rag_manager)
    api_key_manager = APIKeyManager(DATA_DIR)
    preset_manager = PresetManager(DATA_DIR)

    # Initialize memory vector store (share embedding model with RAG if available)
    # Gate behind MEMORY_VECTOR_ENABLED: Chroma vector path is retired when
    # frankenmemory is the active provider. RAG (rag_manager) is NOT affected.
    memory_vector = None
    memory_vector_enabled = os.environ.get("MEMORY_VECTOR_ENABLED", "0").lower() in ("1", "true", "yes")
    if memory_vector_enabled:
        try:
            from src.memory_vector import MemoryVectorStore
            embedding_model = getattr(rag_manager, '_model', None) if rag_manager else None
            memory_vector = MemoryVectorStore(DATA_DIR, embedding_model=embedding_model)
            if memory_vector.healthy:
                if memory_vector.count() == 0:
                    existing = memory_manager.load()
                    if existing:
                        memory_vector.rebuild(existing)
                        logger.info(f"Rebuilt memory vector index from {len(existing)} existing entries")
                logger.info("MemoryVectorStore initialized")
            else:
                # Keep the unhealthy object (do NOT reset to None): consumers gate on
                # `.healthy`, and service_health.chromadb_health() needs a present
                # object to report DEGRADED/DOWN instead of DISABLED ("not configured").
                logger.warning("MemoryVectorStore DEGRADED: ChromaDB vector memory unavailable")
        except Exception as e:
            logger.warning(f"MemoryVectorStore DEGRADED: {e}")
            memory_vector = None
    else:
        logger.info("MemoryVectorStore DISABLED (MEMORY_VECTOR_ENABLED not set)")

    # Register memory providers
    memory_provider = os.environ.get("MEMORY_PROVIDER", "frankenmemory")
    if memory_provider == "frankenmemory":
        database_id = prepare_frankenmemory_database()
        fm_command = os.environ.get("FM_MCP_COMMAND", "fm-mcp")
        fm = FrankenmemoryProvider(
            command=fm_command,
            workspace_id=chat_workspace(),
            env={
                "FM_DB_PATH": FM_DB_PATH,
                "FM_DB_ID": database_id,
                "FM_SCOPE_AUTHORITY": "trusted-caller",
            },
        )
        native = NativeMemoryProvider(memory_manager, memory_vector)
        native.enabled = False
        memory_provider_registry = MemoryProviderRegistry([fm, native])
        logger.info("Memory provider: frankenmemory (native disabled)")
    else:
        memory_provider_registry = MemoryProviderRegistry([
            NativeMemoryProvider(memory_manager, memory_vector),
        ])
        logger.info("Memory provider: native")

    # Initialize processors. Provider-always: consumers never see None —
    # a registry with nothing enabled (can't happen via the branches above)
    # still yields a native provider.
    _active = memory_provider_registry.active()
    active_provider = _active[0] if _active else NativeMemoryProvider(memory_manager, memory_vector)
    chat_processor = ChatProcessor(memory_manager, personal_docs_manager, memory_vector=memory_vector, skills_manager=skills_manager, memory_provider=active_provider)
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
        "memory_provider": active_provider,
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
