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
//! See `.futures/copal-jj-db-source-of-truth-metaplan.md` for the full plan.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use redb::{Database, ReadableTable, TableDefinition};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const DOCS: TableDefinition<&str, &str> = TableDefinition::new("docs");
const COMMITS: TableDefinition<&str, &str> = TableDefinition::new("commits");
const BLOBS: TableDefinition<&str, &[u8]> = TableDefinition::new("blobs");
const OPS: TableDefinition<&str, &str> = TableDefinition::new("ops");
const META: TableDefinition<&str, &str> = TableDefinition::new("meta");

const OP_HEAD_KEY: &str = "op_head";
const SCHEMA_VERSION_KEY: &str = "schema_version";
pub const SCHEMA_VERSION: u64 = 2;

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

// ── Records ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DocRecord {
    pub kind: String,
    pub created_op: String,
    #[serde(default = "shared_owner")]
    pub owner: String,
    #[serde(default = "global_workspace")]
    pub workspace_id: String,
}

fn shared_owner() -> String {
    "shared".to_string()
}

fn global_workspace() -> String {
    "global".to_string()
}

/// Commit content. `Conflict` is reserved (jj first-class conflicts); v1
/// never constructs it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case")]
pub enum Content {
    Blob { hash: String },
    Asset { hash: String, ext: String, size: u64 },
    Conflict { base: Option<String>, sides: Vec<String> },
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

#[derive(Debug, Clone, Serialize)]
pub struct DocView {
    pub id: String,
    pub kind: String,
    pub owner: String,
    pub workspace_id: String,
    pub name: String,
    pub head: String,
    pub ts: u64,
    pub content: Content,
    /// UTF-8 text for Blob content; None for assets/tombstones.
    pub text: Option<String>,
}

#[derive(Debug)]
pub enum WriteOutcome {
    Committed { view: DocView, new_change: bool },
    Unchanged { view: DocView },
    /// baseCommit didn't match the head: nothing was written; caller rebases
    /// onto the returned authoritative view.
    Stale { view: DocView },
}

#[derive(Debug, Default, Serialize)]
pub struct ImportStats {
    pub notes: usize,
    pub assets: usize,
    pub planning: bool,
    pub treehouse: bool,
    pub op: String,
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
        _ => {},
    }
    let Ok(text) = fs::read_to_string(root.join("copal.toml")) else {
        return false;
    };
    for line in text.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if let Some(value) = line.strip_prefix("debug") {
            return value.trim_start().strip_prefix('=').is_some_and(|v| v.trim() == "true");
        }
    }
    false
}

fn xdg_data_home() -> PathBuf {
    if let Some(dir) = std::env::var_os("XDG_DATA_HOME") {
        return PathBuf::from(dir);
    }
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    home.join(".local").join("share")
}

// ── Engine ───────────────────────────────────────────────────────────────

pub struct Db {
    database: Database,
    assets_dir: PathBuf,
}

impl Db {
    /// Open (creating if needed) the database at `<data_dir>/copal.redb`
    /// with assets beside it in `<data_dir>/assets/`.
    pub fn open(data_dir: &Path) -> Result<Self> {
        fs::create_dir_all(data_dir)?;
        let assets_dir = data_dir.join("assets");
        fs::create_dir_all(&assets_dir)?;
        let database_path = data_dir.join("copal.redb");
        let database = if database_path.exists() {
            Database::open(&database_path)?
        } else {
            Database::create(&database_path)?
        };
        // Ensure all tables exist so read transactions never hit
        // TableDoesNotExist on a fresh file, and record the root `init`
        // operation (jj's virtual root op) so there is always an op to
        // restore back to.
        let current_version = database
            .begin_read()
            .ok()
            .and_then(|txn| {
                txn.open_table(META)
                    .ok()
                    .and_then(|table| table.get(SCHEMA_VERSION_KEY).ok().flatten().map(|value| value.value().to_string()))
            });
        let target_version = SCHEMA_VERSION.to_string();
        if current_version.as_deref() != Some(target_version.as_str()) {
            let txn = database.begin_write()?;
            {
                txn.open_table(DOCS)?;
                txn.open_table(COMMITS)?;
                txn.open_table(BLOBS)?;
                txn.open_table(OPS)?;
                let needs_init = txn.open_table(META)?.get(OP_HEAD_KEY)?.is_none();
                if needs_init {
                    put_op(&txn, None, "init", "initialize repository", &BTreeMap::new())?;
                }
                txn.open_table(META)?.insert(SCHEMA_VERSION_KEY, target_version.as_str())?;
            }
            txn.commit()?;
        }
        Ok(Self { database, assets_dir })
    }

