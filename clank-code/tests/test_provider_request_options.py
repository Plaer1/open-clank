from __future__ import annotations

import pytest
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as core_database
from src.openclank.provider_request_options import (
    ProviderRequestOptionError,
    persist_request_option_selection,
    project_request_options,
    validate_request_option_selection,
    persist_session_request_options,
    restore_session_request_options,
)
import src.openclank.transcript_projection as transcript_projection


def test_incognito_turn_options_are_validated_without_preference_access(monkeypatch):
    from routes import chat_routes
    import src.openclank.provider_request_options as request_options
    route = SimpleNamespace(model_route_id="route", option_route_id="public", provider_model_id="model",
                            catalog_revision=7, capabilities={"reasoning_effort": ["none", "high"]})
    monkeypatch.setattr(chat_routes, "resolve_chat_route", lambda **_kwargs: route)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("incognito must not read or write stored preferences")
    monkeypatch.setattr(request_options, "persist_session_request_options", forbidden)
    monkeypatch.setattr(request_options, "restore_session_request_options", forbidden)
    ghost = SimpleNamespace(id="ghost", provider_model_route_id="route", endpoint_id="endpoint", model="model")
    selection = {"route_id": "public", "catalog_revision": 7, "options": {"reasoning_effort": "none"}}
    result = chat_routes._resolve_turn_model_request_options(ghost, "alice", selection, incognito=True)
    assert result["options"] == {"reasoning_effort": "none"}
    assert chat_routes._resolve_turn_model_request_options(ghost, "alice", None, incognito=True)["options"] == {}
    with pytest.raises(chat_routes.HTTPException) as error:
        chat_routes._resolve_turn_model_request_options(ghost, "alice", {**selection, "catalog_revision": 6}, incognito=True)
    assert error.value.status_code == 409


def test_descriptor_requires_explicit_supported_effort_and_carries_route_revision():
    assert project_request_options({"reasoning": True}, catalog_revision=4, source="account-discovery") == []
    descriptor = project_request_options(
        {"reasoning": True, "reasoning_effort": ["low", "none", "0", "low", None, "bad\nvalue"]},
        catalog_revision=4,
        source="account-discovery",
    )
    assert descriptor == [{
        "id": "reasoning_effort",
        "type": "enum",
        "label": "Thinking",
        "values": ["low", "none", "0"],
        "default": "inherit",
        "scope": "model",
        "source": "account-discovery",
        "catalog_revision": 4,
    }]


def test_validation_preserves_explicit_none_and_zero_and_omits_default():
    descriptor = project_request_options(
        {"reasoning_effort": ["none", "0", "high"]},
        catalog_revision=7,
        source="account-discovery",
    )
    def selection(value):
        return {"route_id": "route-a", "catalog_revision": 7, "options": {"reasoning_effort": value}}

    assert validate_request_option_selection(
        route_id="route-a", catalog_revision=7, descriptors=descriptor, selection=selection("default")
    ) == {}
    assert validate_request_option_selection(
        route_id="route-a", catalog_revision=7, descriptors=descriptor, selection=selection("inherit")
    ) == {}
    assert validate_request_option_selection(
        route_id="route-a", catalog_revision=7, descriptors=descriptor, selection=selection("none")
    ) == {"reasoning_effort": "none"}
    assert validate_request_option_selection(
        route_id="route-a", catalog_revision=7, descriptors=descriptor, selection=selection("0")
    ) == {"reasoning_effort": "0"}


@pytest.mark.parametrize(
    "route_id,revision,value",
    [("route-b", 7, "high"), ("route-a", 8, "high"), ("route-a", 7, "max")],
)
def test_validation_rejects_wrong_route_stale_revision_and_unsupported_value(route_id, revision, value):
    descriptor = project_request_options(
        {"reasoning_effort": ["low", "high"]},
        catalog_revision=7,
        source="account-discovery",
    )
    selection = {"route_id": route_id, "catalog_revision": revision, "options": {"reasoning_effort": value}}
    with pytest.raises(ProviderRequestOptionError):
        validate_request_option_selection(
            route_id="route-a", catalog_revision=7, descriptors=descriptor, selection=selection
        )


@pytest.mark.parametrize("revision", [True, 7.0, 7.9, "7", None])
def test_validation_rejects_non_integer_catalog_revision_types(revision):
    descriptor = project_request_options(
        {"reasoning_effort": ["low", "high"]}, catalog_revision=7, source="account-discovery"
    )
    selection = {"route_id": "route-a", "catalog_revision": revision, "options": {"reasoning_effort": "high"}}
    with pytest.raises(ProviderRequestOptionError, match="catalogue revision is invalid"):
        validate_request_option_selection(
            route_id="route-a", catalog_revision=7, descriptors=descriptor, selection=selection
        )


def test_session_state_persists_exact_route_and_removes_default_without_losing_other_state():
    state = {"existing": {"keep": True}}
    selected = persist_request_option_selection(
        state,
        route_id="route-a",
        catalog_revision=3,
        options={"reasoning_effort": "0"},
    )
    assert selected == {
        "existing": {"keep": True},
        "model_request_options": {
            "route_id": "route-a",
            "catalog_revision": 3,
            "options": {"reasoning_effort": "0"},
        },
    }
    assert persist_request_option_selection(
        selected,
        route_id="route-a",
        catalog_revision=3,
        options={},
    ) == {"existing": {"keep": True}}


