"""Authenticated connection controls; no Browser process is launched."""

import unittest
from contextlib import asynccontextmanager

from fastapi import FastAPI
import httpx

from routes.browser_control_routes import setup_browser_control_routes


class _Auth:
    users = {"alice": {"id": "account-a"}}

    def account_id(self, owner):
        return "account-a" if owner == "alice" else None

    def is_account_lifecycle_fenced(self, _owner):
        return False


class _Session:
    owner = "alice"
    workspace_id = "workspace-1"
    archived = False
    incognito = False


class _Sessions:
    def get_session(self, session_id):
        return _Session() if session_id == "chat-1" else None


class _Barrier:
    @asynccontextmanager
    async def admitted(self, _owner):
        yield


class _Service:
    def __init__(self):
        self.latest = {}
        self.opened = []
        self.closed = []

    def admit_ui_generation(self, *, owner, client_id, generation):
        key = (owner, client_id)
        if generation <= self.latest.get(key, 0):
            return False
        self.latest[key] = generation
        return True

    def ui_generation_is_current(self, *, owner, client_id, generation):
        return self.latest.get((owner, client_id)) == generation

    async def open_isolated(self, **kwargs):
        self.opened.append(kwargs)
        return {"connectionGeneration": "g-1", "tabs": [{"index": 0, "current": True, "title": "Blank", "url": "about:blank"}]}

    async def select_tab(self, **kwargs):
        return {"targetId": "opaque", "title": "Chosen"}

    async def list_connection_tabs(self, **_kwargs):
        return {"connected": True, "connectionGeneration": "g-1", "tabs": [{"index": 0, "current": True, "title": "Blank", "url": "about:blank"}]}

    async def close_scope(self, **kwargs):
        self.closed.append(kwargs)
        return True


class BrowserControlPublicRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.binding = {"owner": "alice", "stableChatID": "chat-1", "authorityWorkspaceID": "workspace-1", "workspaceRevision": 7, "transition": None}
        self.service = _Service()
        self.app = FastAPI()
        async def identity(request, call_next):
            request.state.current_user = "alice"
            request.state.authenticated = True
            request.state.api_token = False
            return await call_next(request)
        self.app.middleware("http")(identity)
        self.app.state.browser_session_owner_verifier = lambda *_args: None
        self.app.include_router(setup_browser_control_routes(
            dispatch=lambda **_kwargs: {"ok": True}, auth_manager=_Auth(), session_manager=_Sessions(),
            account_request_barrier=_Barrier(), internal_secret="unused",
            trusted_loopback=lambda _request: True,
            binding_resolver=lambda _sid, owner=None: dict(self.binding) if owner == "alice" else {},
            service=self.service,
        ))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")

    async def asyncTearDown(self):
        await self.client.aclose()

    def _payload(self, **changes):
        return {"session_id": "chat-1", "client_id": "a" * 32, "client_generation": 1, **changes}

    async def test_open_requires_same_origin_and_uses_server_binding(self):
        no_origin = await self.client.post("/api/browser-control/connection/open", json=self._payload())
        self.assertEqual(no_origin.status_code, 403)
        response = await self.client.post("/api/browser-control/connection/open", json=self._payload(), headers={"Origin": "http://127.0.0.1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.opened[0]["workspace_id"], "workspace-1")
        self.assertEqual(self.service.opened[0]["binding_revision"], 7)
        self.assertEqual(self.service.opened[0]["mode"], "isolated")

    async def test_extension_requires_explicit_valid_profile_and_close_retires_generation(self):
        invalid = await self.client.post("/api/browser-control/connection/open", json=self._payload(mode="extension", profile_dir_name="../Default"), headers={"Origin": "http://127.0.0.1"})
        self.assertEqual(invalid.status_code, 422)
        valid = await self.client.post("/api/browser-control/connection/open", json=self._payload(mode="extension", profile_dir_name="Profile 1"), headers={"Origin": "http://127.0.0.1"})
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(self.service.opened[-1]["profile_dir_name"], "Profile 1")
        closed = await self.client.post("/api/browser-control/connection/close", json=self._payload(client_generation=2), headers={"Origin": "http://127.0.0.1"})
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(self.service.closed[-1]["workspace_id"], "workspace-1")


if __name__ == "__main__":
    unittest.main()
