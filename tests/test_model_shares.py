import json
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.chat_routes as chat_routes
import routes.model_routes as model_routes
import routes.session_routes as session_routes
from core import database as database_module
from core.database import (
    MimoAuthStore,
    ModelCapability,
    ModelEndpoint,
    ModelShare,
    ModelShareSubscription,
    Session as DbSession,
    SessionLocal,
)
from src.endpoint_resolver import (
    resolve_endpoint_by_id,
    resolve_model_target,
)
from src.model_dispatch import (
    AgentRunRequest,
    call_model_target,
    run_agent,
    stream_chat_target,
)
from src.model_shares import (
    persist_shared_native_auth,
    resolve_shared_model_access,
    shared_endpoint_id,
    shared_runtime_provider_id,
    source_native_auth,
)
from src.openclank.mimo_projection import (
    build_projection_snapshot,
    build_shared_projection_snapshot,
)
from src.openclank.mimo_supervisor import MimoSupervisor, MimoSupervisorPool


def _request(username: str, users: list[str]):
    auth_manager = SimpleNamespace(
        is_configured=True,
        is_admin=lambda _user: False,
        get_privileges=lambda _user: {},
        list_users=lambda: [
            {"username": user, "is_admin": False, "privileges": {}}
            for user in users
        ],
    )
    return SimpleNamespace(
        state=SimpleNamespace(current_user=username),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=auth_manager,
                mimo_supervisor=None,
            )
        ),
    )


def _route(router, path: str, method: str):
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == path
        and method in getattr(route, "methods", set())
    )


@pytest.fixture
def share_rows():
    suffix = uuid.uuid4().hex[:10]
    owner = f"owner-{suffix}"
    recipient = f"recipient-{suffix}"
    outsider = f"outsider-{suffix}"
    endpoint_id = f"endpoint-{suffix}"
    share_id = f"share-{suffix}"
    db = SessionLocal()
    try:
        db.add(ModelEndpoint(
            id=endpoint_id,
            owner=owner,
            name="Trusted endpoint",
            base_url="https://models.example.test/v1",
            api_key="owner-secret",
            is_enabled=True,
            cached_models=json.dumps(["full-model"]),
            model_type="llm",
        ))
        db.add(ModelShare(
            id=share_id,
            owner=owner,
            source_kind="endpoint",
            source_id=endpoint_id,
            model_id="full-model",
            active=True,
        ))
        db.add(ModelShareSubscription(
            share_id=share_id,
            subscriber=recipient,
            enabled=False,
        ))
        db.commit()
    finally:
        db.close()
    yield SimpleNamespace(
        owner=owner,
        recipient=recipient,
        outsider=outsider,
        endpoint_id=endpoint_id,
        share_id=share_id,
    )
    db = SessionLocal()
    try:
        share_ids = [
            row.id
            for row in db.query(ModelShare).filter(
                ModelShare.owner == owner
            ).all()
        ]
        if share_ids:
            db.query(ModelShareSubscription).filter(
                ModelShareSubscription.share_id.in_(share_ids)
            ).delete(synchronize_session=False)
            db.query(ModelShare).filter(ModelShare.id.in_(share_ids)).delete(
                synchronize_session=False
            )
        db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).delete(
            synchronize_session=False
        )
        for username in (owner, recipient, outsider):
            db.query(MimoAuthStore).filter(MimoAuthStore.owner == username).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def test_named_recipient_must_explicitly_enable_share(share_rows):
    db = SessionLocal()
    try:
        endpoint_id = shared_endpoint_id(share_rows.share_id)
        assert resolve_shared_model_access(
            db,
            actor_owner=share_rows.recipient,
            endpoint_id=endpoint_id,
            model_id="full-model",
        ) is None
        assert resolve_shared_model_access(
            db,
            actor_owner=share_rows.outsider,
            endpoint_id=endpoint_id,
            model_id="full-model",
        ) is None
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
        access = resolve_shared_model_access(
            db,
            actor_owner=share_rows.recipient,
            endpoint_id=endpoint_id,
            model_id="full-model",
        )
        assert access is not None
        assert access.credential_owner == share_rows.owner
    finally:
        db.close()


