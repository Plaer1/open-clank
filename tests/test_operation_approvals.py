"""Canonical OperationApproval cutover (S09) contract tests.

Covers the runtime write/read lane in ``src/openclank/operation_approvals.py``:
typed ``operation`` PolicyBindings replace new compatibility ``permission_grants``
rows whenever the immutable owner subject and a containing enabled Location are
resolvable, and degrade to the measured compatibility fallback otherwise.
"""

import hashlib
import json
import os

import pytest

from src.openclank.file_policy import FilePolicyRepository
from src.openclank.operation_approvals import (
    ReadOnlyAuthSnapshot,
    approval_resource_ref,
    capability_for_permission,
    match_operation_approval,
    record_operation_approval,
)


@pytest.fixture()
def authority(tmp_path):
    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    auth_path = authority_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "users": {
                    "alice": {"account_id": "alice-id", "is_admin": False},
                    "bob": {"account_id": "bob-id", "is_admin": False},
                }
            }
        ),
        encoding="utf-8",
    )
    repository = FilePolicyRepository(authority_dir / "app.db")
    root = tmp_path / "root"
    root.mkdir()
    location = repository.create_location(
        actor_subject_id="admin-id",
        path=str(root),
        kind="directory",
        capabilities=("read", "write"),
    )
    workspace = repository.create_workspace(
        actor_subject_id="alice-id",
        owner_subject_id="alice-id",
        location_id=location.id,
        name="Trusted workspace",
    )
    auth = ReadOnlyAuthSnapshot(str(auth_path))
    return repository, location, workspace, auth, root


def test_resource_ref_matches_migration_digest_scheme():
    permission_type = "write"
    pattern = "/tmp/work"
    resource = "abc"
    expected = hashlib.sha256(
        (permission_type + "\0" + pattern + "\0" + resource).encode("utf-8")
    ).hexdigest()
    assert approval_resource_ref(permission_type, pattern, resource) == (
        f"legacy-resource-{expected}"
    )


def test_capability_mapping_covers_the_three_lanes():
    assert capability_for_permission("native-file-mutation") == "write"
    assert capability_for_permission("native-shell-destructive") == "execute"
    assert capability_for_permission("write") == "write"
    assert capability_for_permission("read_file") == "read"
    assert capability_for_permission("mystery-permission") is None


def test_chat_round_trip_and_cross_chat_isolation(authority):
    repository, _, _, auth, root = authority
    assert record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-1",
        lifetime="chat",
        session_id="chat-1",
        target_path=str(root),
        repository=repository,
        auth=auth,
    )
    assert match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-1",
        session_id="chat-1",
        workspace_path=str(root),
        repository=repository,
        auth=auth,
    )
    # A different chat must not inherit the approval.
    assert not match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-1",
        session_id="chat-2",
        workspace_path=str(root),
        repository=repository,
        auth=auth,
    )
    # A different exact operation is not covered.
    assert not match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-2",
        session_id="chat-1",
        workspace_path=str(root),
        repository=repository,
        auth=auth,
    )


def test_workspace_round_trip_requires_stable_workspace(authority):
    repository, _, workspace, auth, root = authority
    other = repository.create_workspace(
        actor_subject_id="alice-id",
        owner_subject_id="alice-id",
        location_id=workspace.location_id,
        name="Other workspace",
        relative_folder="other",
    )
    assert record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-ws",
        lifetime="workspace",
        workspace_id=workspace.id,
        repository=repository,
        auth=auth,
    )
    assert match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-ws",
        session_id="any-chat",
        workspace_id=workspace.id,
        repository=repository,
        auth=auth,
    )
    assert not match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-ws",
        session_id="any-chat",
        workspace_id=other.id,
        repository=repository,
        auth=auth,
    )
    # Workspace lifetime degrades to Once without a stable ID: nothing durable.
    assert not record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-ws-2",
        lifetime="workspace",
        target_path=str(root),
        repository=repository,
        auth=auth,
    )


def test_always_carries_no_hidden_scope(authority):
    repository, _, workspace, auth, root = authority
    assert record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-always",
        lifetime="always",
        session_id="chat-1",
        workspace_id=workspace.id,
        target_path=str(root),
        repository=repository,
        auth=auth,
    )
    binding = repository.list_bindings(
        subject_id="alice-id", binding_class="operation"
    )[-1]
    assert binding.lifetime == "always"
    assert binding.chat_id is None
    assert binding.workspace_id is None
    # Works from any chat and any workspace inside the same Location.
    assert match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-always",
        session_id="chat-99",
        workspace_path=str(root),
        repository=repository,
        auth=auth,
    )


