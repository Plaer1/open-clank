use async_trait::async_trait;
use rusqlite::{params, Connection, OptionalExtension};
use std::{sync::Mutex, time::Duration};
use tracing::{info, warn};

use crate::record::*;
use crate::store::*;

/// Schema version stamped into `PRAGMA user_version`. Every change is one
/// numbered block in `init_tables`; DBs from before versioning report 0 and
/// flow through the v1 block as a no-op (IF NOT EXISTS). The same chain
/// doubles as the upgrade engine for importing out-of-date DBs.
/// v1 = baseline tiers (curated/raw/facts + FTS shadows).
/// v2 = graph overlay (graph_nodes / graph_edges / graph_cues + cue FTS).
/// v3 = opt-in code graph bookkeeping (code_files incremental index state).
/// v4 = raw-turn owner scope for tenant-safe transcript retrieval.
/// v5 = admission candidates, quarantine, quality metrics, and graph scope.
/// v6 = fail-closed graph namespaces and durable database identity.
pub const SCHEMA_VERSION: i64 = 6;

pub struct SqliteStore {
    pub(crate) conn: Mutex<Connection>,
    embedding_dim: usize,
    capabilities: StoreCapabilities,
}

impl SqliteStore {
    pub fn new(path: &str, embedding_dim: usize) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open(path)?;
        conn.busy_timeout(Duration::from_secs(30))?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;

