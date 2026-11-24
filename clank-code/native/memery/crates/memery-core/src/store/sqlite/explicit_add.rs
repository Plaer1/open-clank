//! Private, original-action-bound named-human Add. The caller owns human/session
//! authority; keys identify an action and never grant authority.
use super::*;
use crate::repository::{self, PolicyPublicationRefs};
use crate::repository::explicit_add::{ExplicitAddProposal, create_explicit_add_proposal, prepare_explicit_add, finish_explicit_add};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
#[serde(rename_all="snake_case")]
pub enum ExplicitAddMode { Manual, ReviewOnly }

#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExplicitAddRequest {
    pub version:u32, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub request_key:String, pub content:String, pub category:String,
    pub source:String, pub question_context:Option<Value>, pub mode:ExplicitAddMode, pub policy_projection:PolicyPublicationRefs,
}
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExplicitAddStatusRequest {
    pub version:u32, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub request_key:String, pub input_sha256:String, pub policy_projection:PolicyPublicationRefs,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Original {
    version:String, owner:String, workspace_id:String, account_id:String, request_key:String,
    request_sha256:String, input_sha256:String, content_sha256:String, candidate_id:String, mode:ExplicitAddMode, category:String, kind:MemoryKind,
}
fn valid_text(s:&str,max:usize)->bool { !s.is_empty() && s==s.trim() && s.len()<=max && !s.chars().any(char::is_control) }
fn hash(s:&[u8])->String { repository::sha256_hex(s) }
fn contains_not_stored(v:&Value)->bool {
    match v { Value::String(s)=>s==crate::privacy::NOT_STORED,
        Value::Array(a)=>a.iter().any(contains_not_stored),
        Value::Object(o)=>o.values().any(contains_not_stored), _=>false }
}
fn ids(_owner:&str,workspace:&str,account:&str,key:&str)->Result<(String,String),String> {
    let b=serde_json::to_vec(&("s03.explicit-add-id.v1",account,workspace,key)).map_err(|e|e.to_string())?;
    let h=hash(&b); Ok((format!("manualadd_{h}"),format!("cs_explicit_add_{h}")))
}
fn validate_scope(version:u32,owner:&str,workspace:&str,account:&str,key:&str)->Result<(),String> {
    if version!=1 || !valid_text(owner,512) || !valid_text(workspace,512) || !valid_text(account,512) || !valid_text(key,512) { return Err("explicit add identity unavailable".into()); } Ok(())
}
fn original(tx:&rusqlite::Transaction<'_>,owner:&str,receipt:&str)->Result<Option<Original>,String> {original_at(tx,owner,owner,receipt)}
fn original_at(tx:&rusqlite::Transaction<'_>,sql_owner:&str,owner:&str,receipt:&str)->Result<Option<Original>,String> {
    let row:Option<(String,String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END,reason,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?",params![sql_owner,receipt],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional().map_err(|e|e.to_string())?;
    let Some((digest,raw,reason,actor_type,actor_id,contract,source_event))=row else { return Ok(None) };
    if reason!="s03.explicit-add.v1" || actor_type!="user" || actor_id!=owner || contract!=crate::ontology::CONTRACT_VERSION || source_event.is_some() || hash(raw.as_bytes())!=digest {return Err("explicit add receipt unavailable".into());}
    let parsed=crate::field_authority::parse_unique_json(&raw).map_err(|_|"explicit add receipt unavailable".to_string())?;
    let item:Original=serde_json::from_value(parsed).map_err(|_|"explicit add receipt unavailable".to_string())?;
    let event:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='explicit_add'",params![sql_owner,format!("outbox_{receipt}_explicit_add"),receipt],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|e|e.to_string())?;
    if item.version!="s03.explicit-add.v1" || event.as_ref().is_none_or(|(payload,rev)|payload!=&raw || *rev!=0) {return Err("explicit add receipt unavailable".into());} Ok(Some(item))
}
/// Verify a terminal marker independently of the erased original receipt.
fn terminal_marker(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str,key:&str,input_hash:&str)->Result<bool,String> {
    let marker=repository::explicit_add::read_explicit_add_terminal(tx,owner,workspace,account,candidate).map_err(|_|"explicit add terminal unavailable".to_string())?;
    let Some(marker)=marker else {return Ok(false)};
    let key_hash=repository::explicit_add::explicit_add_key_sha256(account,workspace,key).map_err(|_|"explicit add identity unavailable".to_string())?;
    if marker.original_key_sha256!=key_hash || marker.input_sha256!=input_hash {return Err("explicit add identity conflict".into());}
    Ok(true)
}
/// Prove the original action before an existing owner reviews or erases it.
/// The caller already holds its current account and policy authority; this
/// reads no candidate/proposal contents and never treats a key as a grant.
pub(super) fn validate_explicit_add_original(
    tx:&rusqlite::Transaction<'_>, owner:&str, workspace:&str, account:&str,
    candidate_id:&str, request_key:&str, input_sha256:&str,
)->Result<ExplicitAddMode,String> {
    validate_scope(1,owner,workspace,account,request_key)?;
    if input_sha256.len()!=64 || !input_sha256.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("explicit add hash unavailable".into());}
    let (candidate,receipt)=ids(owner,workspace,account,request_key)?;
    if candidate!=candidate_id {return Err("explicit add identity conflict".into());}
    if terminal_marker(tx,owner,workspace,account,candidate_id,request_key,input_sha256)? {return Err("explicit add already erased".into());}
    let item=original(tx,owner,&receipt)?.ok_or("explicit add receipt unavailable")?;
    if item.owner!=owner || item.workspace_id!=workspace || item.account_id!=account || item.request_key!=request_key || item.candidate_id!=candidate_id || item.input_sha256!=input_sha256 {return Err("explicit add identity conflict".into());}
    Ok(item.mode)
}
/// Validate the exact captured Add receipt through the current account custody chain.
pub(super) fn validate_explicit_add_original_custody(tx:&rusqlite::Transaction<'_>,sql_owner:&str,captured:&str,workspace:&str,account:&str,candidate_id:&str,request_key:&str,input_sha256:&str,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>)->Result<ExplicitAddMode,String> {
    validate_scope(1,captured,workspace,account,request_key)?;
    let proof=view.for_captured(tx,captured).map_err(|_|"explicit add custody unavailable")?;
    if proof.current_owner()!=sql_owner || proof.account_id()!=account || input_sha256.len()!=64 || !input_sha256.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("explicit add custody unavailable".into());}
    let (candidate,receipt)=ids(captured,workspace,account,request_key)?;if candidate!=candidate_id{return Err("explicit add identity conflict".into());}
    if repository::explicit_add::read_explicit_add_terminal_custody(tx,workspace,account,candidate_id,captured,view).map_err(|_|"explicit add terminal unavailable")?.is_some() {return Err("explicit add already erased".into());}
    let item=original_at(tx,sql_owner,captured,&receipt)?.ok_or("explicit add receipt unavailable")?;
    if item.owner!=captured || item.workspace_id!=workspace || item.account_id!=account || item.request_key!=request_key || item.candidate_id!=candidate_id || item.input_sha256!=input_sha256 {return Err("explicit add identity conflict".into());}Ok(item.mode)
}
fn issuance_orphan(tx:&rusqlite::Transaction<'_>,owner:&str,candidate:&str,receipt:&str)->Result<bool,String> {
    // Only Add's mutex-owned transactions call this helper; they do not install
    // a competing progress handler. Drop restores the connection limit and
    // removes the handler before any write or later transaction operation.
    let mut budget=repository::ResetFenceBudget::install_bounded(tx,std::time::Duration::from_secs(1));
    let terminal_id=format!("outbox_s04_explicit_add_erased_{}",hash(&serde_json::to_vec(&(owner,candidate)).map_err(|e|e.to_string())?));
    let original_event=format!("outbox_{receipt}_explicit_add");
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE change_set_id=?1) OR EXISTS(SELECT 1 FROM candidates WHERE id=?2) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE event_id=?3 OR (owner_id=?4 AND event_id=?5))",params![receipt,candidate,original_event,owner,terminal_id],|r|r.get(0)).map_err(|_|"explicit add custody unavailable".to_string())?;
    budget.check(0).map_err(|_|"explicit add custody unavailable".to_string())?;
    Ok(exists)
}
fn status(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,key:&str,input_hash:&str)->Result<Value,String> {
    let (candidate,receipt)=ids(owner,workspace,account,key)?;
    if terminal_marker(tx,owner,workspace,account,&candidate,key,input_hash)? {return Ok(json!({"state":"terminal_erased","candidate_id":candidate,"input_sha256":input_hash}));}
    let Some(item)=original(tx,owner,&receipt)? else {
        let exists=issuance_orphan(tx,owner,&candidate,&receipt)?;
        if exists {return Err("explicit add custody unavailable".into());}
        return Ok(json!({"state":"absent"}));
    };
    if item.owner!=owner {return Err("explicit add custody unavailable".into());}
    if item.workspace_id!=workspace || item.account_id!=account || item.request_key!=key || item.candidate_id!=candidate || item.input_sha256!=input_hash {return Err("explicit add identity conflict".into());}
    // Original receipt and outbox were committed atomically with creation.
    // Later edits, review, or question answers cannot change that fact.
    let state=if item.mode==ExplicitAddMode::Manual {"applied"} else {"staged"};
    Ok(json!({"state":state,"candidate_id":candidate,"input_sha256":input_hash}))
}
fn charge_original_custody(tx:&rusqlite::Transaction<'_>,owner:&str,receipt:&str,budget:&mut repository::ResetFenceBudget)->Result<(),String> {
    let sizes:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='explicit_add' WHERE c.owner_id=?1 AND c.change_set_id=?2",params![owner,receipt,format!("outbox_{receipt}_explicit_add")],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|"explicit add custody unavailable")?;
    let (a,b)=sizes.ok_or("explicit add custody unavailable")?;
    if !(0..=8192).contains(&a) || !(0..=8192).contains(&b) {return Err("explicit add custody unavailable".into());}
    budget.check((a as usize).saturating_add(b as usize))?;Ok(())
}
/// Validate one indexed original event as a factual rename inventory member.
pub(crate) fn verify_original_event(tx:&rusqlite::Transaction<'_>,current:&str,account:&str,event_id:&str,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<(),String> {
    if view.current_owner()!=current || view.account_id()!=account {return Err("explicit add custody unavailable".into());}
    let receipt=event_id.strip_prefix("outbox_").and_then(|v|v.strip_suffix("_explicit_add")).filter(|v|v.strip_prefix("cs_explicit_add_").is_some_and(|h|h.len()==64 && h.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)))).ok_or("explicit add custody unavailable")?;
    charge_original_custody(tx,current,receipt,budget)?;
    let raw:String=tx.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![current,receipt],|r|r.get(0)).map_err(|_|"explicit add custody unavailable")?;
    let item:Original=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"explicit add custody unavailable")?).map_err(|_|"explicit add custody unavailable")?;
    view.for_captured(tx,&item.owner).map_err(|_|"explicit add custody unavailable")?;
    validate_scope(1,&item.owner,&item.workspace_id,&item.account_id,&item.request_key)?;
    let (candidate,expected)=ids(&item.owner,&item.workspace_id,&item.account_id,&item.request_key)?;
    let is_hash=|v:&str|v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b));
    if item.account_id!=account || candidate!=item.candidate_id || expected!=receipt || ![&item.request_sha256,&item.input_sha256,&item.content_sha256].iter().all(|v|is_hash(v)) || !matches!(item.category.as_str(),"fact"|"contact"|"project"|"goal"|"identity"|"preference"|"task"|"unknown") {return Err("explicit add custody unavailable".into());}
    let checked=original_at(tx,current,&item.owner,receipt)?.ok_or("explicit add custody unavailable")?;
    if checked.candidate_id!=item.candidate_id || checked.input_sha256!=item.input_sha256 {return Err("explicit add custody unavailable".into());}
    Ok(())
}

