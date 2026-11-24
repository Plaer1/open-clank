//! Closed, source-neutral validation of one curated-knowledge transfer.
//! Factual descriptor validation does not grant review or publication authority.
use serde_json::{json,Value};
use super::{sha256_hex,ResetFenceBudget};

const PROFILE:&str="s10.curated-transfer.v1";
fn hash(v:&str)->bool {v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn keys(v:&Value,k:&[&str])->bool {v.as_object().is_some_and(|m|m.len()==k.len() && k.iter().all(|x|m.contains_key(*x)))}
fn bounded(v:&str)->bool {!v.is_empty() && v==v.trim() && v.len()<=512 && !v.chars().any(char::is_control)}
fn canonical(v:&Value)->Result<Vec<u8>,String>{serde_json::to_vec(v).map_err(|_|"knowledge snapshot unavailable".into())}
fn invalid()->String {"knowledge snapshot unavailable".into()}

/// Validate actual staged raw text and the exact original job proposal.
/// The caller independently owns account, current policy, job and transaction.
pub(crate) fn validate_transfer(proposal:&Value,profile:&str,account:&str,workspace:&str,content_hash:&str,byte_size:u64,identity_context_hash:&str,budget:&mut ResetFenceBudget)->Result<Value,String> {
    if profile!=PROFILE || !bounded(account) || !bounded(workspace) || !hash(content_hash) || !hash(identity_context_hash) || !keys(proposal,&["text","category","knowledge_snapshot"]) {return Err(invalid())}
    let descriptor=&proposal["knowledge_snapshot"];
    if !keys(descriptor,&["version","raw_json","raw_sha256","canonical_sha256","text_sha256","category","origin","account_id","workspace_id","action_id","mode","review_sha256"]) || descriptor["version"].as_u64()!=Some(1) || descriptor["account_id"]!=account || descriptor["workspace_id"]!=workspace || descriptor["mode"]!="review_only" {return Err(invalid())}
    let raw=descriptor["raw_json"].as_str().ok_or_else(invalid)?;
    if raw.len()>65536 || raw.len() as u64!=byte_size || sha256_hex(raw.as_bytes())!=content_hash || descriptor["raw_sha256"]!=content_hash {return Err(invalid())}
    budget.check(raw.len())?;
    let package=crate::field_authority::parse_unique_json(raw).map_err(|_|invalid())?;
    if !keys(&package,&["format","origin","knowledge"]) || package["format"]!="openclank.knowledge-snapshot.v1" {return Err(invalid())}
    let origin=&package["origin"];let knowledge=&package["knowledge"];
    if !keys(origin,&["item_ref","revision","value_sha256","record_type","declared_scope"]) || !keys(&origin["declared_scope"],&["kind"]) || origin["declared_scope"]["kind"]!="selected_item" || !keys(knowledge,&["text","category","text_sha256"]) {return Err(invalid())}
    let reference=origin["item_ref"].as_str().and_then(|v|v.strip_prefix("knowledge-")).filter(|v|hash(v)).ok_or_else(invalid)?;let _=reference;
    if !origin["value_sha256"].as_str().is_some_and(hash) || !origin["revision"].as_u64().is_some_and(|v|(1..=9_007_199_254_740_991).contains(&v)) {return Err(invalid())}
    let category=match origin["record_type"].as_str() {Some("fact")=>"fact",Some("persona")=>"identity",Some("instruction")=>"preference",Some("episodic")=>"task",_=>return Err(invalid())};
    let text=knowledge["text"].as_str().ok_or_else(invalid)?;
    if text.is_empty() || text.len()>16384 || text.chars().count()>5000 || text.contains('\0') || text.trim_matches(|c:char|c.is_whitespace()||matches!(c,'\u{1c}'..='\u{1f}'))!=text || knowledge["category"]!=category || knowledge["text_sha256"]!=sha256_hex(text.as_bytes()) || proposal["text"]!=text || proposal["category"]!=category || descriptor["category"]!=category || descriptor["text_sha256"]!=knowledge["text_sha256"] || descriptor["origin"]!=*origin {return Err(invalid())}
    let package_bytes=canonical(&package)?;budget.check(package_bytes.len())?;
    if package_bytes.len()>65536 || descriptor["canonical_sha256"]!=sha256_hex(&package_bytes) {return Err(invalid())}
    let action=descriptor["action_id"].as_str().ok_or_else(invalid)?;
    if !uuid::Uuid::parse_str(action).ok().is_some_and(|v|v.hyphenated().to_string()==action) {return Err(invalid())}
    let review=json!({"version":"s10.curated-knowledge-review.v1","account_id":account,"workspace_id":workspace,"action_id":action,"mode":"review_only","raw_sha256":content_hash,"canonical_sha256":descriptor["canonical_sha256"],"text_sha256":descriptor["text_sha256"],"category":category,"origin":origin});
    let review_bytes=canonical(&review)?;budget.check(review_bytes.len())?;
    if descriptor["review_sha256"]!=sha256_hex(&review_bytes) || descriptor["review_sha256"]!=identity_context_hash {return Err(invalid())}
    let descriptor_bytes=canonical(descriptor)?;let proposal_bytes=canonical(proposal)?;budget.check(descriptor_bytes.len().saturating_add(proposal_bytes.len()))?;
    if descriptor_bytes.len()>256*1024 || proposal_bytes.len()>320*1024 {return Err(invalid())}
    Ok(descriptor.clone())
}

/// Validate the original descriptor before a staged suggestion exists.
pub(crate) fn validate_root(descriptor:&Value,account:&str,workspace:&str,content_hash:&str,byte_size:u64,identity_context_hash:&str,budget:&mut ResetFenceBudget)->Result<Value,String> {
    let raw=descriptor.get("raw_json").and_then(Value::as_str).ok_or_else(invalid)?;
    if raw.len()>65536{return Err(invalid())} budget.check(raw.len())?;
    let package=crate::field_authority::parse_unique_json(raw).map_err(|_|invalid())?;
    let text=package.pointer("/knowledge/text").and_then(Value::as_str).ok_or_else(invalid)?;
    let category=package.pointer("/knowledge/category").and_then(Value::as_str).ok_or_else(invalid)?;
    validate_transfer(&json!({"text":text,"category":category,"knowledge_snapshot":descriptor}),PROFILE,account,workspace,content_hash,byte_size,identity_context_hash,budget)
}

/// Safe materialized projection. The fully verified descriptor remains only in
/// the private import witness; it is not copied into searchable memory metadata.
pub(crate) fn safe_provenance(provenance:&Value)->Result<Value,String> {
    let mut out=provenance.clone();
    let Some(descriptor)=provenance.get("knowledge_snapshot") else {return Ok(out)};
    if !keys(descriptor,&["version","raw_json","raw_sha256","canonical_sha256","text_sha256","category","origin","account_id","workspace_id","action_id","mode","review_sha256"]) || descriptor["version"].as_u64()!=Some(1) || descriptor["mode"]!="review_only" {return Err(invalid())}
    for key in ["raw_sha256","canonical_sha256","text_sha256","review_sha256"] {if !descriptor[key].as_str().is_some_and(hash){return Err(invalid())}}
    let safe=json!({"version":1,"profile":PROFILE,"origin":descriptor["origin"],"category":descriptor["category"],"raw_sha256":descriptor["raw_sha256"],"canonical_sha256":descriptor["canonical_sha256"],"text_sha256":descriptor["text_sha256"],"review_sha256":descriptor["review_sha256"],"origin_status":"unverified"});
    out.as_object_mut().ok_or_else(invalid)?.insert("knowledge_snapshot".into(),safe);Ok(out)
}
