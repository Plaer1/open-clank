"""Owner-scoped connector session reads; no credential impersonation."""
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from core.database import Session, ChatMessage, get_db_session
from src.auth_helpers import require_user


@dataclass(frozen=True)
class ConnectorIdentity:
    owner: str
    account_id: str
    credential_kind: str
    scopes: frozenset[str]
    current: Callable[[], bool]


def capture_identity(request: Request, required: str) -> ConnectorIdentity:
    state = request.state
    if not getattr(state, "authenticated", False):
        raise HTTPException(401, "Authentication required")
    token = bool(getattr(state, "api_token", False))
    scopes = frozenset(getattr(state, "api_token_scopes", ()) or ())
    if token:
        if required not in scopes and not (required == "sessions:read" and "sessions:write" in scopes):
            raise HTTPException(403, "Missing session capability")
        owner = getattr(state, "api_token_owner", None)
        kind = "api"
    else:
        if getattr(state, "internal_tool_authenticated", False):
            raise HTTPException(403, "Internal credentials lack connector session capability")
        owner = require_user(request)
        kind = "cookie"
    manager = getattr(request.app.state, "auth_manager", None)
    capture = getattr(request.app.state, "capture_original_credential", None)
    try:
        account = manager.account_id(owner) if manager and owner else None
        if not account or manager.username_for_account_id(account) != owner or not callable(capture):
            raise ValueError("account unavailable")
        current = capture(request, owner, account)
        if not callable(current) or not current():
            raise ValueError("credential unavailable")
    except Exception as exc:
        raise HTTPException(401, "Credential no longer current") from exc
    return ConnectorIdentity(owner, account, kind, scopes, current)


def _current(identity: ConnectorIdentity) -> None:
    if not identity.current():
        raise HTTPException(401, "Credential no longer current")


def _session(db, owner: str, session_id: str):
    row = db.query(Session).filter(Session.id == session_id, Session.owner == owner).first()
    if row is None:
        raise HTTPException(404, "Session not found")
    return row


def _payload(row):
    return {"id": row.id, "name": row.name, "workspace_id": row.workspace_id,
            "model": row.model, "endpoint_id": row.endpoint_id,
            "revision": row.transcript_revision, "archived": bool(row.archived)}


