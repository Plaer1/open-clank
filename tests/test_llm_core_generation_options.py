"""Provider-specific generation options for OpenAI-compatible requests."""
import pytest

from src import llm_core


@pytest.mark.parametrize(
    "model",
    ["o1", "o1-mini", "o3", "o3-mini", "o4-mini", "gpt-5", "gpt-5-mini",
     "openrouter/openai/o3-mini", "OpenAI/GPT-5", "kimi-for-coding"],
)
def test_reasoning_models_restrict_temperature(model):
    assert llm_core._restricts_temperature(model) is True


@pytest.mark.parametrize(
    "model",
    ["gpt-4o", "gpt-4.1", "gpt-3.5-turbo", "gpt-4.5-preview",
     "claude-3-5-sonnet", "llama3.1", "", None],
)
def test_normal_models_allow_temperature(model):
    assert llm_core._restricts_temperature(model) is False


def test_local_minimax_mlx_payload_gets_stability_defaults(monkeypatch):
    import src.model_context as model_context

    monkeypatch.setattr(model_context, "is_local_endpoint", lambda _url: True)
    payload = {
        "model": "example-org/MiniMax-M2.7-BF16-mlx-4Bit",
        "temperature": 0.9,
    }

    llm_core._apply_local_generation_stability(
        payload,
        "http://192.168.1.22:8091/v1/chat/completions",
        "example-org/MiniMax-M2.7-BF16-mlx-4Bit",
    )

    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.9
    assert payload["top_k"] == 20
    assert payload["max_tokens"] == 2048
    assert payload["repetition_penalty"] == 1.12


@pytest.mark.parametrize("max_tokens", [37, 0])
def test_chatgpt_subscription_payload_omits_max_output_tokens(max_tokens):
    # ChatGPT Subscription Codex API does not support max_output_tokens —
    # passing it returns HTTP 400 "Unsupported parameter: max_output_tokens".
    # The payload should NOT include max_output_tokens regardless of max_tokens.
    payload = llm_core._build_chatgpt_responses_payload(
        "gpt-5.1-codex",
        [{"role": "user", "content": "Say OK"}],
        temperature=0.2,
        max_tokens=max_tokens,
    )

    assert "max_output_tokens" not in payload


@pytest.mark.parametrize(
    "model",
    [
        "kimi-k2.5",
        "kimi-k2.6",
        "moonshot/kimi-k2.6",
        "kimi-k2.6-preview",
    ],
)
def test_moonshot_k2_5_plus_uses_fixed_temperature(model):
    assert llm_core._moonshot_rejects_custom_temperature("moonshot", model)


@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "kimi-k2.6"),
        ("moonshot", "kimi-k2-0905-preview"),
        ("moonshot", "kimi-k2-thinking"),
        ("moonshot", "kimi-k2.50"),
        ("moonshot", None),
    ],
)
def test_other_models_keep_temperature(provider, model):
    assert not llm_core._moonshot_rejects_custom_temperature(provider, model)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.moonshot.ai/v1/chat/completions",
        "https://api.moonshot.cn/v1/chat/completions",
    ],
)
def test_moonshot_provider_detection(url):
    assert llm_core._detect_provider(url) == "moonshot"


def _tool():
    return {
        "type": "function",
        "function": {
            "name": "search",
            "description": "search",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_openai_chat_tools_force_gpt5_reasoning_effort_none():
    payload = {"tools": [_tool()], "reasoning_effort": "high"}

    llm_core._scrub_openai_chat_tool_reasoning(
        payload,
        "https://api.openai.com/v1/chat/completions",
        "gpt-5.6-luna",
    )

    assert payload["reasoning_effort"] == "none"


def test_openai_chat_tools_match_gpt5_variants():
    for model in ["gpt-5", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "openai/gpt-5.6-luna"]:
        payload = {"tools": [_tool()], "reasoning_effort": "medium"}

        llm_core._scrub_openai_chat_tool_reasoning(
            payload,
            "https://api.openai.com/v1/chat/completions",
            model,
        )

        assert payload["reasoning_effort"] == "none"


def test_openai_chat_no_tools_leaves_reasoning_effort_unchanged():
    payload = {"reasoning_effort": "high"}

    llm_core._scrub_openai_chat_tool_reasoning(
        payload,
        "https://api.openai.com/v1/chat/completions",
        "gpt-5.6-luna",
    )

    assert payload["reasoning_effort"] == "high"


def test_non_openai_host_leaves_reasoning_effort_unchanged():
    payload = {"tools": [_tool()], "reasoning_effort": "high"}

    llm_core._scrub_openai_chat_tool_reasoning(
        payload,
        "https://openrouter.ai/api/v1/chat/completions",
        "openai/gpt-5.6-luna",
    )

    assert payload["reasoning_effort"] == "high"


def test_non_gpt5_model_leaves_reasoning_effort_unchanged():
    payload = {"tools": [_tool()], "reasoning_effort": "high"}

    llm_core._scrub_openai_chat_tool_reasoning(
        payload,
        "https://api.openai.com/v1/chat/completions",
        "gpt-4.1",
    )

    assert payload["reasoning_effort"] == "high"