def test_acp_directory_pattern_matches_descendants_not_siblings(authority):
    repository, location, _, auth, root = authority
    sub = root / "sub"
    sub.mkdir()
    target = sub / "file.txt"
    target.write_text("x", encoding="utf-8")
    pattern = os.path.dirname(str(target))
    assert record_operation_approval(
        owner="alice",
        permission_type="write",
        pattern=pattern,
        lifetime="always",
        target_path=str(root),
        repository=repository,
        auth=auth,
    )
    # Exact directory and its descendants match.
    assert match_operation_approval(
        owner="alice",
        permission_type="write",
        filepath=str(target),
        repository=repository,
        auth=auth,
    )
    nested = sub / "deep" / "deeper.txt"
    nested.parent.mkdir()
    nested.write_text("x", encoding="utf-8")
    assert match_operation_approval(
        owner="alice",
        permission_type="write",
        filepath=str(nested),
        repository=repository,
        auth=auth,
    )
    # Siblings outside the approved directory do not.
    sibling = root / "other.txt"
    sibling.write_text("x", encoding="utf-8")
    assert not match_operation_approval(
        owner="alice",
        permission_type="write",
        filepath=str(sibling),
        repository=repository,
        auth=auth,
    )


def test_unresolvable_contexts_fall_back_without_writing(authority):
    repository, _, _, auth, root = authority
    # Unknown owner: no immutable subject, no canonical write.
    assert not record_operation_approval(
        owner="mallory",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-x",
        lifetime="always",
        target_path=str(root),
        repository=repository,
        auth=auth,
    )
    # Path outside every Location: no canonical write.
    outside = root.parent / "elsewhere"
    outside.mkdir()
    assert not record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-x",
        lifetime="always",
        target_path=str(outside),
        repository=repository,
        auth=auth,
    )
    assert not match_operation_approval(
        owner="mallory",
        permission_type="native-file-mutation",
        resource="binding-x",
        workspace_path=str(root),
        repository=repository,
        auth=auth,
    )
    assert repository.list_bindings(binding_class="operation") == []


def test_shell_approval_requires_execute_capability(authority):
    repository, _, _, auth, root = authority
    # The fixture Location grants read/write only; a shell approval must
    # refuse the canonical lane rather than escalate.
    assert not record_operation_approval(
        owner="alice",
        permission_type="native-shell-destructive",
        pattern="*",
        resource="binding-shell",
        lifetime="always",
        target_path=str(root),
        repository=repository,
        auth=auth,
    )
    execute_location = repository.create_location(
        actor_subject_id="admin-id",
        path=str(root / "exec"),
        kind="directory",
        capabilities=("read", "write", "execute"),
    )
    (root / "exec").mkdir(exist_ok=True)
    assert record_operation_approval(
        owner="alice",
        permission_type="native-shell-destructive",
        pattern="*",
        resource="binding-shell",
        lifetime="always",
        target_path=str(root / "exec"),
        repository=repository,
        auth=auth,
    )
    binding = repository.list_bindings(
        subject_id="alice-id", binding_class="operation"
    )[-1]
    assert binding.location_id == execute_location.id
    assert match_operation_approval(
        owner="alice",
        permission_type="native-shell-destructive",
        resource="binding-shell",
        workspace_path=str(root / "exec"),
        repository=repository,
        auth=auth,
    )


def test_expired_approval_never_matches(authority):
    repository, _, _, auth, root = authority
    assert record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-exp",
        lifetime="always",
        target_path=str(root),
        expires_at="2000-01-01T00:00:00+00:00",
        repository=repository,
        auth=auth,
    )
    assert not match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-exp",
        workspace_path=str(root),
        repository=repository,
        auth=auth,
    )


def test_reset_scopes_revoke_canonical_operation_approvals(authority):
    repository, location, workspace, auth, root = authority
    record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-chat",
        lifetime="chat",
        session_id="chat-1",
        target_path=str(root),
        repository=repository,
        auth=auth,
    )
    record_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        pattern="*",
        resource="binding-ws",
        lifetime="workspace",
        workspace_id=workspace.id,
        repository=repository,
        auth=auth,
    )
    summary = repository.reset_agent_permissions(
        actor_subject_id="alice-id",
        subject_id="alice-id",
        scope="chat",
        chat_id="chat-1",
    )
    assert summary["matched"] == 1
    assert not match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-chat",
        session_id="chat-1",
        workspace_path=str(root),
        repository=repository,
        auth=auth,
    )
    # The workspace-scoped approval survived the chat reset...
    assert match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-ws",
        workspace_id=workspace.id,
        repository=repository,
        auth=auth,
    )
    # ...and falls to the location reset.
    summary = repository.reset_agent_permissions(
        actor_subject_id="alice-id",
        subject_id="alice-id",
        scope="location",
        location_id=location.id,
    )
    assert summary["matched"] == 1
    assert not match_operation_approval(
        owner="alice",
        permission_type="native-file-mutation",
        resource="binding-ws",
        workspace_id=workspace.id,
        repository=repository,
        auth=auth,
    )
