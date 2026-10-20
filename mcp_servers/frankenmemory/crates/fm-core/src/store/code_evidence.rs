//! Canonical typed code evidence persistence, not an independent index owner.

pub const EVIDENCE_VERSION: u32 = 1;
pub const MAX_FILE_EVIDENCE_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_CORPUS_EVIDENCE_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_CORPUS_FILES: usize = 10_000;
pub const MAX_RESOLUTION_BYTES: usize = 64 * 1024 * 1024;
pub const DEFAULT_PAGE_ROWS: usize = 200;
pub const DEFAULT_PAGE_BYTES: usize = 256 * 1024;
pub const MAX_PAGE_ROWS: usize = 1_000;
pub const MAX_PAGE_BYTES: usize = 1024 * 1024;

/// Explicit offline migration only. Runtime installs complete current-schema.sql
/// for an empty store and never upgrades historical data with this fragment.
pub const SCHEMA_SQL: &str = r#"
CREATE TABLE code_file_evidence (
 owner TEXT NOT NULL, workspace_id TEXT NOT NULL, checkout_id TEXT NOT NULL,
 rel_path TEXT NOT NULL, source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 source_generation TEXT NOT NULL CHECK(source_generation!=''),
 config_fingerprint TEXT NOT NULL CHECK(config_fingerprint!=''),
 evidence_version INTEGER NOT NULL CHECK(evidence_version=1),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<=8388608),
 PRIMARY KEY(owner,workspace_id,checkout_id,rel_path),
 UNIQUE(owner,workspace_id,checkout_id,rel_path,source_hash,source_generation),
 FOREIGN KEY(owner,workspace_id,checkout_id) REFERENCES code_registrations(owner,workspace_id,checkout_id) ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE code_resolutions (
 owner TEXT NOT NULL, workspace_id TEXT NOT NULL, checkout_id TEXT NOT NULL,
 occurrence_id TEXT NOT NULL, rel_path TEXT NOT NULL,
 source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 source_generation TEXT NOT NULL CHECK(source_generation!=''),
 resolution_generation TEXT NOT NULL CHECK(resolution_generation!=''),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 PRIMARY KEY(owner,workspace_id,checkout_id,occurrence_id),
 FOREIGN KEY(owner,workspace_id,checkout_id,rel_path,source_hash,source_generation) REFERENCES code_file_evidence(owner,workspace_id,checkout_id,rel_path,source_hash,source_generation) ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX idx_code_resolutions_source ON code_resolutions(owner,workspace_id,checkout_id,rel_path,occurrence_id);
"#;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodeEvidenceReceipt {
    pub checkout_id: String,
    pub head_generation: String,
    pub admission_epoch: i64,
    pub policy_generation: i64,
    pub config_fingerprint: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeEvidenceKind {
    Files,
    Declarations,
    Occurrences,
    Resolutions,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodeEvidenceQuery {
    pub kind: CodeEvidenceKind,
    pub rel_path: Option<String>,
    /// Opaque keyset continuation; never an ignored offset or source authority.
    pub cursor: Option<String>,
    pub max_rows: usize,
    pub max_bytes: usize,
}
impl Default for CodeEvidenceQuery {
    fn default() -> Self {
        Self {
            kind: CodeEvidenceKind::Files,
            rel_path: None,
            cursor: None,
            max_rows: DEFAULT_PAGE_ROWS,
            max_bytes: DEFAULT_PAGE_BYTES,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeEvidenceErrorCode {
    Invalid,
    Denied,
    Unavailable,
    Missing,
    Erased,
    ReindexRequired,
    StaleCursor,
    BudgetExceeded,
    InvalidEvidence,
    Unsupported,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeEvidenceError {
    pub code: CodeEvidenceErrorCode,
    pub message: String,
}
impl std::fmt::Display for CodeEvidenceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}: {}", self.code, self.message)
    }
}
impl std::error::Error for CodeEvidenceError {}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeEvidenceAvailability {
    Current,
    Partial,
    Empty,
    ReindexRequired,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodeEvidencePage {
    pub receipt: CodeEvidenceReceipt,
    pub kind: CodeEvidenceKind,
    pub availability: CodeEvidenceAvailability,
    pub source_files: usize,
    pub evidence_files: usize,
    pub rows: Vec<serde_json::Value>,
    pub next_cursor: Option<String>,
    pub returned_rows: usize,
    /// Complete serialized envelope, including receipt/wrappers/cursor.
    pub returned_bytes: usize,
    pub partial: bool,
    /// No unbounded count scan is disguised as an exact match total.
    pub total_matches: Option<usize>,
    /// Stored-head evidence is not proof that mutable source bytes still match.
    pub current_source_verified: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct EvidenceCursor {
    version: u32,
    fingerprint: String,
    after_file: String,
    after_id: String,
}

fn failure(code: CodeEvidenceErrorCode, message: impl Into<String>) -> CodeEvidenceError {
    CodeEvidenceError {
        code,
        message: message.into(),
    }
}

fn validate_query(query: &CodeEvidenceQuery) -> Result<(), CodeEvidenceError> {
    if query.max_rows == 0
        || query.max_rows > MAX_PAGE_ROWS
        || query.max_bytes == 0
        || query.max_bytes > MAX_PAGE_BYTES
    {
        return Err(failure(
            CodeEvidenceErrorCode::Invalid,
            "invalid evidence row/byte budget",
        ));
    }
    if query
        .cursor
        .as_ref()
        .is_some_and(|cursor| cursor.len() > 16 * 1024)
    {
        return Err(failure(
            CodeEvidenceErrorCode::Invalid,
            "invalid evidence continuation",
        ));
    }
    if let Some(path) = &query.rel_path {
        validate_relative_path(path)?;
    }
    Ok(())
}

fn validate_relative_path(path: &str) -> Result<(), CodeEvidenceError> {
    let path_value = std::path::Path::new(path);
    if path.is_empty()
        || path.contains('\0')
        || path_value.is_absolute()
        || path_value
            .components()
            .any(|part| !matches!(part, std::path::Component::Normal(_)))
    {
        return Err(failure(
            CodeEvidenceErrorCode::Invalid,
            "invalid relative source locator",
        ));
    }
    Ok(())
}

fn measure_page(page: &mut CodeEvidencePage) -> Result<usize, CodeEvidenceError> {
    page.returned_rows = page.rows.len();
    // Returned byte count is part of the envelope, so include its own digits.
    for _ in 0..8 {
        let length = serde_json::to_vec(page)
            .map_err(|_| {
                failure(
                    CodeEvidenceErrorCode::InvalidEvidence,
                    "invalid evidence envelope",
                )
            })?
            .len();
        if length == page.returned_bytes {
            return Ok(length);
        }
        page.returned_bytes = length;
    }
    Err(failure(
        CodeEvidenceErrorCode::InvalidEvidence,
        "unstable evidence envelope size",
    ))
}

use crate::{code::WorkspaceBinding, graph::GraphScope, store::sqlite::SqliteStore};
use rusqlite::{params, Connection, OptionalExtension};

fn authority_error(_: String) -> CodeEvidenceError {
    failure(
        CodeEvidenceErrorCode::Denied,
        "code evidence authority is unavailable or changed",
    )
}
fn database_error(_: rusqlite::Error) -> CodeEvidenceError {
    failure(
        CodeEvidenceErrorCode::Unavailable,
        "code evidence store is unavailable",
    )
}
fn producer_error(error: crate::code_evidence::EvidenceError) -> CodeEvidenceError {
    use crate::code_evidence::EvidenceErrorCode;
    failure(
        match error.code {
            EvidenceErrorCode::BudgetExceeded => CodeEvidenceErrorCode::BudgetExceeded,
            EvidenceErrorCode::UnsupportedLanguage => CodeEvidenceErrorCode::Unsupported,
            _ => CodeEvidenceErrorCode::InvalidEvidence,
        },
        "bounded source evidence validation could not complete",
    )
}
fn producer_failure(error: crate::code_evidence::EvidenceError) -> String {
    if error.code == crate::code_evidence::EvidenceErrorCode::BudgetExceeded {
        "budget_exceeded: typed evidence producer limit".into()
    } else {
        "invalid_evidence: typed evidence producer rejected input".into()
    }
}
fn assert_scope(scope: &GraphScope, binding: &WorkspaceBinding) -> Result<(), CodeEvidenceError> {
    if scope.owner != binding.owner || scope.workspace_id != binding.workspace_id {
        return Err(failure(
            CodeEvidenceErrorCode::Denied,
            "workspace evidence scope differs",
        ));
    }
    Ok(())
}

pub(crate) fn current_receipt(
    conn: &Connection,
    scope: &GraphScope,
    binding: &WorkspaceBinding,
    checkout: Option<&str>,
) -> Result<CodeEvidenceReceipt, CodeEvidenceError> {
    let erased:Option<i64>=conn.query_row("SELECT erased FROM code_scope_epochs WHERE owner=?1 AND workspace_id IN (?2,'*') ORDER BY CASE WHEN workspace_id=?2 THEN 0 ELSE 1 END LIMIT 1",params![scope.owner,scope.workspace_id],|r|r.get(0)).optional().map_err(database_error)?;
    if erased == Some(1) {
        return Err(failure(
            CodeEvidenceErrorCode::Erased,
            "workspace index was explicitly erased",
        ));
    }
    let identity = crate::code::directory_identity(&binding.root).map_err(authority_error)?;
    let registered:Option<(String,i64,String,String,String)>=conn.query_row("SELECT checkout_id,admission_epoch,lifecycle,owner_subject_id,location_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND directory_identity=?3 AND (?4 IS NULL OR checkout_id=?4)",params![scope.owner,scope.workspace_id,identity,checkout],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional().map_err(database_error)?;
    let (checkout_id, admission_epoch, lifecycle, subject, location) =
        registered.ok_or_else(|| {
            failure(
                CodeEvidenceErrorCode::Missing,
                "registered workspace index is unavailable",
            )
        })?;
    if subject != binding.owner_subject_id || location != binding.location_id {
        return Err(failure(
            CodeEvidenceErrorCode::Denied,
            "registered workspace ownership differs",
        ));
    }
    if lifecycle == "erased" {
        return Err(failure(
            CodeEvidenceErrorCode::Erased,
            "workspace index was explicitly erased",
        ));
    }
    let head:Option<(String,String)>=conn.query_row("SELECT h.run_id,r.config_fingerprint FROM code_index_heads h JOIN code_index_runs r ON r.owner=h.owner AND r.workspace_id=h.workspace_id AND r.codebase=h.codebase AND r.run_id=h.run_id WHERE h.owner=?1 AND h.workspace_id=?2 AND h.codebase=?3 AND r.status IN ('ready','verified')",params![scope.owner,scope.workspace_id,checkout_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(database_error)?;
    let (head_generation, config_fingerprint) = head.ok_or_else(|| {
        failure(
            CodeEvidenceErrorCode::Missing,
            "workspace index has no ready generation",
        )
    })?;
    Ok(CodeEvidenceReceipt {
        checkout_id,
        head_generation,
        admission_epoch,
        policy_generation: binding.policy_generation,
        config_fingerprint,
    })
}

fn assert_receipt(
    current: &CodeEvidenceReceipt,
    expected: &CodeEvidenceReceipt,
) -> Result<(), CodeEvidenceError> {
    if current != expected {
        return Err(failure(
            CodeEvidenceErrorCode::StaleCursor,
            "code evidence receipt changed",
        ));
    }
    Ok(())
}

pub(crate) fn availability(
    conn: &Connection,
    scope: &GraphScope,
    receipt: &CodeEvidenceReceipt,
) -> Result<(CodeEvidenceAvailability, usize, usize), CodeEvidenceError> {
    if super::code_partitioned::is_partitioned(conn,scope,receipt).map_err(|e|failure(CodeEvidenceErrorCode::ReindexRequired,e))? {
        if receipt.config_fingerprint!=crate::code::producer_fingerprint(){return Ok((CodeEvidenceAvailability::ReindexRequired,0,0));}
        let generation=super::code_partitioned::Generation::open(conn,scope,receipt).map_err(|e|failure(CodeEvidenceErrorCode::ReindexRequired,e))?;
        let snapshot:Option<String>=conn.query_row("SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",params![scope.owner,scope.workspace_id,receipt.checkout_id,receipt.head_generation],|r|r.get(0)).map_err(database_error)?;
        let snapshot:crate::code::CodeSourceSnapshot=serde_json::from_str(&snapshot.ok_or_else(||failure(CodeEvidenceErrorCode::ReindexRequired,"missing cache lineage"))?).map_err(|_|failure(CodeEvidenceErrorCode::ReindexRequired,"invalid cache lineage"))?;
        generation.verify_stamp(&snapshot.evidence_cache_identity).map_err(|e|failure(CodeEvidenceErrorCode::ReindexRequired,e))?;
        let count:i64=generation.db.query_row("SELECT count(*) FROM files",[],|r|r.get(0)).map_err(database_error)?;
        let known:i64=conn.query_row("SELECT count(*) FROM code_files WHERE codebase=?1",params![SqliteStore::codebase_key(scope,&receipt.checkout_id)],|r|r.get(0)).map_err(database_error)?;
        if count<0 || count>MAX_CORPUS_FILES as i64 || count!=known{return Err(failure(CodeEvidenceErrorCode::ReindexRequired,"partition file index is incomplete"));}
        let partial=read_coverage(conn,scope,receipt,MAX_CORPUS_EVIDENCE_BYTES,1_000_000)?.iter().any(crate::code::eligible_unavailable);
        generation.verify_stamp(&snapshot.evidence_cache_identity).map_err(|e|failure(CodeEvidenceErrorCode::ReindexRequired,e))?;
        return Ok((if partial{CodeEvidenceAvailability::Partial}else if count==0{CodeEvidenceAvailability::Empty}else{CodeEvidenceAvailability::Current},count as usize,count as usize));
    }
    let key = format!(
        "code_v1:{}",
        serde_json::to_string(&[&scope.owner, &scope.workspace_id, &receipt.checkout_id]).unwrap()
    );
    let files: i64 = conn
        .query_row(
            "SELECT count(*) FROM code_files WHERE codebase=?1",
            params![key],
            |r| r.get(0),
        )
        .map_err(database_error)?;
    let rich:i64=conn.query_row("SELECT count(*) FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,receipt.checkout_id],|r|r.get(0)).map_err(database_error)?;
    let unavailable = read_coverage(conn, scope, receipt, MAX_CORPUS_EVIDENCE_BYTES, 1_000_000)?
        .iter()
        .any(crate::code::eligible_unavailable);
    let state =
        if rich != files || receipt.config_fingerprint != crate::code::producer_fingerprint() {
            CodeEvidenceAvailability::ReindexRequired
        } else if unavailable {
            CodeEvidenceAvailability::Partial
        } else if files == 0 {
            CodeEvidenceAvailability::Empty
        } else {
            CodeEvidenceAvailability::Current
        };
    Ok((state, files as usize, rich as usize))
}

impl SqliteStore {
    /// S05 snapshot boundary: finite SQL bytes/provenance only under the shared
    /// lock. No rich JSON parsing or query ranking while recall needs this conn.
    pub(crate) fn code_query_snapshot_raw(
        &self, scope:&GraphScope, binding:&WorkspaceBinding,
        expected:Option<&CodeEvidenceReceipt>,
    )->Result<super::code_query::RawQuerySnapshot,CodeEvidenceError>{
        let _timing=crate::code_metrics::span(crate::code_metrics::Stage::QuerySql);
        assert_scope(scope,binding)?;
        let query_authority_error=|message:String|failure(if message.starts_with("denied:"){CodeEvidenceErrorCode::Denied}else{CodeEvidenceErrorCode::Unavailable},"Code query authority unavailable");
        let authority=binding.publication_guard().map_err(query_authority_error)?;
        let mut conn=self.conn.lock().unwrap();
        let tx=conn.transaction().map_err(database_error)?;
        let receipt=current_receipt(&tx,scope,binding,expected.map(|e|e.checkout_id.as_str()))?;
        if let Some(expected)=expected{assert_receipt(&receipt,expected)?;}
        if receipt.config_fingerprint!=crate::code::producer_fingerprint(){return Err(failure(CodeEvidenceErrorCode::ReindexRequired,"rich code evidence requires explicit reindex"));}
        for(table,rows,bytes)in [("code_file_evidence",MAX_CORPUS_FILES,MAX_CORPUS_EVIDENCE_BYTES),("code_resolutions",1_000_000,MAX_RESOLUTION_BYTES)]{
            check_table_budget(&tx,table,scope,&receipt.checkout_id,rows,bytes).map_err(|_|failure(CodeEvidenceErrorCode::BudgetExceeded,"Code query snapshot exceeds corpus budget"))?;
        }
        let (registration_id,identity):(String,String)=tx.query_row("SELECT registration_id,directory_identity FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,receipt.checkout_id],|r|Ok((r.get(0)?,r.get(1)?))).map_err(database_error)?;
        let (snapshot,coverage):(Option<String>,Option<String>)=tx.query_row("SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END, CASE WHEN length(CAST(coverage AS BLOB))<=?5 THEN coverage END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",params![scope.owner,scope.workspace_id,receipt.checkout_id,receipt.head_generation,MAX_CORPUS_EVIDENCE_BYTES],|r|Ok((r.get(0)?,r.get(1)?))).map_err(database_error)?;
        let snapshot=snapshot.ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"Source snapshot exceeds budget"))?;
        let coverage=coverage.ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"Source coverage exceeds budget"))?;
        let mut files=Vec::new();
        {
            let mut stmt=tx.prepare("SELECT f.rel_path,f.source_hash,f.source_generation,f.payload_json,CASE WHEN c.blake3=f.source_hash AND c.source_generation=f.source_generation AND c.config_fingerprint=f.config_fingerprint THEN 1 ELSE 0 END FROM code_file_evidence f LEFT JOIN code_files c ON c.codebase=?4 AND c.rel_path=f.rel_path WHERE f.owner=?1 AND f.workspace_id=?2 AND f.checkout_id=?3 ORDER BY f.rel_path").map_err(database_error)?;
            let rows=stmt.query_map(params![scope.owner,scope.workspace_id,receipt.checkout_id,scope_key(scope,&receipt.checkout_id)],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,i64>(4)?))).map_err(database_error)?;
            for row in rows{let(rel,hash,generation,payload,valid)=row.map_err(database_error)?;
                if valid!=1{return Err(failure(CodeEvidenceErrorCode::InvalidEvidence,"Stored source provenance differs"));}
                files.push((rel,hash,generation,payload));
            }
        }
        let known:i64=tx.query_row("SELECT count(*) FROM code_files WHERE codebase=?1",params![scope_key(scope,&receipt.checkout_id)],|r|r.get(0)).map_err(database_error)?;
        if known!=files.len() as i64{return Err(failure(CodeEvidenceErrorCode::ReindexRequired,"Rich source records are incomplete"));}
        let mut resolutions=Vec::new();{
            let mut stmt=tx.prepare("SELECT occurrence_id,payload_json FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 AND resolution_generation=?4 ORDER BY occurrence_id").map_err(database_error)?;
            for row in stmt.query_map(params![scope.owner,scope.workspace_id,receipt.checkout_id,receipt.head_generation],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).map_err(database_error)?{resolutions.push(row.map_err(database_error)?);}
        }
        binding.validate_authority(&authority).map_err(query_authority_error)?;
        if crate::code::directory_identity(&binding.root).map_err(authority_error)?!=identity{return Err(failure(CodeEvidenceErrorCode::Denied,"Workspace directory changed"));}
        assert_receipt(&current_receipt(&tx,scope,binding,Some(&receipt.checkout_id))?,&receipt)?;
        tx.commit().map_err(database_error)?;
        Ok(super::code_query::RawQuerySnapshot{receipt,registration_id,snapshot,coverage,files,resolutions,identity})
    }

    pub(crate) fn code_query_receipt_check(&self,scope:&GraphScope,binding:&WorkspaceBinding,expected:&CodeEvidenceReceipt,identity:&str)->Result<(),CodeEvidenceError>{
        let query_authority_error=|message:String|failure(if message.starts_with("denied:"){CodeEvidenceErrorCode::Denied}else{CodeEvidenceErrorCode::Unavailable},"Code query authority unavailable");
        assert_scope(scope,binding)?;let authority=binding.publication_guard().map_err(query_authority_error)?;
        let conn=self.conn.lock().unwrap();
        assert_receipt(&current_receipt(&conn,scope,binding,Some(&expected.checkout_id))?,expected)?;
        if crate::code::directory_identity(&binding.root).map_err(authority_error)?!=identity{return Err(failure(CodeEvidenceErrorCode::Denied,"Workspace directory changed"));}
        binding.validate_authority(&authority).map_err(query_authority_error)
    }

    fn validate_evidence_admission(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        receipt: &CodeEvidenceReceipt,
    ) -> Result<(), CodeEvidenceError> {
        let _authority = binding.publication_guard().map_err(authority_error)?;
        let conn = self.conn.lock().unwrap();
        assert_receipt(
            &current_receipt(&conn, scope, binding, Some(&receipt.checkout_id))?,
            receipt,
        )
    }
    /// Bounded observed-source validation, not an atomic filesystem snapshot or
    /// proof of runtime behavior. These reads never enqueue or admit an index.
    pub fn code_validate_source(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        receipt: &CodeEvidenceReceipt,
        claim: &crate::code_evidence::SourceClaim,
    ) -> Result<crate::code_evidence::EvidenceValidation, CodeEvidenceError> {
        self.validate_current_evidence(scope, binding, receipt, ValidationRequest::Source(claim))
    }
    /// Successful acquired bytes across BOTH passes, not all kernel I/O (a
    /// failed OS read may have done unobserved work). Old API stays unchanged.
    pub fn code_validate_source_measured(
        &self,scope:&GraphScope,binding:&WorkspaceBinding,receipt:&CodeEvidenceReceipt,
        claim:&crate::code_evidence::SourceClaim,
    )->(Result<crate::code_evidence::EvidenceValidation,CodeEvidenceError>,usize){
        let mut bytes=0;
        let result=self.validate_current_evidence_measured(scope,binding,receipt,ValidationRequest::Source(claim),&mut bytes);
        (result,bytes)
    }
    pub fn code_validate_resolution(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        receipt: &CodeEvidenceReceipt,
        claim: &ResolutionEvidence,
    ) -> Result<crate::code_evidence::EvidenceValidation, CodeEvidenceError> {
        self.validate_current_evidence(
            scope,
            binding,
            receipt,
            ValidationRequest::Resolution(claim),
        )
    }
    pub fn code_validate_path(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        receipt: &CodeEvidenceReceipt,
        occurrences: &[String],
    ) -> Result<crate::code_evidence::EvidenceValidation, CodeEvidenceError> {
        self.validate_current_evidence(
            scope,
            binding,
            receipt,
            ValidationRequest::Path(occurrences),
        )
    }
    fn validate_current_evidence(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        expected: &CodeEvidenceReceipt,
        request: ValidationRequest<'_>,
    ) -> Result<crate::code_evidence::EvidenceValidation, CodeEvidenceError> {
        self.validate_current_evidence_measured(scope,binding,expected,request,&mut 0)
    }
    fn validate_current_evidence_measured(
        &self,scope:&GraphScope,binding:&WorkspaceBinding,expected:&CodeEvidenceReceipt,
        request:ValidationRequest<'_>,successful_acquired_bytes:&mut usize,
    )->Result<crate::code_evidence::EvidenceValidation,CodeEvidenceError>{
        if let ValidationRequest::Source(claim)=&request {
            if let Some(result)=self.partition_validate_source_measured(scope,binding,expected,claim,successful_acquired_bytes)?{return Ok(result);}
        }
        if let ValidationRequest::Resolution(claim)=&request {
            if let Some(result)=self.partition_validate_resolution(scope,binding,expected,claim)?{return Ok(result);}
        }
        if let ValidationRequest::Path(ids)=&request {
            if let Some(result)=self.partition_validate_path(scope,binding,expected,ids)?{return Ok(result);}
        }
        use crate::code_evidence::{EvidenceValidation, ValidationState};
        assert_scope(scope, binding)?;
        let authority = binding.publication_guard().map_err(authority_error)?;
        let identity = crate::code::directory_identity(&binding.root).map_err(authority_error)?;
        let conn = self.conn.lock().unwrap();
        let receipt = current_receipt(&conn, scope, binding, Some(&expected.checkout_id))?;
        assert_receipt(&receipt, expected)?;
        if availability(&conn, scope, &receipt)?.0 == CodeEvidenceAvailability::ReindexRequired {
            return Err(failure(
                CodeEvidenceErrorCode::ReindexRequired,
                "rich evidence requires reindex",
            ));
        }
        validate_persisted_headers(&conn, scope, &receipt)?;
        let base = load_base(&conn, scope, &receipt.checkout_id).map_err(|error| {
            failure(
                if error.starts_with("budget_exceeded:") {
                    CodeEvidenceErrorCode::BudgetExceeded
                } else {
                    CodeEvidenceErrorCode::InvalidEvidence
                },
                "invalid persisted corpus",
            )
        })?;
        let unavailable_files = current_unavailable(&conn, scope, &receipt)?;
        drop(conn);
        drop(authority);
        let stale = || EvidenceValidation {
            state: ValidationState::Stale,
            reason: "observed source differs from published evidence".into(),
        };
        // A published corpus is not authority to reacquire a path excluded by
        // today's repository policy. This metadata/policy-only scan happens
        // outside SQLite/authority locks and before either source-read pass.
        let policy_error = |_| failure(CodeEvidenceErrorCode::ReindexRequired,
            "repository ignore policy changed or is unavailable; reindex against stable repository ignore rules");
        let selection = crate::code::scan_codebase_pinned(&binding.root, &identity).map_err(policy_error)?;
        let policy = selection.policy;
        if base.policy_digest.as_deref() != Some(policy.digest().as_str()) {
            return Err(policy_error(String::new()));
        }
        let mut bytes = 0usize;
        let mut observed = BTreeMap::new();
        let mut files = Vec::new();
        let mut result = None;
        for (rel, item) in &base.files {
            validate_relative_path(rel)?;
            self.validate_evidence_admission(scope, binding, &receipt)?;
            policy.validate_source(&binding.root, &identity, &binding.root.join(rel)).map_err(policy_error)?;
            #[cfg(test)]
            validation_read_trace(&binding.root, rel);
            let acquired = match crate::code::acquire_source_pinned(
                &binding.root,
                &binding.root.join(rel),
                &identity,
            ) {
                Ok(value) => value,
                Err(_) => {
                    result = Some(stale());
                    break;
                }
            };
            bytes = bytes.checked_add(acquired.bytes.len()).ok_or_else(|| {
                failure(
                    CodeEvidenceErrorCode::BudgetExceeded,
                    "source acquisition budget exceeded",
                )
            })?;
            *successful_acquired_bytes=bytes;
            policy.validate_source(&binding.root, &identity, &binding.root.join(rel)).map_err(policy_error)?;
            if bytes > MAX_CORPUS_EVIDENCE_BYTES {
                return Err(failure(
                    CodeEvidenceErrorCode::BudgetExceeded,
                    "source acquisition budget exceeded",
                ));
            }
            if blake3::hash(&acquired.bytes).to_hex().as_str() != item.file.source_hash {
                result = Some(stale());
                break;
            }
            // Reparse current bytes rather than treating persisted IDs/hash
            // equality alone as proof that semantic predicates are valid.
            let current = crate::code_evidence::extract_evidence(
                &receipt.checkout_id,
                rel,
                &acquired.bytes,
                &ExtractionBudget::default(),
            )
            .map_err(producer_error)?;
            if current != item.file {
                return Err(failure(
                    CodeEvidenceErrorCode::InvalidEvidence,
                    "persisted semantics differ from current source",
                ));
            }
            observed.insert(rel.clone(), acquired.bytes);
            files.push(current);
            #[cfg(test)]
            if observed.len() == 1 {
                validation_test_pause(&binding.root);
            }
        }
        let result = if let Some(value) = result {
            value
        } else {
            let input = CorpusInput {
                checkout_id: receipt.checkout_id.clone(),
                files,
                unavailable_files,
            };
            let validation = match request {
                ValidationRequest::Source(claim) => {
                    validate_relative_path(&claim.source.rel_path)?;
                    let file = base.files.get(&claim.source.rel_path).ok_or_else(|| {
                        failure(
                            CodeEvidenceErrorCode::Missing,
                            "source is not in the published corpus",
                        )
                    })?;
                    crate::code_evidence::validate_source_evidence(
                        &file.file,
                        &observed[&claim.source.rel_path],
                        claim,
                        &ExtractionBudget::default(),
                    )
                }
                ValidationRequest::Resolution(claim) => {
                    crate::code_evidence::validate_resolution_evidence(
                        &input,
                        claim,
                        &ResolutionBudget::default(),
                    )
                }
                ValidationRequest::Path(ids) => {
                    crate::code_evidence::resolve_evidence(&input, &ResolutionBudget::default())
                        .and_then(|corpus| {
                            crate::code_evidence::validate_path_evidence(
                                &corpus,
                                ids,
                                &ResolutionBudget::default(),
                            )
                        })
                }
            };
            validation.map_err(producer_error)?
        };
        // No filesystem crawl or extraction holds authority/SQLite locks.
        // Charge BOTH acquisition passes to the aggregate I/O budget.
        let mut final_result = result;
        for (rel, prior) in &observed {
            self.validate_evidence_admission(scope, binding, &receipt)?;
            policy.validate_source(&binding.root, &identity, &binding.root.join(rel)).map_err(policy_error)?;
            #[cfg(test)]
            validation_read_trace(&binding.root, rel);
            match crate::code::acquire_source_pinned(
                &binding.root,
                &binding.root.join(rel),
                &identity,
            ) {
                Ok(current) => {
                    bytes = bytes.checked_add(current.bytes.len()).ok_or_else(|| {
                        failure(
                            CodeEvidenceErrorCode::BudgetExceeded,
                            "source acquisition budget exceeded",
                        )
                    })?;
                    *successful_acquired_bytes=bytes;
                    policy.validate_source(&binding.root, &identity, &binding.root.join(rel)).map_err(policy_error)?;
                    if bytes > MAX_CORPUS_EVIDENCE_BYTES {
                        return Err(failure(
                            CodeEvidenceErrorCode::BudgetExceeded,
                            "source acquisition budget exceeded",
                        ));
                    }
                    if current.bytes != *prior {
                        final_result = stale();
                        break;
                    }
                }
                Err(_) => {
                    final_result = stale();
                    break;
                }
            }
        }
        policy.validate(&binding.root, &identity).map_err(policy_error)?;
        let authority = binding.publication_guard().map_err(authority_error)?;
        let conn = self.conn.lock().unwrap();
        if crate::code::directory_identity(&binding.root).map_err(authority_error)? != identity {
            return Err(failure(
                CodeEvidenceErrorCode::Denied,
                "workspace directory identity changed",
            ));
        }
        binding
            .validate_authority(&authority)
            .map_err(authority_error)?;
        assert_receipt(
            &current_receipt(&conn, scope, binding, Some(&receipt.checkout_id))?,
            &receipt,
        )?;
        Ok(final_result)
    }
    /// Metadata only: never admits or queues a workspace. Binding scope is
    /// checked BEFORE authority or candidate lookup, including valid strangers.
    pub fn code_evidence_receipt(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
    ) -> Result<CodeEvidenceReceipt, CodeEvidenceError> {
        assert_scope(scope, binding)?;
        let authority = binding.publication_guard().map_err(authority_error)?;
        let conn = self.conn.lock().unwrap();
        let receipt = current_receipt(&conn, scope, binding, None)?;
        if availability(&conn, scope, &receipt)?.0 == CodeEvidenceAvailability::ReindexRequired {
            return Err(failure(
                CodeEvidenceErrorCode::ReindexRequired,
                "rich code evidence requires explicit reindex",
            ));
        }
        binding
            .validate_authority(&authority)
            .map_err(authority_error)?;
        assert_receipt(
            &current_receipt(&conn, scope, binding, Some(&receipt.checkout_id))?,
            &receipt,
        )?;
        Ok(receipt)
    }

    pub fn code_evidence_page(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        expected: &CodeEvidenceReceipt,
        query: CodeEvidenceQuery,
    ) -> Result<CodeEvidencePage, CodeEvidenceError> {
        assert_scope(scope, binding)?;
        validate_query(&query)?;
        if let Some(page)=self.partitioned_evidence_page(scope,binding,expected,&query)?{return Ok(page);}
        let authority = binding.publication_guard().map_err(authority_error)?;
        let conn = self.conn.lock().unwrap();
        let receipt = current_receipt(&conn, scope, binding, Some(&expected.checkout_id))?;
        assert_receipt(&receipt, expected)?;
        let (state, source_files, evidence_files) = availability(&conn, scope, &receipt)?;
        if source_files > MAX_CORPUS_FILES || evidence_files > MAX_CORPUS_FILES {
            return Err(failure(
                CodeEvidenceErrorCode::BudgetExceeded,
                "evidence file budget exceeded",
            ));
        }
        let fingerprint = blake3::hash(
            &serde_json::to_vec(&(
                &scope.owner,
                &scope.workspace_id,
                &receipt,
                query.kind,
                &query.rel_path,
                query.max_rows,
                query.max_bytes,
            ))
            .map_err(|_| failure(CodeEvidenceErrorCode::Invalid, "invalid evidence query"))?,
        )
        .to_hex()
        .to_string();
        let cursor = if let Some(value) = &query.cursor {
            let cursor: EvidenceCursor = serde_json::from_str(value).map_err(|_| {
                failure(
                    CodeEvidenceErrorCode::Invalid,
                    "invalid evidence continuation",
                )
            })?;
            if cursor.version != 1 || cursor.fingerprint != fingerprint {
                return Err(failure(
                    CodeEvidenceErrorCode::StaleCursor,
                    "evidence continuation scope, generation or filters changed",
                ));
            }
            validate_relative_path(&cursor.after_file)?;
            cursor
        } else {
            EvidenceCursor {
                version: 1,
                fingerprint: fingerprint.clone(),
                after_file: String::new(),
                after_id: String::new(),
            }
        };
        let mut page = CodeEvidencePage {
            receipt: receipt.clone(),
            kind: query.kind,
            availability: state.clone(),
            source_files,
            evidence_files,
            rows: Vec::new(),
            next_cursor: None,
            returned_rows: 0,
            returned_bytes: 0,
            partial: state == CodeEvidenceAvailability::Partial,
            total_matches: None,
            current_source_verified: false,
        };
        if measure_page(&mut page)? > query.max_bytes {
            return Err(failure(
                CodeEvidenceErrorCode::BudgetExceeded,
                "evidence envelope exceeds byte budget",
            ));
        }
        if state != CodeEvidenceAvailability::ReindexRequired {
            validate_persisted_headers(&conn, scope, &receipt)?;
            let (from,id,payload)=match query.kind {
                CodeEvidenceKind::Files=>("code_file_evidence f".to_string(),"json_extract(f.payload_json,'$.file_id')".to_string(),"json_object('file_id',json_extract(f.payload_json,'$.file_id'),'rel_path',f.rel_path,'source_hash',f.source_hash,'source_generation',f.source_generation,'source_len',json_extract(f.payload_json,'$.source_len'),'format_version',f.evidence_version,'producer_fingerprint',f.config_fingerprint,'language',json_extract(f.payload_json,'$.language'),'capability',json_extract(f.payload_json,'$.capability'),'public_surface_fingerprint',json_extract(f.payload_json,'$.public_surface_fingerprint'))".to_string()),
                CodeEvidenceKind::Declarations=>("code_file_evidence f,json_each(f.payload_json,'$.declarations') j".into(),"json_extract(j.value,'$.id')".into(),"j.value".into()),
                CodeEvidenceKind::Occurrences=>("code_file_evidence f,json_each(f.payload_json,'$.occurrences') j".into(),"json_extract(j.value,'$.id')".into(),"j.value".into()),
                CodeEvidenceKind::Resolutions=>("code_resolutions r JOIN code_file_evidence f ON f.owner=r.owner AND f.workspace_id=r.workspace_id AND f.checkout_id=r.checkout_id AND f.rel_path=r.rel_path AND f.source_hash=r.source_hash AND f.source_generation=r.source_generation".into(),"r.occurrence_id".into(),"r.payload_json".into()),
            };
            let resolution_filter = if query.kind == CodeEvidenceKind::Resolutions {
                "AND r.resolution_generation=?10"
            } else {
                "AND ?10 IS NOT NULL"
            };
            // LIMIT and byte-length guards execute in SQLite BEFORE payload
            // candidate materialization in Rust, after capped whole-corpus
            // typed/provenance preflight. This is not a constant-cost page.
            let sql=format!("WITH candidates AS (SELECT f.rel_path,{id} evidence_id,{payload} payload,f.source_hash,f.source_generation,json_extract(f.payload_json,'$.file_id') file_id,json_extract(f.payload_json,'$.source_len') source_len FROM {from} WHERE f.owner=?1 AND f.workspace_id=?2 AND f.checkout_id=?3 AND (?4 IS NULL OR f.rel_path=?4) AND (f.rel_path>?5 OR (f.rel_path=?5 AND {id}>?6)) {resolution_filter} ORDER BY f.rel_path,{id} LIMIT ?7) SELECT rel_path,evidence_id,length(CAST(payload AS BLOB)),CASE WHEN length(CAST(payload AS BLOB))<=?8 THEN payload ELSE NULL END,source_hash,source_generation,file_id,source_len FROM candidates WHERE ?9=1");
            let mut statement = conn.prepare(&sql).map_err(database_error)?;
            let mut rows = statement
                .query(params![
                    scope.owner,
                    scope.workspace_id,
                    receipt.checkout_id,
                    query.rel_path,
                    cursor.after_file,
                    cursor.after_id,
                    (query.max_rows + 1) as i64,
                    query.max_bytes as i64,
                    1,
                    receipt.head_generation
                ])
                .map_err(database_error)?;
            let mut pending = next_candidate(&mut rows)?;
            while let Some(candidate) = pending {
                let following = next_candidate(&mut rows)?;
                if page.rows.len() == query.max_rows {
                    page.partial = true;
                    break;
                }
                let Some(payload) = candidate.payload.as_ref() else {
                    if page.rows.is_empty() {
                        return Err(failure(
                            CodeEvidenceErrorCode::BudgetExceeded,
                            "first evidence row exceeds byte budget",
                        ));
                    }
                    page.partial = true;
                    break;
                };
                let value = validate_candidate(query.kind, &candidate, payload)?;
                let wrapped = serde_json::json!({"rel_path":candidate.rel_path,"source_hash":candidate.source_hash,"source_generation":candidate.source_generation,"evidence":value});
                let previous_cursor = page.next_cursor.clone();
                page.rows.push(wrapped);
                page.next_cursor = if following.is_some() {
                    Some(
                        serde_json::to_string(&EvidenceCursor {
                            version: 1,
                            fingerprint: fingerprint.clone(),
                            after_file: candidate.rel_path,
                            after_id: candidate.id,
                        })
                        .unwrap(),
                    )
                } else {
                    None
                };
                page.partial = state == CodeEvidenceAvailability::Partial || following.is_some();
                if measure_page(&mut page)? > query.max_bytes {
                    page.rows.pop();
                    page.next_cursor = previous_cursor;
                    page.partial = true;
                    if page.rows.is_empty() {
                        return Err(failure(
                            CodeEvidenceErrorCode::BudgetExceeded,
                            "first evidence row and wrappers exceed byte budget",
                        ));
                    }
                    break;
                }
                pending = following;
            }
        }
        measure_page(&mut page)?;
        binding
            .validate_authority(&authority)
            .map_err(authority_error)?;
        assert_receipt(
            &current_receipt(&conn, scope, binding, Some(&receipt.checkout_id))?,
            &receipt,
        )?;
        Ok(page)
    }
}

#[cfg(test)]
type ValidationGate = std::sync::Arc<(std::sync::Barrier, std::sync::Barrier)>;
#[cfg(test)]
type ValidationTrace = std::sync::Arc<std::sync::Mutex<Vec<String>>>;
#[cfg(test)]
static VALIDATION_GATES: std::sync::OnceLock<
    std::sync::Mutex<
        std::collections::HashMap<std::path::PathBuf, (ValidationGate, ValidationTrace)>,
    >,
> = std::sync::OnceLock::new();
#[cfg(test)]
pub(crate) fn pause_next_validation(root: &std::path::Path) -> (ValidationGate, ValidationTrace) {
    let gate = std::sync::Arc::new((std::sync::Barrier::new(2), std::sync::Barrier::new(2)));
    let trace = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    VALIDATION_GATES
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .insert(root.to_path_buf(), (gate.clone(), trace.clone()));
    (gate, trace)
}
#[cfg(test)]
pub(crate) fn trace_validation_reads(root: &std::path::Path) -> ValidationTrace {
    let trace = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let gate = std::sync::Arc::new((std::sync::Barrier::new(1), std::sync::Barrier::new(1)));
    VALIDATION_GATES.get_or_init(Default::default).lock().unwrap().insert(root.to_path_buf(), (gate, trace.clone()));
    trace
}
#[cfg(test)]
fn validation_read_trace(root: &std::path::Path, rel: &str) {
    if let Some((_, trace)) = VALIDATION_GATES
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .get(root)
    {
        trace.lock().unwrap().push(rel.into());
    }
}
#[cfg(test)]
fn validation_test_pause(root: &std::path::Path) {
    let gate = VALIDATION_GATES
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .get(root)
        .map(|(gate, _)| gate.clone());
    if let Some(gate) = gate {
        gate.0.wait();
        gate.1.wait();
    }
}
#[cfg(test)]
pub(crate) fn finish_validation_fixture(root: &std::path::Path) {
    VALIDATION_GATES.get().unwrap().lock().unwrap().remove(root);
}

enum ValidationRequest<'a> {
    Source(&'a crate::code_evidence::SourceClaim),
    Resolution(&'a ResolutionEvidence),
    Path(&'a [String]),
}

fn current_unavailable(
    conn: &Connection,
    scope: &GraphScope,
    receipt: &CodeEvidenceReceipt,
) -> Result<Vec<UnavailableFile>, CodeEvidenceError> {
    let coverage = read_coverage(conn, scope, receipt, MAX_CORPUS_EVIDENCE_BYTES, 1_000_000)?;
    Ok(coverage
        .into_iter()
        .filter(crate::code::eligible_unavailable)
        .map(|entry| UnavailableFile {
            rel_path: entry.rel_path,
            source_hash: entry.source_hash,
            reason: entry.status,
        })
        .collect())
}

fn read_coverage(
    conn: &Connection,
    scope: &GraphScope,
    receipt: &CodeEvidenceReceipt,
    max_bytes: usize,
    max_records: usize,
) -> Result<Vec<crate::code::ScanCoverage>, CodeEvidenceError> {
    // CASE gates JSON inspection on serialized byte size, and gates payload
    // transfer on array shape/record count. No oversized String is materialized
    // in Rust and no json_each traverses unchecked coverage.
    let row:Option<(i64,Option<String>,Option<i64>,Option<String>)>=conn.query_row(
        "WITH sized AS (SELECT length(CAST(coverage AS BLOB)) bytes,CASE WHEN length(CAST(coverage AS BLOB))<=?5 THEN coverage ELSE NULL END safe_json FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4),validated AS (SELECT bytes,CASE WHEN json_valid(safe_json) THEN safe_json ELSE NULL END valid_json FROM sized),shaped AS (SELECT bytes,CASE WHEN json_type(valid_json)='array' THEN valid_json ELSE NULL END array_json FROM validated) SELECT bytes,json_type(array_json),json_array_length(array_json),CASE WHEN json_array_length(array_json)<=?6 THEN array_json ELSE NULL END FROM shaped",
        params![scope.owner,scope.workspace_id,receipt.checkout_id,receipt.head_generation,max_bytes.min(MAX_CORPUS_EVIDENCE_BYTES) as i64,max_records.min(1_000_000) as i64],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(database_error)?;
    let (bytes, kind, records, json) = row.ok_or_else(|| {
        failure(
            CodeEvidenceErrorCode::Missing,
            "coverage receipt is unavailable",
        )
    })?;
    if bytes < 0
        || bytes as usize > max_bytes.min(MAX_CORPUS_EVIDENCE_BYTES)
        || records.is_some_and(|count| count < 0 || count as usize > max_records.min(1_000_000))
    {
        return Err(failure(
            CodeEvidenceErrorCode::BudgetExceeded,
            "coverage exceeds read budget",
        ));
    }
    if kind.as_deref() != Some("array") || records.is_none() {
        return Err(failure(
            CodeEvidenceErrorCode::InvalidEvidence,
            "coverage receipt is malformed",
        ));
    }
    let json = json.ok_or_else(|| {
        failure(
            CodeEvidenceErrorCode::InvalidEvidence,
            "coverage receipt is malformed",
        )
    })?;
    serde_json::from_str(&json).map_err(|_| {
        failure(
            CodeEvidenceErrorCode::InvalidEvidence,
            "coverage receipt is malformed",
        )
    })
}

#[cfg(test)]
pub(crate) fn coverage_test_budget(
    conn: &Connection,
    scope: &GraphScope,
    receipt: &CodeEvidenceReceipt,
    bytes: usize,
    records: usize,
) -> Result<usize, CodeEvidenceError> {
    read_coverage(conn, scope, receipt, bytes, records).map(|rows| rows.len())
}

struct Candidate {
    rel_path: String,
    id: String,
    payload: Option<String>,
    source_hash: String,
    source_generation: String,
    file_id: String,
    source_len: u64,
}
fn next_candidate(rows: &mut rusqlite::Rows<'_>) -> Result<Option<Candidate>, CodeEvidenceError> {
    let Some(row) = rows.next().map_err(database_error)? else {
        return Ok(None);
    };
    let candidate = Candidate {
        rel_path: row.get(0).map_err(database_error)?,
        id: row.get(1).map_err(database_error)?,
        payload: row.get(3).map_err(database_error)?,
        source_hash: row.get(4).map_err(database_error)?,
        source_generation: row.get(5).map_err(database_error)?,
        file_id: row.get(6).map_err(database_error)?,
        source_len: row.get(7).map_err(database_error)?,
    };
    Ok(Some(candidate))
}

fn validate_locator(
    source: &crate::code_evidence::SourceLocator,
    candidate: &Candidate,
) -> Result<(), CodeEvidenceError> {
    if source.rel_path != candidate.rel_path
        || source.source_hash != candidate.source_hash
        || source.file_id != candidate.file_id
        || source.span.start_byte > source.span.end_byte
        || source.span.end_byte > candidate.source_len
        || source.span.start_line == 0
        || source.span.end_line < source.span.start_line
    {
        return Err(failure(
            CodeEvidenceErrorCode::InvalidEvidence,
            "persisted source locator differs from file receipt",
        ));
    }
    Ok(())
}

fn validate_candidate(
    kind: CodeEvidenceKind,
    candidate: &Candidate,
    payload: &str,
) -> Result<serde_json::Value, CodeEvidenceError> {
    use crate::code_evidence::{DeclarationEvidence, OccurrenceEvidence, ResolutionEvidence};
    let invalid = || {
        failure(
            CodeEvidenceErrorCode::InvalidEvidence,
            "persisted typed evidence is malformed",
        )
    };
    match kind {
        CodeEvidenceKind::Declarations => {
            let value: DeclarationEvidence =
                serde_json::from_str(payload).map_err(|_| invalid())?;
            if value.id != candidate.id {
                return Err(invalid());
            }
            validate_locator(&value.source, candidate)?;
            if value.name_span.start_byte < value.source.span.start_byte
                || value.name_span.end_byte > value.source.span.end_byte
            {
                return Err(invalid());
            }
        }
        CodeEvidenceKind::Occurrences => {
            let value: OccurrenceEvidence = serde_json::from_str(payload).map_err(|_| invalid())?;
            if value.id != candidate.id {
                return Err(invalid());
            }
            validate_locator(&value.source, candidate)?;
        }
        CodeEvidenceKind::Resolutions => {
            let value: ResolutionEvidence = serde_json::from_str(payload).map_err(|_| invalid())?;
            if value.occurrence_id != candidate.id {
                return Err(invalid());
            }
            validate_locator(&value.source, candidate)?;
        }
        CodeEvidenceKind::Files => {
            let value: serde_json::Value = serde_json::from_str(payload).map_err(|_| invalid())?;
            let _: crate::code_evidence::LanguageCapability =
                serde_json::from_value(value["capability"].clone()).map_err(|_| invalid())?;
        }
    }
    serde_json::from_str(payload).map_err(|_| invalid())
}

fn validate_persisted_headers(
    conn: &Connection,
    scope: &GraphScope,
    receipt: &CodeEvidenceReceipt,
) -> Result<(), CodeEvidenceError> {
    // Preflight byte/row limits precede ALL JSON parsing. Typed deserialization
    // rejects absent mandatory fields (unlike nullable SQL json_type tests).
    // Each occurrence is visited once, not once for every resolution in a file.
    let base = load_base(conn, scope, &receipt.checkout_id).map_err(|error| {
        failure(
            if error.starts_with("budget_exceeded:") {
                CodeEvidenceErrorCode::BudgetExceeded
            } else {
                CodeEvidenceErrorCode::InvalidEvidence
            },
            "persisted evidence exceeds bounds or has invalid provenance",
        )
    })?;
    let (_, _, count) = availability(conn, scope, receipt)?;
    if base.files.len() != count || base.head.as_ref() != Some(&receipt.head_generation) {
        return Err(failure(
            CodeEvidenceErrorCode::InvalidEvidence,
            "persisted evidence receipt differs",
        ));
    }
    let mut occurrences = std::collections::BTreeMap::new();
    let mut records = 0usize;
    for file in base.files.values() {
        records = records
            .saturating_add(file.file.declarations.len())
            .saturating_add(file.file.occurrences.len())
            .saturating_add(file.file.imports.len())
            .saturating_add(file.file.bindings.len())
            .saturating_add(file.file.scopes.len());
        if records > 1_000_000 {
            return Err(failure(
                CodeEvidenceErrorCode::BudgetExceeded,
                "persisted evidence record budget exceeded",
            ));
        }
        for occurrence in &file.file.occurrences {
            if occurrences.insert(&occurrence.id, occurrence).is_some() {
                return Err(failure(
                    CodeEvidenceErrorCode::InvalidEvidence,
                    "duplicate persisted occurrence",
                ));
            }
        }
    }
    if occurrences.len() != base.resolutions.len() {
        return Err(failure(
            CodeEvidenceErrorCode::InvalidEvidence,
            "persisted resolutions are incomplete",
        ));
    }
    for (id, resolution) in &base.resolutions {
        let occurrence = occurrences.get(id).ok_or_else(|| {
            failure(
                CodeEvidenceErrorCode::InvalidEvidence,
                "persisted resolution has no occurrence",
            )
        })?;
        if resolution.source != occurrence.source || resolution.caller_id != occurrence.caller_id {
            return Err(failure(
                CodeEvidenceErrorCode::InvalidEvidence,
                "persisted resolution provenance differs",
            ));
        }
    }
    Ok(())
}

use crate::code_evidence::{
    CorpusEvidence, CorpusInput, ExtractionBudget, FileEvidence, ResolutionBudget,
    ResolutionEvidence, UnavailableFile,
};
use std::collections::BTreeMap;

#[derive(Clone)]
pub(crate) struct StoredFileEvidence {
    pub file: FileEvidence,
    pub source_generation: String,
}
pub(crate) struct CodeEvidenceBase {
    pub head: Option<String>,
    pub manifest: Option<String>,
    pub policy_digest: Option<String>,
    pub known: Vec<(String, String, i64, i64)>,
    pub files: BTreeMap<String, StoredFileEvidence>,
    pub resolutions: BTreeMap<String, ResolutionEvidence>,
}
pub(crate) struct PreparedCodeEvidence {
    pub files: BTreeMap<String, StoredFileEvidence>,
    pub corpus: CorpusEvidence,
    pub changed: bool,
}

fn scope_key(scope: &GraphScope, checkout: &str) -> String {
    format!(
        "code_v1:{}",
        serde_json::to_string(&[scope.owner.as_str(), scope.workspace_id.as_str(), checkout])
            .unwrap()
    )
}

fn check_table_budget(
    conn: &Connection,
    table: &str,
    scope: &GraphScope,
    checkout: &str,
    max_rows: usize,
    max_bytes: usize,
) -> Result<(), String> {
    let (rows,bytes):(i64,i64)=conn.query_row(&format!("SELECT count(*),COALESCE(sum(length(CAST(payload_json AS BLOB))),0) FROM {table} WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3"),params![scope.owner,scope.workspace_id,checkout],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|e|e.to_string())?;
    if rows < 0 || rows as usize > max_rows || bytes < 0 || bytes as usize > max_bytes {
        return Err("budget_exceeded: persisted code evidence exceeds corpus bounds".into());
    }
    Ok(())
}

/// Called inside the publication owner's single read snapshot, beginning
/// with the head BEFORE reading any source map or unchanged producer facts.
pub(crate) fn load_base(
    conn: &Connection,
    scope: &GraphScope,
    checkout: &str,
) -> Result<CodeEvidenceBase, String> {
    let head:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).optional().map_err(|e|e.to_string())?;
    let snapshot = if let Some(head) = &head {
        let value:String=conn.query_row("SELECT source_snapshot FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",params![scope.owner,scope.workspace_id,checkout,head],|r|r.get(0)).map_err(|e|e.to_string())?;
        Some(serde_json::from_str::<serde_json::Value>(&value)
            .map_err(|_| "invalid_evidence: malformed source snapshot")?)
    } else {
        None
    };
    let manifest = snapshot.as_ref().and_then(|s| s["file_manifest_digest"].as_str()).map(str::to_owned);
    let policy_digest = snapshot.as_ref().and_then(|s| s["selection_policy_digest"].as_str()).map(str::to_owned);
    let known = {
        let mut stmt=conn.prepare("SELECT rel_path,blake3,mtime_ns,size FROM code_files WHERE codebase=?1 ORDER BY rel_path LIMIT ?2").map_err(|e|e.to_string())?;
        let values = stmt
            .query_map(
                params![scope_key(scope, checkout), (MAX_CORPUS_FILES + 1) as i64],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .map_err(|e| e.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?;
        values
    };
    if known.len() > MAX_CORPUS_FILES {
        return Err("budget_exceeded: too many indexed files".into());
    }
    check_table_budget(
        conn,
        "code_file_evidence",
        scope,
        checkout,
        MAX_CORPUS_FILES,
        MAX_CORPUS_EVIDENCE_BYTES,
    )?;
    check_table_budget(
        conn,
        "code_resolutions",
        scope,
        checkout,
        1_000_000,
        MAX_RESOLUTION_BYTES,
    )?;
    // Only after the serialized corpus bound is established may SQLite inspect
    // JSON. Bound aggregate records before typed materialization/validation.
    let records:i64=conn.query_row("SELECT COALESCE(sum(COALESCE(json_array_length(payload_json,'$.declarations'),0)+COALESCE(json_array_length(payload_json,'$.occurrences'),0)+COALESCE(json_array_length(payload_json,'$.imports'),0)+COALESCE(json_array_length(payload_json,'$.bindings'),0)+COALESCE(json_array_length(payload_json,'$.scopes'),0)),0) FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).map_err(|e|e.to_string())?;
    if records < 0 || records > 1_000_000 {
        return Err("budget_exceeded: persisted evidence record count".into());
    }
    let fingerprint = crate::code::producer_fingerprint();
    let mut files = BTreeMap::new();
    let mut stmt=conn.prepare("SELECT rel_path,source_hash,source_generation,config_fingerprint,evidence_version,payload_json FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 AND config_fingerprint=?4 ORDER BY rel_path").map_err(|e|e.to_string())?;
    let mut rows = stmt
        .query(params![
            scope.owner,
            scope.workspace_id,
            checkout,
            fingerprint
        ])
        .map_err(|e| e.to_string())?;
    while let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let (rel, hash, generation, config, version, json): (
            String,
            String,
            String,
            String,
            u32,
            String,
        ) = (
            row.get(0).map_err(|e| e.to_string())?,
            row.get(1).map_err(|e| e.to_string())?,
            row.get(2).map_err(|e| e.to_string())?,
            row.get(3).map_err(|e| e.to_string())?,
            row.get(4).map_err(|e| e.to_string())?,
            row.get(5).map_err(|e| e.to_string())?,
        );
        let source_config: Option<String> = conn
            .query_row(
                "SELECT config_fingerprint FROM code_files WHERE codebase=?1 AND rel_path=?2",
                params![scope_key(scope, checkout), rel],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        if source_config
            .as_deref()
            .is_some_and(|value| value != config)
        {
            continue;
        } // explicit invalidation requires fresh extraction
        if json.len() > MAX_FILE_EVIDENCE_BYTES {
            return Err("budget_exceeded: stored file evidence".into());
        }
        let file: FileEvidence = serde_json::from_str(&json)
            .map_err(|_| "invalid_evidence: malformed typed file record")?;
        crate::code_evidence::validate_file_evidence(&file, &ExtractionBudget::default())
            .map_err(producer_failure)?;
        let matches:i64=conn.query_row("SELECT count(*) FROM code_files WHERE codebase=?1 AND rel_path=?2 AND blake3=?3 AND source_generation=?4 AND config_fingerprint=?5",params![scope_key(scope,checkout),rel,hash,generation,config],|r|r.get(0)).map_err(|e|e.to_string())?;
        if file.checkout_id != checkout
            || file.rel_path != rel
            || file.source_hash != hash
            || file.producer_fingerprint != config
            || version != EVIDENCE_VERSION
            || matches != 1
        {
            return Err("invalid_evidence: stored file provenance differs".into());
        }
        files.insert(
            rel,
            StoredFileEvidence {
                file,
                source_generation: generation,
            },
        );
    }
    let mut resolutions = BTreeMap::new();
    let mut stmt=conn.prepare("SELECT occurrence_id,rel_path,source_hash,source_generation,resolution_generation,payload_json FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 ORDER BY occurrence_id").map_err(|e|e.to_string())?;
    let mut rows = stmt
        .query(params![scope.owner, scope.workspace_id, checkout])
        .map_err(|e| e.to_string())?;
    while let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let (id, rel, hash, generation, resolution_generation, json): (
            String,
            String,
            String,
            String,
            String,
            String,
        ) = (
            row.get(0).map_err(|e| e.to_string())?,
            row.get(1).map_err(|e| e.to_string())?,
            row.get(2).map_err(|e| e.to_string())?,
            row.get(3).map_err(|e| e.to_string())?,
            row.get(4).map_err(|e| e.to_string())?,
            row.get(5).map_err(|e| e.to_string())?,
        );
        let Some(file) = files.get(&rel) else {
            continue;
        }; // older config requires re-extraction, never reuse it
        let resolution: ResolutionEvidence =
            serde_json::from_str(&json).map_err(|_| "invalid_evidence: malformed resolution")?;
        if resolution.occurrence_id != id
            || resolution.source.rel_path != rel
            || resolution.source.source_hash != hash
            || file.file.source_hash != hash
            || file.source_generation != generation
            || Some(&resolution_generation) != head.as_ref()
        {
            return Err("invalid_evidence: stored resolution provenance differs".into());
        }
        resolutions.insert(id, resolution);
    }
    Ok(CodeEvidenceBase {
        head,
        manifest,
        policy_digest,
        known,
        files,
        resolutions,
    })
}

pub(crate) fn prepare_evidence(
    base: &CodeEvidenceBase,
    checkout: &str,
    staged: &[crate::code::IndexedFile],
    gone: &[String],
    failed: &[String],
    coverage: &[crate::code::ScanCoverage],
    generation: &str,
) -> Result<PreparedCodeEvidence, String> {
    let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Interfaces);
    let mut files = base.files.clone();
    for path in gone.iter().chain(failed) {
        files.remove(path);
    }
    for indexed in staged {
        crate::code_evidence::validate_file_evidence(
            &indexed.evidence,
            &ExtractionBudget::default(),
        )
        .map_err(producer_failure)?;
        if indexed.evidence.checkout_id != checkout
            || indexed.evidence.rel_path != indexed.rel_path
            || indexed.evidence.source_hash != indexed.blake3
            || indexed.evidence.source_len != indexed.size as u64
        {
            return Err("invalid_evidence: extraction source receipt differs".into());
        }
        files.insert(
            indexed.rel_path.clone(),
            StoredFileEvidence {
                file: indexed.evidence.clone(),
                source_generation: generation.into(),
            },
        );
    }
    if files.len() > MAX_CORPUS_FILES {
        return Err("budget_exceeded: corpus file count".into());
    }
    let mut bytes = 0usize;
    for item in files.values() {
        let length = serde_json::to_vec(&item.file)
            .map_err(|e| e.to_string())?
            .len();
        if length > MAX_FILE_EVIDENCE_BYTES {
            return Err("budget_exceeded: file evidence".into());
        }
        bytes = bytes
            .checked_add(length)
            .ok_or("budget_exceeded: corpus overflow")?;
        if bytes > MAX_CORPUS_EVIDENCE_BYTES {
            return Err("budget_exceeded: corpus evidence".into());
        }
    }
    let unavailable_files = coverage
        .iter()
        .filter(|entry| crate::code::eligible_unavailable(entry))
        .map(|entry| UnavailableFile {
            rel_path: entry.rel_path.clone(),
            source_hash: entry.source_hash.clone(),
            reason: entry.status.clone(),
        })
        .collect();
    let input = CorpusInput {
        checkout_id: checkout.into(),
        files: files.values().map(|item| item.file.clone()).collect(),
        unavailable_files,
    };
    let corpus = crate::code_evidence::resolve_evidence(&input, &ResolutionBudget::default())
        .map_err(producer_failure)?;
    if serde_json::to_vec(&corpus)
        .map_err(|e| e.to_string())?
        .len()
        > MAX_RESOLUTION_BYTES
    {
        return Err("budget_exceeded: resolution output".into());
    }
    let resolutions: BTreeMap<_, _> = corpus
        .resolutions
        .iter()
        .map(|item| (item.occurrence_id.clone(), item.clone()))
        .collect();
    if resolutions.len() != corpus.resolutions.len() {
        return Err("invalid_evidence: duplicate resolution IDs".into());
    }
    let changed = resolutions != base.resolutions;
    Ok(PreparedCodeEvidence {
        files,
        corpus,
        changed,
    })
}

pub(crate) fn publish_evidence(
    conn: &Connection,
    scope: &GraphScope,
    checkout: &str,
    generation: &str,
    prepared: &PreparedCodeEvidence,
) -> rusqlite::Result<()> {
    conn.execute(
        "DELETE FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",
        params![scope.owner, scope.workspace_id, checkout],
    )?;
    conn.execute(
        "DELETE FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",
        params![scope.owner, scope.workspace_id, checkout],
    )?;
    for (rel, item) in &prepared.files {
        let json = serde_json::to_string(&item.file).map_err(|_| rusqlite::Error::InvalidQuery)?;
        conn.execute("INSERT INTO code_file_evidence(owner,workspace_id,checkout_id,rel_path,source_hash,source_generation,config_fingerprint,evidence_version,payload_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![scope.owner,scope.workspace_id,checkout,rel,item.file.source_hash,item.source_generation,item.file.producer_fingerprint,EVIDENCE_VERSION,json])?;
    }
    for resolution in &prepared.corpus.resolutions {
        let source = prepared
            .files
            .get(&resolution.source.rel_path)
            .ok_or(rusqlite::Error::InvalidQuery)?;
        let json = serde_json::to_string(resolution).map_err(|_| rusqlite::Error::InvalidQuery)?;
        conn.execute("INSERT INTO code_resolutions(owner,workspace_id,checkout_id,occurrence_id,rel_path,source_hash,source_generation,resolution_generation,payload_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![scope.owner,scope.workspace_id,checkout,resolution.occurrence_id,resolution.source.rel_path,resolution.source.source_hash,source.source_generation,generation,json])?;
    }
    Ok(())
}