@pytest.fixture()
def isolated_session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_database.Session.__table__.create(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(core_database, "SessionLocal", factory)
    monkeypatch.setattr(transcript_projection, "SessionLocal", factory)
    with factory() as db:
        db.add(core_database.Session(
            id="chat-a",
            owner="alice",
            name="Reasoning fixture",
            endpoint_url="openclank://engine",
            endpoint_id="pcn-a",
            provider_model_route_id="route-a",
            model="fixture-model",
            mimo_state={"preserve": True},
        ))
        db.commit()
    yield factory
    engine.dispose()


def test_session_option_round_trip_is_owner_route_and_revision_bound(isolated_session):
    descriptor = project_request_options(
        {"reasoning_effort": ["low", "none", "0"]},
        catalog_revision=5,
        source="account-discovery",
    )
    selection = {
        "route_id": "route-a",
        "catalog_revision": 5,
        "options": {"reasoning_effort": "0"},
    }
    assert persist_session_request_options(
        "chat-a", "ALICE", route_id="route-a", catalog_revision=5,
        descriptors=descriptor, selection=selection,
    ) == {"reasoning_effort": "0"}
    assert restore_session_request_options(
        "chat-a", "alice", route_id="route-a", catalog_revision=5,
        descriptors=descriptor,
    ) == {"reasoning_effort": "0"}
    with pytest.raises(ProviderRequestOptionError, match="stale or different route"):
        restore_session_request_options(
            "chat-a", "alice", route_id="route-a", catalog_revision=6,
            descriptors=project_request_options(
                {"reasoning_effort": ["low", "none", "0"]},
                catalog_revision=6,
                source="account-discovery",
            ),
        )
    with pytest.raises(ProviderRequestOptionError, match="owner"):
        restore_session_request_options(
            "chat-a", "bob", route_id="route-a", catalog_revision=5,
            descriptors=descriptor,
        )
    with isolated_session() as db:
        row = db.query(core_database.Session).filter_by(id="chat-a").one()
        assert row.mimo_state["preserve"] is True
        assert row.mimo_state["model_request_options"] == selection
    transcript_projection.save_mimo_state(
        "chat-a", {"preserve": True, "stale_snapshot": True}, owner="alice"
    )
    with isolated_session() as db:
        row = db.query(core_database.Session).filter_by(id="chat-a").one()
        assert row.mimo_state["model_request_options"] == selection
    assert persist_session_request_options(
        "chat-a", "alice", route_id="route-a", catalog_revision=5,
        descriptors=descriptor,
        selection={"route_id": "route-a", "catalog_revision": 5, "options": {"reasoning_effort": "inherit"}},
    ) == {}
    transcript_projection.save_mimo_state(
        "chat-a", {"preserve": True, "stale_snapshot": True, "model_request_options": selection}, owner="alice"
    )
    with isolated_session() as db:
        row = db.query(core_database.Session).filter_by(id="chat-a").one()
        assert "model_request_options" not in row.mimo_state


def test_shared_public_option_route_key_is_distinct_from_internal_session_route(isolated_session):
    descriptor = project_request_options(
        {"reasoning_effort": ["low", "high"]},
        catalog_revision=9,
        source="account-discovery",
    )
    selection = {
        "route_id": "pmro_grant_scoped_key",
        "catalog_revision": 9,
        "options": {"reasoning_effort": "high"},
    }
    assert persist_session_request_options(
        "chat-a", "alice", route_id="route-a", option_route_id="pmro_grant_scoped_key",
        catalog_revision=9, descriptors=descriptor, selection=selection,
    ) == {"reasoning_effort": "high"}
    assert restore_session_request_options(
        "chat-a", "alice", route_id="route-a", option_route_id="pmro_grant_scoped_key",
        catalog_revision=9, descriptors=descriptor,
    ) == {"reasoning_effort": "high"}
    with isolated_session() as db:
        row = db.query(core_database.Session).filter_by(id="chat-a").one()
        assert row.provider_model_route_id == "route-a"
        assert row.mimo_state["model_request_options"]["route_id"] == "pmro_grant_scoped_key"


def test_child_receipts_restore_own_route_or_default_without_parent_preference():
    from src.openclank.provider_request_options import restore_child_receipt_options
    descriptors = project_request_options({"reasoning_effort":["none", "high"]}, catalog_revision=9, source="engine-catalog")
    kwargs = dict(route_id="child-route", option_route_id="child-option", catalog_revision=9, descriptors=descriptors, provider_id="child-provider", model_id="child-model")
    assert restore_child_receipt_options([], **kwargs) == {}
    assert restore_child_receipt_options([{"state":"consumed", "modelRequestOptions":{"present":False}}], **kwargs) == {}
    frozen = {"present":True, "routeID":"child-route", "optionRouteID":"child-option", "catalogRevision":9, "model":{"providerID":"child-provider", "modelID":"child-model"}, "options":{"reasoning_effort":"none"}}
    assert restore_child_receipt_options([{"state":"consumed", "modelRequestOptions":frozen}], **kwargs) == {"reasoning_effort":"none"}
    assert restore_child_receipt_options([{"state":"consumed", "modelRequestOptions":{**frozen, "options":{}}}], **kwargs) == {}
    with pytest.raises(ProviderRequestOptionError):
        restore_child_receipt_options([{"state":"consumed", "modelRequestOptions":{**frozen, "catalogRevision":8}}], **kwargs)
    with pytest.raises(ProviderRequestOptionError):
        restore_child_receipt_options([{"state":"consumed", "modelRequestOptions":{**frozen, "routeID":"parent-route"}}], **kwargs)
