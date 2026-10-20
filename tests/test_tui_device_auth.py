from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.database import ApiToken
from routes.tui_routes import (
    DEVICE_STARTS_PER_MINUTE,
    TUI_SCOPES,
    _DeviceFlowStore,
    _message_payload,
    _normalize_requested_scopes,
    setup_tui_routes,
)
from routes import tui_routes as tui_module
from src.openclank.account_request_barrier import AccountRequestBarrier
from routes.tui_routes import DeviceTokenRequest


def test_lifecycle_invalidation_retires_old_approved_and_issuing_flows():
    store = _DeviceFlowStore(clock=lambda: 1000.0)
    approved_code, approved = store.create(
        device_label="old laptop",
        scopes=("tui:sessions",),
        remote_address="127.0.0.1",
    )
    issuing_code, issuing = store.create(
        device_label="old terminal",
        scopes=("tui:sessions",),
        remote_address="127.0.0.2",
    )
    for flow in (approved, issuing):
        store.decide(
            user_code=flow.user_code,
            approval_nonce=flow.approval_nonce,
            owner="Alice",
            approve=True,
        )
    store.begin_consume(issuing_code)

    assert store.invalidate_owners({"alice"}) == 2
    for device_code in (approved_code, issuing_code):
        with pytest.raises(HTTPException) as exc:
            store.begin_consume(device_code)
        assert exc.value.status_code == 404


def test_tui_router_exposes_owner_runtime_invalidation_hook():
    router = setup_tui_routes()
    assert callable(router.invalidate_owner_runtime)


def _exchange_endpoint(router):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/tui/v1/device/token"
    )


def _fake_request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(invalidate_token_cache=lambda: None))
    )


@pytest.mark.asyncio
async def test_exchange_rejects_deleted_owner_and_retires_old_flow():
    device_code, flow = _device_flows_for_exchange(owner="alice")
    router = setup_tui_routes(auth_manager=SimpleNamespace(users={}), request_barrier=AccountRequestBarrier(lambda _: False))

    with pytest.raises(HTTPException) as exc:
        await _exchange_endpoint(router)(_fake_request(), DeviceTokenRequest(device_code=device_code))
    assert exc.value.status_code == 410
    with pytest.raises(HTTPException) as retired:
        tui_module._device_flows.begin_consume(device_code)
    assert retired.value.status_code == 404


@pytest.mark.asyncio
async def test_exchange_rejects_durable_lifecycle_fence():
    device_code, _ = _device_flows_for_exchange(owner="alice")
    router = setup_tui_routes(
        auth_manager=SimpleNamespace(users={"alice": {}}),
        request_barrier=AccountRequestBarrier(lambda _: True),
    )

    with pytest.raises(HTTPException) as exc:
        await _exchange_endpoint(router)(_fake_request(), DeviceTokenRequest(device_code=device_code))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_exchange_holds_admission_through_token_mint(monkeypatch):
    device_code, _ = _device_flows_for_exchange(owner="alice")
    barrier = AccountRequestBarrier(lambda _: False)
    router = setup_tui_routes(
        auth_manager=SimpleNamespace(users={"alice": {}}),
        request_barrier=barrier,
    )
    started = threading.Event()
    release = threading.Event()

    def blocked_mint(_flow):
        started.set()
        assert release.wait(3)
        return "token-id", "oct_test-token", datetime.utcnow() + timedelta(days=1)

    monkeypatch.setattr(tui_module, "_mint_tui_token", blocked_mint)
    exchange = asyncio.create_task(
        _exchange_endpoint(router)(_fake_request(), DeviceTokenRequest(device_code=device_code))
    )
    await asyncio.to_thread(started.wait, 3)
    drain = asyncio.create_task(barrier.drain("alice"))
    await asyncio.sleep(0.01)
    assert not drain.done()
    release.set()
    result = await exchange
    assert result["access_token"] == "oct_test-token"
    assert await drain == {"owner": "alice", "drained": 1}


