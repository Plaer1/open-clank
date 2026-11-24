import pytest
from fastapi import HTTPException

from core.platform_compat import _ssh_exec_argv
from routes.hwfit_routes import setup_hwfit_routes


def _endpoint(path: str):
    router = setup_hwfit_routes()
    for route in router.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"{path} route not found")



@pytest.mark.parametrize("query", [
    {"gpu_count": "abc"},
    {"gpu_count": "0"},
    {"manual_mode": "gpu", "manual_gpu_count": "abc"},
])
def test_model_ranking_accepts_count_inputs(query):
    assert isinstance(_endpoint("/api/hwfit/models")(**query), dict)


@pytest.mark.parametrize(
    "path,kwargs",
    [
        ("/api/hwfit/system", {}),
        ("/api/hwfit/models", {"limit": 1}),
        ("/api/hwfit/profiles", {"model": "demo"}),
        ("/api/hwfit/image-models", {}),
    ],
)
@pytest.mark.parametrize("host,port", [
    ("-oProxyCommand=sh", "22"),
    ("alice@gpu-box", "-oProxyCommand=sh"),
])
def test_hwfit_routes_reject_invalid_ssh_connection(path, kwargs, host, port):
    with pytest.raises(HTTPException) as exc:
        _endpoint(path)(host=host, ssh_port=port, **kwargs)
    assert exc.value.status_code == 400


def test_hwfit_routes_reject_port_without_host():
    endpoint = _endpoint("/api/hwfit/system")

    with pytest.raises(HTTPException) as exc:
        endpoint(host="", ssh_port="2222")

    assert exc.value.status_code == 400


def test_ssh_argv_rejects_option_shaped_remote():
    with pytest.raises(ValueError):
        _ssh_exec_argv("-oProxyCommand=sh", "22", remote_cmd="true")
    with pytest.raises(ValueError):
        _ssh_exec_argv("alice@-oProxyCommand=sh", "22", remote_cmd="true")


def test_detect_system_option_host_never_starts_ssh(monkeypatch):
    from core import platform_compat
    from services.hwfit import hardware

    calls = []

    def _record_subprocess_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("ssh subprocess should not start")

    monkeypatch.setattr(platform_compat.subprocess, "run", _record_subprocess_run)
    hardware._cache_by_host.clear()

    try:
        result = hardware.detect_system(
            host="-oProxyCommand=sh",
            ssh_port="22",
            platform="linux",
            fresh=True,
        )
    finally:
        hardware._cache_by_host.clear()
        hardware._remote_host = None
        hardware._remote_port = None
        hardware._remote_platform = None

    assert result == {
        "error": "Cannot connect to -oProxyCommand=sh",
        "host": "-oProxyCommand=sh",
    }
    assert calls == []
