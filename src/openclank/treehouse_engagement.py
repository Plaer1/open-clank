"""Bounded client engagement observations. Never learning proof or award input."""
from datetime import datetime, timezone, timedelta
import json
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from src.openclank import treehouse_stats
from src.openclank.copal_treehouse_contracts import course_learning_contract
from src.openclank.treehouse_learning_extensions import curriculum_digest
from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError


class EngagementReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receiptId: str = Field(min_length=38, max_length=80, pattern=r"^[a-f0-9-]{36}:[0-9]{1,12}$")
    courseId: str = Field(min_length=1, max_length=160)
    curriculumRevision: int = Field(strict=True, ge=0, le=10**12)
    curriculumDigest: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation: int = Field(strict=True, ge=0, le=10**12)
    durationMs: int = Field(strict=True, ge=1, le=60000)
    intervalMs: int = Field(strict=True, ge=1, le=60000)
    occurredAt: datetime
    policyVersion: Literal["foreground-idle-v1"]


def engagement_curriculum_digest(state, course_id):
    # Reuse the credentials' definition authority while excluding view metadata.
    normalized = dict(state)
    normalized["courses"] = dict(state["courses"])
    normalized["courses"][course_id] = {key: value for key, value in state["courses"][course_id].items()
        if key not in {"curriculumRevision", "engagementCurriculumDigest"}}
    return curriculum_digest(normalized, course_id)


def attach_engagement_context(repo, account_id, workspace_id, snapshot):
    """Issue context from actual owner definitions, never the learner overlay."""
    owners = {}
    for course_id, visible in snapshot.get("state", {}).get("courses", {}).items():
        owner = repo.owner_for_course(account_id, workspace_id, course_id)
        if not owner or not repo.access(account_id, workspace_id, owner, course_id, "learn"):
            continue
        if owner not in owners:
            owners[owner] = repo.get_catalogue(owner, workspace_id)
        source, revision = owners[owner]
        if source and course_id in source.get("courses", {}):
            visible["curriculumRevision"] = revision
            visible["engagementCurriculumDigest"] = engagement_curriculum_digest(source, course_id)
    return snapshot


def record_engagement(repo, account_id, workspace_id, receipt: EngagementReceipt):
    """Auth resolved by router; grant, generation, occurrence and count share lock."""
    if receipt.durationMs > receipt.intervalMs or receipt.occurredAt.tzinfo is None:
        raise TreeHouseRepositoryError("invalid_engagement_interval", "Invalid bounded engagement interval", status=422)
    now = datetime.now(timezone.utc)
    if not now-timedelta(days=7) <= receipt.occurredAt <= now+timedelta(seconds=60):
        raise TreeHouseRepositoryError("expired_engagement_interval", "Engagement observation timestamp is outside the delivery window", status=422)
    with repo.achievement_transaction():
        db = repo._connect()
        if not treehouse_stats.available(db):
            raise TreeHouseRepositoryError("offline_stats_activation_required", "Stats collection is unavailable", status=503)
        owner = repo.owner_for_course(account_id, workspace_id, receipt.courseId)
        if not owner or not repo.access(account_id, workspace_id, owner, receipt.courseId, "learn"):
            raise TreeHouseRepositoryError("forbidden", "Course access is required", status=403)
        state, revision = repo.get_catalogue(owner, workspace_id)
        course = (state or {}).get("courses", {}).get(receipt.courseId)
        if not course or course.get("status") != "published" or course.get("deletedAt"):
            raise TreeHouseRepositoryError("forbidden", "Published course access is required", status=403)
        source_owner = f"treehouse-engagement:{workspace_id}"
        previous = db.execute("SELECT facts_json FROM treehouse_stats_occurrences WHERE account_id=? AND source_owner=? AND source_event_id=?", (account_id, source_owner, receipt.receiptId)).fetchone()
        # Replay uses its frozen source contract even if curriculum/reset moved.
        if previous:
            facts = json.loads(previous[0])
        else:
            generation = repo.progress_reset_epoch(account_id, owner, workspace_id, receipt.courseId)
            if account_id == owner:
                resets = state.get("progressResets", {})
                generation = max(generation, int(resets.get(f"{account_id}:*", 0)), int(resets.get(f"{account_id}:{receipt.courseId}", 0)))
            expected_digest = engagement_curriculum_digest(state, receipt.courseId)
            if expected_digest != receipt.curriculumDigest or generation != receipt.generation:
                raise TreeHouseRepositoryError("stale_engagement_context", "Course definitions or progress generation changed", status=409,
                    details={"expectedCurriculumDigest": expected_digest, "observedCurriculumDigest": receipt.curriculumDigest,
                             "expectedGeneration": generation, "observedGeneration": receipt.generation})
            facts = {**course_learning_contract(state, receipt.courseId), "courseOwnerId": owner, "curriculumRevision": revision}
        facts.update(courseId=receipt.courseId, observedCatalogueRevision=receipt.curriculumRevision, curriculumDigest=receipt.curriculumDigest,
                     generation=receipt.generation, durationMs=receipt.durationMs,
                     intervalMs=receipt.intervalMs, ruleVersion=receipt.policyVersion)
        # At most five minutes per wall minute accepted per account/workspace;
        # this bounds duplicate-tab/forged floods without treating them as scores.
        if not previous:
            admitted = db.execute("SELECT coalesce(sum(json_extract(facts_json,'$.durationMs')),0) FROM treehouse_stats_occurrences WHERE account_id=? AND workspace_id=? AND family='learning.active_time.observed' AND ingested_at>?", (account_id, workspace_id, now.timestamp()-60)).fetchone()[0]
            if int(admitted)+receipt.durationMs > 300000:
                raise TreeHouseRepositoryError("engagement_rate_limit", "Engagement observation delivery is temporarily limited", status=429)
        result = treehouse_stats.capture(db, account_id=account_id, workspace_id=workspace_id,
            source_owner=source_owner, source_event_id=receipt.receiptId, source_revision=receipt.curriculumDigest,
            family="learning.active_time.observed", occurred_at=receipt.occurredAt.isoformat(),
            entity_kind="course", entity_id=receipt.courseId, generation=receipt.generation,
            trust_class="observation", facts=facts)
        return {"ok": True, **result, "trustClass": "observation", "scoring": "none"}


def create_engagement_router(*, scope_for, account_scope, repository):
    router = APIRouter()

    @router.post("/stats/engagement")
    async def post_engagement(request: Request, receipt: EngagementReceipt, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        try:
            return record_engagement(repository(request), scope["account_id"], scope["workspace_id"], receipt)
        except TreeHouseRepositoryError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.details}) from exc

    return router
