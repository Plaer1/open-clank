//! Content-free child-operation denials under the existing guarded S06 reset.
//! A verified fact is a denial only; absence is never an issuance grant.
use super::*;
use serde::{Deserialize,Serialize};
use std::collections::BTreeSet;

const PREFIX:&str="outbox_s09_question_child_erased_";
const KIND:&str="s09_question_child_erased";
fn invalid()->String {"question child terminal unavailable".into()}
fn text(v:&str)->bool {!v.is_empty() && v.trim()==v && v.len()<=512 && !v.chars().any(char::is_control)}
fn hex(v:&str)->bool {v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn action(v:&str)->bool {matches!(v,"question_defer"|"question_dismiss"|"question_reopen"|"question_answer")}

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct QuestionChildTerminal {
    pub version:String,pub account_id:String,pub captured_owner:String,pub reset_owner:String,pub workspace_id:String,
    pub selector_sha256:String,pub action:String,pub root_sha256:String,pub operation_sha256:String,
    pub reset_workspace_id:String,pub reset_operation_id:String,pub reset_receipt_id:String,pub reset_request_sha256:String,
    pub mode:String,pub partition_sha256:Option<String>,
}

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct QuestionChildForgetTerminal {
    pub version:String,pub account_id:String,pub captured_owner:String,pub workspace_id:String,
    pub selector_sha256:String,pub action:String,pub root_sha256:String,pub operation_sha256:String,
    pub forget_receipt_id:String,pub forget_receipt_sha256:String,pub candidate_sha256:String,
}

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(untagged)]
pub(crate) enum QuestionChildDenial {Reset(QuestionChildTerminal),Forget(QuestionChildForgetTerminal)}
impl QuestionChildDenial {
    fn selector(&self)->&str {match self {Self::Reset(v)=>&v.selector_sha256,Self::Forget(v)=>&v.selector_sha256}}
    fn workspace(&self)->&str {match self {Self::Reset(v)=>&v.workspace_id,Self::Forget(v)=>&v.workspace_id}}
    fn action(&self)->&str {match self {Self::Reset(v)=>&v.action,Self::Forget(v)=>&v.action}}
    fn receipt_id(&self)->&str {match self {Self::Reset(v)=>&v.reset_receipt_id,Self::Forget(v)=>&v.forget_receipt_id}}
}
fn verify_forget(tx:&Transaction<'_>,owner:&str,account:&str,m:&QuestionChildForgetTerminal,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    if view.current_owner()!=owner || view.account_id()!=account || m.version!="s09.question-child-forgotten.v1" || m.account_id!=account
        || ![m.account_id.as_str(),m.captured_owner.as_str(),m.workspace_id.as_str()].iter().all(|v|text(v)) || !action(&m.action)
        || ![m.selector_sha256.as_str(),m.root_sha256.as_str(),m.operation_sha256.as_str(),m.forget_receipt_sha256.as_str(),m.candidate_sha256.as_str()].iter().all(|v|hex(v)) {return Err(invalid())}
    view.for_captured(tx,&m.captured_owner).map_err(|_|invalid())?;
    let receipt=crate::repository::guarded_batch_forget::verify_receipt_by_id(tx,owner,account,&m.workspace_id,&m.forget_receipt_id,view,budget).map_err(|_|invalid())?;
    let hash:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,m.forget_receipt_id],|r|r.get(0)).map_err(|_|invalid())?;
    let commitment=crate::repository::guarded_batch_forget::question_child_commitment(account,&m.workspace_id,&m.captured_owner,&m.action,&m.selector_sha256,&m.root_sha256,&m.operation_sha256).map_err(|_|invalid())?;
    let mut members=receipt.members.iter().filter(|item|item.candidate_sha256==m.candidate_sha256);let member=members.next().ok_or_else(invalid)?;
    if hash!=m.forget_receipt_sha256 || members.next().is_some() || member.question_child_commitments.binary_search(&commitment).is_err() {return Err(invalid())}Ok(())
}

