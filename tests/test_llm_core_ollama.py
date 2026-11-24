"""Regression tests for native Ollama Cloud provider handling."""
import httpx
import pytest

from src import llm_core


def test_detects_ollama_cloud_native_provider():
    assert llm_core._detect_provider("https://ollama.com/api") == "ollama"
    assert llm_core._detect_provider("https://ollama.com/api/chat") == "ollama"


def test_detects_bare_local_ollama_as_native_provider():
    assert llm_core._detect_provider("http://localhost:11434") == "ollama"
    assert llm_core._detect_provider("http://127.0.0.1:11434/") == "ollama"
    assert llm_core._detect_provider("http://localhost:11434/v1") == "openai"








def test_list_model_ids_from_openai_compatible_v1(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"id": "qwen2.5-coder:7b"}]},
        )

    monkeypatch.setattr(llm_core.httpx, "get", fake_get)

    assert llm_core.list_model_ids("http://localhost:11434/v1") == ["qwen2.5-coder:7b"]
    assert seen["url"] == "http://localhost:11434/v1/models"


# ---------------------------------------------------------------------------
# Tool-call argument serialization for native Ollama
#
# Odysseus carries assistant tool calls in the OpenAI shape, where
# `function.arguments` is a JSON *string*. Native Ollama /api/chat expects a
# JSON *object* and rejects the string form with HTTP 400 ("Value looks like
# object, but can't find closing '}' symbol"), aborting every follow-up
# (tool-result) round. _build_ollama_payload must parse it back to an object.
# ---------------------------------------------------------------------------

def _assistant_tool_call_msgs():
    """A canonical OpenAI-style assistant tool call + tool result, as produced by
    agent_loop._append_tool_results (arguments are a JSON string)."""
    return [
        {"role": "user", "content": "what do you know about me?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "app_api", "arguments": '{"action": "get_memory"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "Memory: user is James."},
    ]


def test_ollama_payload_parses_string_arguments_to_object():
    payload = llm_core._build_ollama_payload(
        "gpt-oss:120b", _assistant_tool_call_msgs(), temperature=0.0, max_tokens=0,
    )
    asst = payload["messages"][1]
    args = asst["tool_calls"][0]["function"]["arguments"]
    # The whole point: arguments must be a dict, not the JSON string.
    assert args == {"action": "get_memory"}
    assert not isinstance(args, str)
    assert asst["tool_calls"][0]["function"]["name"] == "app_api"
    assert asst["tool_calls"][0]["id"] == "call_0"


def test_ollama_payload_drops_gemini_thought_signature():
    """A cross-provider fallback can hand Ollama a tool call that still carries
    Gemini's opaque extra_content; it is meaningless to Ollama and must not leak."""
    msgs = _assistant_tool_call_msgs()
    msgs[1]["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "AAAA"}}
    payload = llm_core._build_ollama_payload(
        "gpt-oss:120b", msgs, temperature=0.0, max_tokens=0,
    )
    tc = payload["messages"][1]["tool_calls"][0]
    assert "extra_content" not in tc
    assert tc["function"]["arguments"] == {"action": "get_memory"}


def test_ollama_payload_leaves_plain_messages_untouched():
    msgs = [{"role": "user", "content": "hello"}]
    payload = llm_core._build_ollama_payload("m", msgs, temperature=0.0, max_tokens=0)
    assert payload["messages"][0] == {"role": "user", "content": "hello"}


def test_ollama_payload_tolerates_malformed_arguments():
    msgs = [{
        "role": "assistant",
        "tool_calls": [{"function": {"name": "x", "arguments": "{not json"}}],
    }]
    payload = llm_core._build_ollama_payload("m", msgs, temperature=0.0, max_tokens=0)
    # Falls back to an empty object rather than raising.
    assert payload["messages"][0]["tool_calls"][0]["function"]["arguments"] == {}


# ---------------------------------------------------------------------------
# num_ctx threading (issue #909)
#
# Ollama defaults num_ctx to 2048 when the option is omitted, so prompts
# going to any Ollama backend are silently truncated there regardless of
# the model's actual capability. The builder must accept a discovered
# context length and emit options.num_ctx — but only when the value is
# trusted and larger than 2048.
# ---------------------------------------------------------------------------


def test_build_ollama_payload_emits_num_ctx_when_known_and_large():
    """num_ctx passes through when the caller supplies a trusted value
    larger than Ollama's 2048 default."""
    payload = llm_core._build_ollama_payload(
        "kimi-k2", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100, num_ctx=131072,
    )
    assert payload["options"]["num_ctx"] == 131072


def test_build_ollama_payload_emits_num_ctx_for_small_known_models():
    """A model with a real context smaller than Ollama's 2048 default
    would OOM if Ollama used its own default. Pass the real value."""
    payload = llm_core._build_ollama_payload(
        "tiny-llm", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100, num_ctx=1024,
    )
    assert payload["options"]["num_ctx"] == 1024


def test_build_ollama_payload_omits_none_and_zero():
    """None means the caller didn't look it up; 0 is nonsensical.
    Both should be dropped, not emitted as a 0-context request."""
    for ctx in (None, 0):
        payload = llm_core._build_ollama_payload(
            "m", [{"role": "user", "content": "x"}],
            temperature=0.5, max_tokens=100, num_ctx=ctx,
        )
        assert "num_ctx" not in payload.get("options", {}), (
            f"num_ctx={ctx} should not be emitted"
        )


def test_build_ollama_payload_omits_default_context_fallback():
    """get_context_length returns DEFAULT_CONTEXT (128000) when it can't
    discover the model's actual window. Emitting that as num_ctx would
    lie to Ollama for unknown models, so the builder filters it out."""
    from src.model_context import DEFAULT_CONTEXT
    payload = llm_core._build_ollama_payload(
        "unknown-llm-9001", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100, num_ctx=DEFAULT_CONTEXT,
    )
    assert "num_ctx" not in payload.get("options", {})


@pytest.mark.parametrize("url,is_compatible", [
    ("http://127.0.0.1:11434/v1", True),
    ("http://127.0.0.1:11434/v1/chat/completions", True),
    ("http://localhost:11434/v1", True),
    ("http://localhost:11434/v1/chat/completions", True),
    ("http://[::1]:11434/v1", True),
    ("http://127.0.0.1:11435/v1", True),
    ("http://localhost:8080/v1/chat/completions", True),
    ("http://0.0.0.0:11434/v1", True),
    ("https://api.openai.com/v1", False),
    ("https://api.openai.com/v1/chat/completions", False),
    ("http://localhost:11434/api", False),
    ("http://localhost:11434/api/chat", False),
    ("https://openrouter.ai/api/v1", False),
    ("", False),
    (None, False),
])
def test_local_openai_compatible_url(url, is_compatible):
    assert llm_core._is_ollama_openai_compat_url(url) is is_compatible
