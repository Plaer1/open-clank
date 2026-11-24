//! F1 owner-reviewed atomic knowledge update batch. Unadvertised until lifecycle custody is joined.
use super::*;
use crate::repository::{self,PolicyPublicationRefs,ResetFenceBudget,V2Repository};
use serde_json::{json,Value};

#[derive(Debug,Clone,serde::Serialize,serde::Deserialize,schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct UpdateBatchItem {pub memory_id:String,pub candidate_id:String,pub origin_request_key:String,pub prior_receipt_id:String,pub expected_revision:i64,pub selected_paths:Vec<String>,pub payload:Value}
#[derive(Debug,Clone,serde::Serialize,serde::Deserialize,schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct UpdateBatchPreview {pub version:u32,pub owner:String,pub workspace_id:String,pub account_id:String,pub policy_projection:PolicyPublicationRefs,pub items:Vec<UpdateBatchItem>}
#[derive(Debug,Clone,serde::Serialize,serde::Deserialize,schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct UpdateBatchApply {pub version:u32,pub owner:String,pub workspace_id:String,pub account_id:String,pub policy_projection:PolicyPublicationRefs,pub items:Vec<UpdateBatchItem>,pub request_key:String,pub preview_sha256:String}
#[derive(Debug,Clone,serde::Serialize,serde::Deserialize,schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct UpdateBatchStatus {pub version:u32,pub owner:String,pub workspace_id:String,pub account_id:String,pub policy_projection:PolicyPublicationRefs,pub request_key:String,pub preview_sha256:String}
fn invalid()->String {"atomic update batch unavailable".into()}
fn text(v:&str)->bool {!v.is_empty() && v.trim()==v && v.len()<=500 && !v.chars().any(char::is_control)}
fn scope_text(v:&str)->bool {!v.is_empty() && v.trim()==v && v.len()<=512 && !v.chars().any(char::is_control)}
fn request_key(v:&str)->bool {(22..=128).contains(&v.len()) && v.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'_' || b==b'-')}
fn sha(v:&str)->bool {v.len()==64 && v.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b))}
fn validate(r:&UpdateBatchPreview)->Result<(),String> {
    if r.version!=1 || ![&r.owner,&r.workspace_id,&r.account_id].iter().all(|v|scope_text(v)) || r.items.is_empty() || r.items.len()>100 || serde_json::to_vec(&r.items).map_err(|_|invalid())?.len()>65536 {return Err(invalid());}
    let mut ids=std::collections::BTreeSet::new();
    for i in &r.items {
        if ![&i.memory_id,&i.candidate_id,&i.origin_request_key,&i.prior_receipt_id].iter().all(|v|text(v)) || !ids.insert(&i.memory_id) || i.expected_revision<1 || i.selected_paths.is_empty() || !i.payload.is_object() {return Err(invalid());}
        let mut paths=std::collections::BTreeSet::new();
        if i.selected_paths.iter().any(|v|!matches!(v.as_str(),"/value/value"|"/confidence"|"/trust"|"/tags") || !paths.insert(v)) {return Err(invalid());}
    } Ok(())
}
fn item_key(r:&UpdateBatchPreview,index:usize)->Result<String,String> {
    let raw=serde_json::to_vec(&("s03.update-batch-item.v1",&r.account_id,&r.workspace_id,&r.policy_projection,&r.items,index)).map_err(|_|invalid())?;
    Ok(format!("batch_{}",repository::sha256_hex(&raw)))
}
fn previews(tx:&Transaction<'_>,r:&UpdateBatchPreview,budget:&mut ResetFenceBudget)->Result<(Vec<Value>,String),String> {
    validate(r)?;crate::writer_fence::require_matched_guarded_epoch(tx).map_err(|_|invalid())?;
    if !SqliteStore::native_request_profile_present(tx)? {return Err(invalid());}
    if tx.query_row("PRAGMA user_version",[],|row|row.get::<_,i64>(0)).map_err(|_|invalid())?!=18{return Err(invalid());}
    repository::require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection)).map_err(|_|invalid())?;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|invalid())?;
    let custody=repository::owner_custody::verified_or_direct_custody_view(tx,&database,&r.account_id,&r.owner,&r.owner,budget).map_err(|_|invalid())?;drop(custody);
    let mut rows=Vec::with_capacity(r.items.len());
    for (index,item) in r.items.iter().enumerate() {
        budget.check(serde_json::to_vec(item).map_err(|_|invalid())?.len()).map_err(|_|invalid())?;
        let row=V2Repository::preview_reviewed_curated_mirror_update_in_transaction(tx,&r.owner,&r.workspace_id,&item.candidate_id,&item.origin_request_key,&item.prior_receipt_id,item.expected_revision,&item_key(r,index)?,"owner approved batch update",&item.selected_paths,item.payload.clone()).map_err(|_|invalid())?;
        if row["binding"]["curated_id"]!=item.memory_id || row["expected_revision"]!=item.expected_revision {return Err(invalid());}
        rows.push(row);
    }
    let material=json!({"version":"s03.update-batch-preview.v1","owner":r.owner,"workspace_id":r.workspace_id,"account_id":r.account_id,"policy_projection":r.policy_projection,"items":r.items,"previews":rows});
    let bytes=serde_json::to_vec(&material).map_err(|_|invalid())?;budget.check(bytes.len()).map_err(|_|invalid())?;
    Ok((rows,repository::sha256_hex(&bytes)))
}
pub(super) fn preview(store:&SqliteStore,r:&UpdateBatchPreview)->Result<Value,String> {
    let mut conn=store.conn.lock().map_err(|_|invalid())?;let tx=conn.transaction_with_behavior(TransactionBehavior::Deferred).map_err(|_|invalid())?;
    store.guarded_schema_snapshot(&tx).map_err(|_|invalid())?;let mut budget=ResetFenceBudget::install(&tx);
    let (rows,digest)=previews(&tx,r,&mut budget)?;
    let items=r.items.iter().zip(rows.iter()).map(|(item,row)|json!({"memory_id":item.memory_id,"expected_revision":item.expected_revision,"changes":row["changes"]})).collect::<Vec<_>>();
    drop(budget);tx.commit().map_err(|_|invalid())?;Ok(json!({"state":"preview","preview_sha256":digest,"items":items}))
}

