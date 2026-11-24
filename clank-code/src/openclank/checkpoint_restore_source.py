"""Read one retained selected checkpoint under fresh account/Files authority."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import host_origin_for_path
from src.openclank.history_client import HistoryClient, registered_resource_pages
from src.openclank.history_io import bounded_history_io
from src.openclank.resource_refs import stable_resource_id


async def retained_checkpoint_source(facade, context, *, request_id, resource_id, auth, barrier,
                                     history_supervisor, current):
    """Return private exact source evidence; the saved receipt is never a grant."""
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id)
            or not isinstance(resource_id, str) or not re.fullmatch(r"resource-[0-9a-f]{64}", resource_id)
            or auth is None or barrier is None or history_supervisor is None or not callable(current)):
        raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
    store = facade._operation_store
    provider = facade._providers.get("host")
    if provider is None or getattr(provider, "operation_store", None) is not store:
        raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
    owner, account = context.owner_username, context.owner_subject_id
    process = history_supervisor.process

    async def live():
        try:
            return bool(await current() is True and auth.account_id(owner) == account
                and auth.username_for_account_id(account) == owner and facade._generation_current(context)
                and process is not None and history_supervisor.process is process and process.poll() is None)
        except Exception:
            return False

    async with barrier.admitted(owner):
        if not await live():
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        saved = store.get_operation(owner_subject_id=account, operation_id=request_id)
        receipt = saved.get("receipt") if isinstance(saved, Mapping) else None
        binding = receipt.get("_binding") if isinstance(receipt, Mapping) and receipt.get("kind") == "selected_checkpoint_v1" else None
        native = receipt.get("_native") if isinstance(receipt, Mapping) else None
        if (not isinstance(saved, Mapping) or not re.fullmatch(r"[0-9a-f]{64}", str(saved.get("digest") or ""))
                or not isinstance(binding, Mapping) or set(binding) != {"root_id", "history_actor", "history_workspace", "items"}
                or not isinstance(native, Mapping) or native.get("state") != "complete"
                or not isinstance(binding.get("items"), list) or not 1 <= len(binding["items"]) <= 16
                or not isinstance(native.get("items"), list) or len(native["items"]) != len(binding["items"])):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        for original, version in zip(binding["items"], native["items"]):
            if (not isinstance(original, Mapping) or set(original) != {"resource_id", "fingerprint"}
                    or not isinstance(version, Mapping) or set(version) != {"resource_id", "version_ref", "fingerprint", "size"}
                    or version["resource_id"] != original["resource_id"] or version["fingerprint"] != original["fingerprint"]
                    or not isinstance(version["version_ref"], str) or not version["version_ref"]
                    or type(version["size"]) is not int or not 0 <= version["size"] <= 16 * 1024 * 1024):
                raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        ids = [row["resource_id"] for row in binding["items"]]
        if any(not isinstance(value, str) or not value.startswith("file:") for value in ids) or len(set(ids)) != len(ids):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        location = store.get_location(binding["root_id"])
        if location.kind != "directory" or not location.enabled or location.availability != "available":
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        root_path = str(location.canonical_path)
        if not os.path.isabs(root_path) or os.path.realpath(root_path) != root_path:
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        workspace = context.workspace_id
        if workspace is not None:
            selected_workspace = store.get_workspace(workspace)
            if selected_workspace.archived or selected_workspace.owner_subject_id != account or selected_workspace.location_id != location.id:
                raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")

        def readable(path):
            relative = "" if path == root_path else os.path.relpath(path, root_path)
            scope = store.resolve(subject_id=account, is_admin=context.is_admin, origin="app", location_id=location.id,
                workspace_id=workspace, relative_resource=relative, capability="read", observation_only=True)
            return scope.allowed and scope.policy_generation == context.policy_generation

        if not readable(root_path):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        root_origin = host_origin_for_path(root_path)
        root = await provider.stat(context, origin_id=root_origin)
        if root.origin_id != root_origin or not root.revision:
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        authorized = await provider.authorize_checkpoint_root(context, origin_id=root_origin, expected_revision=root.revision)
        if any(binding.get(key) != authorized.get(key) for key in ("root_id", "history_actor", "history_workspace")):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        history = HistoryClient(authorized["history_socket"], actor_id=authorized["history_actor"], account_id=account,
            token=history_supervisor.credential_token(authorized["history_actor"], account), timeout=1.5)
        observed = await bounded_history_io(lambda: history.selected_checkpoint_status(request_id))
        if observed != native:
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        matches = {value: [] for value in ids}
        async def page(cursor):
            return await bounded_history_io(lambda: history.list_registered_resources(cursor=cursor, limit=100), timeout=1.5)
        async for result in registered_resource_pages(page):
            if not await live():
                raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
            for row in result["items"]:
                if (row.get("resource_id") not in matches or row.get("account_id") != account
                        or row.get("workspace_id") != binding["history_workspace"] or row.get("root_id") != binding["root_id"]
                        or row.get("root_path") != root_path or row.get("active") is not True
                        or not isinstance(row.get("actor_ids"), list) or binding["history_actor"] not in row["actor_ids"]):
                    continue
                relative = str(row.get("relative_path") or "")
                if not relative or os.path.isabs(relative) or ".." in Path(relative).parts:
                    raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
                path = os.path.normpath(os.path.join(root_path, relative))
                if os.path.commonpath((root_path, path)) != root_path or path == root_path or os.path.realpath(path) != path:
                    raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
                matches[row["resource_id"]].append(path)
        if any(len(matches[value]) != 1 for value in ids) or len({matches[value][0] for value in ids}) != len(ids):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        selected = []
        for index, native_id in enumerate(ids):
            path = matches[native_id][0]
            origin = host_origin_for_path(path)
            public = stable_resource_id(owner_subject_id=account, provider="host", origin_id=origin)
            if public == resource_id:
                selected.append((index, path, origin))
        if len(selected) != 1:
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        index, path, origin = selected[0]
        current_file = await provider.resource_for_path(context, path=path)
        if current_file.origin_id != origin or current_file.kind != "file" or not readable(path):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        version = native["items"][index]
        selection = await bounded_history_io(lambda: history.resolve_resource_version(version["resource_id"], version["version_ref"]))
        descriptor = selection.get("version") if isinstance(selection, Mapping) else None
        selected_resource = selection.get("resource") if isinstance(selection, Mapping) else None
        if (not isinstance(selected_resource, Mapping) or selected_resource.get("resource_id") != version["resource_id"]
                or selected_resource.get("account_id") != account or selected_resource.get("workspace_id") != binding["history_workspace"]
                or not isinstance(descriptor, Mapping) or descriptor.get("id") != version["version_ref"]
                or descriptor.get("source_provider") != "odysseus-files" or descriptor.get("operation") != "selected_checkpoint_v1"
                or descriptor.get("phase") != "checkpoint" or descriptor.get("fingerprint") != version["fingerprint"]
                or descriptor.get("size_bytes") != version["size"] or descriptor.get("availability") != "available"
                or descriptor.get("can_read") is not True or descriptor.get("can_restore") is not True
                or not isinstance(descriptor.get("action_id"), str) or not descriptor["action_id"]
                or not isinstance(descriptor.get("version_id"), str) or not descriptor["version_id"]):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        if (not await live() or store.get_operation(owner_subject_id=account, operation_id=request_id) != saved
                or dict(await provider.authorize_checkpoint_root(context, origin_id=root_origin, expected_revision=root.revision)) != dict(authorized)):
            raise FilesFacadeError("Checkpoint source unavailable", code="resource_unavailable")
        return {"checkpoint_id": request_id, "checkpoint_digest": saved["digest"], "binding": dict(binding),
            "native": dict(native), "resource_id": resource_id, "native_resource_id": version["resource_id"],
            "version_ref": version["version_ref"], "fingerprint": version["fingerprint"], "size": version["size"],
            "path": path, "file": current_file, "history": history, "history_process": process,
            "source_action_id": descriptor["action_id"], "source_version_id": descriptor["version_id"]}
