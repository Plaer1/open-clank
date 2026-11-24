//! Fact-only verification of an existing selective S05 erasure event.
use super::*;
/// The event itself supplies its original job/key selectors. This proves the
/// existing reset commitment for inventory only; it grants no retry or source.
pub(crate) fn verify_selective_ingest_event(tx:&Transaction<'_>,current:&str,account:&str,event_id:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    if view.current_owner()!=current || view.account_id()!=account {return Err(RepositoryError::NotFound);}
    view.for_captured(tx,current)?;
    let len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,event_id],|r|r.get(0)).optional()?;
    let len=len.ok_or(RepositoryError::NotFound)?;if !(0..=8192).contains(&len) {return Err(RepositoryError::NotFound);}budget.check(len as usize).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND kind='s04_ingest_erased'",params![current,event_id],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let captured=value.get("owner").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    let job=value.get("job_id").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    let key=value.get("source_key").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    let receipt=value.get("reset_receipt_id").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    view.for_captured(tx,captured)?;
    if event_id!=ingest_erased_event_id(captured,job)? {return Err(RepositoryError::NotFound);}
    for (sql,id) in [("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",receipt.to_owned()),("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",format!("outbox_{receipt}_s06_guarded_reset"))] {
        let len:Option<i64>=tx.query_row(sql,params![current,id],|r|r.get(0)).optional()?;
        let len=len.ok_or(RepositoryError::NotFound)?;if !(0..=1_048_576).contains(&len) {return Err(RepositoryError::NotFound);}budget.check(len as usize).map_err(RepositoryError::Validation)?;
    }
    if !validate_ingest_erased(tx,current,account,job,key,&raw,view.owners(),budget)? {return Err(RepositoryError::NotFound);}
    Ok(())
}