/// Exact original Add outcome in current SQL custody. A missing receipt is
/// absent only after the caller's existing budget proves no global orphan.
pub(super) fn add_status_custody(tx:&rusqlite::Transaction<'_>,r:&ExplicitAddStatusRequest,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    validate_scope(r.version,&r.owner,&r.workspace_id,&r.account_id,&r.request_key)?;
    if r.owner!=view.current_owner() || r.account_id!=view.account_id() || r.input_sha256.len()!=64 || !r.input_sha256.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("explicit add custody unavailable".into());}
    view.for_captured(tx,&r.owner).map_err(|_|"explicit add custody unavailable")?;
    repository::require_guarded_epoch(tx).map_err(|_|"explicit add custody unavailable")?;
    repository::require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection)).map_err(|_|"explicit add custody unavailable")?;
    let (candidate,receipt)=ids(&r.owner,&r.workspace_id,&r.account_id,&r.request_key)?;
    let key_hash=repository::explicit_add::explicit_add_key_sha256(&r.account_id,&r.workspace_id,&r.request_key).map_err(|_|"explicit add custody unavailable")?;
    let mut terminal=None;
    for captured in view.owners() {
        budget.check(captured.len())?;
        if let Some(m)=repository::explicit_add::read_explicit_add_terminal_custody(tx,&r.workspace_id,&r.account_id,&candidate,captured,view).map_err(|_|"explicit add custody unavailable")? {
            if terminal.replace(m).is_some() {return Err("explicit add custody unavailable".into());}
        }
    }
    let forgotten=repository::guarded_batch_forget::read_forgotten_member(tx,&r.owner,&r.account_id,&r.workspace_id,&candidate,view,budget).map_err(|_|"explicit add custody unavailable")?;
    if let Some(fact)=forgotten {
        if terminal.is_some() || fact.original_key_sha256!=key_hash || fact.input_sha256!=r.input_sha256 {return Err("explicit add identity conflict".into());}
        return Ok(json!({"state":"terminal_erased","candidate_id":candidate,"input_sha256":r.input_sha256}));
    }
    if let Some(m)=terminal {
        if m.original_key_sha256!=key_hash || m.input_sha256!=r.input_sha256 {return Err("explicit add identity conflict".into());}
        return Ok(json!({"state":"terminal_erased","candidate_id":candidate,"input_sha256":r.input_sha256}));
    }
    let row:Option<Option<String>>=tx.query_row("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,receipt],|row|row.get(0)).optional().map_err(|_|"explicit add custody unavailable")?;
    if let Some(raw)=row {
        let raw=raw.ok_or("explicit add custody unavailable")?;charge_original_custody(tx,&r.owner,&receipt,budget)?;
        let item:Original=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"explicit add custody unavailable")?).map_err(|_|"explicit add custody unavailable")?;
        view.for_captured(tx,&item.owner).map_err(|_|"explicit add custody unavailable")?;
        let item=original_at(tx,&r.owner,&item.owner,&receipt)?.ok_or("explicit add custody unavailable")?;
        if item.workspace_id!=r.workspace_id || item.account_id!=r.account_id || item.request_key!=r.request_key || item.candidate_id!=candidate || item.input_sha256!=r.input_sha256 {return Err("explicit add identity conflict".into());}
        return Ok(json!({"state":if item.mode==ExplicitAddMode::Manual {"applied"} else {"staged"},"candidate_id":candidate,"input_sha256":r.input_sha256}));
    }
    let event=format!("outbox_{receipt}_explicit_add");
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE change_set_id=?1) OR EXISTS(SELECT 1 FROM candidates WHERE id=?2) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE event_id=?3)",params![receipt,candidate,event],|row|row.get(0)).map_err(|_|"explicit add custody unavailable")?;
    budget.check(0)?;if orphan {return Err("explicit add custody unavailable".into());}Ok(json!({"state":"absent"}))
}
/// Reused input is status-only; only a proven absent key reaches the
/// current-owner producer, within this same borrowed transaction.
pub(super) fn add_submit_custody(tx:&rusqlite::Transaction<'_>,r:&ExplicitAddRequest,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    validate_scope(r.version,&r.owner,&r.workspace_id,&r.account_id,&r.request_key)?;
    if r.content.is_empty() || r.content.chars().count()>5000 || r.content!=r.content.trim() || r.source.len()>8192 || !matches!(r.category.as_str(),"fact"|"contact"|"project"|"goal"|"identity"|"preference"|"task"|"unknown") {return Err("explicit add content unavailable".into());}
    #[derive(Serialize)] struct Input<'a>{category:&'a str,question_context:&'a Option<Value>,source:&'a str,text:&'a str}
    let input=hash(&serde_json::to_vec(&Input{category:&r.category,question_context:&r.question_context,source:&r.source,text:&r.content}).map_err(|e|e.to_string())?);
    let status=ExplicitAddStatusRequest{version:r.version,owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),account_id:r.account_id.clone(),request_key:r.request_key.clone(),input_sha256:input,policy_projection:r.policy_projection.clone()};
    let outcome=add_status_custody(tx,&status,view,budget)?;
    if outcome["state"]=="absent" {SqliteStore::add_create_current_in_tx(tx,r,Some(budget))} else {Ok(outcome)}
}

