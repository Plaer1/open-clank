"""Thin authenticated routes using the existing Treehouse repository and grants."""
from __future__ import annotations

import copy
from typing import Any
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from src.openclank.copal_treehouse import TreeHouseError, compute_treehouse_projections
from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError
from src.openclank.treehouse_learning_extensions import (
    apply_learning_command, course_learning_view, credential_view, learning_data,
    render_credential_html, text,
)


class LearningCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: str = Field(min_length=1, max_length=80)
    command_id: str = Field(alias="commandId", min_length=1, max_length=128)
    expected_revision: int = Field(alias="expectedRevision", ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


def setup_learning_extensions_routes(*, repository, scope_for, account_scope,
                                     aggregate_visible_state, overlay_progress, publish):
    router = APIRouter()

    def context(request, workspace, course_id):
        scope = account_scope(request, scope_for(request, workspace))
        repo = repository(request)
        actor, workspace_id = scope["account_id"], scope["workspace_id"]
        owner = repo.owner_for_course(actor, workspace_id, course_id)
        if not owner or not repo.access(actor, workspace_id, owner, course_id, "learn"):
            raise HTTPException(404, "Learning course unavailable")
        state, revision = repo.get_catalogue(owner, workspace_id)
        course = state.get("courses", {}).get(course_id)
        if not course or course.get("deletedAt") or (actor != owner and course.get("status") != "published"):
            raise HTTPException(404, "Learning course unavailable")
        return scope, repo, owner, state, revision

    def joined(repo, scope, owner, state, course_id):
        value = copy.deepcopy(state)
        if scope["account_id"] != owner:
            progress, _ = repo.get_progress(scope["account_id"], owner, scope["workspace_id"], course_id)
            overlay_progress(value, progress)
        return value

    def error(exc):
        if isinstance(exc, TreeHouseError): return HTTPException(exc.status, detail=exc.payload())
        return HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.details})

    @router.get("/courses/{course_id}/learning")
    async def get_course_learning(course_id: str, request: Request, workspace: str | None = None):
        scope, repo, owner, state, _ = context(request, workspace, course_id)
        return course_learning_view(joined(repo, scope, owner, state, course_id), course_id, scope["account_id"],
                                    can_moderate=repo.access(scope["account_id"], scope["workspace_id"], owner, course_id, "edit"))

    @router.post("/courses/{course_id}/learning")
    async def command_course_learning(course_id: str, command: LearningCommand, request: Request, workspace: str | None = None):
        committed_revision = None
        with repository(request).achievement_transaction():
            scope, repo, owner, state, revision = context(request, workspace, course_id)
            actor = scope["account_id"]
            can_moderate = repo.access(actor, scope["workspace_id"], owner, course_id, "edit")
            grant = repo.active_grant(actor, scope["workspace_id"], owner, course_id) if actor != owner else None
            if actor != owner and not grant: raise HTTPException(404, "Learning course unavailable")
            can_moderate = actor == owner or bool(grant and grant["role"] == "edit")
            try:
                view_state = joined(repo, scope, owner, state, course_id)
                next_view, result = apply_learning_command(view_state, course_id=course_id, actor_id=actor,
                    can_moderate=can_moderate, kind=command.type, payload=command.payload,
                    command_id=command.command_id, expected_revision=command.expected_revision)
                if not result.get("replayed"):
                    # Learner progress overlay must never be persisted into owner curriculum.
                    candidate = copy.deepcopy(state)
                    candidate.setdefault("extensions", {})["learning"] = next_view["extensions"]["learning"]
                    candidate["revision"] = next_view["revision"]
                    candidate["events"] = state["events"] + next_view["events"][len(view_state["events"]):]
                    repo.put_catalogue(owner, scope["workspace_id"], candidate, expected_revision=revision,
                        access_grant_id=grant["grant_id"] if grant else None,
                        access_revision=grant["revision"] if grant else None)
                    committed_revision = candidate["revision"]
            except (TreeHouseError, TreeHouseRepositoryError) as exc: raise error(exc) from exc
        if committed_revision is not None:
            publish(scope, "document", {"treehouse": True, "revision": committed_revision})
        return result

    @router.get("/learning-library")
    async def learning_library(request: Request, workspace: str | None = None, q: str = "", courseType: str = ""):
        if len(q) > 240 or courseType not in {"", "tutorial", "quest"}: raise HTTPException(400, "Invalid learning search")
        scope = account_scope(request, scope_for(request, workspace))
        repo = repository(request)
        actor = scope["account_id"]
        aggregate = aggregate_visible_state(repo, actor, scope["workspace_id"])
        projection = compute_treehouse_projections(aggregate)["learners"].get(actor, {}).get("courses", {})
        courses, credentials, collections, visited_owners = [], [], [], set()
        visible = set(aggregate.get("courses", {}))
        for ref in repo.accessible_course_refs(actor, scope["workspace_id"]):
            course_id, owner = ref["courseId"], ref["ownerAccountId"]
            state, revision = repo.get_catalogue(owner, scope["workspace_id"])
            course = aggregate.get("courses", {}).get(course_id)
            if not state or not course or course.get("deletedAt"): continue
            progress = projection.get(course_id, {})
            # Search curriculum already admitted by aggregate_visible_state; never answers/grades.
            haystack = " ".join(str(course.get(key, "")) for key in ("title", "description", "tags"))
            haystack += " " + " ".join(str(item.get(key, "")) for kind in ("activities", "assignments")
                for item in aggregate.get(kind, {}).values() if item.get("courseId") == course_id
                for key in ("title", "content", "instructions"))
            if (not q or q.casefold() in haystack.casefold()) and (not courseType or progress.get("courseType") == courseType):
                courses.append({"id": course_id, "title": course.get("title"), "description": course.get("description", ""),
                                "ownerId": owner, "courseType": progress.get("courseType"), "progress": progress,
                                "canEdit": repo.access(actor, scope["workspace_id"], owner, course_id, "edit")})
            for record in learning_data(state).get("courses", {}).get(course_id, {}).get("credentials", []):
                if record.get("learnerId") == actor: credentials.append(credential_view(record, joined(repo, scope, owner, state, course_id), actor))
            if owner in visited_owners: continue
            visited_owners.add(owner)
            for collection in learning_data(state).get("collections", {}).values():
                if collection.get("archivedAt"): continue
                ids = [key for key in collection.get("courseIds", []) if key in visible]
                # Entirely inaccessible collection names are never disclosed.
                if ids or owner == actor: collections.append({**collection, "courseIds": ids, "canEdit": owner == actor})
        own, own_revision = repo.get_catalogue(actor, scope["workspace_id"])
        return {"schemaVersion": 1, "courses": courses, "collections": collections,
                "credentials": credentials, "ownerRevision": own_revision, "searchScope": "accessible-curriculum"}

    @router.post("/collections")
    async def write_collection(command: LearningCommand, request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        repo, actor = repository(request), scope["account_id"]
        state, revision = repo.get_catalogue(actor, scope["workspace_id"])
        if state is None: raise HTTPException(409, "Open Treehouse once before creating collections")
        if revision != command.expected_revision: raise HTTPException(409, detail={"code": "stale", "message": "Library changed; reload before saving"})
        payload = command.payload
        if command.type not in {"collection.save", "collection.archive"} or set(payload) - {"id", "title", "description", "courseIds"}: raise HTTPException(400, "Invalid collection command")
        try:
            from src.openclank.treehouse_learning_extensions import digest
            from datetime import datetime, UTC
            ident = text(payload.get("id") or "collection_" + digest([actor, command.command_id])[:24], "Collection ID", 128)
            candidate = copy.deepcopy(state)
            collections = candidate.setdefault("extensions", {}).setdefault("learning", {}).setdefault("collections", {})
            if command.type == "collection.archive":
                if ident not in collections: raise HTTPException(404, "Collection unavailable")
                collections[ident]["archivedAt"] = datetime.now(UTC).isoformat()
            else:
                title = text(payload.get("title"), "Collection title", 240)
                description = text(payload.get("description", ""), "Collection description", 2000, empty=True)
                ids = payload.get("courseIds", [])
                if not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(key, str) or key not in state.get("courses", {}) or state["courses"][key].get("deletedAt") for key in ids) or len(set(ids)) != len(ids):
                    raise HTTPException(400, "Collections contain unique accessible owner course IDs")
                if len(collections) >= 500 and ident not in collections: raise HTTPException(409, "Collection capacity reached")
                collections[ident] = {"id": ident, "title": title, "description": description, "courseIds": ids, "ownerId": actor}
            if len(candidate.get("events", [])) >= 50000: raise HTTPException(409, "Treehouse event capacity reached")
            now = datetime.now(UTC).isoformat()
            candidate.setdefault("events", []).append({"id": "collection_event_" + digest([actor, command.command_id])[:24],
                "type": "learning.collection.archived" if command.type == "collection.archive" else "learning.collection.saved",
                "actorId": actor, "subjectId": actor, "entityType": "collection", "entityId": ident, "at": now,
                "data": {"count": 1, "kind": command.type}})
            candidate["revision"] = revision + 1
            repo.put_catalogue(actor, scope["workspace_id"], candidate, expected_revision=revision)
            publish(scope, "document", {"treehouse": True, "revision": candidate["revision"]})
            return {"collectionId": ident, "revision": candidate["revision"]}
        except (TreeHouseError, TreeHouseRepositoryError) as exc: raise error(exc) from exc

    @router.get("/courses/{course_id}/credentials/{credential_id}/export")
    async def export_credential(course_id: str, credential_id: str, request: Request, workspace: str | None = None, format: str = "html"):
        scope, repo, owner, state, _ = context(request, workspace, course_id)
        record = next((value for value in learning_data(state).get("courses", {}).get(course_id, {}).get("credentials", [])
                       if value["id"] == credential_id and value.get("learnerId") == scope["account_id"]), None)
        if not record: raise HTTPException(404, "Completion record unavailable")
        value = credential_view(record, joined(repo, scope, owner, state, course_id), scope["account_id"])
        if format == "json": return value
        if format != "html": raise HTTPException(400, "Export format must be html or json")
        return Response(render_credential_html(value), media_type="text/html", headers={"Content-Disposition": f'attachment; filename="treehouse-{credential_id}.html"', "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    return router