#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ReceiptItem {memory_id:String,candidate_id:String,origin_request_key:String,prior_receipt_id:String,expected_revision:i64,item_key:String,operation_json:String,mirror_receipt_id:String,revision:i64}
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {version:String,owner:String,workspace_id:String,account_id:String,request_key:String,preview_sha256:String,body_sha256:String,items:Vec<ReceiptItem>}
const REASON:&str="s03.knowledge-update-batch.v1";
const EVENT:&str="knowledge_update_batch";
fn batch_id(account:&str,workspace:&str,key:&str)->Result<String,String> {
    if !scope_text(account) || !scope_text(workspace) || !request_key(key){return Err(invalid());}
    Ok(format!("cs_f1_update_{}",repository::sha256_hex(&serde_json::to_vec(&(REASON,account,workspace,key)).map_err(|_|invalid())?)))
}
fn read_receipt(tx:&Connection,owner:&str,id:&str,budget:&mut ResetFenceBudget)->Result<Option<Receipt>,String> {
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=262144 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,REASON],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|invalid())?;
    let Some((digest,raw,actor,actor_id,contract,source))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,format!("outbox_{id}_{EVENT}")],|r|r.get(0)).map_err(|_|invalid())?;
        return if collision {Err(invalid())} else {Ok(None)};
    };
    let raw=raw.ok_or_else(invalid)?;budget.check(raw.len()).map_err(|_|invalid())?;
    let receipt:Receipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?).map_err(|_|invalid())?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=262144 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,format!("outbox_{id}_{EVENT}"),id,EVENT],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|invalid())?;
    if digest!=repository::sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=receipt.owner || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || out!=Some((Some(raw),0)) || receipt.version!=REASON || !sha(&receipt.preview_sha256) || !sha(&receipt.body_sha256) || receipt.items.is_empty() || receipt.items.len()>100 || batch_id(&receipt.account_id,&receipt.workspace_id,&receipt.request_key)?!=id {return Err(invalid());}
    Ok(Some(receipt))
}
fn public(id:&str,r:&Receipt)->Value {json!({"state":"committed","batch_id":id,"items":r.items.iter().map(|i|json!({"memory_id":i.memory_id,"revision":i.revision})).collect::<Vec<_>>()})}
fn verify_items(tx:&Transaction<'_>,owner:&str,r:&Receipt,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),String> {
    let proof=view.for_captured(tx,&r.owner).map_err(|_|invalid())?;
    if proof.current_owner()!=owner || proof.account_id()!=r.account_id{return Err(invalid());}
    for item in &r.items {
        budget.check(item.operation_json.len()).map_err(|_|invalid())?;
        let receipt:repository::MirrorUpdateReceipt=serde_json::from_value(repository::read_mirror_receipt_metadata(tx,owner,&item.mirror_receipt_id).map_err(|_|"batch mirror receipt unavailable")?).map_err(|_|invalid())?;
        if receipt.owner!=r.owner || receipt.workspace!=r.workspace_id || receipt.origin_candidate_id!=item.candidate_id || receipt.origin_request_key!=item.origin_request_key || receipt.prior_receipt_id!=item.prior_receipt_id || receipt.expected_revision!=item.expected_revision || receipt.update_request_key!=item.item_key || receipt.operation_json!=item.operation_json || receipt.binding.curated_id!=item.memory_id || receipt.binding.revision!=item.revision {return Err(invalid());}
        let block=&receipt.binding.block_id;
        if receipt.version!="s03.mirror-update.v1" || receipt.operation_sha256!=repository::sha256_hex(receipt.operation_json.as_bytes()) || repository::mirror_update_receipt_id(&r.owner,&r.workspace_id,block,&item.item_key).map_err(|_|invalid())?!=item.mirror_receipt_id {return Err(invalid());}
        let metadata:Option<(String,String,String,Option<String>)>=tx.query_row("SELECT actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='reviewed mirror update'",params![owner,item.mirror_receipt_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?))).optional().map_err(|_|invalid())?;
        let (actor,actor_id,contract,source)=metadata.ok_or_else(invalid)?;
        if actor!="user" || actor_id!=r.owner || contract!=crate::ontology::CONTRACT_VERSION {return Err(invalid());}
        let event=json!({"block_id":block,"curated_id":item.memory_id,"revision":item.revision});
        let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='mirror_update'",params![owner,format!("outbox_{}_mirror_update",item.mirror_receipt_id),item.mirror_receipt_id],|row|Ok((row.get(0)?,row.get(1)?))).optional().map_err(|_|invalid())?;
        let (raw,revision)=out.ok_or_else(invalid)?;budget.check(raw.len()).map_err(|_|invalid())?;
        if revision!=item.revision || crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?!=event {return Err(invalid());}
        let operation=crate::field_authority::parse_unique_json(&item.operation_json).map_err(|_|invalid())?;
        if operation.as_object().map(|v|v.len())!=Some(14) || operation["version"]!="s03.published-update.v1" || operation["owner"]!=r.owner || operation["workspace"]!=r.workspace_id || operation["project"]!="" || operation["block_id"]!=*block || operation["expected_revision"]!=item.expected_revision || operation["request_key"]!=item.item_key || operation["actor_type"]!="user" || operation["actor_id"]!=r.owner || operation["reason"]!="owner approved batch update" || !operation["source_event_id"].is_null() || !operation["producer"].is_null() {return Err(invalid());}
        let (historical,_)=repository::guarded_replay_custody(tx,&r.workspace_id,"",block,&receipt.guarded_change_set_id,&receipt.operation_json,&receipt.operation_sha256,&r.owner,view).map_err(|_|invalid())?.ok_or_else(invalid)?;
        if historical.revision!=item.revision || historical.identity.claim_slot!=item.memory_id || historical.actor_type!="user" || historical.actor_id!=r.owner || historical.change_set_id!=receipt.guarded_change_set_id || historical.source_event_id!=source {return Err(invalid());}
    } Ok(())
}
pub(super) fn apply(store:&SqliteStore,r:&UpdateBatchApply)->Result<Value,String> {
    if !request_key(&r.request_key) || !sha(&r.preview_sha256){return Err(invalid());}
    let input=UpdateBatchPreview{version:r.version,owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),account_id:r.account_id.clone(),policy_projection:r.policy_projection.clone(),items:r.items.clone()};validate(&input)?;
    let id=batch_id(&r.account_id,&r.workspace_id,&r.request_key)?;
    let body_sha=repository::sha256_hex(&serde_json::to_vec(&("s03.update-batch-body.v1",&input)).map_err(|_|invalid())?);
    let mut conn=store.conn.lock().map_err(|_|invalid())?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_|invalid())?;
    store.guarded_schema_snapshot(&tx).map_err(|_|invalid())?;crate::writer_fence::require_matched_guarded_epoch(&tx).map_err(|_|invalid())?;
    if !SqliteStore::native_request_profile_present(&tx)? {return Err(invalid());}
    repository::require_policy_projection(&tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection)).map_err(|_|invalid())?;
    let mut budget=ResetFenceBudget::install(&tx);
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|invalid())?;
    let custody=repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,&r.owner,&r.owner,&mut budget).map_err(|_|invalid())?;
    if purge_terminal_exists(store,&tx,&database,&r.account_id,request_sha(&id)?,&mut budget)? {return Err(invalid());}
    if read_terminal(&tx,&r.owner,request_sha(&id)?,&custody,&mut budget)?.is_some() {return Err(invalid());}
    if let Some(existing)=read_receipt(&tx,&r.owner,&id,&mut budget)? {
        if existing.workspace_id!=r.workspace_id || existing.account_id!=r.account_id || existing.request_key!=r.request_key || existing.preview_sha256!=r.preview_sha256 || existing.body_sha256!=body_sha{return Err(invalid());}
        verify_items(&tx,&r.owner,&existing,&custody,&mut budget)?;let result=public(&id,&existing);drop(custody);drop(budget);tx.commit().map_err(|_|invalid())?;return Ok(result);
    }
    drop(custody);
    let (rows,digest)=previews(&tx,&input,&mut budget)?;if digest!=r.preview_sha256{return Err(invalid());}
    let mut items=Vec::with_capacity(input.items.len());
    for (index,(item,row)) in input.items.iter().zip(rows.iter()).enumerate() {
        let operation=row["operation_json"].as_str().ok_or_else(invalid)?;let op_hash=row["operation_sha256"].as_str().filter(|v|sha(v)).ok_or_else(invalid)?;
        let key=item_key(&input,index)?;let actor=repository::RevisionActor{actor_type:"user".into(),actor_id:r.owner.clone(),reason:"owner approved batch update".into(),source_event_id:None};
        let review=crate::field_authority::OwnerReviewContext{authenticated_owner:r.owner.clone(),reviewer_id:r.owner.clone(),expected_revision:item.expected_revision,proposal_key:key.clone(),payload_hash:op_hash.into(),approved_paths:item.selected_paths.clone()};
        let (revision,_)=V2Repository::reviewed_curated_mirror_update_in_transaction(&tx,&r.owner,&r.workspace_id,&item.candidate_id,&item.origin_request_key,&item.prior_receipt_id,item.expected_revision,&key,operation,&actor,&review).map_err(|_|invalid())?;
        let receipt_id=repository::mirror_update_receipt_id(&r.owner,&r.workspace_id,&revision.identity.block_id,&key).map_err(|_|invalid())?;
        items.push(ReceiptItem{memory_id:item.memory_id.clone(),candidate_id:item.candidate_id.clone(),origin_request_key:item.origin_request_key.clone(),prior_receipt_id:item.prior_receipt_id.clone(),expected_revision:item.expected_revision,item_key:key,operation_json:operation.into(),mirror_receipt_id:receipt_id,revision:revision.revision});
    }
    let receipt=Receipt{version:REASON.into(),owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),account_id:r.account_id.clone(),request_key:r.request_key.clone(),preview_sha256:digest,body_sha256:body_sha,items};
    let raw=serde_json::to_string(&receipt).map_err(|_|invalid())?;budget.check(raw.len()).map_err(|_|invalid())?;if raw.len()>262144{return Err(invalid());}
    let now=chrono::Utc::now().to_rfc3339();tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?1,?3,NULL,?4,?5,?6,?7)",params![r.owner,id,REASON,crate::ontology::CONTRACT_VERSION,repository::sha256_hex(raw.as_bytes()),now,raw]).map_err(|_|invalid())?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![r.owner,format!("outbox_{id}_{EVENT}"),id,EVENT,raw,now]).map_err(|_|invalid())?;
    let result=public(&id,&receipt);drop(budget);tx.commit().map_err(|_|invalid())?;Ok(result)
}
pub(super) fn status(store:&SqliteStore,r:&UpdateBatchStatus)->Result<Value,String> {
    if r.version!=1 || (![&r.owner,&r.workspace_id,&r.account_id].iter().all(|v|scope_text(v)) || !request_key(&r.request_key)) || !sha(&r.preview_sha256){return Err(invalid());}
    let id=batch_id(&r.account_id,&r.workspace_id,&r.request_key)?;let mut conn=store.conn.lock().map_err(|_|invalid())?;let tx=conn.transaction_with_behavior(TransactionBehavior::Deferred).map_err(|_|invalid())?;
    store.guarded_schema_snapshot(&tx).map_err(|_|invalid())?;crate::writer_fence::require_matched_guarded_epoch(&tx).map_err(|_|invalid())?;
    if !SqliteStore::native_request_profile_present(&tx)? {return Err(invalid());}
    repository::require_policy_projection(&tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection)).map_err(|_|invalid())?;let mut budget=ResetFenceBudget::install(&tx);
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|invalid())?;
    let custody=repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,&r.owner,&r.owner,&mut budget).map_err(|_|invalid())?;
    if purge_terminal_exists(store,&tx,&database,&r.account_id,request_sha(&id)?,&mut budget)? {return Err(invalid());}
    let terminal=read_terminal(&tx,&r.owner,request_sha(&id)?,&custody,&mut budget)?;
    let live=read_receipt(&tx,&r.owner,&id,&mut budget)?;
    let result=match (live,terminal) {
        (None,None)=>json!({"state":"unknown"}),
        (None,Some(t))=>{if t.account_id!=r.account_id || t.workspace_id!=r.workspace_id || t.preview_sha256!=r.preview_sha256{return Err(invalid());}json!({"state":"terminal"})},
        (Some(receipt),None)=>{if receipt.account_id!=r.account_id || receipt.workspace_id!=r.workspace_id || receipt.request_key!=r.request_key || receipt.preview_sha256!=r.preview_sha256{return Err(invalid());}verify_items(&tx,&r.owner,&receipt,&custody,&mut budget)?;public(&id,&receipt)},
        _=>return Err(invalid()),
    };
    drop(custody);drop(budget);tx.commit().map_err(|_|invalid())?;Ok(result)
}

