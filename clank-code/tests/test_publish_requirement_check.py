"""publish_file vs the standing-guidance publish requirement (C3-P3). Plain asserts, fakes only."""
import asyncio
import contextlib
import json
import os
import sys
import types
from unittest import mock

# This is a plain-runner file (not pytest), so tests/conftest.py's legacy default never applies to it. `import app`
# below runs init_components() at import time, which now (G-DEF) resolves the memory store profile when
# OPENCLANK_MEMORY_STORE_PROFILE is unset; default it explicitly to the historical legacy profile so that first
# import does not prepare a fresh guarded store against this process's FM_DB_PATH.
os.environ.setdefault("OPENCLANK_MEMORY_STORE_PROFILE", "legacy-v16")

DENY = {"version": 1, "effect": "publish_file", "deny": {"audience": "public"}}
IDENT = {"sessionID": "s", "rootOperationID": "r", "actorID": "main", "executionRevision": 1, "innerTurnID": "i"}
LEGACY = {"version": 1, "store": "none", "deny_public": False, "receipt": ""}
ALLOW = {"version": 1, "deny_public": False, "receipt": ""}


def run(c):
    return asyncio.run(c)


class FakeService:
    calls = []
    storage_root = "/tmp/fake-published-root"

    def publish(self, path, *, owner, audience, expires_in_hours, public_origin):
        FakeService.calls.append((path, owner, audience))
        return {"id": "f1", "filename": "a.txt", "download_url": "/d/f1", "audience": audience, "expires_at": None}


@contextlib.contextmanager
def publishing():
    import src.published_files as pf
    import src.tool_execution as te
    FakeService.calls = []
    with mock.patch.object(pf, "PublishedFileService", FakeService), \
            mock.patch.object(te, "_resolve_tool_path", lambda p: p), \
            mock.patch.object(te, "get_active_workspace", lambda: None):
        yield


def publish(args, checker="absent"):
    from src.agent_tools.filesystem_tools import PublishFileTool
    ctx = {"owner": "alice"}
    if checker != "absent":
        ctx["publish_requirement_check"] = checker
    with publishing():
        return run(PublishFileTool().execute(json.dumps({"path": "/tmp/x.txt", **args}), ctx))


def counting(answer):
    calls = []

    async def check():
        calls.append(1)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return check, calls


def test_legacy_store_publishes_and_says_so():
    check, calls = counting(LEGACY)
    r = publish({"audience": "public"}, check)
    assert r["exit_code"] == 0 and r["requirement_check"] == "no_requirement_store" and len(calls) == 1
    assert len(FakeService.calls) == 1


def test_guarded_deny_is_refused_with_exact_shape_and_nothing_published():
    check, calls = counting({"version": 1, "deny_public": True, "receipt": "abcdef012345"})
    r = publish({"audience": "public"}, check)
    assert r == {
        "error": "Public link blocked by your standing requirement abcdef012345. Ask the user with the question tool whether to share it another way or to change the requirement.",
        "outcome": "blocked_by_requirement",
        "requirement": {"effect": "publish_file", "deny": {"audience": "public"}, "receipt": "abcdef012345"},
        "exit_code": 1,
    }
    assert FakeService.calls == []
    assert "retry" not in r["error"].lower() and "owner" not in r["error"].lower()


def test_guarded_allow_publishes_once():
    check, calls = counting(ALLOW)
    r = publish({"audience": "public"}, check)
    assert r["exit_code"] == 0 and "requirement_check" not in r
    assert len(FakeService.calls) == 1 and len(calls) == 1


def test_unavailable_checker_refuses_on_every_shape():
    bad = [RuntimeError("boom"), None, {}, "x", {"version": 2, "deny_public": False, "receipt": ""},
           {"version": 1, "deny_public": "no", "receipt": ""}, {"version": 1, "deny_public": False, "receipt": 5},
           {"version": 1, "deny_public": False, "receipt": "", "extra": 1},
           {"version": 1, "store": "none", "deny_public": True, "receipt": ""},
           {"version": 1, "store": "other", "deny_public": False, "receipt": ""}]
    for answer in bad:
        check, _ = counting(answer)
        r = publish({"audience": "public"}, check)
        assert r["outcome"] == "requirement_check_unavailable" and r["exit_code"] == 1, answer
        assert FakeService.calls == [], answer
    r = publish({"audience": "public"})  # no checker in ctx
    assert r["outcome"] == "requirement_check_unavailable" and FakeService.calls == []
    r = publish({"audience": "public"}, "not callable")
    assert r["outcome"] == "requirement_check_unavailable" and FakeService.calls == []


