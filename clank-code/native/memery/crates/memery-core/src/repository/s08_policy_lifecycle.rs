//! Same-transaction, custody-qualified S08 policy facts. No live grant is made here.
use super::*;

fn unavailable()->RepositoryError {RepositoryError::NotFound}

/// Read the exact original change/outbox pair in the current SQL namespace.
/// An absent pair is unknown; an incomplete pair is unavailable.
fn original_fact(tx:&Connection,owner:&str,id:&str,reason:&str,kind:&str,actor_id:&str,source:Option<&str>,revision:i64,budget:&mut ResetFenceBudget)->Result<Option<(String,Value)>,RepositoryError> {
    let event=format!("outbox_{id}_{kind}");
    let lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 WHERE c.owner_id=?1 AND c.change_set_id=?2",params![owner,id,event],|r|Ok((s08_bounded_first(r,0)?,r.get(1)?))).optional()?;
    let Some((a,b))=lengths else {
        let residue:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,event],|r|s08_bounded_first(r,0))?;
        return if residue {Err(unavailable())} else {Ok(None)};
    };
    if !(0..=262144).contains(&a) || !(0..=262144).contains(&b) {return Err(unavailable())}
    budget.check((a+b) as usize).map_err(RepositoryError::Validation)?;
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,reason],|r|Ok((s08_bounded_first(r,0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (hash,raw,actor,recorded_actor,contract,recorded_source)=row.ok_or_else(unavailable)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="automation" || recorded_actor!=actor_id || contract!=CONTRACT_VERSION || recorded_source.as_deref()!=source {return Err(unavailable())}
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,kind],|r|Ok((s08_bounded_first(r,0)?,r.get(1)?))).optional()?;
    if out.as_ref().is_none_or(|(payload,n)|*n!=revision || crate::field_authority::parse_unique_json(payload).ok()!=Some(value.clone())) {return Err(unavailable())}
    Ok(Some((raw,value)))
}

/// Exact no-attempt cancellation fact. Absence is never an issuance grant.
pub(crate) fn original_prepared_cancel(tx:&Transaction<'_>,owner:&str,account:&str,m:&S08ModelPrepareRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<Value>,RepositoryError> {
    let s=&m.selection;if view.current_owner()!=owner || view.account_id()!=account || s.source_account_id!=account {return Err(unavailable())}view.for_captured(tx,&s.owner)?;
    let job=format!("s08job_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-job.v1",&s.owner,&s.idempotency_key,&m.output_kind))?));
    let id=format!("cs_s08policycancel_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-cancel.v1",account,&job))?));
    let Some((_,value))=original_fact(tx,owner,&id,"s08.policy-cancel.v1","s08_policy_cancel","s08_policy",None,0,budget)? else {return Ok(None)};
    V2Repository::s08_model_intent_fact_scoped(tx,m,&job,owner,budget)?;
    let raw=serde_json::to_vec(m)?;if raw.len()>262144 {return Err(unavailable())}budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if value!=json!({"version":"s08.policy-cancel.v1","owner":s.owner,"account_id":account,"job_id":job,"request_sha256":sha256_hex(&raw),"state":"cancelled"}) {return Err(unavailable())}
    let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,&job))?));
    let exact:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind='access_consolidation' AND state='cancelled' AND attempt_count=0 AND current_attempt_id IS NULL AND result_json IS NULL) AND NOT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2) AND NOT EXISTS(SELECT 1 FROM candidates WHERE id=?3) AND NOT EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?3) AND NOT EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='candidate' AND original_id=?3) AND NOT EXISTS(SELECT 1 FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase<>'selection') AND NOT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND reason IN ('s08.model-reserve.v1','s08.policy-result.v1','s08.policy-terminal.v1') AND source_event_id=?2)",params![owner,job,candidate],|x|x.get(0))?;
    if !exact {return Err(unavailable())}
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND kind IN ('s08_model_reserve','s08_policy_result','s08_policy_terminal') AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.job_id')=?2 ELSE 0 END)",params![owner,job],|x|x.get(0))?;
    if orphan {return Err(unavailable())}Ok(Some(json!({"version":1,"state":"cancelled","job_id":job})))
}

/// Read a specific original terminal fact without hydrating live policy or sources.
/// The caller supplies current authenticated custody and the unchanged request.
pub(crate) fn original_terminal(tx:&Transaction<'_>,owner:&str,account:&str,r:&S08PolicyTerminalRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<Value>,RepositoryError> {
    let a=&r.attempt;let m=&a.original;let s=&m.selection;let reserve=&a.reservation_original;let q=&reserve.reservation;
    if view.current_owner()!=owner || view.account_id()!=account || s.source_account_id!=account || r.version!=1 || a.version!=1 || reserve.version!=1 || (r.reason.is_some() && (r.disposition!="failed" || r.reason.as_deref()!=Some("invalid_proposal"))) || !matches!(r.disposition.as_str(),"positively_unsent"|"failed"|"cancelled") || !matches!(m.mode.as_str(),"model"|"hybrid"|"rules") || (r.disposition=="positively_unsent" && r.usage.is_some()) || (m.mode=="rules" && (r.disposition!="cancelled" || r.usage.is_some())) || a.attempt_id.is_empty() || a.attempt_id.len()>512 || a.lease_epoch<=0 || q.owner!=s.owner || q.job_id!=a.job_id || serde_json::to_vec(m)?!=serde_json::to_vec(&reserve.original)? {return Err(unavailable())}
    view.for_captured(tx,&s.owner)?;deny_policy_reissue(tx,owner,account,m,view,budget)?;
    let id=format!("cs_s08policyterminal_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-terminal.v1",account,&a.job_id,&a.attempt_id,a.lease_epoch))?));
    let Some((_,receipt))=original_fact(tx,owner,&id,"s08.policy-terminal.v1","s08_policy_terminal","s08_policy",Some(&a.attempt_id),0,budget)? else {return Ok(None)};
    V2Repository::s08_model_intent_fact_scoped(tx,m,&a.job_id,owner,budget)?;
    let raw=serde_json::to_vec(r)?;if raw.len()>262144 {return Err(unavailable())}budget.check(raw.len()).map_err(RepositoryError::Validation)?;let digest=sha256_hex(&raw);
    let identity=V2Repository::s08_model_reserve_identity_scoped(tx,reserve,owner,budget)?.ok_or_else(unavailable)?;
    if identity["job_id"]!=a.job_id || identity["attempt_id"]!=a.attempt_id || identity["lease_epoch"]!=a.lease_epoch {return Err(unavailable())}
    let foreground=identity["foreground_epoch"].as_i64().filter(|n|*n>=0).ok_or_else(unavailable)?;
    let rr=serde_json::to_vec(reserve)?;let rh=sha256_hex(&rr);
    let rid=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,&a.job_id,&rh))?));
    let prior=receipt["prior_reservation_state"].as_str().filter(|v|matches!(*v,"reserved"|"dispatched")).ok_or_else(unavailable)?;
    if prior=="reserved" && r.disposition!="positively_unsent" && m.mode!="rules" {return Err(unavailable())}
    let label=if r.disposition=="positively_unsent" || (m.mode=="rules" && prior=="reserved") {"known_zero"} else if r.usage.is_some() {"reported"} else {"reserved_bound"};
    if receipt!=json!({"version":"s08.policy-terminal.v1","request_sha256":digest,"owner":s.owner,"account_id":account,"job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"disposition":r.disposition,"reason":r.reason,"usage":label,"reported_usage":r.usage,"prior_reservation_state":prior,"reserve_receipt_id":rid}) {return Err(unavailable())}
    if m.mode=="rules" && prior=="reserved" {
        let exact:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND j.state='cancelled' AND j.current_attempt_id IS NULL AND j.result_json IS NULL AND a.attempt_id=?3 AND a.lease_epoch=?4 AND a.state='fenced' AND q.state='revoked' AND q.foreground_epoch=?5 AND q.connection_id='local:rules' AND q.reserved_calls=0 AND q.charged_calls=0 AND q.reserved_input_tokens=0 AND q.reserved_output_tokens=0 AND q.charged_input_tokens=0 AND q.charged_output_tokens=0)",params![owner,a.job_id,a.attempt_id,a.lease_epoch,foreground],|x|x.get(0))?;
        if !exact {return Err(unavailable())}return Ok(Some(json!({"version":1,"state":"settled","disposition":"cancelled"})))
    }
    let (calls,seconds,input,output)=if r.disposition=="positively_unsent" {(0,0,0,0)} else if m.mode=="rules" {(0,i64::from(q.job_seconds),0,0)} else if let Some(u)=&r.usage {if i64::from(u.elapsed_seconds)>i64::from(q.job_seconds) || u.input_tokens>m.max_input_tokens || u.output_tokens>m.max_output_tokens {return Err(unavailable())}(1,i64::from(u.elapsed_seconds),i64::from(u.input_tokens),i64::from(u.output_tokens))} else {(1,i64::from(q.job_seconds),i64::from(m.max_input_tokens),i64::from(m.max_output_tokens))};
    let state=if r.disposition=="cancelled" {"cancelled"} else {"failed_terminal"};
    let outcome=json!({"version":"s08.policy-terminal-outcome.v1","request_sha256":digest,"disposition":r.disposition,"reason":r.reason,"usage":label,"reported_usage":r.usage,"prior_reservation_state":prior,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"reserve_receipt_id":rid});
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND a.attempt_id=?3 AND a.lease_epoch=?4 AND j.state=?5 AND a.state=?5 AND q.state='settled' AND q.settled_at_ms IS NOT NULL AND q.foreground_epoch=?6 AND q.connection_id=?7 AND q.reserved_calls=?19 AND q.reserved_seconds=?8 AND q.reserved_input_tokens=?9 AND q.reserved_output_tokens=?10 AND q.coverage_generation=?11 AND q.clock_generation=?12 AND a.lease_owner=?13 AND q.charged_calls=?14 AND q.charged_seconds=?15 AND q.charged_input_tokens=?16 AND q.charged_output_tokens=?17 AND j.result_json=?18)",params![owner,a.job_id,a.attempt_id,a.lease_epoch,state,foreground,m.connection_id,q.job_seconds,m.max_input_tokens,m.max_output_tokens,q.coverage_generation,q.clock_generation,q.worker_id,calls,seconds,input,output,serde_json::to_string(&outcome)?,if m.mode=="rules" {0} else {1}],|x|x.get(0))?;
    if !exists {return Err(unavailable())}Ok(Some(json!({"version":1,"state":"settled","disposition":r.disposition})))
}

/// Resolve only this exact attempt's retained terminal outcome; no latest-attempt inference.
pub(crate) fn terminal_for_attempt(tx:&Transaction<'_>,owner:&str,account:&str,a:&S08ModelAttemptRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<Value>,RepositoryError> {
    let raw:Option<String>=tx.query_row("SELECT result_json FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND length(CAST(result_json AS BLOB))<=8192",params![owner,a.job_id],|r|r.get(0)).optional()?.flatten();
    let Some(raw)=raw else {if a.original.mode=="rules" {let r=S08PolicyTerminalRequest{version:1,attempt:a.clone(),disposition:"cancelled".into(),usage:None,reason:None};return original_terminal(tx,owner,account,&r,view,budget)}return Ok(None)};budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    if value["version"]!="s08.policy-terminal-outcome.v1" {return Ok(None)}
    if value["attempt_id"]!=a.attempt_id || value["lease_epoch"]!=a.lease_epoch {return Err(unavailable())}
    let r:S08PolicyTerminalRequest=serde_json::from_value(json!({"version":1,"attempt":a,"disposition":value["disposition"],"usage":value["reported_usage"],"reason":value["reason"]}))?;
    original_terminal(tx,owner,account,&r,view,budget)
}

