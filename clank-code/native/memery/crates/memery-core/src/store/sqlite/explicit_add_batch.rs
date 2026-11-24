//! One named-human, same-scope explicit-Add review batch.
use super::*;
use crate::repository::{self,PolicyPublicationRefs,ResetFenceBudget};
use crate::repository::explicit_add_batch::{self as facts,BatchReceipt,BatchItemReceipt};
use crate::repository::owner_custody::{VerifiedCustodyView,verified_or_direct_custody_view};
use serde_json::{json,Value};

#[derive(Debug,Clone,serde::Serialize,serde::Deserialize,schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct BatchSelector {pub version:u32,pub owner:String,pub workspace_id:String,pub account_id:String,pub policy_projection:PolicyPublicationRefs,pub candidate_ids:Vec<String>}
#[derive(Debug,Clone,serde::Serialize,serde::Deserialize,schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct BatchApplyRequest {pub selector:BatchSelector,pub request_key:String,pub preview_sha256:String}
#[derive(Debug,Clone,serde::Serialize,serde::Deserialize,schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct BatchStatusRequest {pub version:u32,pub owner:String,pub workspace_id:String,pub account_id:String,pub policy_projection:PolicyPublicationRefs,pub request_key:String,pub preview_sha256:String}
fn valid_sha256(v:&str)->bool {v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn invalid()->String {"atomic Add batch unavailable".into()}
fn scope(version:u32,owner:&str,workspace:&str,account:&str)->Result<(),String> {
    if version!=1 || [owner,workspace,account].iter().any(|x|x.is_empty()||x.trim()!=*x||x.len()>512||x.chars().any(char::is_control)) {return Err(invalid())} Ok(())
}
fn selector(s:&BatchSelector)->Result<(),String> {
    scope(s.version,&s.owner,&s.workspace_id,&s.account_id)?;
    if s.candidate_ids.is_empty()||s.candidate_ids.len()>100{return Err(invalid())}
    let mut seen=std::collections::BTreeSet::new();
    for id in &s.candidate_ids {if !id.strip_prefix("manualadd_").is_some_and(valid_sha256)||!seen.insert(id){return Err(invalid())}}
    if serde_json::to_vec(s).map_err(|_|invalid())?.len()>65536{return Err(invalid())} Ok(())
}
fn require_v18(tx:&Transaction<'_>)->Result<(),String> {
    crate::writer_fence::require_matched_guarded_epoch(tx).map_err(|_|invalid())?;
    if tx.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0)).map_err(|_|invalid())?!=18{return Err(invalid())} Ok(())
}
fn view<'a,'db>(tx:&'a Transaction<'db>,owner:&str,account:&str,budget:&mut ResetFenceBudget)->Result<VerifiedCustodyView<'a,'db>,String> {
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|invalid())?;
    verified_or_direct_custody_view(tx,&database,account,owner,owner,budget).map_err(|_|invalid())
}
fn item_selector(s:&BatchSelector,id:&str)->ExplicitAddReviewSelector {ExplicitAddReviewSelector{version:1,owner:s.owner.clone(),workspace_id:s.workspace_id.clone(),account_id:s.account_id.clone(),candidate_id:id.into(),policy_projection:s.policy_projection.clone()}}
fn previews(tx:&Transaction<'_>,s:&BatchSelector,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(Vec<Value>,String),String> {
    selector(s)?;repository::require_policy_projection(tx,&s.account_id,&s.workspace_id,Some(&s.policy_projection)).map_err(|_|invalid())?;
    let mut items=Vec::new();
    for id in &s.candidate_ids {
        let row=super::explicit_add::review_preview_custody(tx,&item_selector(s,id),view,budget)?;
        // The first batch profile admits only the original, unedited proposal.
        if row.get("expected_revision").and_then(Value::as_i64)!=Some(1) || row.get("state")!=Some(&json!("preview")) || row.get("candidate_id")!=Some(&json!(id)) ||
           row.get("proposal").and_then(|p|p.get("category")).and_then(Value::as_str)==Some("unknown") ||
           row.get("proposal").and_then(|p|p.get("question_context")).is_some_and(|v|!v.is_null()) {return Err(invalid())}
        items.push(row);
    }
    let material=json!({"version":1,"owner":s.owner,"workspace_id":s.workspace_id,"account_id":s.account_id,"policy_projection":s.policy_projection,"items":items});
    let bytes=serde_json::to_vec(&material).map_err(|_|invalid())?;
    if bytes.len()>65536{return Err(invalid())}
    let digest=repository::sha256_hex(&bytes);
    Ok((items,digest))
}
fn public(id:String,receipt:&BatchReceipt)->Value {
    json!({"state":"committed","batch_id":id,"items":receipt.items.iter().map(|x|json!({"candidate_id":x.candidate_id,"curated_id":x.curated_id,"block_id":x.block_id,"revision":x.revision})).collect::<Vec<_>>()})
}
pub(super) fn preview(store:&SqliteStore,s:&BatchSelector)->Result<Value,String> {
    selector(s)?;let mut conn=store.conn.lock().map_err(|_|invalid())?;let tx=conn.transaction_with_behavior(TransactionBehavior::Deferred).map_err(|_|invalid())?;
    require_v18(&tx)?;let mut budget=ResetFenceBudget::install(&tx);let custody=view(&tx,&s.owner,&s.account_id,&mut budget)?;
    let (items,digest)=previews(&tx,s,&custody,&mut budget)?;drop(custody);drop(budget);tx.commit().map_err(|_|invalid())?;
    Ok(json!({"state":"preview","items":items,"preview_sha256":digest}))
}
pub(super) fn apply(store:&SqliteStore,r:&BatchApplyRequest)->Result<Value,String> {
    selector(&r.selector)?;let s=&r.selector;
    if !valid_sha256(&r.preview_sha256){return Err(invalid())}
    let id=facts::batch_id(&s.account_id,&s.workspace_id,&r.request_key).map_err(|_|invalid())?;
    let mut conn=store.conn.lock().map_err(|_|invalid())?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_|invalid())?;
    require_v18(&tx)?;let mut budget=ResetFenceBudget::install(&tx);let custody=view(&tx,&s.owner,&s.account_id,&mut budget)?;
    // Verify the scoped typed receipt closure before any dependent effect.
    facts::collect_batch_facts(&tx,&s.owner,&s.account_id,&custody,&mut budget).map_err(|_|invalid())?;
    if facts::verify_live_batch(&tx,&s.owner,&s.account_id,&s.workspace_id,&id,&custody,&mut budget).map_err(|_|invalid())?.is_some() || facts::verify_batch_terminal(&tx,&s.owner,&s.account_id,&s.workspace_id,&id,&custody,&mut budget).map_err(|_|invalid())?.is_some(){return Err(invalid())}
    let (items,digest)=previews(&tx,s,&custody,&mut budget)?;if digest!=r.preview_sha256{return Err(invalid())}
    let mut results=Vec::new();
    for (index,row) in items.iter().enumerate() {
        let candidate=&s.candidate_ids[index];let revision=row["expected_revision"].as_i64().filter(|v|*v>0).ok_or_else(invalid)?;
        let binding=row["binding_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or_else(invalid)?;
        let review_key=facts::batch_item_key(&id,index,candidate).map_err(|_|invalid())?;
        let request=ExplicitAddReviewApplyRequest{selector:item_selector(s,candidate),expected_revision:revision,expected_binding:binding.into(),review_key:review_key.clone(),action:"accept".into()};
        let applied=super::explicit_add::review_apply_custody(&tx,&request,&custody,&mut budget)?;
        if applied["state"]!="applied" || applied["candidate_id"]!=*candidate {return Err(invalid())}
        let curated=applied["curated_id"].as_str().filter(|v|!v.is_empty()).ok_or_else(invalid)?.to_string();
        let mirror=repository::current_curated_mirror_context_in(&tx,&s.owner,&s.workspace_id,&curated).map_err(|_|invalid())?;
        let block=mirror["binding"]["block_id"].as_str().filter(|v|!v.is_empty()).ok_or_else(invalid)?.to_string();
        let applied_revision=mirror["binding"]["revision"].as_i64().filter(|v|*v>0).ok_or_else(invalid)?;
        let origin_key=mirror["origin_request_key"].as_str().filter(|v|!v.is_empty()).ok_or_else(invalid)?.to_string();
        if mirror["candidate_id"]!=*candidate{return Err(invalid())}
        results.push(BatchItemReceipt{candidate_id:candidate.clone(),expected_revision:revision,expected_binding:binding.into(),review_key,curated_id:curated,block_id:block,revision:applied_revision,origin_request_key:origin_key});
    }
    let receipt=BatchReceipt{version:"s01.explicit-add-review-batch.v1".into(),owner:s.owner.clone(),workspace_id:s.workspace_id.clone(),account_id:s.account_id.clone(),request_key:r.request_key.clone(),preview_sha256:digest,policy_projection:s.policy_projection.clone(),items:results};
    let actual=facts::write_batch(&tx,&receipt,&mut budget).map_err(|_|invalid())?;
    let checked=facts::verify_live_batch(&tx,&s.owner,&s.account_id,&s.workspace_id,&actual,&custody,&mut budget).map_err(|_|invalid())?.ok_or_else(invalid)?;
    let result=public(actual,&checked);drop(custody);drop(budget);tx.commit().map_err(|_|invalid())?;Ok(result)
}
pub(super) fn status(store:&SqliteStore,r:&BatchStatusRequest)->Result<Value,String> {
    scope(r.version,&r.owner,&r.workspace_id,&r.account_id)?;if !valid_sha256(&r.preview_sha256){return Err(invalid())}
    let id=facts::batch_id(&r.account_id,&r.workspace_id,&r.request_key).map_err(|_|invalid())?;
    let mut conn=store.conn.lock().map_err(|_|invalid())?;let tx=conn.transaction_with_behavior(TransactionBehavior::Deferred).map_err(|_|invalid())?;
    require_v18(&tx)?;let mut budget=ResetFenceBudget::install(&tx);let custody=view(&tx,&r.owner,&r.account_id,&mut budget)?;
    repository::require_policy_projection(&tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection)).map_err(|_|invalid())?;
    let live=facts::verify_live_batch(&tx,&r.owner,&r.account_id,&r.workspace_id,&id,&custody,&mut budget).map_err(|_|invalid())?;
    let terminal=facts::verify_batch_terminal(&tx,&r.owner,&r.account_id,&r.workspace_id,&id,&custody,&mut budget).map_err(|_|invalid())?;
    if live.is_some() && terminal.is_some(){return Err(invalid())}
    let result=if let Some(receipt)=live {
        if receipt.preview_sha256!=r.preview_sha256{return Err(invalid())} public(id,&receipt)
    } else if let Some(terminal)=terminal {
        if terminal.preview_sha256!=r.preview_sha256{return Err(invalid())} json!({"state":"terminal"})
    } else {json!({"state":"unknown"})};
    drop(custody);drop(budget);tx.commit().map_err(|_|invalid())?;Ok(result)
}
