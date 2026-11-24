//! Content-free original-key denials bound to the existing guarded S06 reset.
//! Presence denies only; absence never grants a new issue.
use super::*;
use serde::{Deserialize,Serialize};
use std::collections::BTreeSet;
const PREFIX:&str="outbox_s02_importance_child_erased_";
const KIND:&str="s02_importance_child_erased";
fn invalid()->String {"importance child terminal unavailable".into()}
fn text(v:&str)->bool {!v.is_empty() && v.trim()==v && v.len()<=512 && !v.chars().any(char::is_control)}
fn hex(v:&str)->bool {v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportanceChildTerminal {
    pub version:String,pub account_id:String,pub captured_owner:String,pub reset_owner:String,pub workspace_id:String,
    pub selector_sha256:String,pub root_sha256:String,pub operation_sha256:String,
    pub reset_workspace_id:String,pub reset_operation_id:String,pub reset_receipt_id:String,pub reset_request_sha256:String,
    pub mode:String,pub partition_sha256:Option<String>,
}
fn verify_reset(tx:&Transaction<'_>,owner:&str,account:&str,m:&ImportanceChildTerminal,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    if view.current_owner()!=owner || view.account_id()!=account || m.version!="s02.importance-child-erased.v1" || m.account_id!=account
        || ![m.account_id.as_str(),m.captured_owner.as_str(),m.reset_owner.as_str(),m.workspace_id.as_str(),m.reset_workspace_id.as_str(),m.reset_operation_id.as_str()].iter().all(|v|text(v))
        || ![m.selector_sha256.as_str(),m.root_sha256.as_str(),m.operation_sha256.as_str(),m.reset_request_sha256.as_str()].iter().all(|v|hex(v))
        || m.reset_receipt_id!=crate::repository::ingest_erased_receipt_id(account,&m.reset_operation_id).map_err(|_|invalid())?
        || !match (m.mode.as_str(),m.partition_sha256.as_deref()) {("full",None)=>true,("selective",Some(v))=>hex(v),_=>false} {return Err(invalid())}
    for captured in [&m.captured_owner,&m.reset_owner] {
        let proof=view.for_captured(tx,captured).map_err(|_|invalid())?;
        if proof.current_owner()!=owner || proof.account_id()!=account {return Err(invalid())}
    }
    let lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s06_guarded_reset' AND o.source_revision=0 WHERE c.owner_id=?1 AND c.change_set_id=?2",params![owner,m.reset_receipt_id,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id)],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|invalid())?;
    let (a,b)=lengths.ok_or_else(invalid)?;if !(1..=262144).contains(&a) || !(1..=262144).contains(&b) {return Err(invalid())} budget.check((a+b) as usize)?;
    let (hash,raw,actor,actor_id,contract,source,out):(String,String,String,String,String,Option<String>,String)=tx.query_row("SELECT c.content_hash,c.operation_metadata_json,c.actor_type,c.actor_id,c.contract_version,c.source_event_id,o.payload_json FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s06_guarded_reset' AND o.source_revision=0 WHERE c.owner_id=?1 AND c.change_set_id=?2 AND c.reason='s06.guarded-reset.v1'",params![owner,m.reset_receipt_id,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id)],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional().map_err(|_|invalid())?.ok_or_else(invalid)?;
    let receipt=crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?;
    if hash!=crate::repository::sha256_hex(raw.as_bytes()) || out!=raw || actor!="user" || actor_id!=m.reset_owner || contract!=crate::ontology::CONTRACT_VERSION || source.is_some()
        || receipt.as_object().map(|v|v.len())!=Some(4) || receipt["version"]!="s06.guarded-reset.v1"
        || receipt["request_sha256"]!=m.reset_request_sha256 || receipt["request_sha256"]!=crate::repository::sha256_hex(&serde_json::to_vec(&receipt["request"]).map_err(|_|invalid())?)
        || receipt["request"]["owner"]!=m.reset_owner || receipt["request"]["workspace_id"]!=m.reset_workspace_id
        || receipt["request"]["account_id"]!=account || receipt["request"]["operation_id"]!=m.reset_operation_id || receipt["result"]["complete"]!=true {return Err(invalid())}
    let components=receipt["request"]["components"].as_array().ok_or_else(invalid)?;
    let has=|v:&str|components.iter().any(|c|c.as_str()==Some(v));
    match m.mode.as_str() {
        "full" if has("memories") && receipt["request"].get("selective_manifest_sha256").is_none() && receipt["result"]["categories"]["memories"]["state"]=="complete"=>(),
        "selective" if has("ingest") && receipt["request"]["selective_manifest_sha256"].as_str().is_some_and(hex)
            && receipt["result"]["categories"]["ingest"]["state"]=="complete"
            && receipt["result"]["categories"]["ingest"]["selective"]["partition_sha256"].as_str()==m.partition_sha256.as_deref()=>(),
        _=>return Err(invalid()),
    }
    Ok(())
}

fn read_selector(tx:&Transaction<'_>,owner:&str,account:&str,selector:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Option<ImportanceChildTerminal>,String> {
    if !hex(selector) {return Err(invalid())}
    let event=format!("{PREFIX}{selector}");
    let len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event],|r|r.get(0)).optional().map_err(|_|invalid())?;
    let Some(len)=len else{return Ok(None)};if !(1..=8192).contains(&len){return Err(invalid())}budget.check(len as usize+event.len())?;
    let (id,kind,raw,revision,state,published):(String,String,String,i64,String,Option<String>)=tx.query_row("SELECT change_set_id,kind,payload_json,source_revision,state,published_at FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(|_|invalid())?;
    let m:ImportanceChildTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?).map_err(|_|invalid())?;
    if kind!=KIND || revision!=0 || state!="published" || published.as_deref().is_none_or(|v|v.is_empty()) || id!=m.reset_receipt_id || m.selector_sha256!=selector || serde_json::to_string(&m).map_err(|_|invalid())?!=raw {return Err(invalid())}
    verify_reset(tx,owner,account,&m,view,budget)?;Ok(Some(m))
}
pub(crate) fn probe(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,block:&str,key:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<bool,String> {
    crate::repository::require_guarded_epoch(tx).map_err(|_|invalid())?;
    if view.current_owner()!=owner || view.account_id()!=account {return Err(invalid())}
    let selector=crate::repository::s08_policy_review::importance_selector(account,workspace,block,key).map_err(|_|invalid())?;
    match read_selector(tx,owner,account,&selector,view,budget)? {Some(m) if m.workspace_id==workspace=>Ok(true),Some(_)=>Err(invalid()),None=>Ok(false)}
}
pub(super) fn write(tx:&Transaction<'_>,owner:&str,account:&str,m:&ImportanceChildTerminal,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    verify_reset(tx,owner,account,m,view,budget)?;
    let event=format!("{PREFIX}{}",m.selector_sha256);
    if read_selector(tx,owner,account,&m.selector_sha256,view,budget)?.is_some(){return Err(invalid())}
    let raw=serde_json::to_string(m).map_err(|_|invalid())?;if raw.len()>8192{return Err(invalid())}budget.check(raw.len()+event.len())?;
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,state,created_at,published_at) VALUES(?1,?2,?3,?4,?5,0,'published',?6,?6)",params![owner,event,m.reset_receipt_id,KIND,raw,now]).map_err(|_|invalid())?;
    if read_selector(tx,owner,account,&m.selector_sha256,view,budget)?.as_ref()!=Some(m){return Err(invalid())}Ok(())
}
pub(super) fn inventory(tx:&Transaction<'_>,owner:&str,account:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<ImportanceChildTerminal>,String> {
    let mut expected=BTreeSet::new();let mut values=Vec::new();let mut after=PREFIX.to_owned();let upper=format!("{PREFIX}~");
    loop {
        let mut stmt=tx.prepare("SELECT event_id FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND event_id<?3 ORDER BY event_id LIMIT 128").map_err(|_|invalid())?;
        let page=stmt.query_map(params![owner,after,upper],|r|r.get::<_,String>(0)).map_err(|_|invalid())?.collect::<Result<Vec<_>,_>>().map_err(|_|invalid())?;drop(stmt);
        if page.is_empty(){break}for id in &page {budget.check(id.len())?;let selector=id.strip_prefix(PREFIX).ok_or_else(invalid)?;let m=read_selector(tx,owner,account,selector,view,budget)?.ok_or_else(invalid)?;if !expected.insert(id.clone()){return Err(invalid())}values.push(m)}
        after=page.last().ok_or_else(invalid)?.clone();if page.len()<128 {break}
    }
    let mut stmt=tx.prepare("SELECT event_id FROM fm_v2_outbox WHERE owner_id=?1 AND kind=?2 ORDER BY event_id").map_err(|_|invalid())?;let mut rows=stmt.query(params![owner,KIND]).map_err(|_|invalid())?;
    while let Some(row)=rows.next().map_err(|_|invalid())? {let id:String=row.get(0).map_err(|_|invalid())?;budget.check(id.len())?;if !expected.remove(&id){return Err(invalid())}}
    if !expected.is_empty(){return Err(invalid())}Ok(values)
}

