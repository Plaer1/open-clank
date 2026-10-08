"""Authenticated TreeHouse API mounted inside Copal's existing route adapter."""

from __future__ import annotations

import json
import hashlib
import copy
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.openclank.copal_treehouse import (
    TreeHouseError,
    apply_treehouse_command,
    compute_treehouse_projections,
    new_treehouse_state,
    public_treehouse_snapshot,
    state_fingerprint,
    validate_treehouse_state,
)
from src.openclank.copal_treehouse_repository import TreeHouseRepository, TreeHouseRepositoryError
from src.openclank.treehouse_achievements import TreeHouseAchievementEngine
from src.openclank.treehouse_field_guide import (
    FIELD_GUIDE_TEMPLATE_KEY,
    FIELD_GUIDE_TEMPLATE_VERSION,
    instantiate_field_guide,
    reconcile_field_guide_completion,
)
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.files_facade import FilesFacadeError, ProviderContext
from src.openclank.treehouse_assessment import builtin_activity_verification
from src.openclank.treehouse_engagement import create_engagement_router, attach_engagement_context
from routes.treehouse_learning_extensions_routes import setup_learning_extensions_routes
from routes.treehouse_rich_learning_routes import setup_rich_learning_routes
from src.constants import DATA_DIR
from src.auth_helpers import require_user
from src.openclank.achievement_producers import record_account_activity, record_journal_activity, reset_account_activity


Call = Callable[[Request, str, dict[str, Any]], Awaitable[Any]]
Scope = Callable[[Request, str | None], dict[str, str]]
Publish = Callable[[dict[str, str], str, dict[str, Any]], None]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TreeHouseCommand(_Strict):
    type: str = Field(min_length=1, max_length=80)
    command_id: str = Field(alias="commandId", min_length=1, max_length=128)
    actor_id: str = Field(alias="actorId", default="owner", min_length=1, max_length=128)
    expected_revision: int | None = Field(alias="expectedRevision", default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)




