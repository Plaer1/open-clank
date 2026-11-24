"""Fake-framework check only; no macOS runtime or live input."""
import io
import asyncio
import json

from src.openclank.computer_control import macos_helper as H


def test_scaled_click_and_failed_release_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(H.macos, "permission_status", lambda: {"accessibility": True})
    monkeypatch.setattr(H.macos, "primary_geometry", lambda: (10.0, 20.0, 100.0, 50.0, 200, 100))
    monkeypatch.setattr(H.macos, "primary_display_id", lambda: 10)
    def mouse(kind, x, y, button):
        calls.append((kind, x, y, button))
        if len(calls) == 2: raise OSError("fake release failure")
    monkeypatch.setattr(H.macos, "post_mouse", mouse)
    monkeypatch.setattr(H.signal, "signal", lambda *args: None)
    requests = [{"v": 1, "op": "begin"}, {"v": 1, "op": "click", "x": 100, "y": 50, "button": "left", "frame": {"width": 200, "height": 100}}]
    monkeypatch.setattr(H.sys, "stdin", io.StringIO("".join(json.dumps(r) + "\n" for r in requests)))
    output = io.StringIO(); monkeypatch.setattr(H.sys, "stdout", output)
    H.run_control()
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["state"] == "active"
    assert replies[1] == {"ok": False, "code": "outcome_unknown"}
    assert calls == [(1, 60.0, 45.0, 0), (2, 60.0, 45.0, 0), (2, 60.0, 45.0, 0)]


def test_ax_snapshot_hides_secure_children_and_refuses_stale_process(monkeypatch):
    import os
    app = {"pid": 123, "name": "Editor", "exe": "/Applications/Editor", "uid": os.getuid(), "start": 100}
    current = [app]
    tree = {"window": {"AXRole": "AXWindow", "AXTitle": "Draft", "AXIdentifier": "w"},
            "secure": {"AXRole": "AXSecureTextField", "AXTitle": "Password", "AXIdentifier": "p"},
            "plain": {"AXRole": "AXTextField", "AXTitle": "Note", "AXValue": "hello world"},
            "token": {"AXRole": "AXTextField", "AXTitle": "Note", "AXValue": "sk-secret"}}
    monkeypatch.setattr(H.macos, "permission_status", lambda: {"accessibility": True})
    monkeypatch.setattr(H.macos, "processes", lambda: [app])
    monkeypatch.setattr(H.macos, "process_info", lambda pid: current[0])
    monkeypatch.setattr(H.macos, "ax_application", lambda pid: "app")
    monkeypatch.setattr(H.macos, "ax_text", lambda obj, attr: tree.get(obj, {}).get(attr, ""))
    monkeypatch.setattr(H.macos, "ax_array", lambda obj, attr, limit=1200: ["window"] if obj == "app" and attr == "AXWindows" else ["secure", "plain", "token"] if obj == "window" and attr == "AXChildren" else ["leak"] if obj == "secure" else [])
    monkeypatch.setattr(H.macos, "ax_release", lambda obj: None)
    monkeypatch.setattr(H.macos, "ax_retain", lambda obj: obj)
    monkeypatch.setattr(H.macos, "ax_equal", lambda left, right: left == right)
    monkeypatch.setattr(H.macos, "ax_action_names", lambda obj: [])
    output = io.StringIO(); monkeypatch.setattr(H.sys, "stdout", output)
    def requests():
        yield json.dumps({"v": 1, "op": "app_state", "app": {"pid": 123}}) + "\n"
        snapshot = json.loads(output.getvalue().splitlines()[-1])
        assert snapshot["tree"]["children"][0]["hidden"] is True
        assert "children" not in snapshot["tree"]["children"][0]
        assert snapshot["tree"]["children"][1]["value"] == "hello world"
        assert snapshot["tree"]["children"][2]["value"] == "[hidden]"
        current[0] = {**app, "start": 101}
        yield json.dumps({"v": 1, "op": "act", "snapshot_id": snapshot["snapshot_id"], "element": "0", "action": "click"}) + "\n"
    monkeypatch.setattr(H.sys, "stdin", requests())
    H.run_a11y()
    assert json.loads(output.getvalue().splitlines()[-1]) == {"ok": False, "code": "target_gone"}


