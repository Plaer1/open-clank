"""Owner-scope tests for normalized teacher and skill-audit routes.

Both paths resolve stable catalog routes in the calling owner's scope. Provider
URLs, headers, and credentials must not cross these application seams.
"""

import asyncio
from types import SimpleNamespace

import src.teacher_escalation as teacher_escalation
import routes.skills_routes as skills_routes


def _route(model="teacher-model", *, route_id="route-teacher", grant_id=None):
    return SimpleNamespace(
        model_route_id=route_id,
        provider_grant_id=grant_id,
        provider_model_id=model,
        connection_id="connection-teacher",
        runtime_model=f"connection-teacher/{model}",
        capabilities={"tools": True},
    )


def test_call_teacher_scopes_normalized_route_to_owner(monkeypatch):
    seen = {}

    def fake_resolve_model(*, owner=None, model_spec=None):
        seen["spec"] = model_spec
        seen["route_owner"] = owner
        return _route(grant_id="grant-1")

    async def fake_complete_text(**kwargs):
        seen["completion"] = kwargs
        return "teacher reply"

    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_model_spec", fake_resolve_model)
    monkeypatch.setattr("src.openclank.modality_facade.complete_text", fake_complete_text)

    result = asyncio.run(
        teacher_escalation._call_teacher(
            "teacher-model", "prompt", owner="alice",
            root_operation_id="turn-1",
        )
    )

    assert result == "teacher reply"
    assert seen["route_owner"] == "alice"
    assert seen["spec"] == "teacher-model"
    assert seen["completion"]["owner"] == "alice"
    assert seen["completion"]["purpose"] == "utility"
    assert seen["completion"]["model_route_id"] == "route-teacher"
    assert seen["completion"]["grant_id"] == "grant-1"
    assert seen["completion"]["root_operation_id"] == "turn-1"
    assert "url" not in seen["completion"]
    assert "headers" not in seen["completion"]


def test_audit_teacher_resolution_scoped_to_owner(monkeypatch):
    seen = {}
    worker = _route(model="worker-model", route_id="route-worker")
    teacher = _route()

    def fake_get_user_setting(key, owner, default=None):
        seen.setdefault("setting_owners", []).append(owner)
        return {"teacher_enabled": True, "teacher_model": "teacher-model"}.get(key, default)

    def fake_resolve_model(*, owner=None, model_spec=None):
        seen["spec"] = model_spec
        seen["owner"] = owner
        return teacher

    monkeypatch.setattr(skills_routes, "_bound_skill_route", lambda owner, purpose: worker)
    monkeypatch.setattr("src.settings.get_user_setting", fake_get_user_setting)
    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_model_spec", fake_resolve_model)

    actual_worker, actual_teacher = skills_routes._resolve_audit_models(owner="alice")

    assert actual_worker is worker
    assert actual_teacher is teacher
    assert seen["owner"] == "alice"
    assert seen["spec"] == "teacher-model"
    assert seen["setting_owners"] == ["alice", "alice"]
