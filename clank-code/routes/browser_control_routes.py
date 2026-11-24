"""Private managed-session broker for protected Browser MCP calls."""

from __future__ import annotations

import hmac
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.openclank.browser_control_broker import browser_control_binding_token
from src.openclank.browser_control_service import BrowserActionOutcomeUnknown, BrowserControlRejected
from src.auth_helpers import effective_user, require_privilege


class _BrowserToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    arguments: dict[str, Any]
    managed_identity: dict[str, Any] | None = None


class _ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=256)
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    client_generation: int = Field(ge=1)
    mode: Literal["isolated", "extension"] = "isolated"
    profile_dir_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


class _SelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=256)
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    client_generation: int = Field(ge=1)
    tab_index: int = Field(ge=0, le=1000)
    selection_witness: str = Field(pattern=r"^[a-f0-9]{64}$")


class _CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=256)
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    client_generation: int = Field(ge=1)


class _StopTabRequest(_CloseRequest):
    target_id: str = Field(min_length=1, max_length=128)
    intent: Literal["stop", "pause_for_sign_in"] = "stop"


class _PagePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=256)
    target_id: str = Field(min_length=1, max_length=128)
    connection_generation: str = Field(min_length=1, max_length=256)
    navigation_generation: int = Field(ge=0)
    observation_generation: int = Field(ge=0)