fn verify_reset(tx:&Transaction<'_>,owner:&str,account:&str,m:&QuestionChildTerminal,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    if view.current_owner()!=owner || view.account_id()!=account || m.version!="s09.question-child-erased.v1" || m.account_id!=account
        || ![m.account_id.as_str(),m.captured_owner.as_str(),m.reset_owner.as_str(),m.workspace_id.as_str(),m.reset_workspace_id.as_str(),m.reset_operation_id.as_str()].iter().all(|v|text(v))
        || !action(&m.action) || ![m.selector_sha256.as_str(),m.root_sha256.as_str(),m.operation_sha256.as_str(),m.reset_request_sha256.as_str()].iter().all(|v|hex(v))
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

fn read_selector(tx:&Transaction<'_>,owner:&str,account:&str,selector:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Option<QuestionChildDenial>,String> {
    if !hex(selector) {return Err(invalid())}
    let event=format!("{PREFIX}{selector}");
    let len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event],|r|r.get(0)).optional().map_err(|_|invalid())?;
    let Some(len)=len else{return Ok(None)};if !(1..=8192).contains(&len){return Err(invalid())}budget.check(len as usize+event.len())?;
    let (id,kind,raw,revision,state,published):(String,String,String,i64,String,Option<String>)=tx.query_row("SELECT change_set_id,kind,payload_json,source_revision,state,published_at FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(|_|invalid())?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(|_|invalid())?;
    let m=match value.get("version").and_then(serde_json::Value::as_str) {
        Some("s09.question-child-erased.v1")=>QuestionChildDenial::Reset(serde_json::from_value(value).map_err(|_|invalid())?),
        Some("s09.question-child-forgotten.v1")=>QuestionChildDenial::Forget(serde_json::from_value(value).map_err(|_|invalid())?),
        _=>return Err(invalid()),
    };
    if kind!=KIND || revision!=0 || state!="published" || published.as_deref().is_none_or(|v|v.is_empty()) || id!=m.receipt_id()
        || m.selector()!=selector || serde_json::to_string(&m).map_err(|_|invalid())?!=raw {return Err(invalid())}
    match &m {QuestionChildDenial::Reset(v)=>verify_reset(tx,owner,account,v,view,budget)?,QuestionChildDenial::Forget(v)=>verify_forget(tx,owner,account,v,view,budget)?};Ok(Some(m))
}

pub(super) fn probe(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,block:&str,action_name:&str,operation_key:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<bool,String> {
    crate::repository::require_guarded_epoch(tx).map_err(|_|invalid())?;
    if view.current_owner()!=owner || view.account_id()!=account {return Err(invalid())}
    let selector=crate::repository::question_child_terminal_selector(account,workspace,block,action_name,operation_key).map_err(|_|invalid())?;
    match read_selector(tx,owner,account,&selector,view,budget)? {
        Some(m) if m.workspace()==workspace && m.action()==action_name=>Ok(true),
        Some(_)=>Err(invalid()),None=>Ok(false),
    }
}

/// Caller owns a verified complete chain and the same IMMEDIATE reset transaction.
pub(super) fn write(tx:&Transaction<'_>,owner:&str,account:&str,m:&QuestionChildTerminal,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    verify_reset(tx,owner,account,m,view,budget)?;
    let event=format!("{PREFIX}{}",m.selector_sha256);
    if read_selector(tx,owner,account,&m.selector_sha256,view,budget)?.is_some(){return Err(invalid())}
    let raw=serde_json::to_string(m).map_err(|_|invalid())?;if raw.len()>8192{return Err(invalid())}budget.check(raw.len()+event.len())?;
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,state,created_at,published_at) VALUES(?1,?2,?3,?4,?5,0,'published',?6,?6)",params![owner,event,m.reset_receipt_id,KIND,raw,now]).map_err(|_|invalid())?;
    if read_selector(tx,owner,account,&m.selector_sha256,view,budget)?.as_ref()!=Some(&QuestionChildDenial::Reset(m.clone())){return Err(invalid())}Ok(())
}

