//! Closed factual receipts for one explicitly reviewed atomic Add batch.
use super::*;
use crate::repository::owner_custody::VerifiedCustodyView;

const REASON:&str="s01.explicit-add-review-batch.v1";
const KIND:&str="explicit_add_review_batch";
const TERMINAL_REASON:&str="s01.explicit-add-batch-terminal.v1";
const TERMINAL_KIND:&str="explicit_add_batch_terminal";
const RESET_TERMINAL_REASON:&str="s01.explicit-add-batch-reset-terminal.v1";
const RESET_TERMINAL_KIND:&str="explicit_add_batch_reset_terminal";

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct BatchItemReceipt {
    pub candidate_id:String, pub expected_revision:i64, pub expected_binding:String,
    pub review_key:String, pub curated_id:String, pub block_id:String, pub revision:i64,
    pub origin_request_key:String,
}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct BatchReceipt {
    pub version:String, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub request_key:String, pub preview_sha256:String, pub policy_projection:PolicyPublicationRefs,
    pub items:Vec<BatchItemReceipt>,
}
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct HashedBatchMember {
    pub candidate_sha256:String, pub curated_sha256:String, pub block_sha256:String,
    pub input_sha256:String, pub original_key_sha256:String, pub review_key_sha256:String,
    pub review_revision:i64, pub review_binding:String, pub knowledge_revision:i64,
}
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct BatchTerminal {
    pub version:String, pub batch_id:String, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub request_key_sha256:String, pub preview_sha256:String, pub binding_sha256:String, pub operation_sha256:String,
    #[serde(default,skip_serializing_if="Vec::is_empty")] pub members:Vec<HashedBatchMember>,
    #[serde(default,skip_serializing_if="Option::is_none")] pub forget_receipt_id:Option<String>,
}
fn text(v:&str)->bool {!v.is_empty() && v==v.trim() && v.len()<=512 && !v.chars().any(char::is_control)}
fn key(v:&str)->bool {(22..=128).contains(&v.len()) && v.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'_'||b==b'-')}
fn candidate(v:&str)->bool {v.strip_prefix("manualadd_").is_some_and(valid_sha256)}
pub(crate) fn batch_id(account:&str,workspace:&str,request_key:&str)->Result<String,RepositoryError> {
    if !text(account)||!text(workspace)||!key(request_key) {return Err(RepositoryError::NotFound)}
    Ok(format!("cs_s01batch_{}",sha256_hex(&serde_json::to_vec(&("s01.explicit-add-batch-id.v1",account,workspace,request_key))?)))
}
pub(crate) fn batch_item_key(id:&str,ordinal:usize,candidate_id:&str)->Result<String,RepositoryError> {
    if !id.strip_prefix("cs_s01batch_").is_some_and(valid_sha256)||!candidate(candidate_id)||ordinal>=100 {return Err(RepositoryError::NotFound)}
    Ok(format!("s01batch_{}",sha256_hex(&serde_json::to_vec(&("s01.explicit-add-batch-item.v1",id,ordinal,candidate_id))?)))
}
fn validate(receipt:&BatchReceipt)->Result<(),RepositoryError> {
    if receipt.version!=REASON || !text(&receipt.owner)||!text(&receipt.workspace_id)||!text(&receipt.account_id)||!key(&receipt.request_key)||!valid_sha256(&receipt.preview_sha256)||receipt.items.is_empty()||receipt.items.len()>100 {return Err(RepositoryError::NotFound)}
    let id=batch_id(&receipt.account_id,&receipt.workspace_id,&receipt.request_key)?;let mut seen=std::collections::BTreeSet::new();
    for (index,item) in receipt.items.iter().enumerate() {
        if !candidate(&item.candidate_id)||!seen.insert(&item.candidate_id)||item.expected_revision<1||!valid_sha256(&item.expected_binding)||item.review_key!=batch_item_key(&id,index,&item.candidate_id)?||!text(&item.curated_id)||!text(&item.block_id)||item.revision<1||!text(&item.origin_request_key) {return Err(RepositoryError::NotFound)}
    } Ok(())
}
fn event(id:&str,kind:&str)->String {format!("outbox_{id}_{kind}")}
fn terminal_id(id:&str)->String {format!("cs_s01batch_terminal_{}",sha256_hex(id.as_bytes()))}
fn read_pair(tx:&Connection,sql_owner:&str,id:&str,reason:&str,kind:&str,budget:&mut ResetFenceBudget)->Result<Option<String>,RepositoryError> {
    let event_id=event(id,kind);
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![sql_owner,id,reason],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,contract,source))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![sql_owner,id,event_id],|r|r.get(0))?;
        return if collision {Err(RepositoryError::NotFound)} else {Ok(None)};
    };
    let raw=raw.ok_or(RepositoryError::NotFound)?;
    budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![sql_owner,event_id,id,kind],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let parsed=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=parsed["owner"].as_str().unwrap_or("") || contract!=CONTRACT_VERSION || source.is_some() || out!=Some((Some(raw.clone()),0)) {return Err(RepositoryError::NotFound)}
    Ok(Some(raw))
}
fn write_pair(tx:&Transaction<'_>,owner:&str,id:&str,reason:&str,kind:&str,raw:&str)->Result<(),RepositoryError> {
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?1,?3,NULL,?4,?5,?6,?7)",params![owner,id,reason,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![owner,event(id,kind),id,kind,raw,now])?;Ok(())
}
pub(crate) fn write_batch(tx:&Transaction<'_>,receipt:&BatchReceipt,budget:&mut ResetFenceBudget)->Result<String,RepositoryError> {
    validate(receipt)?;let id=batch_id(&receipt.account_id,&receipt.workspace_id,&receipt.request_key)?;
    if read_pair(tx,&receipt.owner,&id,REASON,KIND,budget)?.is_some() || read_pair(tx,&receipt.owner,&terminal_id(&id),TERMINAL_REASON,TERMINAL_KIND,budget)?.is_some() {return Err(RepositoryError::NotFound)}
    let raw=serde_json::to_string(receipt)?;if raw.len()>65536{return Err(RepositoryError::NotFound)}
    write_pair(tx,&receipt.owner,&id,REASON,KIND,&raw)?;Ok(id)
}
fn verify_selected_item(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,captured:&str,item:&BatchItemReceipt,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
        budget.check(item.candidate_id.len()).map_err(RepositoryError::Validation)?;
        let review=explicit_add::read_explicit_add_review_receipt_custody(tx,workspace,account,&item.candidate_id,captured,view)?.ok_or(RepositoryError::NotFound)?;
        if review.action!="accept" || review.review_key!=item.review_key || review.expected_revision!=item.expected_revision || review.expected_binding!=item.expected_binding || review.curated_id.as_deref()!=Some(item.curated_id.as_str()) {return Err(RepositoryError::NotFound)}
        let candidate_receipt=candidate_receipt_id(captured,workspace,&item.candidate_id,&item.origin_request_key)?;
        let (hash,raw):(String,Option<String>)=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![sql_owner,candidate_receipt],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
        let raw=raw.ok_or(RepositoryError::NotFound)?;
        budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        if hash!=sha256_hex(raw.as_bytes()) {return Err(RepositoryError::NotFound)}
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let event:Option<Option<String>>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review'",params![sql_owner,format!("outbox_{candidate_receipt}_candidate_review"),candidate_receipt],|r|r.get(0)).optional()?;
        let event=event.flatten().ok_or(RepositoryError::NotFound)?;
        budget.check(event.len()).map_err(RepositoryError::Validation)?;
        let event=crate::field_authority::parse_unique_json(&event).map_err(RepositoryError::Validation)?;
        if value["version"]=="s03.explicit-question-receipt.v1" {
            let group=s09_question_group_in(tx,workspace,&item.block_id,view,budget)?;
            if group.account_id!=account || group.root_kind!="sourced" || group.candidate_id.as_deref()!=Some(item.candidate_id.as_str()) || group.curated_id.as_deref()!=Some(item.curated_id.as_str()) || group.root_receipt_id!=candidate_receipt || group.root_receipt_sha256!=hash || group.root_sha256!=hash || item.revision!=1 || value["request_key"]!=item.origin_request_key {return Err(RepositoryError::NotFound)}
            return Ok(());
        }
        let (binding,found,key,owner_account)=resolve_value_successor_root_custody(tx,workspace,&item.curated_id,&candidate_receipt,&event,&value,view)?;
        if found!=item.candidate_id || key!=item.origin_request_key || owner_account!=account || binding.block_id!=item.block_id || binding.revision!=item.revision {return Err(RepositoryError::NotFound)}
    Ok(())
}
pub(crate) fn verify_live_batch(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<BatchReceipt>,RepositoryError> {
    if sql_owner!=view.current_owner() || account!=view.account_id() {return Err(RepositoryError::NotFound)}
    let Some(raw)=read_pair(tx,sql_owner,id,REASON,KIND,budget)? else {return Ok(None)};
    let receipt:BatchReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    validate(&receipt)?;let proof=view.for_captured(tx,&receipt.owner)?;
    if proof.current_owner()!=sql_owner || receipt.account_id!=account || receipt.workspace_id!=workspace || batch_id(account,workspace,&receipt.request_key)?!=id {return Err(RepositoryError::NotFound)}
    for item in &receipt.items {verify_selected_item(tx,sql_owner,account,workspace,&receipt.owner,item,view,budget)?;}
Ok(Some(receipt))
}

/// A factual selected-member proof tied to the original transaction. It grants no
/// erasure authority and cannot be constructed by a caller.
pub(crate) struct VerifiedEraseMember<'tx,'conn> {
    tx:&'tx Transaction<'conn>, current_owner:String, account:String, workspace:String,
    captured_owner:String, batch_id:String, item:BatchItemReceipt,
}
impl<'tx,'conn> VerifiedEraseMember<'tx,'conn> {
    pub(crate) fn assert_transaction(&self,tx:&Transaction<'_>)->Result<(),RepositoryError> {
        if !std::ptr::eq(self.tx as *const _ as *const (),tx as *const _ as *const ()) {return Err(RepositoryError::NotFound)} Ok(())
    }
    pub(crate) fn current_owner(&self)->&str {&self.current_owner}
    pub(crate) fn account(&self)->&str {&self.account}
    pub(crate) fn workspace(&self)->&str {&self.workspace}
    pub(crate) fn captured_owner(&self)->&str {&self.captured_owner}
    pub(crate) fn batch_id(&self)->&str {&self.batch_id}
    pub(crate) fn candidate_id(&self)->&str {&self.item.candidate_id}
    pub(crate) fn curated_id(&self)->&str {&self.item.curated_id}
    pub(crate) fn block_id(&self)->&str {&self.item.block_id}
    pub(crate) fn revision(&self)->i64 {self.item.revision}
    pub(crate) fn origin_request_key(&self)->&str {&self.item.origin_request_key}
}

/// Initial live-batch selected proof. This refuses missing, duplicated or changed
/// historical receipts; a hash supplied by a caller is never treated as evidence.
pub(crate) fn verify_live_erase_member<'tx,'conn>(
    tx:&'tx Transaction<'conn>,sql_owner:&str,account:&str,workspace:&str,id:&str,
    selected:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,
)->Result<VerifiedEraseMember<'tx,'conn>,RepositoryError> {
    let receipt=verify_live_batch(tx,sql_owner,account,workspace,id,view,budget)?.ok_or(RepositoryError::NotFound)?;
    let mut found=receipt.items.iter().filter(|item|item.candidate_id==selected);
    let item=found.next().cloned().ok_or(RepositoryError::NotFound)?;
    if found.next().is_some(){return Err(RepositoryError::NotFound)}
    Ok(VerifiedEraseMember{tx,current_owner:sql_owner.into(),account:account.into(),workspace:workspace.into(),captured_owner:receipt.owner,batch_id:id.into(),item})
}

pub(crate) fn verify_batch_terminal(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<BatchTerminal>,RepositoryError> {
    let reset=super::guarded_batch_forget::verify_reset_batch_terminal(tx,sql_owner,account,workspace,id,view,budget)?;
    let raw=read_pair(tx,sql_owner,&terminal_id(id),TERMINAL_REASON,TERMINAL_KIND,budget)?;
    if reset.is_some() && raw.is_some(){return Err(RepositoryError::NotFound)}
    let Some(raw)=raw else {return Ok(reset)};
    let terminal:BatchTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    let proof=view.for_captured(tx,&terminal.owner)?;
    if proof.current_owner()!=sql_owner || proof.account_id()!=account || terminal.version!=TERMINAL_REASON || terminal.batch_id!=id || terminal.account_id!=account || terminal.workspace_id!=workspace || !valid_sha256(&terminal.request_key_sha256)||!valid_sha256(&terminal.preview_sha256)||!valid_sha256(&terminal.binding_sha256)||!valid_sha256(&terminal.operation_sha256) {return Err(RepositoryError::NotFound)}
    match &terminal.forget_receipt_id {
        None if terminal.members.is_empty()=>{},
        Some(receipt_id) if !terminal.members.is_empty() && terminal.members.len()<=100=>{
            let proof=super::guarded_batch_forget::verify_receipt_by_id(tx,sql_owner,account,workspace,receipt_id,view,budget)?;
            if proof.operation_sha256!=terminal.operation_sha256 {return Err(RepositoryError::NotFound)}
            let mut seen=std::collections::BTreeSet::new();let batch_hash=sha256_hex(id.as_bytes());
            for m in &terminal.members {
                if ![&m.candidate_sha256,&m.curated_sha256,&m.block_sha256,&m.input_sha256,&m.original_key_sha256,&m.review_key_sha256,&m.review_binding].iter().all(|v|valid_sha256(v)) || m.review_revision<1 || m.knowledge_revision<1 || !seen.insert(&m.candidate_sha256){return Err(RepositoryError::NotFound)}
            }
            if !proof.members.iter().any(|f|f.batch_id_sha256==batch_hash && terminal.members.iter().any(|m|f.candidate_sha256==m.candidate_sha256 && f.curated_sha256==m.curated_sha256 && f.input_sha256==m.input_sha256 && f.original_key_sha256==m.original_key_sha256 && f.review_key_sha256==m.review_key_sha256 && f.review_revision==m.review_revision && f.review_binding==m.review_binding)){return Err(RepositoryError::NotFound)}
        },
        _=>return Err(RepositoryError::NotFound),
    }
    Ok(Some(terminal))
}
pub(crate) fn retire_live_batch(tx:&Transaction<'_>,sql_owner:&str,account:&str,workspace:&str,id:&str,operation_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<BatchTerminal,RepositoryError> {
    if !text(operation_id) || verify_batch_terminal(tx,sql_owner,account,workspace,id,view,budget)?.is_some() {return Err(RepositoryError::NotFound)}
    let receipt=verify_live_batch(tx,sql_owner,account,workspace,id,view,budget)?.ok_or(RepositoryError::NotFound)?;
    let raw=serde_json::to_string(&receipt)?;
    let terminal=BatchTerminal{version:TERMINAL_REASON.into(),batch_id:id.into(),owner:receipt.owner.clone(),workspace_id:workspace.into(),account_id:account.into(),request_key_sha256:sha256_hex(receipt.request_key.as_bytes()),preview_sha256:receipt.preview_sha256.clone(),binding_sha256:sha256_hex(raw.as_bytes()),operation_sha256:sha256_hex(operation_id.as_bytes()),members:Vec::new(),forget_receipt_id:None};
    if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![sql_owner,event(id,KIND),id,KIND])?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![sql_owner,id,REASON])?!=1 {return Err(RepositoryError::NotFound)}
    // Terminal embeds captured owner; SQL owner may have changed through verified custody.
    let terminal_raw=serde_json::to_string(&terminal)?;let now=chrono::Utc::now().to_rfc3339();let tid=terminal_id(id);
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![sql_owner,tid,terminal.owner,TERMINAL_REASON,CONTRACT_VERSION,sha256_hex(terminal_raw.as_bytes()),now,terminal_raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![sql_owner,event(&tid,TERMINAL_KIND),tid,TERMINAL_KIND,terminal_raw,now])?;Ok(terminal)
}