/// Verify an original settled result from its immutable original request. The
/// authenticated current account and custody view are independent of that request.
/// This does not hydrate current sources or grant a retry or review mutation.
pub(crate) fn original_result(tx:&Transaction<'_>,current_owner:&str,account:&str,r:&S08PolicyCommitRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<Value>,RepositoryError> {
    if view.current_owner()!=current_owner || view.account_id()!=account || r.version!=1 || r.attempt.version!=1 || r.attempt.reservation_original.version!=1 {return Err(unavailable())}
    if read_terminal(tx,current_owner,account,r,view,budget)?.is_some() {return Ok(Some(json!({"version":1,"state":"erased"})))}
    if let Some(terminal)=terminal_for_attempt(tx,current_owner,account,&r.attempt,view,budget)? {return Ok(Some(terminal))}
    let attempt=&r.attempt;let original=&attempt.original;let selection=&original.selection;let reserve=&attempt.reservation_original;
    view.for_captured(tx,&selection.owner)?;
    if original.version!=1 || selection.version!=1 || selection.source_account_id!=account || reserve.reservation.owner!=selection.owner || attempt.job_id!=reserve.reservation.job_id || attempt.lease_epoch<=0 || attempt.attempt_id.is_empty() || attempt.attempt_id.len()>512 || serde_json::to_vec(original)?!=serde_json::to_vec(&reserve.original)? || !s08_policy_accounting_valid(original) || !valid_sha256(&original.assignment_sha256) || !valid_sha256(&original.snapshots_sha256) || !s08_importance_valid(&original.mode,&original.importance) || !s08_connection_predicate_valid(&original.output_kind,original.connection_predicate.as_ref()) {return Err(unavailable())}
    let job=format!("s08job_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-job.v1",&selection.owner,&selection.idempotency_key,&original.output_kind))?));
    if job!=attempt.job_id {return Err(unavailable())}
    let intent_id=format!("cs_s08model_{}",sha256_hex(&serde_json::to_vec(&(account,&job))?));
    let intent=serde_json::to_string(original)?;if intent.len()>262144 {return Err(unavailable())}budget.check(intent.len()).map_err(RepositoryError::Validation)?;
    if original_fact(tx,current_owner,&intent_id,"s08.model-intent.v1","s08_model_intent","s08_model",None,0,budget)?.is_none_or(|(raw,_)|raw!=intent) {return Err(unavailable())}
    let selection_raw=serde_json::to_string(selection)?;if selection_raw.len()>262144 {return Err(unavailable())}budget.check(selection_raw.len()).map_err(RepositoryError::Validation)?;
    let selection_hash=sha256_hex(selection_raw.as_bytes());let selection_id=format!("cs_s08selection_{}",sha256_hex(&serde_json::to_vec(&(&selection.owner,&job))?));
    if original_fact(tx,current_owner,&selection_id,"s08.selection.v1","s08_selection","s08_rules",None,1,budget)?.is_none_or(|(raw,_)|raw!=selection_raw) {return Err(unavailable())}
    let mut ids=std::collections::BTreeSet::new();for item in &selection.admissions {if !ids.insert(item.event_id.clone()) {return Err(unavailable())}}for item in &selection.candidates {ids.insert(format!("s08revision_{}",sha256_hex(&serde_json::to_vec(item)?)));}
    if let Some(j)=&selection.journal {ids=j.selected_ids()?;}
    let tool=sha256_hex(format!("s08-policy-{}-{}-v1",original.mode,original.output_kind).as_bytes());
    let manifest:Option<(String,String,String,String)>=tx.query_row("SELECT j.input_hash,m.model_hash,m.tool_hash,m.selected_json FROM fm_v2_jobs j JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind='access_consolidation' AND j.workspace_key=?3 AND j.project_key='' AND m.phase='selection' AND m.input_hash=j.input_hash AND m.content_hash=j.input_hash",params![current_owner,job,selection.workspace_id],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?;
    if manifest!=Some((selection_hash,original.assignment_sha256.clone(),tool,serde_json::to_string(&ids)?)) {return Err(unavailable())}
    let reserve_raw=serde_json::to_vec(reserve)?;if reserve_raw.len()>262144 {return Err(unavailable())}budget.check(reserve_raw.len()).map_err(RepositoryError::Validation)?;let reserve_hash=sha256_hex(&reserve_raw);
    let reserve_id=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,&job,&reserve_hash))?));
    let (_,value)=original_fact(tx,current_owner,&reserve_id,"s08.model-reserve.v1","s08_model_reserve","s08_model",None,0,budget)?.ok_or_else(unavailable)?;
    let foreground=value["foreground_epoch"].as_i64().filter(|n|*n>=0).ok_or_else(unavailable)?;
    if value!=json!({"version":"s08.model-reserve.v1","request_sha256":reserve_hash,"owner":selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt.attempt_id,"lease_epoch":attempt.lease_epoch,"foreground_epoch":foreground}) {return Err(unavailable())}
    let states:Option<(String,String,String)>=tx.query_row("SELECT j.state,a.state,q.state FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND a.attempt_id=?3 AND a.lease_epoch=?4 AND q.foreground_epoch=?5 AND q.connection_id=?6 AND q.reserved_seconds=?10 AND q.coverage_generation=?11 AND q.clock_generation=?12 AND a.lease_owner=?13 AND ((?9='rules' AND q.connection_id='local:rules' AND q.reserved_calls=0 AND q.charged_calls=0 AND q.reserved_input_tokens=0 AND q.reserved_output_tokens=0 AND q.charged_input_tokens=0 AND q.charged_output_tokens=0) OR (?9 IN ('model','hybrid') AND q.connection_id!='local:rules' AND q.reserved_calls=1 AND q.charged_calls=1 AND q.reserved_input_tokens=?7 AND q.reserved_output_tokens=?8)) AND q.settled_at_ms IS NOT NULL",params![current_owner,job,attempt.attempt_id,attempt.lease_epoch,foreground,original.connection_id,original.max_input_tokens,original.max_output_tokens,original.mode,reserve.reservation.job_seconds,reserve.reservation.coverage_generation,reserve.reservation.clock_generation,reserve.reservation.worker_id],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?))).optional()?;
    let request_raw=serde_json::to_vec(r)?;if request_raw.len()>32768 {return Err(unavailable())}budget.check(request_raw.len()).map_err(RepositoryError::Validation)?;let request_hash=sha256_hex(&request_raw);
    let result_id=format!("cs_s08policyresult_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-result.v1",account,&job,&attempt.attempt_id,attempt.lease_epoch))?));
    let Some((_,result))=original_fact(tx,current_owner,&result_id,"s08.policy-result.v1","s08_policy_result","s08_policy",Some(&attempt.attempt_id),0,budget)? else {return Ok(None)};
    if states!=Some(("succeeded".into(),"succeeded".into(),"settled".into())) {return Err(unavailable())}
    let state=result["state"].as_str().filter(|v|matches!(*v,"staged"|"no_proposal")).ok_or_else(unavailable)?;
    let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,&job))?));
    if result.as_object().map(|v|v.len())!=Some(11) || result["reserve_receipt_id"]!=reserve_id || result["version"]!="s08.policy-result.v1" || result["request_sha256"]!=request_hash || result["owner"]!=selection.owner || result["account_id"]!=account || result["job_id"]!=job || result["attempt_id"]!=attempt.attempt_id || result["lease_epoch"]!=attempt.lease_epoch || result["output_kind"]!=original.output_kind || (state=="staged" && result["candidate_id"].as_str()!=Some(candidate.as_str())) || (state=="no_proposal" && !result["candidate_id"].is_null()) {return Err(unavailable())}
    let size:Option<i64>=tx.query_row("SELECT length(CAST(result_json AS BLOB)) FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![current_owner,job],|x|x.get(0)).optional()?;let size=size.ok_or_else(unavailable)?;if !(0..=8192).contains(&size) {return Err(unavailable())}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT result_json FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![current_owner,job],|x|x.get(0))?;let outcome=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let (_,importance)=s08_effective_importance(original,&r.proposal,state=="no_proposal")?;
    if outcome.as_object().map(|v|v.len())!=Some(8) || outcome["reserve_receipt_id"]!=reserve_id || outcome["version"]!="s08.policy-outcome.v1" || outcome["request_sha256"]!=request_hash || outcome["candidate_id"]!=result["candidate_id"] || outcome["attempt_id"]!=attempt.attempt_id || outcome["lease_epoch"]!=attempt.lease_epoch || outcome["importance"]!=importance || (state=="no_proposal" && !outcome["content_sha256"].is_null()) || (state=="staged" && outcome["content_sha256"].as_str().is_none_or(|v|!valid_sha256(v))) {return Err(unavailable())}
    Ok(Some(json!({"version":1,"state":state,"candidate_id":result["candidate_id"]})))
}


/// Historical settled proposal facts; caller must separately establish fresh
/// source, policy, endpoint and human-review authority before any mutation.
pub(crate) struct SettledPolicyCandidate {
    pub(crate) intent:S08ModelPrepareRequest,
    pub(crate) job_id:String,
    pub(crate) attempt_id:String,
    pub(crate) lease_epoch:i64,
    pub(crate) content:String,
    pub(crate) importance:Value,
    pub(crate) outcome_sha256:String,
    pub(crate) selection_sha256:String,
    pub(crate) intent_sha256:String,
    pub(crate) reserve_receipt_id:String,
    pub(crate) reserve_receipt_sha256:String,
    pub(crate) result_receipt_id:String,
    pub(crate) result_receipt_sha256:String,
    pub(crate) request_sha256:String,
}

/// Content-free settled job proof; no source rehydration or review grant.
pub(crate) struct SettledPolicyOutcome {
    pub(crate) state:String,
    pub(crate) intent:S08ModelPrepareRequest, pub(crate) job_id:String, pub(crate) attempt_id:String,
    pub(crate) lease_epoch:i64, pub(crate) foreground_epoch:i64, pub(crate) candidate_id:Option<String>, pub(crate) content_sha256:Option<String>,
    pub(crate) importance:Value, pub(crate) selection_sha256:String, pub(crate) intent_sha256:String,
    pub(crate) reserve_receipt_id:String, pub(crate) reserve_receipt_sha256:String,
    pub(crate) result_receipt_id:String, pub(crate) result_receipt_sha256:String, pub(crate) request_sha256:String,
}