        let store = Self {
            conn: Mutex::new(conn),
            embedding_dim,
            capabilities: StoreCapabilities {
                vector_search: true,
                fts_search: true,
                native_hybrid: false,
                sparse_vectors: false,
            },
        };
        store.init_tables()?;
        Ok(store)
    }

    pub fn memory(embedding_dim: usize) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        let store = Self {
            conn: Mutex::new(conn),
            embedding_dim,
            capabilities: StoreCapabilities {
                vector_search: true,
                fts_search: true,
                native_hybrid: false,
                sparse_vectors: false,
            },
        };
        store.init_tables()?;
        Ok(store)
    }

    pub(crate) fn init_tables(&self) -> Result<(), rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN EXCLUSIVE;")?;
        let migration = (|| -> Result<(), rusqlite::Error> {
            let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;

            if version < 1 {
                Self::baseline_schema(&conn)?;
                conn.pragma_update(None, "user_version", 1)?;
            }
            if version < 2 {
                Self::graph_schema(&conn)?;
                conn.pragma_update(None, "user_version", 2)?;
            }
            if version < 3 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS code_files (
                    codebase TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    blake3 TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    symbol_count INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL,
                    PRIMARY KEY (codebase, rel_path)
                );",
                )?;
                conn.pragma_update(None, "user_version", 3)?;
            }
            if version < 4 {
                let has_owner = conn
                    .prepare("PRAGMA table_info(raw)")?
                    .query_map([], |row| row.get::<_, String>(1))?
                    .filter_map(|name| name.ok())
                    .any(|name| name == "owner");
                if !has_owner {
                    conn.execute_batch("ALTER TABLE raw ADD COLUMN owner TEXT;")?;
                }
                conn.pragma_update(None, "user_version", 4)?;
            }
            if version < 5 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    importance_score REAL NOT NULL,
                    owner TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    workspace_path TEXT,
                    session_id TEXT NOT NULL DEFAULT '',
                    turn_id TEXT NOT NULL,
                    raw_evidence_ids TEXT NOT NULL DEFAULT '[]',
                    evidence_role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    accepted_curated_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_scope_status
                    ON candidates(owner, workspace_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS memory_quarantine (
                    id TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    original_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    owner TEXT,
                    workspace_id TEXT NOT NULL DEFAULT 'global',
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL,
                    UNIQUE(tier, original_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_quarantine_scope
                    ON memory_quarantine(owner, workspace_id, quarantined_at);
                CREATE TABLE IF NOT EXISTS memory_metrics (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );",
                )?;
                for (table, column, definition) in [
                    ("graph_nodes", "owner", "TEXT"),
                    (
                        "graph_nodes",
                        "workspace_id",
                        "TEXT NOT NULL DEFAULT 'global'",
                    ),
                    ("graph_nodes", "candidate_id", "TEXT"),
                    ("graph_nodes", "status", "TEXT NOT NULL DEFAULT 'active'"),
                    ("graph_edges", "owner", "TEXT"),
                    (
                        "graph_edges",
                        "workspace_id",
                        "TEXT NOT NULL DEFAULT 'global'",
                    ),
                    ("graph_edges", "candidate_id", "TEXT"),
                    ("graph_edges", "status", "TEXT NOT NULL DEFAULT 'active'"),
                    ("graph_cues", "owner", "TEXT"),
                    (
                        "graph_cues",
                        "workspace_id",
                        "TEXT NOT NULL DEFAULT 'global'",
                    ),
                    ("graph_cues", "candidate_id", "TEXT"),
                    ("graph_cues", "status", "TEXT NOT NULL DEFAULT 'active'"),
                    ("facts", "owner", "TEXT"),
                    ("facts", "workspace_id", "TEXT NOT NULL DEFAULT 'global'"),
                    ("facts", "candidate_id", "TEXT"),
                    ("facts", "status", "TEXT NOT NULL DEFAULT 'active'"),
                ] {
                    if !Self::has_column(&conn, table, column)? {
                        conn.execute_batch(&format!(
                            "ALTER TABLE {table} ADD COLUMN {column} {definition};"
                        ))?;
                    }
                }
                conn.pragma_update(None, "user_version", 5)?;
            }
            if version < 6 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS fm_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                 );
                 INSERT OR IGNORE INTO fm_meta(key,value)
                    VALUES ('database_id', lower(hex(randomblob(16))));
                 CREATE INDEX IF NOT EXISTS idx_graph_nodes_scope
                    ON graph_nodes(owner,workspace_id,status);
                 CREATE INDEX IF NOT EXISTS idx_graph_edges_scope
                    ON graph_edges(owner,workspace_id,status);
                 CREATE INDEX IF NOT EXISTS idx_graph_cues_scope
                    ON graph_cues(owner,workspace_id,status);
                 CREATE INDEX IF NOT EXISTS idx_facts_scope
                    ON facts(owner,workspace_id,status);
                 UPDATE graph_nodes SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';
                 UPDATE graph_edges SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';
                 UPDATE graph_cues SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';
                 UPDATE facts SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';",
                )?;
                conn.pragma_update(None, "user_version", 6)?;
            }
            Ok(())
        })();
        if let Err(error) = migration {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err(error);
        }
        conn.execute_batch("COMMIT;")?;

        // FTS virtual tables stay OUTSIDE the version gate: creation is
        // tolerant (environments without FTS5 only warn), so it must retry
        // on every open rather than being skipped forever after one stamp.
        Self::fts_schema(&conn);
        Self::graph_fts_sync(&conn);

        Ok(())
    }

    fn has_column(conn: &Connection, table: &str, column: &str) -> Result<bool, rusqlite::Error> {
        let mut statement = conn.prepare(&format!("PRAGMA table_info({table})"))?;
        let found = statement
            .query_map([], |row| row.get::<_, String>(1))?
            .filter_map(Result::ok)
            .any(|name| name == column);
        Ok(found)
    }

    fn graph_fts_sync(conn: &Connection) {
        let result = conn.execute_batch(
            "CREATE TRIGGER IF NOT EXISTS graph_cues_fts_insert
                 AFTER INSERT ON graph_cues WHEN new.status = 'active' BEGIN
                   INSERT INTO graph_cues_fts(cue, node_id) VALUES (new.cue, new.node_id);
                 END;
             CREATE TRIGGER IF NOT EXISTS graph_cues_fts_delete
                 AFTER DELETE ON graph_cues BEGIN
                   DELETE FROM graph_cues_fts WHERE cue = old.cue AND node_id = old.node_id;
                 END;
             CREATE TRIGGER IF NOT EXISTS graph_cues_fts_status
                 AFTER UPDATE OF status ON graph_cues BEGIN
                   DELETE FROM graph_cues_fts WHERE cue = old.cue AND node_id = old.node_id;
                   INSERT INTO graph_cues_fts(cue, node_id)
                     SELECT new.cue, new.node_id WHERE new.status = 'active';
                 END;
             DELETE FROM graph_cues_fts;
             INSERT INTO graph_cues_fts(cue, node_id)
                 SELECT c.cue, c.node_id FROM graph_cues c
                 JOIN graph_nodes n ON n.id = c.node_id
                 WHERE c.status = 'active' AND n.status = 'active';",
        );
        if let Err(error) = result {
            warn!("graph cue FTS synchronization unavailable: {error}");
        }
    }

    fn baseline_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS curated (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'episodic',
                priority INTEGER NOT NULL DEFAULT 50,
                trust_score REAL NOT NULL DEFAULT 0.50,
                confidence_score REAL NOT NULL DEFAULT 0.6,
                importance_score REAL NOT NULL DEFAULT 0.5,
                scene_name TEXT,
                source TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'auto_extracted',
                owner TEXT,
                workspace_id TEXT NOT NULL DEFAULT 'global',
                session_key TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                source_message_ids TEXT NOT NULL DEFAULT '[]',
                timestamps TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                last_accessed_at TEXT,
                exempt_from_decay INTEGER NOT NULL DEFAULT 0,
                exempt_from_dedup INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT 'null',
                workspace_path TEXT,
                embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS raw (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                session_key TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT 'global',
                owner TEXT,
                recorded_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT 'null',
                workspace_path TEXT,
                embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                entities TEXT NOT NULL DEFAULT '[]',
                trust_score REAL NOT NULL DEFAULT 0.50,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                embedding BLOB
            );",
        )?;
        Ok(())
    }

    fn graph_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS graph_nodes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT,
                name TEXT NOT NULL,
                norm_name TEXT NOT NULL,
                layer TEXT NOT NULL DEFAULT 'semantic',
                ref_table TEXT,
                ref_id TEXT,
                trust INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_graph_nodes_norm ON graph_nodes(norm_name);
            CREATE TABLE IF NOT EXISTS graph_cues (
                cue TEXT NOT NULL,
                node_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'extracted',
                created_at TEXT NOT NULL,
                PRIMARY KEY (cue, node_id)
            );
            CREATE TABLE IF NOT EXISTS graph_edges (
                id TEXT PRIMARY KEY,
                src_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                dst_id TEXT NOT NULL,
                fact_id TEXT,
                weight REAL NOT NULL DEFAULT 1.0,
                traversal_count INTEGER NOT NULL DEFAULT 0,
                trust INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE (src_id, tag, dst_id)
            );
            CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_id);
            CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_id);",
        )?;
        // Cue FTS mirrors the tier shadows: tolerant creation, manual sync.
        if let Err(e) = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS graph_cues_fts USING fts5(
                cue, node_id UNINDEXED
            );",
        ) {
            warn!("FTS5 not available for graph_cues: {e}");
        }
        Ok(())
    }

    fn fts_schema(conn: &Connection) {
        let fts_curated = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS curated_fts USING fts5(
                content, scene_name, tags, workspace_id,
                content='curated', content_rowid='rowid'
            );",
        );
        if let Err(e) = fts_curated {
            warn!("FTS5 not available for curated: {e}");
        }

        let fts_raw = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
                content, workspace_id,
                content='raw', content_rowid='rowid'
            );",
        );
        if let Err(e) = fts_raw {
            warn!("FTS5 not available for raw: {e}");
        }

        let fts_facts = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                content, entities,
                content='facts', content_rowid='rowid'
            );",
        );
        if let Err(e) = fts_facts {
            warn!("FTS5 not available for facts: {e}");
        }
    }

    fn record_to_row(r: &MemoryRecord) -> Vec<rusqlite::types::Value> {
        vec![
            rusqlite::types::Value::Text(r.id.clone()),
            rusqlite::types::Value::Text(r.content.clone()),
            rusqlite::types::Value::Text(format!("{:?}", r.kind).to_lowercase()),
            rusqlite::types::Value::Integer(r.priority as i64),
            rusqlite::types::Value::Real(r.trust_score as f64),
            rusqlite::types::Value::Real(r.confidence_score as f64),
            rusqlite::types::Value::Real(r.importance_score as f64),
            r.scene_name
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
            rusqlite::types::Value::Text(r.source.clone()),
            rusqlite::types::Value::Text(format!("{:?}", r.source_type).to_lowercase()),
            r.owner
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
            rusqlite::types::Value::Text(r.workspace_id.clone()),
            rusqlite::types::Value::Text(r.session_key.clone()),
            rusqlite::types::Value::Text(r.session_id.clone()),
            rusqlite::types::Value::Text(serde_json::to_string(&r.tags).unwrap_or_default()),
            rusqlite::types::Value::Text(
                serde_json::to_string(&r.source_message_ids).unwrap_or_default(),
            ),
            rusqlite::types::Value::Text(serde_json::to_string(&r.timestamps).unwrap_or_default()),
            rusqlite::types::Value::Text(r.created_at.clone()),
            rusqlite::types::Value::Text(r.updated_at.clone()),
            rusqlite::types::Value::Integer(r.archived as i64),
            r.last_accessed_at
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
            rusqlite::types::Value::Integer(r.exempt_from_decay as i64),
            rusqlite::types::Value::Integer(r.exempt_from_dedup as i64),
            rusqlite::types::Value::Text(serde_json::to_string(&r.metadata).unwrap_or_default()),
            r.workspace_path
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
        ]
    }

    fn row_to_record(row: &rusqlite::Row) -> rusqlite::Result<MemoryRecord> {
        let kind_str: String = row.get(2)?;
        let kind = match kind_str.as_str() {
            "persona" => MemoryKind::Persona,
            "episodic" => MemoryKind::Episodic,
            "instruction" => MemoryKind::Instruction,
            "fact" => MemoryKind::Fact,
            "fabric" => MemoryKind::Fabric,
            "wiki" => MemoryKind::Wiki,
            "raw" => MemoryKind::Raw,
            _ => MemoryKind::Episodic,
        };
        let source_type_str: String = row.get(9)?;
        let source_type = match source_type_str.as_str() {
            "human" => SourceType::Human,
            "procedural" => SourceType::Procedural,
            "ai" => SourceType::Ai,
            _ => SourceType::AutoExtracted,
        };
        let tags_str: String = row.get(14)?;
        let tags: Vec<String> = serde_json::from_str(&tags_str).unwrap_or_default();
        let smids_str: String = row.get(15)?;
        let source_message_ids: Vec<String> = serde_json::from_str(&smids_str).unwrap_or_default();
        let ts_str: String = row.get(16)?;
        let timestamps: Vec<String> = serde_json::from_str(&ts_str).unwrap_or_default();
        let meta_str: String = row.get(23)?;
        let metadata: serde_json::Value = serde_json::from_str(&meta_str).unwrap_or_default();

        Ok(MemoryRecord {
            id: row.get(0)?,
            content: row.get(1)?,
            kind,
            priority: row.get::<_, i64>(3)? as i32,
            trust_score: row.get::<_, f64>(4)? as f32,
            confidence_score: row.get::<_, f64>(5)? as f32,
            importance_score: row.get::<_, f64>(6)? as f32,
            scene_name: row.get(7)?,
            source: row.get(8)?,
            source_type,
            owner: row.get(10)?,
            workspace_id: row.get(11)?,
            workspace_path: row.get(24)?,
            session_key: row.get(12)?,
            session_id: row.get(13)?,
            tags,
            source_message_ids,
            timestamps,
            created_at: row.get(17)?,
            updated_at: row.get(18)?,
            archived: row.get::<_, i64>(19)? != 0,
            last_accessed_at: row.get(20)?,
            exempt_from_decay: row.get::<_, i64>(21)? != 0,
            exempt_from_dedup: row.get::<_, i64>(22)? != 0,
            metadata,
        })
    }

    fn raw_row_to_record(row: &rusqlite::Row) -> rusqlite::Result<RawTurn> {
        let meta_str: String = row.get(8)?;
        Ok(RawTurn {
            id: row.get(0)?,
            role: row.get(1)?,
            content: row.get(2)?,
            session_key: row.get(3)?,
            session_id: row.get(4)?,
            workspace_id: row.get(5)?,
            owner: row.get(6)?,
            workspace_path: row.get(9)?,
            recorded_at: row.get(7)?,
            metadata: serde_json::from_str(&meta_str).unwrap_or_default(),
        })
    }

    fn candidate_row(row: &rusqlite::Row) -> rusqlite::Result<CandidateRecord> {
        let kind = match row.get::<_, String>(2)?.as_str() {
            "persona" => MemoryKind::Persona,
            "instruction" => MemoryKind::Instruction,
            "fact" => MemoryKind::Fact,
            "fabric" => MemoryKind::Fabric,
            "wiki" => MemoryKind::Wiki,
            "raw" => MemoryKind::Raw,
            _ => MemoryKind::Episodic,
        };
        let status = match row.get::<_, String>(15)?.as_str() {
            "accepted" => CandidateStatus::Accepted,
            "rejected" => CandidateStatus::Rejected,
            "quarantined" => CandidateStatus::Quarantined,
            _ => CandidateStatus::Pending,
        };
        Ok(CandidateRecord {
            id: row.get(0)?,
            content: row.get(1)?,
            kind,
            confidence_score: row.get::<_, f64>(3)? as f32,
            importance_score: row.get::<_, f64>(4)? as f32,
            owner: row.get(5)?,
            workspace_id: row.get(6)?,
            workspace_path: row.get(7)?,
            session_id: row.get(8)?,
            turn_id: row.get(9)?,
            raw_evidence_ids: serde_json::from_str(&row.get::<_, String>(10)?).unwrap_or_default(),
            evidence_role: row.get(11)?,
            source: row.get(12)?,
            source_event_id: row.get(13)?,
            dedup_key: row.get(14)?,
            status,
            reason: row.get(16)?,
            accepted_curated_id: row.get(17)?,
            created_at: row.get(18)?,
            updated_at: row.get(19)?,
        })
    }

    pub fn insert_candidate(&self, candidate: &CandidateRecord) -> Result<bool, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let inserted = conn.execute(
            "INSERT OR IGNORE INTO candidates (
                id, content, kind, confidence_score, importance_score, owner,
                workspace_id, workspace_path, session_id, turn_id,
                raw_evidence_ids, evidence_role, source, source_event_id,
                dedup_key, status, reason, accepted_curated_id, created_at, updated_at
             ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20)",
            params![
                candidate.id,
                candidate.content,
                format!("{:?}", candidate.kind).to_lowercase(),
                candidate.confidence_score,
                candidate.importance_score,
                candidate.owner,
                candidate.workspace_id,
                candidate.workspace_path,
                candidate.session_id,
                candidate.turn_id,
                serde_json::to_string(&candidate.raw_evidence_ids).unwrap_or_else(|_| "[]".into()),
                candidate.evidence_role,
                candidate.source,
                candidate.source_event_id,
                candidate.dedup_key,
                format!("{:?}", candidate.status).to_lowercase(),
                candidate.reason,
                candidate.accepted_curated_id,
                candidate.created_at,
                candidate.updated_at,
            ],
        )?;
        Ok(inserted > 0)
    }

    pub fn candidate_by_dedup(
        &self,
        dedup_key: &str,
    ) -> Result<Option<CandidateRecord>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut statement = conn.prepare(
            "SELECT id,content,kind,confidence_score,importance_score,owner,
                    workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,
                    evidence_role,source,source_event_id,dedup_key,status,reason,
                    accepted_curated_id,created_at,updated_at
             FROM candidates WHERE dedup_key = ?1",
        )?;
        let mut rows = statement.query(params![dedup_key])?;
        rows.next()?.map(Self::candidate_row).transpose()
    }

    pub fn candidate_by_id(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<CandidateRecord>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut statement = conn.prepare(
            "SELECT id,content,kind,confidence_score,importance_score,owner,
                    workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,
                    evidence_role,source,source_event_id,dedup_key,status,reason,
                    accepted_curated_id,created_at,updated_at
             FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3",
        )?;
        let mut rows = statement.query(params![id, owner, workspace_id])?;
        rows.next()?.map(Self::candidate_row).transpose()
    }

    pub fn list_candidates(
        &self,
        owner: Option<&str>,
        workspace_id: Option<&str>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<CandidateRecord>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut statement = conn.prepare(
            "SELECT id,content,kind,confidence_score,importance_score,owner,
                    workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,
                    evidence_role,source,source_event_id,dedup_key,status,reason,
                    accepted_curated_id,created_at,updated_at
             FROM candidates
             WHERE owner = ?1 AND workspace_id = ?2
               AND (?3 IS NULL OR status = ?3)
             ORDER BY updated_at DESC LIMIT ?4",
        )?;
        let rows = statement.query_map(
            params![owner, workspace_id, status, limit as i64],
            Self::candidate_row,
        )?;
        rows.collect()
    }

    pub fn set_candidate_status(
        &self,
        id: &str,
        status: CandidateStatus,
        reason: &str,
        curated_id: Option<&str>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<bool, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let updated = conn.execute(
            "UPDATE candidates SET status = ?1, reason = ?2,
                    accepted_curated_id = ?3, updated_at = ?4
             WHERE id = ?5 AND owner = ?6 AND workspace_id = ?7",
            params![
                format!("{:?}", status).to_lowercase(),
                reason,
                curated_id,
                chrono::Utc::now().to_rfc3339(),
                id,
                owner,
                workspace_id,
            ],
        )?;
        Ok(updated > 0)
    }

    pub fn metric_add(&self, name: &str, amount: usize) {
        let conn = self.conn.lock().unwrap();
        let _ = conn.execute(
            "INSERT INTO memory_metrics(name,value) VALUES (?1,?2)
             ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            params![name, amount as i64],
        );
    }

    pub fn quality_status(&self) -> Result<serde_json::Value, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let count = |sql: &str| conn.query_row(sql, [], |row| row.get::<_, i64>(0));
        let cue_base = count("SELECT count(*) FROM graph_cues WHERE status='active'")?;
        let cue_fts = count("SELECT count(*) FROM graph_cues_fts")?;
        let cue_orphans = count(
            "SELECT count(*) FROM graph_cues c LEFT JOIN graph_nodes n ON n.id=c.node_id
             WHERE c.status='active' AND (n.id IS NULL OR n.status <> 'active')",
        )?;
        let mut metrics = serde_json::Map::new();
        let mut statement = conn.prepare("SELECT name,value FROM memory_metrics ORDER BY name")?;
        for row in statement.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })? {
            let (name, value) = row?;
            metrics.insert(name, serde_json::Value::from(value));
        }
        let database_id: String = conn.query_row(
            "SELECT value FROM fm_meta WHERE key='database_id'",
            [],
            |row| row.get(0),
        )?;
        let schema_version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        Ok(serde_json::json!({
            "database_id": database_id,
            "schema_version": schema_version,
            "raw": count("SELECT count(*) FROM raw")?,
            "candidates": count("SELECT count(*) FROM candidates")?,
            "curated": count("SELECT count(*) FROM curated WHERE archived=0")?,
            "quarantined": count("SELECT count(*) FROM memory_quarantine")?,
            "graph": {
                "cues": cue_base,
                "cue_fts": cue_fts,
                "orphan_cues": cue_orphans,
                "integrity_ok": cue_base == cue_fts && cue_orphans == 0,
            },
            "metrics": metrics,
        }))
    }

    pub fn database_identity(&self) -> Result<(String, i64), rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let id = conn.query_row(
            "SELECT value FROM fm_meta WHERE key='database_id'",
            [],
            |row| row.get(0),
        )?;
        let version = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        Ok((id, version))
    }

    /// Index-card digest of the bank for (owner, workspace ∪ global):
    /// counts, pinned headlines, top relationship clusters, newest cue
    /// topics. Bounded by item counts, never token budgets. Computed
    /// directly — every query is a covered scan over scoped indexes, so
    /// there is no cache to invalidate and writes are visible immediately.
    pub fn digest(
        &self,
        owner: &str,
        workspace_id: &str,
        include_global: bool,
    ) -> Result<serde_json::Value, String> {
        const PINNED_MAX: usize = 5;
        const CLUSTERS_MAX: usize = 6;
        const RECENT_MAX: usize = 5;

        if owner.trim().is_empty() {
            return Err("owner is required".into());
        }
        let conn = self.conn.lock().unwrap();
        let scope_params = params![owner, workspace_id, include_global];

        let mut by_kind = serde_json::Map::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT kind, COUNT(*) FROM curated
                     WHERE archived = 0 AND owner = ?1
                       AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))
                     GROUP BY kind",
                )
                .map_err(|error| error.to_string())?;
            let rows = stmt
                .query_map(scope_params, |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
                })
                .map_err(|error| error.to_string())?;
            for row in rows {
                let (kind, count) = row.map_err(|error| error.to_string())?;
                by_kind.insert(kind, count.into());
            }
        }

        let scoped_count = |sql: &str| -> Result<i64, String> {
            conn.query_row(sql, scope_params, |row| row.get(0))
                .map_err(|error| error.to_string())
        };
        let curated_total = scoped_count(
            "SELECT COUNT(*) FROM curated
             WHERE archived = 0 AND owner = ?1
               AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))",
        )?;
        let raw_total = scoped_count(
            "SELECT COUNT(*) FROM raw
             WHERE owner = ?1
               AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))",
        )?;
        let candidates_pending = scoped_count(
            "SELECT COUNT(*) FROM candidates
             WHERE status = 'pending' AND owner = ?1
               AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))",
        )?;

        let mut pinned = Vec::new();
        {
            // The trust classifier downstream keys on id/source_type/pinned;
            // full content rides so endorsed guidance can render whole
            // (render-time budgets cap it, not SQL truncation). The explicit
            // `pinned` flag distinguishes a user pin from the persona-kind
            // auto-inclusion below — array membership alone is NOT an
            // endorsement.
            let mut stmt = conn
                .prepare(
                    "SELECT id, substr(content, 1, 80), content, kind, source_type,
                            COALESCE(json_extract(metadata, '$.pinned'), 0) IN (1, 'true')
                     FROM curated
                     WHERE archived = 0 AND owner = ?1
                       AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))
                       AND (kind = 'persona'
                            OR COALESCE(json_extract(metadata, '$.pinned'), 0) IN (1, 'true'))
                     ORDER BY updated_at DESC
                     LIMIT ?4",
                )
                .map_err(|error| error.to_string())?;
            let rows = stmt
                .query_map(
                    params![owner, workspace_id, include_global, PINNED_MAX as i64],
                    |row| {
                        Ok(serde_json::json!({
                            "id": row.get::<_, String>(0)?,
                            "headline": row.get::<_, String>(1)?,
                            "content": row.get::<_, String>(2)?,
                            "kind": row.get::<_, String>(3)?,
                            "source_type": row.get::<_, String>(4)?,
                            "pinned": row.get::<_, bool>(5)?,
                        }))
                    },
                )
                .map_err(|error| error.to_string())?;
            for row in rows {
                pinned.push(row.map_err(|error| error.to_string())?);
            }
        }

        let mut clusters = Vec::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT tag, COUNT(*), MAX(last_seen) FROM graph_edges
                     WHERE status = 'active' AND owner = ?1
                       AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))
                     GROUP BY tag
                     ORDER BY COUNT(*) DESC, MAX(last_seen) DESC
                     LIMIT ?4",
                )
                .map_err(|error| error.to_string())?;
            let rows = stmt
                .query_map(
                    params![owner, workspace_id, include_global, CLUSTERS_MAX as i64],
                    |row| {
                        Ok(serde_json::json!({
                            "label": row.get::<_, String>(0)?,
                            "size": row.get::<_, i64>(1)?,
                            "last_touched": row.get::<_, String>(2)?,
                        }))
                    },
                )
                .map_err(|error| error.to_string())?;
            for row in rows {
                clusters.push(row.map_err(|error| error.to_string())?);
            }
        }

        let mut recent = Vec::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT cue, MAX(created_at) FROM graph_cues
                     WHERE status = 'active' AND owner = ?1
                       AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))
                     GROUP BY cue
                     ORDER BY MAX(created_at) DESC
                     LIMIT ?4",
                )
                .map_err(|error| error.to_string())?;
            let rows = stmt
                .query_map(
                    params![owner, workspace_id, include_global, RECENT_MAX as i64],
                    |row| {
                        Ok(serde_json::json!({
                            "topic": row.get::<_, String>(0)?,
                            "at": row.get::<_, String>(1)?,
                        }))
                    },
                )
                .map_err(|error| error.to_string())?;
            for row in rows {
                recent.push(row.map_err(|error| error.to_string())?);
            }
        }

        Ok(serde_json::json!({
            "counts": {
                "by_kind": by_kind,
                "by_tier": {
                    "raw": raw_total,
                    "curated": curated_total,
                    "candidates_pending": candidates_pending,
                },
                "candidates_pending": candidates_pending,
            },
            "pinned": pinned,
            "clusters": clusters,
            "recent": recent,
            "generated_at": chrono::Utc::now().to_rfc3339(),
        }))
    }

    /// Ledger lookup for authored-file ingest: every curated record whose
    /// metadata marks it as originating from `source_path`, as
    /// (id, content_hash) pairs. The ledger lives in the records themselves,
    /// so ingest is idempotent with no caller-side state.
    pub fn authored_records(
        &self,
        owner: &str,
        workspace_id: &str,
        source_path: &str,
    ) -> Result<Vec<(String, String)>, String> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT id, COALESCE(json_extract(metadata, '$.content_hash'), '')
                 FROM curated
                 WHERE owner = ?1 AND workspace_id = ?2
                   AND json_extract(metadata, '$.authored_path') = ?3",
            )
            .map_err(|error| error.to_string())?;
        let rows = stmt
            .query_map(params![owner, workspace_id, source_path], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|error| error.to_string())?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())
    }

    pub fn owner_counts(&self, owner: &str) -> Result<serde_json::Value, String> {
        if owner.trim().is_empty() {
            return Err("owner is required".into());
        }
        let conn = self.conn.lock().unwrap();
        let count = |table: &str| -> Result<i64, String> {
            conn.query_row(
                &format!("SELECT count(*) FROM {table} WHERE owner = ?1"),
                params![owner],
                |row| row.get(0),
            )
            .map_err(|error| error.to_string())
        };
        Ok(serde_json::json!({
            "raw": count("raw")?,
            "candidates": count("candidates")?,
            "curated": count("curated")?,
            "quarantine": count("memory_quarantine")?,
            "facts": count("facts")?,
            "graph_nodes": count("graph_nodes")?,
            "graph_edges": count("graph_edges")?,
            "graph_cues": count("graph_cues")?,
        }))
    }

    pub fn purge_owner(&self, owner: &str) -> Result<serde_json::Value, String> {
        let counts = self.owner_counts(owner)?;
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.transaction().map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM curated_fts WHERE rowid IN (SELECT rowid FROM curated WHERE owner=?1)",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM raw_fts WHERE rowid IN (SELECT rowid FROM raw WHERE owner=?1)",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM facts_fts WHERE rowid IN (SELECT rowid FROM facts WHERE owner=?1)",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        for table in [
            "graph_cues",
            "graph_edges",
            "graph_nodes",
            "facts",
            "candidates",
            "memory_quarantine",
            "raw",
            "curated",
        ] {
            tx.execute(
                &format!("DELETE FROM {table} WHERE owner=?1"),
                params![owner],
            )
            .map_err(|error| error.to_string())?;
        }
        tx.execute(
            "DELETE FROM code_files WHERE codebase LIKE (?1 || char(31) || '%')",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.commit().map_err(|error| error.to_string())?;
        Ok(serde_json::json!({"purged": true, "counts": counts}))
    }

    pub fn rename_owner(&self, owner: &str, new_owner: &str) -> Result<serde_json::Value, String> {
        if owner.trim().is_empty() || new_owner.trim().is_empty() || owner == new_owner {
            return Err("distinct non-empty owner names are required".into());
        }
        let before = self.owner_counts(owner)?;
        let target = self.owner_counts(new_owner)?;
        if target
            .as_object()
            .is_some_and(|counts| counts.values().any(|value| value.as_i64().unwrap_or(0) > 0))
        {
            return Err("target owner already has memory".into());
        }

        let mut conn = self.conn.lock().unwrap();
        let tx = conn.transaction().map_err(|error| error.to_string())?;
        let nodes: Vec<(String, String, String, String)> = {
            let mut statement = tx
                .prepare("SELECT id,kind,name,workspace_id FROM graph_nodes WHERE owner=?1")
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner], |row| {
                    Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<_, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let mut node_ids = Vec::with_capacity(nodes.len());
        for (old_id, kind, name, workspace_id) in nodes {
            let temporary = format!("rename:{old_id}");
            let scope = crate::graph::GraphScope::new(new_owner, workspace_id.clone())
                .map_err(str::to_string)?;
            let new_id = scope.node_id(&kind, &name);
            tx.execute(
                "UPDATE graph_nodes SET id=?1 WHERE id=?2",
                params![temporary, old_id],
            )
            .map_err(|error| error.to_string())?;
            tx.execute(
                "UPDATE graph_edges SET src_id=?1 WHERE src_id=?2",
                params![temporary, old_id],
            )
            .map_err(|error| error.to_string())?;
            tx.execute(
                "UPDATE graph_edges SET dst_id=?1 WHERE dst_id=?2",
                params![temporary, old_id],
            )
            .map_err(|error| error.to_string())?;
            tx.execute(
                "UPDATE graph_cues SET node_id=?1 WHERE node_id=?2",
                params![temporary, old_id],
            )
            .map_err(|error| error.to_string())?;
            node_ids.push((temporary, new_id));
        }
        for (temporary, new_id) in &node_ids {
            tx.execute(
                "UPDATE graph_edges SET src_id=?1 WHERE src_id=?2",
                params![new_id, temporary],
            )
            .map_err(|error| error.to_string())?;
            tx.execute(
                "UPDATE graph_edges SET dst_id=?1 WHERE dst_id=?2",
                params![new_id, temporary],
            )
            .map_err(|error| error.to_string())?;
            tx.execute(
                "UPDATE graph_cues SET node_id=?1 WHERE node_id=?2",
                params![new_id, temporary],
            )
            .map_err(|error| error.to_string())?;
            tx.execute(
                "UPDATE graph_nodes SET id=?1 WHERE id=?2",
                params![new_id, temporary],
            )
            .map_err(|error| error.to_string())?;
        }
        for table in [
            "raw",
            "candidates",
            "curated",
            "memory_quarantine",
            "facts",
            "graph_nodes",
            "graph_edges",
            "graph_cues",
        ] {
            tx.execute(
                &format!("UPDATE {table} SET owner=?1 WHERE owner=?2"),
                params![new_owner, owner],
            )
            .map_err(|error| error.to_string())?;
        }
        tx.execute(
            "UPDATE code_files SET codebase=(?1 || substr(codebase, length(?2)+1))
             WHERE codebase LIKE (?2 || char(31) || '%')",
            params![new_owner, owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute("DELETE FROM graph_cues_fts", [])
            .map_err(|error| error.to_string())?;
        tx.execute(
            "INSERT INTO graph_cues_fts(cue,node_id)
             SELECT cue,node_id FROM graph_cues WHERE status='active'",
            [],
        )
        .map_err(|error| error.to_string())?;
        tx.commit().map_err(|error| error.to_string())?;
        Ok(serde_json::json!({"renamed": true, "from": owner, "to": new_owner, "counts": before}))
    }

    pub fn get_curated_record(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<MemoryRecord>, rusqlite::Error> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Ok(None);
        }
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT id,content,kind,priority,trust_score,confidence_score,
                    importance_score,scene_name,source,source_type,owner,workspace_id,
                    session_key,session_id,tags,source_message_ids,timestamps,created_at,
                    updated_at,archived,last_accessed_at,exempt_from_decay,
                    exempt_from_dedup,metadata,workspace_path
             FROM curated WHERE id=?1 AND owner=?2 AND archived=0
               AND (workspace_id=?3 OR workspace_id='global')",
            params![id, owner, workspace_id],
            Self::row_to_record,
        )
        .optional()
    }

    pub fn list_curated_records(
        &self,
        owner: &str,
        workspace_id: &str,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<MemoryRecord>, rusqlite::Error> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Ok(vec![]);
        }
        let conn = self.conn.lock().unwrap();
        let mut statement = conn.prepare(
            "SELECT id,content,kind,priority,trust_score,confidence_score,
                    importance_score,scene_name,source,source_type,owner,workspace_id,
                    session_key,session_id,tags,source_message_ids,timestamps,created_at,
                    updated_at,archived,last_accessed_at,exempt_from_decay,
                    exempt_from_dedup,metadata,workspace_path
             FROM curated WHERE owner=?1 AND archived=0
               AND (workspace_id=?2 OR workspace_id='global')
             ORDER BY updated_at DESC,id LIMIT ?3 OFFSET ?4",
        )?;
        let rows = statement
            .query_map(
                params![owner, workspace_id, limit as i64, offset as i64],
                Self::row_to_record,
            )?
            .collect();
        rows
    }

    pub fn list_pinned_curated(
        &self,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Vec<MemoryRecord>, rusqlite::Error> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Ok(vec![]);
        }
        let conn = self.conn.lock().unwrap();
        let mut statement = conn.prepare(
            "SELECT id,content,kind,priority,trust_score,confidence_score,
                    importance_score,scene_name,source,source_type,owner,workspace_id,
                    session_key,session_id,tags,source_message_ids,timestamps,created_at,
                    updated_at,archived,last_accessed_at,exempt_from_decay,
                    exempt_from_dedup,metadata,workspace_path
             FROM curated WHERE owner=?1 AND archived=0
               AND (workspace_id=?2 OR workspace_id='global')
               AND json_extract(metadata,'$.pinned')=1
             ORDER BY updated_at DESC,id",
        )?;
        let rows = statement
            .query_map(params![owner, workspace_id], Self::row_to_record)?
            .collect();
        rows
    }

    pub fn record_curated_access(
        &self,
        ids: &[String],
        owner: &str,
        workspace_id: &str,
    ) -> Result<usize, rusqlite::Error> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Ok(0);
        }
        let conn = self.conn.lock().unwrap();
        let now = chrono::Utc::now().to_rfc3339();
        let mut updated = 0;
        for id in ids {
            updated += conn.execute(
                "UPDATE curated SET last_accessed_at=?1,
                   metadata=json_set(
                     CASE WHEN json_valid(metadata) AND json_type(metadata)='object'
                          THEN metadata ELSE '{}' END,
                     '$.uses',COALESCE(json_extract(metadata,'$.uses'),0)+1)
                 WHERE id=?2 AND owner=?3 AND archived=0
                   AND (workspace_id=?4 OR workspace_id='global')",
                params![now, id, owner, workspace_id],
            )?;
        }
        Ok(updated)
    }

    pub fn rebuild_graph_cue_fts(&self) -> Result<serde_json::Value, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "DELETE FROM graph_cues WHERE node_id NOT IN (SELECT id FROM graph_nodes)",
            [],
        )?;
        conn.execute("DELETE FROM graph_cues_fts", [])?;
        conn.execute(
            "INSERT INTO graph_cues_fts(cue,node_id)
             SELECT c.cue,c.node_id FROM graph_cues c
             JOIN graph_nodes n ON n.id=c.node_id
             WHERE c.status='active' AND n.status='active'",
            [],
        )?;
        drop(conn);
        self.quality_status()
    }

    pub fn list_quarantine(
        &self,
        owner: Option<&str>,
        workspace_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<serde_json::Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut statement = conn.prepare(
            "SELECT id,tier,original_id,content,payload,owner,workspace_id,reason,quarantined_at
             FROM memory_quarantine
             WHERE owner = ?1
               AND (workspace_id = ?2 OR workspace_id = 'global')
             ORDER BY quarantined_at DESC LIMIT ?3",
        )?;
        let rows = statement.query_map(params![owner, workspace_id, limit as i64], |row| {
            let payload: String = row.get(4)?;
            Ok(serde_json::json!({
                "id": row.get::<_, String>(0)?,
                "tier": row.get::<_, String>(1)?,
                "original_id": row.get::<_, String>(2)?,
                "content": row.get::<_, String>(3)?,
                "payload": serde_json::from_str::<serde_json::Value>(&payload).unwrap_or_default(),
                "owner": row.get::<_, Option<String>>(5)?,
                "workspace_id": row.get::<_, String>(6)?,
                "reason": row.get::<_, String>(7)?,
                "quarantined_at": row.get::<_, String>(8)?,
            }))
        })?;
        rows.collect()
    }

    pub fn quarantine_legacy_state(
        &self,
        dry_run: bool,
        reason: &str,
    ) -> Result<serde_json::Value, rusqlite::Error> {
        let mut conn = self.conn.lock().unwrap();
        let counts = serde_json::json!({
            "curated": conn.query_row("SELECT count(*) FROM curated WHERE archived=0", [], |row| row.get::<_, i64>(0))?,
            "facts": conn.query_row("SELECT count(*) FROM facts WHERE status='active'", [], |row| row.get::<_, i64>(0))?,
            "graph_nodes": conn.query_row("SELECT count(*) FROM graph_nodes WHERE status='active' AND layer <> 'code'", [], |row| row.get::<_, i64>(0))?,
            "graph_edges": conn.query_row("SELECT count(*) FROM graph_edges WHERE status='active'", [], |row| row.get::<_, i64>(0))?,
            "graph_cues": conn.query_row("SELECT count(*) FROM graph_cues WHERE status='active'", [], |row| row.get::<_, i64>(0))?,
        });
        if dry_run {
            return Ok(serde_json::json!({"dry_run": true, "would_quarantine": counts}));
        }
        let now = chrono::Utc::now().to_rfc3339();
        let tx = conn.transaction()?;
        tx.execute(
            "INSERT OR IGNORE INTO memory_quarantine
             (id,tier,original_id,content,payload,owner,workspace_id,reason,quarantined_at)
             SELECT 'q_curated_'||id,'curated',id,content,
                    json_object('kind',kind,'source',source,'session_id',session_id,
                                'metadata',json(metadata),'created_at',created_at),
                    owner,workspace_id,?1,?2 FROM curated WHERE archived=0",
            params![reason, now],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO memory_quarantine
             (id,tier,original_id,content,payload,owner,workspace_id,reason,quarantined_at)
             SELECT 'q_fact_'||id,'fact',id,content,
                    json_object('entities',json(entities),'trust_score',trust_score,'created_at',created_at),
                    owner,workspace_id,?1,?2 FROM facts WHERE status='active'",
            params![reason, now],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO memory_quarantine
             (id,tier,original_id,content,payload,owner,workspace_id,reason,quarantined_at)
             SELECT 'q_node_'||id,'graph_node',id,COALESCE(label,name),
                    json_object('kind',kind,'name',name,'layer',layer,'ref_table',ref_table,'ref_id',ref_id),
                    owner,workspace_id,?1,?2 FROM graph_nodes
             WHERE status='active' AND layer <> 'code'",
            params![reason, now],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO memory_quarantine
             (id,tier,original_id,content,payload,owner,workspace_id,reason,quarantined_at)
             SELECT 'q_edge_'||id,'graph_edge',id,tag,
                    json_object('src_id',src_id,'dst_id',dst_id,'fact_id',fact_id,'weight',weight),
                    owner,workspace_id,?1,?2 FROM graph_edges WHERE status='active'",
            params![reason, now],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO memory_quarantine
             (id,tier,original_id,content,payload,owner,workspace_id,reason,quarantined_at)
             SELECT 'q_cue_'||hex(randomblob(8)),'graph_cue',node_id||':'||cue,cue,
                    json_object('node_id',node_id,'source',source),owner,workspace_id,?1,?2
             FROM graph_cues WHERE status='active'",
            params![reason, now],
        )?;
        tx.execute(
            "UPDATE curated SET archived=1, updated_at=?1 WHERE archived=0",
            params![now],
        )?;
        tx.execute(
            "UPDATE facts SET status='quarantined' WHERE status='active'",
            [],
        )?;
        tx.execute(
            "UPDATE graph_nodes SET status='quarantined' WHERE status='active' AND layer <> 'code'",
            [],
        )?;
        tx.execute(
            "UPDATE graph_edges SET status='quarantined' WHERE status='active'",
            [],
        )?;
        tx.execute(
            "UPDATE graph_cues SET status='quarantined' WHERE status='active'",
            [],
        )?;
        // curated_fts/facts_fts are external-content indexes; their supported
        // rebuild command preserves rowid synchronization. Plain DELETE can
        // corrupt an external-content FTS table on an idempotent rerun.
        tx.execute("INSERT INTO curated_fts(curated_fts) VALUES('rebuild')", [])?;
        tx.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')", [])?;
        tx.commit()?;
        drop(conn);
        let integrity = self.rebuild_graph_cue_fts()?;
        Ok(serde_json::json!({"dry_run": false, "quarantined": counts, "integrity": integrity}))
    }

    fn embedding_to_blob(embedding: &[f32]) -> Vec<u8> {
        let mut blob = Vec::with_capacity(embedding.len() * 4);
        for &v in embedding {
            blob.extend_from_slice(&v.to_le_bytes());
        }
        blob
    }

    fn blob_to_embedding(blob: &[u8]) -> Vec<f32> {
        blob.chunks_exact(4)
            .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
            .collect()
    }

    fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
        if a.len() != b.len() || a.is_empty() {
            return 0.0;
        }
        let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
        let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
        let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm_a == 0.0 || norm_b == 0.0 {
            0.0
        } else {
            dot / (norm_a * norm_b)
        }
    }

    fn build_fts_query(raw_query: &str) -> Option<String> {
        let tokens: Vec<String> = raw_query
            .split(|c: char| !c.is_alphanumeric() && c != '_')
            .filter(|t| !t.is_empty())
            .map(|t| format!("\"{}\"", t.replace('"', "")))
            .collect();
        if tokens.is_empty() {
            None
        } else {
            Some(tokens.join(" OR "))
        }
    }
}

