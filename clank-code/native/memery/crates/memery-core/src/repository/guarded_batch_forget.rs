//! Content-free proof of an original account-bound guarded batch forget.
use super::*;
use crate::repository::owner_custody::VerifiedCustodyView;

const REASON:&str="s01.guarded-batch-forget.v1";
const KIND:&str="guarded_batch_forget";

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ForgottenMemberFact {
    pub batch_id_sha256:String, pub candidate_sha256:String, pub curated_sha256:String, pub input_sha256:String,
    pub original_key_sha256:String, pub review_key_sha256:String,
    pub review_revision:i64, pub review_binding:String,
    #[serde(default,skip_serializing_if="Vec::is_empty")] pub question_child_commitments:Vec<String>,
}
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct GuardedForgetReceipt {
    pub version:String, pub owner:String, pub account_id:String, pub workspace_id:String,
    pub operation_sha256:String, pub preview_sha256:String, pub closure_sha256:String,
    pub policy_sha256:String, pub tombstone_id:String, pub members:Vec<ForgottenMemberFact>,
}
fn text(v:&str)->bool {!v.is_empty() && v==v.trim() && v.len()<=512 && !v.chars().any(char::is_control)}
pub(crate) fn receipt_id(account:&str,workspace:&str,operation:&str)->Result<String,RepositoryError> {
    if !text(account)||!text(workspace)||!text(operation){return Err(RepositoryError::NotFound)}
    Ok(format!("cs_s01forget_{}",sha256_hex(&serde_json::to_vec(&(REASON,account,workspace,operation))?)))
}
pub(crate) fn erasure_id(id:&str)->Result<String,RepositoryError> {
    let digest=id.strip_prefix("cs_s01forget_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
    Ok(format!("s01forget_{digest}"))
}
fn validate(r:&GuardedForgetReceipt)->Result<(),RepositoryError> {
    if r.version!=REASON || ![&r.owner,&r.account_id,&r.workspace_id,&r.tombstone_id].iter().all(|v|text(v))
        || ![&r.operation_sha256,&r.preview_sha256,&r.closure_sha256,&r.policy_sha256].iter().all(|v|valid_sha256(v))
        || r.members.len()>1000 {return Err(RepositoryError::NotFound)}
    let mut seen=std::collections::BTreeSet::new();
    for m in &r.members {
        if ![&m.batch_id_sha256,&m.candidate_sha256,&m.curated_sha256,&m.input_sha256,&m.original_key_sha256,&m.review_key_sha256,&m.review_binding].iter().all(|v|valid_sha256(v))
            || m.review_revision<1 || m.question_child_commitments.len()>65 || m.question_child_commitments.iter().any(|v|!valid_sha256(v)) || m.question_child_commitments.windows(2).any(|v|v[0]>=v[1]) || !seen.insert(&m.candidate_sha256) {return Err(RepositoryError::NotFound)}
    } Ok(())
}
/// Content-free binding of one verified child to a selected forget member.
pub(crate) fn question_child_commitment(account:&str,workspace:&str,captured:&str,action:&str,selector:&str,root:&str,operation:&str)->Result<String,RepositoryError> {
    if ![account,workspace,captured].iter().all(|v|text(v)) || !matches!(action,"question_defer"|"question_dismiss"|"question_reopen"|"question_answer") || ![selector,root,operation].iter().all(|v|valid_sha256(v)) {return Err(RepositoryError::NotFound)}
    Ok(sha256_hex(&serde_json::to_vec(&("s09.forgotten-question-child.v1",account,workspace,captured,action,selector,root,operation))?))
}
pub(crate) fn write_receipt(tx:&Transaction<'_>,id:&str,operation:&str,r:&GuardedForgetReceipt)->Result<(),RepositoryError> {
    validate(r)?;if receipt_id(&r.account_id,&r.workspace_id,operation)?!=id || r.operation_sha256!=sha256_hex(operation.as_bytes()){return Err(RepositoryError::NotFound)}
    let raw=serde_json::to_string(r)?;if raw.len()>65536{return Err(RepositoryError::NotFound)}
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?1,?3,NULL,?4,?5,?6,?7)",params![r.owner,id,REASON,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![r.owner,format!("outbox_{id}_{KIND}"),id,KIND,raw,now])?;Ok(())
}
pub(crate) fn verify_receipt(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,operation:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<GuardedForgetReceipt>,RepositoryError> {
    let id=receipt_id(account,workspace,operation)?;
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![sql_owner,id,REASON],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,contract,source))=row else {
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![sql_owner,id,format!("outbox_{id}_{KIND}")],|r|r.get(0))?;
        return if exists {Err(RepositoryError::NotFound)} else {Ok(None)};
    };
    let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let r:GuardedForgetReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;validate(&r)?;
    let proof=view.for_captured(tx,&r.owner)?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![sql_owner,format!("outbox_{id}_{KIND}"),id,KIND],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if proof.current_owner()!=sql_owner || proof.account_id()!=account || r.account_id!=account || r.workspace_id!=workspace || r.operation_sha256!=sha256_hex(operation.as_bytes()) || actor!="user" || actor_id!=r.owner || contract!=CONTRACT_VERSION || source.is_some() || hash!=sha256_hex(raw.as_bytes()) || out!=Some((Some(raw),0)) {return Err(RepositoryError::NotFound)}
    let tombstone:Option<(Option<String>,Option<String>,Option<String>,Option<String>)>=tx.query_row("SELECT CASE WHEN length(CAST(affected_ids AS BLOB))<=65536 THEN affected_ids END,recover_until,recovered_at,operation_id FROM memory_tombstones WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![sql_owner,workspace,r.tombstone_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?))).optional()?;
    let (closure,recover,recovered,original)=tombstone.ok_or(RepositoryError::NotFound)?;
    let closure=closure.ok_or(RepositoryError::NotFound)?;budget.check(closure.len()).map_err(RepositoryError::Validation)?;
    if (!r.members.is_empty() && (recover.is_some() || recovered.is_some())) || original.as_deref()!=Some(operation) || sha256_hex(closure.as_bytes())!=r.closure_sha256 {return Err(RepositoryError::NotFound)}
    Ok(Some(r))
}

