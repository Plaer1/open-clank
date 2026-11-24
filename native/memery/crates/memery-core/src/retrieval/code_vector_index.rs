//! Disposable and recoverable vector artifact next to the native Code cache.
//! Its receipts never authorize source reads. Every operation fences against
//! the existing published Code generation before entering the artifact.

use std::collections::BTreeSet;
use std::time::Duration;

use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};

use super::code_vectors::{DeclarationVector, EmbeddingIdentity};
use crate::code::WorkspaceBinding;
use crate::code_evidence::SourceLocator;
use crate::code_query::{QueryError, QueryReceipt};
use crate::graph::GraphScope;
use crate::store::code_evidence::{CodeEvidenceKind, CodeEvidenceQuery};
use crate::store::code_partitioned::{directory_scope, lock, open_at, CacheDirectory};
use crate::store::sqlite::SqliteStore;

const NAME: &str = "generation_00000000000000000000000000000000.sqlite";
const JOURNAL: &str = "generation_00000000000000000000000000000000.sqlite-journal";
const MAX_ROWS: usize = 262_144;
const MAX_BATCH: usize = 6;
const MAX_BYTES: u64 = 512 * 1024 * 1024;

pub(crate) fn namespace(checkout: &str) -> String {
    format!("derived-code-vectors/v1:{checkout}")
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndexScope {
    pub receipt: QueryReceipt,
    pub consent_epoch: String,
    pub route_fingerprint: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Slot {
    pub declaration_id: String,
    pub source: SourceLocator,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum IndexAction {
    Status,
    Preview,
    Reserve {
        cursor: Option<String>,
        next_cursor: Option<String>,
        original: String,
        slots: Vec<Slot>,
        sent_ids: Vec<String>,
    },
    ResetUndispatched {
        original: String,
    },
    Settle {
        original: String,
        profile: Option<EmbeddingIdentity>,
        vectors: Vec<DeclarationVector>,
    },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndexRequest {
    pub scope: IndexScope,
    pub action: IndexAction,
}

#[derive(Debug, Serialize)]
pub struct IndexState {
    pub receipt: QueryReceipt,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page: Option<IndexPage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pending: Option<PendingBatch>,
    pub state: String,
    pub cursor: Option<String>,
    pub indexed: usize,
    pub visited: usize,
    pub omitted: usize,
    pub can_continue: bool,
}

#[derive(Debug, Serialize)]
pub struct IndexPage {
    next_cursor: Option<String>,
    slots: Vec<Slot>,
    reusable: Vec<String>,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PendingBatch {
    original: String,
    slots: Vec<Slot>,
    sent_ids: Vec<String>,
}

fn unavailable(_: impl std::fmt::Display) -> QueryError {
    QueryError {
        code: "unavailable".into(),
        message: "Code vector artifact unavailable".into(),
    }
}

fn refuse(message: &str) -> QueryError {
    QueryError {
        code: "invalid_evidence".into(),
        message: message.into(),
    }
}

fn state(conn: &Connection, key: &str, receipt: &QueryReceipt) -> Result<IndexState, QueryError> {
    let row = conn
        .query_row(
            "SELECT state,cursor,indexed,visited,omitted FROM jobs WHERE key=?1",
            params![key],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                ))
            },
        )
        .optional()
        .map_err(unavailable)?;
    let (state, cursor, indexed, visited, omitted) =
        row.unwrap_or(("partial".into(), None, 0, 0, 0));
    let can_continue = state == "partial" && visited < MAX_ROWS;
    let pending = if state == "pending" {
        let packed: String = conn
            .query_row("SELECT slots FROM jobs WHERE key=?1", params![key], |row| {
                row.get(0)
            })
            .map_err(unavailable)?;
        Some(serde_json::from_str(&packed).map_err(unavailable)?)
    } else {
        None
    };
    Ok(IndexState {
        receipt: receipt.clone(),
        pending,
        page: None,
        state,
        cursor,
        indexed,
        visited,
        omitted,
        can_continue,
    })
}

fn identity(scope: &IndexScope, subject: &str) -> Result<String, QueryError> {
    if scope.consent_epoch.len() != 32
        || scope.route_fingerprint.len() != 64
        || !scope
            .consent_epoch
            .bytes()
            .chain(scope.route_fingerprint.bytes())
            .all(|b| b.is_ascii_hexdigit())
    {
        return Err(refuse("Invalid Code vector scope"));
    }
    let encoded = serde_json::to_vec(&(
        subject,
        &scope.receipt,
        &scope.consent_epoch,
        &scope.route_fingerprint,
    ))
    .map_err(unavailable)?;
    Ok(blake3::hash(&encoded).to_hex().to_string())
}

fn has_journal(folder: &CacheDirectory) -> Result<bool, QueryError> {
    match std::fs::symlink_metadata(folder.path.join(JOURNAL)) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(unavailable(error)),
    }
}
fn open(folder: &CacheDirectory, create: bool) -> Result<Option<Connection>, QueryError> {
    let path = folder.path.join(NAME);
    if create && !path.exists() {
        open_at(folder, NAME, true, true).map_err(unavailable)?;
    }
    if !path.exists() {
        return Ok(None);
    }
    if has_journal(folder)? {
        open_at(folder, JOURNAL, false, false).map_err(unavailable)?;
    }
    let pin = open_at(folder, NAME, create, false).map_err(unavailable)?;
    let meta = pin.metadata().map_err(unavailable)?;
    if meta.len() > MAX_BYTES {
        return Err(refuse("Code vector artifact exceeds its size allowance"));
    }
    let conn = Connection::open_with_flags(
        &path,
        if create {
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NOFOLLOW
        } else {
            // Read only normal settled DB files. A previous interrupted writer
            // may require an explicit continuation to recover its journal.
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW
        },
    )
    .map_err(unavailable)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let actual = std::fs::symlink_metadata(&path).map_err(unavailable)?;
        if !actual.is_file() || (actual.dev(), actual.ino()) != (meta.dev(), meta.ino()) {
            return Err(refuse("Code vector artifact identity changed"));
        }
    }
    conn.busy_timeout(Duration::from_millis(250))
        .map_err(unavailable)?;
    if create {
        conn.pragma_update(None, "synchronous", "FULL")
            .map_err(unavailable)?;
        let page_size: u64 = conn
            .pragma_query_value(None, "page_size", |r| r.get(0))
            .map_err(unavailable)?;
        if !(512..=65536).contains(&page_size) {
            return Err(refuse("Invalid Code vector artifact page size"));
        }
        conn.pragma_update(None, "max_page_count", MAX_BYTES / page_size)
            .map_err(unavailable)?;
        conn.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, consent TEXT NOT NULL, route TEXT NOT NULL, state TEXT NOT NULL, cursor TEXT,
                original TEXT, next_cursor TEXT, slots TEXT,
                profile TEXT, indexed INTEGER NOT NULL DEFAULT 0,
                visited INTEGER NOT NULL DEFAULT 0, omitted INTEGER NOT NULL DEFAULT 0,
                retained_at INTEGER NOT NULL DEFAULT(unixepoch())
            );
            CREATE TABLE IF NOT EXISTS vectors (
                key TEXT NOT NULL, declaration_id TEXT NOT NULL,
                locator TEXT NOT NULL, values_blob BLOB NOT NULL,
                PRIMARY KEY(key,declaration_id)
            ) WITHOUT ROWID;
        ",
        )
        .map_err(unavailable)?;
    }
    Ok(Some(conn))
}

