"""MiMo-drives-agent slice 02: agent dispatch prefers mimo.

An http-transport agent turn rewrites to ACP when the session's model
resolves to a projected endpoint provider in mimo's catalog; every
miss path (no supervisor, cold catalog, unknown model, kill switch,
already-ACP) keeps today's homegrown behavior unchanged.
"""
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
from src.model_dispatch import mimo_agent_target
from src.openclank.mimo_supervisor import ENDPOINT_PROVIDER_PREFIX


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
def endpoint():
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


def _http_target(ep_base="https://dispatch.example.test/v1", model="glm-5.2"):
    return resolve_model_target(build_chat_url(normalize_base(ep_base)), model)


def test_reverse_url_lookup(endpoint):
    chat_url = build_chat_url(normalize_base("https://dispatch.example.test/v1"))
    assert endpoint_id_for_chat_url(chat_url) == endpoint
    assert endpoint_id_for_chat_url("https://other.example.test/v1") is None
    assert endpoint_id_for_chat_url("mimo://acp") is None


async def test_rewrite_hit_returns_acp_target(endpoint):
    mimo_model = f"{ENDPOINT_PROVIDER_PREFIX}{endpoint}/glm-5.2"
    pool = _Pool(_Worker([mimo_model, "xiaomi/mimo-auto"]))
    rewritten = await mimo_agent_target(_http_target(), owner="", supervisor=pool)
    assert rewritten is not None
    assert rewritten.transport == "acp"
    assert rewritten.model_id == mimo_model


async def test_catalog_miss_keeps_homegrown(endpoint):
    pool = _Pool(_Worker(["xiaomi/mimo-auto"]))
    assert await mimo_agent_target(_http_target(), owner="", supervisor=pool) is None


async def test_unregistered_url_keeps_homegrown(endpoint):
    pool = _Pool(_Worker([f"{ENDPOINT_PROVIDER_PREFIX}{endpoint}/glm-5.2"]))
    target = resolve_model_target("https://unregistered.example.test/v1/chat/completions", "glm-5.2")
    assert await mimo_agent_target(target, owner="", supervisor=pool) is None


async def test_no_supervisor_and_acp_target_are_no_ops(endpoint):
    assert await mimo_agent_target(_http_target(), owner="", supervisor=None) is None
    acp = resolve_model_target("mimo://acp", "xiaomi/mimo-auto")
    assert await mimo_agent_target(acp, owner="", supervisor=_Pool(_Worker([]))) is None


async def test_kill_switch(monkeypatch, endpoint):
    from src import settings as settings_mod

    mimo_model = f"{ENDPOINT_PROVIDER_PREFIX}{endpoint}/glm-5.2"
    pool = _Pool(_Worker([mimo_model]))
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "agent_via_mimo" else default,
    )
    assert await mimo_agent_target(_http_target(), owner="", supervisor=pool) is None


def test_stream_with_save_reassigns_model_target_via_nonlocal():
    """The ACP rewrite reassigns model_target inside the stream_with_save
    closure. Without `nonlocal`, that assignment shadows the name as a
    closure local and every EARLIER read raises UnboundLocalError — this
    was a live 500. Pin it with real scope analysis, not grep."""
    import pathlib
    import symtable

    source = pathlib.Path("routes/chat_routes.py").read_text()
    table = symtable.symtable(source, "chat_routes.py", "exec")

    def walk(tbl):
        yield tbl
        for child in tbl.get_children():
            yield from walk(child)

    checked = 0
    for scope in walk(table):
        if scope.get_name() != "stream_with_save":
            continue
        if "model_target" not in scope.get_identifiers():
            continue
        symbol = scope.lookup("model_target")
        if symbol.is_assigned():
            checked += 1
            assert not symbol.is_local(), (
                "model_target assigned in stream_with_save without nonlocal "
                "— shadows the outer target and 500s on the first read"
            )
    assert checked >= 1, "the rewriting closure exists and was analyzed"


def test_chat_routes_wiring():
    import pathlib

    source = pathlib.Path("routes/chat_routes.py").read_text()
    agent_at = source.index("── Agent mode: full agent loop")
    assert "mimo_agent_target" in source[agent_at:agent_at + 4000], (
        "agent branch consults the ACP rewrite"
    )
    chat_at = source.index("── Chat mode: call stream_llm directly")
    assert "mimo_agent_target" not in source[chat_at:agent_at], (
        "chat lane stays on its own path"
    )
    rewrite_at = source.index("_acp_target = await mimo_agent_target")
    assert "_fallback_candidates = []" in source[rewrite_at:rewrite_at + 800], (
        "mimo owns retries when it owns the turn"
    )