/// Rebind previously verified denials before their original S06 receipt can be erased.
pub(super) fn rebase_full_reset(tx:&Transaction<'_>,owner:&str,identity:&GuardedResetIdentity<'_>,prior:&[ImportanceChildTerminal],reset_id:&str,request_hash:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    for old in prior {
        let event=format!("{PREFIX}{}",old.selector_sha256);let raw=serde_json::to_string(old).map_err(|_|invalid())?;
        budget.check(event.len()+raw.len())?;
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4 AND payload_json=?5 AND source_revision=0 AND state='published'",params![owner,event,old.reset_receipt_id,KIND,raw]).map_err(|_|invalid())?!=1{return Err(invalid())}
        let mut next=old.clone();next.reset_owner=owner.into();next.reset_workspace_id=identity.workspace.into();next.reset_operation_id=identity.operation.into();next.reset_receipt_id=reset_id.into();next.reset_request_sha256=request_hash.into();next.mode="full".into();next.partition_sha256=None;
        write(tx,owner,identity.account,&next,view,budget)?;
    }Ok(())
}
pub(super) fn preserved_after_rebase(before:&[ImportanceChildTerminal],after:&[ImportanceChildTerminal],reset_id:&str)->bool {
    before.iter().all(|old|after.iter().any(|next|next.reset_receipt_id==reset_id && next.account_id==old.account_id && next.captured_owner==old.captured_owner && next.workspace_id==old.workspace_id && next.selector_sha256==old.selector_sha256 && next.root_sha256==old.root_sha256 && next.operation_sha256==old.operation_sha256))
}
/// Consume only exact child receipts whose guarded knowledge revisions were erased.
pub(super) fn finish_full_reset(tx:&Transaction<'_>,owner:&str,identity:&GuardedResetIdentity<'_>,locators:&[crate::repository::s08_policy_lifecycle::S08PolicyCleanupLocator],reset_id:&str,request_hash:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|invalid())?;
    let view=crate::repository::owner_custody::verified_or_direct_custody_view(tx,&database,identity.account,owner,owner,budget).map_err(|_|invalid())?;
    let mut children=std::collections::BTreeMap::new();
    for locator in locators {for child in &locator.importance_children {
        if child.account_id!=identity.account || child.workspace_id!=locator.workspace_id {return Err(invalid())}
        if let Some(previous)=children.insert(child.selector_sha256.clone(),child) {if previous!=child{return Err(invalid())}}
    }}
    for locator in locators {
        if let Some((id,_kind,hash))=locator.accepted_review.as_ref().filter(|(_,kind,_)|kind=="s02_importance_review") {
            let candidate=locator.candidate_id.as_deref().ok_or_else(invalid)?;
            let matches=children.values().filter(|child|child.proposal_candidate_id.as_deref()==Some(candidate) && child.receipt_id==*id && child.receipt_sha256==*hash && child.workspace_id==locator.workspace_id && child.account_id==identity.account).count();
            if matches!=1{return Err(invalid())}
        }
    }
    for child in children.values() {
        let marker=ImportanceChildTerminal{version:"s02.importance-child-erased.v1".into(),account_id:identity.account.into(),captured_owner:child.captured_owner.clone(),reset_owner:owner.into(),workspace_id:child.workspace_id.clone(),selector_sha256:child.selector_sha256.clone(),root_sha256:child.root_sha256.clone(),operation_sha256:child.request_sha256.clone(),reset_workspace_id:identity.workspace.into(),reset_operation_id:identity.operation.into(),reset_receipt_id:reset_id.into(),reset_request_sha256:request_hash.into(),mode:"full".into(),partition_sha256:None};
        write(tx,owner,identity.account,&marker,&view,budget)?;
        let left:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,child.revision_change_id,format!("outbox_{}_knowledge_revision",child.revision_change_id)],|r|r.get(0)).map_err(|_|invalid())?;
        if left{return Err(invalid())}
        let event=format!("outbox_{}_s02_importance_review",child.receipt_id);
        let pair:Option<(String,String,String,i64)>=tx.query_row("SELECT c.content_hash,c.operation_metadata_json,o.payload_json,o.source_revision FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s02_importance_review' WHERE c.owner_id=?1 AND c.change_set_id=?2 AND c.reason='s02.importance-review.v1' AND length(CAST(c.operation_metadata_json AS BLOB)) BETWEEN 1 AND 65536 AND length(CAST(o.payload_json AS BLOB)) BETWEEN 1 AND 65536",params![owner,child.receipt_id,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|invalid())?;
        let (hash,raw,payload,revision)=pair.ok_or_else(invalid)?;budget.check(raw.len()+payload.len())?;
        if hash!=child.receipt_sha256 || crate::repository::sha256_hex(raw.as_bytes())!=hash || raw!=payload || revision!=child.revision{return Err(invalid())}
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s02_importance_review' AND payload_json=?4 AND source_revision=?5",params![owner,event,child.receipt_id,raw,child.revision]).map_err(|_|invalid())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s02.importance-review.v1' AND content_hash=?3",params![owner,child.receipt_id,child.receipt_sha256]).map_err(|_|invalid())?!=1{return Err(invalid())}
    }Ok(())
}