/// Resolve operation only from the actual bounded tombstone, then verify the entire pair.
pub(crate) fn verify_receipt_by_id(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<GuardedForgetReceipt,RepositoryError> {
    if !id.strip_prefix("cs_s01forget_").is_some_and(valid_sha256){return Err(RepositoryError::NotFound)}
    let raw:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![sql_owner,id,REASON],|r|r.get(0)).optional()?.flatten();
    let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let preliminary:GuardedForgetReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;validate(&preliminary)?;
    let operation:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(operation_id AS BLOB))<=512 THEN operation_id END FROM memory_tombstones WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![sql_owner,workspace,preliminary.tombstone_id],|r|r.get(0)).optional()?.flatten();
    let operation=operation.ok_or(RepositoryError::NotFound)?;
    if receipt_id(account,workspace,&operation)?!=id{return Err(RepositoryError::NotFound)}
    let verified=verify_receipt(tx,sql_owner,account,workspace,&operation,view,budget)?.ok_or(RepositoryError::NotFound)?;
    if verified!=preliminary{return Err(RepositoryError::NotFound)} Ok(verified)
}

/// Verify the genuine full-memory S06 reset pair in the current account custody.
/// Used only to rebase content-free facts before that reset removes tombstones.
pub(crate) fn verify_full_reset_receipt_by_id(tx:&Transaction<'_>,sql_owner:&str,account:&str,id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<String,RepositoryError> {
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![sql_owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (hash,raw,actor,actor_id,contract,source)=row.ok_or(RepositoryError::NotFound)?;let raw=raw.ok_or(RepositoryError::NotFound)?;
    budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let request=value.get("request").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    let reset_owner=request.get("owner").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
    let operation=request.get("operation_id").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
    let workspace=request.get("workspace_id").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
    let proof=view.for_captured(tx,reset_owner)?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![sql_owner,format!("outbox_{id}_s06_guarded_reset"),id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if proof.current_owner()!=sql_owner || proof.account_id()!=account || request.get("account_id")!=Some(&json!(account)) || !text(workspace)
        || ingest_erased_receipt_id(account,operation)?!=id || actor!="user" || actor_id!=reset_owner || contract!=CONTRACT_VERSION || source.is_some() || hash!=sha256_hex(raw.as_bytes()) || out!=Some((Some(raw.clone()),0))
        || value.as_object().map(|o|o.len())!=Some(4) || value["version"]!="s06.guarded-reset.v1" || value["request_sha256"]!=sha256_hex(&serde_json::to_vec(&value["request"])? )
        || value["result"]["complete"]!=true || value["result"]["categories"]["memories"]["state"]!="complete"
        || !request.get("components").and_then(Value::as_array).is_some_and(|items|items.iter().any(|v|v.as_str()==Some("memories"))) || request.get("selective_manifest_sha256").is_some() {return Err(RepositoryError::NotFound)}
    Ok(reset_owner.to_owned())
}

fn member_event_id_from_hash(account:&str,workspace:&str,candidate_sha256:&str)->Result<String,RepositoryError> {
    if !text(account)||!text(workspace)||!valid_sha256(candidate_sha256){return Err(RepositoryError::NotFound)}
    Ok(format!("outbox_s01forgotten_{}",sha256_hex(&serde_json::to_vec(&("s01.forgotten-member.v1",account,workspace,candidate_sha256))?)))
}
fn member_event_id(account:&str,workspace:&str,candidate:&str)->Result<String,RepositoryError> {
    if !candidate.strip_prefix("manualadd_").is_some_and(valid_sha256){return Err(RepositoryError::NotFound)}
    member_event_id_from_hash(account,workspace,&sha256_hex(candidate.as_bytes()))
}
/// Persist only the exact factual selected member under the real paired forget receipt.
pub(crate) fn write_forgotten_member(tx:&Transaction<'_>,sql_owner:&str,captured_owner:&str,account:&str,workspace:&str,candidate:&str,receipt_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let receipt=verify_receipt_by_id(tx,sql_owner,account,workspace,receipt_id,view,budget)?;
    let proof=view.for_captured(tx,captured_owner)?;if proof.current_owner()!=sql_owner || proof.account_id()!=account{return Err(RepositoryError::NotFound)}
    let hash=sha256_hex(candidate.as_bytes());let mut found=receipt.members.iter().filter(|m|m.candidate_sha256==hash);
    let fact=found.next().ok_or(RepositoryError::NotFound)?;if found.next().is_some(){return Err(RepositoryError::NotFound)}
    let payload=json!({"version":"s01.forgotten-member.v1","captured_owner":captured_owner,"account_id":account,"workspace_id":workspace,"receipt_id":receipt_id,"fact":fact});
    let raw=serde_json::to_string(&payload)?;if raw.len()>8192{return Err(RepositoryError::NotFound)}
    let event=member_event_id(account,workspace,candidate)?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'explicit_add_forgotten',?4,0,?5)",params![sql_owner,event,receipt_id,raw,chrono::Utc::now().to_rfc3339()])?;Ok(())
}
/// A terminal is factual only after its real forget receipt, scoped account and exact member match.
pub(crate) fn verify_forgotten_member(tx:&Transaction<'_>,sql_owner:&str,captured_owner:&str,account:&str,workspace:&str,candidate:&str,receipt_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let receipt=verify_receipt_by_id(tx,sql_owner,account,workspace,receipt_id,view,budget)?;
    let proof=view.for_captured(tx,captured_owner)?;if proof.current_owner()!=sql_owner || proof.account_id()!=account{return Err(RepositoryError::NotFound)}
    let hash=sha256_hex(candidate.as_bytes());let mut found=receipt.members.iter().filter(|m|m.candidate_sha256==hash);
    let fact=found.next().ok_or(RepositoryError::NotFound)?;if found.next().is_some(){return Err(RepositoryError::NotFound)}
    let event=member_event_id(account,workspace,candidate)?;
    let row:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='explicit_add_forgotten'",params![sql_owner,event,receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let (raw,revision)=row.ok_or(RepositoryError::NotFound)?;let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let expected=json!({"version":"s01.forgotten-member.v1","captured_owner":captured_owner,"account_id":account,"workspace_id":workspace,"receipt_id":receipt_id,"fact":fact});
    if revision!=0 || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=expected {return Err(RepositoryError::NotFound)} Ok(())
}

/// Resolve an original member terminal without trusting a supplied receipt locator.
pub(crate) fn read_forgotten_member(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,candidate:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<ForgottenMemberFact>,RepositoryError> {
    let event=member_event_id(account,workspace,candidate)?;
    let row:Option<(String,String,Option<String>,i64)>=tx.query_row("SELECT kind,change_set_id,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![sql_owner,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let Some((kind,receipt_id,raw,revision))=row else {return Ok(None)};
    let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let captured=value.get("captured_owner").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
    let fact:ForgottenMemberFact=serde_json::from_value(value.get("fact").cloned().ok_or(RepositoryError::NotFound)?)?;
    if kind=="explicit_add_forgotten" {
        verify_forgotten_member(tx,sql_owner,captured,account,workspace,candidate,&receipt_id,view,budget)?;
    } else if kind=="explicit_add_forgotten_reset" {
        let proof=view.for_captured(tx,captured)?;if proof.current_owner()!=sql_owner||proof.account_id()!=account{return Err(RepositoryError::NotFound)}
        verify_full_reset_receipt_by_id(tx,sql_owner,account,&receipt_id,view,budget)?;
        if revision!=0 || value!=json!({"version":"s01.forgotten-member-reset.v1","captured_owner":captured,"account_id":account,"workspace_id":workspace,"reset_receipt_id":receipt_id,"fact":fact}) || fact.candidate_sha256!=sha256_hex(candidate.as_bytes()) || ![&fact.batch_id_sha256,&fact.candidate_sha256,&fact.curated_sha256,&fact.input_sha256,&fact.original_key_sha256,&fact.review_key_sha256,&fact.review_binding].iter().all(|v|valid_sha256(v)) || fact.review_revision<1 {return Err(RepositoryError::NotFound)}
    } else {return Err(RepositoryError::NotFound)}
    Ok(Some(fact))
}

const RESET_BATCH_REASON:&str="s01.explicit-add-batch-reset-terminal.v1";
const RESET_BATCH_KIND:&str="explicit_add_batch_reset_terminal";
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct ResetBatchTerminal {version:String, reset_receipt_id:String, reset_actor:String, batch:super::explicit_add_batch::BatchTerminal}
fn reset_batch_id(batch_id:&str)->Result<String,RepositoryError> {
    if !batch_id.strip_prefix("cs_s01batch_").is_some_and(valid_sha256){return Err(RepositoryError::NotFound)}
    Ok(format!("cs_s01batch_reset_{}",sha256_hex(batch_id.as_bytes())))
}
/// Resolve only an exact paired reset terminal backed by the actual full S06 receipt.
pub(crate) fn verify_reset_batch_terminal(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,batch_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<super::explicit_add_batch::BatchTerminal>,RepositoryError> {
    let id=reset_batch_id(batch_id)?;let event=format!("outbox_{id}_{RESET_BATCH_KIND}");
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![sql_owner,id,RESET_BATCH_REASON],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,contract,source))=row else {let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![sql_owner,id,event],|r|r.get(0))?;return if collision {Err(RepositoryError::NotFound)} else {Ok(None)}};
    let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let value:ResetBatchTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    let captured=view.for_captured(tx,&value.batch.owner)?;
    let reset_actor=verify_full_reset_receipt_by_id(tx,sql_owner,account,&value.reset_receipt_id,view,budget)?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![sql_owner,event,id,RESET_BATCH_KIND],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if value.version!=RESET_BATCH_REASON || value.reset_actor!=reset_actor || actor!="user" || actor_id!=reset_actor || contract!=CONTRACT_VERSION || source.is_some() || hash!=sha256_hex(raw.as_bytes()) || out!=Some((Some(raw),0)) || captured.current_owner()!=sql_owner || captured.account_id()!=account
       || value.batch.version!="s01.explicit-add-batch-terminal.v1" || value.batch.batch_id!=batch_id || value.batch.account_id!=account || value.batch.workspace_id!=workspace || !value.batch.members.is_empty() || value.batch.forget_receipt_id.is_some() || ![&value.batch.request_key_sha256,&value.batch.preview_sha256,&value.batch.binding_sha256,&value.batch.operation_sha256].iter().all(|v|valid_sha256(v)) {return Err(RepositoryError::NotFound)}
    Ok(Some(value.batch))
}
/// Replace a fully verified forget terminal under the real reset before its tombstone is erased.
pub(crate) fn write_reset_batch_terminal(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,batch_id:&str,reset_receipt_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<String>,RepositoryError> {
    let old=super::explicit_add_batch::verify_batch_terminal(tx,sql_owner,account,workspace,batch_id,view,budget)?.ok_or(RepositoryError::NotFound)?;
    let old_receipt=old.forget_receipt_id.clone();
    let reset_actor=verify_full_reset_receipt_by_id(tx,sql_owner,account,reset_receipt_id,view,budget)?;
    if verify_reset_batch_terminal(tx,sql_owner,account,workspace,batch_id,view,budget)?.is_some(){return Err(RepositoryError::NotFound)}
    let old_id=format!("cs_s01batch_terminal_{}",sha256_hex(batch_id.as_bytes()));let old_event=format!("outbox_{old_id}_explicit_add_batch_terminal");
    if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='explicit_add_batch_terminal'",params![sql_owner,old_event,old_id])?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s01.explicit-add-batch-terminal.v1'",params![sql_owner,old_id])?!=1 {return Err(RepositoryError::NotFound)}
    let mut batch=old;batch.members.clear();batch.forget_receipt_id=None;
    let payload=ResetBatchTerminal{version:RESET_BATCH_REASON.into(),reset_receipt_id:reset_receipt_id.into(),reset_actor:reset_actor.clone(),batch};let raw=serde_json::to_string(&payload)?;if raw.len()>65536{return Err(RepositoryError::NotFound)}
    let id=reset_batch_id(batch_id)?;let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![sql_owner,id,reset_actor,RESET_BATCH_REASON,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![sql_owner,format!("outbox_{id}_{RESET_BATCH_KIND}"),id,RESET_BATCH_KIND,raw,now])?;Ok(old_receipt)
}

const RESET_FORGET_REASON:&str="s01.guarded-forget-reset-terminal.v1";
const RESET_FORGET_KIND:&str="guarded_forget_reset_terminal";
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct ResetForgetTerminal {version:String,original_receipt_id:String,owner:String,account_id:String,workspace_id:String,operation_sha256:String,preview_sha256:String,closure_sha256:String,policy_sha256:String,reset_receipt_id:String,reset_actor:String}
fn reset_forget_id(original:&str)->Result<String,RepositoryError> {
    if !original.strip_prefix("cs_s01forget_").is_some_and(valid_sha256){return Err(RepositoryError::NotFound)}
    Ok(format!("cs_s01forget_reset_{}",sha256_hex(original.as_bytes())))
}
/// A reset terminal means only that the original operation is retired; never absent.
pub(crate) fn verify_reset_forget_terminal(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,operation:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<bool,RepositoryError> {
    let original=receipt_id(account,workspace,operation)?;let id=reset_forget_id(&original)?;let event=format!("outbox_{id}_{RESET_FORGET_KIND}");
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![sql_owner,id,RESET_FORGET_REASON],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,contract,source))=row else {let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![sql_owner,id,event],|r|r.get(0))?;return if collision {Err(RepositoryError::NotFound)} else {Ok(false)}};
    let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let t:ResetForgetTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    let captured=view.for_captured(tx,&t.owner)?;let reset_actor=verify_full_reset_receipt_by_id(tx,sql_owner,account,&t.reset_receipt_id,view,budget)?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![sql_owner,event,id,RESET_FORGET_KIND],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if t.version!=RESET_FORGET_REASON || t.original_receipt_id!=original || t.account_id!=account || t.workspace_id!=workspace || t.operation_sha256!=sha256_hex(operation.as_bytes()) || ![&t.operation_sha256,&t.preview_sha256,&t.closure_sha256,&t.policy_sha256].iter().all(|v|valid_sha256(v)) || captured.current_owner()!=sql_owner || captured.account_id()!=account || t.reset_actor!=reset_actor || actor!="user" || actor_id!=reset_actor || contract!=CONTRACT_VERSION || source.is_some() || hash!=sha256_hex(raw.as_bytes()) || out!=Some((Some(raw),0)) {return Err(RepositoryError::NotFound)} Ok(true)
}
/// Rebase one verified original and every exact member event under a genuine full reset.
pub(crate) fn write_reset_forget_terminal(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,original:&str,reset_receipt_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let receipt=verify_receipt_by_id(tx,sql_owner,account,workspace,original,view,budget)?;
    let reset_actor=verify_full_reset_receipt_by_id(tx,sql_owner,account,reset_receipt_id,view,budget)?;
    let still_referenced:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND reason='s01.explicit-add-batch-terminal.v1' AND (NOT json_valid(operation_metadata_json) OR json_extract(CASE WHEN json_valid(operation_metadata_json) THEN operation_metadata_json ELSE '{}' END,'$.forget_receipt_id')=?2))",params![sql_owner,original],|r|r.get(0))?;
    if still_referenced{return Err(RepositoryError::NotFound)}
    let mut expected=std::collections::BTreeSet::new();
    for fact in &receipt.members {
        let event=member_event_id_from_hash(account,workspace,&fact.candidate_sha256)?;
        if !expected.insert(event.clone()){return Err(RepositoryError::NotFound)}
        let row:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='explicit_add_forgotten'",params![sql_owner,event,original],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let (raw,revision)=row.ok_or(RepositoryError::NotFound)?;let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let captured=value.get("captured_owner").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
        let custody=view.for_captured(tx,captured)?;if custody.current_owner()!=sql_owner||custody.account_id()!=account{return Err(RepositoryError::NotFound)}
        if revision!=0 || value!=json!({"version":"s01.forgotten-member.v1","captured_owner":captured,"account_id":account,"workspace_id":workspace,"receipt_id":original,"fact":fact}){return Err(RepositoryError::NotFound)}
        let reset=json!({"version":"s01.forgotten-member-reset.v1","captured_owner":captured,"account_id":account,"workspace_id":workspace,"reset_receipt_id":reset_receipt_id,"fact":fact});let reset_raw=serde_json::to_string(&reset)?;
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='explicit_add_forgotten'",params![sql_owner,event,original])?!=1{return Err(RepositoryError::NotFound)}
        tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'explicit_add_forgotten_reset',?4,0,?5)",params![sql_owner,event,reset_receipt_id,reset_raw,chrono::Utc::now().to_rfc3339()])?;
    }
    let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2 AND kind<>'guarded_batch_forget')",params![sql_owner,original],|r|r.get(0))?;if extra{return Err(RepositoryError::NotFound)}
    let id=reset_forget_id(original)?;let t=ResetForgetTerminal{version:RESET_FORGET_REASON.into(),original_receipt_id:original.into(),owner:receipt.owner,account_id:account.into(),workspace_id:workspace.into(),operation_sha256:receipt.operation_sha256,preview_sha256:receipt.preview_sha256,closure_sha256:receipt.closure_sha256,policy_sha256:receipt.policy_sha256,reset_receipt_id:reset_receipt_id.into(),reset_actor:reset_actor.clone()};
    let raw=serde_json::to_string(&t)?;if raw.len()>65536{return Err(RepositoryError::NotFound)} let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![sql_owner,id,reset_actor,RESET_FORGET_REASON,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![sql_owner,format!("outbox_{id}_{RESET_FORGET_KIND}"),id,RESET_FORGET_KIND,raw,now])?;
    if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='guarded_batch_forget'",params![sql_owner,format!("outbox_{original}_{KIND}"),original])?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![sql_owner,original,REASON])?!=1{return Err(RepositoryError::NotFound)} Ok(())
}

