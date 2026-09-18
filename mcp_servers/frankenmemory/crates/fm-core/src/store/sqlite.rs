use async_trait::async_trait;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use std::{
    collections::{BTreeMap, HashSet},
    path::Path,
    sync::Mutex,
    time::Duration,
};
use tracing::{info, warn};

use crate::config::CodeIndexConfig;
use crate::ontology;
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
/// v7 = lifecycle leases, bounded forget recovery, and candidate provenance.
/// v8 = owner/workspace retention policies.
/// v9 = client operation IDs for crash-recoverable forget commits.
/// v10 = crash-recoverable retention expiry operations.
/// v11 = additive typed ontology, revision, evidence, job, and index tables.
/// v12 = canonical repository constraints, project identity, and migration outbox.
/// v13 = additive ontology extensions and migration compatibility.
/// v14 = durable code-index run manifests and ready-generation pointers.
pub const SCHEMA_VERSION: i64 = 14;

pub const MAX_RAW_RETENTION_DAYS: u32 = 90;
pub const MAX_CANDIDATE_RETENTION_DAYS: u32 = 365;
pub const MAX_CURATED_RETENTION_DAYS: u32 = 3650;
pub const MAX_GRAPH_RETENTION_DAYS: u32 = 3650;
pub const MAX_RECOVERY_SECONDS: u32 = 86_400;

pub struct SqliteStore {
    pub(crate) conn: Mutex<Connection>,
    embedding_dim: usize,
    capabilities: StoreCapabilities,
    code_index_lease_seconds: u64,
}