fn settled_known_terminal(tx:&Connection,owner:&str,account:&str,workspace:&str,job:&str,attempt:&str,intent:S08ModelPrepareRequest,selection_raw:&str,intent_raw:&str,budget:&mut ResetFenceBudget)->Result<SettledPolicyOutcome,RepositoryError> {
    let (lease,job_state,attempt_state,raw):(i64,String,String,String)=tx.query_row("SELECT a.lease_epoch,j.state,a.state,j.result_json FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id WHERE j.owner_id=?1 AND j.job_id=?2 AND a.attempt_id=?3 AND length(CAST(j.result_json AS BLOB))<=8192",params![owner,job,attempt],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?.ok_or_else(unavailable)?;
    if lease<=0 || !matches!(intent.mode.as_str(),"model"|"hybrid"|"rules") {return Err(unavailable())}budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let out=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let disposition=out["disposition"].as_str().filter(|x|matches!(*x,"positively_unsent"|"failed"|"cancelled")).ok_or_else(unavailable)?;
    let reason=if out["reason"].is_null() {None} else {Some(out["reason"].as_str().filter(|v|*v=="invalid_proposal" && disposition=="failed").ok_or_else(unavailable)?)};
    let prior=out["prior_reservation_state"].as_str().filter(|v|matches!(*v,"reserved"|"dispatched")).ok_or_else(unavailable)?;
    if prior=="reserved" && disposition!="positively_unsent" {return Err(unavailable())}
    let label=out["usage"].as_str().ok_or_else(unavailable)?;let usage:Option<S08PolicyUsage>=serde_json::from_value(out["reported_usage"].clone())?;
    if (intent.mode=="rules" && (disposition!="cancelled" || prior!="dispatched" || usage.is_some())) || (disposition=="positively_unsent" && (label!="known_zero" || usage.is_some())) || (disposition!="positively_unsent" && label!=if usage.is_some(){"reported"}else{"reserved_bound"}) || job_state!=if disposition=="cancelled"{"cancelled"}else{"failed_terminal"} || attempt_state!=job_state {return Err(unavailable())}
    let request_hash=out["request_sha256"].as_str().filter(|x|valid_sha256(x)).ok_or_else(unavailable)?;
    let reserve_id=out["reserve_receipt_id"].as_str().filter(|x|x.starts_with("cs_s08modelreserve_") && x.len()<=128).ok_or_else(unavailable)?;
    if out!=json!({"version":"s08.policy-terminal-outcome.v1","request_sha256":request_hash,"disposition":disposition,"reason":reason,"usage":label,"reported_usage":usage,"prior_reservation_state":prior,"attempt_id":attempt,"lease_epoch":lease,"reserve_receipt_id":reserve_id}) {return Err(unavailable())}
    let result_id=format!("cs_s08policyterminal_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-terminal.v1",account,job,attempt,lease))?));
    let (result_raw,result)=original_fact(tx,owner,&result_id,"s08.policy-terminal.v1","s08_policy_terminal","s08_policy",Some(attempt),0,budget)?.ok_or_else(unavailable)?;
    if result!=json!({"version":"s08.policy-terminal.v1","request_sha256":request_hash,"owner":intent.selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":lease,"disposition":disposition,"reason":reason,"usage":label,"reported_usage":usage,"prior_reservation_state":prior,"reserve_receipt_id":reserve_id}) {return Err(unavailable())}
    let (reserve_raw,reserve)=original_fact(tx,owner,reserve_id,"s08.model-reserve.v1","s08_model_reserve","s08_model",None,0,budget)?.ok_or_else(unavailable)?;
    let rh=reserve["request_sha256"].as_str().filter(|x|valid_sha256(x)).ok_or_else(unavailable)?;let foreground=reserve["foreground_epoch"].as_i64().filter(|n|*n>=0).ok_or_else(unavailable)?;
    if reserve_id!=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,job,rh))?)) || reserve!=json!({"version":"s08.model-reserve.v1","request_sha256":rh,"owner":intent.selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":lease,"foreground_epoch":foreground}) {return Err(unavailable())}
    let (reserved,charged_seconds,charged_calls,charged_in,charged_out):(i64,i64,i64,i64,i64)=tx.query_row("SELECT reserved_seconds,charged_seconds,charged_calls,charged_input_tokens,charged_output_tokens FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND foreground_epoch=?5 AND connection_id=?6 AND state='settled' AND settled_at_ms IS NOT NULL AND reserved_calls=?9 AND reserved_input_tokens=?7 AND reserved_output_tokens=?8",params![owner,job,attempt,lease,foreground,intent.connection_id,intent.max_input_tokens,intent.max_output_tokens,if intent.mode=="rules" {0} else {1}],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?))).optional()?.ok_or_else(unavailable)?;
    let expected=if disposition=="positively_unsent" {(0,0,0,0)} else if intent.mode=="rules" {(0,reserved,0,0)} else if let Some(u)=usage {if i64::from(u.elapsed_seconds)>reserved || u.input_tokens>intent.max_input_tokens || u.output_tokens>intent.max_output_tokens {return Err(unavailable())}(1,i64::from(u.elapsed_seconds),i64::from(u.input_tokens),i64::from(u.output_tokens))} else {(1,reserved,i64::from(intent.max_input_tokens),i64::from(intent.max_output_tokens))};
    if (charged_calls,charged_seconds,charged_in,charged_out)!=expected {return Err(unavailable())}
    let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,job))?));
    let residue:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidate_evidence WHERE candidate_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidate_evidence ce JOIN fm_v2_evidence e ON e.owner_id=ce.owner_id AND e.evidence_id=ce.evidence_id WHERE e.owner_id=?2 AND e.locator_type IN ('s08_admitted_source','s08_journal_source') AND json_valid(e.locator_json) AND json_extract(e.locator_json,'$.job_id')=?3) OR EXISTS(SELECT 1 FROM fm_v2_revision_evidence re JOIN fm_v2_evidence e ON e.owner_id=re.owner_id AND e.evidence_id=re.evidence_id WHERE e.owner_id=?2 AND e.locator_type IN ('s08_admitted_source','s08_journal_source') AND json_valid(e.locator_json) AND json_extract(e.locator_json,'$.job_id')=?3)",params![candidate,owner,job],|x|x.get(0))?;if residue {return Err(unavailable())}
    Ok(SettledPolicyOutcome{intent,job_id:job.into(),attempt_id:attempt.into(),lease_epoch:lease,foreground_epoch:foreground,candidate_id:None,content_sha256:None,importance:Value::Null,selection_sha256:sha256_hex(selection_raw.as_bytes()),intent_sha256:sha256_hex(intent_raw.as_bytes()),reserve_receipt_id:reserve_id.into(),reserve_receipt_sha256:sha256_hex(reserve_raw.as_bytes()),result_receipt_id:result_id,result_receipt_sha256:sha256_hex(result_raw.as_bytes()),request_sha256:request_hash.into(),state:disposition.into()})
}

fn settled_rules_cancelled(tx:&Connection,owner:&str,account:&str,job:&str,attempt:&str,intent:S08ModelPrepareRequest,selection_raw:&str,intent_raw:&str,budget:&mut ResetFenceBudget)->Result<SettledPolicyOutcome,RepositoryError> {
    if intent.mode!="rules" {return Err(unavailable())}
    let epoch:i64=tx.query_row("SELECT a.lease_epoch FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND j.state='cancelled' AND j.current_attempt_id IS NULL AND j.result_json IS NULL AND a.attempt_id=?3 AND a.state='fenced' AND q.state='revoked' AND q.connection_id='local:rules' AND q.reserved_calls=0 AND q.charged_calls=0 AND q.reserved_input_tokens=0 AND q.reserved_output_tokens=0 AND q.charged_input_tokens=0 AND q.charged_output_tokens=0",params![owner,job,attempt],|x|x.get(0)).optional()?.ok_or_else(unavailable)?;if epoch<=0 {return Err(unavailable())}
    let id=format!("cs_s08policyterminal_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-terminal.v1",account,job,attempt,epoch))?));
    let (raw,receipt)=original_fact(tx,owner,&id,"s08.policy-terminal.v1","s08_policy_terminal","s08_policy",Some(attempt),0,budget)?.ok_or_else(unavailable)?;
    let request_hash=receipt["request_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;let rid=receipt["reserve_receipt_id"].as_str().filter(|v|v.starts_with("cs_s08modelreserve_") && v.len()<=128).ok_or_else(unavailable)?;
    if receipt!=json!({"version":"s08.policy-terminal.v1","request_sha256":request_hash,"owner":intent.selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":epoch,"disposition":"cancelled","reason":null,"usage":"known_zero","reported_usage":null,"prior_reservation_state":"reserved","reserve_receipt_id":rid}) {return Err(unavailable())}
    let (reserve_raw,reserve)=original_fact(tx,owner,rid,"s08.model-reserve.v1","s08_model_reserve","s08_model",None,0,budget)?.ok_or_else(unavailable)?;
    let rh=reserve["request_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;let foreground=reserve["foreground_epoch"].as_i64().filter(|v|*v>=0).ok_or_else(unavailable)?;
    if rid!=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,job,rh))?)) || reserve!=json!({"version":"s08.model-reserve.v1","request_sha256":rh,"owner":intent.selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":epoch,"foreground_epoch":foreground}) {return Err(unavailable())}
    let scoped:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND foreground_epoch=?5 AND state='revoked' AND connection_id='local:rules')",params![owner,job,attempt,epoch,foreground],|x|x.get(0))?;if !scoped {return Err(unavailable())}
    let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,job))?));
    let residue:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidate_evidence ce JOIN fm_v2_evidence e ON e.owner_id=ce.owner_id AND e.evidence_id=ce.evidence_id WHERE e.owner_id=?2 AND e.locator_type IN ('s08_admitted_source','s08_journal_source') AND json_valid(e.locator_json) AND json_extract(e.locator_json,'$.job_id')=?3) OR EXISTS(SELECT 1 FROM fm_v2_revision_evidence re JOIN fm_v2_evidence e ON e.owner_id=re.owner_id AND e.evidence_id=re.evidence_id WHERE e.owner_id=?2 AND e.locator_type IN ('s08_admitted_source','s08_journal_source') AND json_valid(e.locator_json) AND json_extract(e.locator_json,'$.job_id')=?3)",params![candidate,owner,job],|x|x.get(0))?;if residue {return Err(unavailable())}
    Ok(SettledPolicyOutcome{state:"cancelled".into(),intent,job_id:job.into(),attempt_id:attempt.into(),lease_epoch:epoch,foreground_epoch:foreground,candidate_id:None,content_sha256:None,importance:Value::Null,selection_sha256:sha256_hex(selection_raw.as_bytes()),intent_sha256:sha256_hex(intent_raw.as_bytes()),reserve_receipt_id:rid.into(),reserve_receipt_sha256:sha256_hex(reserve_raw.as_bytes()),result_receipt_id:id,result_receipt_sha256:sha256_hex(raw.as_bytes()),request_sha256:request_hash.into()})
}

