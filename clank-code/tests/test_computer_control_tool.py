"""Control tool against a FAKE helper (a tiny Python script). The real helper is never resolved or run."""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from src.openclank.computer_control import linux as L
from src.openclank.computer_control import protocol as P

FAKE = r'''
import json, os, sys
d = os.environ["FAKE_DIR"]; mode = os.environ.get("FAKE_MODE", "")
open(d + "/pid", "w").write(str(os.getpid()))
def out(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    open(d + "/log", "a").write(line)
    r = json.loads(line)
    if r["op"] == "begin":
        out({"ok": True, "state": "active", "stream": {"width": 100, "height": 50}})
        if mode == "exit_after_begin": sys.exit(0)
    elif r["op"] == "stop":
        out({"ok": True, "state": "stopped"}); sys.exit(0)
    elif mode == "unknown":
        out({"ok": False, "code": "outcome_unknown"})
    else:
        out({"ok": True, "outcome": "sent", "actions_left": 19})
'''
FRAME = {"width": 100, "height": 50}
BEGIN = {"action": "begin"}
CLICK = {"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 10, "y": 20, "frame": FRAME}
KEY = {"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["Control", "s"]}


class Env:
    def __init__(self, mode="", idle=30.0):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "fake.py").write_text(FAKE)
        os.environ["FAKE_DIR"], os.environ["FAKE_MODE"] = str(self.dir), mode
        L._helper_argv = lambda: [sys.executable, "-B", str(self.dir / "fake.py")]
        L._platform_ok = lambda: True
        L.S = L._Session()
        L.IDLE_S = idle
        self.asked = []
        self.allow = True

    async def approve(self, digest):
        self.asked.append(digest)
        if self.allow == "raise":
            raise RuntimeError("broker down")
        return bool(self.allow)

    def lines(self):
        p = self.dir / "log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def pid(self):
        p = self.dir / "pid"
        return int(p.read_text()) if p.exists() else None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def run(coro):
    return asyncio.run(coro)


def test_no_approval_nothing_started_or_written():
    for allow in (False, "raise"):
        e = Env()
        e.allow = allow
        for call in (BEGIN, CLICK, KEY, {"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": "x"}):
            r = run(L.control_action(call, e.approve))
            assert r["outcome"] in ("approval_required", "unavailable") and r["ok"] is False
        assert e.pid() is None and e.lines() == []


def test_rejected_click_is_not_sent():
    async def go():
        e = Env()
        assert (await L.control_action(BEGIN, e.approve))["outcome"] == "active"
        e.allow = False
        r = await L.control_action(CLICK, e.approve)
        assert r["outcome"] == "approval_required" and r["receipt"]["human_approved"] is False
        await L.control_status({"action": "stop"})
        return e
    e = run(go())
    assert [x["op"] for x in e.lines()] == ["begin", "stop"]


def test_happy_path_one_approval_per_action():
    async def go():
        e = Env()
        r = [await L.control_action(c, e.approve) for c in (BEGIN, CLICK, KEY, {"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": "hi"})]
        s = await L.control_status({"action": "stop"})
        return e, r, s
    e, r, s = run(go())
    assert [x["outcome"] for x in r] == ["active", "sent", "sent", "sent"]
    assert "effect not verified" in r[1]["message"] and "never repeated" in r[1]["message"]
    assert all(x["receipt"]["human_approved"] for x in r)
    assert e.asked == [P.args_digest(c) for c in (BEGIN, CLICK, KEY, {"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": "hi"})]
    assert [x["op"] for x in e.lines()] == ["begin", "click", "key", "type", "stop"]
    assert e.lines()[1] == {"v": 1, "op": "click", "x": 10, "y": 20, "button": "left", "frame": FRAME}
    assert e.lines()[0]["deadline_ms"] == 120000
    assert s["outcome"] == "stopped" and not alive(e.pid())


def test_second_begin_and_frame_mismatch_refused_hostside():
    async def go():
        e = Env()
        await L.control_action(BEGIN, e.approve)
        n = len(e.asked)
        a = await L.control_action(BEGIN, e.approve)
        b = await L.control_action({**CLICK, "frame": {"width": 200, "height": 50}}, e.approve)
        c = await L.control_action({**CLICK, "x": 100}, e.approve)
        await L.control_status({"action": "stop"})
        return e, n, a, b, c
    e, n, a, b, c = run(go())
    assert a["outcome"] == "already_active" and b["outcome"] == "frame_mismatch" and c["outcome"] == "out_of_bounds"
    assert len(e.asked) == n  # refused before any approval was consumed
    assert [x["op"] for x in e.lines()] == ["begin", "stop"]


def test_refused_keys_never_ask_or_send():
    async def go():
        e = Env()
        await L.control_action(BEGIN, e.approve)
        n = len(e.asked)
        r = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["Alt", "F4"]}, e.approve)
        await L.control_status({"action": "stop"})
        return e, n, r
    e, n, r = run(go())
    assert r["outcome"] == "refused_chord" and len(e.asked) == n and [x["op"] for x in e.lines()] == ["begin", "stop"]