// These custody helpers borrow the caller's transaction and progress budget;
// they are factual only until the caller establishes current human authority.
fn original_candidate_custody(tx:&rusqlite::Transaction<'_>,s:&super::ExplicitAddReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Original,String> {
    if s.version!=1 || s.owner!=view.current_owner() || s.account_id!=view.account_id() {return Err("explicit add custody unavailable".into());}
    view.for_captured(tx,&s.owner).map_err(|_|"explicit add custody unavailable")?;
    let suffix=s.candidate_id.strip_prefix("manualadd_").filter(|v|v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))).ok_or("explicit add custody unavailable")?;
    let id=format!("cs_explicit_add_{suffix}");
    charge_original_custody(tx,&s.owner,&id,budget)?;
    let raw:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![s.owner,id],|r|r.get(0)).optional().map_err(|_|"explicit add custody unavailable")?.flatten();
    let item:Original=serde_json::from_value(crate::field_authority::parse_unique_json(&raw.ok_or("explicit add custody unavailable")?).map_err(|_|"explicit add custody unavailable")?).map_err(|_|"explicit add custody unavailable")?;
    view.for_captured(tx,&item.owner).map_err(|_|"explicit add custody unavailable")?;
    let checked=original_at(tx,&s.owner,&item.owner,&id)?.ok_or("explicit add custody unavailable")?;
    if checked.workspace_id!=s.workspace_id || checked.account_id!=s.account_id || checked.candidate_id!=s.candidate_id || ids(&checked.owner,&checked.workspace_id,&checked.account_id,&checked.request_key)?.0!=s.candidate_id {return Err("explicit add custody unavailable".into());}
    Ok(checked)
}
fn review_facts_custody(tx:&rusqlite::Transaction<'_>,s:&super::ExplicitAddReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    if s.owner!=view.current_owner() || s.account_id!=view.account_id() {return Err("explicit add custody unavailable".into());}
    view.for_captured(tx,&s.owner).map_err(|_|"explicit add custody unavailable")?;
    let mut terminal=None;let mut receipt=None;
    for captured in view.owners() {
        budget.check(captured.len())?;
        if let Some(marker)=repository::explicit_add::read_explicit_add_terminal_custody(tx,&s.workspace_id,&s.account_id,&s.candidate_id,captured,view).map_err(|_|"explicit add custody unavailable")? {if terminal.replace(marker).is_some() {return Err("explicit add custody unavailable".into());}}
        if let Some(item)=repository::explicit_add::read_explicit_add_review_receipt_custody(tx,&s.workspace_id,&s.account_id,&s.candidate_id,captured,view).map_err(|_|"explicit add custody unavailable")? {if receipt.replace(item).is_some() {return Err("explicit add custody unavailable".into());}}
    }
    let forgotten=repository::guarded_batch_forget::read_forgotten_member(tx,&s.owner,&s.account_id,&s.workspace_id,&s.candidate_id,view,budget).map_err(|_|"explicit add custody unavailable")?;
    if forgotten.is_some() && (terminal.is_some() || receipt.is_some()){return Err("explicit add custody unavailable".into());}
    if terminal.is_some() || forgotten.is_some() {return Ok(json!({"state":"terminal_erased","candidate_id":s.candidate_id}));}
    Ok(match receipt {None=>json!({"state":"absent"}),Some(r)=>json!({"state":"found","original":{"candidate_id":r.candidate,"expected_revision":r.expected_revision,"expected_binding":r.expected_binding,"review_key":r.review_key,"action":r.action},"receipt":{"state":if r.action=="accept" {"applied"} else {"rejected"},"candidate_id":s.candidate_id,"curated_id":r.curated_id}})})
}
pub(super) fn review_original_custody(tx:&rusqlite::Transaction<'_>,s:&super::ExplicitAddReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    repository::require_guarded_epoch(tx).map_err(|_|"explicit add custody unavailable")?;
    repository::require_policy_projection(tx,&s.account_id,&s.workspace_id,Some(&s.policy_projection)).map_err(|_|"explicit add custody unavailable")?;
    review_facts_custody(tx,s,view,budget)
}
pub(super) fn review_preview_custody(tx:&rusqlite::Transaction<'_>,s:&super::ExplicitAddReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    if review_original_custody(tx,s,view,budget)?["state"]!="absent" {return Err("explicit add review unavailable".into());}
    let original=original_candidate_custody(tx,s,view,budget)?;if original.mode!=ExplicitAddMode::ReviewOnly {return Err("explicit add review unavailable".into());}
    let (binding,display)=repository::explicit_add::preview_explicit_add_proposal_custody(tx,&s.workspace_id,&s.candidate_id,&original.owner,view).map_err(|_|"explicit add review unavailable")?;
    if binding.proposal.request_key!=original.request_key || binding.proposal.input_sha256!=original.input_sha256 {return Err("explicit add review unavailable".into());}
    let mut visible=serde_json::to_value(display).map_err(|_|"explicit add review unavailable")?;
    visible.as_object_mut().ok_or("explicit add review unavailable")?.insert("owner".into(),json!(s.owner));
    Ok(json!({"state":"preview","candidate_id":s.candidate_id,"proposal":visible,"expected_revision":binding.expected_revision,"binding_sha256":binding.binding_sha256}))
}
pub(super) fn review_edit_custody(tx:&rusqlite::Transaction<'_>,request:&super::ExplicitAddReviewEditRequest,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    let s=&request.selector;let preview=review_preview_custody(tx,s,view,budget)?;
    if preview["expected_revision"]!=request.expected_revision || preview["binding_sha256"]!=request.expected_binding {return Err("explicit add review changed".into());}
    let original=original_candidate_custody(tx,s,view,budget)?;
    let (mut next,_)=repository::explicit_add::current_explicit_add_proposal_custody(tx,&s.workspace_id,&s.candidate_id,&original.owner,view).map_err(|_|"explicit add review unavailable")?;
    let (text,redacted)=crate::privacy::filter_text(&request.text).ok_or("explicit add edit unavailable")?;let (source,sredacted)=crate::privacy::filter_text(&request.source).ok_or("explicit add edit unavailable")?;
    let (context,credacted)=crate::privacy::filter_json(&serde_json::to_value(&request.question_context).map_err(|e|e.to_string())?);
    if redacted || sredacted || credacted!=0 {return Err("explicit add edit requires filtered input".into());}
    next.text=text;next.category=request.category.clone();next.source=source;next.question_context=serde_json::from_value(context).map_err(|e|e.to_string())?;
    next.stored_input_sha256=hash(serde_json::to_string(&json!({"category":next.category,"question_context":next.question_context,"source":next.source,"text":next.text})).map_err(|e|e.to_string())?.as_bytes());
    repository::explicit_add::edit_explicit_add_proposal_custody(tx,&next,request.expected_revision,&request.expected_binding,&s.policy_projection,view).map_err(|_|"explicit add review unavailable")?;
    review_preview_custody(tx,s,view,budget)
}

