"""P1-B: accessibility protocol and Host client against a FAKE a11y helper (a tiny Python script). The real helper is never resolved or run."""
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

from src.openclank.computer_control import a11y_client as C
from src.openclank.computer_control import linux as L
from src.openclank.computer_control import protocol as P

SNAP = "a1b2c3d4e5f60718"
FAKE_A11Y = r'''
import json, os, sys
SNAP = "a1b2c3d4e5f60718"
d = os.environ["FAKE_DIR"]; mode = os.environ.get("FAKE_MODE", "")
open(d + "/pid", "w").write(str(os.getpid()))
def out(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
TREE = {"id": "root", "role": "frame", "name": "Main", "children": [
    {"id": "0", "role": "push button", "name": "Send", "actions": ["click"]},
    {"id": "1", "role": "text", "name": "Name", "value": "alice", "editable": True},
    {"id": "2", "role": "password text", "name": "Password", "value": "[hidden]", "hidden": True}]}
for line in sys.stdin:
    open(d + "/log", "a").write(line)
    r = json.loads(line); op = r["op"]
    if op == "handshake":
        out({"ok": False, "code": "accessibility_disabled"} if mode == "disabled" else {"ok": True, "backend": "atspi/1", "accessibility": "ready", "actions": [], "limits": {"nodes": 1200, "depth": 64, "value_chars": 500}})
    elif op == "list_apps": out({"ok": True, "apps": [{"name": "Demo", "pid": 42, "toolkit": "Qt", "windows": 1}]})
    elif op == "list_windows": out({"ok": True, "windows": [{"index": 0, "title": "Main"}]})
    elif op == "app_state":
        out({"ok": False, "code": "app_excluded"} if r["app"].get("name") == "keepass" else
            {"ok": True, "snapshot_id": SNAP, "target": {"version": 1}, "app": {"name": "Demo", "pid": 42}, "window": {"index": 0, "title": "Main"}, "tree": TREE})
    elif op == "act":
        if mode == "eof": sys.exit(0)
        if mode == "timeout": out({"ok": False, "code": "timeout"})
        elif mode == "stale": out({"ok": False, "code": "stale_element"})
        elif mode == "window": out({"ok": False, "code": "window_changed"})
        elif mode == "garbage": sys.stdout.write("nope\n"); sys.stdout.flush()
        else: out({"ok": True, "outcome": "performed", "window_changed": mode == "changed", "target_before": {"version": 1}, "target_after": {"version": 1}})
'''
TARGET = {"app": "Demo", "window": "Main", "role": "push button", "name": "Send"}
CLICK_EL = {"action": "click_element", "snapshot_id": SNAP, "element": "0", "target": TARGET}
SET_EL = {"action": "set_value", "snapshot_id": SNAP, "element": "1", "value": "bob secret",
          "target": {"app": "Demo", "window": "Main", "role": "text", "name": "Name"}}
STATE = {"action": "app_state", "app": {"name": "Demo"}}


class Env:
    def __init__(self, mode=""):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "fake.py").write_text(FAKE_A11Y)
        os.environ["FAKE_DIR"], os.environ["FAKE_MODE"] = str(self.dir), mode
        C._helper_argv = lambda: [sys.executable, "-B", str(self.dir / "fake.py")]
        C._platform_ok = lambda: True
        C.A = C._Helper()
        self.asked, self.allow, self.hook = [], True, None

    async def approve(self, digest):
        self.asked.append(digest)
        if self.hook:
            await self.hook()
        return bool(self.allow)

    def lines(self):
        p = self.dir / "log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def run(coro):
    return asyncio.run(coro)


async def read(e):
    return await C.inspect_action(STATE, e.approve)


def refused(fn, *a):
    with pytest.raises(P.ProtocolError):
        fn(*a)