@pytest.mark.asyncio
async def test_cancelled_exchange_joins_mint_before_releasing_admission(monkeypatch):
    device_code, _ = _device_flows_for_exchange(owner="alice")
    barrier = AccountRequestBarrier(lambda _: False)
    router = setup_tui_routes(
        auth_manager=SimpleNamespace(users={"alice": {}}),
        request_barrier=barrier,
    )
    started = threading.Event()
    release = threading.Event()

    def blocked_mint(_flow):
        started.set()
        assert release.wait(3)
        return "token-id", "oct_cancelled-token", datetime.utcnow() + timedelta(days=1)

    monkeypatch.setattr(tui_module, "_mint_tui_token", blocked_mint)
    exchange = asyncio.create_task(
        _exchange_endpoint(router)(_fake_request(), DeviceTokenRequest(device_code=device_code))
    )
    await asyncio.to_thread(started.wait, 3)
    exchange.cancel()
    await asyncio.sleep(0.01)
    assert not exchange.done()
    drain = asyncio.create_task(barrier.drain("alice"))
    await asyncio.sleep(0.01)
    assert not drain.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await exchange
    assert (await drain)["drained"] == 1


def _device_flows_for_exchange(*, owner: str):
    device_code, flow = tui_module._device_flows.create(
        device_label="test",
        scopes=("tui:sessions",),
        remote_address="127.0.0.1",
    )
    tui_module._device_flows.decide(
        user_code=flow.user_code,
        approval_nonce=flow.approval_nonce,
        owner=owner,
        approve=True,
    )
    return device_code, flow


def test_device_flow_approves_and_is_consumed_once():
    now = [1000.0]
    store = _DeviceFlowStore(clock=lambda: now[0])
    device_code, flow = store.create(
        device_label="Laptop",
        scopes=("tui:sessions",),
        remote_address="127.0.0.1",
    )

    looked_up = store.by_user_code(flow.user_code.lower())
    assert looked_up.device_label == "Laptop"
    approved = store.decide(
        user_code=flow.user_code,
        approval_nonce=flow.approval_nonce,
        owner="alice",
        approve=True,
    )
    assert approved.owner == "alice"
    assert store.begin_consume(device_code) is flow
    store.finish_consume(flow, success=True)

    with pytest.raises(HTTPException) as exc:
        store.begin_consume(device_code)
    assert exc.value.status_code in {404, 409}


def test_device_flow_pending_denied_expired_and_retryable_issue():
    now = [2000.0]
    store = _DeviceFlowStore(clock=lambda: now[0])
    device_code, flow = store.create(
        device_label="Terminal",
        scopes=("tui:sessions",),
        remote_address="192.0.2.1",
    )

    with pytest.raises(HTTPException) as pending:
        store.begin_consume(device_code)
    assert pending.value.status_code == 428

    store.decide(
        user_code=flow.user_code,
        approval_nonce=flow.approval_nonce,
        owner="alice",
        approve=True,
    )
    store.begin_consume(device_code)
    store.finish_consume(flow, success=False)
    assert store.begin_consume(device_code) is flow
    store.finish_consume(flow, success=False)

    now[0] += 601
    with pytest.raises(HTTPException) as expired:
        store.begin_consume(device_code)
    assert expired.value.status_code == 410

    device_code, flow = store.create(
        device_label="Denied",
        scopes=("tui:sessions",),
        remote_address="192.0.2.1",
    )
    store.decide(
        user_code=flow.user_code,
        approval_nonce=flow.approval_nonce,
        owner="alice",
        approve=False,
    )
    with pytest.raises(HTTPException) as denied:
        store.begin_consume(device_code)
    assert denied.value.status_code == 403


def test_device_start_rate_limit_is_per_remote_address():
    now = [3000.0]
    store = _DeviceFlowStore(clock=lambda: now[0])
    for _ in range(DEVICE_STARTS_PER_MINUTE):
        store.create(
            device_label="TUI",
            scopes=("tui:sessions",),
            remote_address="198.51.100.2",
        )
    with pytest.raises(HTTPException) as limited:
        store.create(
            device_label="TUI",
            scopes=("tui:sessions",),
            remote_address="198.51.100.2",
        )
    assert limited.value.status_code == 429

    store.create(
        device_label="TUI",
        scopes=("tui:sessions",),
        remote_address="198.51.100.3",
    )
    now[0] += 61
    store.create(
        device_label="TUI",
        scopes=("tui:sessions",),
        remote_address="198.51.100.2",
    )


def test_tui_scope_normalization_rejects_non_tui_authority():
    assert set(_normalize_requested_scopes(None)) == TUI_SCOPES
    assert _normalize_requested_scopes(
        ["tui:sessions", "tui:sessions", "tui:providers"]
    ) == ("tui:sessions", "tui:providers")
    with pytest.raises(HTTPException) as unknown:
        _normalize_requested_scopes(["chat"])
    assert unknown.value.status_code == 400
    with pytest.raises(HTTPException):
        _normalize_requested_scopes([])