def test_session_endpoint_migration_preserves_shared_chat_identity(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "shared-session-migration.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                endpoint_url TEXT,
                endpoint_id TEXT,
                owner TEXT
            );
            CREATE TABLE model_endpoints (
                id TEXT PRIMARY KEY,
                base_url TEXT,
                owner TEXT,
                is_enabled INTEGER
            );
            """
        )
        conn.executemany(
            "INSERT INTO sessions "
            "(id, endpoint_url, endpoint_id, owner) VALUES (?, ?, ?, ?)",
            [
                ("shared", "mimo://acp", "shared:grant-1", "alice"),
                ("native", "mimo://acp", "mimo:xiaomi", "alice"),
                ("legacy", "mimo://acp", "mimo", "alice"),
            ],
        )
    monkeypatch.setattr(
        database_module,
        "DATABASE_URL",
        f"sqlite:///{path}",
    )

    database_module._migrate_add_session_endpoint_id_column()

    with sqlite3.connect(path) as conn:
        rows = dict(conn.execute(
            "SELECT id, endpoint_id FROM sessions"
        ).fetchall())
    assert rows == {
        "shared": "shared:grant-1",
        "native": "mimo:xiaomi",
        "legacy": "mimo:auto",
    }


def test_enabled_shared_chat_is_not_cleared_as_an_orphan(share_rows):
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
    finally:
        db.close()

    session = SimpleNamespace(
        id=f"shared-chat-{uuid.uuid4().hex}",
        endpoint_url="mimo://acp",
        endpoint_id=shared_endpoint_id(share_rows.share_id),
        model="full-model",
        headers={},
    )

    assert chat_routes._clear_orphaned_session_endpoint(
        session,
        share_rows.recipient,
    ) is False
    assert chat_routes._is_image_generation_session(
        session,
        share_rows.recipient,
    ) is False
    assert chat_routes._recover_empty_session_model(
        session,
        session.id,
        share_rows.recipient,
    ) is False
    assert session.model == "full-model"

    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = False
        db.commit()
    finally:
        db.close()
    assert chat_routes._clear_orphaned_session_endpoint(
        session,
        share_rows.recipient,
    ) is True
    assert session.endpoint_id is None
    assert session.model == ""


def test_disconnected_provider_native_chat_is_cleared_as_an_orphan():
    owner = f"disconnected-{uuid.uuid4().hex}"
    session = SimpleNamespace(
        id=f"native-chat-{uuid.uuid4().hex}",
        endpoint_url="mimo://acp",
        endpoint_id="mimo:xiaomi",
        model="xiaomi/mimo-v2.5-pro",
        headers={},
    )

    assert chat_routes._clear_orphaned_session_endpoint(session, owner) is True
    assert session.endpoint_url == ""
    assert session.endpoint_id is None
    assert session.model == ""


def test_share_directory_only_lists_named_recipient(share_rows):
    router = model_routes.setup_model_routes(model_discovery=None)
    listing = _route(router, "/api/model-shares", "GET")
    users = [
        share_rows.owner,
        share_rows.recipient,
        share_rows.outsider,
    ]

    owner_view = listing(_request(share_rows.owner, users))
    assert owner_view["received"] == []
    assert owner_view["owned"][0]["recipients"] == [{
        "username": share_rows.recipient,
        "enabled": False,
    }]
    assert owner_view["share_users"] == [
        {"username": share_rows.outsider},
        {"username": share_rows.recipient},
    ]

    recipient_view = listing(_request(share_rows.recipient, users))
    assert [item["share_id"] for item in recipient_view["received"]] == [
        share_rows.share_id
    ]
    assert recipient_view["received"][0]["tools"] is True

    outsider_view = listing(_request(share_rows.outsider, users))
    assert outsider_view["received"] == []


def test_share_directory_hides_disconnected_native_source(share_rows):
    native_share_id = f"{share_rows.share_id}-offline-native"
    db = SessionLocal()
    try:
        db.add(ModelShare(
            id=native_share_id,
            owner=share_rows.owner,
            source_kind="native",
            source_id="mimo:xiaomi",
            model_id="xiaomi/mimo-v2.5-pro",
            active=True,
        ))
        db.add(ModelShareSubscription(
            share_id=native_share_id,
            subscriber=share_rows.recipient,
            enabled=False,
        ))
        db.commit()
    finally:
        db.close()

    router = model_routes.setup_model_routes(model_discovery=None)
    listing = _route(router, "/api/model-shares", "GET")
    recipient_view = listing(_request(
        share_rows.recipient,
        [share_rows.owner, share_rows.recipient],
    ))

    assert [item["share_id"] for item in recipient_view["received"]] == [
        share_rows.share_id
    ]


def test_shared_direct_model_uses_source_tools_policy(share_rows):
    db = SessionLocal()
    try:
        db.add(ModelCapability(
            endpoint_id=share_rows.endpoint_id,
            model_id="full-model",
            tools_declared=False,
        ))
        db.commit()
    finally:
        db.close()

    router = model_routes.setup_model_routes(model_discovery=None)
    listing = _route(router, "/api/model-shares", "GET")
    recipient_view = listing(_request(
        share_rows.recipient,
        [share_rows.owner, share_rows.recipient],
    ))

    assert recipient_view["received"][0]["tools"] is False


async def test_forged_subscription_is_rejected_and_owner_controls_recipients(
    share_rows,
):
    router = model_routes.setup_model_routes(model_discovery=None)
    subscribe = _route(
        router,
        "/api/model-shares/{share_id}/subscription",
        "PUT",
    )
    users = [
        share_rows.owner,
        share_rows.recipient,
        share_rows.outsider,
    ]
    with pytest.raises(HTTPException) as exc:
        await subscribe(
            share_rows.share_id,
            model_routes.ModelShareSubscriptionUpdate(enabled=True),
            _request(share_rows.outsider, users),
        )
    assert exc.value.status_code == 404

    update = _route(router, "/api/model-shares", "PUT")
    result = await update(
        model_routes.ModelShareUpdate(
            endpoint_id=share_rows.endpoint_id,
            model_id="full-model",
            shared=True,
            recipients=[share_rows.outsider],
        ),
        _request(share_rows.owner, users),
    )
    assert result["recipients"] == [share_rows.outsider]
    db = SessionLocal()
    try:
        grants = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id
        ).all()
        assert [(grant.subscriber, grant.enabled) for grant in grants] == [
            (share_rows.outsider, False)
        ]
    finally:
        db.close()


async def test_recipient_opt_out_revokes_only_its_shared_worker(share_rows):
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
    finally:
        db.close()

    revoked = []

    class Pool:
        async def revoke_shared_access(self, owner, share_id):
            revoked.append((owner, share_id))

        async def refresh_endpoint_projection(self):
            return None

    request = _request(
        share_rows.recipient,
        [share_rows.owner, share_rows.recipient],
    )
    request.app.state.mimo_supervisor = Pool()
    router = model_routes.setup_model_routes(model_discovery=None)
    subscribe = _route(
        router,
        "/api/model-shares/{share_id}/subscription",
        "PUT",
    )

    result = await subscribe(
        share_rows.share_id,
        model_routes.ModelShareSubscriptionUpdate(enabled=False),
        request,
    )

    assert result == {"enabled": False}
    assert revoked == [(share_rows.recipient, share_rows.share_id)]


def test_trusted_native_auth_refreshes_source_without_touching_recipient(
    share_rows,
):
    db = SessionLocal()
    try:
        db.add(MimoAuthStore(
            owner=share_rows.owner,
            payload=json.dumps({
                "xiaomi": {"type": "api", "key": "shared-xiaomi"},
                "openai": {"type": "api", "key": "owner-openai"},
            }),
        ))
        db.add(MimoAuthStore(
            owner=share_rows.recipient,
            payload=json.dumps({
                "deepseek": {"type": "api", "key": "recipient-deepseek"},
            }),
        ))
        db.add(ModelShare(
            id=f"{share_rows.share_id}-native",
            owner=share_rows.owner,
            source_kind="native",
            source_id="mimo:xiaomi",
            model_id="xiaomi/mimo-v2.5-pro",
            active=True,
        ))
        db.add(ModelShareSubscription(
            share_id=f"{share_rows.share_id}-native",
            subscriber=share_rows.recipient,
            enabled=True,
        ))
        db.commit()

        access = resolve_shared_model_access(
            db,
            actor_owner=share_rows.recipient,
            endpoint_id=shared_endpoint_id(
                f"{share_rows.share_id}-native"
            ),
            model_id="xiaomi/mimo-v2.5-pro",
        )
        assert access is not None
        provider_id, credential = source_native_auth(db, access)
        assert provider_id == "xiaomi"
        assert credential["key"] == "shared-xiaomi"
        persist_shared_native_auth(
            db,
            payload={
                "xiaomi": {
                    "type": "api",
                    "key": "shared-refreshed",
                }
            },
            shared_sources={"xiaomi": share_rows.owner},
        )
        db.commit()

        recipient = json.loads(
            db.query(MimoAuthStore)
            .filter(MimoAuthStore.owner == share_rows.recipient)
            .one()
            .payload
        )
        source = json.loads(
            db.query(MimoAuthStore)
            .filter(MimoAuthStore.owner == share_rows.owner)
            .one()
            .payload
        )
        assert recipient == {
            "deepseek": {"type": "api", "key": "recipient-deepseek"},
        }
        assert source["xiaomi"]["key"] == "shared-refreshed"
        assert source["openai"]["key"] == "owner-openai"
    finally:
        db.close()


def test_shared_native_worker_coexists_with_recipient_provider_account(
    share_rows,
    tmp_path,
):
    native_share_id = f"{share_rows.share_id}-coexist"
    db = SessionLocal()
    try:
        db.add(MimoAuthStore(
            owner=share_rows.owner,
            payload=json.dumps({
                "xiaomi": {"type": "api", "key": "source-xiaomi"},
                "openai": {"type": "api", "key": "source-openai"},
            }),
        ))
        db.add(MimoAuthStore(
            owner=share_rows.recipient,
            payload=json.dumps({
                "xiaomi": {"type": "api", "key": "recipient-xiaomi"},
                "deepseek": {"type": "api", "key": "recipient-deepseek"},
            }),
        ))
        db.add(ModelShare(
            id=native_share_id,
            owner=share_rows.owner,
            source_kind="native",
            source_id="mimo:xiaomi",
            model_id="xiaomi/mimo-v2.5-pro",
            active=True,
        ))
        db.add(ModelShareSubscription(
            share_id=native_share_id,
            subscriber=share_rows.recipient,
            enabled=True,
        ))
        db.commit()
        access = resolve_shared_model_access(
            db,
            actor_owner=share_rows.recipient,
            endpoint_id=shared_endpoint_id(native_share_id),
            model_id="xiaomi/mimo-v2.5-pro",
        )
        assert access is not None
    finally:
        db.close()

    personal_snapshot = build_projection_snapshot(share_rows.recipient)
    shared_snapshot = build_shared_projection_snapshot(access)
    pool = MimoSupervisorPool(
        auth_enabled=True,
        data_dir=tmp_path,
        readiness_budget=0,
    )
    personal_worker = pool._new_worker(
        share_rows.recipient,
        personal_snapshot,
        1,
        "personal",
    )
    partition = pool._share_partition(share_rows.recipient, native_share_id)
    shared_worker = pool._new_shared_worker(
        partition,
        access,
        shared_snapshot,
        1,
        "shared",
    )

    assert personal_worker._credential_sources is None
    assert shared_worker._credential_sources == {
        "xiaomi": share_rows.owner,
    }
    assert personal_worker._runtime_home != shared_worker._runtime_home
    assert personal_worker._owner == shared_worker._owner == share_rows.recipient


def test_shared_worker_auth_cache_contains_only_the_granted_source(
    share_rows,
    tmp_path,
):
    db = SessionLocal()
    try:
        db.add(MimoAuthStore(
            owner=share_rows.owner,
            payload=json.dumps({
                "xiaomi": {"type": "api", "key": "source-xiaomi"},
                "openai": {"type": "api", "key": "source-openai"},
            }),
        ))
        db.add(MimoAuthStore(
            owner=share_rows.recipient,
            payload=json.dumps({
                "xiaomi": {"type": "api", "key": "recipient-xiaomi"},
                "deepseek": {"type": "api", "key": "recipient-deepseek"},
            }),
        ))
        db.commit()
    finally:
        db.close()

    worker = MimoSupervisor(
        owner=share_rows.recipient,
        runtime_home=tmp_path,
        partitioned=True,
        credential_sources={"xiaomi": share_rows.owner},
    )
    worker._mimocode_home = str(tmp_path / "mimocode")
    worker._reconcile_auth_store()

    assert json.loads(worker._auth_file().read_text(encoding="utf-8")) == {
        "xiaomi": {"type": "api", "key": "source-xiaomi"},
    }
    assert json.loads(
        (tmp_path / "shared-native-auth-sources.json").read_text(
            encoding="utf-8"
        )
    ) == {"xiaomi": share_rows.owner}


def test_unchanged_personal_worker_cannot_overwrite_shared_token_refresh(
    share_rows,
    tmp_path,
):
    original = {"xiaomi": {"type": "api", "key": "before"}}
    refreshed = {"xiaomi": {"type": "api", "key": "after"}}
    db = SessionLocal()
    try:
        db.add(MimoAuthStore(
            owner=share_rows.owner,
            payload=json.dumps(original),
        ))
        db.commit()
    finally:
        db.close()

    worker = MimoSupervisor(
        owner=share_rows.owner,
        runtime_home=tmp_path / "personal",
        partitioned=True,
    )
    worker._mimocode_home = str(tmp_path / "personal" / "mimocode")
    worker._reconcile_auth_store()

    db = SessionLocal()
    try:
        row = db.query(MimoAuthStore).filter(
            MimoAuthStore.owner == share_rows.owner
        ).one()
        row.payload = json.dumps(refreshed)
        db.commit()
    finally:
        db.close()

    worker.sync_auth_to_db()

    db = SessionLocal()
    try:
        assert json.loads(
            db.query(MimoAuthStore)
            .filter(MimoAuthStore.owner == share_rows.owner)
            .one()
            .payload
        ) == refreshed
    finally:
        db.close()


def test_parallel_shared_workers_merge_only_their_provider(
    share_rows,
    tmp_path,
):
    db = SessionLocal()
    try:
        db.add(MimoAuthStore(
            owner=share_rows.owner,
            payload=json.dumps({
                "xiaomi": {"type": "api", "key": "xiaomi-before"},
                "openai": {"type": "oauth", "access": "openai-before"},
            }),
        ))
        db.commit()
    finally:
        db.close()

    xiaomi = MimoSupervisor(
        owner=share_rows.recipient,
        runtime_home=tmp_path / "xiaomi",
        partitioned=True,
        credential_sources={"xiaomi": share_rows.owner},
    )
    openai = MimoSupervisor(
        owner=share_rows.recipient,
        runtime_home=tmp_path / "openai",
        partitioned=True,
        credential_sources={"openai": share_rows.owner},
    )
    for worker in (xiaomi, openai):
        worker._mimocode_home = str(worker._runtime_home / "mimocode")
        worker._reconcile_auth_store()

    xiaomi._auth_file().write_text(json.dumps({
        "xiaomi": {"type": "api", "key": "xiaomi-after"},
    }), encoding="utf-8")
    openai._auth_file().write_text(json.dumps({
        "openai": {"type": "oauth", "access": "openai-after"},
    }), encoding="utf-8")
    xiaomi.sync_auth_to_db()
    openai.sync_auth_to_db()

    db = SessionLocal()
    try:
        assert json.loads(
            db.query(MimoAuthStore)
            .filter(MimoAuthStore.owner == share_rows.owner)
            .one()
            .payload
        ) == {
            "xiaomi": {"type": "api", "key": "xiaomi-after"},
            "openai": {"type": "oauth", "access": "openai-after"},
        }
    finally:
        db.close()


def test_shared_tuple_resolution_retains_private_route_identity(share_rows):
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
    finally:
        db.close()

    resolved = resolve_endpoint_by_id(
        shared_endpoint_id(share_rows.share_id),
        "full-model",
        owner=share_rows.recipient,
    )
    assert resolved is not None
    target = resolve_model_target(*resolved)
    assert target.endpoint_id == shared_endpoint_id(share_rows.share_id)
    assert target.headers == {}


@pytest.mark.asyncio
async def test_shared_model_can_create_and_update_chat_route(
    share_rows,
    monkeypatch,
):
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
    finally:
        db.close()

    class Manager:
        def __init__(self):
            self.sessions = {}
            self.created = None

        def create_session(self, **kwargs):
            self.created = kwargs
            session = SimpleNamespace(
                id=kwargs["session_id"],
                name=kwargs["name"],
                model=kwargs["model"],
                endpoint_url=kwargs["endpoint_url"],
                endpoint_id=kwargs["endpoint_id"],
                headers={},
            )
            self.sessions[session.id] = session
            return session

        def get_session(self, sid):
            return self.sessions[sid]

    manager = Manager()
    request = _request(
        share_rows.recipient,
        [share_rows.owner, share_rows.recipient],
    )
    monkeypatch.setattr(
        session_routes,
        "effective_user",
        lambda _request: share_rows.recipient,
    )
    monkeypatch.setattr(
        session_routes,
        "_verify_session_owner",
        lambda _request, _sid: None,
    )
    monkeypatch.setattr("src.event_bus.fire_event", lambda *_args, **_kwargs: None)

    async def prepare_context_mutation(_request, _sid):
        return None

    monkeypatch.setattr(
        session_routes,
        "_prepare_context_mutation",
        prepare_context_mutation,
    )
    router = session_routes.setup_session_routes(manager, {})
    create_chat = next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == "/api/session"
        and "POST" in getattr(route, "methods", set())
    )
    patch_chat = next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == "/api/session/{sid}"
        and "PATCH" in getattr(route, "methods", set())
    )
    endpoint_id = shared_endpoint_id(share_rows.share_id)

    created = await create_chat(
        request,
        name="Shared chat",
        endpoint_url="mimo://acp",
        model="full-model",
        rag=None,
        skip_validation=None,
        incognito=None,
        api_key="",
        endpoint_id=endpoint_id,
    )
    assert manager.created["endpoint_id"] == endpoint_id
    assert manager.created["endpoint_url"] == "mimo://acp"
    assert manager.created["model"] == "full-model"
    assert created.endpoint_id == endpoint_id
    assert created.endpoint_url == "mimo://acp"

    session_id = f"shared-patch-{uuid.uuid4().hex}"
    active = SimpleNamespace(
        id=session_id,
        name="Existing chat",
        model="old-model",
        endpoint_url="https://old.example.test/chat",
        endpoint_id="old-endpoint",
        headers={"Authorization": "old"},
    )
    manager.sessions[session_id] = active
    db = SessionLocal()
    try:
        db.add(DbSession(
            id=session_id,
            name=active.name,
            model=active.model,
            endpoint_url=active.endpoint_url,
            endpoint_id=active.endpoint_id,
            headers=active.headers,
            owner=share_rows.recipient,
        ))
        db.commit()

        updated = await patch_chat(
            request,
            session_id,
            name=None,
            folder=None,
            model="full-model",
            endpoint_url="mimo://acp",
            endpoint_id=endpoint_id,
        )
        db.expire_all()
        persisted = db.query(DbSession).filter(DbSession.id == session_id).one()
        assert updated["endpoint_id"] == endpoint_id
        assert updated["endpoint_url"] == "mimo://acp"
        assert updated["model"] == "full-model"
        assert active.endpoint_id == persisted.endpoint_id == endpoint_id
        assert active.endpoint_url == persisted.endpoint_url == "mimo://acp"
        assert active.model == persisted.model == "full-model"
        assert active.headers == persisted.headers == {}
    finally:
        db.query(DbSession).filter(DbSession.id == session_id).delete()
        db.commit()
        db.close()


async def test_shared_session_controls_use_the_shared_worker(share_rows):
    db = SessionLocal()
    session_id = f"shared-controls-{uuid.uuid4().hex}"
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.add(DbSession(
            id=session_id,
            name="Shared controls",
            endpoint_url="mimo://acp",
            endpoint_id=shared_endpoint_id(share_rows.share_id),
            model="full-model",
            owner=share_rows.recipient,
            headers={},
        ))
        db.commit()
    finally:
        db.close()

    calls = []
    releases = []

    class Worker:
        async def negotiate_session(self, sid, *, owner, cwd=None):
            calls.append(("negotiate", sid, owner, cwd))
            return {"commands": [{"name": "test"}]}

        async def set_session_config(
            self,
            sid,
            config_id,
            value,
            *,
            owner,
            cwd=None,
        ):
            calls.append(("config", sid, owner, cwd, config_id, value))
            return {"config_options": [{"id": config_id, "currentValue": value}]}

    class Lease:
        worker = Worker()

        async def release(self, *, successful_terminal=False):
            releases.append(successful_terminal)

    pool = MimoSupervisorPool(auth_enabled=True)

    async def personal(*_args, **_kwargs):
        raise AssertionError("shared controls used the recipient personal worker")

    async def shared(access, provider_id, model_id):
        assert access.actor_owner == share_rows.recipient
        assert provider_id == shared_runtime_provider_id(share_rows.share_id)
        assert model_id == "full-model"
        return Lease()

    pool.for_owner = personal
    pool.admit_shared_agent = shared
    try:
        await pool.negotiate_session(
            session_id,
            owner=share_rows.recipient,
            cwd="/tmp/shared-controls",
        )
        await pool.set_session_config(
            session_id,
            "mode",
            "build",
            owner=share_rows.recipient,
            cwd="/tmp/shared-controls",
        )
    finally:
        db = SessionLocal()
        try:
            db.query(DbSession).filter(DbSession.id == session_id).delete()
            db.commit()
        finally:
            db.close()

    assert [call[0] for call in calls] == ["negotiate", "config"]
    assert releases == [True, True]


def test_shared_model_is_optional_in_defaults_and_settings(
    share_rows,
    monkeypatch,
):
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
    finally:
        db.close()

    endpoint_id = shared_endpoint_id(share_rows.share_id)
    monkeypatch.setattr(model_routes, "_load_settings", lambda: {})
    monkeypatch.setattr(
        "routes.prefs_routes._load_for_user",
        lambda _owner: {
            "default_endpoint_id": endpoint_id,
            "default_model": "full-model",
        },
    )
    router = model_routes.setup_model_routes(model_discovery=None)
    request = _request(
        share_rows.recipient,
        [share_rows.owner, share_rows.recipient],
    )

    default_chat = _route(router, "/api/default-chat", "GET")(request)
    assert default_chat == {
        "endpoint_id": endpoint_id,
        "endpoint_url": "mimo://acp",
        "model": "full-model",
    }

    endpoints = _route(router, "/api/model-endpoints", "GET")(request)
    shared = next(item for item in endpoints if item["id"] == endpoint_id)
    assert shared["shared"] is True
    assert shared["read_only"] is True
    assert shared["selector_only"] is False
    assert shared["models"] == ["full-model"]


@pytest.mark.asyncio
async def test_enabling_share_adds_it_to_catalog_and_added_models(share_rows):
    router = model_routes.setup_model_routes(model_discovery=None)
    request = _request(
        share_rows.recipient,
        [share_rows.owner, share_rows.recipient],
    )
    subscribe = _route(
        router,
        "/api/model-shares/{share_id}/subscription",
        "PUT",
    )
    catalogue_route = _route(router, "/api/models", "GET")
    endpoint_id = shared_endpoint_id(share_rows.share_id)

    before = catalogue_route(request, refresh=False, background=False)
    assert all(
        item["endpoint_id"] != endpoint_id
        for item in before["items"]
    )

    await subscribe(
        share_rows.share_id,
        model_routes.ModelShareSubscriptionUpdate(enabled=True),
        request,
    )

    endpoints = _route(router, "/api/model-endpoints", "GET")(request)
    shared_endpoint = next(
        item for item in endpoints
        if item["id"] == endpoint_id
    )
    assert shared_endpoint["models"] == ["full-model"]
    assert shared_endpoint["shared"] is True
    assert shared_endpoint["read_only"] is True
    assert shared_endpoint["selector_only"] is False

    catalogue = catalogue_route(request, refresh=False, background=False)
    shared_catalogue = next(
        item for item in catalogue["items"]
        if item["endpoint_id"] == shared_endpoint["id"]
    )
    assert shared_catalogue["models"] == ["full-model"]
    assert shared_catalogue["shared"] is True


def test_personal_and_shared_routes_for_same_model_coexist(share_rows):
    personal_endpoint = f"{share_rows.endpoint_id}-recipient"
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.add(ModelEndpoint(
            id=personal_endpoint,
            owner=share_rows.recipient,
            name="My own account",
            base_url="https://personal.example.test/v1",
            api_key="recipient-secret",
            is_enabled=True,
            cached_models=json.dumps(["full-model"]),
            pinned_models=json.dumps(["full-model"]),
            model_type="llm",
        ))
        db.commit()
    finally:
        db.close()

    try:
        router = model_routes.setup_model_routes(model_discovery=None)
        request = _request(
            share_rows.recipient,
            [share_rows.owner, share_rows.recipient],
        )
        catalogue = _route(router, "/api/models", "GET")(request)
        routes = {
            item["endpoint_id"]
            for item in catalogue["items"]
            if "full-model" in item.get("models", [])
        }
        assert routes == {
            personal_endpoint,
            shared_endpoint_id(share_rows.share_id),
        }
    finally:
        db = SessionLocal()
        try:
            db.query(ModelEndpoint).filter(
                ModelEndpoint.id == personal_endpoint
            ).delete()
            db.commit()
        finally:
            db.close()


async def test_shared_direct_model_runs_fully_in_recipient_worker(share_rows):
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
    finally:
        db.close()

    calls = []

    class Bridge:
        async def run_turn(
            self,
            session_id,
            messages,
            *,
            model,
            cwd,
            owner,
            turn_envelope,
        ):
            calls.append({
                "session_id": session_id,
                "messages": messages,
                "model": model,
                "cwd": cwd,
                "owner": owner,
                "envelope": turn_envelope,
            })
            yield "data: [DONE]\n\n"

    worker = SimpleNamespace(bridge=Bridge())

    class Lease:
        generation = 4
        fingerprint = "full-sharing-test"
        projection_pending = False

        def __init__(self):
            self.worker = worker

        async def release(self, *, successful_terminal=False):
            assert successful_terminal is True

    admissions = []

    class Pool:
        async def admit_agent(self, *_args):
            raise AssertionError("shared route used the personal worker")

        async def admit_shared_agent(self, access, provider_id, model_id):
            admissions.append((access.actor_owner, provider_id, model_id))
            return Lease()

    session_id = f"recipient-chat-{uuid.uuid4().hex}"
    request = AgentRunRequest(
        target=resolve_model_target(
            "mimo://acp",
            "full-model",
            endpoint_id=shared_endpoint_id(share_rows.share_id),
        ),
        messages=[{"role": "user", "content": "use my tools"}],
        session_id=session_id,
        owner=share_rows.recipient,
        cwd="/home/e/recipient-workspace",
        supervisor=Pool(),
    )

    events = [event async for event in run_agent(request)]

    assert events[-1] == "data: [DONE]\n\n"
    assert admissions == [(
        share_rows.recipient,
        shared_runtime_provider_id(share_rows.share_id),
        "full-model",
    )]
    assert calls[0]["session_id"] == session_id
    assert calls[0]["owner"] == share_rows.recipient
    assert calls[0]["cwd"] == "/home/e/recipient-workspace"
    assert calls[0]["envelope"]["lane"] == "agent"
    assert calls[0]["envelope"]["shared_model"] == share_rows.share_id
    assert "allowed_tools" not in calls[0]["envelope"]


async def test_shared_model_supports_regular_chat_and_auxiliary_calls(
    share_rows,
):
    db = SessionLocal()
    try:
        grant = db.query(ModelShareSubscription).filter(
            ModelShareSubscription.share_id == share_rows.share_id,
            ModelShareSubscription.subscriber == share_rows.recipient,
        ).one()
        grant.enabled = True
        db.commit()
    finally:
        db.close()

    calls = []
    deleted = []
    released = []

    class Bridge:
        async def run_turn(
            self,
            session_id,
            messages,
            *,
            model,
            cwd,
            owner,
            turn_envelope,
        ):
            calls.append({
                "session_id": session_id,
                "model": model,
                "owner": owner,
                "envelope": turn_envelope,
            })
            yield 'data: {"delta":"shared answer"}\n\n'
            yield "data: [DONE]\n\n"

    class Worker:
        bridge = Bridge()

        async def delete_session(self, session_id):
            deleted.append(session_id)

    class Lease:
        worker = Worker()

        async def release(self, *, successful_terminal=False):
            released.append(successful_terminal)

    class Pool:
        async def for_owner(self, *_args):
            raise AssertionError("shared call used the recipient's personal worker")

        async def admit_shared_agent(self, access, provider_id, model_id):
            assert access.actor_owner == share_rows.recipient
            assert provider_id == shared_runtime_provider_id(share_rows.share_id)
            assert model_id == "full-model"
            return Lease()

    target = resolve_model_target(
        "mimo://acp",
        "full-model",
        endpoint_id=shared_endpoint_id(share_rows.share_id),
    )
    regular = [
        event
        async for event in stream_chat_target(
            target,
            [{"role": "user", "content": "hello"}],
            session_id="regular-shared",
            owner=share_rows.recipient,
            supervisor=Pool(),
        )
    ]
    auxiliary = await call_model_target(
        target,
        [{"role": "user", "content": "title this"}],
        session_id="aux-shared",
        owner=share_rows.recipient,
        supervisor=Pool(),
        purpose="title",
    )

    assert regular[-1] == "data: [DONE]\n\n"
    assert auxiliary == "shared answer"
    assert [call["model"] for call in calls] == [
        f"{shared_runtime_provider_id(share_rows.share_id)}/full-model",
        f"{shared_runtime_provider_id(share_rows.share_id)}/full-model",
    ]
    assert all(call["owner"] == share_rows.recipient for call in calls)
    assert deleted == ["regular-shared", "aux-shared"]
    assert released == [True, True]


async def test_unaddressed_user_cannot_dispatch_shared_model(share_rows):
    class Pool:
        async def admit_agent(self, *_args):
            raise AssertionError("unauthorized share reached a personal worker")

        async def admit_shared_agent(self, *_args):
            raise AssertionError("unauthorized share reached a worker")

    request = AgentRunRequest(
        target=resolve_model_target(
            "mimo://acp",
            "full-model",
            endpoint_id=shared_endpoint_id(share_rows.share_id),
        ),
        messages=[{"role": "user", "content": "no"}],
        session_id="outsider-chat",
        owner=share_rows.outsider,
        supervisor=Pool(),
    )
    events = [event async for event in run_agent(request)]
    assert '"status": 403' in events[0]
    assert events[-1] == "data: [DONE]\n\n"