const TERMINAL_REASON:&str="s03.knowledge-update-batch-terminal.v1";
const TERMINAL_EVENT:&str="knowledge_update_batch_terminal";
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Terminal {version:String,owner:String,workspace_id:String,account_id:String,request_sha256:String,body_sha256:String,preview_sha256:String,lifecycle_kind:String,lifecycle_receipt_id:String,erased_memory_sha256:Vec<String>}
fn request_sha(id:&str)->Result<&str,String> {id.strip_prefix("cs_f1_update_").filter(|v|sha(v)).ok_or_else(invalid)}
fn terminal_id(request:&str)->Result<String,String> {if !sha(request){return Err(invalid());}Ok(format!("cs_f1_terminal_{request}"))}
fn read_terminal(tx:&Transaction<'_>,owner:&str,request:&str,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Option<Terminal>,String> {
    let id=terminal_id(request)?;let event=format!("outbox_{id}_{TERMINAL_EVENT}");
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,TERMINAL_REASON],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|invalid())?;
    let Some((digest,raw,actor,actor_id,contract,source))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,event],|r|r.get(0)).map_err(|_|invalid())?;
        return if collision {Err(invalid())} else {Ok(None)};
    };
    let raw=raw.ok_or_else(invalid)?;budget.check(raw.len()).map_err(|_|invalid())?;
    let t:Terminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?).map_err(|_|invalid())?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,TERMINAL_EVENT],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|invalid())?;
    let proof=view.for_captured(tx,&t.owner).map_err(|_|invalid())?;
    if digest!=repository::sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=t.owner || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || out!=Some((Some(raw),0)) || t.version!=TERMINAL_REASON || t.request_sha256!=request || !sha(&t.body_sha256) || !sha(&t.preview_sha256) || !text(&t.workspace_id) || !text(&t.account_id) || proof.current_owner()!=owner || proof.account_id()!=t.account_id || !matches!(t.lifecycle_kind.as_str(),"forget"|"reset") || t.erased_memory_sha256.iter().any(|v|!sha(v)) || t.erased_memory_sha256.len()>100 {return Err(invalid());}
    if t.lifecycle_kind=="forget" {
        let receipt=repository::guarded_batch_forget::verify_receipt_by_id(tx,owner,&t.account_id,&t.workspace_id,&t.lifecycle_receipt_id,view,budget).map_err(|_|invalid())?;
        let raw:String=tx.query_row("SELECT affected_ids FROM memory_tombstones WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,t.workspace_id,receipt.tombstone_id],|r|r.get(0)).map_err(|_|invalid())?;
        budget.check(raw.len()).map_err(|_|invalid())?;if repository::sha256_hex(raw.as_bytes())!=receipt.closure_sha256{return Err(invalid());}
        let closure:ForgetClosure=serde_json::from_str(&raw).map_err(|_|invalid())?;let actual:std::collections::BTreeSet<_>=closure.curated_ids.iter().map(|id|repository::sha256_hex(id.as_bytes())).collect();
        if t.erased_memory_sha256.is_empty() || t.erased_memory_sha256.iter().any(|v|!actual.contains(v)) {return Err(invalid());}
    } else {
        if !t.erased_memory_sha256.is_empty(){return Err(invalid());}
        repository::guarded_batch_forget::verify_full_reset_receipt_by_id(tx,owner,&t.account_id,&t.lifecycle_receipt_id,view,budget).map_err(|_|invalid())?;
    } Ok(Some(t))
}
fn store_terminal(tx:&Transaction<'_>,owner:&str,t:&Terminal)->Result<(),String> {
    let id=terminal_id(&t.request_sha256)?;let raw=serde_json::to_string(t).map_err(|_|invalid())?;if raw.len()>65536{return Err(invalid());}let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![owner,id,t.owner,TERMINAL_REASON,crate::ontology::CONTRACT_VERSION,repository::sha256_hex(raw.as_bytes()),now,raw]).map_err(|_|invalid())?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![owner,format!("outbox_{id}_{TERMINAL_EVENT}"),id,TERMINAL_EVENT,raw,now]).map_err(|_|invalid())?;Ok(())
}
fn delete_pair(tx:&Transaction<'_>,owner:&str,id:&str,reason:&str,event:&str)->Result<(),String> {
    if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,format!("outbox_{id}_{event}"),id,event]).map_err(|_|invalid())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,reason]).map_err(|_|invalid())?!=1 {return Err(invalid());} Ok(())
}

