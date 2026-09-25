"""Tests for auth policy endpoint and password length validation."""

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

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
    auth_path = tmp_path / "auth.json"
    mgr = auth_mod.AuthManager(str(auth_path))
    return mgr


async def _immediate_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


# ── AuthManager.policy() ───────────────────────────────────────────────


def test_policy_returns_password_min_length(tmp_path):
    mgr = _make_manager(tmp_path)
    policy = mgr.policy()
    assert policy["password_min_length"] == 8


def test_policy_returns_reserved_usernames(tmp_path):
    mgr = _make_manager(tmp_path)
    policy = mgr.policy()
    assert "internal-tool" in policy["reserved_usernames"]
    assert "api" in policy["reserved_usernames"]
    assert "demo" in policy["reserved_usernames"]
    assert "system" in policy["reserved_usernames"]
    assert "shared" in policy["reserved_usernames"]
    assert "local" in policy["reserved_usernames"]
    # Owner-identity mapping names are refused as stored human identities.
    assert "default" in policy["reserved_usernames"]
    assert "__odysseus_local__" in policy["reserved_usernames"]
    assert "local-installation" in policy["reserved_usernames"]
    assert policy["reserved_username_prefixes"] == ["agent:", "deleted:", "user:"]
    assert isinstance(policy["reserved_usernames"], list)


def test_policy_returns_signup_enabled(tmp_path):
    mgr = _make_manager(tmp_path)
    policy = mgr.policy()
    assert policy["signup_enabled"] is False  # default


def test_account_lifecycle_fence_denies_but_does_not_destroy_session(tmp_path):
    mgr = _make_manager(tmp_path)
    assert mgr.setup("admin", "long-enough-password")
    token = mgr.create_session_trusted("admin")
    fenced = {"admin"}
    mgr.configure_account_lifecycle_fence(lambda owner: owner in fenced)

    assert mgr.is_account_lifecycle_fenced("admin") is True
    assert mgr.get_username_for_token(token) is None
    assert mgr.validate_token(token) is False
    assert token in mgr._sessions

    fenced.clear()
    assert mgr.get_username_for_token(token) == "admin"


def test_policy_returns_session_days(tmp_path):
    mgr = _make_manager(tmp_path)
    policy = mgr.policy()
    assert policy["session_days"] == 7


# ── GET /api/auth/policy endpoint ──────────────────────────────────────


def _policy_endpoint(auth_manager):
    sys.modules.pop("routes.auth_routes", None)
    _real_core_package()
    from routes.auth_routes import setup_auth_routes

    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/policy":
            return route.endpoint
    raise AssertionError("policy route not found")


def test_policy_endpoint_returns_dict(tmp_path):
    mgr = _make_manager(tmp_path)
    endpoint = _policy_endpoint(mgr)
    result = asyncio.run(endpoint())
    assert isinstance(result, dict)
    assert "password_min_length" in result
    assert "reserved_usernames" in result
    assert "signup_enabled" in result
    assert "session_days" in result


def test_policy_endpoint_values_match_manager(tmp_path):
    mgr = _make_manager(tmp_path)
    endpoint = _policy_endpoint(mgr)
    result = asyncio.run(endpoint())
    assert result == mgr.policy()


# ── Password length validation ─────────────────────────────────────────


def _setup_endpoint(auth_manager):
    sys.modules.pop("routes.auth_routes", None)
    _real_core_package()
    from routes.auth_routes import SetupRequest, setup_auth_routes

    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/setup":
            return route.endpoint, SetupRequest
    raise AssertionError("setup route not found")


def _signup_endpoint(auth_manager):
    sys.modules.pop("routes.auth_routes", None)
    _real_core_package()
    from routes.auth_routes import SignupRequest, setup_auth_routes

    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/signup":
            return route.endpoint, SignupRequest
    raise AssertionError("signup route not found")


def _change_password_endpoint(auth_manager):
    sys.modules.pop("routes.auth_routes", None)
    _real_core_package()
    from routes.auth_routes import ChangePasswordRequest, setup_auth_routes

    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/change-password":
            return route.endpoint, ChangePasswordRequest
    raise AssertionError("change-password route not found")


@pytest.mark.parametrize("password", ["", "short"])
def test_setup_rejects_short_password(tmp_path, password):
    mgr = _make_manager(tmp_path)
    endpoint, SetupRequest = _setup_endpoint(mgr)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    body = SetupRequest(username="admin", password=password)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(body=body, request=request))

    assert exc.value.status_code == 400
    assert "8 characters" in exc.value.detail