def test_owner_audience_never_calls_the_checker():
    for args in ({}, {"audience": "owner"}, {"audience": None}):
        check, calls = counting(RuntimeError("must not run"))
        r = publish(args, check)
        assert r["exit_code"] == 0 and calls == [] and "requirement_check" not in r
        assert publish(args)["exit_code"] == 0  # no checker at all is fine for owner


def test_public_spelling_is_normalised_like_the_service():
    for spelling in ("PUBLIC", " public "):
        check, calls = counting({"version": 1, "deny_public": True, "receipt": "r"})
        r = publish({"audience": spelling}, check)
        assert r["outcome"] == "blocked_by_requirement" and len(calls) == 1 and FakeService.calls == []


def test_child_broker_client_fails_closed_and_tool_refuses_on_any_host():
    from src.openclank import lifetools_server as lt
    for k in ("OPEN_CLANK_MEMORY_BROKER_URL", "OPEN_CLANK_MEMORY_BROKER_TOKEN"):
        os.environ.pop(k, None)
    assert run(lt._publish_requirement_check(IDENT)) is None
    assert run(lt._publish_requirement_check(None)) is None

    async def check():
        return await lt._publish_requirement_check(IDENT)
    r = publish({"audience": "public"}, check)  # even a legacy-configured Host: the child cannot tell
    assert r["outcome"] == "requirement_check_unavailable" and FakeService.calls == []


def test_child_client_validates_reply_shape():
    from src.openclank import lifetools_server as lt
    os.environ["OPEN_CLANK_MEMORY_BROKER_URL"] = "http://127.0.0.1:1/x"
    os.environ["OPEN_CLANK_MEMORY_BROKER_TOKEN"] = "t"
    sent = []

    class Resp:
        status_code = 200

        def __init__(self, body):
            self.body = body
            self.content = json.dumps(body).encode()

        def json(self):
            return self.body

    def client_for(body):
        class C:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                sent.append(json)
                return Resp(body)
        return C
    import httpx
    try:
        good = {"version": 1, "identity": IDENT, "deny_public": True, "receipt": "abc"}
        with mock.patch.object(httpx, "AsyncClient", client_for(good)):
            assert run(lt._publish_requirement_check(IDENT)) == {"version": 1, "deny_public": True, "receipt": "abc"}
        assert sent[0] == {"name": "publish_requirement_check", "arguments": {"identity": IDENT}}
        for bad in ({**good, "identity": {**IDENT, "innerTurnID": "z"}}, {**good, "version": 2},
                    {"version": 1, "identity": IDENT, "deny_public": True}, {**good, "extra": 1}):
            with mock.patch.object(httpx, "AsyncClient", client_for(bad)):
                assert run(lt._publish_requirement_check(IDENT)) is None
    finally:
        os.environ.pop("OPEN_CLANK_MEMORY_BROKER_URL"); os.environ.pop("OPEN_CLANK_MEMORY_BROKER_TOKEN")


def test_in_process_path_checks_by_owner_through_execute_tool_block():
    import src.tool_execution as te
    from src.agent_tools import ToolBlock
    seen = []

    async def hook(owner):
        seen.append(owner)
        return {"version": 1, "deny_public": True, "receipt": "hostreceipt1"}

    ident = mock.patch.object(te, "_copal_account_id", lambda owner: (owner, "acct-" + str(owner), None))
    ident.start()

    async def go():
        block = ToolBlock(tool_type="publish_file", content=json.dumps({"path": "/tmp/x.txt", "audience": "public"}))
        return await te.execute_tool_block(block, session_id="s", owner="alice")
    with publishing(), mock.patch.object(te, "HOST_PUBLISH_REQUIREMENT_CHECK", hook):
        _desc, result = run(go())
    assert seen == ["alice"] and result["outcome"] == "blocked_by_requirement", result
    assert FakeService.calls == []
    # with no hook and no contextvar a public publish is refused, an owner publish is not
    with publishing(), mock.patch.object(te, "HOST_PUBLISH_REQUIREMENT_CHECK", None):
        _desc, result = run(go())
        assert result["outcome"] == "requirement_check_unavailable"
    # the child contextvar wins over the hook
    async def child():
        return {"version": 1, "deny_public": False, "receipt": ""}
    token = te.PUBLISH_REQUIREMENT_CHECKER.set(child)
    try:
        with publishing(), mock.patch.object(te, "HOST_PUBLISH_REQUIREMENT_CHECK", hook):
            seen.clear()
            _desc, result = run(go())
        assert seen == [] and result["exit_code"] == 0 and len(FakeService.calls) == 1, result
    finally:
        te.PUBLISH_REQUIREMENT_CHECKER.reset(token)
        ident.stop()


