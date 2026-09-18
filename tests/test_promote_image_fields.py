"""The native managed image tool returns structured image fields directly."""

import pytest

from src import ai_interaction
from src.agent_tools import ToolBlock
from src.tool_execution import execute_tool_block


@pytest.mark.asyncio
async def test_native_generate_image_preserves_structured_result_and_affinity(monkeypatch):
    seen = {}
    expected = {
        "results": "Generated image",
        "image_url": "/api/generated-image/managed.png",
        "image_id": "gallery-1",
        "image_prompt": "a red fox in snow",
        "image_model": "pmr_image",
        "image_size": "1024x1024",
        "image_quality": "high",
    }

    async def generate(content, **kwargs):
        seen["content"] = content
        seen.update(kwargs)
        return dict(expected)

    monkeypatch.setattr(ai_interaction, "do_generate_image", generate)
    description, result = await execute_tool_block(
        ToolBlock("generate_image", "a red fox in snow\npmr_image\n1024x1024\nhigh"),
        session_id="session-1",
        owner="alice",
        root_operation_id="root-1",
        provider_grant_id="grant-1",
    )

    assert description == "generate_image"
    assert result == expected
    assert seen["owner"] == "alice"
    assert seen["session_id"] == "session-1"
    assert seen["root_operation_id"] == "root-1"
    assert seen["grant_id"] == "grant-1"
    assert seen["idempotency_key"].startswith("tool_image_")


def test_generate_image_has_no_legacy_mcp_mapping():
    from src import tool_execution

    assert "generate_image" not in tool_execution._MCP_TOOL_MAP
    assert "generate_image" not in tool_execution._MCP_ARG_PARSERS
    assert "generate_image" not in tool_execution._MCP_JSON_PRIMARY_KEYS