pub(crate) fn settled_outcome(tx:&Transaction<'_>,current_owner:&str,account:&str,workspace:&str,job:&str,attempt:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<SettledPolicyOutcome,RepositoryError> {
    if view.current_owner()!=current_owner || view.account_id()!=account {return Err(unavailable())}
    let mut captured=|name:&str,_budget:&mut ResetFenceBudget|view.for_captured(tx,name).map(|_|());
    settled_outcome_read(tx,current_owner,account,workspace,job,attempt,&mut captured,budget)
}
/// Shared factual verifier. The caller supplies a same-snapshot captured-owner
/// check; no read-side proof grants mutation or source authority.
fn settled_outcome_read(tx:&Connection,current_owner:&str,account:&str,workspace:&str,job:&str,attempt:&str,captured:&mut dyn FnMut(&str,&mut ResetFenceBudget)->Result<(),RepositoryError>,budget:&mut ResetFenceBudget)->Result<SettledPolicyOutcome,RepositoryError> {
    if tx.is_autocommit() || job.is_empty() || attempt.is_empty() {return Err(unavailable())}
    let intent_id=format!("cs_s08model_{}",sha256_hex(&serde_json::to_vec(&(account,job))?));
    let (raw,_)=original_fact(tx,current_owner,&intent_id,"s08.model-intent.v1","s08_model_intent","s08_model",None,0,budget)?.ok_or_else(unavailable)?;
    let intent:S08ModelPrepareRequest=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    let selection=&intent.selection;captured(&selection.owner,budget)?;
    if serde_json::to_string(&intent)?!=raw || intent.version!=1 || selection.source_account_id!=account || selection.workspace_id!=workspace || selection.project_id.is_some() || !s08_policy_accounting_valid(&intent) || !valid_sha256(&intent.assignment_sha256) || !valid_sha256(&intent.snapshots_sha256) || !s08_importance_valid(&intent.mode,&intent.importance) || !s08_connection_predicate_valid(&intent.output_kind,intent.connection_predicate.as_ref()) || job!=format!("s08job_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-job.v1",&selection.owner,&selection.idempotency_key,&intent.output_kind))?)) {return Err(unavailable())}
    // A journal selection is exactly the `journal_synthesis` kind and nothing else carries one; its closed shape replaces the
    // admission/candidate bounds, which stay untouched (and unreachable only for that kind).
    if selection.journal.is_some()!=(intent.output_kind==s08_journal::OUTPUT_KIND) {return Err(unavailable())}
    if selection.importance_target.is_some()!=(intent.output_kind=="importance_target") || serde_json::to_value(&selection.importance_target)?!=serde_json::to_value(&intent.importance_target)? {return Err(unavailable())}
    if selection.version!=1 || (if let Some(j)=&selection.journal {!(j.validate(selection).is_ok() && j.sourced().all(|r|r.source.as_ref().is_some_and(|s|s.account_id==account && s.validate().is_ok())))} else {!(3..=64).contains(&selection.admissions.len()) || (if intent.output_kind=="importance_target" {selection.candidates.len()!=1} else {!(2..=10).contains(&selection.candidates.len())}) || selection.admissions.iter().any(|v|v.source.account_id!=account || v.source.validate().is_err()) || selection.candidates.iter().any(|v|v.owner!=selection.owner || v.workspace_id!=workspace || v.project_id.is_some())}) {return Err(unavailable())}
    let selection_raw=serde_json::to_string(selection)?;if selection_raw.len()>262144 {return Err(unavailable())}budget.check(selection_raw.len()).map_err(RepositoryError::Validation)?;
    let input=sha256_hex(selection_raw.as_bytes());let selection_id=format!("cs_s08selection_{}",sha256_hex(&serde_json::to_vec(&(&selection.owner,job))?));
    if original_fact(tx,current_owner,&selection_id,"s08.selection.v1","s08_selection","s08_rules",None,1,budget)?.is_none_or(|(raw,_)|raw!=selection_raw) {return Err(unavailable())}
    let mut ids=std::collections::BTreeSet::new();for item in &selection.admissions {if !ids.insert(item.event_id.clone()) {return Err(unavailable())}}for item in &selection.candidates {ids.insert(format!("s08revision_{}",sha256_hex(&serde_json::to_vec(item)?)));}
    if let Some(j)=&selection.journal {ids=j.selected_ids()?;}
    let tool=sha256_hex(format!("s08-policy-{}-{}-v1",intent.mode,intent.output_kind).as_bytes());
    let manifest:Option<(String,String,String,String)>=tx.query_row("SELECT j.input_hash,m.model_hash,m.tool_hash,m.selected_json FROM fm_v2_jobs j JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind='access_consolidation' AND j.workspace_key=?3 AND j.project_key='' AND m.phase='selection' AND m.input_hash=j.input_hash AND m.content_hash=j.input_hash",params![current_owner,job,workspace],|x|Ok((s08_bounded_first(x,0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?;
    if manifest!=Some((input.clone(),intent.assignment_sha256.clone(),tool,serde_json::to_string(&ids)?)) {return Err(unavailable())}
    V2Repository::verify_s08_evidence(tx,current_owner,selection,job,&input)?;
    let terminal:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND state IN ('failed_terminal','cancelled'))",params![current_owner,job],|x|s08_bounded_first(x,0))?;
    if terminal {
        if intent.mode=="rules" {
            let no_result:bool=tx.query_row("SELECT result_json IS NULL FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![current_owner,job],|x|s08_bounded_first(x,0))?;
            if no_result {return settled_rules_cancelled(tx,current_owner,account,job,attempt,intent,&selection_raw,&raw,budget)}
        }
        return settled_known_terminal(tx,current_owner,account,workspace,job,attempt,intent,&selection_raw,&raw,budget)
    }
    let (lease,raw_outcome):(i64,String)=tx.query_row("SELECT a.lease_epoch,j.result_json FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND a.attempt_id=?3 AND j.state='succeeded' AND a.state='succeeded' AND q.state='settled' AND q.settled_at_ms IS NOT NULL AND length(CAST(j.result_json AS BLOB))<=8192",params![current_owner,job,attempt],|x|Ok((s08_bounded_first(x,0)?,x.get(1)?))).optional()?.ok_or_else(unavailable)?;
    if lease<=0 {return Err(unavailable())}budget.check(raw_outcome.len()).map_err(RepositoryError::Validation)?;
    let outcome=crate::field_authority::parse_unique_json(&raw_outcome).map_err(RepositoryError::Validation)?;
    let request_hash=outcome["request_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
    let reserve_id=outcome["reserve_receipt_id"].as_str().filter(|v|v.starts_with("cs_s08modelreserve_") && v.len()<=128).ok_or_else(unavailable)?;
    if outcome.as_object().map(|v|v.len())!=Some(8) || outcome["version"]!="s08.policy-outcome.v1" || outcome["attempt_id"]!=attempt || outcome["lease_epoch"]!=lease {return Err(unavailable())}
    let candidate_id=outcome["candidate_id"].as_str().map(str::to_owned);
    if let Some(id)=candidate_id.as_deref() {
        if id!=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,job))?)) || outcome["content_sha256"].as_str().is_none_or(|v|!valid_sha256(v)) {return Err(unavailable())}
    } else if !outcome["candidate_id"].is_null() || !outcome["content_sha256"].is_null() {return Err(unavailable())}
    let result_id=format!("cs_s08policyresult_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-result.v1",account,job,attempt,lease))?));
    let (result_raw,result)=original_fact(tx,current_owner,&result_id,"s08.policy-result.v1","s08_policy_result","s08_policy",Some(attempt),0,budget)?.ok_or_else(unavailable)?;
    let state=if candidate_id.is_some() {"staged"} else {"no_proposal"};
    if result!=json!({"version":"s08.policy-result.v1","reserve_receipt_id":reserve_id,"request_sha256":request_hash,"owner":selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":lease,"output_kind":intent.output_kind,"state":state,"candidate_id":candidate_id}) {return Err(unavailable())}
    let (reserve_raw,reserve)=original_fact(tx,current_owner,reserve_id,"s08.model-reserve.v1","s08_model_reserve","s08_model",None,0,budget)?.ok_or_else(unavailable)?;
    let reserve_hash=reserve["request_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
    let foreground=reserve["foreground_epoch"].as_i64().filter(|v|*v>=0).ok_or_else(unavailable)?;
    let expected_reserve=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,job,reserve_hash))?));
    if reserve_id!=expected_reserve || reserve!=json!({"version":"s08.model-reserve.v1","request_sha256":reserve_hash,"owner":selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":lease,"foreground_epoch":foreground}) {return Err(unavailable())}
    let exact:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND foreground_epoch=?5 AND connection_id=?6 AND state='settled' AND ((?9='rules' AND reserved_calls=0 AND charged_calls=0 AND reserved_input_tokens=0 AND reserved_output_tokens=0 AND charged_input_tokens=0 AND charged_output_tokens=0) OR (?9 IN ('model','hybrid') AND reserved_calls=1 AND charged_calls=1 AND reserved_input_tokens=?7 AND reserved_output_tokens=?8)))",params![current_owner,job,attempt,lease,foreground,intent.connection_id,intent.max_input_tokens,intent.max_output_tokens,intent.mode],|x|s08_bounded_first(x,0))?;if !exact {return Err(unavailable())}
    let importance=outcome["importance"].clone();
    if candidate_id.is_none() {
        if importance!=json!({"state":"skipped","reason":"no_candidate"}) && !(intent.output_kind=="importance_target" && importance==json!({"state":"skipped","reason":"unchanged"})) {return Err(unavailable())}
        let expected=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,job))?));
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidate_evidence WHERE candidate_id=?1)",[expected],|x|s08_bounded_first(x,0))?;
        if orphan {return Err(unavailable())}
        // A no-proposal job may not discard source evidence retained by any
        // candidate or knowledge revision. There is no candidate to exempt.
        let shared:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_candidate_evidence ce JOIN fm_v2_evidence e ON e.owner_id=ce.owner_id AND e.evidence_id=ce.evidence_id WHERE e.owner_id=?1 AND e.locator_type IN ('s08_admitted_source','s08_journal_source') AND json_valid(e.locator_json) AND json_extract(e.locator_json,'$.job_id')=?2) OR EXISTS(SELECT 1 FROM fm_v2_revision_evidence re JOIN fm_v2_evidence e ON e.owner_id=re.owner_id AND e.evidence_id=re.evidence_id WHERE e.owner_id=?1 AND e.locator_type IN ('s08_admitted_source','s08_journal_source') AND json_valid(e.locator_json) AND json_extract(e.locator_json,'$.job_id')=?2)",params![current_owner,job],|x|s08_bounded_first(x,0))?;
        if shared {return Err(unavailable())}
    }
    let content_sha256=outcome["content_sha256"].as_str().map(str::to_owned);
    let selection_sha256=sha256_hex(selection_raw.as_bytes());let intent_sha256=sha256_hex(raw.as_bytes());
    let reserve_receipt_id=reserve_id.to_owned();let reserve_receipt_sha256=sha256_hex(reserve_raw.as_bytes());
    let result_receipt_sha256=sha256_hex(result_raw.as_bytes());let request_sha256=request_hash.to_owned();
    Ok(SettledPolicyOutcome{state:state.into(),intent,job_id:job.into(),attempt_id:attempt.into(),lease_epoch:lease,foreground_epoch:foreground,candidate_id,content_sha256,importance,selection_sha256,intent_sha256,reserve_receipt_id,reserve_receipt_sha256,result_receipt_id:result_id,result_receipt_sha256,request_sha256})
}