# ---- pure protocol
def test_parse_new_control_actions_strictly():
    fr = {"width": 100, "height": 50}
    ok = [{"observation": "aaaaaaaaaaaaaaaa", "action": "hotkey", "keys": ["Control", "s"]}, {"observation": "aaaaaaaaaaaaaaaa", "action": "paste", "text": "x"},
          {"observation": "aaaaaaaaaaaaaaaa", "action": "scroll", "x": 1, "y": 2, "direction": "down", "amount": 3, "frame": fr},
          {"observation": "aaaaaaaaaaaaaaaa", "action": "drag", "from": {"x": 1, "y": 2}, "to": {"x": 5, "y": 6}, "frame": fr}, CLICK_EL, SET_EL,
          {"action": "secondary", "snapshot_id": SNAP, "element": "root", "target": TARGET, "action_name": "Menu"},
          {"action": "type_element", "snapshot_id": SNAP, "element": "1.2.3", "target": TARGET, "text": "hi"}]
    for call in ok:
        assert P.parse_call(call)["action"] == call["action"]
    bad = [{"observation": "aaaaaaaaaaaaaaaa", "action": "scroll", "x": 1, "y": 2, "direction": "diagonal", "amount": 3, "frame": fr}, {"observation": "aaaaaaaaaaaaaaaa", "action": "scroll", "x": 1, "y": 2, "direction": "up", "amount": 21, "frame": fr},
           {"observation": "aaaaaaaaaaaaaaaa", "action": "drag", "from": {"x": 1}, "to": {"x": 5, "y": 6}, "frame": fr}, {"observation": "aaaaaaaaaaaaaaaa", "action": "paste", "text": ""},
           {**CLICK_EL, "snapshot_id": "xyz"}, {**CLICK_EL, "element": "a.b"}, {**CLICK_EL, "element": "1..2"}, {**CLICK_EL, "extra": 1},
           {**CLICK_EL, "target": {**TARGET, "role": ""}}, {**CLICK_EL, "target": {"app": "x"}}, {**SET_EL, "value": ""}, {**SET_EL, "value": "a\x00b"},
           {"action": "secondary", "snapshot_id": SNAP, "element": "0", "target": TARGET}]
    for call in bad:
        refused(P.parse_call, call)


def test_parse_inspect_is_strict():
    assert P.parse_inspect({"action": "list_windows", "app": {"pid": 5}}) == {"action": "list_windows", "app": {"pid": 5}}
    assert P.parse_inspect({"action": "app_state", "app": {"name": "x"}, "window_index": 2})["window_index"] == 2
    for bad in [{"action": "app_state"}, {"action": "handshake", "app": {"pid": 1}}, {"action": "list_apps", "x": 1}, {"action": "app_state", "app": {"pid": 1, "name": "x"}},
                {"action": "app_state", "app": {"pid": True}}, {"action": "app_state", "app": {"name": ""}}, {"action": "app_state", "app": {"name": "x"}, "window_index": -1},
                {"action": "tree"}, [], None]:
        refused(P.parse_inspect, bad)


def test_helper_request_lines_are_exact():
    assert P.a11y_request_line(P.parse_inspect({"action": "app_state", "app": {"name": "Demo"}, "window_index": 1})) == b'{"v":1,"op":"app_state","app":{"name":"Demo"},"window_index":1}\n'
    assert P.a11y_request_line(P.parse_call(CLICK_EL)) == b'{"v":1,"op":"act","snapshot_id":"%s","element":"0","action":"click"}\n' % SNAP.encode()
    assert P.a11y_request_line(P.parse_call(SET_EL)) == b'{"v":1,"op":"act","snapshot_id":"%s","element":"1","action":"set_value","text":"bob secret"}\n' % SNAP.encode()
    assert P.build_scroll(1, 2, "left", 2, {"width": 3, "height": 4}) == b'{"v":1,"op":"scroll","x":1,"y":2,"direction":"left","amount":2,"frame":{"width":3,"height":4}}\n'
    assert P.build_drag({"x": 1, "y": 2}, {"x": 3, "y": 4}, {"width": 9, "height": 9}) == b'{"v":1,"op":"drag","x1":1,"y1":2,"x2":3,"y2":4,"frame":{"width":9,"height":9}}\n'
    assert P.build_paste("a") == b'{"v":1,"op":"paste","text":"a"}\n' and P.build_probe() == b'{"v":1,"op":"probe"}\n'
    assert P.request_line(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "hotkey", "keys": ["Control", "s"]})) == P.build_key(["Control", "s"])