/// Persist one verified selected child under the actual committed S01 receipt.
pub(super) fn write_forget(tx:&Transaction<'_>,owner:&str,account:&str,m:&QuestionChildForgetTerminal,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    verify_forget(tx,owner,account,m,view,budget)?;
    if read_selector(tx,owner,account,&m.selector_sha256,view,budget)?.is_some(){return Err(invalid())}
    let event=format!("{PREFIX}{}",m.selector_sha256);let raw=serde_json::to_string(m).map_err(|_|invalid())?;
    if raw.len()>8192{return Err(invalid())}budget.check(raw.len()+event.len())?;
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,state,created_at,published_at) VALUES(?1,?2,?3,?4,?5,0,'published',?6,?6)",params![owner,event,m.forget_receipt_id,KIND,raw,now]).map_err(|_|invalid())?;
    if read_selector(tx,owner,account,&m.selector_sha256,view,budget)?.as_ref()!=Some(&QuestionChildDenial::Forget(m.clone())){return Err(invalid())}Ok(())
}

/// Rebase only preverified S01 facts, captured before this full reset erased
/// their tombstones. The exact old bytes and new S06 proof share the transaction.
pub(super) fn rebase_forget_denials(tx:&Transaction<'_>,owner:&str,identity:&GuardedResetIdentity<'_>,prior:&[QuestionChildDenial],reset_id:&str,reset_request_sha256:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    for item in prior {
        let QuestionChildDenial::Forget(old)=item else {continue};
        let event=format!("{PREFIX}{}",old.selector_sha256);let raw=serde_json::to_string(old).map_err(|_|invalid())?;
        budget.check(raw.len()+event.len())?;
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4 AND payload_json=?5 AND source_revision=0 AND state='published'",params![owner,event,old.forget_receipt_id,KIND,raw]).map_err(|_|invalid())?!=1{return Err(invalid())}
        let replacement=QuestionChildTerminal{version:"s09.question-child-erased.v1".into(),account_id:old.account_id.clone(),captured_owner:old.captured_owner.clone(),reset_owner:owner.into(),workspace_id:old.workspace_id.clone(),selector_sha256:old.selector_sha256.clone(),action:old.action.clone(),root_sha256:old.root_sha256.clone(),operation_sha256:old.operation_sha256.clone(),reset_workspace_id:identity.workspace.into(),reset_operation_id:identity.operation.into(),reset_receipt_id:reset_id.into(),reset_request_sha256:reset_request_sha256.into(),mode:"full".into(),partition_sha256:None};
        write(tx,owner,identity.account,&replacement,view,budget)?;
    }Ok(())
}

/// Existing reset facts remain byte-identical; verified S01 facts may be
/// rebound only to the new S06 fact carrying the same immutable child.
pub(super) fn preserved_after_full_reset(before:&[QuestionChildDenial],after:&[QuestionChildDenial],reset_id:&str)->bool {
    before.iter().all(|old|match old {
        QuestionChildDenial::Reset(_)=>after.contains(old),
        QuestionChildDenial::Forget(v)=>after.iter().any(|item|matches!(item,QuestionChildDenial::Reset(n) if n.reset_receipt_id==reset_id && n.account_id==v.account_id && n.captured_owner==v.captured_owner && n.workspace_id==v.workspace_id && n.selector_sha256==v.selector_sha256 && n.action==v.action && n.root_sha256==v.root_sha256 && n.operation_sha256==v.operation_sha256)),
    })
}

