"""Fake-MCP coverage for explicit selection and safe one-shot Browser calls."""

import unittest

from src.openclank.browser_control_service import BrowserControlService, _sanitize_result
from src.openclank.browser_control_service import validate_browser_arguments


class FakeBrowserManager:
    def __init__(self):
        self.calls = []
        self.open_kwargs = []
        self.list_result = {"stdout": "- 2: (current) [Synthetic page](https://example.test/start?auth=secret)\n"}

    async def open_browser_client(self, **kwargs):
        self.open_kwargs.append(kwargs)
        return {"connected": True, "connection_id": "__openclank_browser_private", "connection_generation": "generation-9"}

    async def call_browser_tool_once(self, connection_id, name, arguments, **_kwargs):
        self.calls.append((connection_id, name, dict(arguments)))
        if name == "browser_tabs":
            return self.list_result
        if name == "browser_snapshot":
            return {"stdout": "Link https://example.test/path?token=secret#frag", "exit_code": 0}
        return {"stdout": "ok", "exit_code": 0}

    async def close_browser_client(self, connection_id):
        return connection_id == "__openclank_browser_private"


class BrowserControlServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_about_blank_bootstrap_and_image_payload_survive_sanitizing(self):
        parsed = BrowserControlService._parse_tabs({"stdout": "- 0: (current) [](about:blank)\n"})
        self.assertEqual(parsed[0]["url"], "about:blank")
        image = "a" * 20000
        sanitized = _sanitize_result({
            "text": "https://user:pass@example.test/x?token=y",
            "images": [{"data": image, "mimeType": "image/png"}],
        })
        self.assertIn("[redacted URL]", sanitized["text"])
        self.assertEqual(sanitized["images"][0]["data"], image)

    def test_pinned_playwright_083_argument_shapes(self):
        # Conform to the pinned runtime input schemas inspected in
        # playwright-core 1.64.0-alpha-1790635538000's coreBundle.
        self.assertEqual(validate_browser_arguments("type", {"target": "e3", "text": "hello", "submit": True}), {"target": "e3", "text": "hello", "submit": True})
        self.assertEqual(validate_browser_arguments("fill", {"fields": [{"target": "e4", "name": "Email", "type": "textbox", "value": "a@example.test"}]}), {"fields": [{"target": "e4", "name": "Email", "type": "textbox", "value": "a@example.test"}]})
        self.assertEqual(validate_browser_arguments("screenshot", {"type": "webp", "scale": "css"}), {"type": "webp", "scale": "css"})
        self.assertEqual(validate_browser_arguments("console", {}), {})
        for action, arguments in (("type", {"text": "missing target"}), ("fill", {"fields": [{"name": "Email", "type": "textbox", "value": "x"}]})):
            with self.assertRaises(ValueError):
                validate_browser_arguments(action, arguments)

    async def test_selection_and_observation_are_bound_and_redacted(self):
        manager = FakeBrowserManager()
        service = BrowserControlService(manager)
        opened = await service.open_isolated(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7,
        )
        self.assertEqual(opened["tabs"][0]["index"], 2)
        self.assertNotIn("auth=secret", opened["tabs"][0]["url"])
        target = await service.select_tab(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1",
            binding_revision=7, tab_index=2,
        )
        self.assertNotIn("__openclank_browser_private", repr(target))
        request = {
            "target_id": target["targetId"],
            "connection_generation": target["connectionGeneration"],
            "navigation_generation": target["navigationGeneration"],
            "observation_generation": target["observationGeneration"],
            "action": "snapshot", "arguments": {},
        }
        result = await service.call(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1",
            binding_revision=7, tool_name="browser_observe", arguments=request,
        )
        self.assertNotIn("token=secret", repr(result))
        self.assertNotIn("#frag", repr(result))
        calls = [call for call in manager.calls if call[1] == "browser_snapshot"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(call[0] == "__openclank_browser_private" for call in manager.calls))

    async def test_status_refresh_mints_current_model_target(self):
        manager = FakeBrowserManager()
        service = BrowserControlService(manager)
        await service.open_isolated(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7)
        result = await service.call(owner="alice", host_session_id="chat-1", workspace_id="workspace-1",
                                    binding_revision=7, tool_name="browser_status", arguments={})
        self.assertEqual(result["selectedTarget"]["tabId"], "index:2")
        self.assertNotIn("auth=secret", result["selectedTarget"]["origin"])

    async def test_extension_mode_uses_only_explicit_pinned_profile_name(self):
        manager = FakeBrowserManager()
        service = BrowserControlService(manager)
        await service.open_isolated(owner="alice", host_session_id="chat-1", workspace_id="workspace-1",
                                    binding_revision=7, mode="extension", profile_dir_name="Profile 1")
        startup_args = manager.open_kwargs[0]["args"]
        self.assertIn("--extension", startup_args)
        self.assertIn("--profile-dir-name", startup_args)
        self.assertIn("Profile 1", startup_args)
        self.assertNotIn("--browser", startup_args)

    async def test_superseded_connection_is_closed_before_publication(self):
        import asyncio

        class SlowManager(FakeBrowserManager):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.closed = []

            async def open_browser_client(self, **kwargs):
                self.started.set()
                await self.release.wait()
                return await super().open_browser_client(**kwargs)

            async def close_browser_client(self, connection_id):
                self.closed.append(connection_id)
                return True

        manager = SlowManager()
        service = BrowserControlService(manager)
        client_id = "a" * 32
        self.assertTrue(service.admit_ui_generation(owner="alice", client_id=client_id, generation=1))
        opening = asyncio.create_task(service.open_isolated(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7,
            client_id=client_id, client_generation=1,
        ))
        await manager.started.wait()
        self.assertTrue(service.admit_ui_generation(owner="alice", client_id=client_id, generation=2))
        manager.release.set()
        with self.assertRaises(Exception):
            await opening
        self.assertEqual(manager.closed, ["__openclank_browser_private"])

    async def test_other_binding_cannot_use_selected_target(self):
        manager = FakeBrowserManager()
        service = BrowserControlService(manager)
        await service.open_isolated(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7,
        )
        target = await service.select_tab(
            owner="alice", host_session_id="chat-1", workspace_id="workspace-1",
            binding_revision=7, tab_index=2,
        )
        request = {
            "target_id": target["targetId"], "connection_generation": target["connectionGeneration"],
            "navigation_generation": 0, "observation_generation": 0,
            "action": "click", "arguments": {"element": "button"},
        }
        with self.assertRaises(Exception):
            await service.call(
                owner="alice", host_session_id="chat-2", workspace_id="workspace-1",
                binding_revision=7, tool_name="browser_control", arguments=request,
            )
        self.assertFalse(any(name == "browser_click" for _cid, name, _args in manager.calls))

    async def test_host_file_arguments_are_rejected_before_dispatch(self):
        manager = FakeBrowserManager()
        service = BrowserControlService(manager)
        await service.open_isolated(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7)
        target = await service.select_tab(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7, tab_index=2)
        request = {
            "target_id": target["targetId"], "connection_generation": target["connectionGeneration"],
            "navigation_generation": 0, "observation_generation": 0, "action": "screenshot",
            "arguments": {"filename": "/tmp/owned-by-model.png"},
        }
        with self.assertRaises(ValueError):
            await service.call(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7, tool_name="browser_observe", arguments=request)
        self.assertFalse(any(name == "browser_take_screenshot" for _cid, name, _args in manager.calls))

    async def test_scope_selection_and_action_share_serialization_boundary(self):
        import asyncio

        class GatedManager(FakeBrowserManager):
            def __init__(self):
                super().__init__()
                self.selection_started = asyncio.Event()
                self.release_selection = asyncio.Event()
                self.block_selection = False

            async def call_browser_tool_once(self, connection_id, name, arguments, **kwargs):
                if name == "browser_tabs" and arguments.get("action") == "select" and self.block_selection:
                    self.selection_started.set()
                    await self.release_selection.wait()
                return await super().call_browser_tool_once(connection_id, name, arguments, **kwargs)

        manager = GatedManager()
        service = BrowserControlService(manager)
        await service.open_isolated(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7)
        target = await service.select_tab(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7, tab_index=2)
        manager.block_selection = True
        selection = asyncio.create_task(service.select_tab(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7, tab_index=2))
        await manager.selection_started.wait()
        request = {
            "target_id": target["targetId"], "connection_generation": target["connectionGeneration"],
            "navigation_generation": 0, "observation_generation": 0, "action": "snapshot", "arguments": {},
        }
        call = asyncio.create_task(service.call(owner="alice", host_session_id="chat-1", workspace_id="workspace-1", binding_revision=7, tool_name="browser_observe", arguments=request))
        await asyncio.sleep(0)
        self.assertFalse(any(name == "browser_snapshot" for _cid, name, _args in manager.calls))
        manager.release_selection.set()
        await selection
        await call
        self.assertEqual(sum(name == "browser_snapshot" for _cid, name, _args in manager.calls), 1)

    async def test_waiting_control_call_cannot_borrow_new_turn_policy(self):
        import asyncio

        manager = FakeBrowserManager()
        service = BrowserControlService(manager)
        owner, chat, workspace, revision = "alice", "chat-1", "workspace-1", 7
        await service.open_isolated(owner=owner, host_session_id=chat, workspace_id=workspace,
                                    binding_revision=revision)
        target = await service.select_tab(owner=owner, host_session_id=chat, workspace_id=workspace,
                                          binding_revision=revision, tab_index=2)
        scope = service._scope_key(owner, chat, workspace, revision)
        request = {
            "target_id": target["targetId"], "connection_generation": target["connectionGeneration"],
            "navigation_generation": target["navigationGeneration"],
            "observation_generation": target["observationGeneration"],
            "action": "click", "arguments": {"target": "button-1"},
        }
        service.publish_turn_policy(owner=owner, host_session_id=chat, workspace_id=workspace,
                                    binding_revision=revision, turn_id="turn-1",
                                    envelope={"mode": "agent", "allowed_tools": ["browser_control"]})
        lock = service._scope_lock(scope)
        async with lock:
            call = asyncio.create_task(service.call(
                owner=owner, host_session_id=chat, workspace_id=workspace, binding_revision=revision,
                tool_name="browser_control", arguments=request, require_active_policy=True,
            ))
            await asyncio.sleep(0)
            service.publish_turn_policy(owner=owner, host_session_id=chat, workspace_id=workspace,
                                        binding_revision=revision, turn_id="turn-2",
                                        envelope={"mode": "agent", "allowed_tools": ["browser_control"]})
        with self.assertRaises(Exception):
            await call
        self.assertFalse(any(name == "browser_click" for _cid, name, _args in manager.calls))


if __name__ == "__main__":
    unittest.main()
