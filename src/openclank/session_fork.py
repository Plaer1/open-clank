"""Reserved native forks are prebound before any managed clone callbacks."""
from __future__ import annotations
from copy import deepcopy
import hashlib, json, uuid
from pathlib import Path
from core.database import Session, ChatMessage, SessionLocal
from core.models import ChatMessage as DisplayMessage
from src.openclank.session_operations import HistoryOperationError, _read, _binding, visibility
from src.upload_handler import reserve_message_upload_references
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL

class ForkOperationError(HistoryOperationError):
    def __init__(self, code, message, *, candidate_id=None, retain_lease=False):
        super().__init__(code, message)
        self.candidate_id = candidate_id
        self.retain_lease = retain_lease


def fork_source_kind(row):
    """Classify the original Host row, including cold and retired Native sources."""
    state = row.mimo_state or {}
    if (row.provider_model_route_id or row.endpoint_url == MANAGED_ENGINE_PUBLIC_URL
            or row.mimo_session_id or "managed_binding" in state or "directory_retired_by" in state):
        return "native"
    return "transcript-copy"


def fork_source_key(row):
    """Pin the original mapping and workspace without exposing Native handles."""
    state = row.mimo_state or {}
    material = [row.id, row.workspace_id, row.provider_model_route_id, row.mimo_session_id,
                state.get("managed_binding"), state.get("directory_retired_by")]
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _pending_fork(db, sid, owner):
    receipt = Session.mimo_state["history_operation"]
    return db.query(Session).filter(
        Session.owner == owner,
        Session.archived.is_(True),
        receipt["operation"].as_string() == "fork",
        receipt["source_chat_id"].as_string() == sid,
        receipt["status"].as_string() == "pending",
    ).order_by(Session.created_at, Session.id).first()


def _source_matches(row, pending):
    if row is None or row.archived or int(row.transcript_revision or 0) != pending["source_revision"]:
        return False
    state = row.mimo_state or {}
    binding = _binding(state)
    return ("directory_retired_by" not in state
            and (state.get("history_operation") or {}).get("status") != "pending"
            and binding["engineSessionID"] == pending["source_native_id"]
            and ("source_binding" not in pending or binding == pending["source_binding"])
            and ("source_workspace_id" not in pending or row.workspace_id == pending["source_workspace_id"])
            and ("source_model_route_id" not in pending
                 or row.provider_model_route_id == pending["source_model_route_id"])
            and ("source_key" not in pending or fork_source_key(row) == pending["source_key"]))


def fork_receipt_status(*, sid, owner, account_id, candidate_id=None):
    """Inspect a Host candidate under its original scope without invoking Native."""
    from src.openclank.conversation_archive import get_conversation_archive

    archive = get_conversation_archive()
    if archive.chat_erasure_started(owner=owner, chat_id=sid):
        raise ForkOperationError("FORK_SOURCE_UNAVAILABLE", "Original fork source is unavailable")
    with SessionLocal() as db:
        source = db.query(Session).filter(Session.id == sid, Session.owner == owner).first()
        if source is None or source.archived:
            raise ForkOperationError("FORK_SOURCE_UNAVAILABLE", "Original fork source is unavailable")
        if candidate_id:
            child = db.query(Session).filter(Session.id == candidate_id, Session.owner == owner).first()
        else:
            child = _pending_fork(db, sid, owner)
        child_state = (child.mimo_state or {}) if child is not None else {}
        pending = child_state.get("history_operation") or {}
        if (pending.get("operation") != "fork" or pending.get("source_chat_id") != sid
                or pending.get("source_account_id", account_id) != account_id
                or archive.chat_erasure_started(owner=owner, chat_id=child.id)):
            return {"status": "unknown" if candidate_id else "no-pending-candidate", "candidate_id": candidate_id}
        binding = _binding(source.mimo_state or {})
        if (child.workspace_id != source.workspace_id or binding["engineSessionID"] != pending["source_native_id"]
                or ("source_key" in pending and fork_source_key(source) != pending["source_key"])
                or "directory_retired_by" in (source.mimo_state or {}) or "directory_retired_by" in child_state):
            return {"status": "stale", "candidate_id": child.id}
        if pending.get("status") == "pending":
            return {"status": "pending" if _source_matches(source, pending) else "stale", "candidate_id": child.id}
        if (pending.get("status") == "settled" and not child.archived
                and _binding(child_state)["engineSessionID"] == pending["target_native_id"]):
            _require_receipt(pending, pending.get("native_receipt"), child.id)
            return {"status": "settled", "candidate_id": child.id, "id": child.id, "name": child.name}
        return {"status": "unknown", "candidate_id": child.id}