/// Captured before effects; private fields keep the factual proof bound to this transaction.
pub(crate) struct VerifiedBatchForgetPlan<'tx,'conn> {
    tx:&'tx Transaction<'conn>, sql_owner:String, account:String, workspace:String,
    id:String, original:String, receipt:BatchReceipt, members:Vec<HashedBatchMember>, selected:Vec<String>,
}
impl<'tx,'conn> VerifiedBatchForgetPlan<'tx,'conn> {
    pub(crate) fn assert_transaction(&self,tx:&Transaction<'_>)->Result<(),RepositoryError> {
        if !std::ptr::eq(self.tx as *const _ as *const (),tx as *const _ as *const ()) {return Err(RepositoryError::NotFound)} Ok(())
    }
}
/// Verify all original live members before any erase and retain only bounded hashes
/// for the eventual durable terminal. The selected IDs remain private to this plan.
pub(crate) fn prepare_live_batch_forget<'tx,'conn>(tx:&'tx Transaction<'conn>,sql_owner:&str,account:&str,workspace:&str,id:&str,selected:&[String],view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<VerifiedBatchForgetPlan<'tx,'conn>,RepositoryError> {
    let receipt=verify_live_batch(tx,sql_owner,account,workspace,id,view,budget)?.ok_or(RepositoryError::NotFound)?;
    let original=read_pair(tx,sql_owner,id,REASON,KIND,budget)?.ok_or(RepositoryError::NotFound)?;
    let mut chosen=std::collections::BTreeSet::new();
    if selected.is_empty() || selected.iter().any(|id|!chosen.insert(id.clone()) || !receipt.items.iter().any(|v|&v.candidate_id==id)){return Err(RepositoryError::NotFound)}
    let mut members=Vec::with_capacity(receipt.items.len());
    for item in &receipt.items {
        let (proposal,_)=explicit_add::current_explicit_add_proposal_custody(tx,workspace,&item.candidate_id,&receipt.owner,view)?;
        if proposal.account_id!=account || proposal.request_key!=item.origin_request_key {return Err(RepositoryError::NotFound)}
        members.push(HashedBatchMember{candidate_sha256:sha256_hex(item.candidate_id.as_bytes()),curated_sha256:sha256_hex(item.curated_id.as_bytes()),block_sha256:sha256_hex(item.block_id.as_bytes()),input_sha256:proposal.input_sha256,original_key_sha256:explicit_add::explicit_add_key_sha256(account,workspace,&item.origin_request_key)?,review_key_sha256:explicit_add::explicit_add_review_key_sha256(account,workspace,&item.candidate_id,&item.review_key)?,review_revision:item.expected_revision,review_binding:item.expected_binding.clone(),knowledge_revision:item.revision});
    }
    Ok(VerifiedBatchForgetPlan{tx,sql_owner:sql_owner.into(),account:account.into(),workspace:workspace.into(),id:id.into(),original,receipt,members,selected:selected.to_vec()})
}
/// Finish only after the actual paired forget receipt exists, in the same tx.
pub(crate) fn finish_live_batch_forget(tx:&Transaction<'_>,plan:VerifiedBatchForgetPlan<'_,'_>,receipt_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    plan.assert_transaction(tx)?;
    let fact=super::guarded_batch_forget::verify_receipt_by_id(tx,&plan.sql_owner,&plan.account,&plan.workspace,receipt_id,view,budget)?;
    if fact.members.is_empty() || read_pair(tx,&plan.sql_owner,&plan.id,REASON,KIND,budget)?.as_deref()!=Some(plan.original.as_str()) || verify_batch_terminal(tx,&plan.sql_owner,&plan.account,&plan.workspace,&plan.id,view,budget)?.is_some(){return Err(RepositoryError::NotFound)}
    let batch_hash=sha256_hex(plan.id.as_bytes());
    for selected in &plan.selected {
        let member=plan.members.iter().find(|m|m.candidate_sha256==sha256_hex(selected.as_bytes())).ok_or(RepositoryError::NotFound)?;
        let matching=fact.members.iter().filter(|m|m.batch_id_sha256==batch_hash && m.candidate_sha256==member.candidate_sha256 && m.curated_sha256==member.curated_sha256 && m.input_sha256==member.input_sha256 && m.original_key_sha256==member.original_key_sha256 && m.review_key_sha256==member.review_key_sha256 && m.review_revision==member.review_revision && m.review_binding==member.review_binding).count();
        if matching!=1{return Err(RepositoryError::NotFound)}
    }
    if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![plan.sql_owner,event(&plan.id,KIND),plan.id,KIND])?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![plan.sql_owner,plan.id,REASON])?!=1 {return Err(RepositoryError::NotFound)}
    let terminal=BatchTerminal{version:TERMINAL_REASON.into(),batch_id:plan.id.clone(),owner:plan.receipt.owner,workspace_id:plan.workspace,account_id:plan.account,request_key_sha256:sha256_hex(plan.receipt.request_key.as_bytes()),preview_sha256:plan.receipt.preview_sha256,binding_sha256:sha256_hex(plan.original.as_bytes()),operation_sha256:fact.operation_sha256,members:plan.members,forget_receipt_id:Some(receipt_id.into())};
    let raw=serde_json::to_string(&terminal)?;if raw.len()>65536{return Err(RepositoryError::NotFound)}
    let now=chrono::Utc::now().to_rfc3339();let tid=terminal_id(&plan.id);
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![plan.sql_owner,tid,terminal.owner,TERMINAL_REASON,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![plan.sql_owner,event(&tid,TERMINAL_KIND),tid,TERMINAL_KIND,raw,now])?;Ok(())
}

