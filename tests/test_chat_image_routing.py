"""Image routing uses normalized provider routes, never legacy endpoints."""

from types import SimpleNamespace

import pytest

from tests.helpers.import_state import clear_fake_endpoint_resolver_modules

clear_fake_endpoint_resolver_modules("routes.chat_routes")

from routes import chat_routes


def _session(
    model="qwen3.5:latest",
    *,
    endpoint_id="connection_1",
    model_route_id="route_1",
    owner="alice",
):
    return SimpleNamespace(
        model=model,
        endpoint_id=endpoint_id,
        provider_model_route_id=model_route_id,
        owner=owner,
    )


def test_known_image_model_prefix_routes_without_resolving(monkeypatch):
    def fail_if_called(**_kwargs):
        raise AssertionError("known image model IDs do not require route lookup")

    monkeypatch.setattr(chat_routes, "resolve_chat_route", fail_if_called)

    assert chat_routes._is_image_generation_session(
        _session(model="openai/gpt-5-image")
    )


def test_normalized_image_operation_routes_to_image_generation(monkeypatch):
    seen = {}

    def resolve(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(operations=("chat.stream", "image.generate"))

    monkeypatch.setattr(chat_routes, "resolve_chat_route", resolve)

    assert chat_routes._is_image_generation_session(_session(), "alice")
    assert seen == {
        "owner": "alice",
        "endpoint_id": "connection_1",
        "model_id": "qwen3.5:latest",
        "model_route_id": "route_1",
    }


def test_normalized_text_route_is_not_image_generation(monkeypatch):
    monkeypatch.setattr(
        chat_routes,
        "resolve_chat_route",
        lambda **_kwargs: SimpleNamespace(operations=("chat.stream",)),
    )

    assert not chat_routes._is_image_generation_session(_session(), "alice")


def test_unavailable_normalized_route_is_reported_orphaned(monkeypatch):
    def unavailable(**_kwargs):
        raise chat_routes.ChatRouteUnavailable("disabled")

    monkeypatch.setattr(chat_routes, "resolve_chat_route", unavailable)

    assert chat_routes._clear_orphaned_session_endpoint(_session(), "alice")


def test_available_normalized_route_preserves_session_provenance(monkeypatch):
    monkeypatch.setattr(
        chat_routes,
        "resolve_chat_route",
        lambda **_kwargs: SimpleNamespace(operations=("image.generate",)),
    )

    assert not chat_routes._clear_orphaned_session_endpoint(_session(), "alice")


def test_no_normalized_route_never_queries_model_endpoint(monkeypatch):
    monkeypatch.setattr(
        chat_routes,
        "resolve_chat_route",
        lambda **_kwargs: pytest.fail("legacy rows must not be resolved"),
    )
    legacy = SimpleNamespace(model="legacy", endpoint_id="old-endpoint")

    assert chat_routes._clear_orphaned_session_endpoint(legacy, "alice")
