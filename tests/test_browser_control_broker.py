"""Bounded broker authorization checks; no browser or live profile is started."""

import unittest
from contextlib import asynccontextmanager

from fastapi import FastAPI
import httpx

from routes.browser_control_routes import setup_browser_control_routes
from src.openclank.browser_control_broker import (
    BROKER_BINDING_ENV,
    BROKER_ENABLED_ENV,
    BROKER_MUTATIONS_ENV,
    BROKER_OWNER_ENV,
    BROKER_SESSION_ENV,
    BROKER_TOKEN_ENV,
    BROKER_URL_ENV,
    BROKER_WORKSPACE_ENV,
    BrowserControlBrokerClient,
    browser_control_descriptor_env,
)


class _Auth:
    users = {"alice": {"id": "account-alice"}}

    def account_id(self, username):
        return "account-alice" if username == "alice" else None

    def is_account_lifecycle_fenced(self, username):
        return False


class _Session:
    owner = "alice"
    workspace_id = "workspace-1"
    archived = False


class _Sessions:
    def __init__(self):
        self.session = _Session()

    def get_session(self, session_id):
        return self.session if session_id == "chat-1" else None


class _Barrier:
    @asynccontextmanager
    async def admitted(self, owner):
        if owner != "alice":
            raise RuntimeError("not admitted")
        yield


class BrowserControlBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.secret = "synthetic-host-secret"
        self.binding = {
            "owner": "alice", "stableChatID": "chat-1",
            "authorityWorkspaceID": "workspace-1", "workspaceRevision": 7,
            "transition": None,
        }
        self.calls = []
        self.revoke_after_dispatch = False
        self.dispatch_error = None
        self.response_payload = None
        from src.openclank.browser_control_service import BrowserControlService
        self.policy_service = BrowserControlService(object())
        self.policy_service.publish_turn_policy(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1",
            binding_revision=7, turn_id="turn-1", envelope={"mode": "agent", "disabled_tools": []},
        )

        async def dispatch(**kwargs):
            self.calls.append(kwargs)
            if self.dispatch_error:
                raise self.dispatch_error
            if self.revoke_after_dispatch:
                self.binding["workspaceRevision"] += 1
            if self.response_payload is not None:
                return self.response_payload
            return {"ok": True, "tool": kwargs["tool_name"]}

        self.app = FastAPI()
        self.app.include_router(setup_browser_control_routes(
            dispatch=dispatch, auth_manager=_Auth(), session_manager=_Sessions(),
            account_request_barrier=_Barrier(), internal_secret=self.secret,
            trusted_loopback=lambda request: request.client.host in {"127.0.0.1", "::1"},
            binding_resolver=lambda sid, owner=None: dict(self.binding) if sid == "chat-1" and owner == "alice" else {},
            policy_checker=self.policy_service.turn_policy_allows,
            policy_revision=self.policy_service.current_turn_policy_revision,
        ))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")
        self.env = browser_control_descriptor_env(
            broker_url="http://127.0.0.1/api/internal/browser-control/tool", secret=self.secret,
            owner="alice", host_session_id="chat-1", authority_workspace_id="workspace-1",
            binding_revision=7, enabled=True, mutations_enabled=True,
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    def _headers(self, *, token=None, owner=None, revision=None):
        return {
            "X-Odysseus-Internal-Token": token if token is not None else self.env[BROKER_TOKEN_ENV],
            "X-Open-Clank-Owner": owner if owner is not None else self.env[BROKER_OWNER_ENV],
            "X-Open-Clank-Browser-Session": self.env[BROKER_SESSION_ENV],
            "X-Open-Clank-Browser-Workspace": self.env[BROKER_WORKSPACE_ENV],
            "X-Open-Clank-Browser-Binding": str(revision if revision is not None else self.env[BROKER_BINDING_ENV]),
            "X-Open-Clank-Browser-Capabilities": "observe,control",
        }

    async def test_descriptor_is_scoped_and_mutation_gate_is_explicit(self):
        self.assertNotIn("synthetic-host-secret", repr(self.env))
        self.assertEqual(self.env[BROKER_ENABLED_ENV], "1")
        self.assertEqual(self.env[BROKER_MUTATIONS_ENV], "1")
        observe_only = browser_control_descriptor_env(
            broker_url=self.env[BROKER_URL_ENV], secret=self.secret, owner="alice",
            host_session_id="chat-1", authority_workspace_id="workspace-1",
            binding_revision=7, enabled=True, mutations_enabled=False,
        )
        self.assertEqual(observe_only[BROKER_MUTATIONS_ENV], "0")
        client = BrowserControlBrokerClient(observe_only)
        self.assertTrue((await client.call("browser_control", {}))["blocked"])

    async def test_missing_wrong_and_revoked_binding_are_denied(self):
        payload = {"tool": "browser_observe", "arguments": {"action": "snapshot"}}
        missing = await self.client.post(self.env[BROKER_URL_ENV], json=payload)
        self.assertEqual(missing.status_code, 403)
        wrong = await self.client.post(self.env[BROKER_URL_ENV], json=payload, headers=self._headers(token="wrong"))
        self.assertEqual(wrong.status_code, 403)
        self.binding["workspaceRevision"] = 8
        stale = await self.client.post(self.env[BROKER_URL_ENV], json=payload, headers=self._headers())
        self.assertEqual(stale.status_code, 403)
        self.assertEqual(self.calls, [])

    async def test_observe_only_capability_cannot_call_control_directly(self):
        observe_only = browser_control_descriptor_env(
            broker_url=self.env[BROKER_URL_ENV], secret=self.secret, owner="alice",
            host_session_id="chat-1", authority_workspace_id="workspace-1",
            binding_revision=7, enabled=True, mutations_enabled=False,
        )
        headers = self._headers()
        headers["X-Odysseus-Internal-Token"] = observe_only[BROKER_TOKEN_ENV]
        headers["X-Open-Clank-Browser-Capabilities"] = "observe"
        response = await self.client.post(
            self.env[BROKER_URL_ENV], json={"tool": "browser_control", "arguments": {"action": "click"}}, headers=headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "browser_capability_not_admitted")
        self.assertEqual(self.calls, [])

    async def test_valid_call_rechecks_binding_after_dispatch(self):
        payload = {"tool": "browser_control", "arguments": {"action": "click"}}
        response = await self.client.post(self.env[BROKER_URL_ENV], json=payload, headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "tool": "browser_control"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["expected_policy_revision"], "turn-1")

    async def test_old_control_token_is_denied_after_current_turn_policy_narrows(self):
        self.policy_service.publish_turn_policy(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1",
            binding_revision=7, turn_id="turn-2", envelope={"mode": "plan", "disabled_tools": ["browser_control"]},
        )
        response = await self.client.post(
            self.env[BROKER_URL_ENV],
            json={"tool": "browser_control", "arguments": {"action": "click"}},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "browser_current_turn_policy_denied")
        self.assertEqual(self.calls, [])

    async def test_revocation_during_action_discards_result(self):
        self.revoke_after_dispatch = True
        response = await self.client.post(
            self.env[BROKER_URL_ENV],
            json={"tool": "browser_observe", "arguments": {"action": "snapshot"}},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["code"], "browser_outcome_unknown")
        self.assertTrue(response.json()["detail"]["outcome_unknown"])
        self.assertEqual(len(self.calls), 1)

    async def test_dispatch_rejection_and_unknown_outcome_keep_distinct_status(self):
        from src.openclank.browser_control_service import BrowserActionOutcomeUnknown, BrowserControlRejected
        self.dispatch_error = BrowserControlRejected("stale target before dispatch")
        rejected = await self.client.post(
            self.env[BROKER_URL_ENV], json={"tool": "browser_control", "arguments": {}}, headers=self._headers(),
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertFalse(rejected.json()["detail"]["outcome_unknown"])
        self.dispatch_error = BrowserActionOutcomeUnknown("call sent")
        unknown = await self.client.post(
            self.env[BROKER_URL_ENV], json={"tool": "browser_control", "arguments": {}}, headers=self._headers(),
        )
        self.assertEqual(unknown.status_code, 502)
        self.assertEqual(unknown.json()["detail"]["code"], "browser_outcome_unknown")
        self.assertTrue(unknown.json()["detail"]["outcome_unknown"])

    async def test_lifetools_client_bounds_streamed_response_and_marks_control_unknown(self):
        class ChunkStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"ok":true,"result":"'
                yield b"x" * 300
                yield b'"}'

        async def chunked(_request):
            return httpx.Response(200, stream=ChunkStream())

        client = BrowserControlBrokerClient(
            self.env, response_limit=128, transport=httpx.MockTransport(chunked),
        )
        result = await client.call("browser_control", {"action": "click"})
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(result["code"], "browser_outcome_unknown")

    async def test_lifetools_client_deadline_covers_response_body(self):
        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                import asyncio
                yield b'{"ok":'
                await asyncio.sleep(0.05)
                yield b"true}"

        async def delayed(_request):
            return httpx.Response(200, stream=SlowBody())

        client = BrowserControlBrokerClient(
            self.env, timeout=0.005, transport=httpx.MockTransport(delayed),
        )
        result = await client.call("browser_control", {"action": "click"})
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(result["code"], "browser_outcome_unknown")

if __name__ == "__main__":
    unittest.main()
