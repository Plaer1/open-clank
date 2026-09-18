"""Canonical, local-first document RAG projection for Frankenmemory.

Documents and chunks are stored in the v2 Frankenmemory SQLite authority.
Search is exact/token based until an explicit derived embedding generation is
published; no Chroma service is required for ingestion or retrieval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from src.constants import FM_DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_FILE_EXTENSIONS: Set[str] = {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".csv", ".html", ".css", ".js", ".log"}
MAX_FILE_BYTES = 50 * 1024 * 1024
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 120
CHUNKER_VERSION = "frankenmemory-rag-v2"
FTS_LOGICAL_SPACE = "documents_fts"
VECTOR_LOGICAL_SPACE = "documents_vector"
RRF_K = 60
RRF_WEIGHTS = {"exact": 2.0, "fts": 1.0, "vector": 1.0}
RANKER_VERSION = "scope-first-rrf-v1"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _owner(value: Optional[str]) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("owner is required for canonical RAG")
    return value


def _source_id(owner: str, workspace: str, project: str, uri: str) -> str:
    return "src_" + _hash(f"{owner}\0{workspace}\0{project}\0{uri}")[:32]


def _rewrite_source_uri(
    value: str,
    path_map: Dict[str, str],
    path_prefixes: List[tuple[str, str]],
) -> str:
    if not value:
        return value
    absolute = os.path.abspath(value)
    if absolute in path_map:
        return path_map[absolute]
    for old_prefix, new_prefix in path_prefixes:
        old_absolute = os.path.abspath(old_prefix)
        if absolute == old_absolute or absolute.startswith(old_absolute + os.sep):
            return os.path.abspath(new_prefix) + absolute[len(old_absolute):]
    return value


def _chunks(text: str) -> Iterable[tuple[int, str]]:
    if not text:
        return
    start = 0
    ordinal = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        piece = text[start:end]
        if piece.strip():
            yield ordinal, piece
            ordinal += 1
        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)


@dataclass(frozen=True)
class ChunkSpan:
    """Deterministic chunk text plus source-aware document locator fields."""

    ordinal: int
    text: str
    start: int
    end: int
    line_start: int
    line_end: int
    headings: tuple[str, ...] = ()
    block_type: str = "text"


def _document_chunk_spans(text: str) -> Iterable[ChunkSpan]:
    """Yield the v2 deterministic chunks with heading/table provenance.

    Character windows remain the stable sizing primitive for compatibility,
    while the locator records the nearest Markdown heading breadcrumb and
    whether a window contains table rows. This is a clean-room typed IR seam:
    richer parsers can supply the same fields later without changing retrieval
    or introducing a second document authority.
    """
    if not text:
        return
    lines = text.splitlines(keepends=True)
    line_ranges: list[tuple[int, int, str]] = []
    cursor = 0
    headings: list[tuple[int, str]] = []
    headings_by_line: list[tuple[str, ...]] = []
    for index, line in enumerate(lines, start=1):
        end = cursor + len(line)
        line_ranges.append((cursor, end, line))
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line.rstrip("\r\n"))
        if match:
            level = len(match.group(1))
            headings = [item for item in headings if item[0] < level]
            headings.append((level, match.group(2).strip()))
        headings_by_line.append(tuple(item[1] for item in headings))
        cursor = end

    def line_for(offset: int) -> int:
        for number, (start, end, _line) in enumerate(line_ranges, start=1):
            if offset < end or (offset == end and end == len(text)):
                return number
        return max(1, len(line_ranges))

    start = 0
    ordinal = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        piece = text[start:end]
        if not piece.strip():
            if end >= len(text):
                break
            start = max(start + 1, end - CHUNK_OVERLAP)
            continue
        chunk_end = min(len(text), start + len(piece))
        line_start = line_for(start)
        line_end = line_for(max(start, chunk_end - 1))
        breadcrumb: list[str] = list(headings_by_line[line_start - 1]) if headings_by_line else []
        has_table = False
        for number, (line_offset, line_end_offset, line) in enumerate(line_ranges, start=1):
            if number > line_end:
                break
            if line_end_offset <= start or line_offset >= chunk_end:
                continue
            if "|" in line and line.strip().startswith("|"):
                has_table = True
        yield ChunkSpan(
            ordinal=ordinal,
            text=piece,
            start=start,
            end=chunk_end,
            line_start=line_start,
            line_end=line_end,
            headings=tuple(dict.fromkeys(breadcrumb)),
            block_type="table" if has_table else "text",
        )
        ordinal += 1
        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)


def _read_stable_text(path: Path) -> str:
    """Read one bounded regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb", closefd=True) as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError("source is not a bounded regular file")
        payload = source.read(MAX_FILE_BYTES + 1)
        after = os.fstat(source.fileno())
    path_after = os.stat(path, follow_symlinks=False)
    before_fingerprint = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_fingerprint = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    path_fingerprint = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_size,
        path_after.st_mtime_ns,
    )
    if (
        len(payload) > MAX_FILE_BYTES
        or before_fingerprint != after_fingerprint
        or before_fingerprint != path_fingerprint
    ):
        raise ValueError("source changed while it was being indexed")
    return payload.decode("utf-8", errors="replace")


