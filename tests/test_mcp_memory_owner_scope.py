import asyncio
from pathlib import Path

import mcp_servers.memory_server as memory_server
from src.memory import MemoryManager


class FakeVector:
    healthy = True

    def __init__(self):
        self.added = []
        self.removed = []

    def add(self, memory_id, text):
        self.added.append((memory_id, text))

    def remove(self, memory_id):
        self.removed.append(memory_id)


def _tool_text(arguments):
    result = asyncio.run(memory_server.call_tool("manage_memory", arguments))
    return result[0].text


def _entry(manager, text, owner=None, memory_id=None, category="fact"):
    entry = manager.add_entry(text, owner=owner, category=category)
    if memory_id:
        entry["id"] = memory_id
    return entry


def _configure_server(monkeypatch, manager, vector=None):
    monkeypatch.setattr(memory_server, "_memory_manager", manager)
    monkeypatch.setattr(memory_server, "_memory_vector", vector)
    monkeypatch.setattr(memory_server, "_initialized", True)
    for key in memory_server._OWNER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_mcp_memory_uses_configured_owner_for_all_operations(monkeypatch, tmp_path):
    manager = MemoryManager(str(tmp_path))
    vector = FakeVector()
    alice = _entry(
        manager,
        "Alice likes green tea",
        owner="alice",
        memory_id="aaaaaaaa-0000-0000-0000-000000000000",
    )
    bob = _entry(
        manager,
        "Bob likes espresso",
        owner="bob",
        memory_id="bbbbbbbb-0000-0000-0000-000000000000",
    )
    manager.save([alice, bob])
    _configure_server(monkeypatch, manager, vector)
    monkeypatch.setenv("ODYSSEUS_MCP_MEMORY_OWNER", "alice")

    list_text = _tool_text({"action": "list"})
    assert "Alice likes green tea" in list_text
    assert "Bob likes espresso" not in list_text

    search_text = _tool_text({"action": "search", "text": "likes"})
    assert "Alice likes green tea" in search_text
    assert "Bob likes espresso" not in search_text

    add_text = _tool_text({
        "action": "add",
        "text": "Alice prefers concise notes",
        "category": "preference",
    })
    assert "Memory added" in add_text
    added = next(
        entry for entry in manager.load_all()
        if entry["text"] == "Alice prefers concise notes"
    )
    assert added["owner"] == "alice"
    assert vector.added == [(added["id"], "Alice prefers concise notes")]

    edit_text = _tool_text({
        "action": "edit",
        "memory_id": bob["id"][:8],
        "text": "Bob changed",
    })
    assert edit_text == "Error: Memory 'bbbbbbbb' not found"
    bob_after_edit = next(
        entry for entry in manager.load_all()
        if entry["id"] == bob["id"]
    )
    assert bob_after_edit["text"] == "Bob likes espresso"

    delete_text = _tool_text({"action": "delete", "memory_id": bob["id"][:8]})
    assert delete_text == "Error: Memory 'bbbbbbbb' not found"
    assert any(entry["id"] == bob["id"] for entry in manager.load_all())


def test_mcp_memory_fails_closed_without_owner_for_owner_scoped_store(monkeypatch, tmp_path):
    manager = MemoryManager(str(tmp_path))
    alice = _entry(manager, "Alice private memory", owner="alice", memory_id="aaaaaaaa-0000")
    bob = _entry(manager, "Bob private memory", owner="bob", memory_id="bbbbbbbb-0000")
    manager.save([alice, bob])
    _configure_server(monkeypatch, manager, FakeVector())
    before = manager.load_all()

    actions = [
        {"action": "list"},
        {"action": "search", "text": "private"},
        {"action": "add", "text": "new ownerless memory"},
        {"action": "edit", "memory_id": alice["id"][:8], "text": "changed"},
        {"action": "delete", "memory_id": alice["id"][:8]},
    ]

    for arguments in actions:
        assert _tool_text(arguments).startswith("Error: Memory MCP owner is not configured")

    assert manager.load_all() == before


