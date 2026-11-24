import pytest

from src import endpoint_resolver as er


def test_headers_anthropic_uses_x_api_key():
    h = er.build_headers("secret", "https://api.anthropic.com")
    assert h["x-api-key"] == "secret"
    assert h["anthropic-version"] == "2023-06-01"
    assert set(h) == {"x-api-key", "anthropic-version"}


def test_headers_anthropic_without_key_still_sends_version():
    h = er.build_headers(None, "https://api.anthropic.com")
    assert h["anthropic-version"] == "2023-06-01"
    assert "x-api-key" not in h


@pytest.mark.parametrize("base", [
    "https://api.openai.com/v1",
    "https://api.x.ai/v1",
    "https://api.deepseek.com",
    "https://api.groq.com/openai/v1",
    "https://integrate.api.nvidia.com/v1",
    "https://generativelanguage.googleapis.com/v1beta/openai",
])
def test_headers_openai_style_use_bearer(base):
    h = er.build_headers("secret", base)
    assert h["Authorization"] == "Bearer secret"
    assert "HTTP-Referer" not in h
    assert set(h) == {"Authorization"}


def test_headers_openrouter_adds_attribution():
    h = er.build_headers("secret", "https://openrouter.ai/api/v1")
    assert h["Authorization"] == "Bearer secret"
    # OpenRouter ranks/labels apps via these headers.
    assert h["HTTP-Referer"].startswith("https://github.com/")
    assert h["X-OpenRouter-Title"] == "Open Clank"


@pytest.mark.parametrize("key", [None, ""])
def test_headers_omit_authorization_when_no_key(key):
    assert er.build_headers(key, "https://api.openai.com/v1") == {}
