//! Pending-only paired custody. These proofs do not authorize dispatch or
//! establish current Host/archive contributor authority.
use super::*;

#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingPairBinding {
    pub source_account_id:String,pub memory_owner:String,pub memory_workspace:String,
    pub owner_epoch:u64,pub durable_epoch:String,pub fingerprint:String,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingPairAssignment {
    pub policy_fingerprint:String,pub authority_fingerprint:String,pub connection_id:String,
    pub model_route_id:String,pub assignment:Value,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingPairFenceRequest {
    pub version:u32,pub phase:QueuedTransitionBinding,pub workspace_id:String,pub job_id:String,
    pub source:HostArchiveSourceRef,pub coverage:AssistantInterpretationCoverage,pub policy_fingerprint:String,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PairedErasedFactRequest {pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,pub job_id:String}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingPairMove {
    pub request:crate::store::sqlite::GuardedOwnerRenameRequest,pub receipt_id:String,pub receipt_hash:String,pub fence_id:String,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingPairRebindRequest {
    pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,pub job_id:String,pub request_key:String,
    pub source:HostArchiveSourceRef,pub coverage:AssistantInterpretationCoverage,pub expected_input_sha256:String,
    pub new_binding:PendingPairBinding,pub new_assignment:PendingPairAssignment,
    pub policy_projection:PolicyPublicationRefs,pub movement:Option<PendingPairMove>,
}
fn unavailable()->RepositoryError {RepositoryError::NotFound}
fn text(v:&str)->bool {!v.is_empty() && v==v.trim() && v.len()<=512 && !v.chars().any(char::is_control)}
// Python's _job_input_hash uses compact sort_keys=True, ensure_ascii=True.
// serde_json's Value map is ordered; encode non-ASCII as UTF-16 escape units.
fn python_ascii(value:&Value)->Result<Vec<u8>,RepositoryError> {
    fn supported(value:&Value)->bool {match value {Value::Number(v)=>!v.is_f64(),Value::Array(a)=>a.iter().all(supported),Value::Object(o)=>o.values().all(supported),_=>true}}
    if !supported(value) {return Err(unavailable());}
    let json=serde_json::to_string(value)?;let mut out=String::with_capacity(json.len());
    for ch in json.chars() {if ch.is_ascii() && ch!='\u{7f}' {out.push(ch)} else {let mut units=[0u16;2];for unit in ch.encode_utf16(&mut units).iter() {use std::fmt::Write;write!(&mut out,"\\u{unit:04x}").map_err(|_|unavailable())?;}}}
    Ok(out.into_bytes())
}
fn ascii_hash(value:&Value)->Result<String,RepositoryError> {Ok(sha256_hex(&python_ascii(value)?))}
fn target_hash(b:&PendingPairBinding)->Result<String,RepositoryError> {
    ascii_hash(&json!({"source_account_id":b.source_account_id,"memory_owner":b.memory_owner,"memory_workspace":b.memory_workspace,"owner_epoch":b.owner_epoch,"durable_epoch":b.durable_epoch}))
}
fn valid_binding(b:&PendingPairBinding,account:&str,owner:&str,workspace:&str)->Result<(),RepositoryError> {
    if b.source_account_id!=account || b.memory_owner!=owner || b.memory_workspace!=workspace || ![account,owner,workspace].iter().all(|s|text(s))
        || b.owner_epoch>9007199254740991 || b.durable_epoch.is_empty() || b.durable_epoch.len()>20 || !b.durable_epoch.bytes().all(|c|c.is_ascii_digit())
        || (b.durable_epoch.len()>1 && b.durable_epoch.starts_with('0')) || !valid_sha256(&b.fingerprint) || target_hash(b)?!=b.fingerprint {return Err(unavailable());}
    Ok(())
}
fn valid_assignment(a:&PendingPairAssignment)->Result<(),RepositoryError> {
    let obj=a.assignment.as_object().ok_or_else(unavailable)?;
    let keys=["version","workKind","assignmentRevision","assignmentFingerprint","source","modelRouteID","connectionID","modelID","grantID","optionRouteID","catalogRevision","options"];
    let options=obj.get("options").and_then(Value::as_object).ok_or_else(unavailable)?;
    let origin=obj.get("source").and_then(Value::as_object).ok_or_else(unavailable)?;
    let origin_ok=match origin.get("kind").and_then(Value::as_str) {
        Some("owner_selection")=>origin.len()==1,
        Some("initiating_execution")=>origin.len()==7 && ["source","rootOperationID","sessionID","executionRevision","bindingID","bindingRevision"].iter().all(|k|origin.contains_key(*k)),
        _=>false,
    };
    if !valid_sha256(&a.policy_fingerprint) || !valid_sha256(&a.authority_fingerprint) || !text(&a.connection_id) || !text(&a.model_route_id)
        || obj.len()!=keys.len() || keys.iter().any(|k|!obj.contains_key(*k)) || serde_json::to_vec(&a.assignment)?.len()>16384
        || obj.get("version")!=Some(&json!(1)) || obj.get("workKind").and_then(Value::as_str)!=Some("interpretation")
        || obj.get("assignmentRevision").and_then(Value::as_u64).is_none_or(|v|v>9007199254740991)
        || obj.get("assignmentFingerprint").and_then(Value::as_str).is_none_or(|v|!valid_sha256(v))
        || obj.get("catalogRevision").and_then(Value::as_u64).is_none_or(|v|v==0 || v>9007199254740991)
        || ["modelID","optionRouteID"].iter().any(|k|obj.get(*k).and_then(Value::as_str).is_none_or(|v|!text(v) || v.len()>256))
        || !matches!(obj.get("grantID"),Some(Value::Null)|Some(Value::String(_))) || obj.get("grantID").and_then(Value::as_str).is_some_and(|v|!text(v) || v.len()>256)
        || options.keys().any(|k|k!="reasoning_effort") || options.values().any(|v|v.as_str().is_none_or(|s|s.is_empty() || s.len()>32 || matches!(s,"default"|"inherit")))
        || !origin_ok || obj.get("connectionID").and_then(Value::as_str)!=Some(&a.connection_id)
        || obj.get("modelRouteID").and_then(Value::as_str)!=Some(&a.model_route_id) {return Err(unavailable());}Ok(())
}
fn input_hash(source:&HostArchiveSourceRef,coverage:&AssistantInterpretationCoverage,admission:&str,binding:&PendingPairBinding,assignment:&PendingPairAssignment)->Result<String,RepositoryError> {
    if assignment.assignment.pointer("/source/kind").and_then(Value::as_str)==Some("initiating_execution") && assignment.assignment.pointer("/source/source")!=Some(&serde_json::to_value(source)?) {return Err(unavailable());}
    ascii_hash(&json!({"kind":"assistant_interpretation_review","coverage":coverage,"evidence":{"source":source,"admission_id":admission,"policy_fingerprint":assignment.policy_fingerprint,"authority_fingerprint":assignment.authority_fingerprint,"connection_id":assignment.connection_id,"model_route_id":assignment.model_route_id,"assignment":assignment.assignment},"binding":binding}))
}
#[derive(Clone)]
struct PendingSnapshot {
    source_key:String,idempotency_key:String,input_hash:String,admission_id:String,disposition:Option<Value>,
    binding:PendingPairBinding,assignment:PendingPairAssignment,
}
/// Reads one exact job under the caller's existing transaction and budget.
/// It has no effect and is not a grant. All historical attempt rows deny.
fn pending_snapshot(tx:&Transaction<'_>,owner:&str,binding_owners:&[String],workspace:&str,job:&str,source:&HostArchiveSourceRef,coverage:&AssistantInterpretationCoverage,terminal:bool,erased:bool,budget:&mut ResetFenceBudget)->Result<PendingSnapshot,RepositoryError> {
    if terminal && erased {return Err(unavailable())}
    require_guarded_epoch(tx)?;source.validate_in(tx)?;coverage.validate(source)?;
    if !text(owner) || !text(workspace) || !text(job) {return Err(unavailable());}
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.assistant_interpretation.pending_rebind.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(unavailable());}
    let lengths:Option<(i64,i64,i64)>=tx.query_row("SELECT length(CAST(r.source_ref_json AS BLOB)),length(CAST(r.coverage_json AS BLOB)),length(CAST(r.assignment_json AS BLOB)) FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE r.owner_id=?1 AND r.job_id=?2 AND j.kind='assistant_interpretation_review'",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let (a,b,c)=lengths.ok_or_else(unavailable)?;if !(0..=8192).contains(&a) || !(0..=8192).contains(&b) || !(0..=16384).contains(&c) {return Err(unavailable());}budget.check((a+b+c) as usize).map_err(RepositoryError::Validation)?;
    let row:Option<(String,String,String,String,i64,String,String,String,String,String,String,String,String,String,Option<String>,i64,Option<String>,Option<String>,Option<i64>)>=tx.query_row("SELECT r.source_ref_json,r.coverage_json,r.source_key,r.source_account_id,r.owner_epoch,r.durable_epoch,r.binding_fingerprint,r.admission_id,r.policy_fingerprint,r.authority_fingerprint,r.connection_id,r.model_route_id,r.assignment_json,j.idempotency_key,j.current_attempt_id,j.attempt_count,r.candidate_id,r.candidate_attempt_id,r.candidate_lease_epoch FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE r.owner_id=?1 AND r.job_id=?2 AND r.target_workspace_id=?3 AND r.source_workspace_id IS ?4 AND r.source_hash=?5 AND j.workspace_key=?3 AND j.project_key='' AND j.kind='assistant_interpretation_review' AND ((?6=0 AND ?7=0 AND j.state='queued') OR (?6=1 AND ?7=0 AND j.state IN ('succeeded','failed_terminal','cancelled')) OR (?6=0 AND ?7=1 AND j.state='cancelled'))",params![owner,job,workspace,source.workspace_id,source.content_hash,terminal,erased],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?,r.get(10)?,r.get(11)?,r.get(12)?,r.get(13)?,r.get(14)?,r.get(15)?,r.get(16)?,r.get(17)?,r.get(18)?))).optional()?;
    let (raw_source,raw_coverage,source_key,account,epoch,durable,fingerprint,admission,policy,authority,connection,route,raw_assignment,idempotency,current,attempts,candidate,candidate_attempt,candidate_epoch)=row.ok_or_else(unavailable)?;
    let src:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&raw_source).map_err(RepositoryError::Validation)?)?;
    let pair:AssistantInterpretationCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw_coverage).map_err(RepositoryError::Validation)?)?;
    if src!=*source || pair!=*coverage || source_key!=coverage.source_key(source)? || account!=source.account_id || admission!=source.message_id || epoch<0 || (!terminal && (current.is_some() || attempts!=0)) || (terminal && (current.is_none() || attempts<=0)) || candidate.is_some() || candidate_attempt.is_some() || candidate_epoch.is_some() {return Err(unavailable());}
    let any:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2)",params![owner,job],|r|r.get(0))?;
    if !terminal && any {return Err(unavailable());}
    let mut disposition=None;
    if terminal {
        let attempt=current.as_deref().ok_or_else(unavailable)?;
        let length:Option<i64>=tx.query_row("SELECT length(CAST(result_json AS BLOB)) FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0)).optional()?;let length=length.ok_or_else(unavailable)?;if !(0..=8192).contains(&length) {return Err(unavailable());}budget.check(length as usize).map_err(RepositoryError::Validation)?;
        let row:Option<(String,i64,i64,String,String,String)>=tx.query_row("SELECT a.state,a.attempt_number,a.lease_epoch,q.state,j.state,j.result_json FROM fm_v2_attempts a JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id WHERE a.owner_id=?1 AND a.job_id=?2 AND a.attempt_id=?3 AND j.current_attempt_id=a.attempt_id AND q.settled_at_ms IS NOT NULL",params![owner,job,attempt],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
        let (a_state,number,lease,q_state,j_state,raw)=row.ok_or_else(unavailable)?;
        if raw.len()>8192 || number!=attempts || lease<=0 || q_state!="settled" || a_state!=j_state {return Err(unavailable());}budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let result=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let d=&result["paired_disposition"];
        let reason=match j_state.as_str() {"succeeded"=>"no_finding","failed_terminal"=>"failed","cancelled"=>"cancelled",_=>return Err(unavailable())};
        let outer_ok=(result.as_object().map(|v|v.len())==Some(4) && result["version"]==1 && result["actualUsage"]=="unknown" && result["chargedAs"]=="reserved_bound") || (result.as_object().map(|v|v.len())==Some(3) && result["version"]==1 && result["actualUsage"]=="reported");
        if !outer_ok || d.as_object().map(|v|v.len())!=Some(8) || d["version"]!=1 || d["kind"]!="no_publication" || d["reason"]!=reason || d["job_id"]!=job || d["attempt_id"]!=attempt || d["lease_epoch"]!=lease || d["source_key"]!=source_key || d["pair_sha256"]!=coverage.pair_sha256 {return Err(unavailable());}
        disposition=Some(d.clone());
        let unresolved:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts a LEFT JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.attempt_id=a.attempt_id AND q.job_id=a.job_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2 AND (a.state IN ('leased','running') OR q.state IN ('reserved','dispatched','uncertain') OR q.attempt_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_change_sets INDEXED BY s04_pair_publication_attempt WHERE owner_id=?1 AND source_event_id=?3 AND reason='unreviewed assistant interpretation')",params![owner,job,attempt],|r|r.get(0))?;if unresolved {return Err(unavailable());}
        let digest=sha256_hex(&serde_json::to_vec(&(source.account_id.as_str(),job))?);let candidate=format!("s05paircandidate_{digest}");let evidence=format!("s05evidence_{digest}");let assistant=format!("s05assistant_evidence_{digest}");
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_evidence WHERE evidence_id IN (?2,?3))",params![candidate,evidence,assistant],|r|r.get(0))?;if orphan {return Err(unavailable());}
    }
    deny_erased_import_job(tx,owner,job,Some(&idempotency))?;
    deny_chain(tx,owner,binding_owners,job,&idempotency,&source_key,budget)?;
    let assignment_value=crate::field_authority::parse_unique_json(&raw_assignment).map_err(RepositoryError::Validation)?;
    if serde_json::to_string(&assignment_value)?!=raw_assignment {return Err(unavailable());}
    let mut matches=Vec::new();for captured in binding_owners {let binding=PendingPairBinding{source_account_id:account.clone(),memory_owner:captured.clone(),memory_workspace:workspace.into(),owner_epoch:epoch as u64,durable_epoch:durable.clone(),fingerprint:fingerprint.clone()};if valid_binding(&binding,&source.account_id,captured,workspace).is_ok() {matches.push(binding);}}if matches.len()!=1 {return Err(unavailable());}let binding=matches.pop().ok_or_else(unavailable)?;
    let assignment=PendingPairAssignment{policy_fingerprint:policy,authority_fingerprint:authority,connection_id:connection,model_route_id:route,assignment:assignment_value};valid_assignment(&assignment)?;
    let expected_key=sha256_hex(&serde_json::to_vec(&json!({"version":1,"kind":"assistant_interpretation_review","coverage":coverage,"source":source,"admissionID":source.message_id,"rootOperationID":source.message_id}))?);
    if idempotency!=expected_key {return Err(unavailable());}
    let stored:String=tx.query_row("SELECT input_hash FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0))?;
    if !valid_sha256(&stored) || input_hash(source,coverage,&admission,&binding,&assignment)?!=stored {return Err(unavailable());}
    Ok(PendingSnapshot{source_key,idempotency_key:idempotency,input_hash:stored,admission_id:admission,binding,assignment,disposition})
}


/// Factual exact pair validation only; an erased snapshot is not a grant.
pub(crate) fn verified_pair_snapshot_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,erased:bool,budget:&mut ResetFenceBudget)->Result<(String,String,String),RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable())}
    let (root,coverage):(String,String)=tx.query_row("SELECT source_ref_json,coverage_json FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2 AND source_account_id=?3 AND target_workspace_id=?4 AND length(CAST(source_ref_json AS BLOB))<=8192 AND length(CAST(coverage_json AS BLOB))<=8192",params![owner,job,account,workspace],|r|Ok((r.get(0)?,r.get(1)?)))?;
    budget.check(root.len()+coverage.len()).map_err(RepositoryError::Validation)?;
    let source:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&root).map_err(RepositoryError::Validation)?)?;
    let pair:AssistantInterpretationCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&coverage).map_err(RepositoryError::Validation)?)?;
    if source.account_id!=account {return Err(unavailable())}
    let snap=pending_snapshot(tx,owner,view.owners(),workspace,job,&source,&pair,false,erased,budget)?;
    Ok((snap.source_key,pair.pair_sha256,snap.input_hash))
}