/// Enumerate each actual retained guarded forget before a full reset removes tombstones.
pub(crate) fn collect_forget_receipts(tx:&Transaction<'_>,sql_owner:&str,account:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Vec<(String,String)>,RepositoryError> {
    let mut stmt=tx.prepare("SELECT change_set_id,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND reason=?2 ORDER BY change_set_id LIMIT 1025")?;
    let rows=stmt.query_map(params![sql_owner,REASON],|r|Ok((r.get::<_,String>(0)?,r.get::<_,Option<String>>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024{return Err(RepositoryError::NotFound)} let mut expected=std::collections::BTreeSet::new();let mut output=Vec::new();
    for (id,raw) in rows {let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let r:GuardedForgetReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        let verified=verify_receipt_by_id(tx,sql_owner,account,&r.workspace_id,&id,view,budget)?;
        if verified!=r || !expected.insert(format!("outbox_{id}_{KIND}")){return Err(RepositoryError::NotFound)} output.push((r.workspace_id,id));}
    let mut stmt=tx.prepare("SELECT event_id,length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND kind=?2 ORDER BY event_id LIMIT 1025")?;
    let events=stmt.query_map(params![sql_owner,KIND],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if events.len()>1024{return Err(RepositoryError::NotFound)} for (id,size) in events {if !(0..=65536).contains(&size){return Err(RepositoryError::NotFound)} budget.check(id.len().saturating_add(size as usize)).map_err(RepositoryError::Validation)?;if !expected.remove(&id){return Err(RepositoryError::NotFound)}}
    if !expected.is_empty(){return Err(RepositoryError::NotFound)}
    let mut expected_members=std::collections::BTreeSet::new();
    for (workspace,id) in &output {
        let receipt=verify_receipt_by_id(tx,sql_owner,account,workspace,id,view,budget)?;
        for fact in &receipt.members {
            let event=member_event_id_from_hash(account,workspace,&fact.candidate_sha256)?;
            if !expected_members.insert(event.clone()){return Err(RepositoryError::NotFound)}
            let row:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='explicit_add_forgotten'",params![sql_owner,event,id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            let (raw,revision)=row.ok_or(RepositoryError::NotFound)?;let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
            let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
            let captured=value.get("captured_owner").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
            let proof=view.for_captured(tx,captured)?;if proof.current_owner()!=sql_owner||proof.account_id()!=account{return Err(RepositoryError::NotFound)}
            if revision!=0 || value!=json!({"version":"s01.forgotten-member.v1","captured_owner":captured,"account_id":account,"workspace_id":workspace,"receipt_id":id,"fact":fact}){return Err(RepositoryError::NotFound)}
        }
    }
    let mut stmt=tx.prepare("SELECT event_id,length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND kind='explicit_add_forgotten' ORDER BY event_id LIMIT 1025")?;
    let rows=stmt.query_map([sql_owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024{return Err(RepositoryError::NotFound)} for (id,size) in rows {if !(0..=8192).contains(&size){return Err(RepositoryError::NotFound)} budget.check(id.len().saturating_add(size as usize)).map_err(RepositoryError::Validation)?;if !expected_members.remove(&id){return Err(RepositoryError::NotFound)}}
    if !expected_members.is_empty(){return Err(RepositoryError::NotFound)} Ok(output)
}

/// Verify all reset-rebased operation and member facts during owner custody assessment.
pub(crate) fn assess_reset_forget_facts(tx:&Transaction<'_>,sql_owner:&str,account:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let mut stmt=tx.prepare("SELECT change_set_id,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,content_hash,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND reason=?2 ORDER BY change_set_id LIMIT 1025")?;
    let rows=stmt.query_map(params![sql_owner,RESET_FORGET_REASON],|r|Ok((r.get::<_,String>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,String>(4)?,r.get::<_,String>(5)?,r.get::<_,Option<String>>(6)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024{return Err(RepositoryError::NotFound)} let mut expected=std::collections::BTreeSet::new();
    for (id,raw,hash,actor,actor_id,contract,source) in rows {
        let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let t:ResetForgetTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        let captured=view.for_captured(tx,&t.owner)?;let reset_actor=verify_full_reset_receipt_by_id(tx,sql_owner,account,&t.reset_receipt_id,view,budget)?;
        let event=format!("outbox_{id}_{RESET_FORGET_KIND}");
        let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![sql_owner,event,id,RESET_FORGET_KIND],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if id!=reset_forget_id(&t.original_receipt_id)? || t.version!=RESET_FORGET_REASON || t.account_id!=account || !text(&t.workspace_id) || ![&t.operation_sha256,&t.preview_sha256,&t.closure_sha256,&t.policy_sha256].iter().all(|v|valid_sha256(v)) || captured.current_owner()!=sql_owner || captured.account_id()!=account || t.reset_actor!=reset_actor || actor!="user" || actor_id!=reset_actor || contract!=CONTRACT_VERSION || source.is_some() || hash!=sha256_hex(raw.as_bytes()) || out!=Some((Some(raw),0)) || !expected.insert(event) {return Err(RepositoryError::NotFound)}
    }
    let mut stmt=tx.prepare("SELECT event_id,length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND kind=?2 ORDER BY event_id LIMIT 1025")?;
    let rows=stmt.query_map(params![sql_owner,RESET_FORGET_KIND],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024{return Err(RepositoryError::NotFound)} for (id,size) in rows {if !(0..=65536).contains(&size){return Err(RepositoryError::NotFound)} budget.check(id.len().saturating_add(size as usize)).map_err(RepositoryError::Validation)?;if !expected.remove(&id){return Err(RepositoryError::NotFound)}}if !expected.is_empty(){return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT event_id,change_set_id,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND kind='explicit_add_forgotten_reset' ORDER BY event_id LIMIT 1025")?;
    let rows=stmt.query_map([sql_owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,Option<String>>(2)?,r.get::<_,i64>(3)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024{return Err(RepositoryError::NotFound)}
    for (event,reset_id,raw,revision) in rows {
        let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let captured=value.get("captured_owner").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
        let workspace=value.get("workspace_id").and_then(Value::as_str).filter(|v|text(v)).ok_or(RepositoryError::NotFound)?;
        let fact:ForgottenMemberFact=serde_json::from_value(value.get("fact").cloned().ok_or(RepositoryError::NotFound)?)?;
        let proof=view.for_captured(tx,captured)?;verify_full_reset_receipt_by_id(tx,sql_owner,account,&reset_id,view,budget)?;
        if proof.current_owner()!=sql_owner || proof.account_id()!=account || revision!=0 || event!=member_event_id_from_hash(account,workspace,&fact.candidate_sha256)? || ![&fact.batch_id_sha256,&fact.candidate_sha256,&fact.curated_sha256,&fact.input_sha256,&fact.original_key_sha256,&fact.review_key_sha256,&fact.review_binding].iter().all(|v|valid_sha256(v)) || fact.review_revision<1 || value!=json!({"version":"s01.forgotten-member-reset.v1","captured_owner":captured,"account_id":account,"workspace_id":workspace,"reset_receipt_id":reset_id,"fact":fact}){return Err(RepositoryError::NotFound)}
    } Ok(())
}
