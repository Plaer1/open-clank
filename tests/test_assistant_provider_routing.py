"""Personal Assistant settings persist only normalized provider authority."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.assistant_routes as assistant_routes
from core.database import CrewMember, ScheduledTask, Session as DbSession
from routes.assistant_routes import AssistantSettingsUpdate, setup_assistant_routes
from src.openclank.chat_routing import ChatRouteUnavailable, MANAGED_ENGINE_PUBLIC_URL


ROOT = Path(__file__).resolve().parents[1]


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *_expressions):
        return self

    def order_by(self, *_expressions):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _DB:
    def __init__(self, crew, tasks, pinned):
        self.crew = crew
        self.tasks = tasks
        self.pinned = pinned
        self.commits = 0

    def query(self, model):
        if model is CrewMember:
            return _Query([self.crew])
        if model is ScheduledTask:
            return _Query(self.tasks)
        if model is DbSession:
            return _Query([self.pinned])
        raise AssertionError(f"unexpected query model: {model}")

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _crew():
    return SimpleNamespace(
        id="crew-1",
        owner="alice",
        name="Assistant",
        avatar=None,
        personality="Helpful",
        model="old-model",
        endpoint_url="https://legacy.invalid/v1/chat/completions",
        endpoint_id="legacy-endpoint",
        provider_model_route_id=None,
        greeting=None,
        enabled_tools="[]",
        session_id="session-1",
        is_default_assistant=True,
        timezone=None,
        updated_at=None,
    )


def _task():
    return SimpleNamespace(
        id="task-1",
        owner="alice",
        crew_member_id="crew-1",
        name="Check in",
        scheduled_time="09:00",
        prompt="Hello",
        status="active",
        next_run=None,
        last_run=None,
        run_count=0,
        endpoint_url="https://legacy.invalid/v1/chat/completions",
        endpoint_id="legacy-endpoint",
        provider_model_route_id=None,
        model="old-model",
        allowed_tools="[]",
        interaction_policy="fail_on_interaction",
        updated_at=None,
    )


def _pinned():
    return SimpleNamespace(
        id="session-1",
        endpoint_url="https://legacy.invalid/v1/chat/completions",
        endpoint_id="legacy-endpoint",
        provider_model_route_id=None,
        model="old-model",
        headers={"Authorization": "must-be-erased"},
    )


def _request(privileges=None):
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(
                    get_privileges=lambda _owner: privileges or {},
                )
            )
        ),
    )


def _settings_patch(task_scheduler=None):
    router = setup_assistant_routes(task_scheduler or SimpleNamespace())
    for route in router.routes:
        if route.path == "/api/assistant/settings" and "PATCH" in route.methods:
            return route.endpoint
    raise AssertionError("PATCH /api/assistant/settings route not found")


@pytest.mark.asyncio
async def test_route_change_updates_crew_tasks_and_pinned_session_atomically(monkeypatch):
    crew = _crew()
    task = _task()
    pinned = _pinned()
    db = _DB(crew, [task], pinned)
    route = SimpleNamespace(
        public_endpoint_id="share:grant-1",
        provider_model_id="shared-model",
        model_route_id="pmr_shared",
    )
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs)
        return route

    monkeypatch.setattr(assistant_routes, "SessionLocal", lambda: db)
    monkeypatch.setattr(assistant_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(assistant_routes, "resolve_chat_route", resolve)

    result = await _settings_patch()(
        AssistantSettingsUpdate(
            endpoint_id="share:grant-1",
            model="shared-model",
        ),
        _request(
            {
                "allowed_models_restricted": True,
                "allowed_models": ["pmr_shared"],
            }
        ),
    )

    assert calls[0] == {
        "owner": "alice",
        "endpoint_id": "share:grant-1",
        "model_id": "shared-model",
    }
    assert {
        "endpoint_id": crew.endpoint_id,
        "endpoint_url": crew.endpoint_url,
        "provider_model_route_id": crew.provider_model_route_id,
        "model": crew.model,
    } == {
        "endpoint_id": "share:grant-1",
        "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
        "provider_model_route_id": "pmr_shared",
        "model": "shared-model",
    }
    assert task.endpoint_id == crew.endpoint_id
    assert task.endpoint_url == MANAGED_ENGINE_PUBLIC_URL
    assert task.provider_model_route_id == "pmr_shared"
    assert task.model == "shared-model"
    assert pinned.endpoint_id == crew.endpoint_id
    assert pinned.endpoint_url == MANAGED_ENGINE_PUBLIC_URL
    assert pinned.provider_model_route_id == "pmr_shared"
    assert pinned.model == "shared-model"
    assert pinned.headers == {}
    assert result["crew"]["endpoint_id"] == "share:grant-1"
    assert result["crew"]["endpoint_url"] == MANAGED_ENGINE_PUBLIC_URL
    assert db.commits == 1


@pytest.mark.asyncio
async def test_route_change_enforces_model_privileges(monkeypatch):
    crew = _crew()
    db = _DB(crew, [_task()], _pinned())
    route = SimpleNamespace(
        public_endpoint_id="pcn_owned",
        provider_model_id="denied-model",
        model_route_id="pmr_denied",
    )
    monkeypatch.setattr(assistant_routes, "SessionLocal", lambda: db)
    monkeypatch.setattr(assistant_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(assistant_routes, "resolve_chat_route", lambda **_kwargs: route)

    with pytest.raises(HTTPException) as exc:
        await _settings_patch()(
            AssistantSettingsUpdate(endpoint_id="pcn_owned", model="denied-model"),
            _request(
                {
                    "allowed_models_restricted": True,
                    "allowed_models": ["allowed-model"],
                }
            ),
        )

    assert exc.value.status_code == 403
    assert crew.provider_model_route_id is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_route_change_rejects_raw_endpoint_url(monkeypatch):
    db = _DB(_crew(), [_task()], _pinned())
    monkeypatch.setattr(assistant_routes, "SessionLocal", lambda: db)
    monkeypatch.setattr(assistant_routes, "get_current_user", lambda _request: "alice")

    with pytest.raises(HTTPException) as exc:
        await _settings_patch()(
            AssistantSettingsUpdate(
                endpoint_id="pcn_owned",
                model="model-1",
                endpoint_url="https://attacker.invalid/v1/chat/completions",
            ),
            _request(),
        )

    assert exc.value.status_code == 400
    assert "do not accept provider URLs" in exc.value.detail
    assert db.commits == 0


def test_stale_route_never_reexposes_legacy_crew_url(monkeypatch):
    crew = _crew()
    crew.provider_model_route_id = "pmr_revoked"
    monkeypatch.setattr(
        assistant_routes,
        "resolve_chat_route",
        lambda **_kwargs: (_ for _ in ()).throw(ChatRouteUnavailable("revoked")),
    )

    result = assistant_routes._crew_to_dict(crew)

    assert result["endpoint_url"] is None
    assert result["endpoint_id"] is None
    assert "legacy.invalid" not in repr(result)


def test_assistant_surfaces_have_no_legacy_provider_authority():
    route_source = (ROOT / "routes" / "assistant_routes.py").read_text(encoding="utf-8")
    ui_source = (ROOT / "static" / "js" / "assistant.js").read_text(encoding="utf-8")

    assert "ModelEndpoint" not in route_source
    assert "endpoint_resolver" not in route_source
    assert "provider_model_route_id" in route_source
    assert "/api/model-endpoints" not in ui_source
    assert "_fetchJSON('/api/models')" in ui_source
