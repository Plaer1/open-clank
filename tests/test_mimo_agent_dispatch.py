"""Strict Agent dispatch resolves exact persisted endpoint identity to MiMo."""
import json
import uuid

import pytest

from core.database import ModelEndpoint
from src import endpoint_resolver
from src.endpoint_resolver import (
    build_chat_url,
    endpoint_id_for_chat_url,
    normalize_base,
    resolve_model_target,
)

# Write through the SAME session binding the resolver reads. Another test
# module (test_manage_tasks_owner_scope) rebinds core.database.SessionLocal
# to a temp DB at import time; endpoint_resolver captured the original at
# ITS import, so fixtures must use the resolver's binding or rows land in
# a database the code under test never sees.
SessionLocal = endpoint_resolver.SessionLocal
from src.model_dispatch import AgentRunRequest, mimo_agent_target, run_agent
from src.model_capabilities import set_declared
from src.openclank.mimo_supervisor import ENDPOINT_PROVIDER_PREFIX, SupervisorAdmissionError


class _Worker:
    def __init__(self, model_ids):
        self._models = [{"modelId": mid, "name": mid} for mid in model_ids]
        self.bridge = object()

    def is_alive(self):
        return True

    def available_models(self, owner=None):
        return list(self._models)


class _Pool:
    def __init__(self, worker):
        self._worker = worker

    async def for_owner(self, owner):
        return self._worker


@pytest.fixture
def endpoint(monkeypatch):
    # A legacy owner-scope module rebinds core.database.SessionLocal at import
    # time. Keep dispatch and endpoint_resolver on the same canonical DB for
    # this test, then let monkeypatch restore the suite's prior binding.
    monkeypatch.setattr("core.database.SessionLocal", SessionLocal)
    ep_id = f"disp-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        db.add(ModelEndpoint(
            id=ep_id,
            name="Dispatch Test",
            base_url="https://dispatch.example.test/v1",
            api_key=None,
            is_enabled=True,
            cached_models=json.dumps(["glm-5.2"]),
        ))
        endpoint_row = db.get(ModelEndpoint, ep_id)
        if endpoint_row is None:
            db.flush()
            endpoint_row = db.get(ModelEndpoint, ep_id)
        set_declared(db, endpoint_row, "glm-5.2", True)
        db.commit()
    finally:
        db.close()
    yield ep_id
    db = SessionLocal()
    try:
        db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).delete()
        db.commit()
    finally:
        db.close()


def _http_target(endpoint_id=None, ep_base="https://dispatch.example.test/v1", model="glm-5.2"):
    return resolve_model_target(
        build_chat_url(normalize_base(ep_base)), model, endpoint_id=endpoint_id,
    )


def test_reverse_url_lookup(endpoint):
    chat_url = build_chat_url(normalize_base("https://dispatch.example.test/v1"))
    assert endpoint_id_for_chat_url(chat_url) == endpoint
    assert endpoint_id_for_chat_url("https://other.example.test/v1") is None
    assert endpoint_id_for_chat_url("mimo://acp") is None


async def test_rewrite_hit_returns_acp_target(endpoint):
    mimo_model = f"{ENDPOINT_PROVIDER_PREFIX}{endpoint}/glm-5.2"
    pool = _Pool(_Worker([mimo_model, "xiaomi/mimo-auto"]))
    rewritten = await mimo_agent_target(_http_target(endpoint), owner="", supervisor=pool)
    assert rewritten is not None
    assert rewritten.transport == "acp"
    assert rewritten.model_id == mimo_model


async def test_unrelated_native_catalog_entries_do_not_block_rewrite(endpoint):
    pool = _Pool(_Worker(["xiaomi/mimo-auto"]))
    rewritten = await mimo_agent_target(_http_target(endpoint), owner="", supervisor=pool)
    assert rewritten.model_id == f"{ENDPOINT_PROVIDER_PREFIX}{endpoint}/glm-5.2"


