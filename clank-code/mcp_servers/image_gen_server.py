"""Compatibility MCP surface for Open Clank managed image generation.

This process never resolves an endpoint, reads a provider credential, or makes
an upstream HTTP request. The application injects owner/root capability
context; execution itself is delegated to the typed managed-operation facade.
"""

import asyncio
import sys
from pathlib import Path

from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.openclank.mcp_tool_server import ToolServer


server = ToolServer("image_gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description="Generate an image through an Open Clank managed Images route",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description prompt"},
                    "model": {"type": "string", "description": "Stable managed model-route ID (optional)"},
                    "size": {"type": "string", "description": "Image size (default 1024x1024)"},
                    "quality": {"type": "string", "description": "Quality: low, medium, high, auto (default medium)"},
                },
                "required": ["prompt"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "generate_image":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    prompt = str(arguments.get("prompt") or "").strip()
    model_route_id = str(arguments.get("model") or "").strip()
    size = str(arguments.get("size") or "1024x1024").strip()
    quality = str(arguments.get("quality") or "medium").strip()
    owner = str(arguments.get("_open_clank_owner") or "").strip().lower()
    root_operation_id = (
        str(arguments.get("_open_clank_root_operation_id") or "").strip()
        or None
    )
    grant_id = str(arguments.get("_open_clank_grant_id") or "").strip() or None

    if not prompt:
        return [TextContent(type="text", text="Error: Image prompt is required")]
    if not owner:
        return [
            TextContent(
                type="text",
                text="Error: Managed image generation requires an authenticated Open Clank owner.",
            )
        ]

    from src.ai_interaction import do_generate_image
    from src.settings import get_setting, get_user_setting

    if not get_user_setting("image_gen_enabled", owner, True):
        return [
            TextContent(
                type="text",
                text="Error: Image generation is disabled by the administrator.",
            )
        ]

    result = await do_generate_image(
        "\n".join((prompt, model_route_id, size, quality)),
        owner=owner,
        root_operation_id=root_operation_id,
        grant_id=grant_id,
    )
    if result.get("error"):
        return [TextContent(type="text", text=f"Error: {result['error']}")]

    public_base = str(get_setting("app_public_url", "") or "").rstrip("/")
    image_url = f"{public_base}{result['image_url']}"
    text_result = (
        f"Generated image for: {prompt[:100]}\n"
        f"Direct link: {image_url}\n"
        f"model route: {result['image_model']}\n"
        f"size: {result['image_size']}"
    )
    return [TextContent(type="text", text=text_result)]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