#[async_trait]
impl MemoryStore for SqliteStore {
    fn as_sqlite(&self) -> Option<&SqliteStore> {
        Some(self)
    }

    fn capabilities(&self) -> StoreCapabilities {
        self.capabilities.clone()
    }

    fn is_degraded(&self) -> bool {
        false
    }

    async fn upsert_curated(&self, r: &MemoryRecord, embedding: Option<&[f32]>) -> bool {
        let conn = self.conn.lock().unwrap();
        let embedding_blob = embedding.map(|e| Self::embedding_to_blob(e));
        let values = Self::record_to_row(r);

        let result = conn.execute(
            "INSERT OR REPLACE INTO curated (
                id, content, kind, priority, trust_score, confidence_score,
                importance_score, scene_name, source, source_type, owner,
                workspace_id, session_key, session_id, tags, source_message_ids,
                timestamps, created_at, updated_at, archived, last_accessed_at,
                exempt_from_decay, exempt_from_dedup, metadata, workspace_path, embedding
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25,?26)",
            rusqlite::params![
                values[0], values[1], values[2], values[3], values[4], values[5],
                values[6], values[7], values[8], values[9], values[10], values[11],
                values[12], values[13], values[14], values[15], values[16], values[17],
                values[18], values[19], values[20], values[21], values[22], values[23],
                values[24],
                embedding_blob,
            ],
        );