# ---- Host side (imports the real app module; no lifespan) ----

class Env:
    def __init__(self, profile):
        self.profile = profile

    def __enter__(self):
        self.old = os.environ.get("OPENCLANK_MEMORY_STORE_PROFILE")
        if self.profile is None:
            os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
        else:
            os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = self.profile

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
        else:
            os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = self.old


def _item(n, requirement=None):
    text = f"rule {n}"
    import hashlib
    item = {"curated_id": f"c{n}", "block_id": f"b{n}", "revision": 1,
            "receipt_id": "cs_s10_purpose_" + (f"{n:02d}abcdef0123456789" + "0" * 46)[:64],
            "receipt_sha256": "a" * 64, "value_sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text}
    if requirement is not None:
        item["requirement"] = requirement
    return item


def _host(items):
    import app
    import src.openclank.chat_source_bridge as csb
    import src.openclank.memory_job_admission as mja
    import src.openclank.memory_policy_projection as mpp
    calls = []

    class Provider:
        _workspace_id = "ws"

        def _scope(self, owner):
            return types.SimpleNamespace(owner="mem-" + owner)

        async def _host_standing_guidance_select(self, **kw):
            calls.append(kw)
            if isinstance(items, Exception):
                raise items
            return {"items": items}

    provider = Provider()

    class Target:
        def __init__(self, auth_manager, supervisor): pass
        def resolve(self, account):
            return types.SimpleNamespace(memory_owner="mem-alice", memory_workspace="ws")

    class Coordinator:
        def checked_refs(self, owner, account, workspace):
            calls.append(("refs", owner, account, workspace))
            return {"account": {}, "global": {}}

    auth = types.SimpleNamespace(users={"alice": {}}, account_id=lambda o: "acct-" + o if o == "alice" else None,
                                 username_for_account_id=lambda a: a[5:])
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(app, "auth_manager", auth))
    stack.enter_context(mock.patch.object(app, "memory_provider", provider))
    stack.enter_context(mock.patch.object(app, "_owner_request_fenced", lambda o: False))
    stack.enter_context(mock.patch.object(app.app.state, "mimo_supervisor", types.SimpleNamespace(owner_lifecycle_pool=lambda: None), create=True))
    stack.enter_context(mock.patch.object(csb, "canonical_source_provider", lambda state: provider))
    stack.enter_context(mock.patch.object(mja, "TargetAuthority", Target))
    stack.enter_context(mock.patch.object(mpp, "policy_coordinator", lambda: Coordinator()))
    return app, stack, calls


def test_host_function_legacy_profile_answers_none_without_touching_the_store():
    import app
    for profile in (None, "legacy-v16"):
        with Env(profile):
            assert run(app._publish_requirement_answer("alice")) == LEGACY
    assert app._memory_requirement_store_present() is False
    with Env("guarded-v17"):
        assert app._memory_requirement_store_present() is True
    with Env("guarded-v18"):
        assert app._memory_requirement_store_present() is True


def test_host_function_current_items_decide_deny_public_and_receipt():
    with Env("guarded-v17"):
        for items, deny, receipt in (
            ([], False, ""),
            ([_item(1)], False, ""),
            ([_item(1), _item(2, DENY)], True, "02abcdef0123"),
            ([_item(3, DENY)], True, "03abcdef0123"),
        ):
            app, stack, calls = _host(items)
            with stack:
                ans = run(app._publish_requirement_answer("alice"))
            assert ans == {"version": 1, "deny_public": deny, "receipt": receipt}, (items, ans)
            select = [c for c in calls if isinstance(c, dict)][0]
            assert select["owner"] == "mem-alice" and select["account_id"] == "acct-alice" and select["workspace_id"] == "ws"
        # select failure on a guarded store: raises, never an allow
        app, stack, calls = _host(RuntimeError("native down"))
        with stack:
            try:
                run(app._publish_requirement_answer("alice"))
                raise AssertionError("must raise")
            except RuntimeError:
                pass
        # unknown owner
        app, stack, calls = _host([])
        with stack:
            try:
                run(app._publish_requirement_answer("mallory"))
                raise AssertionError("must raise")
            except ValueError:
                pass


def test_host_inprocess_checker_is_registered_and_checks_by_owner():
    import src.tool_execution as te
    app, stack, calls = _host([_item(2, DENY)])

    @contextlib.asynccontextmanager
    async def admitted(owner):
        assert owner == "alice"
        yield
    with Env("guarded-v18"), stack, mock.patch.object(app, "_account_request_barrier", types.SimpleNamespace(admitted=admitted)):
        assert te.HOST_PUBLISH_REQUIREMENT_CHECK is app._publish_requirement_inprocess
        ans = run(te.HOST_PUBLISH_REQUIREMENT_CHECK("alice"))
    assert ans["deny_public"] is True and ans["receipt"] == "02abcdef0123"