fn settled_candidate_inner(review_state:&str,tx:&Transaction<'_>,current_owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<SettledPolicyCandidate,RepositoryError> {
    if view.current_owner()!=current_owner || view.account_id()!=account {return Err(unavailable())}
    let mut captured=|name:&str,_budget:&mut ResetFenceBudget|view.for_captured(tx,name).map(|_|());
    settled_candidate_read(review_state,tx,current_owner,account,workspace,candidate,&mut captured,budget)
}
fn settled_candidate_read(review_state:&str,tx:&Connection,current_owner:&str,account:&str,workspace:&str,candidate:&str,captured:&mut dyn FnMut(&str,&mut ResetFenceBudget)->Result<(),RepositoryError>,budget:&mut ResetFenceBudget)->Result<SettledPolicyCandidate,RepositoryError> {
    if tx.is_autocommit() || candidate.is_empty() || candidate.len()>512 {return Err(unavailable())}
    let size:Option<i64>=tx.query_row("SELECT length(CAST(content AS BLOB)) FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![current_owner,workspace,candidate],|x|s08_bounded_first(x,0)).optional()?;let size=size.ok_or_else(unavailable)?;if !(1..=65536).contains(&size) {return Err(unavailable())}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let row:Option<(String,String,String,String,String,f64)>=tx.query_row("SELECT content,turn_id,source_event_id,content_hash,status,importance_score FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source_uri='s08://'||turn_id AND source_revision=1 AND source='auto_extracted' AND evidence_role='derived'",params![current_owner,workspace,candidate],|x|Ok((s08_bounded_first(x,0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?,x.get(5)?))).optional()?;
    let (content,job,attempt,privacy_hash,status,score)=row.ok_or_else(unavailable)?;
    if status!=review_state || !score.is_finite() || !(0.0..=1.0).contains(&score) || privacy_hash!=crate::privacy::content_hash(&content) || candidate!=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,&job))?)) {return Err(unavailable())}
    let outcome=settled_outcome_read(tx,current_owner,account,workspace,&job,&attempt,captured,budget)?;
    if outcome.candidate_id.as_deref()!=Some(candidate) || outcome.content_sha256.as_deref()!=Some(sha256_hex(content.as_bytes()).as_str()) {return Err(unavailable())}
    let intent=&outcome.intent;let selection=&intent.selection;let lease=outcome.lease_epoch;
    let actor=match intent.output_kind.as_str() {"summary"=>"s08_policy","importance_target"=>"s08_importance_target",s08_journal::OUTPUT_KIND=>s08_journal::ACTOR,_=>"s08_connection"};
    let canonical:Option<(String,String,String,String,Option<String>)>=tx.query_row("SELECT r.proposal_json,r.state,r.actor_type,r.actor_id,r.source_event_id FROM fm_v2_candidates c JOIN fm_v2_candidate_revisions r ON r.owner_id=c.owner_id AND r.candidate_id=c.candidate_id AND r.revision=3 AND r.change_set_id=c.created_change_set_id WHERE c.owner_id=?1 AND c.workspace_key=?2 AND c.project_key='' AND c.candidate_id=?3 AND c.current_revision=?4",params![current_owner,workspace,candidate,if review_state=="pending" {3} else {4}],|x|Ok((s08_bounded_first(x,0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?))).optional()?;
    let (proposal,state,actor_type,actor_id,source)=canonical.ok_or_else(unavailable)?;
    if proposal!=content || state!="awaiting_review" || actor_type!="automation" || actor_id!=actor || source.as_deref()!=Some(attempt.as_str()) {return Err(unavailable())}
    let importance=outcome.importance.clone();
    let expected_score=match &intent.importance {
        S08ImportanceDecision::Skipped{reason} if importance==json!({"state":"skipped","reason":reason})=>0.0,
        S08ImportanceDecision::Rules{value,provenance_sha256} if importance==json!({"state":"applied","mode":"rules","value":value,"provenance_sha256":provenance_sha256})=>*value,
        S08ImportanceDecision::Managed{..} if importance==json!({"state":"skipped","reason":"score_missing"})=>0.0,
        S08ImportanceDecision::Managed{mode,provenance_sha256}=>{
            let n=importance["value"].as_f64().filter(|v|v.is_finite() && (0.0..=1.0).contains(v)).ok_or_else(unavailable)?;
            if importance!=json!({"state":"applied","mode":mode,"value":n,"provenance_sha256":provenance_sha256}) {return Err(unavailable())}n
        },
        _=>return Err(unavailable()),
    };
    if score!=expected_score {return Err(unavailable())}
    let mut evidence=Vec::new();for item in &selection.admissions {evidence.push(format!("s08evidence_{}",sha256_hex(&serde_json::to_vec(&(&selection.owner,&job,&item.event_id))?)));}
    if let Some(j)=&selection.journal {evidence=j.expected_evidence_ids(&selection.owner,&job)?;}
    evidence.sort();evidence.dedup();
    let listed:String=tx.query_row("SELECT raw_evidence_ids FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![current_owner,workspace,candidate],|x|s08_bounded_first(x,0))?;
    let listed:Vec<String>=serde_json::from_value(crate::field_authority::parse_unique_json(&listed).map_err(RepositoryError::Validation)?)?;
    let linked={let mut stmt=tx.prepare("SELECT evidence_id FROM fm_v2_candidate_evidence WHERE owner_id=?1 AND candidate_id=?2 AND revision=3 ORDER BY evidence_id LIMIT 65")?;let rows=stmt.query_map(params![current_owner,candidate],|x|s08_bounded_first::<String>(x,0))?.collect::<Result<Vec<_>,_>>()?;rows};
    if evidence.is_empty() || evidence.len()>64 || listed!=evidence || linked!=evidence {return Err(unavailable())}
    let change:String=tx.query_row("SELECT created_change_set_id FROM fm_v2_candidates WHERE owner_id=?1 AND candidate_id=?2",params![current_owner,candidate],|x|s08_bounded_first(x,0))?;
    let first=read_candidate_inner(tx,current_owner,candidate,candidate,1,workspace,"",false)?;
    let third=read_candidate_inner(tx,current_owner,candidate,candidate,3,workspace,"",false)?;
    let mut expected=first.payload.clone();expected.state=CandidateState::AwaitingReview;
    let reason=match intent.output_kind.as_str() {"summary"=>"unreviewed inferred summary","importance_target"=>"unreviewed target importance",s08_journal::OUTPUT_KIND=>s08_journal::REASON,_=>"unreviewed connection proposal"};
    if first.payload.state!=CandidateState::Proposed || third.payload!=expected || first.change_set_id!=change || third.change_set_id!=change || first.actor_type!="automation" || first.actor_id!=actor || first.source_event_id.as_deref()!=Some(attempt.as_str()) || first.payload.reason!=reason || first.content_hash!=hash_value(&serde_json::to_value(&first.payload)?)? || third.content_hash!=hash_value(&serde_json::to_value(&third.payload)?)? {return Err(unavailable())}
    let create:Option<(String,String,String,Option<String>,String,String)>=tx.query_row("SELECT actor_type,actor_id,reason,source_event_id,contract_version,content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![current_owner,change],|x|Ok((s08_bounded_first(x,0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?,x.get(5)?))).optional()?;
    if create!=Some(("automation".into(),actor.into(),reason.into(),Some(attempt.clone()),CONTRACT_VERSION.into(),first.content_hash.clone())) {return Err(unavailable())}
    let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_revision'",params![current_owner,format!("outbox_{change}_candidate_revision"),change],|x|Ok((s08_bounded_first(x,0)?,x.get(1)?))).optional()?;
    if out.as_ref().is_none_or(|(raw,rev)|*rev!=3 || crate::field_authority::parse_unique_json(raw).ok()!=Some(json!({"candidate_id":candidate,"revision":3}))) {return Err(unavailable())}
    let proposal=crate::field_authority::parse_unique_json(&content).map_err(RepositoryError::Validation)?;
    if intent.output_kind=="summary" {
        if proposal.as_object().map(|v|v.len())!=Some(6) || proposal["version"]!="s08.policy-summary.v1" || proposal["classification"]!="inferred_untrusted" || proposal["semantic_claim"]!="none" || proposal["importance"]!=importance || proposal["summary"].as_str().is_none_or(|v|v.trim().is_empty()) {return Err(unavailable())}
        let blocks=proposal["source_block_ids"].as_array().ok_or_else(unavailable)?;let mut unique=std::collections::BTreeSet::new();
        if blocks.len()<2 || blocks.len()>selection.candidates.len() {return Err(unavailable())}
        for block in blocks {let id=block.as_str().ok_or_else(unavailable)?;if !unique.insert(id) || !selection.candidates.iter().any(|v|v.block_id==id) {return Err(unavailable())}}
    } else if intent.output_kind=="importance_target" {
        let expected=super::s08_importance_target_result(intent,if intent.mode=="rules" {&Value::Null} else {&proposal},false)?.ok_or_else(unavailable)?;
        if proposal!=expected {return Err(unavailable())}
    } else if intent.output_kind==s08_journal::OUTPUT_KIND {
        if !selection.journal.as_ref().is_some_and(|j|j.proposal_ok(&proposal)) {return Err(unavailable())}
    } else if intent.output_kind!="connection" {return Err(unavailable())}
    // `settled_outcome` verified these exact bytes and the attempt/reservation
    // lineage on this held transaction; hash the bounded persisted outcome,
    // rather than treating the separate result receipt as a score commitment.
    let outcome_raw:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(result_json AS BLOB))<=8192 THEN result_json END FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND current_attempt_id=?3 AND state='succeeded'",params![current_owner,job,attempt],|r|s08_bounded_first(r,0)).optional()?.flatten();
    let outcome_raw=outcome_raw.ok_or_else(unavailable)?;budget.check(outcome_raw.len()).map_err(RepositoryError::Validation)?;
    let outcome_sha256=sha256_hex(outcome_raw.as_bytes());
    Ok(SettledPolicyCandidate{intent:outcome.intent,job_id:job,attempt_id:attempt,lease_epoch:lease,content,importance,outcome_sha256,selection_sha256:outcome.selection_sha256,intent_sha256:outcome.intent_sha256,reserve_receipt_id:outcome.reserve_receipt_id,reserve_receipt_sha256:outcome.reserve_receipt_sha256,result_receipt_id:outcome.result_receipt_id,result_receipt_sha256:outcome.result_receipt_sha256,request_sha256:outcome.request_sha256})
}


/// Exact settled policy-candidate facts under the caller's existing read
/// snapshot. This proves historical custody only; live sources are separate.
pub(crate) fn settled_candidate_factual(tx:&Connection,owner:&str,account:&str,workspace:&str,candidate:&str,budget:&mut ResetFenceBudget)->Result<SettledPolicyCandidate,RepositoryError> {
    if tx.is_autocommit() {return Err(unavailable())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|s08_bounded_first(r,0)).optional()?.ok_or_else(unavailable)?;
    let status:Option<String>=tx.query_row("SELECT status FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|r|s08_bounded_first(r,0)).optional()?;
    let state=status.as_deref().filter(|s|matches!(*s,"pending"|"accepted"|"rejected")).ok_or_else(unavailable)?;
    let mut captured=|name:&str,budget:&mut ResetFenceBudget|owner_custody::captured_owner_factual(tx,&database,account,owner,name,budget);
    settled_candidate_read(state,tx,owner,account,workspace,candidate,&mut captured,budget)
}

/// Original S08 proposal provenance; accepted callers must also prove the actual S03 receipt/root.
pub(crate) fn settled_candidate_accepted(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<SettledPolicyCandidate,RepositoryError> {settled_candidate_inner("accepted",tx,owner,account,workspace,candidate,view,budget)}
pub(crate) fn settled_candidate(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<SettledPolicyCandidate,RepositoryError> {settled_candidate_inner("pending",tx,owner,account,workspace,candidate,view,budget)}
/// Original proposal provenance only; caller must independently prove rejection receipt.
pub(crate) fn settled_candidate_rejected(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<SettledPolicyCandidate,RepositoryError> {settled_candidate_inner("rejected",tx,owner,account,workspace,candidate,view,budget)}



/// Private, content-free preview/commit commitment for the existing partition.
pub(crate) fn partition_binding(account:&str,fact:&SettledPolicyOutcome)->Result<Value,RepositoryError> {
    if fact.intent.selection.source_account_id!=account {return Err(unavailable())}
    let candidate=fact.candidate_id.as_deref().map(|v|selector("s04.s08-policy-candidate.v1",account,v)).transpose()?;
    Ok(json!({"version":"s04.s08-policy-binding.v1","original_owner":fact.intent.selection.owner,
        "account_id":account,"workspace_id":fact.intent.selection.workspace_id,
        "job_sha256":selector("s04.s08-policy-job.v1",account,&fact.job_id)?,
        "attempt_sha256":selector("s04.s08-policy-attempt.v1",account,&fact.attempt_id)?,
        "lease_epoch":fact.lease_epoch,"foreground_epoch":fact.foreground_epoch,"candidate_sha256":candidate,
        "selection_sha256":fact.selection_sha256,"intent_sha256":fact.intent_sha256,
        "reserve_receipt_id":fact.reserve_receipt_id,"reserve_receipt_sha256":fact.reserve_receipt_sha256,
        "result_receipt_id":fact.result_receipt_id,"result_receipt_sha256":fact.result_receipt_sha256,
        "request_sha256":fact.request_sha256,"output_kind":fact.intent.output_kind,
        "state":fact.state}))
}


/// Transaction-local deletion locators. Never serialize these through Host.
#[derive(Debug,Clone,PartialEq,Eq)]
pub(crate) struct S08PolicyCleanupLocator {
    pub(crate) workspace_id:String,pub(crate) job_id:String,pub(crate) attempt_id:Option<String>,pub(crate) state:String,
    pub(crate) candidate_id:Option<String>,pub(crate) selection_id:String,pub(crate) intent_id:String,
    pub(crate) reserve_receipt_id:Option<String>,pub(crate) result_receipt_id:String,pub(crate) accepted_review:Option<(String,String,String)>,
    pub(crate) importance_children:Vec<super::s08_policy_review::ImportanceChildFact>,
}
pub(crate) fn cleanup_locator(account:&str,fact:&SettledPolicyOutcome)->Result<S08PolicyCleanupLocator,RepositoryError> {
    if fact.intent.selection.source_account_id!=account {return Err(unavailable())}
    Ok(S08PolicyCleanupLocator{workspace_id:fact.intent.selection.workspace_id.clone(),job_id:fact.job_id.clone(),attempt_id:Some(fact.attempt_id.clone()),state:fact.state.clone(),candidate_id:fact.candidate_id.clone(),
        selection_id:format!("cs_s08selection_{}",sha256_hex(&serde_json::to_vec(&(&fact.intent.selection.owner,&fact.job_id))?)),
        intent_id:format!("cs_s08model_{}",sha256_hex(&serde_json::to_vec(&(account,&fact.job_id))?)),
        reserve_receipt_id:Some(fact.reserve_receipt_id.clone()),result_receipt_id:fact.result_receipt_id.clone(),accepted_review:None,importance_children:Vec::new()})
}

/// A deny-only, content-free terminal bound to the existing guarded reset.
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct S08PolicyTerminal {
    pub version:String,pub owner:String,pub original_owner:String,pub account_id:String,pub key_sha256:String,
    pub workspace_id:String,pub reset_workspace_id:String,pub operation_id:String,pub mode:String,
    pub partition_sha256:Option<String>,pub reset_receipt_id:String,
    pub job_sha256:String,pub attempt_sha256:Option<String>,pub foreground_epoch:Option<i64>,pub candidate_sha256:Option<String>,pub candidate_content_sha256:Option<String>,
    pub selection_sha256:String,pub intent_sha256:String,pub reserve_receipt_id:Option<String>,
    pub reserve_receipt_sha256:Option<String>,pub result_receipt_id:String,pub result_receipt_sha256:String,
    pub request_sha256:String,pub output_kind:String,pub state:String,pub review_receipt_id:Option<String>,pub review_receipt_kind:Option<String>,pub review_receipt_sha256:Option<String>,
}
fn terminal_event(account:&str,job:&str,attempt:&str,epoch:i64)->Result<String,RepositoryError> {
    if account.is_empty() || job.is_empty() || attempt.is_empty() || epoch<=0 {return Err(unavailable())}
    Ok(format!("outbox_s04_s08_policy_erased_{}",sha256_hex(&serde_json::to_vec(&("s04.s08-policy-terminal.v1",account,job,attempt,epoch))?)))
}
fn selector(kind:&str,account:&str,id:&str)->Result<String,RepositoryError> {
    if id.is_empty() || id.len()>512 {return Err(unavailable())}
    Ok(sha256_hex(&serde_json::to_vec(&(kind,account,id))?))
}
fn verify_terminal_reset(tx:&Transaction<'_>,current_owner:&str,account:&str,m:&S08PolicyTerminal,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if view.current_owner()!=current_owner || view.account_id()!=account || m.account_id!=account {return Err(unavailable())}
    view.for_captured(tx,&m.owner)?;view.for_captured(tx,&m.original_owner)?;
    if m.output_kind=="importance_target" {
        super::s02_policy::profile(tx)?;
        let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance_target.v1'",[],|r|r.get(0)).optional()?;
        if marker.as_deref()!=Some("1") || m.review_receipt_kind.as_deref().is_some_and(|kind|kind!="s02_importance_review") {return Err(unavailable())}
    }
    let no_attempt=m.state=="prepared_cancelled";
    if no_attempt {if m.attempt_sha256.is_some() || m.foreground_epoch.is_some() || m.reserve_receipt_id.is_some() || m.reserve_receipt_sha256.is_some() || m.candidate_sha256.is_some() {return Err(unavailable())}}
    else if m.attempt_sha256.as_deref().is_none_or(|v|!valid_sha256(v)) || m.foreground_epoch.is_none_or(|v|v<0) || m.reserve_receipt_id.as_deref().is_none_or(|v|v.is_empty()) || m.reserve_receipt_sha256.as_deref().is_none_or(|v|!valid_sha256(v)) {return Err(unavailable())}
    if m.review_receipt_id.is_some()!=m.review_receipt_kind.is_some() || m.review_receipt_id.is_some()!=m.review_receipt_sha256.is_some() || m.review_receipt_sha256.as_deref().is_some_and(|v|!valid_sha256(v)) {return Err(unavailable())}
    if !valid_sha256(&m.key_sha256) || m.version!="s04.s08-policy-erased.v1" || !matches!(m.output_kind.as_str(),"summary"|"connection"|"journal_synthesis"|"importance_target") || !matches!(m.state.as_str(),"staged"|"no_proposal"|"positively_unsent"|"failed"|"cancelled"|"prepared_cancelled") || (m.state=="staged")!=m.candidate_sha256.is_some() || ![m.job_sha256.as_str(),m.selection_sha256.as_str(),m.intent_sha256.as_str(),m.result_receipt_sha256.as_str(),m.request_sha256.as_str()].iter().all(|v|valid_sha256(v)) || m.candidate_sha256.as_deref().is_some_and(|v|!valid_sha256(v)) || m.candidate_content_sha256.as_deref().is_some_and(|v|!valid_sha256(v)) || m.candidate_sha256.is_some()!=m.candidate_content_sha256.is_some() || !match (m.mode.as_str(),&m.partition_sha256) {("full",None)=>true,("selective",Some(hash))=>valid_sha256(hash),_=>false} || m.reset_receipt_id!=ingest_erased_receipt_id(account,&m.operation_id)? {return Err(unavailable())}
    let lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s06_guarded_reset' WHERE c.owner_id=?1 AND c.change_set_id=?2",params![current_owner,m.reset_receipt_id,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id)],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;let (a,b)=lengths.ok_or_else(unavailable)?;
    if !(0..=262144).contains(&a) || !(0..=262144).contains(&b) {return Err(unavailable())}budget.check((a+b) as usize).map_err(RepositoryError::Validation)?;
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![current_owner,m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (hash,raw,actor,actor_id,contract,source)=row.ok_or_else(unavailable)?;let reset=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=m.owner || contract!=CONTRACT_VERSION || source.is_some() || reset.as_object().map(|v|v.len())!=Some(4) || reset["version"]!="s06.guarded-reset.v1" || reset["request_sha256"]!=sha256_hex(&serde_json::to_vec(&reset["request"])?) || reset["request"]["owner"]!=m.owner || reset["request"]["workspace_id"]!=m.reset_workspace_id || reset["request"]["account_id"]!=account || reset["request"]["operation_id"]!=m.operation_id || reset["result"]["complete"]!=true {return Err(unavailable())}
    let components=reset["request"]["components"].as_array().ok_or_else(unavailable)?;let has=|name:&str|components.iter().any(|v|v.as_str()==Some(name));
    match m.mode.as_str() {
        "full" if has("memories") && reset["request"].get("selective_manifest_sha256").is_none() && reset["result"]["categories"]["memories"]["state"]=="complete"=>(),
        "selective" if has("ingest") && reset["request"]["selective_manifest_sha256"].as_str().is_some_and(valid_sha256) && reset["result"]["categories"]["ingest"]["state"]=="complete" && reset["result"]["categories"]["ingest"]["selective"]["partition_sha256"].as_str()==m.partition_sha256.as_deref()=>(),
        _=>return Err(unavailable()),
    }
    let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![current_owner,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id),m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if out.as_ref().is_none_or(|(v,n)|*n!=0 || v!=&raw) {return Err(unavailable())}Ok(())
}

/// Full-reset candidate proof: accepted candidates require their output-specific S03 root.
fn full_candidate(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,fact:&SettledPolicyOutcome,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<(String,String,String)>,RepositoryError> {
    full_candidate_in(tx,owner,account,workspace,candidate,fact,view,budget,S08HistoricalRead::Ordinary)
}
fn full_candidate_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,fact:&SettledPolicyOutcome,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,mode:S08HistoricalRead<'_, '_, '_>)->Result<Option<(String,String,String)>,RepositoryError> {
    let status:Option<String>=tx.query_row("SELECT status FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|x|x.get(0)).optional()?;
    match status.as_deref() {
        Some("pending")=>{let c=settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;if c.job_id!=fact.job_id || c.attempt_id!=fact.attempt_id || c.lease_epoch!=fact.lease_epoch {return Err(unavailable())}Ok(None)},
        Some("accepted") if fact.intent.output_kind=="importance_target"=>{
            let target=fact.intent.importance_target.as_ref().ok_or_else(unavailable)?;
            let size:Option<i64>=tx.query_row("SELECT length(CAST(claim_slot AS BLOB)) FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND block_id=?2 AND workspace_key=?3 AND project_key=''",params![owner,target.block_id,workspace],|r|r.get(0)).optional()?;
            let size=size.filter(|n|(1..=512).contains(n)).ok_or_else(unavailable)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
            let root:String=tx.query_row("SELECT claim_slot FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND block_id=?2 AND workspace_key=?3 AND project_key=''",params![owner,target.block_id,workspace],|r|r.get(0))?;
            let (context,children)=match mode {S08HistoricalRead::Ordinary=>super::s08_policy_review::importance_chain_facts(tx,owner,account,workspace,&root,view,budget)?,S08HistoricalRead::Phase(proof)=>super::s08_policy_review::importance_chain_facts_phase(tx,owner,account,workspace,&root,view,budget,proof)?};
            if context["block_id"]!=target.block_id {return Err(unavailable())}
            let mut matched=children.iter().filter(|child|child.proposal_candidate_id.as_deref()==Some(candidate));
            let child=matched.next().ok_or_else(unavailable)?;if matched.next().is_some() || child.account_id!=account || child.workspace_id!=workspace || child.block_id!=target.block_id {return Err(unavailable())}
            Ok(Some((child.receipt_id.clone(),"s02_importance_review".into(),child.receipt_sha256.clone())))
        },
        Some("accepted")=>{let (receipt,domain,prefix,kind)=match fact.intent.output_kind.as_str() {"connection"=>(super::s02_connection::connection_accepted_root(tx,owner,account,workspace,candidate,view,budget)?,"s02.connection-review.v1","cs_s02connection_review_","s02_connection_review"),"summary"=>(super::s08_policy_review::summary_accepted_root_in(tx,owner,account,workspace,candidate,view,budget,mode)?,"s08.policy-review.v1","cs_s08policy_review_","s08_policy_review"),"journal_synthesis"=>(super::s08_journal_review::journal_accepted_root(tx,owner,account,workspace,candidate,view,budget)?,"s08.policy-review.v1","cs_s08policy_review_","s08_policy_review"),_=>return Err(unavailable())};let key=receipt["request_key"].as_str().ok_or_else(unavailable)?;let id=format!("{}{}",prefix,sha256_hex(&serde_json::to_vec(&(domain,account,candidate,key))?));Ok(Some((id,kind.into(),sha256_hex(&serde_json::to_vec(&receipt)?))))},
        Some("rejected")=>{let receipt=super::s08_policy_review::rejected_root(tx,owner,account,workspace,candidate,&fact.intent.output_kind,view,budget)?;let key=receipt["request_key"].as_str().ok_or_else(unavailable)?;let id=format!("cs_s08policy_reject_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-reject.v1",account,candidate,key))?));Ok(Some((id,"s08_policy_reject".into(),sha256_hex(&serde_json::to_vec(&receipt)?))))},
        _=>Err(unavailable()),
    }
}
/// Exact full-reset policy locator; caller retains owner-wide orphan accounting.
pub(crate) fn full_cleanup_for_job(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,attempt:Option<&str>,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(S08ModelPrepareRequest,S08PolicyCleanupLocator,Value),RepositoryError> {
    full_cleanup_for_job_in(tx,owner,account,workspace,job,attempt,view,budget,S08HistoricalRead::Ordinary)
}
pub(crate) fn full_cleanup_for_job_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,attempt:Option<&str>,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,mode:S08HistoricalRead<'_, '_, '_>)->Result<(S08ModelPrepareRequest,S08PolicyCleanupLocator,Value),RepositoryError> {
    let Some(attempt)=attempt else {return prepared_cleanup_for_job(tx,owner,account,workspace,job,view,budget)};
    let fact=settled_outcome(tx,owner,account,workspace,job,attempt,view,budget)?;
    // Only an accepted scored summary or its exact target receipt may use
    // phase-factual replay. Other lifecycle families keep their old owner.
    let selected=if let Some(candidate)=fact.candidate_id.as_deref() {
        if fact.intent.output_kind=="importance_target" {true}
        else if fact.intent.output_kind=="summary" {
            let row:Option<(i64,bool,Option<String>)>=tx.query_row("SELECT length(CAST(m.metadata_json AS BLOB)),json_valid(m.metadata_json),CASE WHEN json_valid(m.metadata_json) THEN json_type(m.metadata_json,'$.importance_score') END FROM candidates c JOIN fm_v2_revision_metadata m ON m.owner_id=c.owner AND m.block_id=(SELECT accepted_block_id FROM fm_v2_candidate_revisions r WHERE r.owner_id=c.owner AND r.candidate_id=c.id AND r.revision=4) AND m.revision=1 WHERE c.owner=?1 AND c.workspace_id=?2 AND c.id=?3 AND c.status='accepted'",params![owner,workspace,candidate],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
            if let Some((length,valid,kind))=row {if !(1..=16384).contains(&length) || !valid {return Err(unavailable())}budget.check(length as usize).map_err(RepositoryError::Validation)?;kind.is_some()} else {false}
        } else {false}
    } else {false};
    let effective=if selected {mode} else {S08HistoricalRead::Ordinary};
    let accepted=if let Some(candidate)=fact.candidate_id.as_deref() {full_candidate_in(tx,owner,account,workspace,candidate,&fact,view,budget,effective)?} else {None};
    let mut locator=cleanup_locator(account,&fact)?;locator.accepted_review=accepted;let mut binding=partition_binding(account,&fact)?;
    if let Some((id,kind,hash))=&locator.accepted_review {binding.as_object_mut().ok_or_else(unavailable)?.insert("accepted_review".into(),json!({"id":id,"kind":kind,"sha256":hash}));}
    if fact.intent.output_kind=="summary" && locator.accepted_review.as_ref().is_some_and(|(_,kind,_)|kind=="s08_policy_review") {
        let candidate=locator.candidate_id.as_deref().ok_or_else(unavailable)?;
        let root=super::s08_policy_review::summary_accepted_root_in(tx,owner,account,workspace,candidate,view,budget,effective)?;
        let block=root["block_id"].as_str().ok_or_else(unavailable)?;
        if let Some(metadata)=value_metadata_carry(tx,owner,block,1)? {
            budget.check(metadata.json.len()).map_err(RepositoryError::Validation)?;
            let value=crate::field_authority::parse_unique_json(&metadata.json).map_err(RepositoryError::Validation)?;
            if value.get("importance_score").is_some() {
                let (context,children)=match effective {S08HistoricalRead::Ordinary=>super::s08_policy_review::importance_chain_facts(tx,owner,account,workspace,candidate,view,budget)?,S08HistoricalRead::Phase(proof)=>super::s08_policy_review::importance_chain_facts_phase(tx,owner,account,workspace,candidate,view,budget,proof)?};
                let raw=serde_json::to_vec(&(context,&children))?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
                binding.as_object_mut().ok_or_else(unavailable)?.insert("s02_importance_binding_sha256".into(),json!(sha256_hex(&raw)));
                locator.importance_children=children;
            }
        }
    }
    Ok((fact.intent,locator,binding))
}
/// Called by the reset owner after its receipt is recorded and before deletion.
/// Re-reads all facts in this exact transaction; a supplied object is not authority.
pub(crate) fn write_terminal(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,attempt:&str,reset_workspace:&str,operation:&str,mode:&str,partition:Option<&str>,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<S08PolicyTerminal,RepositoryError> {
    let fact=settled_outcome(tx,owner,account,workspace,job,attempt,view,budget)?;
    let review=if let Some(candidate)=fact.candidate_id.as_deref() {
        let status:Option<String>=tx.query_row("SELECT status FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|x|x.get(0)).optional()?;
        if mode=="full" || status.as_deref()==Some("rejected") {full_candidate(tx,owner,account,workspace,candidate,&fact,view,budget)?}
        else {let checked=settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;if checked.job_id!=job || checked.attempt_id!=attempt || checked.lease_epoch!=fact.lease_epoch {return Err(unavailable())}None}
    } else {None};
    let m=S08PolicyTerminal{version:"s04.s08-policy-erased.v1".into(),owner:owner.into(),original_owner:fact.intent.selection.owner.clone(),account_id:account.into(),key_sha256:policy_key_hash(account,&fact.intent.selection.owner,&fact.intent.selection.idempotency_key,&fact.intent.output_kind)?,workspace_id:workspace.into(),reset_workspace_id:reset_workspace.into(),operation_id:operation.into(),mode:mode.into(),partition_sha256:partition.map(str::to_owned),reset_receipt_id:ingest_erased_receipt_id(account,operation)?,job_sha256:selector("s04.s08-policy-job.v1",account,job)?,attempt_sha256:Some(selector("s04.s08-policy-attempt.v1",account,attempt)?),foreground_epoch:Some(fact.foreground_epoch),candidate_sha256:fact.candidate_id.as_deref().map(|v|selector("s04.s08-policy-candidate.v1",account,v)).transpose()?,candidate_content_sha256:fact.content_sha256.clone(),selection_sha256:fact.selection_sha256,intent_sha256:fact.intent_sha256,reserve_receipt_id:Some(fact.reserve_receipt_id),reserve_receipt_sha256:Some(fact.reserve_receipt_sha256),result_receipt_id:fact.result_receipt_id,result_receipt_sha256:fact.result_receipt_sha256,request_sha256:fact.request_sha256,output_kind:fact.intent.output_kind.clone(),state:fact.state.clone(),review_receipt_id:review.as_ref().map(|v|v.0.clone()),review_receipt_kind:review.as_ref().map(|v|v.1.clone()),review_receipt_sha256:review.as_ref().map(|v|v.2.clone())};
    verify_terminal_reset(tx,owner,account,&m,view,budget)?;
    let event=terminal_event(account,job,attempt,fact.lease_epoch)?;let raw=serde_json::to_string(&m)?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s04_s08_policy_erased',?4,0,?5)",params![owner,event,m.reset_receipt_id,raw,chrono::Utc::now().to_rfc3339()])?;
    let key_hash=policy_key_hash(account,&m.original_owner,&fact.intent.selection.idempotency_key,&fact.intent.output_kind)?;
    let key_event=format!("outbox_s04_s08_policy_key_erased_{key_hash}");
    let index=json!({"version":"s04.s08-policy-key-erased.v1","account_id":account,"original_owner":m.original_owner,"output_kind":m.output_kind,"key_sha256":key_hash,"job_sha256":m.job_sha256,"terminal_event":event,"reset_receipt_id":m.reset_receipt_id});
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s04_s08_policy_key_erased',?4,0,?5)",params![owner,key_event,m.reset_receipt_id,serde_json::to_string(&index)?,chrono::Utc::now().to_rfc3339()])?;
    Ok(m)
}


fn prepared_event(account:&str,job:&str)->Result<String,RepositoryError> {
    if account.is_empty() || job.is_empty() {return Err(unavailable())}
    Ok(format!("outbox_s04_s08_policy_erased_{}",sha256_hex(&serde_json::to_vec(&("s04.s08-policy-prepared-terminal.v1",account,job))?)))
}
/// Transaction-local no-attempt locator and content-free partition binding.
pub(crate) fn prepared_cleanup(tx:&Transaction<'_>,owner:&str,account:&str,m:&S08ModelPrepareRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(S08PolicyCleanupLocator,Value),RepositoryError> {
    let fact=original_prepared_cancel(tx,owner,account,m,view,budget)?.ok_or_else(unavailable)?;
    let job=fact["job_id"].as_str().ok_or_else(unavailable)?;let selection=&m.selection;
    let selection_id=format!("cs_s08selection_{}",sha256_hex(&serde_json::to_vec(&(&selection.owner,job))?));
    let intent_id=format!("cs_s08model_{}",sha256_hex(&serde_json::to_vec(&(account,job))?));
    let result_id=format!("cs_s08policycancel_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-cancel.v1",account,job))?));
    let (result_raw,_)=original_fact(tx,owner,&result_id,"s08.policy-cancel.v1","s08_policy_cancel","s08_policy",None,0,budget)?.ok_or_else(unavailable)?;
    let selection_raw=serde_json::to_vec(selection)?;let intent_raw=serde_json::to_vec(m)?;budget.check(selection_raw.len()+intent_raw.len()).map_err(RepositoryError::Validation)?;
    let binding=json!({"version":"s04.s08-policy-binding.v1","original_owner":selection.owner,"account_id":account,"workspace_id":selection.workspace_id,"job_sha256":selector("s04.s08-policy-job.v1",account,job)?,"attempt_sha256":null,"lease_epoch":null,"foreground_epoch":null,"candidate_sha256":null,"selection_sha256":sha256_hex(&selection_raw),"intent_sha256":sha256_hex(&intent_raw),"reserve_receipt_id":null,"reserve_receipt_sha256":null,"result_receipt_id":result_id,"result_receipt_sha256":sha256_hex(result_raw.as_bytes()),"request_sha256":sha256_hex(&intent_raw),"output_kind":m.output_kind,"state":"prepared_cancelled"});
    Ok((S08PolicyCleanupLocator{workspace_id:selection.workspace_id.clone(),job_id:job.into(),attempt_id:None,state:"prepared_cancelled".into(),candidate_id:None,selection_id,intent_id,reserve_receipt_id:None,result_receipt_id:result_id,accepted_review:None,importance_children:Vec::new()},binding))
}
/// Resolve the exact immutable prepared intent from a current-owner job scope.
pub(crate) fn prepared_cleanup_for_job(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(S08ModelPrepareRequest,S08PolicyCleanupLocator,Value),RepositoryError> {
    let id=format!("cs_s08model_{}",sha256_hex(&serde_json::to_vec(&(account,job))?));
    let (raw,_)=original_fact(tx,owner,&id,"s08.model-intent.v1","s08_model_intent","s08_model",None,0,budget)?.ok_or_else(unavailable)?;
    let m:S08ModelPrepareRequest=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if serde_json::to_string(&m)?!=raw || m.selection.workspace_id!=workspace {return Err(unavailable())}
    let (locator,binding)=prepared_cleanup(tx,owner,account,&m,view,budget)?;if locator.job_id!=job {return Err(unavailable())}Ok((m,locator,binding))
}
/// Positive original no-attempt status after exact S06 erasure.
pub(crate) fn read_erased_prepared(tx:&Transaction<'_>,owner:&str,account:&str,m:&S08ModelPrepareRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<S08PolicyTerminal>,RepositoryError> {
    let Some((event,terminal))=read_key_terminal(tx,owner,account,m,view,budget)? else {return Ok(None)};
    let s=&m.selection;let job=format!("s08job_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-job.v1",&s.owner,&s.idempotency_key,&m.output_kind))?));
    let request=serde_json::to_vec(m)?;if request.len()>262144 {return Err(unavailable())}budget.check(request.len()).map_err(RepositoryError::Validation)?;
    let hash=sha256_hex(&request);let id=format!("cs_s08policycancel_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-cancel.v1",account,&job))?));
    let receipt=json!({"version":"s08.policy-cancel.v1","owner":s.owner,"account_id":account,"job_id":job,"request_sha256":hash,"state":"cancelled"});
    if terminal.state!="prepared_cancelled" || event!=prepared_event(account,&job)? || terminal.result_receipt_id!=id || terminal.result_receipt_sha256!=sha256_hex(&serde_json::to_vec(&receipt)?) || terminal.request_sha256!=hash {return Err(unavailable())}Ok(Some(terminal))
}
/// Write the same S06-bound denial family without inventing an attempt or reservation.
pub(crate) fn write_prepared_terminal(tx:&Transaction<'_>,owner:&str,account:&str,m:&S08ModelPrepareRequest,reset_workspace:&str,operation:&str,mode:&str,partition:Option<&str>,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<S08PolicyTerminal,RepositoryError> {
    let (loc,binding)=prepared_cleanup(tx,owner,account,m,view,budget)?;let selection=&m.selection;
    let terminal=S08PolicyTerminal{version:"s04.s08-policy-erased.v1".into(),owner:owner.into(),original_owner:selection.owner.clone(),account_id:account.into(),key_sha256:policy_key_hash(account,&selection.owner,&selection.idempotency_key,&m.output_kind)?,workspace_id:selection.workspace_id.clone(),reset_workspace_id:reset_workspace.into(),operation_id:operation.into(),mode:mode.into(),partition_sha256:partition.map(str::to_owned),reset_receipt_id:ingest_erased_receipt_id(account,operation)?,job_sha256:selector("s04.s08-policy-job.v1",account,&loc.job_id)?,attempt_sha256:None,foreground_epoch:None,candidate_sha256:None,candidate_content_sha256:None,selection_sha256:binding["selection_sha256"].as_str().ok_or_else(unavailable)?.into(),intent_sha256:binding["intent_sha256"].as_str().ok_or_else(unavailable)?.into(),reserve_receipt_id:None,reserve_receipt_sha256:None,result_receipt_id:loc.result_receipt_id,result_receipt_sha256:binding["result_receipt_sha256"].as_str().ok_or_else(unavailable)?.into(),request_sha256:binding["request_sha256"].as_str().ok_or_else(unavailable)?.into(),output_kind:m.output_kind.clone(),state:"prepared_cancelled".into(),review_receipt_id:None,review_receipt_kind:None,review_receipt_sha256:None};
    verify_terminal_reset(tx,owner,account,&terminal,view,budget)?;let event=prepared_event(account,&loc.job_id)?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s04_s08_policy_erased',?4,0,?5)",params![owner,event,terminal.reset_receipt_id,serde_json::to_string(&terminal)?,chrono::Utc::now().to_rfc3339()])?;
    let key_event=format!("outbox_s04_s08_policy_key_erased_{}",terminal.key_sha256);
    let index=json!({"version":"s04.s08-policy-key-erased.v1","account_id":account,"original_owner":terminal.original_owner,"output_kind":terminal.output_kind,"key_sha256":terminal.key_sha256,"job_sha256":terminal.job_sha256,"terminal_event":event,"reset_receipt_id":terminal.reset_receipt_id});
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s04_s08_policy_key_erased',?4,0,?5)",params![owner,key_event,terminal.reset_receipt_id,serde_json::to_string(&index)?,chrono::Utc::now().to_rfc3339()])?;Ok(terminal)
}
/// Positive post-deletion verification of the exact no-attempt terminal.
pub(crate) fn verify_written_prepared_terminal(tx:&Transaction<'_>,owner:&str,account:&str,m:&S08ModelPrepareRequest,job:&str,expected:&S08PolicyTerminal,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let (event,actual)=read_key_terminal(tx,owner,account,m,view,budget)?.ok_or_else(unavailable)?;
    if actual!=*expected || actual.state!="prepared_cancelled" || event!=prepared_event(account,job)? || actual.job_sha256!=selector("s04.s08-policy-job.v1",account,job)? {return Err(unavailable())}Ok(())
}

