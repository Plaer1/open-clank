"""Validation and wire helpers for native-authoritative actor input receipts."""
from __future__ import annotations

import hashlib
from typing import Any

from src.tool_policy import normalize_turn_policy_snapshot


class InputRequestError(ValueError):
    pass


def unsupported_receipt_policy(policy: Any, *, legacy: bool = False) -> str | None:
    """Explain receipt-only modes without claiming earlier context was removed."""
    if policy is None:
        return "This older follow-up lacks a recorded memory policy and cannot be safely delivered." if legacy else None
    if not isinstance(policy, dict):
        return "The follow-up policy is unavailable."
    if policy.get("noMemory") is True:
        return "No Memory follow-ups are unavailable in this existing actor context."
    if policy.get("compareMode") is True:
        return "Compare follow-ups require compare execution, which is unavailable here."
    return None


def normalize_admission(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputRequestError("input request must be an object")
    allowed = {"requestID", "text", "attachmentRefs", "uploadIDs", "questionRefs", "questionAnswers", "mode", "handoffID", "actorSessionId", "modelOptions", "turnPolicy", "expectedExecutionRevision"}
    if set(value) - allowed:
        raise InputRequestError("input request contains unsupported fields")
    request_id = value.get("requestID")
    text = value.get("text", "")
    if not isinstance(request_id, str) or not request_id.strip() or request_id != request_id.strip() or len(request_id) > 200:
        raise InputRequestError("requestID must be a trimmed nonempty string of at most 200 characters")
    if not isinstance(text, str) or len(text) > 200_000:
        raise InputRequestError("text must be a string of at most 200000 characters")
    attachments = value.get("attachmentRefs", [])
    questions = value.get("questionRefs", [])
    upload_ids = value.get("uploadIDs", [])
    if not isinstance(attachments, list) or any(not isinstance(item, str) or not item for item in attachments):
        raise InputRequestError("attachmentRefs must be nonempty string references")
    if not isinstance(questions, list) or any(not isinstance(item, str) or not item for item in questions):
        raise InputRequestError("questionRefs must be nonempty string references")
    if not isinstance(upload_ids, list) or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in upload_ids):
        raise InputRequestError("uploadIDs must be trimmed nonempty strings")
    if len(attachments) > 100 or len(questions) > 100 or len(upload_ids) > 100 or len(set(attachments)) != len(attachments) or len(set(questions)) != len(questions) or len(set(upload_ids)) != len(upload_ids):
        raise InputRequestError("input contains too many or duplicate references")
    raw_answers = value.get("questionAnswers", [])
    if not isinstance(raw_answers, list) or len(raw_answers) > 32:
        raise InputRequestError("questionAnswers must be a bounded array")
    question_answers = []
    seen_questions = set()
    for answer in raw_answers:
        if not isinstance(answer, dict) or set(answer) - {"requestID", "questionRevision", "disposition", "answers"}:
            raise InputRequestError("question answer contains unsupported fields")
        answer_id = answer.get("requestID")
        revision = answer.get("questionRevision")
        disposition = answer.get("disposition")
        selections = answer.get("answers")
        if (not isinstance(answer_id, str) or not answer_id.strip() or answer_id != answer_id.strip()
                or len(answer_id) > 200 or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
                or disposition not in {"answered", "dismissed"} or answer_id in seen_questions):
            raise InputRequestError("question answer identity or disposition is invalid")
        seen_questions.add(answer_id)
        normalized = {"requestID": answer_id, "questionRevision": revision, "disposition": disposition}
        if selections is not None:
            if not isinstance(selections, list) or len(selections) > 16:
                raise InputRequestError("question answer cardinality is invalid")
            clean_selections = []
            for selected in selections:
                if not isinstance(selected, list) or not selected or len(selected) > 100 or any(
                    not isinstance(item, str) or not item.strip() or item != item.strip() or len(item) > 2_048
                    for item in selected
                ):
                    raise InputRequestError("question answer selection is invalid")
                clean_selections.append(list(selected))
            normalized["answers"] = clean_selections
        if disposition == "answered" and selections is None:
            raise InputRequestError("answered question requires selections")
        if disposition == "dismissed" and selections not in (None, []):
            raise InputRequestError("dismissed question cannot include selections")
        question_answers.append(normalized)
    expected_question_refs = [f"{answer['requestID']}@{answer['questionRevision']}" for answer in question_answers]
    if question_answers and (set(expected_question_refs) != set(questions) or len(expected_question_refs) != len(questions)):
        raise InputRequestError("questionRefs must match the structured question answers")
    if question_answers and (text or attachments or upload_ids):
        raise InputRequestError("structured question answers cannot include freeform text or attachments")
    mode = value.get("mode", "steer")
    if mode not in {"steer", "after-turn"}:
        raise InputRequestError("mode must be steer or after-turn")
    handoff_id = value.get("handoffID", f"send:{request_id}")
    if not isinstance(handoff_id, str) or not handoff_id.strip() or handoff_id != handoff_id.strip() or len(handoff_id) > 200:
        raise InputRequestError("handoffID must be a trimmed nonempty string of at most 200 characters")
    actor_session_id = value.get("actorSessionId")
    if actor_session_id is not None and (not isinstance(actor_session_id, str) or not actor_session_id.strip()):
        raise InputRequestError("actorSessionId must be a nonempty string")
    expected_execution = value.get("expectedExecutionRevision")
    if "expectedExecutionRevision" in value and (type(expected_execution) is not int or expected_execution < 0 or expected_execution > 9007199254740991):
        raise InputRequestError("expectedExecutionRevision must be a nonnegative safe integer")
    model_options = value.get("modelOptions")
    if model_options is not None:
        if not isinstance(model_options, dict) or set(model_options) != {"route_id", "catalog_revision", "options"}:
            raise InputRequestError("modelOptions must be a route-bound option selection")
        route_id = model_options.get("route_id")
        revision = model_options.get("catalog_revision")
        options = model_options.get("options")
        if not isinstance(route_id, str) or not route_id.strip() or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise InputRequestError("modelOptions route and catalog revision are invalid")
        if not isinstance(options, dict) or set(options) - {"reasoning_effort"} or any(not isinstance(item, str) for item in options.values()):
            raise InputRequestError("modelOptions contains unsupported values")
        model_options = {"route_id": route_id, "catalog_revision": revision, "options": dict(options)}
    raw_turn_policy = value.get("turnPolicy")
    turn_policy = None
    if raw_turn_policy is not None:
        try:
            turn_policy = normalize_turn_policy_snapshot(raw_turn_policy)
        except ValueError as exc:
            raise InputRequestError(str(exc)) from exc
    return {
        "requestID": request_id,
        "handoffID": handoff_id,
        "text": text,
        "attachmentRefs": attachments,
        "uploadIDs": upload_ids,
        "questionRefs": questions,
        "questionAnswers": question_answers,
        "mode": mode,
        **({"modelOptions": model_options} if model_options is not None else {}),
        **({"turnPolicy": turn_policy} if turn_policy is not None else {}),
        **({"actorSessionId": actor_session_id} if actor_session_id else {}),
        **({"expectedExecutionRevision": expected_execution} if expected_execution is not None else {}),
    }


