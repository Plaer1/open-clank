"""S01: %USER% read-time rendering at the provider-serialization seams.

Stored claims keep the canonical %USER% token; human- and model-facing
payloads render the owner-scoped Handler label, and any editable payload
carries the raw companion so editors round-trip the stored form.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import routes.memory_routes as memory_routes


def _record(record_id="m_1", text="%USER% prefers tea.", **overrides):
    base = dict(
        id=record_id,
        text=text,
        timestamp="now",
        category="fact",
        source="user",
        owner="alice",
        session_id=None,
        metadata={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _router(provider, monkeypatch):
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr("src.auth_helpers.require_user", lambda request: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(memory_routes, "resolve_handler_display_label", lambda *a, **k: "Allie")
    return memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=provider,
    )


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={},
    )


def _endpoint(router, path, method):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in route.methods
    )


def test_memory_list_renders_handler_label_with_raw_companion(tmp_path, monkeypatch):
    class Provider:
        provider_id = "stub"
        _fm_db_path = str(tmp_path / "fm.db")

        async def list_memories(self, *, owner=None, limit=1000):
            return [_record(), _record("m_2", "Plain fact without tokens.")]

    router = _router(Provider(), monkeypatch)
    endpoint = _endpoint(router, "/api/memory", "GET")
    payload = asyncio.run(endpoint(request=_request(), limit=100, cursor=None))

    rendered, plain = payload["memory"]
    assert rendered["text"] == "Allie prefers tea."
    assert rendered["raw_text"] == "%USER% prefers tea."
    # Untokenized records are byte-identical and carry no companion.
    assert plain["text"] == "Plain fact without tokens."
    assert "raw_text" not in plain


def test_memory_get_renders_handler_label(tmp_path, monkeypatch):
    class Provider:
        provider_id = "stub"
        _fm_db_path = str(tmp_path / "fm.db")

        async def get(self, memory_id, *, owner=None):
            return _record(memory_id)

    router = _router(Provider(), monkeypatch)
    endpoint = _endpoint(router, "/api/memory/{memory_id}", "GET")
    payload = asyncio.run(endpoint(request=_request(), memory_id="m_1"))
    assert payload["memory"]["text"] == "Allie prefers tea."
    assert payload["memory"]["raw_text"] == "%USER% prefers tea."


def test_memory_export_preserves_the_token(tmp_path, monkeypatch):
    class Provider:
        provider_id = "stub"
        _fm_db_path = str(tmp_path / "fm.db")

        async def export_scope(self, *, owner=None):
            return {"memories": [{"id": "m_1", "text": "%USER% prefers tea."}]}

    router = _router(Provider(), monkeypatch)
    endpoint = _endpoint(router, "/api/memory/export", "GET")
    payload = asyncio.run(endpoint(request=_request(), format=None))
    # Export is the canonical, re-importable form: rename-safe, never rendered.
    assert payload["memories"][0]["text"] == "%USER% prefers tea."


def test_digest_identity_rendering_feeds_injection_and_preview():
    from src.frankenmemory_provider import _render_digest_identity
    from src.memory_digest import render_digest

    digest = {
        "counts": {"by_tier": {"curated": 1, "raw": 0}, "candidates_pending": 0},
        "pinned": [{"id": "m_1", "headline": "%USER%'s checklist", "content": "%USER% prefers tea."}],
        "open_questions": [{"id": "m_2", "content": "%USER%'s name?", "source_type": "human"}],
        "recent": [{"topic": "%USER% and tea"}],
        "clusters": [],
    }
    _render_digest_identity(digest, "Allie")

    assert digest["pinned"][0]["headline"] == "Allie's checklist"
    assert digest["pinned"][0]["raw_headline"] == "%USER%'s checklist"
    assert digest["pinned"][0]["content"] == "Allie prefers tea."
    assert digest["pinned"][0]["raw_content"] == "%USER% prefers tea."
    assert digest["open_questions"][0]["content"] == "Allie's name?"
    assert digest["recent"][0]["topic"] == "Allie and tea"

    card = render_digest(digest)
    assert "Allie" in card
    assert "%USER%" not in card
