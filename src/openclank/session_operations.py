"""Recoverable native history operations over exact canonical source boundaries.

The canonical transcript stays intact. A host-owned IDs-only view receipt makes
native revert boundaries survive reload and worker bootstrap; pending mutations
fail closed until an explicit reconciliation observes the native owner pointer.
"""
from __future__ import annotations
from copy import deepcopy
from typing import Any
import uuid
from core.database import Session, ChatMessage, SessionLocal

KEY = "history_operation"

class HistoryOperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, choices: list[str] | None = None):
        super().__init__(message); self.code = code; self.status = 409; self.choices = choices or []


def visibility(state: dict | None, *, require_settled: bool = False) -> set[str]:
    operation = (state or {}).get(KEY) or {}
    if not isinstance(operation, dict): raise HistoryOperationError("HISTORY_BOUNDARY_INVALID", "History boundary is invalid")
    if operation.get("status") == "pending":
        if require_settled: raise HistoryOperationError("HISTORY_RECONCILIATION_REQUIRED", "Native history mutation requires explicit reconciliation")
        value = operation.get("before_hidden", [])
    else: value = operation.get("hidden_message_ids", [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HistoryOperationError("HISTORY_BOUNDARY_INVALID", "History boundary is invalid")
    return set(value)


def _binding(state: dict) -> dict:
    value = state.get("managed_binding") or {}
    return {key: value.get(key) for key in ("owner", "stableChatID", "engineSessionID", "physicalCwd", "workspaceRevision", "mappingRevision", "mapRevision")}


def _read(sid: str, owner: str) -> tuple[dict, int, list[tuple[str, str]]]:
    with SessionLocal() as db:
        row = db.query(Session).filter(Session.id == sid, Session.owner == owner).first()
        if row is None: raise KeyError(sid)
        records = db.query(ChatMessage).filter(ChatMessage.session_id == sid).order_by(ChatMessage.timestamp, ChatMessage.id).all()
        return deepcopy(row.mimo_state or {}), int(row.transcript_revision or 0), [(item.id, item.role) for item in records]


def _publish(sid: str, owner: str, *, previous: dict, expected_revision: int, operation: dict, advance: bool) -> int:
    with SessionLocal() as db:
        row = db.query(Session).filter(Session.id == sid, Session.owner == owner).with_for_update().first()
        if row is None: raise KeyError(sid)
        latest = deepcopy(row.mimo_state or {})
        if int(row.transcript_revision or 0) != expected_revision or _binding(latest) != _binding(previous) or latest.get(KEY) != previous.get(KEY):
            raise HistoryOperationError("HISTORY_REVISION_CONFLICT", "History or managed binding changed; reload before retrying")
        latest[KEY] = deepcopy(operation)
        next_revision = expected_revision + int(advance)
        changed = db.query(Session).filter(Session.id == sid, Session.owner == owner, Session.transcript_revision == expected_revision).update({Session.mimo_state: latest, Session.transcript_revision: next_revision}, synchronize_session=False)
        if changed != 1: raise HistoryOperationError("HISTORY_REVISION_CONFLICT", "History changed during mutation")
        db.commit(); return next_revision



def restore_draft(sid: str, owner: str, message_id: str | None) -> dict | None:
    if not message_id: return None
    import json
    from src.attachment_refs import attachment_refs_from_metadata
    with SessionLocal() as db:
        row = db.query(ChatMessage).join(Session, Session.id == ChatMessage.session_id).filter(ChatMessage.id == message_id, ChatMessage.session_id == sid, Session.owner == owner, ChatMessage.role == "user").first()
        if row is None: return None
        # Pasted JSON remains exact saved text; only metadata owns uploads.
        content = row.content
        try: metadata = json.loads(row.meta_data) if row.meta_data else {}
        except (ValueError, TypeError): metadata = {}
        return {"host_message_id": message_id, "content": content, "raw_content": content, "content_kind": "text", "attachment_refs": attachment_refs_from_metadata(metadata if isinstance(metadata, dict) else {})}

def public_operation(value: dict) -> dict:
    return {key: deepcopy(item) for key, item in value.items() if key not in {"binding", "source_binding", "before_hidden", "after_hidden"}}


def _pointer(value: Any) -> dict | None:
    if value is None: return None
    if not isinstance(value, dict) or not isinstance(value.get("messageID"), str):
        raise HistoryOperationError("NATIVE_HISTORY_INVALID", "Native history pointer is invalid")
    return {key: value[key] for key in ("messageID", "partID") if key in value}


async def execute(worker, *, sid: str, owner: str, operation: str, payload: dict, check_current) -> dict:
    """Caller holds one worker lease and this chat's durable admission lock."""
    bridge = worker._bridge
    if sid not in bridge.mapped_sessions(owner=owner):
        raise HistoryOperationError("SESSION_NOT_READY", "Chat must be admitted before native history operations")
    native_id = bridge.mapped_session_id(sid)
    directory = bridge.mapped_session_workspace(sid)
    state, revision, records = _read(sid, owner)
    binding = _binding(state)
    if binding.get("engineSessionID") != native_id or binding.get("physicalCwd") != directory:
        raise HistoryOperationError("HISTORY_BINDING_CONFLICT", "Managed history binding changed")
    check_current()
    native = await worker.managed_engine_call("_openclank/session/v1/messages/source-map", {"directory": directory, "sessionID": native_id})
    check_current()
    anchors = native.get("anchors")
    if not isinstance(anchors, list): raise HistoryOperationError("NATIVE_HISTORY_INVALID", "Native source mapping is unavailable")
    source_map = {}
    native_ids = set()
    for anchor in anchors:
        if not isinstance(anchor, dict) or not isinstance(anchor.get("hostSourceMessageID"), str) or not isinstance(anchor.get("nativeMessageID"), str):
            raise HistoryOperationError("NATIVE_HISTORY_INVALID", "Native source mapping is invalid")
        source_id, message_id = anchor["hostSourceMessageID"], anchor["nativeMessageID"]
        if source_id in source_map or message_id in native_ids: raise HistoryOperationError("SOURCE_MAPPING_AMBIGUOUS", "Native source mapping is ambiguous")
        source_map[source_id] = message_id; native_ids.add(message_id)
    pointer = _pointer(native.get("revert"))
    old = state.get(KEY) or {}
    hidden = visibility(state)
    choices = [message_id for message_id, role in records if role == "user" and message_id not in hidden and message_id in source_map]
    if operation == "timeline":
        return {"transcript_revision": revision, "source_map": [{"host_message_id": key, "native_message_id": source_map[key]} for key in choices], "history_operation": public_operation(old), "revert": pointer}
    expected = payload.get("expected_transcript_revision")
    if type(expected) is not int or expected != revision:
        raise HistoryOperationError("HISTORY_REVISION_CONFLICT", "expected_transcript_revision must match the current transcript")
    if operation == "history-reconcile":
        if old.get("status") != "pending": return {"status": "settled", "transcript_revision": revision, "history_operation": public_operation(old)}
        if pointer == old.get("after_native_revert"):
            after_hidden = old["after_hidden"]; outcome = "confirmed"
        elif pointer == old.get("before_native_revert"):
            after_hidden = old["before_hidden"]; outcome = "unapplied"
        else: raise HistoryOperationError("HISTORY_RECONCILIATION_REQUIRED", "Native pointer does not match this pending operation")
        receipt = {**old, "status": "settled", "hidden_message_ids": after_hidden, "outcome": outcome}
        next_revision = _publish(sid, owner, previous=state, expected_revision=revision, operation=receipt, advance=True)
        return {"status": "settled", "transcript_revision": next_revision, "history_operation": public_operation(receipt), "restore_draft": restore_draft(sid, owner, receipt.get("host_message_id")) if receipt.get("operation") == "undo" and receipt.get("outcome") == "confirmed" else None}
    if old.get("status") == "pending": raise HistoryOperationError("HISTORY_RECONCILIATION_REQUIRED", "Reconcile the pending operation before another mutation")
    if operation == "undo":
        selected = payload.get("host_message_id") or (choices[-1] if choices else None)
        if selected not in choices: raise HistoryOperationError("SOURCE_BOUNDARY_UNMAPPED", "Choose a source-mapped user turn; legacy or hidden boundaries cannot be reverted", choices=choices)
        index = next(index for index, record in enumerate(records) if record[0] == selected)
        after_hidden = list(dict.fromkeys([*hidden, *(item[0] for item in records[index:])]))
        after_pointer = {"messageID": source_map[selected]}
        suffix = "revert"; native_payload = after_pointer
    elif operation == "redo":
        if old.get("operation") != "undo" or pointer != old.get("after_native_revert"):
            raise HistoryOperationError("REDO_UNAVAILABLE", "A later turn or native change has invalidated redo")
        after_hidden = []; after_pointer = None; suffix = "unrevert"; native_payload = {}
    else: raise ValueError("unsupported native history operation")
    intent = {"id": uuid.uuid4().hex, "operation": operation, "status": "pending", "before_hidden": list(hidden), "after_hidden": after_hidden, "before_native_revert": pointer, "after_native_revert": after_pointer, "binding": binding, "host_message_id": selected if operation == "undo" else None, "generation": getattr(worker, "installed_generation", 0)}
    _publish(sid, owner, previous=state, expected_revision=revision, operation=intent, advance=False)
    # After publication any interruption/error deliberately leaves a fence.
    check_current()
    response = await worker.session_http_request(sid, "POST", suffix, owner=owner, payload=native_payload)
    check_current()
    if response.status_code >= 400:
        raise HistoryOperationError("NATIVE_HISTORY_REJECTED", "Native history operation was rejected; reconcile its pending intent")
    result = response.json()
    if not isinstance(result, dict) or _pointer(result.get("revert")) != after_pointer:
        raise HistoryOperationError("NATIVE_HISTORY_INVALID", "Native receipt did not confirm this exact boundary")
    latest, latest_revision, _ = _read(sid, owner)
    if latest_revision != revision or _binding(latest) != binding or (latest.get(KEY) or {}).get("id") != intent["id"]:
        raise HistoryOperationError("HISTORY_REVISION_CONFLICT", "Native operation completed but host history changed; reconcile before continuing")
    receipt = {**intent, "status": "settled", "hidden_message_ids": after_hidden, "file_snapshot_available": bool((result.get("revert") or {}).get("snapshot")) if operation == "undo" else None, "outcome": "confirmed"}
    next_revision = _publish(sid, owner, previous=latest, expected_revision=revision, operation=receipt, advance=True)
    return {"status": "settled", "transcript_revision": next_revision, "history_operation": public_operation(receipt), "restore_draft": restore_draft(sid, owner, receipt.get("host_message_id")) if receipt.get("operation") == "undo" and receipt.get("outcome") == "confirmed" else None}
