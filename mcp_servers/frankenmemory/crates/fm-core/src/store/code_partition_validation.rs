//! Explicit Current/source validation for immutable partition generations.
//! Metadata never admits a source read. Source predicates acquire only their
//! requested file; relationship predicates stream selected sources twice.
//! Scanner-excluded unavailable sources remain metadata only, never Current bytes.
use super::code_evidence::{current_receipt, CodeEvidenceError, CodeEvidenceErrorCode, CodeEvidenceReceipt};
use super::code_partitioned::{is_partitioned, policy_fence, Generation};
use super::sqlite::SqliteStore;
use crate::code::{CodeSourceSnapshot, SelectionPolicy, WorkspaceBinding};
use crate::code_evidence::{EvidenceValidation, ExtractionBudget, FileEvidence, SourceClaim, ValidationState};
use crate::graph::GraphScope;
use rusqlite::{params, OptionalExtension};
use std::path::{Component, Path};
use std::collections::{BTreeMap, BTreeSet};

enum CorpusClaim<'a> {
    Resolution(&'a crate::code_evidence::ResolutionEvidence),
    Path(&'a [String]),
}

fn invalid(reason: &str) -> EvidenceValidation {
    EvidenceValidation { state: ValidationState::Invalid, reason: reason.into() }
}

// Metadata-only admission proof. Selected sources need pinned hashes. Sources
// excluded by the scanner instead need the exact unchanged unavailable boundary;
// neither their bytes nor their exports are treated as observed/current.
fn selected_sources(
    root: &Path, identity: &str, snapshot: &CodeSourceSnapshot,
    interfaces: &[FileEvidence], coverage: &[crate::code::ScanCoverage],
    unavailable: &[crate::code_evidence::UnavailableFile],
) -> Result<Option<BTreeMap<String,String>>,CodeEvidenceError> {
    let scan = crate::code::scan_codebase_pinned(root,identity).map_err(policy_error)?;
    if scan.policy.digest() != snapshot.selection_policy_digest { return Err(policy_error(String::new())); }
    if interfaces.len().saturating_add(unavailable.len()) > 10_000 {
        return Err(failure(CodeEvidenceErrorCode::BudgetExceeded,"source file limit"));
    }
    let indexed: BTreeMap<_,_> = interfaces.iter().map(|f|(f.rel_path.as_str(),f.source_hash.as_str())).collect();
    if indexed.len()!=interfaces.len() { return Err(failure(CodeEvidenceErrorCode::InvalidEvidence,"duplicate interface path")); }
    let current: BTreeSet<_> = scan.files.iter().map(|p|p.strip_prefix(root).ok().and_then(|p|p.to_str()).map(str::to_owned))
        .collect::<Option<_>>().ok_or_else(||failure(CodeEvidenceErrorCode::Invalid,"non-UTF8 current source"))?;
    let mut absent=BTreeMap::new();
    for file in unavailable {
        valid_path(&file.rel_path)?;
        if absent.insert(file.rel_path.as_str(),file).is_some() || indexed.contains_key(file.rel_path.as_str()) { return Ok(None); }
    }
    let mut expected = BTreeMap::new();
    let mut excluded = BTreeMap::new();
    for entry in coverage {
        if indexed.contains_key(entry.rel_path.as_str()) || crate::code::eligible_unavailable(entry) {
            valid_path(&entry.rel_path)?;
            if !indexed.contains_key(entry.rel_path.as_str()) {
                let Some(file)=absent.get(entry.rel_path.as_str()) else { return Ok(None); };
                if file.reason!=entry.status || file.source_hash!=entry.source_hash { return Ok(None); }
                if !current.contains(&entry.rel_path) {
                    if entry.source_hash.is_some() { return Ok(None); }
                    if excluded.insert(entry.rel_path.clone(),entry.status.clone()).is_some() {
                        return Err(failure(CodeEvidenceErrorCode::InvalidEvidence,"duplicate excluded coverage"));
                    }
                    continue;
                }
            }
            let Some(hash) = &entry.source_hash else { return Ok(None); };
            if hash.len()!=64 || !hash.bytes().all(|b|b.is_ascii_hexdigit()) { return Ok(None); }
            if expected.insert(entry.rel_path.clone(),hash.clone()).is_some() {
                return Err(failure(CodeEvidenceErrorCode::InvalidEvidence,"duplicate source coverage"));
            }
        }
    }
    for file in interfaces {
        if expected.get(&file.rel_path)!=Some(&file.source_hash) { return Ok(None); }
    }
    for file in unavailable {
        if file.source_hash.as_ref()!=expected.get(&file.rel_path) && !excluded.contains_key(&file.rel_path) { return Ok(None); }
    }
    if expected.len()+excluded.len()!=interfaces.len()+unavailable.len() { return Ok(None); }
    if current != expected.keys().cloned().collect() { return Ok(None); }
    let mut fresh_excluded=BTreeMap::new();
    for entry in &scan.coverage {
        if crate::code::eligible_unavailable(entry) && !current.contains(&entry.rel_path) {
            valid_path(&entry.rel_path)?;
            if entry.source_hash.is_some() { return Ok(None); }
            if fresh_excluded.insert(entry.rel_path.clone(),entry.status.clone()).is_some() {
                return Err(failure(CodeEvidenceErrorCode::InvalidEvidence,"duplicate scanner exclusion"));
            }
        }
    }
    if excluded!=fresh_excluded { return Ok(None); }
    Ok(Some(expected))
}

fn failure(code: CodeEvidenceErrorCode, message: &str) -> CodeEvidenceError {
    CodeEvidenceError { code, message: message.into() }
}
fn policy_error(_: String) -> CodeEvidenceError {
    failure(CodeEvidenceErrorCode::ReindexRequired, "repository ignore policy changed or is unavailable")
}
fn store_error(_: rusqlite::Error) -> CodeEvidenceError {
    failure(CodeEvidenceErrorCode::Unavailable, "partition validation store unavailable")
}
fn cache_error(message: String) -> CodeEvidenceError {
    failure(if message.starts_with("budget_exceeded:") { CodeEvidenceErrorCode::BudgetExceeded }
        else { CodeEvidenceErrorCode::ReindexRequired }, "partition cache requires bounded reindex")
}
fn producer_error(error: crate::code_evidence::EvidenceError) -> CodeEvidenceError {
    failure(if error.code==crate::code_evidence::EvidenceErrorCode::BudgetExceeded {CodeEvidenceErrorCode::BudgetExceeded}
        else {CodeEvidenceErrorCode::InvalidEvidence},"bounded current evidence validation failed")
}
fn fence_error(error: crate::code_query::QueryError) -> CodeEvidenceError {
    let code = match error.code.as_str() {
        "denied" => CodeEvidenceErrorCode::Denied,
        "erased" => CodeEvidenceErrorCode::Erased,
        "stale_cursor" => CodeEvidenceErrorCode::StaleCursor,
        "invalid_evidence" => CodeEvidenceErrorCode::InvalidEvidence,
        "reindex_required" => CodeEvidenceErrorCode::ReindexRequired,
        "budget_exceeded" => CodeEvidenceErrorCode::BudgetExceeded,
        "missing" => CodeEvidenceErrorCode::Missing,
        _ => CodeEvidenceErrorCode::Unavailable,
    };
    failure(code, "partition validation authority/head fence changed")
}
fn stale() -> EvidenceValidation {
    EvidenceValidation { state: ValidationState::Stale, reason: "observed source differs from published evidence".into() }
}
fn valid_path(path: &str) -> Result<(), CodeEvidenceError> {
    if path.is_empty() || path.len() > 4096 || path.contains('\0') || Path::new(path).is_absolute()
        || Path::new(path).components().any(|c| !matches!(c, Component::Normal(_))) {
        return Err(failure(CodeEvidenceErrorCode::Invalid, "invalid relative source locator"));
    }
    Ok(())
}

/// Measured independently from the result so a later policy/authority error
/// cannot erase the successful I/O charge from the first pass.
fn validate_target(
    root: &Path, identity: &str, policy: &SelectionPolicy, file: &FileEvidence,
    claim: &SourceClaim, bytes: &mut usize,
    mut admission: impl FnMut() -> Result<(), CodeEvidenceError>,
) -> Result<EvidenceValidation, CodeEvidenceError> {
    valid_path(&file.rel_path)?;
    let path = root.join(&file.rel_path);
    policy.validate(root, identity).map_err(policy_error)?;
    let mut first = None;
    let mut result = stale();
    for pass in 0..2 {
        admission()?;
        policy.validate_source(root, identity, &path).map_err(policy_error)?;
        let acquired = match crate::code::acquire_source_pinned(root, &path, identity) {
            Ok(acquired) => acquired,
            Err(_) => { result = stale(); break; }
        };
        *bytes = bytes.checked_add(acquired.bytes.len())
            .ok_or_else(|| failure(CodeEvidenceErrorCode::BudgetExceeded, "source acquisition budget overflow"))?;
        policy.validate_source(root, identity, &path).map_err(policy_error)?;
        if *bytes > super::code_partitioned::MAX_SOURCE {
            return Err(failure(CodeEvidenceErrorCode::BudgetExceeded, "source acquisition budget exceeded"));
        }
        if blake3::hash(&acquired.bytes).to_hex().as_str() != file.source_hash {
            result = stale(); break;
        }
        if pass == 0 {
            let budget = ExtractionBudget { max_serialized_bytes: crate::code_evidence::storage::MAX_DECODED, ..ExtractionBudget::default() };
            result = crate::code_evidence::validate_source_evidence(file, &acquired.bytes, claim, &budget)
                .map_err(|error| failure(if error.code == crate::code_evidence::EvidenceErrorCode::BudgetExceeded {
                    CodeEvidenceErrorCode::BudgetExceeded
                } else { CodeEvidenceErrorCode::InvalidEvidence }, "current source predicate validation failed"))?;
            first = Some(acquired.bytes);
        } else if first.as_deref() != Some(acquired.bytes.as_slice()) {
            result = stale();
        }
    }
    policy.validate(root, identity).map_err(policy_error)?;
    Ok(result)
}

impl SqliteStore {
    pub(crate) fn partition_validate_resolution(
        &self,scope:&GraphScope,binding:&WorkspaceBinding,expected:&CodeEvidenceReceipt,
        claim:&crate::code_evidence::ResolutionEvidence,
    )->Result<Option<EvidenceValidation>,CodeEvidenceError> {
        self.partition_validate_corpus(scope,binding,expected,CorpusClaim::Resolution(claim))
    }
    pub(crate) fn partition_validate_path(
        &self,scope:&GraphScope,binding:&WorkspaceBinding,expected:&CodeEvidenceReceipt,ids:&[String],
    )->Result<Option<EvidenceValidation>,CodeEvidenceError> {
        self.partition_validate_corpus(scope,binding,expected,CorpusClaim::Path(ids))
    }
    fn partition_validate_corpus(
        &self,scope:&GraphScope,binding:&WorkspaceBinding,expected:&CodeEvidenceReceipt,claim:CorpusClaim<'_>,
    )->Result<Option<EvidenceValidation>,CodeEvidenceError> {
        use crate::code_evidence::{self as evidence,CorpusInput,PartitionResolver,ResolutionBudget,UnavailableFile};
        if scope.owner!=binding.owner || scope.workspace_id!=binding.workspace_id {
            return Err(failure(CodeEvidenceErrorCode::Denied,"workspace evidence scope differs"));
        }
        let before=self.code_read_fence(scope,binding,None).map_err(fence_error)?;
        let snapshot={let conn=self.conn.lock().unwrap();
            let receipt=current_receipt(&conn,scope,binding,Some(&expected.checkout_id))?;
            if receipt!=*expected{return Err(failure(CodeEvidenceErrorCode::StaleCursor,"code evidence receipt changed"));}
            if !is_partitioned(&conn,scope,&receipt).map_err(cache_error)?{return Ok(None);}
            if receipt.config_fingerprint!=crate::code::producer_fingerprint(){return Err(failure(CodeEvidenceErrorCode::ReindexRequired,"partition producer changed"));}
            let text:Option<String>=conn.query_row("SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",params![scope.owner,scope.workspace_id,expected.checkout_id,expected.head_generation],|r|r.get(0)).map_err(store_error)?;
            serde_json::from_str::<CodeSourceSnapshot>(&text.ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"snapshot exceeds limit"))?).map_err(|_|failure(CodeEvidenceErrorCode::InvalidEvidence,"invalid source snapshot"))?
        };
        let (policy,identity)=policy_fence(binding,&snapshot).map_err(policy_error)?;
        let (generation,coverage)={let conn=self.conn.lock().unwrap();
            if current_receipt(&conn,scope,binding,Some(&expected.checkout_id))?!=*expected{return Err(failure(CodeEvidenceErrorCode::StaleCursor,"code evidence receipt changed"));}
            let text:Option<String>=conn.query_row("SELECT CASE WHEN length(CAST(coverage AS BLOB))<=67108864 THEN coverage END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",params![scope.owner,scope.workspace_id,expected.checkout_id,expected.head_generation],|r|r.get(0)).map_err(store_error)?;
            let coverage:Vec<crate::code::ScanCoverage>=serde_json::from_str(&text.ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"coverage exceeds limit"))?).map_err(|_|failure(CodeEvidenceErrorCode::InvalidEvidence,"invalid source coverage"))?;
            if coverage.len()>1_000_000{return Err(failure(CodeEvidenceErrorCode::BudgetExceeded,"coverage count exceeds limit"));}
            let generation=Generation::open(&conn,scope,expected).map_err(cache_error)?;
            generation.verify_stamp(&snapshot.evidence_cache_identity).map_err(cache_error)?;
            (generation,coverage)
        };
        let interfaces=generation.interfaces().map_err(cache_error)?;
        let text:Option<String>=generation.db.query_row("SELECT CASE WHEN length(CAST(value AS BLOB))<=67108864 THEN value END FROM meta WHERE key='unavailable'",[],|r|r.get(0)).map_err(store_error)?;
        let unavailable:Vec<UnavailableFile>=serde_json::from_str(&text.ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"unavailable coverage exceeds limit"))?).map_err(|_|failure(CodeEvidenceErrorCode::InvalidEvidence,"invalid unavailable coverage"))?;
        let sources=match selected_sources(&binding.root,&identity,&snapshot,&interfaces,&coverage,&unavailable)? {
            Some(value)=>value,None=>return Ok(Some(stale())),
        };
        let mut active=BTreeSet::new();
        let mut requested=BTreeSet::new();
        match &claim {
            CorpusClaim::Resolution(value)=>{valid_path(&value.source.rel_path)?;if value.occurrence_id.len()>128{return Err(failure(CodeEvidenceErrorCode::Invalid,"invalid resolution occurrence"));}active.insert(value.source.rel_path.clone());requested.insert(value.occurrence_id.clone());},
            CorpusClaim::Path(ids)=>{
                if ids.len()>ResolutionBudget::default().max_path_hops{return Err(failure(CodeEvidenceErrorCode::BudgetExceeded,"path hop limit"));}
                if ids.is_empty(){return Ok(Some(invalid("empty path")));}
                for id in *ids {
                    if id.len()>128{return Err(failure(CodeEvidenceErrorCode::Invalid,"invalid path occurrence"));}
                    requested.insert(id.clone());
                    let path:Option<Option<String>>=generation.db.query_row("SELECT CASE WHEN length(CAST(f.path AS BLOB))<=4096 THEN f.path END FROM edges e JOIN files f ON f.id=e.file WHERE e.occurrence=?1 LIMIT 1",params![id],|r|r.get(0)).optional().map_err(store_error)?;
                    let Some(Some(path))=path else{return Ok(Some(invalid("missing path occurrence")));};
                    valid_path(&path)?;active.insert(path);
                }
            }
        }
        if active.iter().any(|path|!interfaces.iter().any(|f|&f.rel_path==path)){return Ok(Some(invalid("claim source not indexed")));}
        let indexed:BTreeMap<_,_>=interfaces.iter().map(|f|(f.rel_path.as_str(),f)).collect();
        let admission=||{
            if self.code_read_fence(scope,binding,None).map_err(fence_error)?!=before{return Err(failure(CodeEvidenceErrorCode::StaleCursor,"code read generation changed"));}
            let conn=self.conn.lock().unwrap();
            if current_receipt(&conn,scope,binding,Some(&expected.checkout_id))?!=*expected{return Err(failure(CodeEvidenceErrorCode::StaleCursor,"code evidence receipt changed"));}
            Ok(())
        };
        let extraction=ExtractionBudget{max_serialized_bytes:evidence::storage::MAX_DECODED,..ExtractionBudget::default()};
        // At most64MiB per pass,128MiB actual total. Keep only one acquired
        // source/full DTO at a time; module interfaces have an independent
        // measured128MiB aggregate window; each decoded file remains64MiB.
        let mut first_bytes=0usize;
        let mut source_records=0usize;
        for (path,hash) in &sources {
            admission()?;policy.validate_source(&binding.root,&identity,&binding.root.join(path)).map_err(policy_error)?;
            let acquired=match crate::code::acquire_source_pinned(&binding.root,&binding.root.join(path),&identity){Ok(v)=>v,Err(_)=>return Ok(Some(stale()))};
            first_bytes=first_bytes.checked_add(acquired.bytes.len()).ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"source acquisition overflow"))?;
            if first_bytes>super::code_partitioned::MAX_SOURCE{return Err(failure(CodeEvidenceErrorCode::BudgetExceeded,"source acquisition pass exceeds64MiB"));}
            policy.validate_source(&binding.root,&identity,&binding.root.join(path)).map_err(policy_error)?;
            if blake3::hash(&acquired.bytes).to_hex().as_str()!=hash{return Ok(Some(stale()));}
            if let Some(interface)=indexed.get(path.as_str()) {
                let current=evidence::extract_evidence(&expected.checkout_id,path,&acquired.bytes,&extraction).map_err(producer_error)?;
                source_records=source_records.saturating_add(current.declarations.len()+current.occurrences.len()+current.bindings.len()+current.imports.len()+current.scopes.len());
                if source_records>super::code_partitioned::MAX_SOURCE_RECORDS{return Err(failure(CodeEvidenceErrorCode::BudgetExceeded,"source record quota"));}
                if evidence::storage::module_interface(&current)!=**interface{return Err(failure(CodeEvidenceErrorCode::InvalidEvidence,"published interface differs from reparsed current source"));}
            }
        }
        let input=CorpusInput{checkout_id:expected.checkout_id.clone(),files:interfaces,unavailable_files:unavailable};
        let resolver=PartitionResolver::new(&input).map_err(producer_error)?;
        let mut second_bytes=0usize;
        let mut proof=None;
        let mut resolutions=Vec::new();
        let mut proof_bytes=0usize;
        for (path,hash) in &sources {
            admission()?;policy.validate_source(&binding.root,&identity,&binding.root.join(path)).map_err(policy_error)?;
            let acquired=match crate::code::acquire_source_pinned(&binding.root,&binding.root.join(path),&identity){Ok(v)=>v,Err(_)=>return Ok(Some(stale()))};
            second_bytes=second_bytes.checked_add(acquired.bytes.len()).ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"source acquisition overflow"))?;
            if second_bytes>super::code_partitioned::MAX_SOURCE{return Err(failure(CodeEvidenceErrorCode::BudgetExceeded,"source acquisition pass exceeds64MiB"));}
            policy.validate_source(&binding.root,&identity,&binding.root.join(path)).map_err(policy_error)?;
            if blake3::hash(&acquired.bytes).to_hex().as_str()!=hash{return Ok(Some(stale()));}
            if active.contains(path) {
                let file=evidence::extract_evidence(&expected.checkout_id,path,&acquired.bytes,&extraction).map_err(producer_error)?;
                let mut result=resolver.resolve(&file).map_err(producer_error)?;
                for resolution in result.resolutions.drain(..).filter(|r|requested.contains(&r.occurrence_id)) {
                    let size=serde_json::to_vec(&resolution).map_err(|_|failure(CodeEvidenceErrorCode::InvalidEvidence,"invalid current resolution"))?.len();
                    proof_bytes=proof_bytes.checked_add(size).ok_or_else(||failure(CodeEvidenceErrorCode::BudgetExceeded,"current proof overflow"))?;
                    if proof_bytes>evidence::storage::MAX_DECODED{return Err(failure(CodeEvidenceErrorCode::BudgetExceeded,"current proof exceeds64MiB"));}
                    resolutions.push(resolution);
                }
                result.resolutions.clear();if proof.is_none(){proof=Some(result);}
            }
        }
        let mut corpus=proof.ok_or_else(||failure(CodeEvidenceErrorCode::InvalidEvidence,"no current claim source"))?;
        corpus.resolutions=resolutions;
        let result=match claim {
            CorpusClaim::Resolution(claim)=>EvidenceValidation{state:if corpus.resolutions.iter().any(|r|r==claim){ValidationState::Current}else{ValidationState::Invalid},reason:"resolution recomputed against two-pass selected source and unchanged unavailable-source boundary".into()},
            CorpusClaim::Path(ids)=>evidence::validate_path_evidence(&corpus,ids,&ResolutionBudget::default()).map_err(producer_error)?,
        };
        // Repeat membership/policy and final authority/head checks after all
        // source I/O. A new source path during either pass cannot be Current.
        admission()?;
        if selected_sources(&binding.root,&identity,&snapshot,&input.files,&coverage,&input.unavailable_files)?.as_ref()!=Some(&sources){return Ok(Some(stale()));}
        let authority=binding.publication_guard().map_err(|_|failure(CodeEvidenceErrorCode::Denied,"source authority changed"))?;
        let conn=self.conn.lock().unwrap();
        binding.validate_authority(&authority).map_err(|_|failure(CodeEvidenceErrorCode::Denied,"source authority changed"))?;
        if current_receipt(&conn,scope,binding,Some(&expected.checkout_id))?!=*expected{return Err(failure(CodeEvidenceErrorCode::StaleCursor,"code evidence receipt changed"));}
        if crate::code::directory_identity(&binding.root).map_err(|_|failure(CodeEvidenceErrorCode::Denied,"source directory unavailable"))?!=identity{return Err(failure(CodeEvidenceErrorCode::Denied,"source directory changed"));}
        generation.verify_stamp(&snapshot.evidence_cache_identity).map_err(cache_error)?;
        Ok(Some(result))
    }

    pub(crate) fn partition_validate_source(
        &self, scope: &GraphScope, binding: &WorkspaceBinding, expected: &CodeEvidenceReceipt, claim: &SourceClaim,
    ) -> Result<Option<(EvidenceValidation, usize)>, CodeEvidenceError> {
        let mut bytes = 0;
        self.partition_validate_source_measured(scope, binding, expected, claim, &mut bytes)
            .map(|result| result.map(|result| (result, bytes)))
    }

    pub(crate) fn partition_validate_source_measured(
        &self, scope: &GraphScope, binding: &WorkspaceBinding, expected: &CodeEvidenceReceipt,
        claim: &SourceClaim, bytes: &mut usize,
    ) -> Result<Option<EvidenceValidation>, CodeEvidenceError> {
        if scope.owner != binding.owner || scope.workspace_id != binding.workspace_id {
            return Err(failure(CodeEvidenceErrorCode::Denied, "workspace evidence scope differs"));
        }
        valid_path(&claim.source.rel_path)?;
        let before = self.code_read_fence(scope, binding, None).map_err(fence_error)?;
        let snapshot = {
            let conn = self.conn.lock().unwrap();
            let receipt = current_receipt(&conn, scope, binding, Some(&expected.checkout_id))?;
            if receipt != *expected { return Err(failure(CodeEvidenceErrorCode::StaleCursor, "code evidence receipt changed")); }
            if !is_partitioned(&conn, scope, &receipt).map_err(cache_error)? { return Ok(None); }
            if receipt.config_fingerprint != crate::code::producer_fingerprint() {
                return Err(failure(CodeEvidenceErrorCode::ReindexRequired, "partition producer changed"));
            }
            let snapshot: Option<String> = conn.query_row(
                "SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",
                params![scope.owner, scope.workspace_id, receipt.checkout_id, receipt.head_generation], |r| r.get(0)).map_err(store_error)?;
            serde_json::from_str::<CodeSourceSnapshot>(&snapshot.ok_or_else(|| failure(CodeEvidenceErrorCode::BudgetExceeded, "source snapshot exceeds limit"))?)
                .map_err(|_| failure(CodeEvidenceErrorCode::InvalidEvidence, "invalid source snapshot"))?
        };
        // This metadata/policy-only crawl precedes cached rich partition
        // loading AND every real-source acquisition. No FM/authority lock.
        let (policy, identity) = policy_fence(binding, &snapshot).map_err(policy_error)?;
        let generation = {
            let conn = self.conn.lock().unwrap();
            if current_receipt(&conn, scope, binding, Some(&expected.checkout_id))? != *expected {
                return Err(failure(CodeEvidenceErrorCode::StaleCursor, "code evidence receipt changed"));
            }
            Generation::open(&conn, scope, expected).map_err(cache_error)?
        };
        generation.verify_stamp(&snapshot.evidence_cache_identity).map_err(cache_error)?;
        let row: Option<(i64, Option<String>, Option<String>)> = generation.db.query_row(
            "SELECT id,CASE WHEN length(CAST(path AS BLOB))<=4096 THEN path END,CASE WHEN length(CAST(hash AS BLOB))=64 THEN hash END FROM files WHERE path=?1 LIMIT 1",
            params![claim.source.rel_path], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(store_error)?;
        let (id, path, hash) = row.ok_or_else(|| failure(CodeEvidenceErrorCode::Missing, "source is not in published partition"))?;
        let file = generation.load(id).map_err(cache_error)?;
        if path.as_deref() != Some(file.rel_path.as_str()) || hash.as_deref() != Some(file.source_hash.as_str())
            || file.checkout_id != expected.checkout_id || file.producer_fingerprint != expected.config_fingerprint {
            return Err(failure(CodeEvidenceErrorCode::InvalidEvidence, "partition source provenance differs"));
        }
        let admission = || {
            if self.code_read_fence(scope, binding, None).map_err(fence_error)? != before {
                return Err(failure(CodeEvidenceErrorCode::StaleCursor, "code read generation changed"));
            }
            let conn = self.conn.lock().unwrap();
            if current_receipt(&conn, scope, binding, Some(&expected.checkout_id))? != *expected {
                return Err(failure(CodeEvidenceErrorCode::StaleCursor, "code evidence receipt changed"));
            }
            Ok(())
        };
        let result = validate_target(&binding.root, &identity, &policy, &file, claim, bytes, admission)?;
        admission()?;
        // Re-scan to also detect new policy inputs/directories, not merely
        // modifications to files captured in the initial SelectionPolicy.
        let (_, final_identity) = policy_fence(binding, &snapshot).map_err(policy_error)?;
        if final_identity != identity { return Err(failure(CodeEvidenceErrorCode::Denied, "workspace directory changed")); }
        let authority = binding.publication_guard().map_err(|_| failure(CodeEvidenceErrorCode::Denied, "source authority changed"))?;
        let conn = self.conn.lock().unwrap();
        binding.validate_authority(&authority).map_err(|_| failure(CodeEvidenceErrorCode::Denied, "source authority changed"))?;
        if current_receipt(&conn, scope, binding, Some(&expected.checkout_id))? != *expected {
            return Err(failure(CodeEvidenceErrorCode::StaleCursor, "code evidence receipt changed"));
        }
        if crate::code::directory_identity(&binding.root).map_err(|_| failure(CodeEvidenceErrorCode::Denied, "source directory unavailable"))? != identity {
            return Err(failure(CodeEvidenceErrorCode::Denied, "source directory changed"));
        }
        generation.verify_stamp(&snapshot.evidence_cache_identity).map_err(cache_error)?;
        Ok(Some(result))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::code_evidence::{SourceLocator, SourcePredicate, SourceSpan};
    struct Repo(std::path::PathBuf);
    impl Drop for Repo { fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); } }
    fn fixture() -> (Repo, FileEvidence, SourceClaim) {
        let root = std::env::temp_dir().join(format!("fm-partition-current-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let bytes = b"def target():\n    return 1\n";
        std::fs::write(root.join("main.py"), bytes).unwrap();
        let file = crate::code_evidence::extract_evidence("checkout", "main.py", bytes, &ExtractionBudget::default()).unwrap();
        let claim = SourceClaim { source: SourceLocator { file_id:file.file_id.clone(),rel_path:file.rel_path.clone(),source_hash:file.source_hash.clone(),
            span:SourceSpan { start_byte:0,end_byte:bytes.len() as u64,start_line:1,start_column:0,end_line:3,end_column:0 } }, predicate:SourcePredicate::File };
        (Repo(root),file,claim)
    }
    #[test]
    fn explicit_current_acquires_and_charges_two_real_passes() {
        let (root,file,claim) = fixture();
        let identity = crate::code::directory_identity(&root.0).unwrap();
        let policy = crate::code::scan_codebase_pinned(&root.0,&identity).unwrap().policy;
        let (mut bytes,mut admissions) = (0,0);
        let result = validate_target(&root.0,&identity,&policy,&file,&claim,&mut bytes,||{admissions+=1;Ok(())}).unwrap();
        assert_eq!(result.state,ValidationState::Current);
        assert_eq!(bytes,2*file.source_len as usize);
        assert_eq!(admissions,2);
    }
    #[test]
    fn changed_policy_prevents_first_source_acquisition() {
        let (root,file,claim) = fixture();
        let identity = crate::code::directory_identity(&root.0).unwrap();
        let policy = crate::code::scan_codebase_pinned(&root.0,&identity).unwrap().policy;
        std::fs::write(root.0.join(".gitignore"),"main.py\n").unwrap();
        let (mut bytes,mut admissions) = (0,0);
        let error = validate_target(&root.0,&identity,&policy,&file,&claim,&mut bytes,||{admissions+=1;Ok(())}).unwrap_err();
        assert_eq!(error.code,CodeEvidenceErrorCode::ReindexRequired);
        assert_eq!((bytes,admissions),(0,0));
    }
    #[test]
    fn second_pass_edit_is_stale_and_revocation_preserves_io_charge() {
        let (root,file,claim) = fixture();
        let identity = crate::code::directory_identity(&root.0).unwrap();
        let policy = crate::code::scan_codebase_pinned(&root.0,&identity).unwrap().policy;
        let (mut bytes,mut calls) = (0,0);
        let result = validate_target(&root.0,&identity,&policy,&file,&claim,&mut bytes,||{
            calls+=1;if calls==2 {std::fs::write(root.0.join("main.py"),"def changed(): pass\n").unwrap();}Ok(())
        }).unwrap();
        assert_eq!(result.state,ValidationState::Stale);
        assert_eq!(bytes,file.source_len as usize+"def changed(): pass\n".len());
        std::fs::write(root.0.join("main.py"),"def target():\n    return 1\n").unwrap();
        bytes=0;calls=0;
        let error = validate_target(&root.0,&identity,&policy,&file,&claim,&mut bytes,||{
            calls+=1;if calls==2 {return Err(failure(CodeEvidenceErrorCode::Denied,"revoked"));}Ok(())
        }).unwrap_err();
        assert_eq!(error.code,CodeEvidenceErrorCode::Denied);
        assert_eq!(bytes,file.source_len as usize);
    }
    #[test]
    fn corpus_membership_unknown_hash_and_new_paths_refuse_before_body_passes() {
        let (root,file,_) = fixture();
        let identity=crate::code::directory_identity(&root.0).unwrap();
        let scan=crate::code::scan_codebase_pinned(&root.0,&identity).unwrap();
        let mut snapshot=crate::code::source_snapshot_pinned(&root.0,&[],&identity).unwrap();
        snapshot.selection_policy_digest=scan.policy.digest();
        let interfaces=vec![crate::code_evidence::storage::module_interface(&file)];
        let mut coverage=vec![crate::code::ScanCoverage {rel_path:file.rel_path.clone(),status:"indexed".into(),source_hash:Some(file.source_hash.clone()),..Default::default()}];
        assert_eq!(selected_sources(&root.0,&identity,&snapshot,&interfaces,&coverage,&[]).unwrap().unwrap().len(),1);
        coverage[0].source_hash=None;
        assert!(selected_sources(&root.0,&identity,&snapshot,&interfaces,&coverage,&[]).unwrap().is_none());
        coverage[0].source_hash=Some(file.source_hash.clone());
        std::fs::write(root.0.join("new.py"),"def newly_added(): pass\n").unwrap();
        assert!(selected_sources(&root.0,&identity,&snapshot,&interfaces,&coverage,&[]).unwrap().is_none());
    }
    #[test]
    fn unavailable_sources_require_exact_known_published_hash_and_path_set() {
        let (root,file,_) = fixture();
        let broken=b"def broken(:\n";
        std::fs::write(root.0.join("broken.py"),broken).unwrap();
        let identity=crate::code::directory_identity(&root.0).unwrap();
        let scan=crate::code::scan_codebase_pinned(&root.0,&identity).unwrap();
        let mut snapshot=crate::code::source_snapshot_pinned(&root.0,&[],&identity).unwrap();snapshot.selection_policy_digest=scan.policy.digest();
        let interfaces=vec![crate::code_evidence::storage::module_interface(&file)];
        let hash=blake3::hash(broken).to_hex().to_string();
        let mut coverage=vec![crate::code::ScanCoverage {rel_path:file.rel_path.clone(),status:"indexed".into(),source_hash:Some(file.source_hash.clone()),..Default::default()},
            crate::code::ScanCoverage {rel_path:"broken.py".into(),status:"parse_failed".into(),source_hash:Some(hash.clone()),..Default::default()}];
        let mut unavailable=vec![crate::code_evidence::UnavailableFile {rel_path:"broken.py".into(),source_hash:Some(hash),reason:"parse_failed".into()}];
        assert_eq!(selected_sources(&root.0,&identity,&snapshot,&interfaces,&coverage,&unavailable).unwrap().unwrap().len(),2);
        unavailable[0].source_hash=None;
        assert!(selected_sources(&root.0,&identity,&snapshot,&interfaces,&coverage,&unavailable).unwrap().is_none());
        coverage[1].source_hash=None;
        assert!(selected_sources(&root.0,&identity,&snapshot,&interfaces,&coverage,&unavailable).unwrap().is_none(),"selected unavailable bytes cannot borrow an unknown hash");
    }
    #[cfg(unix)]
    #[test]
    fn scanner_excluded_sources_are_only_an_exact_metadata_boundary() {
        let (repo,file,_)=fixture();let root=repo.0.join("source");std::fs::create_dir(&root).unwrap();
        std::fs::copy(repo.0.join("main.py"),root.join("main.py")).unwrap();
        let oversized=vec![b'#';512*1024+1];std::fs::write(root.join("large.py"),&oversized).unwrap();
        std::os::unix::fs::symlink(repo.0.join("main.py"),root.join("external.py")).unwrap();
        let identity=crate::code::directory_identity(&root).unwrap();let scan=crate::code::scan_codebase_pinned(&root,&identity).unwrap();
        let mut snapshot=crate::code::source_snapshot_pinned(&root,&[],&identity).unwrap();snapshot.selection_policy_digest=scan.policy.digest();
        let interfaces=vec![crate::code_evidence::storage::module_interface(&file)];
        let mut coverage=vec![crate::code::ScanCoverage {rel_path:file.rel_path.clone(),status:"indexed".into(),source_hash:Some(file.source_hash.clone()),..Default::default()}];
        coverage.extend(scan.coverage.into_iter().filter(crate::code::eligible_unavailable));
        let unavailable:Vec<_>=coverage.iter().filter(|c|crate::code::eligible_unavailable(c)).map(|c|crate::code_evidence::UnavailableFile {rel_path:c.rel_path.clone(),source_hash:c.source_hash.clone(),reason:c.status.clone()}).collect();
        assert_eq!(unavailable.len(),2);assert!(unavailable.iter().all(|u|u.source_hash.is_none()));
        let selected=||selected_sources(&root,&identity,&snapshot,&interfaces,&coverage,&unavailable).unwrap();
        let admitted=selected().unwrap();assert_eq!(admitted.keys().cloned().collect::<Vec<_>>(),vec!["main.py"],"excluded bodies are never put in either acquisition pass");
        let mut bad=unavailable.clone();bad[0].source_hash=Some("0".repeat(64));
        assert!(selected_sources(&root,&identity,&snapshot,&interfaces,&coverage,&bad).unwrap().is_none());
        bad=unavailable.clone();bad[0].reason="permission_denied".into();
        assert!(selected_sources(&root,&identity,&snapshot,&interfaces,&coverage,&bad).unwrap().is_none());
        std::fs::write(root.join("new-large.py"),&oversized).unwrap();assert!(selected().is_none(),"new excluded path is a boundary change");
        std::fs::remove_file(root.join("new-large.py")).unwrap();
        std::fs::remove_file(root.join("external.py")).unwrap();assert!(selected().is_none(),"removed excluded path is a boundary change");
        std::os::unix::fs::symlink(repo.0.join("main.py"),root.join("external.py")).unwrap();
        std::fs::write(root.join("large.py"),"def newly_eligible(): pass\n").unwrap();assert!(selected().is_none(),"excluded becoming selected cannot borrow Current");
        std::fs::write(root.join("large.py"),&oversized).unwrap();assert_eq!(selected().unwrap(),admitted);
    }
}