def test_mac_launch_proof_uses_bound_task_and_owner_scope(monkeypatch):
    import time
    import pytest
    from src.openclank.computer_control import hoststate as state_module
    class Pipe:
        def __init__(self, secret, kind):
            self.name = "/tmp/oc-fake/s"; self.deadline = time.monotonic() + 15
            self.entry = {"pid": 321, "platform": "macos", "port": 77}
            import threading
            self.done = threading.Event(); self.done.set()
        def take(self):
            entry, self.entry = self.entry, None
            return entry
        def cancel(self): pass
    calls = []
    monkeypatch.setattr(state_module.sys, "platform", "darwin")
    monkeypatch.setattr(H.macos, "LaunchSocket", Pipe)
    monkeypatch.setattr(H.macos, "helper_alive", lambda entry: True)
    monkeypatch.setattr(H.macos, "terminate_helper", lambda entry: calls.append(("terminate", entry["port"])) or True)
    monkeypatch.setattr(H.macos, "close_helper", lambda entry: calls.append(("close", entry["port"])))
    state = state_module.ControlState()
    token = state.reserve_macos_launch("owner", "session", "actor", "control")["reservation"]
    with pytest.raises(state_module.HelperRefused): state.register_helper(321, "control")
    asyncio.run(state.complete_launch("owner", "session", "actor", token))
    assert not state.end_proved_helper("owner", "session", "other", token)
    assert state.end_proved_helper("owner", "session", "actor", token)
    assert calls == [("terminate", 77), ("close", 77)]
    assert not state.end_proved_helper("owner", "session", "actor", token)


def test_pixel_action_refuses_when_capture_display_is_not_bound(monkeypatch):
    import asyncio
    from src.openclank.computer_control import linux as control
    monkeypatch.setattr(control.sys, "platform", "darwin")
    async def approve(_): return True
    result = asyncio.run(control.control_action({"action": "click", "x": 1, "y": 1,
        "frame": {"width": 100, "height": 100}, "observation": "aaaaaaaaaaaaaaaa"}, approve))
    assert result["outcome"] == "unavailable" and result["reason"] == "capture_target_unbound"


def test_observation_requires_same_primary_display_even_when_sizes_match(monkeypatch):
    from src.openclank.computer_control import hoststate as state_module
    monkeypatch.setattr(state_module.sys, "platform", "darwin")
    current = [10]
    monkeypatch.setattr(H.macos, "primary_display_id", lambda: current[0])
    monkeypatch.setattr(H.macos, "primary_geometry", lambda: (0.0, 0.0, 100.0, 50.0, 200, 100))
    state = state_module.ControlState()
    target = {"source": "display", "id": 11, "bounds": [0.0, 0.0, 100.0, 50.0]}
    other = state.capture_observed("owner", "session", "actor", 200, 100, "resource", target)
    call = {"action": "click", "observation": other["observationId"]}
    assert state.precheck("owner", "session", "actor", call, {}) == "capture_target_unbound"
    target["id"] = 10
    primary = state.capture_observed("owner", "session", "actor", 200, 100, "resource", target)
    call["observation"] = primary["observationId"]
    assert state.precheck("owner", "session", "actor", call, {}) is None
    current[0] = 11
    assert state.precheck("owner", "session", "actor", call, {}) == "capture_target_unbound"
    current[0] = 10
    state.pixel[primary["observationId"]]["epoch"] -= 1
    assert state.precheck("owner", "session", "actor", call, {}) == "observation_stale"


def test_ax_secondary_requires_exposed_action(monkeypatch):
    import os
    app = {"pid": 123, "name": "Editor", "exe": "/Applications/Editor", "uid": os.getuid(), "start": 100}
    performed = []
    monkeypatch.setattr(H.macos, "permission_status", lambda: {"accessibility": True})
    monkeypatch.setattr(H.macos, "processes", lambda: [app])
    monkeypatch.setattr(H.macos, "process_info", lambda pid: app)
    monkeypatch.setattr(H.macos, "ax_application", lambda pid: "app")
    monkeypatch.setattr(H.macos, "ax_text", lambda obj, attr: {"AXRole": "AXWindow", "AXTitle": "Draft"}.get(attr, ""))
    monkeypatch.setattr(H.macos, "ax_array", lambda obj, attr, limit=1200: ["window"] if obj == "app" and attr == "AXWindows" else [])
    monkeypatch.setattr(H.macos, "ax_release", lambda obj: None)
    monkeypatch.setattr(H.macos, "ax_retain", lambda obj: obj)
    monkeypatch.setattr(H.macos, "ax_equal", lambda a, b: a == b)
    monkeypatch.setattr(H.macos, "ax_action_names", lambda obj: ["AXShowMenu"])
    monkeypatch.setattr(H.macos, "ax_perform", lambda obj, name: performed.append(name) or True)
    output = io.StringIO(); monkeypatch.setattr(H.sys, "stdout", output)
    def requests():
        yield json.dumps({"v": 1, "op": "app_state", "app": {"pid": 123}}) + "\n"
        snap = json.loads(output.getvalue().splitlines()[-1])["snapshot_id"]
        for name in ("AXUnknown", "AXShowMenu"):
            yield json.dumps({"v": 1, "op": "act", "snapshot_id": snap, "element": "root", "action": "secondary", "name": name}) + "\n"
    monkeypatch.setattr(H.sys, "stdin", requests())
    H.run_a11y()
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[1] == {"ok": False, "code": "unsupported_action"}
    assert replies[2]["outcome"] == "performed" and performed == ["AXShowMenu"]