def setup_treehouse_routes(
    *,
    call: Call,
    scope_for: Scope,
    publish: Publish,
    policy_repository: FilePolicyRepository | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/treehouse", tags=["copal-treehouse"])
    state_name = ".copal/treehouse-state.json"

    def fail(exc: TreeHouseError) -> HTTPException:
        return HTTPException(exc.status, detail=exc.payload())

    def actor_for(request: Request, scope: dict[str, str], requested: str | None) -> str:
        """Resolve the domain actor from server scope.

        Authenticated requests always use the account's aggregate owner
        profile.  The requested actor remains accepted only for the explicit
        unauthenticated local fixture mode, where there is no account boundary
        to forge and legacy profile tests still exercise role filtering.
        """
        account_id = str(scope.get("account_id") or "")
        if not account_id or account_id == "local-installation":
            raise HTTPException(401, "Authenticated account required")
        return account_id

    def repository(request: Request) -> TreeHouseRepository:
        current = getattr(request.app.state, "treehouse_repository", None)
        if current is None:
            configured = os.environ.get("TREEHOUSE_REPOSITORY_PATH")
            path = Path(configured) if configured else Path(DATA_DIR) / "treehouse.sqlite3"
            current = TreeHouseRepository(path)
            request.app.state.treehouse_repository = current
        return current

    def file_policy(request: Request) -> FilePolicyRepository:
        """Return the application-scoped Files policy authority.

        TreeHouse preparation and final CAS must observe the same policy
        generation as ``/api/files-v1``. A route-local fallback could allow a
        non-default Files repository to prepare successfully while TreeHouse
        compared the receipt against a different authority, so missing
        registration is an explicit service-unavailable failure.
        """
        current = policy_repository or getattr(request.app.state, "files_policy_repository", None)
        if not isinstance(current, FilePolicyRepository):
            raise HTTPException(
                503,
                detail={
                    "code": "files_policy_unavailable",
                    "message": "TreeHouse attachment policy authority is not registered",
                },
            )
        return current

    def account_scoped(request: Request, scope: dict[str, str]) -> bool:
        return bool(scope.get("account_id") and scope.get("account_id") != "local-installation")

    def account_scope(request: Request, scope: dict[str, str]) -> dict[str, str]:
        username = require_user(request)
        manager = getattr(request.app.state, "auth_manager", None)
        account_id = manager.account_id(username) if manager is not None and hasattr(manager, "account_id") else None
        if not account_id:
            raise HTTPException(409, detail={"code": "missing_account_identity", "message": "Authenticated account has no immutable TreeHouse identity"})
        return {**scope, "account_id": str(account_id)}

    def repository_error(exc: TreeHouseRepositoryError) -> HTTPException:
        return HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.details})

    def ensure_account_profile(state: dict[str, Any], account_id: str) -> None:
        """Give every authenticated account its own author/learner profile."""
        if account_id not in state["profiles"]:
            owner = state["profiles"].get("owner") or {}
            state["profiles"][account_id] = {
                "id": account_id,
                "displayName": account_id,
                "roles": ["admin", "instructor", "learner"],
                "active": True,
                "createdAt": owner.get("createdAt"),
            }

    def resolve_recipient_account(request: Request, raw: str, owner_account_id: str) -> str | None:
        """Resolve a human username or immutable account ID to the grant subject."""
        candidate = str(raw or "").strip()
        if not candidate:
            return None
        manager = getattr(request.app.state, "auth_manager", None)
        if manager is None or not getattr(manager, "is_configured", False):
            return candidate if candidate != owner_account_id else None
        if candidate == owner_account_id:
            return None
        if hasattr(manager, "username_for_account_id") and manager.username_for_account_id(candidate):
            return candidate
        if hasattr(manager, "account_id"):
            account_id = manager.account_id(candidate)
            if account_id and account_id != owner_account_id:
                return str(account_id)
        return None

    def recipient_options(request: Request, owner_account_id: str) -> list[dict[str, str]]:
        manager = getattr(request.app.state, "auth_manager", None)
        if manager is None or not getattr(manager, "is_configured", False) or not hasattr(manager, "list_users"):
            return []
        options = []
        for user in manager.list_users():
            account_id = str(user.get("account_id") or "")
            username = str(user.get("username") or "")
            if account_id and account_id != owner_account_id and username:
                options.append({"accountId": account_id, "username": username, "label": username})
        return options

    def ensure_account_catalogue(repo: TreeHouseRepository, account_id: str, workspace: str) -> tuple[dict[str, Any], int]:
        """Create a fresh private Field Guide or validate its current version."""
        state, revision = repo.get_catalogue(account_id, workspace)
        if state is not None:
            installed = state.get("fieldGuide") or {}
            if (
                installed.get("templateKey") == FIELD_GUIDE_TEMPLATE_KEY
                and installed.get("templateVersion") != FIELD_GUIDE_TEMPLATE_VERSION
            ):
                raise HTTPException(409, detail={"code": "manual_migration_required", "message": "Legacy Field Guide requires .clanker/tools/migrations/python/secondary.py copal-field-guide"})
            reconciled = reconcile_field_guide_completion(state)
            if reconciled is not state:
                revision = repo.put_catalogue(account_id, workspace, reconciled, expected_revision=revision)
                state = reconciled
            return state, revision
        candidate = instantiate_field_guide(new_treehouse_state(account_id), account_id)
        winner, winner_revision, _created = repo.create_catalogue_if_absent(account_id, workspace, candidate)
        return winner, winner_revision

    _PROGRESS_FIELDS = ("profiles", "enrollments", "submissions", "evidence", "events", "processedCommands", "progressResets")
    _PROGRESS_COMMANDS = {"course.open", "enrollment.enroll", "enrollment.unenroll", "enrollment.drop", "activity.complete", "submission.submit", "submission.draft", "evidence.submit", "progress.reset"}
    _PORTABLE_DEFINITION_KINDS = ("courses", "modules", "activities", "assignments", "skills", "badges", "quests")
    _PORTABILITY_FORBIDDEN_KEYS = frozenset({
        "profiles", "enrollments", "submissions", "evidence", "events", "processedCommands",
        "progressResets", "courseGrants", "shareToken", "tokenHash", "grantId", "grant_id",
        "learnerId", "profileId", "submissionId", "awardId", "accessRevision",
        "sourceAttachments", "preparationReceiptId", "operationId", "captionOperationId",
        "resource_ref", "resourceRef", "resource_key", "resourceKey", "richLearning",
    })

    def portable_definition(value: Any) -> Any:
        """Copy curriculum data while rejecting learner and authority state."""
        if isinstance(value, dict):
            return {
                str(key): portable_definition(item)
                for key, item in value.items()
                if str(key) not in _PORTABILITY_FORBIDDEN_KEYS
                and str(key) not in {"ownerId", "authorIds", "createdBy", "updatedBy"}
            }
        if isinstance(value, list):
            return [portable_definition(item) for item in value]
        return copy.deepcopy(value)

    def reject_progress_payload(value: Any, path: str = "package") -> None:
        if isinstance(value, dict):
            forbidden = sorted(set(value).intersection(_PORTABILITY_FORBIDDEN_KEYS | {"ownerId", "authorIds", "createdBy", "updatedBy"}))
            if forbidden:
                raise HTTPException(400, detail={"code": "course_package_contains_private_state", "message": f"Course package contains private or authority field at {path}: {', '.join(forbidden)}"})
            for key, item in value.items():
                reject_progress_payload(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                reject_progress_payload(item, f"{path}[{index}]")

    def remap_definition(kind: str, value: Any, mapping: dict[str, dict[str, str]]) -> Any:
        """Remap only schema references; human text may equal an entity ID."""
        record = copy.deepcopy(value)
        if not isinstance(record, dict):
            return record
        def remap_list(field: str, ref_kind: str) -> None:
            if field in record and isinstance(record[field], list):
                record[field] = [mapping[ref_kind].get(str(item), str(item)) for item in record[field]]
        def remap_field(field: str, ref_kind: str) -> None:
            if field in record and record[field] is not None:
                record[field] = mapping[ref_kind].get(str(record[field]), str(record[field]))
        if kind == "courses":
            remap_list("moduleIds", "modules"); remap_list("prerequisites", "courses")
            if isinstance(record.get("completionCriteria"), dict):
                criteria = copy.deepcopy(record["completionCriteria"])
                for field, ref_kind in (("activityIds", "activities"), ("assignmentIds", "assignments")):
                    if isinstance(criteria.get(field), list):
                        criteria[field] = [mapping[ref_kind].get(str(item), str(item)) for item in criteria[field]]
                record["completionCriteria"] = criteria
        elif kind == "modules":
            remap_field("courseId", "courses"); remap_list("activityIds", "activities"); remap_list("assignmentIds", "assignments")
        elif kind in {"activities", "assignments"}:
            remap_field("courseId", "courses"); remap_field("moduleId", "modules"); remap_list("skillIds", "skills")
        elif kind == "skills":
            remap_list("prerequisiteIds", "skills")
        elif kind == "quests":
            remap_list("activityIds", "activities"); remap_list("assignmentIds", "assignments")
        elif kind == "badges":
            criteria = record.get("criteria")
            if isinstance(criteria, dict):
                criteria = copy.deepcopy(criteria)
                criteria_kind = {"course": "courses", "skill": "skills", "quest": "quests"}.get(str(criteria.get("type")))
                if criteria_kind and criteria.get(f"{criteria.get('type')}Id") is not None:
                    field = f"{criteria.get('type')}Id"
                    criteria[field] = mapping[criteria_kind].get(str(criteria[field]), str(criteria[field]))
                record["criteria"] = criteria
        return record

    def course_package_digest(package: dict[str, Any]) -> str:
        # Storage revisions describe the owner's catalogue, not curriculum
        # identity.  Keeping it outside the semantic digest makes re-export
        # stable across unrelated edits to that catalogue.
        unsigned = {key: value for key, value in package.items() if key not in {"packageId", "sourceRevision"}}
        return hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def overlay_progress(state: dict[str, Any], progress: dict[str, Any] | None) -> None:
        if not progress:
            return
        for key in _PROGRESS_FIELDS:
            if key in progress:
                if isinstance(progress[key], dict):
                    state[key] = {**state.get(key, {}), **progress[key]}
                elif key == "events":
                    state[key] = [*state.get(key, []), *progress[key]]
                else:
                    state[key] = progress[key]

        if progress.get("richLearning"):
            state.setdefault("richLearning", {}).update(copy.deepcopy(progress["richLearning"]))

    def progress_projection(state: dict[str, Any], account_id: str) -> dict[str, Any]:
        result = {"schemaVersion": state.get("schemaVersion"), "revision": state.get("revision", 0), "profiles": {account_id: state.get("profiles", {}).get(account_id, {})}, "enrollments": {}, "submissions": {}, "evidence": {}, "events": [], "processedCommands": {}, "progressResets": {}}
        for key in ("enrollments", "submissions", "evidence"):
            for ident, value in state.get(key, {}).items():
                if value.get("learnerId") == account_id or value.get("profileId") == account_id or value.get("actorId") == account_id:
                    result[key][ident] = value
        result["events"] = [
            event for event in state.get("events", [])
            if event.get("actorId") == account_id
            or event.get("subjectId") == account_id
            or event.get("profileId") == account_id
        ]
        result["processedCommands"] = {
            key: value for key, value in state.get("processedCommands", {}).items()
            if value.get("actorId") == account_id
        }
        if state.get("richLearning"):
            result["richLearning"] = copy.deepcopy(state["richLearning"])
        result["progressResets"] = {key: value for key, value in state.get("progressResets", {}).items() if key.startswith(account_id + ":") or key == account_id}
        return result

    def merge_grants(state: dict[str, Any], repo: TreeHouseRepository, account_id: str, workspace: str) -> None:
        """Project the global grant index into the domain compatibility view."""
        state.setdefault("courseGrants", {})
        for ref in repo.accessible_course_refs(account_id, workspace):
            if ref["ownerAccountId"] == account_id:
                continue
            owner_state, _ = repo.get_catalogue(ref["ownerAccountId"], workspace)
            if not owner_state:
                continue
            projected = None
            for grant in owner_state.get("courseGrants", {}).values():
                if grant.get("courseId") == ref["courseId"] and grant.get("recipientId") == account_id:
                    durable = repo.active_grant(account_id, workspace, ref["ownerAccountId"], ref["courseId"])
                    projected = copy.deepcopy(grant)
                    if durable:
                        projected.update({"grantId": durable["grant_id"], "id": durable["grant_id"], "acceptedAt": durable["accepted_at"], "revokedAt": durable["revoked_at"], "capability": durable["role"], "revision": durable["revision"]})
                    state["courseGrants"][projected.get("grantId") or projected.get("id") or f"{ref['ownerAccountId']}:{ref['courseId']}"] = projected
                    break
            if projected is None:
                durable = repo.active_grant(account_id, workspace, ref["ownerAccountId"], ref["courseId"])
                if durable:
                    state["courseGrants"][durable["grant_id"]] = {
                        "id": durable["grant_id"], "grantId": durable["grant_id"],
                        "ownerId": ref["ownerAccountId"], "ownerAccountId": ref["ownerAccountId"],
                        "recipientId": account_id, "courseId": ref["courseId"],
                        "capability": durable["role"], "acceptedAt": durable["accepted_at"], "revokedAt": durable["revoked_at"], "revision": durable["revision"],
                    }

    def aggregate_visible_state(repo: TreeHouseRepository, account_id: str, workspace: str) -> dict[str, Any]:
        """Union every owner catalogue admitted by the repository predicate."""
        aggregate = new_treehouse_state(account_id)
        ensure_account_profile(aggregate, account_id)
        refs = repo.accessible_course_refs(account_id, workspace)
        for ref in refs:
            source, source_revision = repo.get_catalogue(ref["ownerAccountId"], workspace)
            if not source:
                continue
            course_id = ref["courseId"]
            course = (source.get("courses") or {}).get(course_id)
            if not course or course.get("deletedAt"):
                continue
            # Per-owner source revision survives the multi-catalogue union.
            course = {**copy.deepcopy(course), "curriculumRevision": source_revision}
            visible = {"courses": {course_id: course}, "modules": {}, "activities": {}, "assignments": {}, "skills": {}, "badges": {}, "quests": {}, "courseGrants": {}}
            module_ids = set(course.get("moduleIds") or [])
            visible["modules"] = {key: value for key, value in (source.get("modules") or {}).items() if key in module_ids and not value.get("deletedAt")}
            activity_ids = {item_id for module in visible["modules"].values() for item_id in module.get("activityIds", [])}
            assignment_ids = {item_id for module in visible["modules"].values() for item_id in module.get("assignmentIds", [])}
            visible["activities"] = {key: value for key, value in (source.get("activities") or {}).items() if key in activity_ids and not value.get("deletedAt") and (value.get("status") == "published" or ref["ownerAccountId"] == account_id)}
            visible["assignments"] = {key: value for key, value in (source.get("assignments") or {}).items() if key in assignment_ids and not value.get("deletedAt") and (value.get("status") == "published" or ref["ownerAccountId"] == account_id)}
            skill_ids = {skill_id for item in (*visible["activities"].values(), *visible["assignments"].values()) for skill_id in item.get("skillIds", [])}
            visible["skills"] = {key: value for key, value in (source.get("skills") or {}).items() if key in skill_ids and not value.get("deletedAt")}
            quest_ids = {quest_id for quest_id, quest in (source.get("quests") or {}).items() if set(quest.get("activityIds", [])) & set(visible["activities"]) or set(quest.get("assignmentIds", [])) & set(visible["assignments"])}
            visible["quests"] = {key: value for key, value in (source.get("quests") or {}).items() if key in quest_ids and not value.get("deletedAt")}
            visible["badges"] = {
                key: value for key, value in (source.get("badges") or {}).items()
                if not value.get("deletedAt") and (
                    value.get("criteria", {}).get("courseId") == course_id
                    or value.get("criteria", {}).get("skillId") in visible["skills"]
                    or value.get("criteria", {}).get("questId") in visible["quests"]
                )
            }
            if ref["ownerAccountId"] == account_id:
                visible["courseGrants"] = {
                    key: copy.deepcopy(value) for key, value in (source.get("courseGrants") or {}).items()
                    if value.get("courseId") == course_id and (value.get("ownerId") == account_id or value.get("ownerAccountId") == account_id)
                }
            for key in visible:
                aggregate.setdefault(key, {}).update(visible[key])
            if source.get("fieldGuide") and "fieldGuide" not in aggregate:
                aggregate["fieldGuide"] = copy.deepcopy(source["fieldGuide"])
            if source.get("extensions", {}).get("fieldGuideManifest") and "fieldGuideManifest" not in aggregate.setdefault("extensions", {}):
                aggregate["extensions"]["fieldGuideManifest"] = copy.deepcopy(source["extensions"]["fieldGuideManifest"])
            if repo.access(account_id, workspace, ref["ownerAccountId"], course_id, "edit"):
                for reviewed in repo.course_progress_for_review(account_id, ref["ownerAccountId"], workspace, course_id):
                    if reviewed["learnerAccountId"] != account_id:
                        overlay_progress(aggregate, reviewed["state"])
            visible_entities = {course_id, *set(visible["modules"]), *set(visible["activities"]), *set(visible["assignments"]), *set(visible["skills"]), *set(visible["quests"]), *set(visible["badges"])}
            visible_events = [
                event for event in source.get("events", [])
                if event.get("entityId") in visible_entities
                or event.get("data", {}).get("courseId") == course_id
            ]
            if ref["ownerAccountId"] != account_id:
                # A shared course exposes curriculum history and this
                # learner's own evidence only.  Owner/other-learner private
                # progress must stay in the owner's account namespace.
                visible_events = [
                    event for event in visible_events
                    if event.get("subjectId") == account_id
                    or event.get("type") in {"course.created", "course.updated", "course.published", "module.created", "module.updated", "activity.created", "activity.updated", "assignment.created", "assignment.updated", "assignment.published", "skill.created", "badge.created", "quest.created"}
                ]
            aggregate["events"].extend(visible_events)
            merge_grants(aggregate, repo, account_id, workspace)
            progress, _ = repo.get_progress(account_id, ref["ownerAccountId"], workspace, ref["courseId"])
            overlay_progress(aggregate, progress)
        aggregate["revision"] = max([aggregate.get("revision", 0)] + [int(repo.get_catalogue(ref["ownerAccountId"], workspace)[1]) for ref in refs])
        return aggregate

    async def load_state(request: Request, scope: dict[str, str], *, initialize: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        if account_scoped(request, scope):
            repo = repository(request)
            account_id = scope["account_id"]
            owner_id = scope.get("_treehouse_owner") or (repo.owner_for_course(account_id, scope["workspace_id"], str(request.query_params.get("courseId") or "")) if request.query_params.get("courseId") else account_id)
            owner_id = owner_id or account_id
            state, _ = repo.get_catalogue(owner_id, scope["workspace_id"])
            if state is None:
                state, _ = ensure_account_catalogue(repo, owner_id, scope["workspace_id"])
            ensure_account_profile(state, account_id)
            if scope.get("_progress_only") or owner_id != account_id:
                progress, _ = repo.get_progress(account_id, owner_id, scope["workspace_id"], str(scope.get("_course_id") or request.query_params.get("courseId") or ""))
                overlay_progress(state, progress)
            merge_grants(state, repo, account_id, scope["workspace_id"])
            return {"id": None, "head": str(state.get("revision", 0)), "kind": "treehouse-state"}, state
        indexed = await call(request, "index", {**scope, "kind": "treehouse-state"})
        docs = [doc for doc in indexed.get("docs", []) if doc.get("name") == state_name]
        if len(docs) > 1:
            raise HTTPException(409, "Multiple TreeHouse state documents exist; repair the workspace before writing")
        if not docs and not initialize:
            # Pure GET/integrity reads synthesize the default in memory. The
            # first command remains the sole state-creation boundary.
            return {"id": None, "head": None, "kind": "treehouse-state"}, new_treehouse_state(scope["owner"])
        if not docs:
            initial = new_treehouse_state(scope["owner"])
            try:
                created = await call(request, "create", {**scope, "name": state_name, "kind": "treehouse-state", "content": json.dumps(initial, separators=(",", ":"))})
                document_id = created.get("doc", {}).get("id")
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                document_id = None
            if not document_id:
                indexed = await call(request, "index", {**scope, "kind": "treehouse-state"})
                match = next((doc for doc in indexed.get("docs", []) if doc.get("name") == state_name), None)
                if not match:
                    raise HTTPException(503, "TreeHouse state could not be initialized")
                document_id = match["id"]
            doc = await call(request, "get", {**scope, "id": document_id})
        else:
            doc = docs[0]
        try:
            state = json.loads(doc.get("text") or "{}")
            validate_treehouse_state(state)
        except (json.JSONDecodeError, TreeHouseError) as exc:
            detail = exc.payload() if isinstance(exc, TreeHouseError) else {"code": "corrupt_state", "message": "TreeHouse state is not valid JSON"}
            raise HTTPException(409, detail=detail) from exc
        return doc, state

    def engagement_snapshot(state, actor_id, request, scope):
        snapshot = public_treehouse_snapshot(state, actor_id)
        if account_scoped(request, scope):
            attach_engagement_context(repository(request), scope["account_id"], scope["workspace_id"], snapshot)
        return snapshot

    def resume_class_publications(request: Request, scope: dict[str, str]) -> None:
        repo = repository(request)
        state, _ = repo.get_catalogue(scope["account_id"], scope["workspace_id"])
        for event in (state or {}).get("events", []):
            facts = event.get("data", {}).get("publicationReceipt")
            if event.get("type") != "course.published" or event.get("actorId") != scope["account_id"] or not isinstance(facts, dict):
                continue
            try:
                record_journal_activity(scope["account_id"], "class.published", str(event["id"]), facts,
                                        occurred_at=str(event["at"]), workspace_id=scope["workspace_id"], repository=repo)
            except Exception:
                # The committed catalogue journal remains resumable on the
                # next real load. A delivery error cannot undo publication.
                break

    def owner_draft_preview(request: Request, scope: dict[str, str], course_id: str):
        state, revision = repository(request).get_catalogue(scope["account_id"], scope["workspace_id"])
        course = (state or {}).get("courses", {}).get(course_id)
        if not course or course.get("deletedAt") or course.get("ownerId") != scope["account_id"]:
            raise HTTPException(403, detail={"code": "preview_owner_required", "message": "Preview requires your own draft"})
        if course.get("status") != "draft" or course.get("fieldGuideKey"):
            raise HTTPException(409, detail={"code": "preview_draft_required", "message": "Preview requires a personal draft"})
        lessons = [activity for module_id in course.get("moduleIds", [])
                   for activity_id in (state.get("modules", {}).get(module_id) or {}).get("activityIds", [])
                   if (activity := state.get("activities", {}).get(activity_id)) and not activity.get("deletedAt")
                   and activity.get("status") == "published" and str(activity.get("content") or "").strip()]
        if not lessons:
            raise HTTPException(409, detail={"code": "preview_empty", "message": "Add a populated lesson before previewing"})
        return state, course, revision, len(lessons)

    @router.get("/courses/{course_id}/learner-preview")
    async def learner_preview(course_id: str, request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        state, course, revision, count = owner_draft_preview(request, scope, course_id)
        snapshot = public_treehouse_snapshot(state, scope["account_id"])
        return {**snapshot, "accountId": scope["account_id"], "workspace": scope["workspace_id"],
                "preview": {"courseId": course_id, "revision": revision, "lessonCount": count}}

    @router.post("/courses/{course_id}/learner-preview")
    async def acknowledge_learner_preview(course_id: str, request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        body = await request.json()
        state, course, revision, count = owner_draft_preview(request, scope, course_id)
        if not isinstance(body, dict) or body.get("visible") is not True or type(body.get("revision")) is not int or body["revision"] != revision:
            raise HTTPException(409, detail={"code": "stale_preview", "message": "Reopen the current draft preview"})
        if body.get("accountId") != scope["account_id"]:
            raise HTTPException(409, detail={"code": "stale_account", "message": "The signed-in account changed"})
        record_account_activity(scope["account_id"], "class.previewed", f"preview:{course_id}:{revision}",
                                {"classId": course_id, "catalogueRevision": revision, "previewedAsLearner": True,
                                 "authoredByOwner": True, "seededOfficial": False, "lessonCount": count},
                                workspace_id=scope["workspace_id"], repository=repository(request))
        return {"ok": True, "courseId": course_id, "revision": revision}

    @router.get("/courses/{course_id}/export")
    async def export_course_package(course_id: str, request: Request, workspace: str | None = None):
        """Export one curriculum graph without learner progress or grants."""
        scope = account_scope(request, scope_for(request, workspace))
        if not account_scoped(request, scope):
            raise HTTPException(409, detail={"code": "course_package_requires_account", "message": "Course packages require an authenticated account"})
        repo = repository(request)
        state, revision = repo.get_catalogue(scope["account_id"], scope["workspace_id"])
        course = (state or {}).get("courses", {}).get(course_id) if state else None
        if not isinstance(course, dict) or course.get("deletedAt"):
            raise HTTPException(404, detail={"code": "course_not_found", "message": "Course is not owned by this account"})
        # Include the complete prerequisite course and skill closure so an
        # imported curriculum has no dangling unlock references.
        course_ids = {course_id}
        pending_courses = [str(value) for value in course.get("prerequisites") or []]
        while pending_courses:
            prerequisite_id = pending_courses.pop()
            if prerequisite_id in course_ids:
                continue
            prerequisite = (state.get("courses") or {}).get(prerequisite_id)
            if not isinstance(prerequisite, dict) or prerequisite.get("deletedAt"):
                raise HTTPException(409, detail={"code": "course_prerequisite_missing", "message": f"Course prerequisite {prerequisite_id} is missing"})
            course_ids.add(prerequisite_id)
            pending_courses.extend(str(value) for value in prerequisite.get("prerequisites") or [])
        module_ids = {str(value) for ident in course_ids for value in ((state.get("courses") or {}).get(ident, {}).get("moduleIds") or [])}
        modules = {}
        for course_ident in course_ids:
            for raw_module_id in ((state.get("courses") or {}).get(course_ident, {}).get("moduleIds") or []):
                ident = str(raw_module_id)
                module = (state.get("modules") or {}).get(ident)
                if not isinstance(module, dict) or module.get("deletedAt") or str(module.get("courseId") or "") != course_ident:
                    raise HTTPException(409, detail={"code": "course_module_missing", "message": f"Course module {ident} is missing or belongs to another course"})
                modules[ident] = module
        activity_ids = {str(value) for module in modules.values() for value in module.get("activityIds") or []}
        assignment_ids = {str(value) for module in modules.values() for value in module.get("assignmentIds") or []}
        activities = {}
        for ident in activity_ids:
            value = (state.get("activities") or {}).get(ident)
            module = modules.get(str(value.get("moduleId"))) if isinstance(value, dict) else None
            if not isinstance(value, dict) or value.get("deletedAt") or not module or str(value.get("courseId") or "") != str(module.get("courseId")):
                raise HTTPException(409, detail={"code": "course_activity_missing", "message": f"Course activity {ident} is missing or has an invalid module/course"})
            activities[ident] = value
        assignments = {}
        for ident in assignment_ids:
            value = (state.get("assignments") or {}).get(ident)
            module = modules.get(str(value.get("moduleId"))) if isinstance(value, dict) else None
            if not isinstance(value, dict) or value.get("deletedAt") or not module or str(value.get("courseId") or "") != str(module.get("courseId")):
                raise HTTPException(409, detail={"code": "course_assignment_missing", "message": f"Course assignment {ident} is missing or has an invalid module/course"})
            assignments[ident] = value
        for module_id, module in modules.items():
            if any(str(activities[item_id].get("moduleId")) != module_id or str(activities[item_id].get("courseId")) != str(module.get("courseId")) for item_id in module.get("activityIds") or []):
                raise HTTPException(409, detail={"code": "course_activity_missing", "message": f"Module {module_id} lists an activity owned by another module"})
            if any(str(assignments[item_id].get("moduleId")) != module_id or str(assignments[item_id].get("courseId")) != str(module.get("courseId")) for item_id in module.get("assignmentIds") or []):
                raise HTTPException(409, detail={"code": "course_assignment_missing", "message": f"Module {module_id} lists an assignment owned by another module"})
        skill_ids = {str(value) for item in (*activities.values(), *assignments.values()) for value in item.get("skillIds") or []}
        pending_skills = list(skill_ids)
        while pending_skills:
            skill_id = pending_skills.pop()
            skill = (state.get("skills") or {}).get(skill_id)
            if not isinstance(skill, dict) or skill.get("deletedAt"):
                raise HTTPException(409, detail={"code": "skill_missing", "message": f"Skill {skill_id} is missing"})
            for prerequisite_id in skill.get("prerequisiteIds") or []:
                prerequisite_id = str(prerequisite_id)
                if prerequisite_id not in skill_ids:
                    skill_ids.add(prerequisite_id)
                    pending_skills.append(prerequisite_id)
        skills = {ident: value for ident, value in (state.get("skills") or {}).items() if ident in skill_ids and isinstance(value, dict)}
        quest_ids = {ident for ident, value in (state.get("quests") or {}).items() if isinstance(value, dict) and ({*value.get("activityIds", []), *value.get("assignmentIds", [])} & ({*activities, *assignments}))}
        quests = {}
        for ident in quest_ids:
            value = (state.get("quests") or {}).get(ident)
            if not isinstance(value, dict) or value.get("deletedAt") or any(str(ref) not in activities for ref in value.get("activityIds", [])) or any(str(ref) not in assignments for ref in value.get("assignmentIds", [])):
                raise HTTPException(409, detail={"code": "quest_closure_missing", "message": f"Quest {ident} references work outside the exported closure"})
            quests[ident] = value
        badges = {
            ident: value for ident, value in (state.get("badges") or {}).items()
            if isinstance(value, dict) and (
                value.get("criteria", {}).get("courseId") in course_ids
                or value.get("criteria", {}).get("skillId") in skills
                or value.get("criteria", {}).get("questId") in quests
            )
        }
        package = {
            "format": "copal-treehouse-course-v1",
            "schemaVersion": 1,
            "sourceRevision": revision,
            "course": portable_definition(course),
            "courses": {ident: portable_definition((state.get("courses") or {})[ident]) for ident in sorted(course_ids)},
            "modules": portable_definition(modules),
            "activities": portable_definition(activities),
            "assignments": portable_definition(assignments),
            "skills": portable_definition(skills),
            "badges": portable_definition(badges),
            "quests": portable_definition(quests),
        }
        from src.openclank.treehouse_rich_learning import export_podcasts
        package["learningAdapters"] = {"podcasts": export_podcasts(state, course_ids)}
        # Descriptions are portable; opaque Files grants and preparation receipts are not.
        for ident, activity in activities.items():
            descriptors = []
            for attachment in activity.get("sourceAttachments", []):
                receipt = repo.lesson_attachment_preparation(caller_account_id=scope["account_id"], workspace_id=scope["workspace_id"], operation_id=attachment.get("operationId", ""))
                source = (receipt or {}).get("source", {})
                descriptors.append({"name": str(source.get("name", "Resource"))[:240], "mimeType": str(source.get("mime_type", "application/octet-stream"))[:128], "mode": attachment.get("mode", "link")})
            if descriptors: package["activities"][ident]["portableResources"] = descriptors
        package["packageId"] = course_package_digest(package)
        return package

    @router.post("/courses/import")
    async def import_course_package(request: Request, workspace: str | None = None):
        """Atomically import a curriculum graph with durable replay semantics."""
        scope = account_scope(request, scope_for(request, workspace))
        if not account_scoped(request, scope):
            raise HTTPException(409, detail={"code": "course_package_requires_account", "message": "Course packages require an authenticated account"})
        try:
            package = await request.json()
        except Exception as exc:
            raise HTTPException(400, detail={"code": "invalid_course_package", "message": "Course package must be JSON"}) from exc
        if not isinstance(package, dict) or package.get("format") != "copal-treehouse-course-v1" or package.get("schemaVersion") != 1:
            raise HTTPException(400, detail={"code": "unsupported_course_package", "message": "Course package format is unsupported"})
        if len(json.dumps(package, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
            raise HTTPException(413, detail={"code": "course_package_too_large", "message": "Course package exceeds the import limit"})
        reject_progress_payload(package)
        course = package.get("course")
        if not isinstance(course, dict) or not str(course.get("id") or "").strip():
            raise HTTPException(400, detail={"code": "invalid_course_package", "message": "Course package has no course definition"})
        package_id = str(package.get("packageId") or "").strip()
        if not package_id or len(package_id) > 128:
            raise HTTPException(400, detail={"code": "invalid_course_package", "message": "Course package has no stable package ID"})
        expected_package_id = course_package_digest(package)
        if package_id != expected_package_id:
            raise HTTPException(409, detail={"code": "course_package_digest_mismatch", "message": "Course package was changed after export"})
        courses_payload = package.get("courses")
        root_id = str(course.get("id"))
        if not isinstance(courses_payload, dict) or not isinstance(courses_payload.get(root_id), dict):
            raise HTTPException(400, detail={"code": "invalid_course_package", "message": "Course package has no authoritative root course"})
        if json.dumps(portable_definition(courses_payload[root_id]), ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(portable_definition(course), ensure_ascii=False, sort_keys=True, separators=(",", ":")):
            raise HTTPException(409, detail={"code": "course_package_root_mismatch", "message": "Course package root does not match its courses map"})
        repo = repository(request)
        state, revision = repo.get_catalogue(scope["account_id"], scope["workspace_id"])
        state = copy.deepcopy(state or instantiate_field_guide(new_treehouse_state(scope["account_id"]), scope["account_id"]))
        state.setdefault("extensions", {})
        receipts = state["extensions"].setdefault("courseImports", {})
        prior = receipts.get(package_id)
        if isinstance(prior, dict):
            return {"ok": True, "outcome": "replayed", "packageId": package_id, **prior}
        definitions = {kind: package.get(kind) for kind in _PORTABLE_DEFINITION_KINDS}
        if any(not isinstance(value, dict) for value in definitions.values()):
            raise HTTPException(400, detail={"code": "invalid_course_package", "message": "Course package definition maps are invalid"})
        if root_id not in definitions["courses"] or any(len(value) > 2_000 for value in definitions.values()):
            raise HTTPException(413, detail={"code": "course_package_definition_limit", "message": "Course package contains too many definitions"})
        seen_ids: dict[str, str] = {}
        for kind, records in definitions.items():
            for ident, record in records.items():
                ident = str(ident)
                if not isinstance(record, dict) or str(record.get("id") or "") != ident:
                    raise HTTPException(400, detail={"code": "invalid_course_package_record", "message": f"{kind} record {ident} has an inconsistent id"})
                previous = seen_ids.get(ident)
                if previous and previous != kind:
                    raise HTTPException(409, detail={"code": "course_package_id_collision", "message": f"ID {ident} is used by both {previous} and {kind}"})
                seen_ids[ident] = kind
        for ident, item in definitions["courses"].items():
            missing = [ref for ref in item.get("prerequisites") or [] if str(ref) not in definitions["courses"]]
            if missing:
                raise HTTPException(409, detail={"code": "course_package_dangling_reference", "message": f"Course {ident} references missing prerequisites"})
            for ref in item.get("moduleIds") or []:
                module = definitions["modules"].get(str(ref))
                if not isinstance(module, dict) or str(module.get("courseId") or "") != str(ident):
                    raise HTTPException(409, detail={"code": "course_package_wrong_owner", "message": f"Course {ident} references a module owned by another course or missing"})
        for ident, item in definitions["modules"].items():
            if str(item.get("courseId") or "") not in definitions["courses"]:
                raise HTTPException(409, detail={"code": "course_package_dangling_reference", "message": f"Module {ident} references a missing course"})
            activity_ids = {str(ref) for ref in item.get("activityIds") or []}
            assignment_ids = {str(ref) for ref in item.get("assignmentIds") or []}
            if activity_ids & assignment_ids:
                raise HTTPException(409, detail={"code": "course_package_wrong_kind", "message": f"Module {ident} lists one item as both activity and assignment"})
            if any(ref not in definitions["activities"] for ref in activity_ids) or any(ref not in definitions["assignments"] for ref in assignment_ids):
                raise HTTPException(409, detail={"code": "course_package_wrong_kind", "message": f"Module {ident} references an item of the wrong kind or missing"})
        for kind in ("activities", "assignments"):
            for ident, item in definitions[kind].items():
                module = definitions["modules"].get(str(item.get("moduleId") or ""))
                listed = {str(ref) for ref in module.get("activityIds" if kind == "activities" else "assignmentIds") or []} if module else set()
                if not module or str(item.get("courseId") or "") != str(module.get("courseId") or "") or str(ident) not in listed:
                    raise HTTPException(409, detail={"code": "course_package_wrong_owner", "message": f"{kind} {ident} has an invalid module/course relationship"})
                for ref in item.get("skillIds") or []:
                    if str(ref) not in definitions["skills"]:
                        raise HTTPException(409, detail={"code": "course_package_dangling_reference", "message": f"{kind} {ident} references a missing skill"})
        for module_id, module in definitions["modules"].items():
            for ref in module.get("activityIds") or []:
                activity = definitions["activities"].get(str(ref))
                if not activity or str(activity.get("moduleId") or "") != str(module_id) or str(activity.get("courseId") or "") != str(module.get("courseId") or ""):
                    raise HTTPException(409, detail={"code": "course_package_wrong_owner", "message": f"Module {module_id} lists an activity owned by another module"})
            for ref in module.get("assignmentIds") or []:
                assignment = definitions["assignments"].get(str(ref))
                if not assignment or str(assignment.get("moduleId") or "") != str(module_id) or str(assignment.get("courseId") or "") != str(module.get("courseId") or ""):
                    raise HTTPException(409, detail={"code": "course_package_wrong_owner", "message": f"Module {module_id} lists an assignment owned by another module"})
        for ident, item in definitions["skills"].items():
            if any(str(ref) not in definitions["skills"] for ref in item.get("prerequisiteIds") or []):
                raise HTTPException(409, detail={"code": "course_package_dangling_reference", "message": f"Skill {ident} references a missing prerequisite"})
        for ident, item in definitions["quests"].items():
            if not item.get("activityIds") and not item.get("assignmentIds"):
                raise HTTPException(409, detail={"code": "course_package_wrong_kind", "message": f"Quest {ident} has no work"})
            if any(str(ref) not in definitions["activities"] for ref in item.get("activityIds") or []) or any(str(ref) not in definitions["assignments"] for ref in item.get("assignmentIds") or []):
                raise HTTPException(409, detail={"code": "course_package_wrong_kind", "message": f"Quest {ident} references work of the wrong kind or missing"})
        for ident, item in definitions["badges"].items():
            criteria = item.get("criteria")
            if not isinstance(criteria, dict) or criteria.get("type") not in {"points", "course", "skill", "quest"}:
                raise HTTPException(409, detail={"code": "course_package_wrong_kind", "message": f"Badge {ident} has invalid criteria"})
            ref_kind = {"course": "courses", "skill": "skills", "quest": "quests"}.get(str(criteria.get("type")))
            if ref_kind and str(criteria.get(f"{criteria.get('type')}Id") or "") not in definitions[ref_kind]:
                raise HTTPException(409, detail={"code": "course_package_dangling_reference", "message": f"Badge {ident} references a missing {criteria.get('type')}"})
        expected_modules = {str(ref) for item in definitions["courses"].values() for ref in item.get("moduleIds") or []}
        expected_activities = {str(ref) for item in definitions["modules"].values() for ref in item.get("activityIds") or []}
        expected_assignments = {str(ref) for item in definitions["modules"].values() for ref in item.get("assignmentIds") or []}
        expected_skills = {str(ref) for kind in ("activities", "assignments") for item in definitions[kind].values() for ref in item.get("skillIds") or []}
        pending_skills = list(expected_skills)
        while pending_skills:
            skill_id = pending_skills.pop()
            for ref in definitions["skills"].get(skill_id, {}).get("prerequisiteIds") or []:
                ref = str(ref)
                if ref not in expected_skills:
                    expected_skills.add(ref); pending_skills.append(ref)
        if set(definitions["modules"]) != expected_modules or set(definitions["activities"]) != expected_activities or set(definitions["assignments"]) != expected_assignments or set(definitions["skills"]) != expected_skills:
            raise HTTPException(409, detail={"code": "course_package_closure", "message": "Course package contains definitions outside the referenced curriculum closure"})
        digest = hashlib.sha256(package_id.encode()).hexdigest()[:12]
        mapping: dict[str, dict[str, str]] = {kind: {} for kind in _PORTABLE_DEFINITION_KINDS}
        for kind in _PORTABLE_DEFINITION_KINDS:
            existing = state.setdefault(kind, {})
            for ident in definitions[kind]:
                candidate = str(ident)
                if candidate in existing:
                    candidate = f"{candidate}:import-{digest}"
                    ordinal = 2
                    while candidate in existing or candidate in mapping[kind].values():
                        candidate = f"{ident}:import-{digest}-{ordinal}"
                        ordinal += 1
                mapping[kind][str(ident)] = candidate
        imported: dict[str, dict[str, Any]] = {}
        for kind in _PORTABLE_DEFINITION_KINDS:
            for ident, value in definitions[kind].items():
                target_id = mapping[kind][str(ident)]
                record = remap_definition(kind, value, mapping)
                record["id"] = target_id
                if kind == "courses":
                    record["ownerId"] = scope["account_id"]
                    record["authorIds"] = [scope["account_id"]]
                state[kind][target_id] = record
                imported.setdefault(kind, {})[str(ident)] = target_id
        from src.openclank.treehouse_rich_learning import import_podcasts, validate_adapter
        try:
            for record in state["activities"].values():
                if record.get("id") in mapping["activities"].values() and "adapter" in record:
                    record["adapter"] = validate_adapter(record["adapter"], record.get("activityType", "lesson"))
            import_podcasts(package.get("learningAdapters"), state, mapping)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        state["revision"] = int(state.get("revision") or revision) + 1
        receipt = {"courseId": mapping["courses"].get(str(course["id"])), "revision": state["revision"], "idMap": imported}
        receipts[package_id] = receipt
        try:
            validate_treehouse_state(state)
            repo.put_catalogue(scope["account_id"], scope["workspace_id"], state, expected_revision=revision)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        except TreeHouseRepositoryError as exc:
            # A concurrent import may have won the CAS.  Reload once so a
            # retry of the same package observes its durable receipt; a
            # different concurrent edit remains a typed stale conflict.
            if exc.code == "stale":
                current, _current_revision = repo.get_catalogue(scope["account_id"], scope["workspace_id"])
                replay = (current or {}).get("extensions", {}).get("courseImports", {}).get(package_id)
                if isinstance(replay, dict):
                    return {"ok": True, "outcome": "replayed", "packageId": package_id, **replay}
            raise repository_error(exc) from exc
        publish(scope, "document", {"treehouse": True, "revision": state["revision"], "operation": "course-import", "packageId": package_id})
        return {"ok": True, "outcome": "applied", "packageId": package_id, **receipt}

    async def write_state(request: Request, scope: dict[str, str], doc: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if account_scoped(request, scope):
            try:
                owner_id = scope.get("_treehouse_owner") or scope["account_id"]
                if scope.get("_progress_only"):
                    course_id = str(scope.get("_course_id") or "")
                    progress = progress_projection(state, scope["account_id"])
                    previous_epoch = int(scope.get("reset_epoch") or 0)
                    next_epoch = max([previous_epoch, *[int(value or 0) for value in (progress.get("progressResets") or {}).values()]])
                    repository(request).put_progress(
                        scope["account_id"], owner_id, scope["workspace_id"], course_id, progress,
                        expected_revision=int(scope.get("_progress_revision") or 0),
                        reset_epoch=next_epoch, expected_reset_epoch=previous_epoch,
                        grant_id=scope.get("_grant_id"), stats_context=state,
                        access_revision=int(scope["_grant_revision"]) if scope.get("_grant_revision") is not None else None,
                    )
                else:
                    persisted = state
                    if scope.get("_treehouse_owner") and scope.get("_treehouse_owner") != scope["account_id"]:
                        persisted = copy.deepcopy(state)
                        persisted.get("profiles", {}).pop(scope["account_id"], None)
                    repository(request).put_catalogue(owner_id, scope["workspace_id"], persisted, expected_revision=int(doc.get("head") or 0), access_grant_id=scope.get("_grant_id"), access_revision=int(scope["_grant_revision"]) if scope.get("_grant_revision") is not None else None)
            except TreeHouseRepositoryError as exc:
                raise repository_error(exc) from exc
            publish(scope, "document", {"treehouse": True, "revision": state["revision"]})
            return {"outcome": "committed", "revision": state["revision"]}
        content = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
        if len(content.encode()) > 8_388_608:
            raise HTTPException(409, detail={"code": "state_too_large", "message": "TreeHouse state exceeds the Redb document safety limit"})
        result = await call(request, "write", {**scope, "id": doc["id"], "content": content, "base": doc.get("head")})
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"code": "stale", "message": "TreeHouse changed in another tab", "revision": state.get("revision")})
        publish(scope, "document", {"treehouse": True, "revision": state["revision"]})
        return result

    @router.get("")
    async def get_treehouse(
        request: Request,
        workspace: str | None = None,
        actor: str = Query("owner", max_length=128),
        course_id: str | None = Query(None, alias="courseId", max_length=256),
    ):
        scope = account_scope(request, scope_for(request, workspace))
        actor_id = actor_for(request, scope, actor)
        if account_scoped(request, scope):
            ensure_account_catalogue(repository(request), scope["account_id"], scope["workspace_id"])
            if course_id:
                owner_id = repository(request).owner_for_course(scope["account_id"], scope["workspace_id"], course_id)
                if not owner_id or not repository(request).access(scope["account_id"], scope["workspace_id"], owner_id, course_id, "learn"):
                    raise HTTPException(404, "TreeHouse course not found")
            resume_class_publications(request, scope)
            state = aggregate_visible_state(repository(request), scope["account_id"], scope["workspace_id"])
            snapshot = engagement_snapshot(state, actor_id, request, scope)
            return {**snapshot, "recipientOptions": recipient_options(request, scope["account_id"]), "document": {"id": None, "head": str(state.get("revision", 0))}, "workspace": scope["workspace_id"], "fingerprint": state_fingerprint(state), "accountId": scope["account_id"]}
        doc, state = await load_state(request, scope, initialize=False)
        try:
            snapshot = engagement_snapshot(state, actor_id, request, scope)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        return {**snapshot, "document": {"id": doc["id"], "head": doc.get("head")}, "workspace": scope["workspace_id"], "fingerprint": state_fingerprint(state), "accountId": scope.get("account_id")}

    @router.post("/commands")
    async def command_treehouse(
        command: TreeHouseCommand,
        request: Request,
        workspace: str | None = None,
    ):
        scope = account_scope(request, scope_for(request, workspace))
        actor_id = actor_for(request, scope, command.actor_id)
        attachment_policy = file_policy(request) if command.type == "lesson.attach_source" else None
        repo = repository(request) if account_scoped(request, scope) else None
        if repo and command.type == "course.share":
            resolved_recipient = resolve_recipient_account(request, command.payload.get("recipientId"), scope["account_id"])
            if not resolved_recipient:
                raise HTTPException(404, detail={"code": "recipient_not_found", "message": "Choose an existing account by username or account ID"})
            command.payload = {**command.payload, "recipientId": resolved_recipient}
        if repo and command.type == "progress.reset" and not command.payload.get("courseId"):
            # The UI's learner reset is an all-course reset in the learner's
            # visible namespace.  Each owner/course progress row keeps its
            # own CAS and reset epoch; curricula and other learners remain
            # untouched.
            refs = repo.accessible_course_refs(scope["account_id"], scope["workspace_id"])
            reset_rows = []
            for index, ref in enumerate(refs):
                owner_id = ref["ownerAccountId"]
                course_id = ref["courseId"]
                source, source_revision = repo.get_catalogue(owner_id, scope["workspace_id"])
                if not source:
                    continue
                progress, progress_revision = repo.get_progress(scope["account_id"], owner_id, scope["workspace_id"], course_id)
                working = copy.deepcopy(source)
                ensure_account_profile(working, scope["account_id"])
                overlay_progress(working, progress)
                grant = repo.active_grant(scope["account_id"], scope["workspace_id"], owner_id, course_id) if owner_id != scope["account_id"] else None
                reset_epoch = repo.progress_reset_epoch(scope["account_id"], owner_id, scope["workspace_id"], course_id)
                working_command = {"type": "progress.reset", "payload": {**command.payload, "profileId": scope["account_id"], "courseId": course_id}}
                next_state, result, changed = apply_treehouse_command(
                    working, working_command, actor_id=scope["account_id"],
                    command_id=f"{command.command_id}:reset:{index}", expected_revision=working.get("revision", source_revision),
                )
                if changed:
                    progress_state = progress_projection(next_state, scope["account_id"])
                    next_epoch = max([reset_epoch, *[int(value or 0) for value in (progress_state.get("progressResets") or {}).values()]])
                    reset_rows.append({
                        "owner_account_id": owner_id,
                        "course_id": course_id,
                        "state": progress_state,
                        "expected_revision": progress_revision,
                        "reset_epoch": next_epoch,
                        "expected_reset_epoch": reset_epoch,
                        "grant_id": grant["grant_id"] if grant else None,
                        "access_revision": int(grant["revision"]) if grant else None,
                        "result": result,
                    })
            reset_result = repo.reset_progress_all(
                scope["account_id"], scope["workspace_id"], reset_rows,
                command_id=command.command_id, payload={"type": command.type, "payload": command.payload},
            )
            visible = aggregate_visible_state(repo, scope["account_id"], scope["workspace_id"])
            snapshot = engagement_snapshot(visible, scope["account_id"], request, scope)
            return {"ok": True, "changed": bool(reset_rows) and not reset_result.get("replayed"), "result": reset_result, **snapshot, "accountId": scope["account_id"], "workspace": scope["workspace_id"], "document": {"id": None, "head": str(visible.get("revision", 0))}}
        if repo and command.type == "submission.grade":
            # Grading changes the learner's existing progress partition. The
            # reviewer never becomes the subject and no catalogue gradebook
            # copy is introduced. The shared transaction guards current grant,
            # curriculum, reset generation, attempt and target progress CAS.
            with repo.achievement_transaction():
                selected = None
                for ref in repo.accessible_course_refs(scope["account_id"], scope["workspace_id"]):
                    if command.payload.get("courseId") and command.payload["courseId"] != ref["courseId"]:
                        continue
                    if not repo.access(scope["account_id"], scope["workspace_id"], ref["ownerAccountId"], ref["courseId"], "edit"):
                        continue
                    for row in repo.course_progress_for_review(scope["account_id"], ref["ownerAccountId"], scope["workspace_id"], ref["courseId"]):
                        if str(command.payload.get("submissionId") or "") in row["state"].get("submissions", {}):
                            selected = (ref, row)
                            break
                    if selected:
                        break
                if not selected:
                    # Older owner catalogue records join the same learner
                    # partition on their first review; their stable IDs remain.
                    for ref in repo.accessible_course_refs(scope["account_id"], scope["workspace_id"]):
                        if not repo.access(scope["account_id"], scope["workspace_id"], ref["ownerAccountId"], ref["courseId"], "edit"):
                            continue
                        legacy, _ = repo.get_catalogue(ref["ownerAccountId"], scope["workspace_id"])
                        old = (legacy or {}).get("submissions", {}).get(str(command.payload.get("submissionId") or ""))
                        if old and old.get("courseId") == ref["courseId"]:
                            partition, revision = repo.get_progress(old["profileId"], ref["ownerAccountId"], scope["workspace_id"], ref["courseId"])
                            selected = (ref, {"learnerAccountId": old["profileId"], "state": partition or {}, "revision": revision, "resetEpoch": repo.progress_reset_epoch(old["profileId"], ref["ownerAccountId"], scope["workspace_id"], ref["courseId"])})
                            break
                if not selected:
                    raise HTTPException(403, detail={"code": "review_unavailable", "message": "This submitted attempt is unavailable to the current course editor"})
                ref, row = selected
                working, catalogue_revision = repo.get_catalogue(ref["ownerAccountId"], scope["workspace_id"])
                expected_curriculum = command.payload.get("curriculumRevision")
                if expected_curriculum is not None and int(expected_curriculum) != catalogue_revision:
                    raise HTTPException(409, detail={"code": "stale", "message": "Curriculum changed; reopen the review", "revision": catalogue_revision})
                working = copy.deepcopy(working)
                ensure_account_profile(working, scope["account_id"])
                ensure_account_profile(working, row["learnerAccountId"])
                merge_grants(working, repo, scope["account_id"], scope["workspace_id"])
                overlay_progress(working, row["state"])
                try:
                    next_state, result, changed = apply_treehouse_command(working, {"type": command.type, "payload": command.payload}, actor_id=scope["account_id"], command_id=command.command_id, expected_revision=working["revision"])
                    if changed:
                        progress_state = progress_projection(next_state, row["learnerAccountId"])
                        for collection in ("enrollments", "submissions", "evidence"):
                            progress_state[collection] = {key: value for key, value in progress_state[collection].items() if value.get("courseId") == ref["courseId"]}
                        progress_state["events"] = [event for event in progress_state["events"] if event.get("data", {}).get("courseId") == ref["courseId"] or event.get("entityId") == ref["courseId"]]
                        progress_state["processedCommands"][command.command_id] = next_state["processedCommands"][command.command_id]
                        learner_grant = repo.active_grant(row["learnerAccountId"], scope["workspace_id"], ref["ownerAccountId"], ref["courseId"]) if row["learnerAccountId"] != ref["ownerAccountId"] else None
                        if row["learnerAccountId"] != ref["ownerAccountId"] and not learner_grant:
                            raise HTTPException(403, "Learner course access was revoked")
                        repo.put_progress(row["learnerAccountId"], ref["ownerAccountId"], scope["workspace_id"], ref["courseId"], progress_state, expected_revision=row["revision"], reset_epoch=row["resetEpoch"], expected_reset_epoch=row["resetEpoch"], grant_id=learner_grant["grant_id"] if learner_grant else None, access_revision=int(learner_grant["revision"]) if learner_grant else None, stats_context=next_state)
                except TreeHouseError as exc:
                    raise fail(exc) from exc
                except TreeHouseRepositoryError as exc:
                    raise repository_error(exc) from exc
            visible = aggregate_visible_state(repo, scope["account_id"], scope["workspace_id"])
            publish(scope, "document", {"treehouse": True, "operation": "submission-review", "revision": visible["revision"]})
            return {"ok": True, "changed": changed, "result": result, **engagement_snapshot(visible, scope["account_id"], request, scope), "accountId": scope["account_id"], "workspace": scope["workspace_id"]}
        owner_for_command = repo.owner_for_course(scope["account_id"], scope["workspace_id"], str(command.payload.get("courseId") or command.payload.get("course_id") or "")) if repo and command.payload.get("courseId") else None
        if repo and not owner_for_command:
            entity_id = str(command.payload.get("activityId") or command.payload.get("assignmentId") or command.payload.get("moduleId") or "")
            if entity_id:
                resolved = repo.owner_for_entity(scope["account_id"], scope["workspace_id"], entity_id)
                if resolved:
                    owner_for_command = resolved[0]
                    command.payload.setdefault("courseId", resolved[1])
        if repo and command.type == "course.accept_share":
            grant = repo.grant_for_token(scope["workspace_id"], str(command.payload.get("shareToken") or ""))
            if grant and grant["recipient_account_id"] == scope["account_id"]:
                owner_for_command = str(grant["owner_account_id"])
        if repo and command.type == "course.revoke_share" and command.payload.get("grantId"):
            grant = repo.grant(str(command.payload["grantId"]))
            if grant and grant["owner_account_id"] == scope["account_id"]:
                owner_for_command = scope["account_id"]
        if repo and owner_for_command and owner_for_command != scope["account_id"]:
            # Recipient writes are evaluated against the owner's immutable
            # course catalogue, while their progress remains account keyed.
            token_grant = repo.grant_for_token(scope["workspace_id"], str(command.payload.get("shareToken") or "")) if command.type == "course.accept_share" else None
            course_id = str((token_grant or {}).get("course_id") or command.payload.get("courseId") or "")
            grant = token_grant or repo.active_grant(scope["account_id"], scope["workspace_id"], owner_for_command, course_id)
            capability = "learn" if command.type == "course.accept_share" else ("edit" if command.type not in _PROGRESS_COMMANDS else "learn")
            if not grant or (command.type != "course.accept_share" and not repo.access(scope["account_id"], scope["workspace_id"], owner_for_command, course_id, capability)):
                raise HTTPException(404, "TreeHouse course not found")
            scope = {**scope, "_treehouse_owner": owner_for_command, "_grant_id": grant["grant_id"], "_grant_revision": str(grant["revision"]), "_course_id": course_id}
        if repo and owner_for_command and owner_for_command != scope["account_id"] and command.type in _PROGRESS_COMMANDS:
            course_id = str(command.payload.get("courseId") or "")
            progress, progress_revision = repo.get_progress(scope["account_id"], owner_for_command, scope["workspace_id"], course_id)
            reset_epoch = repo.progress_reset_epoch(scope["account_id"], owner_for_command, scope["workspace_id"], course_id)
            attempt_epoch = int(command.payload.get("resetEpoch", reset_epoch))
            scope = {**scope, "_progress_only": True, "_course_id": course_id, "_progress_revision": str(progress_revision), "reset_epoch": str(reset_epoch), "_attempt_reset_epoch": str(attempt_epoch)}
        if repo and owner_for_command == scope["account_id"] and command.type in _PROGRESS_COMMANDS and command.payload.get("courseId"):
            course_id = str(command.payload["courseId"])
            progress, progress_revision = repo.get_progress(scope["account_id"], scope["account_id"], scope["workspace_id"], course_id)
            reset_epoch = repo.progress_reset_epoch(scope["account_id"], scope["account_id"], scope["workspace_id"], course_id)
            attempt_epoch = int(command.payload.get("resetEpoch", reset_epoch))
            scope = {**scope, "_progress_only": True, "_course_id": course_id, "_progress_revision": str(progress_revision), "reset_epoch": str(reset_epoch), "_attempt_reset_epoch": str(attempt_epoch)}
        doc, state = await load_state(request, scope, initialize=True)
        try:
            added_recipient_profile = False
            share_committed = False
            revoke_committed = False
            accept_committed = False
            lesson_repository_committed = False
            if repo and command.type == "course.share":
                recipient_id = str(command.payload.get("recipientId") or "").strip()
                if not recipient_id or actor_id != scope["account_id"]:
                    raise TreeHouseError("Only the course owner can share", code="forbidden", status=403)
                added_recipient_profile = recipient_id not in state["profiles"]
                ensure_account_profile(state, recipient_id)
            if repo and command.type == "course.accept_share":
                token = str(command.payload.get("shareToken") or "")
                grant = repo.grant_for_token(scope["workspace_id"], token)
                if not grant or grant["recipient_account_id"] != scope["account_id"]:
                    raise TreeHouseError("Share link is invalid", code="share_not_found", status=404)
                ensure_account_profile(state, scope["account_id"])
            command_payload = dict(command.payload)
            if command.type == "lesson.attach_source":
                # Lesson attachments are TreeHouse semantic mutations.  The
                # Files preparation receipt carries provenance, while this
                # catalogue stores only the bounded receipt and lesson IDs.
                course_id = str(command.payload.get("courseId") or scope.get("_course_id") or "").strip()
                lesson_id = str(command.payload.get("lessonId") or "").strip()
                preparation_id = str(command.payload.get("preparationReceiptId") or "").strip()
                operation_id = str(command.payload.get("operationId") or "").strip()
                if not course_id or not lesson_id or not preparation_id or not operation_id or len(preparation_id) > 128 or len(operation_id) > 128:
                    raise HTTPException(422, detail={"code": "invalid_lesson_attachment", "message": "Lesson attachment receipt is invalid"})
                if repo:
                    owner_id = str(scope.get("_treehouse_owner") or scope["account_id"])
                    if not repo.access(scope["account_id"], scope["workspace_id"], owner_id, course_id, "edit"):
                        raise HTTPException(403, detail={"code": "lesson_attachment_forbidden", "message": "Editor access is required for lesson attachments"})
                    expected_grant_revision = int(scope.get("_grant_revision") or 0)
                    if command.payload.get("expectedRevision") is not None and str(command.payload.get("expectedRevision")) != str(expected_grant_revision):
                        raise HTTPException(409, detail={"code": "lesson_access_changed", "message": "Lesson access changed; retry from the preparation receipt"})
                activity = (state.get("activities") or {}).get(lesson_id)
                if not isinstance(activity, dict) or str(activity.get("courseId") or "") != course_id:
                    raise HTTPException(404, detail={"code": "lesson_not_found", "message": "Lesson is not in this course"})
                if repo:
                    next_state = copy.deepcopy(state)
                    attachment = {"operationId": operation_id, "preparationReceiptId": preparation_id, "courseId": course_id, "lessonId": lesson_id, "mode": str(command.payload.get("mode") or "link")}
                    next_state.setdefault("extensions", {}).setdefault("lessonAttachments", {})[operation_id] = attachment
                    next_state["activities"][lesson_id] = copy.deepcopy(next_state["activities"][lesson_id])
                    attachments = next_state["activities"][lesson_id].setdefault("sourceAttachments", [])
                    if not any(item.get("operationId") == operation_id for item in attachments if isinstance(item, dict)):
                        attachments.append(attachment)
                    try:
                        persisted = copy.deepcopy(next_state)
                        if owner_id != scope["account_id"]:
                            persisted.get("profiles", {}).pop(scope["account_id"], None)
                        result = repo.commit_lesson_attachment(
                            caller_account_id=scope["account_id"], owner_account_id=owner_id,
                            workspace_id=scope["workspace_id"], course_id=course_id,
                            lesson_id=lesson_id, operation_id=operation_id,
                            preparation_id=preparation_id,
                            expected_catalogue_revision=int(doc.get("head") or 0),
                            expected_grant_revision=expected_grant_revision,
                            expected_policy_generation=attachment_policy.generation(),
                            state=persisted, mode=str(command.payload.get("mode") or "link"),
                        )
                    except TreeHouseRepositoryError as exc:
                        raise repository_error(exc) from exc
                    changed = result.get("outcome") == "committed"
                    lesson_repository_committed = True
                    next_state["revision"] = int(result.get("revision") or next_state.get("revision") or 0)
                else:
                    extensions = state.setdefault("extensions", {})
                    receipts = extensions.setdefault("lessonAttachments", {})
                    prior = receipts.get(operation_id)
                    if isinstance(prior, dict):
                        next_state, result, changed = state, {"outcome": "replayed", **prior}, False
                    elif command.expected_revision is not None and int(command.expected_revision) != int(state.get("revision") or 0):
                        raise HTTPException(409, detail={"code": "stale", "message": "TreeHouse changed in another tab", "revision": state.get("revision")})
                    else:
                        next_state = copy.deepcopy(state)
                        next_state.setdefault("extensions", {}).setdefault("lessonAttachments", {})[operation_id] = {"operationId": operation_id, "preparationReceiptId": preparation_id, "courseId": course_id, "lessonId": lesson_id, "mode": str(command.payload.get("mode") or "link")}
                        next_state["activities"][lesson_id] = copy.deepcopy(next_state["activities"][lesson_id])
                        attachments = next_state["activities"][lesson_id].setdefault("sourceAttachments", [])
                        if not any(item.get("operationId") == operation_id for item in attachments if isinstance(item, dict)):
                            attachments.append(next_state["extensions"]["lessonAttachments"][operation_id])
                        next_state["revision"] = int(state.get("revision") or 0) + 1
                        result, changed = {"outcome": "committed", "operationId": operation_id, "preparationReceiptId": preparation_id}, True
            else:
                next_state = None
            if scope.get("_progress_only") and command.type in {"course.open", "activity.complete", "submission.submit", "submission.draft", "evidence.submit"}:
                # The generation is captured before the command starts.  The
                # repository checks it again inside BEGIN IMMEDIATE, so a
                # reset that wins a concurrent race rejects this callback.
                command_payload.setdefault("resetGeneration", int(scope.get("_attempt_reset_epoch") or 0))
            if command.type == "activity.complete":
                activity = state.get("activities", {}).get(str(command_payload.get("activityId") or ""), {})
                if activity.get("completion") == "verified" or activity.get("verifierSpec"):
                    try:
                        state['_activityVerification'] = builtin_activity_verification(repo, scope["account_id"], scope["workspace_id"], activity, guide_owner_id=state.get('courses', {}).get(activity.get('courseId'), {}).get('ownerId'))
                    except ValueError as exc:
                        raise HTTPException(409, detail={"code": "verifier_result_required", "message": str(exc)}) from exc
            if command.type in {"submission.submit", "submission.draft"}:
                assignment = state.get("assignments", {}).get(str(command_payload.get("assignmentId") or ""), {})
                if assignment.get("assessmentType") == "file":
                    current_policy = file_policy(request)
                    factory = getattr(request.app.state, "treehouse_files_facade", None)
                    context_factory = getattr(request.app.state, "treehouse_files_context", None)
                    if not callable(factory) or not callable(context_factory):
                        raise HTTPException(503, detail={"code": "files_unavailable", "message": "Open Files and prepare your submission attachment first"})
                    files_context = context_factory(request, workspace=scope["workspace_id"])
                    files = factory(request, copal_workspace=scope["workspace_id"])
                    validated = set()
                    answer = command_payload.get("answer")
                    receipts = answer.get("fileReceipts", []) if isinstance(answer, dict) else []
                    if not isinstance(receipts, list) or len(receipts) > 16:
                        raise HTTPException(400, "Invalid file receipts")
                    for item in receipts:
                        receipt = repo.lesson_attachment_preparation(caller_account_id=scope["account_id"], workspace_id=scope["workspace_id"], operation_id=str(item.get("operationId") or "")) if repo and isinstance(item, dict) else None
                        if not receipt or receipt.get("targetKind") != "treehouse_submission" or receipt.get("lessonId") != assignment.get("id") or receipt.get("courseId") != assignment.get("courseId") or receipt.get("preparationReceiptId") != item.get("preparationReceiptId"):
                            raise HTTPException(403, detail={"code": "submission_attachment_denied", "message": "File evidence receipt does not belong to this account and task"})
                        if receipt.get("policyGeneration") != current_policy.generation() or receipt.get("catalogueRevision") != int(doc.get("head") or 0) or receipt.get("grantRevision") != int(scope.get("_grant_revision") or 0):
                            raise HTTPException(409, detail={"code": "submission_attachment_stale", "message": "File access or curriculum changed; select and prepare the file again"})
                        try:
                            source = await files.stat(files_context, resource_ref=receipt["source"]["resource_ref"])
                            if source.get("revision") != receipt["source"].get("revision"):
                                raise HTTPException(409, "Submission source changed; prepare it again")
                        except FilesFacadeError as exc:
                            raise HTTPException(403, detail={"code": exc.code, "message": str(exc)}) from exc
                        validated.add(receipt["preparationReceiptId"])
                    state['_validatedSubmissionReceipts'] = sorted(validated)
            if next_state is None:
                next_state, result, changed = apply_treehouse_command(
                    state,
                    {"type": command.type, "payload": command_payload},
                    actor_id=actor_id,
                    command_id=command.command_id,
                    # Recipient progress has its own CAS revision.  The owner
                    # catalogue revision is only the curriculum base and must
                    # not reject a learner's next attempt after a prior
                    # progress write incremented the learner namespace.
                    expected_revision=state["revision"] if (scope.get("_progress_only") or command.type == "course.accept_share") else command.expected_revision,
                )
            if repo and command.type == "course.share" and changed:
                if added_recipient_profile:
                    next_state["profiles"].pop(str(command.payload.get("recipientId")), None)
                grant_result = repo.create_share(
                    owner_account_id=scope["account_id"], workspace_id=scope["workspace_id"],
                    course_id=str(command.payload["courseId"]), recipient_account_id=str(command.payload["recipientId"]),
                    role=str(command.payload.get("capability") or "learn"), access_revision=int(next_state["revision"]),
                    now=datetime.now(UTC).isoformat(), command_id=command.command_id, payload=command.payload,
                    state=next_state, expected_catalogue_revision=int(doc.get("head") or 0),
                )
                share_committed = True
                # Replace the compatibility event's local token with the
                # globally indexed opaque link.  The hash is never exposed.
                replacement_key = None
                replacement_grant = None
                for grant_key, grant in next_state.get("courseGrants", {}).items():
                    if grant.get("courseId") == command.payload.get("courseId") and grant.get("recipientId") == command.payload.get("recipientId") and not grant.get("revokedAt"):
                        replacement_key = grant_key
                        replacement_grant = grant
                        grant.update({"id": grant_result["grantId"], "grantId": grant_result["grantId"], "shareToken": grant_result["shareToken"], "tokenHash": hashlib.sha256(grant_result["shareToken"].encode()).hexdigest(), "accessRevision": grant_result["accessRevision"]})
                if replacement_key and replacement_key != grant_result["grantId"] and replacement_grant is not None:
                    next_state["courseGrants"].pop(replacement_key, None)
                    next_state["courseGrants"][grant_result["grantId"]] = replacement_grant
                result = {**result, **grant_result}
            elif repo and command.type == "course.accept_share" and changed:
                repo.accept_share(recipient_account_id=scope["account_id"], workspace_id=scope["workspace_id"], token=str(command.payload.get("shareToken") or ""), now=datetime.now(UTC).isoformat())
                accepted = repo.grant(str(scope.get("_grant_id") or ""))
                if accepted:
                    scope = {**scope, "_grant_revision": str(accepted["revision"])}
                # The grant/index transaction is the acceptance event.  The
                # recipient must never rewrite the owner's curriculum
                # catalogue merely to record its own acceptance.
                accept_committed = True
            elif repo and command.type == "course.revoke_share" and changed:
                grant_before = repo.grant(str(command.payload.get("grantId") or ""))
                repo.revoke_share_with_catalogue(
                    owner_account_id=scope["account_id"], workspace_id=scope["workspace_id"],
                    grant_id=str(command.payload.get("grantId") or ""),
                    expected_grant_revision=int(grant_before["revision"]) if grant_before else None,
                    expected_catalogue_revision=int(doc.get("head") or 0), state=next_state,
                    now=datetime.now(UTC).isoformat(),
                )
                revoke_committed = True
            if repo and command.type == "course.share" and added_recipient_profile:
                # Recipient identity lives in auth and the grant index.  It is
                # never copied into the owner's curriculum profile catalogue.
                next_state["profiles"].pop(str(command.payload.get("recipientId")), None)
            next_state.pop("_activityVerification", None)
            state.pop("_activityVerification", None)
            next_state.pop("_validatedSubmissionReceipts", None)
            state.pop("_validatedSubmissionReceipts", None)
            if changed and not share_committed and not revoke_committed and not accept_committed and not lesson_repository_committed:
                await write_state(request, scope, doc, next_state)
            snapshot = engagement_snapshot(next_state, actor_id, request, scope)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        if changed and command.type == "course.publish":
            resume_class_publications(request, scope)
        # S30: a committed Field Guide lesson completion is the N29 lesson-key
        # producer.  Achievement ingestion is best-effort and never fails the
        # learner's completion; the durable record is the domain event above.
        if changed and command.type == "activity.complete" and not command_payload.get("_achievementIngested"):
            try:
                completed_activity = (next_state.get("activities") or {}).get(str(command_payload.get("activityId") or "")) or {}
                lesson_key = str(completed_activity.get("fieldGuideKey") or "")
                if lesson_key:
                    achievement_engine(request).ingest(scope.get("account_id") or actor_id, [{
                        "source_event_id": f"guide-lesson:{lesson_key}:{command.command_id}",
                        "event_family": "guide.lesson.completed",
                        "kind": "R",
                        "result": "committed",
                        "actor_kind": "user",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "facts": {"lessonKey": lesson_key, "committed": True, "classKey": completed_activity.get("classKey")},
                    }], via="live")
            except Exception:
                pass
        return {"ok": True, "changed": changed, "result": result, **snapshot, "accountId": scope.get("account_id"), "workspace": scope.get("workspace_id")}


    @router.get("/integrity")
    async def treehouse_integrity(request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        _, state = await load_state(request, scope, initialize=False)
        try:
            projection = compute_treehouse_projections(state)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        return {"ok": True, "schemaVersion": state["schemaVersion"], "revision": state["revision"], "eventCount": len(state["events"]), "projectionLearners": len(projection["learners"]), "fingerprint": state_fingerprint(state), "accountId": scope.get("account_id"), "workspace": scope.get("workspace_id")}

    # ------------------------------------------------------------------
    # Account-wide achievements (S29)
    #
    # Deterministic receipt predicates only.  Workspace is event context;
    # the partition is the stable account identity.  These endpoints are
    # account-scoped and never award installer/maintenance/seed activity.
    # ------------------------------------------------------------------

    def achievement_engine(request: Request) -> TreeHouseAchievementEngine:
        return TreeHouseAchievementEngine(repository(request))

    def _require_account(request: Request, scope: dict[str, str]) -> str:
        if not account_scoped(request, scope):
            raise HTTPException(401, detail={"code": "not_authenticated", "message": "Achievements require an authenticated account"})
        return str(scope["account_id"])

    @router.get("/stats")
    async def get_personal_stats(request: Request, workspace: str | None = None,
                                 before: int | None = Query(None, ge=1), limit: int = Query(30, ge=1, le=100)):
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        return repository(request).personal_stats(account_id, scope["workspace_id"], before=before, limit=limit)

    @router.get("/achievements")
    async def get_achievements(
        request: Request,
        workspace: str | None = None,
        admin: bool = Query(False),
    ):
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        resume_class_publications(request, scope)
        engine = achievement_engine(request)
        presentation = engine.presentation(account_id, admin=admin)
        return {**presentation, "accountId": account_id, "workspace": scope["workspace_id"]}

    @router.post("/achievements/reset")
    async def reset_achievements(request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        body = await request.json()
        if not isinstance(body, dict) or body.get("confirm") != "reset-achievements":
            raise HTTPException(400, detail={"code": "confirmation_required", "message": "Confirm achievement reset"})
        if body.get("accountId") and body["accountId"] != account_id:
            raise HTTPException(409, detail={"code": "stale_account", "message": "The signed-in account changed"})
        counts = reset_account_activity(account_id, repository=repository(request))
        return {"ok": True, "accountId": account_id, "cleared": counts}

    @router.post("/achievements/events")
    async def ingest_achievement_events(
        request: Request,
        workspace: str | None = None,
    ):
        """Ingest committed operation receipts and authenticated UI acks.

        Body: ``{"events": [...]}``.  Each event carries a stable
        ``source_event_id``, an ``event_family``, ``kind`` (R/U), ``result``,
        ``actor_kind``, ``occurred_at`` and minimal predicate ``facts``.
        Duplicate and out-of-order deliveries never double-award.
        """
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, detail={"code": "invalid_json", "message": "Expected a JSON body"})
        if isinstance(body, dict) and body.get("accountId") and body["accountId"] != account_id:
            raise HTTPException(409, detail={"code": "stale_account", "message": "The signed-in account changed"})
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or not events:
            raise HTTPException(400, detail={"code": "missing_events", "message": "Expected a non-empty events list"})
        if len(events) > 200:
            raise HTTPException(413, detail={"code": "events_too_many", "message": "Ingest at most 200 events per request"})
        if any(isinstance(event, dict) and (event.get("event_family") or event.get("eventFamily")) in {"goal.verified.completed", "class.previewed", "class.published"} for event in events):
            raise HTTPException(403, detail={"code": "trusted_receipt_required", "message": "This receipt must come from its committed workflow"})
        result = achievement_engine(request).ingest(account_id, events, via="live")
        return {"ok": True, "accountId": account_id, **result}

    @router.post("/achievements/backfill")
    async def backfill_achievements(
        request: Request,
        workspace: str | None = None,
    ):
        """Ingest proven historical structured evidence with resumable cursors.

        Body: ``{"sourceFamily": "...", "records": [...], "cursor": {...}}``.
        Prose descriptions, digest-only restore claims and ambiguous owners are
        rejected.  No LLM reads Lore/chat to decide achievements.
        """
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, detail={"code": "invalid_json", "message": "Expected a JSON body"})
        source_family = str(body.get("sourceFamily") or body.get("source_family") or "").strip()
        records = body.get("records")
        if not source_family or not isinstance(records, list):
            raise HTTPException(400, detail={"code": "bad_backfill", "message": "Expected sourceFamily and records"})
        if len(records) > 500:
            raise HTTPException(413, detail={"code": "backfill_too_large", "message": "Backfill at most 500 records per request"})
        if any(isinstance(record, dict) and (record.get("event_family") or record.get("eventFamily")) in {"goal.verified.completed", "class.previewed", "class.published"} for record in records):
            raise HTTPException(403, detail={"code": "trusted_receipt_required", "message": "Resume the authoritative workflow journal to deliver this receipt"})
        cursor = body.get("cursor") if isinstance(body.get("cursor"), dict) else {}
        result = achievement_engine(request).backfill(account_id, source_family, records, cursor=cursor)
        return {"ok": True, "accountId": account_id, "sourceFamily": source_family, **result}

    @router.get("/achievements/notifications")
    async def pending_achievement_notifications(
        request: Request,
        workspace: str | None = None,
        limit: int = Query(20, ge=1, le=50),
    ):
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        repo = repository(request)
        pending = repo.list_pending_notifications(account_id, limit=limit)
        items = []
        for row in pending:
            award = repo.get_achievement_award(account_id, str(row["achievement_id"]))
            evidence = (award or {}).get("evidence") or {}
            definition = None
            try:
                from src.openclank.treehouse_achievements import BY_ID
                definition = BY_ID.get(str(row["achievement_id"]))
            except Exception:
                definition = None
            items.append({
                "outboxId": row["outbox_id"],
                "achievementId": row["achievement_id"],
                "achievementKey": getattr(definition, "key", ""),
                "title": getattr(definition, "title", ""),
                "rarity": getattr(definition, "rarity", "normal"),
                "summary": getattr(definition, "summary", ""),
                "batchId": row.get("batch_id"),
                "state": row["state"],
                # S12 task-identity: when the award evidence names a task and
                # its durable session, the toast opens that ORIGINAL task chat.
                "taskId": evidence.get("taskId") or (award or {}).get("evidence", {}).get("taskId"),
                "sessionId": evidence.get("sessionId"),
                "earnedAt": (award or {}).get("earned_at"),
            })
        return {"notifications": items, "accountId": account_id}

    @router.post("/achievements/notifications/{outbox_id}/claim")
    async def claim_achievement_notification(outbox_id: str, request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        return {"claimed": repository(request).claim_notification(account_id, outbox_id), "accountId": account_id}

    @router.post("/achievements/notifications/{outbox_id}/delivered")
    async def mark_achievement_notification_delivered(outbox_id: str, request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        repo = repository(request)
        if repo.owns_notification(account_id, outbox_id):
            return {"ok": True, **repo.mark_notification_delivered(outbox_id)}
        raise HTTPException(404, detail={"code": "notification_not_found", "message": "No pending notification with that id"})

    @router.post("/achievements/notifications/{outbox_id}/failed")
    async def mark_achievement_notification_failed(outbox_id: str, request: Request, workspace: str | None = None):
        """Record a toast failure.  The durable award is never erased."""
        scope = account_scope(request, scope_for(request, workspace))
        account_id = _require_account(request, scope)
        repo = repository(request)
        if repo.owns_notification(account_id, outbox_id):
            return {"ok": True, **repo.mark_notification_failed(outbox_id)}
        raise HTTPException(404, detail={"code": "notification_not_found", "message": "No pending notification with that id"})

    @router.get("/courses/{course_id}/activities/{activity_id}/resources/{operation_id}")
    async def lesson_resource(course_id: str, activity_id: str, operation_id: str, request: Request, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        repo = repository(request)
        owner = repo.owner_for_course(scope["account_id"], scope["workspace_id"], course_id)
        if not owner or not repo.access(scope["account_id"], scope["workspace_id"], owner, course_id, "learn"):
            raise HTTPException(403, "Course resource access is unavailable")
        state, _ = repo.get_catalogue(owner, scope["workspace_id"])
        activity = (state or {}).get("activities", {}).get(activity_id, {})
        if activity.get("courseId") != course_id or activity.get("deletedAt") or not any(x.get("operationId") == operation_id for x in activity.get("sourceAttachments", [])):
            raise HTTPException(404, "Prepared lesson resource not found")
        receipt = repo.lesson_attachment_preparation(caller_account_id=scope["account_id"], workspace_id=scope["workspace_id"], operation_id=operation_id)
        factory = getattr(request.app.state, "treehouse_files_facade", None)
        context_factory = getattr(request.app.state, "treehouse_files_context", None)
        if not receipt or receipt.get("targetKind", "treehouse_lesson") != "treehouse_lesson" or not callable(factory) or not callable(context_factory):
            raise HTTPException(403, "Source resource requires current Files authorization; reopen Files or ask the author to adopt a shareable asset")
        ctx = context_factory(request, workspace=scope["workspace_id"])
        try:
            resource = await factory(request, copal_workspace=scope["workspace_id"]).stat(ctx, resource_ref=receipt["source"]["resource_ref"])
            if resource.get("revision") != receipt["source"].get("revision"):
                raise HTTPException(409, "Resource changed; ask the author to prepare it again")
        except FilesFacadeError as exc:
            raise HTTPException(403, detail={"code": exc.code, "message": str(exc)}) from exc
        return {"name": resource.get("name"), "mimeType": resource.get("mime_type"), "resourceRef": resource.get("ref") or resource.get("resource_ref")}

    @router.api_route("/courses/{course_id}/submissions/{submission_id}/files/{operation_id}", methods=["GET", "HEAD"])
    async def submitted_file_content(course_id: str, submission_id: str, operation_id: str, request: Request, attemptId: str, workspace: str | None = None):
        scope = account_scope(request, scope_for(request, workspace))
        repo = repository(request)
        owner = repo.owner_for_course(scope["account_id"], scope["workspace_id"], course_id)
        if not owner or not repo.access(scope["account_id"], scope["workspace_id"], owner, course_id, "edit"):
            raise HTTPException(403, "Submitted evidence requires current course review access")
        rows = repo.course_progress_for_review(scope["account_id"], owner, scope["workspace_id"], course_id)
        selected = next((row for row in rows if submission_id in row["state"].get("submissions", {})), None)
        submission = selected["state"]["submissions"][submission_id] if selected else None
        if not submission or submission.get("courseId") != course_id or submission.get("status") not in {"submitted", "graded"} or submission.get("attemptId") != attemptId:
            raise HTTPException(409, "This submitted attempt changed; reopen the review")
        learner = selected["learnerAccountId"]
        answer = next((item.get("answer") for item in submission.get("attemptHistory", []) if item.get("attemptId") == attemptId), None)
        item = next((item for item in (answer or {}).get("fileReceipts", []) if isinstance(item, dict) and item.get("operationId") == operation_id), None) if isinstance(answer, dict) else None
        receipt = repo.lesson_attachment_preparation(caller_account_id=learner, workspace_id=scope["workspace_id"], operation_id=operation_id)
        if not item or not receipt or receipt.get("targetKind") != "treehouse_submission" or receipt.get("courseId") != course_id or receipt.get("lessonId") != submission.get("assignmentId") or receipt.get("preparationReceiptId") != item.get("preparationReceiptId"):
            raise HTTPException(403, "File is not evidence in this submitted attempt")
        catalogue, _ = repo.get_catalogue(owner, scope["workspace_id"])
        task = (catalogue or {}).get("assignments", {}).get(submission.get("assignmentId"), {})
        if task.get("courseId") != course_id or task.get("deletedAt"):
            raise HTTPException(403, "Submitted task is no longer available")
        policy = file_policy(request)
        manager = getattr(request.app.state, "auth_manager", None)
        username = manager.username_for_account_id(learner) if manager else None
        content_response = getattr(request.app.state, "treehouse_files_content_response", None)
        if not username or not callable(content_response):
            raise HTTPException(503, "Submitted evidence source is unavailable; reopen Files")
        grant = repo.active_grant(learner, scope["workspace_id"], owner, course_id) if learner != owner else None
        if receipt.get("policyGeneration") != policy.generation() or (learner != owner and (not grant or int(grant["revision"]) != receipt.get("grantRevision"))):
            raise HTTPException(403, "Submitted evidence source access changed or was revoked")
        def still_authorized():
            current, _ = repo.get_progress(learner, owner, scope["workspace_id"], course_id)
            current_submission = (current or {}).get("submissions", {}).get(submission_id, {})
            current_grant = repo.active_grant(learner, scope["workspace_id"], owner, course_id) if learner != owner else None
            return bool(manager.username_for_account_id(learner) == username and manager.is_admin(username) == context.is_admin and repo.access(scope["account_id"], scope["workspace_id"], owner, course_id, "edit") and repo.access(learner, scope["workspace_id"], owner, course_id, "learn") and (learner == owner or (current_grant and int(current_grant["revision"]) == receipt.get("grantRevision"))) and current_submission.get("attemptId") == attemptId and current_submission.get("status") in {"submitted", "graded"})
        context = ProviderContext(owner_subject_id=learner, owner_username=username, policy_generation=policy.generation(), is_admin=manager.is_admin(username), workspace_id=scope["workspace_id"])
        # This internal context never escapes: the existing Files content plane
        # opens only the immutable submitted receipt's source and revision.
        return await content_response(receipt["source"]["resource_ref"], request, "download", source_context=context, expected_revision=receipt["source"]["revision"], access_guard=still_authorized)

    router.include_router(setup_rich_learning_routes(repository=repository, scope_for=scope_for, account_scope=account_scope, aggregate_visible_state=aggregate_visible_state, publish=publish))
    router.include_router(setup_learning_extensions_routes(repository=repository, scope_for=scope_for, account_scope=account_scope, aggregate_visible_state=aggregate_visible_state, overlay_progress=overlay_progress, publish=publish))
    router.include_router(create_engagement_router(scope_for=scope_for, account_scope=account_scope, repository=repository))
    return router
