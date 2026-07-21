from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from core.database import Session as DbSession

# Import the route helper during collection so sibling session tests that use
# partial import stubs do not become the first loader of core.session_manager.
import routes.session_routes as session_routes
from routes.session_routes import _reject_raw_endpoint_url_for_non_admin


def _request(user, *, admin=False):
    auth_manager = SimpleNamespace(is_admin=lambda username: bool(admin))
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
    )


def test_non_admin_session_create_rejects_raw_endpoint_url_without_endpoint_id():
    with pytest.raises(HTTPException) as exc:
        _reject_raw_endpoint_url_for_non_admin(
            _request("alice", admin=False),
            "alice",
            "",
            "http://169.254.169.254/latest/meta-data",
        )

    assert exc.value.status_code == 403


def test_admin_and_registered_endpoint_can_use_endpoint_url():
    _reject_raw_endpoint_url_for_non_admin(
        _request("alice", admin=False),
        "alice",
        "endpoint-id",
        "http://127.0.0.1:8000/v1/chat/completions",
    )
    _reject_raw_endpoint_url_for_non_admin(
        _request("admin", admin=True),
        "admin",
        "",
        "http://127.0.0.1:8000/v1/chat/completions",
    )


@pytest.mark.asyncio
async def test_existing_session_can_switch_to_virtual_mimo_endpoint(monkeypatch):
    session = SimpleNamespace(
        id="session-1",
        owner="alice",
        model="old-model",
        endpoint_url="https://old.example/v1/chat/completions",
        endpoint_id="old-endpoint",
        headers={"Authorization": "old"},
    )
    persisted = SimpleNamespace(
        model=session.model,
        endpoint_url=session.endpoint_url,
        endpoint_id=session.endpoint_id,
        headers=session.headers,
        updated_at=None,
    )

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return persisted

    class Db:
        def query(self, model_cls):
            assert model_cls is DbSession, "virtual MiMo must not query ModelEndpoint"
            return Query()

        def commit(self):
            pass

        def close(self):
            pass

    manager = SimpleNamespace(get_session=lambda sid: session)
    supervisor = SimpleNamespace(
        available_models=lambda owner=None: [{"modelId": "xiaomi/mimo-v2.5-pro"}],
    )
    auth_manager = SimpleNamespace(
        is_admin=lambda user: False,
        get_privileges=lambda user: {},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(
            auth_manager=auth_manager,
            mimo_supervisor=supervisor,
        )),
    )

    monkeypatch.setattr(session_routes, "SessionLocal", Db)
    monkeypatch.setattr(session_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda request, sid: None)

    async def prepare_context_mutation(request, sid):
        return None

    monkeypatch.setattr(session_routes, "_prepare_context_mutation", prepare_context_mutation)
    router = session_routes.setup_session_routes(manager, {})
    patch_session = next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == "/api/session/{sid}"
        and "PATCH" in getattr(route, "methods", set())
    )

    result = await patch_session(
        request,
        "session-1",
        name=None,
        folder=None,
        model="xiaomi/mimo-v2.5-pro",
        endpoint_url="mimo://acp",
        endpoint_id="mimo",
    )

    assert result["model"] == "xiaomi/mimo-v2.5-pro"
    assert result["endpoint_url"] == "mimo://acp"
    assert result["endpoint_id"] == "mimo"
    assert session.model == persisted.model == "xiaomi/mimo-v2.5-pro"
    assert session.endpoint_url == persisted.endpoint_url == "mimo://acp"
    assert session.endpoint_id == persisted.endpoint_id == "mimo"
    assert session.headers == persisted.headers == {}


def test_chat_endpoint_recovery_paths_are_owner_scoped():
    root = Path(__file__).resolve().parents[1]
    chat_routes = (root / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    chat_helpers = (root / "routes" / "chat_helpers.py").read_text(encoding="utf-8")

    assert "def _clear_orphaned_session_endpoint(sess, owner:" in chat_routes
    assert "def _recover_empty_session_model(sess, session_id: str, owner:" in chat_routes
    assert "q = owner_filter(q, ModelEndpoint, owner)" in chat_routes
    assert "resolve_session_auth(sess, session, owner=effective_user(request))" in chat_routes
    assert "def resolve_session_auth(sess, session_id: str, owner:" in chat_helpers
    assert "update_q = update_q.filter(DBSession.owner == owner)" in chat_helpers
