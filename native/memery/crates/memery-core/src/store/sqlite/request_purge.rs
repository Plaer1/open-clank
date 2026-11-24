//! Typed, bounded request-terminal index proof under an existing purge receipt.
use super::*;
use serde::{Serialize,Deserialize};
use serde_json::{json,Value};
use crate::repository::{self,ResetFenceBudget};
#[derive(Clone,Copy)]
pub(super) enum Family {F8,F1,M5,S05Reset,S05Purge,M5Quota,S05Slot}
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RequestHash {pub request_sha256:String,pub body_sha256:Option<String>}
fn invalid()->String {"request terminal unavailable".into()}
fn hex(v:&str)->bool {v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn text(v:&str)->bool {!v.is_empty() && v.trim()==v && v.len()<=512 && !v.chars().any(char::is_control)}
impl Family {
    fn name(self)->&'static str {match self {Self::F8=>"preextracted_candidates_v1",Self::F1=>"knowledge_update_batch_v1",Self::M5=>"skill_draft_v1",Self::S05Reset=>"s05_s08_quota_reset_v1",Self::S05Purge=>"s05_s08_quota_v1",Self::M5Quota=>"m5_quota_window_v1",Self::S05Slot=>"s05_reserve_terminal_v1"}}
    fn domain(self)->&'static str {match self {Self::F8=>"s03.f8-purge-account.v1",Self::F1=>"s03.f1-purge-account.v1",Self::M5=>"s05.skill-draft-purge-account.v1",Self::S05Reset=>"s05.quota-reset-account.v1",Self::S05Purge=>"s05.quota-purge-account.v1",Self::M5Quota=>"s05.m5-quota-account.v1",Self::S05Slot=>"s05.reserve-terminal-account.v1"}}
    fn context(self,id:&str)->String {format!("{}-purge:{id}",match self {Self::F8=>"f8",Self::F1=>"f1",Self::M5=>"m5",Self::S05Reset=>"s05-reset",Self::S05Purge=>"s05-purge",Self::M5Quota=>"m5-quota",Self::S05Slot=>"s05-slot"})}
    fn collision_sql(self)->&'static str {match self {Self::F8=>"SELECT EXISTS(SELECT 1 FROM f8_purge_request_terminal WHERE account_sha256=?1 AND request_sha256=?2)",Self::F1=>"SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal WHERE family='knowledge_update_batch_v1' AND account_sha256=?1 AND request_sha256=?2)",Self::M5=>"SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal WHERE family='skill_draft_v1' AND account_sha256=?1 AND request_sha256=?2)",Self::S05Reset=>"SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal WHERE family='s05_s08_quota_reset_v1' AND account_sha256=?1 AND request_sha256=?2)",Self::S05Purge=>"SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2)",Self::M5Quota=>"SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal WHERE family='m5_quota_window_v1' AND account_sha256=?1 AND request_sha256=?2)",Self::S05Slot=>"SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal WHERE family='s05_reserve_terminal_v1' AND account_sha256=?1 AND request_sha256=?2)"}}
    fn insert_sql(self)->&'static str {match self {Self::F8=>"INSERT INTO f8_purge_request_terminal(account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id) VALUES(?1,?2,?3,?4,?5)",Self::F1=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id) VALUES('knowledge_update_batch_v1',?1,?2,?3,?4,?5)",Self::M5=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id) VALUES('skill_draft_v1',?1,?2,?3,?4,?5)",Self::S05Reset=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id) VALUES('s05_s08_quota_reset_v1',?1,?2,?3,?4,?5)",Self::S05Purge=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id) VALUES('s05_s08_quota_v1',?1,?2,?3,?4,?5)",Self::M5Quota=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id) VALUES('m5_quota_window_v1',?1,?2,?3,?4,?5)",Self::S05Slot=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id) VALUES('s05_reserve_terminal_v1',?1,?2,?3,?4,?5)"}}
    fn rows_sql(self)->&'static str {match self {Self::F8=>"SELECT account_sha256,request_sha256,body_sha256,purge_context_id FROM f8_purge_request_terminal WHERE purge_receipt_id=?1 ORDER BY request_sha256",Self::F1=>"SELECT account_sha256,request_sha256,body_sha256,purge_context_id FROM native_request_purge_terminal WHERE purge_receipt_id=?1 AND family='knowledge_update_batch_v1' ORDER BY request_sha256",Self::M5=>"SELECT account_sha256,request_sha256,body_sha256,purge_context_id FROM native_request_purge_terminal WHERE purge_receipt_id=?1 AND family='skill_draft_v1' ORDER BY request_sha256",Self::S05Reset=>"SELECT account_sha256,request_sha256,body_sha256,purge_context_id FROM native_request_purge_terminal WHERE purge_receipt_id=?1 AND family='s05_s08_quota_reset_v1' ORDER BY request_sha256",Self::S05Purge=>"SELECT account_sha256,request_sha256,body_sha256,purge_context_id FROM native_request_purge_terminal WHERE purge_receipt_id=?1 AND family='s05_s08_quota_v1' ORDER BY request_sha256",Self::M5Quota=>"SELECT account_sha256,request_sha256,body_sha256,purge_context_id FROM native_request_purge_terminal WHERE purge_receipt_id=?1 AND family='m5_quota_window_v1' ORDER BY request_sha256",Self::S05Slot=>"SELECT account_sha256,request_sha256,body_sha256,purge_context_id FROM native_request_purge_terminal WHERE purge_receipt_id=?1 AND family='s05_reserve_terminal_v1' ORDER BY request_sha256"}}
}
pub(super) fn account_hash(family:Family,database:&str,account:&str)->Result<String,String> {
    Ok(repository::sha256_hex(&serde_json::to_vec(&(family.domain(),database,account)).map_err(|_|invalid())?))
}
pub(super) fn prepare(tx:&Transaction<'_>,family:Family,database:&str,account:&str,entries:&[RequestHash],budget:&mut ResetFenceBudget)->Result<Value,String> {
    if entries.is_empty() || !text(database) || !text(account) || entries.windows(2).any(|p|p[0].request_sha256>=p[1].request_sha256) {return Err(invalid());}
    let account_sha256=account_hash(family,database,account)?;let bytes=serde_json::to_vec(entries).map_err(|_|invalid())?;budget.check(bytes.len()).map_err(|_|invalid())?;
    for e in entries {
        if !hex(&e.request_sha256) || e.body_sha256.as_ref().is_some_and(|v|!hex(v)) {return Err(invalid());}
        budget.check(e.request_sha256.len()+e.body_sha256.as_ref().map_or(0,String::len)).map_err(|_|invalid())?;
        let exists:bool=tx.query_row(family.collision_sql(),params![account_sha256,e.request_sha256],|r|r.get(0)).map_err(|_|invalid())?;if exists{return Err(invalid());}
    }
    let mut result=json!({"version":1,"database_id":database,"account_sha256":account_sha256,"count":entries.len(),"entries_sha256":repository::sha256_hex(&bytes)});
    if !matches!(family,Family::F8) {result["family"]=json!(family.name());} Ok(result)
}
pub(super) fn write(tx:&Transaction<'_>,family:Family,account_sha256:&str,receipt_id:&str,context_id:&str,entries:&[RequestHash])->Result<(),String> {
    if !hex(account_sha256) || !text(receipt_id) || context_id!=family.context(receipt_id){return Err(invalid());}
    for e in entries {if tx.execute(family.insert_sql(),params![account_sha256,e.request_sha256,e.body_sha256,receipt_id,context_id]).map_err(|_|invalid())?!=1{return Err(invalid());}} Ok(())
}
/// Timed quota pointers are inserted only under one validated lifecycle context.
pub(super) fn write_timed(tx:&Transaction<'_>,family:Family,account_sha256:&str,header_id:&str,receipt_id:&str,context_id:&str,entry:&RequestHash,effective_at_ms:i64)->Result<(),String> {
    if !matches!(family,Family::S05Reset|Family::S05Purge|Family::M5Quota) || !hex(account_sha256) || !text(header_id) || !text(receipt_id) || context_id!=family.context(header_id) || !hex(&entry.request_sha256) || entry.body_sha256.as_ref().is_none_or(|v|!hex(v)) || effective_at_ms<0 {return Err(invalid());}
    let sql=match family {
        Family::S05Reset=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms) VALUES('s05_s08_quota_reset_v1',?1,?2,?3,?4,?5,?6)",
        Family::S05Purge=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms) VALUES('s05_s08_quota_v1',?1,?2,?3,?4,?5,?6)",
        Family::M5Quota=>"INSERT INTO native_request_purge_terminal(family,account_sha256,request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms) VALUES('m5_quota_window_v1',?1,?2,?3,?4,?5,?6)",
        _=>return Err(invalid()),
    };
    if tx.execute(sql,params![account_sha256,entry.request_sha256,entry.body_sha256,receipt_id,context_id,effective_at_ms]).map_err(|_|invalid())?!=1 {return Err(invalid());}Ok(())
}
pub(super) fn verify(tx:&Transaction<'_>,family:Family,database:&str,account:&str,receipt_id:&str,manifest:&Value,budget:&mut ResetFenceBudget)->Result<(),String> {
    let m=manifest.as_object().ok_or_else(invalid)?;let account_hash=account_hash(family,database,account)?;
    let count=m.get("count").and_then(Value::as_u64).ok_or_else(invalid)?;let digest=m.get("entries_sha256").and_then(Value::as_str).filter(|v|hex(v)).ok_or_else(invalid)?;
    if m.len()!=(if matches!(family,Family::F8){5}else{6}) || m.get("version")!=Some(&json!(1)) || m.get("database_id").and_then(Value::as_str)!=Some(database) || m.get("account_sha256").and_then(Value::as_str)!=Some(account_hash.as_str()) || count==0 || (!matches!(family,Family::F8) && m.get("family").and_then(Value::as_str)!=Some(family.name())) {return Err(invalid());}
    let mut stmt=tx.prepare(family.rows_sql()).map_err(|_|invalid())?;let mut rows=stmt.query([receipt_id]).map_err(|_|invalid())?;let mut entries=Vec::new();
    while let Some(row)=rows.next().map_err(|_|invalid())? {
        let account:String=row.get(0).map_err(|_|invalid())?;let request_sha256:String=row.get(1).map_err(|_|invalid())?;let body_sha256:Option<String>=row.get(2).map_err(|_|invalid())?;let context:String=row.get(3).map_err(|_|invalid())?;
        budget.check(account.len()+request_sha256.len()+body_sha256.as_ref().map_or(0,String::len)+context.len()).map_err(|_|invalid())?;
        if account!=account_hash || !hex(&request_sha256) || body_sha256.as_ref().is_some_and(|v|!hex(v)) || context!=family.context(receipt_id){return Err(invalid());}
        entries.push(RequestHash{request_sha256,body_sha256});
    }
    drop(rows);drop(stmt);let bytes=serde_json::to_vec(&entries).map_err(|_|invalid())?;budget.check(bytes.len()).map_err(|_|invalid())?;
    if entries.len() as u64!=count || repository::sha256_hex(&bytes)!=digest{return Err(invalid());} Ok(())
}

