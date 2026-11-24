"""Hidden, data-only view over an already verified task-observation page.

No reads, subscriptions, model calls or second identity/state authority. Call via
orchestration_projection.project_task_observations so scope is validated first.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

_TOKEN = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")
_TASK_STATES = {"open": "waiting", "in_progress": "working", "blocked": "waiting",
                "done": "idle", "abandoned": "idle"}


def _token(value: Any) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def validate_achievement_ids(values: Any) -> list[str]:
    from src.openclank.treehouse_achievements import BY_ID
    if (type(values) is not list or len(values) > 64
            or any(type(value) is not str or value not in BY_ID for value in values)
            or values != sorted(set(values))):
        raise ValueError("invalid achievement references")
    return values


def project_companion(scope: Any, observation: dict[str, Any], *, connection: str = "unknown", questions: list[dict[str, Any]] | None = None, achievement_ids: list[str] | None = None) -> dict[str, Any]:
    """Derive scoped states only from the validated observation projection.

    This is a page, not a complete account-wide inventory. Free-form content and
    contextual paths are intentionally absent; unavailable identifiers are null.
    """
    if connection not in {"connected", "disconnected", "unknown"}:
        raise ValueError("invalid companion connection state")
    page = observation["sources"]["native_task"]
    tasks = []
    for item in page["items"]:
        ref = item["sourceRef"]
        native_id, session_id = _token(ref.get("nativeId")), _token(ref.get("sessionId"))
        if not native_id or not session_id:
            continue
        actor = None
        for link in item.get("links", []):
            target = link.get("target", {})
            if link.get("kind") == "assigned_to" and target.get("kind") == "native_actor":
                actor_id = _token(target.get("nativeId"))
                actor_session = _token(target.get("sessionId"))
                if actor_id and actor_session == session_id:
                    actor = {"kind": "native_actor", "sessionId": actor_session, "nativeId": actor_id}
                break
        state = _TASK_STATES.get(item.get("nativeStatus"), "unknown")
        tasks.append({"sourceRef": {"kind": "native_task", "sessionId": session_id, "nativeId": native_id},
                      "actorRef": actor, "requestRef": None,
                      "state": "disconnected" if connection == "disconnected" else state,
                      "observedState": state, "sourceTime": item.get("sourceWatermark"),
                      "observedAt": item.get("observedAt")})
    actor_page = observation["sources"]["native_actor"]
    actors = []
    valid_actors = set()
    for item in actor_page["items"]:
        ref = item["sourceRef"]
        native_id, session_id = _token(ref.get("nativeId")), _token(ref.get("sessionId"))
        revision = ref.get("executionRevision")
        if not native_id or not session_id or type(revision) is not int or revision < 0: continue
        valid_actors.add((session_id, native_id))
        status = item.get("nativeStatus")
        observed = "error" if item.get("nativeOutcome") == "failure" else {"running":"working","pending":"waiting","idle":"idle"}.get(status,"unknown")
        actors.append({"sourceRef":{"kind":"native_actor","sessionId":session_id,"nativeId":native_id,"executionRevision":revision},
                       "state":"disconnected" if connection=="disconnected" else observed,"observedState":observed,"observedAt":item.get("observedAt")})
    requests = []
    if questions is not None:
        if type(questions) is not list or len(questions)>64: raise ValueError("invalid companion questions")
        for item in questions:
            fields={"sessionID","actorID","requestID","questionRef","questionRevision","originTurnID","state","observedAt"}
            if type(item) is not dict or set(item)!=fields: raise ValueError("invalid companion question")
            session_id,actor_id,request_id=(_token(item[k]) for k in ("sessionID","actorID","requestID"))
            question_revision=item["questionRevision"]; origin=item["originTurnID"]
            if (not session_id or not actor_id or not request_id or (session_id,actor_id) not in valid_actors
                or type(question_revision) is not int or not 1<=question_revision<=9007199254740991
                or item["questionRef"]!=f"{request_id}@{question_revision}"
                or (origin is not None and not _token(origin)) or type(item["observedAt"]) is not int or item["observedAt"]<0
                or item["state"] not in {"pending","answer-queued","stale"}): raise ValueError("invalid companion question")
            observed={"pending":"inputneeded","answer-queued":"waiting","stale":"unknown"}[item["state"]]
            requests.append({"requestRef":{"kind":"native_question","sessionId":session_id,"actorId":actor_id,"requestID":request_id,"questionRef":item["questionRef"],"revision":question_revision,
                                           **({"originTurnID":origin} if origin is not None else {})},
                             "actorRef":{"kind":"native_actor","sessionId":session_id,"nativeId":actor_id},
                             "state":"disconnected" if connection=="disconnected" else observed,"observedState":observed,"observedAt":item["observedAt"]})
    achievements = {"status": "unavailable", "refs": []}
    if achievement_ids is not None:
        validate_achievement_ids(achievement_ids)
        achievements = {"status": "available", "refs": [{"kind": "treehouse_achievement", "achievementId": value} for value in achievement_ids]}
    counts = {state: sum(task["state"] == state for task in tasks) for state in
              ("working", "waiting", "inputneeded", "idle", "disconnected", "error", "unknown")}
    return {"version": 1, "collective": {"kind": "user_clank", "ownerKey": hashlib.sha256(scope.owner.encode()).hexdigest()},
            "mascotRef": "static/icons/treehouse-achievements/kit/mascot.svg",
            "scope": {"kind": "verified_task_observation_page", "complete": False},
            "source": {"kind": "native_task", "availability": page["availability"],
                       "connection": connection, "observedAt": page.get("observedAt"),
                       "mapRevision": scope.binding.map_revision, "bindingRevision": scope.binding.binding_revision},
            "tasks": tasks, "actors":actors, "requests":{"availability":"partial" if questions is not None else "unavailable","items":requests}, "summary": {"taskCounts": counts},
            "extensions": {"interests": {"status": "unavailable", "refs": []},
                           "achievements": achievements}}