/// Inventory every scoped batch proof before any lifecycle mutation.
pub(super) struct Inventory {live:Vec<(String,Receipt)>, terminal:Vec<Terminal>}
pub(super) fn collect(tx:&Transaction<'_>,owner:&str,account:&str,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Inventory,String> {
    let mut stmt=tx.prepare("SELECT change_set_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND (reason IN (?2,?3) OR change_set_id GLOB 'cs_f1_update_*' OR change_set_id GLOB 'cs_f1_terminal_*') ORDER BY change_set_id").map_err(|_|invalid())?;
    let mut cursor=stmt.query(params![owner,REASON,TERMINAL_REASON]).map_err(|_|invalid())?;let mut rows=Vec::new();
    while let Some(row)=cursor.next().map_err(|_|invalid())? {
        let id:String=row.get(0).map_err(|_|invalid())?;let reason:String=row.get(1).map_err(|_|invalid())?;
        budget.check(id.len()+reason.len()).map_err(|_|invalid())?;rows.push((id,reason));
    } drop(cursor);drop(stmt);
    let mut live=Vec::new();let mut terminal=Vec::new();let mut requests=std::collections::BTreeSet::new();let mut expected=std::collections::BTreeSet::new();
    for (id,reason) in rows {
        if reason==REASON {
            let key=request_sha(&id)?.to_string();let r=read_receipt(tx,owner,&id,budget)?.ok_or_else(invalid)?;
            if r.account_id!=account || !requests.insert(key) {return Err(invalid());}
            verify_items(tx,owner,&r,view,budget)?;expected.insert(format!("outbox_{id}_{EVENT}"));live.push((id,r));
        } else if reason==TERMINAL_REASON {
            let key=id.strip_prefix("cs_f1_terminal_").filter(|v|sha(v)).ok_or_else(invalid)?;
            let t=read_terminal(tx,owner,key,view,budget)?.ok_or_else(invalid)?;
            if t.account_id!=account || !requests.insert(key.to_string()){return Err(invalid());}
            expected.insert(format!("outbox_{id}_{TERMINAL_EVENT}"));terminal.push(t);
        } else {return Err(invalid());}
    }
    let mut stmt=tx.prepare("SELECT event_id FROM fm_v2_outbox WHERE owner_id=?1 AND (kind IN (?2,?3) OR event_id GLOB 'outbox_cs_f1_update_*' OR event_id GLOB 'outbox_cs_f1_terminal_*' OR change_set_id GLOB 'cs_f1_update_*' OR change_set_id GLOB 'cs_f1_terminal_*') ORDER BY event_id").map_err(|_|invalid())?;
    let mut rows=stmt.query(params![owner,EVENT,TERMINAL_EVENT]).map_err(|_|invalid())?;
    while let Some(row)=rows.next().map_err(|_|invalid())? {let id:String=row.get(0).map_err(|_|invalid())?;budget.check(id.len()).map_err(|_|invalid())?;if !expected.remove(&id){return Err(invalid());}}
    if !expected.is_empty(){return Err(invalid());}Ok(Inventory{live,terminal})
}
pub(super) fn is_empty(inv:&Inventory)->bool {inv.live.is_empty() && inv.terminal.is_empty()}
/// No existing guarded erasure planner proves the revised F1 member closure.
/// Refuse before showing a preview; no receipt or memory is changed.
pub(super) fn unqualified_forget(inv:&Inventory,closure:&ForgetClosure)->bool {
    inv.live.iter().any(|(_,r)|r.items.iter().any(|i|closure.curated_ids.contains(&i.memory_id) || closure.candidate_ids.contains(&i.candidate_id)))
}
pub(super) fn purge_hashes(inv:&Inventory)->Result<Vec<super::request_purge::RequestHash>,String> {
    let mut map=std::collections::BTreeMap::new();
    for (id,r) in &inv.live {if map.insert(request_sha(id)?.to_string(),Some(r.body_sha256.clone())).is_some(){return Err(invalid());}}
    for t in &inv.terminal {if map.insert(t.request_sha256.clone(),Some(t.body_sha256.clone())).is_some(){return Err(invalid());}}
    Ok(map.into_iter().map(|(request_sha256,body_sha256)|super::request_purge::RequestHash{request_sha256,body_sha256}).collect())
}
/// After the guarded forget receipt is written, terminalize only intersecting
/// batch receipts; preserve other single-edit receipts and memories.
pub(super) fn finish_forget(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,receipt_id:&str,closure:&ForgetClosure,inv:Inventory,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),String> {
    let forget=repository::guarded_batch_forget::verify_receipt_by_id(tx,owner,account,workspace,receipt_id,view,budget).map_err(|_|invalid())?;
    let raw=serde_json::to_string(closure).map_err(|_|invalid())?;if repository::sha256_hex(raw.as_bytes())!=forget.closure_sha256{return Err(invalid());}
    let selected:std::collections::BTreeSet<_>=closure.curated_ids.iter().collect();
    for (id,r) in inv.live {
        let erased:Vec<String>=r.items.iter().filter(|i|selected.contains(&i.memory_id)).map(|i|repository::sha256_hex(i.memory_id.as_bytes())).collect();
        if erased.is_empty(){continue;}if r.workspace_id!=workspace{return Err(invalid());}
        let key=request_sha(&id)?.to_string();delete_pair(tx,owner,&id,REASON,EVENT)?;
        store_terminal(tx,owner,&Terminal{version:TERMINAL_REASON.into(),owner:r.owner,workspace_id:r.workspace_id,account_id:r.account_id,request_sha256:key,body_sha256:r.body_sha256,preview_sha256:r.preview_sha256,lifecycle_kind:"forget".into(),lifecycle_receipt_id:receipt_id.into(),erased_memory_sha256:erased})?;
    } Ok(())
}
/// Rebase all verified historical status proofs to the real full-reset receipt.
pub(super) fn finish_full_reset(tx:&Transaction<'_>,owner:&str,account:&str,receipt_id:&str,inv:Inventory,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),String> {
    repository::guarded_batch_forget::verify_full_reset_receipt_by_id(tx,owner,account,receipt_id,view,budget).map_err(|_|invalid())?;
    for (id,r) in inv.live {
        let key=request_sha(&id)?.to_string();delete_pair(tx,owner,&id,REASON,EVENT)?;
        store_terminal(tx,owner,&Terminal{version:TERMINAL_REASON.into(),owner:r.owner,workspace_id:r.workspace_id,account_id:r.account_id,request_sha256:key,body_sha256:r.body_sha256,preview_sha256:r.preview_sha256,lifecycle_kind:"reset".into(),lifecycle_receipt_id:receipt_id.into(),erased_memory_sha256:Vec::new()})?;
    }
    for mut t in inv.terminal {
        let id=terminal_id(&t.request_sha256)?;delete_pair(tx,owner,&id,TERMINAL_REASON,TERMINAL_EVENT)?;
        t.lifecycle_kind="reset".into();t.lifecycle_receipt_id=receipt_id.into();t.erased_memory_sha256.clear();store_terminal(tx,owner,&t)?;
    } Ok(())
}