@pytest.mark.parametrize("password", ["", "short"])
def test_signup_rejects_short_password(tmp_path, password):
    mgr = _make_manager(tmp_path)
    mgr.create_user("admin", "admin-password", is_admin=True)
    mgr.signup_enabled = True
    endpoint, SignupRequest = _signup_endpoint(mgr)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    body = SignupRequest(username="newuser", password=password)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(body=body, request=request))

    assert exc.value.status_code == 400
    assert "8 characters" in exc.value.detail


def test_change_password_rejects_short_password(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.create_user("alice", "old-password", is_admin=False)
    endpoint, ChangePasswordRequest = _change_password_endpoint(mgr)
    request = SimpleNamespace(
        cookies={"odysseus_session": "current-token"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    # Mock get_username_for_token to return alice
    mgr.get_username_for_token = MagicMock(return_value="alice")
    body = ChangePasswordRequest(current_password="old-password", new_password="short")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(body=body, request=request))

    assert exc.value.status_code == 400
    assert "8 characters" in exc.value.detail


def test_admin_self_change_accepts_exactly_empty_password(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.create_user("admin", "old-password", is_admin=True)
    endpoint, ChangePasswordRequest = _change_password_endpoint(mgr)
    request = SimpleNamespace(
        cookies={"odysseus_session": "current-token"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    mgr.get_username_for_token = MagicMock(return_value="admin")

    result = asyncio.run(
        endpoint(
            body=ChangePasswordRequest(
                current_password="old-password",
                new_password="",
            ),
            request=request,
        )
    )

    assert result == {"ok": True}
    assert mgr.verify_password("admin", "") is True
    assert mgr.verify_password("admin", "old-password") is False


@pytest.mark.parametrize("new_password", ["", "short"])
def test_non_admin_self_change_keeps_minimum_password_policy(tmp_path, new_password):
    mgr = _make_manager(tmp_path)
    mgr.create_user("alice", "old-password", is_admin=False)
    endpoint, ChangePasswordRequest = _change_password_endpoint(mgr)
    request = SimpleNamespace(
        cookies={"odysseus_session": "current-token"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    mgr.get_username_for_token = MagicMock(return_value="alice")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            endpoint(
                body=ChangePasswordRequest(
                    current_password="old-password",
                    new_password=new_password,
                ),
                request=request,
            )
        )

    assert exc.value.status_code == 400
    assert "8 characters" in exc.value.detail
    assert mgr.verify_password("alice", "old-password") is True


def test_admin_self_change_rejects_short_nonempty_password(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.create_user("admin", "old-password", is_admin=True)
    endpoint, ChangePasswordRequest = _change_password_endpoint(mgr)
    request = SimpleNamespace(
        cookies={"odysseus_session": "current-token"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    mgr.get_username_for_token = MagicMock(return_value="admin")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            endpoint(
                body=ChangePasswordRequest(
                    current_password="old-password",
                    new_password="short",
                ),
                request=request,
            )
        )

    assert exc.value.status_code == 400
    assert mgr.verify_password("admin", "old-password") is True


def test_admin_create_user_cannot_use_empty_password_even_for_an_admin(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.create_user("admin", "admin-password", is_admin=True)
    sys.modules.pop("routes.auth_routes", None)
    _real_core_package()
    from routes.auth_routes import CreateUserRequest, setup_auth_routes

    router = setup_auth_routes(mgr)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/api/auth/users"
        and "POST" in getattr(route, "methods", set())
    )
    mgr.get_username_for_token = MagicMock(return_value="admin")
    request = SimpleNamespace(cookies={"odysseus_session": "current-token"})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            endpoint(
                body=CreateUserRequest(username="second-admin", password="", is_admin=True),
                request=request,
            )
        )

    assert exc.value.status_code == 400
    assert "8 characters" in exc.value.detail
    assert "second-admin" not in mgr.users


def test_login_form_permits_blank_login_but_requires_creation_passwords():
    source = (Path(__file__).resolve().parent.parent / "static" / "login.html").read_text(
        encoding="utf-8"
    )

    password_markup = source.split('id="password"', 1)[1].split(">", 1)[0]
    assert "required" not in password_markup
    assert "passwordInput.required = creatingAccount" in source
    assert "confirmPasswordInput.required = creatingAccount" in source
    settings_source = (Path(__file__).resolve().parent.parent / "static" / "js" / "settings.js").read_text(
        encoding="utf-8"
    )
    assert "if (!pw) { msg.textContent = 'Enter your password'" not in settings_source


def test_setup_accepts_exactly_min_length_password(tmp_path):
    mgr = _make_manager(tmp_path)
    endpoint, SetupRequest = _setup_endpoint(mgr)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    body = SetupRequest(username="admin", password="12345678")

    result = asyncio.run(endpoint(body=body, request=request))

    assert result == {"ok": True, "message": "Admin account created"}


def test_setup_claims_pre_auth_local_copal_owner(tmp_path):
    mgr = _make_manager(tmp_path)
    endpoint, SetupRequest = _setup_endpoint(mgr)

    class Bridge:
        def __init__(self):
            self.calls = []

        def is_alive(self):
            return True

        async def call(self, operation, args, timeout=20):
            self.calls.append((operation, dict(args), timeout))
            return {"documents": 1}

    bridge = Bridge()
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        app=SimpleNamespace(state=SimpleNamespace(copal_bridge=bridge)),
    )
    body = SetupRequest(username="admin", password="12345678")

    result = asyncio.run(endpoint(body=body, request=request))

    assert result["ok"] is True
    assert bridge.calls == [
        (
            "preflight_rename_owner",
            {"old_owner": "local", "new_owner": "admin"},
            20,
        ),
        (
            "rename_owner",
            {
                "old_owner": "local",
                "new_owner": "admin",
                "manifest": {"documents": 1},
            },
            20,
        ),
    ]


def test_setup_fails_closed_when_copal_bridge_is_explicitly_unavailable(tmp_path):
    mgr = _make_manager(tmp_path)
    endpoint, SetupRequest = _setup_endpoint(mgr)
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        app=SimpleNamespace(state=SimpleNamespace(copal_bridge=None)),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(SetupRequest(username="admin", password="12345678"), request))

    assert exc.value.status_code == 503
    assert mgr.is_configured is False


def test_concurrent_setup_serializes_copal_claim_with_auth_winner(tmp_path):
    mgr = _make_manager(tmp_path)
    endpoint, SetupRequest = _setup_endpoint(mgr)

    class Bridge:
        def __init__(self):
            self.owner = "local"
            self.calls = []
            self.first_claim_started = asyncio.Event()
            self.release_first_claim = asyncio.Event()

        def is_alive(self):
            return True

        async def call(self, operation, args, timeout=20):
            self.calls.append((operation, dict(args), timeout))
            if operation == "preflight_rename_owner":
                return {"source": {"documents": 1}, "target": {"documents": 0}}
            if operation != "rename_owner" or self.owner != args["old_owner"]:
                return {"documents": 0}
            self.owner = args["new_owner"]
            if self.owner == "alice":
                self.first_claim_started.set()
                await self.release_first_claim.wait()
            return {"documents": 1}

    async def run_candidates():
        bridge = Bridge()
        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            app=SimpleNamespace(state=SimpleNamespace(copal_bridge=bridge)),
        )
        first = asyncio.create_task(
            endpoint(SetupRequest(username="alice", password="12345678"), request)
        )
        await bridge.first_claim_started.wait()
        second = asyncio.create_task(
            endpoint(SetupRequest(username="bob", password="12345678"), request)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert [
            call[1]["new_owner"]
            for call in bridge.calls
            if call[0] == "rename_owner"
        ] == ["alice"]
        bridge.release_first_claim.set()
        assert (await first)["ok"] is True
        with pytest.raises(HTTPException) as error:
            await second
        return bridge, error.value

    bridge, error = asyncio.run(run_candidates())

    assert error.status_code == 400
    assert error.detail == "Already configured"
    assert set(mgr.users) == {"alice"}
    assert bridge.owner == "alice"
    assert [call[0] for call in bridge.calls] == [
        "preflight_rename_owner",
        "rename_owner",
    ]
    assert bridge.calls[1][1] == {
        "old_owner": "local",
        "new_owner": "alice",
        "manifest": {"source": {"documents": 1}, "target": {"documents": 0}},
    }


def test_setup_rejects_seven_char_password(tmp_path):
    mgr = _make_manager(tmp_path)
    endpoint, SetupRequest = _setup_endpoint(mgr)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    body = SetupRequest(username="admin", password="1234567")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(body=body, request=request))

    assert exc.value.status_code == 400


# ── Login "remember me" cookie lifetime ────────────────────────────────


class _CapturingResponse:
    """Stand-in for fastapi.Response that records set_cookie kwargs."""

    def __init__(self):
        self.cookie_kwargs = None

    def set_cookie(self, **kwargs):
        self.cookie_kwargs = kwargs


def _login_endpoint(auth_manager):
    sys.modules.pop("routes.auth_routes", None)
    _real_core_package()
    from routes.auth_routes import LoginRequest, setup_auth_routes

    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/login":
            return route.endpoint, LoginRequest
    raise AssertionError("login route not found")


def test_remember_cookie_max_age_matches_token_ttl(tmp_path):
    auth_mod = _auth_module()
    mgr = _make_manager(tmp_path)
    mgr.create_user("alice", "alice-password", is_admin=False)
    endpoint, LoginRequest = _login_endpoint(mgr)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    response = _CapturingResponse()
    body = LoginRequest(username="alice", password="alice-password", remember=True)

    result = asyncio.run(endpoint(body=body, request=request, response=response))

    assert result == {"ok": True, "username": "alice"}
    # The persistent cookie must outlive neither more nor less than the token.
    assert response.cookie_kwargs["max_age"] == auth_mod.TOKEN_TTL
    token, session = next(iter(mgr._sessions.items()))
    assert response.cookie_kwargs["expires"].timestamp() == pytest.approx(session["expiry"])
    lifetime = mgr.session_cookie_lifetime(token)
    assert lifetime["max_age"] == auth_mod.TOKEN_TTL
    assert lifetime["expires"].timestamp() == pytest.approx(session["expiry"])


def test_no_remember_omits_cookie_max_age(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.create_user("bob", "bob-password", is_admin=False)
    endpoint, LoginRequest = _login_endpoint(mgr)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    response = _CapturingResponse()
    body = LoginRequest(username="bob", password="bob-password", remember=False)

    asyncio.run(endpoint(body=body, request=request, response=response))

    # Without "remember", the cookie is a session cookie (no max_age).
    assert "max_age" not in response.cookie_kwargs


def test_secure_cookie_setting_allows_only_direct_loopback_http_login(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path)
    mgr.create_user("loopback", "loopback-password", is_admin=False)
    endpoint, LoginRequest = _login_endpoint(mgr)
    monkeypatch.setenv("SECURE_COOKIES", "true")
    body = LoginRequest(username="loopback", password="loopback-password", remember=False)

    cases = [
        ("http", "127.0.0.1", "127.0.0.1", False),
        ("http", "localhost", "::1", False),
        ("https", "openclank.dev", "127.0.0.1", True),
        ("http", "openclank.dev", "127.0.0.1", True),
        ("http", "127.0.0.1", "203.0.113.7", True),
    ]
    for scheme, hostname, peer, expected_secure in cases:
        request = SimpleNamespace(
            client=SimpleNamespace(host=peer),
            url=SimpleNamespace(scheme=scheme, hostname=hostname),
        )
        response = _CapturingResponse()

        result = asyncio.run(endpoint(body=body, request=request, response=response))

        assert result == {"ok": True, "username": "loopback"}
        assert response.cookie_kwargs["secure"] is expected_secure


def test_accounts_receive_immutable_ids_that_survive_rename(tmp_path):
    mgr = _make_manager(tmp_path)
    assert mgr.create_user("admin", "admin-password", is_admin=True)
    assert mgr.create_user("alice", "alice-password", is_admin=False)

    account_id = mgr.account_id("alice")
    assert account_id and account_id.startswith("account-")
    assert mgr.username_for_account_id(account_id) == "alice"

    assert mgr.rename_user("alice", "allie", "admin")
    assert mgr.account_id("alice") is None
    assert mgr.account_id("allie") == account_id
    assert mgr.username_for_account_id(account_id) == "allie"
    assert next(row for row in mgr.list_users() if row["username"] == "allie")["account_id"] == account_id


def test_legacy_accounts_get_distinct_persisted_immutable_ids(tmp_path):
    auth_mod = _auth_module()
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "users": {
                    "alice": {
                        "password_hash": auth_mod._hash_password("alice-password"),
                        "created": 1,
                        "is_admin": True,
                    },
                    "bob": {
                        "password_hash": auth_mod._hash_password("bob-password"),
                        "created": 2,
                        "is_admin": False,
                        "account_id": "not-an-account-id",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    first = auth_mod.AuthManager(str(path))
    alice_id = first.account_id("alice")
    bob_id = first.account_id("bob")
    assert alice_id and bob_id and alice_id != bob_id

    second = auth_mod.AuthManager(str(path))
    assert second.account_id("alice") == alice_id
    assert second.account_id("bob") == bob_id
