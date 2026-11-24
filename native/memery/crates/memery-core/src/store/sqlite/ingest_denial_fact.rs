//! Shared exact privacy-denial validation. This supplies facts, never a grant.
use super::*;
fn unavailable()->String {"ingest denial unavailable".into()}
fn charge(tx:&rusqlite::Transaction<'_>,owner:&str,sql:&str,id:&str,limit:i64,budget:&mut Option<&mut crate::repository::ResetFenceBudget>)->Result<(),String>{
    if let Some(b)=budget.as_deref_mut() {
        let len:Option<i64>=tx.query_row(sql,params![owner,id],|r|r.get(0)).optional().map_err(|_|unavailable())?;
        let len=len.ok_or_else(unavailable)?;if !(0..=limit).contains(&len){return Err(unavailable());}b.check(len as usize)?;
    }Ok(())
}
/// The legacy caller supplies no account/view and requires captured==current;
/// guarded inventory supplies both actual account and a same-tx custody view.
pub(super) fn verify_record(tx:&rusqlite::Transaction<'_>,current:&str,captured:&str,account:Option<&str>,id:&str,view:Option<&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>>,mut budget:Option<&mut crate::repository::ResetFenceBudget>)->Result<(String,ForgetClosure),String>{
    if let Some(v)=view {if v.current_owner()!=current || Some(v.account_id())!=account {return Err(unavailable());}v.for_captured(tx,captured).map_err(|_|unavailable())?;} else if current!=captured || account.is_some() {return Err(unavailable());}
    for (sql,limit) in [("SELECT length(CAST(affected_ids AS BLOB)) FROM memory_tombstones WHERE owner=?1 AND id=?2",16*1024*1024),("SELECT length(CAST(workspace_id AS BLOB)) FROM memory_tombstones WHERE owner=?1 AND id=?2",512),("SELECT length(CAST(source_key AS BLOB)) FROM memory_tombstones WHERE owner=?1 AND id=?2",64),("SELECT length(CAST(operation_key AS BLOB)) FROM memory_tombstones WHERE owner=?1 AND id=?2",512)] {charge(tx,current,sql,id,limit,&mut budget)?;}
    let (workspace,source,affected,has_recovery,receipt):(String,String,String,bool,Option<String>)=tx.query_row("SELECT workspace_id,source_key,affected_ids,(recovery_payload IS NOT NULL OR recover_until IS NOT NULL OR recovered_at IS NOT NULL),operation_key FROM memory_tombstones WHERE owner=?1 AND id=?2",params![current,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let denial:ForgetClosure=serde_json::from_value(crate::field_authority::parse_unique_json(&affected).map_err(|_|unavailable())?).map_err(|_|unavailable())?;
    if workspace.is_empty() || workspace.trim()!=workspace || has_recovery || !denial.curated_ids.is_empty() || !denial.graph_node_ids.is_empty() || !denial.native_event_links.is_empty(){return Err(unavailable());}
    let receipt=receipt.ok_or_else(unavailable)?;let digest=crate::repository::sha256_hex(&serde_json::to_vec(&(captured,&workspace,&receipt)).map_err(|_|unavailable())?);
    if id!=format!("s04deny_{digest}"){return Err(unavailable());}
    let event=format!("outbox_{receipt}_deny_{digest}");charge(tx,current,"SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",&event,8192,&mut budget)?;
    let (marker_raw,revision):(String,i64)=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s04_ingest_denial'",params![current,event,receipt],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let marker=crate::field_authority::parse_unique_json(&marker_raw).map_err(|_|unavailable())?;
    let partition=marker.get("partition_sha256").and_then(serde_json::Value::as_str).filter(|v|v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))).ok_or_else(unavailable)?;
    if revision!=0 || source!=crate::repository::sha256_hex(marker_raw.as_bytes()) || marker!=serde_json::json!({"version":"s04.ingest-denial.v1","owner":captured,"workspace_id":workspace,"receipt_id":receipt,"partition_sha256":partition,"tombstone_id":id,"closure_sha256":crate::repository::sha256_hex(affected.as_bytes())}) {return Err(unavailable());}
    charge(tx,current,"SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",&receipt,1_048_576,&mut budget)?;
    let (hash,reset_raw,actor,actor_id,contract,event_source):(String,String,String,String,String,Option<String>)=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![current,receipt],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let reset=crate::field_authority::parse_unique_json(&reset_raw).map_err(|_|unavailable())?;
    let original_id=format!("outbox_{receipt}_s06_guarded_reset");charge(tx,current,"SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",&original_id,1_048_576,&mut budget)?;
    let original:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![current,original_id,receipt],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
    let actual_account=reset["request"]["account_id"].as_str().filter(|v|!v.is_empty()).ok_or_else(unavailable)?;let operation=reset["request"]["operation_id"].as_str().filter(|v|!v.is_empty()).ok_or_else(unavailable)?;
    let expected=format!("cs_reset_{}",crate::repository::sha256_hex(&serde_json::to_vec(&(actual_account,operation)).map_err(|_|unavailable())?));
    if account.is_some_and(|v|v!=actual_account) || receipt!=expected || actor!="user" || actor_id!=captured || contract!=crate::ontology::CONTRACT_VERSION || event_source.is_some() || reset.as_object().map(|v|v.len())!=Some(4) || hash!=crate::repository::sha256_hex(reset_raw.as_bytes()) || reset["version"]!="s06.guarded-reset.v1" || reset["request"]["owner"]!=captured || reset["request_sha256"]!=crate::repository::sha256_hex(&serde_json::to_vec(&reset["request"]).map_err(|_|unavailable())?) || reset["result"]["complete"]!=true || reset["result"]["categories"]["ingest"]["selective"]["partition_sha256"]!=partition || original.as_ref().is_none_or(|(p,r)|*r!=0 || p!=&reset_raw){return Err(unavailable());}
    Ok((workspace,denial))
}
/// A bounded event-derived selector for whole-family inventory only.
pub(crate) fn verify_event(tx:&rusqlite::Transaction<'_>,current:&str,account:&str,event_id:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<String,String>{
    let mut b=Some(&mut *budget);charge(tx,current,"SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",event_id,8192,&mut b)?;
    let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND kind='s04_ingest_denial'",params![current,event_id],|r|r.get(0)).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let marker=crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?;let captured=marker["owner"].as_str().ok_or_else(unavailable)?;let id=marker["tombstone_id"].as_str().ok_or_else(unavailable)?;let workspace=marker["workspace_id"].as_str().ok_or_else(unavailable)?;let receipt=marker["receipt_id"].as_str().ok_or_else(unavailable)?;
    let digest=crate::repository::sha256_hex(&serde_json::to_vec(&(captured,workspace,receipt)).map_err(|_|unavailable())?);
    if event_id!=format!("outbox_{receipt}_deny_{digest}"){return Err(unavailable());}
    verify_record(tx,current,captured,Some(account),id,Some(view),Some(budget))?;Ok(id.to_owned())
}
/// Reverse tombstone inventory without inverting any erased source selector.
pub(crate) fn verify_tombstone(tx:&rusqlite::Transaction<'_>,current:&str,account:&str,id:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    let mut b=Some(&mut *budget);
    charge(tx,current,"SELECT length(CAST(workspace_id AS BLOB)) FROM memory_tombstones WHERE owner=?1 AND id=?2",id,512,&mut b)?;
    charge(tx,current,"SELECT length(CAST(operation_key AS BLOB)) FROM memory_tombstones WHERE owner=?1 AND id=?2",id,512,&mut b)?;
    let (workspace,receipt):(String,String)=tx.query_row("SELECT workspace_id,operation_key FROM memory_tombstones WHERE owner=?1 AND id=?2",params![current,id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let mut captured=None;for owner in view.owners(){budget.check(owner.len())?;let digest=crate::repository::sha256_hex(&serde_json::to_vec(&(owner,&workspace,&receipt)).map_err(|_|unavailable())?);if id==format!("s04deny_{digest}"){if captured.is_some(){return Err(unavailable());}captured=Some(owner);}}
    verify_record(tx,current,captured.ok_or_else(unavailable)?,Some(account),id,Some(view),Some(budget))?;Ok(())
}