fn pending_fence_id(phase:&QueuedTransitionBinding,job:&str)->Result<String,RepositoryError> {
    Ok(format!("cs_s04pairpending_{}",sha256_hex(&serde_json::to_vec(&("s04.paired-pending-fence.v1",phase,job))?)))
}
/// Fences an actually active contributor with the canonical phase proof; a
/// genuinely absent contributor is recorded without inserting a source row.
fn fence_contributor(tx:&Transaction<'_>,request:&super::paired_transition::PairedRenameFenceRequest,id:&str,kind:&str,source_kind:&str,revision:i64,hash:&str,source_key:&str,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    use super::paired_transition as p;
    let owner=&request.phase.owner;
    let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![owner,id],|r|r.get(0))?;
    if erased {return Err(unavailable());}
    let proof_id=p::proof_id(&request.phase,kind,id,if kind=="assistant" {Some(&request.job_id)} else {None})?;
    let body=if kind=="root" {p::root_record(request,id)} else {p::assistant_record(request,id,source_key)};
    let event=if kind=="root" {"paired_rename_root_fence"} else {"paired_rename_assistant_fence"};
    let proved=p::receipt(tx,owner,&proof_id,event,&body,revision,budget)?;
    let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2)",params![owner,id],|r|r.get(0))?;
    if !present {
        if proved {return Err(unavailable());}
        return Ok(json!({"source_id":id,"revision":revision,"content_hash":hash,"prior_state":"absent","fence_id":null}));
    }
    let state=p::source_state(tx,owner,&request.workspace_id,id,revision,hash,source_kind)?;
    if proved {if state!="forget_requested" {return Err(unavailable());}}
    else {
        if state!="active" {return Err(unavailable());}
        if tx.execute("UPDATE fm_v2_sources SET forget_state='forget_requested' WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 AND forget_state='active'",params![owner,id,revision])?!=1 {return Err(unavailable());}
    }
    let stray:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND forget_state='active')",params![owner,id],|r|r.get(0))?;if stray {return Err(unavailable());}
    if kind=="root" {host_visibility_with_account(tx,owner,&request.source.account_id,&request.workspace_id,id,budget,true)?;}
    if !proved {p::insert_receipt(tx,owner,&proof_id,event,&body,revision,budget)?;}
    Ok(json!({"source_id":id,"revision":revision,"content_hash":hash,"prior_state":"active","fence_id":proof_id}))
}
/// Host holds the active exact transition claim and fresh dual-source guard.
/// This only records source facts and fences existing active contributors; it
/// never creates absent source rows or authorizes dispatch.
pub(crate) fn fence_pending_pair(tx:&Transaction<'_>,r:&PendingPairFenceRequest,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {fence_pair_disposition(tx,r,false,false,budget)}
pub(crate) fn fence_terminal_pair(tx:&Transaction<'_>,r:&PendingPairFenceRequest,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {fence_pair_disposition(tx,r,true,false,budget)}
pub(crate) fn fence_erased_pair(tx:&Transaction<'_>,r:&PendingPairFenceRequest,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {fence_pair_disposition(tx,r,false,true,budget)}
fn fence_pair_disposition(tx:&Transaction<'_>,r:&PendingPairFenceRequest,terminal:bool,erased:bool,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.version!=1 || r.phase.account_id!=r.source.account_id || !valid_sha256(&r.policy_fingerprint) {tracing::debug!(target: "openclank::pair_fence", "request refused");return Err(unavailable());}
    validate_queued_binding(tx,&r.phase,&r.source.account_id).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "phase binding refused");e})?;
    let view=owner_custody::verified_or_direct_custody_view(tx,&r.phase.database_id,&r.phase.account_id,&r.phase.owner,&r.phase.owner,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "custody view refused");e})?;
    let erased_fact=if erased {Some(crate::store::sqlite::SqliteStore::paired_erased_fact_in(tx,&r.phase.owner,&r.phase.account_id,&r.workspace_id,&r.job_id,&view,budget).map_err(RepositoryError::Validation)?)} else {None};
    let owners=view.owners().to_vec();drop(view);
    let snap=pending_snapshot(tx,&r.phase.owner,&owners,&r.workspace_id,&r.job_id,&r.source,&r.coverage,terminal,erased,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "job snapshot refused");e})?;
    if snap.assignment.policy_fingerprint!=r.policy_fingerprint {tracing::debug!(target: "openclank::pair_fence", "policy binding refused");return Err(unavailable());}
    let shared=super::paired_transition::PairedRenameFenceRequest{version:1,phase:r.phase.clone(),workspace_id:r.workspace_id.clone(),job_id:r.job_id.clone(),source:r.source.clone(),coverage:r.coverage.clone(),policy_fingerprint:r.policy_fingerprint.clone()};
    if super::paired_transition::exact_job(tx,&shared,&r.phase.owner,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "exact job refused");e})?!=snap.source_key {tracing::debug!(target: "openclank::pair_fence", "source binding refused");return Err(unavailable());}
    let root=host_archive_source_id(&r.source)?;let assistant=pair_assistant_source_id(&r.source,&r.coverage)?;
    let (root_proof,root_active)=super::paired_transition::fence_root(tx,&r.phase,&r.workspace_id,&r.source,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "root fence refused");e})?;
    if root_active {host_visibility_with_account_phase(tx,&r.phase,&r.workspace_id,&root,budget,true).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "root visibility refused");e})?;}
    let root_fact=json!({"source_id":root,"revision":r.source.revision,"content_hash":r.source.content_hash,"prior_state":if root_active {"active"} else {"absent"},"fence_id":root_proof});
    let assistant_fact=fence_contributor(tx,&shared,&assistant,"assistant","host_assistant_part.v1",r.coverage.assistant.revision,&r.coverage.assistant.content_hash,&snap.source_key,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "assistant fence refused");e})?;
    let id=pending_fence_id(&r.phase,&r.job_id)?;
    let mut body=json!({"version":"s04.paired-pending-fence.v1","phase":r.phase,"workspace_id":r.workspace_id,"job_id":r.job_id,"source_key":snap.source_key,"input_hash":snap.input_hash,"root":root_fact,"assistant":assistant_fact});
    if let Some(disposition)=&snap.disposition {body["terminal_disposition"]=disposition.clone();}
    if let Some(fact)=erased_fact {if fact["source_key"]!=snap.source_key || fact["pair_sha256"]!=r.coverage.pair_sha256 || fact["input_sha256"]!=snap.input_hash {return Err(unavailable())} body["erased_original_receipt_sha256"]=fact["original_receipt_sha256"].clone();}
    let exists=super::paired_transition::receipt(tx,&r.phase.owner,&id,"paired_pending_fence",&body,0,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "aggregate receipt refused");e})?;
    if !exists {super::paired_transition::insert_receipt(tx,&r.phase.owner,&id,"paired_pending_fence",&body,0,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "aggregate write refused");e})?;}
    Ok(json!({"version":1,"state":"fenced","fence_id":id,"root":body["root"],"assistant":body["assistant"]}))
}

