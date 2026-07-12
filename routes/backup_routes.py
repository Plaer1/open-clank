"""Backup routes — export/import user data (memories, presets, settings, skills, preferences)."""

import json
import hashlib
import logging
import os
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from core.middleware import require_admin
from src.auth_helpers import get_current_user
from src.settings import load_settings, save_settings, load_features, save_features

logger = logging.getLogger(__name__)

_SECRET_EXPORT_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|credential|credentials|api_key|private_key|access_key|token)(?:$|_)",
    re.IGNORECASE,
)


def _sanitize_export(value, removed: list[str], path: str = ""):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            key_path = f"{path}.{key_text}" if path else key_text
            if _SECRET_EXPORT_KEY.search(key_text) or (
                key_text.lower().endswith("_key") and isinstance(item, str)
            ):
                removed.append(key_path)
                continue
            clean[key] = _sanitize_export(item, removed, key_path)
        return clean
    if isinstance(value, list):
        return [_sanitize_export(item, removed, f"{path}[]") for item in value]
    return value


def setup_backup_routes(memory_manager, preset_manager, skills_manager, memory_provider=None) -> APIRouter:
    router = APIRouter(tags=["backup"])

    @router.get("/api/export")
    async def export_data(request: Request):
        """Export all user data as a downloadable JSON file."""
        require_admin(request)
        user = get_current_user(request)
        memory_owner = user or os.environ.get("ODYSSEUS_MEMORY_OWNER") or "legacy"

        # Memories (filtered by owner when auth is enabled)
        if memory_provider:
            memories = []
            cursor = None
            while True:
                page, cursor = await memory_provider.list_page(
                    owner=memory_owner, limit=1000, cursor=cursor
                )
                memories.extend({
                    "id": record.id,
                    "text": record.text,
                    "category": record.category,
                    "source": record.source,
                    "owner": record.owner,
                    "session_id": record.session_id,
                    "pinned": record.pinned,
                    "metadata": record.metadata,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                } for record in page)
                if cursor is None:
                    break
            memory_authority = getattr(memory_provider, "provider_id", "unknown")
        else:
            memories = memory_manager.load(owner=user)
            memory_authority = "native"

        # Presets (shared across users — export all)
        presets = preset_manager.get_all()

        # Skills (filtered by owner when auth is enabled)
        skills = skills_manager.load(owner=user)

        # Settings
        settings = load_settings()

        # Feature flags
        features = load_features()

        # User preferences
        from routes.prefs_routes import _load_for_user
        preferences = _load_for_user(user)

        removed_secret_fields = []
        memories = _sanitize_export(memories, removed_secret_fields, "memories")
        presets = _sanitize_export(presets, removed_secret_fields, "presets")
        skills = _sanitize_export(skills, removed_secret_fields, "skills")
        settings = _sanitize_export(settings, removed_secret_fields, "settings")
        features = _sanitize_export(features, removed_secret_fields, "features")
        preferences = _sanitize_export(preferences, removed_secret_fields, "preferences")

        export_data = {
            "version": 2,
            "exported_at": datetime.now().isoformat(),
            "exported_by": user,
            "memories": memories,
            "manifest": {
                "owner": memory_owner,
                "memory_authority": memory_authority,
                "memory_count": len(memories),
                "memory_sha256": hashlib.sha256(
                    json.dumps(memories, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "secrets_included": False,
                "excluded_secret_fields": sorted(set(removed_secret_fields)),
            },
            "presets": presets,
            "skills": skills,
            "settings": settings,
            "features": features,
            "preferences": preferences,
        }

        filename = f"odysseus_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            content=json.dumps(export_data, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @router.post("/api/import")
    async def import_data(request: Request):
        """Import user data from a previously exported JSON file. Merges with existing data."""
        require_admin(request)
        user = get_current_user(request)
        memory_owner = user or os.environ.get("ODYSSEUS_MEMORY_OWNER") or "legacy"
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")

        manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else {}
        expected_memory_hash = manifest.get("memory_sha256")
        if expected_memory_hash and isinstance(body.get("memories"), list):
            actual_memory_hash = hashlib.sha256(
                json.dumps(body["memories"], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if actual_memory_hash != expected_memory_hash:
                raise HTTPException(400, "Backup memory checksum mismatch")

        imported = []

        # ── Memories ──
        if "memories" in body and isinstance(body["memories"], list):
            if memory_provider:
                existing = []
                cursor = None
                while True:
                    page, cursor = await memory_provider.list_page(
                        owner=memory_owner, limit=1000, cursor=cursor
                    )
                    existing.extend(page)
                    if cursor is None:
                        break
                existing_texts = {record.text.strip().lower() for record in existing}
                added = 0
                for mem in body["memories"]:
                    if not isinstance(mem, dict) or not str(mem.get("text") or "").strip():
                        continue
                    text = str(mem["text"]).strip()
                    if text.lower() in existing_texts:
                        continue
                    metadata = dict(mem.get("metadata") or {})
                    metadata["restored_from_backup"] = True
                    metadata["original_created_at"] = mem.get("created_at")
                    record = await memory_provider.remember(
                        text,
                        owner=memory_owner,
                        session_id=mem.get("session_id"),
                        category=mem.get("category") or "fact",
                        source=mem.get("source") or "backup_restore",
                        metadata=metadata,
                    )
                    if mem.get("pinned"):
                        await memory_provider.pin(record.id, True, owner=memory_owner)
                    existing_texts.add(text.lower())
                    added += 1
                imported.append(f"{added} memories")
            else:
                existing = memory_manager.load_all()
            # Dedup against THIS user's own memories only. Using every tenant's
            # rows (load_all) meant a memory whose text matched any other
            # user's was silently skipped, so the importing user lost their own
            # data. The full store is still saved back below.
                existing_texts = {e.get("text", "").strip().lower()
                                  for e in existing if e.get("owner") == user}
                added = 0
                for mem in body["memories"]:
                    if not isinstance(mem, dict) or not mem.get("text"):
                        continue
                    if mem["text"].strip().lower() in existing_texts:
                        continue  # skip duplicates
                    mem = dict(mem)
                    mem["owner"] = user
                    existing.append(mem)
                    existing_texts.add(mem["text"].strip().lower())
                    added += 1
                memory_manager.save(existing)
                imported.append(f"{added} memories")

        # ── Skills ──
        if "skills" in body and isinstance(body["skills"], list):
            existing = skills_manager.load_all()
            # Dedup against THIS user's own skills only. Using every tenant's
            # rows (load_all) meant a skill whose id/name/title matched any
            # other user's was silently skipped, so the importing user lost
            # their own data — same cross-tenant bug fixed for memories above.
            # The full store is still saved back below.
            own = [s for s in existing if s.get("owner") == user]
            existing_names = {s.get("name") for s in own if s.get("name")}
            existing_ids = {s.get("id") for s in own if s.get("id")}
            existing_titles = {
                (s.get("title") or s.get("description") or "").strip().lower()
                for s in own
            }
            added = 0
            for skill in body["skills"]:
                if not isinstance(skill, dict):
                    continue
                title = (
                    skill.get("title") or skill.get("description")
                    or skill.get("name") or ""
                ).strip()
                if not title:
                    continue
                sid = skill.get("id") or skill.get("name")
                if sid and sid in existing_ids:
                    continue
                nm = skill.get("name")
                if nm and nm in existing_names:
                    continue
                if title.lower() in existing_titles:
                    continue
                owner = skill.get("owner")
                if user and not owner:
                    owner = user
                # Skills live on disk as SKILL.md files; the old JSON-era
                # skills_manager.save() no longer exists. Write each new skill
                # via add_skill (source="user" skips auto-dedup — this is an
                # explicit backup restore).
                result = skills_manager.add_skill(
                    title=title,
                    name=skill.get("name"),
                    description=skill.get("description"),
                    problem=skill.get("problem", ""),
                    solution=skill.get("solution", ""),
                    steps=skill.get("steps"),
                    tags=skill.get("tags"),
                    source="user",
                    teacher_model=skill.get("teacher_model"),
                    confidence=skill.get("confidence", 0.8),
                    owner=owner,
                    category=skill.get("category", "general"),
                    when_to_use=skill.get("when_to_use"),
                    procedure=skill.get("procedure"),
                    pitfalls=skill.get("pitfalls"),
                    verification=skill.get("verification"),
                    platforms=skill.get("platforms"),
                    requires_toolsets=skill.get("requires_toolsets"),
                    fallback_for_toolsets=skill.get("fallback_for_toolsets"),
                    status=skill.get("status", "draft"),
                    version=skill.get("version", "1.0.0"),
                )
                if result.get("_deduped"):
                    continue
                if result.get("name"):
                    existing_names.add(result["name"])
                if result.get("id"):
                    existing_ids.add(result["id"])
                existing_titles.add(title.lower())
                added += 1
            imported.append(f"{added} skills")

        # ── Presets ──
        if "presets" in body and isinstance(body["presets"], dict):
            current = preset_manager.get_all()
            for key, value in body["presets"].items():
                if isinstance(value, dict):
                    current[key] = value
                elif isinstance(value, list):
                    current[key] = value
            preset_manager.save(current)
            imported.append("presets")

        # ── Settings ──
        if "settings" in body and isinstance(body["settings"], dict):
            current = load_settings()
            current.update(body["settings"])
            save_settings(current)
            imported.append("settings")

        # ── Features ──
        if "features" in body and isinstance(body["features"], dict):
            current = load_features()
            current.update(body["features"])
            save_features(current)
            imported.append("features")

        # ── Preferences ──
        if "preferences" in body and isinstance(body["preferences"], dict):
            from routes.prefs_routes import _load_for_user, _save_for_user
            current = _load_for_user(user)
            current.update(body["preferences"])
            _save_for_user(user, current)
            imported.append("preferences")

        if not imported:
            return {"ok": False, "message": "No recognized data found in the file"}

        return {"ok": True, "imported": imported, "message": f"Imported: {', '.join(imported)}"}

    return router