def test_a11y_reply_parsing_never_treats_garbage_as_success():
    good = json.dumps({"ok": True, "outcome": "performed", "window_changed": False}).encode() + b"\n"
    assert P.parse_a11y_reply(good, "act")["ok"] is True
    for line in [b"", b"nope\n", good.rstrip(b"\n"), b'{"ok":true,"outcome":"performed"}\n', b'{"ok":false,"code":"boom"}\n', b'{"ok":false,"code":"stale_element","x":1}\n',
                 b'{"ok":true,"snapshot_id":"nothex","tree":{},"target":{}}\n']:
        assert P.parse_a11y_reply(line, "act")["code"] == "invalid_reply" or line.startswith(b'{"ok":true,"snapshot_id"')
    assert P.parse_a11y_reply(b'{"ok":false,"code":"stale_element"}\n', "act") == {"ok": False, "code": "stale_element"}
    assert P.parse_a11y_reply(b'{"ok":true,"snapshot_id":"nothex","tree":{},"target":{}}\n', "state")["code"] == "invalid_reply"
    assert P.parse_reply(b'{"ok":true,"state":"probe","version":2,"keyboard":true,"pointer":false}\n', "probe") == {"ok": True, "version": 2, "keyboard": True, "pointer": False}
    assert P.parse_reply(b'{"ok":false,"code":"taken_over"}\n', "action") == {"ok": False, "code": "taken_over"}


def test_summaries_show_the_exact_action_and_receipts_hold_no_private_text():
    assert P.approval_summary(P.parse_call(CLICK_EL)) == "Click push button 'Send' in Demo, window 'Main'"
    assert P.approval_summary(P.parse_call(SET_EL)) == "Set text 'Name' in Demo, window 'Main' to: bob secret"
    assert "Scroll down 3" in P.approval_summary(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "scroll", "x": 1, "y": 2, "direction": "down", "amount": 3, "frame": {"width": 5, "height": 5}}))
    assert "Drag from (1, 2) to (3, 4)" in P.approval_summary(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "drag", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "frame": {"width": 5, "height": 5}}))
    assert P.approval_summary(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "paste", "text": "zzz"})).endswith("zzz")
    assert "secret-looking values are hidden" in P.inspect_summary(P.parse_inspect(STATE))
    rec = P.receipt(P.parse_call(SET_EL), "performed", True, 1)
    text = json.dumps(rec)
    assert "bob secret" not in text and "Send" not in text and "Name" not in text and rec["chars"] == 10 and len(rec["text_sha256"]) == 64 and len(rec["name_sha256"]) == 64
    obs = P.observation_receipt(P.parse_call(SET_EL), "performed", {"target_before": {"a": 1}, "target_after": {"a": 2}, "window_changed": False}, "op1", True, {"host": 3}, 1)
    assert obs["version"] == 1 and obs["operationId"] == "op1" and obs["coordinateSpace"] == "window" and obs["targetBefore"] == {"a": 1} and "bob secret" not in json.dumps(obs)


def test_digest_is_bound_to_snapshot_and_element():
    d = P.args_digest(CLICK_EL)
    assert d != P.args_digest({**CLICK_EL, "element": "1"}) and d != P.args_digest({**CLICK_EL, "snapshot_id": "b" * 16}) and d == P.args_digest(dict(CLICK_EL))


def test_registry_revoke_drops_pending_approvals():
    r = P.ApprovalRegistry()
    r.grant("o", "s1", "d1"); r.grant("o", "s2", "d2"); r.grant("p", "s1", "d3")
    assert r.revoke("o", "s1") == 1 and r.consume("o", "s1", "d1") is False and r.consume("o", "s2", "d2") is True
    assert r.revoke() == 1 and r.consume("p", "s1", "d3") is False