async def test_missing_endpoint_identity_fails_closed(endpoint):
    pool = _Pool(_Worker([f"{ENDPOINT_PROVIDER_PREFIX}{endpoint}/glm-5.2"]))
    target = resolve_model_target("https://unregistered.example.test/v1/chat/completions", "glm-5.2")
    with pytest.raises(SupervisorAdmissionError, match="persisted endpoint identity"):
        await mimo_agent_target(target, owner="", supervisor=pool)


async def test_no_supervisor_fails_and_acp_target_is_stable(monkeypatch, endpoint):
    monkeypatch.setattr("src.model_dispatch._mimo_supervisor", None)
    with pytest.raises(SupervisorAdmissionError, match="unavailable"):
        await mimo_agent_target(_http_target(endpoint), owner="", supervisor=None)
    acp = resolve_model_target("mimo://acp", "xiaomi/mimo-auto")
    assert await mimo_agent_target(acp, owner="", supervisor=_Pool(_Worker([]))) is acp


async def test_removed_kill_switch_cannot_restore_legacy_agent(monkeypatch, endpoint):
    mimo_model = f"{ENDPOINT_PROVIDER_PREFIX}{endpoint}/glm-5.2"
    pool = _Pool(_Worker([mimo_model]))
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "agent_via_mimo" else default,
    )
    rewritten = await mimo_agent_target(_http_target(endpoint), owner="", supervisor=pool)
    assert rewritten.transport == "acp"


async def test_unprobed_defaults_on_and_explicit_off_runs_without_tools(endpoint):
    db = SessionLocal()
    try:
        row = db.get(ModelEndpoint, endpoint)
        set_declared(db, row, "glm-5.2", None)
        db.commit()
    finally:
        db.close()

    pool = _Pool(_Worker([]))
    default_on = await mimo_agent_target(_http_target(endpoint), owner="", supervisor=pool)
    assert default_on.capabilities["tools"] is True

    db = SessionLocal()
    try:
        row = db.get(ModelEndpoint, endpoint)
        set_declared(db, row, "glm-5.2", False)
        db.commit()
    finally:
        db.close()

    class Bridge:
        def __init__(self):
            self.envelopes = []

        async def run_turn(self, *_args, turn_envelope=None, **_kwargs):
            self.envelopes.append(turn_envelope)
            yield "data: [DONE]\n\n"

    class Lease:
        generation = 1
        fingerprint = "test-fingerprint"
        projection_pending = False

        def __init__(self):
            self.worker = _Worker([])
            self.worker.bridge = Bridge()
            self.released = False

        async def release(self, *, successful_terminal=False):
            self.released = successful_terminal

    lease = Lease()

    async def admit_agent(*_args, **_kwargs):
        return lease

    pool.admit_agent = admit_agent
    request = AgentRunRequest(
        target=_http_target(endpoint),
        messages=[{"role": "user", "content": "test"}],
        session_id="tools-off",
        owner="",
        supervisor=pool,
    )
    events = [event async for event in run_agent(request)]
    assert events[-1] == "data: [DONE]\n\n"
    assert lease.worker.bridge.envelopes[0]["lane"] == "agent"
    assert lease.worker.bridge.envelopes[0]["allowed_tools"] == []
    assert lease.released is True


def test_chat_routes_wiring():
    import pathlib

    source = pathlib.Path("routes/chat_routes.py").read_text()
    agent_at = source.index("── Agent mode: full agent loop")
    assert "stream_agent_target(" in source[agent_at:agent_at + 4000]
    assert "Chat mode was removed" in source
    assert "stream_chat_target(" not in source
    assert "_acp_target = await mimo_agent_target" not in source
    stream_at = source.index("_chunk_source = stream_agent_target", agent_at)
    assert "_fallback_candidates = []" in source[agent_at:stream_at], (
        "mimo owns retries when it owns the turn"
    )
