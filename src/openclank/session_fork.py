"""Reserved native forks are prebound before any managed clone callbacks."""
from __future__ import annotations
from copy import deepcopy
import hashlib, json, uuid
from pathlib import Path
from core.database import Session, ChatMessage, SessionLocal
from core.models import ChatMessage as DisplayMessage
from src.openclank.session_operations import HistoryOperationError, _read, _binding, visibility
from src.upload_handler import reserve_message_upload_references

class ForkOperationError(HistoryOperationError):
    def __init__(self, code, message, *, candidate_id=None):
        super().__init__(code, message); self.candidate_id = candidate_id


def fingerprint(request):
    material = [request["sourceSessionID"], request["targetSessionID"], str(Path(request["directory"]).resolve(strict=False)), sorted(request["hostSourceMap"].items()), request.get("messageID"), request.get("title")]
    return hashlib.sha256(json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _finish(child_id, owner, *, pending, receipt):
    with SessionLocal() as db:
        row = db.query(Session).filter(Session.id == child_id, Session.owner == owner).with_for_update().first()
        if row is None: raise ForkOperationError("FORK_TARGET_MISSING", "Fork candidate is unavailable", candidate_id=child_id)
        state = deepcopy(row.mimo_state or {})
        if state.get("history_operation") != pending or state.get("managed_binding", {}).get("engineSessionID") != pending["target_native_id"]:
            raise ForkOperationError("FORK_REVISION_CONFLICT", "Fork candidate authority changed", candidate_id=child_id)
        expected = pending["fingerprint"]
        if not isinstance(receipt, dict) or receipt.get("fingerprint") != expected or receipt.get("sourceSessionID") != pending["source_native_id"] or receipt.get("targetSessionID") != pending["target_native_id"] or type(receipt.get("sourceMessageCount")) is not int or receipt["sourceMessageCount"] != pending.get("expected_source_message_count"):
            raise ForkOperationError("FORK_RECEIPT_INCOMPLETE", "Native fork lacks its final complete-clone receipt", candidate_id=child_id)
        state["history_operation"] = {**pending, "status": "settled", "hidden_message_ids": [], "outcome": "confirmed", "native_receipt": receipt}
        row.mimo_state = state; row.archived = False; row.transcript_revision = int(row.transcript_revision or 0) + 1
        db.commit()
        return {"status": "settled", "id": child_id, "name": row.name, "transcript_revision": int(row.transcript_revision or 0)}


async def execute(worker, manager, *, sid, owner, operation, payload, check_current):
    bridge = worker._bridge
    source_state, revision, order = _read(sid, owner)
    if type(payload.get("expected_transcript_revision")) is not int or payload["expected_transcript_revision"] != revision:
        raise ForkOperationError("HISTORY_REVISION_CONFLICT", "expected_transcript_revision must match the source transcript")
    if operation == "fork-reconcile":
        child_id = payload.get("child_session_id")
        child_state, _, _ = _read(child_id, owner)
        pending = child_state.get("history_operation") or {}
        if pending.get("operation") != "fork" or pending.get("source_chat_id") != sid:
            raise ForkOperationError("FORK_AUTHORITY_MISMATCH", "Candidate does not belong to this source chat")
        if pending.get("status") == "settled": return {"status": "settled", "id": child_id}
        if bridge.mapped_session_id(child_id) != pending["target_native_id"]:
            raise ForkOperationError("FORK_AUTHORITY_MISMATCH", "Candidate mapping changed", candidate_id=child_id)
        check_current()
        result = await worker.managed_engine_call("_openclank/session/v1/messages/source-map", {"sessionID": pending["target_native_id"], "directory": bridge.mapped_session_workspace(child_id)})
        check_current()
        receipt = result.get("forkReceipt")
        if receipt is None and pending.get("expected_source_message_count") == 0 and result.get("sourceMessageCount") == 0:
            receipt = {"sourceSessionID": pending["source_native_id"], "targetSessionID": pending["target_native_id"], "fingerprint": pending["fingerprint"], "sourceMessageCount": 0}
        binding = child_state.get("managed_binding") or {}
        await bridge.resume_session(child_id, pending["target_native_id"], with_memory=bool(binding.get("memoryEnabled", True)), copal_workspace=str(binding.get("copalWorkspace") or "default"), authority_workspace_id=str(binding.get("authorityWorkspaceID") or ""))
        check_current()
        return _finish(child_id, owner, pending=pending, receipt=receipt)
    visibility(source_state, require_settled=True)
    if type(payload.get("expected_transcript_revision")) is not int or payload["expected_transcript_revision"] != revision:
        raise ForkOperationError("HISTORY_REVISION_CONFLICT", "expected_transcript_revision must match the source transcript")
    source_native_id = bridge.mapped_session_id(sid)
    directory = bridge.mapped_session_workspace(sid)
    check_current()
    source = await worker.managed_engine_call("_openclank/session/v1/messages/source-map", {"sessionID": source_native_id, "directory": directory})
    source_map = {item["hostSourceMessageID"]: item["nativeMessageID"] for item in source.get("anchors", [])}
    if len(source_map) != len(source.get("anchors", [])): raise ForkOperationError("SOURCE_MAPPING_AMBIGUOUS", "Source map is ambiguous")
    hidden = visibility(source_state)
    visible = [item for item in order if item[0] not in hidden]
    cutoff = payload.get("host_message_id")
    native_cutoff = None
    if cutoff is not None:
        if cutoff not in source_map or not any(item == (cutoff, "user") for item in visible):
            raise ForkOperationError("SOURCE_BOUNDARY_UNMAPPED", "Fork boundary must name a mapped visible user turn")
        native_cutoff = source_map[cutoff]
        visible = visible[:next(index for index, item in enumerate(visible) if item[0] == cutoff)]
    elif source.get("revert"):
        native_cutoff = source["revert"]["messageID"]
    if native_cutoff:
        source = await worker.managed_engine_call("_openclank/session/v1/messages/source-map", {"sessionID": source_native_id, "directory": directory, "messageID": native_cutoff})
    user_ids = [item[0] for item in visible if item[1] == "user"]
    if any(item not in source_map for item in user_ids):
        raise ForkOperationError("SOURCE_BOUNDARY_UNMAPPED", "Legacy source turns cannot be safely native-forked")
    child_id = str(uuid.uuid4()); source_model = manager.get_session(sid)
    title = payload.get("title") or f"⫝ {source_model.name}"
    if not isinstance(title, str) or not title.strip(): raise ValueError("Fork title must be a non-empty string")
    reserved = await bridge._client.reserve_session(); target_native_id = reserved["sessionID"]
    source_binding = source_state.get("managed_binding") or {}
    pending = {"id": uuid.uuid4().hex, "operation": "fork", "status": "pending", "before_hidden": [], "after_hidden": [], "source_chat_id": sid, "source_revision": revision, "source_native_id": source_native_id, "target_native_id": target_native_id, "generation": getattr(worker, "installed_generation", 0), "expected_source_message_count": source.get("sourceMessageCount")}
    selected_ids = [item[0] for item in visible]; cloned = []; host_map = {}
    with SessionLocal() as db:
        source_row = db.query(Session).filter(Session.id == sid, Session.owner == owner).one()
        if int(source_row.transcript_revision or 0) != revision or _binding(source_row.mimo_state or {}) != _binding(source_state):
            raise ForkOperationError("HISTORY_REVISION_CONFLICT", "Source changed before fork reservation")
        rows = {item.id: item for item in db.query(ChatMessage).filter(ChatMessage.session_id == sid, ChatMessage.id.in_(selected_ids)).all()}
        child = Session(id=child_id, owner=owner, name=title, endpoint_url=source_row.endpoint_url, endpoint_id=source_row.endpoint_id, model=source_row.model, provider_model_route_id=source_row.provider_model_route_id, workspace_id=source_row.workspace_id, archived=True, mimo_state={"history_operation": deepcopy(pending)})
        db.add(child); db.flush()
        from datetime import timedelta
        last_timestamp = None
        for old_id in selected_ids:
            item = rows[old_id]; new_id = f"root_{uuid.uuid4().hex}" if item.role == "user" else str(uuid.uuid4())
            metadata = json.loads(item.meta_data) if item.meta_data else {}
            metadata = deepcopy(metadata or {}); metadata.pop("_db_id", None); metadata.pop("root_operation_id", None)
            metadata["forked_from_message_id"] = old_id
            if item.role == "user": metadata["root_operation_id"] = new_id; host_map[old_id] = new_id
            content = item.content
            try: parsed = json.loads(content) if isinstance(content, str) else content
            except (ValueError, TypeError): parsed = content
            missing = reserve_message_upload_references(getattr(manager, "upload_handler", None), owner, parsed, metadata, session_id=child_id)
            if missing: raise ValueError("A source attachment is unavailable")
            timestamp = item.timestamp
            if last_timestamp is not None and timestamp <= last_timestamp: timestamp = last_timestamp + timedelta(microseconds=1)
            last_timestamp = timestamp
            db.add(ChatMessage(id=new_id, session_id=child_id, role=item.role, content=content, meta_data=json.dumps(metadata), timestamp=timestamp))
            cloned.append((new_id, DisplayMessage(item.role, parsed, {**metadata, "_db_id": new_id})))
        request = {"sourceSessionID": source_native_id, "targetSessionID": target_native_id, "directory": directory, "hostSourceMap": host_map, "title": title}
        if native_cutoff: request["messageID"] = native_cutoff
        pending["fingerprint"] = fingerprint(request)
        child.mimo_state = {"history_operation": deepcopy(pending)}
        db.commit()
    # Full host rows and attachments are captured before native callback writes.
    for index, (new_id, message) in enumerate(cloned):
        manager._archive_message_parts(child_id, message, msg_id=new_id, revision=1, event_sequence=index)
    try:
        async with bridge._durable_session_map.async_chat_admission_lock(child_id):
            bridge._stage_candidate_binding(child_id, target_native_id, owner, previous_binding=None, cwd=directory, authority_workspace_id=str(source_binding.get("authorityWorkspaceID") or ""), memory_enabled=bool(source_binding.get("memoryEnabled", True)), copal_workspace=str(source_binding.get("copalWorkspace") or "default"), memory_workspace_id=str(source_binding.get("memoryWorkspaceID") or ""), expected_current=None, expected_mapping_revision=0)
            await bridge._bind_engine_session(child_id, target_native_id, expected_current=None, expected_mapping_revision=0)
            map_revision, mapping_revision = bridge._map_revisions_for_chat(child_id)
            bridge._bind_canonical_session(target_native_id, child_id, owner, physical_cwd=directory, authority_workspace_id=str(source_binding.get("authorityWorkspaceID") or ""), memory_enabled=bool(source_binding.get("memoryEnabled", True)), map_revision=map_revision, mapping_revision=mapping_revision, copal_workspace=str(source_binding.get("copalWorkspace") or "default"), memory_workspace_id=str(source_binding.get("memoryWorkspaceID") or ""))
            check_current()
            result = await worker.managed_engine_call("_openclank/session/v1/fork/create", request)
            check_current()
            current_source, current_revision, _ = _read(sid, owner)
            if current_revision != revision or _binding(current_source) != _binding(source_state): raise ForkOperationError("HISTORY_REVISION_CONFLICT", "Source changed while forking", candidate_id=child_id)
            current_child, _, _ = _read(child_id, owner)
            native_receipt = result.get("forkReceipt")
            if native_receipt is None and pending.get("expected_source_message_count") == 0 and result.get("created") is True and result.get("fingerprint") == pending["fingerprint"]:
                native_receipt = {"sourceSessionID": source_native_id, "targetSessionID": target_native_id, "fingerprint": pending["fingerprint"], "sourceMessageCount": 0}
            await bridge.resume_session(child_id, target_native_id, with_memory=bool(source_binding.get("memoryEnabled", True)), copal_workspace=str(source_binding.get("copalWorkspace") or "default"), authority_workspace_id=str(source_binding.get("authorityWorkspaceID") or ""))
            receipt = _finish(child_id, owner, pending=current_child["history_operation"], receipt=native_receipt)
            return receipt
    except BaseException as exc:
        if isinstance(exc, ForkOperationError): raise
        raise ForkOperationError("FORK_RECONCILIATION_REQUIRED", "Reserved fork is pending; reconcile this exact candidate before opening it", candidate_id=child_id) from exc