fn prior_key(
    conn: &Connection,
    key: &str,
    consent: &str,
    route: &str,
) -> Result<Option<String>, QueryError> {
    conn.query_row(
        "SELECT key FROM jobs WHERE key<>?1 AND consent=?2 AND route=?3 AND state='ready' AND profile IS NOT NULL
         AND (NOT EXISTS(SELECT 1 FROM jobs WHERE key=?1 AND profile IS NOT NULL)
              OR profile=(SELECT profile FROM jobs WHERE key=?1))
         ORDER BY retained_at DESC, rowid DESC LIMIT 1",
        params![key,consent,route],
        |row| row.get(0),
    ).optional().map_err(unavailable)
}

impl SqliteStore {
    fn code_vector_page(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        receipt: &QueryReceipt,
        cursor: Option<String>,
    ) -> Result<IndexPage, QueryError> {
        let page = self
            .code_evidence_page(
                scope,
                binding,
                &receipt.evidence,
                CodeEvidenceQuery {
                    kind: CodeEvidenceKind::Declarations,
                    rel_path: None,
                    cursor,
                    max_rows: MAX_BATCH,
                    max_bytes: 512 * 1024,
                },
            )
            .map_err(unavailable)?;
        let mut slots = Vec::with_capacity(page.rows.len());
        for wrapped in page.rows {
            let row = wrapped
                .get("evidence")
                .ok_or_else(|| refuse("Stored declaration work unavailable"))?;
            let id = row
                .get("id")
                .and_then(|id| id.as_str())
                .filter(|id| !id.is_empty() && id.len() <= 128)
                .ok_or_else(|| refuse("Stored declaration identity unavailable"))?;
            let source = serde_json::from_value(
                row.get("source")
                    .cloned()
                    .ok_or_else(|| refuse("Stored declaration locator unavailable"))?,
            )
            .map_err(unavailable)?;
            slots.push(Slot {
                declaration_id: format!("decl:{id}"),
                source,
            });
        }
        Ok(IndexPage {
            next_cursor: page.next_cursor,
            slots,
            reusable: vec![],
        })
    }

