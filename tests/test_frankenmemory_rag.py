"""Real SQLite-backed canonical RAG tests (no Chroma mock)."""

import sqlite3
from pathlib import Path

import pytest

from src.frankenmemory_rag import FrankenmemoryRAG, _document_chunk_spans


FM_BIN = (
    Path(__file__).resolve().parents[1]
    / "mcp_servers"
    / "frankenmemory"
    / "target"
    / "release"
    / "fm-mcp"
)


class _FakeEmbeddingClient:
    model = "fixture-embedding-v1"

    def get_sentence_embedding_dimension(self):
        return 3

    def encode(self, texts, normalize_embeddings=True):
        vectors = []
        for text in texts:
            folded = text.casefold()
            if "cat" in folded or "feline" in folded:
                vectors.append([1.0, 0.0, 0.0])
            elif "ocean" in folded or "marine" in folded:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class _FailingEmbeddingClient(_FakeEmbeddingClient):
    def encode(self, texts, normalize_embeddings=True):
        raise RuntimeError("fixture provider unavailable")


class _InterruptingEmbeddingClient(_FakeEmbeddingClient):
    def __init__(self):
        self.calls = 0

    def encode(self, texts, normalize_embeddings=True):
        self.calls += 1
        if self.calls == 2:
            raise KeyboardInterrupt("simulated process death")
        return super().encode(texts, normalize_embeddings=normalize_embeddings)


class _ConcurrentWriterEmbeddingClient(_FakeEmbeddingClient):
    def __init__(self, callback):
        self.callback = callback
        self.called = False

    def encode(self, texts, normalize_embeddings=True):
        if not self.called:
            self.called = True
            self.callback()
        return super().encode(texts, normalize_embeddings=normalize_embeddings)


@pytest.mark.skipif(not FM_BIN.exists(), reason="fm-mcp release binary not built")
@pytest.mark.asyncio
async def test_rust_and_python_share_one_generation_schema(tmp_path: Path) -> None:
    """A current-source Rust database must already satisfy Python RAG's DDL."""
    from src.frankenmemory_provider import FrankenmemoryProvider

    db_path = str(tmp_path / "frankenmemory.db")
    provider = FrankenmemoryProvider(
        command=str(FM_BIN), env={"FM_DB_PATH": db_path}
    )
    await provider.initialize()
    await provider.shutdown()

    expected_columns = {
        "fm_v2_derived_generations": {
            "owner_id", "generation_id", "logical_space", "workspace_key",
            "project_key", "provider_ref", "model", "endpoint_class",
            "dimension", "normalization", "metric", "chunker_version",
            "config_fingerprint", "source_watermark", "row_count", "state",
            "retention_state", "failure_json", "created_at", "updated_at",
            "validated_at",
        },
        "fm_v2_index_pointers": {
            "owner_id", "logical_space", "workspace_key", "project_key",
            "generation_id", "publication_tx", "publication_watermark",
            "published_at",
        },
        "fm_v2_chunk_embeddings": {
            "owner_id", "generation_id", "chunk_id", "dimension", "embedding",
            "content_hash", "created_at",
        },
        "fm_v2_index_publications": {
            "owner_id", "publication_id", "logical_space", "workspace_key",
            "project_key", "previous_generation_id", "generation_id", "action",
            "publication_watermark", "created_at",
        },
    }
    canonical_objects = set(expected_columns) | {
        "fm_v2_generation_state_immutable",
        "fm_v2_active_generation_retained",
    }
    with sqlite3.connect(db_path) as conn:
        for table, columns in expected_columns.items():
            assert {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} == columns
        object_sql_before = dict(
            conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE name IN ("
                + ",".join("?" for _ in canonical_objects)
                + ")",
                tuple(sorted(canonical_objects)),
            )
        )
        assert set(object_sql_before) == canonical_objects
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name IN ('fm_v2_index_generations','fm_v2_index_heads')"
        ).fetchone()
        views = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE ?",
                ("fm_v2_index_%_compat",),
            )
        }
        assert views == {
            "fm_v2_index_generations_compat",
            "fm_v2_index_heads_compat",
        }

    assert FrankenmemoryRAG(db_path).healthy
    with sqlite3.connect(db_path) as conn:
        object_sql_after = dict(
            conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE name IN ("
                + ",".join("?" for _ in canonical_objects)
                + ")",
                tuple(sorted(canonical_objects)),
            )
        )
    assert object_sql_after == object_sql_before