def test_outcome_unknown_surfaces_and_is_not_retried():
    async def go():
        e = Env(mode="unknown")
        await L.control_action(BEGIN, e.approve)
        r = await L.control_action(CLICK, e.approve)
        again = await L.control_action(CLICK, e.approve)
        return e, r, again
    e, r, again = run(go())
    assert r["outcome"] == "outcome_unknown" and "Not repeated" in r["message"]
    assert [x["op"] for x in e.lines()].count("click") == 1
    assert again["outcome"] == "unavailable" and again["reason"] == "no_session"
    assert not alive(e.pid())


def test_helper_exit_mid_session_closes_it():
    async def go():
        e = Env(mode="exit_after_begin")
        await L.control_action(BEGIN, e.approve)
        await asyncio.sleep(0.3)
        r = await L.control_action(KEY, e.approve)
        return e, r
    e, r = run(go())
    assert r["outcome"] == "unavailable" and r["reason"] == "no_session" and L.S.proc is None
    assert [x["op"] for x in e.lines()] == ["begin"]


def test_idle_timeout_kills_helper_process_group():
    async def go():
        e = Env(idle=0.3)
        await L.control_action(BEGIN, e.approve)
        pid = e.pid()
        assert alive(pid)
        await asyncio.sleep(1.0)
        return e, pid
    e, pid = run(go())
    assert not alive(pid) and L.S.proc is None


def test_stop_needs_no_approval_and_readiness_reports():
    async def go():
        e = Env()
        rd = await L.control_status({"action": "readiness"})
        await L.control_action(BEGIN, e.approve)
        n = len(e.asked)
        s = await L.control_status({"action": "stop"})
        bad = await L.control_status({"action": "begin"})
        return e, rd, n, s, bad
    e, rd, n, s, bad = run(go())
    assert rd["state"] == "unverified" and rd["wayland"] is True and len(e.asked) == n
    assert s["session_active"] is False and bad["exit_code"] == 1 and not alive(e.pid())


def test_non_wayland_is_unavailable_and_legacy_handler_never_acts():
    e = Env()
    L._platform_ok = lambda: False
    r = run(L.control_action(BEGIN, e.approve))
    assert r["outcome"] == "unavailable" and r["reason"] == "no_wayland_session" and e.asked == [] and e.pid() is None
    r = run(L.legacy_control_tool(json.dumps(BEGIN), {}))
    assert r["outcome"] == "approval_required" and r["exit_code"] == 1


def test_typed_text_never_logged():
    secret = "hunter2-SECRET-phrase"
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage() + " " + str(record.args) + " " + str(record.exc_info))

    h = H()
    root = logging.getLogger()
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        async def go():
            e = Env()
            await L.control_action(BEGIN, e.approve)
            r = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": secret}, e.approve)
            bad = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": secret + "\x1b"}, e.approve)
            await L.control_status({"action": "stop"})
            return r, bad
        r, bad = run(go())
    finally:
        root.removeHandler(h)
        root.setLevel(old)
    assert any("computer_control receipt" in x for x in records)
    assert not any(secret in x for x in records)
    assert secret not in json.dumps(r) and secret not in json.dumps(bad)
    assert r["receipt"]["chars"] == len(secret) and bad["outcome"] == "invalid_text"


def test_lifetools_broker_client_fails_closed_without_broker():
    from src.openclank import lifetools_server as lt
    ident = {"sessionID": "s", "rootOperationID": "r", "actorID": "main", "executionRevision": 1, "innerTurnID": "i"}
    for k in ("OPEN_CLANK_MEMORY_BROKER_URL", "OPEN_CLANK_MEMORY_BROKER_TOKEN"):
        os.environ.pop(k, None)
    assert run(lt._consume_control_approval(ident, "0" * 64)) is False
    assert run(lt._consume_control_approval(None, "0" * 64)) is False


