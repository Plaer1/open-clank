"""Kimi endpoints must not impersonate other clients."""

import pytest
from fastapi import HTTPException

from src import llm_core
from src.endpoint_resolver import build_headers


KIMI_CHAT_URL = "https://api.kimi.com/coding/v1/chat/completions"


def test_kimi_headers_do_not_claim_another_client():
    assert build_headers("secret", "https://api.kimi.com/coding/v1") == {
        "Authorization": "Bearer secret",
    }


def test_kimi_access_denial_is_not_retried_under_another_identity(monkeypatch):
    calls = []

    class Response:
        status_code = 403
        text = '{"error":{"type":"access_terminated_error"}}'
        is_success = False

    def post(url, headers=None, **kwargs):
        calls.append(dict(headers or {}))
        return Response()

    monkeypatch.setattr(llm_core.httpx, "post", post)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda key: None)

    with pytest.raises(HTTPException):
        llm_core.llm_call(
            KIMI_CHAT_URL,
            "kimi-for-coding",
            [{"role": "user", "content": "hi"}],
            headers={"Authorization": "Bearer secret"},
        )

    assert calls == [{
        "Content-Type": "application/json",
        "Authorization": "Bearer secret",
    }]