def native_admission(value: dict[str, Any], actor: dict[str, Any], origin_turn_id: str | None, *, attachment_parts=None, model_request_options=None, effective_policy_ceiling=None, queued_source_proof=None) -> dict[str, Any]:
    revision = actor.get("executionRevision")
    status = actor.get("status")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise InputRequestError("native actor execution revision is unavailable")
    if status not in {"pending", "running", "idle"}:
        raise InputRequestError("native actor lifecycle status is unavailable")
    result = {
        "requestID": value["requestID"],
        "executionRevision": revision,
        "text": value["text"],
        "attachmentRefs": value["attachmentRefs"],
        "attachmentUploadIDs": value["uploadIDs"],
        "attachmentParts": list(attachment_parts or []),
        "questionRefs": value["questionRefs"],
        "mode": value["mode"],
        "modelRequestOptions": model_request_options or {"present": False},
    }
    if "expectedExecutionRevision" in value:
        result["selectedExecutionRevision"] = value["expectedExecutionRevision"]
    if value.get("questionAnswers"):
        result["questionAnswers"] = value["questionAnswers"]
    if type(queued_source_proof) is str and queued_source_proof:
        result["queuedSourceProof"] = queued_source_proof
    if origin_turn_id:
        result["originTurnID"] = origin_turn_id
    if value.get("turnPolicy") is not None:
        result["turnPolicy"] = dict(value["turnPolicy"])
    if effective_policy_ceiling is not None:
        result["effectivePolicyCeiling"] = dict(effective_policy_ceiling)
    return result


