//! copal-db — Copal's versioned source of truth.
//!
//! Dolt-like in what it offers (the database IS the repo: history, diff, log,
//! restore are queries), jj-like in how versioning behaves:
//!
//! - No staging area: a doc's live state is exactly its head commit; every
//!   accepted write is an amend commit (same change identity, predecessor
//!   chain intact). The commit landing is the sync event.
//! - Change ID vs commit ID: `doc_id` (ULID) is the stable change identity;
//!   commits are content-addressed (blake3) and never rewritten in place.
//! - Operation log: every mutation is an operation carrying a full view
//!   (doc → head commit). Undo/restore = new op with an older view.
//! - Conflicts are representable as data (`Content` reserves the variant);
//!   v1 never creates them (server-authoritative, single writer).
//!
//! Storage: redb (pure Rust, ACID). Blobs are raw content-addressed bytes —
//! dedup comes from hashing; compression is a later knob if size ever matters.
//! Binary assets live OUTSIDE the DB in `<data-dir>/assets/<blake3>.<ext>`,
//! tracked by AssetRef docs whose history chain records every update.
//!
//! See `.clanker/futures/copal-jj-db-source-of-truth-metaplan.md` for the full plan.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use redb::{Database, ReadableTable, TableDefinition, TableHandle};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use unicase::UniCase;
use unicode_normalization::UnicodeNormalization;

const DOCS: TableDefinition<&str, &str> = TableDefinition::new("docs");
const COMMITS: TableDefinition<&str, &str> = TableDefinition::new("commits");
const BLOBS: TableDefinition<&str, &[u8]> = TableDefinition::new("blobs");
const OPS: TableDefinition<&str, &str> = TableDefinition::new("ops");
const META: TableDefinition<&str, &str> = TableDefinition::new("meta");
const OWNER_LIFECYCLES: TableDefinition<&str, &str> = TableDefinition::new("owner_lifecycles");
const ACTIONS: TableDefinition<&str, &str> = TableDefinition::new("guarded_actions");
// Derived note-task projections live in their own keyed table. They are
// rebuildable views, never part of the canonical document view or operation
// history. Keys are scoped by owner/workspace and source document id.
const TASK_INDEX: TableDefinition<&str, &str> = TableDefinition::new("task_index");
const TASK_INDEX_RESOURCES: TableDefinition<&str, &str> = TableDefinition::new("task_index_resources");

const OP_HEAD_KEY: &str = "op_head";
const SCHEMA_VERSION_KEY: &str = "schema_version";
pub const SCHEMA_VERSION: u64 = 3;

/// A new change (checkpoint boundary) opens when the head commit is older
/// than this at write time. Decided in the metaplan (§8 Q2).
pub const CHECKPOINT_IDLE_MS: u64 = 30 * 60 * 1000;

// ── Errors ───────────────────────────────────────────────────────────────

#[derive(Debug)]
pub struct DbError(pub String);

impl std::fmt::Display for DbError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl<E: std::error::Error> From<E> for DbError {
    fn from(error: E) -> Self {
        DbError(error.to_string())
    }
}

pub type Result<T> = std::result::Result<T, DbError>;

fn err(message: impl Into<String>) -> DbError {
    DbError(message.into())
}

fn task_index_prefix(owner: &str, workspace_id: &str) -> String {
    format!("r\0{}\0{}\0", owner, workspace_id)
}

fn task_index_generation_key(owner: &str, workspace_id: &str) -> String {
    format!("g\0{}\0{}", owner, workspace_id)
}

fn task_index_count_key(owner: &str, workspace_id: &str) -> String {
    format!("c\0{}\0{}", owner, workspace_id)
}

fn task_index_filter_count_key(owner: &str, workspace_id: &str, source: &str, checked: bool) -> String {
    format!("f\0{}\0{}\0{}\0{}", owner, workspace_id, source, checked as u8)
}

fn task_index_row_prefix(owner: &str, workspace_id: &str) -> String {
    format!("t\0{}\0{}\0", owner, workspace_id)
}

fn task_index_row_key(owner: &str, workspace_id: &str, item: &Value) -> String {
    let label = item
        .get("label")
        .or_else(|| item.get("text"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase()
        .replace('\0', " ");
    let id = item.get("id").and_then(Value::as_str).unwrap_or("");
    format!("{}{}\0{}", task_index_row_prefix(owner, workspace_id), label, id)
}

fn task_index_resource_prefix(owner: &str, workspace_id: &str) -> String {
    format!("m\0{}\0{}\0", owner, workspace_id)
}

fn task_index_resource_key(owner: &str, workspace_id: &str, resource_id: &str) -> String {
    format!("{}{}", task_index_resource_prefix(owner, workspace_id), resource_id)
}

// ── Records ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DocRecord {
    #[serde(default = "doc_record_schema_version")]
    pub schema_version: u64,
    #[serde(default)]
    pub corpus: String,
    pub kind: String,
    pub created_op: String,
    #[serde(default = "unclaimed_owner")]
    pub owner: String,
    #[serde(default = "unclaimed_workspace")]
    pub workspace_id: String,
    /// Only bridge-recognized bundled content may cross tenant scopes.
    #[serde(default)]
    pub builtin: bool,
}

fn doc_record_schema_version() -> u64 {
    2
}

fn unclaimed_owner() -> String {
    "__copal_unclaimed_owner__".to_string()
}

fn unclaimed_workspace() -> String {
    "__copal_unclaimed_workspace__".to_string()
}

fn hidden_name(name: &str) -> bool {
    name.split(['/', '\\'])
        .any(|component| component.starts_with('.') && component.len() > 1)
}

fn canonical_corpus(kind: &str) -> &'static str {
    match kind {
        "markdown" | "note" | "base" | "canvas" => "notes",
        "wiki" => "wiki",
        "copal-event" | "copal-tracks" | "planning" | "calendar-projection" => "events",
        kind if kind.starts_with("treehouse-") => "treehouse",
        _ => "system",
    }
}