fn rebind_id(r:&PendingPairRebindRequest)->Result<String,RepositoryError> {
    Ok(format!("cs_s04pairrebind_{}",sha256_hex(&serde_json::to_vec(&("s04.paired-pending-rebind.v1",&r.account_id,&r.workspace_id,&r.job_id,&r.request_key))?)))
}
fn request_core(r:&PendingPairRebindRequest)->Result<Value,RepositoryError> {
    // Retain content-free provenance, never relocation paths from the full
    // transition request. Its exact original bytes are bound by digest.
    let movement=match &r.movement {Some(m)=>json!({"phase":queued_binding(&m.request),"transition_request_sha256":commitment(&m.request)?,"receipt_id":m.receipt_id,"receipt_hash":m.receipt_hash,"fence_id":m.fence_id}),None=>Value::Null};
    Ok(json!({"version":r.version,"account_id":r.account_id,"workspace_id":r.workspace_id,"job_id":r.job_id,"request_key":r.request_key,"source":r.source,"coverage":r.coverage,"expected_input_sha256":r.expected_input_sha256,"new_binding":r.new_binding,"new_assignment":r.new_assignment,"movement":movement}))
}
fn request_hash(r:&PendingPairRebindRequest)->Result<String,RepositoryError> {Ok(sha256_hex(&serde_json::to_vec(&request_core(r)?)?))}
fn commitment<T:Serialize>(value:&T)->Result<String,RepositoryError> {Ok(sha256_hex(&serde_json::to_vec(value)?))}
fn deny_chain(tx:&Transaction<'_>,owner:&str,owners:&[String],job:&str,key:&str,source_key:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    for captured in owners {
        budget.check(captured.len().saturating_add(job.len()).saturating_add(key.len())).map_err(RepositoryError::Validation)?;
        let j=sha256_hex(&serde_json::to_vec(&json!(["s04.import-job-id.v1",captured,job]))?);
        let k=sha256_hex(&serde_json::to_vec(&json!(["s04.import-idempotency.v1",captured,key]))?);
        let je=format!("outbox_s04_import_erased_{}",sha256_hex(&serde_json::to_vec(&json!([captured,j]))?));let ke=format!("outbox_s04_import_key_erased_{k}");
        let pair_job=sha256_hex(&serde_json::to_vec(&json!(["s04.pair-job.v1",captured,job]))?);
        let pair_job_event=format!("outbox_s04_pair_full_job_{}",sha256_hex(&serde_json::to_vec(&json!([captured,pair_job]))?));
        let pair_source_event=format!("outbox_s04_pair_full_source_{}",sha256_hex(&serde_json::to_vec(&json!([captured,source_key]))?));
        let found:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id IN (?2,?3,?4,?5))",params![owner,je,ke,pair_job_event,pair_source_event],|r|r.get(0))?;if found {return Err(unavailable());}
    }Ok(())
}
fn verified_pending_fence(tx:&Transaction<'_>,r:&PendingPairRebindRequest,m:&PendingPairMove,phase:&QueuedTransitionBinding,snap:&PendingSnapshot,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let id=pending_fence_id(phase,&r.job_id)?;if id!=m.fence_id {return Err(unavailable());}
    let size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,id],|row|row.get(0)).optional()?;
    let size=size.ok_or_else(unavailable)?;if !(0..=16384).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,id],|row|row.get(0))?;
    let body=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let root=host_archive_source_id(&r.source)?;let assistant=pair_assistant_source_id(&r.source,&r.coverage)?;
    if body.as_object().map(|v|v.len())!=Some(8) || body["version"]!="s04.paired-pending-fence.v1" || body["phase"]!=serde_json::to_value(phase)? || body["workspace_id"]!=r.workspace_id || body["job_id"]!=r.job_id || body["source_key"]!=snap.source_key || body["input_hash"]!=r.expected_input_sha256 {return Err(unavailable());}
    if !super::paired_transition::receipt(tx,&r.owner,&id,"paired_pending_fence",&body,0,budget)? {return Err(unavailable());}
    let fence=super::paired_transition::PairedRenameFenceRequest{version:1,phase:phase.clone(),workspace_id:r.workspace_id.clone(),job_id:r.job_id.clone(),source:r.source.clone(),coverage:r.coverage.clone(),policy_fingerprint:snap.assignment.policy_fingerprint.clone()};
    for (field,kind,id,revision,hash,source_kind) in [("root","root",&root,r.source.revision,r.source.content_hash.as_str(),"host_archive_part.v1"),("assistant","assistant",&assistant,r.coverage.assistant.revision,r.coverage.assistant.content_hash.as_str(),"host_assistant_part.v1")] {
        let fact=&body[field];if fact.as_object().map(|v|v.len())!=Some(5) || fact["source_id"]!=*id || fact["revision"]!=revision || fact["content_hash"]!=hash {return Err(unavailable());}
        let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![r.owner,id],|row|row.get(0))?;if erased {return Err(unavailable());}
        match fact["prior_state"].as_str() {
            Some("absent")=>{
                if kind=="root" {
                    let proof=super::paired_transition::proof_id(phase,"root_absent",id,None)?;
                    let expected=json!({"version":"s04.paired-rename-fence.v1","phase":phase,"kind":"root_absent","workspace_id":r.workspace_id,"source_id":id,"revision":revision,"content_hash":hash});
                    if fact["fence_id"]!=proof || !super::paired_transition::receipt(tx,&r.owner,&proof,"paired_rename_root_absent",&expected,revision,budget)? {return Err(unavailable());}
                } else if !fact["fence_id"].is_null() {return Err(unavailable());}
                let any:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3)",params![r.owner,id,revision],|row|row.get(0))?;if any {return Err(unavailable());}
            },
            Some("active")=>{
                let proof=super::paired_transition::proof_id(phase,kind,id,if kind=="assistant" {Some(&r.job_id)} else {None})?;
                if fact["fence_id"]!=proof {return Err(unavailable());}
                let expected=if kind=="root" {super::paired_transition::root_record(&fence,id)} else {super::paired_transition::assistant_record(&fence,id,&snap.source_key)};
                let event=if kind=="root" {"paired_rename_root_fence"} else {"paired_rename_assistant_fence"};
                if !super::paired_transition::receipt(tx,&r.owner,&proof,event,&expected,revision,budget)? {return Err(unavailable());}
                let state=super::paired_transition::source_state(tx,&r.owner,&r.workspace_id,id,revision,hash,source_kind)?;
                if !matches!(state.as_str(),"forget_requested"|"active") {return Err(unavailable());}
            },_=>return Err(unavailable()),
        }
    }Ok(())
}
/// Caller owns the current named-human policy, actual dual-source/target guard,
/// and wraps this transaction in the connection-local pending rebind guard.
/// This method records an original action, not a dispatch grant.
/// Exact original outcome only. Caller authenticates current account/read
/// authority; neither a receipt nor a missing row authorizes a new effect.
pub(crate) fn pending_pair_original(tx:&Transaction<'_>,r:&PendingPairRebindRequest,view:Option<&owner_custody::VerifiedCustodyView<'_,'_>>,budget:&mut ResetFenceBudget)->Result<Option<Value>,RepositoryError> {
    if r.version!=1 || ![r.owner.as_str(),r.account_id.as_str(),r.workspace_id.as_str(),r.job_id.as_str(),r.request_key.as_str()].iter().all(|v|text(v)) || r.source.account_id!=r.account_id || !valid_sha256(&r.expected_input_sha256) {return Err(unavailable());}
    require_guarded_epoch(tx)?;
    if let Some(v)=view {if v.current_owner()!=r.owner || v.account_id()!=r.account_id {return Err(unavailable());}v.for_captured(tx,&r.owner)?;}
    let id=rebind_id(r)?;let request_sha=request_hash(r)?;
    let receipt_size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,id],|row|row.get(0)).optional()?;
    if let Some(size)=receipt_size {if !(0..=65536).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;}
    let existing:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s04.paired-pending-rebind.v1'",params![r.owner,id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?))).optional()?;
    if let Some((hash,raw,actor,captured,contract,source))=existing {
        if actor!="automation" || captured!="paired_pending_rebind" {return Err(unavailable());}
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let captured=value["owner"].as_str().ok_or_else(unavailable)?.to_owned();
        if let Some(v)=view {v.for_captured(tx,&captured)?;} else if captured!=r.owner {return Err(unavailable());}
        let event_size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![r.owner,format!("outbox_{id}_paired_pending_rebind")],|row|row.get(0)).optional()?;
        let event_size=event_size.ok_or_else(unavailable)?;if !(0..=65536).contains(&event_size) {return Err(unavailable());}budget.check(event_size as usize).map_err(RepositoryError::Validation)?;
        let event:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='paired_pending_rebind'",params![r.owner,format!("outbox_{id}_paired_pending_rebind"),id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
        let old_binding:PendingPairBinding=serde_json::from_value(value["old_binding"].clone())?;
        let new_binding:PendingPairBinding=serde_json::from_value(value["new_binding"].clone())?;
        let old_assignment:PendingPairAssignment=serde_json::from_value(value["old_assignment"].clone())?;
        let new_assignment:PendingPairAssignment=serde_json::from_value(value["new_assignment"].clone())?;
        if let Some(v)=view {v.for_captured(tx,&old_binding.memory_owner)?;} else if old_binding.memory_owner!=r.owner {return Err(unavailable());}
        valid_binding(&old_binding,&r.account_id,&old_binding.memory_owner,&r.workspace_id)?;valid_binding(&new_binding,&r.account_id,&captured,&r.workspace_id)?;
        valid_assignment(&old_assignment)?;valid_assignment(&new_assignment)?;
        if sha256_hex(raw.as_bytes())!=hash || actor!="automation" || contract!=CONTRACT_VERSION || source.is_some() || value.as_object().map(|v|v.len())!=Some(18) || value["version"]!="s04.paired-pending-rebind.v1" || value["actor"]!="paired_pending_rebind" || value["owner"]!=captured || value["account_id"]!=r.account_id || value["workspace_id"]!=r.workspace_id || value["job_id"]!=r.job_id || value["request"]!=request_core(r)? || value["request_sha256"]!=request_sha || value["old_input_sha256"]!=r.expected_input_sha256 || value["old_input_sha256"]!=input_hash(&r.source,&r.coverage,&r.source.message_id,&old_binding,&old_assignment)? || value["new_input_sha256"]!=input_hash(&r.source,&r.coverage,&r.source.message_id,&new_binding,&new_assignment)? || value["old_binding_sha256"]!=commitment(&old_binding)? || value["new_binding_sha256"]!=commitment(&new_binding)? || value["old_assignment_sha256"]!=commitment(&old_assignment)? || value["new_assignment_sha256"]!=commitment(&new_assignment)? || event.as_ref().is_none_or(|(payload,revision)|payload!=&raw || *revision!=0) {return Err(unavailable());}
        return Ok(Some(json!({"version":1,"state":"rebound","replayed":true,"job_id":r.job_id,"input_sha256":value["new_input_sha256"]})));
    }
    let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![r.owner,id,format!("outbox_{id}_paired_pending_rebind")],|row|row.get(0))?;if collision {return Err(unavailable());}
    Ok(None)
}

