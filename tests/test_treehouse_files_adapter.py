from __future__ import annotations

import json

import pytest

from src.openclank.copal_treehouse import new_treehouse_state
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.files_facade import FilesFacadeError, ProviderContext, ProviderResource
from src.openclank.treehouse_files_adapter import TreeHouseLessonAttachmentTarget


def _state():
    state = new_treehouse_state("acct-owner")
    state["profiles"]["acct-owner"] = state["profiles"].pop("owner")
    state["courses"]["course:one"] = {"id": "course:one", "ownerId": "acct-owner", "authorIds": ["acct-owner"], "status": "published", "moduleIds": []}
    state["activities"]["lesson:one"] = {"id": "lesson:one", "courseId": "course:one", "moduleId": "module:one", "title": "One"}
    return state


class _SourceProvider:
    name = "host"


def _source():
    return ProviderResource(
        "host-origin", "lesson.md", "file", ("stat", "read", "download"),
        mime_type="text/markdown", revision={"kind": "hostFingerprint", "value": "r1"},
    )


def _target(repo, account="acct-owner", workspace="school"):
    return TreeHouseLessonAttachmentTarget(repo), ProviderContext(
        owner_subject_id=account, owner_username=account, policy_generation=4, workspace_id=workspace,
    )


@pytest.mark.asyncio
async def test_lesson_preparation_is_durable_and_commit_replays(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    state = _state()
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    target, context = _target(repo)
    prepared = await target.prepare_attachment(
        context, source={"resource_ref": "sealed-source"},
        target={"kind": "treehouse_lesson", "course_id": "course:one", "lesson_id": "lesson:one", "expected_revision": {"kind": "treehouse", "value": json.dumps({"grantRevision": 0, "catalogueRevision": 0})}},
        mode="link", operation_id="lesson-op", source_provider=_SourceProvider(), source_origin_id="host-origin", source_entry=_source(),
    )
    assert prepared["preparation_receipt_id"]
    committed = repo.commit_lesson_attachment(
        caller_account_id="acct-owner", owner_account_id="acct-owner", workspace_id="school",
        course_id="course:one", lesson_id="lesson:one", operation_id="lesson-op",
        preparation_id=prepared["preparation_receipt_id"], expected_catalogue_revision=0,
        expected_grant_revision=0, state=state, mode="link",
    )
    replay = repo.commit_lesson_attachment(
        caller_account_id="acct-owner", owner_account_id="acct-owner", workspace_id="school",
        course_id="course:one", lesson_id="lesson:one", operation_id="lesson-op",
        preparation_id=prepared["preparation_receipt_id"], expected_catalogue_revision=0,
        expected_grant_revision=0, state=state, mode="link",
    )
    assert committed["outcome"] == "committed"
    assert replay["outcome"] == "replayed"
    stored, revision = repo.get_catalogue("acct-owner", "school")
    assert revision == 1 and len(stored["activities"]["lesson:one"]["sourceAttachments"]) == 1


@pytest.mark.asyncio
async def test_lesson_preparation_rejects_catalogue_change(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    state = _state()
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    target, context = _target(repo)
    with pytest.raises(FilesFacadeError) as error:
        await target.prepare_attachment(
            context, source={"resource_ref": "sealed-source"},
            target={"kind": "treehouse_lesson", "course_id": "course:one", "lesson_id": "lesson:one", "expected_revision": {"kind": "treehouse", "value": json.dumps({"grantRevision": 0, "catalogueRevision": 8})}},
            mode="link", operation_id="stale-op", source_provider=_SourceProvider(), source_origin_id="host-origin", source_entry=_source(),
        )
    assert getattr(error.value, "code", None) == "resource_ref_stale"
