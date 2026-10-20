"""Frankenmemory is the only live memory/RAG authority."""
from unittest.mock import MagicMock

import src.app_initializer as app_init
import src.memory_vector as memory_vector_mod
import src.service_health as sh


class _UnhealthyVectorStore:
    """Stand-in for a MemoryVectorStore whose init failed: present but inert."""
    healthy = False

    def count(self):
        return 0

    def search(self, *a, **k):
        return []


def _neutralize_collaborators(monkeypatch):
    """Stub out everything initialize_managers() builds except the vector store,
    so the test isolates the memory_vector health-handling branch."""
    for name in [
        "MemoryManager", "SkillsManager", "SessionManager", "UploadHandler",
        "PersonalDocsManager", "APIKeyManager", "PresetManager",
        "MemoryProviderRegistry", "NativeMemoryProvider", "ChatProcessor",
        "ResearchHandler", "ChatHandler", "ModelDiscovery",
    ]:
        monkeypatch.setattr(app_init, name, lambda *a, **k: MagicMock())
    monkeypatch.setattr(app_init, "set_session_manager", lambda *a, **k: None)
    monkeypatch.setattr(app_init, "update_search_config", lambda *a, **k: None)
    monkeypatch.setattr(app_init, "create_directories", lambda: None)


def test_memory_vector_environment_toggle_cannot_instantiate_chroma(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_PROVIDER", "native")
    monkeypatch.delenv("FM_DB_ID", raising=False)
    _neutralize_collaborators(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy memory vector must not be constructed")

    monkeypatch.setattr(
        memory_vector_mod, "MemoryVectorStore",
        forbidden,
    )

    result = app_init.initialize_managers(str(tmp_path), rag_manager=None)

    assert result["memory_vector"] is None


def test_primary_provider_never_inherits_child_broker_environment(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MEMORY_PROVIDER", "frankenmemory")
    monkeypatch.setenv(
        "OPEN_CLANK_MEMORY_BROKER_URL",
        "http://127.0.0.1:7000/api/internal/frankenmemory/tool",
    )
    monkeypatch.setenv("OPEN_CLANK_MEMORY_BROKER_TOKEN", "child-only-token")
    monkeypatch.delenv("MEMORY_VECTOR_ENABLED", raising=False)
    _neutralize_collaborators(monkeypatch)
    monkeypatch.setattr(
        app_init,
        "prepare_frankenmemory_database",
        lambda: "db-test",
    )
    constructor = MagicMock(return_value=MagicMock(provider_id="frankenmemory"))
    monkeypatch.setattr(app_init, "FrankenmemoryProvider", constructor)

    app_init.initialize_managers(str(tmp_path), rag_manager=None)

    assert constructor.call_args.kwargs["broker_url"] == ""
    assert constructor.call_args.kwargs["broker_token"] == ""


def test_frankenmemory_health_ignores_retired_vector_state():
    healthy_rag = MagicMock(healthy=True)

    assert sh.frankenmemory_rag_health(None)["status"] == sh.DISABLED
    assert sh.frankenmemory_rag_health(healthy_rag)["status"] == sh.OK
