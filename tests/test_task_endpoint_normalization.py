"""Scheduled tasks resolve normalized routes, never provider URLs."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.task_scheduler import _resolve_managed_task_route


ROOT = Path(__file__).resolve().parents[1]


class _Store:
    def __init__(self):
        self.route = SimpleNamespace(
            id="pmr_tasks",
            connection_id="pcn_tasks",
            provider_model_id="model-1",
            operations=["chat.stream", "chat.complete"],
            capabilities={"tools": True},
            enabled=True,
            deleted_at=None,
        )
        self.connection = SimpleNamespace(
            id="pcn_tasks",
            enabled=True,
            deleted_at=None,
        )

    def get_route_bindings_for_purpose(self, *, owner, purpose):
        assert owner == "alice"
        assert purpose == "tasks"
        return [SimpleNamespace(model_route_id=self.route.id, enabled=True)]

    def get_model_route(self, *, owner, model_route_id):
        assert owner == "alice"
        assert model_route_id == self.route.id
        return self.route

    def get_connection(self, *, owner, connection_id):
        assert owner == "alice"
        assert connection_id == self.connection.id
        return self.connection


def _task(**values):
    fields = dict(
        owner="alice",
        endpoint_url="https://legacy.invalid/v1",
        endpoint_id="legacy-endpoint",
        provider_model_route_id=None,
    )
    fields.update(values)
    return SimpleNamespace(**fields)


def test_owner_tasks_binding_replaces_legacy_endpoint_url(monkeypatch):
    store = _Store()
    monkeypatch.setattr("src.openclank.provider_store.ProviderStore", lambda: store)

    route = _resolve_managed_task_route(_task())

    assert route.model_route_id == "pmr_tasks"
    assert route.runtime_model == "pcn_tasks/model-1"
    assert route.public_endpoint_id == "pcn_tasks"


def test_persisted_model_route_stays_pinned(monkeypatch):
    store = _Store()
    monkeypatch.setattr("src.openclank.provider_store.ProviderStore", lambda: store)

    route = _resolve_managed_task_route(
        _task(provider_model_route_id="pmr_tasks")
    )

    assert route.model_route_id == "pmr_tasks"
    assert route.capabilities == {"tools": True}


def test_tool_agent_requires_stream_capability(monkeypatch):
    store = _Store()
    store.route.operations = ["chat.complete"]
    monkeypatch.setattr("src.openclank.provider_store.ProviderStore", lambda: store)

    with pytest.raises(RuntimeError, match="chat.stream"):
        _resolve_managed_task_route(
            _task(provider_model_route_id="pmr_tasks")
        )


@pytest.mark.asyncio
async def test_shared_task_completion_ignores_legacy_transport(monkeypatch):
    from src import task_endpoint
    from src.openclank import modality_facade

    captured = {}

    async def complete_text(**kwargs):
        captured.update(kwargs)
        return "done"

    async def ready(_label):
        return None

    monkeypatch.setattr(modality_facade, "complete_text", complete_text)
    monkeypatch.setattr(task_endpoint, "wait_for_interactive_quiet", ready)

    result = await task_endpoint.task_complete_text(
        [{"role": "user", "content": "work"}],
        fallback_url="https://legacy.invalid/v1",
        fallback_model="legacy-model",
        fallback_headers={"Authorization": "must-not-cross"},
        owner="alice",
        model_route_id="pmr_tasks",
        root_operation_id="task-utility:test",
        temperature=0.2,
        max_tokens=64,
        timeout=10,
    )

    assert result == "done"
    assert captured == {
        "owner": "alice",
        "messages": [{"role": "user", "content": "work"}],
        "purpose": "tasks",
        "model_route_id": "pmr_tasks",
        "grant_id": None,
        "root_operation_id": "task-utility:test",
        "temperature": 0.2,
        "max_output_tokens": 64,
    }


def test_legacy_task_endpoint_resolvers_are_retired():
    source = (ROOT / "src" / "task_endpoint.py").read_text(encoding="utf-8")

    assert "def resolve_task_endpoint(" not in source
    assert "def resolve_task_candidates(" not in source
    assert "resolve_endpoint" not in source
    assert "async def task_complete_text(" in source


def test_email_poller_uses_one_managed_route_and_root_for_its_pass():
    source = (ROOT / "routes" / "email_pollers.py").read_text(encoding="utf-8")

    assert "managed_route_summary" in source
    assert 'purpose="tasks"' in source
    assert 'operation="chat.complete"' in source
    assert "model_route_id=model_route_id" in source
    assert "root_operation_id=root_operation_id" in source
    assert "resolve_task_candidates" not in source
    assert "fallback_url=" not in source
    assert "fallback_headers=" not in source
