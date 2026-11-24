//! Resumable, scoped structural projection. Canonical source bytes remain with the producer.
use rusqlite::{params, OptionalExtension, Transaction};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::{document_upsert, owner_custody::VerifiedCustodyView, sha256_hex, RepositoryError, ResetFenceBudget};

const MAX_TEXT: usize = 256 * 1024;
const PAGE: usize = 24;
const CAPABILITY: &str = "openclank.document_structure.v1";

#[derive(Debug, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
enum SourceKind {
    File,
    Attachment,
    Journal,
}
impl SourceKind {
    fn name(&self) -> &'static str {
        match self {
            Self::File => "file",
            Self::Attachment => "attachment",
            Self::Journal => "journal",
        }
    }
}

#[derive(Debug, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Source {
    kind: SourceKind,
    workspace: String,
    resource: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum DocumentStructureRequest {
    Advance {
        account_id: String,
        source: Source,
        revision: String,
        content_sha256: String,
        text: String,
        page_ends: Vec<usize>,
    },
    Read {
        account_id: String,
        source: Source,
        revision: String,
        content_sha256: String,
    },
    Revoke {
        account_id: String,
        source: Source,
    },
}

impl DocumentStructureRequest {
    pub(crate) fn account(&self) -> &str {
        match self {
            Self::Advance {account_id, ..} | Self::Read {account_id, ..} | Self::Revoke {account_id, ..} => account_id,
        }
    }
    pub fn writes(&self) -> bool {
        !matches!(self, Self::Read {..})
    }
    fn source(&self) -> &Source {
        match self {
            Self::Advance {source, ..} | Self::Read {source, ..} | Self::Revoke {source, ..} => source,
        }
    }
}

fn unavailable() -> RepositoryError {
    RepositoryError::Validation("document structure unavailable".into())
}

pub(crate) fn enabled(tx: &Transaction<'_>) -> Result<bool, RepositoryError> {
    let value: Option<String> = tx.query_row("SELECT value FROM fm_meta WHERE key=?1", [CAPABILITY], |row| row.get(0)).optional()?;
    match value.as_deref() {
        Some("1") => Ok(true),
        None => Ok(false),
        _ => Err(unavailable()),
    }
}

pub(crate) fn owns_schema_table(table: &str) -> bool {
    matches!(table, "fm_v2_doc_structure_heads" | "fm_v2_doc_structure_jobs" | "fm_v2_doc_structure_chunks")
}

