"""Focused owner/generation checks for the Browser target lease registry."""

import asyncio
import unittest

from src.openclank.browser_control import (
    BrowserTargetNotFound,
    BrowserTargetRegistry,
    BrowserTargetScopeError,
    BrowserTargetStale,
)


class FakeManager:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.observation = {"current": True, "tab_id": "index:2", "title": "Synthetic page", "url": "https://example.test/start"}

    async def observe(self, target):
        return dict(self.observation)

    async def call_once(self, connection_id, tool_name, arguments):
        self.calls.append((connection_id, tool_name, arguments))
        if self.fail:
            raise TimeoutError("synthetic unknown action outcome")
        return {"ok": True}


class BrowserTargetRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = BrowserTargetRegistry(clock=lambda: 100.0)
        self.target = self.registry.register_selected_tab(
            owner_id="alice",
            host_session_id="chat-1",
            workspace_id="work-a",
            binding_revision=4,
            connection_id="private-mcp-client-1",
            connection_generation="c7",
            tab_id="index:2",
            title="Synthetic page",
            url="https://example.test/start",
        )

    def test_public_target_hides_internal_connection_id(self):
        self.assertNotIn("connectionId", self.target)
        self.assertNotIn("private-mcp-client-1", repr(self.target))
        self.assertEqual(self.target["tabId"], "index:2")

    def test_owner_workspace_and_generations_are_required(self):
        with self.assertRaises(BrowserTargetScopeError):
            self.registry.resolve(
                target_id=self.target["targetId"], owner_id="bob", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0,
            )
        with self.assertRaises(BrowserTargetStale):
            self.registry.resolve(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="old", navigation_generation=0,
            )
        with self.assertRaises(BrowserTargetStale):
            self.registry.resolve(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=1,
            )

    def test_navigation_same_origin_advances_page_generation(self):
        updated = self.registry.record_observation(
            target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
            connection_generation="c7", navigation_generation=0, observed_tab_id="index:2",
            title="Next page", url="https://example.test/next",
        )
        self.assertEqual(updated["navigationGeneration"], 1)
        self.assertEqual(updated["observationGeneration"], 1)
        with self.assertRaises(BrowserTargetStale):
            self.registry.resolve(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0,
            )

    def test_changed_tab_invalidates_instead_of_retargeting(self):
        with self.assertRaises(BrowserTargetStale):
            self.registry.record_observation(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0, observed_tab_id="tab-3",
                title="Different tab", url="https://other.test/",
            )
        with self.assertRaises(BrowserTargetNotFound):
            self.registry.resolve(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0,
            )

    async def test_dispatches_once_and_invalidates_snapshot_refs_before_call(self):
        manager = FakeManager()
        result = await self.registry.dispatch_once(
            target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
            connection_generation="c7", navigation_generation=0, observation_generation=0,
            tool_name="browser_click", arguments={"target": "snapshot-ref"}, verify_selected_tab=manager.observe,
            call=manager.call_once, refresh_selected_tab=manager.observe,
        )
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(manager.calls[0][0], "private-mcp-client-1")
        self.assertEqual(result["observationGeneration"], 1)
        with self.assertRaises(BrowserTargetStale):
            await self.registry.dispatch_once(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0, observation_generation=0,
                tool_name="browser_click", arguments={"target": "snapshot-ref"}, verify_selected_tab=manager.observe,
            call=manager.call_once, refresh_selected_tab=manager.observe,
            )
        self.assertEqual(len(manager.calls), 1)

    async def test_unknown_action_outcome_is_not_replayed(self):
        manager = FakeManager()
        manager.fail = True
        with self.assertRaises(TimeoutError):
            await self.registry.dispatch_once(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0, observation_generation=0,
                tool_name="browser_click", arguments={"target": "snapshot-ref"}, verify_selected_tab=manager.observe,
            call=manager.call_once, refresh_selected_tab=manager.observe,
            )
        self.assertEqual(len(manager.calls), 1)
        with self.assertRaises(BrowserTargetStale):
            self.registry.resolve(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0, observation_generation=0,
            )

    async def test_preflight_rejects_index_reuse_before_dispatch(self):
        manager = FakeManager()
        manager.observation["url"] = "https://different.test/reused-index"
        with self.assertRaises(BrowserTargetStale):
            await self.registry.dispatch_once(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0, observation_generation=0,
                tool_name="browser_click", arguments={}, verify_selected_tab=manager.observe,
                call=manager.call_once, refresh_selected_tab=manager.observe,
            )
        self.assertEqual(manager.calls, [])
        with self.assertRaises(BrowserTargetNotFound):
            self.registry.resolve(
                target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
                connection_generation="c7", navigation_generation=0,
            )

    def test_embedded_url_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register_selected_tab(
                owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4, connection_id="private",
                connection_generation="c7", tab_id="index:0", title="secret",
                url="https://alice:password@example.test/private?token=secret",
            )

    async def test_actions_on_one_connection_are_serialized(self):
        manager = FakeManager()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def first_call(connection_id, tool_name, arguments):
            calls.append("first-start")
            first_entered.set()
            await release_first.wait()
            calls.append("first-end")
            return {"ok": True}

        async def second_call(connection_id, tool_name, arguments):
            calls.append("second")
            return {"ok": True}

        first = asyncio.create_task(self.registry.dispatch_once(
            target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
            connection_generation="c7", navigation_generation=0, observation_generation=0,
            tool_name="browser_click", arguments={}, verify_selected_tab=manager.observe,
            call=first_call, refresh_selected_tab=manager.observe,
        ))
        await first_entered.wait()
        second = asyncio.create_task(self.registry.dispatch_once(
            target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
            connection_generation="c7", navigation_generation=0, observation_generation=1,
            tool_name="browser_click", arguments={}, verify_selected_tab=manager.observe,
            call=second_call, refresh_selected_tab=manager.observe,
        ))
        await asyncio.sleep(0)
        self.assertEqual(calls, ["first-start"])
        release_first.set()
        await asyncio.gather(first, second)
        self.assertEqual(calls, ["first-start", "first-end", "second"])

    async def test_release_during_pending_action_suppresses_late_result(self):
        manager = FakeManager()
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def pending_call(connection_id, tool_name, arguments):
            manager.calls.append((connection_id, tool_name, arguments))
            entered.set()
            await finish.wait()
            return {"ok": True}

        action = asyncio.create_task(self.registry.dispatch_once(
            target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
            connection_generation="c7", navigation_generation=0, observation_generation=0,
            tool_name="browser_click", arguments={}, verify_selected_tab=manager.observe, call=pending_call,
            refresh_selected_tab=manager.observe,
        ))
        await entered.wait()
        self.assertTrue(self.registry.release(
            target_id=self.target["targetId"], owner_id="alice", host_session_id="chat-1", workspace_id="work-a", binding_revision=4,
        ))
        finish.set()
        with self.assertRaises(BrowserTargetStale):
            await action
        self.assertEqual(len(manager.calls), 1)


if __name__ == "__main__":
    unittest.main()
