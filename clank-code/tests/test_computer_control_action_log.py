"""P5: the actions-only text log (M2-P05). Off by default; On writes one plain line per action from the receipts, nothing private."""
import json
import tempfile
from pathlib import Path

import pytest

from src.openclank.computer_control import action_log as AL
from src.openclank.computer_control import a11y_client as C
from src.openclank.computer_control import linux as L
from src.openclank.computer_control import protocol as P
from tests.test_computer_control_tool import BEGIN, CLICK, KEY, FRAME, Env, run
from tests.test_computer_control_a11y import Env as AEnv, CLICK_EL, SET_EL, STATE, SNAP

ON = {"control": True, "accessibility": True, "action_log": True}
OFF = {"control": True, "accessibility": True, "action_log": False}


@pytest.fixture(autouse=True)
def log_file():
    AL.PATH_OVERRIDE = str(Path(tempfile.mkdtemp()) / "desktop-actions.log")
    yield AL.PATH_OVERRIDE
    AL.PATH_OVERRIDE = None


def lines():
    return AL.read()


def test_off_writes_nothing_and_creates_no_file(log_file):
    async def go():
        e = Env()
        for allow in (None, OFF):
            await L.control_action(BEGIN, e.approve, allow)
            await L.control_action(CLICK, e.approve, allow)
            await L.control_status({"action": "stop"})
        return e
    run(go())
    assert not Path(log_file).exists() and lines() == []
    AL.record_event("human_stop", False)
    assert not Path(log_file).exists()


def test_on_writes_one_line_per_action_with_no_private_field():
    secret = "top-secret-typed-text"
    async def go():
        e = Env()
        await L.control_action(BEGIN, e.approve, ON)
        await L.control_action(CLICK, e.approve, ON)
        await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": secret}, e.approve, ON)
        await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["Control", "s"]}, e.approve, ON)
        await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "paste", "text": secret}, e.approve, ON)
        await L.control_status({"action": "stop"})
    run(go())
    got = lines()
    assert len(got) == 5, got
    assert [x.split()[1] for x in got] == ["action=begin", "action=click", "action=type", "action=key", "action=paste"]
    assert got[2].count("chars=21") == 1 and "result=sent" in got[1] and "approved=yes" in got[1] and got[0].startswith("20")
    text = "\n".join(got)
    assert secret not in text and "Control" not in text and "text_sha256" not in text and "keys=2" in got[3]


def test_element_actions_and_reads_log_app_role_and_hashes_only():
    async def go():
        e = AEnv()
        await C.inspect_action(STATE, e.approve, ON)
        await L.control_action(CLICK_EL, e.approve, ON)
        await L.control_action(SET_EL, e.approve, ON)
        await C.A.close()
    run(go())
    got = lines()
    assert [x.split()[1] for x in got] == ["action=app_state", "action=click_element", "action=set_value"]
    text = "\n".join(got)
    assert "app=Demo" in got[1] and "role=push button" in got[1] and "name#=" in got[1] and "window#=" in got[1] and "result=performed" in got[1]
    for private in ("bob secret", "Send", "Main"):
        assert private not in text, private
    assert "chars=10" in got[2]


def test_refused_and_stopped_actions_are_logged_as_such():
    async def go():
        e = Env()
        await L.control_action(BEGIN, e.approve, ON)
        e.allow = False
        await L.control_action(CLICK, e.approve, ON)                      # refused by the person
        e.allow = True

        async def stop_now():
            await L.take_over()
        orig = e.approve

        async def approve(d):
            await stop_now()
            return await orig(d)
        await L.control_action(KEY, approve, ON)                           # stopped while waiting
        await L.control_action({"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 1, "y": 1, "frame": {"width": 9, "height": 9}}, e.approve, ON)   # no session now
    run(go())
    results = [x.split()[2] for x in lines()]
    assert results == ["result=active", "result=approval_required", "result=taken_over", "result=unavailable"], results
    # setting Off at the typed-refusal point: a refusal is not logged when the log is off
    AL.clear()
    run(L.control_action(BEGIN, Env().approve, {"control": False, "accessibility": False, "action_log": False}))
    assert lines() == []


def test_clear_empties_and_read_is_bounded(log_file):
    for i in range(30):
        AL.record_event("human_stop", True)
    assert len(AL.read(5)) == 5 and len(AL.read()) == 30
    assert oct(Path(log_file).stat().st_mode & 0o777) == "0o600"
    AL.clear()
    assert AL.read() == [] and Path(log_file).exists()


def test_routes_read_and_clear_for_the_admin_only():
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import routes.desktop_control_routes as R
    import src.settings as S
    app = FastAPI()
    app.include_router(R.setup_desktop_control_routes())
    who = {"user": None}

    class Mgr:
        def is_admin(self, u): return u == "root"
    app.state.auth_manager = Mgr()

    def fake_require_user(request):
        if who["user"] is None:
            raise HTTPException(401, "Not authenticated")
        return who["user"]
    R.require_user = fake_require_user
    R.is_internal_tool_identity = lambda u: u == "internal-tool"
    real = S.get_user_setting
    S.get_user_setting = lambda key, owner, default=None: key == "desktop_action_log_enabled"
    try:
        c = TestClient(app)
        for u, code in ((None, 401), ("internal-tool", 403), ("alice", 403)):
            who["user"] = u
            assert c.get("/api/desktop-control/actions").status_code == code and c.delete("/api/desktop-control/actions").status_code == code
        who["user"] = "root"
        assert c.get("/api/desktop-control/actions").json() == {"enabled": True, "lines": []}
        assert c.post("/api/desktop-control/stop").status_code == 200           # logged as a human stop (setting On)
        got = c.get("/api/desktop-control/actions").json()
        assert len(got["lines"]) == 1 and "action=human_stop" in got["lines"][0]
        assert c.delete("/api/desktop-control/actions").json() == {"cleared": True}
        assert c.get("/api/desktop-control/actions").json()["lines"] == []
        S.get_user_setting = lambda key, owner, default=None: False
        c.post("/api/desktop-control/stop")
        assert c.get("/api/desktop-control/actions").json() == {"enabled": False, "lines": []}
    finally:
        S.get_user_setting = real