def setup_browser_control_routes(
    *,
    dispatch: Callable[..., Any],
    auth_manager: Any,
    session_manager: Any,
    account_request_barrier: Any,
    internal_secret: str,
    trusted_loopback: Callable[[Request], bool],
    binding_resolver: Callable[..., dict] | None = None,
    policy_checker: Callable[..., bool] | None = None,
    policy_revision: Callable[..., str | None] | None = None,
    service: Any = None,
) -> APIRouter:
    router = APIRouter()
    resolve_binding = binding_resolver
    if resolve_binding is None:
        from src.openclank.transcript_projection import get_managed_binding
        resolve_binding = get_managed_binding

    def require_same_origin_mutation(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(403, "Same-origin Browser mutation required")
        parsed = urlsplit(origin)
        expected_host = str(request.headers.get("host") or "").lower()
        if (parsed.scheme != request.url.scheme or not parsed.netloc
                or parsed.netloc.lower() != expected_host):
            raise HTTPException(403, "Same-origin Browser mutation required")

    def public_binding(request: Request, session_id: str, *, require_browser: bool = True):
        owner = str(effective_user(request) or "").strip().lower()
        if not owner or not getattr(request.state, "authenticated", False) or getattr(request.state, "api_token", False):
            raise HTTPException(401, "Authentication required")
        if require_browser:
            require_privilege(request, "can_use_browser")
        try:
            account_id = auth_manager.account_id(owner)
            if account_id is None or auth_manager.is_account_lifecycle_fenced(owner):
                raise ValueError("Browser account unavailable")
            verify_owner = getattr(request.app.state, "browser_session_owner_verifier", None)
            if verify_owner is None:
                from routes.session_routes import _verify_session_owner
                verify_owner = _verify_session_owner
            verify_owner(request, session_id, session_manager)
            binding = resolve_binding(session_id, owner=owner)
            session = session_manager.get_session(session_id)
            _assert_binding(binding, session, owner, session_id,
                            str(binding.get("authorityWorkspaceID") or ""),
                            int(binding.get("workspaceRevision", -1)))
            return owner, binding, session, account_id
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(409, {"code": "BROWSER_BINDING_UNAVAILABLE", "message": "Browser chat binding is unavailable"}) from None

    def assert_public_binding(request: Request, owner: str, session_id: str, original: dict, session: Any,
                              account_id: Any, *, require_browser: bool = True) -> None:
        if require_browser:
            require_privilege(request, "can_use_browser")
        if auth_manager.is_account_lifecycle_fenced(owner) or auth_manager.account_id(owner) != account_id:
            raise HTTPException(409, {"code": "BROWSER_ACCOUNT_RETIRED", "message": "Browser account authority changed"})
        current = resolve_binding(session_id, owner=owner)
        live = session_manager.get_session(session_id)
        _assert_binding(current, live, owner, session_id,
                        str(original.get("authorityWorkspaceID") or ""),
                        int(original.get("workspaceRevision", -1)))
        if current != original:
            raise HTTPException(409, {"code": "BROWSER_BINDING_CHANGED", "message": "Browser chat changed while the request was in progress"})

    @router.get("/api/browser-control/connection")
    async def connection_state(request: Request, session_id: str, readback_only: bool = False):
        owner, binding, session, account_id = public_binding(request, session_id)
        scope = dict(owner=owner, host_session_id=session_id, workspace_id=str(binding["authorityWorkspaceID"]),
                     binding_revision=int(binding["workspaceRevision"]))
        async with account_request_barrier.admitted(owner):
            assert_public_binding(request, owner, session_id, binding, session, account_id)
            if readback_only:
                return service.connection_readback(**scope)
            try:
                result = await service.list_connection_tabs(
                    **scope, verify_access=lambda: assert_public_binding(request, owner, session_id, binding, session, account_id),
                )
            except BrowserControlRejected as exc:
                raise HTTPException(503, {"code": "BROWSER_TABS_UNAVAILABLE", "message": str(exc)}) from exc
            assert_public_binding(request, owner, session_id, binding, session, account_id)
            return result

    @router.post("/api/browser-control/connection/open")
    async def open_connection(request: Request, body: _ConnectionRequest):
        require_same_origin_mutation(request)
        owner, binding, session, account_id = public_binding(request, body.session_id)
        if service is None or not service.admit_ui_generation(owner=owner, client_id=body.client_id, generation=body.client_generation):
            raise HTTPException(409, {"code": "BROWSER_REQUEST_STALE", "message": "Browser request was superseded"})
        workspace_id = str(binding["authorityWorkspaceID"])
        try:
            async with account_request_barrier.admitted(owner):
                assert_public_binding(request, owner, body.session_id, binding, session, account_id)
                result = await service.open_isolated(
                    owner=owner, host_session_id=body.session_id, workspace_id=workspace_id,
                    binding_revision=int(binding["workspaceRevision"]), mode=body.mode,
                    profile_dir_name=body.profile_dir_name, client_id=body.client_id,
                    client_generation=body.client_generation,
                    verify_access=lambda: assert_public_binding(request, owner, body.session_id, binding, session, account_id),
                )
                try:
                    assert_public_binding(request, owner, body.session_id, binding, session, account_id)
                except HTTPException:
                    await service.close_scope(owner=owner, host_session_id=body.session_id,
                                              workspace_id=workspace_id,
                                              binding_revision=int(binding["workspaceRevision"]))
                    raise
                if not service.ui_generation_is_current(owner=owner, client_id=body.client_id,
                                                        generation=body.client_generation):
                    raise HTTPException(409, {"code": "BROWSER_REQUEST_STALE", "message": "Browser request was superseded"})
                return result
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, {"code": "BROWSER_CONNECTION_UNAVAILABLE", "message": str(exc)[:240]}) from None

    @router.post("/api/browser-control/connection/select")
    async def select_connection_tab(request: Request, body: _SelectRequest):
        require_same_origin_mutation(request)
        owner, binding, session, account_id = public_binding(request, body.session_id)
        if service is None or not service.admit_ui_generation(owner=owner, client_id=body.client_id, generation=body.client_generation):
            raise HTTPException(409, {"code": "BROWSER_REQUEST_STALE", "message": "Browser request was superseded"})
        workspace_id = str(binding["authorityWorkspaceID"])
        async with account_request_barrier.admitted(owner):
            assert_public_binding(request, owner, body.session_id, binding, session, account_id)
            try:
                result = await service.select_tab(
                    owner=owner, host_session_id=body.session_id, workspace_id=workspace_id,
                    binding_revision=int(binding["workspaceRevision"]), tab_index=body.tab_index,
                    selection_witness=body.selection_witness, client_id=body.client_id,
                    client_generation=body.client_generation,
                    verify_access=lambda: assert_public_binding(request, owner, body.session_id, binding, session, account_id),
                )
            except BrowserControlRejected as exc:
                raise HTTPException(409, {"code": "BROWSER_SELECTION_UNAVAILABLE", "message": str(exc)}) from exc
            assert_public_binding(request, owner, body.session_id, binding, session, account_id)
            if not service.ui_generation_is_current(owner=owner, client_id=body.client_id,
                                                    generation=body.client_generation):
                raise HTTPException(409, {"code": "BROWSER_REQUEST_STALE", "message": "Browser request was superseded"})
            return result

    @router.post("/api/browser-control/connection/preview-context")
    async def preview_page_context(request: Request, body: _PagePreviewRequest):
        require_same_origin_mutation(request)
        owner, binding, session, account_id = public_binding(request, body.session_id)
        def current():
            try:
                assert_public_binding(request, owner, body.session_id, binding, session, account_id)
                return True
            except (HTTPException, ValueError):
                return False

        try:
            async with account_request_barrier.admitted(owner):
                arguments = body.model_dump(exclude={"session_id"})
                return await service.call(
                    owner=owner, host_session_id=body.session_id,
                    workspace_id=str(binding["authorityWorkspaceID"]), binding_revision=int(binding["workspaceRevision"]),
                    tool_name="browser_observe", arguments={**arguments, "action": "snapshot", "arguments": {"depth": 3}},
                    binding_is_current=current, operator_preview=True,
                )
        except BrowserControlRejected as exc:
            raise HTTPException(409, {"code": "BROWSER_CONTEXT_UNAVAILABLE", "message": str(exc)}) from exc

    @router.post("/api/browser-control/connection/stop-tab")
    async def stop_connection_tab(request: Request, body: _StopTabRequest):
        require_same_origin_mutation(request)
        owner, binding, session, account_id = public_binding(request, body.session_id, require_browser=False)
        if service is None or not service.admit_ui_generation(owner=owner, client_id=body.client_id, generation=body.client_generation):
            raise HTTPException(409, {"code": "BROWSER_REQUEST_STALE", "message": "Browser request was superseded"})
        async with account_request_barrier.admitted(owner):
            assert_public_binding(request, owner, body.session_id, binding, session, account_id, require_browser=False)
            if not service.ui_generation_is_current(owner=owner, client_id=body.client_id,
                                                    generation=body.client_generation):
                raise HTTPException(409, {"code": "BROWSER_REQUEST_STALE", "message": "Browser request was superseded"})
            try:
                return service.stop_tab(owner=owner, host_session_id=body.session_id,
                                        workspace_id=str(binding["authorityWorkspaceID"]),
                                        binding_revision=int(binding["workspaceRevision"]),
                                        target_id=body.target_id, intent=body.intent)
            except BrowserControlRejected as exc:
                raise HTTPException(409, {"code": "BROWSER_TAKEOVER_UNAVAILABLE", "message": str(exc)}) from exc

    @router.post("/api/browser-control/connection/close")
    async def close_connection(request: Request, body: _CloseRequest):
        require_same_origin_mutation(request)
        owner, binding, session, account_id = public_binding(request, body.session_id, require_browser=False)
        if service is None or not service.admit_ui_generation(owner=owner, client_id=body.client_id, generation=body.client_generation):
            raise HTTPException(409, {"code": "BROWSER_REQUEST_STALE", "message": "Browser request was superseded"})
        assert_public_binding(request, owner, body.session_id, binding, session, account_id, require_browser=False)
        async with account_request_barrier.admitted(owner):
            closed = await service.close_scope(owner=owner, host_session_id=body.session_id,
                                               workspace_id=str(binding["authorityWorkspaceID"]),
                                               binding_revision=int(binding["workspaceRevision"]))
            assert_public_binding(request, owner, body.session_id, binding, session, account_id, require_browser=False)
            return {"closed": bool(closed)}

    @router.post("/api/internal/browser-control/tool")
    async def browser_tool(request: Request, body: _BrowserToolRequest):
        owner = str(request.headers.get("X-Open-Clank-Owner") or "").strip().lower()
        session_id = str(request.headers.get("X-Open-Clank-Browser-Session") or "").strip()
        workspace_id = str(request.headers.get("X-Open-Clank-Browser-Workspace") or "").strip()
        revision_header = str(request.headers.get("X-Open-Clank-Browser-Binding") or "").strip()
        capabilities_header = str(request.headers.get("X-Open-Clank-Browser-Capabilities") or "").strip()
        capabilities = tuple(part for part in capabilities_header.split(",") if part)
        token = str(request.headers.get("X-Odysseus-Internal-Token") or "")
        if (not owner or len(owner) > 128 or not session_id or len(session_id) > 256
                or not workspace_id or len(workspace_id) > 256 or not revision_header.isdigit()
                or not trusted_loopback(request)):
            raise HTTPException(status_code=403, detail="Browser broker unavailable")
        revision = int(revision_header)
        try:
            expected = browser_control_binding_token(
                internal_secret, owner=owner, host_session_id=session_id,
                authority_workspace_id=workspace_id, binding_revision=revision,
                capabilities=capabilities,
            )
        except Exception:
            raise HTTPException(status_code=403, detail="Browser broker unavailable") from None
        if not token or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=403, detail="Browser broker unavailable")
        if body.tool not in {"browser_status", "browser_observe", "browser_control"}:
            raise HTTPException(status_code=400, detail="Invalid Browser tool")
        required_capability = "control" if body.tool == "browser_control" else "observe"
        if required_capability not in capabilities:
            raise HTTPException(status_code=403, detail={
                "code": "browser_capability_not_admitted", "outcome_unknown": False,
                "message": "Browser tool is not admitted for this managed turn",
            })

        if body.managed_identity is None or service is None:
            raise HTTPException(status_code=409, detail={"code": "browser_managed_turn_missing", "outcome_unknown": False})
        expected_policy_revision = service.current_turn_policy_revision(
            owner=owner, host_session_id=session_id, workspace_id=workspace_id,
            binding_revision=revision, tool_name=body.tool, managed_identity=body.managed_identity,
        )
        if expected_policy_revision is None:
            raise HTTPException(status_code=409, detail={"code": "browser_current_turn_policy_denied", "outcome_unknown": False})

        try:
            known_owner = next((str(key).strip().lower() for key in auth_manager.users if str(key).strip().lower() == owner), None)
            if not known_owner or auth_manager.account_id(known_owner) is None:
                raise ValueError("owner inactive")
            if auth_manager.is_account_lifecycle_fenced(known_owner):
                raise ValueError("owner lifecycle transition")
            if (auth_manager.get_privileges(known_owner) or {}).get("can_use_browser") is not True:
                raise HTTPException(403, {"code": "browser_privilege_denied", "outcome_unknown": False})
            async with account_request_barrier.admitted(known_owner):
                binding = resolve_binding(session_id, owner=known_owner)
                session = session_manager.get_session(session_id)
                _assert_binding(binding, session, known_owner, session_id, workspace_id, revision)

                def binding_is_current():
                    try:
                        current = resolve_binding(session_id, owner=known_owner)
                        live = session_manager.get_session(session_id)
                        _assert_binding(current, live, known_owner, session_id, workspace_id, revision)
                        return (current == binding and live is session and
                                auth_manager.account_id(known_owner) == account_id and
                                (auth_manager.get_privileges(known_owner) or {}).get("can_use_browser") is True and
                                not auth_manager.is_account_lifecycle_fenced(known_owner))
                    except Exception:
                        return False

                identity = body.managed_identity
                account_id = auth_manager.account_id(known_owner)
                supervisor = request.app.state.mimo_supervisor
                worker = supervisor.worker_for_owner(known_owner)
                bridge = worker.bridge if worker else None
                key = bridge._managed_turn_key(identity) if bridge else None
                active = bridge._managed_actor_turns.get(key) if bridge else None
                root_session = active.get("rootSessionID") if active else None
                root = bridge._turns.get(root_session) if bridge else None
                admitted_revision = active.get("policyRevision") if active else None

                def managed_turn_is_current():
                    return bool(root and active and account_id
                                and supervisor.worker_for_owner(known_owner) is worker
                                and worker.is_alive() and worker.bridge is bridge
                                and bridge._managed_actor_turns.get(key) is active
                                and active.get("state") == "active"
                                and active.get("policyRevision") == admitted_revision
                                and active.get("owner") == known_owner and active.get("ownerAccountID") == account_id
                                and active.get("stableChatID") == session_id
                                and active.get("authorityWorkspaceID") == workspace_id
                                and active.get("workspaceRevision") == revision
                                and not active.get("authority") and not active.get("ownerAdmissionStopped")
                                and not getattr(bridge, "_ownerAdmissionHolderStopped", False)
                                and active.get("holderID") == bridge._managed_provider_generation
                                and bridge._turns.get(root_session) is root and not root.provider_root_closing
                                and root.root_operation_id == identity["rootOperationID"]
                                and root.owner_account_id == account_id)

                try:
                    if not managed_turn_is_current():
                        raise BrowserControlRejected("The parent Browser execution is unavailable")
                    try:
                        admitted = await bridge.resolve_managed_control_turn(identity, tool_name=body.tool)
                    except ValueError as exc:
                        raise BrowserControlRejected("Browser execution is no longer admitted") from exc
                    if (admitted.get("owner") != known_owner or admitted.get("ownerAccountID") != account_id
                            or not managed_turn_is_current()):
                        raise BrowserControlRejected("Browser execution is no longer admitted")
                    result = await dispatch(
                        owner=known_owner, host_session_id=session_id,
                        workspace_id=workspace_id, binding_revision=revision,
                        tool_name=body.tool, arguments=body.arguments, managed_identity=body.managed_identity,
                        control_capability="control" in capabilities, binding_is_current=binding_is_current,
                        managed_turn_is_current=managed_turn_is_current, managed_account_id=account_id,
                        **({"expected_policy_revision": expected_policy_revision}
                           if expected_policy_revision is not None else {}),
                    )
                except BrowserControlRejected as exc:
                    raise HTTPException(status_code=409, detail={
                        "code": "browser_rejected_before_dispatch", "outcome_unknown": False,
                        "message": str(exc)[:300],
                    }) from exc
                except BrowserActionOutcomeUnknown as exc:
                    raise HTTPException(status_code=502, detail={
                        "code": "browser_outcome_unknown", "outcome_unknown": True,
                        "message": str(exc)[:300],
                    }) from exc
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail={
                        "code": "browser_invalid_request", "outcome_unknown": False,
                        "message": str(exc)[:300],
                    }) from exc
                if not binding_is_current():
                    action = body.tool == "browser_control"
                    raise HTTPException(status_code=502 if action else 409, detail={
                        "code": "browser_outcome_unknown" if action else "browser_binding_changed",
                        "outcome_unknown": action,
                        "message": "Browser binding changed while the request was in progress",
                    })
                return result
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=403, detail="Browser session binding unavailable") from None

    return router


def _assert_binding(binding: dict, session: Any, owner: str, session_id: str, workspace_id: str, revision: int) -> None:
    if (not isinstance(binding, dict) or binding.get("owner") != owner
            or binding.get("stableChatID") != session_id
            or binding.get("authorityWorkspaceID") != workspace_id
            or binding.get("workspaceRevision") != revision
            or binding.get("transition") is not None or session is None
            or getattr(session, "owner", None) != owner
            or getattr(session, "workspace_id", None) != workspace_id
                    or getattr(session, "archived", False) or getattr(session, "incognito", False)):
        raise ValueError("Browser session binding unavailable")
