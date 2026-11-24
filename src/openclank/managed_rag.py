"""Host-side administrative document index operations for a managed Agent turn."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path

from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result

METHOD = "_openclank/session/v1/memory/manage-rag"
REFUSED = {"state": "refused", "output": "Document administration unavailable."}
UNKNOWN = {"state": "unknown", "output": "The original document operation has no confirmed result."}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _drain(awaitable):
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _index_directory(files, manager, path, owner, workspace, current):
    """List and read every source through Agent Files, including every page."""
    from src.memery_rag import DEFAULT_FILE_EXTENSIONS, MAX_FILE_BYTES

    pending = [path]
    indexed = skipped = 0
    while pending:
        folder = pending.pop()
        cursor = None
        while True:
            await current()
            response = await files.request("list_directory", folder, {"cursor": cursor} if cursor else {})
            data = response["data"]
            for item in data["entries"]:
                name = item["name"]
                if not name or name in (".", "..") or os.path.basename(name) != name:
                    raise ValueError("Invalid directory entry")
                source = os.path.join(folder, name)
                kind = str(item["kind"]).lower()
                if kind == "directory":
                    pending.append(source)
                elif (kind == "file" and Path(name).suffix.lower() in DEFAULT_FILE_EXTENSIONS
                      and type(item.get("size")) is int and item["size"] <= MAX_FILE_BYTES):
                    await current()
                    contents = (await files.request("read_lines", source, {}))["data"]["text"]
                    if type(contents) is not str or len(contents.encode("utf-8")) > MAX_FILE_BYTES:
                        skipped += 1
                        continue
                    await current()
                    retained = await _drain(asyncio.to_thread(manager.add_document, contents, {
                        "owner": owner, "workspace_id": workspace, "project_id": None,
                        "source": source, "title": name, "source_type": "owner_imported_file",
                    }))
                    indexed += int(bool(retained))
                    skipped += int(not retained)
            cursor = data.get("next_cursor")
            if not cursor:
                break
    return {"stdout": json.dumps({"indexed_count": indexed, "skipped": skipped}), "exit_code": 0}


async def _approve(bridge, worker, identity, chat, owner, workspace, workspace_id, args):
    from src.openclank.acp_bridge import PermissionRequest
    from src.openclank.transcript_projection import get_projection

    projection = await asyncio.to_thread(get_projection, chat, owner=owner)
    if (not projection or projection.get("mimo_session_id") != identity["sessionID"]
            or projection.get("active_turn_id") != identity["rootOperationID"]):
        return False
    handler = worker.permission_handler
    request_id = "perm_" + secrets.token_hex(16)
    target = args.get("directory") or args.get("generation_id") or workspace
    summary = f"Memery document index · {args['action']} · {target}"
    req = PermissionRequest(request_id=request_id, tool_call={}, raw_input={"summary": summary, **args},
        options=["once", "reject"], title="manage_rag", session_id=identity["sessionID"], odysseus_session_id=chat,
        owner=owner, workspace=workspace, authority_workspace_id=workspace_id, turn_id=identity["rootOperationID"],
        revision=int(projection.get("transcript_revision") or 0))
    handler.pending_requests[request_id] = req
    try:
        await bridge._surface_permission(req)
        return await req.wait() == "once"
    finally:
        handler.pending_requests.pop(request_id, None)


async def operate(bridge, params):
    from core.models import get_session_manager_instance
    from src.ai_interaction import do_manage_rag
    from src.openclank.files_service_client import agent_client_for
    from src.openclank.workspace_policy_service import resolve_owned_workspace
    from src.rag_singleton import get_rag_manager

    issued = False
    try:
        request = validate_managed_method_request(METHOD, params)
        identity, args = request["identity"], dict(request["args"])
        if identity["actorID"] != "main":
            return REFUSED
        manager = get_session_manager_instance()
        state = manager.lifecycle_context
        auth, barrier, supervisor = state.auth_manager, state.account_request_barrier, state.mimo_supervisor
        repository = state.files_policy_repository
        original = await bridge._resolve_standing_guidance_turn(identity)
        owner, account = original["owner"], original["ownerAccountID"]
        chat, workspace_id = original["stableChatID"], original["authorityWorkspaceID"]
        session = manager.get_session(chat)
        worker = supervisor.worker_for_owner(owner)
        binding = await bridge._handle_session_binding_read({"sessionID": identity["sessionID"]})
        workspace = binding["canonicalCwd"]
        generation = repository.generation()
        worker_generation = worker.installed_generation

        async def current():
            context = bridge._session_context.get(identity["sessionID"], {})
            decision = await bridge._handle_managed_tool_admit({**identity, "toolName":"manage_rag", "kind":"tool", "toolCallID":request["toolCallID"]})
            if (not account or not owner or not workspace_id or not workspace or not os.path.isabs(workspace)
                    or original != await bridge._resolve_standing_guidance_turn(identity)
                    or decision.get("allowed") is not True or auth.account_id(owner) != account
                    or auth.username_for_account_id(account) != owner or not auth.is_admin(owner) or barrier._fenced(owner)
                    or supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or not worker.is_alive()
                    or worker.installed_generation != worker_generation or session is None or manager.get_session(chat) is not session
                    or session.owner != owner or session.incognito or session.archived or repository.generation() != generation
                    or binding != await bridge._handle_session_binding_read({"sessionID":identity["sessionID"]})
                    or binding.get("owner") != owner or binding.get("stableChatID") != chat or binding.get("authorityWorkspaceID") != workspace_id
                    or binding.get("memoryEnabled") is not True or binding.get("transition") or binding.get("authority")
                    or context.get("root_operation_id") != identity["rootOperationID"] or context.get("incognito") or context.get("auxiliary")
                    or context.get("personal_context_allowed") is not True
                    or resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner, auth_manager=auth,
                        purpose="agent_workspace", observation_only=True).path != workspace):
                raise PermissionError("Document administration authority changed")

        await current()
        action = args["action"]
        files = None
        if "directory" in args:
            path = os.path.normpath(os.path.join(workspace, args["directory"]))
            if os.path.commonpath((workspace, path)) != workspace or os.path.realpath(path) != path:
                return REFUSED
            args["directory"] = path
            files = agent_client_for(owner, workspace, workspace_id=workspace_id, chat_id=chat)
            await files.request("list_directory", path, {})
        mutation = action not in {"list", "status"}
        token = "managed_rag_" + _digest([chat, identity["sessionID"], identity["rootOperationID"], request["messageID"], request["toolCallID"]])
        custody = dict(owner_subject_id=account, operation_id=token, request_digest=_digest([workspace_id, args]), generation=generation)

        if mutation:
            existing = repository.get_operation(owner_subject_id=account, operation_id=token)
            if existing:
                if existing["digest"] != custody["request_digest"]:
                    return REFUSED
                return existing["receipt"].get("result", UNKNOWN)
            if (bridge._session_context[identity["sessionID"]].get("interaction_policy") == "fail_on_interaction"
                    or not await _approve(bridge, worker, identity, chat, owner, workspace, workspace_id, args)):
                return REFUSED

        async with barrier.admitted(owner):
            await current()
            if mutation:
                existing = repository.reserve_operation(**custody, receipt={"kind": "managed_rag", "state": "pending"})
                if existing:
                    return existing["receipt"].get("result", UNKNOWN)
                issued = True
            if files is not None and action == "add_directory":
                backend = get_rag_manager()
                if backend is None:
                    raise ValueError("Document index is unavailable")
                result = await _index_directory(files, backend, args["directory"], owner, workspace, current)
            else:
                result = await _drain(do_manage_rag(args, owner=owner, workspace_id=workspace))
            output = str(result.get("stdout") or result.get("error") or "")[:65536]
            answer = validate_managed_method_result(METHOD, {"state":"complete" if result.get("exit_code")==0 else "refused", "output":output})
            if mutation:
                repository.record_operation(**custody, receipt={"kind":"managed_rag", "state":"finished", "result":answer})
            await current()
            return answer
    except Exception:
        return UNKNOWN if issued else REFUSED