    pub fn code_vector_index(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        input: IndexRequest,
    ) -> Result<IndexState, QueryError> {
        if binding.origin != "app" {
            return Err(refuse("Code vector build is app-only"));
        }
        let key = identity(&input.scope, &binding.owner_subject_id)?;
        let receipt = &input.scope.receipt;
        self.code_read_fence(scope, binding, Some(receipt))?;
        let consent = format!("{}:{}", binding.owner_subject_id, input.scope.consent_epoch);
        let route = &input.scope.route_fingerprint;
        let writable = !matches!(input.action, IndexAction::Status | IndexAction::Preview);
        let preview = |mut state: IndexState,
                       db: Option<&Connection>|
         -> Result<IndexState, QueryError> {
            if !matches!(input.action, IndexAction::Preview) || !state.can_continue {
                return Ok(state);
            }
            let mut page = self.code_vector_page(scope, binding, receipt, state.cursor.clone())?;
            if state.visited.saturating_add(page.slots.len()) > MAX_ROWS {
                state.can_continue = false;
                return Ok(state);
            }
            if let Some(db) = db {
                if let Some(prior) = prior_key(db, &key, &consent, route)? {
                    for slot in &page.slots {
                        let locator = serde_json::to_string(&slot.source).map_err(unavailable)?;
                        let exists: bool=db.query_row("SELECT EXISTS(SELECT 1 FROM vectors WHERE key=?1 AND declaration_id=?2 AND locator=?3)",
                            params![prior,slot.declaration_id,locator], |row|row.get(0)).map_err(unavailable)?;
                        if exists {
                            page.reusable.push(slot.declaration_id.clone());
                        }
                    }
                }
            }
            self.code_query_check(scope, binding, receipt)?;
            state.page = Some(page);
            Ok(state)
        };
        let folder = {
            let conn = self.conn.lock().map_err(unavailable)?;
            directory_scope(
                &conn,
                scope,
                &namespace(&receipt.evidence.checkout_id),
                writable,
            )
            .map_err(unavailable)?
        };
        let empty = || IndexState {
            receipt: receipt.clone(),
            pending: None,
            page: None,
            state: "partial".into(),
            cursor: None,
            indexed: 0,
            visited: 0,
            omitted: 0,
            can_continue: true,
        };
        let Some(folder) = folder else {
            return preview(empty(), None);
        };
        let recover = !writable && has_journal(&folder)?;
        lock(&folder.pin, writable || recover).map_err(unavailable)?;
        let Some(mut db) = open(&folder, writable || recover)? else {
            return preview(empty(), None);
        };
        if !writable {
            return preview(state(&db, &key, receipt)?, Some(&db));
        }
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(unavailable)?;
        tx.execute(
            "INSERT OR IGNORE INTO jobs(key,consent,route,state) VALUES(?1,?2,?3,'partial')",
            params![key, consent, route],
        )
        .map_err(unavailable)?;
        match input.action {
            IndexAction::Reserve {
                cursor,
                next_cursor,
                original,
                slots,
                sent_ids,
            } => {
                let prior = state(&tx, &key, receipt)?;
                let valid_cursor = |c: &String| !c.is_empty() && c.len() <= 4096;
                let page = self.code_vector_page(scope, binding, receipt, cursor.clone())?;
                if page.slots != slots || page.next_cursor != next_cursor {
                    return Err(refuse("Code declaration publication changed"));
                }
                if prior.state != "partial"
                    || prior.cursor != cursor
                    || (slots.is_empty() && next_cursor.is_some())
                    || slots.len() > MAX_BATCH
                    || prior.visited.saturating_add(slots.len()) > MAX_ROWS
                    || !original.bytes().all(|b| b.is_ascii_hexdigit())
                    || original.len() != 32
                    || next_cursor.as_ref().is_some_and(|c| !valid_cursor(c))
                    || (next_cursor.is_some() && next_cursor == cursor)
                    || slots
                        .iter()
                        .map(|row| &row.declaration_id)
                        .collect::<BTreeSet<_>>()
                        .len()
                        != slots.len()
                {
                    return Err(refuse("Code vector batch changed or exceeds its allowance"));
                }
                for slot in &slots {
                    if !slot.declaration_id.starts_with("decl:")
                        || slot.declaration_id.len() > 133
                        || slot.source.span.end_byte < slot.source.span.start_byte
                    {
                        return Err(refuse("Invalid declaration vector locator"));
                    }
                }
                let ordered: Vec<_> = slots
                    .iter()
                    .filter(|slot| sent_ids.contains(&slot.declaration_id))
                    .map(|slot| slot.declaration_id.clone())
                    .collect();
                if ordered != sent_ids {
                    return Err(refuse("Invalid sent declaration identities"));
                }
                let packed = serde_json::to_string(&PendingBatch {
                    original: original.clone(),
                    slots,
                    sent_ids,
                })
                .map_err(unavailable)?;
                if packed.len() > 96 * 1024 {
                    return Err(refuse("Code vector batch exceeds its allowance"));
                }
                tx.execute("UPDATE jobs SET state='pending', original=?2,next_cursor=?3,slots=?4 WHERE key=?1",
                    params![key,original,next_cursor,packed]).map_err(unavailable)?;
            }
            IndexAction::ResetUndispatched {original} => {
                if tx.execute("UPDATE jobs SET state='partial', original=NULL, next_cursor=NULL, slots=NULL WHERE key=?1 AND state='pending' AND original=?2",
                    params![key,original]).map_err(unavailable)?!=1 {
                    return Err(refuse("Code vector original changed"));
                }
            }
            IndexAction::Settle {
                original,
                profile,
                vectors,
            } => {
                let (slots, prior_profile):(String,Option<String>)=tx.query_row(
                    "SELECT slots,profile FROM jobs WHERE key=?1 AND state='pending' AND original=?2",
                    params![key,original],|r|Ok((r.get(0)?,r.get(1)?)),
                ).map_err(unavailable)?;
                let PendingBatch {
                    slots, sent_ids, ..
                }: PendingBatch = serde_json::from_str(&slots).map_err(unavailable)?;
                let mut used = BTreeSet::new();
                if vectors.len() > MAX_BATCH
                    || (!vectors.is_empty() && profile.is_none())
                    || vectors
                        .iter()
                        .map(|v| &v.declaration_id)
                        .ne(sent_ids.iter())
                    || profile.as_ref().is_some_and(|p| {
                        [&p.provider, &p.model, &p.revision]
                            .into_iter()
                            .any(|v| v.trim().is_empty() || v.len() > 512)
                    })
                {
                    return Err(refuse("Invalid Code vector outcome"));
                }
                let mut profile_json = profile
                    .as_ref()
                    .map(serde_json::to_string)
                    .transpose()
                    .map_err(unavailable)?;
                if prior_profile
                    .as_ref()
                    .zip(profile_json.as_ref())
                    .is_some_and(|(old, new)| old != new)
                {
                    return Err(refuse("Code embedding model changed during build"));
                }
                if let Some(prior) = prior_key(&tx, &key, &consent, route)? {
                    let reusable_profile: String = tx
                        .query_row(
                            "SELECT profile FROM jobs WHERE key=?1",
                            params![prior],
                            |r| r.get(0),
                        )
                        .map_err(unavailable)?;
                    if profile_json
                        .as_ref()
                        .or(prior_profile.as_ref())
                        .is_none_or(|current| current == &reusable_profile)
                    {
                        for Slot {
                            declaration_id: id,
                            source,
                        } in &slots
                        {
                            if vectors.iter().any(|row| &row.declaration_id == id) {
                                continue;
                            }
                            let locator = serde_json::to_string(source).map_err(unavailable)?;
                            let copied=tx.execute("INSERT INTO vectors SELECT ?1,declaration_id,locator,values_blob FROM vectors WHERE key=?2 AND declaration_id=?3 AND locator=?4",
                                params![key,prior,id,locator]).map_err(unavailable)?;
                            if copied == 1 {
                                used.insert(id.clone());
                                profile_json = Some(reusable_profile.clone());
                            }
                        }
                    }
                }
                for row in vectors {
                    let dimension = profile.as_ref().map_or(0, |p| p.dimension);
                    let norm: f64 = row
                        .values
                        .iter()
                        .map(|v| f64::from(*v) * f64::from(*v))
                        .sum();
                    if dimension == 0
                        || dimension > 1024
                        || row.values.len() != dimension
                        || !norm.is_finite()
                        || (norm - 1.0).abs() > 0.0001
                        || !slots.iter().any(|slot| {
                            slot.declaration_id == row.declaration_id && slot.source == row.source
                        })
                        || !used.insert(row.declaration_id.clone())
                    {
                        return Err(refuse("Invalid settled declaration vector"));
                    }
                    let bytes: Vec<_> = row.values.iter().flat_map(|v| v.to_le_bytes()).collect();
                    let locator = serde_json::to_string(&row.source).map_err(unavailable)?;
                    tx.execute(
                        "INSERT INTO vectors VALUES(?1,?2,?3,?4)",
                        params![key, row.declaration_id, locator, bytes],
                    )
                    .map_err(unavailable)?;
                }
                tx.execute("UPDATE jobs SET state=CASE WHEN next_cursor IS NULL THEN 'ready' ELSE 'partial' END,
                    cursor=next_cursor, original=NULL, next_cursor=NULL, slots=NULL, profile=coalesce(profile,?2),
                    indexed=indexed+?3, visited=visited+?4, omitted=omitted+?5, retained_at=unixepoch()
                    WHERE key=?1",params![key,profile_json,used.len(),slots.len(),slots.len()-used.len()]).map_err(unavailable)?;
                // One current and one previous generation per scoped artifact.
                tx.execute("DELETE FROM vectors WHERE key IN(SELECT key FROM jobs WHERE key<>?1 ORDER BY (state='ready') DESC,retained_at DESC,rowid DESC LIMIT -1 OFFSET 1)",params![key]).map_err(unavailable)?;
                tx.execute("DELETE FROM jobs WHERE key IN(SELECT key FROM jobs WHERE key<>?1 ORDER BY (state='ready') DESC,retained_at DESC,rowid DESC LIMIT -1 OFFSET 1)",params![key]).map_err(unavailable)?;
            }
            IndexAction::Status | IndexAction::Preview => unreachable!(),
        }
        // Recheck after work and before visibility changes. The latest head
        // is checked again by every public ranking operation.
        self.code_read_fence(scope, binding, Some(receipt))?;
        tx.commit().map_err(unavailable)?;
        state(&db, &key, receipt)
    }
}

/// An exact compatible-generation scan. Its component ceiling keeps cold
/// retained corpora from monopolizing the existing interactive query lane.
#[derive(Debug)]
pub(crate) enum RetainedScan {
    Missing,
    Budget,
    Ranked {
        candidates: Vec<DeclarationVector>,
        visited: usize,
        omitted: usize,
    },
}

impl SqliteStore {
    pub(crate) fn scan_code_vector_index(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        request: &IndexScope,
        profile: &EmbeddingIdentity,
        query: &[f32],
        work: usize,
        cancel: &std::sync::atomic::AtomicBool,
    ) -> Result<RetainedScan, QueryError> {
        let norm: f64 = query.iter().map(|value| f64::from(*value).powi(2)).sum();
        if !(1..=1024).contains(&profile.dimension)
            || query.len() != profile.dimension
            || !norm.is_finite()
            || (norm - 1.0).abs() > 0.0001
        {
            return Ok(RetainedScan::Missing);
        }
        let receipt = &request.receipt;
        let fence = self.code_read_fence(scope, binding, Some(receipt))?;
        let key = identity(request, &binding.owner_subject_id)?;
        let folder = {
            let conn = self.conn.lock().map_err(unavailable)?;
            directory_scope(
                &conn,
                scope,
                &namespace(&receipt.evidence.checkout_id),
                false,
            )
            .map_err(unavailable)?
        };
        let Some(folder) = folder else {
            return Ok(RetainedScan::Missing);
        };
        lock(&folder.pin, false).map_err(unavailable)?;
        let Some(db) = open(&folder, false)? else {
            return Ok(RetainedScan::Missing);
        };
        let Some((stored_profile,indexed,omitted)) = db.query_row(
            "SELECT profile,indexed,omitted FROM jobs WHERE key=?1 AND state='ready' AND profile IS NOT NULL",
            params![key],|r|Ok((r.get::<_,String>(0)?,r.get::<_,usize>(1)?,r.get::<_,usize>(2)?)),
        ).optional().map_err(unavailable)? else {return Ok(RetainedScan::Missing);};
        if serde_json::to_string(profile).map_err(unavailable)? != stored_profile {
            return Ok(RetainedScan::Missing);
        }
        if indexed > work || indexed.saturating_mul(query.len()) > 16_000_000 {
            return Ok(RetainedScan::Budget);
        }
        let mut statement=db.prepare("SELECT declaration_id,locator,values_blob FROM vectors WHERE key=?1 ORDER BY declaration_id").map_err(unavailable)?;
        let mut rows = statement.query(params![key]).map_err(unavailable)?;
        let mut scanned = 0usize;
        let mut candidates: Vec<(f64, DeclarationVector)> = Vec::new();
        const SHORTLIST: usize = 32;
        while let Some(row) = rows.next().map_err(unavailable)? {
            if cancel.load(std::sync::atomic::Ordering::Relaxed) {
                return Err(QueryError {
                    code: "cancelled".into(),
                    message: "Code query cancelled".into(),
                });
            }
            scanned += 1;
            if scanned > indexed {
                return Err(refuse("Code vector artifact row count changed"));
            }
            let id: String = row.get(0).map_err(unavailable)?;
            let blob = row
                .get_ref(2)
                .map_err(unavailable)?
                .as_blob()
                .map_err(unavailable)?;
            if blob.len() != query.len() * 4 {
                return Err(refuse("Code vector artifact dimension changed"));
            }
            let mut score = 0.0;
            let mut norm = 0.0;
            for (bytes, q) in blob.chunks_exact(4).zip(query) {
                let value = f64::from(f32::from_le_bytes(bytes.try_into().unwrap()));
                score += value * f64::from(*q);
                norm += value * value;
            }
            if !norm.is_finite() || (norm - 1.0).abs() > 0.0001 {
                return Err(refuse("Code vector artifact is malformed"));
            }
            let pos = candidates.partition_point(|(previous, c)| {
                previous.total_cmp(&score).is_gt()
                    || (previous.total_cmp(&score).is_eq() && c.declaration_id < id)
            });
            if pos < SHORTLIST {
                let locator: String = row.get(1).map_err(unavailable)?;
                let source = serde_json::from_str(&locator).map_err(unavailable)?;
                let values = blob
                    .chunks_exact(4)
                    .map(|v| f32::from_le_bytes(v.try_into().unwrap()))
                    .collect();
                candidates.insert(
                    pos,
                    (
                        score,
                        DeclarationVector {
                            declaration_id: id,
                            source,
                            values,
                        },
                    ),
                );
                candidates.truncate(SHORTLIST);
            }
        }
        if scanned != indexed || fence != self.code_read_fence(scope, binding, Some(receipt))? {
            return Err(refuse("Code vector publication changed during ranking"));
        }
        Ok(RetainedScan::Ranked {
            candidates: candidates
                .into_iter()
                .map(|(_, candidate)| candidate)
                .collect(),
            visited: scanned,
            omitted,
        })
    }
}