fn purge_terminal_exists(store:&SqliteStore,tx:&Transaction<'_>,database:&str,account:&str,key:&str,budget:&mut ResetFenceBudget)->Result<bool,String> {
    if !SqliteStore::native_request_profile_present(tx)? || !sha(key){return Err(invalid());}store.guarded_schema_snapshot(tx).map_err(|_|invalid())?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::F1,database,account)?;
    let id:Option<String>=tx.query_row("SELECT purge_receipt_id FROM native_request_purge_terminal WHERE family='knowledge_update_batch_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,key],|r|r.get(0)).optional().map_err(|_|invalid())?;
    let Some(id)=id else {return Ok(false)};if !id.strip_prefix("owner_transition_").is_some_and(sha){return Err(invalid());}
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=65536",[&id],|r|r.get(0)).map_err(|_|invalid())?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?;let req=value.get("request_sha256").and_then(Value::as_str).filter(|v|sha(v)).ok_or_else(invalid)?;
    SqliteStore::validate_purge_receipt_record_with_budget(tx,&value,&id,req,database,account,budget).map_err(|_|invalid())?;
    store.read_owned_schema_fact(tx,&id,&repository::sha256_hex(raw.as_bytes()),req,database).map_err(|_|invalid())?;budget.check(raw.len()).map_err(|_|invalid())?;Ok(true)
}