def materialize_upload_content(upload_ids: list[str], *, owner: str, upload_handler=None) -> list[dict[str, str]]:
    """Convert owner-scoped uploads with the same content materializer used by ACP sends."""
    if not upload_ids:
        return []
    if upload_handler is None:
        from src.tool_utils import get_upload_handler
        upload_handler = get_upload_handler()
    if upload_handler is None:
        raise InputRequestError("attachment service is unavailable")
    import os
    from src.constants import UPLOAD_DIR
    from src.openclank.acp_bridge import _NATIVE_MEDIA_PAYLOAD_BYTES, _NATIVE_MEDIA_SOURCE_BYTES
    from src.document_processor import build_user_content
    resolved = {}
    for identifier in upload_ids:
        item = upload_handler.resolve_upload(identifier, owner=owner)
        if item is None:
            raise InputRequestError("an uploaded attachment is unavailable for this owner")
        path = item.get("path") if isinstance(item, dict) else None
        try:
            size = os.path.getsize(path) if path else -1
        except OSError:
            size = -1
        if size < 0 or size > _NATIVE_MEDIA_SOURCE_BYTES:
            raise InputRequestError("an uploaded attachment exceeds the supported source size")
        resolved[identifier] = item
    total_payload_bytes = sum(((os.path.getsize(item["path"]) + 2) // 3) * 4 for item in resolved.values())
    if total_payload_bytes > _NATIVE_MEDIA_PAYLOAD_BYTES:
        raise InputRequestError("combined uploaded attachments exceed the supported payload size")
    content = build_user_content(
        "", upload_ids, UPLOAD_DIR, upload_handler, session_id=None, owner=owner,
        resolved_uploads=resolved, structured_resources=True, extract_attachments=False,
    )
    if isinstance(content, str):
        content = [{"type": "text", "text": content}] if content else []
    parts: list[dict[str, str]] = []
    for block in content:
        if not isinstance(block, dict):
            raise InputRequestError("attachment conversion returned an invalid content block")
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
            continue
        if kind in {"image_url", "image"}:
            image = block.get("image_url") if kind == "image_url" else block
            url = image.get("url") if isinstance(image, dict) else None
            mime = str((image or {}).get("mediaType") or "") if isinstance(image, dict) else ""
            if isinstance(url, str) and url.startswith("data:"):
                header = url[5:].split(",", 1)[0].split(";", 1)[0]
                mime = mime or header
                parts.append({"type": "file", "mime": mime, "url": url})
                continue
        if kind in {"audio", "input_audio"}:
            audio = block.get("audio") or block.get("input_audio") or {}
            url = audio.get("url") if isinstance(audio, dict) else None
            if isinstance(url, str) and url.startswith("data:"):
                mime = url[5:].split(",", 1)[0].split(";", 1)[0]
                parts.append({"type": "file", "mime": mime, "url": url})
                continue
        if kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict) and isinstance(resource.get("blob"), str):
                mime = str(resource.get("mimeType") or "application/octet-stream")
                uri = str(resource.get("uri") or "")
                filename = uri.rsplit("/", 1)[-1] if uri else "attachment"
                parts.append({"type": "file", "mime": mime, "filename": filename[:512], "url": f"data:{mime};base64,{resource['blob']}"})
                continue
        raise InputRequestError("attachment content cannot be represented safely for actor delivery")
    return parts


def same_client_receipt(receipt: dict[str, Any], value: dict[str, Any], actor_session_id: str) -> bool:
    """Compare client-controlled fields without recomputing frozen actor state."""
    frozen_options = receipt.get("modelRequestOptions") or {"present": False}
    has_structured_answer = bool(value.get("questionAnswers"))
    return (
        receipt.get("sessionID") == actor_session_id
        and ("expectedExecutionRevision" not in value or receipt.get("selectedExecutionRevision") == value["expectedExecutionRevision"])
        and (isinstance(receipt.get("text"), str) if has_structured_answer else receipt.get("text") == value["text"])
        and receipt.get("attachmentRefs", []) == value["attachmentRefs"]
        and receipt.get("attachmentUploadIDs", []) == value["uploadIDs"]
        and receipt.get("questionRefs", []) == value["questionRefs"]
        and receipt.get("questionAnswers", []) == value.get("questionAnswers", [])
        and frozen_options.get("present", False) == ("modelOptions" in value)
        and (not ("modelOptions" in value) or (
            frozen_options.get("optionRouteID") == value["modelOptions"]["route_id"]
            and frozen_options.get("catalogRevision") == value["modelOptions"]["catalog_revision"]
            and frozen_options.get("options") == value["modelOptions"]["options"]
        ))
        and (
            value.get("turnPolicy") is None
            and receipt.get("turnPolicy") is None
            or value.get("turnPolicy") is not None
            and receipt.get("turnPolicy") == value["turnPolicy"]
        )
    )


def should_resume(status: str) -> bool:
    """An idle actor needs an explicit handoff; active actors consume at a boundary."""
    return status == "idle"


def handoff_root_operation_id(owner: str, stable_chat_id: str, request_id: str, handoff_id: str) -> str:
    identity = "\0".join((owner, stable_chat_id, request_id, handoff_id)).encode("utf-8")
    return "input_" + hashlib.sha256(identity).hexdigest()


def native_handoff(value: dict[str, Any], actor: dict[str, Any], managed_route: dict[str, Any] | None = None) -> dict[str, Any]:
    revision = actor.get("executionRevision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise InputRequestError("native actor execution revision is unavailable")
    result = {"handoffID": value["handoffID"], "expectedExecutionRevision": revision,
              "rootOperationID": value["rootOperationID"]}
    if managed_route is not None:
        if not isinstance(managed_route, dict) or managed_route.get("rootOperationID") != value.get("rootOperationID"):
            raise InputRequestError("managed operation route is unavailable")
        result["managedRoute"] = managed_route
    return result