pub(crate) fn rebind_pending_pair(tx:&Transaction<'_>,r:&PendingPairRebindRequest,view:Option<&owner_custody::VerifiedCustodyView<'_,'_>>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.version!=1 || ![r.owner.as_str(),r.account_id.as_str(),r.workspace_id.as_str(),r.job_id.as_str(),r.request_key.as_str()].iter().all(|v|text(v)) || r.source.account_id!=r.account_id || !valid_sha256(&r.expected_input_sha256) {return Err(unavailable());}
    require_guarded_epoch(tx)?;
    if let Some(v)=view {if v.current_owner()!=r.owner || v.account_id()!=r.account_id {return Err(unavailable());}v.for_captured(tx,&r.owner)?;}
    if let Some(original)=pending_pair_original(tx,r,view,budget)? {return Ok(original);}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    let id=rebind_id(r)?;let request_sha=request_hash(r)?;
    if let Some(m)=&r.movement {
        let v=view.ok_or_else(unavailable)?;let phase=queued_binding(&m.request);
        if phase.new_owner!=r.owner || phase.account_id!=r.account_id || !v.is_head_transition(tx,&m.request,&m.receipt_id,&m.receipt_hash)? {return Err(unavailable());}
    }
    let owners=view.map(|v|v.owners().to_vec()).unwrap_or_else(||vec![r.owner.clone()]);
    let snap=pending_snapshot(tx,&r.owner,&owners,&r.workspace_id,&r.job_id,&r.source,&r.coverage,false,false,budget)?;
    if snap.input_hash!=r.expected_input_sha256 || (r.movement.is_none() && snap.binding.memory_owner!=r.owner) {return Err(unavailable());}
    let owners=view.map(|v|v.owners().to_vec()).unwrap_or_else(||vec![r.owner.clone()]);deny_chain(tx,&r.owner,&owners,&r.job_id,&snap.idempotency_key,&snap.source_key,budget)?;
    if let Some(m)=&r.movement {verified_pending_fence(tx,r,m,&queued_binding(&m.request),&snap,budget)?;}
    else {
        // Refresh alone never resurrects a withdrawn or erased contributor.
        for (id,revision,hash,kind) in [(host_archive_source_id(&r.source)?,r.source.revision,r.source.content_hash.as_str(),"host_archive_part.v1"),(pair_assistant_source_id(&r.source,&r.coverage)?,r.coverage.assistant.revision,r.coverage.assistant.content_hash.as_str(),"host_assistant_part.v1")] {
            let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![r.owner,id],|row|row.get(0))?;if erased {return Err(unavailable());}
            let any:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2)",params![r.owner,id],|row|row.get(0))?;
            if any && super::paired_transition::source_state(tx,&r.owner,&r.workspace_id,&id,revision,hash,kind)?!="active" {return Err(unavailable());}
        }
    }
    valid_binding(&r.new_binding,&r.account_id,&r.owner,&r.workspace_id)?;valid_assignment(&r.new_assignment)?;
    let next=input_hash(&r.source,&r.coverage,&snap.admission_id,&r.new_binding,&r.new_assignment)?;
    let old_assignment=serde_json::to_string(&snap.assignment.assignment)?;let new_assignment=serde_json::to_string(&r.new_assignment.assignment)?;
    let changed=tx.execute("UPDATE fm_v2_s05_job_refs SET owner_epoch=?1,durable_epoch=?2,binding_fingerprint=?3,policy_fingerprint=?4,authority_fingerprint=?5,connection_id=?6,model_route_id=?7,assignment_json=?8 WHERE owner_id=?9 AND job_id=?10 AND owner_epoch=?11 AND durable_epoch=?12 AND binding_fingerprint=?13 AND policy_fingerprint=?14 AND authority_fingerprint=?15 AND connection_id=?16 AND model_route_id=?17 AND assignment_json=?18",params![r.new_binding.owner_epoch,r.new_binding.durable_epoch,r.new_binding.fingerprint,r.new_assignment.policy_fingerprint,r.new_assignment.authority_fingerprint,r.new_assignment.connection_id,r.new_assignment.model_route_id,new_assignment,r.owner,r.job_id,snap.binding.owner_epoch,snap.binding.durable_epoch,snap.binding.fingerprint,snap.assignment.policy_fingerprint,snap.assignment.authority_fingerprint,snap.assignment.connection_id,snap.assignment.model_route_id,old_assignment])?;if changed!=1 {return Err(unavailable());}
    if tx.execute("UPDATE fm_v2_jobs SET input_hash=?1 WHERE owner_id=?2 AND job_id=?3 AND kind='assistant_interpretation_review' AND state='queued' AND current_attempt_id IS NULL AND attempt_count=0 AND input_hash=?4",params![next,r.owner,r.job_id,snap.input_hash])?!=1 {return Err(unavailable());}
    let value=json!({"version":"s04.paired-pending-rebind.v1","actor":"paired_pending_rebind","owner":r.owner,"account_id":r.account_id,"workspace_id":r.workspace_id,"job_id":r.job_id,"request":request_core(r)?,"request_sha256":request_sha,"old_input_sha256":r.expected_input_sha256,"new_input_sha256":next,"old_binding":snap.binding,"new_binding":r.new_binding,"old_assignment":snap.assignment,"new_assignment":r.new_assignment,"old_binding_sha256":commitment(&snap.binding)?,"new_binding_sha256":commitment(&r.new_binding)?,"old_assignment_sha256":commitment(&snap.assignment)?,"new_assignment_sha256":commitment(&r.new_assignment)?});let raw=serde_json::to_string(&value)?;if raw.len()>65536 {return Err(unavailable());}budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let now=chrono::Utc::now().to_rfc3339();tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','paired_pending_rebind','s04.paired-pending-rebind.v1',NULL,?3,?4,?5,?6)",params![r.owner,id,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    insert_outbox(tx,&r.owner,&id,"paired_pending_rebind",&value,0,&now)?;
    Ok(json!({"version":1,"state":"rebound","replayed":false,"job_id":r.job_id,"input_sha256":next}))
}

