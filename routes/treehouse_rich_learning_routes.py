"""Course-scoped podcast and personal media resume adapters."""
from __future__ import annotations
import copy
from datetime import datetime, UTC
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from src.openclank.copal_treehouse import TreeHouseError
from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError
from src.openclank.treehouse_rich_learning import FORMAT_SUPPORT, validate_podcast, validate_position


class PodcastWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_revision: int = Field(alias="expectedRevision", ge=0)
    podcast: dict


class PlaybackWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_revision: int = Field(alias="expectedRevision", ge=0)
    position: float = Field(ge=0, le=864000)


def setup_rich_learning_routes(*, repository, scope_for, account_scope, aggregate_visible_state, publish):
    router = APIRouter()

    def context(request, workspace, course_id):
        scope = account_scope(request, scope_for(request, workspace))
        repo = repository(request)
        actor, ws = scope["account_id"], scope["workspace_id"]
        owner = repo.owner_for_course(actor, ws, course_id)
        if not owner or not repo.access(actor, ws, owner, course_id, "learn"): raise HTTPException(404, "Course unavailable")
        state, revision = repo.get_catalogue(owner, ws)
        course = (state or {}).get("courses", {}).get(course_id)
        if not course or course.get("deletedAt") or (owner != actor and course.get("status") != "published"): raise HTTPException(404, "Course unavailable")
        grant = repo.active_grant(actor, ws, owner, course_id) if owner != actor else None
        if owner != actor and not grant: raise HTTPException(404, "Course unavailable")
        return scope, repo, owner, state, revision, grant

    @router.get("/rich-learning-support")
    async def support(request: Request, workspace: str | None = None):
        account_scope(request, scope_for(request, workspace))
        return {"formats": FORMAT_SUPPORT}

    @router.get("/podcasts")
    async def podcasts(request: Request, workspace: str | None = None, q: str = ""):
        if len(q) > 240: raise HTTPException(400, "Search is too long")
        scope = account_scope(request, scope_for(request, workspace))
        repo, actor = repository(request), scope["account_id"]
        visible = aggregate_visible_state(repo, actor, scope["workspace_id"])
        rows = []
        for ref in repo.accessible_course_refs(actor, scope["workspace_id"]):
            cid = ref["courseId"]
            if cid not in visible.get("courses", {}): continue
            _, _, owner, state, revision, _ = context(request, workspace, cid)
            podcast = state.get("extensions", {}).get("richLearning", {}).get("podcasts", {}).get(cid)
            can_edit = repo.access(actor, scope["workspace_id"], owner, cid, "edit")
            if not podcast or (not podcast.get("published") and not can_edit): continue
            admitted = copy.deepcopy(podcast)
            admitted["episodes"] = [ep for ep in podcast["episodes"] if not state.get("activities", {}).get(ep["activityId"], {}).get("deletedAt") and (state.get("activities", {}).get(ep["activityId"], {}).get("status") == "published" or can_edit)]
            if q and q.casefold() not in " ".join([podcast["title"], podcast["description"], *[ep["title"] for ep in admitted["episodes"]]]).casefold(): continue
            rows.append({**admitted, "courseId": cid, "revision": revision, "canEdit": can_edit})
        return {"schemaVersion": 1, "podcasts": rows}

    @router.get("/courses/{course_id}/podcast")
    async def podcast(course_id: str, request: Request, workspace: str | None = None):
        scope, repo, owner, state, revision, _ = context(request, workspace, course_id)
        can_edit = repo.access(scope["account_id"], scope["workspace_id"], owner, course_id, "edit")
        value = state.get("extensions", {}).get("richLearning", {}).get("podcasts", {}).get(course_id)
        if value and not value.get("published") and not can_edit: value = None
        if value:
            value = copy.deepcopy(value)
            value["episodes"] = [ep for ep in value["episodes"] if not state.get("activities", {}).get(ep["activityId"], {}).get("deletedAt") and (state.get("activities", {}).get(ep["activityId"], {}).get("status") == "published" or can_edit)]
        return {"podcast": copy.deepcopy(value), "revision": revision, "canEdit": can_edit}

    @router.put("/courses/{course_id}/podcast")
    async def save_podcast(course_id: str, body: PodcastWrite, request: Request, workspace: str | None = None):
        try:
            with repository(request).achievement_transaction():
                scope, repo, owner, state, revision, grant = context(request, workspace, course_id)
                if not repo.access(scope["account_id"], scope["workspace_id"], owner, course_id, "edit"): raise HTTPException(403, "Author access required")
                if revision != body.expected_revision: raise HTTPException(409, "Podcast changed; reload before saving")
                value = validate_podcast(body.podcast, state, course_id)
                candidate = copy.deepcopy(state)
                candidate.setdefault("extensions", {}).setdefault("richLearning", {}).setdefault("podcasts", {})[course_id] = value
                candidate["revision"] = revision + 1
                repo.put_catalogue(owner, scope["workspace_id"], candidate, expected_revision=revision,
                    access_grant_id=grant["grant_id"] if grant else None, access_revision=grant["revision"] if grant else None)
            publish(scope, "document", {"treehouse": True, "revision": revision + 1})
            return {"podcast": value, "revision": revision + 1}
        except (TreeHouseError, TreeHouseRepositoryError) as exc: raise HTTPException(exc.status, str(exc)) from exc

    def activity_context(request, workspace, course_id, activity_id):
        data = context(request, workspace, course_id)
        scope, repo, owner, state, revision, grant = data
        activity = state.get("activities", {}).get(activity_id, {})
        if activity.get("courseId") != course_id or activity.get("deletedAt") or activity.get("activityType") not in {"audio", "video"} or (activity.get("status") != "published" and not repo.access(scope["account_id"], scope["workspace_id"], owner, course_id, "edit")):
            raise HTTPException(404, "Media activity unavailable")
        return data

    @router.get("/courses/{course_id}/activities/{activity_id}/playback")
    async def playback(course_id: str, activity_id: str, request: Request, workspace: str | None = None):
        scope, repo, owner, _, _, _ = activity_context(request, workspace, course_id, activity_id)
        progress, revision = repo.get_progress(scope["account_id"], owner, scope["workspace_id"], course_id)
        value = (progress or {}).get("richLearning", {}).get("playback", {}).get(activity_id, {})
        return {"position": value.get("position", 0), "updatedAt": value.get("updatedAt"), "revision": revision}

    @router.put("/courses/{course_id}/activities/{activity_id}/playback")
    async def save_playback(course_id: str, activity_id: str, body: PlaybackWrite, request: Request, workspace: str | None = None):
        try:
            with repository(request).achievement_transaction():
                scope, repo, owner, _, _, grant = activity_context(request, workspace, course_id, activity_id)
                actor, ws = scope["account_id"], scope["workspace_id"]
                progress, revision = repo.get_progress(actor, owner, ws, course_id)
                if revision != body.expected_revision: raise HTTPException(409, "Playback changed in another session; reopen before saving")
                candidate = copy.deepcopy(progress or {})
                value = {"position": validate_position(body.position), "updatedAt": datetime.now(UTC).isoformat()}
                candidate.setdefault("richLearning", {}).setdefault("playback", {})[activity_id] = value
                epoch = repo.progress_reset_epoch(actor, owner, ws, course_id)
                result = repo.put_progress(actor, owner, ws, course_id, candidate, expected_revision=revision,
                    reset_epoch=epoch, expected_reset_epoch=epoch, grant_id=grant["grant_id"] if grant else None,
                    access_revision=grant["revision"] if grant else None)
            return {**value, "revision": result}
        except (TreeHouseError, TreeHouseRepositoryError) as exc: raise HTTPException(exc.status, str(exc)) from exc
    return router
