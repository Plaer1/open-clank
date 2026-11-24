//! Phase-bound paired source custody. Host owns the active claim and fresh
//! contributor authorization; these records prove only exact Native facts.
use super::*;
use crate::store::sqlite::{GuardedOwnerRenameRequest,LegacyLineageKind,LegacyLineageTarget,SqliteStore};

#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PairedRenameFenceRequest {
    pub version:u32,pub phase:QueuedTransitionBinding,pub workspace_id:String,pub job_id:String,
    pub source:HostArchiveSourceRef,pub coverage:AssistantInterpretationCoverage,pub policy_fingerprint:String,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PairedRenameRenewRequest {
    pub version:u32,pub transition:GuardedOwnerRenameRequest,pub receipt_id:String,pub receipt_hash:String,
    pub workspace_id:String,pub job_id:String,pub source:HostArchiveSourceRef,
    pub coverage:AssistantInterpretationCoverage,pub policy_fingerprint:String,
    pub root_fence_id:String,pub assistant_fence_id:String,pub policy_projection:PolicyPublicationRefs,
}
fn unavailable()->RepositoryError {RepositoryError::NotFound}
pub(super) fn proof_id(phase:&QueuedTransitionBinding,kind:&str,source:&str,job:Option<&str>)->Result<String,RepositoryError> {
    Ok(format!("cs_s04pair_{}",sha256_hex(&serde_json::to_vec(&("s04.paired-rename-fence.v1",phase,kind,source,job))?)))
}
pub(super) fn root_record(r:&PairedRenameFenceRequest,id:&str)->Value {
    json!({"version":"s04.paired-rename-fence.v1","phase":r.phase,"kind":"root","workspace_id":r.workspace_id,"source_id":id,"revision":r.source.revision,"content_hash":r.source.content_hash})
}
pub(super) fn assistant_record(r:&PairedRenameFenceRequest,id:&str,key:&str)->Value {
    json!({"version":"s04.paired-rename-fence.v1","phase":r.phase,"kind":"assistant","workspace_id":r.workspace_id,"job_id":r.job_id,"source_id":id,"source_key":key,"coverage":r.coverage,"policy_fingerprint":r.policy_fingerprint,"revision":r.coverage.assistant.revision,"content_hash":r.coverage.assistant.content_hash})
}
pub(super) fn exact_job(tx:&Transaction<'_>,r:&PairedRenameFenceRequest,current:&str,budget:&mut ResetFenceBudget)->Result<String,RepositoryError> {
    r.source.validate_in(tx)?;r.coverage.validate(&r.source)?;
    if r.version!=1 || r.phase.account_id!=r.source.account_id || r.workspace_id.is_empty() || r.job_id.is_empty() || !valid_sha256(&r.policy_fingerprint) {return Err(unavailable());}
    validate_queued_binding(tx,&r.phase,&r.source.account_id)?;
    let sizes:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(r.source_ref_json AS BLOB)),length(CAST(r.coverage_json AS BLOB)) FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE r.owner_id=?1 AND r.job_id=?2 AND r.target_workspace_id=?3 AND r.source_account_id=?4 AND j.workspace_key=?3 AND j.project_key='' AND j.kind='assistant_interpretation_review'",params![current,r.job_id,r.workspace_id,r.source.account_id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
    let (source_size,coverage_size)=sizes.ok_or_else(unavailable)?;
    if !(0..=65536).contains(&source_size) || !(0..=65536).contains(&coverage_size) {return Err(unavailable());}
    budget.check((source_size as usize).saturating_add(coverage_size as usize)).map_err(RepositoryError::Validation)?;
    let row:Option<(String,String,String,String,Option<String>,String,Option<String>)>=tx.query_row("SELECT r.source_ref_json,r.coverage_json,r.source_key,r.source_hash,r.source_workspace_id,r.policy_fingerprint,r.candidate_id FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE r.owner_id=?1 AND r.job_id=?2 AND r.target_workspace_id=?3 AND r.source_account_id=?4 AND j.workspace_key=?3 AND j.project_key='' AND j.kind='assistant_interpretation_review'",params![current,r.job_id,r.workspace_id,r.source.account_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?))).optional()?;
    let (source,coverage,key,hash,workspace,policy,_candidate)=row.ok_or_else(unavailable)?;
    let source:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&source).map_err(RepositoryError::Validation)?)?;
    let coverage:AssistantInterpretationCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&coverage).map_err(RepositoryError::Validation)?)?;
    if source!=r.source || coverage!=r.coverage || key!=coverage.source_key(&source)? || hash!=source.content_hash || workspace!=source.workspace_id || policy!=r.policy_fingerprint {return Err(unavailable());}Ok(key)
}
fn charge_fact(tx:&Transaction<'_>,owner:&str,id:&str,kind:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    for (sql,key) in [("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",id.to_owned()),("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",format!("outbox_{id}_{kind}"))] {
        let len:Option<i64>=tx.query_row(sql,params![owner,key],|r|r.get(0)).optional()?;
        if let Some(len)=len {if !(0..=65536).contains(&len) {return Err(unavailable());}budget.check(len as usize).map_err(RepositoryError::Validation)?;}
    }Ok(())
}
pub(super) fn receipt(tx:&Transaction<'_>,sql_owner:&str,id:&str,kind:&str,expected:&Value,revision:i64,budget:&mut ResetFenceBudget)->Result<bool,RepositoryError> {
    charge_fact(tx,sql_owner,id,kind,budget)?;
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s04.paired-rename-fence.v1'",params![sql_owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,contract,source))=row else {return Ok(false)};
    if sha256_hex(raw.as_bytes())!=hash || actor!="automation" || actor_id!="account_transition" || contract!=CONTRACT_VERSION || source.is_some() || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=*expected {return Err(unavailable());}
    let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![sql_owner,format!("outbox_{id}_{kind}"),id,kind],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if event.as_ref().is_none_or(|(value,rev)|value!=&raw || *rev!=revision) {return Err(unavailable());}Ok(true)
}
pub(super) fn insert_receipt(tx:&Transaction<'_>,owner:&str,id:&str,kind:&str,value:&Value,revision:i64,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let raw=serde_json::to_string(value)?;if raw.len()>65536 {return Err(unavailable());}budget.check(raw.len()).map_err(RepositoryError::Validation)?;let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','account_transition','s04.paired-rename-fence.v1',NULL,?3,?4,?5,?6)",params![owner,id,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    insert_outbox(tx,owner,id,kind,value,revision,&now)
}
pub(super) fn source_state(tx:&Transaction<'_>,owner:&str,workspace:&str,id:&str,revision:i64,hash:&str,kind:&str)->Result<String,RepositoryError> {
    let uri=format!("host-archive://{id}");
    let row:Option<(String,String,String,String,String,String)>=tx.query_row("SELECT workspace_key,project_key,source_uri,content_hash,forget_state,source_type FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3",params![owner,id,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (scope,project,stored_uri,stored_hash,state,stored_kind)=row.ok_or_else(unavailable)?;
    if scope!=workspace || !project.is_empty() || stored_uri!=uri || stored_hash!=hash || stored_kind!=kind {return Err(unavailable());}Ok(state)
}
/// A chat deletion precedes account custody. Its proof authorizes factual
/// replay of the already-closed root during deletion and cannot authorize renewal.
pub(super) fn deleted_chat_phase(phase: &QueuedTransitionBinding) -> bool {
    let tombstone = format!("deleted:{}", phase.account_id);
    phase.step == "memory_fm_tombstoned" && match phase.action.as_str() {
        "forward" => phase.new_owner == tombstone,
        "reverse" => phase.owner == tombstone,
        _ => false,
    }
}

pub(super) fn deleted_chat_proof(phase: &QueuedTransitionBinding, workspace: &str, id: &str,
    revision: i64, hash: &str) -> Result<(String, Value), RepositoryError> {
    let key = proof_id(phase, "root_deleted_chat", id, None)?;
    let body = json!({"version":"s04.paired-rename-fence.v1","phase":phase,"kind":"root_deleted_chat",
        "workspace_id":workspace,"source_id":id,"revision":revision,"content_hash":hash});
    Ok((key, body))
}

pub(super) fn has_deleted_chat_proof(tx: &Transaction<'_>, owner: &str, phase: &QueuedTransitionBinding,
    workspace: &str, id: &str, revision: i64, hash: &str, budget: &mut ResetFenceBudget) -> Result<bool, RepositoryError> {
    if !deleted_chat_phase(phase) { return Ok(false); }
    let (key, body) = deleted_chat_proof(phase, workspace, id, revision, hash)?;
    receipt(tx, owner, &key, "paired_rename_root_deleted_chat", &body, revision, budget)
}

/// One ordinary or paired root, fenced under the exact claimed phase.
/// A later already-fenced phase must prove its exact committed predecessor.
pub(crate) fn fence_root(tx:&Transaction<'_>,phase:&QueuedTransitionBinding,workspace:&str,source:&HostArchiveSourceRef,budget:&mut ResetFenceBudget)->Result<(String,bool),RepositoryError> {
    require_guarded_epoch(tx)?;source.validate_in(tx)?;validate_queued_binding(tx,phase,&source.account_id)?;
    if workspace.is_empty() || workspace.trim()!=workspace || workspace.len()>512 {return Err(unavailable());}
    let owner=&phase.owner;let id=host_archive_source_id(source)?;
    let proof=proof_id(phase,"root",&id,None)?;
    let body=json!({"version":"s04.paired-rename-fence.v1","phase":phase,"kind":"root","workspace_id":workspace,"source_id":id,"revision":source.revision,"content_hash":source.content_hash});
    let absent_body=json!({"version":"s04.paired-rename-fence.v1","phase":phase,"kind":"root_absent","workspace_id":workspace,"source_id":id,"revision":source.revision,"content_hash":source.content_hash});
    let active_proof=receipt(tx,owner,&proof,"paired_rename_root_fence",&body,source.revision,budget)?;
    let absent_id=proof_id(phase,"root_absent",&id,None)?;
    let absent_proof=receipt(tx,owner,&absent_id,"paired_rename_root_absent",&absent_body,source.revision,budget)?;
    if has_deleted_chat_proof(tx,owner,phase,workspace,&id,source.revision,&source.content_hash,budget)? {return Err(unavailable());}
    if active_proof && absent_proof {return Err(unavailable());}
    let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![owner,id],|r|r.get(0))?;
    if erased {tracing::debug!(target: "openclank::pair_fence", "root erasure refused");return Err(unavailable());}
    let row:Option<(String,String,String,String,String,String)>=tx.query_row("SELECT workspace_key,project_key,source_uri,content_hash,forget_state,source_type FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3",params![owner,id,source.revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((scope,project,uri,hash,state,kind))=row else {
        let any_revision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2)",params![owner,id],|r|r.get(0))?;
        if active_proof || any_revision {return Err(unavailable());}
        if !absent_proof {insert_receipt(tx,owner,&absent_id,"paired_rename_root_absent",&absent_body,source.revision,budget)?;}
        return Ok((absent_id,false));
    };
    if absent_proof || scope!=workspace || !project.is_empty() || uri!=format!("host-archive://{id}") || hash!=source.content_hash || kind!="host_archive_part.v1" {tracing::debug!(target: "openclank::pair_fence", "root source binding refused");return Err(unavailable());}
    if active_proof {
        if state!="forget_requested" {return Err(unavailable());}
    } else if state=="active" {
        if tx.execute("UPDATE fm_v2_sources SET forget_state='forget_requested' WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 AND forget_state='active'",params![owner,id,source.revision])?!=1 {return Err(unavailable());}
    } else if state=="forget_requested" {
        let view=owner_custody::verified_custody_view(tx,&phase.database_id,&phase.account_id,owner,owner,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "root prior custody refused");e})?;
        let (request,receipt_id,receipt_hash)=view.head_transition(tx).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "root prior head refused");e})?.ok_or_else(|| {tracing::debug!(target: "openclank::pair_fence", "root prior head missing");unavailable()})?;
        let previous=queued_binding(request);
        let follows=phase.database_id==previous.database_id && phase.account_id==previous.account_id && phase.operation_id==previous.operation_id && phase.step==previous.step && phase.owner==previous.new_owner && phase.new_owner==previous.owner && ((phase.action=="reverse" && previous.action=="forward" && phase.generation==previous.generation) || (phase.action=="forward" && previous.action=="reverse" && previous.generation.checked_add(1)==Some(phase.generation)));
        if !follows {tracing::debug!(target: "openclank::pair_fence", "root phase continuity refused");return Err(unavailable());}
        if !view.is_head_transition(tx,request,receipt_id,receipt_hash)? {tracing::debug!(target: "openclank::pair_fence", "root head identity refused");return Err(unavailable());}
        let prior_id=proof_id(&previous,"root",&id,None)?;
        let prior_body=json!({"version":"s04.paired-rename-fence.v1","phase":previous,"kind":"root","workspace_id":workspace,"source_id":id,"revision":source.revision,"content_hash":source.content_hash});
        if !receipt(tx,owner,&prior_id,"paired_rename_root_fence",&prior_body,source.revision,budget).map_err(|e| {tracing::debug!(target: "openclank::pair_fence", "root prior receipt invalid");e})? {tracing::debug!(target: "openclank::pair_fence", "root prior receipt missing");return Err(unavailable());}
    } else {return Err(unavailable());}
    if !active_proof {insert_receipt(tx,owner,&proof,"paired_rename_root_fence",&body,source.revision,budget)?;}
    Ok((proof,true))
}