fn policy_key_hash(account:&str,original_owner:&str,key:&str,kind:&str)->Result<String,RepositoryError> {
    if [account,original_owner,key].iter().any(|v|v.is_empty() || v.trim()!=*v || v.len()>512) || !matches!(kind,"summary"|"connection"|"journal_synthesis"|"importance_target") {return Err(unavailable())}
    Ok(sha256_hex(&serde_json::to_vec(&("s04.s08-policy-key.v1",account,original_owner,key,kind))?))
}
fn read_key_terminal(tx:&Transaction<'_>,current_owner:&str,account:&str,original:&S08ModelPrepareRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<(String,S08PolicyTerminal)>,RepositoryError> {
    let selection=&original.selection;
    if view.current_owner()!=current_owner || view.account_id()!=account || selection.source_account_id!=account {return Err(unavailable())}view.for_captured(tx,&selection.owner)?;
    let key_hash=policy_key_hash(account,&selection.owner,&selection.idempotency_key,&original.output_kind)?;
    let key_event=format!("outbox_s04_s08_policy_key_erased_{key_hash}");
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT kind,change_set_id,source_revision,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current_owner,key_event],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?;
    let Some((kind,change,revision,raw))=row else{return Ok(None)};budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if kind!="s04_s08_policy_key_erased" || revision!=0 {return Err(unavailable())}
    let index=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let event=index["terminal_event"].as_str().filter(|v|v.starts_with("outbox_s04_s08_policy_erased_") && v.len()<=128).ok_or_else(unavailable)?;
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT kind,change_set_id,source_revision,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current_owner,event],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?;
    let (terminal_kind,terminal_change,terminal_revision,terminal_raw)=row.ok_or_else(unavailable)?;budget.check(terminal_raw.len()).map_err(RepositoryError::Validation)?;
    if terminal_kind!="s04_s08_policy_erased" || terminal_revision!=0 || terminal_change!=change {return Err(unavailable())}
    let m:S08PolicyTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&terminal_raw).map_err(RepositoryError::Validation)?)?;
    verify_terminal_reset(tx,current_owner,account,&m,view,budget)?;
    let job=format!("s08job_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-job.v1",&selection.owner,&selection.idempotency_key,&original.output_kind))?));
    let selection_raw=serde_json::to_vec(selection)?;let intent_raw=serde_json::to_vec(original)?;
    if selection_raw.len()>262144 || intent_raw.len()>262144 {return Err(unavailable())}budget.check(selection_raw.len()+intent_raw.len()).map_err(RepositoryError::Validation)?;
    let expected=json!({"version":"s04.s08-policy-key-erased.v1","account_id":account,"original_owner":selection.owner,"output_kind":original.output_kind,"key_sha256":key_hash,"job_sha256":m.job_sha256,"terminal_event":event,"reset_receipt_id":m.reset_receipt_id});
    if m.key_sha256!=key_hash || index!=expected || change!=m.reset_receipt_id || m.original_owner!=selection.owner || m.workspace_id!=selection.workspace_id || m.output_kind!=original.output_kind || m.job_sha256!=selector("s04.s08-policy-job.v1",account,&job)? || m.selection_sha256!=sha256_hex(&selection_raw) || m.intent_sha256!=sha256_hex(&intent_raw) {return Err(unavailable())}
    Ok(Some((event.to_owned(),m)))
}
/// Positive historical candidate erasure only; absence never proves non-issuance.
pub(crate) fn candidate_erased(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,content_sha256:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<bool,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account || !valid_sha256(content_sha256) {return Err(unavailable())}
    let candidate_hash=selector("s04.s08-policy-candidate.v1",account,candidate)?;
    let records={let mut q=tx.prepare("SELECT event_id,change_set_id,source_revision,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND kind='s04_s08_policy_erased' AND json_valid(payload_json) AND json_extract(payload_json,'$.candidate_sha256')=?2 LIMIT 2")?;let rows=q.query_map(params![owner,candidate_hash],|x|Ok((x.get::<_,String>(0)?,x.get::<_,String>(1)?,x.get::<_,i64>(2)?,x.get::<_,String>(3)?)))?.collect::<Result<Vec<_>,_>>()?;rows};
    if records.len()!=1 {return Err(unavailable())}let (event,change,revision,raw)=&records[0];budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let m:S08PolicyTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(raw).map_err(RepositoryError::Validation)?)?;
    if m.workspace_id!=workspace || *revision!=0 || !event.starts_with("outbox_s04_s08_policy_erased_") || change!=&m.reset_receipt_id || m.candidate_sha256.as_deref()!=Some(candidate_hash.as_str()) || m.candidate_content_sha256.as_deref()!=Some(content_sha256) || m.state!="staged" {return Err(unavailable())}
    verify_terminal_reset(tx,owner,account,&m,view,budget)?;
    let index_event=format!("outbox_s04_s08_policy_key_erased_{}",m.key_sha256);
    let row:Option<(String,i64,String)>=tx.query_row("SELECT change_set_id,source_revision,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND kind='s04_s08_policy_key_erased'",params![owner,index_event],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?))).optional()?;
    let (index_change,index_revision,index_raw)=row.ok_or_else(unavailable)?;budget.check(index_raw.len()).map_err(RepositoryError::Validation)?;
    let index=crate::field_authority::parse_unique_json(&index_raw).map_err(RepositoryError::Validation)?;
    if index_revision!=0 || index_change!=m.reset_receipt_id || index!=json!({"version":"s04.s08-policy-key-erased.v1","account_id":account,"original_owner":m.original_owner,"output_kind":m.output_kind,"key_sha256":m.key_sha256,"job_sha256":m.job_sha256,"terminal_event":event,"reset_receipt_id":m.reset_receipt_id}) {return Err(unavailable())}Ok(true)
}
/// Positive content-free rejection status, never inferred from missing rows.
pub(crate) fn rejected_erased(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,content_sha256:&str,review_id:&str,review_sha256:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<bool,RepositoryError> {
    candidate_erased(tx,owner,account,workspace,candidate,content_sha256,view,budget)?;
    if !valid_sha256(review_sha256) {return Err(unavailable())}let candidate_hash=selector("s04.s08-policy-candidate.v1",account,candidate)?;
    let raw:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND kind='s04_s08_policy_erased' AND json_valid(payload_json) AND json_extract(payload_json,'$.candidate_sha256')=?2",params![owner,candidate_hash],|x|x.get(0)).optional()?;let raw=raw.ok_or_else(unavailable)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let m:S08PolicyTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if m.review_receipt_id.as_deref()!=Some(review_id) || m.review_receipt_kind.as_deref()!=Some("s08_policy_reject") || m.review_receipt_sha256.as_deref()!=Some(review_sha256) {return Err(unavailable())}Ok(true)
}
/// Positive post-deletion proof against the retained transaction-local fact.
pub(crate) fn verify_written_terminal(tx:&Transaction<'_>,current_owner:&str,account:&str,original:&S08ModelPrepareRequest,job:&str,attempt:&str,epoch:i64,expected:&S08PolicyTerminal,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let (event,actual)=read_key_terminal(tx,current_owner,account,original,view,budget)?.ok_or_else(unavailable)?;
    if actual!=*expected || event!=terminal_event(account,job,attempt,epoch)? || actual.job_sha256!=selector("s04.s08-policy-job.v1",account,job)? || actual.attempt_sha256.as_deref()!=Some(selector("s04.s08-policy-attempt.v1",account,attempt)?.as_str()) {return Err(unavailable())}
    Ok(())
}
/// Factual erased status for the exact original terminal request.
pub(crate) fn read_erased_known_terminal(tx:&Transaction<'_>,owner:&str,account:&str,r:&S08PolicyTerminalRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<S08PolicyTerminal>,RepositoryError> {
    let a=&r.attempt;let m=&a.original;let selection=&m.selection;
    let Some((event,marker))=read_key_terminal(tx,owner,account,m,view,budget)? else {return Ok(None)};
    if (r.reason.is_some() && (r.disposition!="failed" || r.reason.as_deref()!=Some("invalid_proposal"))) || r.version!=1 || a.version!=1 || a.reservation_original.version!=1 || serde_json::to_vec(m)?!=serde_json::to_vec(&a.reservation_original.original)? || a.job_id!=a.reservation_original.reservation.job_id || a.reservation_original.reservation.owner!=selection.owner || !matches!(r.disposition.as_str(),"positively_unsent"|"failed"|"cancelled") || (r.disposition=="positively_unsent" && r.usage.is_some()) || (m.mode=="rules" && (r.disposition!="cancelled" || r.usage.is_some())) || marker.state!=r.disposition || marker.candidate_sha256.is_some() || marker.job_sha256!=selector("s04.s08-policy-job.v1",account,&a.job_id)? || marker.attempt_sha256.as_deref()!=Some(selector("s04.s08-policy-attempt.v1",account,&a.attempt_id)?.as_str()) || event!=terminal_event(account,&a.job_id,&a.attempt_id,a.lease_epoch)? {return Err(unavailable())}
    let request=serde_json::to_vec(r)?;let reservation=serde_json::to_vec(&a.reservation_original)?;if request.len()>262144 || reservation.len()>262144 {return Err(unavailable())}budget.check(request.len()+reservation.len()).map_err(RepositoryError::Validation)?;
    let request_hash=sha256_hex(&request);let reserve_hash=sha256_hex(&reservation);let reserve_id=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,&a.job_id,&reserve_hash))?));
    let reserve=json!({"version":"s08.model-reserve.v1","request_sha256":reserve_hash,"owner":selection.owner,"account_id":account,"job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"foreground_epoch":marker.foreground_epoch});
    let result_id=format!("cs_s08policyterminal_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-terminal.v1",account,&a.job_id,&a.attempt_id,a.lease_epoch))?));
    let label=if r.disposition=="positively_unsent" {"known_zero"} else if r.usage.is_some() {"reported"} else {"reserved_bound"};
    // Prior state is not present in the request; compare only its two closed,
    // physically possible original receipts to the immutable marker hash.
    let prior_states:&[&str]=if m.mode=="rules" {&["reserved","dispatched"]} else if r.disposition=="positively_unsent" {&["reserved","dispatched"]} else {&["dispatched"]};
    let mut matches=0;for prior in prior_states {let label=if m.mode=="rules" && *prior=="reserved" {"known_zero"} else {label};let receipt=json!({"version":"s08.policy-terminal.v1","request_sha256":request_hash,"owner":selection.owner,"account_id":account,"job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"disposition":r.disposition,"reason":r.reason,"usage":label,"reported_usage":r.usage,"prior_reservation_state":prior,"reserve_receipt_id":reserve_id});if marker.result_receipt_sha256==sha256_hex(&serde_json::to_vec(&receipt)?) {matches+=1}}
    if matches!=1 || marker.reserve_receipt_id.as_deref()!=Some(reserve_id.as_str()) || marker.reserve_receipt_sha256.as_deref()!=Some(sha256_hex(&serde_json::to_vec(&reserve)?).as_str()) || marker.result_receipt_id!=result_id || marker.request_sha256!=request_hash || marker.output_kind!=m.output_kind {return Err(unavailable())}
    Ok(Some(marker))
}
/// Validate the exact S08 key-denial fact; absence is not an issuance grant.
pub(crate) fn deny_policy_reissue(tx:&Transaction<'_>,current_owner:&str,account:&str,original:&S08ModelPrepareRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if read_key_terminal(tx,current_owner,account,original,view,budget)?.is_some() {return Err(unavailable())}Ok(())
}

/// Retained terminal is a historical denial only; its absence is not authority.
pub(crate) fn read_terminal(tx:&Transaction<'_>,current_owner:&str,account:&str,r:&S08PolicyCommitRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<S08PolicyTerminal>,RepositoryError> {
    let original=&r.attempt.original;let selection=&original.selection;view.for_captured(tx,&selection.owner)?;
    if r.version!=1 || r.attempt.version!=1 || r.attempt.reservation_original.version!=1 || original.version!=1 || selection.version!=1 || view.current_owner()!=current_owner || view.account_id()!=account || selection.source_account_id!=account || serde_json::to_vec(original)?!=serde_json::to_vec(&r.attempt.reservation_original.original)? || r.attempt.job_id!=r.attempt.reservation_original.reservation.job_id || r.attempt.reservation_original.reservation.owner!=selection.owner || !s08_policy_accounting_valid(original) || !s08_importance_valid(&original.mode,&original.importance) || !s08_connection_predicate_valid(&original.output_kind,original.connection_predicate.as_ref()) {return Err(unavailable())}
    let job=&r.attempt.job_id;let attempt=&r.attempt.attempt_id;let epoch=r.attempt.lease_epoch;
    let event=terminal_event(account,job,attempt,epoch)?;
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT kind,change_set_id,source_revision,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current_owner,event],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?;
    let Some((kind,change,revision,raw))=row else{return Ok(None)};budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if kind!="s04_s08_policy_erased" || revision!=0 {return Err(unavailable())}
    let m:S08PolicyTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if change!=m.reset_receipt_id {return Err(unavailable())}verify_terminal_reset(tx,current_owner,account,&m,view,budget)?;
    let (indexed_event,indexed)=read_key_terminal(tx,current_owner,account,original,view,budget)?.ok_or_else(unavailable)?;
    if indexed_event!=event || indexed!=m {return Err(unavailable())}
    let selection_raw=serde_json::to_vec(selection)?;let intent_raw=serde_json::to_vec(original)?;let request_raw=serde_json::to_vec(r)?;
    if selection_raw.len()>262144 || intent_raw.len()>262144 || request_raw.len()>32768 {return Err(unavailable())}budget.check(selection_raw.len()+intent_raw.len()+request_raw.len()).map_err(RepositoryError::Validation)?;
    let reserve_raw=serde_json::to_vec(&r.attempt.reservation_original)?;let reserve_digest=sha256_hex(&reserve_raw);
    let reserve_id=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,job,&reserve_digest))?));
    let result_id=format!("cs_s08policyresult_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-result.v1",account,job,attempt,epoch))?));
    let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,job))?));
    let expected_candidate=if m.state=="staged" {Some(candidate.as_str())} else {None};
    let reserve=json!({"version":"s08.model-reserve.v1","request_sha256":reserve_digest,"owner":selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":epoch,"foreground_epoch":m.foreground_epoch});
    let result=json!({"version":"s08.policy-result.v1","reserve_receipt_id":reserve_id,"request_sha256":sha256_hex(&request_raw),"owner":selection.owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":epoch,"output_kind":original.output_kind,"state":m.state,"candidate_id":expected_candidate});
    if m.original_owner!=selection.owner || m.workspace_id!=selection.workspace_id || m.job_sha256!=selector("s04.s08-policy-job.v1",account,job)? || m.attempt_sha256.as_deref()!=Some(selector("s04.s08-policy-attempt.v1",account,attempt)?.as_str()) || m.candidate_sha256!=expected_candidate.map(|v|selector("s04.s08-policy-candidate.v1",account,v)).transpose()? || m.selection_sha256!=sha256_hex(&selection_raw) || m.intent_sha256!=sha256_hex(&intent_raw) || m.reserve_receipt_id.as_deref()!=Some(reserve_id.as_str()) || m.reserve_receipt_sha256.as_deref()!=Some(sha256_hex(&serde_json::to_vec(&reserve)?).as_str()) || m.result_receipt_id!=result_id || m.result_receipt_sha256!=sha256_hex(&serde_json::to_vec(&result)?) || m.request_sha256!=sha256_hex(&request_raw) || m.output_kind!=original.output_kind {return Err(unavailable())}
    Ok(Some(m))
}