# ---- Host client against the fake helper
def test_handshake_needs_no_approval_but_reading_does():
    async def go():
        e = Env()
        hs = await C.inspect_action({"action": "handshake"}, e.approve)
        n = len(e.asked)
        e.allow = False
        denied = await C.inspect_action({"action": "list_apps"}, e.approve)
        e.allow = True
        apps = await C.inspect_action({"action": "list_apps"}, e.approve)
        await C.A.close()
        return e, hs, n, denied, apps
    e, hs, n, denied, apps = run(go())
    assert hs["outcome"] == "ready" and hs["result"]["accessibility"] == "ready" and n == 0
    assert denied["outcome"] == "approval_required" and [x["op"] for x in e.lines()] == ["handshake", "list_apps"], "a refused read sends nothing"
    assert apps["ok"] and apps["result"]["apps"][0]["name"] == "Demo"


def test_disabled_accessibility_and_excluded_app_are_typed_refusals():
    async def go():
        e = Env("disabled")
        hs = await C.inspect_action({"action": "handshake"}, e.approve)
        await C.A.close()
        e2 = Env()
        ex = await C.inspect_action({"action": "app_state", "app": {"name": "keepass"}}, e2.approve)
        await C.A.close()
        return hs, ex
    hs, ex = run(go())
    assert hs["outcome"] == "accessibility_disabled" and ex["outcome"] == "app_excluded"


def test_read_then_click_one_approval_each_and_effect_not_verified():
    async def go():
        e = Env()
        s = await read(e)
        r = await C.element_action(P.parse_call(CLICK_EL), CLICK_EL, e.approve)
        await C.A.close()
        return e, s, r
    e, s, r = run(go())
    assert s["result"]["snapshot_id"] == SNAP and len(e.asked) == 2 and e.asked[1] == P.args_digest(CLICK_EL)
    assert r["outcome"] == "performed" and r["ok"] and "not verified" in r["message"] and r["window_changed"] is False
    assert r["receipt"]["version"] == 1 and r["receipt"]["result"] == "performed"
    assert [x["op"] for x in e.lines()] == ["app_state", "act"] and e.lines()[1]["action"] == "click"


def test_claims_are_checked_against_the_snapshot_before_the_human_is_asked():
    async def go():
        e = Env()
        await read(e)
        n = len(e.asked)
        out = {}
        out["unknown"] = await C.element_action(P.parse_call({**CLICK_EL, "snapshot_id": "b" * 16}), {**CLICK_EL, "snapshot_id": "b" * 16}, e.approve)
        lie = {**CLICK_EL, "target": {**TARGET, "name": "Delete everything"}}
        out["lie"] = await C.element_action(P.parse_call(lie), lie, e.approve)
        miss = {**CLICK_EL, "element": "9.9"}
        out["missing"] = await C.element_action(P.parse_call(miss), miss, e.approve)
        await C.A.close()
        return e, n, out
    e, n, out = run(go())
    assert (out["unknown"]["outcome"], out["lie"]["outcome"], out["missing"]["outcome"]) == ("snapshot_expired", "stale_element", "no_such_element")
    assert len(e.asked) == n and [x["op"] for x in e.lines()] == ["app_state"]


def test_helper_refusals_pass_through_and_are_never_retried():
    for mode, want in [("stale", "stale_element"), ("window", "window_changed")]:
        async def go():
            e = Env(mode)
            await read(e)
            r = await C.element_action(P.parse_call(CLICK_EL), CLICK_EL, e.approve)
            await C.A.close()
            return e, r
        e, r = run(go())
        assert r["outcome"] == want and r["ok"] is False and len([x for x in e.lines() if x["op"] == "act"]) == 1


def test_unknown_outcomes_are_reported_and_not_repeated():
    for mode in ("timeout", "garbage", "eof"):
        async def go():
            e = Env(mode)
            await read(e)
            r = await C.element_action(P.parse_call(CLICK_EL), CLICK_EL, e.approve)
            again = await C.element_action(P.parse_call(CLICK_EL), CLICK_EL, e.approve) if mode == "eof" else None
            await C.A.close()
            return e, r, again
        e, r, again = run(go())
        assert r["outcome"] == "outcome_unknown" and "Not repeated" in r["message"] and len([x for x in e.lines() if x["op"] == "act"]) == 1
        if mode == "eof":
            assert again["outcome"] == "snapshot_expired", "the helper died, so its snapshots died with it"


def test_window_changed_by_the_action_is_flagged():
    async def go():
        e = Env("changed")
        await read(e)
        r = await C.element_action(P.parse_call(CLICK_EL), CLICK_EL, e.approve)
        await C.A.close()
        return r
    assert run(go())["window_changed"] is True