/// Ensure every reserved child receipt is accounted for by the verified roots.
pub(super) fn validate_receipt_family(tx:&Transaction<'_>,owner:&str,locators:&[crate::repository::s08_policy_lifecycle::S08PolicyCleanupLocator],budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    let mut expected=std::collections::BTreeMap::new();
    for child in locators.iter().flat_map(|v|v.importance_children.iter()) {
        if let Some(old)=expected.insert(child.receipt_id.clone(),child.receipt_sha256.clone()) {if old!=child.receipt_sha256{return Err(invalid())}}
    }
    for child in locators.iter().flat_map(|v|v.importance_children.iter()) {
        let event=format!("outbox_{}_candidate_revision",child.receipt_id);
        let row:Option<(Option<String>,i64,String,String)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision,change_set_id,kind FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|invalid())?;
        match (&child.proposal_candidate_id,row) {
            (Some(candidate),Some((Some(raw),4,change,kind))) if change==child.receipt_id && kind=="candidate_revision"=>{budget.check(raw.len())?;let value=crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?;if value!=serde_json::json!({"candidate_id":candidate,"revision":4,"accepted_block_id":child.block_id}) {return Err(invalid())}},
            (None,None)=>(),_=>return Err(invalid()),
        }
        let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2 AND event_id<>?3 AND event_id<>?4)",params![owner,child.receipt_id,format!("outbox_{}_s02_importance_review",child.receipt_id),event],|r|r.get(0)).map_err(|_|invalid())?;
        if extra{return Err(invalid())}
    }
    let mut seen=BTreeSet::new();
    let mut stmt=tx.prepare("SELECT change_set_id,reason,content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND (change_set_id GLOB 'cs_s02importance_*' AND change_set_id NOT GLOB 'cs_s02importance_revision_*')").map_err(|_|invalid())?;
    let mut rows=stmt.query([owner]).map_err(|_|invalid())?;
    while let Some(row)=rows.next().map_err(|_|invalid())? {let (id,reason,hash):(String,String,String)=(row.get(0).map_err(|_|invalid())?,row.get(1).map_err(|_|invalid())?,row.get(2).map_err(|_|invalid())?);budget.check(id.len()+reason.len()+hash.len())?;if reason!="s02.importance-review.v1" || expected.get(&id)!=Some(&hash) || !seen.insert(id){return Err(invalid())}}
    if seen.len()!=expected.len(){return Err(invalid())}drop(rows);drop(stmt);seen.clear();
    let mut stmt=tx.prepare("SELECT event_id,change_set_id,kind FROM fm_v2_outbox WHERE owner_id=?1 AND (kind='s02_importance_review' OR event_id GLOB 'outbox_cs_s02importance_*_s02_importance_review')").map_err(|_|invalid())?;
    let mut rows=stmt.query([owner]).map_err(|_|invalid())?;
    while let Some(row)=rows.next().map_err(|_|invalid())? {let (event,id,kind):(String,String,String)=(row.get(0).map_err(|_|invalid())?,row.get(1).map_err(|_|invalid())?,row.get(2).map_err(|_|invalid())?);budget.check(event.len()+id.len()+kind.len())?;if kind!="s02_importance_review" || !expected.contains_key(&id) || event!=format!("outbox_{id}_s02_importance_review") || !seen.insert(id){return Err(invalid())}}
    if seen.len()!=expected.len(){return Err(invalid())}Ok(())
}

