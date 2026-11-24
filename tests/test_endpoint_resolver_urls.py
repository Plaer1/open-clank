import pytest

from src import endpoint_resolver as er


# ── normalize_base: strip whatever path the user pasted ──

@pytest.mark.parametrize("raw,expected", [
    ("https://api.openai.com/v1/chat/completions", "https://api.openai.com/v1"),
    ("https://api.openai.com/v1/completions", "https://api.openai.com/v1"),
    ("https://api.openai.com/v1/models", "https://api.openai.com/v1"),
    ("https://api.openai.com/v1", "https://api.openai.com/v1"),
    ("https://ollama.com/api/chat", "https://ollama.com/api"),
    ("https://api.openai.com/v1/models/", "https://api.openai.com/v1"),
    ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com"),
    ("http://localhost:11434/api/chat", "http://localhost:11434/api"),
    ("http://localhost:11434/api/tags", "http://localhost:11434/api"),
    ("http://localhost:11434/api/generate", "http://localhost:11434/api"),
    ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
    ("  https://api.openai.com/v1  ", "https://api.openai.com/v1"),
    ("", ""),
    (None, ""),
])
def test_normalize_base(raw, expected):
    assert er.normalize_base(raw) == expected


# ── provider-root helpers ──

@pytest.mark.parametrize("base,expected", [
    ("https://api.anthropic.com/v1", "https://api.anthropic.com"),
    ("https://api.anthropic.com", "https://api.anthropic.com"),
    # /v1 on a non-Anthropic host (OpenAI-compatible) must be preserved.
    ("https://api.openai.com/v1", "https://api.openai.com/v1"),
])
def test_anthropic_api_root(base, expected):
    assert er._anthropic_api_root(base) == expected


@pytest.mark.parametrize("base,expected", [
    ("https://ollama.com", "https://ollama.com/api"),
    ("http://localhost:11434/api", "http://localhost:11434/api"),
    # A non-Ollama host is returned untouched.
    ("https://api.openai.com/v1", "https://api.openai.com/v1"),
])
def test_ollama_api_root(base, expected):
    assert er._ollama_api_root(base) == expected


# (id, base_url, expected_chat_url, expected_models_url)
PROVIDER_CASES = [
    ("openai", "https://api.openai.com/v1",
     "https://api.openai.com/v1/chat/completions",
     "https://api.openai.com/v1/models"),
    ("openai_pathless", "https://api.openai.com",
     "https://api.openai.com/v1/chat/completions",
     "https://api.openai.com/v1/models"),
    ("anthropic", "https://api.anthropic.com",
     "https://api.anthropic.com/v1/messages",
     "https://api.anthropic.com/v1/models"),
    # Anthropic base that already carries /v1 must not become /v1/v1/messages.
    ("anthropic_v1", "https://api.anthropic.com/v1",
     "https://api.anthropic.com/v1/messages",
     "https://api.anthropic.com/v1/models"),
    ("openrouter", "https://openrouter.ai/api/v1",
     "https://openrouter.ai/api/v1/chat/completions",
     "https://openrouter.ai/api/v1/models"),
    ("groq", "https://api.groq.com/openai/v1",
     "https://api.groq.com/openai/v1/chat/completions",
     "https://api.groq.com/openai/v1/models"),
    ("nvidia", "https://integrate.api.nvidia.com/v1",
     "https://integrate.api.nvidia.com/v1/chat/completions",
     "https://integrate.api.nvidia.com/v1/models"),
    ("xai", "https://api.x.ai/v1",
     "https://api.x.ai/v1/chat/completions",
     "https://api.x.ai/v1/models"),
    ("deepseek", "https://api.deepseek.com",
     "https://api.deepseek.com/chat/completions",
     "https://api.deepseek.com/v1/models"),
    # Gemini's OpenAI-compatible surface — treated as a generic OpenAI endpoint.
    ("gemini_openai", "https://generativelanguage.googleapis.com/v1beta/openai",
     "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "https://generativelanguage.googleapis.com/v1beta/openai/models"),
    ("ollama_local", "http://localhost:11434/api",
     "http://localhost:11434/api/chat",
     "http://localhost:11434/api/tags"),
    ("ollama_cloud", "https://ollama.com",
     "https://ollama.com/api/chat",
     "https://ollama.com/api/tags"),
    ("ollama_nas", "http://nas:11434", "http://nas:11434/api/chat", "http://nas:11434/api/tags"),
    ("ollama_compat", "http://nas:11434/v1", "http://nas:11434/v1/chat/completions", "http://nas:11434/v1/models"),
    ("ollama_cloud_api", "https://ollama.com/api", "https://ollama.com/api/chat", "https://ollama.com/api/tags"),
]


@pytest.mark.parametrize("base,chat,models", [row[1:] for row in PROVIDER_CASES],
                         ids=[row[0] for row in PROVIDER_CASES])
def test_provider_urls(monkeypatch, base, chat, models):
    monkeypatch.setattr(er, "resolve_url", lambda url: url)
    assert er.build_chat_url(base) == chat
    assert er.build_models_url(base) == models


@pytest.mark.parametrize("build_url", [er.build_chat_url, er.build_models_url])
@pytest.mark.parametrize("base", [
    "https://api.example.com/v1?token=abc",
    "https://api.example.com/v1#fragment",
    "http://localhost:1234?",
])
def test_rejects_query_or_fragment_base(build_url, base):
    with pytest.raises(ValueError, match="query or fragment"):
        build_url(base)


def test_canonical_identity_collapses_host_port_and_suffix_variants():
    assert er.canonical_endpoint_base("HTTPS://API.Example.test:443/v1/models?x=1#frag") == "https://api.example.test/v1"
    assert er.canonical_endpoint_base("http://LOCALHOST:80/v1/") == "http://localhost/v1"