def test_api_token_schema_carries_tui_client_metadata():
    columns = ApiToken.__table__.columns
    assert {"client_kind", "expires_at", "revoked_at", "device_label"}.issubset(columns.keys())


def test_auth_middleware_accepts_and_confines_oct_tokens():
    source = Path("app.py").read_text(encoding="utf-8")
    assert 'auth_header.startswith(("Bearer ody_", "Bearer oct_"))' in source
    assert 'path.startswith("/api/tui/v1/")' in source
    assert 'request.state.api_token_client_kind = matched_kind' in source
    token_match = source.index("if matched_id:")
    lifecycle_fence = source.index(
        "auth_manager.is_account_lifecycle_fenced(matched_owner)",
        token_match,
    )
    token_admission = source.index(
        "return await _call_next_for_account_owner(",
        lifecycle_fence,
    )
    assert token_match < lifecycle_fence < token_admission


def test_internal_tool_impersonation_is_account_lifecycle_fenced():
    source = Path("app.py").read_text(encoding="utf-8")
    internal_branch = source.index("# In-process internal-tool token bypass")
    owner_resolution = source.index(
        "_impersonate = normalize_known_username(",
        internal_branch,
    )
    lifecycle_fence = source.index(
        "_auth_mgr.is_account_lifecycle_fenced(_impersonate)",
        owner_resolution,
    )
    owner_admission = source.index(
        "request.state.current_user = _impersonate",
        lifecycle_fence,
    )
    route_admission = source.index(
        "return await _call_next_for_account_owner(",
        owner_admission,
    )
    assert internal_branch < owner_resolution < lifecycle_fence < owner_admission < route_admission


def test_tui_public_device_endpoints_are_explicitly_auth_exempt():
    source = Path("app.py").read_text(encoding="utf-8")
    for path in (
        "/api/tui/v1/info",
        "/api/tui/v1/device/start",
        "/api/tui/v1/device/token",
    ):
        assert f'"{path}"' in source
    assert '"/api/tui/device"' not in source.split("AUTH_EXEMPT_PREFIXES", 1)[0]


def test_tui_router_exposes_control_plane_not_legacy_provider_routes():
    router = setup_tui_routes()
    paths = {route.path for route in router.routes}
    assert {
        "/api/tui/v1/info",
        "/api/tui/v1/device/start",
        "/api/tui/v1/device/token",
        "/api/tui/v1/bootstrap",
        "/api/tui/v1/sessions",
        "/api/tui/v1/sessions/{session_id}/messages",
        "/api/tui/v1/sessions/{session_id}/turns",
        "/api/tui/v1/sessions/{session_id}/turns/active/stream",
        "/api/tui/v1/sessions/{session_id}/actors",
        "/api/tui/v1/tasks",
    }.issubset(paths)
    assert not any(path.startswith("/api/mimo/") for path in paths)


def test_message_projection_prefers_ordered_content_blocks():
    from datetime import datetime
    from types import SimpleNamespace
    import json

    row = SimpleNamespace(
        id="m1",
        session_id="s1",
        role="assistant",
        content="fallback",
        timestamp=datetime(2026, 8, 9, 12, 0, 0),
        meta_data=json.dumps(
            {
                "content_blocks": [
                    {"type": "thinking", "text": "hmm"},
                    {"type": "text", "text": "answer"},
                ],
                "turn_id": "turn-1",
                "model": "provider/model",
            }
        ),
    )
    payload = _message_payload(row)
    assert [block["type"] for block in payload["blocks"]] == ["thinking", "text"]
    assert payload["turn_id"] == "turn-1"
    assert payload["model"] == "provider/model"


def test_tui_turn_idempotency_binds_owner_key_and_request():
    from routes.tui_routes import (
        TurnStartRequest,
        _turn_request_hash,
        _turn_submission_identity,
    )

    body = TurnStartRequest(message="hello", idempotency_key="a" * 16)
    assert _turn_submission_identity("alice", "a" * 16) != _turn_submission_identity(
        "bob", "a" * 16
    )
    assert _turn_request_hash("session-a", body) != _turn_request_hash(
        "session-b", body
    )


def test_tui_turn_submission_table_contains_no_provider_secret_fields():
    from core.database import TuiTurnSubmission

    columns = set(TuiTurnSubmission.__table__.columns.keys())
    assert {"owner", "session_id", "idempotency_key", "request_hash", "state"}.issubset(columns)
    assert columns.isdisjoint({"token", "credential", "headers", "api_key"})