/// A verified once-original is separate from any applied knowledge revision.
#[derive(Debug,Clone,Serialize,PartialEq,Eq)]
pub(super) struct PreparedLifecycleFact {
    pub captured_owner:String,pub account_id:String,pub workspace_id:String,pub block_id:String,pub root_candidate_id:String,
    pub selector_sha256:String,pub request_sha256:String,pub root_receipt_id:String,pub root_sha256:String,
    pub prepare_id:String,pub prepare_sha256:String,pub claim:Option<(String,String)>,
}

/// Cross-check reserved prepare and claim namespaces in both directions.  A
/// claim is factual only when the actual applied child is also present.
pub(super) fn prepared_inventory(tx:&Transaction<'_>,owner:&str,account:&str,locators:&[crate::repository::s08_policy_lifecycle::S08PolicyCleanupLocator],view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<PreparedLifecycleFact>,String> {
    prepared_inventory_in(tx,owner,account,locators,view,budget,crate::repository::S08HistoricalRead::Ordinary)
}

pub(super) fn prepared_inventory_in(tx:&Transaction<'_>,owner:&str,account:&str,locators:&[crate::repository::s08_policy_lifecycle::S08PolicyCleanupLocator],view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget,mode:crate::repository::S08HistoricalRead<'_, '_, '_>)->Result<Vec<PreparedLifecycleFact>,String> {
    use crate::repository::s02_importance_once::{ImportancePrepareFact,collect_prepared_mode_in};
    if view.current_owner()!=owner || view.account_id()!=account {return Err(invalid())}
    let mut facts=Vec::new();let mut prepared=std::collections::BTreeSet::new();let mut claims=std::collections::BTreeSet::new();let mut selectors=std::collections::BTreeSet::new();
    let mut stmt=tx.prepare("SELECT change_set_id,CASE WHEN length(CAST(operation_metadata_json AS BLOB)) BETWEEN 1 AND 65536 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id GLOB 'cs_s02importanceprepare_*' ORDER BY change_set_id").map_err(|_|invalid())?;
    let mut rows=stmt.query([owner]).map_err(|_|invalid())?;
    while let Some(row)=rows.next().map_err(|_|invalid())? {
        if facts.len()>=10000{return Err(invalid())}
        let id:String=row.get(0).map_err(|_|invalid())?;let raw:Option<String>=row.get(1).map_err(|_|invalid())?;let raw=raw.ok_or_else(invalid)?;
        budget.check(id.len()+raw.len())?;
        let initial:ImportancePrepareFact=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?).map_err(|_|invalid())?;
        let (original,claim)=collect_prepared_mode_in(tx,owner,account,&initial.request.workspace_id,&id,view,budget,mode).map_err(|_|invalid())?;
        let fact=original.fact;let r=&fact.request;
        if r.account_id!=account || !prepared.insert(id.clone()) || !selectors.insert(fact.selector_sha256.clone()) {return Err(invalid())}
        let roots=locators.iter().filter(|v|v.candidate_id.as_deref()==Some(r.candidate_id.as_str()) && v.workspace_id==r.workspace_id && v.accepted_review.as_ref().is_some_and(|(receipt,_,hash)|receipt==&fact.root_receipt_id && hash==&fact.root_sha256)).count();
        if roots!=1{return Err(invalid())}
        let matched=locators.iter().flat_map(|v|v.importance_children.iter()).filter(|child|child.account_id==account && child.captured_owner==r.owner && child.workspace_id==r.workspace_id && child.block_id==r.block_id && child.selector_sha256==fact.selector_sha256 && child.request_sha256==fact.request_sha256 && child.root_sha256==fact.root_sha256).collect::<Vec<_>>();
        let matched_selectors=matched.iter().map(|child|child.receipt_id.as_str()).collect::<std::collections::BTreeSet<_>>();
        if (claim.is_some() && matched_selectors.len()!=1) || (claim.is_none() && !matched.is_empty()) {return Err(invalid())}
        let claim=if let Some(c)=claim {if !claims.insert(c.claim_id.clone()){return Err(invalid())}Some((c.claim_id,c.claim_sha256))} else {None};
        facts.push(PreparedLifecycleFact{captured_owner:r.owner.clone(),account_id:account.into(),workspace_id:r.workspace_id.clone(),block_id:r.block_id.clone(),root_candidate_id:r.candidate_id.clone(),selector_sha256:fact.selector_sha256,request_sha256:fact.request_sha256,root_receipt_id:fact.root_receipt_id,root_sha256:fact.root_sha256,prepare_id:id,prepare_sha256:original.prepare_sha256,claim});
    }
    drop(rows);drop(stmt);
    for (prefix,kind,expected) in [("cs_s02importanceprepare_","s02_importance_prepared",&prepared),("cs_s02importanceclaim_","s02_importance_claimed",&claims)] {
        let mut change_seen=std::collections::BTreeSet::new();let mut stmt=tx.prepare("SELECT change_set_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id>=?2 AND change_set_id<?3 ORDER BY change_set_id").map_err(|_|invalid())?;
        let mut rows=stmt.query(params![owner,prefix,format!("{prefix}~")]).map_err(|_|invalid())?;
        while let Some(row)=rows.next().map_err(|_|invalid())? {let id:String=row.get(0).map_err(|_|invalid())?;budget.check(id.len())?;if !expected.contains(&id)||!change_seen.insert(id){return Err(invalid())}}
        if &change_seen!=expected{return Err(invalid())}drop(rows);drop(stmt);
        let event_prefix=format!("outbox_{prefix}");let mut event_seen=std::collections::BTreeSet::new();
        let mut stmt=tx.prepare("SELECT event_id,change_set_id,kind FROM fm_v2_outbox WHERE owner_id=?1 AND (kind=?2 OR (event_id>=?3 AND event_id<?4))").map_err(|_|invalid())?;
        let mut rows=stmt.query(params![owner,kind,event_prefix,format!("{event_prefix}~")]).map_err(|_|invalid())?;
        while let Some(row)=rows.next().map_err(|_|invalid())? {let (event,id,actual):(String,String,String)=(row.get(0).map_err(|_|invalid())?,row.get(1).map_err(|_|invalid())?,row.get(2).map_err(|_|invalid())?);budget.check(event.len()+id.len()+actual.len())?;if actual!=kind || !expected.contains(&id) || event!=format!("outbox_{id}_{kind}") || !event_seen.insert(id){return Err(invalid())}}
        if &event_seen!=expected{return Err(invalid())}
    }
    Ok(facts)
}

