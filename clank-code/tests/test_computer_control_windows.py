"""P2: the Windows adapter, protocol level, against a FAKE helper (run on Linux). The PowerShell helper itself was NOT run on Windows here."""
import asyncio
import json
import os
import re
import signal
import sys
import tempfile
from pathlib import Path

import pytest

from src.openclank.computer_control import a11y_client as C
from src.openclank.computer_control import linux as L
from src.openclank.computer_control import protocol as P
from src.openclank.computer_control import windows as W

SCRIPT = Path(W.SCRIPT)
SNAP = "a1b2c3d4e5f60718"
FAKE = r'''
import json, os, sys
SNAP = "a1b2c3d4e5f60718"
def out(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
mode = sys.argv[1]
for line in sys.stdin:
    r = json.loads(line); op = r["op"]
    if op == "launch_proof":
        out({"ok": True, "state": "launch_proof_sent"}); continue
    if mode == "a11y":
        if op == "handshake": out({"ok": True, "backend": "uia/1", "accessibility": "ready", "actions": [], "limits": {"nodes": 1200, "depth": 64, "value_chars": 500}})
        elif op == "app_state": out({"ok": True, "snapshot_id": SNAP, "target": {"version": 1, "backend": "uia/1", "processStartIdentity": "7:99"}, "app": {"name": "notepad", "pid": 7},
                                     "window": {"index": 0, "title": "Untitled"}, "tree": {"id": "root", "role": "window", "name": "Untitled", "children": [{"id": "0", "role": "button", "name": "OK", "actions": ["Invoke"]}]}})
        elif op == "act" and r["action"] == "select_text": out({"ok": False, "code": "unsupported_action"})
        elif op == "act": out({"ok": True, "outcome": "performed", "verified": "unknown", "window_changed": False, "target_before": {"version": 1}, "target_after": {"version": 1}})
        else: out({"ok": False, "code": "invalid_request"})
    else:
        if op == "begin": out({"ok": True, "state": "active", "stream": {"width": 100, "height": 50}})
        elif op == "stop": out({"ok": True, "state": "stopped"}); sys.exit(0)
        elif op in ("click", "key", "type"): out({"ok": True, "outcome": "sent", "actions_left": 19})
        else: out({"ok": False, "code": "unsupported_action"})
'''
TARGET = {"app": "notepad", "window": "Untitled", "role": "button", "name": "OK"}
OBS = "aaaaaaaaaaaaaaaa"


@pytest.fixture
def windows_env(monkeypatch):
    d = Path(tempfile.mkdtemp()); (d / "fake.py").write_text(FAKE)
    monkeypatch.setattr(W, "is_windows", lambda: True)
    monkeypatch.setattr(W, "platform_ok", lambda: True)
    monkeypatch.setattr(W, "helper_argv", lambda mode: [sys.executable, "-B", str(d / "fake.py"), mode])
    monkeypatch.setattr(W, "spawn_kwargs", lambda: {"start_new_session": True})
    C.A = C._Helper(); L.S = L._Session()
    yield d


async def approve(_digest):
    return True


class FakeRegisteredState:
    async def reserve_windows_launch(self, kind): return {"reservation": "fake", "pipe": "fake", "secret": "fake"}
    async def complete_windows_launch(self, token): assert token == "fake"
    async def cancel_windows_launch(self, token): pass
    async def register_helper(self, pid, kind): pass
    async def unregister_helper(self, pid): pass
    async def epoch(self): return 0
    async def precheck(self, call, allow): return None
    async def consume_observation(self, call): pass
    async def window_observed(self, *args): return None


def run(coro):
    return asyncio.run(coro)