/// Re-establish one surviving member from its own actual review and historical
/// root. Terminal hashes select a candidate; they are never authority.
pub(crate) fn verify_surviving_erase_member<'tx,'conn>(tx:&'tx Transaction<'conn>,sql_owner:&str,account:&str,workspace:&str,id:&str,candidate_id:&str,curated_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<VerifiedEraseMember<'tx,'conn>,RepositoryError> {
    let terminal=verify_batch_terminal(tx,sql_owner,account,workspace,id,view,budget)?.ok_or(RepositoryError::NotFound)?;
    let receipt_id=terminal.forget_receipt_id.as_deref().ok_or(RepositoryError::NotFound)?;
    let forget=super::guarded_batch_forget::verify_receipt_by_id(tx,sql_owner,account,workspace,receipt_id,view,budget)?;
    let candidate_hash=sha256_hex(candidate_id.as_bytes());let curated_hash=sha256_hex(curated_id.as_bytes());
    let mut matches=terminal.members.iter().filter(|m|m.candidate_sha256==candidate_hash && m.curated_sha256==curated_hash);
    let stored=matches.next().ok_or(RepositoryError::NotFound)?;if matches.next().is_some(){return Err(RepositoryError::NotFound)}
    let batch_hash=sha256_hex(id.as_bytes());
    if forget.members.iter().any(|m|m.batch_id_sha256==batch_hash && m.candidate_sha256==candidate_hash){return Err(RepositoryError::NotFound)}
    let (proposal,_)=explicit_add::current_explicit_add_proposal_custody(tx,workspace,candidate_id,&terminal.owner,view)?;
    if proposal.account_id!=account || proposal.input_sha256!=stored.input_sha256 || explicit_add::explicit_add_key_sha256(account,workspace,&proposal.request_key)?!=stored.original_key_sha256{return Err(RepositoryError::NotFound)}
    let review=explicit_add::read_explicit_add_review_receipt_custody(tx,workspace,account,candidate_id,&terminal.owner,view)?.ok_or(RepositoryError::NotFound)?;
    if review.action!="accept" || review.curated_id.as_deref()!=Some(curated_id) || review.expected_revision!=stored.review_revision || review.expected_binding!=stored.review_binding || explicit_add::explicit_add_review_key_sha256(account,workspace,candidate_id,&review.review_key)?!=stored.review_key_sha256{return Err(RepositoryError::NotFound)}
    let root=candidate_receipt_id(&terminal.owner,workspace,candidate_id,&proposal.request_key)?;
    let (hash,raw):(String,Option<String>)=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![sql_owner,root],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
    let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;if hash!=sha256_hex(raw.as_bytes()){return Err(RepositoryError::NotFound)}
    let original=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let event:Option<Option<String>>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review'",params![sql_owner,format!("outbox_{root}_candidate_review"),root],|r|r.get(0)).optional()?;
    let event=event.flatten().ok_or(RepositoryError::NotFound)?;budget.check(event.len()).map_err(RepositoryError::Validation)?;
    let event=crate::field_authority::parse_unique_json(&event).map_err(RepositoryError::Validation)?;
    let (binding,found,key,owner_account)=resolve_value_successor_root_custody(tx,workspace,curated_id,&root,&event,&original,view)?;
    if found!=candidate_id || key!=proposal.request_key || owner_account!=account || sha256_hex(binding.block_id.as_bytes())!=stored.block_sha256 || binding.revision!=stored.knowledge_revision{return Err(RepositoryError::NotFound)}
    let item=BatchItemReceipt{candidate_id:candidate_id.into(),expected_revision:review.expected_revision,expected_binding:review.expected_binding,review_key:review.review_key,curated_id:curated_id.into(),block_id:binding.block_id,revision:binding.revision,origin_request_key:proposal.request_key};
    verify_selected_item(tx,sql_owner,account,workspace,&terminal.owner,&item,view,budget)?;
    Ok(VerifiedEraseMember{tx,current_owner:sql_owner.into(),account:account.into(),workspace:workspace.into(),captured_owner:terminal.owner,batch_id:id.into(),item})
}

/// Pre-effect proof for one later member, tied to this transaction and the
/// unchanged original batch terminal. No new durable survivor ledger is created.
pub(crate) struct VerifiedSurvivorForgetPlan<'tx,'conn> {
    member:VerifiedEraseMember<'tx,'conn>, terminal_raw:String, stored:HashedBatchMember,
}
impl<'tx,'conn> VerifiedSurvivorForgetPlan<'tx,'conn> {
    pub(crate) fn member(&self)->&VerifiedEraseMember<'tx,'conn> {&self.member}
}
pub(crate) fn prepare_survivor_forget<'tx,'conn>(tx:&'tx Transaction<'conn>,sql_owner:&str,account:&str,workspace:&str,id:&str,candidate_id:&str,curated_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<VerifiedSurvivorForgetPlan<'tx,'conn>,RepositoryError> {
    let member=verify_surviving_erase_member(tx,sql_owner,account,workspace,id,candidate_id,curated_id,view,budget)?;
    let terminal_raw=read_pair(tx,sql_owner,&terminal_id(id),TERMINAL_REASON,TERMINAL_KIND,budget)?.ok_or(RepositoryError::NotFound)?;
    let terminal:BatchTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&terminal_raw).map_err(RepositoryError::Validation)?)?;
    let mut matches=terminal.members.iter().filter(|m|m.candidate_sha256==sha256_hex(candidate_id.as_bytes()) && m.curated_sha256==sha256_hex(curated_id.as_bytes()));
    let stored=matches.next().cloned().ok_or(RepositoryError::NotFound)?;if matches.next().is_some(){return Err(RepositoryError::NotFound)}
    Ok(VerifiedSurvivorForgetPlan{member,terminal_raw,stored})
}
/// Complete after the real receipt and the existing per-item forget terminal
/// have been written. No batch receipt is revived or rewritten.
pub(crate) fn finish_survivor_forget(tx:&Transaction<'_>,plan:VerifiedSurvivorForgetPlan<'_,'_>,receipt_id:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    plan.member.assert_transaction(tx)?;
    let m=&plan.member;let stored=&plan.stored;
    if read_pair(tx,&m.current_owner,&terminal_id(&m.batch_id),TERMINAL_REASON,TERMINAL_KIND,budget)?.as_deref()!=Some(plan.terminal_raw.as_str()){return Err(RepositoryError::NotFound)}
    let fact=super::guarded_batch_forget::verify_receipt_by_id(tx,&m.current_owner,&m.account,&m.workspace,receipt_id,view,budget)?;
    let count=fact.members.iter().filter(|f|f.batch_id_sha256==sha256_hex(m.batch_id.as_bytes()) && f.candidate_sha256==stored.candidate_sha256 && f.curated_sha256==stored.curated_sha256 && f.input_sha256==stored.input_sha256 && f.original_key_sha256==stored.original_key_sha256 && f.review_key_sha256==stored.review_key_sha256 && f.review_revision==stored.review_revision && f.review_binding==stored.review_binding).count();
    if count!=1{return Err(RepositoryError::NotFound)}
    super::guarded_batch_forget::verify_forgotten_member(tx,&m.current_owner,&m.captured_owner,&m.account,&m.workspace,&m.item.candidate_id,receipt_id,view,budget)?;
    Ok(())
}

