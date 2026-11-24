"""Human Stop for desktop control (pointer/keyboard and accessibility sessions).

A signed-in human only: no API token, no internal-tool identity, no model, no approval. Stop is idempotent and always ends
everything that is running; the status route tells the UI whether a session exists so it can show its "Stop control" button.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import is_internal_tool_identity, require_user


def _human_admin(request: Request) -> str:
    user = require_user(request)                      # 401 unauthenticated, 403 for API tokens
    if is_internal_tool_identity(user):
        raise HTTPException(403, "Desktop control can only be stopped by a signed-in person")
    manager = getattr(request.app.state, "auth_manager", None)
    if manager is None or not manager.is_admin(user):
        raise HTTPException(403, "Desktop control is only available to administrators")
    return user


def _log_on(user: str) -> bool:
    from src.settings import get_user_setting
    return bool(get_user_setting("desktop_action_log_enabled", user, False))


def setup_desktop_control_routes() -> APIRouter:
    router = APIRouter(prefix="/api/desktop-control", tags=["desktop-control"])

    @router.get("/status")
    async def status(request: Request, session_id: str = "", include_recording: bool = True):
        user = _human_admin(request)
        from routes.session_routes import _verify_session_owner
        from src.openclank.computer_control.hoststate import STATE
        from src.settings import get_user_setting
        result = {"session_active": False, "accessibility_active": False, "busy": False,
                  "activity": [], "takeoverScope": "host", "recording_target_available": False}
        if not session_id or len(session_id) > 256:
            return result
        _verify_session_owner(request, session_id)
        auth = request.app.state.auth_manager
        account = auth.account_id(user)
        native_session = STATE.native_session_for_chat(user, session_id)
        if not account or auth.username_for_account_id(account) != user or not native_session:
            return result
        result.update(STATE.scoped_helper_status(user, account, native_session))
        if (include_recording and get_user_setting('desktop_control_recording_enabled',user,False) is True
                and get_user_setting('desktop_capture_enabled',user,False) is True):
            result['recording_target_available'] = STATE.recording_stream_binding(user,account,session_id) is not None
            if result['recording_target_available']:
                import os, signal
                result['recording_target_available'] = all(hasattr(module, name) for module, name in ((os,'pidfd_open'),(os,'P_PIDFD'),(signal,'pidfd_send_signal')))
                if result['recording_target_available']:
                    try:
                        probe=os.pidfd_open(os.getpid(),0); os.close(probe)
                    except OSError: result['recording_target_available'] = False
            if result['recording_target_available']:
                import os
                from src.openclank.recording_child import control_recording_readiness
                helper = await control_recording_readiness()
                if helper is not None: os.close(helper['fd'])
                result['recording_target_available'] = bool(helper is not None and STATE.recording_stream_binding(user,account,session_id) is not None
                    and get_user_setting('desktop_control_recording_enabled',user,False) is True and get_user_setting('desktop_capture_enabled',user,False) is True)
        if (auth.account_id(user) != account or auth.is_account_lifecycle_fenced(user)
                or STATE.native_session_for_chat(user, session_id) != native_session):
            raise HTTPException(409, "Desktop control scope changed")
        return result

    @router.get("/android-sdk")
    async def android_sdk(request: Request):
        import asyncio
        from fastapi.responses import JSONResponse
        from src.openclank.android_sdk import android_sdk_status

        owner = _human_admin(request)
        auth = request.app.state.auth_manager
        account = auth.account_id(owner)
        if not account or auth.is_account_lifecycle_fenced(owner):
            raise HTTPException(409, "Android SDK status unavailable")
        try:
            status = await asyncio.wait_for(asyncio.to_thread(android_sdk_status), timeout=2)
        except TimeoutError as exc:
            raise HTTPException(503, "Android SDK status unavailable") from exc
        if (_human_admin(request) != owner or auth.account_id(owner) != account
                or auth.username_for_account_id(account) != owner or auth.is_account_lifecycle_fenced(owner)):
            raise HTTPException(409, "Android SDK account changed")
        return JSONResponse(status, headers={"Cache-Control": "private, no-store"})

    @router.post("/stop")
    async def stop(request: Request):
        user = _human_admin(request)
        from src.openclank.computer_control.linux import take_over
        from src.openclank.computer_control import action_log
        out = await take_over(owner=user)
        action_log.record_event("human_stop", _log_on(user))
        return {**out, "takeoverScope": "host"}

    @router.get("/actions")
    async def actions(request: Request, limit: int = 200):
        user = _human_admin(request)
        from src.openclank.computer_control import action_log
        return {"enabled": _log_on(user), "lines": action_log.read(limit)}

    @router.delete("/actions")
    async def clear_actions(request: Request):
        _human_admin(request)
        from src.openclank.computer_control import action_log
        action_log.clear()
        return {"cleared": True}

    return router