def install_session_gateway(router: APIRouter, submit_actor_input=None, create_session=None) -> None:
    @router.get("/sessions")
    def sessions(request: Request, offset: int = 0, limit: int = 50):
        identity = capture_identity(request, "sessions:read")
        offset, limit = max(0, offset), min(100, max(1, limit))
        with get_db_session() as db:
            rows = (db.query(Session).filter(Session.owner == identity.owner, Session.archived.is_(False))
                    .order_by(Session.last_accessed.desc(), Session.id.asc()).offset(offset).limit(limit + 1).all())
            items = [_payload(row) for row in rows[:limit]]
        _current(identity)
        return {"items": items, "offset": offset, "limit": limit, "has_more": len(rows) > limit}

    @router.get("/sessions/{session_id}")
    def read_session(request: Request, session_id: str, limit: int = 50):
        identity = capture_identity(request, "sessions:read")
        limit = min(100, max(1, limit))
        with get_db_session() as db:
            row = _session(db, identity.owner, session_id)
            result = _payload(row)
            messages = (db.query(ChatMessage).filter(ChatMessage.session_id == row.id)
                        .order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc()).limit(limit + 1).all())
            result["content_trust"] = "untrusted_session_content"
            result["messages"] = [{"id": m.id, "role": m.role, "content": (m.content or "")[:16000],
                                   "content_truncated": len(m.content or "") > 16000,
                                   "timestamp": m.timestamp.isoformat() if m.timestamp else None}
                                  for m in reversed(messages[:limit])]
            result["has_more"] = len(messages) > limit
        _current(identity)
        return result

    @router.post("/sessions/{session_id}/send")
    async def send(request: Request, session_id: str, body: dict):
        identity = capture_identity(request, "sessions:write")
        from src.openclank.connector_turns import submit_connector_turn
        return await submit_connector_turn(request, session_id, body, identity)

    @router.get("/sessions/{session_id}/progress")
    async def progress(request: Request, session_id: str, map_revision: int | None = None,
                       actor_cursor: str | None = None, task_cursor: str | None = None):
        """Observe bounded current root pages without starting a managed worker."""
        import asyncio
        from src.openclank.orchestration_projection import (ProjectionError, TaskObservationScope,
            decode_task_observation_cursor, project_task_observations, resolve_binding)
        from src.openclank.session_map import OwnerSessionMap
        identity = capture_identity(request, "sessions:read")
        if ((map_revision is not None and map_revision < 0) or any(
                value is not None and (not value or len(value) > 4096) for value in (actor_cursor, task_cursor))):
            raise HTTPException(400, "Invalid progress cursor")
        def snapshot():
            _current(identity)
            auth = getattr(request.app.state, "auth_manager", None)
            if auth is None or auth.account_id(identity.owner) != identity.account_id or auth.username_for_account_id(identity.account_id) != identity.owner:
                raise HTTPException(401, "Credential no longer current")
            with get_db_session() as db:
                row = _session(db, identity.owner, session_id)
                return (row.transcript_revision, row.workspace_id, bool(row.archived))
        baseline = await asyncio.to_thread(snapshot)
        supervisor = getattr(request.app.state, "mimo_supervisor", None)
        runtime_home = getattr(supervisor, "_runtime_home", None)
        reader = getattr(supervisor, "session_observation_request", None)
        managed = {"state": "unavailable", "reason": "managed_observation_unavailable"}
        mapping = binding = scope = None
        if callable(runtime_home) and callable(reader):
            try:
                mapping = OwnerSessionMap(runtime_home(identity.owner) / "session-map.json", identity.owner)
                mapped = await asyncio.to_thread(mapping.read)
                revision = mapped.get("mapRevision") if map_revision is None else map_revision
                binding = resolve_binding(mapped, identity.owner, session_id, revision)
                scope = TaskObservationScope(identity.owner, binding, (binding.engine_session_id,), baseline[1] or None)
            except Exception:
                if map_revision is not None or actor_cursor is not None or task_cursor is not None:
                    raise HTTPException(409, "Progress scope unavailable or changed")
        if binding is not None:
            async def verify():
                if await asyncio.to_thread(snapshot) != baseline:
                    raise HTTPException(409, "Session changed during progress read")
                try:
                    now = await asyncio.to_thread(mapping.read)
                    if resolve_binding(now, identity.owner, session_id, binding.map_revision) != binding:
                        raise ValueError("binding changed")
                except Exception as exc:
                    raise HTTPException(409, "Session mapping changed during progress read") from exc
                _current(identity)
            pages = {}
            observed_worker = None
            for source, cursor in (("native_actor", actor_cursor), ("native_task", task_cursor)):
                try:
                    after = decode_task_observation_cursor(scope, source, cursor)
                except ProjectionError as exc:
                    raise HTTPException(400, "Invalid progress cursor") from exc
                await verify()
                try:
                    def live_worker():
                        lookup = getattr(supervisor, "worker_for_owner", None)
                        worker = lookup(identity.owner) if callable(lookup) else supervisor
                        bridge = getattr(worker, "_bridge", None)
                        process = getattr(worker, "_proc", None)
                        generation = getattr(worker, "installed_generation", None)
                        if (worker is None or bridge is None or process is None or not worker.is_alive()
                                or getattr(bridge, "_owner", None) != identity.owner
                                or session_id not in bridge.mapped_sessions(owner=identity.owner)
                                or bridge.mapped_session_id(session_id) != binding.engine_session_id
                                or type(generation) is not int or generation < 0):
                            raise ValueError("managed observation unavailable")
                        return worker, bridge, process, generation, bridge.mapped_session_workspace(session_id)
                    worker, bridge, process, generation, workspace = live_worker()
                    if observed_worker is None:
                        observed_worker = (worker, bridge, process, generation, workspace)
                    elif (observed_worker[0] is not worker or observed_worker[1] is not bridge
                            or observed_worker[2] is not process or observed_worker[3:] != (generation, workspace)):
                        raise ValueError("managed observation generation changed")
                    response = await reader(session_id, binding.engine_session_id,
                        "actor" if source == "native_actor" else "task", 32, after, owner=identity.owner, timeout=5.0)
                    now_worker, now_bridge, now_process, now_generation, now_workspace = live_worker()
                    if (now_worker is not worker or now_bridge is not bridge or now_process is not process
                            or now_generation != generation or now_workspace != workspace):
                        raise ValueError("managed observation changed")
                    if response.status_code != 200: raise ValueError("native observation unavailable")
                    body = response.json()
                    if (type(body) is not dict or set(body) - {"items", "observedAt", "nextCursor"}
                            or type(body.get("items")) is not list or len(body["items"]) > 32
                            or type(body.get("observedAt")) is not int or body["observedAt"] < 0
                            or (body.get("nextCursor") is not None and (type(body["nextCursor"]) is not str
                                or not body["nextCursor"] or len(body["nextCursor"]) > 512))):
                        raise ValueError("native observation invalid")
                    native_cursor = body.get("nextCursor")
                    page = {"availability": "partial" if native_cursor else "available", "items": body["items"],
                            "observedAt": body["observedAt"],
                            **({"reason": "more_pages", "nextCursor": native_cursor} if native_cursor else {})}
                    projected = project_task_observations(scope, {source: page})["sources"][source]
                    rows = []
                    for row in projected["items"]:
                        ref = row["sourceRef"]
                        if (not isinstance(ref.get("nativeId"), str) or not 0 < len(ref["nativeId"].encode("utf-8")) <= 256):
                            raise ValueError("native observation invalid")
                        entry = {"session_id": ref["sessionId"], "id": ref["nativeId"], "status": row["nativeStatus"]}
                        if source == "native_actor":
                            entry["execution_revision"] = ref["executionRevision"]
                            if "nativeOutcome" in row: entry["last_outcome"] = row["nativeOutcome"]
                        rows.append(entry)
                    pages[source] = {"availability": projected["availability"], "items": rows,
                                     "observed_at": projected["observedAt"],
                                     **({"next_cursor": projected["nextCursor"]} if projected.get("nextCursor") else {})}
                except Exception:
                    pages[source] = {"availability": "unavailable", "items": []}
                await verify()
            if observed_worker is not None:
                try:
                    final_worker = live_worker()
                    if (final_worker[0] is not observed_worker[0] or final_worker[1] is not observed_worker[1]
                            or final_worker[2] is not observed_worker[2] or final_worker[3:] != observed_worker[3:]):
                        raise ValueError("managed observation generation changed")
                except Exception:
                    pages = {source: {"availability": "unavailable", "items": []} for source in ("native_actor", "native_task")}
            managed = {"state": "partial", "coverage": "root_session_only", "map_revision": binding.map_revision,
                       "binding_revision": binding.binding_revision, "sources": pages}
        if await asyncio.to_thread(snapshot) != baseline:
            raise HTTPException(409, "Session changed during progress read")
        from src import agent_runs
        result = {"session_id": session_id, "revision": baseline[0], "coverage": "partial",
                  "managed_actor_progress": managed,
                  "legacy_run": {"status": agent_runs.get_status(session_id),
                                 "active": agent_runs.is_active(session_id),
                                 "run_id": agent_runs.get_run_id(session_id)}}
        _current(identity)
        return result

    @router.post("/sessions")
    async def create(request: Request):
        import hashlib, json, uuid
        identity = capture_identity(request, "sessions:write")
        if not callable(create_session):
            raise HTTPException(503, "Session creation unavailable")
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"operation_key", "workspace_id", "endpoint_id", "model", "name"}
                or any(not isinstance(body[k], str) or body[k] != body[k].strip() for k in body)
                or not all(body[k] for k in ("operation_key", "workspace_id", "endpoint_id", "model"))
                or len(body["operation_key"]) > 128 or any(len(body[k]) > 512 for k in body)):
            raise HTTPException(400, "Explicit operation, workspace, provider, model and name required")
        material = json.dumps([identity.account_id, body], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        digest = hashlib.sha256(material).hexdigest()
        id_bytes = hashlib.sha256(("connector-session-v1\0" + identity.account_id + "\0" + body["operation_key"]).encode()).digest()[:16]
        sid = str(uuid.UUID(bytes=id_bytes))
        _current(identity)
        request.state.connector_create = {"session_id": sid, "digest": digest, "identity": identity}
        try:
            return await create_session(request, name=body["name"], endpoint_url="", model=body["model"],
                rag=None, skip_validation=None, incognito=None, api_key="", endpoint_id=body["endpoint_id"],
                workspace_id=body["workspace_id"], response_style="normal")
        finally:
            del request.state.connector_create

    @router.post("/sessions/{session_id}/actors/{actor_id}/steer")
    async def steer(request: Request, session_id: str, actor_id: str, map_revision: int, expected_execution_revision: int):
        identity = capture_identity(request, "sessions:write")
        if not callable(submit_actor_input):
            raise HTTPException(503, "Session steering unavailable")
        if map_revision < 0 or expected_execution_revision < 1:
            raise HTTPException(400, "Invalid execution revision")
        body = await request.json()
        if not isinstance(body, dict) or body.get("mode", "steer") not in {"steer", "after-turn"} or not isinstance(body.get("requestID"), str):
            raise HTTPException(400, "Valid input mode and requestID required")
        queued = body.get("mode") == "after-turn"
        if queued:
            from routes.prefs_routes import _load_for_user
            if _load_for_user(identity.owner).get("input_queue_enabled") is not True:
                raise HTTPException(409, "Queued input is disabled")
        _current(identity)
        request.state.connector_queued_input = queued
        request.state.connector_session_identity = identity
        request.state.connector_execution_revision = expected_execution_revision
        try:
            return await submit_actor_input(request, session_id, actor_id, map_revision)
        finally:
            del request.state.connector_queued_input
            del request.state.connector_session_identity
            del request.state.connector_execution_revision