    pub fn assets_dir(&self) -> &Path {
        &self.assets_dir
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
        let record = ops.get(head.value())?.ok_or_else(|| err("op head points at missing op"))?;
        let op: OpRecord = serde_json::from_str(record.value())?;
        Ok(op.view)
    }

    fn load_commit(&self, txn: &redb::ReadTransaction, id: &str) -> Result<CommitRecord> {
        let commits = txn.open_table(COMMITS)?;
        let record = commits.get(id)?.ok_or_else(|| err(format!("missing commit {id}")))?;
        Ok(serde_json::from_str(record.value())?)
    }

    fn doc_record(&self, txn: &redb::ReadTransaction, id: &str) -> Result<DocRecord> {
        let docs = txn.open_table(DOCS)?;
        let record = docs.get(id)?.ok_or_else(|| err(format!("missing doc {id}")))?;
        Ok(serde_json::from_str(record.value())?)
    }

    fn view_of(&self, txn: &redb::ReadTransaction, id: &str, head: &str) -> Result<DocView> {
        let commit = self.load_commit(txn, head)?;
        let text = match &commit.content {
            Content::Blob { hash } => {
                let blobs = txn.open_table(BLOBS)?;
                let bytes = blobs.get(hash.as_str())?.ok_or_else(|| err(format!("missing blob {hash}")))?;
                Some(String::from_utf8_lossy(bytes.value()).into_owned())
            },
            _ => None,
        };
        let doc = self.doc_record(txn, id)?;
        Ok(DocView {
            id: id.to_string(),
            kind: doc.kind,
            owner: doc.owner,
            workspace_id: doc.workspace_id,
            name: commit.name,
            head: head.to_string(),
            ts: commit.ts,
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
        out.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(out)
    }

    pub fn get_doc(&self, id: &str) -> Result<Option<DocView>> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let Some(head) = view.get(id) else { return Ok(None) };
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
            .filter(|doc| doc.owner == owner && doc.workspace_id == workspace_id)
            .map(|doc| doc.name.clone())
            .collect::<BTreeSet<_>>();
        Ok(docs
            .into_iter()
            .filter(|doc| {
                (doc.owner == owner && doc.workspace_id == workspace_id)
                    || (doc.owner == "shared"
                        && doc.workspace_id == "global"
                        && !exact_names.contains(&doc.name))
            })
            .collect())
    }

    pub fn get_doc_scoped(&self, id: &str, owner: &str, workspace_id: &str) -> Result<Option<DocView>> {
        Ok(self.get_doc(id)?.filter(|doc| scope_allows(doc, owner, workspace_id)))
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

    pub fn list_deleted_docs_scoped(&self, owner: &str, workspace_id: &str) -> Result<Vec<DocView>> {
        let txn = self.database.begin_read()?;
        let view = self.read_view(&txn)?;
        let mut deleted = Vec::new();
        for (doc_id, head) in &view {
            let doc = self.view_of(&txn, doc_id, head)?;
            if matches!(doc.content, Content::Tombstone) && scope_allows(&doc, owner, workspace_id) {
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
        let Some(head) = view.get(id) else { return Err(err("doc not found")) };
        let mut changes = Vec::new();
        let mut cursor = Some(head.clone());
        while let Some(commit_id) = cursor {
            let commit = self.load_commit(&txn, &commit_id)?;
            let mut amends = Vec::new();
            let mut pred = commit.predecessors.first().cloned();
            while let Some(pred_id) = pred {
                let pred_commit = self.load_commit(&txn, &pred_id)?;
                amends.push(json!({ "commit": pred_id, "ts": pred_commit.ts, "name": pred_commit.name }));
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
        let read_text = |commit_id: &str| -> Result<String> {
            let commit = self.load_commit(&txn, commit_id)?;
            match commit.content {
                Content::Blob { hash } => {
                    let blobs = txn.open_table(BLOBS)?;
                    let bytes = blobs.get(hash.as_str())?.ok_or_else(|| err("missing blob"))?;
                    Ok(String::from_utf8_lossy(bytes.value()).into_owned())
                },
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
            out.push(json!({
                "op": id,
                "parent": op.parent,
                "kind": op.kind,
                "description": op.description,
                "ts": op.ts,
                "docs": op.view.len(),
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

    pub fn create_doc(&self, kind: &str, name: &str, content: &str, message: Option<&str>) -> Result<DocView> {
        self.create_doc_scoped("shared", "global", kind, name, content, message)
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
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Err(err("owner and workspace are required"));
        }
        if self.find_doc_by_name_scoped(name, owner, workspace_id)?.is_some() {
            return Err(err(format!("name already exists: {name}")));
        }
        let doc_id = new_ulid();
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;
        {
            let mut docs = txn.open_table(DOCS)?;
            let record = DocRecord {
                kind: kind.to_string(),
                created_op: "pending".to_string(),
                owner: owner.to_string(),
                workspace_id: workspace_id.to_string(),
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

    pub fn write_doc_scoped(
        &self,
        id: &str,
        content: &str,
        base: Option<&str>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<WriteOutcome> {
        self.require_scope(id, owner, workspace_id)?;
        self.write_doc(id, content, base)
    }

    pub fn history_scoped(&self, id: &str, owner: &str, workspace_id: &str) -> Result<Value> {
        self.require_scope(id, owner, workspace_id)?;
        self.history(id)
    }

    pub fn rename_doc_scoped(
        &self,
        id: &str,
        new_name: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<DocView> {
        self.require_scope(id, owner, workspace_id)?;
        if let Some(existing) = self.find_doc_by_name_scoped(new_name, owner, workspace_id)? {
            if existing.id != id {
                return Err(err(format!("name already exists: {new_name}")));
            }
        }
        self.rename_doc_unchecked(id, new_name)
    }

    pub fn delete_doc_scoped(&self, id: &str, owner: &str, workspace_id: &str) -> Result<()> {
        self.require_scope(id, owner, workspace_id)?;
        self.delete_doc(id)
    }

    pub fn checkpoint_scoped(
        &self,
        id: &str,
        message: Option<&str>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<DocView> {
        self.require_scope(id, owner, workspace_id)?;
        self.checkpoint(id, message)
    }

    pub fn restore_doc_scoped(
        &self,
        id: &str,
        commit_id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<DocView> {
        self.require_scope(id, owner, workspace_id)?;
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
        let head = view.get(id).ok_or_else(|| err("doc not found in this scope"))?;
        let deleted = self.view_of(&txn, id, head)?;
        if !scope_allows(&deleted, owner, workspace_id) || !matches!(deleted.content, Content::Tombstone) {
            return Err(err("doc not found in this scope"));
        }
        let tombstone = self.load_commit(&txn, head)?;
        let previous = tombstone.parent.ok_or_else(|| err("deleted doc has no restorable parent"))?;
        drop(txn);
        self.restore_doc(id, &previous)
    }

    fn require_scope(&self, id: &str, owner: &str, workspace_id: &str) -> Result<()> {
        if self.get_doc_scoped(id, owner, workspace_id)?.is_none() {
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
                let bytes = blobs.get(hash.as_str())?.ok_or_else(|| err("missing blob"))?;
                Some(String::from_utf8_lossy(bytes.value()).into_owned())
            },
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
            parent: if new_change { Some(head.clone()) } else { head_commit.parent.clone() },
            predecessors: if new_change { Vec::new() } else { vec![head.clone()] },
            name: head_commit.name.clone(),
            content: Content::Blob { hash },
            ts: now,
            message: None,
        };
        let commit_id = put_commit(&txn, &commit)?;
        view.insert(id.to_string(), commit_id);
        put_op(&txn, parent_op, "snapshot", &format!("snapshot {}", head_commit.name), &view)?;
        txn.commit()?;
        let updated = self.get_doc(id)?.ok_or_else(|| err("write failed"))?;
        Ok(WriteOutcome::Committed { view: updated, new_change })
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
        put_op(&txn, parent_op, "checkpoint", &format!("checkpoint {}", current.name), &view)?;
        txn.commit()?;
        Ok(self.get_doc(id)?.ok_or_else(|| err("checkpoint failed"))?)
    }

    pub fn rename_doc(&self, id: &str, new_name: &str) -> Result<DocView> {
        if let Some(existing) = self.find_doc_by_name(new_name)? {
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
            if reference_scope.owner != target_scope.owner
                || reference_scope.workspace_id != target_scope.workspace_id
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
                reference_updates.push((reference_id.clone(), reference_head.clone(), reference_commit, rewritten));
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
        put_op(&txn, parent_op, "delete", &format!("delete {}", current.name), &view)?;
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
        let head = self.current_view()?.get(id).cloned().ok_or_else(|| err("doc not found"))?;
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
        put_op(&txn, parent_op, "restore", &format!("restore {} to {commit_id}", old.name), &view)?;
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
                let record = ops.get(current_op.as_str())?.ok_or_else(|| err("missing current op"))?;
                let op: OpRecord = serde_json::from_str(record.value())?;
                op.parent.ok_or_else(|| err("nothing to undo"))?
            },
        };
        let restored_view: BTreeMap<String, String> = {
            let ops = txn.open_table(OPS)?;
            let record = ops.get(target.as_str())?.ok_or_else(|| err("target op not found"))?;
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
        put_op(&txn, parent_op, "undo", &format!("restore repo to op {target}"), &restored_view)?;
        txn.commit()?;
        Ok(changed)
    }

    // ── Assets (metaplan §3b: outside the DB, tracked by it) ─────────────

    /// Write asset bytes content-addressed into `assets/` and create or
    /// amend the AssetRef doc named `name`. Old versions stay on disk;
    /// the doc's history is the chain of hashes.
    pub fn put_asset(&self, name: &str, ext: &str, bytes: &[u8]) -> Result<DocView> {
        let hash = blake3::hash(bytes).to_hex().to_string();
        let ext = ext.trim_start_matches('.').to_ascii_lowercase();
        let file = self.assets_dir.join(format!("{hash}.{ext}"));
        if !file.exists() {
            let tmp = self.assets_dir.join(format!("{hash}.{ext}.tmp"));
            fs::write(&tmp, bytes)?;
            fs::rename(&tmp, &file)?;
        }
        let content = Content::Asset { hash: hash.clone(), ext: ext.clone(), size: bytes.len() as u64 };
        match self.find_doc_by_name(name)? {
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
                put_op(&txn, parent_op, "asset-update", &format!("update asset {name}"), &view)?;
                txn.commit()?;
                Ok(self.get_doc(&existing.id)?.ok_or_else(|| err("asset update failed"))?)
            },
            None => {
                let doc_id = new_ulid();
                let txn = self.database.begin_write()?;
                let (mut view, parent_op) = self.write_view(&txn)?;
                {
                    let mut docs = txn.open_table(DOCS)?;
                    let record = DocRecord {
                        kind: "asset".to_string(),
                        created_op: "pending".to_string(),
                        owner: shared_owner(),
                        workspace_id: global_workspace(),
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
                put_op(&txn, parent_op, "asset-update", &format!("add asset {name}"), &view)?;
                txn.commit()?;
                Ok(self.get_doc(&doc_id)?.ok_or_else(|| err("asset create failed"))?)
            },
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
    pub fn import_vault(&self, vault_dir: &Path, planning_file: Option<&Path>) -> Result<ImportStats> {
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
        if owner.trim().is_empty() || workspace_id.trim().is_empty() {
            return Err(err("owner and workspace are required"));
        }
        const NOTE_SUFFIXES: &[&str] = &["md", "markdown", "base", "canvas", "dclg"];
        const ASSET_SUFFIXES: &[&str] = &["png", "jpg", "jpeg", "gif", "webp", "svg"];

        let mut files = Vec::new();
        collect_files(vault_dir, &mut files);
        let existing: BTreeMap<String, DocView> = self
            .list_docs()?
            .into_iter()
            .filter(|doc| doc.owner == owner && doc.workspace_id == workspace_id)
            .map(|doc| (doc.name.clone(), doc))
            .collect();

        let mut stats = ImportStats::default();
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;

        let create_doc_in_txn = |txn: &redb::WriteTransaction,
                                     view: &mut BTreeMap<String, String>,
                                     kind: &str,
                                     name: &str,
                                     parent: Option<String>,
                                     predecessors: Vec<String>,
                                     content: Content,
                                     doc_id: Option<String>|
         -> Result<()> {
            let doc_id = match doc_id {
                Some(id) => id,
                None => {
                    let id = new_ulid();
                    let mut docs = txn.open_table(DOCS)?;
                    let record = DocRecord {
                        kind: kind.to_string(),
                        created_op: "pending".to_string(),
                        owner: owner.to_string(),
                        workspace_id: workspace_id.to_string(),
                    };
                    docs.insert(id.as_str(), serde_json::to_string(&record)?.as_str())?;
                    id
                },
            };
            let commit = CommitRecord {
                doc: doc_id.clone(),
                parent,
                predecessors,
                name: name.to_string(),
                content,
                ts: now_ms(),
                message: Some("import".to_string()),
            };
            let commit_id = put_commit(txn, &commit)?;
            view.insert(doc_id, commit_id);
            Ok(())
        };

        for path in files {
            let Ok(rel) = path.strip_prefix(vault_dir) else { continue };
            let rel_name = rel.to_string_lossy().replace('\\', "/");
            if rel.components().any(|part| part.as_os_str().to_string_lossy().starts_with('.')) {
                continue;
            }
            let ext = path.extension().and_then(|value| value.to_str()).unwrap_or("").to_ascii_lowercase();
            if NOTE_SUFFIXES.contains(&ext.as_str()) {
                let Ok(content) = fs::read_to_string(&path) else { continue };
                let kind = match ext.as_str() {
                    "base" => "base",
                    "canvas" => "canvas",
                    _ => "markdown",
                };
                match existing.get(&rel_name) {
                    Some(doc) if doc.text.as_deref() == Some(content.as_str()) => {},
                    Some(doc) => {
                        let hash = put_blob(&txn, content.as_bytes())?;
                        create_doc_in_txn(
                            &txn,
                            &mut view,
                            kind,
                            &rel_name,
                            Some(doc.head.clone()),
                            Vec::new(),
                            Content::Blob { hash },
                            Some(doc.id.clone()),
                        )?;
                        stats.notes += 1;
                    },
                    None => {
                        let hash = put_blob(&txn, content.as_bytes())?;
                        create_doc_in_txn(&txn, &mut view, kind, &rel_name, None, Vec::new(), Content::Blob { hash }, None)?;
                        stats.notes += 1;
                    },
                }
            } else if ASSET_SUFFIXES.contains(&ext.as_str()) {
                let Ok(bytes) = fs::read(&path) else { continue };
                let hash = blake3::hash(&bytes).to_hex().to_string();
                let file = self.assets_dir.join(format!("{hash}.{ext}"));
                if !file.exists() {
                    fs::write(&file, &bytes)?;
                }
                let content = Content::Asset { hash, ext: ext.clone(), size: bytes.len() as u64 };
                match existing.get(&rel_name) {
                    Some(doc) if doc.content == content => {},
                    Some(doc) => {
                        create_doc_in_txn(&txn, &mut view, "asset", &rel_name, Some(doc.head.clone()), Vec::new(), content, Some(doc.id.clone()))?;
                        stats.assets += 1;
                    },
                    None => {
                        create_doc_in_txn(&txn, &mut view, "asset", &rel_name, None, Vec::new(), content, None)?;
                        stats.assets += 1;
                    },
                }
            }
        }

        if let Some(planning) = planning_file {
            if let Ok(content) = fs::read_to_string(planning) {
                if serde_json::from_str::<Value>(&content).is_ok() {
                    match existing.get("move-data.json") {
                        Some(doc) if doc.text.as_deref() == Some(content.as_str()) => {},
                        Some(doc) => {
                            let hash = put_blob(&txn, content.as_bytes())?;
                            create_doc_in_txn(&txn, &mut view, "planning", "move-data.json", Some(doc.head.clone()), Vec::new(), Content::Blob { hash }, Some(doc.id.clone()))?;
                            stats.planning = true;
                        },
                        None => {
                            let hash = put_blob(&txn, content.as_bytes())?;
                            create_doc_in_txn(&txn, &mut view, "planning", "move-data.json", None, Vec::new(), Content::Blob { hash }, None)?;
                            stats.planning = true;
                        },
                    }
                }
            }
        }

        // Copal exports its durable LMMS aggregate under .copal. General
        // hidden files stay ignored, but this one versioned recovery artifact
        // round-trips explicitly.
        let treehouse = vault_dir.join(".copal/treehouse-state.json");
        if let Ok(content) = fs::read_to_string(&treehouse) {
            if serde_json::from_str::<Value>(&content).is_ok() {
                let name = ".copal/treehouse-state.json";
                match existing.get(name) {
                    Some(doc) if doc.text.as_deref() == Some(content.as_str()) => {},
                    Some(doc) => {
                        let hash = put_blob(&txn, content.as_bytes())?;
                        create_doc_in_txn(&txn, &mut view, "treehouse-state", name, Some(doc.head.clone()), Vec::new(), Content::Blob { hash }, Some(doc.id.clone()))?;
                        stats.treehouse = true;
                    },
                    None => {
                        let hash = put_blob(&txn, content.as_bytes())?;
                        create_doc_in_txn(&txn, &mut view, "treehouse-state", name, None, Vec::new(), Content::Blob { hash }, None)?;
                        stats.treehouse = true;
                    },
                }
            }
        }

        let description = format!(
            "import vault {}: {} notes, {} assets{}{}",
            vault_dir.file_name().and_then(|value| value.to_str()).unwrap_or("?"),
            stats.notes,
            stats.assets,
            if stats.planning { ", planning" } else { "" },
            if stats.treehouse { ", treehouse" } else { "" },
        );
        stats.op = put_op(&txn, parent_op, "import", &description, &view)?;
        txn.commit()?;
        Ok(stats)
    }

    // ── Internals ─────────────────────────────────────────────────────────

    /// Current view + current op id, readable inside a write transaction.
    fn write_view(&self, txn: &redb::WriteTransaction) -> Result<(BTreeMap<String, String>, Option<String>)> {
        let meta = txn.open_table(META)?;
        let head = meta.get(OP_HEAD_KEY)?.map(|value| value.value().to_string());
        drop(meta);
        let Some(head) = head else {
            return Ok((BTreeMap::new(), None));
        };
        let ops = txn.open_table(OPS)?;
        let record = ops.get(head.as_str())?.ok_or_else(|| err("op head points at missing op"))?;
        let op: OpRecord = serde_json::from_str(record.value())?;
        Ok((op.view, Some(head)))
    }
}

fn load_commit_in_txn(txn: &redb::WriteTransaction, id: &str) -> Result<CommitRecord> {
    let commits = txn.open_table(COMMITS)?;
    let record = commits.get(id)?.ok_or_else(|| err(format!("missing commit {id}")))?;
    Ok(serde_json::from_str(record.value())?)
}

fn load_doc_record_in_txn(txn: &redb::WriteTransaction, id: &str) -> Result<DocRecord> {
    let docs = txn.open_table(DOCS)?;
    let record = docs.get(id)?.ok_or_else(|| err(format!("missing doc {id}")))?;
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

fn collect_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_files(&path, out);
        } else {
            out.push(path);
        }
    }
    out.sort();
}

fn scope_allows(doc: &DocView, owner: &str, workspace_id: &str) -> bool {
    (doc.owner == "shared" && doc.workspace_id == "global")
        || (doc.owner == owner && doc.workspace_id == workspace_id)
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
        let Some(relative_end) = text[inner_start..].find("]]" ) else {
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
        output.push_str("]]" );
        cursor = end + 2;
    }
    output.push_str(&text[cursor..]);
    output
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

fn new_ulid() -> String {
    ulid::Ulid::new().to_string()
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_db() -> (Db, PathBuf) {
        let dir = std::env::temp_dir().join(format!("copal-db-test-{}", new_ulid()));
        (Db::open(&dir).unwrap(), dir)
    }

    #[test]
    fn create_write_amend_and_history() {
        let (db, _dir) = temp_db();
        let doc = db.create_doc("markdown", "Notes/Hello.md", "# Hello\n", None).unwrap();
        assert_eq!(doc.text.as_deref(), Some("# Hello\n"));

        // Amend: same change, predecessor chain grows.
        let outcome = db.write_doc(&doc.id, "# Hello\nWorld\n", Some(&doc.head)).unwrap();
        let WriteOutcome::Committed { view, new_change } = outcome else { panic!("expected commit") };
        assert!(!new_change);
        assert_ne!(view.head, doc.head);

        // Identical content is a no-op.
        let outcome = db.write_doc(&doc.id, "# Hello\nWorld\n", None).unwrap();
        assert!(matches!(outcome, WriteOutcome::Unchanged { .. }));

        // Stale base writes nothing and returns the authoritative head.
        let outcome = db.write_doc(&doc.id, "clobber", Some(&doc.head)).unwrap();
        let WriteOutcome::Stale { view: stale_view } = outcome else { panic!("expected stale") };
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

        let diff = db.diff(&doc.head, &db.get_doc(&doc.id).unwrap().unwrap().head).unwrap();
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
    fn duplicate_names_rejected() {
        let (db, _dir) = temp_db();
        db.create_doc("markdown", "same.md", "a", None).unwrap();
        assert!(db.create_doc("markdown", "same.md", "b", None).is_err());
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
        assert!(db.get_doc_scoped(&alice.id, "bob", "home").unwrap().is_none());
        assert!(db
            .write_doc_scoped(&bob.id, "clobber", None, "alice", "home")
            .is_err());
        assert_eq!(db.get_doc_scoped(&bob.id, "bob", "home").unwrap().unwrap().text.as_deref(), Some("bob"));

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
    fn scoped_trash_restores_deleted_content() {
        let (db, _dir) = temp_db();
        let doc = db
            .create_doc_scoped("alice", "home", "markdown", "recover.md", "keep me", None)
            .unwrap();
        db.delete_doc_scoped(&doc.id, "alice", "home").unwrap();

        assert_eq!(db.list_deleted_docs_scoped("alice", "home").unwrap().len(), 1);
        assert!(db.list_deleted_docs_scoped("bob", "home").unwrap().is_empty());

        let restored = db
            .restore_deleted_doc_scoped(&doc.id, "alice", "home")
            .unwrap();
        assert_eq!(restored.text.as_deref(), Some("keep me"));
        assert!(db.list_deleted_docs_scoped("alice", "home").unwrap().is_empty());
    }

    #[test]
    fn assets_are_files_with_history() {
        let (db, dir) = temp_db();
        let v1 = db.put_asset("img/pic.png", "png", b"AAAA").unwrap();
        let Content::Asset { hash: h1, .. } = v1.content.clone() else { panic!() };
        assert!(dir.join("assets").join(format!("{h1}.png")).is_file());

        let v2 = db.put_asset("img/pic.png", "png", b"BBBB").unwrap();
        let Content::Asset { hash: h2, .. } = v2.content.clone() else { panic!() };
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
        fs::write(vault.join(".copal/treehouse-state.json"), "{\"schemaVersion\":1}").unwrap();

        let stats = db.import_vault_scoped(&vault, Some(&planning), "alice", "school").unwrap();
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
        let shared_note = db.create_doc("markdown", "Welcome.md", "shared", None).unwrap();
        let shared_planning = db.create_doc("planning", "move-data.json", "{\"tracks\":[\"shared\"]}", None).unwrap();
        let vault = std::env::temp_dir().join(format!("copal-overlay-import-{}", new_ulid()));
        fs::create_dir_all(&vault).unwrap();
        fs::write(vault.join("Welcome.md"), "private").unwrap();
        let planning = vault.join("planning.json");
        fs::write(&planning, "{\"tracks\":[\"private\"]}").unwrap();

        let stats = db.import_vault_scoped(&vault, Some(&planning), "alice", "school").unwrap();
        assert_eq!(stats.notes, 1);
        assert!(stats.planning);
        let alice = db.list_docs_scoped("alice", "school").unwrap();
        assert_eq!(alice.len(), 2);
        assert_eq!(alice.iter().find(|doc| doc.name == "Welcome.md").unwrap().text.as_deref(), Some("private"));
        assert_eq!(db.get_doc(&shared_note.id).unwrap().unwrap().text.as_deref(), Some("shared"));
        assert_eq!(db.get_doc(&shared_planning.id).unwrap().unwrap().text.as_deref(), Some("{\"tracks\":[\"shared\"]}"));
        assert_eq!(db.list_docs_scoped("bob", "school").unwrap().len(), 2);
        fs::remove_dir_all(vault).unwrap();
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
}
