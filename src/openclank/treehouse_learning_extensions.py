"""Native scoped learning collaboration, collections and completion records.

Data lives in the existing owner curriculum aggregate. Only the bounded
learning extension namespace may be changed by learner contribution routes.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
from datetime import UTC, datetime
from typing import Any

from src.openclank.copal_treehouse import TreeHouseError, compute_treehouse_projections
from src.openclank.copal_treehouse_contracts import course_learning_contract


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def reject(message: str, code: str = "invalid_learning_command", status: int = 400):
    raise TreeHouseError(message, code=code, status=status)


def text(value: Any, name: str, limit: int = 12000, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        reject(f"{name} must be {'optional' if empty else 'nonempty'} text of at most {limit} characters")
    return value.strip()


def learning_data(state: dict[str, Any]) -> dict[str, Any]:
    return state.get("extensions", {}).get("learning", {})


def curriculum_digest(state: dict[str, Any], course_id: str) -> str:
    contract = course_learning_contract(state, course_id)
    contract.pop("curriculumRevision", None)
    course = state["courses"][course_id]
    definitions = {kind: {key: value for key, value in state.get(kind, {}).items()
                         if value.get("courseId") == course_id}
                   for kind in ("modules", "activities", "assignments")}
    # Changed titles, requirements, assessment rules or resources require a new record.
    return digest({"course": course, "definitions": definitions, "contract": contract})


def completion_evidence(state: dict[str, Any], course_id: str, learner_id: str) -> dict[str, Any]:
    projection = compute_treehouse_projections(state)["learners"].get(learner_id, {}).get("courses", {}).get(course_id, {})
    event_ids = projection.get("evidenceEventIds", [])
    events = {event["id"]: event for event in state.get("events", []) if event.get("id") in event_ids}
    return {"eligible": bool(projection.get("complete") and projection.get("total") and state["courses"][course_id].get("status") == "published"),
            "courseType": projection.get("courseType"), "completionBasis": projection.get("completionBasis"),
            "completionRuleVersion": projection.get("completionRuleVersion"),
            "resetGeneration": projection.get("resetGeneration", 0), "evidenceEventIds": event_ids,
            "evidenceDigest": digest(events), "curriculumDigest": curriculum_digest(state, course_id),
            "criteria": course_learning_contract(state, course_id)["completionCriteria"],
            "reviewedAssessment": bool(projection.get("verified"))}


def credential_view(record: dict[str, Any], state: dict[str, Any], learner_id: str) -> dict[str, Any]:
    current = completion_evidence(state, record["courseId"], learner_id)
    matches = all(record.get(key) == current.get(key) for key in
                  ("evidenceDigest", "curriculumDigest", "resetGeneration", "completionBasis", "criteria"))
    status = "revoked" if record.get("revokedAt") else ("valid" if current["eligible"] and matches else "superseded")
    return {**copy.deepcopy(record), "status": status,
            "validityReason": record.get("revocationReason") if status == "revoked" else
            (None if status == "valid" else "Completion evidence, requirements or progress generation changed; request a new record after qualifying.")}


def course_learning_view(state: dict[str, Any], course_id: str, actor_id: str, *, can_moderate: bool) -> dict[str, Any]:
    store = learning_data(state).get("courses", {}).get(course_id, {})
    posts = []
    for post in store.get("posts", []):
        item = copy.deepcopy(post)
        if item.get("removedAt"):
            item.update({"body": "This contribution was removed.", "attachments": [], "reactions": {}})
        else:
            item["reactions"] = {key: {"count": len(actors), "mine": actor_id in actors}
                                 for key, actors in item.get("reactions", {}).items()}
        item["canRemove"] = can_moderate or post["authorId"] == actor_id
        posts.append(item)
    return {"schemaVersion": 1, "revision": int(state.get("revision", 0)), "courseId": course_id,
            "transport": "explicit-refresh-cas", "canModerate": can_moderate, "posts": posts,
            "board": copy.deepcopy(store.get("board", {"body": "", "revision": 0, "history": []})),
            "attachmentActivities": [{"id": key, "title": activity.get("title", key)}
                                     for key, activity in state.get("activities", {}).items()
                                     if activity.get("courseId") == course_id and activity.get("status") == "published"
                                     and not activity.get("deletedAt") and activity.get("sourceAttachments")],
            "completion": completion_evidence(state, course_id, actor_id),
            "credentials": [credential_view(value, state, actor_id) for value in store.get("credentials", [])
                            if value.get("learnerId") == actor_id]}


def apply_learning_command(state: dict[str, Any], *, course_id: str, actor_id: str,
                           can_moderate: bool, kind: str, payload: dict[str, Any],
                           command_id: str, expected_revision: int, now: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    now = now or datetime.now(UTC).isoformat()
    fingerprint = digest({"courseId": course_id, "actorId": actor_id, "kind": kind, "payload": payload})
    receipts = learning_data(state).get("receipts", {})
    receipt_key = digest([actor_id, command_id])
    if receipt_key in receipts:
        receipt = receipts[receipt_key]
        if receipt["digest"] != fingerprint:
            reject("Command identity was already used with different contents", "idempotency_conflict", 409)
        return state, {**receipt["result"], "replayed": True}
    if expected_revision != int(state.get("revision", 0)):
        reject("Learning changed in another session; reload before applying your preserved draft", "stale", 409)
    if len(receipts) >= 20000:
        reject("Learning receipt capacity reached; export and maintain this course before contributing", "learning_capacity", 409)
    candidate = copy.deepcopy(state)
    store = candidate.setdefault("extensions", {}).setdefault("learning", {})
    course = store.setdefault("courses", {}).setdefault(course_id, {"posts": [], "credentials": []})
    result: dict[str, Any] = {"courseId": course_id}
    ident = "learning_" + digest([actor_id, command_id])[:24]
    if kind == "discussion.post":
        if set(payload) - {"body", "parentId", "activityId", "attachmentActivityIds"}: reject("Unknown discussion fields")
        body = text(payload.get("body"), "Contribution")
        parent = payload.get("parentId")
        if parent and not any(post["id"] == parent and not post.get("removedAt") for post in course["posts"]):
            reject("Reply target is missing or removed", "post_not_found", 404)
        if len(course["posts"]) >= 3000: reject("Discussion capacity reached", "learning_capacity", 409)
        activity_id = payload.get("activityId")
        if activity_id is not None and not isinstance(activity_id, str): reject("Invalid lesson reference")
        attachments = payload.get("attachmentActivityIds", [])
        if not isinstance(attachments, list) or len(attachments) > 8 or any(not isinstance(key, str) for key in attachments): reject("Invalid attachment references")
        for key in [*attachments, *([activity_id] if activity_id else [])]:
            activity = state.get("activities", {}).get(key, {})
            if activity.get("courseId") != course_id or activity.get("deletedAt") or activity.get("status") != "published": reject("Referenced lesson is unavailable", "attachment_unavailable", 404)
            if key in attachments and not activity.get("sourceAttachments"): reject("Attach resources through Files to this lesson first", "attachment_unavailable", 409)
        course["posts"].append({"id": ident, "authorId": actor_id, "body": body, "parentId": parent,
                               "activityId": activity_id, "attachments": [{"activityId": key} for key in attachments],
                               "createdAt": now, "reactions": {}})
        result["postId"] = ident
    elif kind in {"discussion.react", "discussion.remove"}:
        allowed = {"postId", "reaction"} if kind.endswith("react") else {"postId"}
        if set(payload) - allowed: reject("Unknown contribution fields")
        post = next((item for item in course["posts"] if item["id"] == payload.get("postId")), None)
        if not post or post.get("removedAt"): reject("Contribution is missing or removed", "post_not_found", 404)
        if kind.endswith("remove"):
            if not can_moderate and post["authorId"] != actor_id: reject("Only the contributor or course author may remove this contribution", "forbidden", 403)
            post.update({"removedAt": now, "removedBy": actor_id})
        else:
            reaction = payload.get("reaction")
            if not isinstance(reaction, str) or reaction not in {"helpful", "thanks", "question"}: reject("Unknown reaction")
            actors = post.setdefault("reactions", {}).setdefault(reaction, [])
            if actor_id in actors: actors.remove(actor_id)
            else: actors.append(actor_id)
        result["postId"] = post["id"]
    elif kind == "board.save":
        if set(payload) - {"body", "boardRevision"}: reject("Unknown board fields")
        board = course.setdefault("board", {"body": "", "revision": 0, "history": []})
        if isinstance(payload.get("boardRevision"), bool) or not isinstance(payload.get("boardRevision"), int) or payload.get("boardRevision") != board["revision"]: reject("Shared board changed; reload and merge your draft", "board_conflict", 409)
        body = text(payload.get("body"), "Board", 40000, empty=True)
        if len(board["history"]) >= 500: reject("Board history capacity reached", "learning_capacity", 409)
        board["history"].append({"revision": board["revision"], "body": board["body"], "replacedAt": now, "replacedBy": actor_id})
        board.update({"body": body, "revision": board["revision"] + 1, "updatedAt": now, "updatedBy": actor_id})
        result["boardRevision"] = board["revision"]
    elif kind == "credential.issue":
        if payload: reject("Credential facts come from authoritative completion evidence")
        facts = completion_evidence(state, course_id, actor_id)
        if not facts["eligible"]: reject("Complete the published course requirements before requesting a completion record", "completion_required", 409)
        existing = next((value for value in course["credentials"] if value.get("learnerId") == actor_id
                         and not value.get("revokedAt") and all(value.get(key) == facts.get(key) for key in ("evidenceDigest", "curriculumDigest", "resetGeneration"))), None)
        if existing: result.update({"credentialId": existing["id"], "existing": True})
        else:
            if len(course["credentials"]) >= 3000: reject("Completion record capacity reached", "learning_capacity", 409)
            record = {"id": ident, "courseId": course_id, "learnerId": actor_id, "learnerName": state.get("profiles", {}).get(actor_id, {}).get("displayName", actor_id),
                      "issuerId": state["courses"][course_id].get("ownerId") or state.get("ownerId"), "issuerName": "Open Clank Treehouse",
                      "title": state["courses"][course_id].get("title", course_id), "issuedAt": now,
                      "curriculumRevision": state.get("revision", 0), "recordVersion": 1, **facts}
            course["credentials"].append(record)
            result["credentialId"] = ident
    elif kind == "credential.revoke":
        if set(payload) - {"credentialId", "reason"}: reject("Unknown revocation fields")
        record = next((value for value in course["credentials"] if value["id"] == payload.get("credentialId")), None)
        if not record or (record["learnerId"] != actor_id and not can_moderate): reject("Completion record unavailable", "credential_not_found", 404)
        reason = text(payload.get("reason"), "Revocation reason", 500)
        if not can_moderate: reject("Course author capability is required to revoke a record", "forbidden", 403)
        record.update({"revokedAt": now, "revokedBy": actor_id, "revocationReason": reason})
        result["credentialId"] = record["id"]
    else: reject("Unknown learning command")
    family = {"discussion.post": "learning.discussion.posted", "discussion.react": "learning.discussion.reacted",
              "discussion.remove": "learning.discussion.removed", "board.save": "learning.board.saved",
              "credential.issue": "learning.credential.issued", "credential.revoke": "learning.credential.revoked"}[kind]
    if not result.get("existing"):
        if len(candidate.get("events", [])) >= 50000: reject("Treehouse event capacity reached", "learning_capacity", 409)
        contract = course_learning_contract(state, course_id)
        safe = {"courseId": course_id, "courseType": contract["courseType"], "completionBasis": contract["completionBasis"],
                "count": 1, "kind": kind, "revoked": kind == "credential.revoke"}
        if kind == "credential.issue":
            safe.update({"generation": facts["resetGeneration"], "verified": False, "verificationBasis": "completion-record"})
        candidate.setdefault("events", []).append({"id": "learning_event_" + digest([actor_id, command_id])[:24],
            "type": family, "actorId": actor_id, "subjectId": actor_id, "entityType": "learning",
            "entityId": result.get("credentialId") or result.get("postId") or course_id, "at": now, "data": safe})
    candidate["revision"] = expected_revision + 1
    result["revision"] = candidate["revision"]
    store.setdefault("receipts", {})[receipt_key] = {"digest": fingerprint, "result": result}
    if len(json.dumps(candidate, ensure_ascii=False).encode()) > 8_388_608: reject("Learning aggregate exceeds its storage safety limit", "learning_capacity", 409)
    return candidate, result


def render_credential_html(record: dict[str, Any]) -> str:
    e = lambda value: html.escape(str(value or ""), quote=True)
    basis = "Tutorial · content traversal" if record["courseType"] == "tutorial" else "Quest · required work"
    review = "Reviewed assessment evidence" if record.get("reviewedAssessment") else "Completion according to the course requirements"
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{e(record['title'])} — completion record</title>
<style>:root{{color-scheme:light dark}}body{{font:18px/1.6 system-ui;max-width:760px;margin:5vh auto;padding:2em;border:2px solid #4e9875;border-radius:24px}}h1{{line-height:1.2;overflow-wrap:anywhere}}small{{display:block}}.status{{font-weight:700}}@media print{{body{{margin:0;color:#111;background:white}}}}</style>
<small>OPEN CLANK TREEHOUSE · LEARNING RECORD</small><h1>{e(record['title'])}</h1><p>Issued to <strong>{e(record['learnerName'])}</strong></p><p>{e(basis)}<br>{e(review)}</p><p class="status">Status at export: {e(record['status'])}</p><p>{e(record.get('validityReason'))}</p><p>Issued {e(record['issuedAt'])}<br>Issuer: {e(record['issuerName'])} ({e(record.get('issuerId'))})</p><small>Record {e(record['id'])} · Curriculum revision {e(record['curriculumRevision'])} · Completion rule {e(record['completionRuleVersion'])} · Progress generation {e(record['resetGeneration'])}</small><p>This app learning record does not claim third-party accreditation or assessed mastery. Export is a dated snapshot; reopen in Treehouse to check current validity.</p></html>'''
