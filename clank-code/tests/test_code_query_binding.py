"""Shared query binding plumbing; fake resolver, no native or Files owner.

Real policy ownership/native evidence qualification remains in existing suites.
These checks protect the exact pre/post-await contract during helper extraction.
"""
import asyncio
from types import SimpleNamespace

import pytest

from src.openclank import code_query as cq
from src.openclank import workspace_policy_service as policy


@pytest.fixture
def scope(monkeypatch):
    workspace = SimpleNamespace(id="workspace-a", location_id="location-a",
        revision=4, owner_subject_id="immutable-alice")
    state = SimpleNamespace(workspace=workspace, path="/synthetic/authorized",
        generation=7, denied=False, resolutions=[])
    repository = SimpleNamespace(generation=lambda: state.generation)
    auth = object()

    def resolve(actual_repository, **kwargs):
        assert actual_repository is repository
        assert kwargs == dict(workspace_id="workspace-a", owner_username="alice",
            auth_manager=auth, purpose=kwargs["purpose"])
        state.resolutions.append(kwargs["purpose"])
        if state.denied:
            raise policy.WorkspacePolicyServiceError("revoked", code="denied")
        return state

    monkeypatch.setattr(policy, "resolve_owned_workspace", resolve)
    return state, dict(repository=repository, auth_manager=auth, owner="alice",
        workspace_id="workspace-a", purpose="app_folder")


def test_captured_app_binding_and_identity_are_unchanged(scope):
    state, arguments = scope
    native, identity = cq.resolve_query_binding(**arguments)
    assert native == dict(owner="alice", owner_subject_id="immutable-alice",
        workspace_id="workspace-a", location_id="location-a", workspace_revision=4,
        policy_generation=7, root="/synthetic/authorized", subscriber_id="query:app", origin="app")
    assert identity == ("workspace-a", "location-a", 4, "/synthetic/authorized", 7)
    assert state.resolutions == ["app_folder"]


@pytest.mark.parametrize("session", [None,
    SimpleNamespace(owner="bob", workspace_id="workspace-a"),
    SimpleNamespace(owner="alice", workspace_id="workspace-b"),
    SimpleNamespace(owner="alice", workspace_id="workspace-a", archived=True)])
def test_invalid_agent_binding_stops_before_policy_resolution(scope, session):
    state, arguments = scope
    with pytest.raises(cq.CodeQueryError) as error:
        cq.resolve_query_binding(**{**arguments, "purpose":"agent_workspace"}, session=session)
    assert error.value.code == "denied" and state.resolutions == []


@pytest.mark.parametrize("patch", [{"owner":""}, {"purpose":"host"}])
def test_invalid_authority_stops_before_policy_resolution(scope, patch):
    state, arguments = scope
    with pytest.raises(cq.CodeQueryError):
        cq.resolve_query_binding(**{**arguments, **patch})
    assert state.resolutions == []


def test_valid_agent_origin_and_session_subscriber_are_preserved(scope):
    state, arguments = scope
    native, _ = cq.resolve_query_binding(**{**arguments, "purpose":"agent_workspace"},
        session=SimpleNamespace(id="session-a", owner="alice", workspace_id="workspace-a"))
    assert native["origin"] == "agent" and native["subscriber_id"] == "query:session-a"
    assert state.resolutions == ["agent_workspace"]


@pytest.mark.parametrize("changed", ["id", "location_id", "revision", "path", "generation", "denied"])
@pytest.mark.parametrize("native_error", [False, True])
async def test_evidence_query_rechecks_binding_after_success_and_failure(scope, changed, native_error):
    state, arguments = scope

    async def invoke(**kwargs):
        assert kwargs["binding"]["root"] == "/synthetic/authorized"
        if changed in {"id", "location_id"}:
            setattr(state.workspace, changed, "different")
        elif changed == "revision":
            state.workspace.revision += 1
        elif changed == "path":
            state.path = "/synthetic/rebound"
        elif changed == "generation":
            state.generation += 1
        else:
            state.denied = True
        await asyncio.sleep(0)
        result = cq.error_result({}, "unavailable", "native failed")
        if not native_error:
            result.update(error=None, degraded=False, reasons=[],
                hits=[dict(id="file:abcd", kind="file", label="must-not-return")])
        result["cost"]["metadata_bytes"] = 27
        return result

    result = await cq.query_code(**arguments, provider=SimpleNamespace(code_query=invoke), query={"operation":"catalog"})
    assert result["error"]["code"] == "denied" and result["hits"] == []
    assert result["receipt"] is None and result["cost"]["metadata_bytes"] == 27
    assert state.resolutions == ["app_folder", "app_folder"]


async def test_unchanged_binding_keeps_result_and_cancellation_propagates(scope):
    state, arguments = scope
    async def invoke(**kwargs):
        result = cq.error_result({}, "missing", "no index")
        result["cost"]["metadata_bytes"] = 12
        return result
    provider = SimpleNamespace(code_query=invoke)
    result = await cq.query_code(**arguments, provider=provider, query={})
    assert result["error"]["code"] == "missing" and result["cost"]["metadata_bytes"] == 12
    assert len(state.resolutions) == 2

    async def cancelled(**kwargs):
        raise asyncio.CancelledError
    provider.code_query = cancelled
    with pytest.raises(asyncio.CancelledError):
        await cq.query_code(**arguments, provider=provider, query={})
    assert len(state.resolutions) == 3
