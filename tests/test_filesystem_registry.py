import json

import pytest

from src.openclank.filesystem_registry import FilesystemRegistryError, FilesystemRootRegistry


def test_registry_persists_owner_scoped_recursive_and_exact_roots(tmp_path):
    folder = tmp_path / "workspace"
    folder.mkdir()
    file = folder / "main.rs"
    file.write_text("fn main() {}", encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    recursive = registry.add("owner-1", str(folder), "recursive_directory", ["read", "write"])
    exact = registry.add("owner-1", str(file), "exact_file", ["read"])
    assert [root["id"] for root in registry.list("owner-1")] == sorted([recursive["id"], exact["id"]], key=lambda value: next(root["display_path"] for root in registry.list("owner-1") if root["id"] == value).casefold())
    assert registry.list("owner-2") == []
    stored = json.loads((tmp_path / "roots.json").read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert stored["roots"][recursive["id"]]["kind"] == "recursive_directory"


def test_registry_allows_explicit_filesystem_root_and_rejects_duplicates(tmp_path):
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    filesystem_root = registry.add("owner-1", "/", "recursive_directory", ["read"])
    assert filesystem_root["canonical_path"] == "/"
    folder = tmp_path / "workspace"
    folder.mkdir()
    registry.add("owner-1", str(folder), "recursive_directory", ["read"])
    with pytest.raises(FilesystemRegistryError) as error:
        registry.add("owner-1", str(folder), "recursive_directory", ["read"])
    assert error.value.code == "duplicate_root"


def test_registry_disable_and_remove_are_owner_scoped(tmp_path):
    folder = tmp_path / "workspace"
    folder.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("owner-1", str(folder), "recursive_directory", ["read"])
    updated = registry.update("owner-1", root["id"], enabled=False)
    assert updated["enabled"] is False
    with pytest.raises(FilesystemRegistryError) as error:
        registry.update("owner-2", root["id"], enabled=True)
    assert error.value.code == "root_not_found"
    registry.remove("owner-1", root["id"])
    assert registry.list("owner-1") == []


def test_disable_projection_narrows_all_derived_rows_in_one_generation(tmp_path):
    shared = tmp_path / "shared"
    unrelated = tmp_path / "unrelated"
    shared.mkdir()
    unrelated.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    admin_root = registry.add("admin", str(shared), "recursive_directory", ["read", "write"])
    projection_root = registry.add("__openclank_app_visibility__", str(shared), "recursive_directory", ["read"])
    unrelated_root = registry.add("admin", str(unrelated), "recursive_directory", ["read"])
    shared_assignment = registry.assign_visibility(
        "__openclank_app_visibility__", "alice", projection_root["id"], ["read"]
    )
    unrelated_assignment = registry.assign_visibility("admin", "bob", unrelated_root["id"], ["read"])
    before = registry.generation()

    result = registry.disable_projection(str(shared), "directory")

    assert result == {"roots_disabled": 2, "assignments_disabled": 1}
    assert registry.generation() == before + 1
    assert next(root for root in registry.list("admin") if root["id"] == admin_root["id"])["enabled"] is False
    assert registry.list("__openclank_app_visibility__")[0]["enabled"] is False
    assignments = {item["id"]: item for item in registry.list_visibility()}
    assert assignments[shared_assignment["id"]]["enabled"] is False
    assert assignments[unrelated_assignment["id"]]["enabled"] is True
    assert next(root for root in registry.list("admin") if root["id"] == unrelated_root["id"])["enabled"] is True

    generation = registry.generation()
    assert registry.disable_projection(str(shared), "whole_root") == {
        "roots_disabled": 0,
        "assignments_disabled": 0,
    }
    assert registry.generation() == generation


def test_visibility_assignments_project_only_assigned_roots_and_bump_generation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    workspace_root = registry.add("admin", str(workspace), "recursive_directory", ["read", "write"])
    secret_root = registry.add("admin", str(secret), "recursive_directory", ["read"])
    before = registry.app_scope("user", is_admin=False)
    assignment = registry.assign_visibility("admin", "user", workspace_root["id"], ["read"])

    visible = registry.visibility_for_subject("user")
    assert [item["root_id"] for item in visible] == [workspace_root["id"]]
    assert visible[0]["root"]["canonical_path"] == str(workspace.resolve())
    scope = registry.app_scope("user", is_admin=False)
    assert scope["host"] is False
    assert scope["visible_root_ids"] == [workspace_root["id"]]
    assert scope["capabilities"] == ["read"]
    assert scope["root_capabilities"] == {workspace_root["id"]: ["read"]}
    assert scope["generation"] > before["generation"]
    assert registry.app_scope("admin", is_admin=True)["host"] is True
    assert registry.list_visibility()[0]["id"] == assignment["id"]
    assert secret_root["id"] not in scope["visible_root_ids"]
    registry.update("admin", workspace_root["id"], enabled=False)
    unavailable = registry.visibility_for_subject("user")
    assert unavailable[0]["id"] == assignment["id"]
    assert unavailable[0]["root"]["enabled"] is False
    assert registry.app_scope("user", is_admin=False)["visible_root_ids"] == []


def test_app_scope_keeps_capabilities_attached_to_each_visible_root(tmp_path):
    read_only = tmp_path / "read-only"
    write_only = tmp_path / "write-only"
    read_only.mkdir()
    write_only.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    read_root = registry.add("admin", str(read_only), "recursive_directory", ["read", "write"])
    write_root = registry.add("admin", str(write_only), "recursive_directory", ["read", "write"])
    registry.assign_visibility("admin", "user", read_root["id"], ["read"])
    registry.assign_visibility("admin", "user", write_root["id"], ["write"])

    scope = registry.app_scope("user", is_admin=False)

    assert scope["capabilities"] == ["read", "write"]
    assert scope["root_capabilities"] == {
        read_root["id"]: ["read"],
        write_root["id"]: ["write"],
    }


def test_visibility_cannot_exceed_root_or_be_mutated_by_non_owner(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("admin", str(workspace), "recursive_directory", ["read"])
    with pytest.raises(FilesystemRegistryError) as error:
        registry.assign_visibility("admin", "user", root["id"], ["write"])
    assert error.value.code == "invalid_capabilities"
    with pytest.raises(FilesystemRegistryError) as error:
        registry.assign_visibility("other-admin", "user", root["id"], ["read"])
    assert error.value.code == "root_not_found"


def test_group_visibility_never_matches_a_same_named_user_without_membership(tmp_path):
    folder = tmp_path / "workspace"
    folder.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("admin", str(folder), "recursive_directory", ["read"])
    registry.assign_visibility("admin", "e", root["id"], ["read"], subject_kind="group")

    assert registry.visibility_for_subject("e") == []
    assert registry.visibility_for_subject("nobody", groups=["e"])[0]["subject_kind"] == "group"


def test_agent_scope_is_intersected_with_non_admin_visibility_ceiling(tmp_path):
    visible = tmp_path / "visible"
    nested = visible / "nested"
    visible.mkdir()
    nested.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    app_root = registry.add("admin", str(visible), "recursive_directory", ["read"])
    registry.assign_visibility("admin", "user", app_root["id"], ["read"])
    agent_root = registry.add("user", str(nested), "recursive_directory", ["read", "write"])
    scope = registry.agent_scope("user", app_visibility=registry.visibility_for_subject("user"))
    assert scope["approved_root_ids"] == [agent_root["id"]]
    assert scope["root_capabilities"] == {agent_root["id"]: ["read"]}

    registry.update("user", agent_root["id"], capabilities=["read"])
    narrowed = registry.agent_scope("user", app_visibility=registry.visibility_for_subject("user"))
    assert narrowed["approved_root_ids"] == [agent_root["id"]]
    assert narrowed["root_capabilities"] == {agent_root["id"]: ["read"]}


def test_agent_scope_never_reuses_physical_root_write_after_assignment_downgrade(tmp_path):
    visible = tmp_path / "visible"
    nested = visible / "nested"
    visible.mkdir()
    nested.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    app_root = registry.add("admin", str(visible), "recursive_directory", ["read", "write"])
    assignment = registry.assign_visibility("admin", "user", app_root["id"], ["read", "write"])
    agent_root = registry.add("user", str(nested), "recursive_directory", ["read", "write"])

    registry.update_visibility("admin", assignment["id"], capabilities=["read"])
    scope = registry.agent_scope("user", app_visibility=registry.visibility_for_subject("user"))

    assert scope["approved_root_ids"] == [agent_root["id"]]
    assert scope["root_capabilities"] == {agent_root["id"]: ["read"]}


def test_owner_lifecycle_renames_and_purges_access_without_username_reuse(tmp_path):
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    folder = tmp_path / "folder"
    folder.mkdir()
    root = registry.add("alice", str(folder), "recursive_directory", ["read"])
    registry.assign_visibility("alice", "bob", root["id"], ["read"])

    registry.rename_owner("alice", "alice-renamed")
    assert registry.list("alice") == []
    assert registry.list("alice-renamed")[0]["owner_id"] == "alice-renamed"
    assert registry.visibility_for_subject("bob")[0]["root"]["owner_id"] == "alice-renamed"

    registry.delete_owner("alice-renamed")
    assert registry.list("alice-renamed") == []
    assert registry.visibility_for_subject("bob") == []
