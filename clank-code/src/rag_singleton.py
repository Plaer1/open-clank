"""
RAG singleton instance for the application.
"""
import logging
import time
import threading

logger = logging.getLogger(__name__)

rag_instance = None
_last_attempt = 0.0
_RETRY_INTERVAL = 30  # seconds between re-init attempts


# One nonblocking gate covers lazy construction and the complete reset handoff.
_handoff_lock = threading.Lock()
_active_witness = None

class _ResetPreparationWitness:
    __slots__ = ('manager', 'old', 'identity', 'capabilities', 'new', 'released')
    def __init__(self, manager, old, identity, capabilities):
        self.manager, self.old, self.identity, self.capabilities = manager, old, identity, capabilities
        self.new = None
        self.released = False

def begin_reset_preparation_handoff(old_preparation):
    from src.openclank.fm_reset_receipt import _identity
    global _active_witness
    if not _handoff_lock.acquire(blocking=False):
        raise RuntimeError('RAG handoff busy')
    try:
        old_preparation.assert_valid()
        identity = _identity(old_preparation)
        capabilities = dict(old_preparation.capabilities)
        manager = rag_instance
        old = None if manager is None else manager._guarded_preparation
        if manager is not None:
            if old is None:
                raise ValueError('RAG preparation unavailable')
            old.assert_valid()
            if (_identity(old) != identity or old.schema_cookie != old_preparation.schema_cookie
                    or dict(old.capabilities) != capabilities):
                raise ValueError('RAG preparation changed')
        _active_witness = _ResetPreparationWitness(manager, old, identity, capabilities)
        return _active_witness
    except BaseException:
        _handoff_lock.release()
        raise

def rebind_reset_preparation_handoff(witness, new_preparation):
    from src.openclank.fm_reset_receipt import _identity
    if (type(witness) is not _ResetPreparationWitness or witness is not _active_witness or witness.released or witness.new is not None
            or not _handoff_lock.locked() or rag_instance is not witness.manager):
        raise ValueError('RAG handoff unavailable')
    new_preparation.assert_valid()
    if _identity(new_preparation) != witness.identity or dict(new_preparation.capabilities) != witness.capabilities:
        raise ValueError('new RAG preparation changed')
    if witness.manager is not None:
        witness.manager._rebind_guarded_preparation(witness.old, new_preparation)
    witness.new = new_preparation
    return witness

def finish_reset_preparation_handoff(witness):
    global _active_witness
    if (type(witness) is not _ResetPreparationWitness or witness is not _active_witness or witness.released or witness.new is None
            or not _handoff_lock.locked() or rag_instance is not witness.manager
            or (witness.manager is not None and witness.manager._guarded_preparation is not witness.new)):
        raise ValueError('RAG handoff incomplete')
    witness.released = True
    _active_witness = None
    _handoff_lock.release()
    return True

def abort_reset_preparation_handoff(witness):
    """Release only an unchanged, unrebound witness before any reset issue."""
    global _active_witness
    if (type(witness) is not _ResetPreparationWitness or witness is not _active_witness
            or witness.released or witness.new is not None or not _handoff_lock.locked()
            or rag_instance is not witness.manager
            or (witness.manager is not None and witness.manager._guarded_preparation is not witness.old)):
        raise ValueError('RAG handoff unavailable')
    witness.released = True
    _active_witness = None
    _handoff_lock.release()
    return True

def get_rag_manager():
    """Lazy canonical Memery RAG initializer.

    Returns the owner-scoped SQLite projection on first successful init. Failed
    attempts are throttled to once per ``_RETRY_INTERVAL`` so a damaged data
    path cannot busy-retry on every request.
    """
    global rag_instance, _last_attempt

    if not _handoff_lock.acquire(blocking=False):
        return None
    try:

        if rag_instance is not None:
            return rag_instance

        now = time.monotonic()
        if now - _last_attempt < _RETRY_INTERVAL:
            return None  # too soon to retry — last attempt failed

        _last_attempt = now

        try:
            import os
            from src.memery_database import (
                prepare_memery_database, prepare_guarded_memery_database, resolve_memory_store_profile,
            )
            from src.constants import FM_DB_PATH

            # Same resolver src/app_initializer.py uses; idempotent (it only resolves/prepares once per process,
            # caching its result in OPENCLANK_MEMORY_STORE_PROFILE) so a normal start does no work here twice.
            profile = resolve_memory_store_profile(FM_DB_PATH)
            if profile in ("guarded-v17", "guarded-v18"):
                preparation = prepare_guarded_memery_database(FM_DB_PATH, profile=profile)
            else:
                prepare_memery_database()
                preparation = None
            from src.memery_rag import MemeryRAG
            from src.embeddings import get_embedding_client

            # This is an owner-keyed factory, not an ambient provider client.  It
            # resolves the caller's normalized ``embeddings`` binding only when a
            # vector generation or query actually needs it.
            embedding_client = get_embedding_client()
            rag_instance = MemeryRAG(
                embedding_client=embedding_client,
                embedding_provider_ref=None,
                guarded_preparation=preparation,
            )
            if not rag_instance.healthy:
                logger.warning("Document RAG backend created but not healthy, will retry later")
                rag_instance = None
            else:
                logger.info("Initialized canonical Memery document RAG")

        except ImportError as e:
            logger.warning(f"Document RAG backend not available: {e}")
            rag_instance = None
        except Exception as e:
            logger.error(f"Failed to initialize document RAG: {e}")
            rag_instance = None

        return rag_instance
    finally:
        _handoff_lock.release()
