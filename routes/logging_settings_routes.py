"""Account-scoped Logging settings/status/advanced-detail endpoints."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Body, HTTPException, Request
from services.stats.privacy import owner_scope
from src.openclank.logging_policy import LoggingPolicyError, _STORE as POLICY
from src.openclank.logging_capture_store import CAPTURE, CaptureError

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="logging-settings")
_CAPACITY = threading.BoundedSemaphore(2)


def setup_logging_settings_routes(*, policy_store=POLICY, capture_store=CAPTURE, owner_resolver=None):
    router = APIRouter(prefix="/api/logging/v1", tags=["logging-settings"])

    def owner(request):
        if owner_resolver:
            result = owner_resolver(request)
        else:
            from routes.stats_routes import _owner
            result = _owner(request)
        if not result:
            raise HTTPException(401, "Trusted logging owner required")
        return str(result).strip().lower()

    async def work(function, *args, **kwargs):
        if not _CAPACITY.acquire(blocking=False):
            raise HTTPException(503, "Logging settings are busy")
        try:
            future = _EXECUTOR.submit(function, *args, **kwargs)
        except Exception:
            _CAPACITY.release()
            raise
        future.add_done_callback(lambda _done: _CAPACITY.release())
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), 5)
        except LoggingPolicyError as exc:
            raise HTTPException(409 if exc.code == "revision_conflict" else 422, {"error": exc.code, "message": str(exc)}) from exc
        except CaptureError as exc:
            raise HTTPException(409 if "stale" in str(exc) or "preview" in str(exc) else 404, {"error": "capture_unavailable", "message": str(exc)}) from exc
        except asyncio.TimeoutError as exc:
            raise HTTPException(504, "Logging settings deadline exceeded") from exc

    def policy_response(identity):
        return {"schema": "open-clank.logging.v1", "owner_scope": owner_scope(identity),
                "policy": policy_store.get_policy(identity), "effective": policy_store.logging_status(identity)}

    @router.get("/policy")
    async def get_policy(request: Request):
        return await work(policy_response, owner(request))

    @router.put("/policy")
    async def put_policy(request: Request, payload: dict = Body(...)):
        identity = owner(request)
        if not isinstance(payload.get("policy"), dict):
            raise HTTPException(422, "policy patch and expected_revision required")
        def update():
            for field in ("body_retention", "metadata_retention"):
                if field in payload["policy"]:
                    if not isinstance(payload["policy"][field], dict):
                        raise LoggingPolicyError("invalid_policy", "Retention must be an object")
                    if payload["policy"][field].get("mode") == "keep_until_deleted":
                        continue
                    capture_store.validate_preview(identity, field, payload["policy"][field], (payload.get("retention_previews") or {}).get(field))
            result = policy_store.update_policy(identity, payload["policy"], expected_revision=payload.get("expected_revision"))
            capture_store.maintain_retention(identity, result)
            return result
        await work(update)
        return await work(policy_response, identity)

    @router.get("/status")
    async def status(request: Request):
        identity = owner(request)
        effective = await work(policy_store.logging_status, identity)
        try:
            capture = await work(capture_store.status, identity)
        except HTTPException:
            raise
        except Exception:
            capture = {"storage_state": "unavailable", "reason": "capture_storage_unavailable"}
        return {"schema": "open-clank.logging.v1", "owner_scope": owner_scope(identity), "effective": effective, "capture": capture}

    @router.get("/attempts")
    async def attempts(request: Request, limit: int = 50, cursor: str | None = None, session: str | None = None,
                       operation: str | None = None, since: str | None = None, until: str | None = None,
                       provider_id: str | None = None, account_id: str | None = None, actual_model: str | None = None, workspace_id: str | None = None, requested_model: str | None = None):
        return await work(capture_store.attempts, owner(request), limit=limit, cursor=cursor, session=session,
                          operation=operation, since=since, until=until, provider_id=provider_id, account_id=account_id, actual_model=actual_model, workspace_id=workspace_id, requested_model=requested_model)

    @router.get("/attempts/{handle}")
    async def attempt(request: Request, handle: str, include_bodies: bool = False):
        return await work(capture_store.attempt, owner(request), handle, include_bodies=include_bodies)

    @router.post("/prune/preview")
    async def prune_preview(request: Request, payload: dict = Body(...)):
        return await work(capture_store.prune, owner=owner(request), action="preview", payload=payload)

    @router.post("/prune/apply")
    async def prune_apply(request: Request, payload: dict = Body(...)):
        return await work(capture_store.prune, owner=owner(request), action="apply", payload=payload)

    return router
