//! Read-only S05 adapter. SQL captures are bounded; typed parsing/ranking runs
//! only after the shared SQLite and authority guards have been released.
use super::code_evidence::{
    CodeEvidenceError, CodeEvidenceReceipt, MAX_CORPUS_FILES, MAX_FILE_EVIDENCE_BYTES,
};
use crate::{
    code::WorkspaceBinding, code_evidence::*, code_query::*, graph::GraphScope,
    store::sqlite::SqliteStore,
};
use rusqlite::params;
use std::sync::atomic::{AtomicBool, Ordering};
pub(crate) struct RawQuerySnapshot {
    pub receipt: CodeEvidenceReceipt,
    pub registration_id: String,
    pub snapshot: String,
    pub coverage: String,
    pub files: Vec<(String, String, String, String)>,
    pub resolutions: Vec<(String, String)>,
    pub identity: String,
}
fn failure(code: &str, message: &str) -> QueryError {
    QueryError {
        code: code.into(),
        message: message.into(),
    }
}
pub(super) fn store_error(error: CodeEvidenceError) -> QueryError {
    let code = serde_json::to_value(error.code)
        .unwrap()
        .as_str()
        .unwrap()
        .to_owned();
    failure(&code, &error.message)
}
impl SqliteStore {
    /// Optional current selection-policy fence for the existing owned read
    /// lane. Source-content scans call this at their boundaries, not per file.
    pub fn code_policy_fence(&self,scope:&GraphScope,binding:&WorkspaceBinding,expected:Option<&QueryReceipt>)->Result<String,QueryError>{
        let before=self.code_read_fence(scope,binding,expected)?;
        let identity=crate::code::directory_identity(&binding.root).map_err(|_|failure("denied","Code directory unavailable"))?;
        let scan=crate::code::scan_codebase_pinned(&binding.root,&identity).map_err(|_|failure("unavailable","Current code selection policy unavailable"))?;
        let digest=scan.policy.digest();
        if let Some(receipt)=expected {
            let conn=self.conn.lock().unwrap();
            let snapshot:Option<String>=conn.query_row("SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4 AND status IN('ready','verified')",rusqlite::params![scope.owner,scope.workspace_id,receipt.evidence.checkout_id,receipt.evidence.head_generation],|r|r.get(0)).map_err(|_|failure("reindex_required","Code policy snapshot unavailable"))?;
            let snapshot:crate::code::CodeSourceSnapshot=serde_json::from_str(&snapshot.ok_or_else(||failure("reindex_required","Code policy snapshot unavailable"))?).map_err(|_|failure("invalid_evidence","Invalid code policy snapshot"))?;
            if snapshot.selection_policy_digest!=digest{return Err(failure("reindex_required","Current selection policy requires index refresh"));}
        }
        if before!=self.code_read_fence(scope,binding,expected)?{return Err(failure("stale_cursor","Code selection authority changed"));}
        Ok(blake3::hash(&serde_json::to_vec(&(before,digest)).map_err(|_|failure("unavailable","Policy fence unavailable"))?).to_hex().to_string())
    }
    /// Private raw-read fence, including the deliberately missing-head state.
    /// No admission, registration or other row is created by this read.
    pub fn code_read_fence(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        expected: Option<&QueryReceipt>,
    ) -> Result<String, QueryError> {
        if scope.owner != binding.owner || scope.workspace_id != binding.workspace_id {
            return Err(failure("denied", "Code workspace scope differs"));
        }
        let authority = binding.publication_guard().map_err(|e| {
            failure(
                if e.starts_with("denied:") {
                    "denied"
                } else {
                    "unavailable"
                },
                "Code read authority unavailable",
            )
        })?;
        let identity = crate::code::directory_identity(&binding.root)
            .map_err(|_| failure("denied", "Code directory unavailable"))?;
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .transaction()
            .map_err(|_| failure("unavailable", "Code fence store unavailable"))?;
        let epochs = {
            let mut stmt=tx.prepare("SELECT workspace_id,admission_epoch,erased FROM code_scope_epochs WHERE owner=?1 AND workspace_id IN (?2,'*') ORDER BY CASE WHEN workspace_id=?2 THEN 0 ELSE 1 END").map_err(|_|failure("unavailable","Code scope fence unavailable"))?;
            let rows = stmt
                .query_map(params![scope.owner, scope.workspace_id], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, i64>(1)?,
                        r.get::<_, i64>(2)?,
                    ))
                })
                .map_err(|_| failure("unavailable", "Code scope fence unavailable"))?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|_| failure("unavailable", "Code scope fence unavailable"))?;
            rows
        };
        if epochs.first().is_some_and(|e| e.2 != 0) {
            return Err(failure(
                "erased",
                "Workspace code index was explicitly erased",
            ));
        }
        let rows = {
            let mut stmt=tx.prepare("SELECT c.registration_id,c.checkout_id,c.admission_epoch,c.lifecycle,c.config_fingerprint,h.run_id,r.config_fingerprint,CASE WHEN length(CAST(r.source_snapshot AS BLOB))<=65536 THEN r.source_snapshot END FROM code_registrations c LEFT JOIN code_index_heads h ON h.owner=c.owner AND h.workspace_id=c.workspace_id AND h.codebase=c.checkout_id LEFT JOIN code_index_runs r ON r.owner=h.owner AND r.workspace_id=h.workspace_id AND r.codebase=h.codebase AND r.run_id=h.run_id AND r.status IN ('ready','verified') WHERE c.owner=?1 AND c.workspace_id=?2 AND c.directory_identity=?3 AND c.owner_subject_id=?4 AND c.location_id=?5 ORDER BY c.checkout_id LIMIT 2").map_err(|_|failure("unavailable","Code resource fence unavailable"))?;
            let rows = stmt
                .query_map(
                    params![
                        scope.owner,
                        scope.workspace_id,
                        identity,
                        binding.owner_subject_id,
                        binding.location_id
                    ],
                    |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, String>(1)?,
                            r.get::<_, i64>(2)?,
                            r.get::<_, String>(3)?,
                            r.get::<_, String>(4)?,
                            r.get::<_, Option<String>>(5)?,
                            r.get::<_, Option<String>>(6)?,
                            r.get::<_, Option<String>>(7)?,
                        ))
                    },
                )
                .map_err(|_| failure("unavailable", "Code resource fence unavailable"))?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|_| failure("unavailable", "Code resource fence unavailable"))?;
            rows
        };
        if rows.len() > 1 {
            return Err(failure(
                "invalid_evidence",
                "Ambiguous code read registration",
            ));
        }
        if rows.first().is_some_and(|r| r.3 == "erased") {
            return Err(failure(
                "erased",
                "Workspace code index was explicitly erased",
            ));
        }
        tx.commit()
            .map_err(|_| failure("unavailable", "Code fence snapshot unavailable"))?;
        drop(conn);
        if let Some(expected) = expected {
            let row = rows
                .first()
                .ok_or_else(|| failure("stale_cursor", "Code read registration changed"))?;
            let snapshot: crate::code::CodeSourceSnapshot = serde_json::from_str(
                row.7
                    .as_deref()
                    .ok_or_else(|| failure("stale_cursor", "Code read head changed"))?,
            )
            .map_err(|_| failure("invalid_evidence", "Invalid source snapshot"))?;
            if expected.workspace_id != scope.workspace_id
                || expected.registration_id != row.0
                || expected.source_snapshot_id != snapshot.file_manifest_digest
                || expected.evidence.checkout_id != row.1
                || expected.evidence.admission_epoch != row.2
                || Some(&expected.evidence.head_generation) != row.5.as_ref()
                || Some(&expected.evidence.config_fingerprint) != row.6.as_ref()
                || expected.evidence.policy_generation != binding.policy_generation
            {
                return Err(failure("stale_cursor", "Code read receipt changed"));
            }
        }
        binding.validate_authority(&authority).map_err(|e| {
            failure(
                if e.starts_with("denied:") {
                    "denied"
                } else {
                    "unavailable"
                },
                "Code read authority changed",
            )
        })?;
        if crate::code::directory_identity(&binding.root).ok().as_ref() != Some(&identity) {
            return Err(failure("denied", "Code directory changed"));
        }
        Ok(blake3::hash(
            &serde_json::to_vec(&(binding, &identity, epochs, rows, crate::code::producer_fingerprint()))
                .map_err(|_| failure("unavailable", "Code fence serialization unavailable"))?,
        )
        .to_hex()
        .to_string())
    }
    pub fn code_query(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        query: &CodeQuery,
        cancel: &AtomicBool,
        cursor_secret: &[u8; 32],
    ) -> Result<CodeQueryResult, QueryError> {
        let _timing = crate::code_metrics::span(crate::code_metrics::Stage::QueryTotal);
        validate_query(query)?;
        if let Some(result)=self.partitioned_query(scope,binding,query,cancel,cursor_secret)?{return Ok(result);}
        if query
            .expected_receipt
            .as_ref()
            .is_some_and(|e| e.workspace_id != scope.workspace_id)
        {
            return Err(failure("denied", "Code workspace scope differs"));
        }
        let raw = self
            .code_query_snapshot_raw(
                scope,
                binding,
                query.expected_receipt.as_ref().map(|e| &e.evidence),
            )
            .map_err(store_error)?;
        let identity = raw.identity.clone();
        let snapshot = parse_snapshot(raw, scope, binding, cancel, query.budgets.work)?;
        let result = run_query(&snapshot, query, cancel, cursor_secret)?;
        self.code_query_receipt_check(scope, binding, &snapshot.receipt.evidence, &identity)
            .map_err(store_error)?;
        if cancel.load(Ordering::Relaxed) {
            return Err(failure("cancelled", "Code query cancelled"));
        }
        Ok(result)
    }
    pub fn code_query_check(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        expected: &QueryReceipt,
    ) -> Result<(), QueryError> {
        if expected.workspace_id != scope.workspace_id {
            return Err(failure("denied", "Code workspace scope differs"));
        }
        let identity = crate::code::directory_identity(&binding.root)
            .map_err(|_| failure("denied", "Workspace directory unavailable"))?;
        self.code_query_receipt_check(scope, binding, &expected.evidence, &identity)
            .map_err(store_error)
    }
}
fn parse_snapshot(
    raw: RawQuerySnapshot,
    scope: &GraphScope,
    binding: &WorkspaceBinding,
    cancel: &AtomicBool,
    max_work: usize,
) -> Result<QuerySnapshot, QueryError> {
    let _timing = crate::code_metrics::span(crate::code_metrics::Stage::QueryPrepare);
    let invalid = || {
        failure(
            "invalid_evidence",
            "Stored code evidence is malformed or inconsistent",
        )
    };
    let bytes = raw.files.iter().map(|(_, _, _, s)| s.len()).sum::<usize>()
        + raw.resolutions.iter().map(|(_, s)| s.len()).sum::<usize>()
        + raw.coverage.len()
        + raw.snapshot.len();
    if raw.files.len() > MAX_CORPUS_FILES {
        return Err(failure("budget_exceeded", "Too many code files"));
    }
    let source: crate::code::CodeSourceSnapshot =
        serde_json::from_str(&raw.snapshot).map_err(|_| invalid())?;
    let coverage: Vec<crate::code::ScanCoverage> =
        serde_json::from_str(&raw.coverage).map_err(|_| invalid())?;
    if coverage.len() > 1_000_000 {
        return Err(failure("budget_exceeded", "Too many coverage entries"));
    }
    let mut files = Vec::new();
    let mut occurrences = std::collections::BTreeMap::new();
    let mut records = coverage.len();
    if records > max_work {
        return Err(failure(
            "budget_exceeded",
            "Metadata coverage exceeds requested work budget",
        ));
    }
    for (rel, hash, generation, payload) in raw.files {
        if cancel.load(Ordering::Relaxed) {
            return Err(failure("cancelled", "Code query cancelled"));
        }
        if payload.len() > MAX_FILE_EVIDENCE_BYTES {
            return Err(failure(
                "budget_exceeded",
                "Code file evidence exceeds budget",
            ));
        }
        let file: FileEvidence = serde_json::from_str(&payload).map_err(|_| invalid())?;
        if file.rel_path != rel
            || file.source_hash != hash
            || file.checkout_id != raw.receipt.checkout_id
            || generation.is_empty()
        {
            return Err(invalid());
        }
        records = records.saturating_add(
            1 + file.scopes.len()
                + file.bindings.len()
                + file.declarations.len()
                + file.occurrences.len()
                + file.imports.len(),
        );
        if records > max_work {
            return Err(failure(
                "budget_exceeded",
                "Code evidence exceeds requested work budget",
            ));
        }
        validate_file_evidence(&file, &ExtractionBudget::default()).map_err(|_| invalid())?;
        for o in &file.occurrences {
            if occurrences
                .insert(o.id.clone(), (o.source.clone(), o.caller_id.clone()))
                .is_some()
            {
                return Err(invalid());
            }
        }
        files.push(file);
    }
    let mut resolutions = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for (id, payload) in raw.resolutions {
        if cancel.load(Ordering::Relaxed) {
            return Err(failure("cancelled", "Code query cancelled"));
        }
        let r: ResolutionEvidence = serde_json::from_str(&payload).map_err(|_| invalid())?;
        records = records
            .saturating_add(1 + r.candidate_ids.len() + r.witnesses.len() + r.dependencies.len());
        if records > max_work {
            return Err(failure(
                "budget_exceeded",
                "Code resolutions exceed requested work budget",
            ));
        }
        if r.occurrence_id != id
            || occurrences.get(&id) != Some(&(r.source.clone(), r.caller_id.clone()))
            || !seen.insert(id)
        {
            return Err(invalid());
        }
        resolutions.push(r);
    }
    if seen.len() != occurrences.len() {
        return Err(invalid());
    }
    Ok(QuerySnapshot {
        receipt: QueryReceipt {
            workspace_id: scope.workspace_id.clone(),
            registration_id: raw.registration_id,
            source_snapshot_id: source.file_manifest_digest,
            evidence: raw.receipt,
        },
        scope_key: serde_json::to_string(&(
            scope.owner.as_str(),
            binding.owner_subject_id.as_str(),
            scope.workspace_id.as_str(),
            binding.location_id.as_str(),
            binding.workspace_revision,
            binding.policy_generation,
            binding.origin.as_str(),
            binding.subscriber_id.as_str(),
        ))
        .unwrap(),
        files,
        resolutions,
        coverage,
        metadata_bytes: bytes,
        metadata_records: records,
        eligible_records:None,
    })
}
