//! Content-free full-reset denial for the original paired source/job identity.
//! This fact grants neither source access nor a new submission.
use super::*;
use crate::repository::{AssistantInterpretationCoverage,HostArchiveSourceRef,ResetFenceBudget};
use serde::{Deserialize,Serialize};
use serde_json::{json,Value};
use std::collections::BTreeSet;

#[derive(Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FullPairDenial {
    version:String,owner:String,reset_workspace_id:String,account_id:String,
    job_selector:String,source_key:String,operation_id:String,reset_receipt_id:String,
}
fn unavailable()->String {"paired full reset denial unavailable".into()}
fn hash(value:&Value)->Result<String,String> {Ok(crate::repository::sha256_hex(&serde_json::to_vec(value).map_err(|_|unavailable())?))}
fn sha(v:&str)->bool {v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn text(v:&str)->bool {!v.is_empty() && v.trim()==v && v.len()<=512}
fn job_event(m:&FullPairDenial)->Result<String,String> {Ok(format!("outbox_s04_pair_full_job_{}",hash(&json!([m.owner,m.job_selector]))?))}
fn source_event(m:&FullPairDenial)->Result<String,String> {Ok(format!("outbox_s04_pair_full_source_{}",hash(&json!([m.owner,m.source_key]))?))}
fn reset_id(account:&str,operation:&str)->Result<String,String> {Ok(format!("cs_reset_{}",hash(&json!([account,operation]))?))}

/// Caller owns the existing exact guarded reset claim and an IMMEDIATE tx.
/// Raw historical job IDs never leave this in-memory collection.
pub(super) fn collect(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,operation:&str,receipt:&str,budget:&mut ResetFenceBudget)->Result<Vec<FullPairDenial>,String> {
    if ![owner,workspace,account,operation].iter().all(|v|text(v)) || receipt!=reset_id(account,operation)? {return Err(unavailable());}
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j LEFT JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id WHERE j.owner_id=?1 AND j.kind='assistant_interpretation_review' AND r.job_id IS NULL)",[owner],|r|r.get(0)).map_err(|_|unavailable())?;
    if orphan {return Err(unavailable());}
    let mut stmt=tx.prepare("SELECT j.job_id,r.source_account_id,r.source_key,length(CAST(r.source_ref_json AS BLOB)),length(CAST(r.coverage_json AS BLOB)) FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id WHERE j.owner_id=?1 AND j.kind='assistant_interpretation_review' ORDER BY j.job_id").map_err(|_|unavailable())?;
    let mut rows=stmt.query([owner]).map_err(|_|unavailable())?;let mut result=Vec::new();let mut jobs=BTreeSet::new();let mut sources=BTreeSet::new();
    while let Some(row)=rows.next().map_err(|_|unavailable())? {
        let job:&str=row.get_ref(0).map_err(|_|unavailable())?.as_str().map_err(|_|unavailable())?;let actual:&str=row.get_ref(1).map_err(|_|unavailable())?.as_str().map_err(|_|unavailable())?;let key:&str=row.get_ref(2).map_err(|_|unavailable())?.as_str().map_err(|_|unavailable())?;
        let source_len:i64=row.get(3).map_err(|_|unavailable())?;let coverage_len:i64=row.get(4).map_err(|_|unavailable())?;
        if !text(job) || actual!=account || !sha(key) || !(0..=65536).contains(&source_len) || !(0..=65536).contains(&coverage_len) || !jobs.insert(job.to_owned()) || !sources.insert(key.to_owned()) {return Err(unavailable());}
        budget.check(job.len().saturating_add(key.len()).saturating_add(source_len as usize).saturating_add(coverage_len as usize))?;
        let (raw,coverage):(String,String)=tx.query_row("SELECT source_ref_json,coverage_json FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_|unavailable())?;
        let source:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?).map_err(|_|unavailable())?;
        let coverage:AssistantInterpretationCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&coverage).map_err(|_|unavailable())?).map_err(|_|unavailable())?;
        if source.account_id!=account || coverage.source_key(&source).map_err(|_|unavailable())?!=key {return Err(unavailable());}
        result.push(FullPairDenial{version:"s04.paired-full-erased.v1".into(),owner:owner.into(),reset_workspace_id:workspace.into(),account_id:account.into(),job_selector:hash(&json!(["s04.pair-job.v1",owner,job]))?,source_key:key.into(),operation_id:operation.into(),reset_receipt_id:receipt.into()});
    }Ok(result)
}
fn verify_reset(tx:&rusqlite::Transaction<'_>,m:&FullPairDenial,budget:&mut ResetFenceBudget)->Result<(),String> {verify_reset_at(tx,&m.owner,m,budget)}
fn verify_reset_at(tx:&rusqlite::Transaction<'_>,sql_owner:&str,m:&FullPairDenial,budget:&mut ResetFenceBudget)->Result<(),String> {
    if m.version!="s04.paired-full-erased.v1" || ![m.owner.as_str(),m.reset_workspace_id.as_str(),m.account_id.as_str(),m.operation_id.as_str()].iter().all(|v|text(v)) || !sha(&m.job_selector) || !sha(&m.source_key) || m.reset_receipt_id!=reset_id(&m.account_id,&m.operation_id)? {return Err(unavailable());}
    let lengths:(i64,i64)=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 WHERE c.owner_id=?1 AND c.change_set_id=?2",params![sql_owner,m.reset_receipt_id,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id)],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_|unavailable())?;
    if !(0..=1_048_576).contains(&lengths.0) || !(0..=1_048_576).contains(&lengths.1) {return Err(unavailable());}
    budget.check((lengths.0 as usize).saturating_add(lengths.1 as usize))?;
    let (hash,raw,actor,actor_id,contract,source):(String,String,String,String,String,Option<String>)=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![sql_owner,m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?;
    if crate::repository::sha256_hex(raw.as_bytes())!=hash || actor!="user" || actor_id!=m.owner || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || value.as_object().map(|v|v.len())!=Some(4) || value["version"]!="s06.guarded-reset.v1" || value["request_sha256"]!=hash_json(&value["request"])? || value["request"]["owner"]!=m.owner || value["request"]["workspace_id"]!=m.reset_workspace_id || value["request"]["account_id"]!=m.account_id || value["request"]["operation_id"]!=m.operation_id || !value["request"]["components"].as_array().is_some_and(|v|v.iter().any(|x|x=="memories")) || value["result"]["complete"]!=true || value["result"]["categories"]["ingest"]["state"]!="complete" || value["result"]["categories"]["memories"]["state"]!="complete" || !value["request"]["selective_manifest_sha256"].is_null() {return Err(unavailable());}
    let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![sql_owner,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id),m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
    if out.as_ref().is_none_or(|(v,rev)|v!=&raw || *rev!=0) {return Err(unavailable());}Ok(())
}
fn hash_json(value:&Value)->Result<String,String> {Ok(crate::repository::sha256_hex(&serde_json::to_vec(value).map_err(|_|unavailable())?))}
pub(super) fn write(tx:&rusqlite::Transaction<'_>,markers:&[FullPairDenial],now:&str,budget:&mut ResetFenceBudget)->Result<(),String> {
    for marker in markers {
        verify_reset(tx,marker,budget)?;let raw=serde_json::to_string(marker).map_err(|_|unavailable())?;
        for (id,kind) in [(job_event(marker)?,"s04_pair_full_job_erased"),(source_event(marker)?,"s04_pair_full_source_erased")] {
            if tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![marker.owner,id,marker.reset_receipt_id,kind,raw,now]).map_err(|_|unavailable())?!=1 {return Err(unavailable());}
            let stored:Option<(String,String,String,i64)>=tx.query_row("SELECT change_set_id,kind,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![marker.owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|unavailable())?;
            if stored.as_ref().is_none_or(|(change,k,payload,revision)|change!=&marker.reset_receipt_id || k!=kind || payload!=&raw || *revision!=0) {return Err(unavailable());}
        }
    }Ok(())
}

/// Exact content-free terminal proof; a returned fact says the pair input was
/// erased, not that any model publication applied. Caller owns current human
/// account/read authority and the same transaction/budget.
pub(crate) fn read_terminal(tx:&rusqlite::Transaction<'_>,current:&str,account:&str,job:&str,source_key:&str,view:Option<&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>>,budget:&mut ResetFenceBudget)->Result<bool,String> {
    if ![current,account,job].iter().all(|v|text(v)) || !sha(source_key) {return Err(unavailable());}
    let owners:Vec<String>=if let Some(v)=view {if v.current_owner()!=current || v.account_id()!=account {return Err(unavailable());}v.for_captured(tx,current).map_err(|_|unavailable())?;v.owners().to_vec()} else {vec![current.into()]};
    let mut found=false;
    for captured in owners {
        budget.check(captured.len().saturating_add(job.len()).saturating_add(source_key.len()))?;
        let selector=hash(&json!(["s04.pair-job.v1",captured,job]))?;
        let je=format!("outbox_s04_pair_full_job_{}",hash(&json!([captured,selector]))?);
        let se=format!("outbox_s04_pair_full_source_{}",hash(&json!([captured,source_key]))?);
        let mut records=Vec::new();
        for id in [&je,&se] {
            let length:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,id],|r|r.get(0)).optional().map_err(|_|unavailable())?;
            if let Some(length)=length {if !(0..=8192).contains(&length) {return Err(unavailable());}budget.check(length as usize)?;
                let row:(String,String,String,i64)=tx.query_row("SELECT change_set_id,kind,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).map_err(|_|unavailable())?;records.push(Some(row));
            } else {records.push(None);}
        }
        match (&records[0],&records[1]) {
            (None,None)=>continue,
            (Some((jc,jk,jp,jr)),Some((sc,sk,sp,sr)))=>{
                if found || jc!=sc || jk!="s04_pair_full_job_erased" || sk!="s04_pair_full_source_erased" || jp!=sp || *jr!=0 || *sr!=0 {return Err(unavailable());}
                let marker:FullPairDenial=serde_json::from_value(crate::field_authority::parse_unique_json(jp).map_err(|_|unavailable())?).map_err(|_|unavailable())?;
                if marker.owner!=captured || marker.account_id!=account || marker.job_selector!=selector || marker.source_key!=source_key || marker.reset_receipt_id!=*jc || job_event(&marker)?!=je || source_event(&marker)?!=se {return Err(unavailable());}
                if let Some(v)=view {v.for_captured(tx,&marker.owner).map_err(|_|unavailable())?;}
                verify_reset_at(tx,current,&marker,budget)?;found=true;
            },_=>return Err(unavailable()),
        }
    }Ok(found)
}

/// Verify an indexed marker event and its counterpart without recovering an
/// erased raw job ID. This is a historical fact, never an admission grant.
pub(crate) fn verify_terminal_event(tx:&rusqlite::Transaction<'_>,current:&str,account:&str,event_id:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(String,String),String> {
    if !text(current) || !text(account) || view.current_owner()!=current || view.account_id()!=account {return Err(unavailable());}
    let len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,event_id],|r|r.get(0)).optional().map_err(|_|unavailable())?;
    let len=len.ok_or_else(unavailable)?;if !(0..=8192).contains(&len) {return Err(unavailable());}budget.check(len as usize)?;
    let (change,kind,raw,revision):(String,String,String,i64)=tx.query_row("SELECT change_set_id,kind,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,event_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).map_err(|_|unavailable())?;
    let marker:FullPairDenial=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?).map_err(|_|unavailable())?;
    view.for_captured(tx,&marker.owner).map_err(|_|unavailable())?;
    let job=job_event(&marker)?;let source=source_event(&marker)?;
    if marker.account_id!=account || marker.reset_receipt_id!=change || revision!=0 || !((event_id==job && kind=="s04_pair_full_job_erased") || (event_id==source && kind=="s04_pair_full_source_erased")) {return Err(unavailable());}
    for (id,expected) in [(&job,"s04_pair_full_job_erased"),(&source,"s04_pair_full_source_erased")] {
        let size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,id],|r|r.get(0)).optional().map_err(|_|unavailable())?;
        let size=size.ok_or_else(unavailable)?;if !(0..=8192).contains(&size) {return Err(unavailable());}budget.check(size as usize)?;
        let row:(String,String,String,i64)=tx.query_row("SELECT change_set_id,kind,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).map_err(|_|unavailable())?;
        if row.0!=change || row.1!=expected || row.2!=raw || row.3!=0 {return Err(unavailable());}
    }
    verify_reset_at(tx,current,&marker,budget)?;
    Ok((marker.job_selector,marker.source_key))
}