def test_settings_off_refuses_with_a_typed_reason_and_starts_nothing():
    async def go():
        e = Env()
        a = await L.control_action(BEGIN, e.approve, {"control": False, "accessibility": True})
        el = {"action": "click_element", "snapshot_id": "a" * 16, "element": "0", "target": {"app": "A", "window": "W", "role": "r", "name": "n"}}
        b = await L.control_action(el, e.approve, {"control": True, "accessibility": False})
        from src.openclank.computer_control import a11y_client as C
        c = await C.inspect_action({"action": "app_state", "app": {"name": "x"}}, e.approve, {"control": True, "accessibility": False})
        h = await C.inspect_action({"action": "handshake"}, e.approve, {"control": True, "accessibility": False})
        return e, a, b, c, h
    e, a, b, c, h = run(go())
    assert (a["outcome"], a["reason"]) == ("setting_off", "desktop_control_disabled")
    assert (b["outcome"], b["reason"]) == ("setting_off", "desktop_accessibility_disabled")
    assert (c["outcome"], c["reason"]) == ("setting_off", "desktop_accessibility_disabled") and h["outcome"] == "setting_off"
    assert e.asked == [] and e.pid() is None


def test_control_runs_with_only_the_control_setting_on():
    async def go():
        e = Env()
        r = await L.control_action(BEGIN, e.approve, {"control": True, "accessibility": False})
        await L.control_status({"action": "stop"})
        return r
    assert run(go())["outcome"] == "active"


def test_stop_mid_action_releases_and_a_late_action_gets_the_typed_refusal():
    async def go():
        e = Env()
        await L.control_action(BEGIN, e.approve)

        async def stop_now():
            await L.take_over()
        e.approve_hook = stop_now
        orig = e.approve

        async def approve(digest):
            await stop_now()
            return await orig(digest)
        late = await L.control_action(CLICK, approve)         # the human pressed Stop while this waited for approval
        after = await L.control_action(KEY, e.approve)
        return e, late, after
    e, late, after = run(go())
    assert late["outcome"] == "taken_over" and [x["op"] for x in e.lines()] == ["begin", "stop"] or [x["op"] for x in e.lines()] == ["begin"]
    assert not any(x["op"] == "click" for x in e.lines())
    assert after["outcome"] == "unavailable" and after["reason"] == "no_session" and not alive(e.pid())


def test_stop_route_is_human_admin_only_and_idempotent():
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import routes.desktop_control_routes as R
    app = FastAPI()
    app.include_router(R.setup_desktop_control_routes())
    who = {"user": None}

    class Mgr:
        def is_admin(self, u): return u == "root"
    app.state.auth_manager = Mgr()

    def fake_require_user(request):
        if who["user"] == "api":
            raise HTTPException(403, "API tokens must use a scope-aware API route")
        if who["user"] is None:
            raise HTTPException(401, "Not authenticated")
        return who["user"]
    R.require_user = fake_require_user
    R.is_internal_tool_identity = lambda u: u == "internal-tool"
    c = TestClient(app)
    for u, code in ((None, 401), ("api", 403), ("internal-tool", 403), ("alice", 403)):
        who["user"] = u
        assert c.post("/api/desktop-control/stop").status_code == code, u
        assert c.get("/api/desktop-control/status").status_code == code, u
    who["user"] = "root"
    P.REGISTRY.grant("root", "s", "d" * 64)
    r1, r2 = c.post("/api/desktop-control/stop"), c.post("/api/desktop-control/stop")
    assert r1.status_code == r2.status_code == 200 and r1.json()["outcome"] == "stopped" and r1.json() == r2.json()
    assert P.REGISTRY.consume("root", "s", "d" * 64) is False
    assert c.get("/api/desktop-control/status").json()["session_active"] is False


def test_turn_cancel_takeover_only_when_something_is_running():
    async def go():
        e = Env()
        none = await L.take_over_if_active()
        await L.control_action(BEGIN, e.approve)
        some = await L.take_over_if_active()
        return e, none, some
    e, none, some = run(go())
    assert none is None and some["taken_over"] is True and not alive(e.pid())


def test_lifetools_settings_client_fails_closed_without_broker():
    from src.openclank import lifetools_server as lt
    ident = {"sessionID": "s", "rootOperationID": "r", "actorID": "main", "executionRevision": 1, "innerTurnID": "i"}
    for k in ("OPEN_CLANK_MEMORY_BROKER_URL", "OPEN_CLANK_MEMORY_BROKER_TOKEN"):
        os.environ.pop(k, None)
    assert run(lt._control_settings(ident)) is None and run(lt._control_settings(None)) is None
    assert run(lt._consume_control_approval(ident, "0" * 64, "inspect_desktop")) is False
