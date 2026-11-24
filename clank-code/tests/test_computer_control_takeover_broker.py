"""Token-bound stop-only broker behavior with a synthetic Host; no desktop access."""
import contextlib
import types
import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_expired_turn_can_only_revoke_with_its_exact_token_and_holder(monkeypatch):
    import app
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_OWNER_HEADER, INTERNAL_TOOL_WORKSPACE_HEADER
    from src.openclank.acp_bridge import memery_broker_token
    from src.openclank.computer_control import hoststate

    class Bridge:
        async def resolve_managed_control_turn(self, *args):
            raise ValueError('turn is no longer active')
    bridge = Bridge(); worker = types.SimpleNamespace(bridge=bridge, is_alive=lambda: True)
    current = [worker]
    supervisor = types.SimpleNamespace(worker_for_owner=lambda owner: current[0])
    request = types.SimpleNamespace(headers={INTERNAL_TOOL_HEADER: memery_broker_token(app.INTERNAL_TOOL_TOKEN, 'alice', 'ws'),
        INTERNAL_TOOL_OWNER_HEADER: 'alice', INTERNAL_TOOL_WORKSPACE_HEADER: 'ws'},
        app=types.SimpleNamespace(state=types.SimpleNamespace(mimo_supervisor=supervisor)))
    @contextlib.asynccontextmanager
    async def admitted(owner):
        yield
    monkeypatch.setattr(app, '_account_request_barrier', types.SimpleNamespace(admitted=admitted))
    monkeypatch.setattr(app, '_is_trusted_loopback', lambda request: True)
    monkeypatch.setattr(app, '_owner_request_fenced', lambda owner: False)
    monkeypatch.setattr(app, 'auth_manager', types.SimpleNamespace(users={'alice': {}}, account_id=lambda owner: 'acct', username_for_account_id=lambda account: 'alice'))
    state = hoststate.ControlState(); monkeypatch.setattr(hoststate, 'STATE', state)
    monkeypatch.setattr(hoststate, '_still', lambda pid, entry: False)
    identity = {'sessionID': 's', 'rootOperationID': 'r', 'actorID': 'a', 'executionRevision': 4, 'innerTurnID': 'i'}
    scope = ('alice', 's', 'a', 'acct', 'r', 4, 'i')
    state.helpers[99999] = {'kind': 'control', 'registration': 'secret', 'scope': scope, 'epoch': 0, 'holder': worker}
    async def call(op, token):
        return await app.internal_memery_tool(request, {'name': 'control_lease', 'arguments': {'identity': identity, 'request': {'op': op, 'registration': token}}})
    bad = await call('helper_takeover', 'wrong')
    assert bad['accepted'] is False and state.epoch == 0
    current[0] = types.SimpleNamespace(bridge=bridge, is_alive=lambda: True)
    wrong_holder = await call('helper_takeover', 'secret')
    assert wrong_holder['accepted'] is False and state.epoch == 0
    current[0] = worker
    with pytest.raises(HTTPException) as failure:
        await call('helper_capabilities', 'secret')
    assert failure.value.status_code == 409
    result = await call('helper_takeover', 'secret')
    assert result['accepted'] is True and state.epoch == 1
    assert not (await call('helper_takeover', 'secret'))['accepted']
