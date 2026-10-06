"""
RAG singleton instance for the application.
"""
import logging
import time

logger = logging.getLogger(__name__)

rag_instance = None
_last_attempt = 0.0
_RETRY_INTERVAL = 30  # seconds between re-init attempts


def get_rag_manager():
    """Lazy canonical Frankenmemory RAG initializer.

    Returns the owner-scoped SQLite projection on first successful init. Failed
    attempts are throttled to once per ``_RETRY_INTERVAL`` so a damaged data
    path cannot busy-retry on every request.
    """
    global rag_instance, _last_attempt

    if rag_instance is not None:
        return rag_instance

    now = time.monotonic()
    if now - _last_attempt < _RETRY_INTERVAL:
        return None  # too soon to retry — last attempt failed

    _last_attempt = now

    try:
        from src.frankenmemory_database import prepare_frankenmemory_database

        # Admit the native current store before a projection can write any tables.
        prepare_frankenmemory_database()
        from src.frankenmemory_rag import FrankenmemoryRAG
        from src.embeddings import get_embedding_client

        # This is an owner-keyed factory, not an ambient provider client.  It
        # resolves the caller's normalized ``embeddings`` binding only when a
        # vector generation or query actually needs it.
        embedding_client = get_embedding_client()
        rag_instance = FrankenmemoryRAG(
            embedding_client=embedding_client,
            embedding_provider_ref=None,
        )
        if not rag_instance.healthy:
            logger.warning("Document RAG backend created but not healthy, will retry later")
            rag_instance = None
        else:
            logger.info("Initialized canonical Frankenmemory document RAG")

    except ImportError as e:
        logger.warning(f"Document RAG backend not available: {e}")
        rag_instance = None
    except Exception as e:
        logger.error(f"Failed to initialize document RAG: {e}")
        rag_instance = None

    return rag_instance