def fingerprint(request):
    material = [request["sourceSessionID"], request["targetSessionID"], str(Path(request["directory"]).resolve(strict=False)), sorted(request["hostSourceMap"].items()), request.get("messageID"), request.get("title")]
    return hashlib.sha256(json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _source_metadata(message):
    value = json.loads(message.meta_data) if message.meta_data else {}
    return value if isinstance(value, dict) else {}


def _require_receipt(pending, receipt, child_id):
    if (not isinstance(receipt, dict) or receipt.get("fingerprint") != pending["fingerprint"]
            or receipt.get("sourceSessionID") != pending["source_native_id"]
            or receipt.get("targetSessionID") != pending["target_native_id"]
            or type(receipt.get("sourceMessageCount")) is not int
            or receipt["sourceMessageCount"] != pending.get("expected_source_message_count")):
        raise ForkOperationError("FORK_RECEIPT_INCOMPLETE", "Native fork lacks its final complete-clone receipt", candidate_id=child_id)


def _finish(child_id, owner, *, pending, receipt, source_state):
    _require_receipt(pending, receipt, child_id)
    with SessionLocal() as db:
        # SQLite ignores FOR UPDATE; acquire its write reservation before
        # reading either row so an intervening source edit cannot commit.
        if db.get_bind().dialect.name == 'sqlite':
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        source = db.query(Session).filter(Session.id == pending['source_chat_id'], Session.owner == owner).with_for_update().first()
        if not _source_matches(source, pending) or _binding(source.mimo_state or {}) != _binding(source_state):
            raise ForkOperationError('HISTORY_REVISION_CONFLICT', 'Source changed while forking', candidate_id=child_id)
        row = db.query(Session).filter(Session.id == child_id, Session.owner == owner).with_for_update().first()
        if row is None: raise ForkOperationError("FORK_TARGET_MISSING", "Fork candidate is unavailable", candidate_id=child_id)
        state = deepcopy(row.mimo_state or {})
        if state.get("history_operation") != pending or state.get("managed_binding", {}).get("engineSessionID") != pending["target_native_id"]:
            raise ForkOperationError("FORK_REVISION_CONFLICT", "Fork candidate authority changed", candidate_id=child_id)
        state["history_operation"] = {**pending, "status": "settled", "hidden_message_ids": [], "outcome": "confirmed", "native_receipt": receipt}
        row.mimo_state = state; row.archived = False; row.transcript_revision = int(row.transcript_revision or 0) + 1
        db.commit()
        return {"status": "settled", "id": child_id, "name": row.name, "transcript_revision": int(row.transcript_revision or 0)}


async def execute(worker, manager, *, sid, owner, operation, payload, check_current):
    bridge = worker._bridge
    source_state, revision, order = _read(sid, owner)
    account_id = payload.get("_source_account_id")
    if operation == "fork-reconcile":
        child_id = payload["child_session_id"]
        status = fork_receipt_status(sid=sid, owner=owner, account_id=account_id, candidate_id=child_id)
        if status["status"] == "settled":
            return status
        if status["status"] != "pending":
            raise ForkOperationError("FORK_AUTHORITY_MISMATCH", "The original fork candidate is unavailable or stale", candidate_id=child_id)
        child_state, _, _ = _read(child_id, owner)
        pending = child_state["history_operation"]
        if pending.get("source_revision") != revision:
            raise ForkOperationError("HISTORY_REVISION_CONFLICT", "Original fork source revision changed", candidate_id=child_id)
        binding = child_state.get("managed_binding") or {}
        directory = bridge.mapped_session_workspace(child_id)
        if (bridge.mapped_session_id(child_id) != pending["target_native_id"]
                or binding.get("engineSessionID") != pending["target_native_id"]
                or not directory or directory != bridge.mapped_session_workspace(sid)
                or directory != binding.get("physicalCwd")
                or binding.get("authorityWorkspaceID") != (source_state.get("managed_binding") or {}).get("authorityWorkspaceID")):
            raise ForkOperationError("FORK_AUTHORITY_MISMATCH", "Candidate mapping changed", candidate_id=child_id)
        durable_map = bridge._durable_session_map
        original_map = await durable_map.async_lookup(child_id)
        if not original_map or original_map.get("current") != pending["target_native_id"]:
            raise ForkOperationError("FORK_AUTHORITY_MISMATCH", "Candidate mapping changed", candidate_id=child_id)
        check_current()
        result = await worker.managed_engine_call("_openclank/session/v1/messages/source-map", {
            "sessionID": pending["target_native_id"], "directory": directory,
        })
        check_current()
        receipt = result.get("forkReceipt")
        _require_receipt(pending, receipt, child_id)
        await bridge.resume_session(child_id, pending["target_native_id"], with_memory=bool(binding.get("memoryEnabled", True)), copal_workspace=str(binding.get("copalWorkspace") or "default"), authority_workspace_id=str(binding.get("authorityWorkspaceID") or ""))
        async with durable_map.async_chat_admission_lock(child_id):
            check_current()
            current_map = await durable_map.async_lookup(child_id)
            current_child, _, _ = _read(child_id, owner)
            if current_map != original_map or current_child.get("managed_binding") != binding:
                raise ForkOperationError("FORK_AUTHORITY_MISMATCH", "Candidate mapping changed", candidate_id=child_id)
            return _finish(child_id, owner, pending=pending, receipt=receipt, source_state=source_state)
    try:
        hidden = visibility(source_state, require_settled=True)
    except HistoryOperationError as exc:
        raise ForkOperationError(exc.code, str(exc)) from exc
    if type(payload.get("expected_transcript_revision")) is not int or payload["expected_transcript_revision"] != revision:
        raise ForkOperationError("HISTORY_REVISION_CONFLICT", "expected_transcript_revision must match the source transcript")
    child_id = payload.get("request_id") or str(uuid.uuid4())
    with SessionLocal() as db:
        existing = db.get(Session, child_id)
        if existing is not None:
            pending = (existing.mimo_state or {}).get("history_operation") or {}
            if (existing.owner == owner and pending.get("operation") == "fork"
                    and pending.get("source_chat_id") == sid and pending.get("source_account_id") == account_id):
                raise ForkOperationError("FORK_RECONCILIATION_REQUIRED", "Inspect the receipt for the original fork request", candidate_id=child_id)
            raise ForkOperationError("FORK_REQUEST_CONFLICT", "Fork request ID is already in use")
        existing = _pending_fork(db, sid, owner)
        if existing is not None:
            raise ForkOperationError("FORK_RECONCILIATION_REQUIRED", "Settle the original fork before creating another", candidate_id=existing.id)
    source_native_id = bridge.mapped_session_id(sid)
    directory = bridge.mapped_session_workspace(sid)
    check_current()
    source = await worker.managed_engine_call("_openclank/session/v1/messages/source-map", {"sessionID": source_native_id, "directory": directory})
    source_map = {item["hostSourceMessageID"]: item["nativeMessageID"] for item in source.get("anchors", [])}
    if len(source_map) != len(source.get("anchors", [])) or len(set(source_map.values())) != len(source_map):
        raise ForkOperationError("SOURCE_MAPPING_AMBIGUOUS", "Source map is ambiguous")
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
    source_model = manager.get_session(sid)
    title = payload.get("title") or f"⫝ {source_model.name}"
    if not isinstance(title, str) or not title.strip():
        raise ForkOperationError("FORK_TITLE_INVALID", "Fork title must be a non-empty string")
    if type(source.get("sourceMessageCount")) is not int or source["sourceMessageCount"] < 0:
        raise ForkOperationError("SOURCE_BOUNDARY_UNMAPPED", "Native source message coverage is unavailable")
    reserved = await bridge._client.reserve_session()
    target_native_id = reserved["sessionID"]
    check_current()
    source_binding = source_state.get("managed_binding") or {}
    pending = {"id": child_id, "operation": "fork", "status": "pending", "before_hidden": [], "after_hidden": [],
               "source_chat_id": sid, "source_revision": revision, "source_native_id": source_native_id,
               "source_account_id": account_id, "source_binding": _binding(source_state),
               "source_workspace_id": source_model.workspace_id, "source_model_route_id": source_model.provider_model_route_id,
               "source_host_boundary_id": cutoff, "target_native_id": target_native_id,
               "generation": getattr(worker, "installed_generation", 0),
               "expected_source_message_count": source["sourceMessageCount"]}
    if payload.get("expected_source_key"):
        pending["source_key"] = payload["expected_source_key"]
    selected_ids = [item[0] for item in visible]; cloned = []; host_map = {}
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        source_row = db.query(Session).filter(Session.id == sid, Session.owner == owner).with_for_update().first()
        if not _source_matches(source_row, pending):
            raise ForkOperationError("HISTORY_REVISION_CONFLICT", "Source changed before fork reservation")
        pending["source_key"] = fork_source_key(source_row)
        existing = _pending_fork(db, sid, owner)
        if existing is not None:
            raise ForkOperationError("FORK_RECONCILIATION_REQUIRED", "Settle the original fork before creating another", candidate_id=existing.id)
        if db.get(Session, child_id) is not None:
            raise ForkOperationError("FORK_REQUEST_CONFLICT", "Fork request ID is already in use")
        rows = {item.id: item for item in db.query(ChatMessage).filter(ChatMessage.session_id == sid, ChatMessage.id.in_(selected_ids)).all()}
        from src.openclank.response_style import style_snapshot
        source_style = style_snapshot(source_row.response_style, source_row.response_style_revision)
        child = Session(id=child_id, owner=owner, name=title, endpoint_url=source_row.endpoint_url, endpoint_id=source_row.endpoint_id, model=source_row.model, provider_model_route_id=source_row.provider_model_route_id, workspace_id=source_row.workspace_id, response_style=source_style['mode'], response_style_revision=0, archived=True, mimo_state={"history_operation": deepcopy(pending)})
        db.add(child); db.flush()
        from datetime import timedelta
        last_timestamp = None
        for old_id in selected_ids:
            item = rows[old_id]
            metadata = _source_metadata(item)
            # Existing model context excludes local command display rows.
            # If a user row has a Native anchor, that authority takes priority.
            if metadata.get("source") == "slash" and old_id not in source_map:
                continue
            if item.role == "user" and old_id not in source_map:
                raise ForkOperationError("SOURCE_BOUNDARY_UNMAPPED", "Unmapped source turns cannot be safely native-forked")
            new_id = f"root_{uuid.uuid4().hex}" if item.role == "user" else str(uuid.uuid4())
            metadata.pop("_db_id", None)
            metadata.pop("root_operation_id", None)
            metadata["forked_from_message_id"] = old_id
            if item.role == "user": metadata["root_operation_id"] = new_id; host_map[old_id] = new_id
            content = item.content
            try: parsed = json.loads(content) if isinstance(content, str) else content
            except (ValueError, TypeError): parsed = content
            missing = reserve_message_upload_references(getattr(manager, "upload_handler", None), owner, parsed, metadata, session_id=child_id)
            if missing:
                raise ForkOperationError("FORK_ATTACHMENT_UNAVAILABLE", "A source attachment is unavailable")
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
    try:
        # Full host rows and attachments are captured before native callback writes.
        for index, (new_id, message) in enumerate(cloned):
            manager._archive_message_parts(child_id, message, msg_id=new_id, revision=1, event_sequence=index)
        async with bridge._durable_session_map.async_chat_admission_lock(child_id) as guard:
            bridge._stage_candidate_binding(child_id, target_native_id, owner, previous_binding=None, cwd=directory, authority_workspace_id=str(source_binding.get("authorityWorkspaceID") or ""), memory_enabled=bool(source_binding.get("memoryEnabled", True)), copal_workspace=str(source_binding.get("copalWorkspace") or "default"), memory_workspace_id=str(source_binding.get("memoryWorkspaceID") or ""), expected_current=None, expected_mapping_revision=0)
            await bridge._bind_engine_session(child_id, target_native_id, expected_current=None, expected_mapping_revision=0, guard=guard)
            map_revision, mapping_revision = await bridge._durable_session_map.async_revisions(child_id)
            await bridge._bind_canonical_session_async(target_native_id, child_id, owner, guard=guard, physical_cwd=directory, authority_workspace_id=str(source_binding.get("authorityWorkspaceID") or ""), memory_enabled=bool(source_binding.get("memoryEnabled", True)), map_revision=map_revision, mapping_revision=mapping_revision, copal_workspace=str(source_binding.get("copalWorkspace") or "default"), memory_workspace_id=str(source_binding.get("memoryWorkspaceID") or ""))
            admitted_map = await bridge._durable_session_map.async_lookup(child_id)
            admitted_child, _, _ = _read(child_id, owner)
            admitted_binding = deepcopy(_binding(admitted_child))
            if not admitted_map or admitted_map.get("current") != target_native_id or admitted_binding.get("engineSessionID") != target_native_id:
                raise ForkOperationError("FORK_REVISION_CONFLICT", "Fork candidate authority changed", candidate_id=child_id)
        # Native fork/resume can call back into host cwd authority; do not hold
        # the child's non-reentrant admission lock while awaiting either call.
        check_current()
        result = await worker.managed_engine_call("_openclank/session/v1/fork/create", request)
        check_current()
        native_receipt = result.get("forkReceipt")
        if native_receipt is None and pending["expected_source_message_count"] == 0 and result.get("created") is True:
            # The direct create response carries the Native proof for an
            # empty fork. A later observation of a zero count alone cannot.
            native_receipt = {key: result.get(key) for key in ("sourceSessionID", "targetSessionID", "fingerprint")}
            native_receipt["sourceMessageCount"] = 0
        _require_receipt(pending, native_receipt, child_id)
        await bridge.resume_session(child_id, target_native_id, with_memory=bool(source_binding.get("memoryEnabled", True)), copal_workspace=str(source_binding.get("copalWorkspace") or "default"), authority_workspace_id=str(source_binding.get("authorityWorkspaceID") or ""))
        async with bridge._durable_session_map.async_chat_admission_lock(child_id):
            check_current()
            current_map = await bridge._durable_session_map.async_lookup(child_id)
            current_source, current_revision, _ = _read(sid, owner)
            current_child, _, _ = _read(child_id, owner)
            if current_map != admitted_map or _binding(current_child) != admitted_binding:
                raise ForkOperationError("FORK_REVISION_CONFLICT", "Fork candidate authority changed", candidate_id=child_id)
            if current_revision != revision or _binding(current_source) != _binding(source_state):
                raise ForkOperationError("HISTORY_REVISION_CONFLICT", "Source changed while forking", candidate_id=child_id)
            receipt = _finish(child_id, owner, pending=current_child["history_operation"], receipt=native_receipt, source_state=source_state)
            return receipt

    except BaseException as exc:
        if isinstance(exc, ForkOperationError):
            exc.retain_lease = True
            raise
        raise ForkOperationError(
            "FORK_RECONCILIATION_REQUIRED",
            "Reserved fork is pending; reconcile this exact candidate before opening it",
            candidate_id=child_id,
            retain_lease=True,
        ) from exc