        if let Err(e) = result {
            warn!("upsert_curated failed: {e}");
            return false;
        }

        // Update FTS
        let _ = conn.execute(
            "INSERT OR REPLACE INTO curated_fts(rowid, content, scene_name, tags, workspace_id) \
             VALUES ((SELECT rowid FROM curated WHERE id = ?1), ?2, ?3, ?4, ?5)",
            params![
                r.id,
                r.content,
                r.scene_name.as_deref().unwrap_or(""),
                r.tags.join(" "),
                r.workspace_id
            ],
        );

        info!("upserted curated record {}", r.id);
        true
    }

    async fn delete_curated_batch(&self, ids: &[String]) -> bool {
        let conn = self.conn.lock().unwrap();
        for id in ids {
            let _ = conn.execute("DELETE FROM curated WHERE id = ?1", params![id]);
        }
        true
    }

    async fn update_curated_record(
        &self,
        id: &str,
        content: Option<&str>,
        kind: Option<MemoryKind>,
        category: Option<&str>,
        pinned: Option<bool>,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        let conn = self.conn.lock().unwrap();
        let row = conn.query_row(
            "SELECT rowid, content, owner, workspace_id, metadata FROM curated WHERE id = ?1 AND archived = 0",
            params![id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                ))
            },
        );
        let Ok((rowid, old_content, stored_owner, stored_workspace, metadata_raw)) = row else {
            return false;
        };
        let owner_ok = owner.is_some_and(|wanted| stored_owner.as_deref() == Some(wanted));
        let workspace_ok = workspace_id
            .is_some_and(|wanted| stored_workspace == wanted || stored_workspace == "global");
        if !owner_ok || !workspace_ok {
            return false;
        }

        let mut metadata: serde_json::Value =
            serde_json::from_str(&metadata_raw).unwrap_or_else(|_| serde_json::json!({}));
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        if let Some(value) = pinned {
            if let Some(object) = metadata.as_object_mut() {
                object.insert("pinned".into(), serde_json::Value::Bool(value));
            }
        }
        if let Some(value) = category {
            if let Some(object) = metadata.as_object_mut() {
                object.insert("category".into(), serde_json::Value::String(value.into()));
            }
        }
        let next_content = content.unwrap_or(&old_content);
        let next_kind = kind
            .map(|value| format!("{value:?}").to_lowercase())
            .unwrap_or_else(|| {
                conn.query_row(
                    "SELECT kind FROM curated WHERE id = ?1",
                    params![id],
                    |row| row.get(0),
                )
                .unwrap_or_else(|_| "episodic".into())
            });
        let now = chrono::Utc::now().to_rfc3339();
        if conn
            .execute(
                "UPDATE curated SET content = ?1, kind = ?2, metadata = ?3,
                     exempt_from_decay = CASE WHEN ?4 IS NULL THEN exempt_from_decay ELSE ?4 END,
                     exempt_from_dedup = CASE WHEN ?4 IS NULL THEN exempt_from_dedup ELSE ?4 END,
                     updated_at = ?5 WHERE id = ?6",
                params![
                    next_content,
                    next_kind,
                    serde_json::to_string(&metadata).unwrap_or_else(|_| "{}".into()),
                    pinned.map(i64::from),
                    now,
                    id
                ],
            )
            .is_err()
        {
            return false;
        }
        let _ = conn.execute(
            "INSERT OR REPLACE INTO curated_fts(rowid, content, scene_name, tags, workspace_id)
             SELECT rowid, content, COALESCE(scene_name, ''), tags, workspace_id FROM curated WHERE id = ?1",
            params![id],
        );
        let _ = rowid;
        true
    }

    async fn delete_curated_record(
        &self,
        id: &str,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        let conn = self.conn.lock().unwrap();
        let row = conn.query_row(
            "SELECT rowid, owner, workspace_id FROM curated WHERE id = ?1 AND archived = 0",
            params![id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        );
        let Ok((rowid, stored_owner, stored_workspace)) = row else {
            return false;
        };
        let owner_ok = owner.is_some_and(|wanted| stored_owner.as_deref() == Some(wanted));
        let workspace_ok = workspace_id
            .is_some_and(|wanted| stored_workspace == wanted || stored_workspace == "global");
        if !owner_ok || !workspace_ok {
            return false;
        }
        let _ = conn.execute("DELETE FROM curated_fts WHERE rowid = ?1", params![rowid]);
        conn.execute("DELETE FROM curated WHERE id = ?1", params![id])
            .is_ok()
    }

    async fn delete_curated_expired(&self, cutoff_iso: &str) -> usize {
        let conn = self.conn.lock().unwrap();
        let count = conn
            .execute(
                "DELETE FROM curated WHERE archived = 1 AND updated_at < ?1",
                params![cutoff_iso],
            )
            .unwrap_or(0);
        count
    }

    async fn search_curated_vector(&self, q: &[f32], top_k: usize) -> Vec<ScoredRecord> {
        self.search_curated_vector_scoped(q, top_k, None, None)
            .await
    }

    async fn search_curated_vector_scoped(
        &self,
        q: &[f32],
        top_k: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRecord> {
        if q.len() != self.embedding_dim {
            warn!(
                "vector dim mismatch: expected {}, got {}",
                self.embedding_dim,
                q.len()
            );
            return Vec::new();
        }
        let conn = self.conn.lock().unwrap();
        let mut stmt = match conn.prepare(
            "SELECT id, content, kind, priority, trust_score, confidence_score,
                    importance_score, scene_name, source, source_type, owner,
                    workspace_id, session_key, session_id, tags, source_message_ids,
                    timestamps, created_at, updated_at, archived, last_accessed_at,
                    exempt_from_decay, exempt_from_dedup, metadata, workspace_path, embedding
             FROM curated
             WHERE archived = 0 AND embedding IS NOT NULL
               AND owner = ?1
               AND (workspace_id = ?2 OR workspace_id = 'global')",
        ) {
            Ok(s) => s,
            Err(e) => {
                warn!("search_curated_vector prepare failed: {e}");
                return Vec::new();
            }
        };

        let rows = match stmt.query_map(params![owner, workspace_id], |row| {
            let record = Self::row_to_record(row)?;
            let blob: Vec<u8> = row.get(25)?;
            let emb = Self::blob_to_embedding(&blob);
            let score = Self::cosine_similarity(q, &emb);
            Ok(ScoredRecord {
                record,
                score,
                source_label: "curated_vector".into(),
            })
        }) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("search_curated_vector query failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRecord> = rows.filter_map(|r| r.ok()).collect();
        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        results.truncate(top_k);
        results
    }

    async fn search_curated_fts(&self, fts_query: &str, limit: usize) -> Vec<ScoredRecord> {
        self.search_curated_fts_scoped(fts_query, limit, None, None)
            .await
    }

    async fn search_curated_fts_scoped(
        &self,
        fts_query: &str,
        limit: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRecord> {
        if fts_query.trim().is_empty() {
            let conn = self.conn.lock().unwrap();
            let mut stmt = match conn.prepare(
                "SELECT id, content, kind, priority, trust_score, confidence_score,
                        importance_score, scene_name, source, source_type, owner,
                        workspace_id, session_key, session_id, tags, source_message_ids,
                        timestamps, created_at, updated_at, archived, last_accessed_at,
                        exempt_from_decay, exempt_from_dedup, metadata, workspace_path,
                        0.0 as rank
                 FROM curated
                 WHERE archived = 0
                   AND owner = ?1
                   AND (workspace_id = ?2 OR workspace_id = 'global')
                 ORDER BY updated_at DESC LIMIT ?3",
            ) {
                Ok(s) => s,
                Err(e) => {
                    warn!("list curated prepare failed: {e}");
                    return Vec::new();
                }
            };
            let rows = match stmt.query_map(params![owner, workspace_id, limit as i64], |row| {
                let record = Self::row_to_record(row)?;
                Ok(ScoredRecord {
                    record,
                    score: 0.0,
                    source_label: "curated_list".into(),
                })
            }) {
                Ok(rows) => rows,
                Err(e) => {
                    warn!("list curated query failed: {e}");
                    return Vec::new();
                }
            };
            return rows.filter_map(|row| row.ok()).collect();
        }

        let query = match Self::build_fts_query(fts_query) {
            Some(q) => q,
            None => return Vec::new(),
        };

        let conn = self.conn.lock().unwrap();
        let sql = "SELECT c.id, c.content, c.kind, c.priority, c.trust_score, c.confidence_score,
                          c.importance_score, c.scene_name, c.source, c.source_type, c.owner,
                          c.workspace_id, c.session_key, c.session_id, c.tags, c.source_message_ids,
                          c.timestamps, c.created_at, c.updated_at, c.archived, c.last_accessed_at,
                          c.exempt_from_decay, c.exempt_from_dedup, c.metadata, c.workspace_path, bm25(curated_fts) as rank
                   FROM curated_fts
                   JOIN curated c ON c.rowid = curated_fts.rowid
                   WHERE curated_fts MATCH ?1 AND c.archived = 0
                     AND c.owner = ?2
                     AND (c.workspace_id = ?3 OR c.workspace_id = 'global')
                   ORDER BY rank
                   LIMIT ?4";

        let over_fetch = (limit * 3).min(50);
        let mut stmt = match conn.prepare(sql) {
            Ok(s) => s,
            Err(e) => {
                warn!("FTS query failed: {e}");
                return Vec::new();
            }
        };

        let rows = match stmt.query_map(
            params![query, owner, workspace_id, over_fetch as i64],
            |row| {
                let mut record = Self::row_to_record(row)?;
                let bm25_score: f64 = row.get(25)?;
                let score = -bm25_score as f32;
                record.id = row.get(0)?;
                Ok(ScoredRecord {
                    record,
                    score,
                    source_label: "curated_fts".into(),
                })
            },
        ) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("FTS query_map failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRecord> = rows.filter_map(|r| r.ok()).collect();

        // Relative score floor trim (mimo pattern)
        if !results.is_empty() {
            let top_score = results[0].score;
            let floor_ratio = 0.15f32;
            let cutoff = top_score * floor_ratio;
            results.retain(|r| r.score >= cutoff);
        }
        results.truncate(limit);
        results
    }

    async fn search_curated_hybrid(&self, q: HybridQuery) -> Vec<ScoredRecord> {
        self.search_curated_hybrid_scoped(q, None, None).await
    }

    async fn search_curated_hybrid_scoped(
        &self,
        q: HybridQuery,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRecord> {
        let mut fts_results = Vec::new();
        let mut vec_results = Vec::new();

        if let Some(ref text) = q.query_text {
            fts_results = self
                .search_curated_fts_scoped(text, q.top_k * 2, owner, workspace_id)
                .await;
        }
        if let Some(ref emb) = q.query_embedding {
            vec_results = self
                .search_curated_vector_scoped(emb, q.top_k * 2, owner, workspace_id)
                .await;
        }

        // RRF merge (k=60)
        let rrf_k = 60.0f32;
        let mut score_map: std::collections::HashMap<String, (ScoredRecord, f32)> =
            std::collections::HashMap::new();

        for (rank, r) in fts_results.iter().enumerate() {
            let rrf_score = 1.0 / (rrf_k + rank as f32);
            let entry = score_map
                .entry(r.record.id.clone())
                .or_insert_with(|| (r.clone(), 0.0));
            entry.1 += rrf_score;
        }
        for (rank, r) in vec_results.iter().enumerate() {
            let rrf_score = 1.0 / (rrf_k + rank as f32);
            let entry = score_map
                .entry(r.record.id.clone())
                .or_insert_with(|| (r.clone(), 0.0));
            entry.1 += rrf_score;
        }

        let mut merged: Vec<ScoredRecord> = score_map
            .into_values()
            .map(|(mut r, score)| {
                r.score = score;
                r.source_label = "hybrid_rrf".into();
                r
            })
            .collect();
        merged.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        merged.truncate(q.top_k);
        merged
    }

    async fn upsert_raw(&self, r: &RawTurn, embedding: Option<&[f32]>) -> bool {
        let conn = self.conn.lock().unwrap();
        let embedding_blob = embedding.map(|e| Self::embedding_to_blob(e));
        let result = conn.execute(
            "INSERT OR IGNORE INTO raw (id, role, content, session_key, session_id, workspace_id, owner, recorded_at, metadata, workspace_path, embedding)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
            params![
                r.id,
                r.role,
                r.content,
                r.session_key,
                r.session_id,
                r.workspace_id,
                r.owner,
                r.recorded_at,
                serde_json::to_string(&r.metadata).unwrap_or_default(),
                r.workspace_path,
                embedding_blob,
            ],
        );
        let inserted = match result {
            Ok(value) => value,
            Err(e) => {
                warn!("upsert_raw failed: {e}");
                return false;
            }
        };
        if inserted == 0 {
            return false;
        }
        let _ = conn.execute(
            "INSERT OR REPLACE INTO raw_fts(rowid, content, workspace_id) \
             VALUES ((SELECT rowid FROM raw WHERE id = ?1), ?2, ?3)",
            params![r.id, r.content, r.workspace_id],
        );
        true
    }

    async fn search_raw_vector(&self, q: &[f32], top_k: usize) -> Vec<ScoredRaw> {
        self.search_raw_vector_scoped(q, top_k, None, None).await
    }

    async fn search_raw_vector_scoped(
        &self,
        q: &[f32],
        top_k: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRaw> {
        if q.len() != self.embedding_dim {
            return Vec::new();
        }
        let conn = self.conn.lock().unwrap();
        let mut stmt = match conn.prepare(
            "SELECT id, role, content, session_key, session_id, workspace_id, owner,
                    recorded_at, metadata, workspace_path, embedding
             FROM raw
             WHERE embedding IS NOT NULL
               AND owner = ?1
               AND (workspace_id = ?2 OR workspace_id = 'global')",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        let rows = match stmt.query_map(params![owner, workspace_id], |row| {
            let turn = Self::raw_row_to_record(row)?;
            let blob: Vec<u8> = row.get(10)?;
            let emb = Self::blob_to_embedding(&blob);
            let score = Self::cosine_similarity(q, &emb);
            Ok(ScoredRaw { turn, score })
        }) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("search_raw_vector query failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRaw> = rows.filter_map(|r| r.ok()).collect();
        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        results.truncate(top_k);
        results
    }

    async fn search_raw_fts(&self, fts_query: &str, limit: usize) -> Vec<ScoredRaw> {
        self.search_raw_fts_scoped(fts_query, limit, None, None)
            .await
    }

    async fn search_raw_fts_scoped(
        &self,
        fts_query: &str,
        limit: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRaw> {
        if fts_query.trim().is_empty() {
            let conn = self.conn.lock().unwrap();
            let mut stmt = match conn.prepare(
                "SELECT id, role, content, session_key, session_id, workspace_id,
                        owner, recorded_at, metadata, workspace_path
                 FROM raw
                 WHERE owner = ?1
                   AND (workspace_id = ?2 OR workspace_id = 'global')
                 ORDER BY recorded_at DESC LIMIT ?3",
            ) {
                Ok(stmt) => stmt,
                Err(error) => {
                    warn!("list raw prepare failed: {error}");
                    return Vec::new();
                }
            };
            let rows = match stmt.query_map(params![owner, workspace_id, limit as i64], |row| {
                Ok(ScoredRaw {
                    turn: Self::raw_row_to_record(row)?,
                    score: 0.0,
                })
            }) {
                Ok(rows) => rows,
                Err(error) => {
                    warn!("list raw query failed: {error}");
                    return Vec::new();
                }
            };
            return rows.filter_map(|row| row.ok()).collect();
        }

        let query = match Self::build_fts_query(fts_query) {
            Some(q) => q,
            None => return Vec::new(),
        };

        let conn = self.conn.lock().unwrap();
        let sql = "SELECT r.id, r.role, r.content, r.session_key, r.session_id, r.workspace_id,
                          r.owner, r.recorded_at, r.metadata, r.workspace_path, bm25(raw_fts) as rank
                   FROM raw_fts
                   JOIN raw r ON r.rowid = raw_fts.rowid
                   WHERE raw_fts MATCH ?1
                     AND r.owner = ?2
                     AND (r.workspace_id = ?3 OR r.workspace_id = 'global')
                   ORDER BY rank
                   LIMIT ?4";

        let mut stmt = match conn.prepare(sql) {
            Ok(s) => s,
            Err(e) => {
                warn!("raw FTS query failed: {e}");
                return Vec::new();
            }
        };

        let rows = match stmt.query_map(params![query, owner, workspace_id, limit as i64], |row| {
            let turn = RawTurn {
                id: row.get(0)?,
                role: row.get(1)?,
                content: row.get(2)?,
                session_key: row.get(3)?,
                session_id: row.get(4)?,
                workspace_id: row.get(5)?,
                owner: row.get(6)?,
                workspace_path: row.get(9)?,
                recorded_at: row.get(7)?,
                metadata: serde_json::from_str(&row.get::<_, String>(8)?).unwrap_or_default(),
            };
            let bm25_score: f64 = row.get(10)?;
            Ok(ScoredRaw {
                turn,
                score: -bm25_score as f32,
            })
        }) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("raw FTS query_map failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRaw> = rows.filter_map(|r| r.ok()).collect();
        results.truncate(limit);
        results
    }

    async fn reindex_all(&self, embed: &EmbedFn) -> ReindexCounts {
        let conn = self.conn.lock().unwrap();
        let mut curated_count = 0usize;
        let mut raw_count = 0usize;

        // Reindex curated
        if let Ok(mut stmt) =
            conn.prepare("SELECT id, content FROM curated WHERE embedding IS NULL")
        {
            if let Ok(rows) = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            }) {
                for row in rows.flatten() {
                    if let Some(emb) = embed(&row.1) {
                        let blob = Self::embedding_to_blob(&emb);
                        let _ = conn.execute(
                            "UPDATE curated SET embedding = ?1 WHERE id = ?2",
                            params![blob, row.0],
                        );
                        curated_count += 1;
                    }
                }
            }
        }

        // Reindex raw
        if let Ok(mut stmt) = conn.prepare("SELECT id, content FROM raw WHERE embedding IS NULL") {
            if let Ok(rows) = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            }) {
                for row in rows.flatten() {
                    if let Some(emb) = embed(&row.1) {
                        let blob = Self::embedding_to_blob(&emb);
                        let _ = conn.execute(
                            "UPDATE raw SET embedding = ?1 WHERE id = ?2",
                            params![blob, row.0],
                        );
                        raw_count += 1;
                    }
                }
            }
        }

        ReindexCounts {
            curated_count,
            raw_count,
        }
    }

    async fn fact(&self, op: FactOp) -> FactResult {
        let conn = self.conn.lock().unwrap();
        match op {
            FactOp::Add { content, entities } => {
                let id = generate_id();
                let now = chrono::Utc::now().to_rfc3339();
                let entities_json = serde_json::to_string(&entities).unwrap_or_default();
                let _ = conn.execute(
                    "INSERT INTO facts (id, content, entities, trust_score, created_at, updated_at) VALUES (?1,?2,?3,0.50,?4,?4)",
                    params![id, content, entities_json, now],
                );
                let _ = conn.execute(
                    "INSERT INTO facts_fts(rowid, content, entities) VALUES ((SELECT rowid FROM facts WHERE id = ?1), ?2, ?3)",
                    params![id, content, entities_json],
                );
                FactResult::Added { id }
            }
            FactOp::Search { query, limit } => {
                let fts_query = match Self::build_fts_query(&query) {
                    Some(q) => q,
                    None => return FactResult::SearchResults { results: vec![] },
                };
                let sql = "SELECT f.id, f.content, f.entities, f.trust_score, f.created_at, f.updated_at, bm25(facts_fts) as rank
                           FROM facts_fts JOIN facts f ON f.rowid = facts_fts.rowid
                           WHERE facts_fts MATCH ?1 AND f.status='active' ORDER BY rank LIMIT ?2";
                let results = if let Ok(mut stmt) = conn.prepare(sql) {
                    stmt.query_map(params![fts_query, limit as i64], |row| {
                        Ok(ScoredRecord {
                            record: MemoryRecord {
                                id: row.get(0)?,
                                content: row.get(1)?,
                                kind: MemoryKind::Fact,
                                trust_score: row.get::<_, f64>(3)? as f32,
                                ..MemoryRecord::new("")
                            },
                            score: -row.get::<_, f64>(6)? as f32,
                            source_label: "fact_fts".into(),
                        })
                    })
                    .map(|r| r.filter_map(|r| r.ok()).collect())
                    .unwrap_or_default()
                } else {
                    vec![]
                };
                FactResult::SearchResults { results }
            }
            FactOp::Probe { entity } => {
                let sql = "SELECT id, content, entities, trust_score, created_at, updated_at FROM facts WHERE status='active' AND entities LIKE ?1";
                let pattern = format!("%\"{}\"%", entity);
                let results = if let Ok(mut stmt) = conn.prepare(sql) {
                    stmt.query_map(params![pattern], |row| {
                        Ok(ScoredRecord {
                            record: MemoryRecord {
                                id: row.get(0)?,
                                content: row.get(1)?,
                                kind: MemoryKind::Fact,
                                trust_score: row.get::<_, f64>(3)? as f32,
                                ..MemoryRecord::new("")
                            },
                            score: 1.0,
                            source_label: "fact_probe".into(),
                        })
                    })
                    .map(|r| r.filter_map(|r| r.ok()).collect())
                    .unwrap_or_default()
                } else {
                    vec![]
                };
                FactResult::Probe { results }
            }
            FactOp::Update { id, content } => {
                let now = chrono::Utc::now().to_rfc3339();
                let changed = conn
                    .execute(
                        "UPDATE facts SET content = ?1, updated_at = ?2 WHERE id = ?3 AND status='active'",
                        params![content, now, id],
                    )
                    .unwrap_or(0);
                FactResult::Updated {
                    success: changed > 0,
                }
            }
            FactOp::Remove { id } => {
                let _ = conn.execute(
                    "DELETE FROM facts_fts WHERE rowid=(SELECT rowid FROM facts WHERE id=?1)",
                    params![id],
                );
                let changed = conn
                    .execute("DELETE FROM facts WHERE id = ?1", params![id])
                    .unwrap_or(0);
                FactResult::Removed {
                    success: changed > 0,
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_record(content: &str) -> MemoryRecord {
        let mut r = MemoryRecord::new(content);
        r.source = "test".into();
        r.tags = vec!["test".into()];
        r.owner = Some("alice".into());
        r.workspace_id = "global".into();
        r
    }

    #[tokio::test]
    async fn digest_empty_bank_is_valid_shape() {
        let store = SqliteStore::memory(4).unwrap();
        let d = store.digest("alice", "global", true).unwrap();
        assert!(d["pinned"].as_array().unwrap().is_empty());
        assert!(d["clusters"].as_array().unwrap().is_empty());
        assert!(d["recent"].as_array().unwrap().is_empty());
        assert_eq!(d["counts"]["by_tier"]["curated"], 0);
        assert_eq!(d["counts"]["by_tier"]["raw"], 0);
        assert_eq!(d["counts"]["candidates_pending"], 0);
        assert!(d["generated_at"].is_string());
    }

    #[tokio::test]
    async fn digest_requires_owner() {
        let store = SqliteStore::memory(4).unwrap();
        assert!(store.digest("", "global", true).is_err());
    }

    #[tokio::test]
    async fn digest_reflects_writes_immediately_and_scopes_by_owner() {
        let store = SqliteStore::memory(4).unwrap();
        let mut persona = test_record("keeper of the amber greenhouse ledger");
        persona.kind = MemoryKind::Persona;
        store.upsert_curated(&persona, None).await;
        store
            .upsert_curated(&test_record("prefers green tea in the mornings"), None)
            .await;

        let d = store.digest("alice", "global", true).unwrap();
        assert_eq!(d["counts"]["by_tier"]["curated"], 2);
        assert_eq!(d["counts"]["by_kind"]["persona"], 1);
        let pinned = d["pinned"].as_array().unwrap();
        assert_eq!(pinned.len(), 1);
        assert!(pinned[0]["headline"]
            .as_str()
            .unwrap()
            .contains("amber greenhouse"));
        // Enriched digest (trust classifier inputs): id + full content +
        // source_type ride along; persona-kind auto-inclusion is NOT an
        // explicit pin.
        assert_eq!(pinned[0]["id"], persona.id);
        assert_eq!(
            pinned[0]["content"].as_str().unwrap(),
            "keeper of the amber greenhouse ledger"
        );
        assert!(pinned[0]["source_type"].is_string());
        assert_eq!(pinned[0]["pinned"], false);

        let bob = store.digest("bob", "global", true).unwrap();
        assert_eq!(bob["counts"]["by_tier"]["curated"], 0);
        assert!(bob["pinned"].as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn digest_explicit_pin_flag_survives_for_non_persona_kinds() {
        let store = SqliteStore::memory(4).unwrap();
        let mut fact = test_record("the boiler reset code is 4711");
        fact.kind = MemoryKind::Fact;
        fact.metadata = serde_json::json!({"pinned": true});
        store.upsert_curated(&fact, None).await;

        let d = store.digest("alice", "global", true).unwrap();
        let pinned = d["pinned"].as_array().unwrap();
        assert_eq!(pinned.len(), 1);
        assert_eq!(pinned[0]["pinned"], true, "explicit user pin is flagged");
        assert_eq!(pinned[0]["kind"], "fact");
    }

    #[tokio::test]
    async fn digest_unions_workspace_and_global() {
        let store = SqliteStore::memory(4).unwrap();
        let mut scoped = test_record("repo-local build quirk");
        scoped.workspace_id = "repo-x".into();
        store.upsert_curated(&scoped, None).await;
        store
            .upsert_curated(&test_record("global tea preference"), None)
            .await;

        let d = store.digest("alice", "repo-x", true).unwrap();
        assert_eq!(d["counts"]["by_tier"]["curated"], 2);
        let strict = store.digest("alice", "repo-x", false).unwrap();
        assert_eq!(strict["counts"]["by_tier"]["curated"], 1);
    }

    #[tokio::test]
    async fn schema_version_is_stamped() {
        let store = SqliteStore::memory(4).unwrap();
        let v: i64 = store
            .conn
            .lock()
            .unwrap()
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION);
    }

    #[tokio::test]
    async fn migrate_existing_unversioned_db_preserves_data() {
        let path = std::env::temp_dir().join(format!(
            "fm-migrate-test-{}-{}.db",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path_s = path.to_str().unwrap().to_string();
        {
            let store = SqliteStore::new(&path_s, 4).unwrap();
            let r = test_record("survives the migration intact");
            store.upsert_curated(&r, None).await;
            // Simulate a DB from before schema versioning existed.
            store
                .conn
                .lock()
                .unwrap()
                .pragma_update(None, "user_version", 0)
                .unwrap();
        }
        {
            let store = SqliteStore::new(&path_s, 4).unwrap();
            let v: i64 = store
                .conn
                .lock()
                .unwrap()
                .query_row("PRAGMA user_version", [], |r| r.get(0))
                .unwrap();
            assert_eq!(v, SCHEMA_VERSION);
            let results = store
                .search_curated_fts_scoped("survives migration", 10, Some("alice"), Some("global"))
                .await;
            assert!(!results.is_empty(), "pre-migration data must survive");
        }
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn roundtrip_upsert_search() {
        let store = SqliteStore::memory(4).unwrap();
        let r = test_record("rust is a systems programming language");
        store.upsert_curated(&r, None).await;

        let results = store
            .search_curated_fts_scoped("rust programming", 10, Some("alice"), Some("global"))
            .await;
        assert!(!results.is_empty());
        assert!(results[0].record.content.contains("rust"));
    }

    #[tokio::test]
    async fn vector_search_with_embedding() {
        let store = SqliteStore::memory(4).unwrap();
        let mut r = test_record("hello world");
        r.workspace_id = "ws1".into();
        let emb = vec![1.0, 0.0, 0.0, 0.0];
        store.upsert_curated(&r, Some(&emb)).await;

        let query_emb = vec![1.0, 0.0, 0.0, 0.0];
        let results = store
            .search_curated_vector_scoped(&query_emb, 10, Some("alice"), Some("ws1"))
            .await;
        assert!(!results.is_empty());
        assert!((results[0].score - 1.0).abs() < 0.001);
    }

    #[tokio::test]
    async fn hybrid_rrf_merge() {
        let store = SqliteStore::memory(4).unwrap();
        let r1 = test_record("rust memory safety borrow checker");
        let r2 = test_record("python garbage collection runtime");
        let emb1 = vec![1.0, 0.0, 0.0, 0.0];
        let emb2 = vec![0.0, 1.0, 0.0, 0.0];
        store.upsert_curated(&r1, Some(&emb1)).await;
        store.upsert_curated(&r2, Some(&emb2)).await;

        let q = HybridQuery {
            query_text: Some("rust borrow".into()),
            query_embedding: Some(vec![1.0, 0.0, 0.0, 0.0]),
            sparse_vector: None,
            top_k: 5,
            workspace_id: None,
        };
        let results = store
            .search_curated_hybrid_scoped(q, Some("alice"), Some("global"))
            .await;
        assert!(!results.is_empty());
        assert!(results[0].record.content.contains("rust"));
    }

    #[tokio::test]
    async fn raw_turn_fts() {
        let store = SqliteStore::memory(4).unwrap();
        let mut t = RawTurn::new("user", "how do I configure the database?");
        t.owner = Some("alice".into());
        store.upsert_raw(&t, None).await;

        let results = store
            .search_raw_fts_scoped("configure database", 5, Some("alice"), Some("global"))
            .await;
        assert!(!results.is_empty());
        assert!(results[0].turn.content.contains("configure"));
    }

    #[tokio::test]
    async fn fact_store_ops() {
        let store = SqliteStore::memory(4).unwrap();
        let result = store
            .fact(FactOp::Add {
                content: "Rust uses ownership for memory safety".into(),
                entities: vec!["rust".into(), "memory".into()],
            })
            .await;
        let id = match &result {
            FactResult::Added { id } => id.clone(),
            _ => panic!("expected Added"),
        };

        let search = store
            .fact(FactOp::Search {
                query: "ownership".into(),
                limit: 5,
            })
            .await;
        match search {
            FactResult::SearchResults { results } => assert!(!results.is_empty()),
            _ => panic!("expected SearchResults"),
        }

        let probe = store
            .fact(FactOp::Probe {
                entity: "rust".into(),
            })
            .await;
        match probe {
            FactResult::Probe { results } => assert!(!results.is_empty()),
            _ => panic!("expected Probe"),
        }

        let update = store
            .fact(FactOp::Update {
                id: id.clone(),
                content: "Rust uses ownership and borrowing for memory safety".into(),
            })
            .await;
        match update {
            FactResult::Updated { success } => assert!(success),
            _ => panic!("expected Updated"),
        }

        let remove = store.fact(FactOp::Remove { id }).await;
        match remove {
            FactResult::Removed { success } => assert!(success),
            _ => panic!("expected Removed"),
        }
    }

    #[tokio::test]
    async fn workspace_isolation() {
        let store = SqliteStore::memory(4).unwrap();
        let mut r1 = test_record("project alpha secret");
        r1.workspace_id = "alpha".into();
        let mut r2 = test_record("project beta secret");
        r2.workspace_id = "beta".into();
        store.upsert_curated(&r1, None).await;
        store.upsert_curated(&r2, None).await;

        let alpha_results = store
            .search_curated_fts_scoped("secret", 10, Some("alice"), Some("alpha"))
            .await;
        assert_eq!(alpha_results.len(), 1);
        assert_eq!(alpha_results[0].record.workspace_id, "alpha");
    }

    #[tokio::test]
    async fn scoped_queries_filter_before_limit() {
        let store = SqliteStore::memory(4).unwrap();
        let mut bob = test_record("shared secret from bob");
        bob.owner = Some("bob".into());
        bob.workspace_id = "ws".into();
        let mut alice = test_record("shared secret from alice");
        alice.owner = Some("alice".into());
        alice.workspace_id = "ws".into();
        let emb = vec![1.0, 0.0, 0.0, 0.0];
        store.upsert_curated(&bob, Some(&emb)).await;
        store.upsert_curated(&alice, Some(&emb)).await;

        let fts = store
            .search_curated_fts_scoped("shared secret", 1, Some("alice"), Some("ws"))
            .await;
        assert_eq!(fts.len(), 1);
        assert_eq!(fts[0].record.owner.as_deref(), Some("alice"));

        let vector = store
            .search_curated_vector_scoped(&emb, 1, Some("alice"), Some("ws"))
            .await;
        assert_eq!(vector.len(), 1);
        assert_eq!(vector[0].record.owner.as_deref(), Some("alice"));

        let hybrid = store
            .search_curated_hybrid_scoped(
                HybridQuery {
                    query_text: Some("shared secret".into()),
                    query_embedding: Some(emb.clone()),
                    sparse_vector: None,
                    top_k: 1,
                    workspace_id: Some("ws".into()),
                },
                Some("alice"),
                Some("ws"),
            )
            .await;
        assert_eq!(hybrid.len(), 1);
        assert_eq!(hybrid[0].record.owner.as_deref(), Some("alice"));

        let mut raw_bob = RawTurn::new("user", "raw shared secret from bob");
        raw_bob.owner = Some("bob".into());
        raw_bob.workspace_id = "ws".into();
        let mut raw_alice = RawTurn::new("user", "raw shared secret from alice");
        raw_alice.owner = Some("alice".into());
        raw_alice.workspace_id = "ws".into();
        store.upsert_raw(&raw_bob, None).await;
        store.upsert_raw(&raw_alice, None).await;
        let raw = store
            .search_raw_fts_scoped("raw shared secret", 1, Some("alice"), Some("ws"))
            .await;
        assert_eq!(raw.len(), 1);
        assert_eq!(raw[0].turn.owner.as_deref(), Some("alice"));

        let raw_list = store
            .search_raw_fts_scoped("", 1, Some("alice"), Some("ws"))
            .await;
        assert_eq!(raw_list.len(), 1);
        assert_eq!(raw_list[0].turn.owner.as_deref(), Some("alice"));
    }

    #[tokio::test]
    async fn legacy_quarantine_is_transactional_hidden_and_idempotent() {
        let store = SqliteStore::memory(4).unwrap();
        let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
        let mut record = test_record("temporary permission fixture");
        record.owner = Some("alice".into());
        store.upsert_curated(&record, None).await;
        store
            .fact(FactOp::Add {
                content: "temporary fixture fact".into(),
                entities: vec!["fixture".into()],
            })
            .await;
        store
            .graph_upsert(
                &scope,
                &crate::graph::GraphUpsertInput {
                    nodes: vec![crate::graph::GraphNodeInput {
                        kind: "project".into(),
                        name: "temporary fixture".into(),
                        label: None,
                        layer: None,
                        trust: None,
                    }],
                    edges: vec![],
                    cues: vec![crate::graph::GraphCueInput {
                        cue: "temporary fixture".into(),
                        node: crate::graph::NodeRef {
                            kind: "project".into(),
                            name: "temporary fixture".into(),
                        },
                        source: None,
                    }],
                },
            )
            .unwrap();

        let dry = store.quarantine_legacy_state(true, "test").unwrap();
        assert_eq!(dry["would_quarantine"]["curated"], 1);
        assert!(!store
            .search_curated_fts_scoped("temporary", 10, Some("alice"), Some("global"))
            .await
            .is_empty());

        store.quarantine_legacy_state(false, "test").unwrap();
        store.quarantine_legacy_state(false, "test").unwrap();
        assert!(store
            .search_curated_fts_scoped("temporary", 10, Some("alice"), Some("global"))
            .await
            .is_empty());
        assert!(store
            .graph_cues(&scope, "temporary", 10)
            .unwrap()
            .is_empty());
        let quarantined: i64 = store
            .conn
            .lock()
            .unwrap()
            .query_row("SELECT count(*) FROM memory_quarantine", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(quarantined, 4);
        assert_eq!(
            store.quality_status().unwrap()["graph"]["integrity_ok"],
            true
        );
    }

    #[test]
    fn graph_cue_fts_tracks_status_and_deletion() {
        let store = SqliteStore::memory(4).unwrap();
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO graph_cues(cue,node_id,created_at) VALUES(?1,?2,?3)",
            params![
                "orphan sentinel",
                "node-sentinel",
                chrono::Utc::now().to_rfc3339()
            ],
        )
        .unwrap();
        let count = || {
            conn.query_row("SELECT count(*) FROM graph_cues_fts", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap()
        };
        assert_eq!(count(), 1);
        conn.execute(
            "UPDATE graph_cues SET status='quarantined' WHERE cue=?1 AND node_id=?2",
            params!["orphan sentinel", "node-sentinel"],
        )
        .unwrap();
        assert_eq!(count(), 0);
        conn.execute(
            "UPDATE graph_cues SET status='active' WHERE cue=?1 AND node_id=?2",
            params!["orphan sentinel", "node-sentinel"],
        )
        .unwrap();
        assert_eq!(count(), 1);
        conn.execute(
            "DELETE FROM graph_cues WHERE cue=?1 AND node_id=?2",
            params!["orphan sentinel", "node-sentinel"],
        )
        .unwrap();
        assert_eq!(count(), 0);
    }

    #[tokio::test]
    async fn owner_rename_and_purge_are_scoped_and_graph_safe() {
        let store = SqliteStore::memory(4).unwrap();
        for owner in ["alice", "bob"] {
            let mut record = test_record(&format!("{owner} private memory"));
            record.id = format!("m_{owner}");
            record.owner = Some(owner.into());
            record.workspace_id = "workspace".into();
            assert!(store.upsert_curated(&record, None).await);
            let scope = crate::graph::GraphScope::new(owner, "workspace").unwrap();
            store
                .graph_upsert(
                    &scope,
                    &crate::graph::GraphUpsertInput {
                        nodes: vec![],
                        edges: vec![],
                        cues: vec![crate::graph::GraphCueInput {
                            cue: "private project".into(),
                            node: crate::graph::NodeRef {
                                kind: "project".into(),
                                name: "private".into(),
                            },
                            source: None,
                        }],
                    },
                )
                .unwrap();
        }

        store.rename_owner("alice", "alicia").unwrap();
        assert_eq!(store.owner_counts("alice").unwrap()["curated"], 0);
        assert_eq!(store.owner_counts("alicia").unwrap()["curated"], 1);
        let old_scope = crate::graph::GraphScope::new("alice", "workspace").unwrap();
        let new_scope = crate::graph::GraphScope::new("alicia", "workspace").unwrap();
        assert!(store
            .graph_cues(&old_scope, "private", 10)
            .unwrap()
            .is_empty());
        let renamed = store.graph_cues(&new_scope, "private", 10).unwrap();
        assert_eq!(renamed.len(), 1);
        assert_eq!(renamed[0].node.id, new_scope.node_id("project", "private"));

        store.purge_owner("alicia").unwrap();
        assert_eq!(store.owner_counts("alicia").unwrap()["curated"], 0);
        assert_eq!(store.owner_counts("bob").unwrap()["curated"], 1);
        assert!(!store
            .search_curated_fts_scoped("bob private", 10, Some("bob"), Some("workspace"))
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn fts_query_builder() {
        let q = SqliteStore::build_fts_query("postgres database port 5433");
        assert_eq!(
            q.unwrap(),
            "\"postgres\" OR \"database\" OR \"port\" OR \"5433\""
        );
        assert!(SqliteStore::build_fts_query("").is_none());
        assert!(SqliteStore::build_fts_query("  ").is_none());
    }
}