/// Keep the SQLite authority and any persistent journal sidecars owner-only.
/// SQLite creates these files after the connection opens, so callers invoke
/// this both before and after initialization. Existing symlinks are rejected
/// rather than chmod'ing an unexpected target.
fn relock_sqlite_files(path: &str) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let base = Path::new(path);
        for candidate in [
            base.to_path_buf(),
            Path::new(&format!("{path}-wal")).to_path_buf(),
            Path::new(&format!("{path}-shm")).to_path_buf(),
            Path::new(&format!("{path}-journal")).to_path_buf(),
        ] {
            let metadata = match std::fs::symlink_metadata(&candidate) {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            };
            if metadata.file_type().is_symlink() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    format!("SQLite path is a symlink: {}", candidate.display()),
                ));
            }
            let mut permissions = metadata.permissions();
            permissions.set_mode(0o600);
            std::fs::set_permissions(&candidate, permissions)?;
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Default)]
pub struct CuratedMaintenancePlan {
    pub upserts: Vec<MemoryRecord>,
    pub delete_ids: Vec<String>,
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct ForgetClosure {
    pub raw_ids: Vec<String>,
    pub candidate_ids: Vec<String>,
    pub curated_ids: Vec<String>,
    pub graph_node_ids: Vec<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ForgetSelector {
    RecordId(String),
    SourceUri(String),
    SourceMessageId(String),
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct ForgetPreview {
    pub selector: ForgetSelector,
    pub closure: ForgetClosure,
    pub token: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct RetentionExpiryPreview {
    pub closure: ForgetClosure,
    pub token: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RetentionOperationState {
    Absent,
    Committed,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct RetentionOperationStatus {
    pub operation_id: String,
    pub state: RetentionOperationState,
    pub closure: Option<ForgetClosure>,
    pub committed_at: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct ForgetCommit {
    pub tombstone_id: String,
    pub closure: ForgetClosure,
    pub recover_until: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ForgetOperationState {
    Absent,
    Committed,
    Restored,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct ForgetOperationStatus {
    pub operation_id: String,
    pub state: ForgetOperationState,
    pub tombstone_id: Option<String>,
    pub closure: Option<ForgetClosure>,
    pub recover_until: Option<String>,
    pub recovered_at: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct RetentionPolicy {
    pub raw_days: u32,
    pub candidate_days: u32,
    pub curated_days: Option<u32>,
    pub graph_days: Option<u32>,
    pub recovery_seconds: u32,
}

impl Default for RetentionPolicy {
    fn default() -> Self {
        Self {
            raw_days: 30,
            candidate_days: 90,
            curated_days: None,
            graph_days: Some(365),
            recovery_seconds: 0,
        }
    }
}

impl RetentionPolicy {
    fn validate(&self) -> Result<(), String> {
        if self.raw_days > MAX_RAW_RETENTION_DAYS {
            return Err(format!(
                "raw_days exceeds safe maximum {MAX_RAW_RETENTION_DAYS}"
            ));
        }
        if self.candidate_days > MAX_CANDIDATE_RETENTION_DAYS {
            return Err(format!(
                "candidate_days exceeds safe maximum {MAX_CANDIDATE_RETENTION_DAYS}"
            ));
        }
        if self
            .curated_days
            .is_some_and(|days| days > MAX_CURATED_RETENTION_DAYS)
        {
            return Err(format!(
                "curated_days exceeds safe maximum {MAX_CURATED_RETENTION_DAYS}"
            ));
        }
        if self
            .graph_days
            .is_some_and(|days| days > MAX_GRAPH_RETENTION_DAYS)
        {
            return Err(format!(
                "graph_days exceeds safe maximum {MAX_GRAPH_RETENTION_DAYS}"
            ));
        }
        if self.recovery_seconds > MAX_RECOVERY_SECONDS {
            return Err(format!(
                "recovery_seconds exceeds safe maximum {MAX_RECOVERY_SECONDS}"
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct RawSnapshot {
    record: RawTurn,
    embedding: Option<Vec<f32>>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct CuratedSnapshot {
    record: MemoryRecord,
    embedding: Option<Vec<f32>>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct GraphNodeSnapshot {
    id: String,
    kind: String,
    label: Option<String>,
    name: String,
    norm_name: String,
    layer: String,
    ref_table: Option<String>,
    ref_id: Option<String>,
    trust: i64,
    created_at: String,
    last_seen: String,
    owner: Option<String>,
    workspace_id: String,
    candidate_id: Option<String>,
    status: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct GraphEdgeSnapshot {
    id: String,
    src_id: String,
    tag: String,
    dst_id: String,
    fact_id: Option<String>,
    weight: f64,
    traversal_count: i64,
    trust: i64,
    created_at: String,
    last_seen: String,
    owner: Option<String>,
    workspace_id: String,
    candidate_id: Option<String>,
    status: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct GraphCueSnapshot {
    cue: String,
    node_id: String,
    source: String,
    created_at: String,
    owner: Option<String>,
    workspace_id: String,
    candidate_id: Option<String>,
    status: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct FactSnapshot {
    id: String,
    content: String,
    entities: String,
    trust_score: f64,
    created_at: String,
    updated_at: String,
    embedding: Option<Vec<f32>>,
    owner: Option<String>,
    workspace_id: String,
    candidate_id: Option<String>,
    status: String,
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
struct ForgetSnapshot {
    raw: Vec<RawSnapshot>,
    candidates: Vec<CandidateRecord>,
    curated: Vec<CuratedSnapshot>,
    graph_nodes: Vec<GraphNodeSnapshot>,
    graph_edges: Vec<GraphEdgeSnapshot>,
    graph_cues: Vec<GraphCueSnapshot>,
    facts: Vec<FactSnapshot>,
}

impl ForgetClosure {
    fn sort_dedup(&mut self) {
        for ids in [
            &mut self.raw_ids,
            &mut self.candidate_ids,
            &mut self.curated_ids,
            &mut self.graph_node_ids,
        ] {
            ids.sort();
            ids.dedup();
        }
    }

    fn is_empty(&self) -> bool {
        self.raw_ids.is_empty()
            && self.candidate_ids.is_empty()
            && self.curated_ids.is_empty()
            && self.graph_node_ids.is_empty()
    }
}

pub struct MemoryLease<'a> {
    store: &'a SqliteStore,
    kind: String,
    owner: String,
    workspace_id: String,
    subject: String,
    holder: String,
}

impl Drop for MemoryLease<'_> {
    fn drop(&mut self) {
        let conn = self.store.conn.lock().unwrap();
        let _ = conn.execute(
            "DELETE FROM memory_leases
             WHERE kind=?1 AND owner=?2 AND workspace_id=?3
               AND subject=?4 AND holder=?5",
            params![
                self.kind,
                self.owner,
                self.workspace_id,
                self.subject,
                self.holder
            ],
        );
    }
}

impl SqliteStore {
    pub fn new(path: &str, embedding_dim: usize) -> Result<Self, rusqlite::Error> {
        relock_sqlite_files(path)
            .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
        let conn = Connection::open(path)?;
        relock_sqlite_files(path)
            .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
        let version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if version > SCHEMA_VERSION {
            return Err(rusqlite::Error::InvalidQuery);
        }
        Self::configure_connection(&conn)?;

        let store = Self {
            conn: Mutex::new(conn),
            embedding_dim,
            capabilities: StoreCapabilities {
                vector_search: true,
                fts_search: true,
                native_hybrid: false,
                sparse_vectors: false,
            },
            code_index_lease_seconds: CodeIndexConfig::DEFAULT_LEASE_SECONDS,
        };
        store.init_tables()?;
        relock_sqlite_files(path)
            .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
        Ok(store)
    }

    fn configure_connection(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.busy_timeout(Duration::from_secs(30))?;
        conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA synchronous=NORMAL;")?;
        // journal_mode is a database-wide write and can race another
        // process's first migration. SQLite's busy timeout does not cover
        // every journal-mode transition, so retry the bounded transition
        // explicitly instead of making a concurrent tenant process die with
        // `database is locked` during startup.
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        loop {
            match conn.query_row("PRAGMA journal_mode=WAL", [], |row| row.get::<_, String>(0)) {
                Ok(_) => return Ok(()),
                Err(error)
                    if Self::is_busy_error(&error) && std::time::Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(50));
                }
                Err(error) => return Err(error),
            }
        }
    }

    fn is_busy_error(error: &rusqlite::Error) -> bool {
        matches!(
            error,
            rusqlite::Error::SqliteFailure(inner, _)
                if matches!(
                    inner.code,
                    rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
                )
        )
    }

    fn sqlite_error_class(error: &rusqlite::Error) -> &'static str {
        match error {
            rusqlite::Error::SqliteFailure(inner, _)
                if matches!(
                    inner.code,
                    rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
                ) =>
            {
                "busy_or_locked"
            }
            rusqlite::Error::SqliteFailure(inner, _)
                if matches!(
                    inner.code,
                    rusqlite::ErrorCode::DatabaseCorrupt | rusqlite::ErrorCode::NotADatabase
                ) =>
            {
                "corrupt_or_not_database"
            }
            _ => "query_error",
        }
    }

    pub fn memory(embedding_dim: usize) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        conn.execute_batch("PRAGMA foreign_keys=ON;")?;
        let store = Self {
            conn: Mutex::new(conn),
            embedding_dim,
            capabilities: StoreCapabilities {
                vector_search: true,
                fts_search: true,
                native_hybrid: false,
                sparse_vectors: false,
            },
            code_index_lease_seconds: CodeIndexConfig::DEFAULT_LEASE_SECONDS,
        };
        store.init_tables()?;
        Ok(store)
    }

    pub fn set_code_index_lease_seconds(&mut self, seconds: u64) -> Result<(), String> {
        if !(CodeIndexConfig::MIN_LEASE_SECONDS..=CodeIndexConfig::MAX_LEASE_SECONDS)
            .contains(&seconds)
        {
            return Err(format!(
                "code-index lease must be between {} and {} seconds",
                CodeIndexConfig::MIN_LEASE_SECONDS,
                CodeIndexConfig::MAX_LEASE_SECONDS
            ));
        }
        self.code_index_lease_seconds = seconds;
        Ok(())
    }

    /// Return the configured stale-run recovery lease. This is deliberately
    /// separate from the indexing execution duration: an index may run longer
    /// than the lease as long as it remains the active owner of its run.
    pub fn code_index_lease_seconds(&self) -> u64 {
        self.code_index_lease_seconds
    }

    pub(crate) fn init_tables(&self) -> Result<(), rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN EXCLUSIVE;")?;
        let migration = (|| -> Result<(), rusqlite::Error> {
            let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;

            if version > SCHEMA_VERSION {
                return Err(rusqlite::Error::InvalidQuery);
            }

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
            if version < 7 {
                for (column, definition) in [
                    ("source_uri", "TEXT NOT NULL DEFAULT ''"),
                    ("source_revision", "INTEGER NOT NULL DEFAULT 1"),
                    ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                    ("source_message_ids", "TEXT NOT NULL DEFAULT '[]'"),
                ] {
                    if !Self::has_column(&conn, "candidates", column)? {
                        conn.execute_batch(&format!(
                            "ALTER TABLE candidates ADD COLUMN {column} {definition};"
                        ))?;
                    }
                }
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS memory_leases (
                        kind TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        holder TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY(kind, owner, workspace_id, subject)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_leases_expiry
                        ON memory_leases(expires_at);
                    CREATE TABLE IF NOT EXISTS memory_tombstones (
                        id TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        source_key TEXT NOT NULL,
                        affected_ids TEXT NOT NULL,
                        recovery_payload TEXT,
                        created_at TEXT NOT NULL,
                        recover_until TEXT,
                        recovered_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_tombstones_scope
                        ON memory_tombstones(owner, workspace_id, created_at);",
                )?;
                conn.pragma_update(None, "user_version", 7)?;
            }
            if version < 8 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS memory_retention_policy (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        raw_days INTEGER NOT NULL,
                        candidate_days INTEGER NOT NULL,
                        curated_days INTEGER,
                        graph_days INTEGER,
                        recovery_seconds INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(owner, workspace_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_tombstones_expiry
                        ON memory_tombstones(recover_until, recovered_at);",
                )?;
                conn.pragma_update(None, "user_version", 8)?;
            }
            if version < 9 {
                if !Self::has_column(&conn, "memory_tombstones", "operation_id")? {
                    conn.execute_batch(
                        "ALTER TABLE memory_tombstones ADD COLUMN operation_id TEXT;",
                    )?;
                }
                if !Self::has_column(&conn, "memory_tombstones", "operation_key")? {
                    conn.execute_batch(
                        "ALTER TABLE memory_tombstones ADD COLUMN operation_key TEXT;",
                    )?;
                }
                conn.execute_batch(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_tombstones_operation
                        ON memory_tombstones(owner, workspace_id, operation_id)
                        WHERE operation_id IS NOT NULL;",
                )?;
                conn.pragma_update(None, "user_version", 9)?;
            }
            if version < 10 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS memory_retention_operations (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        request_key TEXT NOT NULL,
                        closure TEXT NOT NULL,
                        committed_at TEXT NOT NULL,
                        PRIMARY KEY(owner, workspace_id, operation_id)
                    );",
                )?;
                conn.pragma_update(None, "user_version", 10)?;
            }
            if version < 11 {
                ontology::install_schema(&conn)?;
                conn.pragma_update(None, "user_version", 11)?;
            }
            if version < 12 {
                ontology::ensure_schema_extensions(&conn)?;
                conn.pragma_update(None, "user_version", 12)?;
            }
            if version < 13 {
                ontology::ensure_schema_extensions(&conn)?;
                conn.pragma_update(None, "user_version", 13)?;
            }
            if version < 14 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS code_index_runs (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        codebase TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        source_snapshot TEXT NOT NULL,
                        status TEXT NOT NULL,
                        files_indexed INTEGER NOT NULL DEFAULT 0,
                        files_unchanged INTEGER NOT NULL DEFAULT 0,
                        files_removed INTEGER NOT NULL DEFAULT 0,
                        symbols INTEGER NOT NULL DEFAULT 0,
                        errors TEXT NOT NULL DEFAULT '[]',
                        coverage TEXT NOT NULL DEFAULT '[]',
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        PRIMARY KEY (owner, workspace_id, codebase, run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_code_index_runs_status
                        ON code_index_runs(owner, workspace_id, codebase, status);
                    CREATE TABLE IF NOT EXISTS code_index_heads (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        codebase TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        PRIMARY KEY (owner, workspace_id, codebase)
                    );",
                )?;
                conn.pragma_update(None, "user_version", 14)?;
            }
            conn.execute(
                "UPDATE memory_tombstones SET recovery_payload=NULL
                 WHERE recovered_at IS NULL AND recover_until IS NOT NULL
                   AND recover_until<?1",
                params![chrono::Utc::now().to_rfc3339()],
            )?;
            // Keep additive v2 scope columns present for databases that were
            // already stamped at version 11 before those columns existed.
            ontology::ensure_schema_extensions(&conn)?;
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
        super::graph::backfill_curated_projections_inner(&conn)?;

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

    fn upsert_curated_inner(
        conn: &Connection,
        record: &MemoryRecord,
        embedding: Option<&[f32]>,
    ) -> Result<(), String> {
        let mut record = record.clone();
        let (content, content_redacted) = crate::privacy::filter_text(&record.content)
            .ok_or_else(|| "curated content is marked no-store".to_string())?;
        let (mut metadata, _) = crate::privacy::filter_json(&record.metadata);
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        metadata
            .as_object_mut()
            .expect("metadata was normalized to an object")
            .insert(
                "content_hash".into(),
                crate::privacy::content_hash(&content).into(),
            );
        record.content = content;
        record.metadata = metadata;
        // A vector computed from pre-redaction text is a derived secret sink.
        // Drop it; callers can re-embed the persisted redacted text.
        let embedding_blob = (!content_redacted)
            .then_some(embedding)
            .flatten()
            .map(Self::embedding_to_blob);
        let values = Self::record_to_row(&record);
        conn.execute(
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
        )
        .map_err(|error| error.to_string())?;

        let rowid = conn
            .query_row(
                "SELECT rowid FROM curated WHERE id = ?1",
                params![record.id],
                |row| row.get::<_, i64>(0),
            )
            .map_err(|error| error.to_string())?;
        conn.execute(
            "INSERT OR REPLACE INTO curated_fts(
                rowid, content, scene_name, tags, workspace_id
             ) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                rowid,
                record.content,
                record.scene_name.as_deref().unwrap_or(""),
                record.tags.join(" "),
                record.workspace_id,
            ],
        )
        .map_err(|error| error.to_string())?;
        super::graph::sync_curated_projection_inner(conn, &record.id)
            .map_err(|error| error.to_string())?;
        Ok(())
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
            "unknown" => MemoryKind::Unknown,
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

    fn maintenance_scope_matches(
        stored_owner: Option<&str>,
        stored_workspace: &str,
        owner: &str,
        workspace_id: &str,
    ) -> bool {
        stored_owner == Some(owner)
            && (stored_workspace == workspace_id || stored_workspace == "global")
    }

    fn apply_maintenance_upsert(
        conn: &Connection,
        record: &MemoryRecord,
        owner: &str,
        workspace_id: &str,
    ) -> Result<(), String> {
        let mut record = record.clone();
        let (content, _) = crate::privacy::filter_text(&record.content)
            .ok_or_else(|| "maintenance content is marked no-store".to_string())?;
        let (mut metadata, _) = crate::privacy::filter_json(&record.metadata);
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        metadata
            .as_object_mut()
            .expect("metadata was normalized to an object")
            .insert(
                "content_hash".into(),
                crate::privacy::content_hash(&content).into(),
            );
        record.content = content;
        record.metadata = metadata;
        let (rowid, stored_owner, stored_workspace) = conn
            .query_row(
                "SELECT rowid, owner, workspace_id FROM curated WHERE id = ?1",
                params![record.id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| error.to_string())?
            .ok_or_else(|| format!("maintenance record {} no longer exists", record.id))?;
        if !Self::maintenance_scope_matches(
            stored_owner.as_deref(),
            &stored_workspace,
            owner,
            workspace_id,
        ) || record.owner.as_deref() != Some(owner)
            || record.workspace_id != stored_workspace
        {
            return Err(format!(
                "maintenance record {} is outside authenticated scope",
                record.id
            ));
        }

        let values = Self::record_to_row(&record);
        let updated = conn
            .execute(
                "UPDATE curated SET
                    content=?2, kind=?3, priority=?4, trust_score=?5,
                    confidence_score=?6, importance_score=?7, scene_name=?8,
                    source=?9, source_type=?10, owner=?11, workspace_id=?12,
                    session_key=?13, session_id=?14, tags=?15,
                    source_message_ids=?16, timestamps=?17, created_at=?18,
                    updated_at=?19, archived=?20, last_accessed_at=?21,
                    exempt_from_decay=?22, exempt_from_dedup=?23,
                    metadata=?24, workspace_path=?25
                 WHERE id=?1",
                rusqlite::params![
                    values[0], values[1], values[2], values[3], values[4], values[5], values[6],
                    values[7], values[8], values[9], values[10], values[11], values[12],
                    values[13], values[14], values[15], values[16], values[17], values[18],
                    values[19], values[20], values[21], values[22], values[23], values[24],
                ],
            )
            .map_err(|error| error.to_string())?;
        if updated != 1 {
            return Err(format!(
                "maintenance record {} changed while applying the plan",
                record.id
            ));
        }

        if record.archived {
            conn.execute("DELETE FROM curated_fts WHERE rowid = ?1", params![rowid])
                .map_err(|error| error.to_string())?;
        } else {
            conn.execute(
                "INSERT OR REPLACE INTO curated_fts(
                    rowid, content, scene_name, tags, workspace_id
                 ) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![
                    rowid,
                    record.content,
                    record.scene_name.as_deref().unwrap_or(""),
                    record.tags.join(" "),
                    record.workspace_id,
                ],
            )
            .map_err(|error| error.to_string())?;
        }
        super::graph::sync_curated_projection_inner(conn, &record.id)
            .map_err(|error| error.to_string())?;
        Ok(())
    }

    fn apply_maintenance_delete(
        conn: &Connection,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<(), String> {
        let (rowid, stored_owner, stored_workspace) = conn
            .query_row(
                "SELECT rowid, owner, workspace_id FROM curated WHERE id = ?1",
                params![id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| error.to_string())?
            .ok_or_else(|| format!("maintenance record {id} no longer exists"))?;
        if !Self::maintenance_scope_matches(
            stored_owner.as_deref(),
            &stored_workspace,
            owner,
            workspace_id,
        ) {
            return Err(format!(
                "maintenance record {id} is outside authenticated scope"
            ));
        }

        conn.execute("DELETE FROM curated_fts WHERE rowid = ?1", params![rowid])
            .map_err(|error| error.to_string())?;
        let deleted = conn
            .execute("DELETE FROM curated WHERE id = ?1", params![id])
            .map_err(|error| error.to_string())?;
        if deleted != 1 {
            return Err(format!(
                "maintenance record {id} changed while applying the plan"
            ));
        }
        super::graph::sync_curated_projection_inner(conn, id).map_err(|error| error.to_string())?;
        Ok(())
    }

    pub fn apply_curated_maintenance_plan(
        &self,
        owner: &str,
        workspace_id: &str,
        plan: &CuratedMaintenancePlan,
    ) -> Result<(), String> {
        let owner = owner.trim();
        let workspace_id = workspace_id.trim();
        if owner.is_empty() || workspace_id.is_empty() {
            return Err("authenticated owner and workspace_id are required".into());
        }

        let mut conn = self.conn.lock().unwrap();
        let transaction = conn.transaction().map_err(|error| error.to_string())?;
        for record in &plan.upserts {
            Self::apply_maintenance_upsert(&transaction, record, owner, workspace_id)?;
        }
        for id in &plan.delete_ids {
            Self::apply_maintenance_delete(&transaction, id, owner, workspace_id)?;
        }
        transaction.commit().map_err(|error| error.to_string())
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
            "unknown" => MemoryKind::Unknown,
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
            source_uri: row.get(20)?,
            source_revision: row.get(21)?,
            content_hash: row.get(22)?,
            source_message_ids: serde_json::from_str(&row.get::<_, String>(23)?)
                .unwrap_or_default(),
            status,
            reason: row.get(16)?,
            accepted_curated_id: row.get(17)?,
            created_at: row.get(18)?,
            updated_at: row.get(19)?,
        })
    }

    pub fn insert_candidate(&self, candidate: &CandidateRecord) -> Result<bool, rusqlite::Error> {
        let mut candidate = candidate.clone();
        let Some((content, _)) = crate::privacy::filter_text(&candidate.content) else {
            return Ok(false);
        };
        candidate.content = content;
        candidate.content_hash = crate::privacy::content_hash(&candidate.content);
        candidate.source_uri = crate::privacy::filter_text(&candidate.source_uri)
            .map(|(value, _)| value)
            .unwrap_or_default();
        let conn = self.conn.lock().unwrap();
        Self::insert_candidate_inner(&conn, &candidate, true)
    }

    fn insert_candidate_inner(
        conn: &Connection,
        candidate: &CandidateRecord,
        ignore_duplicate: bool,
    ) -> Result<bool, rusqlite::Error> {
        let insert = if ignore_duplicate {
            "INSERT OR IGNORE"
        } else {
            "INSERT"
        };
        let inserted = conn.execute(
            &format!(
                "{insert} INTO candidates (
                    id, content, kind, confidence_score, importance_score, owner,
                    workspace_id, workspace_path, session_id, turn_id,
                    raw_evidence_ids, evidence_role, source, source_event_id,
                    dedup_key, status, reason, accepted_curated_id, created_at, updated_at,
                    source_uri, source_revision, content_hash, source_message_ids
                 ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24)"
            ),
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
                candidate.source_uri,
                candidate.source_revision,
                candidate.content_hash,
                serde_json::to_string(&candidate.source_message_ids)
                    .unwrap_or_else(|_| "[]".into()),
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
                    accepted_curated_id,created_at,updated_at,
                    source_uri,source_revision,content_hash,source_message_ids
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
                    accepted_curated_id,created_at,updated_at,
                    source_uri,source_revision,content_hash,source_message_ids
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
                    accepted_curated_id,created_at,updated_at,
                    source_uri,source_revision,content_hash,source_message_ids
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

    /// Edit a review candidate without publishing it.  Only the exact
    /// tenant's still-pending candidate can change; accepted, rejected, and
    /// quarantined rows remain immutable review evidence.
    pub fn update_pending_candidate(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
        content: &str,
        kind: Option<MemoryKind>,
        reason: &str,
    ) -> Result<Option<CandidateRecord>, rusqlite::Error> {
        let Some((content, _)) = crate::privacy::filter_text(content) else {
            return Ok(None);
        };
        if content.trim().is_empty() {
            return Ok(None);
        }
        let conn = self.conn.lock().unwrap();
        let updated = conn.execute(
            "UPDATE candidates
                SET content=?1,
                    kind=COALESCE(?2, kind),
                    reason=?3,
                    content_hash=?4,
                    source_revision=source_revision+1,
                    updated_at=?5
              WHERE id=?6 AND owner=?7 AND workspace_id=?8 AND status='pending'",
            params![
                content,
                kind.map(|value| format!("{value:?}").to_lowercase()),
                reason,
                crate::privacy::content_hash(&content),
                chrono::Utc::now().to_rfc3339(),
                id,
                owner,
                workspace_id,
            ],
        )?;
        if updated == 0 {
            return Ok(None);
        }
        let mut statement = conn.prepare(
            "SELECT id,content,kind,confidence_score,importance_score,owner,
                    workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,
                    evidence_role,source,source_event_id,dedup_key,status,reason,
                    accepted_curated_id,created_at,updated_at,
                    source_uri,source_revision,content_hash,source_message_ids
             FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3",
        )?;
        let mut rows = statement.query(params![id, owner, workspace_id])?;
        rows.next()?.map(Self::candidate_row).transpose()
    }

    pub fn acquire_memory_lease(
        &self,
        kind: &str,
        owner: &str,
        workspace_id: &str,
        subject: &str,
        holder: &str,
        ttl: Duration,
    ) -> Result<Option<MemoryLease<'_>>, String> {
        self.acquire_memory_lease_at(
            kind,
            owner,
            workspace_id,
            subject,
            holder,
            ttl,
            chrono::Utc::now(),
        )
    }

    fn acquire_memory_lease_at(
        &self,
        kind: &str,
        owner: &str,
        workspace_id: &str,
        subject: &str,
        holder: &str,
        ttl: Duration,
        now: chrono::DateTime<chrono::Utc>,
    ) -> Result<Option<MemoryLease<'_>>, String> {
        for (name, value) in [
            ("kind", kind),
            ("owner", owner),
            ("workspace_id", workspace_id),
            ("subject", subject),
            ("holder", holder),
        ] {
            if value.trim().is_empty() {
                return Err(format!("{name} is required"));
            }
        }
        let expires = now
            + chrono::Duration::from_std(ttl)
                .map_err(|_| "lease ttl is outside supported range".to_string())?;
        let now = now.to_rfc3339();
        let expires = expires.to_rfc3339();
        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        let current = transaction
            .query_row(
                "SELECT holder, expires_at, attempt FROM memory_leases
                 WHERE kind=?1 AND owner=?2 AND workspace_id=?3 AND subject=?4",
                params![kind, owner, workspace_id, subject],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| error.to_string())?;
        if current
            .as_ref()
            .is_some_and(|(active_holder, expires_at, _)| {
                active_holder != holder && expires_at > &now
            })
        {
            return Ok(None);
        }
        let attempt = current.map_or(1, |(_, _, attempt)| attempt.saturating_add(1));
        transaction
            .execute(
                "INSERT INTO memory_leases(
                    kind,owner,workspace_id,subject,holder,acquired_at,expires_at,attempt
                 ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)
                 ON CONFLICT(kind,owner,workspace_id,subject) DO UPDATE SET
                    holder=excluded.holder,
                    acquired_at=excluded.acquired_at,
                    expires_at=excluded.expires_at,
                    attempt=excluded.attempt",
                params![
                    kind,
                    owner,
                    workspace_id,
                    subject,
                    holder,
                    now,
                    expires,
                    attempt
                ],
            )
            .map_err(|error| error.to_string())?;
        transaction.commit().map_err(|error| error.to_string())?;
        Ok(Some(MemoryLease {
            store: self,
            kind: kind.to_string(),
            owner: owner.to_string(),
            workspace_id: workspace_id.to_string(),
            subject: subject.to_string(),
            holder: holder.to_string(),
        }))
    }

    pub fn accept_candidate_transaction(
        &self,
        candidate_id: &str,
        record: &MemoryRecord,
        embedding: Option<&[f32]>,
        reason: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<(), String> {
        let owner = owner.trim();
        let workspace_id = workspace_id.trim();
        if owner.is_empty() || workspace_id.is_empty() {
            return Err("authenticated owner and workspace_id are required".into());
        }
        if record.owner.as_deref() != Some(owner) || record.workspace_id != workspace_id {
            return Err("accepted memory is outside authenticated candidate scope".into());
        }

        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        let holder = format!("consolidate:{}", record.id);
        transaction
            .execute(
                "DELETE FROM memory_leases
                 WHERE kind='consolidation' AND owner=?1 AND workspace_id=?2
                   AND subject=?3 AND expires_at<=?4",
                params![
                    owner,
                    workspace_id,
                    candidate_id,
                    chrono::Utc::now().to_rfc3339()
                ],
            )
            .map_err(|error| error.to_string())?;
        let lease_inserted = transaction
            .execute(
                "INSERT OR IGNORE INTO memory_leases(
                    kind,owner,workspace_id,subject,holder,acquired_at,expires_at,attempt
                 ) VALUES ('consolidation',?1,?2,?3,?4,?5,?6,1)",
                params![
                    owner,
                    workspace_id,
                    candidate_id,
                    holder,
                    chrono::Utc::now().to_rfc3339(),
                    (chrono::Utc::now() + chrono::Duration::minutes(5)).to_rfc3339(),
                ],
            )
            .map_err(|error| error.to_string())?;
        if lease_inserted != 1 {
            return Err("candidate consolidation is already leased".into());
        }
        let (status, accepted_curated_id) = transaction
            .query_row(
                "SELECT status, accepted_curated_id FROM candidates
                 WHERE id=?1 AND owner=?2 AND workspace_id=?3",
                params![candidate_id, owner, workspace_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?)),
            )
            .optional()
            .map_err(|error| error.to_string())?
            .ok_or_else(|| "candidate not found in this scope".to_string())?;
        if status == "accepted" && accepted_curated_id.as_deref() == Some(record.id.as_str()) {
            return Ok(());
        }
        if status != "pending" {
            return Err("only pending candidates can be accepted".into());
        }
        let curated_collision = transaction
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1)",
                params![record.id],
                |row| row.get::<_, bool>(0),
            )
            .map_err(|error| error.to_string())?;
        if curated_collision {
            return Err("accepted curated id already exists".into());
        }

        Self::upsert_curated_inner(&transaction, record, embedding)?;
        let updated = transaction
            .execute(
                "UPDATE candidates SET status='accepted', reason=?1,
                        accepted_curated_id=?2, updated_at=?3
                 WHERE id=?4 AND owner=?5 AND workspace_id=?6 AND status='pending'",
                params![
                    reason,
                    record.id,
                    chrono::Utc::now().to_rfc3339(),
                    candidate_id,
                    owner,
                    workspace_id,
                ],
            )
            .map_err(|error| error.to_string())?;
        if updated != 1 {
            return Err("candidate changed while accepting it".into());
        }
        transaction
            .execute(
                "DELETE FROM memory_leases
                 WHERE kind='consolidation' AND owner=?1 AND workspace_id=?2
                   AND subject=?3 AND holder=?4",
                params![owner, workspace_id, candidate_id, holder],
            )
            .map_err(|error| error.to_string())?;
        transaction.commit().map_err(|error| error.to_string())
    }

    fn require_scope<'a>(
        owner: &'a str,
        workspace_id: &'a str,
    ) -> Result<(&'a str, &'a str), String> {
        let owner = owner.trim();
        let workspace_id = workspace_id.trim();
        if owner.is_empty() || workspace_id.is_empty() {
            return Err("authenticated owner and workspace_id are required".into());
        }
        Ok((owner, workspace_id))
    }

    fn selector_matches(
        selector: &ForgetSelector,
        id: &str,
        source_uri: Option<&str>,
        source_message_ids: &[String],
    ) -> bool {
        match selector {
            ForgetSelector::RecordId(value) => id == value,
            ForgetSelector::SourceUri(value) => source_uri == Some(value.as_str()),
            ForgetSelector::SourceMessageId(value) => source_message_ids
                .iter()
                .any(|candidate| candidate == value),
        }
    }

    fn metadata_strings(value: &serde_json::Value, key: &str) -> Vec<String> {
        value
            .get(key)
            .and_then(serde_json::Value::as_array)
            .map(|values| {
                values
                    .iter()
                    .filter_map(serde_json::Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    }

    fn collect_forget_closure(
        conn: &Connection,
        owner: &str,
        workspace_id: &str,
        selector: &ForgetSelector,
    ) -> Result<ForgetClosure, String> {
        let mut closure = ForgetClosure::default();
        let mut raw_statement = conn
            .prepare(
                "SELECT id,metadata FROM raw
                 WHERE owner=?1 AND workspace_id=?2",
            )
            .map_err(|error| error.to_string())?;
        let raw_rows = raw_statement
            .query_map(params![owner, workspace_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|error| error.to_string())?;
        for row in raw_rows {
            let (id, metadata) = row.map_err(|error| error.to_string())?;
            let metadata: serde_json::Value =
                serde_json::from_str(&metadata).unwrap_or(serde_json::Value::Null);
            let source_uri = metadata
                .get("source_uri")
                .and_then(serde_json::Value::as_str);
            let mut source_message_ids = Self::metadata_strings(&metadata, "source_message_ids");
            if let Some(message_id) = metadata
                .get("source_message_id")
                .and_then(serde_json::Value::as_str)
            {
                source_message_ids.push(message_id.to_string());
            }
            if Self::selector_matches(selector, &id, source_uri, &source_message_ids) {
                closure.raw_ids.push(id);
            }
        }

        let raw_ids: HashSet<&str> = closure.raw_ids.iter().map(String::as_str).collect();
        let mut candidate_statement = conn
            .prepare(
                "SELECT id,raw_evidence_ids,source_uri,source_message_ids,accepted_curated_id
                 FROM candidates WHERE owner=?1 AND workspace_id=?2",
            )
            .map_err(|error| error.to_string())?;
        let candidate_rows = candidate_statement
            .query_map(params![owner, workspace_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, Option<String>>(4)?,
                ))
            })
            .map_err(|error| error.to_string())?;
        for row in candidate_rows {
            let (id, raw_evidence, source_uri, source_messages, accepted_curated_id) =
                row.map_err(|error| error.to_string())?;
            let raw_evidence: Vec<String> = serde_json::from_str(&raw_evidence).unwrap_or_default();
            let source_messages: Vec<String> =
                serde_json::from_str(&source_messages).unwrap_or_default();
            let derived_from_raw = raw_evidence
                .iter()
                .any(|raw_id| raw_ids.contains(raw_id.as_str()));
            if derived_from_raw
                || Self::selector_matches(
                    selector,
                    &id,
                    (!source_uri.is_empty()).then_some(source_uri.as_str()),
                    &source_messages,
                )
            {
                closure.candidate_ids.push(id);
                if let Some(curated_id) = accepted_curated_id {
                    closure.curated_ids.push(curated_id);
                }
            }
        }

        let candidate_ids: HashSet<&str> =
            closure.candidate_ids.iter().map(String::as_str).collect();
        let mut curated_statement = conn
            .prepare(
                "SELECT id,metadata,source_message_ids FROM curated
                 WHERE owner=?1 AND workspace_id=?2",
            )
            .map_err(|error| error.to_string())?;
        let curated_rows = curated_statement
            .query_map(params![owner, workspace_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(|error| error.to_string())?;
        for row in curated_rows {
            let (id, metadata, source_messages) = row.map_err(|error| error.to_string())?;
            let metadata: serde_json::Value =
                serde_json::from_str(&metadata).unwrap_or(serde_json::Value::Null);
            let source_messages: Vec<String> =
                serde_json::from_str(&source_messages).unwrap_or_default();
            let source_uri = metadata
                .get("source_uri")
                .and_then(serde_json::Value::as_str);
            let candidate_match = metadata
                .get("candidate_id")
                .and_then(serde_json::Value::as_str)
                .is_some_and(|candidate_id| candidate_ids.contains(candidate_id));
            let raw_match = Self::metadata_strings(&metadata, "raw_evidence_ids")
                .iter()
                .any(|raw_id| raw_ids.contains(raw_id.as_str()));
            if candidate_match
                || raw_match
                || Self::selector_matches(selector, &id, source_uri, &source_messages)
            {
                closure.curated_ids.push(id);
            }
        }

        closure.sort_dedup();
        let curated_ids: HashSet<&str> = closure.curated_ids.iter().map(String::as_str).collect();
        let candidate_ids: HashSet<&str> =
            closure.candidate_ids.iter().map(String::as_str).collect();
        let mut graph_statement = conn
            .prepare(
                "SELECT id,ref_table,ref_id,candidate_id FROM graph_nodes
                 WHERE owner=?1 AND workspace_id=?2",
            )
            .map_err(|error| error.to_string())?;
        let graph_rows = graph_statement
            .query_map(params![owner, workspace_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            })
            .map_err(|error| error.to_string())?;
        for row in graph_rows {
            let (id, ref_table, ref_id, candidate_id) = row.map_err(|error| error.to_string())?;
            let projection_match = ref_table.as_deref() == Some("curated")
                && ref_id
                    .as_deref()
                    .is_some_and(|ref_id| curated_ids.contains(ref_id));
            let candidate_match = candidate_id
                .as_deref()
                .is_some_and(|candidate_id| candidate_ids.contains(candidate_id));
            if projection_match
                || candidate_match
                || matches!(selector, ForgetSelector::RecordId(value) if value == &id)
            {
                closure.graph_node_ids.push(id);
            }
        }
        closure.sort_dedup();
        Ok(closure)
    }

    fn closure_token(
        owner: &str,
        workspace_id: &str,
        selector: &ForgetSelector,
        closure: &ForgetClosure,
    ) -> Result<String, String> {
        serde_json::to_vec(&(owner, workspace_id, selector, closure))
            .map(|value| crate::privacy::content_hash(&String::from_utf8_lossy(&value)))
            .map_err(|error| error.to_string())
    }

    fn require_operation_id(operation_id: &str) -> Result<&str, String> {
        if operation_id.len() != 32
            || !operation_id
                .as_bytes()
                .iter()
                .all(|byte| byte.is_ascii_hexdigit())
        {
            return Err("operation_id must be exactly 32 hexadecimal characters".into());
        }
        Ok(operation_id)
    }

    fn forget_operation_key(
        owner: &str,
        workspace_id: &str,
        selector: &ForgetSelector,
        preview_token: &str,
    ) -> Result<String, String> {
        serde_json::to_vec(&(owner, workspace_id, selector, preview_token))
            .map(|value| crate::privacy::content_hash(&String::from_utf8_lossy(&value)))
            .map_err(|error| error.to_string())
    }

    pub fn preview_forget(
        &self,
        owner: &str,
        workspace_id: &str,
        selector: ForgetSelector,
    ) -> Result<ForgetPreview, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let conn = self.conn.lock().unwrap();
        let closure = Self::collect_forget_closure(&conn, owner, workspace_id, &selector)?;
        let token = Self::closure_token(owner, workspace_id, &selector, &closure)?;
        Ok(ForgetPreview {
            selector,
            closure,
            token,
        })
    }

    fn snapshot_closure(
        conn: &Connection,
        closure: &ForgetClosure,
    ) -> Result<ForgetSnapshot, String> {
        let mut snapshot = ForgetSnapshot::default();
        for id in &closure.raw_ids {
            let row = conn
                .query_row(
                    "SELECT id,role,content,session_key,session_id,workspace_id,owner,
                            recorded_at,metadata,workspace_path,embedding
                     FROM raw WHERE id=?1",
                    params![id],
                    |row| {
                        let record = Self::raw_row_to_record(row)?;
                        let embedding = row
                            .get::<_, Option<Vec<u8>>>(10)?
                            .map(|blob| Self::blob_to_embedding(&blob));
                        Ok(RawSnapshot { record, embedding })
                    },
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some(row) = row {
                snapshot.raw.push(row);
            }
        }
        for id in &closure.candidate_ids {
            let row = conn
                .query_row(
                    "SELECT id,content,kind,confidence_score,importance_score,owner,
                            workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,
                            evidence_role,source,source_event_id,dedup_key,status,reason,
                            accepted_curated_id,created_at,updated_at,
                            source_uri,source_revision,content_hash,source_message_ids
                     FROM candidates WHERE id=?1",
                    params![id],
                    Self::candidate_row,
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some(row) = row {
                snapshot.candidates.push(row);
            }
        }
        for id in &closure.curated_ids {
            let row = conn
                .query_row(
                    "SELECT id,content,kind,priority,trust_score,confidence_score,
                            importance_score,scene_name,source,source_type,owner,workspace_id,
                            session_key,session_id,tags,source_message_ids,timestamps,created_at,
                            updated_at,archived,last_accessed_at,exempt_from_decay,
                            exempt_from_dedup,metadata,workspace_path,embedding
                     FROM curated WHERE id=?1",
                    params![id],
                    |row| {
                        let record = Self::row_to_record(row)?;
                        let embedding = row
                            .get::<_, Option<Vec<u8>>>(25)?
                            .map(|blob| Self::blob_to_embedding(&blob));
                        Ok(CuratedSnapshot { record, embedding })
                    },
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some(row) = row {
                snapshot.curated.push(row);
            }
        }
        let mut edge_ids = HashSet::new();
        let mut fact_ids = HashSet::new();
        let mut cue_ids = HashSet::new();
        for id in &closure.graph_node_ids {
            let node = conn
                .query_row(
                    "SELECT id,kind,label,name,norm_name,layer,ref_table,ref_id,trust,
                            created_at,last_seen,owner,workspace_id,candidate_id,status
                     FROM graph_nodes WHERE id=?1",
                    params![id],
                    |row| {
                        Ok(GraphNodeSnapshot {
                            id: row.get(0)?,
                            kind: row.get(1)?,
                            label: row.get(2)?,
                            name: row.get(3)?,
                            norm_name: row.get(4)?,
                            layer: row.get(5)?,
                            ref_table: row.get(6)?,
                            ref_id: row.get(7)?,
                            trust: row.get(8)?,
                            created_at: row.get(9)?,
                            last_seen: row.get(10)?,
                            owner: row.get(11)?,
                            workspace_id: row.get(12)?,
                            candidate_id: row.get(13)?,
                            status: row.get(14)?,
                        })
                    },
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some(node) = node {
                snapshot.graph_nodes.push(node);
            }
            let mut edge_statement = conn
                .prepare(
                    "SELECT id,src_id,tag,dst_id,fact_id,weight,traversal_count,trust,
                            created_at,last_seen,owner,workspace_id,candidate_id,status
                     FROM graph_edges WHERE src_id=?1 OR dst_id=?1",
                )
                .map_err(|error| error.to_string())?;
            let edges = edge_statement
                .query_map(params![id], |row| {
                    Ok(GraphEdgeSnapshot {
                        id: row.get(0)?,
                        src_id: row.get(1)?,
                        tag: row.get(2)?,
                        dst_id: row.get(3)?,
                        fact_id: row.get(4)?,
                        weight: row.get(5)?,
                        traversal_count: row.get(6)?,
                        trust: row.get(7)?,
                        created_at: row.get(8)?,
                        last_seen: row.get(9)?,
                        owner: row.get(10)?,
                        workspace_id: row.get(11)?,
                        candidate_id: row.get(12)?,
                        status: row.get(13)?,
                    })
                })
                .map_err(|error| error.to_string())?;
            for edge in edges {
                let edge = edge.map_err(|error| error.to_string())?;
                if edge_ids.insert(edge.id.clone()) {
                    if let Some(fact_id) = &edge.fact_id {
                        fact_ids.insert(fact_id.clone());
                    }
                    snapshot.graph_edges.push(edge);
                }
            }
            let mut cue_statement = conn
                .prepare(
                    "SELECT cue,node_id,source,created_at,owner,workspace_id,candidate_id,status
                     FROM graph_cues WHERE node_id=?1",
                )
                .map_err(|error| error.to_string())?;
            let cues = cue_statement
                .query_map(params![id], |row| {
                    Ok(GraphCueSnapshot {
                        cue: row.get(0)?,
                        node_id: row.get(1)?,
                        source: row.get(2)?,
                        created_at: row.get(3)?,
                        owner: row.get(4)?,
                        workspace_id: row.get(5)?,
                        candidate_id: row.get(6)?,
                        status: row.get(7)?,
                    })
                })
                .map_err(|error| error.to_string())?;
            for cue in cues {
                let cue = cue.map_err(|error| error.to_string())?;
                if cue_ids.insert(format!("{}\u{1f}{}", cue.cue, cue.node_id)) {
                    snapshot.graph_cues.push(cue);
                }
            }
        }
        for candidate_id in &closure.candidate_ids {
            let mut fact_statement = conn
                .prepare("SELECT id FROM facts WHERE candidate_id=?1")
                .map_err(|error| error.to_string())?;
            for fact_id in fact_statement
                .query_map(params![candidate_id], |row| row.get::<_, String>(0))
                .map_err(|error| error.to_string())?
            {
                fact_ids.insert(fact_id.map_err(|error| error.to_string())?);
            }
        }
        for id in fact_ids {
            let fact = conn
                .query_row(
                    "SELECT id,content,entities,trust_score,created_at,updated_at,embedding,
                            owner,workspace_id,candidate_id,status
                     FROM facts WHERE id=?1",
                    params![id],
                    |row| {
                        Ok(FactSnapshot {
                            id: row.get(0)?,
                            content: row.get(1)?,
                            entities: row.get(2)?,
                            trust_score: row.get(3)?,
                            created_at: row.get(4)?,
                            updated_at: row.get(5)?,
                            embedding: row
                                .get::<_, Option<Vec<u8>>>(6)?
                                .map(|blob| Self::blob_to_embedding(&blob)),
                            owner: row.get(7)?,
                            workspace_id: row.get(8)?,
                            candidate_id: row.get(9)?,
                            status: row.get(10)?,
                        })
                    },
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some(fact) = fact {
                snapshot.facts.push(fact);
            }
        }
        Ok(snapshot)
    }

    fn delete_closure_inner(conn: &Connection, closure: &ForgetClosure) -> Result<(), String> {
        for id in &closure.raw_ids {
            let rowid = conn
                .query_row("SELECT rowid FROM raw WHERE id=?1", params![id], |row| {
                    row.get::<_, i64>(0)
                })
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some(rowid) = rowid {
                let _ = conn.execute("DELETE FROM raw_fts WHERE rowid=?1", params![rowid]);
                conn.execute("DELETE FROM raw WHERE id=?1", params![id])
                    .map_err(|error| error.to_string())?;
            }
        }
        for id in &closure.curated_ids {
            let rowid = conn
                .query_row(
                    "SELECT rowid FROM curated WHERE id=?1",
                    params![id],
                    |row| row.get::<_, i64>(0),
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some(rowid) = rowid {
                let _ = conn.execute("DELETE FROM curated_fts WHERE rowid=?1", params![rowid]);
                super::graph::delete_curated_projection_inner(conn, id)
                    .map_err(|error| error.to_string())?;
                conn.execute("DELETE FROM curated WHERE id=?1", params![id])
                    .map_err(|error| error.to_string())?;
            }
        }
        for id in &closure.candidate_ids {
            conn.execute("DELETE FROM candidates WHERE id=?1", params![id])
                .map_err(|error| error.to_string())?;
        }
        for node_id in &closure.graph_node_ids {
            let _ = conn.execute(
                "DELETE FROM facts_fts WHERE rowid IN (
                    SELECT f.rowid FROM facts f JOIN graph_edges e ON e.fact_id=f.id
                    WHERE e.src_id=?1 OR e.dst_id=?1
                 )",
                params![node_id],
            );
            conn.execute(
                "DELETE FROM facts WHERE id IN (
                    SELECT fact_id FROM graph_edges
                    WHERE (src_id=?1 OR dst_id=?1) AND fact_id IS NOT NULL
                 )",
                params![node_id],
            )
            .map_err(|error| error.to_string())?;
            conn.execute(
                "DELETE FROM graph_edges WHERE src_id=?1 OR dst_id=?1",
                params![node_id],
            )
            .map_err(|error| error.to_string())?;
            let _ = conn.execute(
                "DELETE FROM graph_cues_fts WHERE node_id=?1",
                params![node_id],
            );
            conn.execute("DELETE FROM graph_cues WHERE node_id=?1", params![node_id])
                .map_err(|error| error.to_string())?;
            conn.execute("DELETE FROM graph_nodes WHERE id=?1", params![node_id])
                .map_err(|error| error.to_string())?;
        }
        for candidate_id in &closure.candidate_ids {
            let _ = conn.execute(
                "DELETE FROM facts_fts WHERE rowid IN (
                    SELECT rowid FROM facts WHERE candidate_id=?1
                 )",
                params![candidate_id],
            );
            conn.execute(
                "DELETE FROM facts WHERE candidate_id=?1",
                params![candidate_id],
            )
            .map_err(|error| error.to_string())?;
            conn.execute(
                "DELETE FROM graph_edges WHERE candidate_id=?1",
                params![candidate_id],
            )
            .map_err(|error| error.to_string())?;
            conn.execute(
                "DELETE FROM graph_cues WHERE candidate_id=?1",
                params![candidate_id],
            )
            .map_err(|error| error.to_string())?;
            conn.execute(
                "DELETE FROM graph_nodes WHERE candidate_id=?1",
                params![candidate_id],
            )
            .map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    fn retention_policy_inner(
        conn: &Connection,
        owner: &str,
        workspace_id: &str,
    ) -> Result<RetentionPolicy, String> {
        conn.query_row(
            "SELECT raw_days,candidate_days,curated_days,graph_days,recovery_seconds
             FROM memory_retention_policy WHERE owner=?1 AND workspace_id=?2",
            params![owner, workspace_id],
            |row| {
                Ok(RetentionPolicy {
                    raw_days: row.get::<_, i64>(0)?.max(0) as u32,
                    candidate_days: row.get::<_, i64>(1)?.max(0) as u32,
                    curated_days: row.get::<_, Option<i64>>(2)?.map(|v| v.max(0) as u32),
                    graph_days: row.get::<_, Option<i64>>(3)?.map(|v| v.max(0) as u32),
                    recovery_seconds: row.get::<_, i64>(4)?.max(0) as u32,
                })
            },
        )
        .optional()
        .map_err(|error| error.to_string())
        .map(|policy| policy.unwrap_or_default())
    }

    pub fn retention_policy(
        &self,
        owner: &str,
        workspace_id: &str,
    ) -> Result<RetentionPolicy, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let conn = self.conn.lock().unwrap();
        Self::retention_policy_inner(&conn, owner, workspace_id)
    }

    pub fn set_retention_policy(
        &self,
        owner: &str,
        workspace_id: &str,
        policy: &RetentionPolicy,
    ) -> Result<RetentionPolicy, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        policy.validate()?;
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO memory_retention_policy(
                owner,workspace_id,raw_days,candidate_days,curated_days,graph_days,
                recovery_seconds,updated_at
             ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)
             ON CONFLICT(owner,workspace_id) DO UPDATE SET
                raw_days=excluded.raw_days,
                candidate_days=excluded.candidate_days,
                curated_days=excluded.curated_days,
                graph_days=excluded.graph_days,
                recovery_seconds=excluded.recovery_seconds,
                updated_at=excluded.updated_at",
            params![
                owner,
                workspace_id,
                policy.raw_days,
                policy.candidate_days,
                policy.curated_days,
                policy.graph_days,
                policy.recovery_seconds,
                chrono::Utc::now().to_rfc3339()
            ],
        )
        .map_err(|error| error.to_string())?;
        Ok(policy.clone())
    }

    pub fn commit_forget(
        &self,
        owner: &str,
        workspace_id: &str,
        selector: ForgetSelector,
        preview_token: &str,
    ) -> Result<ForgetCommit, String> {
        self.commit_forget_at(
            owner,
            workspace_id,
            selector,
            preview_token,
            None,
            chrono::Utc::now(),
        )
    }

    pub fn commit_forget_with_operation(
        &self,
        owner: &str,
        workspace_id: &str,
        selector: ForgetSelector,
        preview_token: &str,
        operation_id: &str,
    ) -> Result<ForgetCommit, String> {
        Self::require_operation_id(operation_id)?;
        self.commit_forget_at(
            owner,
            workspace_id,
            selector,
            preview_token,
            Some(operation_id),
            chrono::Utc::now(),
        )
    }

    fn commit_forget_at(
        &self,
        owner: &str,
        workspace_id: &str,
        selector: ForgetSelector,
        preview_token: &str,
        operation_id: Option<&str>,
        now: chrono::DateTime<chrono::Utc>,
    ) -> Result<ForgetCommit, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let operation_key = operation_id
            .map(|_| Self::forget_operation_key(owner, workspace_id, &selector, preview_token))
            .transpose()?;
        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        if let (Some(operation_id), Some(operation_key)) = (operation_id, operation_key.as_deref())
        {
            let prior = transaction
                .query_row(
                    "SELECT id,affected_ids,recover_until,operation_key
                     FROM memory_tombstones
                     WHERE owner=?1 AND workspace_id=?2 AND operation_id=?3",
                    params![owner, workspace_id, operation_id],
                    |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, Option<String>>(2)?,
                            row.get::<_, Option<String>>(3)?,
                        ))
                    },
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some((tombstone_id, affected_ids, recover_until, prior_key)) = prior {
                if prior_key.as_deref() != Some(operation_key) {
                    return Err(
                        "operation_id is already bound to a different forget request".into(),
                    );
                }
                let closure =
                    serde_json::from_str(&affected_ids).map_err(|error| error.to_string())?;
                return Ok(ForgetCommit {
                    tombstone_id,
                    closure,
                    recover_until,
                });
            }
        }
        transaction
            .execute(
                "UPDATE memory_tombstones SET recovery_payload=NULL
                 WHERE recovered_at IS NULL AND recover_until IS NOT NULL
                   AND recover_until<?1",
                params![now.to_rfc3339()],
            )
            .map_err(|error| error.to_string())?;
        let closure = Self::collect_forget_closure(&transaction, owner, workspace_id, &selector)?;
        let token = Self::closure_token(owner, workspace_id, &selector, &closure)?;
        if token != preview_token {
            return Err("forget preview is stale; preview again before committing".into());
        }
        if closure.is_empty() {
            return Err("forget selector matched no records in this scope".into());
        }
        let policy = Self::retention_policy_inner(&transaction, owner, workspace_id)?;
        let snapshot = (policy.recovery_seconds > 0)
            .then(|| Self::snapshot_closure(&transaction, &closure))
            .transpose()?;
        Self::delete_closure_inner(&transaction, &closure)?;
        let created_at = now.to_rfc3339();
        let recover_until = (policy.recovery_seconds > 0).then(|| {
            (now + chrono::Duration::seconds(policy.recovery_seconds as i64)).to_rfc3339()
        });
        let source_key = crate::privacy::content_hash(
            &serde_json::to_string(&selector).map_err(|error| error.to_string())?,
        );
        let tombstone_id = format!(
            "forget_{}",
            &crate::privacy::content_hash(&format!(
                "{owner}\u{1f}{workspace_id}\u{1f}{created_at}\u{1f}{source_key}"
            ))[..24]
        );
        let affected_ids = serde_json::to_string(&closure).map_err(|error| error.to_string())?;
        let recovery_payload = snapshot
            .as_ref()
            .map(serde_json::to_string)
            .transpose()
            .map_err(|error| error.to_string())?;
        transaction
            .execute(
                "INSERT INTO memory_tombstones(
                    id,owner,workspace_id,source_key,affected_ids,recovery_payload,
                    created_at,recover_until,recovered_at,operation_id,operation_key
                 ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,NULL,?9,?10)",
                params![
                    tombstone_id,
                    owner,
                    workspace_id,
                    source_key,
                    affected_ids,
                    recovery_payload,
                    created_at,
                    recover_until,
                    operation_id,
                    operation_key,
                ],
            )
            .map_err(|error| error.to_string())?;
        transaction.commit().map_err(|error| error.to_string())?;
        Ok(ForgetCommit {
            tombstone_id,
            closure,
            recover_until,
        })
    }

    pub fn forget_operation_status(
        &self,
        owner: &str,
        workspace_id: &str,
        operation_id: &str,
    ) -> Result<ForgetOperationStatus, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let operation_id = Self::require_operation_id(operation_id)?;
        let conn = self.conn.lock().unwrap();
        let row = conn
            .query_row(
                "SELECT id,affected_ids,recover_until,recovered_at
                 FROM memory_tombstones
                 WHERE owner=?1 AND workspace_id=?2 AND operation_id=?3",
                params![owner, workspace_id, operation_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, Option<String>>(2)?,
                        row.get::<_, Option<String>>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| error.to_string())?;
        let Some((tombstone_id, affected_ids, recover_until, recovered_at)) = row else {
            return Ok(ForgetOperationStatus {
                operation_id: operation_id.into(),
                state: ForgetOperationState::Absent,
                tombstone_id: None,
                closure: None,
                recover_until: None,
                recovered_at: None,
            });
        };
        let closure = serde_json::from_str(&affected_ids).map_err(|error| error.to_string())?;
        let state = if recovered_at.is_some() {
            ForgetOperationState::Restored
        } else {
            ForgetOperationState::Committed
        };
        Ok(ForgetOperationStatus {
            operation_id: operation_id.into(),
            state,
            tombstone_id: Some(tombstone_id),
            closure: Some(closure),
            recover_until,
            recovered_at,
        })
    }

    pub fn restore_forget(
        &self,
        owner: &str,
        workspace_id: &str,
        tombstone_id: &str,
    ) -> Result<ForgetClosure, String> {
        self.restore_forget_at(owner, workspace_id, tombstone_id, chrono::Utc::now())
    }

    fn restore_forget_at(
        &self,
        owner: &str,
        workspace_id: &str,
        tombstone_id: &str,
        now: chrono::DateTime<chrono::Utc>,
    ) -> Result<ForgetClosure, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        let (affected_ids, payload, recover_until, recovered_at) = transaction
            .query_row(
                "SELECT affected_ids,recovery_payload,recover_until,recovered_at
                 FROM memory_tombstones
                 WHERE id=?1 AND owner=?2 AND workspace_id=?3",
                params![tombstone_id, owner, workspace_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, Option<String>>(2)?,
                        row.get::<_, Option<String>>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| error.to_string())?
            .ok_or_else(|| "forget tombstone not found in this scope".to_string())?;
        let closure: ForgetClosure =
            serde_json::from_str(&affected_ids).map_err(|error| error.to_string())?;
        if recovered_at.is_some() {
            return Ok(closure);
        }
        let recover_until =
            recover_until.ok_or_else(|| "forget tombstone is not recoverable".to_string())?;
        if recover_until < now.to_rfc3339() {
            transaction
                .execute(
                    "UPDATE memory_tombstones SET recovery_payload=NULL
                     WHERE id=?1",
                    params![tombstone_id],
                )
                .map_err(|error| error.to_string())?;
            transaction.commit().map_err(|error| error.to_string())?;
            return Err("forget recovery window has expired".into());
        }
        let payload =
            payload.ok_or_else(|| "forget recovery payload is unavailable".to_string())?;
        let snapshot: ForgetSnapshot =
            serde_json::from_str(&payload).map_err(|error| error.to_string())?;
        for id in closure
            .raw_ids
            .iter()
            .chain(&closure.candidate_ids)
            .chain(&closure.curated_ids)
        {
            let exists = transaction
                .query_row(
                    "SELECT EXISTS(
                        SELECT 1 FROM raw WHERE id=?1
                        UNION ALL SELECT 1 FROM candidates WHERE id=?1
                        UNION ALL SELECT 1 FROM curated WHERE id=?1
                     )",
                    params![id],
                    |row| row.get::<_, bool>(0),
                )
                .map_err(|error| error.to_string())?;
            if exists {
                return Err(format!("cannot restore because id {id} now exists"));
            }
        }
        for snapshot in &snapshot.raw {
            let embedding = snapshot.embedding.as_deref().map(Self::embedding_to_blob);
            transaction
                .execute(
                    "INSERT INTO raw(
                        id,role,content,session_key,session_id,workspace_id,owner,
                        recorded_at,metadata,workspace_path,embedding
                     ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
                    params![
                        snapshot.record.id,
                        snapshot.record.role,
                        snapshot.record.content,
                        snapshot.record.session_key,
                        snapshot.record.session_id,
                        snapshot.record.workspace_id,
                        snapshot.record.owner,
                        snapshot.record.recorded_at,
                        serde_json::to_string(&snapshot.record.metadata)
                            .map_err(|error| error.to_string())?,
                        snapshot.record.workspace_path,
                        embedding,
                    ],
                )
                .map_err(|error| error.to_string())?;
            let rowid = transaction.last_insert_rowid();
            let _ = transaction.execute(
                "INSERT INTO raw_fts(rowid,content,workspace_id) VALUES (?1,?2,?3)",
                params![rowid, snapshot.record.content, snapshot.record.workspace_id],
            );
        }
        for candidate in &snapshot.candidates {
            Self::insert_candidate_inner(&transaction, candidate, false)
                .map_err(|error| error.to_string())?;
        }
        for node in &snapshot.graph_nodes {
            transaction
                .execute(
                    "INSERT INTO graph_nodes(
                        id,kind,label,name,norm_name,layer,ref_table,ref_id,trust,
                        created_at,last_seen,owner,workspace_id,candidate_id,status
                     ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15)",
                    params![
                        node.id,
                        node.kind,
                        node.label,
                        node.name,
                        node.norm_name,
                        node.layer,
                        node.ref_table,
                        node.ref_id,
                        node.trust,
                        node.created_at,
                        node.last_seen,
                        node.owner,
                        node.workspace_id,
                        node.candidate_id,
                        node.status,
                    ],
                )
                .map_err(|error| error.to_string())?;
        }
        for fact in &snapshot.facts {
            transaction
                .execute(
                    "INSERT INTO facts(
                        id,content,entities,trust_score,created_at,updated_at,embedding,
                        owner,workspace_id,candidate_id,status
                     ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
                    params![
                        fact.id,
                        fact.content,
                        fact.entities,
                        fact.trust_score,
                        fact.created_at,
                        fact.updated_at,
                        fact.embedding.as_deref().map(Self::embedding_to_blob),
                        fact.owner,
                        fact.workspace_id,
                        fact.candidate_id,
                        fact.status,
                    ],
                )
                .map_err(|error| error.to_string())?;
            let rowid = transaction.last_insert_rowid();
            if fact.status == "active" {
                let _ = transaction.execute(
                    "INSERT INTO facts_fts(rowid,content,entities) VALUES (?1,?2,?3)",
                    params![rowid, fact.content, fact.entities],
                );
            }
        }
        for edge in &snapshot.graph_edges {
            transaction
                .execute(
                    "INSERT INTO graph_edges(
                        id,src_id,tag,dst_id,fact_id,weight,traversal_count,trust,
                        created_at,last_seen,owner,workspace_id,candidate_id,status
                     ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
                    params![
                        edge.id,
                        edge.src_id,
                        edge.tag,
                        edge.dst_id,
                        edge.fact_id,
                        edge.weight,
                        edge.traversal_count,
                        edge.trust,
                        edge.created_at,
                        edge.last_seen,
                        edge.owner,
                        edge.workspace_id,
                        edge.candidate_id,
                        edge.status,
                    ],
                )
                .map_err(|error| error.to_string())?;
        }
        for cue in &snapshot.graph_cues {
            transaction
                .execute(
                    "INSERT INTO graph_cues(
                        cue,node_id,source,created_at,owner,workspace_id,candidate_id,status
                     ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
                    params![
                        cue.cue,
                        cue.node_id,
                        cue.source,
                        cue.created_at,
                        cue.owner,
                        cue.workspace_id,
                        cue.candidate_id,
                        cue.status,
                    ],
                )
                .map_err(|error| error.to_string())?;
        }
        for snapshot in &snapshot.curated {
            Self::upsert_curated_inner(
                &transaction,
                &snapshot.record,
                snapshot.embedding.as_deref(),
            )?;
        }
        transaction
            .execute(
                "UPDATE memory_tombstones
                 SET recovered_at=?1,recovery_payload=NULL
                 WHERE id=?2",
                params![now.to_rfc3339(), tombstone_id],
            )
            .map_err(|error| error.to_string())?;
        transaction.commit().map_err(|error| error.to_string())?;
        Ok(closure)
    }

    pub fn expire_retention(
        &self,
        owner: &str,
        workspace_id: &str,
    ) -> Result<ForgetClosure, String> {
        self.expire_retention_with_preview(owner, workspace_id, None)
    }

    pub fn preview_expire_retention(
        &self,
        owner: &str,
        workspace_id: &str,
    ) -> Result<RetentionExpiryPreview, String> {
        self.preview_expire_retention_at(owner, workspace_id, chrono::Utc::now())
    }

    fn preview_expire_retention_at(
        &self,
        owner: &str,
        workspace_id: &str,
        now: chrono::DateTime<chrono::Utc>,
    ) -> Result<RetentionExpiryPreview, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Deferred)
            .map_err(|error| error.to_string())?;
        let closure = Self::collect_retention_expiry(&transaction, owner, workspace_id, now)?;
        let token = Self::retention_expiry_token(owner, workspace_id, &closure)?;
        transaction.commit().map_err(|error| error.to_string())?;
        Ok(RetentionExpiryPreview { closure, token })
    }

    pub fn expire_retention_with_preview(
        &self,
        owner: &str,
        workspace_id: &str,
        preview_token: Option<&str>,
    ) -> Result<ForgetClosure, String> {
        self.expire_retention_with_preview_at(
            owner,
            workspace_id,
            preview_token,
            None,
            chrono::Utc::now(),
        )
    }

    pub fn expire_retention_with_operation(
        &self,
        owner: &str,
        workspace_id: &str,
        preview_token: &str,
        operation_id: &str,
    ) -> Result<ForgetClosure, String> {
        let operation_id = Self::require_operation_id(operation_id)?;
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let request_key = Self::retention_operation_key(owner, workspace_id, preview_token)?;
        self.expire_retention_with_preview_at(
            owner,
            workspace_id,
            Some(preview_token),
            Some((operation_id, request_key.as_str())),
            chrono::Utc::now(),
        )
    }

    pub fn retention_operation_status(
        &self,
        owner: &str,
        workspace_id: &str,
        operation_id: &str,
    ) -> Result<RetentionOperationStatus, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let operation_id = Self::require_operation_id(operation_id)?;
        let conn = self.conn.lock().unwrap();
        let row = conn
            .query_row(
                "SELECT closure,committed_at FROM memory_retention_operations
                 WHERE owner=?1 AND workspace_id=?2 AND operation_id=?3",
                params![owner, workspace_id, operation_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(|error| error.to_string())?;
        let Some((closure, committed_at)) = row else {
            return Ok(RetentionOperationStatus {
                operation_id: operation_id.into(),
                state: RetentionOperationState::Absent,
                closure: None,
                committed_at: None,
            });
        };
        Ok(RetentionOperationStatus {
            operation_id: operation_id.into(),
            state: RetentionOperationState::Committed,
            closure: Some(serde_json::from_str(&closure).map_err(|error| error.to_string())?),
            committed_at: Some(committed_at),
        })
    }

    fn collect_retention_expiry(
        conn: &Connection,
        owner: &str,
        workspace_id: &str,
        now: chrono::DateTime<chrono::Utc>,
    ) -> Result<ForgetClosure, String> {
        let policy = Self::retention_policy_inner(conn, owner, workspace_id)?;
        let mut closure = ForgetClosure::default();
        let raw_cutoff = (now - chrono::Duration::days(policy.raw_days as i64)).to_rfc3339();
        let mut raw_statement = conn
            .prepare(
                "SELECT id FROM raw
                 WHERE owner=?1 AND workspace_id=?2 AND recorded_at<?3",
            )
            .map_err(|error| error.to_string())?;
        closure.raw_ids = raw_statement
            .query_map(params![owner, workspace_id, raw_cutoff], |row| row.get(0))
            .map_err(|error| error.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())?;
        drop(raw_statement);

        let candidate_cutoff =
            (now - chrono::Duration::days(policy.candidate_days as i64)).to_rfc3339();
        let mut candidate_statement = conn
            .prepare(
                "SELECT id FROM candidates
                 WHERE owner=?1 AND workspace_id=?2 AND updated_at<?3",
            )
            .map_err(|error| error.to_string())?;
        closure.candidate_ids = candidate_statement
            .query_map(params![owner, workspace_id, candidate_cutoff], |row| {
                row.get(0)
            })
            .map_err(|error| error.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())?;
        drop(candidate_statement);

        if let Some(days) = policy.curated_days {
            let cutoff = (now - chrono::Duration::days(days as i64)).to_rfc3339();
            let mut statement = conn
                .prepare(
                    "SELECT id FROM curated
                     WHERE owner=?1 AND workspace_id=?2 AND updated_at<?3
                       AND exempt_from_decay=0",
                )
                .map_err(|error| error.to_string())?;
            closure.curated_ids = statement
                .query_map(params![owner, workspace_id, cutoff], |row| row.get(0))
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
        }
        if let Some(days) = policy.graph_days {
            let cutoff = (now - chrono::Duration::days(days as i64)).to_rfc3339();
            let mut statement = conn
                .prepare(
                    "SELECT id FROM graph_nodes
                     WHERE owner=?1 AND workspace_id=?2 AND last_seen<?3
                       AND ref_table IS NULL",
                )
                .map_err(|error| error.to_string())?;
            closure.graph_node_ids = statement
                .query_map(params![owner, workspace_id, cutoff], |row| row.get(0))
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
        }
        closure.sort_dedup();
        Ok(closure)
    }

    fn retention_expiry_token(
        owner: &str,
        workspace_id: &str,
        closure: &ForgetClosure,
    ) -> Result<String, String> {
        serde_json::to_vec(&(owner, workspace_id, closure))
            .map(|value| crate::privacy::content_hash(&String::from_utf8_lossy(&value)))
            .map_err(|error| error.to_string())
    }

    fn retention_operation_key(
        owner: &str,
        workspace_id: &str,
        preview_token: &str,
    ) -> Result<String, String> {
        serde_json::to_vec(&("retention_expiry", owner, workspace_id, preview_token))
            .map(|value| crate::privacy::content_hash(&String::from_utf8_lossy(&value)))
            .map_err(|error| error.to_string())
    }

    fn expire_retention_with_preview_at(
        &self,
        owner: &str,
        workspace_id: &str,
        preview_token: Option<&str>,
        operation: Option<(&str, &str)>,
        now: chrono::DateTime<chrono::Utc>,
    ) -> Result<ForgetClosure, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        if let Some((operation_id, request_key)) = operation {
            let prior = transaction
                .query_row(
                    "SELECT request_key,closure FROM memory_retention_operations
                     WHERE owner=?1 AND workspace_id=?2 AND operation_id=?3",
                    params![owner, workspace_id, operation_id],
                    |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
                )
                .optional()
                .map_err(|error| error.to_string())?;
            if let Some((prior_key, closure)) = prior {
                if prior_key != request_key {
                    return Err(
                        "operation_id is already bound to a different retention expiry request"
                            .into(),
                    );
                }
                return serde_json::from_str(&closure).map_err(|error| error.to_string());
            }
        }
        let closure = Self::collect_retention_expiry(&transaction, owner, workspace_id, now)?;
        if let Some(preview_token) = preview_token {
            let current_token = Self::retention_expiry_token(owner, workspace_id, &closure)?;
            if current_token != preview_token {
                return Err(
                    "retention expiry preview is stale; preview again before expiring".into(),
                );
            }
        }
        Self::delete_closure_inner(&transaction, &closure)?;
        transaction
            .execute(
                "UPDATE memory_tombstones SET recovery_payload=NULL
                 WHERE recovered_at IS NULL AND recover_until IS NOT NULL
                   AND recover_until<?1",
                params![now.to_rfc3339()],
            )
            .map_err(|error| error.to_string())?;
        if let Some((operation_id, request_key)) = operation {
            transaction
                .execute(
                    "INSERT INTO memory_retention_operations(
                        owner,workspace_id,operation_id,request_key,closure,committed_at
                     ) VALUES (?1,?2,?3,?4,?5,?6)",
                    params![
                        owner,
                        workspace_id,
                        operation_id,
                        request_key,
                        serde_json::to_string(&closure).map_err(|error| error.to_string())?,
                        now.to_rfc3339(),
                    ],
                )
                .map_err(|error| error.to_string())?;
        }
        transaction.commit().map_err(|error| error.to_string())?;
        Ok(closure)
    }

    pub fn export_scope(
        &self,
        owner: &str,
        workspace_id: &str,
    ) -> Result<serde_json::Value, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let conn = self.conn.lock().unwrap();
        let raw = {
            let mut statement = conn
                .prepare(
                    "SELECT id,role,content,session_key,session_id,workspace_id,owner,
                            recorded_at,metadata,workspace_path
                     FROM raw WHERE owner=?1 AND workspace_id=?2 ORDER BY recorded_at,id",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], Self::raw_row_to_record)
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let candidates = {
            let mut statement = conn
                .prepare(
                    "SELECT id,content,kind,confidence_score,importance_score,owner,
                            workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,
                            evidence_role,source,source_event_id,dedup_key,status,reason,
                            accepted_curated_id,created_at,updated_at,
                            source_uri,source_revision,content_hash,source_message_ids
                     FROM candidates WHERE owner=?1 AND workspace_id=?2 ORDER BY created_at,id",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], Self::candidate_row)
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let curated = {
            let mut statement = conn
                .prepare(
                    "SELECT id,content,kind,priority,trust_score,confidence_score,
                            importance_score,scene_name,source,source_type,owner,workspace_id,
                            session_key,session_id,tags,source_message_ids,timestamps,created_at,
                            updated_at,archived,last_accessed_at,exempt_from_decay,
                            exempt_from_dedup,metadata,workspace_path
                     FROM curated WHERE owner=?1 AND workspace_id=?2 ORDER BY created_at,id",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], Self::row_to_record)
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let quarantine = {
            let mut statement = conn
                .prepare(
                    "SELECT id,tier,original_id,content,payload,reason,quarantined_at
                     FROM memory_quarantine
                     WHERE owner=?1 AND workspace_id=?2 ORDER BY quarantined_at,id",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], |row| {
                    let payload: String = row.get(4)?;
                    Ok(serde_json::json!({
                        "id": row.get::<_, String>(0)?,
                        "tier": row.get::<_, String>(1)?,
                        "original_id": row.get::<_, String>(2)?,
                        "content": row.get::<_, String>(3)?,
                        "payload": serde_json::from_str::<serde_json::Value>(&payload)
                            .unwrap_or(serde_json::Value::Null),
                        "reason": row.get::<_, String>(5)?,
                        "quarantined_at": row.get::<_, String>(6)?,
                    }))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let graph_nodes = {
            let mut statement = conn
                .prepare(
                    "SELECT id,kind,label,name,layer,trust,created_at,last_seen,ref_table,ref_id
                     FROM graph_nodes WHERE owner=?1 AND workspace_id=?2 ORDER BY created_at,id",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], |row| {
                    Ok(serde_json::json!({
                        "id": row.get::<_, String>(0)?,
                        "kind": row.get::<_, String>(1)?,
                        "label": row.get::<_, Option<String>>(2)?,
                        "name": row.get::<_, String>(3)?,
                        "layer": row.get::<_, String>(4)?,
                        "trust": row.get::<_, i64>(5)?,
                        "created_at": row.get::<_, String>(6)?,
                        "last_seen": row.get::<_, String>(7)?,
                        "ref_table": row.get::<_, Option<String>>(8)?,
                        "ref_id": row.get::<_, Option<String>>(9)?,
                    }))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let graph_edges = {
            let mut statement = conn
                .prepare(
                    "SELECT id,src_id,tag,dst_id,fact_id,weight,traversal_count,trust,
                            created_at,last_seen,candidate_id,status
                     FROM graph_edges WHERE owner=?1 AND workspace_id=?2 ORDER BY created_at,id",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], |row| {
                    Ok(serde_json::json!({
                        "id": row.get::<_, String>(0)?,
                        "src_id": row.get::<_, String>(1)?,
                        "tag": row.get::<_, String>(2)?,
                        "dst_id": row.get::<_, String>(3)?,
                        "fact_id": row.get::<_, Option<String>>(4)?,
                        "weight": row.get::<_, f64>(5)?,
                        "traversal_count": row.get::<_, i64>(6)?,
                        "trust": row.get::<_, i64>(7)?,
                        "created_at": row.get::<_, String>(8)?,
                        "last_seen": row.get::<_, String>(9)?,
                        "candidate_id": row.get::<_, Option<String>>(10)?,
                        "status": row.get::<_, String>(11)?,
                    }))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let graph_cues = {
            let mut statement = conn
                .prepare(
                    "SELECT cue,node_id,source,created_at,candidate_id,status
                     FROM graph_cues WHERE owner=?1 AND workspace_id=?2 ORDER BY created_at,cue",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], |row| {
                    Ok(serde_json::json!({
                        "cue": row.get::<_, String>(0)?,
                        "node_id": row.get::<_, String>(1)?,
                        "source": row.get::<_, String>(2)?,
                        "created_at": row.get::<_, String>(3)?,
                        "candidate_id": row.get::<_, Option<String>>(4)?,
                        "status": row.get::<_, String>(5)?,
                    }))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let tombstones = {
            let mut statement = conn
                .prepare(
                    "SELECT id,affected_ids,created_at,recover_until,recovered_at
                     FROM memory_tombstones
                     WHERE owner=?1 AND workspace_id=?2 ORDER BY created_at,id",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id], |row| {
                    let affected: String = row.get(1)?;
                    Ok(serde_json::json!({
                        "id": row.get::<_, String>(0)?,
                        "affected": serde_json::from_str::<serde_json::Value>(&affected)
                            .unwrap_or(serde_json::Value::Null),
                        "created_at": row.get::<_, String>(2)?,
                        "recover_until": row.get::<_, Option<String>>(3)?,
                        "recovered_at": row.get::<_, Option<String>>(4)?,
                    }))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        Ok(serde_json::json!({
            "owner": owner,
            "workspace_id": workspace_id,
            "retention": Self::retention_policy_inner(&conn, owner, workspace_id)?,
            "raw": raw,
            "candidates": candidates,
            "curated": curated,
            "quarantine": quarantine,
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "graph_cues": graph_cues,
            "tombstones": tombstones,
            "digest": {"persisted": false},
        }))
    }

    pub fn explain_memory(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<serde_json::Value>, String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        let conn = self.conn.lock().unwrap();
        // Explain follows the same visibility as recall: a caller sees the
        // requested workspace plus the shared 'global' workspace, so a
        // global record surfaced by recall is also explainable from any of
        // the owner's workspace scopes.
        let record = conn
            .query_row(
                "SELECT id,content,kind,priority,trust_score,confidence_score,
                        importance_score,scene_name,source,source_type,owner,workspace_id,
                        session_key,session_id,tags,source_message_ids,timestamps,created_at,
                        updated_at,archived,last_accessed_at,exempt_from_decay,
                        exempt_from_dedup,metadata,workspace_path
                 FROM curated WHERE id=?1 AND owner=?2
                   AND (workspace_id=?3 OR workspace_id='global')",
                params![id, owner, workspace_id],
                Self::row_to_record,
            )
            .optional()
            .map_err(|error| error.to_string())?;
        let Some(record) = record else {
            return Ok(None);
        };
        let graph_nodes: Vec<String> = {
            let mut statement = conn
                .prepare(
                    "SELECT id FROM graph_nodes
                     WHERE owner=?1 AND workspace_id=?2
                       AND ref_table='curated' AND ref_id=?3",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, record.workspace_id.as_str(), id], |row| {
                    row.get(0)
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        Ok(Some(serde_json::json!({
            "id": record.id,
            "source": record.source,
            "source_type": record.source_type,
            "trust_score": record.trust_score,
            "confidence_score": record.confidence_score,
            "source_message_ids": record.source_message_ids,
            "source_uri": record.metadata.get("source_uri"),
            "source_revision": record.metadata.get("source_revision"),
            "content_hash": record.metadata.get("content_hash"),
            "raw_evidence_ids": record.metadata.get("raw_evidence_ids"),
            "candidate_id": record.metadata.get("candidate_id"),
            "graph_node_ids": graph_nodes,
        })))
    }

    pub fn metric_add(&self, name: &str, amount: usize) {
        let conn = self.conn.lock().unwrap();
        let _ = conn.execute(
            "INSERT INTO memory_metrics(name,value) VALUES (?1,?2)
             ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            params![name, amount as i64],
        );
    }

    pub fn sqlite_health(&self) -> serde_json::Value {
        let conn = self.conn.lock().unwrap();
        match conn.query_row("PRAGMA quick_check(1)", [], |row| row.get::<_, String>(0)) {
            Ok(result) if result.eq_ignore_ascii_case("ok") => serde_json::json!({
                "state": "ready",
                "quick_check": result,
                "repair": "none",
            }),
            Ok(result) => serde_json::json!({
                "state": "degraded",
                "class": "integrity_mismatch",
                "quick_check": result,
                "repair": "none",
            }),
            Err(error) => serde_json::json!({
                "state": "degraded",
                "class": Self::sqlite_error_class(&error),
                "error": error.to_string(),
                "repair": "none",
            }),
        }
    }

    pub fn quality_status(&self) -> Result<serde_json::Value, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let sqlite =
            match conn.query_row("PRAGMA quick_check(1)", [], |row| row.get::<_, String>(0)) {
                Ok(result) if result.eq_ignore_ascii_case("ok") => serde_json::json!({
                    "state": "ready",
                    "quick_check": result,
                    "repair": "none",
                }),
                Ok(result) => serde_json::json!({
                    "state": "degraded",
                    "class": "integrity_mismatch",
                    "quick_check": result,
                    "repair": "none",
                }),
                Err(error) => serde_json::json!({
                    "state": "degraded",
                    "class": Self::sqlite_error_class(&error),
                    "error": error.to_string(),
                    "repair": "none",
                }),
            };
        if sqlite.get("state").and_then(|value| value.as_str()) != Some("ready") {
            return Ok(serde_json::json!({
                "degraded": true,
                "sqlite": sqlite,
                "graph": {"integrity_ok": false},
                "recovery": {
                    "mode": "read_only",
                    "automatic_repair": false,
                    "last_verified_generation": serde_json::Value::Null,
                },
            }));
        }
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
            "degraded": false,
            "sqlite": sqlite,
            "recovery": {
                "mode": "normal",
                "automatic_repair": false,
            },
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
        const OPEN_QUESTIONS_MAX: usize = 5;

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

        let mut open_questions = Vec::new();
        {
            // source_type rides along so the host renderer can refuse to
            // render a non-human unknown into the endorsed block — by
            // construction none should exist (capture rejects non-manual
            // unknowns), so any hit there is a bug upstream.
            let mut stmt = conn
                .prepare(
                    "SELECT id, content, workspace_id, source_type FROM curated
                     WHERE archived = 0 AND kind = 'unknown' AND owner = ?1
                       AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))
                     ORDER BY COALESCE(last_accessed_at, updated_at) DESC
                     LIMIT ?4",
                )
                .map_err(|error| error.to_string())?;
            let rows = stmt
                .query_map(
                    params![
                        owner,
                        workspace_id,
                        include_global,
                        OPEN_QUESTIONS_MAX as i64
                    ],
                    |row| {
                        Ok(serde_json::json!({
                            "id": row.get::<_, String>(0)?,
                            "content": row.get::<_, String>(1)?,
                            "workspace_id": row.get::<_, String>(2)?,
                            "source_type": row.get::<_, String>(3)?,
                        }))
                    },
                )
                .map_err(|error| error.to_string())?;
            for row in rows {
                open_questions.push(row.map_err(|error| error.to_string())?);
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
            "open_questions": open_questions,
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

    pub fn reconcile_authored_records(
        &self,
        owner: &str,
        workspace_id: &str,
        source_path: &str,
        desired: &[MemoryRecord],
    ) -> Result<(usize, usize), String> {
        let (owner, workspace_id) = Self::require_scope(owner, workspace_id)?;
        if source_path.trim().is_empty() {
            return Err("source_path is required".into());
        }
        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        let existing: Vec<(String, String)> = {
            let mut statement = transaction
                .prepare(
                    "SELECT id,COALESCE(json_extract(metadata,'$.content_hash'),'') FROM curated
                     WHERE owner=?1 AND workspace_id=?2
                       AND json_extract(metadata,'$.authored_path')=?3",
                )
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map(params![owner, workspace_id, source_path], |row| {
                    Ok((row.get(0)?, row.get(1)?))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            rows
        };
        let existing_hashes: std::collections::HashMap<&str, &str> = existing
            .iter()
            .map(|(id, hash)| (id.as_str(), hash.as_str()))
            .collect();
        let desired_ids: HashSet<&str> = desired.iter().map(|record| record.id.as_str()).collect();
        let mut upserted = 0;
        for record in desired {
            if record.owner.as_deref() != Some(owner)
                || record.workspace_id != workspace_id
                || record
                    .metadata
                    .get("authored_path")
                    .and_then(serde_json::Value::as_str)
                    != Some(source_path)
            {
                return Err("authored reconcile record is outside its source scope".into());
            }
            let desired_hash = record
                .metadata
                .get("content_hash")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default();
            if existing_hashes.get(record.id.as_str()).copied() == Some(desired_hash) {
                continue;
            }
            Self::upsert_curated_inner(&transaction, record, None)?;
            upserted += 1;
        }
        let mut deleted = 0;
        for (id, _) in existing {
            if desired_ids.contains(id.as_str()) {
                continue;
            }
            Self::apply_maintenance_delete(&transaction, &id, owner, workspace_id)?;
            deleted += 1;
        }
        transaction.commit().map_err(|error| error.to_string())?;
        Ok((upserted, deleted))
    }

    /// Open (non-archived) kind=unknown records for an owner. With a
    /// workspace the scope is workspace ∪ global (the recall scope a
    /// passive resolve honors); with None it is every workspace (the
    /// promotion pass groups across them). Earliest first so the oldest
    /// copy of a question is the canonical one.
    pub fn list_open_unknowns(
        &self,
        owner: &str,
        workspace_id: Option<&str>,
    ) -> Result<Vec<MemoryRecord>, String> {
        if owner.trim().is_empty() {
            return Err("owner is required".into());
        }
        let conn = self.conn.lock().unwrap();
        let base = "SELECT id, content, kind, priority, trust_score, confidence_score,
                    importance_score, scene_name, source, source_type, owner,
                    workspace_id, session_key, session_id, tags, source_message_ids,
                    timestamps, created_at, updated_at, archived, last_accessed_at,
                    exempt_from_decay, exempt_from_dedup, metadata, workspace_path
             FROM curated
             WHERE archived = 0 AND kind = 'unknown' AND owner = ?1";
        let order = " ORDER BY created_at ASC";
        let mut records = Vec::new();
        if let Some(workspace) = workspace_id {
            let sql = format!("{base} AND (workspace_id = ?2 OR workspace_id = 'global'){order}");
            let mut stmt = conn.prepare(&sql).map_err(|error| error.to_string())?;
            let rows = stmt
                .query_map(params![owner, workspace], Self::row_to_record)
                .map_err(|error| error.to_string())?;
            for row in rows {
                records.push(row.map_err(|error| error.to_string())?);
            }
        } else {
            let sql = format!("{base}{order}");
            let mut stmt = conn.prepare(&sql).map_err(|error| error.to_string())?;
            let rows = stmt
                .query_map(params![owner], Self::row_to_record)
                .map_err(|error| error.to_string())?;
            for row in rows {
                records.push(row.map_err(|error| error.to_string())?);
            }
        }
        Ok(records)
    }

    /// Archive one curated record and merge a provenance patch into its
    /// metadata ({resolved_by, resolved_at} or {merged_into}). The only
    /// lifecycle for closing an open question — archived records leave
    /// recall/search/digest automatically via their archived = 0
    /// filters. expect_kind guards misuse (resolving a non-unknown);
    /// owner/workspace enforce scope when the call crosses the tool
    /// boundary and are None for engine-internal callers that already
    /// resolved scope.
    pub fn archive_curated_record(
        &self,
        id: &str,
        metadata_patch: &serde_json::Value,
        expect_kind: Option<&str>,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        let mut conn = self.conn.lock().unwrap();
        let Ok(transaction) = conn.transaction_with_behavior(TransactionBehavior::Immediate) else {
            return false;
        };
        let row = transaction.query_row(
            "SELECT kind, owner, workspace_id, metadata FROM curated
             WHERE id = ?1 AND archived = 0",
            params![id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        );
        let Ok((stored_kind, stored_owner, stored_workspace, metadata_raw)) = row else {
            return false;
        };
        if expect_kind.is_some_and(|wanted| stored_kind != wanted) {
            return false;
        }
        if let Some(wanted) = owner {
            if stored_owner.as_deref() != Some(wanted) {
                return false;
            }
        }
        if let Some(wanted) = workspace_id {
            if stored_workspace != wanted && stored_workspace != "global" {
                return false;
            }
        }
        let mut metadata: serde_json::Value =
            serde_json::from_str(&metadata_raw).unwrap_or_else(|_| serde_json::json!({}));
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        if let (Some(object), Some(patch)) = (metadata.as_object_mut(), metadata_patch.as_object())
        {
            for (key, value) in patch {
                object.insert(key.clone(), value.clone());
            }
        }
        if transaction
            .execute(
                "UPDATE curated SET archived = 1, metadata = ?1, updated_at = ?2 WHERE id = ?3",
                params![
                    serde_json::to_string(&metadata).unwrap_or_else(|_| "{}".into()),
                    chrono::Utc::now().to_rfc3339(),
                    id
                ],
            )
            .is_err()
            || super::graph::sync_curated_projection_inner(&transaction, id).is_err()
        {
            return false;
        }
        transaction.commit().is_ok()
    }

    /// Reopen one archived legacy unknown in its exact tenant scope.  This is
    /// the compatibility projection for a canonical open-question revision;
    /// it never changes non-question records or a global/workspace sibling.
    pub fn reopen_curated_unknown(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<bool, String> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Ok(false);
        }
        let mut conn = self.conn.lock().unwrap();
        let transaction = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        let metadata_raw = transaction
            .query_row(
                "SELECT metadata FROM curated
              WHERE id=?1 AND owner=?2 AND workspace_id=?3
                AND kind='unknown' AND archived=1",
                params![id, owner, workspace_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| error.to_string())?;
        let Some(metadata_raw) = metadata_raw else {
            return Ok(false);
        };
        let mut metadata: serde_json::Value =
            serde_json::from_str(&metadata_raw).unwrap_or_else(|_| serde_json::json!({}));
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        if let Some(object) = metadata.as_object_mut() {
            object.remove("resolved_by");
            object.remove("resolved_at");
        }
        if transaction
            .execute(
                "UPDATE curated SET archived=0,metadata=?1,updated_at=?2
                  WHERE id=?3 AND owner=?4 AND workspace_id=?5
                    AND kind='unknown' AND archived=1",
                params![
                    serde_json::to_string(&metadata).unwrap_or_else(|_| "{}".into()),
                    chrono::Utc::now().to_rfc3339(),
                    id,
                    owner,
                    workspace_id,
                ],
            )
            .map_err(|error| error.to_string())?
            != 1
        {
            return Ok(false);
        }
        super::graph::sync_curated_projection_inner(&transaction, id)
            .map_err(|error| error.to_string())?;
        transaction.commit().map_err(|error| error.to_string())?;
        Ok(true)
    }

    /// Discover every owner-keyed table in the shared memory authority.
    ///
    /// Python deliberately extends this database with principal bindings,
    /// tool-catalog, policy-audit, and FTS tables.  A hard-coded Rust list is
    /// therefore not authoritative and previously allowed account rename to
    /// report success while those rows remained under the old username.
    /// Media rows are the one explicit exception: their lifecycle adapter
    /// coordinates database rows with external bytes and must remain the sole
    /// owner of that closure.
    fn owner_domain_tables(conn: &Connection) -> Result<Vec<(String, String)>, String> {
        let mut statement = conn
            .prepare(
                "SELECT name FROM sqlite_master
                 WHERE type='table' AND name NOT LIKE 'sqlite_%'
                 ORDER BY name",
            )
            .map_err(|error| error.to_string())?;
        let names = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|error| error.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())?;
        let mut tables = Vec::new();
        for name in names {
            if name == "fm_v2_media_assets"
                || name == "fm_v2_media_representations"
                || name.starts_with("fm_v2_media_")
            {
                continue;
            }
            if !name
                .chars()
                .all(|character| character.is_ascii_alphanumeric() || character == '_')
            {
                return Err(format!("unsafe owner-domain table name: {name}"));
            }
            let mut columns = conn
                .prepare(&format!("PRAGMA table_info({name})"))
                .map_err(|error| error.to_string())?;
            let column_names = columns
                .query_map([], |row| row.get::<_, String>(1))
                .map_err(|error| error.to_string())?
                .collect::<Result<HashSet<_>, _>>()
                .map_err(|error| error.to_string())?;
            let owner_column = if column_names.contains("owner_id") {
                Some("owner_id")
            } else if column_names.contains("owner") {
                Some("owner")
            } else {
                None
            };
            if let Some(owner_column) = owner_column {
                tables.push((name, owner_column.to_string()));
            }
        }
        Ok(tables)
    }

    fn hash_sql_value(hasher: &mut blake3::Hasher, value: rusqlite::types::ValueRef<'_>) {
        use rusqlite::types::ValueRef;
        match value {
            ValueRef::Null => {
                hasher.update(&[0]);
            }
            ValueRef::Integer(value) => {
                hasher.update(&[1]);
                hasher.update(&value.to_le_bytes());
            }
            ValueRef::Real(value) => {
                hasher.update(&[2]);
                hasher.update(&value.to_bits().to_le_bytes());
            }
            ValueRef::Text(value) => {
                hasher.update(&[3]);
                hasher.update(&(value.len() as u64).to_le_bytes());
                hasher.update(value);
            }
            ValueRef::Blob(value) => {
                hasher.update(&[4]);
                hasher.update(&(value.len() as u64).to_le_bytes());
                hasher.update(value);
            }
        };
    }

    fn owner_inventory_inner(conn: &Connection, owner: &str) -> Result<serde_json::Value, String> {
        if owner.trim().is_empty() {
            return Err("owner is required".into());
        }
        let mut counts = BTreeMap::<String, i64>::new();
        let mut hasher = blake3::Hasher::new();
        for (table, owner_column) in Self::owner_domain_tables(conn)? {
            let count: i64 = conn
                .query_row(
                    &format!("SELECT count(*) FROM {table} WHERE {owner_column}=?1"),
                    params![owner],
                    |row| row.get(0),
                )
                .map_err(|error| error.to_string())?;
            counts.insert(table.clone(), count);
            hasher.update(table.as_bytes());
            hasher.update(&[0]);
            hasher.update(&count.to_le_bytes());
            if count == 0 {
                continue;
            }

            let mut columns = conn
                .prepare(&format!("PRAGMA table_info({table})"))
                .map_err(|error| error.to_string())?;
            let metadata = columns
                .query_map([], |row| {
                    Ok((row.get::<_, String>(1)?, row.get::<_, i64>(5)?))
                })
                .map_err(|error| error.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            let mut primary_key = metadata
                .iter()
                .filter(|(_, position)| *position > 0)
                .cloned()
                .collect::<Vec<_>>();
            primary_key.sort_by_key(|(_, position)| *position);
            let order_by = if primary_key.is_empty() {
                "rowid".to_string()
            } else {
                primary_key
                    .iter()
                    .map(|(name, _)| name.as_str())
                    .collect::<Vec<_>>()
                    .join(",")
            };
            let mut rows_statement = conn
                .prepare(&format!(
                    "SELECT * FROM {table} WHERE {owner_column}=?1 ORDER BY {order_by}"
                ))
                .map_err(|error| error.to_string())?;
            let mut rows = rows_statement
                .query(params![owner])
                .map_err(|error| error.to_string())?;
            while let Some(row) = rows.next().map_err(|error| error.to_string())? {
                hasher.update(&[0xff]);
                for index in 0..metadata.len() {
                    Self::hash_sql_value(
                        &mut hasher,
                        row.get_ref(index).map_err(|error| error.to_string())?,
                    );
                }
            }
        }

        // Legacy code identities predate the explicit owner column and encode
        // it as the first unit-separator-delimited codebase component.
        let code_count: i64 = conn
            .query_row(
                "SELECT count(*) FROM code_files
                 WHERE codebase LIKE (?1 || char(31) || '%')",
                params![owner],
                |row| row.get(0),
            )
            .map_err(|error| error.to_string())?;
        counts.insert("code_files".into(), code_count);
        hasher.update(b"code_files\0");
        hasher.update(&code_count.to_le_bytes());
        let mut code_statement = conn
            .prepare(
                "SELECT codebase,rel_path,blake3,mtime_ns,size,symbol_count,indexed_at
                 FROM code_files WHERE codebase LIKE (?1 || char(31) || '%')
                 ORDER BY codebase,rel_path",
            )
            .map_err(|error| error.to_string())?;
        let mut code_rows = code_statement
            .query(params![owner])
            .map_err(|error| error.to_string())?;
        while let Some(row) = code_rows.next().map_err(|error| error.to_string())? {
            hasher.update(&[0xfe]);
            for index in 0..7 {
                Self::hash_sql_value(
                    &mut hasher,
                    row.get_ref(index).map_err(|error| error.to_string())?,
                );
            }
        }

        let total = counts.values().copied().sum::<i64>();
        let legacy = |name: &str| counts.get(name).copied().unwrap_or(0);
        Ok(serde_json::json!({
            "count": total,
            "fingerprint": hasher.finalize().to_hex().to_string(),
            "tables": counts,
            // Compatibility counters retained for callers and diagnostics.
            "raw": legacy("raw"),
            "candidates": legacy("candidates"),
            "curated": legacy("curated"),
            "quarantine": legacy("memory_quarantine"),
            "facts": legacy("facts"),
            "graph_nodes": legacy("graph_nodes"),
            "graph_edges": legacy("graph_edges"),
            "graph_cues": legacy("graph_cues"),
            "retention_operations": legacy("memory_retention_operations"),
        }))
    }

    pub fn owner_counts(&self, owner: &str) -> Result<serde_json::Value, String> {
        let conn = self.conn.lock().unwrap();
        Self::owner_inventory_inner(&conn, owner)
    }

    fn normalize_reset_components(components: &[String]) -> Result<Vec<String>, String> {
        let mut requested = Vec::new();
        for component in components {
            if !matches!(component.as_str(), "memories" | "graph" | "ingest") {
                return Err(format!("unsupported reset component: {component}"));
            }
            if !requested.contains(component) {
                requested.push(component.clone());
            }
        }
        if requested.is_empty() {
            return Err("at least one reset component is required".into());
        }
        if requested.iter().any(|component| component == "memories") {
            return Ok(vec!["memories".into(), "graph".into(), "ingest".into()]);
        }
        Ok(["memories", "graph", "ingest"]
            .iter()
            .filter(|component| requested.iter().any(|value| value == **component))
            .map(|component| (*component).to_string())
            .collect())
    }

    fn append_reset_keys(
        conn: &Connection,
        owner: &str,
        prefix: &str,
        sql: &str,
        keys: &mut Vec<String>,
    ) -> Result<(), String> {
        let mut statement = conn.prepare(sql).map_err(|error| error.to_string())?;
        let rows = statement
            .query_map(params![owner], |row| row.get::<_, String>(0))
            .map_err(|error| error.to_string())?;
        for row in rows {
            keys.push(format!(
                "{prefix}:{}",
                row.map_err(|error| error.to_string())?
            ));
        }
        Ok(())
    }

    fn reset_component_keys(
        conn: &Connection,
        owner: &str,
        component: &str,
    ) -> Result<Vec<String>, String> {
        let mut keys = Vec::new();
        let queries: &[(&str, &str)] = match component {
            "memories" => &[
                (
                    "curated",
                    "SELECT id || ':' || updated_at FROM curated WHERE owner=?1",
                ),
                (
                    "v2_blocks",
                    "SELECT block_id || ':' || CAST(current_revision AS TEXT) FROM fm_v2_knowledge_blocks WHERE owner_id=?1",
                ),
                (
                    "v2_block_revisions",
                    "SELECT block_id || ':' || CAST(revision AS TEXT) || ':' || content_hash FROM fm_v2_knowledge_revisions WHERE owner_id=?1",
                ),
                (
                    "v2_revision_evidence",
                    "SELECT block_id || ':' || CAST(revision AS TEXT) || ':' || evidence_id FROM fm_v2_revision_evidence WHERE owner_id=?1",
                ),
                (
                    "v2_conflicts",
                    "SELECT conflict_id || ':' || CAST(current_revision AS TEXT) || ':' || state FROM fm_v2_conflicts WHERE owner_id=?1",
                ),
                (
                    "v2_conflict_events",
                    "SELECT conflict_id || ':' || CAST(sequence AS TEXT) || ':' || state FROM fm_v2_conflict_events WHERE owner_id=?1",
                ),
                (
                    "v2_entities",
                    "SELECT entity_id || ':' || CAST(current_revision AS TEXT) FROM fm_v2_entities WHERE owner_id=?1",
                ),
                (
                    "v2_entity_revisions",
                    "SELECT entity_id || ':' || CAST(revision AS TEXT) || ':' || content_hash FROM fm_v2_entity_revisions WHERE owner_id=?1",
                ),
                (
                    "v2_entity_aliases",
                    "SELECT entity_id || ':' || CAST(revision AS TEXT) || ':' || alias FROM fm_v2_entity_aliases WHERE owner_id=?1",
                ),
                (
                    "v2_entity_roles",
                    "SELECT entity_id || ':' || CAST(revision AS TEXT) || ':' || role FROM fm_v2_entity_roles WHERE owner_id=?1",
                ),
                (
                    "v2_entity_tags",
                    "SELECT entity_id || ':' || CAST(revision AS TEXT) || ':' || tag FROM fm_v2_entity_tags WHERE owner_id=?1",
                ),
                (
                    "v2_knowledge_trust",
                    "SELECT assignment_id || ':' || content_hash FROM fm_v2_trust_assignments WHERE owner_id=?1 AND subject_kind='knowledge_revision'",
                ),
                (
                    "v2_source_trust",
                    "SELECT assignment.assignment_id || ':' || assignment.content_hash FROM fm_v2_trust_assignments assignment WHERE assignment.owner_id=?1 AND assignment.subject_kind='source_revision' AND EXISTS (SELECT 1 FROM fm_v2_sources source WHERE source.owner_id=assignment.owner_id AND source.source_id=assignment.subject_id AND source.source_revision=assignment.subject_revision AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=source.owner_id AND document.source_id=source.source_id AND document.source_revision=source.source_revision))",
                ),
                (
                    "v2_episodes",
                    "SELECT episode_id || ':' || content_hash FROM fm_v2_episodes WHERE owner_id=?1",
                ),
                (
                    "v2_evidence",
                    "SELECT evidence_id || ':' || created_at FROM fm_v2_evidence WHERE owner_id=?1",
                ),
                (
                    "v2_sources",
                    "SELECT source.source_id || ':' || CAST(source.source_revision AS TEXT) || ':' || source.content_hash FROM fm_v2_sources source WHERE source.owner_id=?1 AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=source.owner_id AND document.source_id=source.source_id AND document.source_revision=source.source_revision)",
                ),
                (
                    "v2_memory_outbox",
                    "SELECT event_id || ':' || state FROM fm_v2_outbox WHERE owner_id=?1 AND kind IN ('entity_revision','knowledge_revision','knowledge_conflict','conflict_recorded','conflict_resolved','conflict_dismissed','legacy_migrated','legacy_replayed')",
                ),
                (
                    "v2_memory_trust_outbox",
                    "SELECT outbox.event_id || ':' || outbox.state FROM fm_v2_outbox outbox WHERE outbox.owner_id=?1 AND outbox.kind='trust_assignment' AND json_valid(outbox.payload_json) AND (json_extract(outbox.payload_json,'$.subject_kind')='knowledge_revision' OR (json_extract(outbox.payload_json,'$.subject_kind')='source_revision' AND EXISTS (SELECT 1 FROM fm_v2_sources source WHERE source.owner_id=outbox.owner_id AND source.source_id=json_extract(outbox.payload_json,'$.subject_id') AND source.source_revision=json_extract(outbox.payload_json,'$.subject_revision') AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=source.owner_id AND document.source_id=source.source_id AND document.source_revision=source.source_revision))))",
                ),
                (
                    "v2_evidence_outbox",
                    "SELECT outbox.event_id || ':' || outbox.state FROM fm_v2_outbox outbox WHERE outbox.owner_id=?1 AND outbox.kind='evidence_created' AND json_valid(outbox.payload_json) AND EXISTS (SELECT 1 FROM fm_v2_evidence evidence WHERE evidence.owner_id=outbox.owner_id AND evidence.evidence_id=json_extract(outbox.payload_json,'$.evidence_id'))",
                ),
                (
                    "v2_source_outbox",
                    "SELECT outbox.event_id || ':' || outbox.state FROM fm_v2_outbox outbox WHERE outbox.owner_id=?1 AND outbox.kind='source_revision' AND json_valid(outbox.payload_json) AND EXISTS (SELECT 1 FROM fm_v2_sources source WHERE source.owner_id=outbox.owner_id AND source.source_id=json_extract(outbox.payload_json,'$.source_id') AND source.source_revision=json_extract(outbox.payload_json,'$.source_revision') AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=source.owner_id AND document.source_id=source.source_id AND document.source_revision=source.source_revision))",
                ),
                (
                    "legacy_curated_outbox",
                    "SELECT CAST(sequence AS TEXT) || ':' || state FROM fm_v2_legacy_outbox WHERE owner_id=?1 AND source_table='curated'",
                ),
                (
                    "migration_curated_outbox_events",
                    "SELECT event.migration_run_id || ':' || CAST(event.outbox_sequence AS TEXT) FROM fm_v2_migration_outbox_events event JOIN fm_v2_legacy_outbox outbox ON outbox.sequence=event.outbox_sequence WHERE outbox.owner_id=?1 AND outbox.source_table='curated'",
                ),
                (
                    "migration_curated_plan",
                    "SELECT CAST(plan_id AS TEXT) || ':' || source_fingerprint FROM fm_v2_migration_plan WHERE owner_id=?1 AND source_table='curated'",
                ),
                (
                    "migration_curated_events",
                    "SELECT event.event_id || ':' || event.event_hash FROM fm_v2_migration_events event JOIN fm_v2_migration_plan plan ON plan.plan_id=event.plan_id WHERE plan.owner_id=?1 AND plan.source_table='curated'",
                ),
                (
                    "migration_curated_review",
                    "SELECT review.review_id || ':' || review.updated_at FROM fm_v2_migration_review review JOIN fm_v2_migration_plan plan ON plan.plan_id=review.plan_id WHERE plan.owner_id=?1 AND plan.source_table='curated'",
                ),
            ],
            "graph" => &[
                (
                    "facts",
                    "SELECT id || ':' || updated_at FROM facts WHERE owner=?1",
                ),
                (
                    "graph_nodes",
                    "SELECT id || ':' || last_seen FROM graph_nodes WHERE owner=?1",
                ),
                (
                    "graph_edges",
                    "SELECT id || ':' || last_seen FROM graph_edges WHERE owner=?1",
                ),
                (
                    "graph_cues",
                    "SELECT cue || ':' || node_id || ':' || status FROM graph_cues WHERE owner=?1",
                ),
                (
                    "code_files",
                    "SELECT codebase || ':' || rel_path || ':' || blake3 FROM code_files WHERE codebase LIKE (?1 || char(31) || '%')",
                ),
                (
                    "code_index_runs",
                    "SELECT workspace_id || ':' || codebase || ':' || run_id || ':' || status FROM code_index_runs WHERE owner=?1",
                ),
                (
                    "code_index_heads",
                    "SELECT workspace_id || ':' || codebase || ':' || run_id FROM code_index_heads WHERE owner=?1",
                ),
                (
                    "legacy_graph_outbox",
                    "SELECT CAST(sequence AS TEXT) || ':' || state FROM fm_v2_legacy_outbox WHERE owner_id=?1 AND source_table IN ('facts','graph_nodes','graph_edges','graph_cues')",
                ),
                (
                    "migration_graph_outbox_events",
                    "SELECT event.migration_run_id || ':' || CAST(event.outbox_sequence AS TEXT) FROM fm_v2_migration_outbox_events event JOIN fm_v2_legacy_outbox outbox ON outbox.sequence=event.outbox_sequence WHERE outbox.owner_id=?1 AND outbox.source_table IN ('facts','graph_nodes','graph_edges','graph_cues')",
                ),
                (
                    "migration_graph_plan",
                    "SELECT CAST(plan_id AS TEXT) || ':' || source_fingerprint FROM fm_v2_migration_plan WHERE owner_id=?1 AND source_table IN ('facts','graph_nodes','graph_edges','graph_cues')",
                ),
                (
                    "migration_graph_events",
                    "SELECT event.event_id || ':' || event.event_hash FROM fm_v2_migration_events event JOIN fm_v2_migration_plan plan ON plan.plan_id=event.plan_id WHERE plan.owner_id=?1 AND plan.source_table IN ('facts','graph_nodes','graph_edges','graph_cues')",
                ),
                (
                    "migration_graph_review",
                    "SELECT review.review_id || ':' || review.updated_at FROM fm_v2_migration_review review JOIN fm_v2_migration_plan plan ON plan.plan_id=review.plan_id WHERE plan.owner_id=?1 AND plan.source_table IN ('facts','graph_nodes','graph_edges','graph_cues')",
                ),
            ],
            "ingest" => &[
                (
                    "raw",
                    "SELECT id || ':' || recorded_at FROM raw WHERE owner=?1",
                ),
                (
                    "candidates",
                    "SELECT id || ':' || updated_at FROM candidates WHERE owner=?1",
                ),
                (
                    "quarantine",
                    "SELECT id || ':' || quarantined_at FROM memory_quarantine WHERE owner=?1",
                ),
                (
                    "leases",
                    "SELECT kind || ':' || workspace_id || ':' || subject || ':' || holder || ':' || expires_at FROM memory_leases WHERE owner=?1",
                ),
                (
                    "retention_operations",
                    "SELECT workspace_id || ':' || operation_id || ':' || committed_at FROM memory_retention_operations WHERE owner=?1",
                ),
                (
                    "tombstones",
                    "SELECT id || ':' || created_at || ':' || COALESCE(recovered_at,'') FROM memory_tombstones WHERE owner=?1",
                ),
                (
                    "v2_candidates",
                    "SELECT candidate_id || ':' || CAST(current_revision AS TEXT) FROM fm_v2_candidates WHERE owner_id=?1",
                ),
                (
                    "v2_candidate_revisions",
                    "SELECT candidate_id || ':' || CAST(revision AS TEXT) || ':' || content_hash FROM fm_v2_candidate_revisions WHERE owner_id=?1",
                ),
                (
                    "v2_candidate_evidence",
                    "SELECT candidate_id || ':' || CAST(revision AS TEXT) || ':' || evidence_id FROM fm_v2_candidate_evidence WHERE owner_id=?1",
                ),
                (
                    "v2_candidate_trust",
                    "SELECT assignment_id || ':' || content_hash FROM fm_v2_trust_assignments WHERE owner_id=?1 AND subject_kind='candidate_revision'",
                ),
                (
                    "v2_ingest_jobs",
                    "SELECT job_id || ':' || state || ':' || updated_at FROM fm_v2_jobs WHERE owner_id=?1 AND kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch')",
                ),
                (
                    "v2_ingest_attempts",
                    "SELECT attempt.attempt_id || ':' || attempt.state || ':' || attempt.updated_at FROM fm_v2_attempts attempt JOIN fm_v2_jobs job ON job.owner_id=attempt.owner_id AND job.job_id=attempt.job_id WHERE job.owner_id=?1 AND job.kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch')",
                ),
                (
                    "v2_ingest_manifests",
                    "SELECT manifest.manifest_id || ':' || manifest.content_hash FROM fm_v2_job_manifests manifest JOIN fm_v2_jobs job ON job.owner_id=manifest.owner_id AND job.job_id=manifest.job_id WHERE job.owner_id=?1 AND job.kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch')",
                ),
                (
                    "v2_candidate_outbox",
                    "SELECT event_id || ':' || state FROM fm_v2_outbox WHERE owner_id=?1 AND kind='candidate_revision'",
                ),
                (
                    "v2_ingest_job_outbox",
                    "SELECT outbox.event_id || ':' || outbox.state FROM fm_v2_outbox outbox WHERE outbox.owner_id=?1 AND outbox.kind IN ('job_state','job_attempt') AND json_valid(outbox.payload_json) AND EXISTS (SELECT 1 FROM fm_v2_jobs job WHERE job.owner_id=outbox.owner_id AND job.job_id=json_extract(outbox.payload_json,'$.job_id') AND job.kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch'))",
                ),
                (
                    "v2_candidate_trust_outbox",
                    "SELECT event_id || ':' || state FROM fm_v2_outbox WHERE owner_id=?1 AND kind='trust_assignment' AND json_valid(payload_json) AND json_extract(payload_json,'$.subject_kind')='candidate_revision'",
                ),
                (
                    "legacy_ingest_outbox",
                    "SELECT CAST(sequence AS TEXT) || ':' || state FROM fm_v2_legacy_outbox WHERE owner_id=?1 AND source_table IN ('raw','candidates','memory_quarantine','memory_tombstones','memory_retention_operations','memory_leases')",
                ),
                (
                    "migration_ingest_outbox_events",
                    "SELECT event.migration_run_id || ':' || CAST(event.outbox_sequence AS TEXT) FROM fm_v2_migration_outbox_events event JOIN fm_v2_legacy_outbox outbox ON outbox.sequence=event.outbox_sequence WHERE outbox.owner_id=?1 AND outbox.source_table IN ('raw','candidates','memory_quarantine','memory_tombstones','memory_retention_operations','memory_leases')",
                ),
                (
                    "migration_ingest_plan",
                    "SELECT CAST(plan_id AS TEXT) || ':' || source_fingerprint FROM fm_v2_migration_plan WHERE owner_id=?1 AND source_table IN ('raw','candidates','memory_quarantine','memory_tombstones','memory_retention_operations','memory_leases')",
                ),
                (
                    "migration_ingest_events",
                    "SELECT event.event_id || ':' || event.event_hash FROM fm_v2_migration_events event JOIN fm_v2_migration_plan plan ON plan.plan_id=event.plan_id WHERE plan.owner_id=?1 AND plan.source_table IN ('raw','candidates','memory_quarantine','memory_tombstones','memory_retention_operations','memory_leases')",
                ),
                (
                    "migration_ingest_review",
                    "SELECT review.review_id || ':' || review.updated_at FROM fm_v2_migration_review review JOIN fm_v2_migration_plan plan ON plan.plan_id=review.plan_id WHERE plan.owner_id=?1 AND plan.source_table IN ('raw','candidates','memory_quarantine','memory_tombstones','memory_retention_operations','memory_leases')",
                ),
            ],
            _ => return Err(format!("unsupported reset component: {component}")),
        };
        for (prefix, sql) in queries {
            Self::append_reset_keys(conn, owner, prefix, sql, &mut keys)?;
        }
        keys.sort();
        Ok(keys)
    }

    fn reset_snapshot_inner(
        conn: &Connection,
        owner: &str,
    ) -> Result<serde_json::Map<String, serde_json::Value>, String> {
        let mut components = serde_json::Map::new();
        for component in ["memories", "graph", "ingest"] {
            let keys = Self::reset_component_keys(conn, owner, component)?;
            let serialized = serde_json::to_string(&keys).map_err(|error| error.to_string())?;
            components.insert(
                component.into(),
                serde_json::json!({
                    "count": keys.len(),
                    "fingerprint": crate::privacy::content_hash(&serialized),
                }),
            );
        }
        Ok(components)
    }

    fn reset_preflight_inner(
        conn: &Connection,
        owner: &str,
        expanded: &[String],
    ) -> Result<(), String> {
        let foreign_keys: i64 = conn
            .query_row("PRAGMA foreign_keys", [], |row| row.get(0))
            .map_err(|error| error.to_string())?;
        if foreign_keys != 1 {
            return Err("owner reset requires SQLite foreign-key enforcement".into());
        }
        let active_erasure: i64 = conn
            .query_row(
                "SELECT count(*) FROM fm_v2_privacy_erasure_context WHERE owner_id=?1",
                params![owner],
                |row| row.get(0),
            )
            .map_err(|error| error.to_string())?;
        if active_erasure != 0 {
            return Err("another privacy erasure is active for this owner".into());
        }
        if expanded.iter().any(|component| component == "ingest") {
            let immutable_events: i64 = conn
                .query_row(
                    "SELECT count(*) FROM fm_v2_job_events event JOIN fm_v2_jobs job ON job.owner_id=event.owner_id AND job.job_id=event.job_id WHERE job.owner_id=?1 AND job.kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch')",
                    params![owner],
                    |row| row.get(0),
                )
                .map_err(|error| error.to_string())?;
            if immutable_events != 0 {
                return Err(format!(
                    "owner reset cannot safely erase {immutable_events} immutable memory-ingest job event(s)"
                ));
            }
        }
        Ok(())
    }

    fn delete_legacy_migration_rows(
        tx: &rusqlite::Transaction<'_>,
        owner: &str,
        component: &str,
    ) -> Result<(), String> {
        let predicate = match component {
            "memories" => "source_table='curated'",
            "graph" => "source_table IN ('facts','graph_nodes','graph_edges','graph_cues')",
            "ingest" => "source_table IN ('raw','candidates','memory_quarantine','memory_tombstones','memory_retention_operations','memory_leases')",
            _ => return Err(format!("unsupported reset component: {component}")),
        };
        tx.execute(
            &format!("DELETE FROM fm_v2_migration_events WHERE plan_id IN (SELECT plan_id FROM fm_v2_migration_plan WHERE owner_id=?1 AND {predicate})"),
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            &format!("DELETE FROM fm_v2_migration_review WHERE plan_id IN (SELECT plan_id FROM fm_v2_migration_plan WHERE owner_id=?1 AND {predicate})"),
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            &format!("DELETE FROM fm_v2_migration_plan WHERE owner_id=?1 AND {predicate}"),
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            &format!("DELETE FROM fm_v2_migration_outbox_events WHERE outbox_sequence IN (SELECT sequence FROM fm_v2_legacy_outbox WHERE owner_id=?1 AND {predicate})"),
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            &format!("DELETE FROM fm_v2_legacy_outbox WHERE owner_id=?1 AND {predicate}"),
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        Ok(())
    }

    fn delete_orphaned_change_sets(
        tx: &rusqlite::Transaction<'_>,
        owner: &str,
    ) -> Result<(), String> {
        // Change sets are the audit spine of the v2 store. Once every row
        // that referenced one is erased, the set itself is owner residue and
        // must go too; sets still pinned by preserved rows (RAG documents,
        // retained jobs) stay. Change sets carry no append-only trigger, so
        // this runs inside the reset/purge erasure context without needing
        // its exception.
        tx.execute(
            "DELETE FROM fm_v2_change_sets WHERE owner_id=?1
               AND NOT EXISTS (SELECT 1 FROM fm_v2_entities row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.created_change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_entity_revisions row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_knowledge_blocks row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.created_change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_knowledge_revisions row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_candidates row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.created_change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_candidate_revisions row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_conflicts row WHERE row.owner_id=fm_v2_change_sets.owner_id AND (row.created_change_set_id=fm_v2_change_sets.change_set_id OR row.resolved_change_set_id=fm_v2_change_sets.change_set_id))
               AND NOT EXISTS (SELECT 1 FROM fm_v2_conflict_events row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_job_events row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_episodes row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.created_change_set_id=fm_v2_change_sets.change_set_id)
               AND NOT EXISTS (SELECT 1 FROM fm_v2_outbox row WHERE row.owner_id=fm_v2_change_sets.owner_id AND row.change_set_id=fm_v2_change_sets.change_set_id)",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        Ok(())
    }

    fn delete_reset_component_inner(
        tx: &rusqlite::Transaction<'_>,
        owner: &str,
        component: &str,
    ) -> Result<(), String> {
        match component {
            "graph" => {
                tx.execute(
                    "DELETE FROM graph_cues_fts WHERE node_id IN (SELECT id FROM graph_nodes WHERE owner=?1)",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM facts_fts WHERE rowid IN (SELECT rowid FROM facts WHERE owner=?1)",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                for table in ["graph_cues", "graph_edges", "graph_nodes", "facts"] {
                    tx.execute(
                        &format!("DELETE FROM {table} WHERE owner=?1"),
                        params![owner],
                    )
                    .map_err(|error| error.to_string())?;
                }
                tx.execute(
                    "DELETE FROM code_index_heads WHERE owner=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute("DELETE FROM code_index_runs WHERE owner=?1", params![owner])
                    .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM code_files WHERE codebase LIKE (?1 || char(31) || '%')",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                Self::delete_legacy_migration_rows(tx, owner, component)?;
            }
            "ingest" => {
                tx.execute(
                    "DELETE FROM raw_fts WHERE rowid IN (SELECT rowid FROM raw WHERE owner=?1)",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND (kind='candidate_revision' OR (kind='trust_assignment' AND json_valid(payload_json) AND json_extract(payload_json,'$.subject_kind')='candidate_revision') OR (kind IN ('job_state','job_attempt') AND json_valid(payload_json) AND EXISTS (SELECT 1 FROM fm_v2_jobs job WHERE job.owner_id=fm_v2_outbox.owner_id AND job.job_id=json_extract(fm_v2_outbox.payload_json,'$.job_id') AND job.kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch'))))",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_trust_assignments WHERE owner_id=?1 AND subject_kind='candidate_revision'",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_candidate_evidence WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_candidate_revisions WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_candidates WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id IN (SELECT job_id FROM fm_v2_jobs WHERE owner_id=?1 AND kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch'))",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_attempts WHERE owner_id=?1 AND job_id IN (SELECT job_id FROM fm_v2_jobs WHERE owner_id=?1 AND kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch'))",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_jobs WHERE owner_id=?1 AND kind IN ('conversation_capture','memory_file_import','memory_import_item','memory_import_batch')",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                for table in [
                    "memory_leases",
                    "memory_retention_operations",
                    "memory_tombstones",
                    "memory_quarantine",
                    "candidates",
                    "raw",
                ] {
                    tx.execute(
                        &format!("DELETE FROM {table} WHERE owner=?1"),
                        params![owner],
                    )
                    .map_err(|error| error.to_string())?;
                }
                Self::delete_legacy_migration_rows(tx, owner, component)?;
            }
            "memories" => {
                tx.execute(
                    "DELETE FROM curated_fts WHERE rowid IN (SELECT rowid FROM curated WHERE owner=?1)",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_outbox AS outbox WHERE outbox.owner_id=?1 AND (outbox.kind IN ('entity_revision','knowledge_revision','knowledge_conflict','conflict_recorded','conflict_resolved','conflict_dismissed','legacy_migrated','legacy_replayed') OR (outbox.kind='trust_assignment' AND json_valid(outbox.payload_json) AND (json_extract(outbox.payload_json,'$.subject_kind')='knowledge_revision' OR (json_extract(outbox.payload_json,'$.subject_kind')='source_revision' AND EXISTS (SELECT 1 FROM fm_v2_sources source WHERE source.owner_id=outbox.owner_id AND source.source_id=json_extract(outbox.payload_json,'$.subject_id') AND source.source_revision=json_extract(outbox.payload_json,'$.subject_revision') AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=source.owner_id AND document.source_id=source.source_id AND document.source_revision=source.source_revision))))) OR (outbox.kind='evidence_created' AND json_valid(outbox.payload_json) AND EXISTS (SELECT 1 FROM fm_v2_evidence evidence WHERE evidence.owner_id=outbox.owner_id AND evidence.evidence_id=json_extract(outbox.payload_json,'$.evidence_id'))) OR (outbox.kind='source_revision' AND json_valid(outbox.payload_json) AND EXISTS (SELECT 1 FROM fm_v2_sources source WHERE source.owner_id=outbox.owner_id AND source.source_id=json_extract(outbox.payload_json,'$.source_id') AND source.source_revision=json_extract(outbox.payload_json,'$.source_revision') AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=source.owner_id AND document.source_id=source.source_id AND document.source_revision=source.source_revision))))",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_trust_assignments WHERE owner_id=?1 AND (subject_kind='knowledge_revision' OR (subject_kind='source_revision' AND EXISTS (SELECT 1 FROM fm_v2_sources source WHERE source.owner_id=fm_v2_trust_assignments.owner_id AND source.source_id=fm_v2_trust_assignments.subject_id AND source.source_revision=fm_v2_trust_assignments.subject_revision AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=source.owner_id AND document.source_id=source.source_id AND document.source_revision=source.source_revision))))",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_conflict_events WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_conflicts WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_revision_evidence WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_knowledge_revisions WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_knowledge_blocks WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                for table in [
                    "fm_v2_entity_aliases",
                    "fm_v2_entity_roles",
                    "fm_v2_entity_tags",
                ] {
                    tx.execute(
                        &format!("DELETE FROM {table} WHERE owner_id=?1"),
                        params![owner],
                    )
                    .map_err(|error| error.to_string())?;
                }
                tx.execute(
                    "DELETE FROM fm_v2_entity_revisions WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_entities WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_episodes WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_evidence WHERE owner_id=?1",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute(
                    "DELETE FROM fm_v2_sources WHERE owner_id=?1 AND NOT EXISTS (SELECT 1 FROM fm_v2_documents document WHERE document.owner_id=fm_v2_sources.owner_id AND document.source_id=fm_v2_sources.source_id AND document.source_revision=fm_v2_sources.source_revision)",
                    params![owner],
                )
                .map_err(|error| error.to_string())?;
                tx.execute("DELETE FROM curated WHERE owner=?1", params![owner])
                    .map_err(|error| error.to_string())?;
                Self::delete_legacy_migration_rows(tx, owner, component)?;
            }
            _ => return Err(format!("unsupported reset component: {component}")),
        }
        Ok(())
    }

    pub fn reset_owner(
        &self,
        action: &str,
        owner: &str,
        components: &[String],
        expected_counts: Option<&serde_json::Value>,
    ) -> Result<serde_json::Value, String> {
        if owner.trim().is_empty() {
            return Err("owner is required".into());
        }
        if !matches!(action, "reset_preview" | "reset_commit") {
            return Err("action must be reset_preview|reset_commit".into());
        }
        let expanded = Self::normalize_reset_components(components)?;
        let implications = if components.iter().any(|component| component == "memories") {
            vec![
                "Memories expands to the graph and ingest dependency closure.",
                "RAG documents, chunks, generations, and their source revisions are preserved.",
                "Retention policy and project/policy/hex state are preserved.",
            ]
        } else {
            let mut values = vec![
                "Graph includes owner code-index state when selected.",
                "Ingest preserves the owner's retention policy when selected.",
            ];
            if components.iter().any(|component| component == "graph") {
                values
                    .push("Retained memories may rebuild their derived graph nodes after restart.");
            }
            values
        };

        if action == "reset_preview" {
            let conn = self.conn.lock().unwrap();
            Self::reset_preflight_inner(&conn, owner, &expanded)?;
            return Ok(serde_json::json!({
                "components": Self::reset_snapshot_inner(&conn, owner)?,
                "expanded_components": expanded,
                "implications": implications,
            }));
        }

        let expected = expected_counts
            .and_then(serde_json::Value::as_object)
            .ok_or_else(|| "reset_commit requires preview expected_counts".to_string())?;
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        Self::reset_preflight_inner(&tx, owner, &expanded)?;
        let snapshot = Self::reset_snapshot_inner(&tx, owner)?;
        let current_empty = expanded.iter().all(|component| {
            snapshot
                .get(component)
                .and_then(|entry| entry.get("count"))
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0)
                == 0
        });
        if !current_empty {
            for component in &expanded {
                if expected.get(component) != snapshot.get(component) {
                    return Err(format!(
                        "owner reset preview is stale for {component}; preview again before committing"
                    ));
                }
            }
        }

        let needs_erasure_context = expanded
            .iter()
            .any(|component| component == "memories" || component == "ingest")
            && !current_empty;
        let erasure_id = format!("reset_{}", uuid::Uuid::new_v4().simple());
        if needs_erasure_context {
            tx.execute(
                "INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES (?1,?2,NULL,?1,'owner selective reset',?3)",
                params![owner, erasure_id, chrono::Utc::now().to_rfc3339()],
            )
            .map_err(|error| error.to_string())?;
        }
        // Ingest candidates may reference accepted knowledge blocks. Erase
        // that dependency edge before memory history, irrespective of the
        // stable component order returned to callers.
        for component in ["ingest", "graph", "memories"] {
            if expanded.iter().any(|selected| selected == component) {
                Self::delete_reset_component_inner(&tx, owner, component)?;
            }
        }
        if expanded
            .iter()
            .any(|component| component == "memories" || component == "ingest")
        {
            // Audit change sets orphaned by the erased revisions are owner
            // residue too; sets still referenced by preserved rows stay.
            Self::delete_orphaned_change_sets(&tx, owner)?;
        }
        if needs_erasure_context {
            tx.execute(
                "DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2",
                params![owner, erasure_id],
            )
            .map_err(|error| error.to_string())?;
        }
        let after = Self::reset_snapshot_inner(&tx, owner)?;
        for component in &expanded {
            let remaining = after
                .get(component)
                .and_then(|entry| entry.get("count"))
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(u64::MAX);
            if remaining != 0 {
                return Err(format!(
                    "owner reset left {remaining} {component} row(s); transaction rolled back"
                ));
            }
        }
        let categories = expanded
            .iter()
            .filter_map(|component| {
                snapshot
                    .get(component)
                    .and_then(serde_json::Value::as_object)
                    .cloned()
                    .map(|mut entry| {
                        entry.insert("state".into(), serde_json::json!("complete"));
                        (component.clone(), serde_json::Value::Object(entry))
                    })
            })
            .collect::<serde_json::Map<_, _>>();
        tx.commit().map_err(|error| error.to_string())?;
        Ok(serde_json::json!({"complete": true, "categories": categories}))
    }

    fn suspend_owner_triggers(
        conn: &Connection,
        owner_tables: &HashSet<String>,
        operation: &str,
    ) -> Result<Vec<String>, String> {
        let needle = format!("BEFORE {}", operation.to_ascii_uppercase());
        let mut statement = conn
            .prepare(
                "SELECT name,tbl_name,sql FROM sqlite_master
                 WHERE type='trigger' AND sql IS NOT NULL ORDER BY name",
            )
            .map_err(|error| error.to_string())?;
        let triggers = statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(|error| error.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())?;
        drop(statement);
        let mut definitions = Vec::new();
        for (name, table, sql) in triggers {
            if !owner_tables.contains(&table) || !sql.to_ascii_uppercase().contains(&needle) {
                continue;
            }
            if !name
                .chars()
                .all(|character| character.is_ascii_alphanumeric() || character == '_')
            {
                return Err(format!("unsafe owner-domain trigger name: {name}"));
            }
            conn.execute_batch(&format!("DROP TRIGGER IF EXISTS {name};"))
                .map_err(|error| error.to_string())?;
            definitions.push(sql);
        }
        Ok(definitions)
    }

    fn restore_owner_triggers(conn: &Connection, definitions: &[String]) -> Result<(), String> {
        for definition in definitions {
            conn.execute_batch(definition)
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    fn purge_owner_authoritative(&self, owner: &str) -> Result<serde_json::Value, String> {
        let mut conn = self.conn.lock().unwrap();
        let before = Self::owner_inventory_inner(&conn, owner)?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        tx.execute_batch("PRAGMA defer_foreign_keys=ON;")
            .map_err(|error| error.to_string())?;
        let owner_tables = Self::owner_domain_tables(&tx)?;
        let owner_table_names = owner_tables
            .iter()
            .map(|(table, _)| table.clone())
            .collect::<HashSet<_>>();
        let suspended = Self::suspend_owner_triggers(&tx, &owner_table_names, "DELETE")?;

        // The legacy FTS tables do not carry an owner column.  Remove their
        // exact rowid/node closure while the owner-bearing base rows still
        // exist.  The canonical RAG FTS table does carry owner_id and is
        // handled by the authoritative table loop below.
        for statement in [
            "DELETE FROM curated_fts WHERE rowid IN (SELECT rowid FROM curated WHERE owner=?1)",
            "DELETE FROM raw_fts WHERE rowid IN (SELECT rowid FROM raw WHERE owner=?1)",
            "DELETE FROM facts_fts WHERE rowid IN (SELECT rowid FROM facts WHERE owner=?1)",
            "DELETE FROM graph_cues_fts WHERE node_id IN (SELECT id FROM graph_nodes WHERE owner=?1)",
        ] {
            tx.execute(statement, params![owner])
                .map_err(|error| error.to_string())?;
        }

        // These two migration audit tables have no owner column; their owner
        // is reached through the plan/outbox parent.  Remove only the exact
        // owner's children before the generic owner-keyed closure.
        tx.execute(
            "DELETE FROM fm_v2_migration_events
             WHERE plan_id IN (
                 SELECT plan_id FROM fm_v2_migration_plan WHERE owner_id=?1
             )",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM fm_v2_migration_outbox_events
             WHERE outbox_sequence IN (
                 SELECT sequence FROM fm_v2_legacy_outbox WHERE owner_id=?1
             )",
            params![owner],
        )
        .map_err(|error| error.to_string())?;

        // Deferred foreign keys make this a single all-or-nothing transition
        // even though owner_id participates in nearly every v2 composite key.
        // The update/delete guards above are restored in this same SQLite
        // transaction; a crash or error rolls both data and DDL back together.
        for (table, owner_column) in owner_tables.iter().rev() {
            tx.execute(
                &format!("DELETE FROM {table} WHERE {owner_column}=?1"),
                params![owner],
            )
            .map_err(|error| error.to_string())?;
        }
        tx.execute(
            "DELETE FROM code_files
             WHERE codebase LIKE (?1 || char(31) || '%')",
            params![owner],
        )
        .map_err(|error| error.to_string())?;

        let after = Self::owner_inventory_inner(&tx, owner)?;
        if after.get("count").and_then(serde_json::Value::as_i64) != Some(0) {
            return Err("owner purge left durable memory rows; transaction rolled back".into());
        }
        Self::restore_owner_triggers(&tx, &suspended)?;
        tx.commit().map_err(|error| error.to_string())?;
        Ok(serde_json::json!({
            "purged": true,
            "before": before,
            "after": after,
            "counts": before,
        }))
    }

    pub fn purge_owner(&self, owner: &str) -> Result<serde_json::Value, String> {
        self.purge_owner_authoritative(owner)
    }

    #[allow(dead_code)]
    fn purge_owner_legacy(&self, owner: &str) -> Result<serde_json::Value, String> {
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
        tx.execute(
            "DELETE FROM graph_cues_fts WHERE node_id IN (SELECT id FROM graph_nodes WHERE owner=?1)",
            params![owner],
        )
        .map_err(|error| error.to_string())?;

        // The v2 closure: an erasure context passes the append-only revision
        // triggers, mirroring the privacy-erasure pattern.
        let erasure_id = format!("purge_{}", uuid::Uuid::new_v4().simple());
        tx.execute(
            "INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES (?1,?2,NULL,?1,'owner purge',?3)",
            params![owner, erasure_id, chrono::Utc::now().to_rfc3339()],
        )
        .map_err(|error| error.to_string())?;
        // Job events and the legacy index-compat tables are unconditionally
        // append-only; a full owner purge lifts those guards inside this
        // transaction only (the DDL rolls back with it on any failure) and
        // restores them before commit.
        tx.execute_batch(
            "DROP TRIGGER IF EXISTS fm_v2_job_event_no_delete;
             DROP TRIGGER IF EXISTS fm_v2_legacy_generation_no_delete;
             DROP TRIGGER IF EXISTS fm_v2_legacy_head_no_delete;",
        )
        .map_err(|error| error.to_string())?;
        // Children before parents, per the v2 foreign-key graph. Some tables
        // (the legacy index-compat pair) exist only after a legacy
        // migration, so the closure filters by what this store actually has.
        let existing_tables: std::collections::BTreeSet<String> = {
            let mut statement = tx
                .prepare("SELECT name FROM sqlite_master WHERE type='table'")
                .map_err(|error| error.to_string())?;
            let rows = statement
                .query_map([], |row| row.get::<_, String>(0))
                .map_err(|error| error.to_string())?;
            rows.collect::<Result<_, _>>()
                .map_err(|error: rusqlite::Error| error.to_string())?
        };
        for table in [
            "fm_v2_index_pointers",
            "fm_v2_chunk_embeddings",
            "fm_v2_index_publications",
            "fm_v2_derived_generations",
            "fm_v2_index_generations",
            "fm_v2_index_heads",
            "fm_v2_chunks",
            "fm_v2_documents",
            "fm_v2_candidate_evidence",
            "fm_v2_candidate_revisions",
            "fm_v2_candidates",
            "fm_v2_revision_evidence",
            "fm_v2_conflict_events",
            "fm_v2_conflicts",
            "fm_v2_knowledge_revisions",
            "fm_v2_knowledge_blocks",
            "fm_v2_entity_aliases",
            "fm_v2_entity_roles",
            "fm_v2_entity_tags",
            "fm_v2_entity_revisions",
            "fm_v2_entities",
            "fm_v2_episodes",
            "fm_v2_evidence",
            "fm_v2_sources",
            "fm_v2_policy_trust",
            "fm_v2_policy_profiles",
            "fm_v2_project_locators",
            "fm_v2_projects",
            "fm_v2_policy_projections",
            "fm_v2_spells",
            "fm_v2_policy_transitions",
            "fm_v2_trust_assignments",
            "fm_v2_job_manifests",
            "fm_v2_job_events",
            "fm_v2_attempts",
            "fm_v2_jobs",
            "fm_v2_outbox",
            "fm_v2_migration_review",
        ] {
            if !existing_tables.contains(table) {
                continue;
            }
            tx.execute(
                &format!("DELETE FROM {table} WHERE owner_id=?1"),
                params![owner],
            )
            .map_err(|error| error.to_string())?;
        }
        // Migration events and outbox dispositions carry no owner column;
        // reach them through the owner's plan/legacy-outbox rows, matching
        // the selective-reset migration closure. Migration runs are shared
        // across owners and stay.
        tx.execute(
            "DELETE FROM fm_v2_migration_events WHERE plan_id IN (SELECT plan_id FROM fm_v2_migration_plan WHERE owner_id=?1)",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM fm_v2_migration_plan WHERE owner_id=?1",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM fm_v2_migration_outbox_events WHERE outbox_sequence IN (SELECT sequence FROM fm_v2_legacy_outbox WHERE owner_id=?1)",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM fm_v2_legacy_outbox WHERE owner_id=?1",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        Self::delete_orphaned_change_sets(&tx, owner)?;
        tx.execute(
            "DELETE FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute(
            "DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
        tx.execute_batch(
            "CREATE TRIGGER IF NOT EXISTS fm_v2_job_event_no_delete
             BEFORE DELETE ON fm_v2_job_events BEGIN
                 SELECT RAISE(ABORT, 'job events are append-only');
             END;",
        )
        .map_err(|error| error.to_string())?;
        for (table, trigger, message) in [
            (
                "fm_v2_index_generations",
                "fm_v2_legacy_generation_no_delete",
                "fm_v2_index_generations is read-only legacy state",
            ),
            (
                "fm_v2_index_heads",
                "fm_v2_legacy_head_no_delete",
                "fm_v2_index_heads is read-only legacy state",
            ),
        ] {
            let table_present: bool = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
                    [table],
                    |row| row.get(0),
                )
                .map_err(|error| error.to_string())?;
            if table_present {
                tx.execute_batch(&format!(
                    "CREATE TRIGGER IF NOT EXISTS {trigger}
                     BEFORE DELETE ON {table} BEGIN
                         SELECT RAISE(ABORT, '{message}');
                     END;"
                ))
                .map_err(|error| error.to_string())?;
            }
        }

        for table in [
            "graph_cues",
            "graph_edges",
            "graph_nodes",
            "facts",
            "candidates",
            "memory_quarantine",
            "memory_leases",
            "memory_tombstones",
            "memory_retention_policy",
            "memory_retention_operations",
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

    fn rename_owner_authoritative(
        &self,
        owner: &str,
        new_owner: &str,
    ) -> Result<serde_json::Value, String> {
        if owner.trim().is_empty() || new_owner.trim().is_empty() || owner == new_owner {
            return Err("distinct non-empty owner names are required".into());
        }
        let mut conn = self.conn.lock().unwrap();
        let before = Self::owner_inventory_inner(&conn, owner)?;
        let target_before = Self::owner_inventory_inner(&conn, new_owner)?;
        let source_count = before
            .get("count")
            .and_then(serde_json::Value::as_i64)
            .unwrap_or(0);
        let target_count = target_before
            .get("count")
            .and_then(serde_json::Value::as_i64)
            .unwrap_or(0);
        if source_count == 0 {
            // A replay after the first transaction committed observes the
            // source empty and the target populated.  It is an idempotent
            // success; callers freeze the target preflight before first use,
            // so this path never merges two live tenants.
            return Ok(serde_json::json!({
                "renamed": true,
                "already_applied": target_count > 0,
                "from": owner,
                "to": new_owner,
                "before": before,
                "after": target_before,
                "counts": before,
            }));
        }
        if target_count > 0 {
            return Err("target owner already has memory".into());
        }

        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| error.to_string())?;
        tx.execute_batch("PRAGMA defer_foreign_keys=ON;")
            .map_err(|error| error.to_string())?;
        let owner_tables = Self::owner_domain_tables(&tx)?;
        let owner_table_names = owner_tables
            .iter()
            .map(|(table, _)| table.clone())
            .collect::<HashSet<_>>();
        let suspended = Self::suspend_owner_triggers(&tx, &owner_table_names, "UPDATE")?;

        // Legacy graph IDs are owner-derived rather than merely owner-keyed.
        // Rekey them before changing owner columns while preserving every
        // edge and cue reference within this same transaction.
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
            let temporary = format!("rename:{}:{}", uuid::Uuid::new_v4().simple(), old_id);
            let scope =
                crate::graph::GraphScope::new(new_owner, workspace_id).map_err(str::to_string)?;
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

        for (table, owner_column) in &owner_tables {
            tx.execute(
                &format!("UPDATE {table} SET {owner_column}=?1 WHERE {owner_column}=?2"),
                params![new_owner, owner],
            )
            .map_err(|error| error.to_string())?;
        }
        tx.execute(
            "UPDATE code_files
             SET codebase=(?1 || substr(codebase, length(?2)+1))
             WHERE codebase LIKE (?2 || char(31) || '%')",
            params![new_owner, owner],
        )
        .map_err(|error| error.to_string())?;

        // graph_cues_fts has no owner key and does not observe node_id-only
        // updates, so rebuild that derived index transactionally.
        tx.execute("DELETE FROM graph_cues_fts", [])
            .map_err(|error| error.to_string())?;
        tx.execute(
            "INSERT INTO graph_cues_fts(cue,node_id)
             SELECT cue,node_id FROM graph_cues WHERE status='active'",
            [],
        )
        .map_err(|error| error.to_string())?;

        let source_after = Self::owner_inventory_inner(&tx, owner)?;
        let target_after = Self::owner_inventory_inner(&tx, new_owner)?;
        if source_after
            .get("count")
            .and_then(serde_json::Value::as_i64)
            != Some(0)
            || before.get("tables") != target_after.get("tables")
        {
            return Err("owner rename verification failed; transaction rolled back".into());
        }
        Self::restore_owner_triggers(&tx, &suspended)?;
        tx.commit().map_err(|error| error.to_string())?;
        Ok(serde_json::json!({
            "renamed": true,
            "already_applied": false,
            "from": owner,
            "to": new_owner,
            "before": before,
            "after": target_after,
            "counts": before,
        }))
    }

    pub fn rename_owner(&self, owner: &str, new_owner: &str) -> Result<serde_json::Value, String> {
        self.rename_owner_authoritative(owner, new_owner)
    }

    #[allow(dead_code)]
    fn rename_owner_legacy(
        &self,
        owner: &str,
        new_owner: &str,
    ) -> Result<serde_json::Value, String> {
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
            "DELETE FROM memory_retention_operations WHERE owner=?1",
            params![owner],
        )
        .map_err(|error| error.to_string())?;
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
        let mut conn = self.conn.lock().unwrap();
        let transaction = match conn.transaction_with_behavior(TransactionBehavior::Immediate) {
            Ok(transaction) => transaction,
            Err(error) => {
                warn!("upsert_curated failed: {error}");
                return false;
            }
        };
        if let Err(e) = Self::upsert_curated_inner(&transaction, r, embedding) {
            warn!("upsert_curated failed: {e}");
            return false;
        }
        if let Err(error) = transaction.commit() {
            warn!("upsert_curated failed: {error}");
            return false;
        }

        info!("upserted curated record {}", r.id);
        true
    }

    async fn delete_curated_batch(&self, ids: &[String]) -> bool {
        let mut conn = self.conn.lock().unwrap();
        let Ok(transaction) = conn.transaction_with_behavior(TransactionBehavior::Immediate) else {
            return false;
        };
        for id in ids {
            let rowid = match transaction
                .query_row(
                    "SELECT rowid FROM curated WHERE id = ?1",
                    params![id],
                    |row| row.get::<_, i64>(0),
                )
                .optional()
            {
                Ok(rowid) => rowid,
                Err(_) => return false,
            };
            if rowid.is_some_and(|rowid| {
                transaction
                    .execute("DELETE FROM curated_fts WHERE rowid = ?1", params![rowid])
                    .is_err()
            }) || transaction
                .execute("DELETE FROM curated WHERE id = ?1", params![id])
                .is_err()
                || super::graph::sync_curated_projection_inner(&transaction, id).is_err()
            {
                return false;
            }
        }
        transaction.commit().is_ok()
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
        let mut conn = self.conn.lock().unwrap();
        let Ok(transaction) = conn.transaction_with_behavior(TransactionBehavior::Immediate) else {
            return false;
        };
        let row = transaction.query_row(
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
        let next_kind = kind
            .map(|value| format!("{value:?}").to_lowercase())
            .unwrap_or_else(|| {
                transaction
                    .query_row(
                        "SELECT kind FROM curated WHERE id = ?1",
                        params![id],
                        |row| row.get(0),
                    )
                    .unwrap_or_else(|_| "episodic".into())
            });
        let next_content = content.unwrap_or(&old_content);
        let next_content = if next_kind == "unknown" {
            crate::record::normalize_question(next_content)
        } else {
            next_content.to_string()
        };
        let Some((next_content, _)) = crate::privacy::filter_text(&next_content) else {
            return false;
        };
        let (mut metadata, _) = crate::privacy::filter_json(&metadata);
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        metadata
            .as_object_mut()
            .expect("metadata was normalized to an object")
            .insert(
                "content_hash".into(),
                crate::privacy::content_hash(&next_content).into(),
            );
        let now = chrono::Utc::now().to_rfc3339();
        if transaction
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
        if transaction
            .execute(
                "INSERT OR REPLACE INTO curated_fts(rowid, content, scene_name, tags, workspace_id)
                 SELECT rowid, content, COALESCE(scene_name, ''), tags, workspace_id FROM curated WHERE id = ?1",
                params![id],
            )
            .is_err()
            || super::graph::sync_curated_projection_inner(&transaction, id).is_err()
        {
            return false;
        }
        let _ = rowid;
        transaction.commit().is_ok()
    }

    async fn delete_curated_record(
        &self,
        id: &str,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        let mut conn = self.conn.lock().unwrap();
        let Ok(transaction) = conn.transaction_with_behavior(TransactionBehavior::Immediate) else {
            return false;
        };
        let row = transaction.query_row(
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
        if transaction
            .execute("DELETE FROM curated_fts WHERE rowid = ?1", params![rowid])
            .is_err()
            || transaction
                .execute("DELETE FROM curated WHERE id = ?1", params![id])
                .is_err()
            || super::graph::sync_curated_projection_inner(&transaction, id).is_err()
        {
            return false;
        }
        transaction.commit().is_ok()
    }

    async fn delete_curated_expired(&self, cutoff_iso: &str) -> usize {
        let mut conn = self.conn.lock().unwrap();
        let Ok(transaction) = conn.transaction_with_behavior(TransactionBehavior::Immediate) else {
            return 0;
        };
        let expired = {
            let mut statement = match transaction
                .prepare("SELECT id, rowid FROM curated WHERE archived = 1 AND updated_at < ?1")
            {
                Ok(statement) => statement,
                Err(_) => return 0,
            };
            let rows = match statement.query_map(params![cutoff_iso], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            }) {
                Ok(rows) => rows,
                Err(_) => return 0,
            };
            match rows.collect::<Result<Vec<_>, _>>() {
                Ok(rows) => rows,
                Err(_) => return 0,
            }
        };
        for (id, rowid) in &expired {
            if transaction
                .execute("DELETE FROM curated_fts WHERE rowid = ?1", params![rowid])
                .is_err()
                || transaction
                    .execute("DELETE FROM curated WHERE id = ?1", params![id])
                    .is_err()
                || super::graph::sync_curated_projection_inner(&transaction, id).is_err()
            {
                return 0;
            }
        }
        if transaction.commit().is_err() {
            return 0;
        }
        expired.len()
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
        let mut score_map: std::collections::HashMap<
            String,
            (ScoredRecord, f32, Option<f32>, Option<f32>),
        > = std::collections::HashMap::new();

        for (rank, r) in fts_results.iter().enumerate() {
            let rrf_score = 1.0 / (rrf_k + rank as f32);
            let entry = score_map
                .entry(r.record.id.clone())
                .or_insert_with(|| (r.clone(), 0.0, None, None));
            entry.1 += rrf_score;
            entry.2 = Some(rrf_score);
        }
        for (rank, r) in vec_results.iter().enumerate() {
            let rrf_score = 1.0 / (rrf_k + rank as f32);
            let entry = score_map
                .entry(r.record.id.clone())
                .or_insert_with(|| (r.clone(), 0.0, None, None));
            entry.1 += rrf_score;
            entry.3 = Some(rrf_score);
        }

        let mut merged: Vec<ScoredRecord> = score_map
            .into_values()
            .map(|(mut r, score, lexical, vector)| {
                r.score = score;
                r.source_label = "hybrid_rrf".into();
                if !r.record.metadata.is_object() {
                    r.record.metadata = serde_json::json!({});
                }
                if let Some(metadata) = r.record.metadata.as_object_mut() {
                    metadata.insert(
                        "recall_explanation".into(),
                        serde_json::json!({
                            "strategy": "hybrid_rrf",
                            "lexical_rrf": lexical,
                            "vector_rrf": vector,
                            "graph": 0.0,
                            "fusion_score": score,
                        }),
                    );
                }
                r
            })
            .collect();
        merged.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        let mut versions: std::collections::HashMap<String, HashSet<String>> =
            std::collections::HashMap::new();
        for hit in &merged {
            let Some(source_uri) = hit
                .record
                .metadata
                .get("source_uri")
                .and_then(serde_json::Value::as_str)
            else {
                continue;
            };
            let revision = hit
                .record
                .metadata
                .get("source_revision")
                .map(serde_json::Value::to_string)
                .unwrap_or_default();
            let hash = hit
                .record
                .metadata
                .get("content_hash")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default();
            versions
                .entry(source_uri.to_string())
                .or_default()
                .insert(format!("{revision}\u{1f}{hash}"));
        }
        let mut seen = HashSet::new();
        merged.retain_mut(|hit| {
            let source_uri = hit
                .record
                .metadata
                .get("source_uri")
                .and_then(serde_json::Value::as_str);
            let identity = source_uri.map(|source_uri| {
                let revision = hit
                    .record
                    .metadata
                    .get("source_revision")
                    .map(serde_json::Value::to_string)
                    .unwrap_or_default();
                let hash = hit
                    .record
                    .metadata
                    .get("content_hash")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default();
                format!("{source_uri}\u{1f}{revision}\u{1f}{hash}")
            });
            if let Some(source_uri) = source_uri {
                if versions
                    .get(source_uri)
                    .is_some_and(|values| values.len() > 1)
                {
                    if let Some(metadata) = hit.record.metadata.as_object_mut() {
                        metadata.insert("provenance_conflict".into(), true.into());
                    }
                }
            }
            identity.is_none_or(|identity| seen.insert(identity))
        });
        merged.truncate(q.top_k);
        merged
    }

    async fn upsert_raw(&self, r: &RawTurn, embedding: Option<&[f32]>) -> bool {
        let mut r = r.clone();
        let Some((content, content_redacted)) = crate::privacy::filter_text(&r.content) else {
            return false;
        };
        let (mut metadata, _) = crate::privacy::filter_json(&r.metadata);
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        metadata
            .as_object_mut()
            .expect("metadata was normalized to an object")
            .insert(
                "content_hash".into(),
                crate::privacy::content_hash(&content).into(),
            );
        r.content = content;
        r.metadata = metadata;
        let conn = self.conn.lock().unwrap();
        let embedding_blob = (!content_redacted)
            .then_some(embedding)
            .flatten()
            .map(Self::embedding_to_blob);
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

    fn test_candidate(id: &str, content: &str) -> CandidateRecord {
        let now = chrono::Utc::now().to_rfc3339();
        CandidateRecord {
            id: id.into(),
            content: content.into(),
            kind: MemoryKind::Fact,
            confidence_score: 0.9,
            importance_score: 0.8,
            owner: "alice".into(),
            workspace_id: "ws-a".into(),
            workspace_path: None,
            session_id: "session-a".into(),
            turn_id: "turn-a".into(),
            raw_evidence_ids: vec!["raw-a".into()],
            evidence_role: "user".into(),
            source: "test".into(),
            source_event_id: format!("event-{id}"),
            dedup_key: format!("dedup-{id}"),
            source_uri: format!("event://alice/event-{id}"),
            source_revision: 1,
            content_hash: crate::privacy::content_hash(content),
            source_message_ids: vec!["message-a".into()],
            status: CandidateStatus::Pending,
            reason: "pending".into(),
            accepted_curated_id: None,
            created_at: now.clone(),
            updated_at: now,
        }
    }

    #[tokio::test]
    async fn maintenance_plan_rolls_back_curated_fts_and_graph_together() {
        let store = SqliteStore::memory(4).unwrap();
        let mut alice = test_record("original amber ledger");
        alice.id = "maintenance-alice".into();
        alice.workspace_id = "ws-a".into();
        let mut bob = test_record("original cobalt ledger");
        bob.id = "maintenance-bob".into();
        bob.owner = Some("bob".into());
        bob.workspace_id = "ws-a".into();
        assert!(store.upsert_curated(&alice, None).await);
        assert!(store.upsert_curated(&bob, None).await);

        let mut changed_alice = alice.clone();
        changed_alice.content = "changed amber ledger".into();
        let mut changed_bob = bob.clone();
        changed_bob.content = "changed cobalt ledger".into();
        let plan = CuratedMaintenancePlan {
            upserts: vec![changed_alice, changed_bob],
            delete_ids: vec![],
        };
        let error = store
            .apply_curated_maintenance_plan("alice", "ws-a", &plan)
            .unwrap_err();
        assert!(error.contains("outside authenticated scope"));

        let restored = store
            .get_curated_record("maintenance-alice", "alice", "ws-a")
            .unwrap()
            .unwrap();
        assert_eq!(restored.content, "original amber ledger");
        assert!(store
            .search_curated_fts_scoped("changed", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert_eq!(
            store
                .search_curated_fts_scoped("original", 10, Some("alice"), Some("ws-a"))
                .await
                .len(),
            1
        );
        let scope = crate::graph::GraphScope::new("alice", "ws-a").unwrap();
        let node_id = scope.node_id("memory", "maintenance-alice");
        let (_, content) = store.graph_fetch(&scope, &node_id).unwrap().unwrap();
        assert_eq!(content.as_deref(), Some("original amber ledger"));
    }

    #[tokio::test]
    async fn curated_upsert_and_update_roll_back_fts_and_graph_failures() {
        {
            let store = SqliteStore::memory(4).unwrap();
            store
                .conn
                .lock()
                .unwrap()
                .execute_batch(
                    "CREATE TRIGGER reject_curated_projection_insert
                     BEFORE UPDATE OF ref_id ON graph_nodes
                     WHEN new.ref_id = 'upsert-rollback'
                     BEGIN
                       SELECT RAISE(ABORT, 'injected projection insert failure');
                     END;",
                )
                .unwrap();
            let mut record = test_record("atomic amber upsert");
            record.id = "upsert-rollback".into();
            assert!(!store.upsert_curated(&record, None).await);
            assert!(store
                .get_curated_record(&record.id, "alice", "global")
                .unwrap()
                .is_none());
            assert!(store
                .search_curated_fts_scoped("atomic amber", 10, Some("alice"), Some("global"))
                .await
                .is_empty());
        }

        {
            let store = SqliteStore::memory(4).unwrap();
            let mut record = test_record("original amber update");
            record.id = "update-rollback".into();
            assert!(store.upsert_curated(&record, None).await);
            store
                .conn
                .lock()
                .unwrap()
                .execute_batch(
                    "CREATE TRIGGER reject_curated_projection_update
                     BEFORE UPDATE ON graph_nodes
                     WHEN old.ref_id = 'update-rollback'
                     BEGIN
                       SELECT RAISE(ABORT, 'injected projection update failure');
                     END;",
                )
                .unwrap();

            assert!(
                !store
                    .update_curated_record(
                        &record.id,
                        Some("changed cobalt update"),
                        None,
                        None,
                        None,
                        Some("alice"),
                        Some("global"),
                    )
                    .await
            );
            assert_eq!(
                store
                    .get_curated_record(&record.id, "alice", "global")
                    .unwrap()
                    .unwrap()
                    .content,
                "original amber update"
            );
            assert!(store
                .search_curated_fts_scoped("changed cobalt", 10, Some("alice"), Some("global"))
                .await
                .is_empty());
            let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
            let (_, content) = store
                .graph_fetch(&scope, &scope.node_id("memory", &record.id))
                .unwrap()
                .unwrap();
            assert_eq!(content.as_deref(), Some("original amber update"));
        }
    }

    #[tokio::test]
    async fn curated_delete_and_archive_roll_back_graph_failures() {
        for operation in ["delete", "archive"] {
            let store = SqliteStore::memory(4).unwrap();
            let mut record = test_record(&format!("atomic {operation} ledger"));
            record.id = format!("{operation}-rollback");
            assert!(store.upsert_curated(&record, None).await);
            store
                .conn
                .lock()
                .unwrap()
                .execute_batch(&format!(
                    "CREATE TRIGGER reject_curated_projection_{operation}
                     BEFORE DELETE ON graph_nodes
                     WHEN old.ref_id = '{operation}-rollback'
                     BEGIN
                       SELECT RAISE(ABORT, 'injected projection removal failure');
                     END;"
                ))
                .unwrap();

            let succeeded = if operation == "delete" {
                store
                    .delete_curated_record(&record.id, Some("alice"), Some("global"))
                    .await
            } else {
                store.archive_curated_record(
                    &record.id,
                    &serde_json::json!({"resolved_by": "test"}),
                    None,
                    Some("alice"),
                    Some("global"),
                )
            };
            assert!(!succeeded);
            assert!(store
                .get_curated_record(&record.id, "alice", "global")
                .unwrap()
                .is_some());
            assert_eq!(
                store
                    .search_curated_fts_scoped(
                        &format!("atomic {operation}"),
                        10,
                        Some("alice"),
                        Some("global"),
                    )
                    .await
                    .len(),
                1
            );
            let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
            assert!(store
                .graph_fetch(&scope, &scope.node_id("memory", &record.id))
                .unwrap()
                .is_some());
        }
    }

    #[tokio::test]
    async fn curated_batch_delete_rolls_back_the_whole_batch() {
        let store = SqliteStore::memory(4).unwrap();
        let mut first = test_record("first batch rollback");
        first.id = "batch-first".into();
        let mut second = test_record("second batch rollback");
        second.id = "batch-second".into();
        assert!(store.upsert_curated(&first, None).await);
        assert!(store.upsert_curated(&second, None).await);
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER reject_second_batch_projection
                 BEFORE DELETE ON graph_nodes
                 WHEN old.ref_id = 'batch-second'
                 BEGIN
                   SELECT RAISE(ABORT, 'injected second projection failure');
                 END;",
            )
            .unwrap();

        assert!(
            !store
                .delete_curated_batch(&[first.id.clone(), second.id.clone()])
                .await
        );
        for record in [&first, &second] {
            assert!(store
                .get_curated_record(&record.id, "alice", "global")
                .unwrap()
                .is_some());
        }
        assert_eq!(
            store
                .search_curated_fts_scoped("batch rollback", 10, Some("alice"), Some("global"))
                .await
                .len(),
            2
        );
    }

    #[tokio::test]
    async fn candidate_acceptance_rolls_back_curated_projection_on_status_failure() {
        let store = SqliteStore::memory(4).unwrap();
        let candidate = test_candidate("candidate-rollback", "atomic amber memory");
        assert!(store.insert_candidate(&candidate).unwrap());
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER reject_candidate_accept
                 BEFORE UPDATE OF status ON candidates
                 WHEN new.status = 'accepted'
                 BEGIN
                   SELECT RAISE(ABORT, 'injected candidate status failure');
                 END;",
            )
            .unwrap();

        let mut record = test_record(&candidate.content);
        record.id = "curated-rollback".into();
        record.workspace_id = "ws-a".into();
        let error = store
            .accept_candidate_transaction(&candidate.id, &record, None, "approved", "alice", "ws-a")
            .unwrap_err();
        assert!(error.contains("injected candidate status failure"));
        assert!(store
            .get_curated_record(&record.id, "alice", "ws-a")
            .unwrap()
            .is_none());
        assert_eq!(
            store
                .candidate_by_id(&candidate.id, "alice", "ws-a")
                .unwrap()
                .unwrap()
                .status,
            CandidateStatus::Pending
        );
        let scope = crate::graph::GraphScope::new("alice", "ws-a").unwrap();
        assert!(store
            .graph_fetch(&scope, &scope.node_id("memory", &record.id))
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn archived_unknown_reopens_only_in_exact_tenant_scope() {
        let store = SqliteStore::memory(4).unwrap();
        let mut question = test_record("What is the amber code?");
        question.id = "unknown-reopen".into();
        question.kind = MemoryKind::Unknown;
        question.workspace_id = "ws-a".into();
        assert!(store.upsert_curated(&question, None).await);
        assert!(store.archive_curated_record(
            &question.id,
            &serde_json::json!({"resolved_by": "answer-1", "resolved_at": "now"}),
            Some("unknown"),
            Some("alice"),
            Some("ws-a"),
        ));

        assert!(!store
            .reopen_curated_unknown(&question.id, "bob", "ws-a")
            .unwrap());
        assert!(!store
            .reopen_curated_unknown(&question.id, "alice", "other")
            .unwrap());
        assert!(store
            .reopen_curated_unknown(&question.id, "alice", "ws-a")
            .unwrap());
        let reopened = store
            .get_curated_record(&question.id, "alice", "ws-a")
            .unwrap()
            .unwrap();
        assert!(!reopened.archived);
        assert!(reopened.metadata.get("resolved_by").is_none());
        assert!(reopened.metadata.get("resolved_at").is_none());
    }

    #[test]
    fn candidate_edits_stay_pending_and_tenant_scoped() {
        let store = SqliteStore::memory(4).unwrap();
        let candidate = test_candidate("candidate-edit", "draft amber memory");
        assert!(store.insert_candidate(&candidate).unwrap());

        assert!(store
            .update_pending_candidate(&candidate.id, "bob", "ws-a", "stolen", None, "review edit",)
            .unwrap()
            .is_none());
        let edited = store
            .update_pending_candidate(
                &candidate.id,
                "alice",
                "ws-a",
                "corrected amber memory",
                Some(MemoryKind::Instruction),
                "owner corrected import",
            )
            .unwrap()
            .unwrap();
        assert_eq!(edited.content, "corrected amber memory");
        assert_eq!(edited.kind, MemoryKind::Instruction);
        assert_eq!(edited.status, CandidateStatus::Pending);
        assert_eq!(edited.source_revision, 2);
        assert_eq!(
            edited.content_hash,
            crate::privacy::content_hash(&edited.content)
        );

        assert!(store
            .set_candidate_status(
                &candidate.id,
                CandidateStatus::Rejected,
                "no",
                None,
                "alice",
                "ws-a",
            )
            .unwrap());
        assert!(store
            .update_pending_candidate(
                &candidate.id,
                "alice",
                "ws-a",
                "resurrected",
                None,
                "late edit",
            )
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn candidate_consolidation_respects_an_active_tenant_lease() {
        let store = SqliteStore::memory(4).unwrap();
        let candidate = test_candidate("candidate-leased", "leased amber memory");
        assert!(store.insert_candidate(&candidate).unwrap());
        store
            .conn
            .lock()
            .unwrap()
            .execute(
                "INSERT INTO memory_leases(
                    kind,owner,workspace_id,subject,holder,acquired_at,expires_at,attempt
                 ) VALUES ('consolidation','alice','ws-a',?1,'other-worker',?2,?3,1)",
                params![
                    candidate.id,
                    chrono::Utc::now().to_rfc3339(),
                    (chrono::Utc::now() + chrono::Duration::minutes(5)).to_rfc3339()
                ],
            )
            .unwrap();
        let mut record = test_record(&candidate.content);
        record.id = "curated-leased".into();
        record.workspace_id = "ws-a".into();
        assert!(
            store
                .accept_candidate_transaction(
                    &candidate.id,
                    &record,
                    None,
                    "approved",
                    "alice",
                    "ws-a",
                )
                .unwrap_err()
                .contains("already leased")
        );
        assert!(store
            .get_curated_record(&record.id, "alice", "ws-a")
            .unwrap()
            .is_none());
        assert_eq!(
            store
                .candidate_by_id(&candidate.id, "alice", "ws-a")
                .unwrap()
                .unwrap()
                .status,
            CandidateStatus::Pending
        );
    }

    #[tokio::test]
    async fn digest_empty_bank_is_valid_shape() {
        let store = SqliteStore::memory(4).unwrap();
        let d = store.digest("alice", "global", true).unwrap();
        assert!(d["pinned"].as_array().unwrap().is_empty());
        assert!(d["clusters"].as_array().unwrap().is_empty());
        assert!(d["recent"].as_array().unwrap().is_empty());
        assert!(d["open_questions"].as_array().unwrap().is_empty());
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
    async fn digest_open_questions_scoped_capped_and_archived_excluded() {
        let store = SqliteStore::memory(4).unwrap();
        // 7 open questions for alice in-scope: cap keeps 5, most recently
        // updated first.
        for i in 0..7 {
            let mut q = test_record(&format!("question number {i}?"));
            q.kind = MemoryKind::Unknown;
            q.source_type = SourceType::Human;
            q.workspace_id = "repo-x".into();
            q.updated_at = format!("2026-07-17T00:00:0{i}Z");
            store.upsert_curated(&q, None).await;
        }
        let mut archived = test_record("archived question?");
        archived.kind = MemoryKind::Unknown;
        archived.archived = true;
        store.upsert_curated(&archived, None).await;
        let mut global_q = test_record("global question?");
        global_q.kind = MemoryKind::Unknown;
        global_q.source_type = SourceType::Human;
        global_q.updated_at = "2026-07-18T00:00:00Z".into();
        store.upsert_curated(&global_q, None).await;
        let mut bobs = test_record("bobs question?");
        bobs.kind = MemoryKind::Unknown;
        bobs.owner = Some("bob".into());
        bobs.workspace_id = "repo-x".into();
        store.upsert_curated(&bobs, None).await;

        let d = store.digest("alice", "repo-x", true).unwrap();
        let questions = d["open_questions"].as_array().unwrap();
        assert_eq!(questions.len(), 5, "capped at 5");
        assert_eq!(
            questions[0]["content"], "global question?",
            "workspace ∪ global, most recently touched first"
        );
        for q in questions {
            assert_ne!(q["content"], "archived question?");
            assert_ne!(q["content"], "bobs question?");
            assert!(q["id"].is_string());
            assert!(q["workspace_id"].is_string());
            assert_eq!(q["source_type"], "human");
        }

        // Workspace-strict scope drops the global question.
        let strict = store.digest("alice", "repo-x", false).unwrap();
        for q in strict["open_questions"].as_array().unwrap() {
            assert_ne!(q["content"], "global question?");
        }

        let empty = store.digest("carol", "repo-x", true).unwrap();
        assert!(empty["open_questions"].as_array().unwrap().is_empty());
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

    #[test]
    fn quality_status_reports_non_destructive_sqlite_health() {
        let store = SqliteStore::memory(4).unwrap();
        let health = store.sqlite_health();
        assert_eq!(health["state"], "ready");
        assert_eq!(health["quick_check"], "ok");
        assert_eq!(health["repair"], "none");

        let quality = store.quality_status().unwrap();
        assert_eq!(quality["degraded"], false);
        assert_eq!(quality["sqlite"]["state"], "ready");
        assert_eq!(quality["recovery"]["automatic_repair"], false);
    }

    #[test]
    fn future_schema_version_fails_closed_without_ddl() {
        let path = std::env::temp_dir().join(format!(
            "fm-future-schema-{}-{}.db",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE future_sentinel(value TEXT);
                 INSERT INTO future_sentinel VALUES('preserve');
                 PRAGMA user_version=99;",
            )
            .unwrap();
        }
        let bytes_before = std::fs::read(&path).unwrap();
        assert!(SqliteStore::new(path.to_str().unwrap(), 4).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), bytes_before);
        let conn = Connection::open(&path).unwrap();
        assert_eq!(
            conn.query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            99
        );
        assert_eq!(
            conn.query_row("SELECT value FROM future_sentinel", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "preserve"
        );
        assert_eq!(
            conn.query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='curated'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            0
        );
        drop(conn);
        let _ = std::fs::remove_file(path);
    }

    #[cfg(unix)]
    #[test]
    fn persistent_sqlite_authority_is_relocked_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let path = std::env::temp_dir().join(format!(
            "fm-permissions-{}-{}.db",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let store = SqliteStore::new(path.to_str().unwrap(), 4).unwrap();
        drop(store);
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let _ = std::fs::remove_file(path);
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
    async fn migrate_v6_db_to_lifecycle_schema_preserves_data() {
        let path = std::env::temp_dir().join(format!(
            "fm-migrate-v6-test-{}-{}.db",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path_s = path.to_str().unwrap().to_string();
        {
            let store = SqliteStore::new(&path_s, 4).unwrap();
            store
                .upsert_curated(&test_record("survives the v6 lifecycle migration"), None)
                .await;
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "DROP TABLE memory_retention_operations;
                 DROP TABLE memory_retention_policy;
                 DROP TABLE memory_tombstones;
                 DROP TABLE memory_leases;
                 ALTER TABLE candidates DROP COLUMN source_uri;
                 ALTER TABLE candidates DROP COLUMN source_revision;
                 ALTER TABLE candidates DROP COLUMN content_hash;
                 ALTER TABLE candidates DROP COLUMN source_message_ids;
                 PRAGMA user_version=6;",
            )
            .unwrap();
        }
        {
            let store = SqliteStore::new(&path_s, 4).unwrap();
            let conn = store.conn.lock().unwrap();
            let v: i64 = conn
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .unwrap();
            assert_eq!(v, SCHEMA_VERSION);
            for table in [
                "memory_leases",
                "memory_tombstones",
                "memory_retention_policy",
                "memory_retention_operations",
            ] {
                let exists: i64 = conn
                    .query_row(
                        "SELECT count(*) FROM sqlite_master
                         WHERE type='table' AND name=?1",
                        params![table],
                        |row| row.get(0),
                    )
                    .unwrap();
                assert_eq!(exists, 1, "{table} must be created during migration");
            }
            assert!(SqliteStore::has_column(&conn, "memory_tombstones", "operation_id").unwrap());
            assert!(SqliteStore::has_column(&conn, "memory_tombstones", "operation_key").unwrap());
            drop(conn);
            let results = store
                .search_curated_fts_scoped(
                    "survives lifecycle migration",
                    10,
                    Some("alice"),
                    Some("global"),
                )
                .await;
            assert!(!results.is_empty(), "v6 data must survive migration");
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
    async fn curated_upsert_projects_fetchable_structured_graph_node() {
        let store = SqliteStore::memory(4).unwrap();
        let mut record = test_record("keeper of the amber greenhouse ledger");
        record.metadata = serde_json::json!({"category": "greenhouse"});
        assert!(store.upsert_curated(&record, None).await);

        let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
        let memory_id = scope.node_id("memory", &record.id);
        let (node, content) = store.graph_fetch(&scope, &memory_id).unwrap().unwrap();
        assert_eq!(node.kind, "memory");
        assert_eq!(node.layer, "episodic");
        assert_eq!(node.name, record.id);
        assert_eq!(content.as_deref(), Some(record.content.as_str()));
        assert!(store
            .graph_cues(&scope, "amber greenhouse", 10)
            .unwrap()
            .iter()
            .any(|hit| hit.node.id == memory_id));

        let overview = store.graph_overview(&scope, 10).unwrap();
        let category_id = scope.node_id("category", "greenhouse");
        assert!(overview
            .nodes
            .iter()
            .any(|node| node.id == category_id && node.kind == "category"));
        assert!(overview.edges.iter().any(|edge| {
            edge.src_id == memory_id && edge.dst_id == category_id && edge.tag == "is"
        }));
        let category_ref: (Option<String>, Option<String>) = store
            .conn
            .lock()
            .unwrap()
            .query_row(
                "SELECT ref_table, ref_id FROM graph_nodes WHERE id = ?1",
                params![category_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(category_ref, (None, None));
    }

    #[tokio::test]
    async fn curated_update_replaces_projection_cues_and_category() {
        let store = SqliteStore::memory(4).unwrap();
        let mut record = test_record("amber greenhouse ledger");
        record.metadata = serde_json::json!({"category": "greenhouse"});
        assert!(store.upsert_curated(&record, None).await);

        assert!(
            store
                .update_curated_record(
                    &record.id,
                    Some("cobalt observatory notebook"),
                    Some(MemoryKind::Persona),
                    Some("astronomy"),
                    None,
                    Some("alice"),
                    Some("global"),
                )
                .await
        );

        let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
        let memory_id = scope.node_id("memory", &record.id);
        assert!(store
            .graph_cues(&scope, "amber greenhouse", 10)
            .unwrap()
            .is_empty());
        let hits = store.graph_cues(&scope, "cobalt observatory", 10).unwrap();
        assert!(hits.iter().any(|hit| hit.node.id == memory_id));
        let (node, content) = store.graph_fetch(&scope, &memory_id).unwrap().unwrap();
        assert_eq!(node.label.as_deref(), Some("cobalt observatory notebook"));
        assert_eq!(content.as_deref(), Some("cobalt observatory notebook"));

        let overview = store.graph_overview(&scope, 10).unwrap();
        let astronomy_id = scope.node_id("category", "astronomy");
        assert!(overview.edges.iter().any(|edge| {
            edge.src_id == memory_id && edge.dst_id == astronomy_id && edge.tag == "is"
        }));
        assert!(!overview
            .nodes
            .iter()
            .any(|node| node.id == scope.node_id("category", "greenhouse")));
        assert!(!overview
            .nodes
            .iter()
            .any(|node| node.id == scope.node_id("category", "persona")));
    }

    #[tokio::test]
    async fn curated_delete_paths_remove_only_memory_projection() {
        let store = SqliteStore::memory(4).unwrap();
        let first = test_record("first lunar archive");
        let second = test_record("second solar archive");
        assert!(store.upsert_curated(&first, None).await);
        assert!(store.upsert_curated(&second, None).await);

        let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
        store
            .graph_upsert(
                &scope,
                &crate::graph::GraphUpsertInput {
                    nodes: vec![
                        crate::graph::GraphNodeInput {
                            kind: "project".into(),
                            name: "independent overlay".into(),
                            label: None,
                            layer: None,
                            trust: None,
                        },
                        crate::graph::GraphNodeInput {
                            kind: "category".into(),
                            name: "manual taxonomy".into(),
                            label: Some("Hand-maintained taxonomy".into()),
                            layer: Some("semantic".into()),
                            trust: None,
                        },
                    ],
                    edges: vec![],
                    cues: vec![crate::graph::GraphCueInput {
                        cue: "independent overlay".into(),
                        node: crate::graph::NodeRef {
                            kind: "project".into(),
                            name: "independent overlay".into(),
                        },
                        source: Some("extracted".into()),
                    }],
                },
            )
            .unwrap();

        let first_id = scope.node_id("memory", &first.id);
        assert!(
            store
                .delete_curated_record(&first.id, Some("alice"), Some("global"))
                .await
        );
        assert!(store.graph_fetch(&scope, &first_id).unwrap().is_none());
        assert!(store
            .graph_cues(&scope, "first lunar", 10)
            .unwrap()
            .is_empty());
        assert!(!store
            .graph_cues(&scope, "independent overlay", 10)
            .unwrap()
            .is_empty());
        assert!(store
            .graph_overview(&scope, 10)
            .unwrap()
            .nodes
            .iter()
            .any(|node| node.id == scope.node_id("category", "episodic")));

        let second_id = scope.node_id("memory", &second.id);
        assert!(store.delete_curated_batch(&[second.id.clone()]).await);
        assert!(store.graph_fetch(&scope, &second_id).unwrap().is_none());
        assert!(store
            .graph_cues(&scope, "second solar", 10)
            .unwrap()
            .is_empty());
        let overview = store.graph_overview(&scope, 10).unwrap();
        assert!(!overview
            .nodes
            .iter()
            .any(|node| node.id == scope.node_id("category", "episodic")));
        assert!(overview
            .nodes
            .iter()
            .any(|node| node.id == scope.node_id("project", "independent overlay")));
        assert!(overview
            .nodes
            .iter()
            .any(|node| node.id == scope.node_id("category", "manual taxonomy")));
    }

    #[tokio::test]
    async fn curated_projection_preserves_preexisting_semantic_category_node() {
        let store = SqliteStore::memory(4).unwrap();
        let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
        store
            .graph_upsert(
                &scope,
                &crate::graph::GraphUpsertInput {
                    nodes: vec![crate::graph::GraphNodeInput {
                        kind: "category".into(),
                        name: "custom taxonomy".into(),
                        label: Some("Hand-maintained custom taxonomy".into()),
                        layer: Some("semantic".into()),
                        trust: None,
                    }],
                    edges: vec![],
                    cues: vec![],
                },
            )
            .unwrap();

        let mut record = test_record("custom taxonomy memory");
        record.metadata = serde_json::json!({"category": "custom taxonomy"});
        assert!(store.upsert_curated(&record, None).await);
        let category_id = scope.node_id("category", "custom taxonomy");
        let (category, _) = store.graph_fetch(&scope, &category_id).unwrap().unwrap();
        assert_eq!(
            category.label.as_deref(),
            Some("Hand-maintained custom taxonomy")
        );

        assert!(
            store
                .delete_curated_record(&record.id, Some("alice"), Some("global"))
                .await
        );
        let (category, _) = store.graph_fetch(&scope, &category_id).unwrap().unwrap();
        assert_eq!(
            category.label.as_deref(),
            Some("Hand-maintained custom taxonomy")
        );
    }

    #[tokio::test]
    async fn startup_backfills_curated_graph_projection_idempotently() {
        let path = std::env::temp_dir().join(format!(
            "fm-graph-backfill-test-{}-{}.db",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path_s = path.to_str().unwrap().to_string();
        let mut record = test_record("backfilled violet compass");
        record.metadata = serde_json::json!({"category": "navigation"});
        {
            let store = SqliteStore::new(&path_s, 4).unwrap();
            assert!(store.upsert_curated(&record, None).await);
            store
                .conn
                .lock()
                .unwrap()
                .execute_batch(
                    "DELETE FROM graph_edges;
                     DELETE FROM graph_cues;
                     DELETE FROM graph_nodes;",
                )
                .unwrap();
        }

        let scope = crate::graph::GraphScope::new("alice", "global").unwrap();
        let memory_id = scope.node_id("memory", &record.id);
        {
            let store = SqliteStore::new(&path_s, 4).unwrap();
            let (_, content) = store.graph_fetch(&scope, &memory_id).unwrap().unwrap();
            assert_eq!(content.as_deref(), Some(record.content.as_str()));
            assert!(!store
                .graph_cues(&scope, "violet compass", 10)
                .unwrap()
                .is_empty());
            let overview = store.graph_overview(&scope, 10).unwrap();
            assert_eq!(overview.node_total, 2);
            assert_eq!(overview.edge_total, 1);
            assert!(overview
                .nodes
                .iter()
                .any(|node| node.id == scope.node_id("category", "navigation")));
        }
        {
            let store = SqliteStore::new(&path_s, 4).unwrap();
            let overview = store.graph_overview(&scope, 10).unwrap();
            assert_eq!(overview.node_total, 2);
            assert_eq!(overview.edge_total, 1);
        }
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn curated_graph_projection_is_owner_isolated() {
        let store = SqliteStore::memory(4).unwrap();
        let alice_record = test_record("shared quartz observatory");
        let mut bob_record = test_record("shared quartz observatory");
        bob_record.owner = Some("bob".into());
        assert!(store.upsert_curated(&alice_record, None).await);
        assert!(store.upsert_curated(&bob_record, None).await);

        let alice = crate::graph::GraphScope::new("alice", "global").unwrap();
        let bob = crate::graph::GraphScope::new("bob", "global").unwrap();
        let alice_id = alice.node_id("memory", &alice_record.id);
        let bob_id = bob.node_id("memory", &bob_record.id);
        let alice_hits = store.graph_cues(&alice, "quartz observatory", 10).unwrap();
        let bob_hits = store.graph_cues(&bob, "quartz observatory", 10).unwrap();
        assert_eq!(
            alice_hits
                .iter()
                .filter(|hit| hit.node.kind == "memory")
                .map(|hit| &hit.node.id)
                .collect::<Vec<_>>(),
            vec![&alice_id]
        );
        assert_eq!(
            bob_hits
                .iter()
                .filter(|hit| hit.node.kind == "memory")
                .map(|hit| &hit.node.id)
                .collect::<Vec<_>>(),
            vec![&bob_id]
        );
        assert!(store.graph_fetch(&bob, &alice_id).unwrap().is_none());
        assert_eq!(store.graph_overview(&alice, 10).unwrap().node_total, 2);
        assert_eq!(store.graph_overview(&bob, 10).unwrap().node_total, 2);
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
        let explanation = &results[0].record.metadata["recall_explanation"];
        assert_eq!(explanation["strategy"], "hybrid_rrf");
        assert!(explanation["lexical_rrf"].is_number());
        assert!(explanation["vector_rrf"].is_number());
        assert_eq!(explanation["graph"], 0.0);
    }

    #[tokio::test]
    async fn hybrid_dedups_equivalent_sources_and_surfaces_conflicts() {
        let store = SqliteStore::memory(4).unwrap();
        for (id, hash) in [("projection-a", "hash-a"), ("projection-b", "hash-a")] {
            let mut record = test_record("cobalt launch checklist");
            record.id = id.into();
            record.metadata = serde_json::json!({
                "source_uri": "file:///brain/checklist.md",
                "source_revision": 1,
                "content_hash": hash,
            });
            assert!(
                store
                    .upsert_curated(&record, Some(&[1.0, 0.0, 0.0, 0.0]))
                    .await
            );
        }
        let query = HybridQuery {
            query_text: Some("cobalt checklist".into()),
            query_embedding: Some(vec![1.0, 0.0, 0.0, 0.0]),
            sparse_vector: None,
            top_k: 10,
            workspace_id: None,
        };
        let equivalent = store
            .search_curated_hybrid_scoped(query.clone(), Some("alice"), Some("global"))
            .await;
        assert_eq!(equivalent.len(), 1);

        let mut conflict = test_record("cobalt launch checklist revised");
        conflict.id = "projection-conflict".into();
        conflict.metadata = serde_json::json!({
            "source_uri": "file:///brain/checklist.md",
            "source_revision": 2,
            "content_hash": "hash-b",
        });
        assert!(
            store
                .upsert_curated(&conflict, Some(&[1.0, 0.0, 0.0, 0.0]))
                .await
        );
        let conflicted = store
            .search_curated_hybrid_scoped(query, Some("alice"), Some("global"))
            .await;
        assert_eq!(conflicted.len(), 2);
        assert!(conflicted
            .iter()
            .all(|hit| hit.record.metadata["provenance_conflict"] == true));
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
        assert_eq!(quarantined, 8);
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
        assert!(renamed
            .iter()
            .any(|hit| hit.node.id == new_scope.node_id("project", "private")));
        let renamed_memory_id = new_scope.node_id("memory", "m_alice");
        assert!(renamed.iter().any(|hit| hit.node.id == renamed_memory_id));
        assert!(store
            .graph_fetch(&new_scope, &renamed_memory_id)
            .unwrap()
            .is_some());

        store.purge_owner("alicia").unwrap();
        assert_eq!(store.owner_counts("alicia").unwrap()["curated"], 0);
        assert_eq!(store.owner_counts("bob").unwrap()["curated"], 1);
        assert!(!store
            .search_curated_fts_scoped("bob private", 10, Some("bob"), Some("workspace"))
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn owner_lifecycle_covers_v2_python_and_code_domains_but_excludes_media() {
        let store = SqliteStore::memory(4).unwrap();
        for owner in ["alice", "bob"] {
            let mut record = test_record(&format!("{owner} lifecycle sentinel"));
            record.id = format!("curated-{owner}");
            record.owner = Some(owner.into());
            record.workspace_id = "workspace".into();
            assert!(store.upsert_curated(&record, None).await);
        }
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE fm_v2_principal_bindings(
                    owner_id TEXT NOT NULL,binding_id TEXT NOT NULL,payload TEXT NOT NULL,
                    PRIMARY KEY(owner_id,binding_id)
                );
                CREATE TABLE fm_v2_tool_catalog(
                    owner_id TEXT NOT NULL,tool_name TEXT NOT NULL,description TEXT NOT NULL,
                    PRIMARY KEY(owner_id,tool_name)
                );
                CREATE TABLE fm_v2_policy_trust_events(
                    owner_id TEXT NOT NULL,event_id TEXT NOT NULL,event_hash TEXT NOT NULL,
                    PRIMARY KEY(owner_id,event_id)
                );
                CREATE VIRTUAL TABLE fm_v2_chunks_fts
                    USING fts5(owner_id UNINDEXED,chunk_id UNINDEXED,text);
                CREATE TABLE fm_v2_media_assets(
                    owner_id TEXT NOT NULL,asset_id TEXT NOT NULL,
                    PRIMARY KEY(owner_id,asset_id)
                );
                CREATE TABLE fm_v2_media_representations(
                    owner_id TEXT NOT NULL,representation_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    PRIMARY KEY(owner_id,representation_id),
                    FOREIGN KEY(owner_id,asset_id)
                        REFERENCES fm_v2_media_assets(owner_id,asset_id)
                );

                INSERT INTO fm_v2_change_sets(
                    owner_id,change_set_id,actor_type,actor_id,reason,
                    contract_version,content_hash,created_at
                ) VALUES
                    ('alice','cs-alice','user','alice','rename test','2.0','cs-a','2026-01-01T00:00:00Z'),
                    ('bob','cs-bob','user','bob','scope test','2.0','cs-b','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_sources(
                    owner_id,source_id,workspace_key,project_key,source_uri,
                    source_revision,content_hash,source_type,created_at
                ) VALUES
                    ('alice','source-alice','workspace','','file:///private/alice.txt',1,'source-a','document','2026-01-01T00:00:00Z'),
                    ('bob','source-bob','workspace','','file:///private/bob.txt',1,'source-b','document','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_documents(
                    owner_id,document_id,source_id,source_revision,title,
                    parser_version,content_hash,created_at
                ) VALUES
                    ('alice','document-alice','source-alice',1,'Alice','test','doc-a','2026-01-01T00:00:00Z'),
                    ('bob','document-bob','source-bob',1,'Bob','test','doc-b','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_chunks(
                    owner_id,chunk_id,document_id,ordinal,text,content_hash,
                    locator_json,created_at
                ) VALUES
                    ('alice','chunk-alice','document-alice',0,'alice private text','chunk-a','{}','2026-01-01T00:00:00Z'),
                    ('bob','chunk-bob','document-bob',0,'bob private text','chunk-b','{}','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) VALUES
                    ('alice','chunk-alice','alice private text'),
                    ('bob','chunk-bob','bob private text');
                INSERT INTO fm_v2_jobs(
                    owner_id,job_id,kind,idempotency_key,state,attempt_count,
                    input_hash,created_at,updated_at
                ) VALUES
                    ('alice','job-alice','memory_file_import','job-a','succeeded',1,'input-a','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z'),
                    ('bob','job-bob','memory_file_import','job-b','succeeded',1,'input-b','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_job_events(
                    owner_id,job_id,sequence,state,change_set_id,created_at
                ) VALUES
                    ('alice','job-alice',1,'succeeded','cs-alice','2026-01-01T00:00:00Z'),
                    ('bob','job-bob',1,'succeeded','cs-bob','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_projects(
                    owner_id,project_id,workspace_id,created_at,updated_at
                ) VALUES
                    ('alice','project-alice','workspace','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z'),
                    ('bob','project-bob','workspace','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_policy_projections(
                    owner_id,project_id,contract_path,contract_hash,engine_version,
                    summary_json,source_revision,state,updated_at
                ) VALUES
                    ('alice','project-alice','contract','hash-a','test','{}',1,'active','2026-01-01T00:00:00Z'),
                    ('bob','project-bob','contract','hash-b','test','{}',1,'active','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_principal_bindings VALUES
                    ('alice','binding-a','private-a'),('bob','binding-b','private-b');
                INSERT INTO fm_v2_tool_catalog VALUES
                    ('alice','tool-a','private-a'),('bob','tool-b','private-b');
                INSERT INTO fm_v2_policy_trust_events VALUES
                    ('alice','event-a','hash-a'),('bob','event-b','hash-b');
                INSERT INTO code_files(
                    codebase,rel_path,blake3,mtime_ns,size,symbol_count,indexed_at
                ) VALUES
                    ('alice' || char(31) || 'repo','src/a.rs','code-a',1,1,1,'2026-01-01T00:00:00Z'),
                    ('bob' || char(31) || 'repo','src/b.rs','code-b',1,1,1,'2026-01-01T00:00:00Z');
                INSERT INTO code_index_runs(
                    owner,workspace_id,codebase,run_id,source_snapshot,status,started_at
                ) VALUES
                    ('alice','workspace','repo','run-a','{}','ready','2026-01-01T00:00:00Z'),
                    ('bob','workspace','repo','run-b','{}','ready','2026-01-01T00:00:00Z');
                INSERT INTO code_index_heads(owner,workspace_id,codebase,run_id,published_at) VALUES
                    ('alice','workspace','repo','run-a','2026-01-01T00:00:00Z'),
                    ('bob','workspace','repo','run-b','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_media_assets VALUES
                    ('alice','asset-a'),('bob','asset-b');
                INSERT INTO fm_v2_media_representations VALUES
                    ('alice','representation-a','asset-a'),
                    ('bob','representation-b','asset-b');
                "#,
            )
            .unwrap();
        }

        let alice_before = store.owner_counts("alice").unwrap();
        let bob_before = store.owner_counts("bob").unwrap();
        assert!(alice_before["count"].as_i64().unwrap() > 10);
        assert_eq!(alice_before["tables"]["fm_v2_principal_bindings"], 1);
        assert_eq!(alice_before["tables"]["fm_v2_tool_catalog"], 1);
        assert_eq!(alice_before["tables"]["fm_v2_policy_trust_events"], 1);
        assert_eq!(alice_before["tables"]["fm_v2_chunks_fts"], 1);
        assert!(alice_before["tables"].get("fm_v2_media_assets").is_none());

        let receipt = store.rename_owner("alice", "alicia").unwrap();
        assert_eq!(receipt["already_applied"], false);
        assert_eq!(store.owner_counts("alice").unwrap()["count"], 0);
        assert_eq!(
            store.owner_counts("alicia").unwrap()["tables"],
            alice_before["tables"]
        );
        assert_eq!(store.owner_counts("bob").unwrap(), bob_before);
        let replay = store.rename_owner("alice", "alicia").unwrap();
        assert_eq!(replay["already_applied"], true);

        {
            let conn = store.conn.lock().unwrap();
            for table in [
                "fm_v2_sources",
                "fm_v2_documents",
                "fm_v2_chunks",
                "fm_v2_chunks_fts",
                "fm_v2_jobs",
                "fm_v2_job_events",
                "fm_v2_policy_projections",
                "fm_v2_principal_bindings",
                "fm_v2_tool_catalog",
                "fm_v2_policy_trust_events",
            ] {
                let count: i64 = conn
                    .query_row(
                        &format!("SELECT count(*) FROM {table} WHERE owner_id='alicia'"),
                        [],
                        |row| row.get(0),
                    )
                    .unwrap();
                assert_eq!(count, 1, "{table} must be renamed");
            }
            assert!(conn
                .execute(
                    "UPDATE fm_v2_job_events SET state='failed' WHERE owner_id='alicia'",
                    []
                )
                .is_err());
            assert_eq!(
                conn.query_row(
                    "SELECT count(*) FROM fm_v2_media_assets WHERE owner_id='alice'",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
                1
            );
        }

        store.purge_owner("alicia").unwrap();
        assert_eq!(store.owner_counts("alicia").unwrap()["count"], 0);
        assert_eq!(store.owner_counts("bob").unwrap(), bob_before);
        let conn = store.conn.lock().unwrap();
        assert_eq!(
            conn.query_row(
                "SELECT count(*) FROM fm_v2_media_assets WHERE owner_id='alice'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
    }

    #[tokio::test]
    async fn owner_rename_failure_rolls_back_every_domain_and_preserves_target() {
        let store = SqliteStore::memory(4).unwrap();
        for owner in ["alice", "bob"] {
            let mut record = test_record(&format!("{owner} rollback sentinel"));
            record.id = format!("rollback-{owner}");
            record.owner = Some(owner.into());
            assert!(store.upsert_curated(&record, None).await);
        }
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "CREATE TABLE zz_owner_failure(
                     owner_id TEXT NOT NULL,item_id TEXT NOT NULL,
                     PRIMARY KEY(owner_id,item_id));
                 INSERT INTO zz_owner_failure VALUES ('alice','sentinel');
                 CREATE TRIGGER zz_injected_owner_rename_failure
                 AFTER UPDATE OF owner_id ON zz_owner_failure
                 BEGIN SELECT RAISE(ABORT,'injected owner rename failure'); END;",
            )
            .unwrap();
        }
        let alice_before = store.owner_counts("alice").unwrap();
        let bob_before = store.owner_counts("bob").unwrap();
        let conflict = store.rename_owner("alice", "bob").unwrap_err();
        assert!(conflict.contains("target owner already has memory"));
        assert_eq!(store.owner_counts("alice").unwrap(), alice_before);
        assert_eq!(store.owner_counts("bob").unwrap(), bob_before);
        let error = store.rename_owner("alice", "alicia").unwrap_err();
        assert!(error.contains("injected owner rename failure"));
        assert_eq!(store.owner_counts("alice").unwrap(), alice_before);
        assert_eq!(store.owner_counts("alicia").unwrap()["count"], 0);
        assert_eq!(store.owner_counts("bob").unwrap(), bob_before);
        let conn = store.conn.lock().unwrap();
        assert_eq!(
            conn.query_row(
                "SELECT count(*) FROM sqlite_master
                 WHERE type='trigger' AND name='zz_injected_owner_rename_failure'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
    }

    #[tokio::test]
    async fn owner_purge_failure_rolls_back_and_retry_finishes_exactly() {
        let store = SqliteStore::memory(4).unwrap();
        for owner in ["alice", "bob"] {
            let mut record = test_record(&format!("{owner} purge rollback sentinel"));
            record.id = format!("purge-rollback-{owner}");
            record.owner = Some(owner.into());
            assert!(store.upsert_curated(&record, None).await);
        }
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "CREATE TABLE zz_owner_purge_failure(
                     owner_id TEXT NOT NULL,item_id TEXT NOT NULL,
                     PRIMARY KEY(owner_id,item_id));
                 INSERT INTO zz_owner_purge_failure VALUES ('alice','sentinel');
                 CREATE TRIGGER zz_injected_owner_purge_failure
                 AFTER DELETE ON zz_owner_purge_failure
                 BEGIN SELECT RAISE(ABORT,'injected owner purge failure'); END;",
            )
            .unwrap();
        }
        let alice_before = store.owner_counts("alice").unwrap();
        let bob_before = store.owner_counts("bob").unwrap();
        let error = store.purge_owner("alice").unwrap_err();
        assert!(error.contains("injected owner purge failure"));
        assert_eq!(store.owner_counts("alice").unwrap(), alice_before);
        assert_eq!(store.owner_counts("bob").unwrap(), bob_before);
        {
            let conn = store.conn.lock().unwrap();
            assert_eq!(
                conn.query_row(
                    "SELECT count(*) FROM sqlite_master
                     WHERE type='trigger' AND name='zz_injected_owner_purge_failure'",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
                1
            );
            conn.execute_batch("DROP TRIGGER zz_injected_owner_purge_failure;")
                .unwrap();
        }
        let receipt = store.purge_owner("alice").unwrap();
        assert_eq!(receipt["purged"], true);
        assert_eq!(store.owner_counts("alice").unwrap()["count"], 0);
        assert_eq!(store.owner_counts("bob").unwrap(), bob_before);
        let replay = store.purge_owner("alice").unwrap();
        assert_eq!(replay["purged"], true);
        assert_eq!(replay["before"]["count"], 0);
    }

    #[tokio::test]
    async fn owner_reset_is_atomic_scoped_and_preserves_rag_policy_state() {
        let store = SqliteStore::memory(4).unwrap();
        for owner in ["alice", "bob"] {
            let mut record = test_record(&format!("{owner} private reset memory"));
            record.id = format!("reset-{owner}");
            record.owner = Some(owner.into());
            record.workspace_id = "workspace".into();
            assert!(store.upsert_curated(&record, None).await);

            let mut raw = RawTurn::new("user", format!("{owner} raw reset input"));
            raw.id = format!("raw-reset-{owner}");
            raw.owner = Some(owner.into());
            raw.workspace_id = "workspace".into();
            assert!(store.upsert_raw(&raw, None).await);
        }
        let policy = RetentionPolicy {
            raw_days: 12,
            candidate_days: 34,
            curated_days: Some(56),
            graph_days: Some(78),
            recovery_seconds: 90,
        };
        store
            .set_retention_policy("alice", "workspace", &policy)
            .unwrap();

        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                r#"
                INSERT INTO fm_v2_change_sets(
                    owner_id,change_set_id,actor_type,actor_id,reason,
                    contract_version,content_hash,created_at
                ) VALUES ('alice','cs-reset','user','alice','test reset',
                    '2.0','cs-hash','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_sources(
                    owner_id,source_id,workspace_key,project_key,source_uri,
                    source_revision,content_hash,source_type,created_at
                ) VALUES ('alice','source-shared','workspace','',
                    'file:///rag-and-memory.txt',1,'source-hash','document',
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_evidence(
                    owner_id,evidence_id,source_id,source_revision,locator_type,
                    locator_json,quote,extraction_contract,parser_version,
                    raw_value_json,normalized_value_json,created_at
                ) VALUES ('alice','evidence-reset','source-shared',1,'text_range',
                    '{}','private memory evidence','test','test','{}','{}',
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_candidates(
                    owner_id,candidate_id,workspace_key,project_key,
                    idempotency_key,created_change_set_id,current_revision,created_at
                ) VALUES ('alice','candidate-reset','workspace','',
                    'candidate-reset-key','cs-reset',0,'2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_candidate_revisions(
                    owner_id,candidate_id,revision,proposal_json,state,confidence,
                    importance,reason,source_id,source_revision,content_hash,
                    actor_type,actor_id,change_set_id,created_at
                ) VALUES ('alice','candidate-reset',1,'{"text":"private candidate"}',
                    'proposed',0.8,0.5,'test','source-shared',1,
                    'candidate-hash','system','reset-test','cs-reset',
                    '2026-01-01T00:00:00Z');
                UPDATE fm_v2_candidates SET current_revision=1
                 WHERE owner_id='alice' AND candidate_id='candidate-reset';
                INSERT INTO fm_v2_candidate_evidence(
                    owner_id,candidate_id,revision,evidence_id
                ) VALUES ('alice','candidate-reset',1,'evidence-reset');
                INSERT INTO fm_v2_documents(
                    owner_id,document_id,source_id,source_revision,title,
                    parser_version,content_hash,created_at
                ) VALUES ('alice','document-keep','source-shared',1,'keep me',
                    'test','document-hash','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_chunks(
                    owner_id,chunk_id,document_id,ordinal,text,content_hash,
                    locator_json,created_at
                ) VALUES ('alice','chunk-keep','document-keep',0,'RAG survives',
                    'chunk-hash','{}','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_derived_generations(
                    owner_id,generation_id,logical_space,workspace_key,project_key,
                    provider_ref,model,endpoint_class,dimension,normalization,metric,
                    chunker_version,config_fingerprint,source_watermark,row_count,
                    state,created_at,updated_at
                ) VALUES ('alice','generation-keep','documents','workspace','',
                    'provider','model','embedding',4,'l2','cosine','test','config',
                    'watermark',1,'ready','2026-01-01T00:00:00Z',
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_entities(
                    owner_id,entity_id,workspace_key,project_key,
                    created_change_set_id,current_revision,created_at
                ) VALUES ('alice','entity-reset','workspace','','cs-reset',0,
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_entity_revisions(
                    owner_id,entity_id,revision,payload,content_hash,actor_type,
                    actor_id,reason,change_set_id,created_at
                ) VALUES ('alice','entity-reset',1,'{}','entity-hash','user',
                    'alice','test','cs-reset','2026-01-01T00:00:00Z');
                UPDATE fm_v2_entities SET current_revision=1
                 WHERE owner_id='alice' AND entity_id='entity-reset';
                INSERT INTO fm_v2_knowledge_blocks(
                    owner_id,block_id,workspace_key,project_key,subject_entity_id,
                    predicate,claim_slot,cardinality,created_change_set_id,
                    current_revision,created_at
                ) VALUES ('alice','block-reset','workspace','','entity-reset',
                    'definition','reset-slot','single','cs-reset',0,
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_knowledge_revisions(
                    owner_id,block_id,revision,value_type,value_json,kind,tags_json,
                    status,confidence,trust,activation,activation_rationale,
                    evidence_json,content_hash,actor_type,actor_id,reason,
                    change_set_id,created_at
                ) VALUES ('alice','block-reset',1,'string','"private memory"',
                    'fact','[]','active',0.8,0.9,'active','test',
                    '["evidence-reset"]','knowledge-hash','user','alice','test',
                    'cs-reset','2026-01-01T00:00:00Z');
                UPDATE fm_v2_knowledge_blocks SET current_revision=1
                 WHERE owner_id='alice' AND block_id='block-reset';
                INSERT INTO fm_v2_revision_evidence(
                    owner_id,block_id,revision,evidence_id
                ) VALUES ('alice','block-reset',1,'evidence-reset');
                INSERT INTO fm_v2_trust_assignments(
                    owner_id,assignment_id,subject_kind,subject_id,subject_revision,
                    workspace_key,project_key,assignment_revision,state,trust,
                    actor_type,actor_id,reason_code,content_hash,created_at
                ) VALUES ('alice','trust-reset','knowledge_revision','block-reset',1,
                    'workspace','',1,'assigned',0.9,'owner','alice','owner_review',
                    'trust-hash','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_outbox(
                    owner_id,event_id,change_set_id,kind,payload_json,
                    source_revision,created_at
                ) VALUES ('alice','outbox-reset','cs-reset','knowledge_revision',
                    '{"block_id":"block-reset","revision":1}',1,
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_policy_projections(
                    owner_id,project_id,contract_path,contract_hash,engine_version,
                    summary_json,source_revision,state,updated_at
                ) VALUES ('alice','project-keep','.clankers/hexes/contract.yaml',
                    'contract-hash','test','{}',1,'active',
                    '2026-01-01T00:00:00Z');
                INSERT INTO code_files(
                    codebase,rel_path,blake3,mtime_ns,size,symbol_count,indexed_at
                ) VALUES
                    ('alice' || char(31) || 'repo-reset','src/main.rs','alice-code',1,10,1,'2026-01-01T00:00:00Z'),
                    ('bob' || char(31) || 'repo-keep','src/main.rs','bob-code',1,10,1,'2026-01-01T00:00:00Z');
                INSERT INTO code_index_runs(
                    owner,workspace_id,codebase,run_id,source_snapshot,status,started_at,finished_at
                ) VALUES
                    ('alice','workspace','repo-reset','run-reset','{}','ready','2026-01-01T00:00:00Z','2026-01-01T00:00:01Z'),
                    ('bob','workspace','repo-keep','run-keep','{}','ready','2026-01-01T00:00:00Z','2026-01-01T00:00:01Z');
                INSERT INTO code_index_heads(
                    owner,workspace_id,codebase,run_id,published_at
                ) VALUES
                    ('alice','workspace','repo-reset','run-reset','2026-01-01T00:00:01Z'),
                    ('bob','workspace','repo-keep','run-keep','2026-01-01T00:00:01Z');
                "#,
            )
            .unwrap();
        }

        let requested = vec!["memories".to_string()];
        let preview = store
            .reset_owner("reset_preview", "alice", &requested, None)
            .unwrap();
        assert_eq!(
            preview["expanded_components"],
            serde_json::json!(["memories", "graph", "ingest"])
        );
        assert!(preview["components"]["memories"]["count"].as_u64().unwrap() > 1);
        let expected = preview["components"].clone();
        let committed = store
            .reset_owner("reset_commit", "alice", &requested, Some(&expected))
            .unwrap();
        assert_eq!(committed["complete"], true);
        assert_eq!(committed["categories"]["memories"]["state"], "complete");

        let conn = store.conn.lock().unwrap();
        for table in [
            "curated",
            "raw",
            "graph_nodes",
            "fm_v2_entities",
            "fm_v2_entity_revisions",
            "fm_v2_knowledge_blocks",
            "fm_v2_knowledge_revisions",
            "fm_v2_revision_evidence",
            "fm_v2_candidates",
            "fm_v2_candidate_revisions",
            "fm_v2_candidate_evidence",
            "fm_v2_trust_assignments",
            "fm_v2_evidence",
            "fm_v2_change_sets",
        ] {
            let owner_column = if matches!(table, "curated" | "raw" | "graph_nodes") {
                "owner"
            } else {
                "owner_id"
            };
            let remaining: i64 = conn
                .query_row(
                    &format!("SELECT count(*) FROM {table} WHERE {owner_column}='alice'"),
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(remaining, 0, "{table} must be reset");
        }
        for table in [
            "fm_v2_sources",
            "fm_v2_documents",
            "fm_v2_chunks",
            "fm_v2_derived_generations",
            "fm_v2_policy_projections",
        ] {
            let remaining: i64 = conn
                .query_row(
                    &format!("SELECT count(*) FROM {table} WHERE owner_id='alice'"),
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(remaining, 1, "{table} must be preserved");
        }
        let bob_curated: i64 = conn
            .query_row(
                "SELECT count(*) FROM curated WHERE owner='bob'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let bob_raw: i64 = conn
            .query_row("SELECT count(*) FROM raw WHERE owner='bob'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!((bob_curated, bob_raw), (1, 1));
        let alice_code: i64 = conn
            .query_row(
                "SELECT count(*) FROM code_files WHERE codebase LIKE ('alice' || char(31) || '%')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let bob_code: i64 = conn
            .query_row(
                "SELECT count(*) FROM code_files WHERE codebase LIKE ('bob' || char(31) || '%')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let alice_code_heads: i64 = conn
            .query_row(
                "SELECT count(*) FROM code_index_heads WHERE owner='alice'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let bob_code_heads: i64 = conn
            .query_row(
                "SELECT count(*) FROM code_index_heads WHERE owner='bob'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!((alice_code, alice_code_heads), (0, 0));
        assert_eq!((bob_code, bob_code_heads), (1, 1));
        let retained_policy: i64 = conn
            .query_row(
                "SELECT count(*) FROM memory_retention_policy WHERE owner='alice' AND workspace_id='workspace' AND raw_days=12",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retained_policy, 1);
        drop(conn);

        // Retrying an already-complete commit is an idempotent success even
        // though its original non-empty preview no longer matches.
        let retry = store
            .reset_owner("reset_commit", "alice", &requested, Some(&expected))
            .unwrap();
        assert_eq!(retry["complete"], true);
        assert_eq!(retry["categories"]["memories"]["count"], 0);
    }

    #[tokio::test]
    async fn owner_purge_erases_v2_residue_and_restores_append_only_guards() {
        let store = SqliteStore::memory(4).unwrap();
        for owner in ["purge-me", "bob"] {
            let mut record = test_record(&format!("{owner} purge memory"));
            record.id = format!("purge-curated-{owner}");
            record.owner = Some(owner.into());
            record.workspace_id = "workspace".into();
            assert!(store.upsert_curated(&record, None).await);
        }
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                r#"
                INSERT INTO fm_v2_change_sets(
                    owner_id,change_set_id,actor_type,actor_id,reason,
                    contract_version,content_hash,created_at
                ) VALUES ('purge-me','cs-purge','user','purge-me','purge test',
                    '2.0','cs-hash','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_sources(
                    owner_id,source_id,workspace_key,project_key,source_uri,
                    source_revision,content_hash,source_type,created_at
                ) VALUES ('purge-me','source-purge','workspace','',
                    'file:///purge.txt',1,'source-hash','document',
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_entities(
                    owner_id,entity_id,workspace_key,project_key,
                    created_change_set_id,current_revision,created_at
                ) VALUES ('purge-me','entity-purge','workspace','','cs-purge',0,
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_entity_revisions(
                    owner_id,entity_id,revision,payload,content_hash,actor_type,
                    actor_id,reason,change_set_id,created_at
                ) VALUES ('purge-me','entity-purge',1,'{}','entity-hash','user',
                    'purge-me','test','cs-purge','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_knowledge_blocks(
                    owner_id,block_id,workspace_key,project_key,subject_entity_id,
                    predicate,claim_slot,cardinality,created_change_set_id,
                    current_revision,created_at
                ) VALUES ('purge-me','block-purge','workspace','','entity-purge',
                    'definition','purge-slot','single','cs-purge',0,
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_knowledge_revisions(
                    owner_id,block_id,revision,value_type,value_json,kind,tags_json,
                    status,confidence,trust,activation,activation_rationale,
                    evidence_json,content_hash,actor_type,actor_id,reason,
                    change_set_id,created_at
                ) VALUES ('purge-me','block-purge',1,'string','"purge me"',
                    'fact','[]','active',0.8,0.9,'active','test',
                    '[]','knowledge-hash','user','purge-me','test',
                    'cs-purge','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_candidates(
                    owner_id,candidate_id,workspace_key,project_key,
                    idempotency_key,created_change_set_id,current_revision,created_at
                ) VALUES ('purge-me','candidate-purge','workspace','',
                    'candidate-purge-key','cs-purge',0,'2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_candidate_revisions(
                    owner_id,candidate_id,revision,proposal_json,state,confidence,
                    importance,reason,source_id,source_revision,content_hash,
                    actor_type,actor_id,change_set_id,created_at
                ) VALUES ('purge-me','candidate-purge',1,'{"text":"purge candidate"}',
                    'proposed',0.8,0.5,'test','source-purge',1,
                    'candidate-hash','system','purge-test','cs-purge',
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_jobs(
                    owner_id,job_id,kind,workspace_key,project_key,
                    idempotency_key,state,attempt_count,input_hash,
                    created_at,updated_at
                ) VALUES ('purge-me','job-purge','conversation_capture','workspace',
                    '','job-purge-key','succeeded',0,'input-hash',
                    '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_attempts(
                    owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,
                    created_at,updated_at
                ) VALUES ('purge-me','attempt-purge','job-purge',1,'succeeded',1,
                    '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_job_events(
                    owner_id,job_id,sequence,state,change_set_id,created_at
                ) VALUES ('purge-me','job-purge',1,'succeeded','cs-purge',
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_outbox(
                    owner_id,event_id,change_set_id,kind,payload_json,
                    source_revision,created_at
                ) VALUES ('purge-me','outbox-purge','cs-purge','knowledge_revision',
                    '{}',1,'2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_privacy_erasure_tombstones(
                    owner_id,erasure_id,source_id,authorized_by,reason,
                    erased_counts_json,created_at
                ) VALUES ('purge-me','erase-old',NULL,'purge-me','old erasure',
                    '{}','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_migration_runs(
                    migration_run_id,source_database_id,source_schema_version,
                    source_high_water_mark,conversion_version,manifest_hash,
                    created_at,updated_at
                ) VALUES ('run-purge','db',1,'hw','v1','mh',
                    '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_migration_plan(
                    migration_run_id,source_table,source_key,owner_id,
                    source_fingerprint,planned_target_ids,conversion_rule,
                    classification,activation_treatment,created_at
                ) VALUES ('run-purge','curated','key-1','purge-me','fp','[]',
                    'rule','class','treat','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_migration_events(
                    migration_run_id,event_id,plan_id,sequence,phase,attempt,
                    checkpoint,target_ids,apply_result,validation_result,actor,
                    event_hash,created_at
                ) VALUES ('run-purge','event-purge',1,1,'apply',1,'cp','[]','{}',
                    '{}','actor','hash','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_legacy_outbox(
                    event_id,source_table,source_key,operation,owner_id,
                    source_revision,idempotency_key,payload_version,created_at
                ) VALUES ('legacy-purge','curated','key-1','insert','purge-me','1',
                    'legacy-purge-key','1','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_migration_outbox_events(
                    migration_run_id,outbox_sequence,disposition,event_hash,created_at
                ) VALUES ('run-purge',1,'applied','hash','2026-01-01T00:00:00Z');
                INSERT INTO memory_leases(
                    kind,owner,workspace_id,subject,holder,acquired_at,expires_at,
                    attempt
                ) VALUES ('ingest','purge-me','workspace','subject','holder',
                    '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',1);
                INSERT INTO memory_tombstones(
                    id,owner,workspace_id,source_key,affected_ids,created_at
                ) VALUES ('tomb-purge','purge-me','workspace','source','[]',
                    '2026-01-01T00:00:00Z');
                INSERT INTO memory_retention_policy(
                    owner,workspace_id,raw_days,candidate_days,updated_at
                ) VALUES ('purge-me','workspace',1,1,'2026-01-01T00:00:00Z');
                "#,
            )
            .unwrap();
            // The append-only job-event guard is live before the purge.
            assert!(conn
                .execute(
                    "DELETE FROM fm_v2_job_events WHERE owner_id='purge-me'",
                    []
                )
                .is_err());
        }

        store.purge_owner("purge-me").unwrap();

        let conn = store.conn.lock().unwrap();
        for table in [
            "fm_v2_entities",
            "fm_v2_entity_revisions",
            "fm_v2_knowledge_blocks",
            "fm_v2_knowledge_revisions",
            "fm_v2_candidates",
            "fm_v2_candidate_revisions",
            "fm_v2_jobs",
            "fm_v2_attempts",
            "fm_v2_job_events",
            "fm_v2_outbox",
            "fm_v2_sources",
            "fm_v2_migration_plan",
            "fm_v2_legacy_outbox",
            "fm_v2_privacy_erasure_context",
            "fm_v2_privacy_erasure_tombstones",
            "fm_v2_change_sets",
        ] {
            let remaining: i64 = conn
                .query_row(
                    &format!("SELECT count(*) FROM {table} WHERE owner_id='purge-me'"),
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(remaining, 0, "{table} must be purged");
        }
        // Migration events carry no owner column; they are reached through
        // the owner's plan/legacy-outbox rows, so nothing may remain at all.
        for table in ["fm_v2_migration_events", "fm_v2_migration_outbox_events"] {
            let remaining: i64 = conn
                .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(remaining, 0, "{table} must be purged");
        }
        for table in [
            "curated",
            "memory_leases",
            "memory_tombstones",
            "memory_retention_policy",
        ] {
            let remaining: i64 = conn
                .query_row(
                    &format!("SELECT count(*) FROM {table} WHERE owner='purge-me'"),
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(remaining, 0, "{table} must be purged");
        }
        // The append-only guard is restored after the purge.
        let guard: i64 = conn
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='fm_v2_job_event_no_delete'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(guard, 1);
        // Migration runs are shared across owners and stay; bob is untouched.
        let runs: i64 = conn
            .query_row("SELECT count(*) FROM fm_v2_migration_runs", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(runs, 1);
        let bob_curated: i64 = conn
            .query_row("SELECT count(*) FROM curated WHERE owner='bob'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(bob_curated, 1);
    }

    #[tokio::test]
    async fn owner_reset_rejects_stale_preview_without_mutation() {
        let store = SqliteStore::memory(4).unwrap();
        let mut first = test_record("first reset candidate");
        first.id = "reset-first".into();
        assert!(store.upsert_curated(&first, None).await);
        let requested = vec!["memories".to_string()];
        let preview = store
            .reset_owner("reset_preview", "alice", &requested, None)
            .unwrap();

        let mut second = test_record("second reset candidate");
        second.id = "reset-second".into();
        assert!(store.upsert_curated(&second, None).await);
        let error = store
            .reset_owner(
                "reset_commit",
                "alice",
                &requested,
                Some(&preview["components"]),
            )
            .unwrap_err();
        assert!(error.contains("stale"));
        assert_eq!(store.owner_counts("alice").unwrap()["curated"], 2);
    }

    #[tokio::test]
    async fn owner_reset_independent_categories_preserve_unselected_state() {
        let store = SqliteStore::memory(4).unwrap();
        let mut curated = test_record("retained curated memory");
        curated.id = "reset-independent-curated".into();
        assert!(store.upsert_curated(&curated, None).await);
        let mut raw = RawTurn::new("user", "reset independent raw");
        raw.id = "reset-independent-raw".into();
        raw.owner = Some("alice".into());
        raw.workspace_id = "workspace".into();
        assert!(store.upsert_raw(&raw, None).await);

        let graph_preview = store
            .reset_owner("reset_preview", "alice", &["graph".into()], None)
            .unwrap();
        assert!(graph_preview["implications"]
            .as_array()
            .unwrap()
            .iter()
            .any(|value| value.as_str().unwrap_or_default().contains("may rebuild")));
        store
            .reset_owner(
                "reset_commit",
                "alice",
                &["graph".into()],
                Some(&graph_preview["components"]),
            )
            .unwrap();
        let after_graph = store.owner_counts("alice").unwrap();
        assert_eq!(after_graph["curated"], 1);
        assert_eq!(after_graph["raw"], 1);
        assert_eq!(after_graph["graph_nodes"], 0);

        let ingest_preview = store
            .reset_owner("reset_preview", "alice", &["ingest".into()], None)
            .unwrap();
        store
            .reset_owner(
                "reset_commit",
                "alice",
                &["ingest".into()],
                Some(&ingest_preview["components"]),
            )
            .unwrap();
        let after_ingest = store.owner_counts("alice").unwrap();
        assert_eq!(after_ingest["curated"], 1);
        assert_eq!(after_ingest["raw"], 0);
    }

    #[test]
    fn owner_reset_fails_closed_on_immutable_memory_job_events() {
        let store = SqliteStore::memory(4).unwrap();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                r#"
                INSERT INTO fm_v2_change_sets(
                    owner_id,change_set_id,actor_type,actor_id,reason,
                    contract_version,content_hash,created_at
                ) VALUES ('alice','cs-job-reset','user','alice','job reset test',
                    '2.0','job-cs-hash','2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_jobs(
                    owner_id,job_id,kind,idempotency_key,state,attempt_count,
                    input_hash,created_at,updated_at
                ) VALUES ('alice','job-reset','memory_file_import','import-key',
                    'queued',0,'input-hash','2026-01-01T00:00:00Z',
                    '2026-01-01T00:00:00Z');
                INSERT INTO fm_v2_job_events(
                    owner_id,job_id,sequence,state,change_set_id,created_at
                ) VALUES ('alice','job-reset',1,'queued','cs-job-reset',
                    '2026-01-01T00:00:00Z');
                "#,
            )
            .unwrap();
        }
        let error = store
            .reset_owner("reset_preview", "alice", &["ingest".to_string()], None)
            .unwrap_err();
        assert!(error.contains("immutable memory-ingest job event"));
        let jobs: i64 = store
            .conn
            .lock()
            .unwrap()
            .query_row(
                "SELECT count(*) FROM fm_v2_jobs WHERE owner_id='alice'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(jobs, 1);
    }

    #[test]
    fn owner_reset_clears_memory_import_queue_jobs() {
        let store = SqliteStore::memory(4).unwrap();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                r#"
                INSERT INTO fm_v2_jobs(
                    owner_id,job_id,kind,idempotency_key,state,attempt_count,
                    input_hash,created_at,updated_at
                ) VALUES
                    ('alice','batch-1','memory_import_batch','batch-key',
                     'active',0,'batch-hash','2026-01-01T00:00:00Z',
                     '2026-01-01T00:00:00Z'),
                    ('alice','item-1','memory_import_item','item-key',
                     'queued',0,'item-hash','2026-01-01T00:00:00Z',
                     '2026-01-01T00:00:00Z');
                "#,
            )
            .unwrap();
        }
        let preview = store
            .reset_owner("reset_preview", "alice", &["ingest".into()], None)
            .unwrap();
        assert!(
            preview["components"]["ingest"]["count"]
                .as_i64()
                .unwrap()
                >= 2
        );
        store
            .reset_owner(
                "reset_commit",
                "alice",
                &["ingest".into()],
                Some(&preview["components"]),
            )
            .unwrap();
        let remaining: i64 = store
            .conn
            .lock()
            .unwrap()
            .query_row(
                "SELECT count(*) FROM fm_v2_jobs WHERE owner_id='alice'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(remaining, 0);
    }

    #[tokio::test]
    async fn provenance_forget_is_scoped_atomic_and_recoverable() {
        let store = SqliteStore::memory(4).unwrap();
        store
            .set_retention_policy(
                "alice",
                "ws-a",
                &RetentionPolicy {
                    recovery_seconds: 60,
                    ..RetentionPolicy::default()
                },
            )
            .unwrap();
        let mut raw = RawTurn::new("user", "remember the amber launch code");
        raw.id = "raw-forget".into();
        raw.owner = Some("alice".into());
        raw.workspace_id = "ws-a".into();
        raw.metadata = serde_json::json!({
            "source_uri": "message://alice/session/message-1",
            "source_message_id": "message-1",
        });
        assert!(store.upsert_raw(&raw, None).await);

        let mut candidate = test_candidate("candidate-forget", "amber launch code");
        candidate.raw_evidence_ids = vec![raw.id.clone()];
        candidate.source_uri = "message://alice/session/message-1".into();
        candidate.source_message_ids = vec!["message-1".into()];
        candidate.accepted_curated_id = Some("curated-forget".into());
        candidate.status = CandidateStatus::Accepted;
        assert!(store.insert_candidate(&candidate).unwrap());

        let mut curated = test_record("amber launch code");
        curated.id = "curated-forget".into();
        curated.workspace_id = "ws-a".into();
        curated.source_message_ids = vec!["message-1".into()];
        curated.metadata = serde_json::json!({
            "candidate_id": candidate.id,
            "raw_evidence_ids": [raw.id],
            "source_uri": "message://alice/session/message-1",
            "source_revision": 1,
            "content_hash": crate::privacy::content_hash("amber launch code"),
        });
        assert!(store.upsert_curated(&curated, None).await);
        let mut bob = curated.clone();
        bob.id = "curated-forget-bob".into();
        bob.owner = Some("bob".into());
        assert!(store.upsert_curated(&bob, None).await);

        let selector = ForgetSelector::SourceMessageId("message-1".into());
        let scope = crate::graph::GraphScope::new("alice", "ws-a").unwrap();
        let graph_memory_id = scope.node_id("memory", "curated-forget");
        let preview = store
            .preview_forget("alice", "ws-a", selector.clone())
            .unwrap();
        assert_eq!(preview.closure.raw_ids, vec!["raw-forget"]);
        assert_eq!(preview.closure.candidate_ids, vec!["candidate-forget"]);
        assert_eq!(preview.closure.curated_ids, vec!["curated-forget"]);
        assert_eq!(preview.closure.graph_node_ids.len(), 1);

        store
            .conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER injected_forget_failure
                 BEFORE DELETE ON candidates BEGIN
                   SELECT RAISE(ABORT, 'injected forget failure');
                 END;",
            )
            .unwrap();
        assert!(store
            .commit_forget("alice", "ws-a", selector.clone(), &preview.token)
            .unwrap_err()
            .contains("injected forget failure"));
        assert!(store
            .get_curated_record("curated-forget", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(store
            .candidate_by_id("candidate-forget", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(!store
            .search_raw_fts_scoped("amber", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert!(store
            .graph_fetch(&scope, &graph_memory_id)
            .unwrap()
            .is_some());
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch("DROP TRIGGER injected_forget_failure;")
            .unwrap();

        let committed = store
            .commit_forget("alice", "ws-a", selector, &preview.token)
            .unwrap();
        assert_eq!(committed.closure, preview.closure);
        assert!(store
            .get_curated_record("curated-forget", "alice", "ws-a")
            .unwrap()
            .is_none());
        assert!(store
            .candidate_by_id("candidate-forget", "alice", "ws-a")
            .unwrap()
            .is_none());
        assert!(store
            .search_raw_fts_scoped("amber", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert!(store
            .search_curated_fts_scoped("amber", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert!(store
            .graph_fetch(&scope, &graph_memory_id)
            .unwrap()
            .is_none());
        assert!(store
            .get_curated_record("curated-forget-bob", "bob", "ws-a")
            .unwrap()
            .is_some());
        let restored = store
            .restore_forget("alice", "ws-a", &committed.tombstone_id)
            .unwrap();
        assert_eq!(restored, preview.closure);
        assert!(store
            .get_curated_record("curated-forget", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(store
            .candidate_by_id("candidate-forget", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(!store
            .search_raw_fts_scoped("amber", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert!(!store
            .search_curated_fts_scoped("amber", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert!(store
            .graph_fetch(&scope, &graph_memory_id)
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn forget_operation_retries_are_idempotent_scoped_and_observable() {
        let store = SqliteStore::memory(4).unwrap();
        for owner in ["alice", "bob"] {
            store
                .set_retention_policy(
                    owner,
                    "ws-a",
                    &RetentionPolicy {
                        recovery_seconds: 60,
                        ..RetentionPolicy::default()
                    },
                )
                .unwrap();
        }
        let mut alice = test_record("alice forget operation");
        alice.id = "alice-operation-memory".into();
        alice.workspace_id = "ws-a".into();
        assert!(store.upsert_curated(&alice, None).await);
        let mut bob = test_record("bob forget operation");
        bob.id = "bob-operation-memory".into();
        bob.owner = Some("bob".into());
        bob.workspace_id = "ws-a".into();
        assert!(store.upsert_curated(&bob, None).await);

        let operation_id = "0123456789abcdef0123456789abcdef";
        let absent = store
            .forget_operation_status("alice", "ws-a", operation_id)
            .unwrap();
        assert_eq!(absent.state, ForgetOperationState::Absent);
        assert!(absent.tombstone_id.is_none());
        assert_eq!(
            store
                .forget_operation_status("bob", "ws-a", operation_id)
                .unwrap()
                .state,
            ForgetOperationState::Absent
        );

        let alice_selector = ForgetSelector::RecordId(alice.id.clone());
        let alice_preview = store
            .preview_forget("alice", "ws-a", alice_selector.clone())
            .unwrap();
        let first = store
            .commit_forget_with_operation(
                "alice",
                "ws-a",
                alice_selector.clone(),
                &alice_preview.token,
                operation_id,
            )
            .unwrap();
        let retried = store
            .commit_forget_with_operation(
                "alice",
                "ws-a",
                alice_selector,
                &alice_preview.token,
                operation_id,
            )
            .unwrap();
        assert_eq!(retried, first);
        assert!(store
            .commit_forget_with_operation(
                "alice",
                "ws-a",
                ForgetSelector::RecordId(alice.id.clone()),
                "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                operation_id,
            )
            .unwrap_err()
            .contains("different forget request"));
        let committed = store
            .forget_operation_status("alice", "ws-a", operation_id)
            .unwrap();
        assert_eq!(committed.state, ForgetOperationState::Committed);
        assert_eq!(
            committed.tombstone_id.as_deref(),
            Some(first.tombstone_id.as_str())
        );
        assert_eq!(committed.closure.as_ref(), Some(&first.closure));

        let mut mismatch = test_record("different forget operation");
        mismatch.id = "alice-operation-mismatch".into();
        mismatch.workspace_id = "ws-a".into();
        assert!(store.upsert_curated(&mismatch, None).await);
        let mismatch_selector = ForgetSelector::RecordId(mismatch.id.clone());
        let mismatch_preview = store
            .preview_forget("alice", "ws-a", mismatch_selector.clone())
            .unwrap();
        assert!(store
            .commit_forget_with_operation(
                "alice",
                "ws-a",
                mismatch_selector,
                &mismatch_preview.token,
                operation_id,
            )
            .unwrap_err()
            .contains("different forget request"));
        assert!(store
            .get_curated_record(&mismatch.id, "alice", "ws-a")
            .unwrap()
            .is_some());

        let bob_selector = ForgetSelector::RecordId(bob.id.clone());
        let bob_preview = store
            .preview_forget("bob", "ws-a", bob_selector.clone())
            .unwrap();
        let bob_commit = store
            .commit_forget_with_operation(
                "bob",
                "ws-a",
                bob_selector,
                &bob_preview.token,
                operation_id,
            )
            .unwrap();
        assert_ne!(bob_commit.tombstone_id, first.tombstone_id);
        assert_eq!(
            store
                .forget_operation_status("bob", "ws-a", operation_id)
                .unwrap()
                .state,
            ForgetOperationState::Committed
        );

        let restored = store
            .restore_forget("alice", "ws-a", &first.tombstone_id)
            .unwrap();
        assert_eq!(restored, first.closure);
        assert_eq!(
            store
                .restore_forget("alice", "ws-a", &first.tombstone_id)
                .unwrap(),
            first.closure
        );
        let status = store
            .forget_operation_status("alice", "ws-a", operation_id)
            .unwrap();
        assert_eq!(status.state, ForgetOperationState::Restored);
        assert!(status.recovered_at.is_some());
        assert!(store
            .forget_operation_status("alice", "ws-a", "not-an-operation-id")
            .unwrap_err()
            .contains("32 hexadecimal"));
    }

    #[test]
    fn memory_leases_are_tenant_scoped_and_expiry_is_reclaimable() {
        use chrono::TimeZone;

        let store = SqliteStore::memory(4).unwrap();
        let now = chrono::Utc.with_ymd_and_hms(2026, 7, 28, 12, 0, 0).unwrap();
        let lease = store
            .acquire_memory_lease_at(
                "extraction",
                "alice",
                "ws-a",
                "event-1",
                "worker-a",
                Duration::from_secs(30),
                now,
            )
            .unwrap()
            .expect("first holder acquires");
        assert!(store
            .acquire_memory_lease_at(
                "extraction",
                "alice",
                "ws-a",
                "event-1",
                "worker-b",
                Duration::from_secs(30),
                now,
            )
            .unwrap()
            .is_none());
        std::mem::forget(lease);
        let reclaimed = store
            .acquire_memory_lease_at(
                "extraction",
                "alice",
                "ws-a",
                "event-1",
                "worker-b",
                Duration::from_secs(30),
                now + chrono::Duration::seconds(31),
            )
            .unwrap();
        assert!(reclaimed.is_some());
        assert!(store
            .acquire_memory_lease_at(
                "extraction",
                "bob",
                "ws-a",
                "event-1",
                "worker-c",
                Duration::from_secs(30),
                now,
            )
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn retention_preview_is_scoped_non_mutating_and_guards_expiry() {
        use chrono::TimeZone;

        let store = SqliteStore::memory(4).unwrap();
        let now = chrono::Utc.with_ymd_and_hms(2026, 7, 28, 12, 0, 0).unwrap();
        assert!(store
            .set_retention_policy(
                "alice",
                "ws-a",
                &RetentionPolicy {
                    raw_days: MAX_RAW_RETENTION_DAYS + 1,
                    ..RetentionPolicy::default()
                },
            )
            .unwrap_err()
            .contains("safe maximum"));
        store
            .set_retention_policy(
                "alice",
                "ws-a",
                &RetentionPolicy {
                    raw_days: 30,
                    candidate_days: 90,
                    curated_days: Some(90),
                    graph_days: None,
                    recovery_seconds: 0,
                },
            )
            .unwrap();
        let old = (now - chrono::Duration::days(120)).to_rfc3339();
        let mut raw = RawTurn::new("user", "old raw");
        raw.id = "retention-raw".into();
        raw.owner = Some("alice".into());
        raw.workspace_id = "ws-a".into();
        raw.recorded_at = old.clone();
        assert!(store.upsert_raw(&raw, None).await);
        let mut candidate = test_candidate("retention-candidate", "old candidate");
        candidate.created_at = old.clone();
        candidate.updated_at = old.clone();
        assert!(store.insert_candidate(&candidate).unwrap());
        let mut curated = test_record("curated stays");
        curated.id = "retention-curated".into();
        curated.workspace_id = "ws-a".into();
        curated.created_at = old.clone();
        curated.updated_at = old.clone();
        assert!(store.upsert_curated(&curated, None).await);

        let current = now.to_rfc3339();
        let mut young_raw = RawTurn::new("user", "young raw stays");
        young_raw.id = "retention-young-raw".into();
        young_raw.owner = Some("alice".into());
        young_raw.workspace_id = "ws-a".into();
        young_raw.recorded_at = current.clone();
        assert!(store.upsert_raw(&young_raw, None).await);
        let mut young_candidate =
            test_candidate("retention-young-candidate", "young candidate stays");
        young_candidate.created_at = current.clone();
        young_candidate.updated_at = current.clone();
        assert!(store.insert_candidate(&young_candidate).unwrap());
        let mut young_curated = test_record("young curated stays");
        young_curated.id = "retention-young-curated".into();
        young_curated.workspace_id = "ws-a".into();
        young_curated.created_at = current.clone();
        young_curated.updated_at = current;
        assert!(store.upsert_curated(&young_curated, None).await);
        let mut exempt_curated = test_record("old exempt curated stays");
        exempt_curated.id = "retention-exempt-curated".into();
        exempt_curated.workspace_id = "ws-a".into();
        exempt_curated.created_at = old.clone();
        exempt_curated.updated_at = old.clone();
        exempt_curated.exempt_from_decay = true;
        assert!(store.upsert_curated(&exempt_curated, None).await);

        let mut bob = curated.clone();
        bob.id = "retention-curated-bob".into();
        bob.owner = Some("bob".into());
        assert!(store.upsert_curated(&bob, None).await);
        let mut other_workspace = curated.clone();
        other_workspace.id = "retention-curated-other-workspace".into();
        other_workspace.workspace_id = "ws-b".into();
        assert!(store.upsert_curated(&other_workspace, None).await);

        let changes_before_preview = store.conn.lock().unwrap().total_changes();
        let preview = store
            .preview_expire_retention_at("alice", "ws-a", now)
            .unwrap();
        assert_eq!(
            store.conn.lock().unwrap().total_changes(),
            changes_before_preview
        );
        assert_eq!(preview.closure.raw_ids, vec!["retention-raw"]);
        assert_eq!(preview.closure.candidate_ids, vec!["retention-candidate"]);
        assert_eq!(preview.closure.curated_ids, vec!["retention-curated"]);
        assert_ne!(
            preview.token,
            SqliteStore::retention_expiry_token("bob", "ws-a", &preview.closure).unwrap()
        );
        assert!(store
            .candidate_by_id("retention-candidate", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(store
            .get_curated_record("retention-curated", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert_eq!(
            store
                .conn
                .lock()
                .unwrap()
                .query_row(
                    "SELECT count(*) FROM raw WHERE id='retention-raw'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );

        let mut late_raw = RawTurn::new("user", "old raw inserted after preview");
        late_raw.id = "retention-raw-late".into();
        late_raw.owner = Some("alice".into());
        late_raw.workspace_id = "ws-a".into();
        late_raw.recorded_at = old;
        assert!(store.upsert_raw(&late_raw, None).await);
        assert!(store
            .expire_retention_with_preview_at("alice", "ws-a", Some(&preview.token), None, now)
            .unwrap_err()
            .contains("preview is stale"));
        assert!(store
            .get_curated_record("retention-curated", "alice", "ws-a")
            .unwrap()
            .is_some());

        let fresh = store
            .preview_expire_retention_at("alice", "ws-a", now)
            .unwrap();
        let expired = store
            .expire_retention_with_preview_at("alice", "ws-a", Some(&fresh.token), None, now)
            .unwrap();
        assert_eq!(expired, fresh.closure);
        assert_eq!(expired.raw_ids, vec!["retention-raw", "retention-raw-late"]);
        assert_eq!(expired.curated_ids, vec!["retention-curated"]);
        assert_eq!(
            store
                .conn
                .lock()
                .unwrap()
                .query_row(
                    "SELECT count(*) FROM raw
                     WHERE id IN ('retention-raw','retention-raw-late')",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
            0
        );
        assert!(store
            .candidate_by_id("retention-candidate", "alice", "ws-a")
            .unwrap()
            .is_none());
        assert!(store
            .get_curated_record("retention-curated", "alice", "ws-a")
            .unwrap()
            .is_none());
        assert!(!store
            .search_raw_fts_scoped("young raw", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert!(store
            .candidate_by_id("retention-young-candidate", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(store
            .get_curated_record("retention-young-curated", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(store
            .get_curated_record("retention-exempt-curated", "alice", "ws-a")
            .unwrap()
            .is_some());
        assert!(store
            .get_curated_record("retention-curated-bob", "bob", "ws-a")
            .unwrap()
            .is_some());
        assert!(store
            .get_curated_record("retention-curated-other-workspace", "alice", "ws-b")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn retention_expiry_operations_are_atomic_idempotent_and_scoped() {
        let store = SqliteStore::memory(4).unwrap();
        let policy = RetentionPolicy {
            raw_days: 30,
            candidate_days: 90,
            curated_days: Some(90),
            graph_days: None,
            recovery_seconds: 0,
        };
        for (owner, workspace_id) in [("alice", "ws-a"), ("alice", "ws-b"), ("bob", "ws-a")] {
            store
                .set_retention_policy(owner, workspace_id, &policy)
                .unwrap();
        }
        let old = "2020-01-01T00:00:00+00:00";
        let mut raw = RawTurn::new("user", "retention operation raw");
        raw.id = "retention-operation-raw".into();
        raw.owner = Some("alice".into());
        raw.workspace_id = "ws-a".into();
        raw.recorded_at = old.into();
        assert!(store.upsert_raw(&raw, None).await);

        let mut alice = test_record("retention operation curated");
        alice.id = "retention-operation-curated".into();
        alice.workspace_id = "ws-a".into();
        alice.created_at = old.into();
        alice.updated_at = old.into();
        assert!(store.upsert_curated(&alice, None).await);
        let mut bob = alice.clone();
        bob.id = "retention-operation-curated-bob".into();
        bob.owner = Some("bob".into());
        assert!(store.upsert_curated(&bob, None).await);
        let mut other_workspace = alice.clone();
        other_workspace.id = "retention-operation-curated-ws-b".into();
        other_workspace.workspace_id = "ws-b".into();
        assert!(store.upsert_curated(&other_workspace, None).await);

        let operation_id = "fedcba9876543210fedcba9876543210";
        for (owner, workspace_id) in [("alice", "ws-a"), ("alice", "ws-b"), ("bob", "ws-a")] {
            assert_eq!(
                store
                    .retention_operation_status(owner, workspace_id, operation_id)
                    .unwrap()
                    .state,
                RetentionOperationState::Absent
            );
        }
        assert!(store
            .retention_operation_status("alice", "ws-a", "not-an-operation-id")
            .unwrap_err()
            .contains("32 hexadecimal"));

        let stale = store.preview_expire_retention("alice", "ws-a").unwrap();
        let mut late = RawTurn::new("user", "arrived after retention preview");
        late.id = "retention-operation-late".into();
        late.owner = Some("alice".into());
        late.workspace_id = "ws-a".into();
        late.recorded_at = old.into();
        assert!(store.upsert_raw(&late, None).await);
        assert!(store
            .expire_retention_with_operation("alice", "ws-a", &stale.token, operation_id)
            .unwrap_err()
            .contains("preview is stale"));
        assert_eq!(
            store
                .retention_operation_status("alice", "ws-a", operation_id)
                .unwrap()
                .state,
            RetentionOperationState::Absent
        );
        assert!(store
            .get_curated_record(&alice.id, "alice", "ws-a")
            .unwrap()
            .is_some());

        let preview = store.preview_expire_retention("alice", "ws-a").unwrap();
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER injected_retention_operation_failure
                 BEFORE INSERT ON memory_retention_operations BEGIN
                   SELECT RAISE(ABORT, 'injected retention operation failure');
                 END;",
            )
            .unwrap();
        assert!(store
            .expire_retention_with_operation("alice", "ws-a", &preview.token, operation_id)
            .unwrap_err()
            .contains("injected retention operation failure"));
        assert_eq!(
            store
                .retention_operation_status("alice", "ws-a", operation_id)
                .unwrap()
                .state,
            RetentionOperationState::Absent
        );
        assert!(store
            .get_curated_record(&alice.id, "alice", "ws-a")
            .unwrap()
            .is_some());
        assert_eq!(
            store
                .conn
                .lock()
                .unwrap()
                .query_row(
                    "SELECT count(*) FROM raw
                     WHERE id IN ('retention-operation-raw','retention-operation-late')",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
            2
        );
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch("DROP TRIGGER injected_retention_operation_failure;")
            .unwrap();

        let committed = store
            .expire_retention_with_operation("alice", "ws-a", &preview.token, operation_id)
            .unwrap();
        assert_eq!(committed, preview.closure);
        let status = store
            .retention_operation_status("alice", "ws-a", operation_id)
            .unwrap();
        assert_eq!(status.state, RetentionOperationState::Committed);
        assert_eq!(status.closure.as_ref(), Some(&committed));
        assert!(status.committed_at.is_some());
        let status_wire = serde_json::to_value(&status).unwrap();
        assert_eq!(status_wire["operation_id"], operation_id);
        assert_eq!(status_wire["state"], "committed");
        assert_eq!(
            status_wire["closure"],
            serde_json::to_value(&committed).unwrap()
        );

        let mut after_commit = RawTurn::new("user", "expires after original operation");
        after_commit.id = "retention-operation-after-commit".into();
        after_commit.owner = Some("alice".into());
        after_commit.workspace_id = "ws-a".into();
        after_commit.recorded_at = old.into();
        assert!(store.upsert_raw(&after_commit, None).await);
        assert_eq!(
            store
                .expire_retention_with_operation("alice", "ws-a", &preview.token, operation_id)
                .unwrap(),
            committed
        );
        let changed = store.preview_expire_retention("alice", "ws-a").unwrap();
        assert!(store
            .expire_retention_with_operation("alice", "ws-a", &changed.token, operation_id)
            .unwrap_err()
            .contains("different retention expiry request"));
        assert_eq!(
            store
                .conn
                .lock()
                .unwrap()
                .query_row(
                    "SELECT count(*) FROM raw WHERE id='retention-operation-after-commit'",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
            1
        );

        for (owner, workspace_id, record) in
            [("bob", "ws-a", &bob), ("alice", "ws-b", &other_workspace)]
        {
            let scoped_preview = store.preview_expire_retention(owner, workspace_id).unwrap();
            let scoped_commit = store
                .expire_retention_with_operation(
                    owner,
                    workspace_id,
                    &scoped_preview.token,
                    operation_id,
                )
                .unwrap();
            assert_eq!(scoped_commit.curated_ids, vec![record.id.clone()]);
            assert_eq!(
                store
                    .retention_operation_status(owner, workspace_id, operation_id)
                    .unwrap()
                    .closure,
                Some(scoped_commit)
            );
        }
    }

    #[tokio::test]
    async fn every_durable_tier_uses_the_same_privacy_boundary() {
        let store = SqliteStore::memory(4).unwrap();
        let canary = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789";
        let mut raw = RawTurn::new("user", format!("raw secret {canary}"));
        raw.id = "privacy-raw".into();
        raw.owner = Some("alice".into());
        raw.workspace_id = "ws-a".into();
        assert!(store.upsert_raw(&raw, Some(&[1.0, 0.0, 0.0, 0.0])).await);

        let mut candidate = test_candidate("privacy-candidate", &format!("candidate {canary}"));
        assert!(store.insert_candidate(&candidate).unwrap());
        candidate.id = "privacy-no-store".into();
        candidate.dedup_key = "privacy-no-store".into();
        candidate.content = "<no-memory> do not persist".into();
        assert!(!store.insert_candidate(&candidate).unwrap());

        let mut curated = test_record(&format!("curated secret {canary}"));
        curated.id = "privacy-curated".into();
        curated.workspace_id = "ws-a".into();
        assert!(
            store
                .upsert_curated(&curated, Some(&[1.0, 0.0, 0.0, 0.0]))
                .await
        );
        let export = store.export_scope("alice", "ws-a").unwrap().to_string();
        assert!(!export.contains(canary));
        assert!(export.contains(crate::privacy::REDACTED));
        let conn = store.conn.lock().unwrap();
        for (table, id) in [("raw", "privacy-raw"), ("curated", "privacy-curated")] {
            let embedding_is_null: bool = conn
                .query_row(
                    &format!("SELECT embedding IS NULL FROM {table} WHERE id=?1"),
                    params![id],
                    |row| row.get(0),
                )
                .unwrap();
            assert!(
                embedding_is_null,
                "redacted {table} vector must not persist"
            );
        }
        drop(conn);

        let mut blocked_raw = RawTurn::new("user", "[memory:no-store] private");
        blocked_raw.owner = Some("alice".into());
        blocked_raw.workspace_id = "ws-a".into();
        assert!(!store.upsert_raw(&blocked_raw, None).await);
        let mut blocked_curated = test_record("<no-memory> private");
        blocked_curated.workspace_id = "ws-a".into();
        assert!(!store.upsert_curated(&blocked_curated, None).await);
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

    #[tokio::test]
    async fn explain_memory_matches_recall_workspace_visibility() {
        let store = SqliteStore::memory(4).unwrap();
        let mut global_record = test_record("the lighthouse keeps a spare lens");
        global_record.id = "m_global_explain".into();
        global_record.workspace_id = "global".into();
        assert!(store.upsert_curated(&global_record, None).await);
        let mut workspace_record = test_record("the observatory logs a transit");
        workspace_record.id = "m_ws_explain".into();
        workspace_record.workspace_id = "ws-a".into();
        assert!(store.upsert_curated(&workspace_record, None).await);

        // Recall surfaces global records in any workspace scope; explain must
        // resolve the same visible set.
        let explained = store
            .explain_memory("m_global_explain", "alice", "ws-a")
            .unwrap();
        assert!(explained.is_some(), "global record explainable from ws-a");
        let explained = store
            .explain_memory("m_ws_explain", "alice", "ws-a")
            .unwrap();
        assert!(explained.is_some());

        // Other workspaces and other owners stay invisible.
        assert!(store
            .explain_memory("m_ws_explain", "alice", "ws-b")
            .unwrap()
            .is_none());
        assert!(store
            .explain_memory("m_global_explain", "bob", "ws-a")
            .unwrap()
            .is_none());
    }
}