def test_set_value_text_never_reaches_logs_or_receipts(caplog):
    async def go():
        e = Env()
        await read(e)
        r = await C.element_action(P.parse_call(SET_EL), SET_EL, e.approve)
        await C.A.close()
        return r
    with caplog.at_level(logging.DEBUG):
        r = run(go())
    assert r["outcome"] == "performed"
    assert "bob secret" not in caplog.text and "bob secret" not in json.dumps(r["receipt"])


def test_expired_snapshot_after_approval_sends_nothing():
    async def go():
        e = Env()
        await read(e)

        async def age():
            C.A.snaps[SNAP]["made"] -= C.SNAP_TTL_S + 1
        e.hook = age
        r = await C.element_action(P.parse_call(CLICK_EL), CLICK_EL, e.approve)
        await C.A.close()
        return e, r
    e, r = run(go())
    assert r["outcome"] == "snapshot_expired" and [x["op"] for x in e.lines()] == ["app_state"]


def test_human_takeover_between_approval_and_send_sends_nothing_and_ends_the_helper():
    async def go():
        e = Env()
        await read(e)
        pid = int((e.dir / "pid").read_text())

        async def takeover():
            await L.take_over()
        e.hook = takeover
        r = await C.element_action(P.parse_call(CLICK_EL), CLICK_EL, e.approve)
        return e, r, pid
    e, r, pid = run(go())
    assert r["outcome"] == "taken_over" and [x["op"] for x in e.lines()] == ["app_state"]
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(pid, 0)
            __import__("time").sleep(0.05)
        os.kill(pid, 0)


def test_take_over_revokes_pending_approvals_and_reports():
    P.REGISTRY.grant("owner-x", "sess-x", "d" * 64)
    r = run(L.take_over("owner-x", "sess-x"))
    assert r == {"outcome": "stopped", "session_active": False, "taken_over": True, "exit_code": 0}
    assert P.REGISTRY.consume("owner-x", "sess-x", "d" * 64) is False


def test_element_actions_route_through_control_action_without_a_wayland_session():
    async def go():
        e = Env()
        L._platform_ok = lambda: False       # the pixel path would refuse; the accessibility path does not need Wayland
        await read(e)
        r = await L.control_action(CLICK_EL, e.approve)
        await C.A.close()
        return r
    assert run(go())["outcome"] == "performed"


def test_readiness_vocabulary():
    async def go():
        Env()
        s = await L.control_status({"action": "readiness"})
        C._platform_ok = lambda: False
        s2 = a11y = C.readiness()
        return s, s2
    s, s2 = run(go())
    assert s["capabilities"]["accessibility"]["state"] == "permission_required" and s["capabilities"]["input"]["state"] in {"permission_required", "no_interactive_session"}
    assert s["capabilities"]["clipboard"]["state"] == "backend_unavailable" and "Capturing" in s["note"] and "desktop_capture_enabled" not in s["note"]
    assert s2["state"] == "no_interactive_session"


# ---- pixel path additions against a fake CONTROL helper
FAKE_CONTROL = r'''
import json, os, sys
d = os.environ["FAKE_DIR"]; mode = os.environ.get("FAKE_MODE", "")
def out(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    open(d + "/log", "a").write(line)
    r = json.loads(line)
    if r["op"] == "probe": out({"ok": True, "state": "probe", "version": 2, "keyboard": True, "pointer": mode != "nopointer"}); sys.exit(0)
    if r["op"] == "begin": out({"ok": True, "state": "active", "stream": {"width": 100, "height": 50}})
    elif r["op"] == "stop": out({"ok": True, "state": "stopped"}); sys.exit(0)
    elif mode == "taken": out({"ok": False, "code": "taken_over"})
    elif mode == "noclip" and r["op"] == "paste": out({"ok": False, "code": "unsupported_clipboard"})
    else: out({"ok": True, "outcome": "sent", "actions_left": 19})
'''
FR = {"width": 100, "height": 50}