pub(super) fn suspicious(tx:&Transaction<'_>,owner:&str)->Result<bool,String> {
    tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND (reason IN (?2,?3) OR change_set_id GLOB 'cs_f1_update_*' OR change_set_id GLOB 'cs_f1_terminal_*')) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND (kind IN (?4,?5) OR event_id GLOB 'outbox_cs_f1_update_*' OR event_id GLOB 'outbox_cs_f1_terminal_*' OR change_set_id GLOB 'cs_f1_update_*' OR change_set_id GLOB 'cs_f1_terminal_*'))",params![owner,REASON,TERMINAL_REASON,EVENT,TERMINAL_EVENT],|r|r.get(0)).map_err(|_|invalid())
}
pub(crate) fn assess_rename_f1(tx:&Transaction<'_>,owner:&str,account:&str,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),String> {
    if !SqliteStore::native_request_profile_present(tx)? {return if suspicious(tx,owner)? {Err(invalid())} else {Ok(())};}
    let _=collect(tx,owner,account,view,budget)?;Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn retained_edited_forget_refuses_without_mutation() -> Result<(),Box<dyn std::error::Error>> {
        let path=std::env::var("OPENCLANK_F1_FIXTURE_DB")?;if !path.starts_with("/tmp/native-ops-fixture-"){return Err("explicit disposable fixture required".into());}
        let database=std::env::var("OPENCLANK_F1_FIXTURE_ID")?;let input:UpdateBatchPreview=serde_json::from_str(&std::env::var("OPENCLANK_F1_INPUT")?)?;
        let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
        let digest={let conn=store.conn.lock().map_err(|_|"lock")?;let raw:String=conn.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id='alice' AND reason=?1",[REASON],|r|r.get(0))?;let receipt:Receipt=serde_json::from_str(&raw)?;receipt.preview_sha256};
        let status_req=UpdateBatchStatus{version:1,owner:input.owner.clone(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),request_key:"f1_forget_test_abcdefghijklmnop".into(),preview_sha256:digest};
        let before=status(&store,&status_req)?;
        {let conn=store.conn.lock().map_err(|_|"lock")?;let mut budget=ResetFenceBudget::install(&conn);assert!(reject_legacy_f1_delete(&conn,&[],&[input.items[0].memory_id.clone()],&mut budget).is_err());}
        let mut forget=GuardedBatchForgetRequest{version:1,owner:input.owner.clone(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),action:"preview".into(),selector_kind:Some("record_id".into()),selector:Some(input.items[0].memory_id.clone()),preview_token:None,tombstone_id:None,operation_id:None};
        assert_eq!(store.guarded_batch_forget_preview(&forget)?,json!({"state":"unsupported","reason":"f1_edited_head"}));assert_eq!(status(&store,&status_req)?,before);
        {let conn=store.conn.lock().map_err(|_|"lock")?;for item in &input.items {let n:i64=conn.query_row("SELECT count(*) FROM curated WHERE owner=?1 AND id=?2",params![input.owner,item.memory_id],|r|r.get(0))?;assert_eq!(n,1);}}Ok(())
    }

    #[test]
    fn stale_batch_is_atomic_and_edited_forget_refuses() -> Result<(),Box<dyn std::error::Error>> {
        let path=std::env::var("OPENCLANK_F1_FIXTURE_DB")?;if !path.starts_with("/tmp/native-ops-fixture-"){return Err("explicit disposable fixture required".into());}
        let database=std::env::var("OPENCLANK_F1_FIXTURE_ID")?;let mut input:UpdateBatchPreview=serde_json::from_str(&std::env::var("OPENCLANK_F1_INPUT")?)?;
        let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
        let first=preview(&store,&input)?;let stale_digest=first["preview_sha256"].as_str().ok_or("digest")?.to_string();
        let second=&input.items[1];let edit_key="concurrent-owner-edit";
        let (new_id,new_revision)={let mut conn=store.conn.lock().map_err(|_|"lock")?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let row=V2Repository::preview_reviewed_curated_mirror_update_in_transaction(&tx,&input.owner,&input.workspace_id,&second.candidate_id,&second.origin_request_key,&second.prior_receipt_id,second.expected_revision,edit_key,"owner edit",&second.selected_paths,json!({"value":{"type":"string","value":"concurrent update"}})).map_err(|e|format!("single preview: {e}"))?;
            let operation=row["operation_json"].as_str().ok_or("operation")?;let hash=row["operation_sha256"].as_str().ok_or("hash")?;
            let actor=repository::RevisionActor{actor_type:"user".into(),actor_id:input.owner.clone(),reason:"owner edit".into(),source_event_id:None};
            let review=crate::field_authority::OwnerReviewContext{authenticated_owner:input.owner.clone(),reviewer_id:input.owner.clone(),expected_revision:second.expected_revision,proposal_key:edit_key.into(),payload_hash:hash.into(),approved_paths:second.selected_paths.clone()};
            let (revision,_)=V2Repository::reviewed_curated_mirror_update_in_transaction(&tx,&input.owner,&input.workspace_id,&second.candidate_id,&second.origin_request_key,&second.prior_receipt_id,second.expected_revision,edit_key,operation,&actor,&review).map_err(|e|format!("single edit: {e}"))?;
            let id=repository::mirror_update_receipt_id(&input.owner,&input.workspace_id,&revision.identity.block_id,edit_key)?;let n=revision.revision;tx.commit()?;(id,n)};
        let stale=UpdateBatchApply{version:1,owner:input.owner.clone(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),items:input.items.clone(),request_key:"f1_stale_test_abcdefghijklmnop".into(),preview_sha256:stale_digest};
        assert!(apply(&store,&stale).is_err());
        {let conn=store.conn.lock().map_err(|_|"lock")?;let n:i64=conn.query_row("SELECT current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND claim_slot=?2",params![input.owner,input.items[0].memory_id],|r|r.get(0))?;assert_eq!(n,input.items[0].expected_revision);}
        input.items[1].prior_receipt_id=new_id;input.items[1].expected_revision=new_revision;
        let planned=preview(&store,&input)?;let request=UpdateBatchApply{items:input.items.clone(),request_key:"f1_forget_test_abcdefghijklmnop".into(),preview_sha256:planned["preview_sha256"].as_str().ok_or("digest")?.into(),..stale};
        let committed=apply(&store,&request).map_err(|e|format!("apply: {e}"))?;
        {let conn=store.conn.lock().map_err(|_|"lock")?;let mut budget=ResetFenceBudget::install(&conn);assert!(reject_legacy_f1_delete(&conn,&[],&[input.items[0].memory_id.clone()],&mut budget).is_err());}
        let mut forget=GuardedBatchForgetRequest{version:1,owner:input.owner.clone(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),action:"preview".into(),selector_kind:Some("record_id".into()),selector:Some(input.items[0].memory_id.clone()),preview_token:None,tombstone_id:None,operation_id:None};
        assert_eq!(store.guarded_batch_forget_preview(&forget)?,json!({"state":"unsupported","reason":"f1_edited_head"}));
        let status_req=UpdateBatchStatus{version:1,owner:input.owner.clone(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),request_key:request.request_key.clone(),preview_sha256:request.preview_sha256.clone()};
        assert_eq!(status(&store,&status_req)?,committed);assert_eq!(apply(&store,&request)?,committed);
        {let conn=store.conn.lock().map_err(|_|"lock")?;for item in &input.items {let n:i64=conn.query_row("SELECT count(*) FROM curated WHERE owner=?1 AND id=?2",params![input.owner,item.memory_id],|r|r.get(0))?;assert_eq!(n,1);}}
        assert_eq!(committed["items"].as_array().ok_or("items")?.len(),2);Ok(())
    }

    #[test]
    fn retained_renamed_original_status() -> Result<(),Box<dyn std::error::Error>> {
        let path=std::env::var("OPENCLANK_F1_FIXTURE_DB")?;if !path.starts_with("/tmp/native-ops-fixture-"){return Err("explicit disposable fixture required".into());}
        let database=std::env::var("OPENCLANK_F1_FIXTURE_ID")?;let input:UpdateBatchPreview=serde_json::from_str(&std::env::var("OPENCLANK_F1_INPUT")?)?;
        let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
        let digest={let conn=store.conn.lock().map_err(|_|"lock")?;let raw:String=conn.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id='bob' AND reason=?1",[REASON],|r|r.get(0))?;let receipt:Receipt=serde_json::from_str(&raw)?;receipt.preview_sha256};
        let r=UpdateBatchStatus{version:1,owner:"bob".into(),workspace_id:input.workspace_id,account_id:input.account_id,policy_projection:input.policy_projection,request_key:"f1_original_abcdefghijklmnop".into(),preview_sha256:digest};
        assert_eq!(status(&store,&r).map_err(|e|format!("renamed status: {e}"))?["state"],"committed");Ok(())
    }

    #[test]
    fn retained_full_reset_and_dual_family_purge() -> Result<(),Box<dyn std::error::Error>> {
        let path=std::env::var("OPENCLANK_F1_FIXTURE_DB")?;if !path.starts_with("/tmp/native-ops-fixture-"){return Err("explicit disposable fixture required".into());}
        let database=std::env::var("OPENCLANK_F1_FIXTURE_ID")?;let input:UpdateBatchPreview=serde_json::from_str(&std::env::var("OPENCLANK_F1_INPUT")?)?;
        let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
        let digest={let conn=store.conn.lock().map_err(|_|"lock")?;let raw:String=conn.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id='bob' AND reason=?1",[REASON],|r|r.get(0))?;let receipt:Receipt=serde_json::from_str(&raw)?;receipt.preview_sha256};
        let status_req=UpdateBatchStatus{version:1,owner:"bob".into(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),request_key:"f1_original_abcdefghijklmnop".into(),preview_sha256:digest.clone()};
        let apply_req=UpdateBatchApply{version:1,owner:"bob".into(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),items:input.items.clone(),request_key:"f1_original_abcdefghijklmnop".into(),preview_sha256:digest};
        {let mut conn=store.conn.lock().map_err(|_|"lock")?;let tx=conn.transaction()?;let mut budget=ResetFenceBudget::install(&tx);
         let view=repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&input.account_id,"bob","bob",&mut budget).map_err(|e|format!("inventory custody: {e}"))?;
         repository::explicit_add_batch::collect_batch_facts(&tx,"bob",&input.account_id,&view,&mut budget).map_err(|e|format!("pre-reset add batch inventory: {e}"))?;}
        let components=vec!["memories".to_string()];let plan=store.reset_owner("reset_preview","bob",&components,None)?;
        store.guarded_reset_owner("bob",&input.workspace_id,&input.account_id,"f1-reset",&components,&plan["components"],false).map_err(|e|format!("reset: {e}"))?;
        assert_eq!(status(&store,&status_req).map_err(|e|format!("reset status: {e}"))?["state"],"terminal");assert!(apply(&store,&apply_req).is_err());
        let f8=PreextractedStageRequest{version:1,owner:"bob".into(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),request_key:"f8-co-purge".into(),items:vec![PreextractedItem{kind:"fact".into(),text:"synthetic co-purge".into(),source:PreextractedSource{uri:"fixture://co-purge".into(),revision:1,content_sha256:"a".repeat(64)},target:None}]};store.preextracted_stage(&f8)?;
        let count=store.owner_counts("bob")?;let purge=GuardedOwnerPurgeRequest{version:1,database_id:database,account_id:input.account_id.clone(),operation_id:"f1-purge".into(),step:"memory_fm".into(),generation:1,action:"purge".into(),owner:"bob".into(),expected_source_fingerprint:count["fingerprint"].as_str().ok_or("fingerprint")?.into()};
        let purged=store.guarded_purge_owner(&purge).map_err(|e|format!("purge: {e}"))?;assert_eq!(purged["version"],1);assert_eq!(purged["result"].as_object().ok_or("result")?.len(),3);assert_eq!(store.guarded_purge_status(&purge)?,purged);store.guarded_purge_stamp(&purge)?;
        {let conn=store.conn.lock().map_err(|_|"lock")?;let raw:String=conn.query_row("SELECT value FROM fm_meta WHERE key=?1",[purged["receipt_id"].as_str().ok_or("receipt")?],|r|r.get(0))?;let v:Value=serde_json::from_str(&raw)?;assert_eq!(v["version"],3);assert!(v["f8"].is_object() && v["f1"].is_object());}
        assert!(status(&store,&status_req).is_err());assert!(apply(&store,&apply_req).is_err());assert!(store.preextracted_stage(&f8).is_err());Ok(())
    }

    #[test]
    fn actual_owner_atomic_replay_rename_reset_and_purge() -> Result<(),Box<dyn std::error::Error>> {
        let path=std::env::var("OPENCLANK_F1_FIXTURE_DB")?;if !path.starts_with("/tmp/native-ops-fixture-"){return Err("explicit disposable fixture required".into());}
        let database=std::env::var("OPENCLANK_F1_FIXTURE_ID")?;let input:UpdateBatchPreview=serde_json::from_str(&std::env::var("OPENCLANK_F1_INPUT")?)?;
        let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
        let mut stale=input.clone();stale.items[1].expected_revision+=1;assert!(preview(&store,&stale).is_err());
        let planned=preview(&store,&input).map_err(|e|format!("preview: {e}"))?;let digest=planned["preview_sha256"].as_str().ok_or("digest")?.to_string();
        let mut apply_req=UpdateBatchApply{version:input.version,owner:input.owner.clone(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),items:input.items.clone(),request_key:"f1_original_abcdefghijklmnop".into(),preview_sha256:digest.clone()};
        let result=apply(&store,&apply_req).map_err(|e|format!("apply: {e}"))?;assert_eq!(apply(&store,&apply_req).map_err(|e|format!("replay: {e}"))?,result);
        let mut changed=apply_req.clone();changed.items[0].payload=json!({"value":{"type":"string","value":"changed body"}});assert!(apply(&store,&changed).is_err());
        let mut status_req=UpdateBatchStatus{version:1,owner:input.owner.clone(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),request_key:apply_req.request_key.clone(),preview_sha256:digest};assert_eq!(status(&store,&status_req).map_err(|e|format!("status: {e}"))?,result);
        let src=store.owner_counts(&input.owner)?;let dst=store.owner_counts("bob")?;
        let rename=GuardedOwnerRenameRequest{version:1,database_id:database.clone(),account_id:input.account_id.clone(),operation_id:"f1-rename".into(),step:"fm".into(),generation:1,action:"forward".into(),owner:input.owner.clone(),new_owner:"bob".into(),expected_source_fingerprint:src["fingerprint"].as_str().ok_or("source")?.into(),expected_target_fingerprint:dst["fingerprint"].as_str().ok_or("target")?.into(),prior_receipt_id:None,prior_receipt_hash:None,rag_relocation:None};
        store.guarded_rename_owner(&rename).map_err(|e|format!("rename: {e}"))?;status_req.owner="bob".into();assert_eq!(status(&store,&status_req).map_err(|e|format!("renamed status: {e}"))?,result);
        let components=vec!["memories".to_string()];let plan=store.reset_owner("reset_preview","bob",&components,None)?;
        store.guarded_reset_owner("bob",&input.workspace_id,&input.account_id,"f1-reset",&components,&plan["components"],false).map_err(|e|format!("reset: {e}"))?;
        assert_eq!(status(&store,&status_req).map_err(|e|format!("reset status: {e}"))?["state"],"terminal");apply_req.owner="bob".into();assert!(apply(&store,&apply_req).is_err());
        let f8=PreextractedStageRequest{version:1,owner:"bob".into(),workspace_id:input.workspace_id.clone(),account_id:input.account_id.clone(),policy_projection:input.policy_projection.clone(),request_key:"f8-co-purge".into(),items:vec![PreextractedItem{kind:"fact".into(),text:"synthetic co-purge".into(),source:PreextractedSource{uri:"fixture://co-purge".into(),revision:1,content_sha256:"a".repeat(64)},target:None}]};store.preextracted_stage(&f8)?;
        let count=store.owner_counts("bob")?;let purge=GuardedOwnerPurgeRequest{version:1,database_id:database,account_id:input.account_id.clone(),operation_id:"f1-purge".into(),step:"memory_fm".into(),generation:1,action:"purge".into(),owner:"bob".into(),expected_source_fingerprint:count["fingerprint"].as_str().ok_or("fingerprint")?.into()};
        let purged=store.guarded_purge_owner(&purge).map_err(|e|format!("purge: {e}"))?;assert_eq!(purged["version"],1);assert_eq!(purged["result"].as_object().ok_or("result")?.len(),3);assert_eq!(store.guarded_purge_status(&purge)?,purged);store.guarded_purge_stamp(&purge)?;
        {let conn=store.conn.lock().map_err(|_|"lock")?;let raw:String=conn.query_row("SELECT value FROM fm_meta WHERE key=?1",[purged["receipt_id"].as_str().ok_or("receipt")?],|r|r.get(0))?;let v:Value=serde_json::from_str(&raw)?;assert_eq!(v["version"],3);assert!(v["f8"].is_object() && v["f1"].is_object());}
        assert!(status(&store,&status_req).is_err());assert!(apply(&store,&apply_req).is_err());assert!(store.preextracted_stage(&f8).is_err());
        Ok(())
    }
}

/// Denial-only legacy guard: never lets a non-account-bound delete orphan a
/// live F1 receipt. It grants no deletion authority.
pub(crate) fn reject_legacy_f1_delete(conn:&Connection,candidates:&[String],curated:&[String],budget:&mut ResetFenceBudget)->Result<(),String> {
    let mut stmt=conn.prepare("SELECT owner_id,change_set_id FROM fm_v2_change_sets WHERE reason=?1 OR change_set_id GLOB 'cs_f1_update_*' ORDER BY owner_id,change_set_id").map_err(|_|invalid())?;
    let mut rows=stmt.query([REASON]).map_err(|_|invalid())?;let mut expected=std::collections::BTreeSet::new();
    while let Some(row)=rows.next().map_err(|_|invalid())? {
        let owner:String=row.get(0).map_err(|_|invalid())?;let id:String=row.get(1).map_err(|_|invalid())?;budget.check(owner.len()+id.len()).map_err(|_|invalid())?;
        request_sha(&id)?;let receipt=read_receipt(conn,&owner,&id,budget)?.ok_or_else(invalid)?;
        if receipt.items.iter().any(|i|candidates.contains(&i.candidate_id) || curated.contains(&i.memory_id)) {return Err("batch member requires account-bound guarded forget".into());}
        expected.insert((owner,format!("outbox_{id}_{EVENT}")));
    }drop(rows);drop(stmt);
    let mut stmt=conn.prepare("SELECT owner_id,event_id FROM fm_v2_outbox WHERE kind=?1 OR event_id GLOB 'outbox_cs_f1_update_*' OR change_set_id GLOB 'cs_f1_update_*' ORDER BY owner_id,event_id").map_err(|_|invalid())?;
    let mut rows=stmt.query([EVENT]).map_err(|_|invalid())?;
    while let Some(row)=rows.next().map_err(|_|invalid())? {let owner:String=row.get(0).map_err(|_|invalid())?;let event:String=row.get(1).map_err(|_|invalid())?;budget.check(owner.len()+event.len()).map_err(|_|invalid())?;if !expected.remove(&(owner,event)){return Err(invalid());}}
    if !expected.is_empty(){return Err(invalid());}Ok(())
}
