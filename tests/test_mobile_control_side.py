"""Owner-scoped mobile control placement contract tests."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.prefs_routes as prefs_routes


def test_mobile_control_side_is_normalized_validated_and_owner_scoped(tmp_path, monkeypatch):
    current = {"user": "alice"}
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(prefs_routes, "get_current_user", lambda request=None: current["user"])
    app = FastAPI()
    app.include_router(prefs_routes.setup_prefs_routes())
    http = TestClient(app)

    assert http.get("/api/prefs/mobile_control_side").json() == {
        "key": "mobile_control_side",
        "value": "system",
    }
    assert http.put("/api/prefs/mobile_control_side", json={"value": " LEFT "}).json() == {
        "key": "mobile_control_side",
        "value": "left",
    }
    assert http.put("/api/prefs/mobile_control_side", json={"value": "bottom"}).status_code == 422

    current["user"] = "bob"
    assert http.get("/api/prefs/mobile_control_side").json()["value"] == "system"

    # A corrupt historical value is fail-safe on both the dedicated and bulk
    # reads, without rewriting another user's preferences as a side effect.
    prefs_routes._save_for_user("bob", {"mobile_control_side": "unexpected"})
    assert http.get("/api/prefs/mobile_control_side").json()["value"] == "system"
    assert http.get("/api/prefs").json()["mobile_control_side"] == "system"
    raw = json.loads((tmp_path / "user_prefs.json").read_text(encoding="utf-8"))
    assert raw["_users"]["bob"]["mobile_control_side"] == "unexpected"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for browser-module contract")
def test_mobile_control_side_resolution_keeps_handedness_out_of_layout():
    repo = Path(__file__).resolve().parent.parent
    module = repo / "static" / "js" / "sidebar-layout.js"
    script = f"""
      import {{ normalizeMobileControlSide, resolveMobileControlSide }} from {json.dumps(module.as_uri())};
      console.log(JSON.stringify({{
        valid: normalizeMobileControlSide(' LEFT '),
        invalid: normalizeMobileControlSide('ambidextrous'),
        explicitLeft: resolveMobileControlSide('left', 'right'),
        explicitRight: resolveMobileControlSide('right', 'left'),
        systemLegacy: resolveMobileControlSide('system', 'left'),
        systemDefault: resolveMobileControlSide('system', null),
        handednessIsNotAnInput: resolveMobileControlSide(undefined, 'left'),
      }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        cwd=repo,
    )

    assert json.loads(result.stdout) == {
        "valid": "left",
        "invalid": "system",
        "explicitLeft": "left",
        "explicitRight": "right",
        "systemLegacy": "left",
        "systemDefault": "right",
        "handednessIsNotAnInput": "left",
    }