/// Finish only the selected chain frozen by the guarded-forget owner.
pub(super) fn finish_forget(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,group:&crate::repository::S09QuestionGroupFact,receipt_id:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    if group.account_id!=account || group.workspace_id!=workspace || group.candidate_id.as_deref()!=Some(candidate) {return Err(invalid())}
    let receipt_hash:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,receipt_id],|r|r.get(0)).map_err(|_|invalid())?;
    let mut seen=BTreeSet::new();
    for child in &group.children {
        if child.account_id!=account || child.workspace_id!=workspace || child.block_id!=group.block_id || !seen.insert(child.selector_sha256.clone()) {return Err(invalid())}
        let marker=QuestionChildForgetTerminal{version:"s09.question-child-forgotten.v1".into(),account_id:account.into(),captured_owner:child.captured_owner.clone(),workspace_id:workspace.into(),selector_sha256:child.selector_sha256.clone(),action:child.action.clone(),root_sha256:group.root_sha256.clone(),operation_sha256:child.operation_sha256.clone(),forget_receipt_id:receipt_id.into(),forget_receipt_sha256:receipt_hash.clone(),candidate_sha256:crate::repository::sha256_hex(candidate.as_bytes())};
        write_forget(tx,owner,account,&marker,view,budget)?;
        let guard_left:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,child.guarded_change_id,child.knowledge_outbox_id],|r|r.get(0)).map_err(|_|invalid())?;
        if guard_left{return Err(invalid())}
        if child.receipt_id==child.guarded_change_id {continue}
        let pair:Option<(String,String,String,String)>=tx.query_row("SELECT c.content_hash,c.operation_metadata_json,o.kind,o.payload_json FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id WHERE c.owner_id=?1 AND c.change_set_id=?2 AND o.event_id=?3 AND length(CAST(c.operation_metadata_json AS BLOB)) BETWEEN 1 AND 131072 AND length(CAST(o.payload_json AS BLOB)) BETWEEN 1 AND 8192",params![owner,child.receipt_id,child.outbox_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|invalid())?;
        let (hash,raw,kind,payload)=pair.ok_or_else(invalid)?;budget.check(raw.len()+payload.len())?;
        if hash!=child.receipt_sha256 || crate::repository::sha256_hex(raw.as_bytes())!=hash || crate::repository::sha256_hex(payload.as_bytes())!=child.outbox_sha256 || !matches!(kind.as_str(),"mirror_update"|"s09_question_review"|"s09_question_answer") {return Err(invalid())}
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,child.outbox_id,child.receipt_id,kind]).map_err(|_|invalid())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND content_hash=?3",params![owner,child.receipt_id,child.receipt_sha256]).map_err(|_|invalid())?!=1 {return Err(invalid())}
    }Ok(())
}

/// Cross-check the reserved event prefix and kind in both directions under the same budget.
pub(super) fn inventory(tx:&Transaction<'_>,owner:&str,account:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<QuestionChildDenial>,String> {
    let mut expected=BTreeSet::new();let mut values=Vec::new();let mut after=PREFIX.to_owned();let upper=format!("{PREFIX}~");
    loop {
        let mut stmt=tx.prepare("SELECT event_id FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND event_id<?3 ORDER BY event_id LIMIT 128").map_err(|_|invalid())?;
        let page=stmt.query_map(params![owner,after,upper],|r|r.get::<_,String>(0)).map_err(|_|invalid())?.collect::<Result<Vec<_>,_>>().map_err(|_|invalid())?;drop(stmt);
        if page.is_empty(){break}for id in &page {budget.check(id.len())?;let selector=id.strip_prefix(PREFIX).ok_or_else(invalid)?;let m=read_selector(tx,owner,account,selector,view,budget)?.ok_or_else(invalid)?;if !expected.insert(id.clone()){return Err(invalid())}values.push(m)}
        after=page.last().ok_or_else(invalid)?.clone();if page.len()<128 {break}
    }
    let mut stmt=tx.prepare("SELECT event_id FROM fm_v2_outbox WHERE owner_id=?1 AND kind=?2 ORDER BY event_id").map_err(|_|invalid())?;
    let mut rows=stmt.query(params![owner,KIND]).map_err(|_|invalid())?;
    while let Some(row)=rows.next().map_err(|_|invalid())? {let id:String=row.get(0).map_err(|_|invalid())?;budget.check(id.len())?;if !expected.remove(&id){return Err(invalid())}}
    if !expected.is_empty(){return Err(invalid())}Ok(values)
}

