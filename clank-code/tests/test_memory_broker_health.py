"""Broker connect health: answered from Host provider state (no Native call); client fails closed.

The app.py handler cannot be called without booting the app, so the handler is covered by a source-order check
and the pure provider.broker_health() helper; the real client connect check runs against fakes at the HTTP boundary.
"""
import asyncio
import re
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent


def _provider(**kw):
    from src.memery_provider import MemeryProvider
    return MemeryProvider(command="/nonexistent", workspace_id="w", env=kw.get("env", {}), **{k: v for k, v in kw.items() if k != "env"})


def test_broker_health_reports_only_connected_provider_as_ready():
    async def go():
        p = _provider(broker_url="", broker_token="")
        assert p.broker_health() == {"ready": False, "profile": "legacy"}  # never connected
        p._owner_task = asyncio.ensure_future(asyncio.sleep(30))
        p._ready = asyncio.get_running_loop().create_future()
        assert p.broker_health()["ready"] is False  # connecting
        p._ready.set_result(None)
        assert p.broker_health() == {"ready": True, "profile": "legacy"}
        p._ready = asyncio.get_running_loop().create_future(); p._ready.set_exception(RuntimeError("x")); p._ready.exception()
        assert p.broker_health()["ready"] is False  # failed connect
        p._ready = asyncio.get_running_loop().create_future(); p._ready.set_result(None)
        p._owner_task.cancel()
        await asyncio.gather(p._owner_task, return_exceptions=True)
        assert p.broker_health()["ready"] is False  # owner gone
    asyncio.run(go())


def test_handler_answers_health_without_native_call():
    src = (REPO / "app.py").read_text()
    a = src.index('if name == "memory_quality":\n                # Broker connect health')
    b = src.index("return reply", a)
    assert "invoke" not in src[a:b] and "_call_tool" not in src[a:b]
    assert a < src.index("return await invoke(name, scoped_arguments)", a)
    assert "ready" in src[a:b]


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.content = status, payload, b"{}"
    def json(self):
        if isinstance(self._p, Exception):
            raise self._p
        return self._p
    text = ""
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError("http")


def _connect(status, payload):
    posted = []

    class Client:
        def __init__(self, *a, **k): pass
        async def post(self, url, **kw):
            posted.append(kw["json"]["name"])
            return _Resp(status, payload)
        async def aclose(self): pass
    p = _provider(broker_url="http://127.0.0.1:1/x", broker_token="t")
    import httpx
    with patch.object(httpx, "AsyncClient", Client):
        try:
            asyncio.run(p.initialize())
            return True, p, posted
        except Exception:
            return False, p, posted


def test_client_connect_ready_ok_not_ready_malformed_refused():
    ok, p, posted = _connect(200, {"ready": True, "profile": "legacy"})
    assert ok and p._broker_ready and posted == ["memory_quality"]
    for status, payload in ((200, {"ready": False}), (200, {}), (200, {"ready": "yes"}),
                            (200, {"schema_version": 18}), (200, []), (200, ValueError("bad")), (503, {"detail": "x"})):
        ok, p, _ = _connect(status, payload)
        assert not ok and not p._broker_ready, (status, payload)