/// Read-only same-transaction proof for the guarded owner move. No fence is
/// manufactured here; returned queued IDs are the only S05 busy exemptions.
pub(crate) fn validate_pair_move_fences(tx:&Transaction<'_>,request:&crate::store::sqlite::GuardedOwnerRenameRequest,budget:&mut ResetFenceBudget)->Result<Vec<String>,RepositoryError> {
    validate_pair_move_fences_at(tx,request,&request.owner,false,budget)
}

/// Revalidate the committed delete-move fences under their rekeyed SQL owner.
/// The caller has already proved this exact request is the custody head.
pub(crate) fn validate_erased_pair_purge_fences(tx:&Transaction<'_>,request:&crate::store::sqlite::GuardedOwnerRenameRequest,current:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if !validate_pair_move_fences_at(tx,request,current,true,budget)?.is_empty() {return Err(unavailable());}
    Ok(())
}

fn validate_pair_move_fences_at(tx:&Transaction<'_>,request:&crate::store::sqlite::GuardedOwnerRenameRequest,current:&str,erased_only:bool,budget:&mut ResetFenceBudget)->Result<Vec<String>,RepositoryError> {
    require_guarded_epoch(tx)?;let phase=queued_binding(request);let owner=current;
    validate_queued_binding(tx,&phase,&phase.account_id)?;
    let view=owner_custody::verified_or_direct_custody_view(tx,&phase.database_id,&phase.account_id,owner,&phase.owner,budget)?;let owners=view.owners().to_vec();
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j LEFT JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id WHERE j.owner_id=?1 AND j.kind='assistant_interpretation_review' AND r.job_id IS NULL)",[owner],|r|r.get(0))?;if orphan {return Err(unavailable());}
    let mut stmt=tx.prepare("SELECT j.job_id,j.workspace_key,j.state,length(CAST(r.source_ref_json AS BLOB)),length(CAST(r.coverage_json AS BLOB)) FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id WHERE j.owner_id=?1 AND j.kind='assistant_interpretation_review' ORDER BY j.job_id")?;
    let mut rows=stmt.query([owner])?;let mut queued=Vec::new();
    while let Some(row)=rows.next()? {
        let job:String=row.get(0)?;let workspace:String=row.get(1)?;let state:String=row.get(2)?;let a:i64=row.get(3)?;let b:i64=row.get(4)?;
        if !text(&job) || !text(&workspace) || !(0..=8192).contains(&a) || !(0..=8192).contains(&b) {return Err(unavailable());}budget.check(job.len()+workspace.len()+a as usize+b as usize).map_err(RepositoryError::Validation)?;
        let (raw_source,raw_coverage,policy,candidate):(String,String,String,Option<String>)=tx.query_row("SELECT source_ref_json,coverage_json,policy_fingerprint,candidate_id FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
        let source:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&raw_source).map_err(RepositoryError::Validation)?)?;let coverage:AssistantInterpretationCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw_coverage).map_err(RepositoryError::Validation)?)?;
        if source.account_id!=phase.account_id || !valid_sha256(&policy) {return Err(unavailable());}
        let r=super::paired_transition::PairedRenameFenceRequest{version:1,phase:phase.clone(),workspace_id:workspace.clone(),job_id:job.clone(),source:source.clone(),coverage:coverage.clone(),policy_fingerprint:policy};
        let key=super::paired_transition::exact_job(tx,&r,owner,budget)?;let root=host_archive_source_id(&source)?;let assistant=pair_assistant_source_id(&source,&coverage)?;
        if candidate.is_some() {
            if erased_only {return Err(unavailable());}
            if state!="succeeded" {return Err(unavailable());}
            let exact:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id AND a.attempt_id=r.candidate_attempt_id AND a.lease_epoch=r.candidate_lease_epoch JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind='assistant_interpretation_review' AND j.state='succeeded' AND a.state='succeeded' AND a.attempt_number=j.attempt_count AND q.state='settled' AND q.settled_at_ms IS NOT NULL AND r.candidate_id=?3)",params![owner,job,candidate],|r|r.get(0))?;if !exact {return Err(unavailable());}
            let unsafe_state:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts a LEFT JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.attempt_id=a.attempt_id AND q.job_id=a.job_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2 AND (a.state IN ('leased','running') OR q.state IN ('reserved','dispatched','uncertain') OR q.attempt_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id IS NULL OR source_id IN (?3,?4))) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id IS NULL OR source_id IN (?3,?4)))",params![owner,job,root,assistant],|r|r.get(0))?;if unsafe_state {return Err(unavailable());}
            let rp=super::paired_transition::proof_id(&phase,"root",&root,None)?;let ap=super::paired_transition::proof_id(&phase,"assistant",&assistant,Some(&job))?;
            if !super::paired_transition::receipt(tx,owner,&rp,"paired_rename_root_fence",&super::paired_transition::root_record(&r,&root),source.revision,budget)? || !super::paired_transition::receipt(tx,owner,&ap,"paired_rename_assistant_fence",&super::paired_transition::assistant_record(&r,&assistant,&key),coverage.assistant.revision,budget)? {return Err(unavailable());}
            if super::paired_transition::source_state(tx,owner,&workspace,&root,source.revision,&source.content_hash,"host_archive_part.v1")?!="forget_requested" || super::paired_transition::source_state(tx,owner,&workspace,&assistant,coverage.assistant.revision,&coverage.assistant.content_hash,"host_assistant_part.v1")?!="forget_requested" {return Err(unavailable());}
            super::paired_transition::assistant_hidden(tx,owner,&workspace,&job)?;prove_host_visibility_fenced_custody(tx,owner,&phase.account_id,&workspace,&root,&view,budget)?;continue;
        }
        let attempt_count:i64=tx.query_row("SELECT attempt_count FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0))?;
        let erased=state=="cancelled" && attempt_count==0;
        if erased_only && !erased {return Err(unavailable());}
        let terminal=match state.as_str() {"queued"=>false,"cancelled" if erased=>false,"succeeded"|"failed_terminal"|"cancelled"=>true,_=>return Err(unavailable())};
        let erased_fact=if erased {Some(crate::store::sqlite::SqliteStore::paired_erased_fact_in(tx,owner,&phase.account_id,&workspace,&job,&view,budget).map_err(RepositoryError::Validation)?)} else {None};
        let snap=pending_snapshot(tx,owner,&owners,&workspace,&job,&source,&coverage,terminal,erased,budget)?;let id=pending_fence_id(&phase,&job)?;
        let len:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,id],|r|r.get(0)).optional()?;let len=len.ok_or_else(unavailable)?;if !(0..=16384).contains(&len) {return Err(unavailable());}budget.check(len as usize).map_err(RepositoryError::Validation)?;
        let raw:String=tx.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,id],|r|r.get(0))?;let body=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        if body.as_object().map(|v|v.len())!=Some(if terminal || erased {9} else {8}) || (erased && erased_fact.as_ref().is_none_or(|fact| fact["source_key"]!=key || fact["pair_sha256"]!=coverage.pair_sha256 || fact["input_sha256"]!=snap.input_hash || body["erased_original_receipt_sha256"]!=fact["original_receipt_sha256"])) || body["version"]!="s04.paired-pending-fence.v1" || body["phase"]!=serde_json::to_value(&phase)? || body["workspace_id"]!=workspace || body["job_id"]!=job || body["source_key"]!=key || body["input_hash"]!=snap.input_hash || (terminal && body.get("terminal_disposition")!=snap.disposition.as_ref()) || !super::paired_transition::receipt(tx,owner,&id,"paired_pending_fence",&body,0,budget)? {return Err(unavailable());}
        for (field,kind,id,revision,hash,source_kind) in [("root","root",&root,source.revision,source.content_hash.as_str(),"host_archive_part.v1"),("assistant","assistant",&assistant,coverage.assistant.revision,coverage.assistant.content_hash.as_str(),"host_assistant_part.v1")] {
            let fact=&body[field];if fact.as_object().map(|v|v.len())!=Some(5) || fact["source_id"]!=*id || fact["revision"]!=revision || fact["content_hash"]!=hash {return Err(unavailable());}
            let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![owner,id],|r|r.get(0))?;if erased {return Err(unavailable());}
            match fact["prior_state"].as_str() {
                Some("active")=>{let proof=super::paired_transition::proof_id(&phase,kind,id,if kind=="assistant" {Some(&job)} else {None})?;let expected=if kind=="root" {super::paired_transition::root_record(&r,id)} else {super::paired_transition::assistant_record(&r,id,&key)};let event=if kind=="root" {"paired_rename_root_fence"} else {"paired_rename_assistant_fence"};if fact["fence_id"]!=proof || !super::paired_transition::receipt(tx,owner,&proof,event,&expected,revision,budget)? || super::paired_transition::source_state(tx,owner,&workspace,id,revision,hash,source_kind)?!="forget_requested" {return Err(unavailable());}if kind=="root" {prove_host_visibility_fenced_custody(tx,owner,&phase.account_id,&workspace,id,&view,budget)?;}},
                Some("absent")=>{let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2)",params![owner,id],|r|r.get(0))?;if exists {return Err(unavailable());}if kind=="root" {let proof=super::paired_transition::proof_id(&phase,"root_absent",id,None)?;let expected=json!({"version":"s04.paired-rename-fence.v1","phase":phase,"kind":"root_absent","workspace_id":workspace,"source_id":id,"revision":revision,"content_hash":hash});if fact["fence_id"]!=proof || !super::paired_transition::receipt(tx,owner,&proof,"paired_rename_root_absent",&expected,revision,budget)? {return Err(unavailable());}} else if !fact["fence_id"].is_null() {return Err(unavailable());}},
                _=>return Err(unavailable()),
            }
        }
        if !terminal && !erased {queued.push(job);}
    }
    Ok(queued)
}
