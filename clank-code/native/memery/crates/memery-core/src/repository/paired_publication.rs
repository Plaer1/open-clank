//! Exact original paired model-publication facts. No source or review grant.
use super::*;

#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PairedPublicationStatusRequest {
    pub version:u32,pub owner:String,pub workspace_id:String,pub job_id:String,
    pub attempt_id:String,pub lease_epoch:i64,pub source:HostArchiveSourceRef,
    pub coverage:AssistantInterpretationCoverage,pub policy_fingerprint:String,pub content_sha256:String,
}
fn unavailable()->RepositoryError {RepositoryError::NotFound}
fn text(v:&str)->bool {!v.is_empty() && v==v.trim() && v.len()<=512 && !v.chars().any(char::is_control)}
fn body(request:&AssistantInterpretationCandidateRequest,candidate:&str,key:&str)->Result<Value,RepositoryError> {
    Ok(json!({"version":"s05.paired-publication-original.v1","owner":request.owner,"workspace_id":request.workspace_id,"account_id":request.source.account_id,"job_id":request.job_id,"candidate_id":candidate,"attempt_id":request.attempt_id,"lease_epoch":request.lease_epoch,"source_key":key,"request_sha256":sha256_hex(&serde_json::to_vec(request)?)}))
}
/// Called only by the existing writer while its source, attempt and new
/// candidate are owned in the same transaction. Contains no content bytes.
pub(super) fn write_original(tx:&Transaction<'_>,request:&AssistantInterpretationCandidateRequest,candidate:&str,key:&str,change:&str,now:&str)->Result<(),RepositoryError> {
    let value=body(request,candidate,key)?;insert_outbox(tx,&request.owner,change,"paired_candidate_original",&value,0,now)
}

