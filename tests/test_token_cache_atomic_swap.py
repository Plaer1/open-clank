"""Token-cache refresh must atomically swap, never expose an empty map.

`_refresh_token_cache` previously did ``clear()`` then ``update()``, which left
a window where concurrent auth checks saw zero candidates and returned 401.
It now rebinds the ``_token_cache`` reference so readers keep the old map until
the new one is complete.
"""

import threading

import pytest


@pytest.fixture(scope="module")
def app_module():
    import os

    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ["AUTH_ENABLED"] = "true"
    import app as app_module

    assert hasattr(app_module, "_refresh_token_cache")
    return app_module


def test_refresh_swaps_reference_without_empty_window(app_module, monkeypatch):
    """Concurrent readers must never observe an empty cache during refresh."""
    from core.database import ApiToken, SessionLocal

    calls = {"n": 0}

    class _Row:
        def __init__(self, prefix):
            self.id = f"id-{prefix}"
            self.token_hash = f"hash-{prefix}"
            self.owner = "alice"
            self.token_prefix = prefix
            self.scopes = "chat"
            self.client_kind = "api"
            self.expires_at = None
            self.revoked_at = None
            self.is_active = True

    class _Query:
        def filter(self, *a, **k):
            return self

        def all(self):
            calls["n"] += 1
            if calls["n"] == 1:
                return [_Row("prefix-a")]
            return [_Row("prefix-a"), [_Row("prefix-b")][0]]

    class _DB:
        def query(self, *a, **k):
            return _Query()

        def close(self):
            return None

    monkeypatch.setattr(app_module, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(app_module, "auth_manager", type("AM", (), {
        "users": {"alice": {"account_id": "acct-alice"}},
    })())

    # Establish a known non-empty starting map.
    app_module._token_cache = {"seed": [(1, "h", "alice", ["chat"], "api", None)]}
    app_module.app.state._token_cache = app_module._token_cache
    app_module.app.state._token_cache_dirty = True

    stop = threading.Event()
    empty_reads = []
    broken = []

    def _reader():
        while not stop.is_set():
            snapshot = app_module._token_cache
            if not snapshot:
                empty_reads.append(1)

    threads = [threading.Thread(target=_reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for _ in range(200):
            app_module._refresh_token_cache()
            if not app_module._token_cache:
                broken.append("writer saw empty after swap")
    finally:
        stop.set()
        for t in threads:
            t.join()

    assert empty_reads == [], f"readers observed {len(empty_reads)} empty maps"
    assert broken == []
    assert app_module.app.state._token_cache is app_module._token_cache
    assert app_module.app.state._token_cache_dirty is False
    assert set(app_module._token_cache) == {"prefix-a", "prefix-b"}


def test_refresh_replaces_map_contents(app_module, monkeypatch):
    from core.database import ApiToken, SessionLocal  # noqa: F401

    class _Row:
        def __init__(self, prefix, owner="alice"):
            self.id = f"id-{prefix}"
            self.token_hash = f"hash-{prefix}"
            self.owner = owner
            self.token_prefix = prefix
            self.scopes = "chat"
            self.client_kind = "api"
            self.expires_at = None
            self.revoked_at = None
            self.is_active = True

    class _DB:
        def __init__(self, rows):
            self._rows = rows

        def query(self, *a, **k):
            rows = self._rows

            class _Q:
                def filter(self, *a, **k):
                    return self

                def all(self):
                    return rows

            return _Q()

        def close(self):
            return None

    monkeypatch.setattr(
        app_module, "auth_manager",
        type("AM", (), {"users": {"alice": {"account_id": "acct-alice"}}})(),
    )

    monkeypatch.setattr(app_module, "SessionLocal", lambda: _DB([_Row("prefix-a")]))
    app_module._token_cache = {}
    app_module.app.state._token_cache = app_module._token_cache
    app_module._refresh_token_cache()
    assert set(app_module._token_cache) == {"prefix-a"}

    monkeypatch.setattr(app_module, "SessionLocal", lambda: _DB([_Row("prefix-b")]))
    app_module.app.state._token_cache_dirty = True
    app_module._refresh_token_cache()
    # Old map is replaced entirely; no stale prefix survives the swap.
    assert set(app_module._token_cache) == {"prefix-b"}
    assert app_module.app.state._token_cache is app_module._token_cache


def test_refresh_marks_dirty_flag_clean(app_module, monkeypatch):
    class _DB:
        def query(self, *a, **k):
            class _Q:
                def filter(self, *a, **k):
                    return self

                def all(self):
                    return []

            return _Q()

        def close(self):
            return None

    monkeypatch.setattr(app_module, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(
        app_module, "auth_manager",
        type("AM", (), {"users": {}})(),
    )
    app_module.app.state._token_cache_dirty = True
    app_module._refresh_token_cache()
    assert app_module.app.state._token_cache_dirty is False
    assert app_module._token_cache == {}