/// Enumerate exact paired rows in current verified custody; unknown or orphan rows fail closed.
pub(crate) fn collect_batch_facts(tx:&Transaction<'_>,sql_owner:&str,account:&str,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(Vec<BatchReceipt>,Vec<BatchTerminal>),RepositoryError> {
    use std::collections::BTreeSet;
    let mut stmt=tx.prepare("SELECT change_set_id,reason,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND reason IN (?2,?3,?4) ORDER BY change_set_id LIMIT 1025")?;
    let rows=stmt.query_map(params![sql_owner,REASON,TERMINAL_REASON,RESET_TERMINAL_REASON],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,Option<String>>(2)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024 {return Err(RepositoryError::NotFound)}
    let mut live=Vec::new();let mut terminal=Vec::new();let mut expected=BTreeSet::new();let mut ids=BTreeSet::new();
    for (id,reason,raw) in rows {
        let raw=raw.ok_or(RepositoryError::NotFound)?;
        if raw.len()>65536{return Err(RepositoryError::NotFound)} budget.check(raw.len().saturating_add(id.len())).map_err(RepositoryError::Validation)?;
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        if reason==REASON {
            let receipt:BatchReceipt=serde_json::from_value(value)?;
            let verified=verify_live_batch(tx,sql_owner,account,&receipt.workspace_id,&id,view,budget)?.ok_or(RepositoryError::NotFound)?;
            if serde_json::to_value(&verified)?!=serde_json::to_value(&receipt)? || !ids.insert(id.clone()) || !expected.insert(event(&id,KIND)){return Err(RepositoryError::NotFound)} live.push(receipt);
        } else {
            let (receipt,kind)=if reason==TERMINAL_REASON {
                let receipt:BatchTerminal=serde_json::from_value(value)?;
                if terminal_id(&receipt.batch_id)!=id{return Err(RepositoryError::NotFound)}
                (receipt,TERMINAL_KIND)
            } else {
                let receipt:BatchTerminal=serde_json::from_value(value.get("batch").cloned().ok_or(RepositoryError::NotFound)?)?;
                if id!=format!("cs_s01batch_reset_{}",sha256_hex(receipt.batch_id.as_bytes())){return Err(RepositoryError::NotFound)}
                (receipt,RESET_TERMINAL_KIND)
            };
            let verified=verify_batch_terminal(tx,sql_owner,account,&receipt.workspace_id,&receipt.batch_id,view,budget)?.ok_or(RepositoryError::NotFound)?;
            if verified!=receipt || !ids.insert(receipt.batch_id.clone()) || !expected.insert(event(&id,kind)){return Err(RepositoryError::NotFound)} terminal.push(receipt);
        }
    }
    let mut stmt=tx.prepare("SELECT event_id,length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND kind IN (?2,?3,?4) ORDER BY event_id LIMIT 1025")?;
    let rows=stmt.query_map(params![sql_owner,KIND,TERMINAL_KIND,RESET_TERMINAL_KIND],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024 {return Err(RepositoryError::NotFound)}
    for (id,size) in rows {if !(0..=65536).contains(&size){return Err(RepositoryError::NotFound)} budget.check(id.len().saturating_add(size as usize)).map_err(RepositoryError::Validation)?;if !expected.remove(&id){return Err(RepositoryError::NotFound)}}
    if !expected.is_empty(){return Err(RepositoryError::NotFound)} Ok((live,terminal))
}

/// Denial-only guard for legacy deletion/expiry, which lacks account custody.
/// It never authorizes an operation or infers an account from a stored receipt.
pub(crate) fn reject_legacy_batch_delete(conn:&Connection,candidates:&[String],curated:&[String])->Result<(),RepositoryError> {
    let version:i64=conn.query_row("PRAGMA user_version",[],|r|r.get(0))?;
    if version!=18 {return Ok(())}
    // F8 has V2 source/evidence and possibly curated history beyond legacy closure.
    // Refuse until its account-bound erasure owner has verified the whole family.
    for id in candidates {
        let f8:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1 AND source='preextracted')",[id],|r|r.get(0))?;
        if id.starts_with("preextracted_") || f8 {return Err(RepositoryError::Validation("preextracted forget unavailable".into()));}
    }
    for id in curated {
        let linked:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE source='preextracted' AND accepted_curated_id=?1) OR EXISTS(SELECT 1 FROM curated WHERE id=?1 AND source='preextracted')",[id],|r|r.get(0))?;
        if linked {return Err(RepositoryError::Validation("preextracted forget unavailable".into()));}
    }
    crate::writer_fence::require_matched_guarded_epoch(conn).map_err(|_|RepositoryError::NotFound)?;
    let mut budget=ResetFenceBudget::install(conn);
    crate::store::sqlite::knowledge_update_batch::reject_legacy_f1_delete(conn,candidates,curated,&mut budget).map_err(RepositoryError::Validation)?;
    let mut stmt=conn.prepare("SELECT owner_id,change_set_id,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE reason=?1 ORDER BY owner_id,change_set_id LIMIT 1025")?;
    let rows=stmt.query_map([REASON],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,Option<String>>(2)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024 {return Err(RepositoryError::NotFound)}
    let mut expected=std::collections::BTreeSet::new();
    for (owner,id,raw) in rows {
        let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let receipt:BatchReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        validate(&receipt)?;
        if batch_id(&receipt.account_id,&receipt.workspace_id,&receipt.request_key)?!=id || read_pair(conn,&owner,&id,REASON,KIND,&mut budget)?.as_deref()!=Some(raw.as_str()) || !expected.insert((owner,event(&id,KIND))) {return Err(RepositoryError::NotFound)}
        if receipt.items.iter().any(|item|candidates.contains(&item.candidate_id)||curated.contains(&item.curated_id)) {return Err(RepositoryError::Validation("batch member requires account-bound guarded forget".into()))}
    }
    let mut stmt=conn.prepare("SELECT owner_id,event_id FROM fm_v2_outbox WHERE kind=?1 ORDER BY owner_id,event_id LIMIT 1025")?;
    let events=stmt.query_map([KIND],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if events.len()>1024{return Err(RepositoryError::NotFound)}
    for event in events {budget.check(event.0.len().saturating_add(event.1.len())).map_err(RepositoryError::Validation)?;if !expected.remove(&event){return Err(RepositoryError::NotFound)}}
    if !expected.is_empty(){return Err(RepositoryError::NotFound)}
    let mut stmt=conn.prepare("SELECT owner_id,change_set_id,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE reason=?1 ORDER BY owner_id,change_set_id LIMIT 1025")?;
    let rows=stmt.query_map([TERMINAL_REASON],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,Option<String>>(2)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>1024{return Err(RepositoryError::NotFound)}
    let mut terminals=std::collections::BTreeSet::new();
    for (owner,id,raw) in rows {
        let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let terminal:BatchTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if terminal_id(&terminal.batch_id)!=id || read_pair(conn,&owner,&id,TERMINAL_REASON,TERMINAL_KIND,&mut budget)?.as_deref()!=Some(raw.as_str()) || !terminals.insert((owner,event(&id,TERMINAL_KIND))){return Err(RepositoryError::NotFound)}
        if terminal.members.iter().any(|m|candidates.iter().any(|v|sha256_hex(v.as_bytes())==m.candidate_sha256) || curated.iter().any(|v|sha256_hex(v.as_bytes())==m.curated_sha256)){return Err(RepositoryError::Validation("batch member requires account-bound guarded forget".into()))}
    }
    let mut stmt=conn.prepare("SELECT owner_id,event_id FROM fm_v2_outbox WHERE kind=?1 ORDER BY owner_id,event_id LIMIT 1025")?;
    let events=stmt.query_map([TERMINAL_KIND],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if events.len()>1024{return Err(RepositoryError::NotFound)}
    for event in events {budget.check(event.0.len().saturating_add(event.1.len())).map_err(RepositoryError::Validation)?;if !terminals.remove(&event){return Err(RepositoryError::NotFound)}}
    if !terminals.is_empty(){return Err(RepositoryError::NotFound)} Ok(())
}