fn valid_text(text: &str, limit: usize) -> bool {
    !text.is_empty() && text.len() <= limit && text.trim() == text && !text.chars().any(char::is_control)
}
fn valid_hash(text: &str) -> bool {
    text.len() == 64 && text.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
fn checked_digest(value: Value) -> Result<String, RepositoryError> {
    Ok(sha256_hex(document_upsert::sorted_json(&value, false)?.as_bytes()))
}

pub(crate) fn purge(tx: &Transaction<'_>, owner: &str) -> Result<(), RepositoryError> {
    if enabled(tx)? {
        tx.execute("DELETE FROM fm_v2_doc_structure_heads WHERE owner_id=?1", [owner])?;
    }
    Ok(())
}

pub(crate) fn revoke_file(tx: &Transaction<'_>, owner: &str, resource: &str) -> Result<(), RepositoryError> {
    if enabled(tx)? {
        tx.execute(
            "DELETE FROM fm_v2_doc_structure_heads WHERE owner_id=?1 AND kind='file' AND workspace='personal' AND resource=?2",
            params![owner,resource],
        )?;
    }
    Ok(())
}

pub(crate) fn run(
    tx: &Transaction<'_>,
    owner: &str,
    request: DocumentStructureRequest,
    view: &VerifiedCustodyView<'_, '_>,
    budget: &mut ResetFenceBudget,
) -> Result<Value, RepositoryError> {
    if !enabled(tx)? { return Ok(json!({"state":"unavailable"})); }
    let account = request.account();
    let source = request.source();
    if view.account_id()!=account || view.current_owner()!=owner || !valid_text(account,512)
        || !valid_text(&source.resource,512) || !valid_text(&source.workspace,128)
        || (matches!(source.kind,SourceKind::File) && source.workspace!="personal")
    {
        return Err(unavailable());
    }
    let key = checked_digest(json!(["openclank.document-structure.source.v1",account,source]))?;
    if matches!(request, DocumentStructureRequest::Revoke {..}) {
        tx.execute("DELETE FROM fm_v2_doc_structure_heads WHERE owner_id=?1 AND account_id=?2 AND source_key=?3",
                   params![owner,account,key])?;
        return Ok(json!({"state":"revoked","resource_key":key}));
    }

    let head: Option<(i64, Option<String>)> = tx.query_row(
        "SELECT epoch, generation FROM fm_v2_doc_structure_heads WHERE owner_id=?1 AND account_id=?2 AND source_key=?3",
        params![owner,account,key], |row| Ok((row.get(0)?,row.get(1)?))).optional()?;

    let (revision, content_sha256) = match &request {
        DocumentStructureRequest::Advance {revision, content_sha256, ..}
        | DocumentStructureRequest::Read {revision, content_sha256, ..} => (revision, content_sha256),
        DocumentStructureRequest::Revoke {..} => unreachable!(),
    };
    if !valid_text(revision,256) || !valid_hash(content_sha256) { return Err(unavailable()); }
    let file_revision = if matches!(source.kind, SourceKind::File) {
        let revision: i64 = revision.parse().map_err(|_| unavailable())?;
        let current: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM fm_v2_sources s WHERE s.owner_id=?1 AND s.source_id=?2
             AND s.source_revision=?3 AND s.content_hash=?4 AND s.forget_state='active'
             AND s.workspace_key='' AND s.project_key='' AND s.source_type='owner_imported_file'
             AND NOT EXISTS(SELECT 1 FROM fm_v2_sources newer WHERE newer.owner_id=s.owner_id
                 AND newer.source_id=s.source_id AND newer.source_revision>s.source_revision))",
            params![owner,source.resource,revision,content_sha256], |row| row.get(0))?;
        if !current { return Err(unavailable()); }
        Some(revision)
    } else {
        None
    };
    if let Some((_, Some(ref generation))) = head {
        let existing: Option<(i64,String)> = tx.query_row(
            "SELECT total, mentions_json FROM fm_v2_doc_structure_jobs WHERE owner_id=?1 AND source_key=?2 AND generation=?3
             AND revision=?4 AND content_sha256=?5 AND state='complete' AND total=(
                 SELECT count(*) FROM fm_v2_doc_structure_chunks c WHERE c.owner_id=?1 AND c.source_key=?2 AND c.generation=?3)",
            params![owner,key,generation,revision,content_sha256], |row| Ok((row.get(0)?,row.get(1)?))).optional()?;
        if let Some((count, raw)) = existing {
            if raw.len() > 65536 { return Err(unavailable()); }
            budget.check(raw.len()).map_err(RepositoryError::Validation)?;
            let mentions: Value = serde_json::from_str(&raw)?;
            return Ok(json!({"state":"complete","resource_key":key,"generation":generation,
                             "chunk_count":count,"projection":mentions}));
        }
    }
    let DocumentStructureRequest::Advance { text, page_ends, .. } = &request else {
        return Ok(json!({"state":"absent","resource_key":key}));
    };
    if text.is_empty() || text.len() > MAX_TEXT || page_ends.len() > 1024
        || sha256_hex(text.as_bytes()) != *content_sha256 {
        return Err(unavailable());
    }
    let epoch = head.as_ref().map_or(0, |(epoch, _)| *epoch);
    let generation = checked_digest(json!(["openclank.document-structure.generation.v1",key,revision,content_sha256,page_ends,epoch]))?;
    let chunks = document_upsert::structural_chunks(text, page_ends)?;
    let total = chunks.len();
    if total == 0 || total > 512 { return Err(unavailable()); }
    if let Some(revision) = file_revision {
        // The published personal source owns exact locator layout as well
        // as extracted bytes, including PDF breaks when retained.
        let mut query = tx.prepare(
            "SELECT c.ordinal,c.content_hash,c.locator_json FROM fm_v2_documents d JOIN fm_v2_chunks c
             ON c.owner_id=d.owner_id AND c.document_id=d.document_id
             WHERE d.owner_id=?1 AND d.source_id=?2 AND d.source_revision=?3 AND d.parser_version='frankenmemory-rag-v2'
             ORDER BY c.ordinal")?;
        let mut rows = query.query(params![owner,source.resource,revision])?;
        for expected in &chunks {
            let row = rows.next()?.ok_or_else(unavailable)?;
            let (ordinal, hash, locator): (u64, String, String) = (row.get(0)?,row.get(1)?,row.get(2)?);
            budget.check(locator.len()).map_err(RepositoryError::Validation)?;
            if ordinal != expected.ordinal || hash != expected.text_sha256 || locator != expected.locator_json {
                return Err(unavailable());
            }
        }
        if rows.next()?.is_some() { return Err(unavailable()); }
    }
    if head.is_none() {
        tx.execute("INSERT INTO fm_v2_doc_structure_heads(owner_id,account_id,source_key,kind,workspace,resource,epoch)
                    VALUES(?1,?2,?3,?4,?5,?6,0)", params![owner,account,key,source.kind.name(),source.workspace,source.resource])?;
    }
    // A higher publication epoch invalidates an in-flight job's base. Its
    // retained rows cannot become visible through the resource's current head.
    tx.execute("INSERT INTO fm_v2_doc_structure_jobs(owner_id,source_key,generation,base_epoch,revision,content_sha256,total,next,state)
                VALUES(?1,?2,?3,?4,?5,?6,?7,0,'active') ON CONFLICT(owner_id,source_key,generation) DO NOTHING",
               params![owner,key,generation,epoch,revision,content_sha256,total])?;
    let staged: (i64,usize,usize,String) = tx.query_row(
        "SELECT base_epoch,next,total,state FROM fm_v2_doc_structure_jobs WHERE owner_id=?1 AND source_key=?2 AND generation=?3",
        params![owner,key,generation], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?)))?;
    let (base, next, recorded_total, state) = staged;
    if base != epoch || recorded_total != total || state != "active" || next >= total {
        return Ok(json!({"state":"superseded","resource_key":key,"generation":generation}));
    }
    let end = (next + PAGE).min(total);
    for chunk in &chunks[next..end] {
        budget.check(chunk.locator_json.len()).map_err(RepositoryError::Validation)?;
        tx.execute("INSERT INTO fm_v2_doc_structure_chunks(owner_id,source_key,generation,ordinal,content_sha256,locator_json)
                    VALUES(?1,?2,?3,?4,?5,?6)",
                   params![owner,key,generation,chunk.ordinal,chunk.text_sha256,chunk.locator_json])?;
    }
    if end != total {
        if tx.execute("UPDATE fm_v2_doc_structure_jobs SET next=?4 WHERE owner_id=?1 AND source_key=?2 AND generation=?3 AND next=?5",
                      params![owner,key,generation,end,next])? != 1 { return Err(unavailable()); }
        return Ok(json!({"state":"active","resource_key":key,"generation":generation,"next":end,"total":total}));
    }
    let projection = document_upsert::structural_mentions(text);
    let raw = serde_json::to_string(&projection)?;
    if raw.len() > 65536 { return Err(unavailable()); }
    if tx.execute("UPDATE fm_v2_doc_structure_jobs SET state='complete',next=total,mentions_json=?4
                  WHERE owner_id=?1 AND source_key=?2 AND generation=?3 AND state='active' AND next=?5",
                  params![owner,key,generation,raw,next])? != 1 { return Err(unavailable()); }
    if tx.execute("UPDATE fm_v2_doc_structure_heads SET generation=?4,epoch=epoch+1
                  WHERE owner_id=?1 AND account_id=?2 AND source_key=?3 AND epoch=?5",
                  params![owner,account,key,generation,epoch])? != 1 { return Err(unavailable()); }
    // Retain the prior complete generation. Older or abandoned jobs cannot be
    // read as a current generation and can be discarded in the source lifecycle.
    Ok(json!({"state":"complete","resource_key":key,"generation":generation,
              "chunk_count":total,"projection":projection}))
}