/// Content-free settled accounting retained in the existing guarded purge receipt.
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct SkillUsage {pub operation_sha256:String,pub original_sha256:String,pub disposition:String,pub charge_basis:String,pub settled_at_ms:i64,pub charged_calls:i64,pub charged_seconds:i64,pub charged_input_tokens:i64,pub charged_output_tokens:i64}
fn validate_skill_usage(u:&SkillUsage)->Result<(),String>{
    if !hex(&u.operation_sha256)||!hex(&u.original_sha256)||!matches!(u.disposition.as_str(),"draft"|"no_draft"|"failed"|"cancelled")||u.settled_at_ms<0||u.charged_calls<0||u.charged_calls>1||u.charged_seconds<0||u.charged_seconds>300||u.charged_input_tokens<0||u.charged_input_tokens>1_000_000||u.charged_output_tokens<0||u.charged_output_tokens>32768{return Err(invalid())}
    if u.disposition=="cancelled" {if u.charge_basis!="unissued"||u.settled_at_ms!=0||u.charged_calls!=0||u.charged_seconds!=0||u.charged_input_tokens!=0||u.charged_output_tokens!=0{return Err(invalid())}}
    else if !matches!(u.charge_basis.as_str(),"actual"|"reserved_bound")||u.settled_at_ms==0||u.charged_calls!=1{return Err(invalid())}Ok(())
}
fn skill_hashes(usage:&[SkillUsage])->Result<Vec<RequestHash>,String>{
    if usage.is_empty()||usage.len()>128||usage.windows(2).any(|p|p[0].operation_sha256>=p[1].operation_sha256){return Err(invalid())}
    let bytes=serde_json::to_vec(usage).map_err(|_|invalid())?;if bytes.len()>32768{return Err(invalid())}
    usage.iter().map(|u|{validate_skill_usage(u)?;let body=serde_json::to_vec(&serde_json::to_value(u).map_err(|_|invalid())?).map_err(|_|invalid())?;Ok(RequestHash{request_sha256:u.operation_sha256.clone(),body_sha256:Some(repository::sha256_hex(&body))})}).collect()
}
pub(super) fn prepare_skill(tx:&Transaction<'_>,database:&str,account:&str,usage:&[SkillUsage],budget:&mut ResetFenceBudget)->Result<Value,String>{
    let entries=skill_hashes(usage)?;let index=prepare(tx,Family::M5,database,account,&entries,budget)?;Ok(json!({"version":1,"index":index,"usage":usage}))
}
pub(super) fn write_skill(tx:&Transaction<'_>,account_hash:&str,receipt_id:&str,usage:&[SkillUsage])->Result<(),String>{let entries=skill_hashes(usage)?;write(tx,Family::M5,account_hash,receipt_id,&format!("m5-purge:{receipt_id}"),&entries)}
pub(super) fn verify_skill(tx:&Transaction<'_>,database:&str,account:&str,receipt_id:&str,value:&Value,budget:&mut ResetFenceBudget)->Result<Vec<SkillUsage>,String>{
    if value.as_object().map(|v|v.len())!=Some(3)||value["version"]!=1{return Err(invalid())}
    let usage:Vec<SkillUsage>=serde_json::from_value(value["usage"].clone()).map_err(|_|invalid())?;let entries=skill_hashes(&usage)?;budget.check(serde_json::to_vec(&usage).map_err(|_|invalid())?.len()).map_err(|_|invalid())?;
    let index=&value["index"];verify(tx,Family::M5,database,account,receipt_id,index,budget)?;
    if index["count"].as_u64()!=Some(entries.len() as u64)||index["entries_sha256"].as_str()!=Some(repository::sha256_hex(&serde_json::to_vec(&entries).map_err(|_|invalid())?).as_str()){return Err(invalid())}Ok(usage)
}
