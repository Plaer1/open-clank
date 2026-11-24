"""Anthropic Messages request formatting and response parsing."""
import pytest

from src.llm_core import _anthropic_rejects_temperature, _build_anthropic_payload, _parse_anthropic_response


def _payload(model="claude-x", temperature=0.0, *, system=None, tools=None):
    messages = [*(system or []), {"role": "user", "content": "hi"}]
    return _build_anthropic_payload(model, messages, temperature, 100, tools=tools, stream=True)


@pytest.mark.parametrize("temperature,expected", [
    (-0.5, 0.0), (0.0, 0.0), (0.7, 0.7), (1.0, 1.0), (1.2, 1.0), (2.0, 1.0), (None, None),
])
def test_temperature_clamping(temperature, expected):
    assert _payload(temperature=temperature)["temperature"] == expected


@pytest.mark.parametrize("system_message", [{"role": "system"}, {"role": "system", "content": None}])
def test_contentless_system_messages(system_message):
    payload = _payload(system=[system_message])
    assert payload["messages"]
    assert payload["system"][0]["text"] == ""


def test_mixed_system_messages():
    payload = _payload(system=[
        {"role": "system", "content": "You are helpful."},
        {"role": "system"},
        {"role": "system", "content": "Be concise."},
    ])
    text = payload["system"][0]["text"]
    assert "You are helpful." in text
    assert "Be concise." in text


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-4-8-20260101",  # tolerate a dated snapshot suffix
        "claude-opus-4-7-20260201",  # dated 4.7 snapshot — explicit minor, still >= 4.7
        "anthropic/claude-opus-4-7",  # tolerate a provider-prefixed id
        "claude-opus-4-10",  # future minor still >= 4.7
        "claude-opus-5-0",  # future major
    ],
)
def test_opus_47_plus_rejects_temperature(model):
    assert _anthropic_rejects_temperature(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-opus-4-1",
        "claude-opus-4-0",
        "claude-opus-4",  # bare major (no minor) — kept
        "claude-opus-4-20250514",  # Opus 4.0 dated id — the date must NOT read as a 4.7+ minor
        "claude-opus-4-1-20250805",  # Opus 4.1 dated id — explicit minor before the date
        "claude-opus-4-6-20251201",  # dated 4.6 snapshot — older, still keeps temperature
        "claude-sonnet-4-6",
        "claude-3-5-sonnet",
        "claude-3-opus-20240229",  # legacy Claude 3 Opus — no opus-N-M pattern, kept
        "claude-haiku-4-5",
        "claude-x",
        "octopus-4-8",  # "opus" only as a substring of another word — must not match
        "myproxy/octopus-4-8",  # same, behind a provider prefix
        "",
        None,
    ],
)
def test_older_claude_models_keep_temperature(model):
    assert _anthropic_rejects_temperature(model) is False


@pytest.mark.parametrize("model", [123, 1.5, ["claude-opus-4-8"], {"a": 1}, object()])
def test_non_string_model_is_handled_without_crashing(model):
    # Defensive: the gate must not raise on a non-string model (the old builder
    # never called .lower() on it). Truthy non-strings should classify as False.
    assert _anthropic_rejects_temperature(model) is False


def test_payload_omits_temperature_for_opus_47_plus():
    # The endpoint probe sends temperature=0; on Opus 4.7+ that field must be gone.
    payload = _payload("claude-opus-4-8", 0.0)
    assert "temperature" not in payload


def test_payload_keeps_temperature_for_older_models():
    payload = _payload("claude-opus-4-6", 0.3)
    assert payload["temperature"] == 0.3
    # Older models retain the [0,1] clamp (Nietzsche preset at 1.2 -> 1.0).
    assert _payload("claude-3-5-sonnet", 1.2)["temperature"] == 1.0


def test_payload_keeps_temperature_for_dated_opus_4_0():
    # Anthropic's dated id for Opus 4.0 (claude-opus-4-20250514) is in this repo's
    # ANTHROPIC_MODELS list. The date must not be misread as a >= 4.7 minor, or the
    # user's temperature would be silently dropped on a model that accepts it.
    assert _payload("claude-opus-4-20250514", 0.5)["temperature"] == 0.5


def test_agentic_caches_system_and_last_tool():
    tools = [
        {"type": "function", "function": {"name": "a", "description": "x", "parameters": {}}},
        {"type": "function", "function": {"name": "b", "description": "y", "parameters": {}}},
    ]
    p = _payload(system=[{"role": "system", "content": "SYS PROMPT " * 50}], tools=tools)
    assert isinstance(p["system"], list)
    assert p["system"][0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in p["tools"][0], "only the LAST tool is a breakpoint"
    assert p["tools"][-1].get("cache_control") == {"type": "ephemeral"}
    breakpoints = sum("cache_control" in b for b in p["system"]) + sum("cache_control" in t for t in p["tools"])
    assert breakpoints == 2


def test_tiny_tool_less_prompt_not_cached():
    p = _payload(system=[{"role": "system", "content": "hi"}], tools=None)
    assert isinstance(p["system"], list)
    assert "cache_control" not in p["system"][0]


def test_large_system_only_is_cached():
    p = _payload(system=[{"role": "system", "content": "z" * 5000}], tools=None)
    assert p["system"][0].get("cache_control") == {"type": "ephemeral"}


def test_concatenates_multiple_text_blocks():
    # Regression: only the first text block was returned, dropping the rest.
    data = {"content": [
        {"type": "text", "text": "Part A "},
        {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        {"type": "text", "text": "Part B"},
    ]}
    assert _parse_anthropic_response(data) == "Part A Part B"


def test_skips_non_text_blocks():
    data = {"content": [
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "answer"},
    ]}
    assert _parse_anthropic_response(data) == "answer"


def test_single_block_and_empty():
    assert _parse_anthropic_response({"content": [{"type": "text", "text": "hi"}]}) == "hi"
    assert _parse_anthropic_response({"content": []}) == ""
    assert _parse_anthropic_response({}) == ""