pub(super) fn review_status_custody(tx:&rusqlite::Transaction<'_>,request:&super::ExplicitAddReviewApplyRequest,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    let s=&request.selector;let result=review_original_custody(tx,s,view,budget)?;
    if result["state"]=="found" {let original=&result["original"];if original["expected_revision"]!=request.expected_revision || original["expected_binding"]!=request.expected_binding || original["review_key"]!=request.review_key || original["action"]!=request.action {return Err("explicit add review identity conflict".into());}return Ok(result["receipt"].clone());}
    if result["state"]=="terminal_erased" {
        if let Some(fact)=repository::guarded_batch_forget::read_forgotten_member(tx,&s.owner,&s.account_id,&s.workspace_id,&s.candidate_id,view,budget).map_err(|_|"explicit add terminal unavailable")? {
            let key=repository::explicit_add::explicit_add_review_key_sha256(&s.account_id,&s.workspace_id,&s.candidate_id,&request.review_key).map_err(|_|"explicit add terminal unavailable")?;
            if fact.review_key_sha256!=key || request.action!="accept" || fact.review_revision!=request.expected_revision || fact.review_binding!=request.expected_binding {return Err("explicit add review identity conflict".into());}
            return Ok(result);
        }
        let mut terminal=None;for captured in view.owners() {budget.check(captured.len())?;if let Some(m)=repository::explicit_add::read_explicit_add_terminal_custody(tx,&s.workspace_id,&s.account_id,&s.candidate_id,captured,view).map_err(|_|"explicit add terminal unavailable")? {if terminal.replace(m).is_some() {return Err("explicit add terminal unavailable".into());}}}
        let m=terminal.ok_or("explicit add terminal unavailable")?;let key=repository::explicit_add::explicit_add_review_key_sha256(&s.account_id,&s.workspace_id,&s.candidate_id,&request.review_key).map_err(|_|"explicit add terminal unavailable")?;
        if m.review_key_sha256.as_deref()!=Some(key.as_str()) || m.review_action.as_deref()!=Some(request.action.as_str()) || m.review_revision!=Some(request.expected_revision) || m.review_binding.as_deref()!=Some(request.expected_binding.as_str()) {return Err("explicit add review identity conflict".into());}
    }
    Ok(result)
}
/// Caller owns one immediate transaction and commits only after successful work.
pub(super) fn review_apply_custody(tx:&rusqlite::Transaction<'_>,request:&super::ExplicitAddReviewApplyRequest,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut repository::ResetFenceBudget)->Result<Value,String> {
    SqliteStore::explicit_add_review_identity(tx,request)?;let s=&request.selector;
    let facts=review_facts_custody(tx,s,view,budget)?;
    if repository::require_policy_projection(tx,&s.account_id,&s.workspace_id,Some(&s.policy_projection)).is_err() {
        if facts["state"]!="absent" {return Err("explicit add review authority unavailable".into());}
        let original=original_candidate_custody(tx,s,view,budget)?;if original.mode!=ExplicitAddMode::ReviewOnly {return Err("explicit add review unavailable".into());}
        return Ok(SqliteStore::explicit_add_review_not_applied(request,"policy_changed"));
    }
    if facts["state"]!="absent" {return review_status_custody(tx,request,view,budget);}
    let original=original_candidate_custody(tx,s,view,budget)?;if original.mode!=ExplicitAddMode::ReviewOnly {return Err("explicit add review unavailable".into());}
    let binding=repository::explicit_add::explicit_add_review_binding_custody(tx,&s.workspace_id,&s.candidate_id,&original.owner,view).map_err(|_|"explicit add review unavailable")?;
    if binding.proposal.input_sha256!=original.input_sha256 || binding.proposal.request_key!=original.request_key {return Err("explicit add review unavailable".into());}
    if binding.expected_revision!=request.expected_revision || binding.binding_sha256!=request.expected_binding {return Ok(SqliteStore::explicit_add_review_not_applied(request,"conflict"));}
    if request.action=="reject" {
        repository::explicit_add::reject_explicit_add_proposal_custody(tx,&s.workspace_id,&s.candidate_id,&original.owner,request.expected_revision,&request.expected_binding,&s.policy_projection,view).map_err(|_|"explicit add review unavailable")?;
        repository::explicit_add::write_explicit_add_review_receipt(tx,&s.owner,&s.workspace_id,&s.account_id,&s.candidate_id,&request.review_key,request.expected_revision,&request.expected_binding,"reject",None).map_err(|_|"explicit add review unavailable")?;
        return Ok(json!({"state":"rejected","candidate_id":s.candidate_id,"curated_id":null}));
    }
    let prepared=repository::explicit_add::prepare_bound_explicit_add_review_custody(tx,&s.workspace_id,&s.candidate_id,&original.owner,request.expected_revision,&request.expected_binding,&s.policy_projection,view).map_err(|_|"explicit add review unavailable")?;let record=&prepared.record;
    let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1) OR EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='curated' AND original_id=?1)",[&record.id],|r|r.get(0)).map_err(|_|"explicit add review unavailable")?;
    let graph=crate::graph::GraphScope::new(&s.owner,&s.workspace_id).map_err(|_|"explicit add review unavailable")?;let node=graph.node_id("memory",&record.id);
    let graph_collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='graph_node' AND original_id=?1) OR EXISTS(SELECT 1 FROM graph_nodes WHERE id=?1) OR EXISTS(SELECT 1 FROM graph_edges WHERE src_id=?1 OR dst_id=?1) OR EXISTS(SELECT 1 FROM graph_cues WHERE node_id=?1)",[&node],|r|r.get(0)).map_err(|_|"explicit add review unavailable")?;
    if collision || graph_collision {return Err("explicit add destination collision".into());}SqliteStore::upsert_curated_inner_scoped(tx,record,None,false)?;
    let saved:(String,String,Option<String>,String,String,String,String,String)=tx.query_row("SELECT content,metadata,owner,workspace_id,kind,source,source_type,tags FROM curated WHERE id=?",[&record.id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?))).map_err(|_|"explicit add review unavailable")?;
    if saved.0!=record.content || serde_json::from_str::<Value>(&saved.1).map_err(|_|"explicit add review unavailable")?!=record.metadata || saved.2.as_deref()!=Some(s.owner.as_str()) || saved.3!=s.workspace_id || saved.4!=format!("{:?}",record.kind).to_lowercase() || saved.5!=record.source || saved.6!=format!("{:?}",record.source_type).to_lowercase() || serde_json::from_str::<Value>(&saved.7).map_err(|_|"explicit add review unavailable")?!=json!(record.tags) {return Err("explicit add projection changed".into());}
    repository::explicit_add::finish_explicit_add_custody(tx,&prepared,&s.policy_projection,view).map_err(|_|"explicit add review unavailable")?;
    if tx.execute("UPDATE candidates SET status='accepted',reason=?1,accepted_curated_id=?2,updated_at=?3 WHERE id=?4 AND owner=?5 AND workspace_id=?6 AND status='pending'",params!["explicit human add",record.id,chrono::Utc::now().to_rfc3339(),s.candidate_id,s.owner,s.workspace_id]).map_err(|_|"explicit add review unavailable")?!=1 {return Err("explicit add candidate changed".into());}
    repository::explicit_add::write_explicit_add_review_receipt(tx,&s.owner,&s.workspace_id,&s.account_id,&s.candidate_id,&request.review_key,request.expected_revision,&request.expected_binding,"accept",Some(&record.id)).map_err(|_|"explicit add review unavailable")?;
    Ok(json!({"state":"applied","candidate_id":s.candidate_id,"curated_id":record.id}))
}