/// Commit content. `Conflict` is reserved (jj first-class conflicts); v1
/// never constructs it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case")]
pub enum Content {
    Blob {
        hash: String,
    },
    Asset {
        hash: String,
        ext: String,
        size: u64,
    },
    Conflict {
        base: Option<String>,
        sides: Vec<String>,
    },
    Tombstone,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommitRecord {
    pub doc: String,
    /// Previous checkpoint in this doc's history (None for the first change).
    pub parent: Option<String>,
    /// The commit this one replaces (amend chain, jj predecessors).
    pub predecessors: Vec<String>,
    /// Display / export name lives on the commit so renames are versioned
    /// and op-level undo restores them naturally.
    pub name: String,
    pub content: Content,
    pub ts: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OpRecord {
    pub parent: Option<String>,
    pub kind: String,
    pub description: String,
    pub ts: u64,
    /// Full view: every visible doc's head commit (jj view object).
    pub view: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardedRevision {
    pub kind: String,
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardedGuard {
    pub owner: String,
    pub workspace_id: String,
    pub id: String,
    #[serde(default)]
    pub revision: Option<GuardedRevision>,
    #[serde(default)]
    pub head: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardedOperation {
    pub kind: String,
    pub owner: String,
    pub workspace_id: String,
    pub id: String,
    #[serde(default)]
    pub revision: Option<GuardedRevision>,
    #[serde(default)]
    pub head: Option<String>,
    #[serde(default)]
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GuardedRequest {
    pub action_id: String,
    pub actor_id: String,
    #[serde(default)]
    pub request_digest: Option<String>,
    #[serde(default)]
    pub guards: Vec<GuardedGuard>,
    pub operations: Vec<GuardedOperation>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DocView {
    pub id: String,
    pub record_schema_version: u64,
    pub corpus: String,
    pub kind: String,
    pub owner: String,
    pub workspace_id: String,
    pub builtin: bool,
    pub name: String,
    pub head: String,
    pub ts: u64,
    /// Derived from the revisioned canonical name, so renames version hidden state.
    pub hidden: bool,
    pub deleted: bool,
    pub content: Content,
    /// UTF-8 text for Blob content; None for assets/tombstones.
    pub text: Option<String>,
}

#[derive(Debug)]
pub enum WriteOutcome {
    Committed {
        view: DocView,
        new_change: bool,
    },
    Unchanged {
        view: DocView,
    },
    /// baseCommit didn't match the head: nothing was written; caller rebases
    /// onto the returned authoritative view.
    Stale {
        view: DocView,
    },
}

#[derive(Debug, Default, Serialize)]
pub struct ImportStats {
    pub notes: usize,
    pub assets: usize,
    pub compatibility: usize,
    pub unchanged: usize,
    pub planning: bool,
    pub treehouse: bool,
    pub restored_identities: usize,
    pub entries: Vec<ImportEntry>,
    pub op: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportIdentity {
    pub id: String,
    pub corpus: String,
    pub kind: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ImportEntry {
    pub path: String,
    pub status: String,
    pub corpus: String,
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerInventory {
    pub schema_version: u64,
    pub owner: String,
    pub documents: usize,
    pub active_documents: usize,
    pub deleted_documents: usize,
    pub commits: usize,
    pub fingerprint: String,
    pub content_included: bool,
}

impl OwnerInventory {
    pub fn empty(owner: &str) -> Self {
        let encoded = serde_json::to_vec(&json!([[], []]))
            .expect("empty Copal lifecycle inventory must serialize");
        Self {
            schema_version: 1,
            owner: owner.to_string(),
            documents: 0,
            active_documents: 0,
            deleted_documents: 0,
            commits: 0,
            fingerprint: format!("blake3:{}", blake3::hash(&encoded).to_hex()),
            content_included: false,
        }
    }

    fn equivalent(&self, other: &Self) -> bool {
        self.documents == other.documents
            && self.active_documents == other.active_documents
            && self.deleted_documents == other.deleted_documents
            && self.commits == other.commits
            && self.fingerprint == other.fingerprint
    }

    fn validate_for(&self, owner: &str) -> Result<()> {
        let fingerprint = self.fingerprint.strip_prefix("blake3:");
        if self.schema_version != 1
            || self.owner != owner
            || self.content_included
            || self.active_documents.saturating_add(self.deleted_documents) != self.documents
            || !fingerprint.is_some_and(|digest| {
                digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        {
            return Err(err("invalid Copal owner lifecycle inventory"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct OwnerLifecycleRecord {
    old_owner: String,
    new_owner: String,
    manifest: Value,
    state: String,
}

// ── Data-dir resolution (metaplan §2b: the debug bit) ────────────────────

/// Resolve the data directory:
/// 1. `COPAL_DB=/path` wins outright.
/// 2. Debug bit — `COPAL_DEBUG=0|1` env, else `debug = true|false` in
///    `<root>/copal.toml` — on → `<root>/db`, off → `~/.local/share/copal`
///    (respecting `XDG_DATA_HOME`).
pub fn resolve_data_dir(root: &Path) -> PathBuf {
    if let Some(explicit) = std::env::var_os("COPAL_DB") {
        return PathBuf::from(explicit);
    }
    if debug_bit(root) {
        root.join("db")
    } else {
        xdg_data_home().join("copal")
    }
}

fn debug_bit(root: &Path) -> bool {
    match std::env::var("COPAL_DEBUG").ok().as_deref() {
        Some("1") => return true,
        Some("0") => return false,
        _ => {}
    }
    let Ok(text) = fs::read_to_string(root.join("copal.toml")) else {
        return false;
    };
    for line in text.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if let Some(value) = line.strip_prefix("debug") {
            return value
                .trim_start()
                .strip_prefix('=')
                .is_some_and(|v| v.trim() == "true");
        }
    }
    false
}

fn xdg_data_home() -> PathBuf {
    if let Some(dir) = std::env::var_os("XDG_DATA_HOME") {
        return PathBuf::from(dir);
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    home.join(".local").join("share")
}

// ── Engine ───────────────────────────────────────────────────────────────

pub struct Db {
    database: Database,
    assets_dir: PathBuf,
}

fn table_names(database: &Database) -> Result<BTreeSet<String>> {
    let txn = database.begin_read()?;
    let names = txn
        .list_tables()?
        .map(|table| table.name().to_string())
        .collect();
    Ok(names)
}

fn validate_canonical_tables(database: &Database) -> Result<()> {
    let txn = database.begin_read()?;
    txn.open_table(DOCS)
        .map_err(|error| err(format!("canonical source table 'docs' is missing or corrupt: {error}")))?;
    txn.open_table(COMMITS)
        .map_err(|error| err(format!("canonical source table 'commits' is missing or corrupt: {error}")))?;
    txn.open_table(BLOBS)
        .map_err(|error| err(format!("canonical source table 'blobs' is missing or corrupt: {error}")))?;
    txn.open_table(OPS)
        .map_err(|error| err(format!("canonical source table 'ops' is missing or corrupt: {error}")))?;
    txn.open_table(META)
        .map_err(|error| err(format!("canonical source table 'meta' is missing or corrupt: {error}")))?;
    Ok(())
}

fn repair_task_tables(database: &Database) -> Result<bool> {
    let tables = table_names(database)?;
    let task_index_missing = !tables.contains("task_index");
    let resources_missing = !tables.contains("task_index_resources");
    if !task_index_missing && !resources_missing {
        return Ok(false);
    }

    // A current-version store can lose either derived table independently.
    // Recreate both in one transaction and clear every derived row so an old
    // generation can never be advertised while the resource lookup is only
    // partially present. The next bounded task read rebuilds the projection.
    let txn = database.begin_write()?;
    let mut task_index = txn.open_table(TASK_INDEX)?;
    let mut resources = txn.open_table(TASK_INDEX_RESOURCES)?;
    let task_keys = task_index
        .iter()?
        .map(|entry| entry.map(|(key, _)| key.value().to_string()))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    for key in task_keys {
        task_index.remove(key.as_str())?;
    }
    let resource_keys = resources
        .iter()?
        .map(|entry| entry.map(|(key, _)| key.value().to_string()))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    for key in resource_keys {
        resources.remove(key.as_str())?;
    }
    drop(resources);
    drop(task_index);
    txn.commit()?;
    Ok(true)
}

impl Db {
    /// Open (creating if needed) the database at `<data_dir>/copal.redb`
    /// with assets beside it in `<data_dir>/assets/`.
    pub fn open(data_dir: &Path) -> Result<Self> {
        Self::open_with_name(data_dir, "copal")
    }

    /// Open (creating if needed) the database at `<data_dir>/<store_name>.redb`
    /// with assets beside it in `<data_dir>/assets/`.
    pub fn open_with_name(data_dir: &Path, store_name: &str) -> Result<Self> {
        fs::create_dir_all(data_dir)?;
        let assets_dir = data_dir.join("assets");
        fs::create_dir_all(&assets_dir)?;
        let database_path = data_dir.join(format!("{store_name}.redb"));
        let database_preexisted = database_path.exists();
        let database = if database_preexisted {
            Database::open(&database_path)?
        } else {
            Database::create(&database_path)?
        };
        // Ensure all tables exist so read transactions never hit
        // TableDoesNotExist on a fresh file, and record the root `init`
        // operation (jj's virtual root op) so there is always an op to
        // restore back to.
        let tables = table_names(&database)?;
        let current_version = if tables.contains("meta") {
            let txn = database.begin_read()?;
            txn.open_table(META)?
                .get(SCHEMA_VERSION_KEY)?
                .map(|value| value.value().parse::<u64>())
                .transpose()
                .map_err(|error| err(format!("database schema marker is invalid: {error}")))?
        } else {
            None
        };
        if database_preexisted && !tables.is_empty() && current_version.is_none() {
            return Err(err("database schema marker is missing or invalid; refusing to treat an existing store as new"));
        }
        if current_version.is_some_and(|version| version > SCHEMA_VERSION) {
            return Err(err(format!(
                "database schema {} is newer than supported schema {SCHEMA_VERSION}",
                current_version.unwrap()
            )));
        }
        if current_version.is_some() {
            validate_canonical_tables(&database)?;
        }
        if current_version != Some(SCHEMA_VERSION) {
            let txn = database.begin_write()?;
            {
                txn.open_table(DOCS)?;
                txn.open_table(COMMITS)?;
                txn.open_table(BLOBS)?;
                txn.open_table(OPS)?;
                txn.open_table(ACTIONS)?;
                txn.open_table(OWNER_LIFECYCLES)?;
                txn.open_table(TASK_INDEX)?;
                txn.open_table(TASK_INDEX_RESOURCES)?;
                let needs_init = txn.open_table(META)?.get(OP_HEAD_KEY)?.is_none();
                if needs_init {
                    put_op(
                        &txn,
                        None,
                        "init",
                        "initialize repository",
                        &BTreeMap::new(),
                    )?;
                } else {
                    let (head, view) = {
                        let meta = txn.open_table(META)?;
                        let head = meta
                            .get(OP_HEAD_KEY)?
                            .ok_or_else(|| err("schema migration has no operation head"))?
                            .value()
                            .to_string();
                        drop(meta);
                        let ops = txn.open_table(OPS)?;
                        let record = ops
                            .get(head.as_str())?
                            .ok_or_else(|| err("schema migration operation head is missing"))?;
                        let op: OpRecord = serde_json::from_str(record.value())?;
                        (head, op.view)
                    };
                    put_op(
                        &txn,
                        Some(head),
                        "schema-upgrade",
                        &format!(
                            "upgrade database schema {} to {SCHEMA_VERSION}",
                            current_version.unwrap_or(1)
                        ),
                        &view,
                    )?;
                }
                let target_version = SCHEMA_VERSION.to_string();
                txn.open_table(META)?
                    .insert(SCHEMA_VERSION_KEY, target_version.as_str())?;
            }
            txn.commit()?;
        } else {
            repair_task_tables(&database)?;
        }
        Ok(Self {
            database,
            assets_dir,
        })
    }

    pub fn assets_dir(&self) -> &Path {
        &self.assets_dir
    }

    pub fn task_index_get(
        &self,
        owner: &str,
        workspace_id: &str,
        ids: Option<&[String]>,
    ) -> Result<Value> {
        let txn = self.database.begin_read()?;
        let table = txn.open_table(TASK_INDEX)?;
        let prefix = task_index_prefix(owner, workspace_id);
        let generation = table
            .get(task_index_generation_key(owner, workspace_id).as_str())?
            .map(|value| value.value().to_string())
            .unwrap_or_default();
        let count = table
            .get(task_index_count_key(owner, workspace_id).as_str())?
            .and_then(|value| value.value().parse::<usize>().ok())
            .unwrap_or(0);
        let mut records = serde_json::Map::new();
        if let Some(ids) = ids {
            for document_id in ids {
                let key = format!("{prefix}{document_id}");
                if let Some(value) = table.get(key.as_str())? {
                    records.insert(document_id.to_string(), serde_json::from_str(value.value())?);
                }
            }
        } else {
            for entry in table.range(prefix.as_str()..)? {
                let (key, value) = entry?;
                let key = key.value();
                if !key.starts_with(prefix.as_str()) {
                    break;
                }
                let Some(document_id) = key.strip_prefix(prefix.as_str()) else {
                    continue;
                };
                records.insert(document_id.to_string(), serde_json::from_str(value.value())?);
            }
        }
        Ok(json!({"schemaVersion": 1, "sourceRevision": generation, "total": count, "documents": records}))
    }

    pub fn task_index_generation(&self, owner: &str, workspace_id: &str) -> Result<Value> {
        let txn = self.database.begin_read()?;
        let table = txn.open_table(TASK_INDEX)?;
        let generation = table
            .get(task_index_generation_key(owner, workspace_id).as_str())?
            .map(|value| value.value().to_string())
            .unwrap_or_default();
        let count = table
            .get(task_index_count_key(owner, workspace_id).as_str())?
            .and_then(|value| value.value().parse::<usize>().ok())
            .unwrap_or(0);
        Ok(json!({"schemaVersion": 1, "sourceRevision": generation, "total": count}))
    }

    pub fn task_index_resolve(&self, owner: &str, workspace_id: &str, resource_id: &str) -> Result<Value> {
        let txn = self.database.begin_read()?;
        let table = txn.open_table(TASK_INDEX_RESOURCES)?;
        Ok(table
            .get(task_index_resource_key(owner, workspace_id, resource_id).as_str())?
            .map(|value| json!({"id": value.value()}))
            .unwrap_or(Value::Null))
    }

    pub fn task_index_page(
        &self,
        owner: &str,
        workspace_id: &str,
        query: &str,
        completed: Option<bool>,
        source: &str,
        cursor: Option<&str>,
        limit: usize,
        generation: &str,
    ) -> Result<Value> {
        let txn = self.database.begin_read()?;
        let table = txn.open_table(TASK_INDEX)?;
        let current_generation = table
            .get(task_index_generation_key(owner, workspace_id).as_str())?
            .map(|value| value.value().to_string())
            .unwrap_or_default();
        let count = table
            .get(task_index_count_key(owner, workspace_id).as_str())?
            .and_then(|value| value.value().parse::<usize>().ok())
            .unwrap_or(0);
        if current_generation != generation {
            return Err(err("stale_cursor"));
        }
        let filtered_total = if query.is_empty() {
            let sources: Vec<&str> = if source == "all" { vec!["vault", "markdown"] } else { vec![source] };
            completed.map_or_else(
                || sources.iter().map(|value| {
                    [false, true].iter().map(|checked| table
                        .get(task_index_filter_count_key(owner, workspace_id, value, *checked).as_str())
                        .ok()
                        .flatten()
                        .and_then(|entry| entry.value().parse::<usize>().ok())
                        .unwrap_or(0)).sum::<usize>()
                }).sum::<usize>(),
                |checked| sources.iter().map(|value| table
                    .get(task_index_filter_count_key(owner, workspace_id, value, checked).as_str())
                    .ok()
                    .flatten()
                    .and_then(|entry| entry.value().parse::<usize>().ok())
                    .unwrap_or(0)).sum(),
            )
        } else {
            count
        };
        let prefix = task_index_row_prefix(owner, workspace_id);
        let mut returned_rows = Vec::new();
        let mut scanned_rows = 0usize;
        let mut scanned_bytes = 0usize;
        let needle = query.to_lowercase();
        let mut after_cursor = cursor.is_none();
        let mut cursor_seen = cursor.is_none();
        let mut last_returned_key: Option<String> = None;
        let mut next_cursor = None;
        for entry in table.range(prefix.as_str()..)? {
            let (key, value) = entry?;
            let key = key.value();
            if !key.starts_with(prefix.as_str()) {
                break;
            }
            if !after_cursor {
                if Some(key) == cursor {
                    after_cursor = true;
                    cursor_seen = true;
                }
                continue;
            }
            scanned_rows += 1;
            scanned_bytes += value.value().len();
            let item: Value = serde_json::from_str(value.value())?;
            let matches = (source == "all"
                || item.get("source").and_then(Value::as_str) == Some(source))
                && completed.is_none_or(|wanted| item.get("checked").and_then(Value::as_bool) == Some(wanted))
                && (needle.is_empty() || {
                    let label = item.get("label").and_then(Value::as_str).unwrap_or("");
                    let text = item.get("text").and_then(Value::as_str).unwrap_or("");
                    format!("{text} {label}").to_lowercase().contains(&needle)
                });
            if !matches {
                continue;
            }
            if returned_rows.len() >= limit {
                // Keep the cursor on the last row actually returned. The
                // next request treats it as an exclusive anchor, so the
                // candidate used to prove continuation is not lost.
                next_cursor = last_returned_key.clone();
                break;
            }
            returned_rows.push(item);
            last_returned_key = Some(key.to_string());
        }
        if !cursor_seen {
            return Err(err("stale_cursor"));
        }
        let returned_count = returned_rows.len();
        Ok(json!({
            "items": returned_rows,
            "nextCursor": next_cursor,
            "sourceRevision": current_generation,
            "scannedRows": scanned_rows,
            "scannedBytes": scanned_bytes,
            "returnedRows": returned_count,
            "sourceReads": 0,
            "rewrittenRows": 0,
            "rewrittenBytes": 0,
            "total": if query.is_empty() { filtered_total } else { count },
            "indexedTotal": count,
            "matchedTotal": if query.is_empty() { json!(filtered_total) } else { Value::Null },
            "totalExact": query.is_empty(),
        }))
    }

    pub fn task_index_update(
        &self,
        owner: &str,
        workspace_id: &str,
        generation: &str,
        records: &Value,
        removed: &[String],
        rebuild: bool,
        source_reads: usize,
    ) -> Result<Value> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Err(err("owner and workspace are required"));
        }
        let object = records
            .as_object()
            .ok_or_else(|| err("task index records must be an object"))?;
        let txn = self.database.begin_write()?;
        let mut table = txn.open_table(TASK_INDEX)?;
        let prefix = task_index_prefix(owner, workspace_id);
        let row_prefix = task_index_row_prefix(owner, workspace_id);
        let resource_prefix = task_index_resource_prefix(owner, workspace_id);
        let mut resources = txn.open_table(TASK_INDEX_RESOURCES)?;
        let mut rewritten_rows = 0usize;
        let mut rewritten_bytes = 0usize;
        let mut filter_deltas: BTreeMap<(String, bool), isize> = BTreeMap::new();
        if rebuild {
            let keys = table
                .range(prefix.as_str()..)?
                .filter_map(|entry| entry.ok().map(|(key, value)| (key.value().to_string(), value.value().len())))
                .take_while(|(key, _)| key.starts_with(prefix.as_str()))
                .collect::<Vec<_>>();
            for (key, bytes) in keys {
                table.remove(key.as_str())?;
                rewritten_rows += 1;
                rewritten_bytes += bytes;
            }
            let row_keys = table
                .range(row_prefix.as_str()..)?
                .filter_map(|entry| entry.ok().map(|(key, value)| (key.value().to_string(), value.value().len())))
                .take_while(|(key, _)| key.starts_with(row_prefix.as_str()))
                .collect::<Vec<_>>();
            for (key, bytes) in row_keys {
                table.remove(key.as_str())?;
                rewritten_rows += 1;
                rewritten_bytes += bytes;
            }
            let resource_keys = resources
                .range(resource_prefix.as_str()..)?
                .filter_map(|entry| entry.ok().map(|(key, value)| (key.value().to_string(), value.value().len())))
                .take_while(|(key, _)| key.starts_with(resource_prefix.as_str()))
                .collect::<Vec<_>>();
            for (key, bytes) in resource_keys {
                resources.remove(key.as_str())?;
                rewritten_rows += 1;
                rewritten_bytes += bytes;
            }
        }
        let mut task_count = if rebuild {
            0usize
        } else {
            table
                .get(task_index_count_key(owner, workspace_id).as_str())?
                .and_then(|value| value.value().parse::<usize>().ok())
                .unwrap_or(0)
        };
        for (document_id, record) in object {
            let document_key = format!("{prefix}{document_id}");
            let previous = table
                .get(document_key.as_str())?
                .map(|value| value.value().to_string());
            if let Some(previous) = previous {
                let old: Value = serde_json::from_str(&previous)?;
                if let Some(resource_id) = old.get("resourceId").and_then(Value::as_str) {
                    let resource_key = task_index_resource_key(owner, workspace_id, resource_id);
                    if let Some(value) = resources.get(resource_key.as_str())? {
                        rewritten_bytes += value.value().len();
                    }
                    resources.remove(resource_key.as_str())?;
                    rewritten_rows += 1;
                }
                if let Some(items) = old.get("items").and_then(Value::as_array) {
                    task_count = task_count.saturating_sub(items.len());
                    for item in items {
                        if let Some(item_source) = item.get("source").and_then(Value::as_str) {
                            if let Some(checked) = item.get("checked").and_then(Value::as_bool) {
                                *filter_deltas.entry((item_source.to_string(), checked)).or_default() -= 1;
                            }
                        }
                        let row_key = task_index_row_key(owner, workspace_id, item);
                        let row_bytes = table.get(row_key.as_str())?.map(|row| row.value().len());
                        if let Some(row_bytes) = row_bytes {
                            rewritten_bytes += row_bytes;
                            table.remove(row_key.as_str())?;
                            rewritten_rows += 1;
                        }
                    }
                }
            }
            let encoded = serde_json::to_string(record)?;
            table.insert(document_key.as_str(), encoded.as_str())?;
            rewritten_rows += 1;
            rewritten_bytes += encoded.len();
            if let Some(resource_id) = record.get("resourceId").and_then(Value::as_str) {
                let resource_key = task_index_resource_key(owner, workspace_id, resource_id);
                resources.insert(resource_key.as_str(), document_id.as_str())?;
                rewritten_rows += 1;
                rewritten_bytes += document_id.len();
            }
            if let Some(items) = record.get("items").and_then(Value::as_array) {
                task_count += items.len();
                for item in items {
                    if let Some(item_source) = item.get("source").and_then(Value::as_str) {
                        if let Some(checked) = item.get("checked").and_then(Value::as_bool) {
                            *filter_deltas.entry((item_source.to_string(), checked)).or_default() += 1;
                        }
                    }
                    let row_key = task_index_row_key(owner, workspace_id, item);
                    let encoded_item = serde_json::to_string(item)?;
                    table.insert(row_key.as_str(), encoded_item.as_str())?;
                    rewritten_rows += 1;
                    rewritten_bytes += encoded_item.len();
                }
            }
        }
        for document_id in removed {
            let key = format!("{prefix}{document_id}");
            let previous = table
                .get(key.as_str())?
                .map(|value| value.value().to_string());
            if let Some(previous) = previous {
                let old: Value = serde_json::from_str(&previous)?;
                if let Some(resource_id) = old.get("resourceId").and_then(Value::as_str) {
                    let resource_key = task_index_resource_key(owner, workspace_id, resource_id);
                    if let Some(value) = resources.get(resource_key.as_str())? {
                        rewritten_bytes += value.value().len();
                    }
                    resources.remove(resource_key.as_str())?;
                    rewritten_rows += 1;
                }
                if let Some(items) = old.get("items").and_then(Value::as_array) {
                    task_count = task_count.saturating_sub(items.len());
                    for item in items {
                        if let Some(item_source) = item.get("source").and_then(Value::as_str) {
                            if let Some(checked) = item.get("checked").and_then(Value::as_bool) {
                                *filter_deltas.entry((item_source.to_string(), checked)).or_default() -= 1;
                            }
                        }
                        let row_key = task_index_row_key(owner, workspace_id, item);
                        let row_bytes = table.get(row_key.as_str())?.map(|row| row.value().len());
                        if let Some(row_bytes) = row_bytes {
                            rewritten_bytes += row_bytes;
                            table.remove(row_key.as_str())?;
                            rewritten_rows += 1;
                        }
                    }
                }
            }
            let previous_bytes = table.get(key.as_str())?.map(|previous| previous.value().len());
            if let Some(previous_bytes) = previous_bytes {
                rewritten_bytes += previous_bytes;
                table.remove(key.as_str())?;
                rewritten_rows += 1;
            }
        }
        drop(resources);
        table.insert(
            task_index_generation_key(owner, workspace_id).as_str(),
            generation,
        )?;
        rewritten_rows += 2;
        rewritten_bytes += generation.len();
        let count_value = task_count.to_string();
        rewritten_bytes += count_value.len();
        table.insert(task_index_count_key(owner, workspace_id).as_str(), count_value.as_str())?;
        for ((source, checked), delta) in filter_deltas {
            let key = task_index_filter_count_key(owner, workspace_id, &source, checked);
            let prior = table
                .get(key.as_str())?
                .and_then(|value| value.value().parse::<isize>().ok())
                .unwrap_or(0);
            let next = (prior + delta).max(0) as usize;
            let encoded = next.to_string();
            table.insert(key.as_str(), encoded.as_str())?;
            rewritten_rows += 1;
            rewritten_bytes += encoded.len();
        }
        drop(table);
        txn.commit()?;
        Ok(json!({
            "outcome":"applied",
            "sourceRevision":generation,
            "sourceReads":source_reads,
            "rewrittenRows":rewritten_rows,
            "rewrittenBytes":rewritten_bytes
        }))
    }

    /// True when the current view contains no docs (fresh or fully-undone
    /// database) — the auto-import trigger.
    pub fn is_empty(&self) -> Result<bool> {
        Ok(self.current_view()?.is_empty())
    }

    pub fn schema_version(&self) -> Result<u64> {
        let txn = self.database.begin_read()?;
        let meta = txn.open_table(META)?;
        Ok(meta
            .get(SCHEMA_VERSION_KEY)?
            .and_then(|value| value.value().parse().ok())
            .unwrap_or(1))
    }

    // ── Reads ────────────────────────────────────────────────────────────

    pub fn current_view(&self) -> Result<BTreeMap<String, String>> {
        let txn = self.database.begin_read()?;
        Ok(self.read_view(&txn)?)
    }

    fn read_view(&self, txn: &redb::ReadTransaction) -> Result<BTreeMap<String, String>> {
        let meta = txn.open_table(META)?;
        let Some(head) = meta.get(OP_HEAD_KEY)? else {
            return Ok(BTreeMap::new());
        };
        let ops = txn.open_table(OPS)?;
        let record = ops
            .get(head.value())?
            .ok_or_else(|| err("op head points at missing op"))?;
        let op: OpRecord = serde_json::from_str(record.value())?;
        Ok(op.view)
    }

    fn load_commit(&self, txn: &redb::ReadTransaction, id: &str) -> Result<CommitRecord> {
        let commits = txn.open_table(COMMITS)?;
        let record = commits
            .get(id)?
            .ok_or_else(|| err(format!("missing commit {id}")))?;
        Ok(serde_json::from_str(record.value())?)
    }

    fn doc_record(&self, txn: &redb::ReadTransaction, id: &str) -> Result<DocRecord> {
        let docs = txn.open_table(DOCS)?;
        let record = docs
            .get(id)?
            .ok_or_else(|| err(format!("missing doc {id}")))?;
        Ok(serde_json::from_str(record.value())?)
    }

    fn view_of(&self, txn: &redb::ReadTransaction, id: &str, head: &str) -> Result<DocView> {
        let commit = self.load_commit(txn, head)?;
        let text = match &commit.content {
            Content::Blob { hash } => {
                let blobs = txn.open_table(BLOBS)?;
                let bytes = blobs
                    .get(hash.as_str())?
                    .ok_or_else(|| err(format!("missing blob {hash}")))?;
                Some(String::from_utf8_lossy(bytes.value()).into_owned())
            }
            _ => None,
        };
        let doc = self.doc_record(txn, id)?;
        let hidden = hidden_name(&commit.name);
        let deleted = matches!(commit.content, Content::Tombstone);
        let corpus = if doc.corpus.is_empty() {
            canonical_corpus(&doc.kind).to_string()
        } else {
            doc.corpus.clone()
        };
        Ok(DocView {
            id: id.to_string(),
            record_schema_version: doc.schema_version,
            corpus,
            kind: doc.kind,
            owner: doc.owner,
            workspace_id: doc.workspace_id,
            builtin: doc.builtin,
            name: commit.name,
            head: head.to_string(),
            ts: commit.ts,
            hidden,
            deleted,
            content: commit.content,
            text,
        })
    }

    /// All visible (non-tombstoned) docs, sorted by name.
    pub fn list_docs(&self) -> Result<Vec<DocView>> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let mut out = Vec::new();
        for (doc_id, head) in &view {
            let doc_view = self.view_of(&txn, doc_id, head)?;
            if !matches!(doc_view.content, Content::Tombstone) {
                out.push(doc_view);
            }
        }
        out.sort_by(|a, b| {
            (&a.name, &a.corpus, &a.kind, &a.id).cmp(&(&b.name, &b.corpus, &b.kind, &b.id))
        });
        Ok(out)
    }

    pub fn get_doc(&self, id: &str) -> Result<Option<DocView>> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let Some(head) = view.get(id) else {
            return Ok(None);
        };
        let doc_view = self.view_of(&txn, id, head)?;
        if matches!(doc_view.content, Content::Tombstone) {
            return Ok(None);
        }
        Ok(Some(doc_view))
    }

    pub fn find_doc_by_name(&self, name: &str) -> Result<Option<DocView>> {
        Ok(self.list_docs()?.into_iter().find(|doc| doc.name == name))
    }

    pub fn list_docs_scoped(&self, owner: &str, workspace_id: &str) -> Result<Vec<DocView>> {
        let docs = self.list_docs()?;
        let exact_names = docs
            .iter()
            .filter(|doc| {
                !unclaimed_scope(doc)
                    && !(doc.owner == "shared" && doc.workspace_id == "global")
                    && doc.owner == owner
                    && doc.workspace_id == workspace_id
            })
            .map(|doc| (doc.corpus.clone(), doc.kind.clone(), doc.name.clone()))
            .collect::<BTreeSet<_>>();
        Ok(docs
            .into_iter()
            .filter(|doc| {
                scope_allows(doc, owner, workspace_id)
                    && (!(doc.builtin && doc.owner == "shared" && doc.workspace_id == "global")
                        || !exact_names.contains(&(
                            doc.corpus.clone(),
                            doc.kind.clone(),
                            doc.name.clone(),
                        )))
            })
            .collect())
    }

    pub fn get_doc_scoped(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<DocView>> {
        Ok(self
            .get_doc(id)?
            .filter(|doc| scope_allows(doc, owner, workspace_id)))
    }

    pub fn find_doc_by_name_scoped(
        &self,
        name: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<DocView>> {
        Ok(self
            .list_docs_scoped(owner, workspace_id)?
            .into_iter()
            .find(|doc| doc.name == name))
    }

    pub fn find_doc_by_name_kind_scoped(
        &self,
        name: &str,
        kind: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<DocView>> {
        self.find_doc_by_identity_scoped(name, kind, canonical_corpus(kind), owner, workspace_id)
    }

    pub fn find_doc_by_identity_scoped(
        &self,
        name: &str,
        kind: &str,
        corpus: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<DocView>> {
        Ok(self
            .list_docs_scoped(owner, workspace_id)?
            .into_iter()
            .find(|doc| doc.name == name && doc.kind == kind && doc.corpus == corpus))
    }

    pub fn list_deleted_docs_scoped(
        &self,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Vec<DocView>> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let mut deleted = Vec::new();
        for (doc_id, head) in &view {
            let doc = self.view_of(&txn, doc_id, head)?;
            if matches!(doc.content, Content::Tombstone) && scope_allows(&doc, owner, workspace_id)
            {
                deleted.push(doc);
            }
        }
        deleted.sort_by(|a, b| b.ts.cmp(&a.ts));
        Ok(deleted)
    }

    pub fn blob_bytes(&self, hash: &str) -> Result<Option<Vec<u8>>> {
        let txn = self.database.begin_read()?;
        let blobs = txn.open_table(BLOBS)?;
        Ok(blobs.get(hash)?.map(|bytes| bytes.value().to_vec()))
    }

    /// History of a doc: newest change first; each change = its head commit
    /// plus the amend chain (predecessors) inside it.
    pub fn history(&self, id: &str) -> Result<Value> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let Some(head) = view.get(id) else {
            return Err(err("doc not found"));
        };
        let mut changes = Vec::new();
        let mut cursor = Some(head.clone());
        while let Some(commit_id) = cursor {
            let commit = self.load_commit(&txn, &commit_id)?;
            let mut amends = Vec::new();
            let mut pred = commit.predecessors.first().cloned();
            while let Some(pred_id) = pred {
                let pred_commit = self.load_commit(&txn, &pred_id)?;
                amends.push(
                    json!({ "commit": pred_id, "ts": pred_commit.ts, "name": pred_commit.name }),
                );
                pred = pred_commit.predecessors.first().cloned();
            }
            changes.push(json!({
                "commit": commit_id,
                "ts": commit.ts,
                "name": commit.name,
                "message": commit.message,
                "amends": amends,
            }));
            cursor = commit.parent;
        }
        Ok(json!({ "doc": id, "changes": changes }))
    }

    /// Unified text diff between two commits of a doc (rendered on demand;
    /// we store snapshots, not deltas).
    pub fn diff(&self, from: &str, to: &str) -> Result<String> {
        let txn = self.database.begin_read()?;
        self.diff_in_txn(&txn, from, to)
    }

    pub fn diff_scoped(
        &self,
        id: &str,
        from: &str,
        to: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<String> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let head = view
            .get(id)
            .ok_or_else(|| err("doc not found in this scope"))?;
        let current = self
            .view_of(&txn, id, head)
            .map_err(|_| err("doc not found in this scope"))?;
        if matches!(current.content, Content::Tombstone)
            || !scope_allows(&current, owner, workspace_id)
        {
            return Err(err("doc not found in this scope"));
        }
        let from_commit = self
            .load_commit(&txn, from)
            .map_err(|_| err("commit not found in this scope"))?;
        let to_commit = self
            .load_commit(&txn, to)
            .map_err(|_| err("commit not found in this scope"))?;
        if from_commit.doc != id || to_commit.doc != id {
            return Err(err("commit not found in this scope"));
        }
        self.diff_in_txn(&txn, from, to)
    }

    fn diff_in_txn(&self, txn: &redb::ReadTransaction, from: &str, to: &str) -> Result<String> {
        let read_text = |commit_id: &str| -> Result<String> {
            let commit = self.load_commit(&txn, commit_id)?;
            match commit.content {
                Content::Blob { hash } => {
                    let blobs = txn.open_table(BLOBS)?;
                    let bytes = blobs
                        .get(hash.as_str())?
                        .ok_or_else(|| err("missing blob"))?;
                    Ok(String::from_utf8_lossy(bytes.value()).into_owned())
                }
                Content::Tombstone => Ok(String::new()),
                _ => Err(err("diff only supports text content")),
            }
        };
        let old = read_text(from)?;
        let new = read_text(to)?;
        Ok(similar::TextDiff::from_lines(&old, &new)
            .unified_diff()
            .context_radius(3)
            .header(from, to)
            .to_string())
    }

    /// Operation log page, newest first.
    pub fn ops(&self, limit: usize, before: Option<&str>) -> Result<Value> {
        self.ops_for_scope(limit, before, None)
    }

    /// Operation log page restricted to documents visible in one tenant scope.
    pub fn ops_scoped(
        &self,
        limit: usize,
        before: Option<&str>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Value> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Err(err("owner and workspace are required"));
        }
        self.ops_for_scope(limit, before, Some((owner, workspace_id)))
    }

    fn ops_for_scope(
        &self,
        limit: usize,
        before: Option<&str>,
        scope: Option<(&str, &str)>,
    ) -> Result<Value> {
        let txn = self.database.begin_read()?;
        let ops = txn.open_table(OPS)?;
        let mut out = Vec::new();
        for entry in ops.iter()?.rev() {
            let (key, value) = entry?;
            let id = key.value().to_string();
            if let Some(before) = before {
                if id.as_str() >= before {
                    continue;
                }
            }
            let op: OpRecord = serde_json::from_str(value.value())?;
            let parent_view = match op.parent.as_deref() {
                Some(parent) => {
                    let encoded = ops
                        .get(parent)?
                        .ok_or_else(|| err("operation parent is missing"))?;
                    serde_json::from_str::<OpRecord>(encoded.value())?.view
                }
                None => BTreeMap::new(),
            };
            let mut changed = BTreeSet::new();
            for (document_id, head) in &op.view {
                if parent_view.get(document_id) != Some(head) {
                    changed.insert(document_id);
                }
            }
            for document_id in parent_view.keys() {
                if !op.view.contains_key(document_id) {
                    changed.insert(document_id);
                }
            }
            let mut changed_visible = changed.iter().map(|id| (*id).clone()).collect::<Vec<_>>();
            let docs = if let Some((owner, workspace_id)) = scope {
                let mut affects_scope = false;
                let mut scoped_changed = Vec::new();
                for document_id in &changed {
                    let head = op
                        .view
                        .get(*document_id)
                        .or_else(|| parent_view.get(*document_id))
                        .ok_or_else(|| err("operation document head is missing"))?;
                    if scope_allows(&self.view_of(&txn, document_id, head)?, owner, workspace_id) {
                        affects_scope = true;
                        scoped_changed.push((*document_id).clone());
                    }
                }
                if !affects_scope {
                    continue;
                }
                changed_visible = scoped_changed;
                let mut visible = 0;
                for (document_id, head) in &op.view {
                    if scope_allows(&self.view_of(&txn, document_id, head)?, owner, workspace_id) {
                        visible += 1;
                    }
                }
                visible
            } else {
                op.view.len()
            };
            out.push(json!({
                "op": id,
                "parent": op.parent,
                "kind": op.kind,
                "description": op.description,
                "ts": op.ts,
                "docs": docs,
                "changedIds": changed_visible,
            }));
            if out.len() >= limit {
                break;
            }
        }
        Ok(json!({ "ops": out }))
    }

    // ── Mutations ─────────────────────────────────────────────────────────
    //
    // Every mutation is one redb write transaction that appends commits and
    // exactly one operation, then advances the op head. redb serializes
    // writers, so this is the whole concurrency story.

    fn owner_inventory_material(
        owner: &str,
        documents: Vec<(String, DocRecord)>,
        commits: Vec<(String, CommitRecord)>,
        view: &BTreeMap<String, String>,
    ) -> OwnerInventory {
        let commit_map = commits
            .iter()
            .map(|(id, record)| (id.as_str(), record))
            .collect::<BTreeMap<_, _>>();
        let mut active_documents = 0;
        let mut deleted_documents = 0;
        let document_material = documents
            .iter()
            .map(|(id, record)| {
                let deleted = view
                    .get(id)
                    .and_then(|head| commit_map.get(head.as_str()))
                    .is_some_and(|commit| matches!(commit.content, Content::Tombstone));
                if deleted {
                    deleted_documents += 1;
                } else {
                    active_documents += 1;
                }
                json!([
                    id,
                    record.schema_version,
                    record.corpus,
                    record.kind,
                    record.created_op,
                    record.workspace_id,
                    record.builtin,
                    deleted,
                ])
            })
            .collect::<Vec<_>>();
        let commit_material = commits
            .iter()
            .map(|(id, record)| json!([id, record.doc]))
            .collect::<Vec<_>>();
        let encoded = serde_json::to_vec(&json!([document_material, commit_material]))
            .expect("Copal lifecycle inventory must serialize");
        OwnerInventory {
            schema_version: 1,
            owner: owner.to_string(),
            documents: documents.len(),
            active_documents,
            deleted_documents,
            commits: commits.len(),
            fingerprint: format!("blake3:{}", blake3::hash(&encoded).to_hex()),
            content_included: false,
        }
    }

    fn owner_inventory_in_write(
        &self,
        txn: &redb::WriteTransaction,
        owner: &str,
    ) -> Result<OwnerInventory> {
        let documents = {
            let table = txn.open_table(DOCS)?;
            let mut rows = Vec::new();
            for entry in table.iter()? {
                let (id, encoded) = entry?;
                let record: DocRecord = serde_json::from_str(encoded.value())?;
                if record.owner == owner {
                    rows.push((id.value().to_string(), record));
                }
            }
            rows
        };
        let document_ids = documents
            .iter()
            .map(|(id, _)| id.as_str())
            .collect::<BTreeSet<_>>();
        let commits = {
            let table = txn.open_table(COMMITS)?;
            let mut rows = Vec::new();
            for entry in table.iter()? {
                let (id, encoded) = entry?;
                let record: CommitRecord = serde_json::from_str(encoded.value())?;
                if document_ids.contains(record.doc.as_str()) {
                    rows.push((id.value().to_string(), record));
                }
            }
            rows
        };
        let (view, _) = self.write_view(txn)?;
        Ok(Self::owner_inventory_material(
            owner, documents, commits, &view,
        ))
    }

    pub fn owner_inventory(&self, owner: &str) -> Result<OwnerInventory> {
        validate_mutable_owner(owner)?;
        let txn = self.database.begin_read()?;
        let documents = {
            let table = txn.open_table(DOCS)?;
            let mut rows = Vec::new();
            for entry in table.iter()? {
                let (id, encoded) = entry?;
                let record: DocRecord = serde_json::from_str(encoded.value())?;
                if record.owner == owner {
                    rows.push((id.value().to_string(), record));
                }
            }
            rows
        };
        let document_ids = documents
            .iter()
            .map(|(id, _)| id.as_str())
            .collect::<BTreeSet<_>>();
        let commits = {
            let table = txn.open_table(COMMITS)?;
            let mut rows = Vec::new();
            for entry in table.iter()? {
                let (id, encoded) = entry?;
                let record: CommitRecord = serde_json::from_str(encoded.value())?;
                if document_ids.contains(record.doc.as_str()) {
                    rows.push((id.value().to_string(), record));
                }
            }
            rows
        };
        let view = self.read_view(&txn)?;
        Ok(Self::owner_inventory_material(
            owner, documents, commits, &view,
        ))
    }

    fn owner_lifecycle_key(old_owner: &str, new_owner: &str) -> String {
        let encoded = serde_json::to_vec(&[old_owner, new_owner])
            .expect("Copal lifecycle owner key must serialize");
        blake3::hash(&encoded).to_hex().to_string()
    }

    pub fn load_owner_lifecycle_manifest(
        &self,
        old_owner: &str,
        new_owner: &str,
    ) -> Result<Option<Value>> {
        validate_owner_rename(old_owner, new_owner)?;
        let key = Self::owner_lifecycle_key(old_owner, new_owner);
        // A write transaction lets old schema-v3 databases lazily create the
        // coordinator table before the first account lifecycle operation.
        let txn = self.database.begin_write()?;
        let record = {
            let table = txn.open_table(OWNER_LIFECYCLES)?;
            let encoded = table
                .get(key.as_str())?
                .map(|guard| guard.value().to_owned());
            encoded
                .as_deref()
                .map(serde_json::from_str::<OwnerLifecycleRecord>)
                .transpose()?
        };
        txn.commit()?;
        if let Some(record) = record {
            if record.old_owner != old_owner || record.new_owner != new_owner {
                return Err(err("Copal owner lifecycle journal key is inconsistent"));
            }
            Ok(Some(record.manifest))
        } else {
            Ok(None)
        }
    }

    pub fn freeze_owner_lifecycle_manifest(
        &self,
        old_owner: &str,
        new_owner: &str,
        manifest: &Value,
    ) -> Result<Value> {
        validate_owner_rename(old_owner, new_owner)?;
        let key = Self::owner_lifecycle_key(old_owner, new_owner);
        let txn = self.database.begin_write()?;
        let frozen = {
            let mut table = txn.open_table(OWNER_LIFECYCLES)?;
            let existing = table
                .get(key.as_str())?
                .map(|guard| guard.value().to_owned());
            if let Some(encoded) = existing {
                let record: OwnerLifecycleRecord = serde_json::from_str(&encoded)?;
                if record.old_owner != old_owner
                    || record.new_owner != new_owner
                    || record.manifest != *manifest
                {
                    return Err(err("Copal owner lifecycle journal conflicts with manifest"));
                }
                record.manifest
            } else {
                let record = OwnerLifecycleRecord {
                    old_owner: old_owner.to_string(),
                    new_owner: new_owner.to_string(),
                    manifest: manifest.clone(),
                    state: "frozen".to_string(),
                };
                let encoded = serde_json::to_string(&record)?;
                table.insert(key.as_str(), encoded.as_str())?;
                record.manifest
            }
        };
        txn.commit()?;
        Ok(frozen)
    }

    pub fn mark_owner_lifecycle(
        &self,
        old_owner: &str,
        new_owner: &str,
        state: &str,
    ) -> Result<()> {
        let key = Self::owner_lifecycle_key(old_owner, new_owner);
        let txn = self.database.begin_write()?;
        {
            let mut table = txn.open_table(OWNER_LIFECYCLES)?;
            let encoded = table
                .get(key.as_str())?
                .map(|guard| guard.value().to_owned())
                .ok_or_else(|| err("Copal owner lifecycle journal is missing"))?;
            let mut record: OwnerLifecycleRecord = serde_json::from_str(&encoded)?;
            if record.old_owner != old_owner || record.new_owner != new_owner {
                return Err(err("Copal owner lifecycle journal key is inconsistent"));
            }
            record.state = state.to_string();
            let updated = serde_json::to_string(&record)?;
            table.insert(key.as_str(), updated.as_str())?;
        }
        txn.commit()?;
        Ok(())
    }

    pub fn prune_owner_lifecycles(
        &self,
        owner: &str,
        except_old_owner: Option<&str>,
        except_new_owner: Option<&str>,
    ) -> Result<usize> {
        validate_mutable_owner(owner)?;
        let except_key = except_old_owner
            .zip(except_new_owner)
            .map(|(old, new)| Self::owner_lifecycle_key(old, new));
        let txn = self.database.begin_write()?;
        let removals = {
            let table = txn.open_table(OWNER_LIFECYCLES)?;
            let mut keys = Vec::new();
            for entry in table.iter()? {
                let (key, encoded) = entry?;
                let record: OwnerLifecycleRecord = serde_json::from_str(encoded.value())?;
                if (record.old_owner == owner || record.new_owner == owner)
                    && except_key.as_deref() != Some(key.value())
                {
                    keys.push(key.value().to_string());
                }
            }
            keys
        };
        {
            let mut table = txn.open_table(OWNER_LIFECYCLES)?;
            for key in &removals {
                table.remove(key.as_str())?;
            }
        }
        txn.commit()?;
        Ok(removals.len())
    }

    pub fn preflight_rename_owner(
        &self,
        old_owner: &str,
        new_owner: &str,
    ) -> Result<(OwnerInventory, OwnerInventory)> {
        validate_owner_rename(old_owner, new_owner)?;
        let source = self.owner_inventory(old_owner)?;
        let target = self.owner_inventory(new_owner)?;
        if source.documents > 0 && target.documents > 0 {
            return Err(err("destination owner already has Copal documents"));
        }
        Ok((source, target))
    }

    pub fn reconcile_owner_rename(
        &self,
        old_owner: &str,
        new_owner: &str,
        expected_source: &OwnerInventory,
        expected_target: &OwnerInventory,
    ) -> Result<Value> {
        validate_owner_rename(old_owner, new_owner)?;
        expected_source.validate_for(old_owner)?;
        expected_target.validate_for(new_owner)?;
        if !expected_target.equivalent(&OwnerInventory::empty(new_owner)) {
            return Err(err("destination Copal owner manifest is not empty"));
        }
        let txn = self.database.begin_write()?;
        let source = self.owner_inventory_in_write(&txn, old_owner)?;
        let target = self.owner_inventory_in_write(&txn, new_owner)?;
        let state;
        let mut changed = 0;
        if expected_source.documents == 0 {
            if source.documents > 0 || target.documents > 0 {
                return Err(err("Copal owner state changed after preflight"));
            }
            state = "empty";
        } else if source.equivalent(expected_source) && target.documents == 0 {
            let updates = {
                let docs = txn.open_table(DOCS)?;
                let mut updates = Vec::with_capacity(source.documents);
                for entry in docs.iter()? {
                    let (document_id, encoded) = entry?;
                    let mut record: DocRecord = serde_json::from_str(encoded.value())?;
                    if record.owner == old_owner {
                        record.owner = new_owner.to_string();
                        updates.push((document_id.value().to_string(), record));
                    }
                }
                updates
            };
            changed = updates.len();
            let mut docs = txn.open_table(DOCS)?;
            for (document_id, record) in &updates {
                let encoded = serde_json::to_string(record)?;
                docs.insert(document_id.as_str(), encoded.as_str())?;
            }
            state = "applied";
        } else if source.documents == 0 && target.equivalent(expected_source) {
            state = "already_applied";
        } else {
            return Err(err("Copal owner state changed after preflight"));
        }
        let source_after = self.owner_inventory_in_write(&txn, old_owner)?;
        let target_after = self.owner_inventory_in_write(&txn, new_owner)?;
        if source_after.documents > 0
            || (expected_source.documents > 0 && !target_after.equivalent(expected_source))
        {
            return Err(err("Copal owner rename did not converge"));
        }
        txn.commit()?;
        Ok(json!({
            "schema_version": 1,
            "state": state,
            "documents": expected_source.documents,
            "changed_documents": changed,
            "source": source_after,
            "target": target_after,
            "content_included": false,
        }))
    }

    pub fn compensate_owner_rename(
        &self,
        old_owner: &str,
        new_owner: &str,
        expected_source: &OwnerInventory,
        expected_target: &OwnerInventory,
    ) -> Result<Value> {
        validate_owner_rename(old_owner, new_owner)?;
        expected_source.validate_for(old_owner)?;
        expected_target.validate_for(new_owner)?;
        if !expected_target.equivalent(&OwnerInventory::empty(new_owner)) {
            return Err(err("destination Copal owner manifest is not empty"));
        }
        let mut reverse_source = expected_source.clone();
        reverse_source.owner = new_owner.to_string();
        let mut reverse_target = expected_target.clone();
        reverse_target.owner = old_owner.to_string();
        self.reconcile_owner_rename(new_owner, old_owner, &reverse_source, &reverse_target)
    }

    pub fn rename_owner(&self, old_owner: &str, new_owner: &str) -> Result<usize> {
        let (source, target) = self.preflight_rename_owner(old_owner, new_owner)?;
        let receipt = self.reconcile_owner_rename(old_owner, new_owner, &source, &target)?;
        Ok(receipt["changed_documents"].as_u64().unwrap_or(0) as usize)
    }

    pub fn asset_references(&self) -> Result<BTreeSet<String>> {
        let txn = self.database.begin_read()?;
        let commits = txn.open_table(COMMITS)?;
        let mut referenced = BTreeSet::new();
        for entry in commits.iter()? {
            let (_, encoded) = entry?;
            let record: CommitRecord = serde_json::from_str(encoded.value())?;
            if let Content::Asset { hash, ext, .. } = record.content {
                referenced.insert(format!("{}.{}", hash, safe_asset_ext(&ext)));
            }
        }
        drop(commits);
        drop(txn);
        Ok(referenced)
    }

    pub fn compact_orphan_assets(&self, additional_references: &BTreeSet<String>) -> Result<usize> {
        let mut referenced = self.asset_references()?;
        referenced.extend(additional_references.iter().cloned());
        let mut removed = 0;
        for entry in fs::read_dir(&self.assets_dir)? {
            let entry = entry?;
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err(err("Copal asset store contains an unsafe entry"));
            }
            let name = entry.file_name().to_string_lossy().into_owned();
            if !referenced.contains(&name) {
                fs::remove_file(path)?;
                removed += 1;
            }
        }
        Ok(removed)
    }

    fn purge_owner_inner(
        &self,
        owner: &str,
        expected: Option<&OwnerInventory>,
        compact_assets: bool,
    ) -> Result<Value> {
        validate_mutable_owner(owner)?;
        if let Some(frozen) = expected {
            frozen.validate_for(owner)?;
        }
        let txn = self.database.begin_write()?;
        let before = self.owner_inventory_in_write(&txn, owner)?;
        if let Some(frozen) = expected {
            if before.documents > 0 && !before.equivalent(frozen) {
                return Err(err("Copal owner state changed before purge"));
            }
        }
        if before.documents == 0 {
            txn.abort()?;
            let removed_assets = if compact_assets {
                self.compact_orphan_assets(&BTreeSet::new())?
            } else {
                0
            };
            return Ok(json!({
                "schema_version": 1,
                "state": if expected.is_some() { "already_applied" } else { "empty" },
                "before": before,
                "after": self.owner_inventory(owner)?,
                "removed_assets": removed_assets,
                "content_included": false,
                "physical_compaction": true,
                "history_retained": false,
            }));
        }

        let target_doc_ids = {
            let docs = txn.open_table(DOCS)?;
            let mut ids = BTreeSet::new();
            for entry in docs.iter()? {
                let (id, encoded) = entry?;
                let record: DocRecord = serde_json::from_str(encoded.value())?;
                if record.owner == owner {
                    ids.insert(id.value().to_string());
                }
            }
            ids
        };
        let mut target_commit_ids = Vec::new();
        let mut target_blob_hashes = BTreeSet::new();
        let mut retained_blob_hashes = BTreeSet::new();
        {
            let commits = txn.open_table(COMMITS)?;
            for entry in commits.iter()? {
                let (id, encoded) = entry?;
                let record: CommitRecord = serde_json::from_str(encoded.value())?;
                let is_target = target_doc_ids.contains(&record.doc);
                match &record.content {
                    Content::Blob { hash } => {
                        if is_target {
                            target_blob_hashes.insert(hash.clone());
                        } else {
                            retained_blob_hashes.insert(hash.clone());
                        }
                    }
                    Content::Asset { .. } => {}
                    Content::Conflict { base, sides } => {
                        let references = base.iter().chain(sides.iter());
                        if is_target {
                            target_blob_hashes.extend(references.cloned());
                        } else {
                            retained_blob_hashes.extend(references.cloned());
                        }
                    }
                    Content::Tombstone => {}
                }
                if is_target {
                    target_commit_ids.push(id.value().to_string());
                }
            }
        }
        let operation_updates = {
            let ops = txn.open_table(OPS)?;
            let mut updates = Vec::new();
            for entry in ops.iter()? {
                let (id, encoded) = entry?;
                let mut record: OpRecord = serde_json::from_str(encoded.value())?;
                let old_len = record.view.len();
                record
                    .view
                    .retain(|doc_id, _| !target_doc_ids.contains(doc_id));
                if record.view.len() != old_len {
                    record.kind = "owner-lifecycle-redacted".to_string();
                    record.description = "redacted account lifecycle operation".to_string();
                    updates.push((id.value().to_string(), record));
                }
            }
            updates
        };
        {
            let mut ops = txn.open_table(OPS)?;
            for (id, record) in &operation_updates {
                let encoded = serde_json::to_string(record)?;
                ops.insert(id.as_str(), encoded.as_str())?;
            }
        }
        {
            let mut docs = txn.open_table(DOCS)?;
            for id in &target_doc_ids {
                docs.remove(id.as_str())?;
            }
        }
        {
            let mut commits = txn.open_table(COMMITS)?;
            for id in &target_commit_ids {
                commits.remove(id.as_str())?;
            }
        }
        {
            let mut blobs = txn.open_table(BLOBS)?;
            for hash in target_blob_hashes.difference(&retained_blob_hashes) {
                blobs.remove(hash.as_str())?;
            }
        }
        let after = self.owner_inventory_in_write(&txn, owner)?;
        if after.documents > 0 || after.commits > 0 {
            return Err(err("Copal owner purge did not converge"));
        }
        txn.commit()?;
        let removed_assets = if compact_assets {
            self.compact_orphan_assets(&BTreeSet::new())?
        } else {
            0
        };
        Ok(json!({
            "schema_version": 1,
            "state": "applied",
            "before": before,
            "after": after,
            "removed_documents": target_doc_ids.len(),
            "removed_commits": target_commit_ids.len(),
            "removed_assets": removed_assets,
            "content_included": false,
            "physical_compaction": true,
            "history_retained": false,
        }))
    }

    pub fn purge_owner(&self, owner: &str, expected: Option<&OwnerInventory>) -> Result<Value> {
        self.purge_owner_inner(owner, expected, true)
    }

    pub fn purge_owner_deferred_assets(
        &self,
        owner: &str,
        expected: Option<&OwnerInventory>,
    ) -> Result<Value> {
        self.purge_owner_inner(owner, expected, false)
    }

    pub fn create_doc(
        &self,
        kind: &str,
        name: &str,
        content: &str,
        message: Option<&str>,
    ) -> Result<DocView> {
        self.create_doc_scoped("shared", "global", kind, name, content, message)
    }

    /// Create bridge-recognized bundled content. This is intentionally not
    /// reachable through the public bridge protocol: normal shared/global
    /// records are tenant-invisible unless a seed routine marks them.
    #[doc(hidden)]
    pub fn create_builtin_seed_doc(
        &self,
        kind: &str,
        name: &str,
        content: &str,
        message: Option<&str>,
    ) -> Result<DocView> {
        self.create_doc_with_marker("shared", "global", kind, name, content, message, true)
    }

    pub fn create_doc_scoped(
        &self,
        owner: &str,
        workspace_id: &str,
        kind: &str,
        name: &str,
        content: &str,
        message: Option<&str>,
    ) -> Result<DocView> {
        self.create_doc_with_marker(owner, workspace_id, kind, name, content, message, false)
    }

    fn create_doc_with_marker(
        &self,
        owner: &str,
        workspace_id: &str,
        kind: &str,
        name: &str,
        content: &str,
        message: Option<&str>,
        builtin: bool,
    ) -> Result<DocView> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Err(err("owner and workspace are required"));
        }
        if owner == unclaimed_owner() || workspace_id == unclaimed_workspace() {
            return Err(err("unclaimed scope is reserved"));
        }
        let corpus = canonical_corpus(kind);
        let collision = self.list_docs()?.into_iter().any(|doc| {
            doc.owner == owner
                && doc.workspace_id == workspace_id
                && doc.kind == kind
                && doc.corpus == corpus
                && doc.name == name
                && (!builtin || doc.builtin)
        });
        if collision {
            return Err(err(format!("name already exists: {name}")));
        }
        let doc_id = new_ulid();
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        {
            let mut docs = txn.open_table(DOCS)?;
            let record = DocRecord {
                schema_version: doc_record_schema_version(),
                corpus: canonical_corpus(kind).to_string(),
                kind: kind.to_string(),
                created_op: "pending".to_string(),
                owner: owner.to_string(),
                workspace_id: workspace_id.to_string(),
                builtin,
            };
            docs.insert(doc_id.as_str(), serde_json::to_string(&record)?.as_str())?;
        }
        let hash = put_blob(&txn, content.as_bytes())?;
        let commit = CommitRecord {
            doc: doc_id.clone(),
            parent: None,
            predecessors: Vec::new(),
            name: name.to_string(),
            content: Content::Blob { hash },
            ts: now_ms(),
            message: message.map(ToString::to_string),
        };
        let commit_id = put_commit(&txn, &commit)?;
        view.insert(doc_id.clone(), commit_id);
        put_op(&txn, parent_op, "create", &format!("create {name}"), &view)?;
        txn.commit()?;
        Ok(self.get_doc(&doc_id)?.ok_or_else(|| err("create failed"))?)
    }

    /// Promote a bridge-recognized legacy shared seed without rewriting its
    /// commit or content. Callers must first validate the exact bundled seed.
    #[doc(hidden)]
    pub fn claim_builtin_seed_doc(&self, id: &str) -> Result<DocView> {
        let current = self
            .get_doc(id)?
            .ok_or_else(|| err("seed document not found"))?;
        if current.owner != "shared" || current.workspace_id != "global" {
            return Err(err(
                "only shared/global documents can be claimed as builtin",
            ));
        }
        if current.builtin && current.record_schema_version == doc_record_schema_version() {
            return Ok(current);
        }

        let txn = self.database.begin_write()?;
        let (view, parent_op) = self.write_view(&txn)?;
        if view.get(id) != Some(&current.head) {
            return Err(err("seed document changed during claim"));
        }
        {
            let mut docs = txn.open_table(DOCS)?;
            let encoded = docs
                .get(id)?
                .ok_or_else(|| err("seed document not found"))?;
            let mut record: DocRecord = serde_json::from_str(encoded.value())?;
            if record.owner != "shared" || record.workspace_id != "global" {
                return Err(err(
                    "only shared/global documents can be claimed as builtin",
                ));
            }
            record.builtin = true;
            record.schema_version = doc_record_schema_version();
            let replacement = serde_json::to_string(&record)?;
            drop(encoded);
            docs.insert(id, replacement.as_str())?;
        }
        put_op(
            &txn,
            parent_op,
            "seed-promote",
            &format!("promote builtin seed {}", current.name),
            &view,
        )?;
        txn.commit()?;
        self.get_doc(id)?
            .ok_or_else(|| err("seed document disappeared during claim"))
    }

    pub fn write_doc_scoped(
        &self,
        id: &str,
        content: &str,
        base: Option<&str>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<WriteOutcome> {
        self.require_write_scope(id, owner, workspace_id)?;
        self.write_doc(id, content, base)
    }

    /// Apply one same-database guarded write and its idempotency receipt in a
    /// single Redb transaction. Cross-scope operations are rejected here;
    /// only a coordinator may claim atomicity across database files.
    pub fn commit_guarded(
        &self,
        request: &GuardedRequest,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Value> {
        if request.action_id.trim().is_empty() || request.actor_id.trim().is_empty() {
            return Err(err("action_id and actor_id are required"));
        }
        if request.operations.len() != 1 {
            return Ok(
                json!({"outcome":"unsupported", "reason":"managed commit supports exactly one operation"}),
            );
        }
        let operation = &request.operations[0];
        if operation.kind != "write" {
            return Ok(
                json!({"outcome":"unsupported", "reason":"only write operations are supported"}),
            );
        }
        let material = json!({"action_id":request.action_id, "actor_id":request.actor_id, "guards":request.guards, "operations":request.operations});
        let computed_digest = format!(
            "blake3:{}",
            blake3::hash(
                serde_json::to_string(&material)
                    .unwrap_or_default()
                    .as_bytes()
            )
            .to_hex()
        );
        if request
            .request_digest
            .as_deref()
            .is_some_and(|supplied| supplied != computed_digest)
        {
            return Err(err("request digest does not match payload"));
        }
        let digest = request.request_digest.clone().unwrap_or(computed_digest);
        let txn = self.database.begin_write()?;
        let mut actions = txn.open_table(ACTIONS)?;
        if let Some(existing) = actions.get(request.action_id.as_str())? {
            let receipt: Value = serde_json::from_str(existing.value())?;
            if receipt.get("actor_id").and_then(Value::as_str) != Some(request.actor_id.as_str())
                || receipt.get("request_digest").and_then(Value::as_str) != Some(digest.as_str())
            {
                return Ok(
                    json!({"outcome":"idempotency_conflict", "action_id":request.action_id}),
                );
            }
            return Ok(receipt);
        }
        let same_scope = |candidate_owner: &str, candidate_workspace: &str| {
            candidate_owner == owner && candidate_workspace == workspace_id
        };
        if !same_scope(&operation.owner, &operation.workspace_id)
            || request
                .guards
                .iter()
                .any(|guard| !same_scope(&guard.owner, &guard.workspace_id))
        {
            return Ok(
                json!({"outcome":"unsupported", "reason":"cross-database guarded operations require an external coordinator"}),
            );
        }
        let (mut view, parent_op) = self.write_view(&txn)?;
        let docs = txn.open_table(DOCS)?;
        let target_record = docs
            .get(operation.id.as_str())?
            .ok_or_else(|| err("target document not found in selected database"))?;
        let target_metadata: DocRecord = serde_json::from_str(target_record.value())?;
        if target_metadata.owner != owner
            || target_metadata.workspace_id != workspace_id
            || target_metadata.builtin
        {
            return Err(err("target document is not writable in this scope"));
        }
        drop(target_record);
        drop(docs);
        let current_head = view
            .get(&operation.id)
            .cloned()
            .ok_or_else(|| err("doc not found"))?;
        let revision_value = |revision: &Option<GuardedRevision>,
                              head: &Option<String>,
                              label: &str|
         -> Result<String> {
            if let Some(tagged) = revision {
                if tagged.kind != "copalHead" || tagged.value.trim().is_empty() {
                    return Err(err(format!(
                        "{label} must be a non-empty copalHead revision"
                    )));
                }
                return Ok(tagged.value.clone());
            }
            head.clone()
                .filter(|value| !value.trim().is_empty())
                .ok_or_else(|| err(format!("{label} revision is required")))
        };
        let expected = revision_value(&operation.revision, &operation.head, "target")?;
        let resource = json!({"owner":owner, "workspace_id":workspace_id, "id":operation.id});
        let receipt = |outcome: &str,
                       before: Option<&str>,
                       after: Option<&str>,
                       reason: Option<&str>,
                       resource: &Value| {
            let mut value = json!({"outcome":outcome, "action_id":request.action_id, "actor_id":request.actor_id, "request_digest":digest, "resource":resource});
            if let Some(value_before) = before {
                value["before"] = json!({"kind":"copalHead", "value":value_before});
            }
            if let Some(value_after) = after {
                value["after"] = json!({"kind":"copalHead", "value":value_after});
                value["revision"] = value["after"].clone();
            }
            if let Some(value_reason) = reason {
                value["reason"] = json!(value_reason);
            }
            value
        };
        let mut conflict = None;
        for guard in &request.guards {
            let docs = txn.open_table(DOCS)?;
            let guard_record = docs
                .get(guard.id.as_str())?
                .ok_or_else(|| err("guard document not found in selected database"))?;
            let guard_metadata: DocRecord = serde_json::from_str(guard_record.value())?;
            if guard_metadata.owner != owner || guard_metadata.workspace_id != workspace_id {
                return Err(err("cross-scope guard requires an external coordinator"));
            }
            drop(guard_record);
            drop(docs);
            let guard_head = revision_value(&guard.revision, &guard.head, "guard")?;
            let actual = view
                .get(&guard.id)
                .cloned()
                .ok_or_else(|| err("guard document not found"))?;
            if guard_head != actual {
                conflict = Some(
                    json!({"outcome":"conflict", "action_id":request.action_id, "actor_id":request.actor_id, "request_digest":digest, "resource":{"owner":owner, "workspace_id":workspace_id, "id":guard.id}, "expected":{"kind":"copalHead","value":guard_head}, "actual":{"kind":"copalHead","value":actual}, "reason":"guard revision mismatch"}),
                );
                break;
            }
        }
        if conflict.is_none() && expected != current_head {
            conflict = Some(receipt(
                "conflict",
                Some(&expected),
                Some(&current_head),
                Some("target revision mismatch"),
                &resource,
            ));
        }
        if let Some(value) = conflict {
            let encoded = serde_json::to_string(&value)?;
            actions.insert(request.action_id.as_str(), encoded.as_str())?;
            drop(actions);
            txn.commit()?;
            return Ok(value);
        }
        let commit = load_commit_in_txn(&txn, &current_head)?;
        if matches!(commit.content, Content::Tombstone) {
            return Err(err("doc not found"));
        }
        if !matches!(commit.content, Content::Blob { .. }) {
            return Ok(json!({
                "outcome":"unsupported",
                "action_id":request.action_id,
                "reason":"managed guarded writes require Blob text content"
            }));
        }
        if let Content::Blob { hash } = &commit.content {
            let unchanged = {
                let blobs = txn.open_table(BLOBS)?;
                let current = blobs
                    .get(hash.as_str())?
                    .ok_or_else(|| err("missing blob"))?;
                String::from_utf8_lossy(current.value()) == operation.content
            };
            if unchanged {
                let value = receipt(
                    "unchanged",
                    Some(&current_head),
                    Some(&current_head),
                    None,
                    &resource,
                );
                let encoded = serde_json::to_string(&value)?;
                actions.insert(request.action_id.as_str(), encoded.as_str())?;
                drop(actions);
                txn.commit()?;
                return Ok(value);
            }
        }
        let hash = put_blob(&txn, operation.content.as_bytes())?;
        let new_commit = CommitRecord {
            doc: operation.id.clone(),
            parent: commit.parent.clone(),
            predecessors: vec![current_head.clone()],
            name: commit.name.clone(),
            content: Content::Blob { hash },
            ts: now_ms(),
            message: None,
        };
        let new_head = put_commit(&txn, &new_commit)?;
        view.insert(operation.id.clone(), new_head.clone());
        put_op(
            &txn,
            parent_op,
            "guarded-write",
            &format!("guarded write {}", commit.name),
            &view,
        )?;
        let value = receipt(
            "applied",
            Some(&current_head),
            Some(&new_head),
            None,
            &resource,
        );
        let encoded = serde_json::to_string(&value)?;
        actions.insert(request.action_id.as_str(), encoded.as_str())?;
        drop(actions);
        txn.commit()?;
        Ok(value)
    }

    pub fn history_scoped(&self, id: &str, owner: &str, workspace_id: &str) -> Result<Value> {
        // A tombstone remains an owner-scoped history object after trash.  A
        // normal `get_doc_scoped` deliberately hides it from active reads,
        // so inspect the current commit directly for this history-only path.
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let Some(head) = view.get(id) else {
            return Err(err("doc not found in this scope"));
        };
        let current = self.view_of(&txn, id, head)?;
        if !scope_allows(&current, owner, workspace_id) {
            return Err(err("doc not found in this scope"));
        }
        drop(txn);
        self.history(id)
    }

    pub fn rename_doc_scoped(
        &self,
        id: &str,
        new_name: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<DocView> {
        self.require_write_scope(id, owner, workspace_id)?;
        let current = self
            .get_doc(id)?
            .ok_or_else(|| err("doc not found in this scope"))?;
        if let Some(existing) = self.find_doc_by_identity_scoped(
            new_name,
            &current.kind,
            &current.corpus,
            owner,
            workspace_id,
        )? {
            if existing.id != id {
                return Err(err(format!("name already exists: {new_name}")));
            }
        }
        self.rename_doc_unchecked(id, new_name)
    }

    pub fn delete_doc_scoped(&self, id: &str, owner: &str, workspace_id: &str) -> Result<()> {
        self.require_write_scope(id, owner, workspace_id)?;
        self.delete_doc(id)
    }

    pub fn checkpoint_scoped(
        &self,
        id: &str,
        message: Option<&str>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<DocView> {
        self.require_write_scope(id, owner, workspace_id)?;
        self.checkpoint(id, message)
    }

    pub fn restore_doc_scoped(
        &self,
        id: &str,
        commit_id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<DocView> {
        self.require_write_scope(id, owner, workspace_id)?;
        self.restore_doc(id, commit_id)
    }

    pub fn restore_deleted_doc_scoped(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<DocView> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let head = view
            .get(id)
            .ok_or_else(|| err("doc not found in this scope"))?;
        let deleted = self.view_of(&txn, id, head)?;
        if unclaimed_scope(&deleted)
            || owner == unclaimed_owner()
            || workspace_id == unclaimed_workspace()
            || deleted.owner != owner
            || deleted.workspace_id != workspace_id
            || !matches!(deleted.content, Content::Tombstone)
        {
            return Err(err("doc not found in this scope"));
        }
        let tombstone = self.load_commit(&txn, head)?;
        let previous = tombstone
            .parent
            .ok_or_else(|| err("deleted doc has no restorable parent"))?;
        drop(txn);
        self.restore_doc(id, &previous)
    }

    fn require_scope(&self, id: &str, owner: &str, workspace_id: &str) -> Result<()> {
        if self.get_doc_scoped(id, owner, workspace_id)?.is_none() {
            return Err(err("doc not found in this scope"));
        }
        Ok(())
    }

    fn require_write_scope(&self, id: &str, owner: &str, workspace_id: &str) -> Result<()> {
        let Some(doc) = self.get_doc(id)? else {
            return Err(err("doc not found in this scope"));
        };
        if unclaimed_scope(&doc)
            || owner == unclaimed_owner()
            || workspace_id == unclaimed_workspace()
            || (doc.owner == "shared" && doc.workspace_id == "global" && !doc.builtin)
        {
            return Err(err("doc not found in this scope"));
        }
        if doc.owner != owner || doc.workspace_id != workspace_id {
            if doc.builtin && doc.owner == "shared" && doc.workspace_id == "global" {
                return Err(err("document is read-only in this scope"));
            }
            return Err(err("doc not found in this scope"));
        }
        Ok(())
    }

    /// The write pipeline (metaplan §1, recanonized turn 3): every accepted
    /// write is an amend commit; a new change opens at the checkpoint
    /// boundary; identical content is a no-op; a stale `base` writes nothing
    /// and returns the authoritative head for the caller to rebase onto.
    ///
    /// Everything — staleness check included — happens inside the write
    /// transaction, so concurrent writers are fully serialized and the
    /// never-desync guarantee holds.
    pub fn write_doc(&self, id: &str, content: &str, base: Option<&str>) -> Result<WriteOutcome> {
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        let head = view.get(id).cloned().ok_or_else(|| err("doc not found"))?;
        let head_commit = load_commit_in_txn(&txn, &head)?;
        if matches!(head_commit.content, Content::Tombstone) {
            return Err(err("doc not found"));
        }
        if let Some(base) = base {
            if base != head {
                drop(txn);
                let view = self.get_doc(id)?.ok_or_else(|| err("doc not found"))?;
                return Ok(WriteOutcome::Stale { view });
            }
        }
        let current_text = match &head_commit.content {
            Content::Blob { hash } => {
                let blobs = txn.open_table(BLOBS)?;
                let bytes = blobs
                    .get(hash.as_str())?
                    .ok_or_else(|| err("missing blob"))?;
                Some(String::from_utf8_lossy(bytes.value()).into_owned())
            }
            _ => None,
        };
        if current_text.as_deref() == Some(content) {
            drop(txn);
            let view = self.get_doc(id)?.ok_or_else(|| err("doc not found"))?;
            return Ok(WriteOutcome::Unchanged { view });
        }
        let now = now_ms();
        let new_change = now.saturating_sub(head_commit.ts) > CHECKPOINT_IDLE_MS;
        let hash = put_blob(&txn, content.as_bytes())?;
        let commit = CommitRecord {
            doc: id.to_string(),
            parent: if new_change {
                Some(head.clone())
            } else {
                head_commit.parent.clone()
            },
            predecessors: if new_change {
                Vec::new()
            } else {
                vec![head.clone()]
            },
            name: head_commit.name.clone(),
            content: Content::Blob { hash },
            ts: now,
            message: None,
        };
        let commit_id = put_commit(&txn, &commit)?;
        view.insert(id.to_string(), commit_id);
        put_op(
            &txn,
            parent_op,
            "snapshot",
            &format!("snapshot {}", head_commit.name),
            &view,
        )?;
        txn.commit()?;
        let updated = self.get_doc(id)?.ok_or_else(|| err("write failed"))?;
        Ok(WriteOutcome::Committed {
            view: updated,
            new_change,
        })
    }

    /// Freeze the current head as a named history unit; subsequent writes
    /// amend a fresh commit on top of it (jj `new`).
    pub fn checkpoint(&self, id: &str, message: Option<&str>) -> Result<DocView> {
        let current = self.get_doc(id)?.ok_or_else(|| err("doc not found"))?;
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        let commit = CommitRecord {
            doc: id.to_string(),
            parent: Some(current.head.clone()),
            predecessors: Vec::new(),
            name: current.name.clone(),
            content: current.content.clone(),
            ts: now_ms(),
            message: message.map(ToString::to_string),
        };
        let commit_id = put_commit(&txn, &commit)?;
        view.insert(id.to_string(), commit_id);
        put_op(
            &txn,
            parent_op,
            "checkpoint",
            &format!("checkpoint {}", current.name),
            &view,
        )?;
        txn.commit()?;
        Ok(self.get_doc(id)?.ok_or_else(|| err("checkpoint failed"))?)
    }

    pub fn rename_doc(&self, id: &str, new_name: &str) -> Result<DocView> {
        let current = self.get_doc(id)?.ok_or_else(|| err("doc not found"))?;
        if let Some(existing) = self.find_doc_by_identity_scoped(
            new_name,
            &current.kind,
            &current.corpus,
            &current.owner,
            &current.workspace_id,
        )? {
            if existing.id != id {
                return Err(err(format!("name already exists: {new_name}")));
            }
        }
        self.rename_doc_unchecked(id, new_name)
    }

    fn rename_doc_unchecked(&self, id: &str, new_name: &str) -> Result<DocView> {
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        let head = view.get(id).cloned().ok_or_else(|| err("doc not found"))?;
        let head_commit = load_commit_in_txn(&txn, &head)?;
        if matches!(head_commit.content, Content::Tombstone) {
            return Err(err("doc not found"));
        }
        let target_scope = load_doc_record_in_txn(&txn, id)?;
        let old_name = head_commit.name.clone();

        let mut reference_updates = Vec::new();
        for (reference_id, reference_head) in &view {
            if reference_id == id {
                continue;
            }
            let reference_scope = load_doc_record_in_txn(&txn, reference_id)?;
            let reference_corpus = if reference_scope.corpus.is_empty() {
                canonical_corpus(&reference_scope.kind)
            } else {
                &reference_scope.corpus
            };
            let target_corpus = if target_scope.corpus.is_empty() {
                canonical_corpus(&target_scope.kind)
            } else {
                &target_scope.corpus
            };
            if reference_scope.owner != target_scope.owner
                || reference_scope.workspace_id != target_scope.workspace_id
                || reference_corpus != target_corpus
                || matches!(reference_scope.kind.as_str(), "note" | "wiki")
            {
                continue;
            }
            let reference_commit = load_commit_in_txn(&txn, reference_head)?;
            let Content::Blob { hash } = &reference_commit.content else {
                continue;
            };
            let text = {
                let blobs = txn.open_table(BLOBS)?;
                let bytes = blobs
                    .get(hash.as_str())?
                    .ok_or_else(|| err(format!("missing blob {hash}")))?;
                String::from_utf8_lossy(bytes.value()).into_owned()
            };
            let rewritten = rewrite_wikilinks(&text, &old_name, new_name);
            if rewritten != text {
                reference_updates.push((
                    reference_id.clone(),
                    reference_head.clone(),
                    reference_commit,
                    rewritten,
                ));
            }
        }

        let commit = CommitRecord {
            doc: id.to_string(),
            parent: head_commit.parent.clone(),
            predecessors: vec![head],
            name: new_name.to_string(),
            content: head_commit.content.clone(),
            ts: now_ms(),
            message: None,
        };
        let commit_id = put_commit(&txn, &commit)?;
        view.insert(id.to_string(), commit_id);
        for (reference_id, reference_head, reference_commit, rewritten) in &reference_updates {
            let hash = put_blob(&txn, rewritten.as_bytes())?;
            let commit = CommitRecord {
                doc: reference_id.clone(),
                parent: reference_commit.parent.clone(),
                predecessors: vec![reference_head.clone()],
                name: reference_commit.name.clone(),
                content: Content::Blob { hash },
                ts: now_ms(),
                message: Some(format!("update links for rename {old_name} -> {new_name}")),
            };
            let commit_id = put_commit(&txn, &commit)?;
            view.insert(reference_id.clone(), commit_id);
        }
        let description = if reference_updates.is_empty() {
            format!("rename {old_name} -> {new_name}")
        } else {
            format!(
                "rename {old_name} -> {new_name} and update {} linked documents",
                reference_updates.len()
            )
        };
        put_op(&txn, parent_op, "rename", &description, &view)?;
        txn.commit()?;
        Ok(self.get_doc(id)?.ok_or_else(|| err("rename failed"))?)
    }

    /// Tombstone the doc (hidden from view, fully recoverable via undo or
    /// restore of an earlier commit).
    pub fn delete_doc(&self, id: &str) -> Result<()> {
        let current = self.get_doc(id)?.ok_or_else(|| err("doc not found"))?;
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        let commit = CommitRecord {
            doc: id.to_string(),
            parent: Some(current.head.clone()),
            predecessors: Vec::new(),
            name: current.name.clone(),
            content: Content::Tombstone,
            ts: now_ms(),
            message: None,
        };
        let commit_id = put_commit(&txn, &commit)?;
        view.insert(id.to_string(), commit_id);
        put_op(
            &txn,
            parent_op,
            "delete",
            &format!("delete {}", current.name),
            &view,
        )?;
        txn.commit()?;
        Ok(())
    }

    /// Bring an old commit's content forward as the new head (history only
    /// ever moves forward; nothing is rewritten).
    pub fn restore_doc(&self, id: &str, commit_id: &str) -> Result<DocView> {
        let txn_read = self.database.begin_read()?;
        let old = self.load_commit(&txn_read, commit_id)?;
        if old.doc != id {
            return Err(err("commit does not belong to doc"));
        }
        drop(txn_read);
        let head = self
            .current_view()?
            .get(id)
            .cloned()
            .ok_or_else(|| err("doc not found"))?;
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        let commit = CommitRecord {
            doc: id.to_string(),
            parent: Some(head),
            predecessors: Vec::new(),
            name: old.name.clone(),
            content: old.content.clone(),
            ts: now_ms(),
            message: Some(format!("restore {commit_id}")),
        };
        let new_commit = put_commit(&txn, &commit)?;
        view.insert(id.to_string(), new_commit);
        put_op(
            &txn,
            parent_op,
            "restore",
            &format!("restore {} to {commit_id}", old.name),
            &view,
        )?;
        txn.commit()?;
        Ok(self.get_doc(id)?.ok_or_else(|| err("restore failed"))?)
    }

    /// Op-level undo (jj `op restore`): new operation whose view is the
    /// target op's view (default: parent of the current op). Returns the doc
    /// ids whose heads changed so callers can broadcast per-doc events.
    pub fn undo(&self, target_op: Option<&str>) -> Result<Vec<String>> {
        let txn = self.database.begin_write()?;
        let (current_view, parent_op) = self.write_view(&txn)?;
        let current_op = parent_op.clone().ok_or_else(|| err("nothing to undo"))?;
        let target = match target_op {
            Some(id) => id.to_string(),
            None => {
                let ops = txn.open_table(OPS)?;
                let record = ops
                    .get(current_op.as_str())?
                    .ok_or_else(|| err("missing current op"))?;
                let op: OpRecord = serde_json::from_str(record.value())?;
                op.parent.ok_or_else(|| err("nothing to undo"))?
            }
        };
        let restored_view: BTreeMap<String, String> = {
            let ops = txn.open_table(OPS)?;
            let record = ops
                .get(target.as_str())?
                .ok_or_else(|| err("target op not found"))?;
            let op: OpRecord = serde_json::from_str(record.value())?;
            op.view
        };
        let mut changed = Vec::new();
        for (doc, head) in current_view.iter() {
            if restored_view.get(doc) != Some(head) {
                changed.push(doc.clone());
            }
        }
        for doc in restored_view.keys() {
            if !current_view.contains_key(doc) {
                changed.push(doc.clone());
            }
        }
        put_op(
            &txn,
            parent_op,
            "undo",
            &format!("restore repo to op {target}"),
            &restored_view,
        )?;
        txn.commit()?;
        Ok(changed)
    }

    // ── Assets (metaplan §3b: outside the DB, tracked by it) ─────────────

    /// Write asset bytes content-addressed into `assets/` and create or
    /// amend the AssetRef doc named `name`. Old versions stay on disk;
    /// the doc's history is the chain of hashes.
    pub fn put_asset(&self, name: &str, ext: &str, bytes: &[u8]) -> Result<DocView> {
        self.put_asset_scoped("shared", "global", name, ext, bytes)
    }

    /// Write a content-addressed asset into an owner/workspace scope. This is
    /// the native primitive used by `.memes` import so imported asset IDs are
    /// scoped and cannot be supplied by an archive.
    pub fn put_asset_scoped(
        &self,
        owner: &str,
        workspace_id: &str,
        name: &str,
        ext: &str,
        bytes: &[u8],
    ) -> Result<DocView> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Err(err("owner and workspace are required"));
        }
        let ext = safe_asset_ext(ext.trim_start_matches('.'));
        let content = store_import_asset(&self.assets_dir, &ext, bytes)?;
        let existing = self.list_docs()?.into_iter().find(|doc| {
            doc.owner == owner
                && doc.workspace_id == workspace_id
                && !doc.builtin
                && doc.kind == "asset"
                && doc.corpus == "system"
                && doc.name == name
        });
        match existing {
            Some(existing) => {
                if existing.content == content {
                    return Ok(existing);
                }
                let txn = self.database.begin_write()?;
                let (mut view, parent_op) = self.write_view(&txn)?;
                let commit = CommitRecord {
                    doc: existing.id.clone(),
                    parent: Some(existing.head.clone()),
                    predecessors: Vec::new(),
                    name: name.to_string(),
                    content,
                    ts: now_ms(),
                    message: None,
                };
                let commit_id = put_commit(&txn, &commit)?;
                view.insert(existing.id.clone(), commit_id);
                put_op(
                    &txn,
                    parent_op,
                    "asset-update",
                    &format!("update asset {name}"),
                    &view,
                )?;
                txn.commit()?;
                Ok(self
                    .get_doc(&existing.id)?
                    .ok_or_else(|| err("asset update failed"))?)
            }
            None => {
                let doc_id = new_ulid();
                let txn = self.database.begin_write()?;
                let (mut view, parent_op) = self.write_view(&txn)?;
                {
                    let mut docs = txn.open_table(DOCS)?;
                    let record = DocRecord {
                        schema_version: doc_record_schema_version(),
                        corpus: "system".to_string(),
                        kind: "asset".to_string(),
                        created_op: "pending".to_string(),
                        owner: owner.to_string(),
                        workspace_id: workspace_id.to_string(),
                        builtin: false,
                    };
                    docs.insert(doc_id.as_str(), serde_json::to_string(&record)?.as_str())?;
                }
                let commit = CommitRecord {
                    doc: doc_id.clone(),
                    parent: None,
                    predecessors: Vec::new(),
                    name: name.to_string(),
                    content,
                    ts: now_ms(),
                    message: None,
                };
                let commit_id = put_commit(&txn, &commit)?;
                view.insert(doc_id.clone(), commit_id);
                put_op(
                    &txn,
                    parent_op,
                    "asset-update",
                    &format!("add asset {name}"),
                    &view,
                )?;
                txn.commit()?;
                Ok(self
                    .get_doc(&doc_id)?
                    .ok_or_else(|| err("asset create failed"))?)
            }
        }
    }

    pub fn asset_file(&self, hash: &str, ext: &str) -> PathBuf {
        self.assets_dir.join(format!("{hash}.{ext}"))
    }

    // ── Import (vault dir → docs, ONE operation) ─────────────────────────

    /// Walk an Obsidian-style vault directory: note files become docs, image
    /// files become assets, and an optional planning JSON becomes the
    /// `planning` doc — all recorded as one `import` operation (undoable as
    /// a unit). Existing docs with the same name are updated only when
    /// content differs.
    pub fn import_vault(
        &self,
        vault_dir: &Path,
        planning_file: Option<&Path>,
    ) -> Result<ImportStats> {
        self.import_vault_scoped(vault_dir, planning_file, "shared", "global")
    }

    /// Scoped import used by Odysseus. The extracted vault is temporary; Redb
    /// remains the source of truth after this single atomic operation.
    pub fn import_vault_scoped(
        &self,
        vault_dir: &Path,
        planning_file: Option<&Path>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<ImportStats> {
        self.import_vault_scoped_as(vault_dir, planning_file, owner, workspace_id, "markdown")
    }

    /// Import note-like Markdown into an explicit canonical corpus kind. The
    /// route prepares `note`/`wiki` envelopes; direct legacy callers retain
    /// `markdown` behavior through `import_vault_scoped`.
    pub fn import_vault_scoped_as(
        &self,
        vault_dir: &Path,
        planning_file: Option<&Path>,
        owner: &str,
        workspace_id: &str,
        note_kind: &str,
    ) -> Result<ImportStats> {
        self.import_vault_scoped_as_with_ids(
            vault_dir,
            planning_file,
            owner,
            workspace_id,
            note_kind,
            &BTreeMap::new(),
        )
    }

    /// Restore a Copal export while retaining its stable document identities.
    /// Every supplied identity must reconcile to exactly one imported path.
    pub fn import_vault_scoped_as_with_ids(
        &self,
        vault_dir: &Path,
        planning_file: Option<&Path>,
        owner: &str,
        workspace_id: &str,
        note_kind: &str,
        restore_ids: &BTreeMap<String, ImportIdentity>,
    ) -> Result<ImportStats> {
        self.import_vault_scoped_as_with_ids_and_heads(
            vault_dir,
            planning_file,
            owner,
            workspace_id,
            note_kind,
            restore_ids,
            &BTreeMap::new(),
        )
    }

    /// Restore a native export with an atomic expected-head check. Empty
    /// `expected_heads` retains ordinary import behavior; populated maps are
    /// checked inside the same write operation that applies the import.
    pub fn import_vault_scoped_as_with_ids_and_heads(
        &self,
        vault_dir: &Path,
        planning_file: Option<&Path>,
        owner: &str,
        workspace_id: &str,
        note_kind: &str,
        restore_ids: &BTreeMap<String, ImportIdentity>,
        expected_heads: &BTreeMap<String, String>,
    ) -> Result<ImportStats> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Err(err("owner and workspace are required"));
        }
        if !matches!(note_kind, "markdown" | "note" | "wiki") {
            return Err(err("note kind must be markdown, note, or wiki"));
        }
        for (path, identity) in restore_ids {
            if path.is_empty()
                || identity.id.is_empty()
                || identity.id.len() > 128
                || !identity
                    .id
                    .chars()
                    .all(|value| value.is_ascii_alphanumeric() || matches!(value, '_' | '-'))
                || identity.corpus.is_empty()
                || identity.kind.is_empty()
            {
                return Err(err("restore identity map contains invalid fields"));
            }
        }
        let canonical_root = fs::canonicalize(vault_dir)?;
        if let Some(planning) = planning_file {
            let metadata = fs::symlink_metadata(planning)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || !fs::canonicalize(planning)?.starts_with(&canonical_root)
            {
                return Err(err(
                    "planning file must be a real file inside the import root",
                ));
            }
        }
        const NOTE_SUFFIXES: &[&str] = &["md", "markdown", "base", "canvas", "dclg"];

        let mut files = Vec::new();
        collect_files(vault_dir, &mut files)?;
        let mut stats = ImportStats::default();
        let mut restored_paths = BTreeSet::new();
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        // Build the authoritative existing-document snapshot from this write
        // transaction.  Name, identity, and expected-head checks must observe
        // the same serialized state that the import mutates; a read followed
        // by begin_write would allow an intervening writer to slip past a
        // restore guard.
        let mut all_existing = BTreeMap::new();
        let mut existing = BTreeMap::new();
        let mut portable_names: BTreeMap<String, (String, String, String)> = BTreeMap::new();
        for (document_id, head) in &view {
            let document = view_of_in_txn(&txn, document_id, head)?;
            all_existing.insert(document.id.clone(), document.clone());
            if document.deleted
                || document.owner != owner
                || document.workspace_id != workspace_id
                || (owner == "shared" && workspace_id == "global" && document.builtin)
            {
                continue;
            }
            existing.insert(
                (
                    document.corpus.clone(),
                    document.kind.clone(),
                    document.name.clone(),
                ),
                document.clone(),
            );
            if document.corpus == "wiki" {
                let portable = portable_name_key(&document.name);
                if let Some(previous) = portable_names.insert(
                    portable,
                    (
                        document.id.clone(),
                        document.name.clone(),
                        document.kind.clone(),
                    ),
                ) {
                    if previous.0 != document.id {
                        return Err(err(format!(
                            "Wiki resources collide on portable path: {} and {}",
                            previous.1, document.name
                        )));
                    }
                }
            }
        }

        let mut restore_destination_ids = BTreeSet::new();
        for (path, identity) in restore_ids {
            if !restore_destination_ids.insert(identity.id.clone()) {
                return Err(err(format!(
                    "restore identity is duplicated by imported path {path}"
                )));
            }
            if let Some(current) = all_existing.get(&identity.id) {
                if current.deleted
                    || current.builtin
                    || current.owner != owner
                    || current.workspace_id != workspace_id
                    || current.corpus != identity.corpus
                    || current.kind != identity.kind
                {
                    return Err(err(format!(
                        "restore identity is owned outside the requested scope: {}",
                        identity.id
                    )));
                }
            }
        }

        for (document_id, expected_head) in expected_heads {
            let current = existing
                .values()
                .find(|document| document.id == *document_id);
            if current.is_some_and(|document| document.head != *expected_head) {
                return Err(err(format!("stale restore head for {document_id}")));
            }
            if current.is_none() {
                return Err(err(format!("restore head is missing for {document_id}")));
            }
        }

        // Preflight every Wiki path against this same transaction snapshot
        // before writing a blob or content-addressed asset.  The staged vault
        // is one portable namespace: a page and an asset cannot differ only
        // by kind, case, or Unicode composition, including when another
        // writer commits between the caller's list and this import.
        let mut preflight_names = portable_names.clone();
        for path in &files {
            let rel = path
                .strip_prefix(vault_dir)
                .map_err(|_| err("import path escaped its vault root"))?;
            let archive_name = rel.to_string_lossy().replace('\\', "/");
            let restore_identity = restore_ids.get(&archive_name);
            let is_planning = planning_file.is_some_and(|planning| planning == path.as_path());
            let is_treehouse = archive_name == ".copal/treehouse-state.json";
            if is_planning || is_treehouse {
                continue;
            }
            let wiki_relative = rel
                .strip_prefix(Path::new(".copal/wiki"))
                .ok()
                .filter(|value| !value.as_os_str().is_empty());
            let event_relative = rel
                .strip_prefix(Path::new(".events"))
                .ok()
                .filter(|value| !value.as_os_str().is_empty());
            if event_relative.is_some() {
                continue;
            }
            let record_relative = wiki_relative.unwrap_or(rel);
            let rel_name = record_relative.to_string_lossy().replace('\\', "/");
            let effective_note_kind = if wiki_relative.is_some() {
                "wiki"
            } else {
                note_kind
            };
            let corpus = match effective_note_kind {
                "wiki" => "wiki",
                _ => "notes",
            };
            if corpus != "wiki" {
                continue;
            }
            let hidden = record_relative
                .components()
                .any(|part| part.as_os_str().to_string_lossy().starts_with('.'));
            let raw_ext = path
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or("")
                .to_ascii_lowercase();
            let native_wiki_note = wiki_relative.is_some()
                && restore_identity.is_some_and(|identity| identity.kind == "wiki");
            let native_wiki_asset = wiki_relative.is_some()
                && restore_identity.is_some_and(|identity| identity.kind == "asset");
            let note_candidate = (!hidden || native_wiki_note)
                && (NOTE_SUFFIXES.contains(&raw_ext.as_str()) || native_wiki_note);
            let kind = if note_candidate {
                "wiki"
            } else if hidden && !native_wiki_asset {
                "compatibility"
            } else {
                "asset"
            };
            if let Some(identity) = restore_identity {
                if let Some(current) = all_existing.get(&identity.id) {
                    if current.name != rel_name || current.corpus != corpus || current.kind != kind
                    {
                        return Err(err(format!(
                            "restore identity does not match imported path: {}",
                            rel_name
                        )));
                    }
                }
            }
            check_portable_wiki_name(&preflight_names, corpus, kind, &rel_name, restore_identity)?;
            let key = portable_name_key(&rel_name);
            preflight_names.entry(key).or_insert_with(|| {
                (
                    restore_identity
                        .map(|identity| identity.id.clone())
                        .unwrap_or_else(|| archive_name.clone()),
                    rel_name,
                    kind.to_string(),
                )
            });
        }

        for path in files {
            let rel = path
                .strip_prefix(vault_dir)
                .map_err(|_| err("import path escaped its vault root"))?;
            let archive_name = rel.to_string_lossy().replace('\\', "/");
            let restore_identity = restore_ids.get(&archive_name);
            let is_planning = planning_file.is_some_and(|planning| planning == path);
            let is_treehouse = archive_name == ".copal/treehouse-state.json";
            if is_planning || is_treehouse {
                continue;
            }
            let wiki_relative = rel
                .strip_prefix(Path::new(".copal/wiki"))
                .ok()
                .filter(|value| !value.as_os_str().is_empty());
            let event_relative = rel
                .strip_prefix(Path::new(".events"))
                .ok()
                .filter(|value| !value.as_os_str().is_empty());
            let record_relative = wiki_relative.unwrap_or(rel);
            let rel_name = record_relative.to_string_lossy().replace('\\', "/");
            let effective_note_kind = if wiki_relative.is_some() {
                "wiki"
            } else {
                note_kind
            };
            let corpus = if event_relative.is_some() {
                "events"
            } else {
                match effective_note_kind {
                    "wiki" => "wiki",
                    "markdown" | "note" => "notes",
                    _ => "system",
                }
            };
            let hidden = record_relative
                .components()
                .any(|part| part.as_os_str().to_string_lossy().starts_with('.'));
            let raw_ext = path
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or("")
                .to_ascii_lowercase();
            let reserved_event_note =
                event_relative.is_some() && matches!(raw_ext.as_str(), "md" | "markdown");
            let native_wiki_note = wiki_relative.is_some()
                && restore_identity.is_some_and(|identity| identity.kind == "wiki");
            let native_wiki_asset = wiki_relative.is_some()
                && restore_identity.is_some_and(|identity| identity.kind == "asset");
            if let Some(kind) = match archive_name.as_str() {
                ".copal/tracks.json" => Some("copal-tracks"),
                ".copal/planning-migration.json" => Some("copal-migration"),
                _ => None,
            } {
                let bytes = fs::read(&path)?;
                let valid = std::str::from_utf8(&bytes)
                    .ok()
                    .and_then(|content| serde_json::from_str::<Value>(content).ok())
                    .is_some_and(|value| {
                        let schema = value.get("schemaVersion").and_then(Value::as_u64);
                        match kind {
                            "copal-tracks" => matches!(schema, Some(1) | Some(2)),
                            "copal-migration" => schema == Some(1),
                            _ => false,
                        }
                    });
                let (stored_kind, content, reason) = if valid {
                    let hash = put_blob(&txn, &bytes)?;
                    (kind, Content::Blob { hash }, None)
                } else {
                    (
                        "compatibility",
                        store_import_asset(&self.assets_dir, "json", &bytes)?,
                        Some("invalid or future reserved JSON preserved inertly".to_string()),
                    )
                };
                let status = import_content_in_txn(
                    &txn,
                    &mut view,
                    &existing,
                    &mut portable_names,
                    owner,
                    workspace_id,
                    "events",
                    stored_kind,
                    &archive_name,
                    content,
                    restore_identity,
                )?;
                if restore_identity.is_some() {
                    restored_paths.insert(archive_name.clone());
                    stats.restored_identities += 1;
                }
                if status == "unchanged" {
                    stats.unchanged += 1;
                } else if reason.is_some() {
                    stats.compatibility += 1;
                } else {
                    stats.notes += 1;
                }
                stats.entries.push(ImportEntry {
                    path: archive_name,
                    status: status.to_string(),
                    corpus: "events".to_string(),
                    kind: stored_kind.to_string(),
                    reason,
                });
                continue;
            }
            if (!hidden || reserved_event_note || native_wiki_note)
                && (NOTE_SUFFIXES.contains(&raw_ext.as_str()) || native_wiki_note)
            {
                let content = match fs::read_to_string(&path) {
                    Ok(content) => content,
                    Err(error) if error.kind() == std::io::ErrorKind::InvalidData => {
                        let bytes = fs::read(&path)?;
                        let ext = safe_asset_ext(&raw_ext);
                        let asset = store_import_asset(&self.assets_dir, &ext, &bytes)?;
                        let status = import_content_in_txn(
                            &txn,
                            &mut view,
                            &existing,
                            &mut portable_names,
                            owner,
                            workspace_id,
                            corpus,
                            "compatibility",
                            &rel_name,
                            asset,
                            restore_identity,
                        )?;
                        if restore_identity.is_some() {
                            restored_paths.insert(archive_name.clone());
                            stats.restored_identities += 1;
                        }
                        if status == "unchanged" {
                            stats.unchanged += 1;
                        } else {
                            stats.compatibility += 1;
                        }
                        stats.entries.push(ImportEntry {
                            path: archive_name,
                            status: status.to_string(),
                            corpus: corpus.to_string(),
                            kind: "compatibility".to_string(),
                            reason: Some("non-UTF-8 note preserved as inert bytes".to_string()),
                        });
                        continue;
                    }
                    Err(error) => return Err(error.into()),
                };
                let invalid_reserved_event =
                    reserved_event_note && !is_copal_event_record(&content);
                let invalid_canonical_note = !reserved_event_note
                    && !native_wiki_note
                    && matches!(raw_ext.as_str(), "md" | "markdown")
                    && matches!(effective_note_kind, "note" | "wiki")
                    && !is_copal_note_record(&content);
                if invalid_reserved_event || invalid_canonical_note {
                    let ext = safe_asset_ext(&raw_ext);
                    let asset = store_import_asset(&self.assets_dir, &ext, content.as_bytes())?;
                    let status = import_content_in_txn(
                        &txn,
                        &mut view,
                        &existing,
                        &mut portable_names,
                        owner,
                        workspace_id,
                        corpus,
                        "compatibility",
                        &rel_name,
                        asset,
                        restore_identity,
                    )?;
                    if restore_identity.is_some() {
                        restored_paths.insert(archive_name.clone());
                        stats.restored_identities += 1;
                    }
                    if status == "unchanged" {
                        stats.unchanged += 1;
                    } else {
                        stats.compatibility += 1;
                    }
                    stats.entries.push(ImportEntry {
                        path: archive_name,
                        status: status.to_string(),
                        corpus: corpus.to_string(),
                        kind: "compatibility".to_string(),
                        reason: Some(if invalid_reserved_event {
                            "invalid or future event record preserved as inert bytes".to_string()
                        } else {
                            "unprepared or oversized Markdown preserved as inert bytes".to_string()
                        }),
                    });
                    continue;
                }
                let kind = if reserved_event_note {
                    "copal-event"
                } else {
                    match raw_ext.as_str() {
                        "base" => "base",
                        "canvas" => "canvas",
                        "md" | "markdown" => effective_note_kind,
                        _ if native_wiki_note => "wiki",
                        _ => "markdown",
                    }
                };
                let hash = put_blob(&txn, content.as_bytes())?;
                let status = import_content_in_txn(
                    &txn,
                    &mut view,
                    &existing,
                    &mut portable_names,
                    owner,
                    workspace_id,
                    corpus,
                    kind,
                    &rel_name,
                    Content::Blob { hash },
                    restore_identity,
                )?;
                if restore_identity.is_some() {
                    restored_paths.insert(archive_name.clone());
                    stats.restored_identities += 1;
                }
                if status == "unchanged" {
                    stats.unchanged += 1;
                } else {
                    stats.notes += 1;
                }
                stats.entries.push(ImportEntry {
                    path: archive_name,
                    status: status.to_string(),
                    corpus: corpus.to_string(),
                    kind: kind.to_string(),
                    reason: None,
                });
            } else {
                let bytes = fs::read(&path)?;
                let ext = safe_asset_ext(&raw_ext);
                let content = store_import_asset(&self.assets_dir, &ext, &bytes)?;
                let kind = if native_wiki_asset {
                    "asset"
                } else if hidden {
                    "compatibility"
                } else {
                    "asset"
                };
                let status = import_content_in_txn(
                    &txn,
                    &mut view,
                    &existing,
                    &mut portable_names,
                    owner,
                    workspace_id,
                    corpus,
                    kind,
                    &rel_name,
                    content,
                    restore_identity,
                )?;
                if restore_identity.is_some() {
                    restored_paths.insert(archive_name.clone());
                    stats.restored_identities += 1;
                }
                if status == "unchanged" {
                    stats.unchanged += 1;
                } else if hidden && !native_wiki_asset {
                    stats.compatibility += 1;
                } else {
                    stats.assets += 1;
                }
                stats.entries.push(ImportEntry {
                    path: archive_name,
                    status: status.to_string(),
                    corpus: corpus.to_string(),
                    kind: kind.to_string(),
                    reason: hidden.then(|| "dot-namespace data preserved inertly".to_string()),
                });
            }
        }

        if let Some(planning) = planning_file {
            let content = fs::read_to_string(planning)?;
            serde_json::from_str::<Value>(&content)
                .map_err(|error| err(format!("planning JSON is invalid: {error}")))?;
            let hash = put_blob(&txn, content.as_bytes())?;
            let planning_name = planning
                .strip_prefix(vault_dir)
                .unwrap_or(planning)
                .to_string_lossy()
                .replace('\\', "/");
            let restore_identity = restore_ids.get(&planning_name);
            let status = import_content_in_txn(
                &txn,
                &mut view,
                &existing,
                &mut portable_names,
                owner,
                workspace_id,
                "events",
                "planning",
                "move-data.json",
                Content::Blob { hash },
                restore_identity,
            )?;
            if restore_identity.is_some() {
                restored_paths.insert(planning_name.clone());
                stats.restored_identities += 1;
            }
            stats.planning = status != "unchanged";
            if status == "unchanged" {
                stats.unchanged += 1;
            }
            stats.entries.push(ImportEntry {
                path: planning_name,
                status: status.to_string(),
                corpus: "events".to_string(),
                kind: "planning".to_string(),
                reason: None,
            });
        }

        let treehouse = vault_dir.join(".copal/treehouse-state.json");
        if treehouse.is_file() {
            let content = fs::read_to_string(&treehouse)?;
            serde_json::from_str::<Value>(&content)
                .map_err(|error| err(format!("TreeHouse state JSON is invalid: {error}")))?;
            let name = ".copal/treehouse-state.json";
            let hash = put_blob(&txn, content.as_bytes())?;
            let status = import_content_in_txn(
                &txn,
                &mut view,
                &existing,
                &mut portable_names,
                owner,
                workspace_id,
                "treehouse",
                "treehouse-state",
                name,
                Content::Blob { hash },
                restore_ids.get(name),
            )?;
            if restore_ids.contains_key(name) {
                restored_paths.insert(name.to_string());
                stats.restored_identities += 1;
            }
            stats.treehouse = status != "unchanged";
            if status == "unchanged" {
                stats.unchanged += 1;
            }
            stats.entries.push(ImportEntry {
                path: name.to_string(),
                status: status.to_string(),
                corpus: "treehouse".to_string(),
                kind: "treehouse-state".to_string(),
                reason: None,
            });
        }

        if restored_paths.len() != restore_ids.len() {
            return Err(err(
                "restore identity map did not reconcile every imported path",
            ));
        }

        let description = format!(
            "scoped vault import: {} notes, {} assets, {} compatibility, {} unchanged{}{}",
            stats.notes,
            stats.assets,
            stats.compatibility,
            stats.unchanged,
            if stats.planning { ", planning" } else { "" },
            if stats.treehouse { ", treehouse" } else { "" },
        );
        stats.op = put_op(&txn, parent_op, "import", &description, &view)?;
        txn.commit()?;
        Ok(stats)
    }

    // ── Internals ─────────────────────────────────────────────────────────

    /// Current view + current op id, readable inside a write transaction.
    fn write_view(
        &self,
        txn: &redb::WriteTransaction,
    ) -> Result<(BTreeMap<String, String>, Option<String>)> {
        let meta = txn.open_table(META)?;
        let head = meta
            .get(OP_HEAD_KEY)?
            .map(|value| value.value().to_string());
        drop(meta);
        let Some(head) = head else {
            return Ok((BTreeMap::new(), None));
        };
        let ops = txn.open_table(OPS)?;
        let record = ops
            .get(head.as_str())?
            .ok_or_else(|| err("op head points at missing op"))?;
        let op: OpRecord = serde_json::from_str(record.value())?;
        Ok((op.view, Some(head)))
    }
}

fn view_of_in_txn(txn: &redb::WriteTransaction, id: &str, head: &str) -> Result<DocView> {
    let commit = load_commit_in_txn(txn, head)?;
    let text = match &commit.content {
        Content::Blob { hash } => {
            let blobs = txn.open_table(BLOBS)?;
            let bytes = blobs
                .get(hash.as_str())?
                .ok_or_else(|| err(format!("missing blob {hash}")))?;
            Some(String::from_utf8_lossy(bytes.value()).into_owned())
        }
        _ => None,
    };
    let doc = load_doc_record_in_txn(txn, id)?;
    let hidden = hidden_name(&commit.name);
    let deleted = matches!(commit.content, Content::Tombstone);
    let corpus = if doc.corpus.is_empty() {
        canonical_corpus(&doc.kind).to_string()
    } else {
        doc.corpus.clone()
    };
    Ok(DocView {
        id: id.to_string(),
        record_schema_version: doc.schema_version,
        corpus,
        kind: doc.kind,
        owner: doc.owner,
        workspace_id: doc.workspace_id,
        builtin: doc.builtin,
        name: commit.name,
        head: head.to_string(),
        ts: commit.ts,
        hidden,
        deleted,
        content: commit.content,
        text,
    })
}

fn load_commit_in_txn(txn: &redb::WriteTransaction, id: &str) -> Result<CommitRecord> {
    let commits = txn.open_table(COMMITS)?;
    let record = commits
        .get(id)?
        .ok_or_else(|| err(format!("missing commit {id}")))?;
    Ok(serde_json::from_str(record.value())?)
}

fn load_doc_record_in_txn(txn: &redb::WriteTransaction, id: &str) -> Result<DocRecord> {
    let docs = txn.open_table(DOCS)?;
    let record = docs
        .get(id)?
        .ok_or_else(|| err(format!("missing doc {id}")))?;
    Ok(serde_json::from_str(record.value())?)
}

fn put_blob(txn: &redb::WriteTransaction, bytes: &[u8]) -> Result<String> {
    let hash = blake3::hash(bytes).to_hex().to_string();
    let mut blobs = txn.open_table(BLOBS)?;
    if blobs.get(hash.as_str())?.is_none() {
        blobs.insert(hash.as_str(), bytes)?;
    }
    Ok(hash)
}

fn put_commit(txn: &redb::WriteTransaction, commit: &CommitRecord) -> Result<String> {
    let encoded = serde_json::to_string(commit)?;
    let commit_id = blake3::hash(encoded.as_bytes()).to_hex().to_string();
    let mut commits = txn.open_table(COMMITS)?;
    commits.insert(commit_id.as_str(), encoded.as_str())?;
    Ok(commit_id)
}

fn put_op(
    txn: &redb::WriteTransaction,
    parent: Option<String>,
    kind: &str,
    description: &str,
    view: &BTreeMap<String, String>,
) -> Result<String> {
    let op_id = new_ulid();
    let record = OpRecord {
        parent,
        kind: kind.to_string(),
        description: description.to_string(),
        ts: now_ms(),
        view: view.clone(),
    };
    let mut ops = txn.open_table(OPS)?;
    ops.insert(op_id.as_str(), serde_json::to_string(&record)?.as_str())?;
    drop(ops);
    let mut meta = txn.open_table(META)?;
    meta.insert(OP_HEAD_KEY, op_id.as_str())?;
    Ok(op_id)
}

fn safe_asset_ext(extension: &str) -> String {
    let normalized = extension.trim().to_ascii_lowercase();
    if normalized.is_empty()
        || normalized.len() > 16
        || !normalized.bytes().all(|byte| byte.is_ascii_alphanumeric())
    {
        "bin".to_string()
    } else {
        normalized
    }
}

fn portable_name_key(name: &str) -> String {
    let nfc = name.nfc().collect::<String>();
    UniCase::new(nfc).to_folded_case()
}

fn check_portable_wiki_name(
    portable_names: &BTreeMap<String, (String, String, String)>,
    corpus: &str,
    kind: &str,
    name: &str,
    restore_identity: Option<&ImportIdentity>,
) -> Result<()> {
    if corpus != "wiki" {
        return Ok(());
    }
    let portable = portable_name_key(name);
    if let Some((existing_id, existing_name, existing_kind)) = portable_names.get(&portable) {
        let ordinary_same_name =
            restore_identity.is_none() && existing_name == name && existing_kind == kind;
        let guarded_same_identity = restore_identity.is_some_and(|identity| {
            identity.id == *existing_id
                && identity.corpus == corpus
                && identity.kind == kind
                && existing_name == name
        });
        if !ordinary_same_name && !guarded_same_identity {
            return Err(err(format!(
                "Wiki resources collide on portable path: {}",
                name
            )));
        }
    }
    Ok(())
}

fn is_copal_note_record(content: &str) -> bool {
    serde_json::from_str::<Value>(content).is_ok_and(|record| {
        record.get("schemaVersion").and_then(Value::as_u64) == Some(1)
            && record
                .get("body")
                .and_then(Value::as_object)
                .is_some_and(|body| {
                    body.get("type").and_then(Value::as_str) == Some("doc")
                        && body.get("blocks").and_then(Value::as_array).is_some()
                })
    })
}

fn is_copal_event_record(content: &str) -> bool {
    let Some(frontmatter) = content.strip_prefix("---\n") else {
        return false;
    };
    let Some(end) = frontmatter.find("\n---") else {
        return false;
    };
    let mut event = false;
    let mut schema = None;
    for line in frontmatter[..end].lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        match key.trim() {
            "copal_type" => {
                event = value.trim().trim_matches(['\'', '"']) == "event";
            }
            "copal_schema" => {
                schema = value.trim().trim_matches(['\'', '"']).parse::<u64>().ok();
            }
            _ => {}
        }
    }
    event && schema == Some(1)
}

fn store_import_asset(assets_dir: &Path, extension: &str, bytes: &[u8]) -> Result<Content> {
    let hash = blake3::hash(bytes).to_hex().to_string();
    let destination = assets_dir.join(format!("{hash}.{extension}"));
    if destination.is_file() {
        if fs::read(&destination)? != bytes {
            return Err(err(
                "stored content-addressed asset failed integrity verification",
            ));
        }
    } else {
        let temporary = assets_dir.join(format!(".{hash}.{extension}.{}.tmp", new_ulid()));
        fs::write(&temporary, bytes)?;
        match fs::rename(&temporary, &destination) {
            Ok(()) => {}
            Err(_error) if destination.is_file() => {
                fs::remove_file(&temporary)?;
                if fs::read(&destination)? != bytes {
                    return Err(err("content-addressed asset collision"));
                }
            }
            Err(error) => {
                let _ = fs::remove_file(&temporary);
                return Err(error.into());
            }
        }
    }
    Ok(Content::Asset {
        hash,
        ext: extension.to_string(),
        size: bytes.len() as u64,
    })
}

fn import_content_in_txn(
    txn: &redb::WriteTransaction,
    view: &mut BTreeMap<String, String>,
    existing: &BTreeMap<(String, String, String), DocView>,
    portable_names: &mut BTreeMap<String, (String, String, String)>,
    owner: &str,
    workspace_id: &str,
    corpus: &str,
    kind: &str,
    name: &str,
    content: Content,
    restore_identity: Option<&ImportIdentity>,
) -> Result<&'static str> {
    check_portable_wiki_name(portable_names, corpus, kind, name, restore_identity)?;
    let key = (corpus.to_string(), kind.to_string(), name.to_string());
    if let Some(identity) = restore_identity {
        if identity.corpus != corpus || identity.kind != kind {
            return Err(err(
                format!(
                    "restore identity does not match imported corpus and kind: expected {}/{} got {}/{}",
                    identity.corpus, identity.kind, corpus, kind
                ),
            ));
        }
    }
    if let Some(document) = existing.get(&key) {
        if restore_identity.is_some_and(|identity| identity.id != document.id) {
            return Err(err("restore identity conflicts with the existing document"));
        }
        if document.content == content {
            return Ok("unchanged");
        }
        let commit = CommitRecord {
            doc: document.id.clone(),
            parent: Some(document.head.clone()),
            predecessors: Vec::new(),
            name: name.to_string(),
            content,
            ts: now_ms(),
            message: Some("import".to_string()),
        };
        let commit_id = put_commit(txn, &commit)?;
        view.insert(document.id.clone(), commit_id);
        return Ok("updated");
    }

    let document_id = restore_identity
        .map(|identity| identity.id.clone())
        .unwrap_or_else(new_ulid);
    {
        let mut docs = txn.open_table(DOCS)?;
        if docs.get(document_id.as_str())?.is_some() {
            return Err(err("restore identity is already owned by another document"));
        }
        let record = DocRecord {
            schema_version: doc_record_schema_version(),
            corpus: corpus.to_string(),
            kind: kind.to_string(),
            created_op: "pending".to_string(),
            owner: owner.to_string(),
            workspace_id: workspace_id.to_string(),
            builtin: false,
        };
        docs.insert(
            document_id.as_str(),
            serde_json::to_string(&record)?.as_str(),
        )?;
    }
    if corpus == "wiki" {
        portable_names.insert(
            portable_name_key(name),
            (document_id.clone(), name.to_string(), kind.to_string()),
        );
    }
    let commit = CommitRecord {
        doc: document_id.clone(),
        parent: None,
        predecessors: Vec::new(),
        name: name.to_string(),
        content,
        ts: now_ms(),
        message: Some("import".to_string()),
    };
    let commit_id = put_commit(txn, &commit)?;
    view.insert(document_id, commit_id);
    Ok("created")
}

fn collect_files(dir: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    let root_metadata = fs::symlink_metadata(dir)?;
    if root_metadata.file_type().is_symlink() || !root_metadata.is_dir() {
        return Err(err("import root must be a real directory"));
    }
    let mut entries =
        fs::read_dir(dir)?.collect::<std::result::Result<Vec<_>, std::io::Error>>()?;
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            return Err(err(format!(
                "symbolic links are not permitted in imports: {}",
                path.display()
            )));
        }
        if metadata.is_dir() {
            collect_files(&path, out)?;
        } else if metadata.is_file() {
            out.push(path);
        } else {
            return Err(err(format!(
                "special files are not permitted in imports: {}",
                path.display()
            )));
        }
    }
    out.sort();
    Ok(())
}

fn scope_allows(doc: &DocView, owner: &str, workspace_id: &str) -> bool {
    if unclaimed_scope(doc) || owner == unclaimed_owner() || workspace_id == unclaimed_workspace() {
        return false;
    }
    if doc.owner == "shared" && doc.workspace_id == "global" {
        return doc.builtin;
    }
    doc.owner == owner && doc.workspace_id == workspace_id
}

fn unclaimed_scope(doc: &DocView) -> bool {
    doc.owner == unclaimed_owner() || doc.workspace_id == unclaimed_workspace()
}

fn validate_mutable_owner(owner: &str) -> Result<()> {
    if owner.is_empty() || owner.trim() != owner {
        return Err(err("owner is required"));
    }
    if owner.eq_ignore_ascii_case("shared") {
        return Err(err("shared owner is immutable"));
    }
    Ok(())
}

fn validate_owner_rename(old_owner: &str, new_owner: &str) -> Result<()> {
    validate_mutable_owner(old_owner)?;
    validate_mutable_owner(new_owner)?;
    if old_owner == new_owner {
        return Err(err("source and destination owners must differ"));
    }
    Ok(())
}

fn normalized_wikilink(value: &str) -> String {
    value
        .trim()
        .replace('\\', "/")
        .trim_end_matches(".md")
        .to_lowercase()
}

fn rewrite_wikilinks(text: &str, old_name: &str, new_name: &str) -> String {
    let old_full = normalized_wikilink(old_name);
    let old_leaf = normalized_wikilink(old_name.rsplit('/').next().unwrap_or(old_name));
    let new_full = new_name.trim_end_matches(".md");
    let new_leaf = new_full.rsplit('/').next().unwrap_or(new_full);
    let mut output = String::with_capacity(text.len());
    let mut cursor = 0;

    while let Some(relative_start) = text[cursor..].find("[[") {
        let start = cursor + relative_start;
        output.push_str(&text[cursor..start + 2]);
        let inner_start = start + 2;
        let Some(relative_end) = text[inner_start..].find("]]") else {
            output.push_str(&text[inner_start..]);
            return output;
        };
        let end = inner_start + relative_end;
        let inner = &text[inner_start..end];
        let target_end = inner.find(['|', '#']).unwrap_or(inner.len());
        let target = &inner[..target_end];
        let normalized = normalized_wikilink(target);
        let leaf_link = !target.contains('/') && !target.contains('\\');
        if normalized == old_full || (leaf_link && normalized == old_leaf) {
            let explicit_markdown = target.trim().to_lowercase().ends_with(".md");
            let replacement = if leaf_link { new_leaf } else { new_full };
            output.push_str(replacement);
            if explicit_markdown {
                output.push_str(".md");
            }
            output.push_str(&inner[target_end..]);
        } else {
            output.push_str(inner);
        }
        output.push_str("]]");
        cursor = end + 2;
    }
    output.push_str(&text[cursor..]);
    output
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn new_ulid() -> String {
    ulid::Ulid::new().to_string()
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier};
    use std::thread;

    fn temp_db() -> (Db, PathBuf) {
        let dir = std::env::temp_dir().join(format!("copal-db-test-{}", new_ulid()));
        (Db::open(&dir).unwrap(), dir)
    }

    fn overwrite_doc_record(db: &Db, id: &str, record: Value) {
        let txn = db.database.begin_write().unwrap();
        {
            let mut docs = txn.open_table(DOCS).unwrap();
            let encoded = record.to_string();
            docs.insert(id, encoded.as_str()).unwrap();
        }
        txn.commit().unwrap();
    }

    fn install_conflict_commit(db: &Db, id: &str) -> DocView {
        let txn = db.database.begin_write().unwrap();
        let (mut view, parent_op) = db.write_view(&txn).unwrap();
        let previous = view.get(id).cloned().unwrap();
        let current = load_commit_in_txn(&txn, &previous).unwrap();
        let commit = CommitRecord {
            doc: id.into(),
            parent: Some(previous.clone()),
            predecessors: vec![previous],
            name: current.name,
            content: Content::Conflict {
                base: Some(current.doc),
                sides: vec!["left".into(), "right".into()],
            },
            ts: now_ms(),
            message: None,
        };
        let head = put_commit(&txn, &commit).unwrap();
        view.insert(id.into(), head);
        put_op(&txn, parent_op, "test-conflict", "test conflict", &view).unwrap();
        txn.commit().unwrap();
        db.get_doc(id).unwrap().unwrap()
    }

    #[test]
    fn guarded_commit_is_atomic_idempotent_and_scope_bound() {
        let (db, _dir) = temp_db();
        let doc = db
            .create_doc_scoped("alice", "course", "markdown", "progress.md", "one", None)
            .unwrap();
        let request = GuardedRequest {
            action_id: "action-1".into(),
            actor_id: "learner".into(),
            request_digest: None,
            guards: vec![GuardedGuard {
                owner: "alice".into(),
                workspace_id: "course".into(),
                id: doc.id.clone(),
                revision: Some(GuardedRevision {
                    kind: "copalHead".into(),
                    value: doc.head.clone(),
                }),
                head: None,
            }],
            operations: vec![GuardedOperation {
                kind: "write".into(),
                owner: "alice".into(),
                workspace_id: "course".into(),
                id: doc.id.clone(),
                revision: Some(GuardedRevision {
                    kind: "copalHead".into(),
                    value: doc.head.clone(),
                }),
                head: None,
                content: "two".into(),
            }],
        };
        let applied = db.commit_guarded(&request, "alice", "course").unwrap();
        assert_eq!(applied["outcome"], "applied");
        let replay = db.commit_guarded(&request, "alice", "course").unwrap();
        assert_eq!(replay, applied);
        let changed_payload = db
            .commit_guarded(
                &GuardedRequest {
                    operations: vec![GuardedOperation {
                        content: "changed".into(),
                        ..request.operations[0].clone()
                    }],
                    ..request.clone()
                },
                "alice",
                "course",
            )
            .unwrap();
        assert_eq!(changed_payload["outcome"], "idempotency_conflict");
        let mismatch = db
            .commit_guarded(
                &GuardedRequest {
                    actor_id: "other".into(),
                    ..request.clone()
                },
                "alice",
                "course",
            )
            .unwrap();
        assert_eq!(mismatch["outcome"], "idempotency_conflict");
        let current = db.get_doc(&doc.id).unwrap().unwrap();
        let stale = db
            .commit_guarded(
                &GuardedRequest {
                    action_id: "action-2".into(),
                    operations: vec![GuardedOperation {
                        revision: Some(GuardedRevision {
                            kind: "copalHead".into(),
                            value: doc.head.clone(),
                        }),
                        content: "three".into(),
                        ..request.operations[0].clone()
                    }],
                    ..request.clone()
                },
                "alice",
                "course",
            )
            .unwrap();
        assert_eq!(stale["outcome"], "conflict");
        assert_eq!(db.get_doc(&doc.id).unwrap().unwrap().head, current.head);
        let cross = db
            .commit_guarded(
                &GuardedRequest {
                    action_id: "action-3".into(),
                    ..request.clone()
                },
                "bob",
                "other",
            )
            .unwrap();
        assert_eq!(cross["outcome"], "unsupported");
        let wrong_kind = db
            .commit_guarded(
                &GuardedRequest {
                    action_id: "action-4".into(),
                    guards: vec![GuardedGuard {
                        revision: Some(GuardedRevision {
                            kind: "wrong".into(),
                            value: doc.head.clone(),
                        }),
                        ..request.guards[0].clone()
                    }],
                    ..request.clone()
                },
                "alice",
                "course",
            )
            .unwrap_err();
        assert!(wrong_kind.0.contains("copalHead"));
        let wrong_target_kind = db
            .commit_guarded(
                &GuardedRequest {
                    action_id: "action-target-kind".into(),
                    operations: vec![GuardedOperation {
                        revision: Some(GuardedRevision {
                            kind: "wrong".into(),
                            value: doc.head.clone(),
                        }),
                        ..request.operations[0].clone()
                    }],
                    ..request.clone()
                },
                "alice",
                "course",
            )
            .unwrap_err();
        assert!(wrong_target_kind.0.contains("copalHead"));
        let missing_guard = db
            .commit_guarded(
                &GuardedRequest {
                    action_id: "action-5".into(),
                    guards: vec![GuardedGuard {
                        id: "missing".into(),
                        ..request.guards[0].clone()
                    }],
                    ..request.clone()
                },
                "alice",
                "course",
            )
            .unwrap_err();
        assert!(missing_guard.0.contains("selected database"));
        let builtin = db
            .create_builtin_seed_doc("markdown", "builtin.md", "built", None)
            .unwrap();
        let builtin_request = GuardedRequest {
            action_id: "action-6".into(),
            actor_id: "learner".into(),
            request_digest: None,
            guards: vec![],
            operations: vec![GuardedOperation {
                kind: "write".into(),
                owner: "shared".into(),
                workspace_id: "global".into(),
                id: builtin.id,
                revision: Some(GuardedRevision {
                    kind: "copalHead".into(),
                    value: builtin.head,
                }),
                head: None,
                content: "nope".into(),
            }],
        };
        assert!(db
            .commit_guarded(&builtin_request, "shared", "global")
            .is_err());
    }

    #[test]
    fn guarded_commit_reopens_database_for_reset_orders() {
        fn setup() -> (PathBuf, DocView, DocView) {
            let (db, dir) = temp_db();
            let source = db
                .create_doc_scoped("alice", "course", "markdown", "source.md", "rev-1", None)
                .unwrap();
            let target = db
                .create_doc_scoped("alice", "course", "markdown", "target.md", "before", None)
                .unwrap();
            (dir, source, target)
        }
        fn progress(source: &DocView, target: &DocView) -> GuardedRequest {
            GuardedRequest {
                action_id: "progress".into(),
                actor_id: "learner".into(),
                request_digest: None,
                guards: vec![GuardedGuard {
                    owner: "alice".into(),
                    workspace_id: "course".into(),
                    id: source.id.clone(),
                    revision: Some(GuardedRevision {
                        kind: "copalHead".into(),
                        value: source.head.clone(),
                    }),
                    head: None,
                }],
                operations: vec![GuardedOperation {
                    kind: "write".into(),
                    owner: "alice".into(),
                    workspace_id: "course".into(),
                    id: target.id.clone(),
                    revision: Some(GuardedRevision {
                        kind: "copalHead".into(),
                        value: target.head.clone(),
                    }),
                    head: None,
                    content: "done".into(),
                }],
            }
        }
        let (dir, source, target) = setup();
        let progress_db = Db::open(&dir).unwrap();
        assert_eq!(
            progress_db
                .commit_guarded(&progress(&source, &target), "alice", "course")
                .unwrap()["outcome"],
            "applied"
        );
        drop(progress_db);
        let reset_db = Db::open(&dir).unwrap();
        assert!(matches!(
            reset_db
                .write_doc_scoped(
                    &source.id,
                    "rev-reset",
                    Some(&source.head),
                    "alice",
                    "course"
                )
                .unwrap(),
            WriteOutcome::Committed { .. }
        ));
        let (dir, source, target) = setup();
        let reset_db = Db::open(&dir).unwrap();
        assert!(matches!(
            reset_db
                .write_doc_scoped(
                    &source.id,
                    "rev-reset",
                    Some(&source.head),
                    "alice",
                    "course"
                )
                .unwrap(),
            WriteOutcome::Committed { .. }
        ));
        drop(reset_db);
        let progress_db = Db::open(&dir).unwrap();
        let conflict = progress_db
            .commit_guarded(&progress(&source, &target), "alice", "course")
            .unwrap();
        assert_eq!(conflict["outcome"], "conflict");
        assert_eq!(conflict["resource"]["id"], source.id);
        assert_eq!(
            progress_db
                .get_doc(&target.id)
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("before")
        );
    }

    #[test]
    fn guarded_commit_races_shared_db_progress_and_reset_without_mixed_effects() {
        for _ in 0..8 {
            let (db, _dir) = temp_db();
            let source = db
                .create_doc_scoped("alice", "course", "markdown", "source.md", "rev-1", None)
                .unwrap();
            let target = db
                .create_doc_scoped("alice", "course", "markdown", "target.md", "before", None)
                .unwrap();
            let progress = GuardedRequest {
                action_id: format!("progress-{}", new_ulid()),
                actor_id: "learner".into(),
                request_digest: None,
                guards: vec![GuardedGuard {
                    owner: "alice".into(),
                    workspace_id: "course".into(),
                    id: source.id.clone(),
                    revision: Some(GuardedRevision {
                        kind: "copalHead".into(),
                        value: source.head.clone(),
                    }),
                    head: None,
                }],
                operations: vec![GuardedOperation {
                    kind: "write".into(),
                    owner: "alice".into(),
                    workspace_id: "course".into(),
                    id: target.id.clone(),
                    revision: Some(GuardedRevision {
                        kind: "copalHead".into(),
                        value: target.head.clone(),
                    }),
                    head: None,
                    content: "done".into(),
                }],
            };
            let shared = Arc::new(db);
            let barrier = Arc::new(Barrier::new(2));
            let progress_db = Arc::clone(&shared);
            let progress_barrier = Arc::clone(&barrier);
            let progress_request = progress.clone();
            let progress_thread = thread::spawn(move || {
                progress_barrier.wait();
                progress_db
                    .commit_guarded(&progress_request, "alice", "course")
                    .unwrap()
            });
            let reset_db = Arc::clone(&shared);
            let reset_barrier = Arc::clone(&barrier);
            let source_id = source.id.clone();
            let source_head = source.head.clone();
            let reset_thread = thread::spawn(move || {
                reset_barrier.wait();
                reset_db
                    .write_doc_scoped(
                        &source_id,
                        "rev-reset",
                        Some(&source_head),
                        "alice",
                        "course",
                    )
                    .unwrap()
            });
            let guarded = progress_thread.join().unwrap();
            let reset = reset_thread.join().unwrap();
            let guarded_applied = guarded["outcome"] == "applied";
            assert!(guarded_applied || guarded["outcome"] == "conflict");
            assert!(matches!(reset, WriteOutcome::Committed { .. }));
            let final_target = shared.get_doc(&target.id).unwrap().unwrap();
            assert_eq!(
                final_target.text.as_deref(),
                if guarded_applied {
                    Some("done")
                } else {
                    Some("before")
                }
            );
            let final_source = shared.get_doc(&source.id).unwrap().unwrap();
            assert_eq!(final_source.text.as_deref(), Some("rev-reset"));
        }
    }

    #[test]
    fn guarded_commit_rejects_asset_and_conflict_content() {
        let (db, _dir) = temp_db();
        let asset = db.put_asset("asset.bin", "bin", b"bytes").unwrap();
        let asset_request = GuardedRequest {
            action_id: "asset-action".into(),
            actor_id: "learner".into(),
            request_digest: None,
            guards: vec![],
            operations: vec![GuardedOperation {
                kind: "write".into(),
                owner: "shared".into(),
                workspace_id: "global".into(),
                id: asset.id.clone(),
                revision: Some(GuardedRevision {
                    kind: "copalHead".into(),
                    value: asset.head.clone(),
                }),
                head: None,
                content: "wrong".into(),
            }],
        };
        let asset_result = db
            .commit_guarded(&asset_request, "shared", "global")
            .unwrap();
        assert_eq!(asset_result["outcome"], "unsupported");
        assert_eq!(db.get_doc(&asset.id).unwrap().unwrap().head, asset.head);
        let conflict_doc = db
            .create_doc_scoped("alice", "course", "markdown", "conflict.md", "base", None)
            .unwrap();
        let conflict = install_conflict_commit(&db, &conflict_doc.id);
        let conflict_request = GuardedRequest {
            action_id: "conflict-action".into(),
            actor_id: "learner".into(),
            request_digest: None,
            guards: vec![],
            operations: vec![GuardedOperation {
                kind: "write".into(),
                owner: "alice".into(),
                workspace_id: "course".into(),
                id: conflict.id.clone(),
                revision: Some(GuardedRevision {
                    kind: "copalHead".into(),
                    value: conflict.head.clone(),
                }),
                head: None,
                content: "wrong".into(),
            }],
        };
        let conflict_result = db
            .commit_guarded(&conflict_request, "alice", "course")
            .unwrap();
        assert_eq!(conflict_result["outcome"], "unsupported");
        assert_eq!(
            db.get_doc(&conflict.id).unwrap().unwrap().head,
            conflict.head
        );
    }

    #[test]
    fn create_write_amend_and_history() {
        let (db, _dir) = temp_db();
        let doc = db
            .create_doc("markdown", "Notes/Hello.md", "# Hello\n", None)
            .unwrap();
        assert_eq!(doc.text.as_deref(), Some("# Hello\n"));

        // Amend: same change, predecessor chain grows.
        let outcome = db
            .write_doc(&doc.id, "# Hello\nWorld\n", Some(&doc.head))
            .unwrap();
        let WriteOutcome::Committed { view, new_change } = outcome else {
            panic!("expected commit")
        };
        assert!(!new_change);
        assert_ne!(view.head, doc.head);

        // Identical content is a no-op.
        let outcome = db.write_doc(&doc.id, "# Hello\nWorld\n", None).unwrap();
        assert!(matches!(outcome, WriteOutcome::Unchanged { .. }));

        // Stale base writes nothing and returns the authoritative head.
        let outcome = db.write_doc(&doc.id, "clobber", Some(&doc.head)).unwrap();
        let WriteOutcome::Stale { view: stale_view } = outcome else {
            panic!("expected stale")
        };
        assert_eq!(stale_view.text.as_deref(), Some("# Hello\nWorld\n"));

        let history = db.history(&doc.id).unwrap();
        let changes = history["changes"].as_array().unwrap();
        assert_eq!(changes.len(), 1); // one change, amended once
        assert_eq!(changes[0]["amends"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn checkpoint_restore_and_diff() {
        let (db, _dir) = temp_db();
        let doc = db.create_doc("markdown", "a.md", "one\n", None).unwrap();
        let checkpointed = db.checkpoint(&doc.id, Some("v1")).unwrap();
        db.write_doc(&doc.id, "one\ntwo\n", None).unwrap();

        let history = db.history(&doc.id).unwrap();
        assert!(history["changes"].as_array().unwrap().len() >= 2);

        let diff = db
            .diff(&doc.head, &db.get_doc(&doc.id).unwrap().unwrap().head)
            .unwrap();
        assert!(diff.contains("+two"));

        let restored = db.restore_doc(&doc.id, &checkpointed.head).unwrap();
        assert_eq!(restored.text.as_deref(), Some("one\n"));
    }

    #[test]
    fn rename_delete_and_undo() {
        let (db, _dir) = temp_db();
        let doc = db.create_doc("markdown", "old.md", "x\n", None).unwrap();
        let renamed = db.rename_doc(&doc.id, "new.md").unwrap();
        assert_eq!(renamed.name, "new.md");
        assert!(db.find_doc_by_name("old.md").unwrap().is_none());

        db.delete_doc(&doc.id).unwrap();
        assert!(db.get_doc(&doc.id).unwrap().is_none());

        // Undo the delete: doc is visible again with its rename intact.
        let changed = db.undo(None).unwrap();
        assert_eq!(changed, vec![doc.id.clone()]);
        assert_eq!(db.get_doc(&doc.id).unwrap().unwrap().name, "new.md");
    }

    #[test]
    fn rename_does_not_rewrite_structured_note_blobs() {
        let (db, _dir) = temp_db();
        let target = db.create_doc("markdown", "Old.md", "target", None).unwrap();
        let envelope = r#"{"schemaVersion":1,"body":{"type":"doc","blocks":[{"id":"blk_1","type":"paragraph","text":"[[Old]]"}]},"properties":[],"relations":[]}"#;
        let note = db
            .create_doc("note", "Native note", envelope, None)
            .unwrap();

        db.rename_doc(&target.id, "New \"quoted\".md").unwrap();

        assert_eq!(
            db.get_doc(&note.id).unwrap().unwrap().text.as_deref(),
            Some(envelope)
        );
    }

    #[test]
    fn duplicate_names_rejected() {
        let (db, _dir) = temp_db();
        db.create_doc("markdown", "same.md", "a", None).unwrap();
        assert!(db.create_doc("markdown", "same.md", "b", None).is_err());
    }

    #[test]
    fn missing_scope_defaults_to_inaccessible_unclaimed_sentinels() {
        let (db, _dir) = temp_db();
        let legacy = db
            .create_doc("markdown", "legacy.md", "legacy", None)
            .unwrap();
        overwrite_doc_record(
            &db,
            &legacy.id,
            json!({
                "schema_version": 1,
                "corpus": "notes",
                "kind": "markdown",
                "created_op": "legacy"
            }),
        );

        let raw = db.get_doc(&legacy.id).unwrap().unwrap();
        assert_eq!(raw.owner, unclaimed_owner());
        assert_eq!(raw.workspace_id, unclaimed_workspace());
        assert!(!raw.builtin);
        assert!(db.list_docs_scoped("alice", "home").unwrap().is_empty());
        assert!(db
            .get_doc_scoped(&legacy.id, "shared", "global")
            .unwrap()
            .is_none());
        assert!(db
            .write_doc_scoped(
                &legacy.id,
                "claimed by sentinel",
                None,
                &unclaimed_owner(),
                &unclaimed_workspace(),
            )
            .is_err());
    }

    #[test]
    fn explicit_private_v1_records_remain_readable() {
        let (db, _dir) = temp_db();
        let legacy = db
            .create_doc_scoped("alice", "home", "markdown", "legacy.md", "legacy", None)
            .unwrap();
        overwrite_doc_record(
            &db,
            &legacy.id,
            json!({
                "schema_version": 1,
                "corpus": "notes",
                "kind": "markdown",
                "created_op": "legacy",
                "owner": "alice",
                "workspace_id": "home"
            }),
        );

        let visible = db
            .get_doc_scoped(&legacy.id, "alice", "home")
            .unwrap()
            .unwrap();
        assert_eq!(visible.record_schema_version, 1);
        assert!(!visible.builtin);
    }

    #[test]
    fn builtin_claim_promotes_legacy_record_to_v2_without_changing_content() {
        let (db, _dir) = temp_db();
        let legacy = db
            .create_doc("note", "OpenClank/Legacy", "exact bundled body", None)
            .unwrap();
        overwrite_doc_record(
            &db,
            &legacy.id,
            json!({
                "schema_version": 1,
                "corpus": "notes",
                "kind": "note",
                "created_op": "legacy",
                "owner": "shared",
                "workspace_id": "global"
            }),
        );

        assert!(db.list_docs_scoped("alice", "home").unwrap().is_empty());
        let promoted = db.claim_builtin_seed_doc(&legacy.id).unwrap();
        assert_eq!(promoted.record_schema_version, 2);
        assert!(promoted.builtin);
        assert_eq!(promoted.head, legacy.head);
        assert_eq!(promoted.text.as_deref(), Some("exact bundled body"));
        assert_eq!(db.list_docs_scoped("alice", "home").unwrap().len(), 1);
    }

    #[test]
    fn ordinary_shared_records_and_imports_are_invisible_to_hosted_scopes() {
        let (db, _dir) = temp_db();
        db.create_doc("planning", "planning.json", "{}", None)
            .unwrap();
        db.create_doc("treehouse-state", "treehouse.json", "{}", None)
            .unwrap();

        let vault = std::env::temp_dir().join(format!("copal-unscoped-import-{}", new_ulid()));
        fs::create_dir_all(vault.join(".copal")).unwrap();
        fs::write(vault.join("Imported.md"), "ordinary import").unwrap();
        let planning = vault.join(".copal/planning.json");
        fs::write(&planning, r#"{"tracks":[]}"#).unwrap();
        fs::write(
            vault.join(".copal/treehouse-state.json"),
            r#"{"schemaVersion":1}"#,
        )
        .unwrap();
        db.import_vault(&vault, Some(&planning)).unwrap();

        let raw = db.list_docs().unwrap();
        assert!(raw.len() >= 5);
        assert!(raw
            .iter()
            .all(|doc| { doc.owner == "shared" && doc.workspace_id == "global" && !doc.builtin }));
        assert!(db.list_docs_scoped("alice", "home").unwrap().is_empty());
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn ordinary_unscoped_import_cannot_rewrite_a_builtin_seed() {
        let (db, _dir) = temp_db();
        let builtin = db
            .create_builtin_seed_doc("markdown", "Welcome.md", "bundled", None)
            .unwrap();
        let vault = std::env::temp_dir().join(format!("copal-seed-safe-import-{}", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        fs::write(vault.join("Welcome.md"), "ordinary import").unwrap();

        let stats = db.import_vault(&vault, None).unwrap();
        assert_eq!(stats.notes, 1);
        assert_eq!(
            db.get_doc(&builtin.id).unwrap().unwrap().text.as_deref(),
            Some("bundled")
        );
        let copies = db
            .list_docs()
            .unwrap()
            .into_iter()
            .filter(|doc| doc.name == "Welcome.md")
            .collect::<Vec<_>>();
        assert_eq!(copies.len(), 2);
        assert_eq!(copies.iter().filter(|doc| doc.builtin).count(), 1);
        assert_eq!(db.list_docs_scoped("alice", "home").unwrap().len(), 1);
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn builtin_seed_is_visible_and_shadowable_while_unrelated_shared_copy_is_not() {
        let (db, _dir) = temp_db();
        let unrelated = db
            .create_doc("note", "OpenClank/Start Here", "user shared copy", None)
            .unwrap();
        let builtin = db
            .create_builtin_seed_doc("note", "OpenClank/Start Here", "bundled copy", None)
            .unwrap();

        let alice = db.list_docs_scoped("alice", "home").unwrap();
        assert_eq!(alice.len(), 1);
        assert_eq!(alice[0].id, builtin.id);
        assert_ne!(alice[0].id, unrelated.id);

        let private = db
            .create_doc_scoped(
                "alice",
                "home",
                "note",
                "OpenClank/Start Here",
                "private shadow",
                None,
            )
            .unwrap();
        let alice = db.list_docs_scoped("alice", "home").unwrap();
        assert_eq!(alice.len(), 1);
        assert_eq!(alice[0].id, private.id);
    }

    #[test]
    fn private_documents_are_owner_and_workspace_scoped() {
        let (db, _dir) = temp_db();
        let alice = db
            .create_doc_scoped("alice", "home", "markdown", "private.md", "alice", None)
            .unwrap();
        let bob = db
            .create_doc_scoped("bob", "home", "markdown", "private.md", "bob", None)
            .unwrap();

        assert_eq!(db.list_docs_scoped("alice", "home").unwrap().len(), 1);
        assert_eq!(db.list_docs_scoped("bob", "home").unwrap().len(), 1);
        assert!(db
            .get_doc_scoped(&alice.id, "bob", "home")
            .unwrap()
            .is_none());
        assert!(db
            .write_doc_scoped(&bob.id, "clobber", None, "alice", "home")
            .is_err());
        assert_eq!(
            db.get_doc_scoped(&bob.id, "bob", "home")
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("bob")
        );

        let alice_ref = db
            .create_doc_scoped(
                "alice",
                "home",
                "markdown",
                "alice-ref.md",
                "[[private]] and ![[private.md#Details|the note]]",
                None,
            )
            .unwrap();
        let bob_ref = db
            .create_doc_scoped("bob", "home", "markdown", "bob-ref.md", "[[private]]", None)
            .unwrap();
        db.rename_doc_scoped(&alice.id, "alice-private.md", "alice", "home")
            .unwrap();
        assert_eq!(
            db.get_doc_scoped(&alice_ref.id, "alice", "home")
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("[[alice-private]] and ![[alice-private.md#Details|the note]]")
        );
        assert_eq!(
            db.get_doc_scoped(&bob_ref.id, "bob", "home")
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("[[private]]")
        );

        db.create_doc_scoped("bob", "home", "markdown", "bob-private.md", "bob", None)
            .unwrap();
        let renamed = db
            .rename_doc_scoped(&alice.id, "bob-private.md", "alice", "home")
            .unwrap();
        assert_eq!(renamed.name, "bob-private.md");
    }

    #[test]
    fn owner_rename_moves_live_and_deleted_documents_without_merging() {
        let (db, _dir) = temp_db();
        let visible = db
            .create_doc_scoped("alice", "home", "markdown", "visible.md", "old", None)
            .unwrap();
        let deleted = db
            .create_doc_scoped("alice", "home", "markdown", "deleted.md", "old", None)
            .unwrap();
        db.delete_doc_scoped(&deleted.id, "alice", "home").unwrap();
        db.create_doc_scoped("bob", "home", "markdown", "bob.md", "bob", None)
            .unwrap();

        assert!(db.preflight_rename_owner("alice", "bob").is_err());
        assert_eq!(db.list_docs_scoped("alice", "home").unwrap().len(), 1);
        assert_eq!(db.rename_owner("alice", "alice2").unwrap(), 2);
        assert!(db.list_docs_scoped("alice", "home").unwrap().is_empty());
        assert_eq!(db.list_docs_scoped("alice2", "home").unwrap().len(), 1);
        assert_eq!(
            db.list_deleted_docs_scoped("alice2", "home").unwrap().len(),
            1
        );
        assert_eq!(db.get_doc(&visible.id).unwrap().unwrap().owner, "alice2");
        assert!(db.get_doc(&deleted.id).unwrap().is_none());

        assert!(db.rename_owner("shared", "somebody").is_err());
        assert!(db.rename_owner("alice2", "shared").is_err());
    }

    #[test]
    fn owner_lifecycle_inventory_replays_compensates_and_is_content_free() {
        let (db, _dir) = temp_db();
        db.create_doc_scoped(
            "alice",
            "home",
            "markdown",
            "private-name.md",
            "ALICE SECRET BODY",
            None,
        )
        .unwrap();
        db.create_doc_scoped("bob", "home", "markdown", "bob.md", "BOB BODY", None)
            .unwrap();
        let bob_before = db.owner_inventory("bob").unwrap();
        let (source, target) = db.preflight_rename_owner("alice", "alice2").unwrap();
        let encoded = serde_json::to_string(&source).unwrap();
        assert!(!encoded.contains("ALICE SECRET BODY"));
        assert!(!encoded.contains("private-name.md"));

        let applied = db
            .reconcile_owner_rename("alice", "alice2", &source, &target)
            .unwrap();
        assert_eq!(applied["state"], "applied");
        assert_eq!(
            db.owner_inventory("bob").unwrap().fingerprint,
            bob_before.fingerprint
        );

        let replay = db
            .reconcile_owner_rename("alice", "alice2", &source, &target)
            .unwrap();
        assert_eq!(replay["state"], "already_applied");

        let compensated = db
            .compensate_owner_rename("alice", "alice2", &source, &target)
            .unwrap();
        assert_eq!(compensated["state"], "applied");
        assert!(db.owner_inventory("alice").unwrap().equivalent(&source));
        assert_eq!(db.owner_inventory("alice2").unwrap().documents, 0);

        let compensation_replay = db
            .compensate_owner_rename("alice", "alice2", &source, &target)
            .unwrap();
        assert_eq!(compensation_replay["state"], "already_applied");
    }

    #[test]
    fn owner_lifecycle_rejects_foreign_or_malformed_manifests_without_writes() {
        let (db, _dir) = temp_db();
        db.create_doc_scoped("alice", "home", "markdown", "private.md", "PRIVATE", None)
            .unwrap();
        let (source, target) = db.preflight_rename_owner("alice", "alice2").unwrap();
        let source_before = db.owner_inventory("alice").unwrap();

        let mut foreign_source = source.clone();
        foreign_source.owner = "mallory".to_string();
        assert!(db
            .reconcile_owner_rename("alice", "alice2", &foreign_source, &target)
            .is_err());

        let mut malformed_source = source.clone();
        malformed_source.active_documents += 1;
        assert!(db
            .reconcile_owner_rename("alice", "alice2", &malformed_source, &target)
            .is_err());

        let mut occupied_target = source.clone();
        occupied_target.owner = "alice2".to_string();
        assert!(db
            .reconcile_owner_rename("alice", "alice2", &source, &occupied_target)
            .is_err());

        let mut foreign_purge = source.clone();
        foreign_purge.owner = "mallory".to_string();
        assert!(db.purge_owner("alice", Some(&foreign_purge)).is_err());

        assert!(db
            .owner_inventory("alice")
            .unwrap()
            .equivalent(&source_before));
        assert_eq!(db.owner_inventory("alice2").unwrap().documents, 0);
    }

    #[test]
    fn owner_purge_removes_private_history_blobs_assets_and_operation_names() {
        let (db, data_dir) = temp_db();
        let original = db
            .create_doc_scoped(
                "alice",
                "home",
                "markdown",
                "AliceSecretName.md",
                "FIRST ALICE SECRET",
                None,
            )
            .unwrap();
        let first_hash = match &original.content {
            Content::Blob { hash } => hash.clone(),
            _ => panic!("text doc did not use a blob"),
        };
        let updated = db
            .write_doc_scoped(
                &original.id,
                "SECOND ALICE SECRET",
                Some(&original.head),
                "alice",
                "home",
            )
            .unwrap();
        let WriteOutcome::Committed { view: updated, .. } = updated else {
            panic!("expected update commit")
        };
        let second_hash = match &updated.content {
            Content::Blob { hash } => hash.clone(),
            _ => panic!("text doc did not use a blob"),
        };
        db.create_doc_scoped("bob", "home", "markdown", "bob.md", "BOB SURVIVES", None)
            .unwrap();
        let bob_before = db.owner_inventory("bob").unwrap();

        let vault = data_dir.join("alice-asset-vault");
        fs::create_dir_all(&vault).unwrap();
        fs::write(vault.join("private.bin"), b"ALICE PRIVATE ASSET").unwrap();
        db.import_vault_scoped(&vault, None, "alice", "home")
            .unwrap();
        let asset = db
            .list_docs_scoped("alice", "home")
            .unwrap()
            .into_iter()
            .find(|doc| matches!(doc.content, Content::Asset { .. }))
            .unwrap();
        let Content::Asset { hash, ext, .. } = asset.content else {
            unreachable!()
        };
        let asset_path = db.asset_file(&hash, &ext);
        assert!(asset_path.exists());

        let expected = db.owner_inventory("alice").unwrap();
        let receipt = db.purge_owner("alice", Some(&expected)).unwrap();
        assert_eq!(receipt["state"], "applied");
        assert_eq!(receipt["history_retained"], false);
        assert_eq!(db.owner_inventory("alice").unwrap().documents, 0);
        assert_eq!(
            db.owner_inventory("bob").unwrap().fingerprint,
            bob_before.fingerprint
        );
        assert!(db.blob_bytes(&first_hash).unwrap().is_none());
        assert!(db.blob_bytes(&second_hash).unwrap().is_none());
        assert!(!asset_path.exists());
        assert!(db.history(&original.id).is_err());
        let serialized = serde_json::to_string(&receipt).unwrap();
        assert!(!serialized.contains("ALICE SECRET"));

        let txn = db.database.begin_read().unwrap();
        let ops = txn.open_table(OPS).unwrap();
        for entry in ops.iter().unwrap() {
            let (_, encoded) = entry.unwrap();
            assert!(!encoded.value().contains("AliceSecretName"));
        }
        drop(ops);
        drop(txn);

        let replay = db.purge_owner("alice", Some(&expected)).unwrap();
        assert_eq!(replay["state"], "already_applied");
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn scoped_diff_rejects_commit_hashes_from_another_document() {
        let (db, _dir) = temp_db();
        let alice = db
            .create_doc_scoped("alice", "home", "markdown", "alice.md", "before", None)
            .unwrap();
        let updated = db
            .write_doc_scoped(&alice.id, "after", Some(&alice.head), "alice", "home")
            .unwrap();
        let WriteOutcome::Committed { view: alice, .. } = updated else {
            panic!()
        };
        let bob = db
            .create_doc_scoped("bob", "home", "markdown", "bob.md", "secret", None)
            .unwrap();

        assert!(db
            .diff_scoped(&alice.id, &alice.head, &alice.head, "alice", "home")
            .is_ok());
        let foreign = db
            .diff_scoped(&alice.id, &alice.head, &bob.head, "alice", "home")
            .unwrap_err()
            .to_string();
        assert_eq!(foreign, "commit not found in this scope");
        assert!(db
            .diff_scoped(&alice.id, &alice.head, &alice.head, "bob", "home")
            .is_err());
    }

    #[test]
    fn shared_documents_are_visible_but_read_only_to_scoped_clients() {
        let (db, _dir) = temp_db();
        let shared = db
            .create_builtin_seed_doc("note", "OpenClank/Start Here", "shared knowledge", None)
            .unwrap();

        assert!(shared.builtin);
        assert!(db
            .get_doc_scoped(&shared.id, "alice", "home")
            .unwrap()
            .is_some());
        assert!(db.history_scoped(&shared.id, "alice", "home").is_ok());
        assert!(db
            .write_doc_scoped(&shared.id, "changed", None, "alice", "home")
            .is_err());
        assert!(db
            .rename_doc_scoped(&shared.id, "Changed", "alice", "home")
            .is_err());
        assert!(db
            .checkpoint_scoped(&shared.id, Some("checkpoint"), "alice", "home")
            .is_err());
        assert!(db
            .restore_doc_scoped(&shared.id, &shared.head, "alice", "home")
            .is_err());
        assert!(db.delete_doc_scoped(&shared.id, "alice", "home").is_err());
        assert_eq!(
            db.get_doc(&shared.id).unwrap().unwrap().text.as_deref(),
            Some("shared knowledge")
        );
    }

    #[test]
    fn scoped_trash_restores_deleted_content() {
        let (db, _dir) = temp_db();
        let doc = db
            .create_doc_scoped("alice", "home", "markdown", "recover.md", "keep me", None)
            .unwrap();
        db.delete_doc_scoped(&doc.id, "alice", "home").unwrap();

        assert_eq!(
            db.list_deleted_docs_scoped("alice", "home").unwrap().len(),
            1
        );
        assert!(db
            .list_deleted_docs_scoped("bob", "home")
            .unwrap()
            .is_empty());

        let restored = db
            .restore_deleted_doc_scoped(&doc.id, "alice", "home")
            .unwrap();
        assert_eq!(restored.text.as_deref(), Some("keep me"));
        assert!(db
            .list_deleted_docs_scoped("alice", "home")
            .unwrap()
            .is_empty());
    }

    #[test]
    fn assets_are_files_with_history() {
        let (db, dir) = temp_db();
        let v1 = db.put_asset("img/pic.png", "png", b"AAAA").unwrap();
        let Content::Asset { hash: h1, .. } = v1.content.clone() else {
            panic!()
        };
        assert!(dir.join("assets").join(format!("{h1}.png")).is_file());

        let v2 = db.put_asset("img/pic.png", "png", b"BBBB").unwrap();
        let Content::Asset { hash: h2, .. } = v2.content.clone() else {
            panic!()
        };
        assert_ne!(h1, h2);
        // Old version stays on disk; history records the chain.
        assert!(dir.join("assets").join(format!("{h1}.png")).is_file());
        let history = db.history(&v1.id).unwrap();
        assert_eq!(history["changes"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn import_is_one_undoable_op() {
        let (db, _dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-vault-test-{}", new_ulid()));
        fs::create_dir_all(vault.join("Sub")).unwrap();
        fs::write(vault.join("Welcome.md"), "# Welcome\n").unwrap();
        fs::write(vault.join("Sub/Note.md"), "note\n").unwrap();
        fs::write(vault.join("pic.png"), b"PNG").unwrap();
        let planning = vault.join("move-data.json");
        fs::write(&planning, "{\"tracks\":[]}").unwrap();

        let stats = db.import_vault(&vault, Some(&planning)).unwrap();
        assert_eq!(stats.notes, 2);
        assert_eq!(stats.assets, 1);
        assert!(stats.planning);
        assert_eq!(db.list_docs().unwrap().len(), 4);

        // Re-import with no changes: nothing new.
        let stats = db.import_vault(&vault, Some(&planning)).unwrap();
        assert_eq!(stats.notes + stats.assets, 0);

        // Undo the import: everything it created disappears as one unit.
        // (Like jj, undoing twice would redo — so target the pre-import op.)
        let changed = db.undo(None).unwrap(); // undoes the no-op re-import
        assert!(changed.is_empty());
        db.undo(Some(&pre_import_op(&db))).unwrap();
        assert_eq!(db.list_docs().unwrap().len(), 0);
    }

    #[test]
    fn scoped_export_import_round_trips_planning_and_treehouse() {
        let (db, _dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-scoped-import-{}", new_ulid()));
        fs::create_dir_all(vault.join(".copal")).unwrap();
        fs::write(vault.join("Welcome.md"), "# Welcome\n").unwrap();
        let planning = vault.join(".copal/planning.json");
        fs::write(&planning, "{\"tracks\":[]}").unwrap();
        fs::write(
            vault.join(".copal/treehouse-state.json"),
            "{\"schemaVersion\":1}",
        )
        .unwrap();

        let stats = db
            .import_vault_scoped(&vault, Some(&planning), "alice", "school")
            .unwrap();
        assert_eq!(stats.notes, 1);
        assert!(stats.planning);
        assert!(stats.treehouse);
        let docs = db.list_docs_scoped("alice", "school").unwrap();
        assert_eq!(docs.len(), 3);
        assert!(docs.iter().any(|doc| doc.kind == "treehouse-state"));
        assert!(db.list_docs_scoped("bob", "school").unwrap().is_empty());
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn scoped_import_shadows_but_never_rewrites_shared_seed_documents() {
        let (db, _dir) = temp_db();
        let shared_note = db
            .create_builtin_seed_doc("markdown", "Welcome.md", "shared", None)
            .unwrap();
        let shared_planning = db
            .create_builtin_seed_doc(
                "planning",
                "move-data.json",
                "{\"tracks\":[\"shared\"]}",
                None,
            )
            .unwrap();
        let vault = std::env::temp_dir().join(format!("copal-overlay-import-{}", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        fs::write(vault.join("Welcome.md"), "private").unwrap();
        let planning = vault.join("planning.json");
        fs::write(&planning, "{\"tracks\":[\"private\"]}").unwrap();

        let stats = db
            .import_vault_scoped(&vault, Some(&planning), "alice", "school")
            .unwrap();
        assert_eq!(stats.notes, 1);
        assert!(stats.planning);
        let alice = db.list_docs_scoped("alice", "school").unwrap();
        assert_eq!(alice.len(), 2);
        assert_eq!(
            alice
                .iter()
                .find(|doc| doc.name == "Welcome.md")
                .unwrap()
                .text
                .as_deref(),
            Some("private")
        );
        assert_eq!(
            db.get_doc(&shared_note.id)
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("shared")
        );
        assert_eq!(
            db.get_doc(&shared_planning.id)
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("{\"tracks\":[\"shared\"]}")
        );
        assert_eq!(db.list_docs_scoped("bob", "school").unwrap().len(), 2);
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn canonical_wiki_import_preserves_every_file_and_reports_deterministically() {
        let (db, data_dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-complete-import-{}", new_ulid()));
        fs::create_dir_all(vault.join(".obsidian")).unwrap();
        fs::create_dir_all(vault.join("Media")).unwrap();
        let envelope = r##"{"schemaVersion":1,"body":{"type":"doc","blocks":[{"id":"blk_fixed","type":"heading","text":"Article","source":"# Article"}]},"properties":[],"relations":[],"extensions":{"interchange":{"format":"markdown","source":"# Article\n","projectionHash":"fixed","modified":false}}}"##;
        fs::write(vault.join("Article.md"), envelope).unwrap();
        fs::write(vault.join("Media/diagram.pdf"), b"PDF bytes").unwrap();
        fs::write(vault.join("Media/movie.mp4"), b"MP4 bytes").unwrap();
        fs::write(vault.join("opaque.xyz"), b"opaque bytes").unwrap();
        fs::write(
            vault.join(".obsidian/community-plugins.json"),
            br#"["dataview"]"#,
        )
        .unwrap();

        let stats = db
            .import_vault_scoped_as(&vault, None, "alice", "home", "wiki")
            .unwrap();
        assert_eq!(stats.notes, 1);
        assert_eq!(stats.assets, 3);
        assert_eq!(stats.compatibility, 1);
        assert_eq!(stats.unchanged, 0);
        assert_eq!(stats.entries.len(), 5);
        let paths = stats
            .entries
            .iter()
            .map(|entry| entry.path.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            paths,
            vec![
                ".obsidian/community-plugins.json",
                "Article.md",
                "Media/diagram.pdf",
                "Media/movie.mp4",
                "opaque.xyz",
            ]
        );
        let documents = db.list_docs_scoped("alice", "home").unwrap();
        assert_eq!(documents.len(), 5);
        assert_eq!(
            documents
                .iter()
                .find(|document| document.name == "Article.md")
                .unwrap()
                .corpus,
            "wiki"
        );
        assert!(documents.iter().all(|document| document.corpus == "wiki"));
        for (name, expected) in [
            ("Media/diagram.pdf", b"PDF bytes".as_slice()),
            ("Media/movie.mp4", b"MP4 bytes".as_slice()),
            ("opaque.xyz", b"opaque bytes".as_slice()),
            (
                ".obsidian/community-plugins.json",
                br#"["dataview"]"#.as_slice(),
            ),
        ] {
            let document = documents
                .iter()
                .find(|document| document.name == name)
                .unwrap();
            let Content::Asset { hash, ext, .. } = &document.content else {
                panic!("{name} was not preserved as an asset")
            };
            assert_eq!(
                fs::read(data_dir.join("assets").join(format!("{hash}.{ext}"))).unwrap(),
                expected
            );
        }

        let repeated = db
            .import_vault_scoped_as(&vault, None, "alice", "home", "wiki")
            .unwrap();
        assert_eq!(repeated.notes + repeated.assets + repeated.compatibility, 0);
        assert_eq!(repeated.unchanged, 5);
        assert!(repeated
            .entries
            .iter()
            .all(|entry| entry.status == "unchanged"));
        assert_eq!(db.list_docs_scoped("alice", "home").unwrap().len(), 5);
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn native_memes_paths_keep_extensionless_pages_raw_failures_and_assets() {
        let (db, data_dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-native-memes-{}", new_ulid()));
        fs::create_dir_all(vault.join(".copal/wiki/.memes")).unwrap();
        fs::write(
            vault.join(".copal/wiki/.memes/Native Page"),
            br#"{"schemaVersion":1,"futureField":{"keep":true}}"#,
        )
        .unwrap();
        fs::write(
            vault.join(".copal/wiki/.memes/Broken Page"),
            b"future native bytes",
        )
        .unwrap();
        fs::write(vault.join(".copal/wiki/.memes/photo.bin"), b"binary bytes").unwrap();
        let mut identities = BTreeMap::new();
        identities.insert(
            ".copal/wiki/.memes/Native Page".to_string(),
            ImportIdentity {
                id: "MEME-NATIVE".to_string(),
                corpus: "wiki".to_string(),
                kind: "wiki".to_string(),
            },
        );
        identities.insert(
            ".copal/wiki/.memes/Broken Page".to_string(),
            ImportIdentity {
                id: "MEME-BROKEN".to_string(),
                corpus: "wiki".to_string(),
                kind: "wiki".to_string(),
            },
        );
        identities.insert(
            ".copal/wiki/.memes/photo.bin".to_string(),
            ImportIdentity {
                id: "ASSET-NATIVE".to_string(),
                corpus: "wiki".to_string(),
                kind: "asset".to_string(),
            },
        );

        let stats = db
            .import_vault_scoped_as_with_ids(&vault, None, "alice", "home", "wiki", &identities)
            .unwrap();
        assert_eq!(stats.notes, 2);
        assert_eq!(stats.assets, 1);
        let documents = db.list_docs_scoped("alice", "home").unwrap();
        assert_eq!(documents.len(), 3);
        assert_eq!(
            documents
                .iter()
                .find(|doc| doc.id == "MEME-NATIVE")
                .unwrap()
                .name,
            ".memes/Native Page"
        );
        assert_eq!(
            documents
                .iter()
                .find(|doc| doc.id == "MEME-BROKEN")
                .unwrap()
                .text
                .as_deref(),
            Some("future native bytes")
        );
        let asset = documents
            .iter()
            .find(|doc| doc.id == "ASSET-NATIVE")
            .unwrap();
        assert_eq!(asset.name, ".memes/photo.bin");
        let Content::Asset { hash, ext, .. } = &asset.content else {
            panic!("native .memes asset was not an asset")
        };
        assert_eq!(
            fs::read(data_dir.join("assets").join(format!("{hash}.{ext}"))).unwrap(),
            b"binary bytes"
        );
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn native_memes_restore_head_guard_rejects_an_intervening_writer() {
        let (db, _data_dir) = temp_db();
        let page = db
            .create_doc_scoped("alice", "home", "wiki", "page", "base", None)
            .unwrap();
        let vault = std::env::temp_dir().join(format!("copal-native-memes-race-{}", new_ulid()));
        fs::create_dir_all(vault.join(".copal/wiki")).unwrap();
        fs::write(vault.join(".copal/wiki/page"), b"updated by one writer").unwrap();
        let identities = BTreeMap::from([(
            ".copal/wiki/page".to_string(),
            ImportIdentity {
                id: page.id.clone(),
                corpus: "wiki".to_string(),
                kind: "wiki".to_string(),
            },
        )]);
        let expected_heads = BTreeMap::from([(page.id.clone(), page.head.clone())]);
        let shared = Arc::new(db);
        let barrier = Arc::new(Barrier::new(2));
        let first_db = Arc::clone(&shared);
        let first_barrier = Arc::clone(&barrier);
        let first_vault = vault.clone();
        let first_identities = identities.clone();
        let first_heads = expected_heads.clone();
        let first = thread::spawn(move || {
            first_barrier.wait();
            first_db.import_vault_scoped_as_with_ids_and_heads(
                &first_vault,
                None,
                "alice",
                "home",
                "wiki",
                &first_identities,
                &first_heads,
            )
        });
        let second_db = Arc::clone(&shared);
        let second_barrier = Arc::clone(&barrier);
        let second_vault = vault.clone();
        let second_identities = identities.clone();
        let second_heads = expected_heads.clone();
        let second = thread::spawn(move || {
            second_barrier.wait();
            second_db.import_vault_scoped_as_with_ids_and_heads(
                &second_vault,
                None,
                "alice",
                "home",
                "wiki",
                &second_identities,
                &second_heads,
            )
        });
        let results = [first.join().unwrap(), second.join().unwrap()];
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(results.iter().filter(|result| result.is_err()).count(), 1);
        assert!(results.iter().any(|result| {
            result
                .as_ref()
                .err()
                .is_some_and(|error| error.to_string().contains("stale restore head"))
        }));
        assert_eq!(
            shared.get_doc(&page.id).unwrap().unwrap().text.as_deref(),
            Some("updated by one writer")
        );
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn native_memes_portable_path_guard_rejects_an_intervening_cross_kind_writer() {
        let (db, _data_dir) = temp_db();
        let page_vault =
            std::env::temp_dir().join(format!("copal-native-memes-page-{}", new_ulid()));
        let asset_vault =
            std::env::temp_dir().join(format!("copal-native-memes-asset-{}", new_ulid()));
        fs::create_dir_all(page_vault.join(".copal/wiki")).unwrap();
        fs::create_dir_all(asset_vault.join(".copal/wiki")).unwrap();
        fs::write(
            page_vault.join(".copal/wiki/Cafe\u{301} Straße"),
            b"wiki page",
        )
        .unwrap();
        fs::write(
            asset_vault.join(".copal/wiki/CAF\u{00c9} STRASSE"),
            b"binary asset",
        )
        .unwrap();
        let page_identities = BTreeMap::from([(
            ".copal/wiki/Cafe\u{301} Straße".to_string(),
            ImportIdentity {
                id: "PAGE-RACE".to_string(),
                corpus: "wiki".to_string(),
                kind: "wiki".to_string(),
            },
        )]);
        let asset_identities = BTreeMap::from([(
            ".copal/wiki/CAF\u{00c9} STRASSE".to_string(),
            ImportIdentity {
                id: "ASSET-RACE".to_string(),
                corpus: "wiki".to_string(),
                kind: "asset".to_string(),
            },
        )]);
        let shared = Arc::new(db);
        let barrier = Arc::new(Barrier::new(2));
        let page_db = Arc::clone(&shared);
        let page_barrier = Arc::clone(&barrier);
        let page_root = page_vault.clone();
        let page_map = page_identities.clone();
        let page_thread = thread::spawn(move || {
            page_barrier.wait();
            page_db.import_vault_scoped_as_with_ids(
                &page_root, None, "alice", "home", "wiki", &page_map,
            )
        });
        let asset_db = Arc::clone(&shared);
        let asset_barrier = Arc::clone(&barrier);
        let asset_root = asset_vault.clone();
        let asset_map = asset_identities.clone();
        let asset_thread = thread::spawn(move || {
            asset_barrier.wait();
            asset_db.import_vault_scoped_as_with_ids(
                &asset_root,
                None,
                "alice",
                "home",
                "wiki",
                &asset_map,
            )
        });
        let results = [page_thread.join().unwrap(), asset_thread.join().unwrap()];
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(results.iter().filter(|result| result.is_err()).count(), 1);
        assert!(results.iter().any(|result| {
            result
                .as_ref()
                .err()
                .is_some_and(|error| error.to_string().contains("portable path"))
        }));
        assert_eq!(shared.list_docs_scoped("alice", "home").unwrap().len(), 1);
        fs::remove_dir_all(page_vault).unwrap();
        fs::remove_dir_all(asset_vault).unwrap();
    }

    #[test]
    fn copal_export_restore_keeps_stable_ids_and_rejects_identity_conflicts() {
        let (db, _data_dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-identity-import-{}", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        let envelope = r##"{"schemaVersion":1,"body":{"type":"doc","blocks":[]},"properties":[],"relations":[],"extensions":{}}"##;
        fs::write(vault.join("Stable.md"), envelope).unwrap();
        let stable_id = new_ulid();
        let identities = BTreeMap::from([(
            "Stable.md".to_string(),
            ImportIdentity {
                id: stable_id.clone(),
                corpus: "notes".to_string(),
                kind: "note".to_string(),
            },
        )]);

        let first = db
            .import_vault_scoped_as_with_ids(&vault, None, "alice", "home", "note", &identities)
            .unwrap();
        assert_eq!(first.restored_identities, 1);
        assert_eq!(
            db.list_docs_scoped("alice", "home").unwrap()[0].id,
            stable_id
        );

        let second = db
            .import_vault_scoped_as_with_ids(&vault, None, "alice", "home", "note", &identities)
            .unwrap();
        assert_eq!(second.unchanged, 1);
        assert_eq!(second.restored_identities, 1);

        let conflicting = BTreeMap::from([(
            "Stable.md".to_string(),
            ImportIdentity {
                id: new_ulid(),
                corpus: "notes".to_string(),
                kind: "note".to_string(),
            },
        )]);
        assert!(db
            .import_vault_scoped_as_with_ids(&vault, None, "alice", "home", "note", &conflicting,)
            .unwrap_err()
            .to_string()
            .contains("conflicts"));
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn note_and_wiki_corpora_can_hold_the_same_scoped_name() {
        let (db, _dir) = temp_db();
        db.create_doc_scoped("alice", "home", "note", "Same.md", "note", None)
            .unwrap();
        db.create_doc_scoped("alice", "home", "wiki", "Same.md", "wiki", None)
            .unwrap();

        let documents = db.list_docs_scoped("alice", "home").unwrap();
        assert_eq!(documents.len(), 2);
        assert!(documents
            .iter()
            .any(|document| document.kind == "note" && document.corpus == "notes"));
        assert!(documents
            .iter()
            .any(|document| document.kind == "wiki" && document.corpus == "wiki"));
    }

    #[test]
    fn mixed_export_layout_reimports_note_and_wiki_records_and_assets_separately() {
        let (db, data_dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-mixed-corpus-{}", new_ulid()));
        fs::create_dir_all(vault.join(".copal/wiki")).unwrap();
        let note = r#"{"schemaVersion":1,"body":{"type":"doc","blocks":[]},"properties":[],"relations":[]}"#;
        fs::write(vault.join("Same.md"), note).unwrap();
        fs::write(vault.join(".copal/wiki/Same.md"), note).unwrap();
        fs::write(vault.join("same.bin"), b"notes asset").unwrap();
        fs::write(vault.join(".copal/wiki/same.bin"), b"wiki asset").unwrap();

        let stats = db
            .import_vault_scoped_as(&vault, None, "alice", "home", "note")
            .unwrap();
        assert_eq!(stats.notes, 2);
        assert_eq!(stats.assets, 2);
        assert_eq!(stats.entries.len(), 4);
        let documents = db.list_docs_scoped("alice", "home").unwrap();
        let same_notes = documents
            .iter()
            .filter(|document| document.name == "Same.md")
            .collect::<Vec<_>>();
        assert_eq!(same_notes.len(), 2);
        assert!(same_notes
            .iter()
            .any(|document| document.kind == "note" && document.corpus == "notes"));
        assert!(same_notes
            .iter()
            .any(|document| document.kind == "wiki" && document.corpus == "wiki"));
        let same_assets = documents
            .iter()
            .filter(|document| document.name == "same.bin")
            .collect::<Vec<_>>();
        assert_eq!(same_assets.len(), 2);
        for (corpus, expected) in [
            ("notes", b"notes asset".as_slice()),
            ("wiki", b"wiki asset".as_slice()),
        ] {
            let asset = same_assets
                .iter()
                .find(|document| document.corpus == corpus)
                .unwrap();
            let Content::Asset { hash, ext, .. } = &asset.content else {
                panic!()
            };
            assert_eq!(
                fs::read(data_dir.join("assets").join(format!("{hash}.{ext}"))).unwrap(),
                expected
            );
        }

        let repeated = db
            .import_vault_scoped_as(&vault, None, "alice", "home", "note")
            .unwrap();
        assert_eq!(repeated.unchanged, 4);
        assert_eq!(repeated.notes + repeated.assets + repeated.compatibility, 0);
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn reserved_event_namespace_restores_supported_records_and_preserves_future_data() {
        let (db, _dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-events-import-{}", new_ulid()));
        fs::create_dir_all(vault.join(".events")).unwrap();
        fs::create_dir_all(vault.join(".copal")).unwrap();
        fs::write(
            vault.join(".events/current.md"),
            "---\ncopal_type: \"event\"\ncopal_schema: 1\ntitle: \"Current\"\n---\nbody\n",
        )
        .unwrap();
        fs::write(
            vault.join(".events/future.md"),
            "---\ncopal_type: \"event\"\ncopal_schema: 2\ntitle: \"Future\"\n---\nbody\n",
        )
        .unwrap();
        fs::write(
            vault.join(".copal/tracks.json"),
            r#"{"schemaVersion":1,"tracks":[]}"#,
        )
        .unwrap();
        fs::write(
            vault.join(".copal/planning-migration.json"),
            r#"{"schemaVersion":9,"state":"future"}"#,
        )
        .unwrap();

        let stats = db
            .import_vault_scoped_as(&vault, None, "alice", "home", "note")
            .unwrap();

        assert_eq!(stats.notes, 2);
        assert_eq!(stats.compatibility, 2);
        let documents = db.list_docs_scoped("alice", "home").unwrap();
        assert!(documents.iter().any(|document| {
            document.kind == "copal-event"
                && document.corpus == "events"
                && document.name == ".events/current.md"
                && document.hidden
        }));
        assert!(documents.iter().any(|document| {
            document.kind == "copal-tracks"
                && document.corpus == "events"
                && document.name == ".copal/tracks.json"
        }));
        assert!(documents.iter().any(|document| {
            document.kind == "compatibility"
                && document.corpus == "events"
                && document.name == ".events/future.md"
        }));
        assert!(documents.iter().any(|document| {
            document.kind == "compatibility"
                && document.corpus == "events"
                && document.name == ".copal/planning-migration.json"
        }));
        let repeated = db
            .import_vault_scoped_as(&vault, None, "alice", "home", "note")
            .unwrap();
        assert_eq!(repeated.unchanged, 4);
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn reserved_json_versions_are_gated_per_record_kind() {
        for (label, relative, content, expected_kind) in [
            (
                "tracks-v1",
                ".copal/tracks.json",
                br#"{"schemaVersion":1,"tracks":[]}"#.as_slice(),
                "copal-tracks",
            ),
            (
                "tracks-v2",
                ".copal/tracks.json",
                br#"{"schemaVersion":2,"tracks":[]}"#.as_slice(),
                "copal-tracks",
            ),
            (
                "tracks-v3",
                ".copal/tracks.json",
                br#"{"schemaVersion":3,"tracks":[]}"#.as_slice(),
                "compatibility",
            ),
            (
                "tracks-malformed",
                ".copal/tracks.json",
                b"{not-json".as_slice(),
                "compatibility",
            ),
            (
                "migration-v1",
                ".copal/planning-migration.json",
                br#"{"schemaVersion":1,"state":"complete"}"#.as_slice(),
                "copal-migration",
            ),
            (
                "migration-v2",
                ".copal/planning-migration.json",
                br#"{"schemaVersion":2,"state":"future"}"#.as_slice(),
                "compatibility",
            ),
        ] {
            let (db, data_dir) = temp_db();
            let vault = std::env::temp_dir().join(format!("copal-{label}-{}", new_ulid()));
            fs::create_dir_all(vault.join(".copal")).unwrap();
            fs::write(vault.join(relative), content).unwrap();

            let stats = db
                .import_vault_scoped_as(&vault, None, "alice", "home", "note")
                .unwrap();
            let document = db.list_docs_scoped("alice", "home").unwrap().pop().unwrap();

            assert_eq!(document.kind, expected_kind, "{label}");
            assert_eq!(
                stats.notes,
                usize::from(expected_kind != "compatibility"),
                "{label}"
            );
            assert_eq!(
                stats.compatibility,
                usize::from(expected_kind == "compatibility"),
                "{label}"
            );
            if expected_kind == "compatibility" {
                let Content::Asset { hash, ext, .. } = &document.content else {
                    panic!("{label} was not preserved as inert bytes")
                };
                assert_eq!(
                    fs::read(data_dir.join("assets").join(format!("{hash}.{ext}"))).unwrap(),
                    content,
                    "{label}"
                );
            }

            let repeated = db
                .import_vault_scoped_as(&vault, None, "alice", "home", "note")
                .unwrap();
            assert_eq!(repeated.unchanged, 1, "{label}");
            fs::remove_dir_all(vault).unwrap();
            drop(db);
            fs::remove_dir_all(data_dir).unwrap();
        }
    }

    #[test]
    fn record_contract_exposes_schema_hidden_and_deleted_state() {
        let (db, _dir) = temp_db();
        assert_eq!(db.schema_version().unwrap(), 3);
        let event = db
            .create_doc_scoped(
                "alice",
                "home",
                "copal-event",
                ".events/move.md",
                "event",
                None,
            )
            .unwrap();
        assert_eq!(event.record_schema_version, 2);
        assert!(!event.builtin);
        assert_eq!(event.corpus, "events");
        assert!(event.hidden);
        assert!(!event.deleted);

        db.delete_doc_scoped(&event.id, "alice", "home").unwrap();
        let deleted = db
            .list_deleted_docs_scoped("alice", "home")
            .unwrap()
            .pop()
            .unwrap();
        assert!(deleted.hidden);
        assert!(deleted.deleted);
    }

    #[test]
    fn asset_updates_do_not_overwrite_an_unlike_same_named_document() {
        let (db, _dir) = temp_db();
        let note = db
            .create_doc("note", "same.bin", "note payload", None)
            .unwrap();
        let asset = db.put_asset("same.bin", "bin", b"asset payload").unwrap();

        assert_ne!(note.id, asset.id);
        assert_eq!(asset.kind, "asset");
        assert_eq!(
            db.get_doc(&note.id).unwrap().unwrap().text.as_deref(),
            Some("note payload")
        );
    }

    #[test]
    fn unprepared_canonical_markdown_is_preserved_as_compatibility_data() {
        let (db, data_dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-unprepared-import-{}", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        fs::write(vault.join("Too-Large.md"), b"# preserved raw\n").unwrap();

        let stats = db
            .import_vault_scoped_as(&vault, None, "alice", "home", "note")
            .unwrap();
        assert_eq!(stats.notes, 0);
        assert_eq!(stats.compatibility, 1);
        let document = db.list_docs_scoped("alice", "home").unwrap().pop().unwrap();
        assert_eq!(document.kind, "compatibility");
        let Content::Asset { hash, ext, .. } = document.content else {
            panic!()
        };
        assert_eq!(
            fs::read(data_dir.join("assets").join(format!("{hash}.{ext}"))).unwrap(),
            b"# preserved raw\n"
        );
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn planning_file_outside_import_root_is_rejected_without_database_writes() {
        let (db, _dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-root-import-{}", new_ulid()));
        let external = std::env::temp_dir().join(format!("copal-external-{}.json", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        fs::write(vault.join("Note.md"), "note").unwrap();
        fs::write(&external, "{}").unwrap();

        assert!(db.import_vault(&vault, Some(&external)).is_err());
        assert!(db.list_docs().unwrap().is_empty());
        fs::remove_file(external).unwrap();
        fs::remove_dir_all(vault).unwrap();
    }

    #[test]
    fn invalid_planning_rolls_back_files_processed_in_the_same_import() {
        let (db, _dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-atomic-import-{}", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        fs::write(vault.join("Note.md"), "note payload").unwrap();
        let planning = vault.join("planning.json");
        fs::write(&planning, "{invalid").unwrap();

        assert!(db.import_vault(&vault, Some(&planning)).is_err());
        assert!(db.list_docs().unwrap().is_empty());
        fs::write(&planning, "{\"tracks\":[]}").unwrap();
        let recovered = db.import_vault(&vault, Some(&planning)).unwrap();
        assert_eq!(recovered.notes, 1);
        assert!(recovered.planning);
        assert_eq!(db.list_docs().unwrap().len(), 2);
        fs::remove_dir_all(vault).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn import_rejects_symbolic_links_without_database_writes() {
        use std::os::unix::fs::symlink;

        let (db, _dir) = temp_db();
        let vault = std::env::temp_dir().join(format!("copal-symlink-import-{}", new_ulid()));
        let external = std::env::temp_dir().join(format!("copal-symlink-target-{}", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        fs::write(&external, "outside").unwrap();
        symlink(&external, vault.join("linked.md")).unwrap();

        assert!(db.import_vault(&vault, None).is_err());
        assert!(db.list_docs().unwrap().is_empty());
        fs::remove_dir_all(vault).unwrap();
        fs::remove_file(external).unwrap();
    }

    /// Oldest op with an empty view — the state before the import.
    fn pre_import_op(db: &Db) -> String {
        let ops = db.ops(100, None).unwrap();
        let list = ops["ops"].as_array().unwrap().clone();
        list.iter()
            .rev()
            .find(|op| op["docs"].as_u64() == Some(0))
            .map(|op| op["op"].as_str().unwrap().to_string())
            .unwrap()
    }

    #[test]
    fn resolver_honors_debug_bit_and_env() {
        let root = std::env::temp_dir().join(format!("copal-resolver-test-{}", new_ulid()));
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("copal.toml"), "debug = true\n").unwrap();
        assert_eq!(resolve_data_dir(&root), root.join("db"));
        fs::write(root.join("copal.toml"), "# nothing\ndebug = false\n").unwrap();
        assert!(resolve_data_dir(&root).ends_with("copal"));
    }

    #[test]
    fn schema_upgrade_is_recorded_without_changing_document_heads() {
        let (db, data_dir) = temp_db();
        let note = db.create_doc("note", "Upgrade", "payload", None).unwrap();
        {
            let txn = db.database.begin_write().unwrap();
            txn.open_table(META)
                .unwrap()
                .insert(SCHEMA_VERSION_KEY, "2")
                .unwrap();
            txn.commit().unwrap();
        }
        drop(db);

        let upgraded = Db::open(&data_dir).unwrap();

        assert_eq!(upgraded.schema_version().unwrap(), 3);
        assert_eq!(upgraded.get_doc(&note.id).unwrap().unwrap().head, note.head);
        let operations = upgraded.ops(1, None).unwrap();
        assert_eq!(operations["ops"][0]["kind"], "schema-upgrade");
        assert!(operations["ops"][0]["description"]
            .as_str()
            .unwrap()
            .contains("2 to 3"));
    }

    #[test]
    fn future_database_schema_is_rejected_without_downgrade() {
        let (db, data_dir) = temp_db();
        {
            let txn = db.database.begin_write().unwrap();
            txn.open_table(META)
                .unwrap()
                .insert(SCHEMA_VERSION_KEY, "99")
                .unwrap();
            txn.commit().unwrap();
        }
        drop(db);

        let error = Db::open(&data_dir).err().unwrap();

        assert!(error.to_string().contains("newer than supported schema 3"));
        let database = Database::open(data_dir.join("copal.redb")).unwrap();
        let txn = database.begin_read().unwrap();
        assert_eq!(
            txn.open_table(META)
                .unwrap()
                .get(SCHEMA_VERSION_KEY)
                .unwrap()
                .unwrap()
                .value(),
            "99"
        );
    }

    #[test]
    fn current_schema_repairs_missing_task_tables_without_touching_canonical_heads() {
        for missing in ["task_index", "task_index_resources"] {
            let (db, data_dir) = temp_db();
            let note = db.create_doc("note", "Repair", "payload", None).unwrap();
            let operation_count = db.ops(100, None).unwrap()["ops"].as_array().unwrap().len();
            {
                let txn = db.database.begin_write().unwrap();
                if missing == "task_index" {
                    txn.delete_table(TASK_INDEX).unwrap();
                } else {
                    txn.delete_table(TASK_INDEX_RESOURCES).unwrap();
                }
                txn.commit().unwrap();
            }
            drop(db);

            let repaired = Db::open(&data_dir).unwrap();
            assert_eq!(repaired.schema_version().unwrap(), SCHEMA_VERSION);
            assert_eq!(repaired.get_doc(&note.id).unwrap().unwrap().head, note.head);
            assert_eq!(repaired.task_index_generation("alice", "home").unwrap()["sourceRevision"], "");
            assert_eq!(repaired.ops(100, None).unwrap()["ops"].as_array().unwrap().len(), operation_count);
            drop(repaired);
            fs::remove_dir_all(data_dir).unwrap();
        }
    }

    #[test]
    fn existing_store_missing_schema_marker_or_canonical_table_is_rejected() {
        let (db, data_dir) = temp_db();
        {
            let txn = db.database.begin_write().unwrap();
            txn.open_table(META).unwrap().remove(SCHEMA_VERSION_KEY).unwrap();
            txn.commit().unwrap();
        }
        drop(db);
        let marker_error = match Db::open(&data_dir) {
            Ok(_) => panic!("opening a non-empty store without a schema marker must fail"),
            Err(error) => error,
        };
        assert!(marker_error.to_string().contains("schema marker is missing or invalid"));
        fs::remove_dir_all(&data_dir).unwrap();

        let (db, data_dir) = temp_db();
        {
            let txn = db.database.begin_write().unwrap();
            txn.delete_table(DOCS).unwrap();
            txn.commit().unwrap();
        }
        drop(db);
        let canonical_error = match Db::open(&data_dir) {
            Ok(_) => panic!("opening a store without canonical docs must fail"),
            Err(error) => error,
        };
        assert!(canonical_error.to_string().contains("canonical source table 'docs' is missing or corrupt"));
        fs::remove_dir_all(data_dir).unwrap();
    }

    #[test]
    fn keyed_task_index_pages_all_5000_rows_without_skips_and_tracks_writes() {
        let (db, data_dir) = temp_db();
        let items = (0..5000)
            .map(|index| {
                json!({
                    "id": format!("DOC:{index:04}"),
                    "source": if index % 2 == 0 { "vault" } else { "markdown" },
                    "label": format!("Task {index:04}"),
                    "text": format!("Task {index:04}"),
                    "checked": index % 3 == 0,
                })
            })
            .collect::<Vec<_>>();
        let records = json!({"DOC": {"head": "h1", "items": items}});
        let rebuilt = db
            .task_index_update("alice", "home", "g1", &records, &[], true, 1)
            .unwrap();
        assert_eq!(rebuilt["sourceReads"], 1);
        assert!(rebuilt["rewrittenRows"].as_u64().unwrap() >= 5002);
        assert!(rebuilt["rewrittenBytes"].as_u64().unwrap() > 100_000);

        let mut cursor = None;
        let mut seen = BTreeSet::new();
        loop {
            let page = db
                .task_index_page("alice", "home", "", None, "all", cursor.as_deref(), 100, "g1")
                .unwrap();
            for item in page["items"].as_array().unwrap() {
                assert!(seen.insert(item["id"].as_str().unwrap().to_string()));
            }
            cursor = page["nextCursor"].as_str().map(ToString::to_string);
            if cursor.is_none() {
                break;
            }
        }
        assert_eq!(seen.len(), 5000);

        let markdown = db
            .task_index_page("alice", "home", "", None, "markdown", None, 100, "g1")
            .unwrap();
        assert!(markdown["items"].as_array().unwrap().iter().all(|item| item["source"] == "markdown"));
        assert_eq!(markdown["total"], 2500);
        assert_eq!(markdown["indexedTotal"], 5000);
        assert_eq!(markdown["matchedTotal"], 2500);
        assert!(markdown["scannedRows"].as_u64().unwrap() > markdown["returnedRows"].as_u64().unwrap());

        assert!(db
            .task_index_page("alice", "home", "", None, "all", Some("missing-anchor"), 100, "g1")
            .is_err());
        db.task_index_update("alice", "home", "g2", &json!({}), &["DOC".to_string()], false, 1)
            .unwrap();
        let after_delete = db
            .task_index_page("alice", "home", "", None, "all", None, 10, "g2")
            .unwrap();
        assert!(after_delete["items"].as_array().unwrap().is_empty());
        drop(db);
        let restarted = Db::open(&data_dir).unwrap();
        assert_eq!(restarted.task_index_generation("alice", "home").unwrap()["total"], 0);
        fs::remove_dir_all(data_dir).unwrap();
    }

    #[test]
    fn keyed_task_index_changes_one_of_5000_documents_without_corpus_rebuild() {
        let (db, data_dir) = temp_db();
        let records = (0..5000)
            .map(|index| {
                (
                    format!("DOC:{index:04}"),
                    json!({
                        "head": format!("h{index}"),
                        "items": [{
                            "id": format!("DOC:{index:04}:1"),
                            "source": "markdown",
                            "label": format!("Task {index:04}"),
                            "text": format!("Task {index:04}"),
                            "checked": false,
                        }],
                    }),
                )
            })
            .collect::<serde_json::Map<_, _>>();
        let rebuilt = db
            .task_index_update("alice", "home", "g1", &Value::Object(records), &[], true, 5000)
            .unwrap();
        assert_eq!(rebuilt["sourceReads"], 5000);
        let warm = db
            .task_index_page("alice", "home", "", None, "all", None, 100, "g1")
            .unwrap();
        assert_eq!(warm["returnedRows"], 100);
        assert!(warm["scannedRows"].as_u64().unwrap() <= 101);

        let changed = json!({
            "DOC:2500": {
                "head": "h2500-next",
                "items": [{
                    "id": "DOC:2500:1",
                    "source": "markdown",
                    "label": "Task 2500",
                    "text": "Task 2500",
                    "checked": true,
                }],
            },
        });
        let patched = db
            .task_index_update("alice", "home", "g2", &changed, &[], false, 1)
            .unwrap();
        assert_eq!(patched["sourceReads"], 1);
        assert!(patched["rewrittenRows"].as_u64().unwrap() <= 7);
        assert!(patched["rewrittenBytes"].as_u64().unwrap() < 2000);
        let after = db
            .task_index_page("alice", "home", "2500", None, "all", None, 10, "g2")
            .unwrap();
        assert_eq!(after["items"][0]["checked"], true);
        assert_eq!(db.task_index_generation("alice", "home").unwrap()["sourceRevision"], "g2");
        drop(db);
        let restarted = Db::open(&data_dir).unwrap();
        assert_eq!(restarted.task_index_generation("alice", "home").unwrap()["total"], 5000);
        fs::remove_dir_all(data_dir).unwrap();
    }
}
