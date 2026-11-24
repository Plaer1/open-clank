"""Normalized-provider regressions for ``POST /api/compare/start``."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL


class _FakeDB:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)

    def commit(self):
        pass

    def close(self):
        pass


def _request(*, allowed_models=None):
    privileges = {}
    if allowed_models is not None:
        privileges = {
            "allowed_models_restricted": True,
            "allowed_models": allowed_models,
        }
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(
                    get_privileges=lambda _owner: privileges,
                )
            )
        ),
    )


def _route(endpoint_id, model_id):
    return SimpleNamespace(
        public_endpoint_id=endpoint_id,
        provider_model_id=model_id,
        model_route_id=f"pmr-{endpoint_id}-{model_id}",
    )


def _start(session_manager):
    from routes.compare_routes import setup_compare_routes

    router = setup_compare_routes(session_manager)
    return [
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/compare/start"
    ][-1]


@pytest.mark.asyncio
async def test_raw_urls_are_never_execution_authority(monkeypatch):
    import routes.compare_routes as compare

    called = False

    def _resolve(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(compare, "resolve_chat_route", _resolve)
    start = _start(SimpleNamespace(create_session=lambda **_: None))
    with pytest.raises(HTTPException) as exc:
        await start(
            _request(),
            prompt="p",
            model_a="a",
            model_b="b",
            endpoint_a="https://provider-a.example/v1",
            endpoint_b="https://provider-b.example/v1",
            endpoint_a_id="",
            endpoint_b_id="",
        )

    assert exc.value.status_code == 422
    assert called is False


@pytest.mark.asyncio
async def test_compare_sessions_are_managed_and_secret_free(monkeypatch):
    import routes.compare_routes as compare

    db = _FakeDB()
    monkeypatch.setattr(compare, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        compare,
        "resolve_chat_route",
        lambda *, endpoint_id, model_id, **_: _route(endpoint_id, model_id),
    )
    created = []
    start = _start(SimpleNamespace(create_session=lambda **kw: created.append(kw)))

    await start(
        _request(),
        prompt="p",
        model_a="model-a",
        model_b="model-b",
        endpoint_a="https://ignored.example/a",
        endpoint_b="https://ignored.example/b",
        endpoint_a_id="conn-a",
        endpoint_b_id="share:grant-b",
        is_blind="false",
    )

    assert len(created) == 2
    assert {row["endpoint_url"] for row in created} == {MANAGED_ENGINE_PUBLIC_URL}
    assert {row["endpoint_id"] for row in created} == {"conn-a", "share:grant-b"}
    assert {row["provider_model_route_id"] for row in created} == {
        "pmr-conn-a-model-a",
        "pmr-share:grant-b-model-b",
    }
    assert all("headers" not in row for row in created)
    comparison = db.added[-1]
    assert comparison.endpoint_a == MANAGED_ENGINE_PUBLIC_URL
    assert comparison.endpoint_b == MANAGED_ENGINE_PUBLIC_URL
    assert comparison.provider_model_route_a_id == "pmr-conn-a-model-a"
    assert comparison.provider_model_route_b_id == "pmr-share:grant-b-model-b"


@pytest.mark.asyncio
async def test_both_routes_validate_before_any_session_is_created(monkeypatch):
    import routes.compare_routes as compare
    from src.openclank.chat_routing import ChatRouteUnavailable

    monkeypatch.setattr(compare, "SessionLocal", _FakeDB)

    def _resolve(*, endpoint_id, model_id, **_kwargs):
        if endpoint_id == "missing":
            raise ChatRouteUnavailable("route unavailable")
        return _route(endpoint_id, model_id)

    monkeypatch.setattr(compare, "resolve_chat_route", _resolve)
    created = []
    start = _start(SimpleNamespace(create_session=lambda **kw: created.append(kw)))
    with pytest.raises(HTTPException) as exc:
        await start(
            _request(),
            prompt="p",
            model_a="a",
            model_b="b",
            endpoint_a_id="conn-a",
            endpoint_b_id="missing",
        )

    assert exc.value.status_code == 400
    assert created == []


@pytest.mark.asyncio
async def test_privilege_may_allow_stable_route_id(monkeypatch):
    import routes.compare_routes as compare

    db = _FakeDB()
    route = _route("conn", "same-model")
    monkeypatch.setattr(compare, "SessionLocal", lambda: db)
    monkeypatch.setattr(compare, "resolve_chat_route", lambda **_: route)
    created = []
    start = _start(SimpleNamespace(create_session=lambda **kw: created.append(kw)))

    await start(
        _request(allowed_models=[route.model_route_id]),
        prompt="p",
        model_a="same-model",
        model_b="same-model",
        endpoint_a_id="conn",
        endpoint_b_id="conn",
    )
    assert len(created) == 2


def test_compare_has_no_legacy_endpoint_or_credential_path():
    body = Path("routes/compare/compare_routes.py").read_text(encoding="utf-8")
    assert "resolve_chat_route(" in body
    assert "MANAGED_ENGINE_PUBLIC_URL" in body
    for forbidden in (
        "ModelEndpoint",
        "api_key",
        "build_headers",
        "build_chat_url",
        "_owned_endpoint_by_url",
        "_owned_endpoint_by_id",
        "_reject_raw_endpoint_url_for_non_admin",
    ):
        assert forbidden not in body