def test_adapter_is_chosen_by_platform_and_builds_the_powershell_command(monkeypatch):
    assert W.is_windows() is False and L._helper_argv() is None or L._helper_argv()[-1] == "--control-v1"
    monkeypatch.setattr(W, "is_windows", lambda: True)
    monkeypatch.setattr(W, "powershell", lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    a, c = W.helper_argv("a11y"), W.helper_argv("control")
    assert a[:5] == [W.powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"] and a[-2:] == ["-Mode", "a11y"] and c[-2:] == ["-Mode", "control"]
    assert a[a.index("-File") + 1] == str(SCRIPT)
    assert L._helper_argv() == c and C._helper_argv() == a, "linux.py and a11y_client.py delegate on Windows"
    assert W.helper_argv("other") is None
    monkeypatch.setattr(W, "powershell", lambda: None)
    assert W.helper_argv("a11y") is None and C._helper_argv() is None


def test_arm64_is_deferred_and_readiness_reports_what_is_present(monkeypatch):
    monkeypatch.setattr(W, "is_windows", lambda: True)
    for machine, ok in (("AMD64", True), ("x86_64", True), ("ARM64", False), ("aarch64", False)):
        monkeypatch.setattr("platform.machine", lambda m=machine: m)
        assert W.platform_ok() is ok, machine
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    monkeypatch.setattr(W, "powershell", lambda: None)
    assert L._input_readiness() == {"state": "driver_missing"} and C.readiness() == {"state": "driver_missing"}
    f = W.facts()
    assert set(f) == {"platform", "machine", "powershell", "helper_script"} and f["powershell"] is False and f["helper_script"] is True
    assert run(L.probe_input())["state"] == "driver_missing"


def test_reads_and_element_actions_use_the_shared_client_unchanged(windows_env):
    async def go():
        state = FakeRegisteredState()
        hs = await C.inspect_action({"action": "handshake"}, approve, state=state)
        st = await C.inspect_action({"action": "app_state", "app": {"name": "notepad"}}, approve, state=state)
        el = {"action": "click_element", "snapshot_id": SNAP, "element": "0", "target": TARGET}
        r = await L.control_action(el, approve, state=state)
        sel = await L.control_action({"action": "select_text", "snapshot_id": SNAP, "element": "0", "target": TARGET, "start": 0, "end": 1}, approve, state=state)
        await C.A.close()
        return hs, st, r, sel
    hs, st, r, sel = run(go())
    assert hs["outcome"] == "ready" and hs["result"]["backend"] == "uia/1" and st["result"]["snapshot_id"] == SNAP
    assert r["outcome"] == "performed" and "not verified" in r["message"]
    assert sel["outcome"] == "unsupported_action" and sel["exit_code"] == 1, "typed refusal, not a silent no-op"


def test_pointer_and_keys_work_and_the_rest_answers_unsupported_without_ending_the_session(windows_env):
    fr = {"width": 100, "height": 50}
    async def go():
        state = FakeRegisteredState()
        out = {}
        out["begin"] = await L.control_action({"action": "begin"}, approve, state=state)
        out["click"] = await L.control_action({"observation": OBS, "action": "click", "x": 1, "y": 2, "frame": fr}, approve, state=state)
        out["key"] = await L.control_action({"observation": OBS, "action": "hotkey", "keys": ["Control", "s"]}, approve, state=state)
        out["type"] = await L.control_action({"observation": OBS, "action": "type", "text": "hi"}, approve, state=state)
        for a, extra in (("move", {"x": 1, "y": 2, "frame": fr}), ("scroll", {"x": 1, "y": 2, "direction": "up", "amount": 1, "frame": fr}),
                         ("drag", {"from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "frame": fr}), ("paste", {"text": "x"})):
            out[a] = await L.control_action({"observation": OBS, "action": a, **extra}, approve, state=state)
        out["after"] = await L.control_action({"observation": OBS, "action": "key", "keys": ["a"]}, approve, state=state)
        await L.control_status({"action": "stop"})
        return out
    out = run(go())
    assert [out[k]["outcome"] for k in ("begin", "click", "key", "type")] == ["active", "sent", "sent", "sent"]
    assert all(out[k]["outcome"] == "unsupported_action" for k in ("move", "scroll", "drag", "paste"))
    assert out["after"]["outcome"] == "sent", "an unsupported operation does not end the session"
    assert P.keeps_session("unsupported_action") and P.parse_reply(b'{"ok":false,"code":"unsupported_action"}\n', "action") == {"ok": False, "code": "unsupported_action"}


def test_the_helper_script_covers_every_operation_name_the_shared_protocol_sends():
    text = SCRIPT.read_text()
    for token in ("handshake", "list_apps", "list_windows", "app_state", "'act'", "'begin'", "'click'", "'key'", "'type'", "'stop'", "unsupported_action",
                  "stale_element", "window_changed", "target_gone", "snapshot_expired", "app_excluded", "not_actionable", "frame_mismatch", "out_of_bounds",
                  "InvokePattern", "ValuePattern", "SendInput" if False else "SendInput", "Release-All", "[hidden]", "keepassxc", "bitwarden", "1200", "64"):
        assert token in text, token
    assert "Add-Type -AssemblyName" in text and "http" not in text.lower().replace("https://", "")[:0] and "Invoke-WebRequest" not in text and "DownloadString" not in text
    stripped = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "", text)
    for a, b in ("{}", "()", "[]"):
        assert stripped.count(a) == stripped.count(b), (a, b)


def test_helper_command_must_bind_exact_script_and_mode():
    argv = ["/windows/powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "/app/windows_uia.ps1", "-Mode", "control"]
    assert W._matches_command(argv[0], argv, argv)
    assert not W._matches_command(argv[0], argv[:6] + ["/other/windows_uia.ps1"] + argv[7:], argv)
    assert not W._matches_command(argv[0], argv[:-1] + ["a11y"], argv)
    assert not W._matches_command("/other/powershell.exe", argv, argv)
    assert not W._matches_command(argv[0], argv + ["-extra"], argv)


def test_windows_helper_requires_registered_state(windows_env):
    async def go():
        handshake = await C.inspect_action({"action": "handshake"}, approve)
        begin = await L.control_action({"action": "begin"}, approve)
        return handshake, begin
    handshake, begin = run(go())
    assert handshake["outcome"] == "unavailable"
    assert begin["outcome"] == "unavailable"
    assert not C.A.active() and not L.S.active()
