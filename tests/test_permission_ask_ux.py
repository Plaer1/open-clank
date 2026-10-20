"""C1 — permission ask UX: durable grants + no-timeout prompt flow.

e's rulings (2026-07-09): durable always-allow grants surviving restarts;
file grants cover the directory subtree; ALL permission types route through
safe-dirs -> stored grants -> prompt; the prompt waits forever (no 300s
auto-reject). mimo's optionIds are exactly 'once' | 'always' | 'reject'
(packages/opencode/src/acp/agent.ts:147-150).
"""

import asyncio
import concurrent.futures
import inspect
import sqlite3
from types import SimpleNamespace

import pytest

from src.openclank.permission_grants import GrantStore, derive_pattern, grant_scope_for_lifetime
from src.openclank.acp_bridge import PermissionHandler, PermissionRequest


def _params(title="external_directory", raw_input=None, session_id="mimo_sess_1"):
    return {
        "sessionId": session_id,
        "toolCall": {"toolCallId": "tc1", "title": title, "rawInput": raw_input or {}},
        "options": [
            {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
            {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
            {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
        ],
    }


def _route_handler(router, path, method):
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == path
        and method in getattr(route, "methods", set())
    )


# ── GrantStore ──


def test_grant_subtree_match_respects_dir_boundaries(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("external_directory", "/home/x/proj")
    assert store.match("external_directory", filepath="/home/x/proj/sub/f.txt")
    assert store.match("external_directory", filepath="/home/x/proj")
    # /home/x/projother must NOT match a /home/x/proj grant (prefix != subtree)
    assert not store.match("external_directory", filepath="/home/x/projother/f.txt")
    assert not store.match("external_directory", filepath="/home/x/other/f.txt")


def test_grants_are_type_scoped(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("external_directory", "/home/x/proj")
    assert not store.match("bash", filepath="/home/x/proj/f.txt")


def test_wildcard_grant_covers_whole_type(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("bash", "*")
    assert store.match("bash")
    assert store.match("bash", filepath="/anything")
    assert not store.match("webfetch")


def test_grants_persist_across_reopen(tmp_path):
    db = str(tmp_path / "app.db")
    GrantStore(db).add("external_directory", "/home/x/proj")
    assert GrantStore(db).match("external_directory", filepath="/home/x/proj/a")


def test_duplicate_add_is_idempotent(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("bash", "*")
    store.add("bash", "*")
    assert len(store.list()) == 1


def test_derive_pattern():
    assert derive_pattern({"filepath": "/tmp/x/a/b.txt"}) == "/tmp/x/a"
    assert derive_pattern({"command": "ls"}) == "*"
    assert derive_pattern(None) == "*"


def test_grant_lifetime_dimensions_are_not_ambiguous():
    assert grant_scope_for_lifetime(
        "chat",
        session_id="chat-1",
        workspace="/work",
        workspace_id="workspace-a",
    ) == ("chat-1", "", "workspace-a")
    assert grant_scope_for_lifetime("chat", workspace="/work") == ("", "", "")
    assert grant_scope_for_lifetime(
        "workspace",
        session_id="chat-1",
        workspace="/work",
        workspace_id="workspace-a",
    ) == ("", "", "workspace-a")
    assert grant_scope_for_lifetime(
        "workspace", workspace="/work"
    ) == ("", "", "")
    assert grant_scope_for_lifetime(
        "always", session_id="chat-1", workspace="/work"
    ) == ("", "", "")


def test_stable_workspace_grants_do_not_follow_paths(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add(
        "edit",
        "*",
        owner="alice",
        workspace_id="workspace-a",
    )
    assert store.match(
        "edit", owner="alice", workspace="/same", workspace_id="workspace-a"
    )
    assert not store.match(
        "edit", owner="alice", workspace="/same", workspace_id="workspace-b"
    )
    assert not store.match("edit", owner="alice", workspace="/same")


def test_stable_caller_does_not_inherit_legacy_path_grant(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("edit", "*", owner="alice", workspace="/same")
    assert store.match("edit", owner="alice", workspace="/same")
    assert not store.match(
        "edit", owner="alice", workspace="/same", workspace_id="workspace-new"
    )


def test_workspace_id_schema_migration_is_no_broadening_and_idempotent(tmp_path):
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE permission_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                permission_type TEXT NOT NULL,
                pattern TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT '',
                resource TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT,
                revoked_at TEXT,
                UNIQUE(owner,session_id,permission_type,pattern,workspace,resource)
            );
            INSERT INTO permission_grants
                (id,owner,session_id,permission_type,pattern,workspace,resource,revoked_at)
            VALUES
                (1,'alice','','edit','*','/workspace','',NULL),
                (2,'alice','chat-1','edit','*','/workspace','',NULL),
                (3,'alice','','edit','*','','',NULL),
                (4,'alice','','edit','*','/revoked','','2026-01-01');
            """
        )
    GrantStore(str(db))
    GrantStore(str(db))
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT id,session_id,workspace,workspace_id,revoked_at "
            "FROM permission_grants ORDER BY id"
        ).fetchall()
    assert rows[0][4] is not None  # raw Workspace lifetime is disabled
    assert rows[1][4] is None  # legacy chat keeps its chat+path constraint
    assert rows[2][4] is None  # account-wide Always stays account-wide
    assert rows[3][4] == "2026-01-01"  # already revoked stays revoked
    assert all(row[3] == "" for row in rows)


def test_dual_workspace_dimensions_are_rejected(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    with pytest.raises(ValueError):
        store.add(
            "edit",
            "*",
            owner="alice",
            workspace="/work",
            workspace_id="workspace-a",
        )


def test_concurrent_schema_initialization_is_serialized(tmp_path):
    db = str(tmp_path / "app.db")
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE permission_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                permission_type TEXT NOT NULL,
                pattern TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT '',
                resource TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT,
                revoked_at TEXT,
                UNIQUE(owner,session_id,permission_type,pattern,workspace,resource)
            );
            INSERT INTO permission_grants
                (owner,session_id,permission_type,pattern,workspace)
            VALUES ('alice','chat-a','edit','*','/work');
            """
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _index: GrantStore(db), range(24)))
    assert len(stores) == 24
    with sqlite3.connect(db) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(permission_grants)"
            )
        }
    assert "workspace_id" in columns
    assert GrantStore(db).list_records(owner="alice")[0]["session_id"] == "chat-a"


async def test_reset_scope_rejects_pending_requests():
    handler = PermissionHandler()
    chat_request = PermissionRequest("chat", {}, {}, [], "edit", odysseus_session_id="s1", workspace="/work")
    other_request = PermissionRequest("other", {}, {}, [], "edit", odysseus_session_id="s2", workspace="/work")
    handler.pending_requests.update({"chat": chat_request, "other": other_request})

    assert handler.reject_scope(session_id="s1") == 1
    assert chat_request._future.result() == "reject"
    assert not other_request._future.done()
    assert handler.reject_scope(workspace="/work") == 1
    assert other_request._future.result() == "reject"

    first = PermissionRequest("first", {}, {}, [], "edit", odysseus_session_id="s3", workspace="/a")
    second = PermissionRequest("second", {}, {}, [], "edit", odysseus_session_id="s4", workspace="/b")
    handler.pending_requests.update({"first": first, "second": second})
    assert handler.reject_scope(all_pending=True) == 2
    assert first._future.result() == second._future.result() == "reject"

    stable = PermissionRequest(
        "stable",
        {},
        {},
        [],
        "edit",
        odysseus_session_id="s5",
        workspace="/same",
        authority_workspace_id="workspace-a",
    )
    other_stable = PermissionRequest(
        "other-stable",
        {},
        {},
        [],
        "edit",
        odysseus_session_id="s6",
        workspace="/same",
        authority_workspace_id="workspace-b",
    )
    handler.pending_requests.update(
        {"stable": stable, "other-stable": other_stable}
    )
    assert handler.reject_scope(authority_workspace_id="workspace-a") == 1
    assert stable._future.result() == "reject"
    assert not other_stable._future.done()


async def test_workspace_reset_uses_owned_stable_id_and_all_pending_domains(
    monkeypatch,
):
    import routes.chat_routes as chat_routes
    import src.agent_tools.filesystem_tools as file_tools
    import src.shell_policy as shell_policy

    revoked = []
    rejected = []

    class Store:
        def revoke_scope(self, **scope):
            revoked.append(scope)
            return 2

    class Handler:
        def reject_scope(self, **scope):
            rejected.append(("acp", scope))
            return 1

    class Supervisor:
        def grant_store_for(self, _owner):
            return Store()

        def permission_handler_for(self, _owner):
            return Handler()

    class Repository:
        def get_workspace(self, workspace_id):
            assert workspace_id == "workspace-a"
            return SimpleNamespace(
                id=workspace_id,
                owner_subject_id="account-alice",
                location_id="location-a",
                relative_folder="nested",
                archived=True,
            )

        def get_location(self, location_id):
            assert location_id == "location-a"
            return SimpleNamespace(canonical_path="/host")

    class Request:
        app = SimpleNamespace(state=SimpleNamespace(
            auth_manager=SimpleNamespace(
                account_id=lambda owner: "account-alice" if owner == "alice" else None
            ),
            mimo_supervisor=Supervisor(),
        ))

        async def json(self):
            return {"scope": "workspace", "workspace_id": "workspace-a"}

    monkeypatch.setattr(chat_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(chat_routes, "FilePolicyRepository", Repository)
    monkeypatch.setattr(
        file_tools,
        "reject_file_approval_scope",
        lambda **scope: rejected.append(("file", scope)) or 3,
    )
    monkeypatch.setattr(
        shell_policy,
        "reject_shell_approval_scope",
        lambda **scope: rejected.append(("shell", scope)) or 4,
    )
    router = chat_routes.setup_chat_routes(
        None, None, None, None, None, None
    )
    endpoint = _route_handler(
        router, "/api/mimo/permission-grants/reset", "POST"
    )
    result = await endpoint(Request())

    assert result["revoked"] == 2
    assert result["pending_rejected"] == 8
    assert revoked == [{
        "owner": "alice",
        "workspace": "/host/nested",
        "workspace_id": "workspace-a",
    }]
    assert rejected == [
        ("acp", {
            "workspace": "/host/nested",
            "authority_workspace_id": "workspace-a",
        }),
        ("file", {
            "owner": "alice",
            "workspace": "/host/nested",
            "authority_workspace_id": "workspace-a",
        }),
        ("shell", {
            "owner": "alice",
            "workspace": "/host/nested",
            "authority_workspace_id": "workspace-a",
        }),
    ]


async def test_workspace_reset_hides_foreign_stable_id(monkeypatch):
    import routes.chat_routes as chat_routes
    from fastapi import HTTPException

    class Repository:
        def get_workspace(self, _workspace_id):
            return SimpleNamespace(
                owner_subject_id="account-bob",
                location_id="location-b",
                relative_folder="",
            )

    class Request:
        app = SimpleNamespace(state=SimpleNamespace(
            auth_manager=SimpleNamespace(account_id=lambda _owner: "account-alice"),
            mimo_supervisor=SimpleNamespace(
                grant_store_for=lambda _owner: SimpleNamespace()
            ),
        ))

        async def json(self):
            return {"scope": "workspace", "workspace_id": "workspace-b"}

    monkeypatch.setattr(chat_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(chat_routes, "FilePolicyRepository", Repository)
    endpoint = _route_handler(
        chat_routes.setup_chat_routes(None, None, None, None, None, None),
        "/api/mimo/permission-grants/reset",
        "POST",
    )
    with pytest.raises(HTTPException) as denied:
        await endpoint(Request())
    assert denied.value.status_code == 404


def test_grants_are_owner_scoped_expirable_and_revocable(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("bash", "*", owner="alice", workspace="/work")
    assert store.match("bash", owner="alice", workspace="/work")
    assert not store.match("bash", owner="bob", workspace="/work")
    record = store.list_records(owner="alice")[0]
    assert store.revoke(record["id"], owner="alice")
    assert not store.match("bash", owner="alice", workspace="/work")

    store.add(
        "edit",
        "*",
        owner="alice",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    assert not store.match("edit", owner="alice")


async def test_incognito_and_foreign_owner_permissions_fail_closed(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("bash", "*", owner="alice", workspace="/work")
    handler = PermissionHandler(grant_store=store)
    contexts = {
        "incog": {"owner": "alice", "workspace": "/work", "incognito": True, "is_admin": True},
        "bob": {"owner": "bob", "workspace": "/work", "incognito": False, "is_admin": False},
    }
    handler.set_context_resolver(lambda session_id: contexts[session_id])

    incognito = await handler.handle(_params(title="bash", session_id="incog"))
    foreign = await handler.handle(_params(title="bash", session_id="bob"))
    assert incognito["outcome"]["optionId"] == "reject"
    assert foreign["outcome"]["optionId"] == "reject"


# ── PermissionHandler flow ──


async def test_stored_grant_auto_approves_without_prompting(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    store.add("external_directory", "/home/x/proj")
    surfaced = []

    handler = PermissionHandler(grant_store=store)
    handler.on_request(lambda req: surfaced.append(req))

    result = await handler.handle(
        _params(raw_input={"filepath": "/home/x/proj/notes.md"})
    )
    assert result == {"outcome": {"outcome": "selected", "optionId": "always"}}
    assert not surfaced
    assert not handler.pending_requests


async def test_safe_dirs_still_auto_approve(tmp_path):
    handler = PermissionHandler(
        safe_dirs=["/home/x/safe"], grant_store=GrantStore(str(tmp_path / "a.db"))
    )
    result = await handler.handle(
        _params(raw_input={"filepath": "/home/x/safe/f.txt"})
    )
    assert result["outcome"]["optionId"] == "always"


async def test_selected_workspace_does_not_auto_approve_operation(tmp_path):
    handler = PermissionHandler(
        safe_dirs=["/home/x/work"],
        grant_store=GrantStore(str(tmp_path / "a.db")),
    )
    surfaced = []
    handler.set_context_resolver(lambda _session_id: {
        "owner": "alice",
        "odysseus_session_id": "chat-a",
        "workspace": "/home/x/work",
        "authority_workspace_id": "workspace-a",
        "is_admin": True,
        "incognito": True,
    })

    async def on_request(request):
        surfaced.append(request)
        request.resolve("reject")

    handler.on_request(on_request)
    result = await handler.handle(_params(
        raw_input={"filepath": "/home/x/work/file.txt"}
    ))
    assert result["outcome"]["optionId"] == "reject"
    assert len(surfaced) == 1


async def test_prompt_once_resolves_and_writes_no_grant(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    handler = PermissionHandler(grant_store=store)
    surfaced: list[PermissionRequest] = []

    async def on_req(req):
        surfaced.append(req)

    handler.on_request(on_req)
    task = asyncio.ensure_future(
        handler.handle(_params(raw_input={"filepath": "/home/x/proj/f.txt"}))
    )
    while not surfaced:
        await asyncio.sleep(0.01)

    assert handler.resolve(surfaced[0].request_id, "once")
    result = await task
    assert result["outcome"]["optionId"] == "once"
    assert store.list() == []


async def test_prompt_always_writes_subtree_grant_and_skips_next_prompt(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    handler = PermissionHandler(grant_store=store)
    surfaced: list[PermissionRequest] = []

    async def on_req(req):
        surfaced.append(req)

    handler.on_request(on_req)
    task = asyncio.ensure_future(
        handler.handle(_params(title="edit", raw_input={"filepath": "/home/x/proj/f.txt"}))
    )
    while not surfaced:
        await asyncio.sleep(0.01)
    handler.resolve(surfaced[0].request_id, "always")
    result = await task
    assert result["outcome"]["optionId"] == "always"
    assert store.match("edit", filepath="/home/x/proj/other.txt")

    # second request in the same subtree: no prompt at all
    result2 = await handler.handle(
        _params(title="edit", raw_input={"filepath": "/home/x/proj/deeper/g.txt"})
    )
    assert result2["outcome"]["optionId"] == "always"
    assert len(surfaced) == 1


async def test_prompt_always_on_non_file_type_writes_wildcard_grant(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    handler = PermissionHandler(grant_store=store)
    surfaced = []

    async def on_req(req):
        surfaced.append(req)

    handler.on_request(on_req)
    task = asyncio.ensure_future(
        handler.handle(_params(title="bash", raw_input={"command": "ls -la"}))
    )
    while not surfaced:
        await asyncio.sleep(0.01)
    handler.resolve(surfaced[0].request_id, "always")
    await task
    assert store.match("bash")


async def test_prompt_reject_writes_no_grant(tmp_path):
    store = GrantStore(str(tmp_path / "app.db"))
    handler = PermissionHandler(grant_store=store)
    surfaced = []

    async def on_req(req):
        surfaced.append(req)

    handler.on_request(on_req)
    task = asyncio.ensure_future(
        handler.handle(_params(title="bash", raw_input={"command": "rm -rf /"}))
    )
    while not surfaced:
        await asyncio.sleep(0.01)
    handler.resolve(surfaced[0].request_id, "reject")
    result = await task
    assert result["outcome"]["optionId"] == "reject"
    assert store.list() == []


async def test_prompt_waits_with_no_timeout():
    """e's ruling: no auto-reject. The wait default must be None (forever),
    and a pending request must still be pending after a real delay."""
    assert inspect.signature(PermissionRequest.wait).parameters["timeout"].default is None

    handler = PermissionHandler()
    surfaced = []

    async def on_req(req):
        surfaced.append(req)

    handler.on_request(on_req)
    task = asyncio.ensure_future(handler.handle(_params(title="bash")))
    while not surfaced:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    assert not task.done()
    assert surfaced[0].request_id in handler.pending_requests

    handler.resolve(surfaced[0].request_id, "reject")
    await task


async def test_unsurfaceable_request_fails_safe_to_reject():
    """No UI callback registered -> reject immediately instead of hanging
    forever on a prompt nobody can see."""
    handler = PermissionHandler()
    result = await handler.handle(_params(title="bash"))
    assert result["outcome"]["optionId"] == "reject"


async def test_surface_callback_error_fails_safe_to_reject():
    handler = PermissionHandler()

    async def broken(req):
        raise RuntimeError("no active turn")

    handler.on_request(broken)
    result = await handler.handle(_params(title="bash"))
    assert result["outcome"]["optionId"] == "reject"
    assert not handler.pending_requests


# ── Bridge surfacing: permission request rides the turn's SSE stream ──


class _FakeClient:
    def __init__(self):
        self.callbacks = {}

    def register_callback(self, method, fn):
        self.callbacks[method] = fn

    def on_session_update(self, fn):
        self._on_update = fn


async def test_bridge_emits_permission_request_sse(tmp_path):
    import json
    from src.openclank.acp_bridge import ACPBridge, _TurnState

    handler = PermissionHandler(grant_store=GrantStore(str(tmp_path / "a.db")))
    bridge = ACPBridge(_FakeClient(), cwd="/tmp", permission_handler=handler)

    # simulate an active turn for the mimo session
    q: asyncio.Queue = asyncio.Queue()
    bridge._queues["mimo_sess_1"] = q

    task = asyncio.ensure_future(
        handler.handle(_params(raw_input={"filepath": "/home/x/proj/f.txt"}))
    )
    update = await asyncio.wait_for(q.get(), timeout=2.0)

    chunks = bridge._process_update("mimo_sess_1", update, _TurnState())
    assert len(chunks) == 1
    payload = json.loads(chunks[0][len("data: "):])
    assert payload["type"] == "permission_request"
    data = payload["data"]
    assert data["permission_type"] == "external_directory"
    assert data["request_id"]
    assert data["always_pattern"] == "/home/x/proj"
    assert {o["optionId"] for o in data["options"]} == {"once", "always", "reject"}

    handler.resolve(data["request_id"], "reject")
    await task
