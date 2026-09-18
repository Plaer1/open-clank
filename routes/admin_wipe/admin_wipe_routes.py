"""Admin Danger Zone — per-category wipes.

Each endpoint is admin-only and truncates exactly one domain so the
user can selectively reset skills / notes / etc. without
nuking everything. The catch-all `chats` endpoint mirrors the
existing /api/sessions/all so the Danger Zone speaks one URL pattern.

URL shape: DELETE /api/admin/wipe/{kind}
Kinds: chats, skills, notes, tasks, documents, gallery, calendar. The legacy
memory kind is retired; use the account-owned Brain reset flow.
"""

import logging
import os
import shutil
from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from core.database import (
    SessionLocal,
    Session as DbSession,
    ChatMessage as DbChatMessage,
    Note,
    ScheduledTask,
    TaskRun,
    Document,
    DocumentVersion,
    GalleryImage,
    GalleryAlbum,
    CalendarEvent,
    CalendarCal,
)
from src.constants import SKILLS_DIR, SKILLS_FILE, GALLERY_DIR, GALLERY_UPLOADS_DIR
from src.generated_images import GENERATED_IMAGE_DIR, resolve_gallery_image_path

logger = logging.getLogger(__name__)


def _rmtree_quiet(path: str):
    """rmtree that doesn't crash if the path doesn't exist."""
    if os.path.isdir(path):
        try:
            shutil.rmtree(path)
        except OSError as e:
            logger.warning(f"Could not remove {path}: {e}")


def setup_admin_wipe_routes(session_manager, memory_provider=None):
    """The session_manager is passed in so we can also clear its
    in-memory cache when wiping chats — without it the DB is empty
    but the next /api/sessions returns stale entries."""
    router = APIRouter(prefix="/api/admin")

    @router.delete("/wipe/{kind}")
    async def wipe(kind: str, request: Request):
        require_admin(request)
        kind = (kind or "").strip().lower()

        if kind == "memory":
            raise HTTPException(
                410,
                "The admin Memory wipe is retired; open Brain settings to preview and confirm an account-owned reset.",
            )

        db = SessionLocal()
        try:
            if kind == "chats":
                session_rows = db.query(DbSession.id, DbSession.owner).all()
                session_ids = {row[0] for row in session_rows}
                session_owners = {
                    str(row[0]): str(row[1] or "")
                    for row in session_rows
                }
                from src.openclank.transcript_projection import (
                    list_projections,
                    purge_execution_projection,
                )
                supervisor = getattr(request.app.state, "mimo_supervisor", None)
                projections = list_projections()
                session_ids.update(row["odysseus_session_id"] for row in projections)
                for row in projections:
                    session_owners.setdefault(
                        str(row["odysseus_session_id"]),
                        str(row.get("owner") or ""),
                    )
                for session_id, session in session_manager.sessions.items():
                    session_owners.setdefault(
                        str(session_id),
                        str(getattr(session, "owner", None) or ""),
                    )
                bridge = getattr(supervisor, "bridge", None) if supervisor else None
                if bridge is not None:
                    session_ids.update(bridge.mapped_sessions())
                for session_id in session_ids:
                    try:
                        await purge_execution_projection(supervisor, session_id)
                    except RuntimeError as exc:
                        raise HTTPException(503, str(exc)) from exc
                from src import bg_jobs

                for session_id, session_owner in session_owners.items():
                    bg_jobs.delete_for_session_owner(
                        session_id=session_id,
                        owner=session_owner,
                    )
                count = db.query(DbSession).count()
                db.query(DbChatMessage).delete()
                db.query(DbSession).delete()
                db.commit()
                try:
                    session_manager.sessions.clear()
                except Exception:
                    pass
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "skills":
                # Skills live as SKILL.md files under data/skills/. Drop
                # the entire directory; the SkillsManager re-creates the
                # tree on next write.
                skills_dir = SKILLS_DIR
                count = 0
                if os.path.isdir(skills_dir):
                    # Count SKILL.md files for the response — quick walk.
                    for _, _, files in os.walk(skills_dir):
                        count += sum(1 for f in files if f == "SKILL.md")
                    _rmtree_quiet(skills_dir)
                # Legacy fallback file
                legacy = SKILLS_FILE
                if os.path.exists(legacy):
                    try:
                        os.remove(legacy)
                    except OSError:
                        pass
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "notes":
                count = db.query(Note).count()
                db.query(Note).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "tasks":
                # TaskRun rows reference tasks via FK — clear them first.
                db.query(TaskRun).delete()
                count = db.query(ScheduledTask).count()
                db.query(ScheduledTask).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "documents":
                # DocumentVersion FKs Document — clear children first.
                db.query(DocumentVersion).delete()
                count = db.query(Document).count()
                db.query(Document).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "gallery":
                filenames = {
                    str(row[0])
                    for row in db.query(GalleryImage.filename).all()
                    if row[0]
                }
                count = db.query(GalleryImage).count() + db.query(GalleryAlbum).count()
                db.query(GalleryImage).delete()
                db.query(GalleryAlbum).delete()
                db.commit()
                # Metadata proves the exact byte candidates.  Remove them only
                # after the database wipe commits; unknown/legacy files with no
                # row remain quarantined and inaccessible rather than becoming
                # an unbounded recursive-delete target.
                for filename in filenames:
                    try:
                        resolve_gallery_image_path(
                            filename,
                            root=GENERATED_IMAGE_DIR,
                            require_exists=True,
                        ).unlink()
                    except (HTTPException, OSError) as exc:
                        logger.warning(
                            "Could not remove wiped Gallery image %r: %s",
                            filename,
                            exc,
                        )
                # Also drop the upload dir so disk doesn't keep orphans.
                _rmtree_quiet(GALLERY_DIR)
                _rmtree_quiet(GALLERY_UPLOADS_DIR)
                return {"status": "deleted", "kind": kind, "count": count}

            if kind == "calendar":
                # Events FK calendars — clear children first, then both.
                db.query(CalendarEvent).delete()
                count = db.query(CalendarCal).count()
                db.query(CalendarCal).delete()
                db.commit()
                return {"status": "deleted", "kind": kind, "count": count}

            raise HTTPException(400, f"Unknown wipe kind: {kind!r}")
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.exception(f"Wipe {kind} failed")
            raise HTTPException(500, f"Wipe {kind} failed: {e}")
        finally:
            db.close()

    return router
