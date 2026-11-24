"""Canonical Frankenmemory RAG health classification."""

from src import service_health as sh
from src.memery_rag import MemeryRAG


class _Store:
    def __init__(self, healthy):
        self.healthy = healthy


def test_frankenmemory_rag_healthy_ok():
    s = sh.memery_rag_health(_Store(True))
    assert s["status"] == sh.OK
    assert s["name"] == "memery_rag"
    assert s["meta"]["rag"] is True
    assert s["meta"]["index_health"] == {}


def test_frankenmemory_rag_unhealthy_down():
    s = sh.memery_rag_health(_Store(False))
    assert s["status"] == sh.DOWN


def test_frankenmemory_rag_absent_disabled():
    s = sh.memery_rag_health(None)
    assert s["status"] == sh.DISABLED


def test_frankenmemory_rag_reports_missing_vector_as_degraded(tmp_path):
    rag = MemeryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document(
        "fresh exact document",
        {"owner": "alice", "source": str(tmp_path / "fresh.md")},
    )

    status = sh.memery_rag_health(rag)

    assert status["status"] == sh.DEGRADED
    missing = [
        row
        for row in status["meta"]["indexes"]
        if row["logical_space"] == "documents_vector"
    ]
    assert missing[0]["health"] == "missing"
    assert missing[0]["reason"] == "no_active_pointer"


def test_frankenmemory_rag_reports_fts_runtime_loss_as_degraded(tmp_path):
    rag = MemeryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document(
        "fresh exact document",
        {"owner": "alice", "source": str(tmp_path / "fresh.md")},
    )
    rag._fts_available = False

    status = sh.memery_rag_health(rag)

    assert status["status"] == sh.DEGRADED
    assert status["meta"]["fts_available"] is False
    assert "FTS runtime unavailable" in status["detail"]