class FrankenmemoryRAG:
    """Tenant-scoped document/chunk projection with deterministic retrieval."""

    backend = "frankenmemory"

    def __init__(
        self,
        db_path: str = FM_DB_PATH,
        *,
        embedding_client: Any = None,
        embedding_provider_ref: Optional[str] = None,
    ):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        self._embedding_client = embedding_client
        self._embedding_provider_ref = str(embedding_provider_ref or "").strip()
        self._healthy = False
        self._fts_available = False
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_tables(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        try:
            with self._connect() as conn:
                # These definitions match fm-core's migration 11. The Rust
                # migration remains the authority; this additive bootstrap is
                # only for the app's early RAG singleton startup race.
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS fm_v2_sources (
                        owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
                        workspace_key TEXT NOT NULL DEFAULT '', project_key TEXT NOT NULL DEFAULT '',
                        source_uri TEXT NOT NULL, source_revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL, source_type TEXT NOT NULL,
                        forget_state TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, source_id, source_revision)
                        ,CHECK (source_revision > 0)
                        ,CHECK (project_key = '' OR workspace_key <> '')
                        ,CHECK (forget_state IN ('active','forget_requested','forgotten'))
                    );
                    CREATE TABLE IF NOT EXISTS fm_v2_documents (
                        owner_id TEXT NOT NULL, document_id TEXT NOT NULL,
                        source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
                        title TEXT NOT NULL DEFAULT '', parser_version TEXT NOT NULL,
                        content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, document_id),
                        FOREIGN KEY(owner_id, source_id, source_revision)
                          REFERENCES fm_v2_sources(owner_id, source_id, source_revision)
                    );
                    CREATE TABLE IF NOT EXISTS fm_v2_chunks (
                        owner_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
                        document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                        text TEXT NOT NULL, content_hash TEXT NOT NULL,
                        locator_json TEXT NOT NULL, created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, chunk_id),
                        UNIQUE(owner_id, document_id, ordinal),
                        FOREIGN KEY(owner_id, document_id)
                          REFERENCES fm_v2_documents(owner_id, document_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_fm_v2_rag_chunks_owner
                      ON fm_v2_chunks(owner_id, document_id, ordinal);
                    CREATE TABLE IF NOT EXISTS fm_v2_derived_generations (
                        owner_id TEXT NOT NULL,
                        generation_id TEXT NOT NULL,
                        logical_space TEXT NOT NULL,
                        workspace_key TEXT NOT NULL DEFAULT '',
                        project_key TEXT NOT NULL DEFAULT '',
                        provider_ref TEXT NOT NULL,
                        model TEXT NOT NULL,
                        endpoint_class TEXT NOT NULL,
                        dimension INTEGER NOT NULL,
                        normalization TEXT NOT NULL,
                        metric TEXT NOT NULL,
                        chunker_version TEXT NOT NULL,
                        config_fingerprint TEXT NOT NULL,
                        source_watermark TEXT NOT NULL,
                        row_count INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL,
                        retention_state TEXT NOT NULL DEFAULT 'retained',
                        failure_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        validated_at TEXT,
                        PRIMARY KEY(owner_id, generation_id),
                        CHECK (project_key = '' OR workspace_key <> ''),
                        CHECK (dimension >= 0),
                        CHECK (state IN ('planned','building','validating','ready','failed')),
                        CHECK (retention_state IN ('retained','gc_eligible','deleted'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_fm_v2_derived_generations_space
                      ON fm_v2_derived_generations(
                        owner_id, logical_space, workspace_key, project_key,
                        created_at
                      );
                    CREATE TABLE IF NOT EXISTS fm_v2_index_pointers (
                        owner_id TEXT NOT NULL,
                        logical_space TEXT NOT NULL,
                        workspace_key TEXT NOT NULL DEFAULT '',
                        project_key TEXT NOT NULL DEFAULT '',
                        generation_id TEXT NOT NULL,
                        publication_tx TEXT NOT NULL DEFAULT '',
                        publication_watermark TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, logical_space, workspace_key, project_key),
                        FOREIGN KEY(owner_id, generation_id)
                          REFERENCES fm_v2_derived_generations(owner_id, generation_id),
                        CHECK (project_key = '' OR workspace_key <> '')
                    );
                    CREATE TABLE IF NOT EXISTS fm_v2_chunk_embeddings (
                        owner_id TEXT NOT NULL,
                        generation_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        dimension INTEGER NOT NULL,
                        embedding BLOB NOT NULL,
                        content_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, generation_id, chunk_id),
                        FOREIGN KEY(owner_id, generation_id)
                          REFERENCES fm_v2_derived_generations(owner_id, generation_id),
                        FOREIGN KEY(owner_id, chunk_id)
                          REFERENCES fm_v2_chunks(owner_id, chunk_id)
                          ON DELETE CASCADE,
                        CHECK (dimension > 0)
                    );
                    CREATE TABLE IF NOT EXISTS fm_v2_index_publications (
                        owner_id TEXT NOT NULL,
                        publication_id TEXT NOT NULL,
                        logical_space TEXT NOT NULL,
                        workspace_key TEXT NOT NULL DEFAULT '',
                        project_key TEXT NOT NULL DEFAULT '',
                        previous_generation_id TEXT,
                        generation_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        publication_watermark TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, publication_id),
                        CHECK (project_key = '' OR workspace_key <> ''),
                        CHECK (action IN ('publish','rollback'))
                    );
                    CREATE TRIGGER IF NOT EXISTS fm_v2_generation_state_immutable
                    BEFORE UPDATE OF state ON fm_v2_derived_generations
                    WHEN NOT (
                        old.state = new.state OR
                        (old.state = 'planned' AND new.state = 'building') OR
                        (old.state = 'building' AND new.state IN ('validating','failed')) OR
                        (old.state = 'validating' AND new.state IN ('ready','failed'))
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid derived generation state transition');
                    END;
                    CREATE TRIGGER IF NOT EXISTS fm_v2_active_generation_retained
                    BEFORE UPDATE OF retention_state ON fm_v2_derived_generations
                    WHEN new.retention_state <> 'retained' AND EXISTS (
                        SELECT 1 FROM fm_v2_index_pointers p
                        WHERE p.owner_id = old.owner_id
                          AND p.generation_id = old.generation_id
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'active generation must remain retained');
                    END;
                    """
                )
                try:
                    conn.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS fm_v2_chunks_fts USING fts5(owner_id UNINDEXED, chunk_id UNINDEXED, text)"
                    )
                    conn.execute(
                        "INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) SELECT c.owner_id,c.chunk_id,c.text FROM fm_v2_chunks c WHERE NOT EXISTS (SELECT 1 FROM fm_v2_chunks_fts f WHERE f.owner_id=c.owner_id AND f.chunk_id=c.chunk_id)"
                    )
                    self._fts_available = True
                except sqlite3.Error:
                    self._fts_available = False
                columns = {row[1] for row in conn.execute("PRAGMA table_info(fm_v2_sources)")}
                for column in ("workspace_key", "project_key"):
                    if column not in columns:
                        conn.execute(f"ALTER TABLE fm_v2_sources ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                pointer_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(fm_v2_index_pointers)")
                }
                if "publication_tx" not in pointer_columns:
                    conn.execute(
                        "ALTER TABLE fm_v2_index_pointers "
                        "ADD COLUMN publication_tx TEXT NOT NULL DEFAULT ''"
                    )
                generation_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(fm_v2_derived_generations)"
                    )
                }
                if "row_count" not in generation_columns:
                    conn.execute(
                        "ALTER TABLE fm_v2_derived_generations "
                        "ADD COLUMN row_count INTEGER NOT NULL DEFAULT 0"
                    )
                # Older pointers predate publication transaction IDs. Backfill
                # an auditable publication record without changing the target.
                for pointer in conn.execute(
                    "SELECT owner_id,logical_space,workspace_key,project_key,"
                    "generation_id,publication_watermark,published_at "
                    "FROM fm_v2_index_pointers WHERE publication_tx=''"
                ).fetchall():
                    publication_id = "pub_" + uuid.uuid4().hex
                    conn.execute(
                        "UPDATE fm_v2_index_pointers SET publication_tx=? "
                        "WHERE owner_id=? AND logical_space=? AND workspace_key=? "
                        "AND project_key=? AND publication_tx=''",
                        (
                            publication_id,
                            pointer["owner_id"],
                            pointer["logical_space"],
                            pointer["workspace_key"],
                            pointer["project_key"],
                        ),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO fm_v2_index_publications("
                        "owner_id,publication_id,logical_space,workspace_key,"
                        "project_key,previous_generation_id,generation_id,action,"
                        "publication_watermark,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            pointer["owner_id"],
                            publication_id,
                            pointer["logical_space"],
                            pointer["workspace_key"],
                            pointer["project_key"],
                            None,
                            pointer["generation_id"],
                            "publish",
                            pointer["publication_watermark"],
                            pointer["published_at"],
                        ),
                    )
                if self._fts_available:
                    scopes = conn.execute(
                        "SELECT DISTINCT owner_id,workspace_key,project_key "
                        "FROM fm_v2_sources"
                    ).fetchall()
                    for owner, workspace, project in scopes:
                        self._publish_inline_fts(conn, owner, workspace, project)
            self._healthy = True
        except Exception:
            logger.exception("canonical Frankenmemory RAG initialization failed")
            self._healthy = False

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def collection(self):
        return None

    @staticmethod
    def _effective_scopes(
        workspace_id: Optional[str], project_id: Optional[str]
    ) -> List[tuple[str, str, int]]:
        workspace = str(workspace_id or "").strip()
        project = str(project_id or "").strip()
        if project and not workspace:
            raise ValueError("project scope requires workspace scope")
        scopes = [("", "", 0)]
        if workspace:
            scopes.append((workspace, "", 1))
        if project:
            scopes.append((workspace, project, 2))
        return scopes

    @staticmethod
    def _scope_label(workspace: str, project: str) -> str:
        if project:
            return f"project:{workspace}/{project}"
        if workspace:
            return f"workspace:{workspace}"
        return "owner"

    def _canonical_rows(
        self,
        conn: sqlite3.Connection,
        owner: str,
        workspace: str,
        project: str,
    ) -> List[sqlite3.Row]:
        return conn.execute(
            "SELECT c.chunk_id,c.text,c.content_hash AS chunk_hash,c.ordinal,"
            "c.locator_json,"
            "d.document_id,d.title,s.source_id,s.source_revision,s.source_uri,"
            "s.workspace_key,s.project_key "
            "FROM fm_v2_chunks c "
            "JOIN fm_v2_documents d ON d.owner_id=c.owner_id "
            "AND d.document_id=c.document_id "
            "JOIN fm_v2_sources s ON s.owner_id=d.owner_id "
            "AND s.source_id=d.source_id AND s.source_revision=d.source_revision "
            "WHERE c.owner_id=? AND s.workspace_key=? AND s.project_key=? "
            "AND d.parser_version=? "
            "AND s.forget_state='active' AND NOT EXISTS ("
            "SELECT 1 FROM fm_v2_sources newer "
            "WHERE newer.owner_id=s.owner_id AND newer.source_id=s.source_id "
            "AND newer.source_revision>s.source_revision) "
            "ORDER BY s.source_uri,c.ordinal,c.chunk_id",
            (owner, workspace, project, CHUNKER_VERSION),
        ).fetchall()

    def _source_watermark(
        self,
        conn: sqlite3.Connection,
        owner: str,
        workspace: str,
        project: str,
    ) -> str:
        rows = conn.execute(
            "SELECT s.source_id,s.source_revision,s.source_uri,s.content_hash,"
            "s.forget_state,COALESCE(d.document_id,''),COALESCE(c.chunk_id,''),"
            "COALESCE(c.content_hash,'') "
            "FROM fm_v2_sources s "
            "LEFT JOIN fm_v2_documents d ON d.owner_id=s.owner_id "
            "AND d.source_id=s.source_id AND d.source_revision=s.source_revision "
            "AND d.parser_version=? "
            "LEFT JOIN fm_v2_chunks c ON c.owner_id=d.owner_id "
            "AND c.document_id=d.document_id "
            "WHERE s.owner_id=? AND s.workspace_key=? AND s.project_key=? "
            "AND NOT EXISTS (SELECT 1 FROM fm_v2_sources newer "
            "WHERE newer.owner_id=s.owner_id AND newer.source_id=s.source_id "
            "AND newer.source_revision>s.source_revision) "
            "ORDER BY s.source_id,s.source_revision,d.document_id,c.ordinal,c.chunk_id",
            (CHUNKER_VERSION, owner, workspace, project),
        ).fetchall()
        return _hash(json.dumps([tuple(row) for row in rows], separators=(",", ":")))

    def _publish_inline_fts(
        self,
        conn: sqlite3.Connection,
        owner: str,
        workspace: str,
        project: str,
    ) -> str:
        """Publish the transactionally-maintained FTS projection for one scope."""
        watermark = self._source_watermark(conn, owner, workspace, project)
        config = {
            "provider_ref": "sqlite-fts5",
            "model": "fts5",
            "endpoint_class": "in_process",
            "dimension": 0,
            "normalization": "bm25",
            "metric": "bm25",
            "chunker_version": CHUNKER_VERSION,
        }
        fingerprint = _hash(json.dumps(config, sort_keys=True, separators=(",", ":")))
        generation_id = "gen_fts_" + _hash(
            f"{owner}\0{workspace}\0{project}\0{watermark}\0{fingerprint}"
        )[:32]
        now = datetime.now(timezone.utc).isoformat()
        row_count = len(self._canonical_rows(conn, owner, workspace, project))
        inserted = conn.execute(
            "INSERT OR IGNORE INTO fm_v2_derived_generations("
            "owner_id,generation_id,logical_space,workspace_key,project_key,"
            "provider_ref,model,endpoint_class,dimension,normalization,metric,"
            "chunker_version,config_fingerprint,source_watermark,row_count,state,"
            "retention_state,created_at,updated_at,validated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                owner,
                generation_id,
                FTS_LOGICAL_SPACE,
                workspace,
                project,
                config["provider_ref"],
                config["model"],
                config["endpoint_class"],
                config["dimension"],
                config["normalization"],
                config["metric"],
                config["chunker_version"],
                fingerprint,
                watermark,
                row_count,
                "planned",
                "retained",
                now,
                now,
                None,
            ),
        )
        if inserted.rowcount:
            conn.execute(
                "UPDATE fm_v2_derived_generations SET state='building' "
                "WHERE owner_id=? AND generation_id=? AND state='planned'",
                (owner, generation_id),
            )
            conn.execute(
                "UPDATE fm_v2_derived_generations SET state='validating' "
                "WHERE owner_id=? AND generation_id=? AND state='building'",
                (owner, generation_id),
            )
            conn.execute(
                "UPDATE fm_v2_derived_generations SET state='ready',"
                "validated_at=? WHERE owner_id=? AND generation_id=? "
                "AND state='validating'",
                (now, owner, generation_id),
            )
        generation = conn.execute(
            "SELECT * FROM fm_v2_derived_generations "
            "WHERE owner_id=? AND generation_id=?",
            (owner, generation_id),
        ).fetchone()
        if (
            generation is None
            or generation["state"] != "ready"
            or int(generation["row_count"]) != row_count
            or generation["config_fingerprint"] != fingerprint
            or generation["source_watermark"] != watermark
        ):
            raise RuntimeError("FTS generation manifest is invalid")
        current = conn.execute(
            "SELECT generation_id,publication_watermark,publication_tx "
            "FROM fm_v2_index_pointers WHERE owner_id=? AND logical_space=? "
            "AND workspace_key=? AND project_key=?",
            (owner, FTS_LOGICAL_SPACE, workspace, project),
        ).fetchone()
        if not current or current[0] != generation_id or current[1] != watermark or not current[2]:
            self._swap_pointer(conn, generation, action="publish")
        return generation_id

    def _swap_pointer(
        self,
        conn: sqlite3.Connection,
        generation: sqlite3.Row,
        *,
        action: str,
        expected_active_generation: Optional[str] = None,
        allow_lagging: bool = False,
    ) -> Dict[str, Any]:
        """Swap a generation pointer and append history in one transaction."""
        if action not in {"publish", "rollback"}:
            raise ValueError("unsupported publication action")
        owner = generation["owner_id"]
        current = conn.execute(
            "SELECT generation_id FROM fm_v2_index_pointers "
            "WHERE owner_id=? AND logical_space=? AND workspace_key=? "
            "AND project_key=?",
            (
                owner,
                generation["logical_space"],
                generation["workspace_key"],
                generation["project_key"],
            ),
        ).fetchone()
        current_id = current[0] if current else None
        if expected_active_generation is not None and current_id != expected_active_generation:
            raise RuntimeError("active generation changed before publication")
        watermark = self._source_watermark(
            conn,
            owner,
            generation["workspace_key"],
            generation["project_key"],
        )
        if watermark != generation["source_watermark"] and not allow_lagging:
            raise RuntimeError("generation source watermark is behind canonical data")
        publication_id = "pub_" + uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO fm_v2_index_pointers("
            "owner_id,logical_space,workspace_key,project_key,generation_id,"
            "publication_tx,publication_watermark,published_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner_id,logical_space,workspace_key,project_key) "
            "DO UPDATE SET generation_id=excluded.generation_id,"
            "publication_tx=excluded.publication_tx,"
            "publication_watermark=excluded.publication_watermark,"
            "published_at=excluded.published_at",
            (
                owner,
                generation["logical_space"],
                generation["workspace_key"],
                generation["project_key"],
                generation["generation_id"],
                publication_id,
                generation["source_watermark"],
                now,
            ),
        )
        conn.execute(
            "INSERT INTO fm_v2_index_publications("
            "owner_id,publication_id,logical_space,workspace_key,project_key,"
            "previous_generation_id,generation_id,action,publication_watermark,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                owner,
                publication_id,
                generation["logical_space"],
                generation["workspace_key"],
                generation["project_key"],
                current_id,
                generation["generation_id"],
                action,
                generation["source_watermark"],
                now,
            ),
        )
        # A superseded generation stays rollback-ready: keep the newest
        # retained ready one and mark the rest gc-eligible so derived
        # generations do not accumulate without bound.  The newly published
        # generation is restored to retained first (it may have been
        # gc-eligible from an earlier supersession).
        conn.execute(
            "UPDATE fm_v2_derived_generations SET retention_state='retained' "
            "WHERE owner_id=? AND generation_id=? AND retention_state<>'retained'",
            (owner, generation["generation_id"]),
        )
        conn.execute(
            "UPDATE fm_v2_derived_generations SET retention_state='gc_eligible',"
            "updated_at=? "
            "WHERE owner_id=? AND logical_space=? AND workspace_key=? "
            "AND project_key=? AND retention_state='retained' "
            "AND generation_id<>? AND generation_id NOT IN ("
            "SELECT generation_id FROM fm_v2_derived_generations "
            "WHERE owner_id=? AND logical_space=? AND workspace_key=? "
            "AND project_key=? AND retention_state='retained' AND state='ready' "
            "AND generation_id<>? "
            "ORDER BY created_at DESC,generation_id DESC LIMIT 1)",
            (
                now,
                owner,
                generation["logical_space"],
                generation["workspace_key"],
                generation["project_key"],
                generation["generation_id"],
                owner,
                generation["logical_space"],
                generation["workspace_key"],
                generation["project_key"],
                generation["generation_id"],
            ),
        )
        return {
            "published": True,
            "publication_id": publication_id,
            "generation_id": generation["generation_id"],
            "previous_generation_id": current_id,
            "action": action,
            "currentness": (
                "current" if watermark == generation["source_watermark"] else "lagging"
            ),
        }

    @staticmethod
    def _mutation_scope(
        workspace_id: Optional[str], project_id: Optional[str]
    ) -> tuple[str, str]:
        workspace = str(workspace_id or "").strip()
        project = str(project_id or "").strip()
        if project and not workspace:
            raise ValueError("project scope requires workspace scope")
        return workspace, project

    def _delete_documents(
        self, conn: sqlite3.Connection, owner: str, document_ids: List[str]
    ) -> None:
        for document_id in document_ids:
            chunk_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM fm_v2_chunks WHERE owner_id=? AND document_id=?",
                    (owner, document_id),
                )
            ]
            if self._fts_available and chunk_ids:
                conn.executemany(
                    "DELETE FROM fm_v2_chunks_fts WHERE owner_id=? AND chunk_id=?",
                    [(owner, chunk_id) for chunk_id in chunk_ids],
                )
            conn.execute(
                "DELETE FROM fm_v2_chunks WHERE owner_id=? AND document_id=?",
                (owner, document_id),
            )
            conn.execute(
                "DELETE FROM fm_v2_documents WHERE owner_id=? AND document_id=?",
                (owner, document_id),
            )

    @staticmethod
    def _delete_unreferenced_sources(
        conn: sqlite3.Connection,
        owner: str,
        source_keys: Iterable[tuple[str, int]],
    ) -> None:
        has_evidence = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_evidence'"
        ).fetchone() is not None
        for source_id, source_revision in source_keys:
            sql = (
                "DELETE FROM fm_v2_sources "
                "WHERE owner_id=? AND source_id=? AND source_revision=? "
                "AND NOT EXISTS (SELECT 1 FROM fm_v2_documents "
                "WHERE owner_id=? AND source_id=? AND source_revision=?)"
            )
            args: List[Any] = [
                owner,
                source_id,
                source_revision,
                owner,
                source_id,
                source_revision,
            ]
            if has_evidence:
                sql += (
                    " AND NOT EXISTS (SELECT 1 FROM fm_v2_evidence "
                    "WHERE owner_id=? AND source_id=? AND source_revision=?)"
                )
                args.extend((owner, source_id, source_revision))
            conn.execute(sql, args)

    def _split_into_chunks(self, text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
        """Compatibility chunker for upload routes that used VectorRAG.

        Canonical ingestion uses the versioned `_chunks` contract below; this
        adapter keeps the existing upload surface working while preserving
        bounded, deterministic text segmentation.
        """
        if not text:
            return []
        chunk_size = max(1, int(chunk_size))
        overlap = max(0, min(int(overlap), chunk_size - 1))
        if len(text) <= chunk_size:
            return [text]
        result: List[str] = []
        start = 0
        step = max(1, chunk_size - overlap)
        while start < len(text):
            piece = text[start : start + chunk_size]
            if piece.strip():
                result.append(piece)
            if start + len(piece) >= len(text):
                break
            start += step
        return result

    def add_document(self, text: str, metadata: Dict[str, Any]) -> bool:
        if not self.healthy or not isinstance(text, str) or not text.strip():
            return False
        try:
            owner = _owner(metadata.get("owner"))
        except ValueError:
            return False
        workspace = str(metadata.get("workspace_id") or "").strip()
        project = str(metadata.get("project_id") or "").strip()
        if project and not workspace:
            return False
        uri = str(metadata.get("source") or metadata.get("source_uri") or "memory://document")
        digest = _hash(text)
        source_id = _source_id(owner, workspace, project, uri)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT source_revision,content_hash FROM fm_v2_sources WHERE owner_id=? AND source_id=? ORDER BY source_revision DESC LIMIT 1",
                (owner, source_id),
            ).fetchone()
            if source and source[1] == digest:
                source_revision = int(source[0])
            else:
                source_revision = int(source[0]) + 1 if source else 1
            document_id = "doc_" + _hash(
                f"{owner}\0{source_id}\0{source_revision}\0{digest}\0{CHUNKER_VERSION}"
            )[:32]
            title = str(metadata.get("title") or Path(uri).name or document_id)
            conn.execute(
                "INSERT OR IGNORE INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (owner, source_id, workspace, project, uri, source_revision, digest, str(metadata.get("source_type") or "owner_imported_file"), now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO fm_v2_documents(owner_id,document_id,source_id,source_revision,title,parser_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (owner, document_id, source_id, source_revision, title, CHUNKER_VERSION, digest, now),
            )
            for span in _document_chunk_spans(text):
                locator_json = json.dumps(
                    {
                        "type": "text",
                        "start": span.start,
                        "end": span.end,
                        "line_start": span.line_start,
                        "line_end": span.line_end,
                        "headings": list(span.headings),
                        "block_type": span.block_type,
                    },
                    separators=(",", ":"),
                )
                chunk_id = "chunk_" + _hash(
                    f"{owner}\0{document_id}\0{CHUNKER_VERSION}\0{span.ordinal}\0"
                    f"{locator_json}\0{_hash(span.text)}"
                )[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO fm_v2_chunks(owner_id,chunk_id,document_id,ordinal,text,content_hash,locator_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (owner, chunk_id, document_id, span.ordinal, span.text, _hash(span.text), locator_json, now),
                )
                if self._fts_available:
                    conn.execute(
                        "DELETE FROM fm_v2_chunks_fts WHERE owner_id=? AND chunk_id=?",
                        (owner, chunk_id),
                    )
                    conn.execute(
                        "INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) VALUES (?,?,?)",
                        (owner, chunk_id, span.text),
                    )
            if self._fts_available:
                self._publish_inline_fts(conn, owner, workspace, project)
        return True

    def add_documents_batch(self, docs: List[tuple]) -> Dict[str, Any]:
        added = 0
        for text, metadata in docs or []:
            if self.add_document(text, metadata):
                added += 1
        return {"success": added == len(docs or []), "added_count": added, "total_count": len(docs or []), "failed_count": len(docs or []) - added}

    @staticmethod
    def _vector_blob(values: Iterable[Any], expected_dimension: int) -> bytes:
        vector = [float(value) for value in values]
        if len(vector) != expected_dimension:
            raise ValueError(
                f"embedding dimension mismatch: expected {expected_dimension}, got {len(vector)}"
            )
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("embedding contains no values or non-finite values")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("embedding has zero or invalid norm")
        vector = [value / norm for value in vector]
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def _blob_vector(blob: bytes, expected_dimension: int) -> tuple[float, ...]:
        if len(blob) != expected_dimension * 4:
            raise ValueError("stored embedding byte length does not match dimension")
        return struct.unpack(f"<{expected_dimension}f", blob)

    def _validate_vector_generation(
        self,
        conn: sqlite3.Connection,
        generation: sqlite3.Row,
        *,
        require_complete: bool,
    ) -> int:
        """Validate immutable vector rows without consulting ranked results."""
        dimension = int(generation["dimension"])
        if (
            generation["logical_space"] != VECTOR_LOGICAL_SPACE
            or dimension < 1
            or generation["normalization"] != "l2"
            or generation["metric"] != "cosine"
            or generation["chunker_version"] != CHUNKER_VERSION
        ):
            raise ValueError("vector generation configuration is incompatible")
        expected_fingerprint = _hash(
            json.dumps(
                {
                    "provider_ref": generation["provider_ref"],
                    "model": generation["model"],
                    "endpoint_class": generation["endpoint_class"],
                    "dimension": dimension,
                    "normalization": generation["normalization"],
                    "metric": generation["metric"],
                    "chunker_version": generation["chunker_version"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if generation["config_fingerprint"] != expected_fingerprint:
            raise ValueError("vector generation fingerprint is incompatible")
        rows = conn.execute(
            "SELECT e.chunk_id,e.dimension,e.embedding,e.content_hash,"
            "c.content_hash AS canonical_hash,d.parser_version,"
            "s.workspace_key,s.project_key "
            "FROM fm_v2_chunk_embeddings e "
            "LEFT JOIN fm_v2_chunks c ON c.owner_id=e.owner_id "
            "AND c.chunk_id=e.chunk_id "
            "LEFT JOIN fm_v2_documents d ON d.owner_id=c.owner_id "
            "AND d.document_id=c.document_id "
            "LEFT JOIN fm_v2_sources s ON s.owner_id=d.owner_id "
            "AND s.source_id=d.source_id AND s.source_revision=d.source_revision "
            "WHERE e.owner_id=? AND e.generation_id=?",
            (generation["owner_id"], generation["generation_id"]),
        ).fetchall()
        expected = int(generation["row_count"])
        if len(rows) > expected or (require_complete and len(rows) != expected):
            raise ValueError("vector generation row count does not match manifest")
        expected_rows = {
            row["chunk_id"]: row["chunk_hash"]
            for row in self._canonical_rows(
                conn,
                generation["owner_id"],
                generation["workspace_key"],
                generation["project_key"],
            )
        }
        if require_complete and {
            row["chunk_id"]: row["content_hash"] for row in rows
        } != expected_rows:
            raise ValueError("vector generation row identity does not match scope manifest")
        for row in rows:
            if (
                row["canonical_hash"] is None
                or row["canonical_hash"] != row["content_hash"]
                or int(row["dimension"]) != dimension
                or row["parser_version"] != CHUNKER_VERSION
                or row["workspace_key"] != generation["workspace_key"]
                or row["project_key"] != generation["project_key"]
            ):
                raise ValueError("vector generation scope or content hash is invalid")
            vector = self._blob_vector(row["embedding"], dimension)
            if any(not math.isfinite(value) for value in vector):
                raise ValueError("vector generation contains a non-finite value")
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
                raise ValueError("vector generation normalization is invalid")
        return len(rows)

    @staticmethod
    def _client_dimension(client: Any) -> int:
        dimension = int(client.get_sentence_embedding_dimension())
        if dimension < 1:
            raise ValueError("embedding dimension must be positive")
        return dimension

    @staticmethod
    def _client_model(client: Any) -> str:
        model = str(getattr(client, "model", "") or "").strip()
        if not model:
            raise ValueError("embedding client must expose an explicit model")
        return model

    @staticmethod
    def _client_endpoint_class(client: Any) -> str:
        explicit = str(getattr(client, "endpoint_class", "") or "").strip()
        if explicit:
            return explicit
        return "http" if str(getattr(client, "url", "") or "").strip() else "in_process"

    @staticmethod
    def _client_for_owner(client: Any, owner: str) -> Any:
        factory = getattr(client, "for_owner", None)
        return factory(owner) if callable(factory) else client

    @staticmethod
    def _client_provider_ref(client: Any, fallback: Optional[str] = None) -> str:
        value = str(getattr(client, "provider_ref", "") or "").strip()
        return value or str(fallback or "").strip()

    def build_embedding_generation(
        self,
        *,
        owner: str,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        embedding_client: Any = None,
        provider_ref: Optional[str] = None,
        endpoint_class: Optional[str] = None,
        publish: bool = False,
        batch_size: int = 32,
    ) -> Dict[str, Any]:
        """Build one immutable vector generation for one exact tenant scope.

        The caller supplies the embedding client and a non-secret provider or
        credential reference. Ambient API keys are never guessed here.
        """
        owner = _owner(owner)
        workspace, project = self._mutation_scope(workspace_id, project_id)
        client = self._client_for_owner(
            embedding_client or self._embedding_client,
            owner,
        )
        if client is None:
            raise ValueError("an explicit embedding client is required")
        # Dimension probing is itself a managed operation.  It establishes the
        # exact selected route/adapter/model fingerprint before immutable
        # generation metadata is written.
        dimension = self._client_dimension(client)
        reference = str(provider_ref or "").strip() or self._client_provider_ref(
            client,
            self._embedding_provider_ref,
        )
        if not reference:
            raise ValueError("a non-secret embedding provider reference is required")
        model = self._client_model(client)
        config = {
            "provider_ref": reference,
            "model": model,
            "endpoint_class": str(
                endpoint_class or self._client_endpoint_class(client)
            ),
            "dimension": dimension,
            "normalization": "l2",
            "metric": "cosine",
            "chunker_version": CHUNKER_VERSION,
        }
        fingerprint = _hash(json.dumps(config, sort_keys=True, separators=(",", ":")))
        generation_id = "gen_vec_" + uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            watermark = self._source_watermark(conn, owner, workspace, project)
            row_count = len(self._canonical_rows(conn, owner, workspace, project))
            conn.execute(
                "INSERT INTO fm_v2_derived_generations("
                "owner_id,generation_id,logical_space,workspace_key,project_key,"
                "provider_ref,model,endpoint_class,dimension,normalization,metric,"
                "chunker_version,config_fingerprint,source_watermark,row_count,state,"
                "retention_state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    owner,
                    generation_id,
                    VECTOR_LOGICAL_SPACE,
                    workspace,
                    project,
                    reference,
                    model,
                    config["endpoint_class"],
                    dimension,
                    config["normalization"],
                    config["metric"],
                    config["chunker_version"],
                    fingerprint,
                    watermark,
                    row_count,
                    "planned",
                    "retained",
                    now,
                    now,
                ),
            )
        result = self.resume_embedding_generation(
            owner=owner,
            generation_id=generation_id,
            embedding_client=client,
            batch_size=batch_size,
        )
        if publish and result.get("state") == "ready":
            result["publication"] = self.publish_generation(
                owner=owner, generation_id=generation_id
            )
        return result

    def resume_embedding_generation(
        self,
        *,
        owner: str,
        generation_id: str,
        embedding_client: Any = None,
        batch_size: int = 32,
    ) -> Dict[str, Any]:
        """Resume a planned/building generation; partial rows stay invisible."""
        owner = _owner(owner)
        client = self._client_for_owner(
            embedding_client or self._embedding_client,
            owner,
        )
        if client is None:
            raise ValueError("an explicit embedding client is required")
        batch_size = max(1, min(int(batch_size), 256))
        with self._connect() as conn:
            generation = conn.execute(
                "SELECT * FROM fm_v2_derived_generations "
                "WHERE owner_id=? AND generation_id=?",
                (owner, generation_id),
            ).fetchone()
            if generation is None or generation["logical_space"] != VECTOR_LOGICAL_SPACE:
                raise ValueError("unknown vector generation")
            if generation["state"] == "ready":
                count = conn.execute(
                    "SELECT count(*) FROM fm_v2_chunk_embeddings "
                    "WHERE owner_id=? AND generation_id=?",
                    (owner, generation_id),
                ).fetchone()[0]
                return {"generation_id": generation_id, "state": "ready", "embedded": count}
            if generation["state"] not in {"planned", "building"}:
                raise ValueError(f"generation cannot resume from {generation['state']}")
            runtime_dimension = self._client_dimension(client)
            runtime_reference = self._client_provider_ref(
                client,
                self._embedding_provider_ref,
            )
            if self._client_model(client) != generation["model"]:
                raise ValueError("embedding client model does not match generation")
            if runtime_dimension != int(generation["dimension"]):
                raise ValueError("embedding client dimension does not match generation")
            if not runtime_reference or runtime_reference != generation["provider_ref"]:
                raise ValueError("embedding client provider fingerprint does not match generation")
            if self._client_endpoint_class(client) != generation["endpoint_class"]:
                raise ValueError("embedding client adapter class does not match generation")
            rows = self._canonical_rows(
                conn,
                owner,
                generation["workspace_key"],
                generation["project_key"],
            )
            conn.execute(
                "UPDATE fm_v2_derived_generations SET state='building',updated_at=? "
                "WHERE owner_id=? AND generation_id=? AND state='planned'",
                (datetime.now(timezone.utc).isoformat(), owner, generation_id),
            )
            if self._source_watermark(
                conn,
                owner,
                generation["workspace_key"],
                generation["project_key"],
            ) != generation["source_watermark"]:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE fm_v2_derived_generations SET state='failed',"
                    "failure_json=?,updated_at=? WHERE owner_id=? AND generation_id=?",
                    (json.dumps({"reason": "source_changed_before_build"}), now, owner, generation_id),
                )
                return {"generation_id": generation_id, "state": "failed", "reason": "source_changed_before_build"}
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM fm_v2_chunk_embeddings "
                    "WHERE owner_id=? AND generation_id=?",
                    (owner, generation_id),
                )
            }
            pending = [row for row in rows if row["chunk_id"] not in existing]

        try:
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset : offset + batch_size]
                encoded = client.encode(
                    [row["text"] for row in batch], normalize_embeddings=True
                )
                vectors = list(encoded)
                if len(vectors) != len(batch):
                    raise ValueError("embedding client returned the wrong row count")
                now = datetime.now(timezone.utc).isoformat()
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute(
                        "SELECT state FROM fm_v2_derived_generations "
                        "WHERE owner_id=? AND generation_id=?",
                        (owner, generation_id),
                    ).fetchone()
                    if current is None or current[0] != "building":
                        raise ValueError("generation is no longer buildable")
                    for row, vector in zip(batch, vectors):
                        conn.execute(
                            "INSERT OR IGNORE INTO fm_v2_chunk_embeddings("
                            "owner_id,generation_id,chunk_id,dimension,embedding,"
                            "content_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                            (
                                owner,
                                generation_id,
                                row["chunk_id"],
                                int(generation["dimension"]),
                                self._vector_blob(vector, int(generation["dimension"])),
                                row["chunk_hash"],
                                now,
                            ),
                        )
        except Exception as exc:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE fm_v2_derived_generations SET state='failed',"
                    "failure_json=?,updated_at=? WHERE owner_id=? AND generation_id=? "
                    "AND state IN ('planned','building','validating')",
                    (
                        json.dumps({"reason": "embedding_failed", "type": type(exc).__name__}),
                        datetime.now(timezone.utc).isoformat(),
                        owner,
                        generation_id,
                    ),
                )
            return {"generation_id": generation_id, "state": "failed", "reason": "embedding_failed"}

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            generation = conn.execute(
                "SELECT * FROM fm_v2_derived_generations "
                "WHERE owner_id=? AND generation_id=?",
                (owner, generation_id),
            ).fetchone()
            current_watermark = self._source_watermark(
                conn,
                owner,
                generation["workspace_key"],
                generation["project_key"],
            )
            expected_rows = self._canonical_rows(
                conn,
                owner,
                generation["workspace_key"],
                generation["project_key"],
            )
            embedded_count = conn.execute(
                "SELECT count(*) FROM fm_v2_chunk_embeddings "
                "WHERE owner_id=? AND generation_id=?",
                (owner, generation_id),
            ).fetchone()[0]
            if (
                current_watermark != generation["source_watermark"]
                or embedded_count != len(expected_rows)
                or embedded_count != int(generation["row_count"])
            ):
                reason = (
                    "source_changed_during_build"
                    if current_watermark != generation["source_watermark"]
                    else "embedding_count_mismatch"
                )
                conn.execute(
                    "UPDATE fm_v2_derived_generations SET state='failed',"
                    "failure_json=?,updated_at=? WHERE owner_id=? AND generation_id=?",
                    (
                        json.dumps({"reason": reason, "expected": len(expected_rows), "actual": embedded_count}),
                        datetime.now(timezone.utc).isoformat(),
                        owner,
                        generation_id,
                    ),
                )
                return {"generation_id": generation_id, "state": "failed", "reason": reason}
            now = datetime.now(timezone.utc).isoformat()
            transitioned = conn.execute(
                "UPDATE fm_v2_derived_generations SET state='validating',updated_at=? "
                "WHERE owner_id=? AND generation_id=? AND state='building'",
                (now, owner, generation_id),
            )
            if transitioned.rowcount != 1:
                raise RuntimeError("generation state changed before validation")
            try:
                validating = conn.execute(
                    "SELECT * FROM fm_v2_derived_generations "
                    "WHERE owner_id=? AND generation_id=?",
                    (owner, generation_id),
                ).fetchone()
                self._validate_vector_generation(
                    conn, validating, require_complete=True
                )
            except Exception as exc:
                conn.execute(
                    "UPDATE fm_v2_derived_generations SET state='failed',"
                    "failure_json=?,updated_at=? WHERE owner_id=? AND generation_id=? "
                    "AND state='validating'",
                    (
                        json.dumps(
                            {
                                "reason": "validation_failed",
                                "type": type(exc).__name__,
                            }
                        ),
                        now,
                        owner,
                        generation_id,
                    ),
                )
                return {
                    "generation_id": generation_id,
                    "state": "failed",
                    "reason": "validation_failed",
                }
            ready = conn.execute(
                "UPDATE fm_v2_derived_generations SET state='ready',"
                "validated_at=?,updated_at=? WHERE owner_id=? AND generation_id=? "
                "AND state='validating'",
                (now, now, owner, generation_id),
            )
            if ready.rowcount != 1:
                raise RuntimeError("generation state changed before ready publication")
        return {"generation_id": generation_id, "state": "ready", "embedded": embedded_count}

    def publish_generation(
        self,
        *,
        owner: str,
        generation_id: str,
        expected_active_generation: Optional[str] = None,
        allow_lagging: bool = False,
    ) -> Dict[str, Any]:
        """Atomically swap one exact-scope pointer to a validated generation."""
        owner = _owner(owner)
        if allow_lagging:
            raise ValueError(
                "behind generations cannot be published; use rollback_generation"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            generation = conn.execute(
                "SELECT * FROM fm_v2_derived_generations "
                "WHERE owner_id=? AND generation_id=?",
                (owner, generation_id),
            ).fetchone()
            if generation is None or generation["state"] != "ready":
                raise ValueError("only a ready generation can be published")
            if generation["logical_space"] != VECTOR_LOGICAL_SPACE:
                raise ValueError("only vector generations use explicit publication")
            if self._source_watermark(
                conn,
                owner,
                generation["workspace_key"],
                generation["project_key"],
            ) != generation["source_watermark"]:
                raise RuntimeError("generation source watermark is behind canonical data")
            if generation["logical_space"] == VECTOR_LOGICAL_SPACE:
                self._validate_vector_generation(
                    conn, generation, require_complete=True
                )
            return self._swap_pointer(
                conn,
                generation,
                action="publish",
                expected_active_generation=expected_active_generation,
                allow_lagging=False,
            )

    def rollback_generation(
        self,
        *,
        owner: str,
        generation_id: str,
        expected_active_generation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically repoint one scope to a retained, validated generation."""
        owner = _owner(owner)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            generation = conn.execute(
                "SELECT * FROM fm_v2_derived_generations "
                "WHERE owner_id=? AND generation_id=?",
                (owner, generation_id),
            ).fetchone()
            if (
                generation is None
                or generation["state"] != "ready"
                or generation["retention_state"] != "retained"
            ):
                raise ValueError("rollback target must be a retained ready generation")
            if generation["logical_space"] != VECTOR_LOGICAL_SPACE:
                raise ValueError("only vector generations can be rolled back")
            if generation["logical_space"] == VECTOR_LOGICAL_SPACE:
                self._validate_vector_generation(
                    conn, generation, require_complete=False
                )
            return self._swap_pointer(
                conn,
                generation,
                action="rollback",
                expected_active_generation=expected_active_generation,
                allow_lagging=True,
            )

    def _pointer_health(
        self,
        conn: sqlite3.Connection,
        owner: str,
        logical_space: str,
        workspace: str,
        project: str,
    ) -> Dict[str, Any]:
        latest = conn.execute(
            "SELECT generation_id,state,failure_json,created_at FROM "
            "fm_v2_derived_generations WHERE owner_id=? AND logical_space=? "
            "AND workspace_key=? AND project_key=? "
            "ORDER BY created_at DESC,generation_id DESC LIMIT 1",
            (owner, logical_space, workspace, project),
        ).fetchone()
        latest_failure = None
        if latest and latest["failure_json"]:
            try:
                latest_failure = json.loads(latest["failure_json"])
            except (TypeError, json.JSONDecodeError):
                latest_failure = {"reason": "invalid_failure_metadata"}
        latest_attempt = (
            {
                "generation_id": latest["generation_id"],
                "build_state": latest["state"],
                "failure": latest_failure,
                "created_at": latest["created_at"],
            }
            if latest
            else None
        )
        row = conn.execute(
            "SELECT p.generation_id,p.publication_tx,p.publication_watermark,p.published_at,"
            "g.state,g.source_watermark,g.config_fingerprint,g.dimension,"
            "g.model,g.provider_ref,g.endpoint_class,g.normalization,g.metric,"
            "g.chunker_version,g.retention_state,g.logical_space,g.owner_id,"
            "g.workspace_key,g.project_key,g.row_count "
            "FROM fm_v2_index_pointers p LEFT JOIN fm_v2_derived_generations g "
            "ON g.owner_id=p.owner_id AND g.generation_id=p.generation_id "
            "WHERE p.owner_id=? AND p.logical_space=? AND p.workspace_key=? "
            "AND p.project_key=?",
            (owner, logical_space, workspace, project),
        ).fetchone()
        if row is None:
            return {
                "health": "missing",
                "generation_id": None,
                "reason": "no_active_pointer",
                "latest_attempt": latest_attempt,
            }
        if row["state"] != "ready" or row["retention_state"] != "retained":
            return {
                "health": "invalid",
                "generation_id": row["generation_id"],
                "reason": "active_generation_not_retained_ready",
                "latest_attempt": latest_attempt,
            }
        watermark = self._source_watermark(conn, owner, workspace, project)
        expected_fingerprint = _hash(
            json.dumps(
                {
                    "provider_ref": row["provider_ref"],
                    "model": row["model"],
                    "endpoint_class": row["endpoint_class"],
                    "dimension": int(row["dimension"]),
                    "normalization": row["normalization"],
                    "metric": row["metric"],
                    "chunker_version": row["chunker_version"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        invalid_reason = None
        if not row["publication_tx"]:
            invalid_reason = "missing_publication_transaction"
        elif row["publication_watermark"] != row["source_watermark"]:
            invalid_reason = "pointer_generation_watermark_mismatch"
        elif row["config_fingerprint"] != expected_fingerprint:
            invalid_reason = "generation_configuration_fingerprint_mismatch"
        elif logical_space == FTS_LOGICAL_SPACE and (
            row["provider_ref"] != "sqlite-fts5"
            or row["model"] != "fts5"
            or int(row["dimension"]) != 0
            or row["normalization"] != "bm25"
            or row["metric"] != "bm25"
            or row["chunker_version"] != CHUNKER_VERSION
        ):
            invalid_reason = "fts_configuration_mismatch"
        elif logical_space == VECTOR_LOGICAL_SPACE:
            try:
                self._validate_vector_generation(
                    conn,
                    row,
                    require_complete=watermark == row["source_watermark"],
                )
            except (TypeError, ValueError, struct.error):
                invalid_reason = "vector_integrity_mismatch"
        if invalid_reason:
            return {
                "health": "invalid",
                "generation_id": row["generation_id"],
                "reason": invalid_reason,
                "latest_attempt": latest_attempt,
            }
        health = "current" if watermark == row["source_watermark"] else "lagging"
        return {
            "health": health,
            "reason": None if health == "current" else "canonical_watermark_ahead",
            "generation_id": row["generation_id"],
            "publication_id": row["publication_tx"],
            "source_watermark": row["source_watermark"],
            "canonical_watermark": watermark,
            "published_at": row["published_at"],
            "config_fingerprint": row["config_fingerprint"],
            "dimension": row["dimension"],
            "model": row["model"],
            "provider_ref": row["provider_ref"],
            "endpoint_class": row["endpoint_class"],
            "normalization": row["normalization"],
            "metric": row["metric"],
            "latest_attempt": latest_attempt,
        }

    def _fts_ranked_ids(
        self,
        conn: sqlite3.Connection,
        owner: str,
        workspace: str,
        project: str,
        tokens: List[str],
    ) -> List[str]:
        if not self._fts_available or not tokens:
            return []
        match = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
        try:
            rows = conn.execute(
                "SELECT f.chunk_id,bm25(fm_v2_chunks_fts) AS rank_score "
                "FROM fm_v2_chunks_fts f "
                "JOIN fm_v2_chunks c ON c.owner_id=f.owner_id AND c.chunk_id=f.chunk_id "
                "JOIN fm_v2_documents d ON d.owner_id=c.owner_id "
                "AND d.document_id=c.document_id "
                "JOIN fm_v2_sources s ON s.owner_id=d.owner_id "
                "AND s.source_id=d.source_id AND s.source_revision=d.source_revision "
                "WHERE f.owner_id=? AND fm_v2_chunks_fts MATCH ? "
                "AND s.workspace_key=? AND s.project_key=? "
                "AND s.forget_state='active' AND NOT EXISTS ("
                "SELECT 1 FROM fm_v2_sources newer "
                "WHERE newer.owner_id=s.owner_id AND newer.source_id=s.source_id "
                "AND newer.source_revision>s.source_revision) "
                "ORDER BY rank_score,f.chunk_id",
                (owner, match, workspace, project),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [row["chunk_id"] for row in rows]

    def _query_vector(
        self,
        owner: str,
        pointer: Dict[str, Any],
        query: str,
        cache: Dict[tuple[str, int, str, str], tuple[float, ...]],
    ) -> Optional[tuple[float, ...]]:
        client = self._client_for_owner(self._embedding_client, owner)
        if client is None:
            return None
        try:
            runtime_dimension = self._client_dimension(client)
            runtime_reference = self._client_provider_ref(
                client,
                self._embedding_provider_ref,
            )
        except Exception:
            return None
        if not runtime_reference:
            return None
        key = (
            str(pointer.get("model") or ""),
            int(pointer.get("dimension") or 0),
            str(pointer.get("provider_ref") or ""),
            str(pointer.get("endpoint_class") or ""),
        )
        if key[0] != self._client_model(client):
            return None
        if key[1] != runtime_dimension:
            return None
        if key[2] != runtime_reference:
            return None
        if key[3] != self._client_endpoint_class(client):
            return None
        if key not in cache:
            encoded = list(client.encode([query], normalize_embeddings=True))
            if len(encoded) != 1:
                return None
            blob = self._vector_blob(encoded[0], key[1])
            cache[key] = self._blob_vector(blob, key[1])
        return cache[key]

    def _vector_ranked_ids(
        self,
        conn: sqlite3.Connection,
        owner: str,
        generation_id: str,
        canonical: Dict[str, sqlite3.Row],
        query_vector: tuple[float, ...],
        dimension: int,
    ) -> List[tuple[str, float]]:
        ranked: List[tuple[str, float]] = []
        rows = conn.execute(
            "SELECT chunk_id,dimension,embedding,content_hash "
            "FROM fm_v2_chunk_embeddings "
            "WHERE owner_id=? AND generation_id=?",
            (owner, generation_id),
        ).fetchall()
        for row in rows:
            current = canonical.get(row["chunk_id"])
            if current is None or current["chunk_hash"] != row["content_hash"]:
                continue
            if int(row["dimension"]) != dimension:
                continue
            try:
                vector = self._blob_vector(row["embedding"], dimension)
            except ValueError:
                continue
            score = sum(left * right for left, right in zip(query_vector, vector))
            if math.isfinite(score) and score > 0:
                ranked.append((row["chunk_id"], score))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked

    def search(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.healthy or not isinstance(query, str) or not query.strip() or not owner:
            return []
        try:
            owner = _owner(owner)
            scopes = self._effective_scopes(workspace_id, project_id)
        except ValueError:
            return []
        tokens = [
            token for token in re.findall(r"[\w-]+", query.casefold()) if len(token) > 1
        ]
        if not tokens:
            return []
        needle = query.strip().strip('"\'').casefold()
        candidates: Dict[str, Dict[str, Any]] = {}
        generation_map: Dict[str, Dict[str, Any]] = {}
        degradation: List[str] = []
        watermarks: Dict[str, str] = {}
        query_vectors: Dict[tuple[str, int, str, str], tuple[float, ...]] = {}
        effective_scope_labels = [
            self._scope_label(workspace, project)
            for workspace, project, _ in scopes
        ]

        with self._connect() as conn:
            for workspace, project, specificity in scopes:
                scope_label = self._scope_label(workspace, project)
                rows = self._canonical_rows(conn, owner, workspace, project)
                canonical = {row["chunk_id"]: row for row in rows}
                watermark = self._source_watermark(conn, owner, workspace, project)
                watermarks[scope_label] = watermark

                exact_ranked: List[tuple[str, float, bool]] = []
                for row in rows:
                    haystack = "\n".join(
                        (row["title"] or "", row["source_uri"] or "", row["text"] or "")
                    ).casefold()
                    overlap = sum(1 for token in tokens if token in haystack) / len(tokens)
                    phrase = bool(needle and needle in haystack)
                    if phrase or overlap > 0:
                        exact_ranked.append((row["chunk_id"], overlap, phrase))
                exact_ranked.sort(
                    key=lambda item: (
                        -int(item[2]),
                        -item[1],
                        canonical[item[0]]["source_uri"],
                        canonical[item[0]]["ordinal"],
                        item[0],
                    )
                )

                fts_pointer = self._pointer_health(
                    conn, owner, FTS_LOGICAL_SPACE, workspace, project
                )
                generation_map[f"{FTS_LOGICAL_SPACE}:{scope_label}"] = fts_pointer
                if (
                    self._fts_available
                    and fts_pointer["health"] in {"current", "lagging"}
                ):
                    fts_ranked = [
                        chunk_id
                        for chunk_id in self._fts_ranked_ids(
                            conn, owner, workspace, project, tokens
                        )
                        if chunk_id in canonical
                    ]
                else:
                    fts_ranked = []
                    reason = (
                        "runtime_unavailable"
                        if not self._fts_available
                        else fts_pointer["health"]
                    )
                    degradation.append(f"{FTS_LOGICAL_SPACE}:{scope_label}:{reason}")

                vector_pointer = self._pointer_health(
                    conn, owner, VECTOR_LOGICAL_SPACE, workspace, project
                )
                generation_map[f"{VECTOR_LOGICAL_SPACE}:{scope_label}"] = vector_pointer
                vector_ranked: List[tuple[str, float]] = []
                if vector_pointer["health"] in {"current", "lagging"}:
                    query_vector = self._query_vector(
                        owner,
                        vector_pointer,
                        query,
                        query_vectors,
                    )
                    if query_vector is None:
                        degradation.append(
                            f"{VECTOR_LOGICAL_SPACE}:{scope_label}:client_unavailable_or_mismatch"
                        )
                    else:
                        vector_ranked = self._vector_ranked_ids(
                            conn,
                            owner,
                            vector_pointer["generation_id"],
                            canonical,
                            query_vector,
                            int(vector_pointer["dimension"]),
                        )
                else:
                    degradation.append(
                        f"{VECTOR_LOGICAL_SPACE}:{scope_label}:{vector_pointer['health']}"
                    )

                lane_lists = {
                    "exact": [item[0] for item in exact_ranked],
                    "fts": fts_ranked,
                    "vector": [item[0] for item in vector_ranked],
                }
                rank_maps = {
                    lane: {
                        chunk_id: rank
                        for rank, chunk_id in enumerate(ranked_ids, start=1)
                    }
                    for lane, ranked_ids in lane_lists.items()
                }
                exact_scores = {
                    chunk_id: {"overlap": overlap, "phrase": phrase}
                    for chunk_id, overlap, phrase in exact_ranked
                }
                vector_scores = dict(vector_ranked)
                seen_ids = set().union(*(set(items) for items in lane_lists.values()))
                for chunk_id in seen_ids:
                    row = canonical[chunk_id]
                    item = candidates.setdefault(
                        chunk_id,
                        {
                            "row": row,
                            "specificity": specificity,
                            "scope_label": scope_label,
                            "rrf": 0.0,
                            "ranks": {},
                            "exact_overlap": 0.0,
                            "exact_phrase": False,
                            "vector_score": None,
                        },
                    )
                    for lane, rank_map in rank_maps.items():
                        rank = rank_map.get(chunk_id)
                        if rank is None:
                            continue
                        item["ranks"][lane] = rank
                        item["rrf"] += RRF_WEIGHTS[lane] / (RRF_K + rank)
                    exact = exact_scores.get(chunk_id)
                    if exact:
                        item["exact_overlap"] = exact["overlap"]
                        item["exact_phrase"] = exact["phrase"]
                    if chunk_id in vector_scores:
                        item["vector_score"] = vector_scores[chunk_id]

        # Documents are set-valued, but a more-specific version of the same
        # source/ordinal shadows its owner/workspace ancestor.
        visible: Dict[tuple[str, int], Dict[str, Any]] = {}
        for item in candidates.values():
            row = item["row"]
            key = (str(row["source_uri"]), int(row["ordinal"]))
            current = visible.get(key)
            if current is None or item["specificity"] > current["specificity"]:
                item["shadowed_chunk_id"] = (
                    current["row"]["chunk_id"] if current is not None else None
                )
                visible[key] = item
            elif item["specificity"] == current["specificity"] and item["rrf"] > current["rrf"]:
                visible[key] = item

        ranked = list(visible.values())
        ranked.sort(
            key=lambda item: (
                -int(item["exact_phrase"]),
                -item["rrf"],
                -item["specificity"],
                item["row"]["source_uri"],
                item["row"]["ordinal"],
                item["row"]["chunk_id"],
            )
        )
        read_watermark = _hash(
            json.dumps(watermarks, sort_keys=True, separators=(",", ":"))
        )
        results: List[Dict[str, Any]] = []
        for item in ranked[: max(1, min(int(k), 100))]:
            row = item["row"]
            vector_score = item["vector_score"]
            fts_rank = item["ranks"].get("fts")
            similarity = max(
                item["exact_overlap"],
                max(0.0, float(vector_score)) if vector_score is not None else 0.0,
                (1.0 / fts_rank) if fts_rank else 0.0,
            )
            try:
                locator = json.loads(row["locator_json"])
            except (TypeError, json.JSONDecodeError):
                locator = {"type": "unknown"}
            exact_scope = {
                "owner": owner,
                "workspace_id": row["workspace_key"],
                "project_id": row["project_key"],
            }
            explanation = {
                "canonical_id": row["chunk_id"],
                "locator": locator,
                "exact_scope": exact_scope,
                "effective_scope": item["scope_label"],
                "effective_scope_set": effective_scope_labels,
                "shadowed_chunk_id": item.get("shadowed_chunk_id"),
                "score_components": {
                    "rrf": round(item["rrf"], 8),
                    "ranks": item["ranks"],
                    "weights": RRF_WEIGHTS,
                    "exact_overlap": round(item["exact_overlap"], 6),
                    "exact_phrase": item["exact_phrase"],
                    "vector_cosine": (
                        round(float(vector_score), 6)
                        if vector_score is not None
                        else None
                    ),
                },
                "ranker_versions": {
                    "fusion": RANKER_VERSION,
                    "fts": "sqlite-fts5-bm25",
                    "vector": "cosine-l2",
                    "chunker": CHUNKER_VERSION,
                },
                "canonical_read_watermarks": watermarks,
                "generation_map": generation_map,
                "trust_label": "owner_document_untrusted_prompt_content",
                "degraded": bool(degradation),
                "degradation_reasons": sorted(set(degradation)),
            }
            results.append(
                {
                    "id": row["chunk_id"],
                    "document": row["text"],
                    "metadata": {
                        "owner": owner,
                        "workspace_id": row["workspace_key"],
                        "project_id": row["project_key"],
                        "source": row["source_uri"],
                        "source_id": row["source_id"],
                        "source_revision": row["source_revision"],
                        "title": row["title"],
                        "document_id": row["document_id"],
                        "ordinal": row["ordinal"],
                    },
                    "similarity": round(similarity, 6),
                    "search_type": (
                        "frankenmemory_hybrid"
                        if "vector" in item["ranks"]
                        else "frankenmemory_fts"
                        if "fts" in item["ranks"]
                        else "frankenmemory_exact"
                    ),
                    "score_components": {
                        "rrf": round(item["rrf"], 8),
                        "ranks": item["ranks"],
                        "weights": RRF_WEIGHTS,
                        "exact_overlap": round(item["exact_overlap"], 6),
                        "exact_phrase": item["exact_phrase"],
                        "vector_cosine": (
                            round(float(vector_score), 6)
                            if vector_score is not None
                            else None
                        ),
                        "rrf_k": RRF_K,
                    },
                    "effective_scope": item["scope_label"],
                    "exact_scope": exact_scope,
                    "matched_locator": locator,
                    "canonical_read_watermark": read_watermark,
                    "generation_map": generation_map,
                    "trust_label": "owner_document_untrusted_prompt_content",
                    "degraded": bool(degradation),
                    "degradation_reasons": sorted(set(degradation)),
                    "ranker_versions": explanation["ranker_versions"],
                    "retrieval_explanation": explanation,
                }
            )
        return results

    def search_explain(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return search results plus retrieval state even when no row matches."""
        if not isinstance(query, str) or not query.strip():
            return {
                "results": [],
                "error": "query is required",
                "degraded": False,
                "generation_map": {},
                "degradation_reasons": [],
            }
        if not owner:
            return {
                "results": [],
                "error": "owner is required",
                "degraded": False,
                "generation_map": {},
                "degradation_reasons": [],
            }
        try:
            owner = _owner(owner)
            scopes = self._effective_scopes(workspace_id, project_id)
        except ValueError as exc:
            return {
                "results": [],
                "error": str(exc),
                "degraded": False,
                "generation_map": {},
                "degradation_reasons": [],
            }
        results = self.search(
            query,
            k,
            owner=owner,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        if results:
            first = results[0]
            return {
                "results": results,
                "error": None,
                "generation_map": first["generation_map"],
                "canonical_read_watermark": first["canonical_read_watermark"],
                "degraded": bool(first["degradation_reasons"]),
                "degradation_reasons": first["degradation_reasons"],
                "ranker_versions": first["ranker_versions"],
            }
        generation_map: Dict[str, Dict[str, Any]] = {}
        watermarks: Dict[str, str] = {}
        degradation: List[str] = []
        with self._connect() as conn:
            for workspace, project, _ in scopes:
                label = self._scope_label(workspace, project)
                watermarks[label] = self._source_watermark(
                    conn, owner, workspace, project
                )
                for logical_space in (FTS_LOGICAL_SPACE, VECTOR_LOGICAL_SPACE):
                    health = self._pointer_health(
                        conn, owner, logical_space, workspace, project
                    )
                    generation_map[f"{logical_space}:{label}"] = health
                    if health["health"] != "current":
                        degradation.append(
                            f"{logical_space}:{label}:{health['health']}"
                        )
        return {
            "results": [],
            "error": None,
            "generation_map": generation_map,
            "canonical_read_watermark": _hash(
                json.dumps(watermarks, sort_keys=True, separators=(",", ":"))
            ),
            "degraded": bool(degradation),
            "degradation_reasons": sorted(set(degradation)),
            "ranker_versions": {
                "fusion": RANKER_VERSION,
                "fts": "sqlite-fts5-bm25",
                "vector": "cosine-l2",
                "chunker": CHUNKER_VERSION,
            },
        }

    def retrieve(self, query: str, k: int = 5, owner: Optional[str] = None, workspace_id: Optional[str] = None, project_id: Optional[str] = None) -> List[str]:
        return [item["document"] for item in self.search(query, k, owner=owner, workspace_id=workspace_id, project_id=project_id)]

    def list_sources(
        self,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        owner = _owner(owner)
        workspace, project = self._mutation_scope(workspace_id, project_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.source_uri,s.workspace_key,s.project_key,s.source_revision,"
                "s.source_type,count(d.document_id) AS document_count "
                "FROM fm_v2_sources s JOIN fm_v2_documents d "
                "ON d.owner_id=s.owner_id AND d.source_id=s.source_id "
                "AND d.source_revision=s.source_revision "
                "WHERE s.owner_id=? AND s.workspace_key=? AND s.project_key=? "
                "AND d.parser_version=? "
                "AND s.forget_state='active' AND NOT EXISTS ("
                "SELECT 1 FROM fm_v2_sources newer WHERE newer.owner_id=s.owner_id "
                "AND newer.source_id=s.source_id AND newer.source_revision>s.source_revision) "
                "GROUP BY s.source_uri,s.workspace_key,s.project_key,s.source_revision,s.source_type "
                "ORDER BY s.source_uri",
                (owner, workspace, project, CHUNKER_VERSION),
            ).fetchall()
        return [dict(row) for row in rows]

    def index_personal_documents(self, directory: str, file_extensions: Optional[set] = None, owner: Optional[str] = None, workspace_id: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
        owner = _owner(owner)
        root = Path(directory).expanduser().resolve()
        extensions = {str(item).lower() for item in (file_extensions or DEFAULT_FILE_EXTENSIONS)}
        indexed = 0
        skipped = 0
        if not root.is_dir():
            return {"success": False, "indexed": 0, "skipped": 0, "message": "directory not found"}
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            try:
                if path.is_symlink():
                    skipped += 1
                    continue
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                text = _read_stable_text(resolved)
                if self.add_document(text, {"owner": owner, "workspace_id": workspace_id, "project_id": project_id, "source": str(resolved), "title": resolved.name, "source_type": "owner_imported_file"}):
                    indexed += 1
                else:
                    skipped += 1
            except (OSError, ValueError):
                skipped += 1
        return {"success": True, "indexed": indexed, "indexed_count": indexed, "skipped": skipped, "backend": self.backend}

    def remove_directory(self, directory: str, owner: Optional[str] = None, workspace_id: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
        if not owner:
            # A directory alone is not a tenant authority.  Refuse the
            # ambiguous legacy call rather than deleting another user's docs.
            return {"success": False, "removed": 0, "message": "owner is required"}
        owner = _owner(owner)
        try:
            workspace, project = self._mutation_scope(workspace_id, project_id)
        except ValueError as exc:
            return {"success": False, "removed": 0, "message": str(exc)}
        prefix = str(Path(directory).expanduser().resolve())
        with self._connect() as conn:
            sources = conn.execute(
                "SELECT s.source_id,s.source_revision,s.source_uri "
                "FROM fm_v2_sources s "
                "WHERE s.owner_id=? AND s.workspace_key=? AND s.project_key=?",
                (owner, workspace, project),
            ).fetchall()
            def under(path: str) -> bool:
                try:
                    return os.path.commonpath([prefix, str(Path(path).expanduser().resolve())]) == prefix
                except ValueError:
                    return False
            source_keys = [(row[0], row[1]) for row in sources if under(row[2])]
            ids: list[str] = []
            for source_id, source_revision in source_keys:
                ids.extend(row[0] for row in conn.execute(
                    "SELECT document_id FROM fm_v2_documents WHERE owner_id=? AND source_id=? AND source_revision=?",
                    (owner, source_id, source_revision),
                ))
            self._delete_documents(conn, owner, ids)
            self._delete_unreferenced_sources(conn, owner, source_keys)
            if self._fts_available:
                self._publish_inline_fts(conn, owner, workspace, project)
        return {"success": True, "removed": len(ids)}

    def delete_by_source(
        self,
        source: str,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> int:
        """Forget one exact owner-owned source and its derived documents."""
        if not owner:
            return 0
        owner = _owner(owner)
        try:
            workspace, project = self._mutation_scope(workspace_id, project_id)
        except ValueError:
            return 0
        target = str(Path(source).expanduser().resolve())
        with self._connect() as conn:
            keys = conn.execute(
                "SELECT source_id,source_revision FROM fm_v2_sources "
                "WHERE owner_id=? AND source_uri=? AND workspace_key=? AND project_key=?",
                (owner, target, workspace, project),
            ).fetchall()
            documents = [
                row[0]
                for source_id, source_revision in keys
                for row in conn.execute(
                    "SELECT document_id FROM fm_v2_documents "
                    "WHERE owner_id=? AND source_id=? AND source_revision=?",
                    (owner, source_id, source_revision),
                )
            ]
            self._delete_documents(conn, owner, documents)
            self._delete_unreferenced_sources(conn, owner, keys)
            if self._fts_available:
                self._publish_inline_fts(conn, owner, workspace, project)
            return len(documents)

    def rebuild_index(self) -> bool:
        if not self.healthy or not self._fts_available:
            return False
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM fm_v2_chunks_fts")
                conn.execute(
                    "INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) "
                    "SELECT owner_id,chunk_id,text FROM fm_v2_chunks"
                )
                scopes = conn.execute(
                    "SELECT DISTINCT owner_id,workspace_key,project_key "
                    "FROM fm_v2_sources"
                ).fetchall()
                for owner, workspace, project in scopes:
                    self._publish_inline_fts(conn, owner, workspace, project)
            return True
        except sqlite3.Error:
            logger.exception("canonical Frankenmemory FTS rebuild failed")
            return False

    def rewrite_owner_paths(
        self,
        owner: str,
        *,
        path_map: Optional[Dict[str, str]] = None,
        path_prefixes: Optional[List[tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Rewrite source URIs after account bytes move, without changing tenancy.

        Account owner rename is committed transactionally by the canonical
        Frankenmemory store.  Personal uploads move immediately afterward, so
        this narrow, idempotent operation updates only the target owner's path
        projection and never performs a second owner rename.
        """
        owner_key = _owner(owner)
        exact = {
            os.path.abspath(str(source)): os.path.abspath(str(target))
            for source, target in (path_map or {}).items()
        }
        prefixes = list(path_prefixes or [])
        updated = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT source_id,source_revision,source_uri FROM fm_v2_sources "
                "WHERE owner_id=? ORDER BY source_id,source_revision",
                (owner_key,),
            ).fetchall()
            for row in rows:
                old_uri = str(row["source_uri"] or "")
                new_uri = _rewrite_source_uri(old_uri, exact, prefixes)
                if new_uri == old_uri:
                    continue
                changed = conn.execute(
                    "UPDATE fm_v2_sources SET source_uri=? "
                    "WHERE owner_id=? AND source_id=? AND source_revision=? "
                    "AND source_uri=?",
                    (
                        new_uri,
                        owner_key,
                        row["source_id"],
                        row["source_revision"],
                        old_uri,
                    ),
                ).rowcount
                if changed != 1:
                    raise sqlite3.IntegrityError(
                        "RAG source path changed during owner lifecycle"
                    )
                updated += 1
        return {"success": True, "updated_count": updated}

    def rename_owner(
        self,
        old_owner: str,
        new_owner: str,
        *,
        path_map: Optional[Dict[str, str]] = None,
        path_prefixes: Optional[List[tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        old_owner, new_owner = _owner(old_owner), _owner(new_owner)
        if old_owner == new_owner:
            return {"success": True, "updated_count": 0}
        path_map = {
            os.path.abspath(source): os.path.abspath(target)
            for source, target in (path_map or {}).items()
        }
        path_prefixes = path_prefixes or []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Owner participates in every composite key and FK, so updating
            # rows in place would violate immediate SQLite constraints. Copy
            # the complete projection under the new tenant with identities
            # derived from that tenant, then remove the old projection in
            # child-to-parent order. Retaining the old source ID would fork one
            # logical source the next time add_document derives its new-owner ID.
            documents = conn.execute("SELECT * FROM fm_v2_documents WHERE owner_id=?", (old_owner,)).fetchall()
            chunks = conn.execute(
                "SELECT c.*,d.parser_version FROM fm_v2_chunks c "
                "JOIN fm_v2_documents d ON d.owner_id=c.owner_id "
                "AND d.document_id=c.document_id WHERE c.owner_id=?",
                (old_owner,),
            ).fetchall()
            source_keys = {
                (row["source_id"], int(row["source_revision"])) for row in documents
            }
            sources = [
                conn.execute(
                    "SELECT * FROM fm_v2_sources "
                    "WHERE owner_id=? AND source_id=? AND source_revision=?",
                    (old_owner, source_id, source_revision),
                ).fetchone()
                for source_id, source_revision in source_keys
            ]
            source_map: Dict[tuple[str, int], str] = {}
            for row in sources:
                if row is None:
                    raise sqlite3.IntegrityError("RAG document has no source")
                new_uri = _rewrite_source_uri(row["source_uri"], path_map, path_prefixes)
                new_source_id = _source_id(
                    new_owner, row["workspace_key"], row["project_key"], new_uri
                )
                source_map[(row["source_id"], int(row["source_revision"]))] = new_source_id
                conn.execute("INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,forget_state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (new_owner, new_source_id, row["workspace_key"], row["project_key"], new_uri, row["source_revision"], row["content_hash"], row["source_type"], row["forget_state"], row["created_at"]))
            document_map: Dict[str, str] = {}
            for row in documents:
                new_source_id = source_map[
                    (row["source_id"], int(row["source_revision"]))
                ]
                new_document_id = "doc_" + _hash(
                    f"{new_owner}\0{new_source_id}\0{row['source_revision']}\0"
                    f"{row['content_hash']}\0{row['parser_version']}"
                )[:32]
                document_map[row["document_id"]] = new_document_id
                conn.execute("INSERT INTO fm_v2_documents(owner_id,document_id,source_id,source_revision,title,parser_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?)", (new_owner, new_document_id, new_source_id, row["source_revision"], row["title"], row["parser_version"], row["content_hash"], row["created_at"]))
            for row in chunks:
                new_document_id = document_map[row["document_id"]]
                new_chunk_id = "chunk_" + _hash(
                    f"{new_owner}\0{new_document_id}\0{row['parser_version']}\0"
                    f"{row['ordinal']}\0{row['locator_json']}\0{row['content_hash']}"
                )[:32]
                conn.execute("INSERT INTO fm_v2_chunks(owner_id,chunk_id,document_id,ordinal,text,content_hash,locator_json,created_at) VALUES (?,?,?,?,?,?,?,?)", (new_owner, new_chunk_id, new_document_id, row["ordinal"], row["text"], row["content_hash"], row["locator_json"], row["created_at"]))
                if self._fts_available:
                    conn.execute("INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) VALUES (?,?,?)", (new_owner, new_chunk_id, row["text"]))
            self._delete_documents(
                conn, old_owner, [row["document_id"] for row in documents]
            )
            self._delete_unreferenced_sources(conn, old_owner, source_keys)
            # Derived identities include the owner and cannot be transferred.
            # Drop only the old owner's RAG generations; the new owner gets a
            # fresh FTS pointer and may explicitly build fresh embeddings.
            conn.execute(
                "DELETE FROM fm_v2_index_pointers WHERE owner_id=?", (old_owner,)
            )
            conn.execute(
                "DELETE FROM fm_v2_derived_generations WHERE owner_id=?",
                (old_owner,),
            )
            conn.execute(
                "DELETE FROM fm_v2_index_publications WHERE owner_id=?",
                (old_owner,),
            )
            if self._fts_available:
                old_scopes = {
                    (row["workspace_key"], row["project_key"]) for row in sources
                }
                for workspace, project in old_scopes:
                    self._publish_inline_fts(conn, new_owner, workspace, project)
        return {"success": True, "updated_count": len(documents), "chunks": len(chunks), "sources": len(sources)}

    def purge_owner(self, owner: str) -> Dict[str, Any]:
        """Remove only one owner's RAG projection, preserving other v2 data."""
        owner = _owner(owner)
        with self._connect() as conn:
            documents = conn.execute(
                "SELECT document_id,source_id,source_revision "
                "FROM fm_v2_documents WHERE owner_id=?",
                (owner,),
            ).fetchall()
            source_keys = {
                (row["source_id"], int(row["source_revision"])) for row in documents
            }
            chunks = conn.execute(
                "SELECT count(*) FROM fm_v2_chunks WHERE owner_id=?", (owner,)
            ).fetchone()[0]
            self._delete_documents(
                conn, owner, [row["document_id"] for row in documents]
            )
            self._delete_unreferenced_sources(conn, owner, source_keys)
            conn.execute(
                "DELETE FROM fm_v2_index_pointers WHERE owner_id=?", (owner,)
            )
            conn.execute(
                "DELETE FROM fm_v2_derived_generations WHERE owner_id=?", (owner,)
            )
            conn.execute(
                "DELETE FROM fm_v2_index_publications WHERE owner_id=?", (owner,)
            )
        return {
            "success": True,
            "removed_count": len(documents),
            "chunks": chunks,
            "sources": len(source_keys),
        }

    def get_stats(self, owner: Optional[str] = None) -> Dict[str, Any]:
        if not self.healthy:
            return {"healthy": False, "backend": self.backend}
        with self._connect() as conn:
            if owner:
                owner = _owner(owner)
                docs = conn.execute(
                    "SELECT count(*) FROM fm_v2_documents WHERE owner_id=?", (owner,)
                ).fetchone()[0]
                chunks = conn.execute(
                    "SELECT count(*) FROM fm_v2_chunks WHERE owner_id=?", (owner,)
                ).fetchone()[0]
                generation_rows = conn.execute(
                    "SELECT state,count(*) FROM fm_v2_derived_generations "
                    "WHERE owner_id=? GROUP BY state",
                    (owner,),
                ).fetchall()
                scopes = conn.execute(
                    "SELECT owner_id,workspace_key,project_key FROM fm_v2_sources "
                    "WHERE owner_id=? UNION SELECT owner_id,workspace_key,project_key "
                    "FROM fm_v2_derived_generations WHERE owner_id=? "
                    "ORDER BY workspace_key,project_key",
                    (owner, owner),
                ).fetchall()
                publications = conn.execute(
                    "SELECT count(*) FROM fm_v2_index_publications WHERE owner_id=?",
                    (owner,),
                ).fetchone()[0]
            else:
                docs = conn.execute("SELECT count(*) FROM fm_v2_documents").fetchone()[0]
                chunks = conn.execute("SELECT count(*) FROM fm_v2_chunks").fetchone()[0]
                generation_rows = conn.execute(
                    "SELECT state,count(*) FROM fm_v2_derived_generations GROUP BY state"
                ).fetchall()
                scopes = conn.execute(
                    "SELECT owner_id,workspace_key,project_key FROM fm_v2_sources "
                    "UNION SELECT owner_id,workspace_key,project_key "
                    "FROM fm_v2_derived_generations "
                    "ORDER BY owner_id,workspace_key,project_key"
                ).fetchall()
                publications = conn.execute(
                    "SELECT count(*) FROM fm_v2_index_publications"
                ).fetchone()[0]
            indexes = [
                {
                    "owner": row["owner_id"],
                    "logical_space": logical_space,
                    "workspace_id": row["workspace_key"],
                    "project_id": row["project_key"],
                    **self._pointer_health(
                        conn,
                        row["owner_id"],
                        logical_space,
                        row["workspace_key"],
                        row["project_key"],
                    ),
                }
                for row in scopes
                for logical_space in (FTS_LOGICAL_SPACE, VECTOR_LOGICAL_SPACE)
            ]
        return {
            "healthy": True,
            "backend": self.backend,
            "documents": docs,
            "chunks": chunks,
            "collection_count": chunks,
            "fts_available": self._fts_available,
            "generation_states": {row[0]: row[1] for row in generation_rows},
            "publication_count": publications,
            "indexes": indexes,
            "index_health": {
                state: sum(1 for index in indexes if index["health"] == state)
                for state in ("current", "lagging", "invalid", "missing")
            },
        }