class CEnv:
    def __init__(self, mode=""):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "fake.py").write_text(FAKE_CONTROL)
        os.environ["FAKE_DIR"], os.environ["FAKE_MODE"] = str(self.dir), mode
        L._helper_argv = lambda: [sys.executable, "-B", str(self.dir / "fake.py")]
        L._platform_ok = lambda: True
        L.S = L._Session()
        self.asked, self.hook = [], None

    async def approve(self, digest):
        self.asked.append(digest)
        if self.hook:
            await self.hook()
        return True

    def ops(self):
        p = self.dir / "log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_new_pixel_actions_reach_the_helper_with_exact_lines():
    async def go():
        e = CEnv()
        await L.control_action({"action": "begin"}, e.approve)
        calls = [{"observation": "aaaaaaaaaaaaaaaa", "action": "scroll", "x": 1, "y": 2, "direction": "up", "amount": 2, "frame": FR},
                 {"observation": "aaaaaaaaaaaaaaaa", "action": "drag", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "frame": FR},
                 {"observation": "aaaaaaaaaaaaaaaa", "action": "paste", "text": "hello"}, {"observation": "aaaaaaaaaaaaaaaa", "action": "hotkey", "keys": ["Control", "c"]}]
        out = [await L.control_action(c, e.approve) for c in calls]
        await L.control_status({"action": "stop"})
        return e, out
    e, out = run(go())
    assert [o["outcome"] for o in out] == ["sent"] * 4
    assert [x["op"] for x in e.ops()][:5] == ["begin", "scroll", "drag", "paste", "key"] and e.ops()[4]["keysyms"] == [65507, 99]
    assert "hello" not in json.dumps([o["receipt"] for o in out]) and out[2]["receipt"]["chars"] == 5


def test_scroll_and_drag_are_checked_against_the_stream_before_asking():
    async def go():
        e = CEnv()
        await L.control_action({"action": "begin"}, e.approve)
        n = len(e.asked)
        r1 = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "scroll", "x": 1, "y": 2, "direction": "up", "amount": 2, "frame": {"width": 9, "height": 9}}, e.approve)
        r2 = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "drag", "from": {"x": 1, "y": 2}, "to": {"x": 100, "y": 4}, "frame": FR}, e.approve)
        await L.control_status({"action": "stop"})
        return e, n, r1, r2
    e, n, r1, r2 = run(go())
    assert (r1["outcome"], r2["outcome"]) == ("frame_mismatch", "out_of_bounds") and len(e.asked) == n and [x["op"] for x in e.ops()] == ["begin", "stop"]


def test_helper_taken_over_ends_the_session_and_unsupported_clipboard_keeps_it():
    async def go():
        e = CEnv("taken")
        await L.control_action({"action": "begin"}, e.approve)
        r = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["a"]}, e.approve)
        after = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["a"]}, e.approve)
        e2 = CEnv("noclip")
        await L.control_action({"action": "begin"}, e2.approve)
        c = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "paste", "text": "x"}, e2.approve)
        k = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["a"]}, e2.approve)
        await L.control_status({"action": "stop"})
        return r, after, c, k
    r, after, c, k = run(go())
    assert r["outcome"] == "taken_over" and after["reason"] == "no_session"
    assert c["outcome"] == "unsupported_clipboard" and k["outcome"] == "sent"


def test_takeover_while_a_pixel_action_waits_for_approval_sends_nothing():
    async def go():
        e = CEnv()
        await L.control_action({"action": "begin"}, e.approve)

        async def takeover():
            await L.take_over()
        e.hook = takeover
        r = await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["a"]}, e.approve)
        return e, r
    e, r = run(go())
    assert r["outcome"] == "taken_over" and [x["op"] for x in e.ops()] == ["begin"] and L.S.active() is False


def test_probe_asks_the_portal_without_a_session():
    async def go():
        CEnv()
        ok = await L.control_status({"action": "probe"})
        CEnv("nopointer")
        no = await L.control_status({"action": "probe"})
        return ok, no
    ok, no = run(go())
    assert ok["input"] == {"state": "permission_required", "portal_version": 2, "keyboard": True, "pointer": True}
    assert no["input"]["state"] == "backend_unavailable" and no["input"]["pointer"] is False