/// Resolve only an exact original same-attempt publication. The caller owns
/// current account/read authority; no live source or new review is implied.
pub(crate) fn original_status(tx:&Transaction<'_>,r:&PairedPublicationStatusRequest,view:Option<&owner_custody::VerifiedCustodyView<'_,'_>>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_guarded_epoch(tx)?;r.source.validate_in(tx)?;r.coverage.validate(&r.source)?;
    if r.version!=1 || ![r.owner.as_str(),r.workspace_id.as_str(),r.job_id.as_str(),r.attempt_id.as_str()].iter().all(|v|text(v)) || r.lease_epoch<=0 || !valid_sha256(&r.policy_fingerprint) || !valid_sha256(&r.content_sha256) {return Err(unavailable());}
    if let Some(v)=view {if v.current_owner()!=r.owner || v.account_id()!=r.source.account_id {return Err(unavailable());}v.for_captured(tx,&r.owner)?;}
    let source_key=r.coverage.source_key(&r.source)?;
    if crate::store::sqlite::s05_full_denial::read_terminal(tx,&r.owner,&r.source.account_id,&r.job_id,&source_key,view,budget).map_err(RepositoryError::Validation)? {
        return Ok(json!({"version":1,"state":"terminal_erased","publication":"unproved","visibility":"not_rechecked"}));
    }
    let unknown=||json!({"version":1,"state":"unknown"});
    let digest=sha256_hex(&serde_json::to_vec(&(&r.source.account_id,&r.job_id))?);let candidate=format!("s05paircandidate_{digest}");
    let lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(source_ref_json AS BLOB)),length(CAST(coverage_json AS BLOB)) FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2",params![r.owner,r.job_id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
    let Some((a,b))=lengths else {return Ok(unknown())};if !(0..=8192).contains(&a) || !(0..=8192).contains(&b) {return Err(unavailable());}budget.check((a+b) as usize).map_err(RepositoryError::Validation)?;
    let row:Option<(String,String,String,String,Option<String>,String,Option<String>,Option<String>,Option<i64>)>=tx.query_row("SELECT r.source_ref_json,r.coverage_json,r.source_key,r.source_hash,r.source_workspace_id,r.policy_fingerprint,r.candidate_id,r.candidate_attempt_id,r.candidate_lease_epoch FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE r.owner_id=?1 AND r.job_id=?2 AND r.target_workspace_id=?3 AND r.source_account_id=?4 AND j.workspace_key=?3 AND j.project_key='' AND j.kind='assistant_interpretation_review'",params![r.owner,r.job_id,r.workspace_id,r.source.account_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?,row.get(7)?,row.get(8)?))).optional()?;
    let Some((raw_source,raw_coverage,key,hash,source_workspace,policy,associated,attempt,epoch))=row else {return Ok(unknown())};
    let source:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&raw_source).map_err(RepositoryError::Validation)?)?;
    let coverage:AssistantInterpretationCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw_coverage).map_err(RepositoryError::Validation)?)?;
    if source!=r.source || coverage!=r.coverage || key!=coverage.source_key(&source)? || hash!=source.content_hash || source_workspace!=source.workspace_id || policy!=r.policy_fingerprint {return Err(unavailable());}
    match (associated,attempt,epoch) {(None,None,None)=>return Ok(unknown()),(Some(c),Some(a),Some(e)) if c==candidate && a==r.attempt_id && e==r.lease_epoch=>(),_=>return Err(unavailable())};
    let attempt_proof:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2 AND a.attempt_id=?3 AND a.lease_epoch=?4)",params![r.owner,r.job_id,r.attempt_id,r.lease_epoch],|row|row.get(0))?;if !attempt_proof {return Err(unavailable());}
    let mut revisions=Vec::new();for number in 1..=3 {
        let size:Option<i64>=tx.query_row("SELECT length(CAST(proposal_json AS BLOB)) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=?3",params![r.owner,candidate,number],|row|row.get(0)).optional()?;
        let size=size.ok_or_else(unavailable)?;if !(0..=8192).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
        let revision=read_candidate_inner(tx,&r.owner,&candidate,&candidate,number,&r.workspace_id,"",false)?;
        if revision.actor_type!="automation" || revision.actor_id!="assistant_interpretation" || revision.source_event_id.as_deref()!=Some(r.attempt_id.as_str()) || revision.previous_revision!=if number==1 {None} else {Some(number-1)} || revision.payload.state!=match number {1=>CandidateState::Proposed,2=>CandidateState::Validating,_=>CandidateState::AwaitingReview} || revision.content_hash!=hash_value(&serde_json::to_value(&revision.payload)?)? {return Err(unavailable());}
        revisions.push(revision);
    }
    let first=&revisions[0];let last=&revisions[2];if revisions.iter().any(|v|v.change_set_id!=first.change_set_id || v.payload.proposal!=first.payload.proposal) {return Err(unavailable());}
    let proposal=first.payload.proposal.as_object().ok_or_else(unavailable)?;let content=proposal.get("interpretation").and_then(Value::as_str).ok_or_else(unavailable)?;
    if proposal.len()!=3 || proposal.get("version")!=Some(&json!("assistant-interpretation-proposal.v1")) || proposal.get("classification")!=Some(&json!("inferred_untrusted")) || content.is_empty() || content.len()>4096 || sha256_hex(content.as_bytes())!=r.content_sha256 {return Err(unavailable());}
    let event_id=format!("outbox_{}_paired_candidate_original",first.change_set_id);
    let size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![r.owner,event_id],|row|row.get(0)).optional()?;
    let Some(size)=size else {return Ok(unknown())};if !(0..=8192).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let (raw,source_revision):(String,i64)=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='paired_candidate_original'",params![r.owner,event_id,first.change_set_id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?.ok_or_else(unavailable)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let captured=value["owner"].as_str().filter(|v|text(v)).ok_or_else(unavailable)?;
    if let Some(v)=view {v.for_captured(tx,captured)?;} else if captured!=r.owner {return Err(unavailable());}
    let original=AssistantInterpretationCandidateRequest{version:1,owner:captured.into(),workspace_id:r.workspace_id.clone(),job_id:r.job_id.clone(),attempt_id:r.attempt_id.clone(),lease_epoch:r.lease_epoch,source:r.source.clone(),coverage:r.coverage.clone(),policy_fingerprint:r.policy_fingerprint.clone(),content:content.into()};
    if source_revision!=0 || value!=body(&original,&candidate,&key)? {return Err(unavailable());}
    let change:Option<(String,String,String,String,Option<String>)>=tx.query_row("SELECT actor_type,actor_id,reason,content_hash,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND contract_version=?3",params![r.owner,first.change_set_id,CONTRACT_VERSION],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?))).optional()?;
    if change!=Some(("automation".into(),"assistant_interpretation".into(),"unreviewed assistant interpretation".into(),first.content_hash.clone(),Some(r.attempt_id.clone()))) {return Err(unavailable());}
    let candidate_event=format!("outbox_{}_candidate_revision",first.change_set_id);
    let event_size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![r.owner,candidate_event],|row|row.get(0)).optional()?;
    let event_size=event_size.ok_or_else(unavailable)?;if !(0..=8192).contains(&event_size) {return Err(unavailable());}budget.check(event_size as usize).map_err(RepositoryError::Validation)?;
    let event:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_revision'",params![r.owner,candidate_event,first.change_set_id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
    let (raw,revision)=event.ok_or_else(unavailable)?;
    if revision!=3 || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=json!({"candidate_id":candidate,"revision":3}) || last.change_set_id!=first.change_set_id {return Err(unavailable());}
    Ok(json!({"version":1,"state":"applied","candidate_id":candidate,"visibility":"not_rechecked"}))
}