impl SqliteStore {
    pub fn explicit_add_status(&self, r:&ExplicitAddStatusRequest)->Result<Value,String> {
        validate_scope(r.version,&r.owner,&r.workspace_id,&r.account_id,&r.request_key)?;
        if r.input_sha256.len()!=64 || !r.input_sha256.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("explicit add hash unavailable".into());}
        let mut conn=self.conn.lock().map_err(|_|"explicit add unavailable".to_string())?;
        let tx=conn.transaction_with_behavior(TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        repository::require_guarded_epoch(&tx).map_err(|e|e.to_string())?;
        repository::require_policy_projection(&tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection)).map_err(|e|e.to_string())?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|"explicit add custody unavailable")?;
        let mut budget=repository::ResetFenceBudget::install(&tx);
        let keys=repository::owner_custody::account_custody_keys(&tx,&database,&r.account_id,&mut budget).map_err(|_|"explicit add custody unavailable")?;
        let result=if keys.is_empty() && self.guarded_epoch!=Some(18) {drop(budget);status(&tx,&r.owner,&r.workspace_id,&r.account_id,&r.request_key,&r.input_sha256)?} else {
            let view=repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,&r.owner,&r.owner,&mut budget).map_err(|_|"explicit add custody unavailable")?;
            let result=add_status_custody(&tx,r,&view,&mut budget)?;drop(view);drop(budget);result
        };
        tx.commit().map_err(|e|e.to_string())?; Ok(result)
    }
    pub fn explicit_add(&self,r:&ExplicitAddRequest)->Result<Value,String> {
        let mut conn=self.conn.lock().map_err(|_|"explicit add unavailable".to_string())?;
        let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|e|e.to_string())?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|"explicit add custody unavailable")?;
        let mut budget=repository::ResetFenceBudget::install(&tx);
        let keys=repository::owner_custody::account_custody_keys(&tx,&database,&r.account_id,&mut budget).map_err(|_|"explicit add custody unavailable")?;
        let result=if keys.is_empty() && self.guarded_epoch!=Some(18) {drop(budget);Self::add_create_current_in_tx(&tx,r,None)?} else {
            let view=repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,&r.owner,&r.owner,&mut budget).map_err(|_|"explicit add custody unavailable")?;
            let result=add_submit_custody(&tx,r,&view,&mut budget)?;drop(view);drop(budget);result
        };
        tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    /// Existing current-owner producer, called only within the caller's own
    /// transaction and progress budget; never reacquires a store mutex.
    pub(super) fn add_create_current_in_tx(tx:&rusqlite::Transaction<'_>,r:&ExplicitAddRequest,budget:Option<&mut repository::ResetFenceBudget>)->Result<Value,String> {
        validate_scope(r.version,&r.owner,&r.workspace_id,&r.account_id,&r.request_key)?;
        let kind=match r.category.as_str() {
            "fact"|"contact"|"project"|"goal" => MemoryKind::Fact,
            "identity" => MemoryKind::Persona, "preference" => MemoryKind::Instruction,
            "task" => MemoryKind::Episodic, "unknown" => MemoryKind::Unknown,
            _ => return Err("explicit add category unavailable".into()),
        };
        if r.source.len()>8192 || r.content.is_empty() || r.content.chars().count()>5000 || r.content!=r.content.trim() {return Err("explicit add content unavailable".into());}
        let (filtered_text,_)=crate::privacy::filter_text(&r.content).ok_or("explicit add privacy unavailable")?;
        let (filtered_metadata,_)=crate::privacy::filter_json(&json!({"source":r.source,"question_context":r.question_context}));
        if contains_not_stored(&filtered_metadata) {return Err("explicit add privacy unavailable".into());}
        let filtered_source=filtered_metadata.get("source").and_then(Value::as_str).ok_or("explicit add source unavailable")?.to_string();
        let filtered_context=filtered_metadata.get("question_context").cloned().filter(|v|!v.is_null());
        let request_hash=hash(&serde_json::to_vec(r).map_err(|e|e.to_string())?);
        // Struct order is the sorted-key canonical Host JSON order.
        #[derive(Serialize)]
        struct Input<'a> { category:&'a str, question_context:&'a Option<Value>, source:&'a str, text:&'a str }
        let input_hash=hash(&serde_json::to_vec(&Input{category:&r.category,question_context:&r.question_context,source:&r.source,text:&r.content}).map_err(|e|e.to_string())?);
        let stored_input_hash=hash(&serde_json::to_vec(&Input{category:&r.category,question_context:&filtered_context,source:&filtered_source,text:&filtered_text}).map_err(|e|e.to_string())?);
        let (candidate_id,receipt_id)=ids(&r.owner,&r.workspace_id,&r.account_id,&r.request_key)?;
        repository::require_guarded_epoch(tx).map_err(|e|e.to_string())?;
        repository::require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection)).map_err(|e|e.to_string())?;
        if terminal_marker(tx,&r.owner,&r.workspace_id,&r.account_id,&candidate_id,&r.request_key,&input_hash)? {
            return Ok(json!({"state":"terminal_erased","candidate_id":candidate_id,"input_sha256":input_hash}));
        }
        if let Some(previous)=original(tx,&r.owner,&receipt_id)? {
            if previous.owner!=r.owner {return Err("explicit add custody unavailable".into());}
            if previous.workspace_id!=r.workspace_id || previous.account_id!=r.account_id || previous.request_key!=r.request_key || previous.candidate_id!=candidate_id || previous.input_sha256!=input_hash {return Err("explicit add identity conflict".into());}
            return Err("explicit add issued; status only".into());
        }
        let collision=if let Some(budget)=budget {
            let terminal_id=format!("outbox_s04_explicit_add_erased_{}",hash(&serde_json::to_vec(&(&r.owner,&candidate_id)).map_err(|e|e.to_string())?));
            let found:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE change_set_id=?1) OR EXISTS(SELECT 1 FROM candidates WHERE id=?2) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE event_id=?3 OR (owner_id=?4 AND event_id=?5))",params![receipt_id,candidate_id,format!("outbox_{receipt_id}_explicit_add"),r.owner,terminal_id],|row|row.get(0)).map_err(|_|"explicit add custody unavailable")?;
            budget.check(0)?;found
        } else {issuance_orphan(tx,&r.owner,&candidate_id,&receipt_id)?};
        if collision {return Err("explicit add identity unavailable".into());}
        let now=chrono::Utc::now().to_rfc3339();
        let c=CandidateRecord{id:candidate_id.clone(),content:filtered_text.clone(),kind,confidence_score:0.95,importance_score:0.8,owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),workspace_path:None,session_id:String::new(),turn_id:String::new(),raw_evidence_ids:vec![],evidence_role:"user".into(),source:"user_created".into(),source_event_id:String::new(),dedup_key:candidate_id.clone(),status:CandidateStatus::Pending,reason:"awaiting_review".into(),accepted_curated_id:None,created_at:now.clone(),updated_at:now.clone(),source_uri:String::new(),source_revision:1,content_hash:crate::privacy::content_hash(&filtered_text),source_message_ids:vec![]};
        if !Self::insert_explicit_add_candidate_inner(tx,&c).map_err(|e|e.to_string())? {return Err("explicit add candidate unavailable".into());}
        let proposal=ExplicitAddProposal{version:"s03.explicit-add-proposal.v1".into(),owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),account_id:r.account_id.clone(),request_key:r.request_key.clone(),input_sha256:input_hash.clone(),stored_input_sha256:stored_input_hash,candidate_id:candidate_id.clone(),text:filtered_text.clone(),category:r.category.clone(),source:filtered_source,question_context:filtered_context};
        create_explicit_add_proposal(tx,&proposal).map_err(|e|e.to_string())?;
        if r.mode==ExplicitAddMode::Manual {
            let prepared=prepare_explicit_add(tx,&proposal).map_err(|e|e.to_string())?;
            let record=&prepared.record;
            let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1) OR EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='curated' AND original_id=?1)",[&record.id],|row|row.get(0)).map_err(|e|e.to_string())?;
            let scope=crate::graph::GraphScope::new(&r.owner,&r.workspace_id).map_err(|_|"explicit add graph scope unavailable")?;
            let node=scope.node_id("memory",&record.id);
            let graph_collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='graph_node' AND original_id=?1) OR EXISTS(SELECT 1 FROM graph_nodes WHERE id=?1) OR EXISTS(SELECT 1 FROM graph_edges WHERE src_id=?1 OR dst_id=?1) OR EXISTS(SELECT 1 FROM graph_cues WHERE node_id=?1)",[&node],|row|row.get(0)).map_err(|e|e.to_string())?;
            if collision || graph_collision {return Err("explicit add destination collision".into());}
            Self::upsert_curated_inner_scoped(tx,record,None,false)?;
            let saved:(String,String,Option<String>,String,String,String,String,String)=tx.query_row("SELECT content,metadata,owner,workspace_id,kind,source,source_type,tags FROM curated WHERE id=?",[&record.id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?,row.get(7)?))).map_err(|e|e.to_string())?;
            let saved_metadata:Value=serde_json::from_str(&saved.1).map_err(|e|e.to_string())?;
            let saved_tags:Value=serde_json::from_str(&saved.7).map_err(|e|e.to_string())?;
            if saved.0!=record.content || saved_metadata!=record.metadata || saved.2.as_deref()!=Some(r.owner.as_str()) || saved.3!=r.workspace_id || saved.4!=format!("{:?}",record.kind).to_lowercase() || saved.5!=record.source || saved.6!=format!("{:?}",record.source_type).to_lowercase() || saved_tags!=json!(record.tags) {return Err("explicit add curated projection changed".into());}
            finish_explicit_add(tx,&prepared,&r.policy_projection).map_err(|e|e.to_string())?;
            if tx.execute("UPDATE candidates SET status='accepted',reason=?,accepted_curated_id=?,updated_at=? WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params!["explicit human add",record.id,now,candidate_id,r.owner,r.workspace_id]).map_err(|e|e.to_string())?!=1 {return Err("explicit add candidate changed".into());}
        }
        let item=Original{version:"s03.explicit-add.v1".into(),owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),account_id:r.account_id.clone(),request_key:r.request_key.clone(),request_sha256:request_hash.clone(),input_sha256:input_hash.clone(),content_sha256:hash(filtered_text.as_bytes()),candidate_id:candidate_id.clone(),mode:r.mode,category:r.category.clone(),kind};
        let raw=serde_json::to_string(&item).map_err(|e|e.to_string())?;
        tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?,?,'user',?,'s03.explicit-add.v1',NULL,?,?,?,?)",params![r.owner,receipt_id,r.owner,crate::ontology::CONTRACT_VERSION,hash(raw.as_bytes()),now,raw]).map_err(|e|e.to_string())?;
        tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?,?,?,'explicit_add',?,0,?)",params![r.owner,format!("outbox_{receipt_id}_explicit_add"),receipt_id,raw,now]).map_err(|e|e.to_string())?;
        Ok(json!({"state":if r.mode==ExplicitAddMode::Manual {"applied"} else {"staged"},"candidate_id":candidate_id,"input_sha256":input_hash}))
    }
}