/// Fence one exact assistant contributor; current caller separately validates
/// the job and Host authority. A prior policy hash is historical proof only.
pub(crate) fn fence_assistant(tx:&Transaction<'_>,r:&PairedRenameFenceRequest,key:&str,budget:&mut ResetFenceBudget)->Result<(String,bool),RepositoryError> {
    let owner=&r.phase.owner;let id=pair_assistant_source_id(&r.source,&r.coverage)?;
    let proof=proof_id(&r.phase,"assistant",&id,Some(&r.job_id))?;
    let body=assistant_record(r,&id,key);
    let exists=receipt(tx,owner,&proof,"paired_rename_assistant_fence",&body,r.coverage.assistant.revision,budget)?;
    let absent_id=proof_id(&r.phase,"assistant_absent",&id,Some(&r.job_id))?;
    let absent_body=json!({"version":"s04.paired-rename-fence.v1","phase":r.phase,"kind":"assistant_absent","workspace_id":r.workspace_id,"job_id":r.job_id,"source_id":id,"source_key":key,"coverage":r.coverage,"policy_fingerprint":r.policy_fingerprint,"revision":r.coverage.assistant.revision,"content_hash":r.coverage.assistant.content_hash});
    let absent=receipt(tx,owner,&absent_id,"paired_rename_assistant_absent",&absent_body,r.coverage.assistant.revision,budget)?;
    if exists && absent {return Err(unavailable());}
    let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![owner,id],|r|r.get(0))?;
    if erased {return Err(unavailable());}
    let row:Option<(String,String,String,String,String,String)>=tx.query_row("SELECT workspace_key,project_key,source_uri,content_hash,forget_state,source_type FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3",params![owner,id,r.coverage.assistant.revision],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?))).optional()?;
    let Some((scope,project,uri,hash,state,kind))=row else {
        let any_revision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2)",params![owner,id],|row|row.get(0))?;
        if exists || any_revision {return Err(unavailable());}
        if !absent {insert_receipt(tx,owner,&absent_id,"paired_rename_assistant_absent",&absent_body,r.coverage.assistant.revision,budget)?;}
        return Ok((absent_id,false));
    };
    if absent || scope!=r.workspace_id || !project.is_empty() || uri!=format!("host-archive://{id}") || hash!=r.coverage.assistant.content_hash || kind!="host_assistant_part.v1" {return Err(unavailable());}
    if exists {if state!="forget_requested" {return Err(unavailable());}}
    else if state=="active" {
        if tx.execute("UPDATE fm_v2_sources SET forget_state='forget_requested' WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 AND forget_state='active'",params![owner,id,r.coverage.assistant.revision])?!=1 {return Err(unavailable());}
    } else if state=="forget_requested" {
        let view=owner_custody::verified_custody_view(tx,&r.phase.database_id,&r.phase.account_id,owner,owner,budget)?;
        let (request,receipt_id,receipt_hash)=view.head_transition(tx)?.ok_or_else(unavailable)?;let previous=queued_binding(request);
        let follows=r.phase.database_id==previous.database_id && r.phase.account_id==previous.account_id && r.phase.operation_id==previous.operation_id && r.phase.step==previous.step && r.phase.owner==previous.new_owner && r.phase.new_owner==previous.owner && ((r.phase.action=="reverse" && previous.action=="forward" && r.phase.generation==previous.generation) || (r.phase.action=="forward" && previous.action=="reverse" && previous.generation.checked_add(1)==Some(r.phase.generation)));
        if !follows || !view.is_head_transition(tx,request,receipt_id,receipt_hash)? {return Err(unavailable());}
        let prior_id=proof_id(&previous,"assistant",&id,Some(&r.job_id))?;charge_fact(tx,owner,&prior_id,"paired_rename_assistant_fence",budget)?;
        let raw:Option<String>=tx.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s04.paired-rename-fence.v1'",params![owner,prior_id],|row|row.get(0)).optional()?;
        let value=crate::field_authority::parse_unique_json(raw.as_deref().ok_or_else(unavailable)?).map_err(RepositoryError::Validation)?;
        let old_policy=value["policy_fingerprint"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
        let mut old=r.clone();old.phase=previous;old.policy_fingerprint=old_policy.to_owned();
        if !receipt(tx,owner,&prior_id,"paired_rename_assistant_fence",&assistant_record(&old,&id,key),r.coverage.assistant.revision,budget)? {return Err(unavailable());}
    } else {return Err(unavailable());}
    if !exists {insert_receipt(tx,owner,&proof,"paired_rename_assistant_fence",&body,r.coverage.assistant.revision,budget)?;}
    Ok((proof,true))
}