def test_mcp_memory_preserves_ownerless_local_behavior(monkeypatch, tmp_path):
    manager = MemoryManager(str(tmp_path))
    legacy = _entry(
        manager,
        "Legacy local memory",
        memory_id="llllllll-0000-0000-0000-000000000000",
    )
    manager.save([legacy])
    _configure_server(monkeypatch, manager, FakeVector())

    assert "Legacy local memory" in _tool_text({"action": "list"})
    assert "Legacy local memory" in _tool_text({"action": "search", "text": "legacy"})

    add_text = _tool_text({"action": "add", "text": "Another local memory"})
    assert "Memory added" in add_text
    added = next(
        entry for entry in manager.load_all()
        if entry["text"] == "Another local memory"
    )
    assert "owner" not in added

    assert _tool_text({
        "action": "edit",
        "memory_id": legacy["id"][:8],
        "text": "Updated local memory",
    }) == "Memory updated: Updated local memory"
    assert any(entry["text"] == "Updated local memory" for entry in manager.load_all())

    delete_text = _tool_text({"action": "delete", "memory_id": legacy["id"][:8]})
    assert delete_text.startswith("Memory deleted:")
    assert all(entry["id"] != legacy["id"] for entry in manager.load_all())


def test_memory_server_environment_cannot_select_legacy_vector(monkeypatch, tmp_path):
    import src.app_initializer as app_initializer
    import src.constants as constants
    import src.frankenmemory_provider as provider_module
    import src.memory_vector as memory_vector

    created = []

    class _Provider:
        def __init__(self, **kwargs):
            created.append(kwargs)

    def _legacy_vector(*_args, **_kwargs):
        raise AssertionError("legacy MemoryVectorStore must never be instantiated")

    monkeypatch.setenv("MEMORY_PROVIDER", "legacy")
    monkeypatch.setattr(app_initializer, "prepare_frankenmemory_database", lambda: "db-id")
    monkeypatch.setattr(constants, "FM_DB_PATH", str(tmp_path / "frankenmemory.db"))
    monkeypatch.setattr(provider_module, "FrankenmemoryProvider", _Provider)
    monkeypatch.setattr(memory_vector, "MemoryVectorStore", _legacy_vector)
    monkeypatch.setattr(memory_server, "_memory_manager", None)
    monkeypatch.setattr(memory_server, "_memory_vector", None)
    monkeypatch.setattr(memory_server, "_memory_provider", None)
    monkeypatch.setattr(memory_server, "_initialization_error", None)
    monkeypatch.setattr(memory_server, "_initialized", False)

    memory_server._ensure_init()

    assert created and memory_server._memory_provider is not None
    assert memory_server._memory_vector is None
    source = Path(memory_server.__file__).read_text(encoding="utf-8")
    assert "MEMORY_PROVIDER" not in source
    assert "MemoryVectorStore" not in source


def test_memory_server_fails_closed_when_frankenmemory_is_unavailable(monkeypatch):
    monkeypatch.setattr(memory_server, "_ensure_init", lambda: None)
    monkeypatch.setattr(memory_server, "_memory_provider", None)
    monkeypatch.setattr(memory_server, "_memory_manager", None)
    monkeypatch.setattr(memory_server, "_initialization_error", "fm-mcp missing")

    assert _tool_text({"action": "list"}) == (
        "Error: Frankenmemory provider not available: fm-mcp missing"
    )


def test_memory_server_init_retries_after_transient_failure(monkeypatch, tmp_path):
    import src.app_initializer as app_initializer
    import src.constants as constants
    import src.frankenmemory_provider as provider_module

    attempts = []

    def _prepare():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient database preparation failure")
        return "db-id"

    class _Provider:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(app_initializer, "prepare_frankenmemory_database", _prepare)
    monkeypatch.setattr(constants, "FM_DB_PATH", str(tmp_path / "frankenmemory.db"))
    monkeypatch.setattr(provider_module, "FrankenmemoryProvider", _Provider)
    monkeypatch.setattr(memory_server, "_memory_provider", None)
    monkeypatch.setattr(memory_server, "_initialization_error", None)
    monkeypatch.setattr(memory_server, "_initialized", False)

    memory_server._ensure_init()
    assert memory_server._memory_provider is None
    assert "transient" in memory_server._initialization_error

    memory_server._ensure_init()
    assert len(attempts) == 2
    assert memory_server._memory_provider is not None
    assert memory_server._initialization_error is None