/// Verify one enumerated original using its retained job and candidate facts.
/// A raw event or a missing associated row is never a publication proof.
pub(crate) fn verify_original_event(tx:&Transaction<'_>,owner:&str,account:&str,event_id:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    let len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event_id],|r|r.get(0)).optional()?;
    let len=len.ok_or_else(unavailable)?;if !(0..=8192).contains(&len) {return Err(unavailable());}budget.check(len as usize).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND kind='paired_candidate_original'",params![owner,event_id],|r|r.get(0)).optional()?.ok_or_else(unavailable)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let field=|k:&str|value.get(k).and_then(Value::as_str).filter(|v|text(v)).ok_or_else(unavailable);
    let job=field("job_id")?;let candidate=field("candidate_id")?;let workspace=field("workspace_id")?;let attempt=field("attempt_id")?;
    if value.get("account_id").and_then(Value::as_str)!=Some(account) {return Err(unavailable());}
    let epoch=value.get("lease_epoch").and_then(Value::as_i64).ok_or_else(unavailable)?;
    let sizes:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(source_ref_json AS BLOB)),length(CAST(coverage_json AS BLOB)) FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let (a,b)=sizes.ok_or_else(unavailable)?;if !(0..=8192).contains(&a) || !(0..=8192).contains(&b) {return Err(unavailable());}budget.check((a+b) as usize).map_err(RepositoryError::Validation)?;
    let (source_raw,coverage_raw,policy):(String,String,String)=tx.query_row("SELECT source_ref_json,coverage_json,policy_fingerprint FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    let source:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&source_raw).map_err(RepositoryError::Validation)?)?;
    let coverage:AssistantInterpretationCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&coverage_raw).map_err(RepositoryError::Validation)?)?;
    let size:Option<i64>=tx.query_row("SELECT length(CAST(proposal_json AS BLOB)) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=1",params![owner,candidate],|r|r.get(0)).optional()?;
    let size=size.ok_or_else(unavailable)?;if !(0..=8192).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let revision=read_candidate_inner(tx,owner,candidate,candidate,1,workspace,"",false)?;
    let content=revision.payload.proposal.get("interpretation").and_then(Value::as_str).ok_or_else(unavailable)?;
    let request=PairedPublicationStatusRequest{version:1,owner:owner.into(),workspace_id:workspace.into(),job_id:job.into(),attempt_id:attempt.into(),lease_epoch:epoch,source,coverage,policy_fingerprint:policy,content_sha256:sha256_hex(content.as_bytes())};
    let result=original_status(tx,&request,Some(view),budget)?;
    if result.get("state").and_then(Value::as_str)!=Some("applied") || result.get("candidate_id").and_then(Value::as_str)!=Some(candidate) {return Err(unavailable());}
    let change=&revision.change_set_id;if event_id!=format!("outbox_{change}_paired_candidate_original") {return Err(unavailable());}
    Ok(())
}
