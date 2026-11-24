# routes/cleanup_routes.py
"""Routes for cleanup operations."""
import logging
from fastapi import APIRouter, HTTPException, Request
from src.cleanup_service import get_cleanup_preview, cleanup_sessions
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

def setup_cleanup_routes(session_manager):
    """
    Setup cleanup-related routes.

    Args:
        session_manager: SessionManager instance

    Returns:
        APIRouter instance with cleanup routes
    """
    router = APIRouter(prefix="/api/cleanup")

    @router.get("/preview")
    async def cleanup_preview(request: Request):
        """
        Preview what would be cleaned up without making any changes.

        Returns:
            JSON response with lists of sessions that would be archived/deleted and estimated space savings
        """
        user = get_current_user(request)
        try:
            preview = await get_cleanup_preview(owner=user)
            from src.openclank.chat_source_bridge import canonical_source_provider
            unavailable = canonical_source_provider(request.app.state) is not None
            preview['deletion_unavailable'] = unavailable
            if unavailable:
                preview['deletion_reason'] = 'Guarded memory sources require chat lifecycle erasure.'
            return preview
        except Exception as e:
            logger.error(f"Cleanup preview failed: {e}")
            raise HTTPException(500, "Cleanup preview generation failed")

    @router.post("")
    async def cleanup_endpoint(request: Request):
        """
        Perform cleanup operations:
        1. Archive inactive sessions (not accessed for 7 days)
        2. Delete old sessions (archived, not important, not accessed for 14+ days, with fewer than 10 messages)

        Returns:
            JSON response with counts of deleted and archived sessions, and space freed
        """
        user = get_current_user(request)
        try:
            from src.openclank.chat_source_bridge import canonical_source_provider
            unavailable = canonical_source_provider(request.app.state) is not None
            archived_count, deleted_count, space_freed_mb = await cleanup_sessions(session_manager, owner=user)
            result = {
                "archived_count": archived_count,
                "deleted_count": deleted_count,
                "space_freed_mb": round(space_freed_mb, 2),
                "deletion_unavailable": unavailable,
            }
            if unavailable:
                result['deletion_reason'] = 'Guarded memory sources require chat lifecycle erasure.'
            return result
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
            raise HTTPException(500, "Cleanup operation failed")

    return router
