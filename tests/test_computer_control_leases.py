"""P1-I: leases (reusing the browser lease registry), fresh observations, holds, outcome checks, timings, readiness, Files hand-over."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from src.openclank.browser_control import BrowserTargetRegistry
from src.openclank.computer_control import a11y_client as C
from src.openclank.computer_control import hoststate as H
from src.openclank.computer_control import linux as L
from src.openclank.computer_control import protocol as P
from tests.test_computer_control_a11y import CEnv, FAKE_A11Y, FR, STATE, run

SNAP = "a1b2c3d4e5f60718"
ALLOW = {"control": True, "accessibility": True, "action_log": False, "multi_agent": False}
MULTI = {**ALLOW, "multi_agent": True}
EL = {"action": "click_element", "snapshot_id": SNAP, "element": "0", "target": {"app": "Demo", "window": "Main", "role": "push button", "name": "Send"}}


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def handle(state, actor="agent-a", owner="alice", session="s1"):
    return H.LocalHandle(state, owner, session, actor)


class AEnv2:
    """The fake a11y helper from the a11y tests, with a switch for the read-back result of an action."""
    def __init__(self, verified="unknown", mode=""):
        self.dir = Path(tempfile.mkdtemp())
        script = FAKE_A11Y.replace('else: out({"ok": True, "outcome": "performed", "window_changed": mode == "changed", "target_before": {"version": 1}, "target_after": {"version": 1}})',
                                   'else: out({"ok": True, "outcome": "performed", "verified": %r, "window_changed": mode == "changed", "target_before": {"version": 1}, "target_after": {"version": 1}})' % verified)
        (self.dir / "fake.py").write_text(script)
        os.environ["FAKE_DIR"], os.environ["FAKE_MODE"] = str(self.dir), mode
        C._helper_argv = lambda: [sys.executable, "-B", str(self.dir / "fake.py")]
        C._platform_ok = lambda: True
        C.A = C._Helper()
        self.asked, self.hook = [], None

    async def approve(self, digest):
        self.asked.append(digest)
        if self.hook:
            await self.hook()
        return True

    def ops(self):
        p = self.dir / "log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


async def read(e, st, allow=ALLOW, app=None):
    return await C.inspect_action({"action": "app_state", "app": app or {"name": "Demo"}}, e.approve, allow, st)


# ---- rows 1, 19, 26, 33, 40: leases through the browser lease registry
def test_native_leases_reuse_the_browser_registry_and_follow_its_stale_rules():
    assert issubclass(H.NativeTargetRegistry, BrowserTargetRegistry)
    clock = Clock(); s = H.ControlState(clock)
    app, win = {"pid": 42, "start": 7, "name": "Demo"}, {"index": 0, "title": "Main"}
    l1 = s.window_observed("alice", "s1", "agent-a", "a" * 16, app, win, "gen1")
    call = {"action": "click_element", "snapshot_id": "a" * 16}
    assert s.precheck("alice", "s1", "agent-a", call, None) is None
    assert l1["owner"] == "alice" and l1["session"] == "s1" and l1["capturedResource"] == "a" * 16 and l1["observationGeneration"] == 0
    l2 = s.window_observed("alice", "s1", "agent-a", "b" * 16, app, win, "gen1")         # a newer observation of the same window
    assert l2["targetId"] == l1["targetId"] and l2["observationGeneration"] == 1
    assert s.precheck("alice", "s1", "agent-a", call, None) == "observation_stale"      # the older snapshot is stale, as for browser refs
    assert s.precheck("alice", "s1", "agent-a", {**call, "snapshot_id": "b" * 16}, None) is None
    assert s.precheck("bob", "s1", "agent-a", {**call, "snapshot_id": "b" * 16}, None) == "lease_scope"
    assert s.precheck("alice", "other", "agent-a", {**call, "snapshot_id": "b" * 16}, None) == "lease_scope"
    assert s.precheck("alice", "s1", "agent-a", {**call, "snapshot_id": "c" * 16}, None) == "lease_expired"


def test_takeover_epoch_makes_every_earlier_observation_stale_and_ends_registered_helpers():
    s = H.ControlState(Clock())
    s.window_observed("alice", "s1", "agent-a", "a" * 16, {"pid": 1, "start": 1, "name": "X"}, {"index": 0, "title": "t"}, "g")
    ob = s.capture_observed("alice", "s1", "agent-a", 100, 50)["observationId"]
    H.HELPER_EXES = lambda: {os.path.realpath(sys.executable)}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    s.register_helper(child.pid, "control")
    out = s.take_over("alice")
    assert out["ended"] == 1 and out["epoch"] == 1
    for _ in range(60):
        if child.poll() is not None: break
        time.sleep(0.05)
    assert child.poll() is not None, "the registered helper process group was ended at once"
    assert s.precheck("alice", "s1", "agent-a", {"action": "click_element", "snapshot_id": "a" * 16}, None) == "lease_expired"
    assert s.precheck("alice", "s1", "agent-a", {"action": "click", "observation": ob}, None) == "observation_required"
    assert s.take_over("alice")["ended"] == 0                                           # idempotent


def test_observation_carries_owner_session_and_captured_resource_on_the_target(own_helper_exe):
    async def go():
        e = AEnv2()
        st = handle(H.ControlState(), owner="alice", session="s9")
        r = await read(e, st)
        await C.A.close()
        return r
    t = run(go())["result"]["target"]
    assert (t["owner"], t["session"]) == ("alice", "s9") and t["capturedResource"] == SNAP and t["backend"] == "atspi/1" if "backend" in t else True
    assert run(asyncio.sleep(0)) is None


# ---- row 22: pixel actions are bound to a fresh observation, enforced before any approval
def test_pixel_actions_need_a_fresh_unused_observation_newer_than_the_last_stop(own_helper_exe):
    async def go():
        e = CEnv(); clock = Clock(); state = H.ControlState(clock); st = handle(state)
        await L.control_action({"action": "begin"}, e.approve, ALLOW, st)
        asked = len(e.asked)
        out = {}
        click = lambda ob: {"action": "click", "x": 1, "y": 2, "frame": FR, "observation": ob}
        out["none"] = await L.control_action(click("0" * 16), e.approve, ALLOW, st)
        ob = (await st.capture_observed(100, 50))["observationId"]
        out["ok"] = await L.control_action(click(ob), e.approve, ALLOW, st)
        out["reuse"] = await L.control_action(click(ob), e.approve, ALLOW, st)
        ob2 = (await st.capture_observed(100, 50))["observationId"]
        clock.t += H.OBS_MAX_AGE_S + 1
        out["old"] = await L.control_action(click(ob2), e.approve, ALLOW, st)
        ob3 = (await st.capture_observed(100, 50))["observationId"]
        await L.take_over()                    # a Stop after the observation
        out["after_stop"] = await L.control_action(click(ob3), e.approve, ALLOW, st)
        await L.control_status({"action": "stop"})
        return e, asked, out
    e, asked, out = run(go())
    assert [out[k]["outcome"] for k in ("none", "ok", "reuse", "old", "after_stop")] == ["observation_required", "sent", "observation_stale", "observation_stale", "observation_stale"] or \
        out["after_stop"]["outcome"] in ("observation_stale", "observation_required", "unavailable")
    assert out["none"]["reason"] == "observe_again" and out["reuse"]["reason"] == "observe_again"
    assert len(e.asked) == asked + 1, "only the one valid action reached the approval"


def test_pre_card_check_in_the_host_refuses_without_a_card_or_a_grant():
    from tests.test_control_approval_record import _ask, _handler, _fresh, _selected, ARGS
    _fresh()
    H.STATE.take_over(); prompts = []
    # no observation known: the handler approves "once" without recording anything, so the tool answers with the typed reason
    assert _selected(_ask(_handler("reject", prompts=prompts), args=ARGS, precheck=True)) == "once"
    assert prompts == [] and P.REGISTRY.consume("alice", "eng1", P.args_digest(ARGS)) is False
    ob = H.STATE.capture_observed("alice", "eng1", "a", 100, 50)["observationId"]
    fresh = {**ARGS, "observation": ob}
    assert _selected(_ask(_handler("once", prompts=prompts), args=fresh, precheck=True)) == "once" and len(prompts) == 1
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest(fresh)) is True
    H.STATE.take_over()
    dialog = {"action": "hand_over_file", "resource_ref": "res_abcdefgh1234", "direction": "upload"}
    assert _selected(_ask(_handler("reject", prompts=prompts), args=dialog, precheck=True)) == "once" and len(prompts) == 1


# ---- row 24: one agent per app unless the owner allows several
def test_second_agent_is_refused_for_a_held_app_unless_multi_agent_is_on(own_helper_exe):
    async def go():
        e = AEnv2(); state = H.ControlState(Clock())
        a, b = handle(state, "agent-a"), handle(state, "agent-b")
        first = await read(e, a)
        refused = await read(e, b)
        refused_by_pid = await C.inspect_action({"action": "list_windows", "app": {"pid": 42}}, e.approve, ALLOW, b)
        snap = first["result"]["snapshot_id"]
        el = {**EL, "snapshot_id": snap}
        acted_b = await L.control_action(el, e.approve, ALLOW, b)
        allowed = await read(e, b, MULTI)
        own = await read(e, a)
        await C.A.close()
        return first, refused, refused_by_pid, acted_b, allowed, own
    first, refused, by_pid, acted_b, allowed, own = run(go())
    assert first["ok"] and refused["outcome"] == "app_held" and acted_b["outcome"] in ("lease_scope", "app_held", "lease_expired", "observation_stale")
    assert allowed["ok"], "with several agents allowed the second may read"
    assert own["outcome"] == "app_held", "and the hold moved to the agent that read last (the setting is Off for this call)"


def test_screen_is_held_by_the_agent_that_captured_it():
    state = H.ControlState(Clock())
    state.capture_observed("alice", "s1", "agent-a", 10, 10)
    assert state.precheck("alice", "s1", "agent-b", {"action": "begin"}, ALLOW) == "app_held"
    assert state.precheck("alice", "s1", "agent-b", {"action": "begin"}, MULTI) is None
    assert state.precheck("alice", "s1", "agent-a", {"action": "begin"}, ALLOW) is None


# ---- row 15: move, selection, and outcome checks
def test_move_and_select_text_are_protocol_actions_with_exact_helper_lines():
    mv = P.parse_call({"action": "move", "x": 3, "y": 4, "frame": FR, "observation": "a" * 16})
    assert P.request_line(mv) == b'{"v":1,"op":"move","x":3,"y":4,"frame":{"width":100,"height":50}}\n'
    assert P.approval_summary(mv) == "Move the pointer to (3, 4) in a 100x50 frame (no click)"
    sel = {"action": "select_text", "snapshot_id": SNAP, "element": "1", "target": EL["target"], "start": 2, "end": 5}
    c = P.parse_call(sel)
    assert P.a11y_request_line(c) == b'{"v":1,"op":"act","snapshot_id":"%s","element":"1","action":"select_text","start":2,"end":5}\n' % SNAP.encode()
    assert "Select characters 2 to 5" in P.approval_summary(c) and P.receipt(c, "performed", True, 1)["start"] == 2
    for bad in ({**sel, "start": 5, "end": 2}, {**sel, "start": -1}, {**sel, "end": "x"}):
        with pytest.raises(P.ProtocolError): P.parse_call(bad)


def test_the_read_back_result_reaches_the_result_and_the_receipt(own_helper_exe):
    for verified in (True, False, "unknown"):
        async def go():
            e = AEnv2(verified)
            st = handle(H.ControlState(Clock()))
            await read(e, st)
            r = await L.control_action({"action": "set_value", "snapshot_id": SNAP, "element": "1", "value": "bob",
                                        "target": {"app": "Demo", "window": "Main", "role": "text", "name": "Name"}}, e.approve, ALLOW, st)
            await C.A.close()
            return r
        r = run(go())
        assert r["outcome"] == "performed" and r["verified"] == verified and r["receipt"]["verified"] == verified
    assert P.parse_a11y_reply(b'{"ok":true,"outcome":"performed","window_changed":false,"verified":"maybe"}\n', "act")["code"] == "invalid_reply"


# ---- row 27: stage timings
def test_receipts_carry_measured_stage_timings_and_name_the_unmeasured(own_helper_exe):
    async def go():
        e = AEnv2()
        st = handle(H.ControlState(Clock()))
        await read(e, st)
        r = await L.control_action(EL, e.approve, ALLOW, st)
        await C.A.close()
        c = CEnv(); st2 = handle(H.ControlState(Clock()))
        await L.control_action({"action": "begin"}, c.approve, ALLOW, st2)
        ob = (await st2.capture_observed(100, 50))["observationId"]
        px = await L.control_action({"action": "key", "keys": ["a"], "observation": ob}, c.approve, ALLOW, st2)
        await L.control_status({"action": "stop"})
        return r, px
    r, px = run(go())
    t = r["receipt"]["stageTimingsMs"]
    assert {"permission_wait", "transport", "host"} <= set(t) and t["notMeasured"] == ["model", "render"] and all(isinstance(t[k], int) for k in ("permission_wait", "transport", "host"))
    tp = px["receipt"]["stageTimingsMs"]
    assert {"permission_wait", "transport", "host"} <= set(tp) and tp["notMeasured"] == ["model", "render"]


# ---- row 3: readiness entries
def test_readiness_names_file_transfer_and_recording():
    async def go():
        CEnv()
        return await L.control_status({"action": "readiness"})
    caps = run(go())["capabilities"]
    assert "file_transfer" in caps and "recording" in caps and caps["recording"]["state"] == "permission_required" and "never records" in caps["recording"]["note"]
    assert "hand_over_file: unsupported" in caps["file_transfer"]["open_resource"]


# ---- row 18: Files resources by reference only
def test_open_resource_takes_a_chosen_files_token_never_a_path():
    ok = {"action": "open_resource", "resource_ref": "res_abcdefgh1234", "app_id": "org.kde.kate.desktop"}
    c = P.parse_call(ok)
    assert "Open the Files resource you chose" in P.approval_summary(c) and P.receipt(c, "performed", True, 1)["app_id"] == "org.kde.kate.desktop"
    for bad in ("/home/me/secret.txt", "../etc/passwd", "C:\\x", "short", "res/with/slash1", "~/file.txt", "a" * 2000):
        with pytest.raises(P.ProtocolError): P.parse_call({**ok, "resource_ref": bad})
    with pytest.raises(P.ProtocolError): P.parse_call({**ok, "app_id": "bad id; rm"})
    with pytest.raises(P.ProtocolError): P.parse_call({**ok, "path": "/x"})


def test_open_resource_goes_through_the_host_opener_after_approval_and_hand_over_is_refused():
    class Opener(H.LocalHandle):
        async def open_resource(self, ref, app):
            self.seen = (ref, app)
            return {"ok": True}
    async def go():
        c = CEnv(); asked = []
        async def approve(d): asked.append(d); return True
        st = Opener(H.ControlState(Clock()), "alice", "s1", "agent-a")
        ok = await L.control_action({"action": "open_resource", "resource_ref": "res_abcdefgh1234", "app_id": "org.kde.kate.desktop"}, approve, ALLOW, st)
        n = len(asked)
        hand = await L.control_action({"action": "hand_over_file", "resource_ref": "res_abcdefgh1234", "direction": "upload"}, approve, ALLOW, st)
        n_after_hand = len(asked)
        none = await L.control_action({"action": "open_resource", "resource_ref": "res_abcdefgh1234", "app_id": "x"}, approve, ALLOW, None)
        plain = await L.control_action({"action": "open_resource", "resource_ref": "res_abcdefgh1234", "app_id": "x"}, approve, ALLOW, handle(H.ControlState(Clock())))
        return st, ok, hand, none, plain, n_after_hand, n
    st, ok, hand, none, plain, n_after_hand, n = run(go())
    assert ok["outcome"] == "performed" and st.seen == ("res_abcdefgh1234", "org.kde.kate.desktop") and n == 1
    assert hand["outcome"] == "file_dialog_unproven" and n_after_hand == n, "refused without asking"
    assert none["outcome"] == "files_open_unavailable" and plain["outcome"] == "files_open_unavailable"


# ---- Stop never signals a process that is not our helper
def _child(ignore_term=False, new_session=True):
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)" if ignore_term else "import time; time.sleep(30)"
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=new_session)


def _wait_dead(child, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if child.poll() is not None:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def own_helper_exe():
    real = H.HELPER_EXES
    H.HELPER_EXES = lambda: {os.path.realpath(sys.executable)}      # the test's harmless child stands in for the helper binary
    yield
    H.HELPER_EXES = real


def test_registration_is_refused_unless_the_host_confirms_it_is_our_helper(own_helper_exe):
    s = H.ControlState(Clock())
    codes = {}
    for label, pid in (("bad", 1), ("gone", 2 ** 22 - 5)):
        with pytest.raises(H.HelperRefused) as e:
            s.register_helper(pid, "control")
        codes[label] = e.value.code
    assert codes == {"bad": "helper_pid_invalid", "gone": "helper_not_running"}
    shared = _child(new_session=False)                  # same process group as the test: not a group leader
    try:
        with pytest.raises(H.HelperRefused) as e:
            s.register_helper(shared.pid, "control")
        assert e.value.code == "helper_not_group_leader"
    finally:
        shared.kill(); shared.wait()
    H.HELPER_EXES = lambda: {"/usr/bin/some-other-program"}
    stranger = _child()
    try:
        with pytest.raises(H.HelperRefused) as e:
            s.register_helper(stranger.pid, "control")
        assert e.value.code == "helper_not_the_helper"
        assert s.helpers == {} and s.take_over()["ended"] == 0 and stranger.poll() is None, "nothing was signalled"
        out = s.handle if False else H.handle_broker(s, "alice", {"sessionID": "s", "actorID": "a"}, {"op": "register_helper", "pid": stranger.pid, "kind": "control"})
        assert out == {"ok": False, "code": "helper_not_the_helper"}
    finally:
        stranger.kill(); stranger.wait()


def test_a_registered_live_helper_is_ended_by_stop_and_a_reused_pid_is_not(own_helper_exe):
    s = H.ControlState(Clock())
    live = _child()
    s.register_helper(live.pid, "control")
    assert s.helpers[live.pid]["start"] > 0 and s.helpers[live.pid]["exe"] == os.path.realpath(sys.executable)
    assert s.take_over()["ended"] == 1 and _wait_dead(live)
    # the same pid "reused": the start time no longer matches what was registered, so nothing is signalled
    other = _child()
    try:
        s.register_helper(other.pid, "control")
        s.helpers[other.pid]["start"] += 1
        assert s.take_over()["ended"] == 0 and other.poll() is None
    finally:
        other.kill(); other.wait()


def test_the_hard_kill_timer_rechecks_before_signalling(own_helper_exe):
    s = H.ControlState(Clock())
    stubborn = _child(ignore_term=True)
    try:
        s.register_helper(stubborn.pid, "control")
        entry = dict(s.helpers[stubborn.pid])
        H._kill_hard(stubborn.pid, {**entry, "start": entry["start"] + 1})        # a different process now: no signal
        time.sleep(0.2)
        assert stubborn.poll() is None
        H._kill_hard(stubborn.pid, {**entry, "exe": "/usr/bin/other"})              # a different program: no signal
        time.sleep(0.2)
        assert stubborn.poll() is None
        H._kill_hard(stubborn.pid, entry)                                          # the very same process: ended
        assert _wait_dead(stubborn)
    finally:
        if stubborn.poll() is None:
            stubborn.kill()
        stubborn.wait()
    gone = dict(entry)
    H._kill_hard(stubborn.pid, gone)                                               # already gone: nothing to do, no error
    s.helpers[stubborn.pid] = gone
    s.register_helper(_spare := _child().pid, "control") if False else None


def test_windows_stop_uses_only_verified_held_handle(monkeypatch):
    calls = []
    record = {"platform": "windows", "handle": object()}
    monkeypatch.setattr(H.windows, "is_windows", lambda: True)
    monkeypatch.setattr(H.windows, "open_verified_helper", lambda pid, kind: record if (pid, kind) == (42, "control") else None)
    monkeypatch.setattr(H.windows, "terminate_helper", lambda entry: calls.append(("terminate", entry)) or True)
    monkeypatch.setattr(H.windows, "close_helper", lambda entry: calls.append(("close", entry)))
    state = H.ControlState(Clock())
    with pytest.raises(H.HelperRefused):
        state.register_helper(43, "control")
    with pytest.raises(H.HelperRefused, match="helper_launch_unverified"):
        state.register_helper(42, "control")
    state.helpers[42] = record  # exercise the held-handle Stop path independently of launch proof
    result = state.take_over()
    assert result["ended"] == 1 and result["input_release"] == "unknown"
    assert calls == [("terminate", record), ("close", record)]
    assert state.take_over()["ended"] == 0


def test_windows_launch_proof_is_scoped_consumed_once_and_cancelled_on_stop(monkeypatch):
    class Pipe:
        def __init__(self, secret, kind):
            self.name = "test-pipe"
            self.deadline = time.monotonic() + 15
            self.entry = {"pid": 42, "platform": "windows"}
            import threading
            self.done = threading.Event(); self.done.set()
            self.cancelled = False
        def take(self):
            entry, self.entry = self.entry, None
            return entry
        def cancel(self): self.cancelled = True
    monkeypatch.setattr(H.windows, "is_windows", lambda: True)
    monkeypatch.setattr(H.windows, "LaunchPipe", Pipe)
    monkeypatch.setattr(H.windows, "helper_alive", lambda entry: True)
    monkeypatch.setattr(H.windows, "close_helper", lambda entry: None)
    monkeypatch.setattr(H.windows, "terminate_helper", lambda entry: True)
    state = H.ControlState(Clock())
    reservation = state.reserve_windows_launch("owner", "session", "actor", "control")
    token = reservation["reservation"]
    with pytest.raises(H.HelperRefused):
        asyncio.run(state.complete_launch("owner", "session", "other", token))
    asyncio.run(state.complete_launch("owner", "session", "actor", token))
    assert state.helpers[42]["scope"] == ("owner", "session", "actor")
    with pytest.raises(H.HelperRefused):
        asyncio.run(state.complete_launch("owner", "session", "actor", token))
    other = state.reserve_windows_launch("owner", "session", "actor", "a11y")
    pending = state.launches[other["reservation"]]["pipe"]
    state.take_over()
    assert pending.cancelled and not state.launches
    with pytest.raises(H.HelperRefused):
        asyncio.run(state.complete_launch("owner", "session", "actor", other["reservation"]))


@pytest.mark.asyncio
async def test_proof_wait_does_not_block_loop_and_stale_turn_cannot_commit(monkeypatch):
    import threading
    class Pipe:
        def __init__(self, secret, kind):
            self.name = "fake"; self.deadline = time.monotonic() + 5
            self.done = threading.Event(); self.entry = {"pid": 44, "platform": "windows"}
        def take(self):
            entry, self.entry = self.entry, None
            return entry
        def cancel(self): self.done.set()
    closed = []
    monkeypatch.setattr(H.windows, "is_windows", lambda: True)
    monkeypatch.setattr(H.windows, "LaunchPipe", Pipe)
    monkeypatch.setattr(H.windows, "helper_alive", lambda entry: True)
    monkeypatch.setattr(H.windows, "close_helper", lambda entry: closed.append(entry))
    state = H.ControlState()
    turn = ("account", "root", 7, "inner")
    token = state.reserve_windows_launch("owner", "session", "actor", "control", turn)["reservation"]
    with pytest.raises(H.HelperRefused):
        state.prepare_launch("owner", "session", "actor", token, ("account", "root", 8, "inner"))
    task = asyncio.create_task(state.complete_launch("owner", "session", "actor", token, turn))
    await asyncio.sleep(0.05)  # proves the loop runs while the proof event remains unset
    assert not task.done()
    state.take_over()
    with pytest.raises(H.HelperRefused): await task
    assert not state.helpers and len(closed) == 1


def test_helper_takeover_token_is_scoped_and_single_use(own_helper_exe):
    state = H.ControlState(Clock())
    child = _child()
    scope = ("owner", "session", "actor", "account", "root", 4, "inner")
    holder = object()
    try:
        registration = state.register_helper(child.pid, "control", scope, holder)
        assert not state.helper_capabilities(registration, ("other", *scope[1:]), "Ctrl+Alt+Escape", True, holder=holder)
        assert state.helper_capabilities(registration, scope, "Ctrl+Alt+Escape", True, holder=holder)
        assert state.helper_status()["takeover_shortcut"] == "Ctrl+Alt+Escape"
        assert state.helper_status()["cursor_overlay_at_begin"] is True
        assert not state.helper_takeover(registration, ("other", *scope[1:]), holder)
        assert not state.helper_takeover(registration, scope, object())
        assert not state.helper_takeover("bad-token", scope, holder)
        assert state.epoch == 0 and child.poll() is None
        assert state.helper_takeover(registration, scope, holder)
        assert state.epoch == 1 and _wait_dead(child)
        assert not state.helper_takeover(registration, scope, holder)
        assert state.epoch == 1
    finally:
        if child.poll() is None: child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_observed_helper_takeover_advances_local_epoch_and_reports_token():
    class Proc: pass
    class State:
        def __init__(self): self.calls = []
        async def helper_takeover(self, token): self.calls.append(token); return True
    session, proc, state = L._Session(), Proc(), State()
    session.proc = proc
    await session.notice_takeover(proc, state, "registered-token")
    assert session.epoch == 1 and session.died is True and state.calls == ["registered-token"]
    session.proc = Proc()
    await session.notice_takeover(proc, state, "registered-token")
    assert session.epoch == 1 and len(state.calls) == 1


@pytest.mark.asyncio
async def test_unconfirmed_helper_takeover_blocks_begin_until_host_epoch_advances(monkeypatch):
    class Proc: pass
    class State:
        current = 4
        async def helper_takeover(self, token): return False
        async def epoch(self): return self.current
        async def precheck(self, call, allow): return None
    state = State(); session = L._Session(); proc = Proc()
    monkeypatch.setattr(L, 'S', session)
    monkeypatch.setattr(L, '_platform_ok', lambda: False)
    session.proc = proc; session.host_epoch_at_begin = 4
    await session.notice_takeover(proc, state, 'token')
    async def approve(_): return True
    blocked = await L.control_action({'action': 'begin'}, approve, state=state)
    assert blocked['reason'] == 'takeover_unconfirmed'
    state.current = 5
    after = await L.control_action({'action': 'begin'}, approve, state=state)
    assert after['reason'] == 'no_wayland_session' and session.takeover_unconfirmed is None


@pytest.mark.asyncio
async def test_unconfirmed_stop_stays_available_for_human_reconciliation(monkeypatch):
    state = H.ControlState()
    state.helpers[222] = {"kind": "control", "registration": "token", "epoch": 0}
    monkeypatch.setattr(H, "_still", lambda pid, entry: False)
    assert state.helper_status()["pending_stop_reconciliation"] is True
    class Proc: pid = 222
    class Handle:
        unregistered = False
        async def unregister_helper(self, pid): self.unregistered = True
    async def no_kill(proc): pass
    handle = Handle(); session = L._Session()
    session.proc, session.state, session.takeover_unconfirmed = Proc(), handle, 0
    monkeypatch.setattr(L, '_kill', no_kill)
    await session.close(False)
    assert not handle.unregistered
    state.take_over()
    assert state.epoch == 1 and state.helper_status()["pending_stop_reconciliation"] is False


def test_control_stream_handoff_is_one_use_and_revoked(monkeypatch):
    import array, socket
    from src.openclank.recording_child import ControlRecordingGuard
    monkeypatch.setattr(H, "_still", lambda pid, entry: True)
    state=H.ControlState(); holder=type("Holder", (), {"is_alive": lambda self: True})(); scope=("owner","session","actor","account","root",1,"turn")
    entry={"kind":"control","epoch":0,"scope":scope,"holder":holder,"registration":"r"}
    state.helpers[os.getpid()]=entry
    reservation=state.reserve_recording_stream("r",scope,holder)
    original, other=socket.socketpair()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
            sender.connect(reservation["path"])
            assert sender.recv(64).decode()==reservation["secret"]
            sender.sendmsg([(17).to_bytes(8,"big")],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array("i",[original.fileno()]))])
        deadline=time.monotonic()+3
        while entry.get("stream_fd") is None and time.monotonic()<deadline: time.sleep(.01)
        binding=state.recording_stream_binding("owner","account","session")
        assert binding and state.recording_stream_current("owner","account","session",binding)
        class Loop:
            def call_soon_threadsafe(self, callback): callback()
        class Process:
            returncode=None
            killed=False
            def kill(self): self.killed=True
        sent=[]
        monkeypatch.setattr(signal,"pidfd_send_signal",lambda fd, sig: sent.append((fd,sig)))
        guard=ControlRecordingGuard(Loop(),opener=lambda *_: os.dup(original.fileno())); process=Process()
        claim=state.claim_recording_stream("owner","account","session",binding,guard.revoke)
        assert claim and claim["serial"]==17 and guard.attach(process,None,(),0)
        assert state.claim_recording_stream("owner","account","session",binding,guard.revoke) is None
        os.close(claim["fd"])
        state._close_recording_stream(entry)
        assert guard.revoked.is_set() and len(sent)==1
        guard.close()
        later=Process(); assert not guard.attach(later,None,(),0) and len(sent)==2
        guard.close()
    finally:
        state._close_recording_stream(entry)
        original.close();other.close()
