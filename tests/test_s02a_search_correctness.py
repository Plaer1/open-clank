"""Synthetic regressions for S02A; fixture vectors prove transport, not quality."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from src.frankenmemory_rag import FrankenmemoryRAG


class FixtureEmbedding:
    model = "s02a-fixture"

    def get_sentence_embedding_dimension(self):
        return 3

    def encode(self, texts, normalize_embeddings=True):
        return [[1.0, 0.0, 0.0] if "cat" in text else [0.0, 1.0, 0.0] for text in texts]


def vector_rag(tmp_path):
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"), embedding_client=FixtureEmbedding(),
                          embedding_provider_ref="owner:alice:fixture")
    assert rag.add_document("cats are playful", {"owner": "alice", "source": "fixture://cats"})
    rag.build_embedding_generation(owner="alice", provider_ref="owner:alice:fixture", publish=True)
    return rag


def test_first_fts_rank_is_not_relevance(tmp_path):
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"))
    assert rag.add_document("cats are playful", {"owner": "alice", "source": "fixture://cats"})
    hit = rag.search("cats ocean galaxy telescope elephant", owner="alice")[0]
    assert hit["score_components"]["ranks"]["fts"] == 1
    assert hit["similarity"] == 0.2
    assert hit["score_components"]["rrf"] > 0


@pytest.mark.parametrize("query", ["pineapple", "obsolete"])
def test_nonmatching_replacement_shadows_entire_parent_source(tmp_path, query):
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"))
    source = "fixture://shared"
    assert rag.add_document("obsolete pineapple " * 180, {"owner": "alice", "source": source})
    assert rag.add_document("revised banana", {"owner": "alice", "source": source,
                                             "workspace_id": "w", "project_id": "p"})
    assert rag.search(query, owner="alice", workspace_id="w", project_id="p", k=100) == []
    assert rag.search(query, owner="alice", workspace_id="other")
    assert rag.search(query, owner="bob", workspace_id="w", project_id="p") == []


def test_empty_result_keeps_runtime_vector_receipt(tmp_path):
    rag = vector_rag(tmp_path)
    rag._embedding_client = None
    answer = rag.search_explain("unmatched", owner="alice")
    assert answer["results"] == []
    assert answer["generation_map"]["documents_vector:owner"]["health"] == "current"
    assert answer["degraded"]
    assert any("client_unavailable" in reason for reason in answer["degradation_reasons"])
    assert answer["execution_receipt"]["documents_vector:owner"]["status"] == "unavailable"


def test_throwing_embedder_keeps_lexical_and_empty_receipts(tmp_path):
    rag = vector_rag(tmp_path)
    class Throwing(FixtureEmbedding):
        def encode(self, texts, normalize_embeddings=True):
            raise RuntimeError("synthetic provider failure")
    rag._embedding_client = Throwing()
    answer = rag.search_explain("cats", owner="alice")
    assert answer["results"][0]["document"] == "cats are playful"
    assert answer["degraded"]
    assert answer["execution_receipt"]["documents_vector:owner"]["status"] == "failed"
    empty = rag.search_explain("unmatched", owner="alice")
    assert empty["results"] == [] and empty["degraded"]


def test_embedding_cancellation_propagates(tmp_path):
    rag = vector_rag(tmp_path)
    class Cancelled(FixtureEmbedding):
        def encode(self, texts, normalize_embeddings=True):
            raise asyncio.CancelledError()
    rag._embedding_client = Cancelled()
    with pytest.raises(asyncio.CancelledError):
        rag.search_explain("cats", owner="alice")


def test_chat_gate_does_not_inject_weak_first_fts_hit(tmp_path):
    from src.chat_processor import ChatProcessor
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"))
    assert rag.add_document("cats are playful", {"owner": "alice", "source": "fixture://cats"})
    processor = ChatProcessor(None, SimpleNamespace(rag_manager=rag))
    preface, sources, _ = asyncio.run(processor.build_context_preface(
        "cats ocean galaxy telescope elephant", None, owner="alice", use_memory=False,
        use_skills=False))
    assert sources == []
    assert all("cats are playful" not in item["content"] for item in preface)
    _, exact_sources, _ = asyncio.run(processor.build_context_preface(
        "cats", None, owner="alice", use_memory=False, use_skills=False))
    assert exact_sources


@pytest.mark.parametrize("unavailable", ["chunks", "documents", "parser", "hydrate"])
def test_source_authority_shadows_unmaterializable_replacement(tmp_path, unavailable):
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"))
    source = "fixture://shared"
    assert rag.add_document("obsolete pineapple", {"owner": "alice", "source": source})
    assert rag.add_document("revised banana", {"owner": "alice", "source": source, "workspace_id": "w"})
    if unavailable == "hydrate":
        original = rag._canonical_rows
        rag._canonical_rows = lambda conn, owner, workspace, project: [] if workspace == "w" else original(conn, owner, workspace, project)
    else:
        with sqlite3.connect(rag.db_path) as conn:
            if unavailable in {"chunks", "documents"}:
                conn.execute("DELETE FROM fm_v2_chunks WHERE document_id IN (SELECT d.document_id FROM fm_v2_documents d JOIN fm_v2_sources s ON s.source_id=d.source_id WHERE s.workspace_key='w')")
                if unavailable == "documents":
                    conn.execute("DELETE FROM fm_v2_documents WHERE source_id IN (SELECT source_id FROM fm_v2_sources WHERE workspace_key='w')")
            else:
                conn.execute("UPDATE fm_v2_documents SET parser_version='unavailable' WHERE source_id IN (SELECT source_id FROM fm_v2_sources WHERE workspace_key='w')")
    assert rag.search("pineapple", owner="alice", workspace_id="w") == []


def test_source_shadow_explanation_retains_ancestor_identity(tmp_path):
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"))
    source = "fixture://shared"
    assert rag.add_document("parent amber", {"owner": "alice", "source": source})
    parent = rag.search("amber", owner="alice")[0]
    assert rag.add_document("replacement amber", {"owner": "alice", "workspace_id": "w", "source": source})
    current = rag.search("amber", owner="alice", workspace_id="w")[0]
    assert current["retrieval_explanation"]["shadowed_chunk_id"] == parent["id"]


def test_vector_parent_is_filtered_before_candidate_ranking(tmp_path):
    rag = vector_rag(tmp_path)
    assert rag.add_document("banana", {"owner": "alice", "workspace_id": "w", "source": "fixture://cats"})
    assert rag.search("catlike", owner="alice", workspace_id="w") == []


def test_search_schemas_describe_active_capabilities():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schemas = {row["function"]["name"]: row["function"] for row in FUNCTION_TOOL_SCHEMAS}
    grep = schemas["grep"]
    glob = schemas["glob"]
    assert grep["parameters"]["properties"]["mode"]["enum"] == ["literal"]
    assert "file paths" in grep["description"]
    for schema in (grep, glob):
        assert "nonresumable" in schema["description"]
        assert schema["parameters"]["properties"]["cursor"]["enum"] == [0]
        assert "ignore_case" in schema["parameters"]["properties"]
        assert "include_hidden" in schema["parameters"]["properties"]


def test_runtime_fts_failure_is_failed_lane_not_empty_success(tmp_path):
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"))
    assert rag.add_document("cats are playful", {"owner": "alice", "source": "fixture://source"})
    with sqlite3.connect(rag.db_path) as conn:
        conn.execute("DROP TABLE fm_v2_chunks_fts")
    answer = rag.search_explain("cats", owner="alice")
    assert answer["generation_map"]["documents_fts:owner"]["health"] == "current"
    assert answer["execution_receipt"]["documents_fts:owner"]["status"] == "failed"
    assert answer["results"][0]["document"] == "cats are playful"
    assert any("query_failed:OperationalError" in reason for reason in answer["degradation_reasons"])
    empty = rag.search_explain("unmatched", owner="alice")
    assert empty["results"] == []
    assert empty["execution_receipt"]["documents_fts:owner"]["status"] == "failed"


def test_forget_between_row_and_source_reads_has_coherent_snapshot(tmp_path):
    rag = FrankenmemoryRAG(str(tmp_path / "rag.db"))
    assert rag.add_document("cats are playful", {"owner": "alice", "source": "fixture://source"})
    with sqlite3.connect(rag.db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original = rag._canonical_rows
    forgotten = False
    def interleaved(conn, owner, workspace, project):
        nonlocal forgotten
        rows = original(conn, owner, workspace, project)
        if not forgotten:
            forgotten = True
            with sqlite3.connect(rag.db_path) as writer:
                writer.execute("UPDATE fm_v2_sources SET forget_state='forgotten' WHERE owner_id='alice'")
        return rows
    rag._canonical_rows = interleaved
    # This request reads the pre-forget SQLite cut; the next sees forget.
    answer = rag.search_explain("cats", owner="alice")
    assert answer["results"][0]["document"] == "cats are playful"
    assert rag.search("cats", owner="alice") == []


def test_optional_encode_runs_after_snapshot_lock_is_released(tmp_path):
    rag = vector_rag(tmp_path)
    class Writing(FixtureEmbedding):
        def encode(self, texts, normalize_embeddings=True):
            assert rag.add_document("updated cats", {"owner": "alice", "source": "fixture://cats"})
            return super().encode(texts, normalize_embeddings=normalize_embeddings)
    rag._embedding_client = Writing()
    # Uses rollback-journal mode: a retained read lock would block this writer.
    answer = rag.search_explain("cats", owner="alice")
    assert answer["results"][0]["document"] == "cats are playful"
    assert answer["execution_receipt"]["documents_vector:owner"]["status"] == "executed"