def _op(arguments, *, resolver, answer, profile="guarded-v17", owner="alice"):
    """Drive the real broker op with a fake bridge and a fake answer function."""
    import app
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_OWNER_HEADER, INTERNAL_TOOL_WORKSPACE_HEADER
    from src.openclank.acp_bridge import memery_broker_token

    class Bridge:
        async def resolve_managed_control_turn(self, identity, tool_name="control_desktop"):
            return await resolver(identity, tool_name)
    bridge = Bridge()
    worker = types.SimpleNamespace(bridge=bridge, is_alive=lambda: True)
    supervisor = types.SimpleNamespace(worker_for_owner=lambda o: worker)
    request = types.SimpleNamespace(
        headers={INTERNAL_TOOL_HEADER: memery_broker_token(app.INTERNAL_TOOL_TOKEN, owner, "ws"),
                 INTERNAL_TOOL_OWNER_HEADER: owner, INTERNAL_TOOL_WORKSPACE_HEADER: "ws"},
        app=types.SimpleNamespace(state=types.SimpleNamespace(mimo_supervisor=supervisor)))

    @contextlib.asynccontextmanager
    async def admitted(o):
        yield
    auth = types.SimpleNamespace(users={"alice": {}}, account_id=lambda o: "acct-alice",
                                 username_for_account_id=lambda a: "alice")
    with Env(profile), mock.patch.object(app, "_is_trusted_loopback", lambda r: True), \
            mock.patch.object(app, "auth_manager", auth), \
            mock.patch.object(app, "_account_request_barrier", types.SimpleNamespace(admitted=admitted)), \
            mock.patch.object(app, "_owner_request_fenced", lambda o: False), \
            mock.patch.object(app, "_publish_requirement_answer", answer):
        return run(app.internal_memery_tool(request, {"name": "publish_requirement_check", "arguments": arguments}))


def _http_status(arguments, **kw):
    from fastapi import HTTPException
    try:
        _op(arguments, **kw)
    except HTTPException as exc:
        return exc.status_code
    return None


def test_broker_op_answers_with_resolver_called_as_publish_file_for_main_and_subagent():
    seen = []

    async def resolver(identity, tool_name):
        seen.append((identity["actorID"], tool_name))
        return {"owner": "alice", "ownerAccountID": "acct-alice"}

    async def answer(owner):
        return {"version": 1, "deny_public": True, "receipt": "abc"}
    for actor in ("main", "sub-agent-1"):
        ident = {**IDENT, "actorID": actor}
        r = _op({"identity": ident}, resolver=resolver, answer=answer)
        assert r == {"identity": ident, "version": 1, "deny_public": True, "receipt": "abc"}
    assert seen == [("main", "publish_file"), ("sub-agent-1", "publish_file")]


def test_broker_op_legacy_profile_answers_store_none():
    async def resolver(identity, tool_name):
        return {"owner": "alice", "ownerAccountID": "acct-alice"}
    import app
    real = app._publish_requirement_answer

    async def real_answer(owner):
        return await real(owner)
    r = _op({"identity": IDENT}, resolver=resolver, answer=real_answer, profile="legacy-v16")
    assert r["store"] == "none" and r["deny_public"] is False and r["receipt"] == "" and r["version"] == 1


def test_broker_op_refuses_malformed_and_unresolvable_identity():
    async def good(identity, tool_name):
        return {"owner": "alice", "ownerAccountID": "acct-alice"}

    async def rejects(identity, tool_name):
        raise ValueError("control turn unavailable")

    async def other_owner(identity, tool_name):
        return {"owner": "bob", "ownerAccountID": "acct-bob"}

    called = []

    async def answer(owner):
        called.append(owner)
        return {"version": 1, "deny_public": False, "receipt": ""}
    for bad in (None, [], {}, {"identity": "x"}, {"identity": IDENT, "extra": 1}, {"identity": IDENT, "toolName": "x"}):
        assert _http_status(bad, resolver=good, answer=answer) == 400, bad
    assert _http_status({"identity": IDENT}, resolver=rejects, answer=answer) == 409
    assert _http_status({"identity": IDENT}, resolver=other_owner, answer=answer) == 409
    assert _http_status({"identity": IDENT}, resolver=good, answer=answer, profile="legacy-v16") is None
    assert called == ["alice"]  # only the valid call reached the check

    async def failing(owner):
        raise RuntimeError("select down")
    assert _http_status({"identity": IDENT}, resolver=good, answer=failing) == 409