def _add_non_rag_evidence(rag: FrankenmemoryRAG, owner: str) -> None:
    with sqlite3.connect(rag.db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fm_v2_evidence (
                owner_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                PRIMARY KEY(owner_id, evidence_id),
                FOREIGN KEY(owner_id, source_id, source_revision)
                    REFERENCES fm_v2_sources(owner_id, source_id, source_revision)
            )
            """
        )
        conn.execute(
            "INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (owner, "memory_source", "", "", "memory://fact", 1, "hash", "human", "now"),
        )
        conn.execute(
            "INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision) VALUES (?,?,?,?)",
            (owner, "memory_evidence", "memory_source", 1),
        )


def test_canonical_rag_indexes_chunks_and_isolates_owners(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.healthy
    assert rag.add_document("alpha project memory", {"owner": "alice", "source": str(tmp_path / "a.md")})
    assert rag.add_document("alpha project memory", {"owner": "bob", "source": str(tmp_path / "b.md")})
    assert rag.search("alpha", owner="alice")[0]["metadata"]["owner"] == "alice"
    assert rag.search("alpha", owner="bob")[0]["metadata"]["owner"] == "bob"


def test_canonical_rag_filters_same_owner_by_workspace_and_project(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document("private workspace alpha", {"owner": "alice", "workspace_id": "one", "project_id": "p1", "source": str(tmp_path / "one.md")})
    assert rag.add_document("private workspace beta", {"owner": "alice", "workspace_id": "two", "project_id": "p2", "source": str(tmp_path / "two.md")})
    assert [r["document"] for r in rag.search("private", owner="alice", workspace_id="one", project_id="p1")] == ["private workspace alpha"]
    assert [r["document"] for r in rag.search("private", owner="alice", workspace_id="two", project_id="p2")] == ["private workspace beta"]


def test_canonical_rag_rejects_project_scope_without_workspace(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document("global private alpha", {"owner": "alice", "source": str(tmp_path / "global.md")})
    assert rag.search("alpha", owner="alice", project_id="p1") == []


def test_canonical_rag_search_returns_only_latest_source_revision(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    source = str(tmp_path / "changing.md")
    scope = {"owner": "alice", "workspace_id": "w", "project_id": "p", "source": source}
    assert rag.add_document("shared answer obsolete cobalt", scope)
    assert rag.add_document("shared answer current amber", scope)

    assert rag.search("cobalt", owner="alice", workspace_id="w", project_id="p") == []
    assert [
        result["document"]
        for result in rag.search("shared answer", owner="alice", workspace_id="w", project_id="p")
    ] == ["shared answer current amber"]


def test_canonical_rag_forgotten_latest_revision_does_not_resurrect_older_source(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    source = str(tmp_path / "forgotten.md")
    scope = {"owner": "alice", "workspace_id": "w", "project_id": "p", "source": source}
    assert rag.add_document("shared answer obsolete cobalt", scope)
    assert rag.add_document("shared answer current amber", scope)
    with sqlite3.connect(rag.db_path) as conn:
        conn.execute(
            "UPDATE fm_v2_sources SET forget_state='forgotten' "
            "WHERE owner_id=? AND source_id=(SELECT source_id FROM fm_v2_sources WHERE owner_id=? AND source_uri=? LIMIT 1) "
            "AND source_revision=(SELECT MAX(source_revision) FROM fm_v2_sources WHERE owner_id=? AND source_uri=?)",
            ("alice", "alice", source, "alice", source),
        )

    assert rag.search("shared answer", owner="alice", workspace_id="w", project_id="p") == []


def test_canonical_rag_directory_import_is_repeatable_and_removable(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("typed evidence and exact spans", encoding="utf-8")
    (root / "two.txt").write_text("another source", encoding="utf-8")
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    first = rag.index_personal_documents(str(root), owner="alice")
    second = rag.index_personal_documents(str(root), owner="alice")
    assert first["indexed_count"] == 2
    assert second["indexed_count"] == 2
    assert rag.search("evidence", owner="alice")
    removed = rag.remove_directory(str(root), owner="alice")
    assert removed["removed"] == 2
    assert rag.search("evidence", owner="alice") == []


def test_directory_import_does_not_follow_file_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "inside.md").write_text("safe inside text", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret text", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))

    result = rag.index_personal_documents(str(root), owner="alice")

    assert result["indexed_count"] == 1
    assert result["skipped"] == 1
    assert rag.search("safe inside", owner="alice")
    assert rag.search("outside secret", owner="alice") == []


def test_directory_removal_without_owner_fails_closed(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    result = rag.remove_directory(str(tmp_path))
    assert result["success"] is False


def test_scoped_directory_removal_does_not_delete_visible_parent_scopes(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document("globalcobalt", {"owner": "alice", "source": str(root / "global.md")})
    assert rag.add_document("workspaceamber", {"owner": "alice", "workspace_id": "w", "source": str(root / "workspace.md")})
    assert rag.add_document("projectquartz", {"owner": "alice", "workspace_id": "w", "project_id": "p", "source": str(root / "project.md")})

    result = rag.remove_directory(str(root), owner="alice", workspace_id="w", project_id="p")

    assert result == {"success": True, "removed": 1}
    assert rag.search("globalcobalt", owner="alice")
    assert rag.search("workspaceamber", owner="alice", workspace_id="w")
    assert rag.search("projectquartz", owner="alice", workspace_id="w", project_id="p") == []


def test_scoped_source_delete_is_exact_not_read_visibility_union(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    source = str(tmp_path / "shared.md")
    assert rag.add_document("globalsapphire", {"owner": "alice", "source": source})
    assert rag.add_document("projecttopaz", {"owner": "alice", "workspace_id": "w", "project_id": "p", "source": source})

    assert rag.delete_by_source(source, owner="alice", project_id="p") == 0
    assert rag.delete_by_source(source, owner="alice", workspace_id="w", project_id="p") == 1
    assert rag.search("globalsapphire", owner="alice")
    assert rag.search("projecttopaz", owner="alice", workspace_id="w", project_id="p") == []


def test_owner_rename_copies_composite_fk_projection_atomically(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    source = tmp_path / "rename.md"
    assert rag.add_document("rename me safely", {"owner": "before", "source": str(source)})
    result = rag.rename_owner("before", "after")
    assert result["success"] is True
    assert result["updated_count"] == 1
    assert rag.search("safely", owner="before") == []
    assert rag.search("safely", owner="after")


def test_owner_rename_leaves_non_rag_v2_sources_and_evidence_untouched(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document("rename only rag", {"owner": "before", "source": str(tmp_path / "rag.md")})
    _add_non_rag_evidence(rag, "before")

    assert rag.rename_owner("before", "after")["success"] is True

    with sqlite3.connect(rag.db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM fm_v2_sources WHERE owner_id='before' AND source_id='memory_source'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM fm_v2_evidence WHERE owner_id='before' AND evidence_id='memory_evidence'"
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM fm_v2_sources WHERE owner_id='after' AND source_id='memory_source'"
        ).fetchone()
    assert rag.search("rename only rag", owner="after")


def test_owner_purge_leaves_non_rag_v2_sources_and_evidence_untouched(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document("purge only rag", {"owner": "alice", "source": str(tmp_path / "rag.md")})
    _add_non_rag_evidence(rag, "alice")

    result = rag.purge_owner("alice")

    assert result["removed_count"] == 1
    assert rag.search("purge only rag", owner="alice") == []
    with sqlite3.connect(rag.db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM fm_v2_sources WHERE owner_id='alice' AND source_id='memory_source'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM fm_v2_evidence WHERE owner_id='alice' AND evidence_id='memory_evidence'"
        ).fetchone()


def test_owner_rename_preserves_workspace_project_scope(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document("scoped rename", {"owner": "before", "workspace_id": "w", "project_id": "p", "source": str(tmp_path / "scoped.md")})
    assert rag.rename_owner("before", "after")["success"]
    assert rag.search("scoped", owner="after", workspace_id="w", project_id="p")
    assert rag.search("scoped", owner="after", workspace_id="other", project_id="p") == []


def test_owner_rename_rewrites_moved_source_path(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    old_source = tmp_path / "before" / "note.md"
    new_source = tmp_path / "after" / "note.md"
    assert rag.add_document("moved source", {"owner": "before", "source": str(old_source)})

    result = rag.rename_owner(
        "before",
        "after",
        path_map={str(old_source): str(new_source)},
    )

    assert result["success"] is True
    assert rag.search("moved source", owner="after")[0]["metadata"]["source"] == str(new_source)


def test_owner_rename_rekeys_source_so_reindex_advances_same_head(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    source = str(tmp_path / "rename.md")
    scope = {"workspace_id": "w", "project_id": "p", "source": source}
    assert rag.add_document("obsolete cobalt", {"owner": "before", **scope})
    assert rag.rename_owner("before", "after")["success"] is True
    assert rag.add_document("current amber", {"owner": "after", **scope})

    assert rag.search("cobalt", owner="after", workspace_id="w", project_id="p") == []
    assert rag.search("amber", owner="after", workspace_id="w", project_id="p")
    with sqlite3.connect(rag.db_path) as conn:
        rows = conn.execute(
            "SELECT source_id,source_revision FROM fm_v2_sources "
            "WHERE owner_id=? AND workspace_key=? AND project_key=? AND source_uri=? "
            "ORDER BY source_revision",
            ("after", "w", "p", source),
        ).fetchall()
    assert len({row[0] for row in rows}) == 1
    assert [row[1] for row in rows] == [1, 2]


def test_owner_rename_preserves_existing_destination_rows(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document("first", {"owner": "before", "source": str(tmp_path / "a.md")})
    assert rag.add_document("second", {"owner": "after", "source": str(tmp_path / "b.md")})
    result = rag.rename_owner("before", "after")
    assert result["success"] is True
    assert rag.search("first", owner="before") == []
    assert rag.search("first", owner="after")
    assert rag.search("second", owner="after")


def test_upload_compatibility_chunker_is_bounded_and_overlapping(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    chunks = rag._split_into_chunks("abcdefghij", chunk_size=5, overlap=2)
    assert chunks == ["abcde", "defgh", "ghij"]


def test_document_chunk_spans_are_typed_and_heading_table_aware() -> None:
    text = "# Launch\n\nIntro\n\n## Checklist\n\n| item | owner |\n| --- | --- |\n| map | Alice |\n"
    spans = list(_document_chunk_spans(text))
    assert spans
    assert spans[0].line_start == 1
    assert spans[-1].line_end >= spans[0].line_end
    assert any("Launch" in span.headings for span in spans)
    assert any(span.block_type == "table" for span in spans)
    assert all(0 <= span.start < span.end <= len(text) for span in spans)


def test_embedding_generation_is_invisible_until_atomic_publication_and_survives_restart(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "frankenmemory.db")
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        db_path,
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "A feline rests on the windowsill.",
        {"owner": "alice", "source": str(tmp_path / "subject.md")},
    )
    assert rag.search("cat", owner="alice") == []

    built = rag.build_embedding_generation(
        owner="alice",
        provider_ref="owner:alice:fixture",
    )
    assert built["state"] == "ready"
    assert rag.search("cat", owner="alice") == []

    published = rag.publish_generation(
        owner="alice", generation_id=built["generation_id"]
    )
    assert published["currentness"] == "current"
    result = rag.search("cat", owner="alice")[0]
    assert result["document"] == "A feline rests on the windowsill."
    assert result["search_type"] == "frankenmemory_hybrid"
    assert result["score_components"]["vector_cosine"] == 1.0
    assert result["generation_map"]["documents_vector:owner"]["health"] == "current"
    assert result["canonical_read_watermark"]
    assert result["trust_label"] == "owner_document_untrusted_prompt_content"

    restarted = FrankenmemoryRAG(
        db_path,
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert restarted.search("cat", owner="alice")[0]["document"].startswith("A feline")


def test_vector_generation_is_exact_scope_and_cross_owner_safe(tmp_path: Path) -> None:
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        str(tmp_path / "frankenmemory.db"),
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "A feline owner fact.",
        {"owner": "alice", "source": str(tmp_path / "owner.md")},
    )
    assert rag.add_document(
        "A feline project secret.",
        {
            "owner": "alice",
            "workspace_id": "w",
            "project_id": "p",
            "source": str(tmp_path / "project.md"),
        },
    )
    assert rag.add_document(
        "A feline Bob secret.",
        {"owner": "bob", "source": str(tmp_path / "bob.md")},
    )
    generation = rag.build_embedding_generation(
        owner="alice",
        workspace_id="w",
        project_id="p",
        provider_ref="owner:alice:fixture",
        publish=True,
    )
    assert generation["publication"]["currentness"] == "current"

    alice = rag.search("cat", owner="alice", workspace_id="w", project_id="p")
    assert [row["document"] for row in alice] == ["A feline project secret."]
    assert rag.search("cat", owner="bob") == []


def test_more_specific_document_source_shadows_parent_before_ranking(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    source = str(tmp_path / "same.md")
    assert rag.add_document(
        "owner level sharedtoken",
        {"owner": "alice", "source": source},
    )
    assert rag.add_document(
        "project level sharedtoken",
        {
            "owner": "alice",
            "workspace_id": "w",
            "project_id": "p",
            "source": source,
        },
    )
    rows = rag.search(
        "sharedtoken", owner="alice", workspace_id="w", project_id="p", k=10
    )
    assert [row["document"] for row in rows] == ["project level sharedtoken"]
    assert rows[0]["effective_scope"] == "project:w/p"


def test_stale_vector_pointer_never_resurrects_old_source_revision(tmp_path: Path) -> None:
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        str(tmp_path / "frankenmemory.db"),
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    source = str(tmp_path / "changing.md")
    assert rag.add_document("feline obsolete", {"owner": "alice", "source": source})
    generation = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )
    assert generation["state"] == "ready"

    assert rag.add_document("marine current", {"owner": "alice", "source": source})
    assert rag.search("cat", owner="alice") == []
    # The active vector generation predates this revision, so freshness comes
    # from exact/FTS while the old vector is canonically post-filtered.
    current = rag.search("marine", owner="alice")[0]
    assert current["document"] == "marine current"
    assert current["generation_map"]["documents_vector:owner"]["health"] == "lagging"


def test_publication_rejects_behind_generation_and_failed_build_keeps_pointer(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "frankenmemory.db")
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        db_path,
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline stable", {"owner": "alice", "source": str(tmp_path / "one.md")}
    )
    first = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )
    assert first["publication"]["currentness"] == "current"

    staged = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture"
    )
    assert rag.add_document(
        "marine new", {"owner": "alice", "source": str(tmp_path / "two.md")}
    )
    with pytest.raises(RuntimeError, match="behind canonical data"):
        rag.publish_generation(owner="alice", generation_id=staged["generation_id"])

    failed = rag.build_embedding_generation(
        owner="alice",
        embedding_client=_FailingEmbeddingClient(),
        provider_ref="owner:alice:fixture",
    )
    assert failed["state"] == "failed"
    stats = rag.get_stats(owner="alice")
    vector = next(
        row for row in stats["indexes"] if row["logical_space"] == "documents_vector"
    )
    assert vector["generation_id"] == first["generation_id"]
    assert vector["health"] == "lagging"
    assert stats["generation_states"]["failed"] == 1


def test_fts_rebuild_publishes_current_generation_and_reports_health(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document(
        "exact quoted phrase", {"owner": "alice", "source": str(tmp_path / "one.md")}
    )
    assert rag.rebuild_index() is True
    result = rag.search('"exact quoted phrase"', owner="alice")[0]
    assert result["score_components"]["exact_phrase"] is True
    assert result["generation_map"]["documents_fts:owner"]["health"] == "current"
    stats = rag.get_stats(owner="alice")
    assert stats["fts_available"] is True
    assert stats["index_health"]["current"] >= 1


def test_fts_runtime_loss_is_explicit_while_exact_search_still_answers(
    tmp_path: Path,
) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document(
        "exact survives fts loss",
        {"owner": "alice", "source": str(tmp_path / "one.md")},
    )
    rag._fts_available = False

    result = rag.search("exact survives", owner="alice")[0]

    assert result["document"] == "exact survives fts loss"
    assert "fts" not in result["score_components"]["ranks"]
    assert result["degraded"] is True
    assert "documents_fts:owner:runtime_unavailable" in result["degradation_reasons"]


def test_vector_client_configuration_mismatch_degrades_without_losing_exact(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "frankenmemory.db")
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        db_path,
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline exact fallback",
        {"owner": "alice", "source": str(tmp_path / "subject.md")},
    )
    generation = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )
    assert generation["publication"]["currentness"] == "current"

    mismatched = FrankenmemoryRAG(
        db_path,
        embedding_client=client,
        embedding_provider_ref="owner:alice:different",
    )
    result = mismatched.search("feline", owner="alice")[0]

    assert result["document"] == "feline exact fallback"
    assert "vector" not in result["score_components"]["ranks"]
    assert (
        "documents_vector:owner:client_unavailable_or_mismatch"
        in result["degradation_reasons"]
    )


def test_generation_publication_history_and_atomic_rollback(tmp_path: Path) -> None:
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        str(tmp_path / "frankenmemory.db"),
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline rollback target",
        {"owner": "alice", "source": str(tmp_path / "subject.md")},
    )
    first = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )
    second = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture"
    )
    published = rag.publish_generation(
        owner="alice",
        generation_id=second["generation_id"],
        expected_active_generation=first["generation_id"],
    )
    rolled_back = rag.rollback_generation(
        owner="alice",
        generation_id=first["generation_id"],
        expected_active_generation=second["generation_id"],
    )
    assert published["previous_generation_id"] == first["generation_id"]
    assert rolled_back["action"] == "rollback"
    assert rolled_back["currentness"] == "current"

    with sqlite3.connect(rag.db_path) as conn:
        pointer = conn.execute(
            "SELECT generation_id,publication_tx FROM fm_v2_index_pointers "
            "WHERE owner_id='alice' AND logical_space='documents_vector'"
        ).fetchone()
        history = conn.execute(
            "SELECT action,generation_id FROM fm_v2_index_publications "
            "WHERE owner_id='alice' AND logical_space='documents_vector' "
            "ORDER BY created_at,publication_id"
        ).fetchall()
    assert pointer == (first["generation_id"], rolled_back["publication_id"])
    assert [row[0] for row in history] == ["publish", "publish", "rollback"]

    with sqlite3.connect(rag.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="state transition"):
            conn.execute(
                "UPDATE fm_v2_derived_generations SET state='building' "
                "WHERE owner_id='alice' AND generation_id=?",
                (first["generation_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="remain retained"):
            conn.execute(
                "UPDATE fm_v2_derived_generations SET retention_state='gc_eligible' "
                "WHERE owner_id='alice' AND generation_id=?",
                (first["generation_id"],),
            )


def test_superseded_generations_marked_gc_eligible_beyond_rollback_window(
    tmp_path: Path,
) -> None:
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        str(tmp_path / "frankenmemory.db"),
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline retention window",
        {"owner": "alice", "source": str(tmp_path / "subject.md")},
    )
    first = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )
    second = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )
    third = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )

    with sqlite3.connect(rag.db_path) as conn:
        rows = dict(
            conn.execute(
                "SELECT generation_id,retention_state FROM fm_v2_derived_generations "
                "WHERE owner_id='alice' AND logical_space='documents_vector'"
            ).fetchall()
        )
    assert rows[first["generation_id"]] == "gc_eligible"
    # The newest superseded generation stays retained as the rollback window.
    assert rows[second["generation_id"]] == "retained"
    assert rows[third["generation_id"]] == "retained"

    rolled_back = rag.rollback_generation(
        owner="alice",
        generation_id=second["generation_id"],
        expected_active_generation=third["generation_id"],
    )
    assert rolled_back["action"] == "rollback"
    # A gc-eligible generation is outside the rollback window.
    with pytest.raises(ValueError, match="retained ready"):
        rag.rollback_generation(
            owner="alice",
            generation_id=first["generation_id"],
            expected_active_generation=second["generation_id"],
        )


def test_failed_publication_transaction_keeps_previous_pointer(tmp_path: Path) -> None:
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        str(tmp_path / "frankenmemory.db"),
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline atomic pointer",
        {"owner": "alice", "source": str(tmp_path / "subject.md")},
    )
    first = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )
    second = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture"
    )
    with sqlite3.connect(rag.db_path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_vector_publication BEFORE INSERT ON "
            "fm_v2_index_publications WHEN new.generation_id='"
            + second["generation_id"]
            + "' BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="publication failure"):
        rag.publish_generation(
            owner="alice", generation_id=second["generation_id"]
        )
    with sqlite3.connect(rag.db_path) as conn:
        active = conn.execute(
            "SELECT generation_id FROM fm_v2_index_pointers "
            "WHERE owner_id='alice' AND logical_space='documents_vector'"
        ).fetchone()[0]
    assert active == first["generation_id"]


def test_interrupted_generation_resumes_without_duplicate_rows(tmp_path: Path) -> None:
    db_path = str(tmp_path / "frankenmemory.db")
    interrupted = _InterruptingEmbeddingClient()
    rag = FrankenmemoryRAG(
        db_path,
        embedding_client=interrupted,
        embedding_provider_ref="owner:alice:fixture",
    )
    for index in range(2):
        assert rag.add_document(
            f"feline part {index}",
            {"owner": "alice", "source": str(tmp_path / f"part-{index}.md")},
        )
    with pytest.raises(KeyboardInterrupt, match="process death"):
        rag.build_embedding_generation(
            owner="alice",
            provider_ref="owner:alice:fixture",
            batch_size=1,
        )
    with sqlite3.connect(db_path) as conn:
        generation_id, state = conn.execute(
            "SELECT generation_id,state FROM fm_v2_derived_generations "
            "WHERE logical_space='documents_vector' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert state == "building"
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_chunk_embeddings WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0] == 1

    resumed = rag.resume_embedding_generation(
        owner="alice",
        generation_id=generation_id,
        embedding_client=_FakeEmbeddingClient(),
        batch_size=1,
    )
    assert resumed == {"generation_id": generation_id, "state": "ready", "embedded": 2}
    assert rag.publish_generation(owner="alice", generation_id=generation_id)[
        "currentness"
    ] == "current"


def test_concurrent_write_fails_staging_without_repointing(tmp_path: Path) -> None:
    db_path = str(tmp_path / "frankenmemory.db")
    stable = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        db_path,
        embedding_client=stable,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline stable",
        {"owner": "alice", "source": str(tmp_path / "stable.md")},
    )
    active = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )["generation_id"]
    writer = _ConcurrentWriterEmbeddingClient(
        lambda: rag.add_document(
            "marine concurrent",
            {"owner": "alice", "source": str(tmp_path / "concurrent.md")},
        )
    )
    failed = rag.build_embedding_generation(
        owner="alice",
        embedding_client=writer,
        provider_ref="owner:alice:fixture",
        publish=True,
    )
    assert failed["state"] == "failed"
    assert failed["reason"] == "source_changed_during_build"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT generation_id FROM fm_v2_index_pointers "
            "WHERE owner_id='alice' AND logical_space='documents_vector'"
        ).fetchone()[0] == active


def test_corrupt_vector_is_invalid_but_exact_and_fts_still_answer(tmp_path: Path) -> None:
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        str(tmp_path / "frankenmemory.db"),
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline integrity",
        {"owner": "alice", "source": str(tmp_path / "subject.md")},
    )
    generation = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture", publish=True
    )["generation_id"]
    with sqlite3.connect(rag.db_path) as conn:
        conn.execute(
            "UPDATE fm_v2_chunk_embeddings SET embedding=x'00' "
            "WHERE owner_id='alice' AND generation_id=?",
            (generation,),
        )
    result = rag.search("feline", owner="alice")[0]
    assert result["document"] == "feline integrity"
    assert "vector" not in result["score_components"]["ranks"]
    assert result["generation_map"]["documents_vector:owner"]["health"] == "invalid"
    assert "documents_vector:owner:invalid" in result["degradation_reasons"]
    assert rag.get_stats(owner="alice")["index_health"]["invalid"] == 1


def test_vector_publication_rejects_same_owner_chunk_from_another_scope(
    tmp_path: Path,
) -> None:
    client = _FakeEmbeddingClient()
    rag = FrankenmemoryRAG(
        str(tmp_path / "frankenmemory.db"),
        embedding_client=client,
        embedding_provider_ref="owner:alice:fixture",
    )
    assert rag.add_document(
        "feline owner document",
        {"owner": "alice", "source": str(tmp_path / "owner.md")},
    )
    assert rag.add_document(
        "feline workspace document",
        {
            "owner": "alice",
            "workspace_id": "private",
            "source": str(tmp_path / "workspace.md"),
        },
    )
    generation = rag.build_embedding_generation(
        owner="alice", provider_ref="owner:alice:fixture"
    )["generation_id"]

    with sqlite3.connect(rag.db_path) as conn:
        dimension, embedding = conn.execute(
            "SELECT dimension,embedding FROM fm_v2_chunk_embeddings "
            "WHERE owner_id='alice' AND generation_id=?",
            (generation,),
        ).fetchone()
        workspace_chunk, workspace_hash = conn.execute(
            "SELECT c.chunk_id,c.content_hash FROM fm_v2_chunks c "
            "JOIN fm_v2_documents d ON d.owner_id=c.owner_id "
            "AND d.document_id=c.document_id "
            "JOIN fm_v2_sources s ON s.owner_id=d.owner_id "
            "AND s.source_id=d.source_id AND s.source_revision=d.source_revision "
            "WHERE c.owner_id='alice' AND s.workspace_key='private'"
        ).fetchone()
        conn.execute(
            "DELETE FROM fm_v2_chunk_embeddings "
            "WHERE owner_id='alice' AND generation_id=?",
            (generation,),
        )
        conn.execute(
            "INSERT INTO fm_v2_chunk_embeddings("
            "owner_id,generation_id,chunk_id,dimension,embedding,content_hash,created_at) "
            "VALUES ('alice',?,?,?,?,?,?)",
            (generation, workspace_chunk, dimension, embedding, workspace_hash, "now"),
        )

    with pytest.raises(ValueError, match="scope manifest"):
        rag.publish_generation(owner="alice", generation_id=generation)


def test_golden_quoted_unicode_and_empty_explanation(tmp_path: Path) -> None:
    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    assert rag.add_document(
        "Café 東京 exact launch phrase",
        {
            "owner": "alice",
            "title": "Foreign guide",
            "source": str(tmp_path / "foreign.md"),
        },
    )
    result = rag.search('"Café 東京 exact"', owner="alice")[0]
    assert result["metadata"]["title"] == "Foreign guide"
    assert result["score_components"]["exact_phrase"] is True
    assert result["matched_locator"]["type"] == "text"
    assert result["retrieval_explanation"]["ranker_versions"]["fusion"] == "scope-first-rrf-v1"

    empty = rag.search_explain("not-present", owner="alice")
    assert empty["results"] == []
    assert empty["error"] is None
    assert empty["degraded"] is True
    assert empty["generation_map"]["documents_fts:owner"]["health"] == "current"
    assert empty["generation_map"]["documents_vector:owner"]["health"] == "missing"


@pytest.mark.asyncio
async def test_docs_consumer_preserves_canonical_text_score_and_source(
    tmp_path: Path, monkeypatch
) -> None:
    from services.docs import service as docs_service

    rag = FrankenmemoryRAG(str(tmp_path / "frankenmemory.db"))
    source = str(tmp_path / "consumer.md")
    assert rag.add_document("consumer exact", {"owner": "alice", "source": source})
    monkeypatch.setattr(docs_service, "get_rag_manager", lambda: rag)
    docs = docs_service.DocsService()
    result = (await docs.query("consumer", owner="alice"))[0]
    assert result.text == "consumer exact"
    assert result.score > 0
    assert result.source == source
