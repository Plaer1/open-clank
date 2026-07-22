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

fn shared_owner() -> String {
    "shared".to_string()
}

fn global_workspace() -> String {
    "global".to_string()
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
        let database = if database_path.exists() {
            Database::open(&database_path)?
        } else {
            Database::create(&database_path)?
        };
        // Ensure all tables exist so read transactions never hit
        // TableDoesNotExist on a fresh file, and record the root `init`
        // operation (jj's virtual root op) so there is always an op to
        // restore back to.
        let current_version = database.begin_read().ok().and_then(|txn| {
            txn.open_table(META).ok().and_then(|table| {
                table
                    .get(SCHEMA_VERSION_KEY)
                    .ok()
                    .flatten()
                    .and_then(|value| value.value().parse::<u64>().ok())
            })
        });
        if current_version.is_some_and(|version| version > SCHEMA_VERSION) {
            return Err(err(format!(
                "database schema {} is newer than supported schema {SCHEMA_VERSION}",
                current_version.unwrap()
            )));
        }
        if current_version != Some(SCHEMA_VERSION) {
            let txn = database.begin_write()?;
            {
                txn.open_table(DOCS)?;
                txn.open_table(COMMITS)?;
                txn.open_table(BLOBS)?;
                txn.open_table(OPS)?;
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
        }
        Ok(Self {
            database,
            assets_dir,
        })
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
            let docs = if let Some((owner, workspace_id)) = scope {
                let mut affects_scope = false;
                for document_id in changed {
                    let head = op
                        .view
                        .get(document_id)
                        .or_else(|| parent_view.get(document_id))
                        .ok_or_else(|| err("operation document head is missing"))?;
                    if scope_allows(&self.view_of(&txn, document_id, head)?, owner, workspace_id) {
                        affects_scope = true;
                        break;
                    }
                }
                if !affects_scope {
                    continue;
                }
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

    pub fn preflight_rename_owner(&self, old_owner: &str, new_owner: &str) -> Result<usize> {
        validate_owner_rename(old_owner, new_owner)?;
        let txn = self.database.begin_read()?;
        let docs = txn.open_table(DOCS)?;
        let mut source_count = 0;
        for entry in docs.iter()? {
            let (_, encoded) = entry?;
            let record: DocRecord = serde_json::from_str(encoded.value())?;
            if record.owner == new_owner {
                return Err(err("destination owner already has Copal documents"));
            }
            if record.owner == old_owner {
                source_count += 1;
            }
        }
        Ok(source_count)
    }

    pub fn rename_owner(&self, old_owner: &str, new_owner: &str) -> Result<usize> {
        let source_count = self.preflight_rename_owner(old_owner, new_owner)?;
        if source_count == 0 {
            return Ok(0);
        }

        let txn = self.database.begin_write()?;
        let (view, parent_op) = self.write_view(&txn)?;
        let updates = {
            let docs = txn.open_table(DOCS)?;
            let mut updates = Vec::with_capacity(source_count);
            for entry in docs.iter()? {
                let (document_id, encoded) = entry?;
                let mut record: DocRecord = serde_json::from_str(encoded.value())?;
                if record.owner == new_owner {
                    return Err(err("destination owner already has Copal documents"));
                }
                if record.owner == old_owner {
                    record.owner = new_owner.to_string();
                    updates.push((document_id.value().to_string(), record));
                }
            }
            updates
        };
        {
            let mut docs = txn.open_table(DOCS)?;
            for (document_id, record) in &updates {
                let encoded = serde_json::to_string(record)?;
                docs.insert(document_id.as_str(), encoded.as_str())?;
            }
        }
        put_op(
            &txn,
            parent_op,
            "rename-owner",
            &format!("rename owner {old_owner} to {new_owner}"),
            &view,
        )?;
        txn.commit()?;
        Ok(updates.len())
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
        let ext = safe_asset_ext(ext.trim_start_matches('.'));
        let content = store_import_asset(&self.assets_dir, &ext, bytes)?;
        let existing = self.list_docs()?.into_iter().find(|doc| {
            doc.owner == "shared"
                && doc.workspace_id == "global"
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
                        owner: shared_owner(),
                        workspace_id: global_workspace(),
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
        let existing: BTreeMap<(String, String, String), DocView> = self
            .list_docs()?
            .into_iter()
            .filter(|doc| {
                doc.owner == owner
                    && doc.workspace_id == workspace_id
                    && !(owner == "shared" && workspace_id == "global" && doc.builtin)
            })
            .map(|doc| {
                (
                    (doc.corpus.clone(), doc.kind.clone(), doc.name.clone()),
                    doc,
                )
            })
            .collect();

        let mut stats = ImportStats::default();
        let mut restored_paths = BTreeSet::new();
        let txn = self.database.begin_write()?;
        let (mut view, parent_op) = self.write_view(&txn)?;

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
                        value.get("schemaVersion").and_then(Value::as_u64) == Some(1)
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
            if (!hidden || reserved_event_note) && NOTE_SUFFIXES.contains(&raw_ext.as_str()) {
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
                        _ => "markdown",
                    }
                };
                let hash = put_blob(&txn, content.as_bytes())?;
                let status = import_content_in_txn(
                    &txn,
                    &mut view,
                    &existing,
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
                let kind = if hidden { "compatibility" } else { "asset" };
                let status = import_content_in_txn(
                    &txn,
                    &mut view,
                    &existing,
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
                } else if hidden {
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
    owner: &str,
    workspace_id: &str,
    corpus: &str,
    kind: &str,
    name: &str,
    content: Content,
    restore_identity: Option<&ImportIdentity>,
) -> Result<&'static str> {
    let key = (corpus.to_string(), kind.to_string(), name.to_string());
    if let Some(identity) = restore_identity {
        if identity.corpus != corpus || identity.kind != kind {
            return Err(err(
                "restore identity does not match imported corpus and kind",
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
}