pub(super) fn assistant_hidden(tx:&Transaction<'_>,owner:&str,workspace:&str,job:&str)->Result<(),RepositoryError> {
    let candidate:Option<String>=tx.query_row("SELECT candidate_id FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0)).optional()?.flatten();
    if let Some(candidate)=candidate {
        let (status,curated):(String,Option<String>)=tx.query_row("SELECT status,accepted_curated_id FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or_else(unavailable)?;
        if !matches!(status.as_str(),"pending"|"accepted"|"rejected") || (status=="accepted")!=curated.is_some() {return Err(unavailable());}
        let mut targets=vec![LegacyLineageTarget{kind:LegacyLineageKind::Candidate,owner:owner.into(),workspace:workspace.into(),id:candidate}];
        if let Some(id)=curated {targets.push(LegacyLineageTarget{kind:LegacyLineageKind::Curated,owner:owner.into(),workspace:workspace.into(),id});}
        let hidden=SqliteStore::rejected_legacy_lineage(tx,&targets)?;if targets.iter().any(|v|!hidden.contains(v)){return Err(unavailable());}
    }Ok(())
}
/// Caller holds the actual Host transition claim and exact named scope. This
/// transaction records only contributors that were actually active beforehand.
pub(crate) fn fence_pair(tx:&Transaction<'_>,r:&PairedRenameFenceRequest,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_guarded_epoch(tx)?;let owner=&r.phase.owner;let key=exact_job(tx,r,owner,budget)?;
    let root=host_archive_source_id(&r.source)?;let assistant=pair_assistant_source_id(&r.source,&r.coverage)?;
    let root_id=proof_id(&r.phase,"root",&root,None)?;let assistant_id=proof_id(&r.phase,"assistant",&assistant,Some(&r.job_id))?;
    let (actual_root,root_active)=fence_root(tx,&r.phase,&r.workspace_id,&r.source,budget)?;
    if !root_active || actual_root!=root_id {return Err(unavailable());}
    let (actual_assistant,assistant_active)=fence_assistant(tx,r,&key,budget)?;
    if !assistant_active || actual_assistant!=assistant_id {return Err(unavailable());}
    let stray:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND forget_state='active')",params![owner,assistant],|r|r.get(0))?;
    if stray {return Err(unavailable());}
    host_visibility_with_account_phase(tx,&r.phase,&r.workspace_id,&root,budget,true)?;assistant_hidden(tx,owner,&r.workspace_id,&r.job_id)?;
    Ok(json!({"version":1,"state":"fenced","root_fence_id":root_id,"assistant_fence_id":assistant_id}))
}

pub(super) fn renewal_id(phase:&QueuedTransitionBinding,kind:&str,source:&str,job:Option<&str>)->Result<String,RepositoryError> {
    Ok(format!("cs_s04pairrenew_{}",sha256_hex(&serde_json::to_vec(&("s04.paired-rename-renew.v1",phase,kind,source,job))?)))
}
pub(super) fn renewal_receipt(tx:&Transaction<'_>,owner:&str,id:&str,kind:&str,expected:&Value,revision:i64,budget:&mut ResetFenceBudget)->Result<bool,RepositoryError> {
    charge_fact(tx,owner,id,kind,budget)?;
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s04.paired-rename-renew.v1'",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,contract,source))=row else {return Ok(false)};
    if sha256_hex(raw.as_bytes())!=hash || actor!="automation" || actor_id!="account_transition" || contract!=CONTRACT_VERSION || source.is_some() || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=*expected {return Err(unavailable());}
    let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,format!("outbox_{id}_{kind}"),id,kind],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if event.as_ref().is_none_or(|(value,rev)|value!=&raw || *rev!=revision) {return Err(unavailable());}Ok(true)
}
fn insert_renewal(tx:&Transaction<'_>,owner:&str,id:&str,kind:&str,value:&Value,revision:i64,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let raw=serde_json::to_string(value)?;if raw.len()>65536 {return Err(unavailable());}budget.check(raw.len()).map_err(RepositoryError::Validation)?;let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','account_transition','s04.paired-rename-renew.v1',NULL,?3,?4,?5,?6)",params![owner,id,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    insert_outbox(tx,owner,id,kind,value,revision,&now)
}
/// Renew one original root after the exact committed phase. The caller owns
/// fresh Host source/account guards and invokes this in its original transaction.
pub(crate) fn renew_root(tx:&Transaction<'_>,transition:&GuardedOwnerRenameRequest,receipt_id:&str,receipt_hash:&str,workspace:&str,source:&HostArchiveSourceRef,fence_id:&str,policy:&PolicyPublicationRefs,budget:&mut ResetFenceBudget)->Result<(String,bool),RepositoryError> {
    require_guarded_epoch(tx)?;source.validate_in(tx)?;
    let phase=queued_binding(transition);let current=&transition.new_owner;
    if workspace.is_empty() || workspace.trim()!=workspace || workspace.len()>512 || phase.account_id!=source.account_id {return Err(unavailable());}
    require_policy_projection(tx,&phase.account_id,workspace,Some(policy))?;
    let view=owner_custody::verified_custody_view(tx,&phase.database_id,&phase.account_id,current,&phase.owner,budget)?;
    if !view.is_head_transition(tx,transition,receipt_id,receipt_hash)? {return Err(unavailable());}
    let id=host_archive_source_id(source)?;
    let active_id=proof_id(&phase,"root",&id,None)?;let absent_id=proof_id(&phase,"root_absent",&id,None)?;
    let active_body=json!({"version":"s04.paired-rename-fence.v1","phase":phase,"kind":"root","workspace_id":workspace,"source_id":id,"revision":source.revision,"content_hash":source.content_hash});
    let absent_body=json!({"version":"s04.paired-rename-fence.v1","phase":phase,"kind":"root_absent","workspace_id":workspace,"source_id":id,"revision":source.revision,"content_hash":source.content_hash});
    let active=receipt(tx,current,&active_id,"paired_rename_root_fence",&active_body,source.revision,budget)?;
    let absent=receipt(tx,current,&absent_id,"paired_rename_root_absent",&absent_body,source.revision,budget)?;
    if active==absent || fence_id!=if active {active_id.as_str()} else {absent_id.as_str()} {return Err(unavailable());}
    let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![current,id],|r|r.get(0))?;
    if erased {return Err(unavailable());}
    if absent {
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2)",params![current,id],|r|r.get(0))?;
        if exists {return Err(unavailable());} return Ok((absent_id,false));
    }
    let renewal=renewal_id(&phase,"root",&id,None)?;
    let body=json!({"version":"s04.paired-rename-renew.v1","phase":phase,"owner":current,"kind":"root","workspace_id":workspace,"source_id":id,"revision":source.revision,"content_hash":source.content_hash,"fence_id":active_id});
    let done=renewal_receipt(tx,current,&renewal,"paired_rename_root_renewed",&body,source.revision,budget)?;
    let state=source_state(tx,current,workspace,&id,source.revision,&source.content_hash,"host_archive_part.v1")?;
    if done {if state!="active" {return Err(unavailable());}} else {
        if state!="forget_requested" {return Err(unavailable());}
        if tx.execute("UPDATE fm_v2_sources SET forget_state='active' WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 AND forget_state='forget_requested'",params![current,id,source.revision])?!=1 {return Err(unavailable());}
        insert_renewal(tx,current,&renewal,"paired_rename_root_renewed",&body,source.revision,budget)?;
    }
    Ok((renewal,true))
}