/// Retire prepared originals under the same verified S06 receipt. A claimed
/// original shares its selector with the already verified applied child.
pub(super) fn finish_prepared_full_reset(tx:&Transaction<'_>,owner:&str,identity:&GuardedResetIdentity<'_>,facts:&[PreparedLifecycleFact],reset_id:&str,request_hash:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|invalid())?;
    let view=crate::repository::owner_custody::verified_or_direct_custody_view(tx,&database,identity.account,owner,owner,budget).map_err(|_|invalid())?;
    for fact in facts {
        if fact.account_id!=identity.account{return Err(invalid())}
        let marker=ImportanceChildTerminal{version:"s02.importance-child-erased.v1".into(),account_id:identity.account.into(),captured_owner:fact.captured_owner.clone(),reset_owner:owner.into(),workspace_id:fact.workspace_id.clone(),selector_sha256:fact.selector_sha256.clone(),root_sha256:fact.root_sha256.clone(),operation_sha256:fact.request_sha256.clone(),reset_workspace_id:identity.workspace.into(),reset_operation_id:identity.operation.into(),reset_receipt_id:reset_id.into(),reset_request_sha256:request_hash.into(),mode:"full".into(),partition_sha256:None};
        if fact.claim.is_some() {
            if read_selector(tx,owner,identity.account,&fact.selector_sha256,&view,budget)?.as_ref()!=Some(&marker){return Err(invalid())}
        } else {write(tx,owner,identity.account,&marker,&view,budget)?;}
        let mut pairs=vec![(fact.prepare_id.as_str(),fact.prepare_sha256.as_str(),"s02.importance-prepare.v1","s02_importance_prepared")];
        if let Some((id,hash))=&fact.claim {pairs.push((id,hash,"s02.importance-claim.v1","s02_importance_claimed"));}
        // Remove the claim before its prepare; both exact pairs were proved
        // under the same guarded transaction before any reset effects.
        pairs.reverse();
        for (id,hash,reason,kind) in pairs {
            let event=format!("outbox_{id}_{kind}");
            let row:Option<(String,String,String)>=tx.query_row("SELECT c.content_hash,c.operation_metadata_json,o.payload_json FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind=?4 AND o.source_revision=0 WHERE c.owner_id=?1 AND c.change_set_id=?2 AND c.actor_type='user' AND c.actor_id=?5 AND c.reason=?6 AND length(CAST(c.operation_metadata_json AS BLOB)) BETWEEN 1 AND 65536 AND length(CAST(o.payload_json AS BLOB)) BETWEEN 1 AND 65536",params![owner,id,event,kind,fact.captured_owner,reason],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(|_|invalid())?;
            let (actual,raw,payload)=row.ok_or_else(invalid)?;budget.check(raw.len()+payload.len())?;
            if actual!=hash || crate::repository::sha256_hex(raw.as_bytes())!=hash || raw!=payload{return Err(invalid())}
            let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2 AND event_id<>?3)",params![owner,id,event],|r|r.get(0)).map_err(|_|invalid())?;if extra{return Err(invalid())}
            if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4 AND payload_json=?5 AND source_revision=0",params![owner,event,id,kind,payload]).map_err(|_|invalid())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3 AND content_hash=?4",params![owner,id,reason,hash]).map_err(|_|invalid())?!=1{return Err(invalid())}
        }
    }Ok(())
}
