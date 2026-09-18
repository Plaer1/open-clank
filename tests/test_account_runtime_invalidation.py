from __future__ import annotations


def test_prompt_cache_invalidation_is_owner_scoped(monkeypatch):
    import src.agent_loop as agent_loop

    key = (frozenset(), False, False, None, False, "sig", "alice", False, False)
    monkeypatch.setattr(agent_loop, "_cached_base_prompt", "alice prompt")
    monkeypatch.setattr(agent_loop, "_cached_base_prompt_key", key)

    assert agent_loop.invalidate_cached_base_prompt("bob") is False
    assert agent_loop._cached_base_prompt == "alice prompt"
    assert agent_loop.invalidate_cached_base_prompt("ALICE") is True
    assert agent_loop._cached_base_prompt is None
    assert agent_loop._cached_base_prompt_key is None


def test_files_client_invalidation_closes_only_matching_owner(monkeypatch):
    import src.openclank.files_service_client as clients

    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    alice = FakeClient()
    alice_agent = FakeClient()
    bob = FakeClient()
    monkeypatch.setattr(clients, "_CLIENTS", {
        ("alice", "scope", "framed"): alice,
        ("bob", "scope", "framed"): bob,
    })
    monkeypatch.setattr(clients, "_AGENT_CLIENTS", {
        ("alice", None, 1, "framed"): alice_agent,
    })

    assert clients.close_clients_for_owner("ALICE") == 2
    assert alice.closed and alice_agent.closed
    assert not bob.closed
    assert list(clients._CLIENTS) == [("bob", "scope", "framed")]


def test_email_runtime_invalidation_preserves_other_owner(monkeypatch):
    import routes.email_routes as email_routes

    monkeypatch.setattr(email_routes, "_start_poller", lambda: None)
    router = email_routes.setup_email_routes()
    invalidate = router.invalidate_owner_runtime
    closure = dict(zip(invalidate.__code__.co_freevars, (cell.cell_contents for cell in invalidate.__closure__)))

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def logout(self):
            self.closed = True

    alice_conn = FakeConnection()
    bob_conn = FakeConnection()
    closure["_LIST_CACHE"][("a", "INBOX", "", 20, 0, "", 0, "alice")] = (99, {})
    closure["_LIST_CACHE"][("b", "INBOX", "", 20, 0, "", 0, "bob")] = (99, {})
    closure["_FOLDER_CACHE"][("a", "alice")] = (99, {})
    closure["_FOLDER_CACHE"][("b", "bob")] = (99, {})
    closure["_READ_CACHE"][("a", "INBOX", "1", "alice", 0)] = (99, {})
    closure["_READ_CACHE"][("b", "INBOX", "1", "bob", 0)] = (99, {})
    closure["_IMAP_POOL"][("a", "alice")] = (alice_conn, 1)
    closure["_IMAP_POOL"][("b", "bob")] = (bob_conn, 1)

    receipt = invalidate("ALICE")

    assert receipt == {"list": 1, "folder": 1, "read": 1, "warming": 0, "pool": 1}
    assert alice_conn.closed and not bob_conn.closed
    assert all("alice" not in key for key in closure["_LIST_CACHE"])
    assert ("b", "bob") in closure["_FOLDER_CACHE"]
    assert ("b", "INBOX", "1", "bob", 0) in closure["_READ_CACHE"]
    assert ("b", "bob") in closure["_IMAP_POOL"]