/// Host must freshly authorize both contributors and the current named account
/// immediately before this private operation. A historical fence or custody
/// link alone does not grant renewal.
pub(crate) fn renew_pair(tx:&Transaction<'_>,r:&PairedRenameRenewRequest,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_guarded_epoch(tx)?;
    if r.version!=1 {return Err(unavailable());}
    let phase=queued_binding(&r.transition);let current=&r.transition.new_owner;
    require_policy_projection(tx,&phase.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    let view=owner_custody::verified_custody_view(tx,&phase.database_id,&phase.account_id,current,&phase.owner,budget)?;
    if !view.is_head_transition(tx,&r.transition,&r.receipt_id,&r.receipt_hash)? {return Err(unavailable());}
    let fence=PairedRenameFenceRequest{version:1,phase:phase.clone(),workspace_id:r.workspace_id.clone(),job_id:r.job_id.clone(),source:r.source.clone(),coverage:r.coverage.clone(),policy_fingerprint:r.policy_fingerprint.clone()};
    let key=exact_job(tx,&fence,current,budget)?;
    let root=host_archive_source_id(&r.source)?;let assistant=pair_assistant_source_id(&r.source,&r.coverage)?;
    let root_id=proof_id(&phase,"root",&root,None)?;let assistant_id=proof_id(&phase,"assistant",&assistant,Some(&r.job_id))?;
    if r.root_fence_id!=root_id || r.assistant_fence_id!=assistant_id || !receipt(tx,current,&root_id,"paired_rename_root_fence",&root_record(&fence,&root),r.source.revision,budget)? || !receipt(tx,current,&assistant_id,"paired_rename_assistant_fence",&assistant_record(&fence,&assistant,&key),r.coverage.assistant.revision,budget)? {return Err(unavailable());}
    let root_renew=renewal_id(&phase,"root",&root,None)?;let assistant_renew=renewal_id(&phase,"assistant",&assistant,Some(&r.job_id))?;
    let root_body=json!({"version":"s04.paired-rename-renew.v1","phase":phase,"owner":current,"kind":"root","workspace_id":r.workspace_id,"source_id":root,"revision":r.source.revision,"content_hash":r.source.content_hash,"fence_id":root_id});
    let assistant_body=json!({"version":"s04.paired-rename-renew.v1","phase":phase,"owner":current,"kind":"assistant","workspace_id":r.workspace_id,"job_id":r.job_id,"source_id":assistant,"source_key":key,"coverage":r.coverage,"revision":r.coverage.assistant.revision,"content_hash":r.coverage.assistant.content_hash,"fence_id":assistant_id,"root_fence_id":root_id});
    for (id,revision,hash,source_kind,renew_id,event_kind,body) in [(&root,r.source.revision,r.source.content_hash.as_str(),"host_archive_part.v1",&root_renew,"paired_rename_root_renewed",&root_body),(&assistant,r.coverage.assistant.revision,r.coverage.assistant.content_hash.as_str(),"host_assistant_part.v1",&assistant_renew,"paired_rename_assistant_renewed",&assistant_body)] {
        budget.check(id.len().saturating_add(hash.len())).map_err(RepositoryError::Validation)?;
        let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![current,id],|r|r.get(0))?;
        if erased {return Err(unavailable());}
        let exists=renewal_receipt(tx,current,renew_id,event_kind,body,revision,budget)?;
        let state=source_state(tx,current,&r.workspace_id,id,revision,hash,source_kind)?;
        if exists {if state!="active" {return Err(unavailable());}} else {
            if state!="forget_requested" {return Err(unavailable());}
            if tx.execute("UPDATE fm_v2_sources SET forget_state='active' WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 AND forget_state='forget_requested'",params![current,id,revision])?!=1 {return Err(unavailable());}
            insert_renewal(tx,current,renew_id,event_kind,body,revision,budget)?;
        }
    }
    Ok(json!({"version":1,"state":"renewed","root_fence_id":root_id,"assistant_fence_id":assistant_id}))
}

impl<'a> V2Repository<'a> {
    pub(crate) fn paired_rename_fence(&mut self,request:&PairedRenameFenceRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let result=fence_pair(&tx,request,&mut budget)?;drop(budget);tx.commit()?;Ok(result)
    }
    pub(crate) fn paired_rename_renew(&mut self,request:&PairedRenameRenewRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let result=renew_pair(&tx,request,&mut budget)?;drop(budget);tx.commit()?;Ok(result)
    }
}
