from pathlib import Path

import pytest

from src.rag_manager import RAGManager


ROOT = Path(__file__).resolve().parents[1]


class _FakeVectorRAG:
    def __init__(self):
        self.calls = []
        self.search_calls = []
        self.retrieve_calls = []

    def index_personal_documents(
        self,
        directory,
        file_extensions=None,
        owner=None,
        workspace_id=None,
        project_id=None,
    ):
        self.calls.append(
            {
                "directory": directory,
                "file_extensions": file_extensions,
                "owner": owner,
                "workspace_id": workspace_id,
                "project_id": project_id,
            }
        )
        return {"success": True, "indexed_count": 1}

    def search(self, query, k=5, owner=None, workspace_id=None, project_id=None):
        self.search_calls.append((query, k, owner, workspace_id, project_id))
        return []

    def retrieve(self, query, k=5, owner=None, workspace_id=None, project_id=None):
        self.retrieve_calls.append((query, k, owner, workspace_id, project_id))
        return []


def test_rag_manager_forwards_owner_and_file_extensions():
    fake = _FakeVectorRAG()
    manager = RAGManager.__new__(RAGManager)
    manager.vector_rag = fake
    extensions = {".md", ".txt"}

    result = manager.index_personal_documents(
        "/tmp/personal",
        file_extensions=extensions,
        owner="alice",
    )

    assert result == {"success": True, "indexed_count": 1}
    assert fake.calls == [
        {
            "directory": "/tmp/personal",
            "file_extensions": extensions,
            "owner": "alice",
            "workspace_id": None,
            "project_id": None,
        }
    ]


def test_rag_manager_forwards_workspace_and_project_scope():
    fake = _FakeVectorRAG()
    manager = RAGManager.__new__(RAGManager)
    manager.vector_rag = fake

    manager.search("needle", k=3, owner="alice", workspace_id="workspace", project_id="project")
    manager.retrieve("needle", k=4, owner="alice", workspace_id="workspace", project_id="project")
    manager.index_personal_documents(
        "/tmp/personal",
        owner="alice",
        workspace_id="workspace",
        project_id="project",
    )

    assert fake.search_calls == [("needle", 3, "alice", "workspace", "project")]
    assert fake.retrieve_calls == [("needle", 4, "alice", "workspace", "project")]
    assert fake.calls == [
        {
            "directory": "/tmp/personal",
            "file_extensions": None,
            "owner": "alice",
            "workspace_id": "workspace",
            "project_id": "project",
        }
    ]


def test_rag_manager_cannot_select_chroma_from_environment(monkeypatch):
    import src.rag_manager as module

    created = []

    class _Canonical:
        backend = "frankenmemory"

        def __init__(self):
            created.append(True)

    monkeypatch.setenv("RAG_BACKEND", "chroma")
    monkeypatch.setattr(module, "VectorRAG", _Canonical)

    manager = module.RAGManager(persist_directory="/tmp/legacy-chroma")

    assert created == [True]
    assert manager.vector_rag.backend == "frankenmemory"


def test_live_stack_has_no_chroma_backend_selector():
    runtime = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "src/rag_manager.py",
            "src/rag_singleton.py",
            "src/tool_index.py",
            "src/app_initializer.py",
            "routes/embedding_routes.py",
            "mcp_servers/memory_server.py",
            "mcp_servers/rag_server.py",
            "scripts/index_documents.py",
            "start-macos.sh",
        )
    )

    assert "RAG_BACKEND" not in runtime
    assert "TOOL_INDEX_BACKEND" not in runtime
    assert "MEMORY_VECTOR_ENABLED" not in runtime
    assert "MemoryVectorStore" not in runtime
    assert "MemoryVectorStore(" not in (ROOT / "src/app_initializer.py").read_text(encoding="utf-8")
    assert "chromadb_health" not in (ROOT / "src/service_health.py").read_text(encoding="utf-8")
    # The app still has a provider-compatibility selector, but it cannot
    # resurrect Chroma because app initialization never constructs the retired
    # vector store.  The standalone memory MCP path was the unsafe selector.
    memory_server = (ROOT / "mcp_servers/memory_server.py").read_text(encoding="utf-8")
    assert "MEMORY_PROVIDER" not in memory_server
    assert "MemoryVectorStore" not in memory_server


def test_index_documents_cli_requires_owner():
    from scripts import index_documents

    with pytest.raises(SystemExit) as exc:
        index_documents.main([])
    assert exc.value.code == 2


def test_index_documents_cli_forwards_tenant_scope(monkeypatch, tmp_path):
    from scripts import index_documents
    import src.rag_singleton as rag_singleton

    (tmp_path / "note.md").write_text("scoped knowledge", encoding="utf-8")
    calls = []

    class _Rag:
        def index_personal_documents(self, directory, **scope):
            calls.append((directory, scope))
            return {"success": True, "indexed_count": 1, "failed_count": 0}

        @staticmethod
        def get_stats():
            return {"healthy": True}

    monkeypatch.setattr(rag_singleton, "get_rag_manager", lambda: _Rag())

    result = index_documents.main([
        "--owner", "alice",
        "--workspace-id", "workspace",
        "--project-id", "project",
        "--directory", str(tmp_path),
    ])

    assert result == 0
    assert calls == [(
        str(tmp_path),
        {"owner": "alice", "workspace_id": "workspace", "project_id": "project"},
    )]
