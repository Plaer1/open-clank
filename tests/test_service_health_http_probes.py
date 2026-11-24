import types

from src import service_health as sh


def _resp(status_code):
    return types.SimpleNamespace(status_code=status_code)


def _raise(*_a, **_k):
    raise RuntimeError("connection refused")


def _ntfy_intg():
    return [{"preset": "ntfy", "enabled": True, "base_url": "http://ntfy:80"}]


def test_ntfy_disabled_without_integration():
    s = sh.ntfy_health([], {"reminder_channel": "ntfy"})
    assert s["status"] == sh.DISABLED


def test_ntfy_ok():
    s = sh.ntfy_health(_ntfy_intg(), {"reminder_channel": "ntfy"},
                       http_get=lambda url, timeout: _resp(200))
    assert s["status"] == sh.OK
    assert s["meta"]["base"] == "http://ntfy:80"


def test_ntfy_probes_v1_health_not_a_topic():
    seen = {}

    def getter(url, timeout):
        seen["url"] = url
        return _resp(200)

    sh.ntfy_health(_ntfy_intg(), {"reminder_channel": "ntfy"}, http_get=getter)
    # Non-intrusive: hits /v1/health, never publishes to a topic.
    assert seen["url"].endswith("/v1/health")


def test_ntfy_down_on_exception():
    s = sh.ntfy_health(_ntfy_intg(), {"reminder_channel": "ntfy"},
                       http_get=_raise)
    assert s["status"] == sh.DOWN


def test_ntfy_meta_redacts_userinfo_in_base():
    intg = [{"preset": "ntfy", "enabled": True,
             "base_url": "https://user:topsecret@ntfy.example.com"}]
    seen = {}

    def getter(url, timeout):
        seen["url"] = url          # the probe itself may keep credentials
        return _resp(200)

    s = sh.ntfy_health(intg, {"reminder_channel": "ntfy"}, http_get=getter)
    assert s["meta"]["base"] == "https://ntfy.example.com"
    assert "topsecret" not in repr(s)


def test_searxng_disabled_when_other_provider():
    s = sh.searxng_health({"search_provider": "brave"})
    assert s["status"] == sh.DISABLED


def test_searxng_ok_on_healthz():
    s = sh.searxng_health(
        {"search_provider": "searxng", "search_url": "http://sx:8080"},
        http_get=lambda url, timeout: _resp(200),
    )
    assert s["status"] == sh.OK
    assert s["meta"]["probed"] == "/healthz"


def test_searxng_ok_on_root_fallback():
    def getter(url, timeout):
        return _resp(404) if url.endswith("/healthz") else _resp(200)

    s = sh.searxng_health(
        {"search_provider": "searxng", "search_url": "http://sx:8080"},
        http_get=getter,
    )
    assert s["status"] == sh.OK
    assert s["meta"]["probed"] == "/"


def test_searxng_down_on_exception():
    s = sh.searxng_health(
        {"search_provider": "searxng", "search_url": "http://sx:8080"},
        http_get=_raise,
    )
    assert s["status"] == sh.DOWN


def test_searxng_down_on_5xx():
    s = sh.searxng_health(
        {"search_provider": "searxng", "search_url": "http://sx:8080"},
        http_get=lambda url, timeout: _resp(502),
    )
    assert s["status"] == sh.DOWN


def test_searxng_meta_redacts_instance_url():
    s = sh.searxng_health(
        {"search_provider": "searxng",
         "search_url": "http://user:s3cr3t@searx.local:8080/?token=zzz"},
        http_get=lambda url, timeout: _resp(200),
    )
    blob = repr(s)
    assert "s3cr3t" not in blob and "zzz" not in blob and "user:" not in blob
    assert s["meta"]["instance"] == "http://searx.local:8080"


def test_searxng_down_uses_error_category_not_raw_exception():
    def boom(url, timeout):
        raise RuntimeError("failed connecting to http://user:pw@searx.local secret-token")
    s = sh.searxng_health(
        {"search_provider": "searxng", "search_url": "http://searx.local"},
        http_get=boom,
    )
    assert s["status"] == sh.DOWN
    assert s["meta"]["error"] == "error"           # controlled category token
    assert "secret-token" not in repr(s) and "pw@" not in repr(s)