pub(super) fn finish_full_reset(
    tx:&Transaction<'_>, owner:&str, identity:&GuardedResetIdentity<'_>,
    groups:&[crate::repository::S09QuestionGroupFact], reset_id:&str,
    reset_request_sha256:&str, budget:&mut crate::repository::ResetFenceBudget,
)->Result<(),String> {
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"question reset unavailable")?;
    let view=crate::repository::owner_custody::verified_or_direct_custody_view(tx,&database,identity.account,owner,owner,budget).map_err(|_|"question reset unavailable")?;
    let mut selectors=std::collections::BTreeSet::new();
    for group in groups {
        if group.account_id!=identity.account {return Err("question reset unavailable".into())}
        for child in &group.children {
            if child.account_id!=identity.account || child.workspace_id!=group.workspace_id || child.block_id!=group.block_id || !selectors.insert(child.selector_sha256.clone()) {return Err("question reset unavailable".into())}
            let marker=s09_question_terminal::QuestionChildTerminal{
                version:"s09.question-child-erased.v1".into(),account_id:identity.account.into(),captured_owner:child.captured_owner.clone(),reset_owner:owner.into(),workspace_id:child.workspace_id.clone(),
                selector_sha256:child.selector_sha256.clone(),action:child.action.clone(),root_sha256:group.root_sha256.clone(),operation_sha256:child.operation_sha256.clone(),
                reset_workspace_id:identity.workspace.into(),reset_operation_id:identity.operation.into(),reset_receipt_id:reset_id.into(),reset_request_sha256:reset_request_sha256.into(),mode:"full".into(),partition_sha256:None,
            };
            s09_question_terminal::write(tx,owner,identity.account,&marker,&view,budget)?;
            // Generic memory deletion has already removed the exact guarded
            // revision, knowledge outbox and now-orphaned S03 change-set.
            let guard_left:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,child.guarded_change_id,child.knowledge_outbox_id],|r|r.get(0)).map_err(|_|"question reset unavailable")?;
            if guard_left {return Err("question reset guarded evidence changed".into())}
            if child.receipt_id==child.guarded_change_id {continue}
            let pair:Option<(String,String,String,String)>=tx.query_row("SELECT c.content_hash,c.operation_metadata_json,o.kind,o.payload_json FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id WHERE c.owner_id=?1 AND c.change_set_id=?2 AND o.event_id=?3 AND length(CAST(c.operation_metadata_json AS BLOB)) BETWEEN 1 AND 131072 AND length(CAST(o.payload_json AS BLOB)) BETWEEN 1 AND 8192",params![owner,child.receipt_id,child.outbox_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"question reset unavailable")?;
            let (hash,raw,kind,payload)=pair.ok_or("question reset unavailable")?;
            budget.check(raw.len()+payload.len())?;
            if hash!=child.receipt_sha256 || crate::repository::sha256_hex(raw.as_bytes())!=hash || crate::repository::sha256_hex(payload.as_bytes())!=child.outbox_sha256 || !matches!(kind.as_str(),"mirror_update"|"s09_question_review"|"s09_question_answer") {return Err("question reset receipt changed".into())}
            if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,child.outbox_id,child.receipt_id,kind]).map_err(|_|"question reset unavailable")?!=1 {return Err("question reset receipt changed".into())}
            if tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND content_hash=?3",params![owner,child.receipt_id,child.receipt_sha256]).map_err(|_|"question reset unavailable")?!=1 {return Err("question reset receipt changed".into())}
        }
    }
    Ok(())
}
