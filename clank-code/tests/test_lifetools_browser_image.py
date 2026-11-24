"""Exercises Browser screenshot conversion at the actual lifetools call path."""

import importlib
import sys
import types
import unittest
from unittest.mock import patch


def _module(name, **attrs):
    value = types.ModuleType(name)
    for key, item in attrs.items():
        setattr(value, key, item)
    return value


class BrowserLifetoolsImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot_is_an_mcp_image_not_prompt_base64(self):
        class Item:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class Server:
            def __init__(self, *_args, **_kwargs):
                pass

            def list_tools(self):
                return lambda fn: fn

            def call_tool(self):
                return lambda fn: fn

        stdio = _module("mcp.server.stdio", stdio_server=lambda: None)
        types_module = _module("mcp.types", CallToolResult=Item, Tool=Item, TextContent=Item, ImageContent=Item)
        mcp = _module("mcp")
        mcp_server = _module("mcp.server")
        tool_server = _module("src.openclank.mcp_tool_server", ToolServer=Server)
        schemas = _module("src.tool_schemas", FUNCTION_TOOL_SCHEMAS=[], OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS=set(), function_call_to_tool_block=lambda *_a, **_k: None)
        memory = _module("src.memery_provider", BROKER_MEMORY_TOOLS=[])
        injected = {
            "mcp": mcp, "mcp.server": mcp_server, "mcp.server.stdio": stdio,
            "mcp.types": types_module, "src.openclank.mcp_tool_server": tool_server,
            "src.tool_schemas": schemas, "src.memery_provider": memory,
        }
        with patch.dict(sys.modules, injected):
            sys.modules.pop("src.openclank.lifetools_server", None)
            module = importlib.import_module("src.openclank.lifetools_server")
            module._BROWSER_CONTROL_ENABLED = True
            module._BROWSER_MUTATIONS_ENABLED = False
            image_data = "iVBORw0KGgo="

            async def fake_call(self, *_args, **_kwargs):
                return {"targetId": "target-synthetic", "observationGeneration": 3,
                        "result": {"stdout": "screenshot attached", "images": [
                            {"data": image_data, "mimeType": "image/png"}
                        ]}}

            with patch("src.openclank.browser_control_broker.BrowserControlBrokerClient.call", fake_call):
                content = await module.call_tool("browser_observe", {"action": "screenshot"})
            self.assertEqual(len(content), 2)
            self.assertEqual(content[0].type, "text")
            self.assertNotIn(image_data, content[0].text)
            self.assertIn("target-synthetic", content[0].text)
            self.assertEqual(content[1].type, "image")
            self.assertEqual(content[1].data, image_data)
            self.assertEqual(content[1].mimeType, "image/png")
            sys.modules.pop("src.openclank.lifetools_server", None)


if __name__ == "__main__":
    unittest.main()
