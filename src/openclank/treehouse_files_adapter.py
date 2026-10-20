"""TreeHouse semantic target for the Files attachment preparation seam.

TreeHouse lessons are not Files resources.  This adapter only authorizes the
lesson and records a bounded source descriptor in the TreeHouse repository;
the lesson aggregate is changed later by its own CAS command.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.openclank.files_facade import FilesFacadeError, ProviderContext, ProviderResource
from src.openclank.copal_treehouse_repository import TreeHouseRepository, TreeHouseRepositoryError


class TreeHouseLessonAttachmentTarget:
    """Prepare a source for one authorized editable lesson."""

    name = "treehouse_lesson"
    capability = "edit"
    collection = "activities"

    def __init__(self, repository: TreeHouseRepository):
        self.repository = repository

    @staticmethod
    def _revision(value: Any, field: str) -> str:
        if not isinstance(value, Mapping) or set(value) != {"kind", "value"}:
            raise FilesFacadeError(f"TreeHouse {field} revision is required", code="resource_ref_stale")
        kind, token = str(value.get("kind") or "").strip(), str(value.get("value") or "").strip()
        if not kind or not token or len(token) > 512:
            raise FilesFacadeError(f"TreeHouse {field} revision is invalid", code="resource_ref_stale")
        return token

    def _expected_revisions(self, target: Mapping[str, Any]) -> tuple[int, int]:
        expected = target.get("expected_revision")
        if not isinstance(expected, Mapping):
            raise FilesFacadeError("TreeHouse lesson revision is required", code="resource_ref_stale")
        kind = str(expected.get("kind") or "").strip()
        value = str(expected.get("value") or "").strip()
        if kind == "treehouse" and value:
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise FilesFacadeError("TreeHouse lesson revision is invalid", code="resource_ref_stale") from exc
            if not isinstance(decoded, Mapping):
                raise FilesFacadeError("TreeHouse lesson revision is invalid", code="resource_ref_stale")
            try:
                grant_revision, catalogue_revision = int(decoded["grantRevision"]), int(decoded["catalogueRevision"])
                if grant_revision < 0 or catalogue_revision < 0:
                    raise ValueError("negative revision")
                return grant_revision, catalogue_revision
            except (KeyError, TypeError, ValueError) as exc:
                raise FilesFacadeError("TreeHouse lesson revision is invalid", code="resource_ref_stale") from exc
        # Keep the first client generation compatible while requiring the
        # catalogue revision from the target context when available.
        if kind != "accessRevision" or not value:
            raise FilesFacadeError("TreeHouse lesson revision is invalid", code="resource_ref_stale")
        try:
            return int(value), int(target.get("catalogue_revision"))
        except (TypeError, ValueError) as exc:
            raise FilesFacadeError("TreeHouse catalogue revision is required", code="resource_ref_stale") from exc

    async def operation_status(self, context: ProviderContext, *, operation_id: str) -> Mapping[str, Any] | None:
        """Recover a preparation when the Files response was lost."""
        value = self.repository.lesson_attachment_preparation(
            caller_account_id=context.owner_subject_id,
            workspace_id=str(getattr(context, "workspace_id", "default") or "default"),
            operation_id=str(operation_id),
        )
        if not value or value.get("targetKind", "treehouse_lesson") != self.name:
            return None
        source = value.get("source") if isinstance(value.get("source"), Mapping) else {}
        grant_revision = int(value.get("grantRevision") or 0)
        catalogue_revision = int(value.get("catalogueRevision") or 0)
        return {
            "preparation_receipt_id": value.get("preparationReceiptId"),
            "source_revision": source.get("revision") or {"kind": "provider", "value": "unknown"},
            "target_identity": {"kind": self.name, "course_id": value.get("courseId"), ("assignment_id" if self.name == "treehouse_submission" else "lesson_id"): value.get("lessonId")},
            "target_revision": {"kind": "treehouse", "value": json.dumps({"grantRevision": grant_revision, "catalogueRevision": catalogue_revision}, separators=(",", ":"))},
            "insertion": {"format": "markdown", "link_target": f"treehouse://{value.get('workspaceId', getattr(context, 'workspace_id', 'default'))}/{value.get('courseId')}/{value.get('lessonId')}/{value.get('preparationReceiptId')}", "label": str(source.get("name") or "resource")[:240], "media_kind": str(source.get("mime_type") or "application/octet-stream")[:128]},
            "source_digest": str(value.get("sourceDigest") or "") or None,
        }

    async def prepare_attachment(
        self,
        context: ProviderContext,
        *,
        source: Mapping[str, Any],
        target: Mapping[str, Any],
        mode: str,
        operation_id: str,
        source_provider: Any | None = None,
        source_origin_id: str | None = None,
        source_entry: ProviderResource | None = None,
        **_: Any,
    ) -> Mapping[str, Any]:
        if str(mode or "").strip().lower() not in {"link", "embed"}:
            raise FilesFacadeError("attachment mode is invalid", code="invalid_resource_request")
        if source_provider is None or source_entry is None or not source_origin_id:
            raise FilesFacadeError("attachment source is unavailable", code="resource_unavailable")
        course_id = str(target.get("course_id") or "").strip()
        lesson_id = str(target.get("assignment_id" if self.name == "treehouse_submission" else "lesson_id") or "").strip()
        if not course_id or not lesson_id:
            raise FilesFacadeError("TreeHouse lesson target is invalid", code="invalid_resource_request")
        grant_revision, catalogue_revision = self._expected_revisions(target)
        workspace = str(getattr(context, "workspace_id", "default") or "default")
        owner = self.repository.owner_for_course(context.owner_subject_id, workspace, course_id)
        if not owner or not self.repository.access(context.owner_subject_id, workspace, owner, course_id, self.capability):
            raise FilesFacadeError("Lesson editor access is unavailable", code="resource_unavailable")
        state, current_catalogue_revision = self.repository.get_catalogue(owner, workspace)
        if not state or int(current_catalogue_revision) != catalogue_revision:
            raise FilesFacadeError("TreeHouse catalogue changed; retry the lesson attachment", code="resource_ref_stale")
        activity = (state.get(self.collection) or {}).get(lesson_id)
        if not isinstance(activity, Mapping) or str(activity.get("courseId") or "") != course_id:
            raise FilesFacadeError("TreeHouse lesson is unavailable", code="resource_unavailable")
        if self.name == "treehouse_submission" and (activity.get("status") != "published" or state["courses"][course_id].get("status") != "published"):
            raise FilesFacadeError("Publish the course and task before preparing learner evidence", code="resource_unavailable")
        grant = self.repository.active_grant(context.owner_subject_id, workspace, owner, course_id)
        if owner != context.owner_subject_id and (not grant or (self.capability == "edit" and grant.get("role") != "edit") or int(grant.get("revision", -1)) != grant_revision):
            raise FilesFacadeError("Lesson editor access changed; retry the attachment", code="resource_ref_stale")
        source_revision = source_entry.revision or {"kind": "provider", "value": hashlib.sha256(source_origin_id.encode()).hexdigest()[:32]}
        if not isinstance(source_revision, Mapping) or set(source_revision) != {"kind", "value"} or not str(source_revision.get("kind") or "").strip() or not str(source_revision.get("value") or "").strip():
            raise FilesFacadeError("attachment source revision is unavailable", code="resource_changed")
        source_revision = {"kind": str(source_revision["kind"]).strip(), "value": str(source_revision["value"]).strip()}
        source_descriptor = {
            "resource_ref": str(source.get("resource_ref") or ""),
            "resource_key": str(source.get("resource_key") or ""),
            "provider": str(getattr(source_provider, "name", "") or ""),
            "name": str(source_entry.name or "resource")[:240],
            "mime_type": str(source_entry.mime_type or "application/octet-stream")[:128],
            "revision": dict(source_revision),
        }
        source_digest = hashlib.sha256(json.dumps(source_descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            receipt = self.repository.prepare_lesson_attachment(
                caller_account_id=context.owner_subject_id,
                owner_account_id=owner,
                workspace_id=workspace,
                course_id=course_id,
                lesson_id=lesson_id,
            operation_id=operation_id,
            grant_revision=grant_revision,
            catalogue_revision=catalogue_revision,
            policy_generation=int(context.policy_generation),
            source=source_descriptor,
                source_digest=source_digest,
                mode=str(mode).strip().lower(), target_kind=self.name,
            )
        except TreeHouseRepositoryError as exc:
            raise FilesFacadeError(str(exc), code=exc.code) from exc
        return {
            "preparation_receipt_id": receipt["preparationReceiptId"],
            "source_revision": source_revision,
            "target_identity": {"kind": self.name, "course_id": course_id, ("assignment_id" if self.name == "treehouse_submission" else "lesson_id"): lesson_id},
            "target_revision": {"kind": "treehouse", "value": json.dumps({"grantRevision": grant_revision, "catalogueRevision": catalogue_revision}, separators=(",", ":"))},
            "insertion": {"format": "markdown", "link_target": f"treehouse://{workspace}/{course_id}/{lesson_id}/{receipt['preparationReceiptId']}", "label": source_descriptor["name"], "media_kind": source_descriptor["mime_type"]},
            "source_digest": source_digest,
        }


class TreeHouseSubmissionAttachmentTarget(TreeHouseLessonAttachmentTarget):
    """Prepare learner-owned file evidence without editing the curriculum."""
    name = "treehouse_submission"
    capability = "learn"
    collection = "assignments"
