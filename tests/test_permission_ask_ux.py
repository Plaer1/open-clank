"""C1 — permission ask UX: durable grants + no-timeout prompt flow.

e's rulings (2026-07-09): durable always-allow grants surviving restarts;
file grants cover the directory subtree; ALL permission types route through
safe-dirs -> stored grants -> prompt; the prompt waits forever (no 300s
auto-reject). mimo's optionIds are exactly 'once' | 'always' | 'reject'
(packages/opencode/src/acp/agent.ts:147-150).
"""

import asyncio
import inspect

import pytest

from src.openclank.permission_grants import GrantStore, derive_pattern
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
