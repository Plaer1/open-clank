"""Sliding session expiration tests.

Sessions renew on activity (throttled) up to an absolute cap from creation,
so an open app no longer dies at a fixed 7-day wall while a genuinely idle
session still expires. See .futures/QOL-PASS-2026-08-14/slice_2.md.
"""

import importlib
import sys
import time
import types
from pathlib import Path

import pytest

from tests.helpers.import_state import clear_module


def _real_core_package():
    root = Path(__file__).resolve().parent.parent
    core_path = str(root / "core")
    core = sys.modules.get("core")
    if core is None:
        core = types.ModuleType("core")
        sys.modules["core"] = core
    core.__path__ = [core_path]
    clear_module("core.auth")
    return core


def _auth_module():
    _real_core_package()
    return importlib.import_module("core.auth")


def _make_manager(tmp_path):
    auth_mod = _auth_module()
    auth_mod._hash_password = lambda password: f"hash:{password}"
    auth_mod._verify_password = lambda password, hashed: hashed == f"hash:{password}"
    mgr = auth_mod.AuthManager(str(tmp_path / "auth.json"))
    assert mgr.create_user("alice", "pw", is_admin=False)
    return mgr, auth_mod


def test_session_slides_after_touch_interval(tmp_path):
    mgr, auth_mod = _make_manager(tmp_path)
    token = mgr.create_session("alice", "pw")
    session = mgr._sessions[token]
    original_expiry = session["expiry"]

    # Simulate activity well past the touch throttle.
    session["touched"] -= auth_mod.SESSION_TOUCH_INTERVAL + 1

    valid, refresh_cookie = mgr.validate_session(token)

    assert valid is True
    assert session["expiry"] > original_expiry
    # Not a remember-me login: the middleware must not renew the cookie.
    assert refresh_cookie is False


def test_remember_me_session_requests_cookie_refresh(tmp_path):
    mgr, auth_mod = _make_manager(tmp_path)
    token = mgr.create_session_trusted("alice", remember=True)
    session = mgr._sessions[token]
    session["touched"] -= auth_mod.SESSION_TOUCH_INTERVAL + 1

    valid, refresh_cookie = mgr.validate_session(token)

    assert valid is True
    assert refresh_cookie is True


def test_cookie_lifetime_uses_the_same_fake_clock_tick_as_token_issuance(
    tmp_path, monkeypatch
):
    now = [1000.25]
    mgr, auth_mod = _make_manager(tmp_path)
    monkeypatch.setattr(auth_mod.time, "time", lambda: now[0])

    token = mgr.create_session_trusted("alice", remember=True)
    session = mgr._sessions[token]
    now[0] += 0.9  # route latency must not floor the cookie to TOKEN_TTL - 1

    lifetime = mgr.session_cookie_lifetime(token)
    assert lifetime["max_age"] == auth_mod.TOKEN_TTL
    assert lifetime["expires"].timestamp() == pytest.approx(
        session["created"] + auth_mod.TOKEN_TTL
    )


def test_cookie_renewal_rounds_to_shared_absolute_cap_and_server_wins_boundary(
    tmp_path, monkeypatch
):
    now = [2000.25]
    mgr, auth_mod = _make_manager(tmp_path)
    monkeypatch.setattr(auth_mod.time, "time", lambda: now[0])
    token = mgr.create_session_trusted("alice", remember=True)
    session = mgr._sessions[token]
    absolute_expiry = session["created"] + auth_mod.SESSION_ABSOLUTE_TTL

    now[0] = absolute_expiry - 2.4
    # Model an already-active session whose preceding slide reached the cap.
    session["expiry"] = absolute_expiry
    session["touched"] = now[0] - auth_mod.SESSION_TOUCH_INTERVAL - 1
    valid, refresh_cookie = mgr.validate_session(token)

    assert valid is True
    assert refresh_cookie is True
    lifetime = mgr.session_cookie_lifetime(token)
    assert lifetime["max_age"] == 3  # explicit ceiling of the shared 2.4s
    assert lifetime["expires"].timestamp() == pytest.approx(absolute_expiry)

    now[0] = absolute_expiry
    assert mgr.validate_session(token) == (False, False)


def test_slide_is_throttled(tmp_path):
    mgr, auth_mod = _make_manager(tmp_path)
    token = mgr.create_session("alice", "pw")
    session = mgr._sessions[token]

    # Just touched (creation counts): a validation inside the throttle window
    # must not extend or persist again.
    valid, refresh_cookie = mgr.validate_session(token)

    assert valid is True
    assert refresh_cookie is False
    assert abs(session["expiry"] - (session["created"] + auth_mod.TOKEN_TTL)) < 1.0


def test_absolute_cap_rejects_even_when_sliding_window_looks_valid(tmp_path):
    mgr, auth_mod = _make_manager(tmp_path)
    token = mgr.create_session("alice", "pw")
    session = mgr._sessions[token]

    # Session created longer ago than the absolute cap; still inside its
    # current sliding window.
    session["created"] -= auth_mod.SESSION_ABSOLUTE_TTL + 1
    session["touched"] = session["created"]  # past the throttle interval

    valid, refresh_cookie = mgr.validate_session(token)

    assert valid is False
    assert token not in mgr._sessions
    assert refresh_cookie is False


def test_legacy_session_without_stamps_slides(tmp_path):
    mgr, auth_mod = _make_manager(tmp_path)
    token = mgr.create_session("alice", "pw")
    session = mgr._sessions[token]

    # Sessions persisted before sliding expiration carry only username+expiry.
    legacy_created = session["expiry"] - auth_mod.TOKEN_TTL
    del session["created"]
    del session["touched"]
    del session["remember"]

    valid, _refresh = mgr.validate_session(token)

    assert valid is True
    assert session["created"] == legacy_created
    assert session["expiry"] <= legacy_created + auth_mod.SESSION_ABSOLUTE_TTL


def test_renewal_is_clamped_at_absolute_cap(tmp_path):
    mgr, auth_mod = _make_manager(tmp_path)
    token = mgr.create_session("alice", "pw")
    session = mgr._sessions[token]
    session["created"] = time.time() - auth_mod.SESSION_ABSOLUTE_TTL + 30
    session["expiry"] = time.time() + auth_mod.TOKEN_TTL
    session["touched"] = session["created"]

    valid, refresh_cookie = mgr.validate_session(token)

    assert valid is True
    assert session["expiry"] <= session["created"] + auth_mod.SESSION_ABSOLUTE_TTL
    assert refresh_cookie is False


def test_expired_session_still_rejected(tmp_path):
    mgr, auth_mod = _make_manager(tmp_path)
    token = mgr.create_session("alice", "pw")
    mgr._sessions[token]["expiry"] = time.time() - 1

    valid, refresh_cookie = mgr.validate_session(token)

    assert valid is False
    assert refresh_cookie is False
    assert token not in mgr._sessions
    assert mgr.validate_token(token) is False


def test_validate_token_remains_plain_bool(tmp_path):
    mgr, _auth_mod = _make_manager(tmp_path)
    token = mgr.create_session("alice", "pw")

    assert mgr.validate_token(token) is True
    assert mgr.validate_token("nope") is False
    assert mgr.validate_token(None) is False
