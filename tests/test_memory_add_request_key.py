"""POST /api/memory/add on the guarded path: missing/bad request_key is refused (422) before any provider call."""
import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI


class _Provider:
    def __init__(self, guarded):
        self._guarded_v17, self.calls = guarded, []

    def __getattr__(self, name):  # any provider use is recorded and then fails
        if name.startswith("__"):
            raise AttributeError(name)
        self.calls.append(name)
        raise RuntimeError("provider unavailable in fake")


def _post(provider, body):
    from routes.memory.memory_routes import setup_memory_routes
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(users={"alice": {}}, get_privileges=lambda user: {"can_manage_memory": True})

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = "alice"
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider,
                                           memory_lifecycle=SimpleNamespace(skill_forget=None)))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            return await client.post("/api/memory/add", json=body)
    return asyncio.run(run())


def test_guarded_missing_or_malformed_key_is_422_with_zero_provider_calls():
    for extra in ({}, {"request_key": ""}, {"request_key": " k "}, {"request_key": "k" * 513}, {"request_key": 7}):
        provider = _Provider(True)
        response = _post(provider, {"text": "kumquat", **extra})
        assert response.status_code == 422, (extra, response.status_code, response.text)
        assert "request_key" in str(response.json()), extra
        assert provider.calls == [], (extra, provider.calls)


def test_guarded_valid_key_passes_the_check():
    response = _post(_Provider(True), {"text": "kumquat", "request_key": "k-1"})
    assert "request_key" not in response.text  # proceeds into the guarded flow (the fake then refuses it as unavailable)
    assert response.status_code == 503


def test_legacy_profile_never_asks_for_a_key():
    response = _post(_Provider(False), {"text": "kumquat"})
    assert "request_key" not in response.text and response.status_code != 422
