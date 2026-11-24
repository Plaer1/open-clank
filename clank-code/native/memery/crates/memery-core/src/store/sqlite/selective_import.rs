//! Read-only, transaction-bound selective import membership. The reset owner
//! must recheck this digest and separately own erasure, immutable events,
//! replay denial, snapshot claims, and staged-file custody.
use super::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use crate::repository::{self, ResetFenceBudget};

#[derive(Debug, Clone, Serialize)]
pub(super) struct ImportStage {
    pub workspace_id:String, pub batch_id:String, pub staged_item_id:String, pub source_id_sha256:String,
    pub content_sha256:String, pub byte_size:u64, pub job_selectors:Vec<String>,
}
#[derive(Debug, Clone, Serialize)]
pub(super) struct ImportJobBinding {
    pub job_selector:String, pub workspace_id:String, pub kind:String, pub idempotency_sha256:String,
    pub row_sha256:String, pub relation_sha256:String,
}
#[derive(Debug, Clone, Serialize)]
pub(super) struct ImportCandidateBinding {
    pub candidate_selector:String, pub job_selector:String, pub state:String, pub proof_sha256:String,
}
#[derive(Debug, Clone, Serialize)]
pub(super) struct ImportApproval {
    pub workspace_id:String, pub job_selector:String, pub suggestion_sha256:String,
    pub publication_sha256:String, pub publication_kind:String, pub review_key_hash:String,
    pub receipt_id_sha256:String, pub receipt_sha256:String,
}
#[derive(Debug, Clone, Serialize)]
pub(super) struct ImportSnapshotClaim {
    pub workspace_id:String, pub job_selector:String, pub item_id:String,
    pub account:String, pub upload_id:String, pub scope:String,
}
#[derive(Clone)]
struct ImportRawLocator { id:String, key:String }
#[derive(Clone, Serialize)]
pub(super) struct SelectiveImportPartition {
    pub version:u32, pub owner:String, pub account_id:String,
    pub retained_job_selectors:Vec<String>, pub retained_content_job_selectors:Vec<String>, pub erasable_job_selectors:Vec<String>, pub job_bindings:Vec<ImportJobBinding>,
    pub retained_staging:Vec<ImportStage>, pub erasable_staging:Vec<ImportStage>,
    pub approved_refs:Vec<ImportApproval>, pub snapshot_claims:Vec<ImportSnapshotClaim>,
    pub retained_candidate_selectors:Vec<String>, pub erasable_candidate_selectors:Vec<String>, pub candidate_bindings:Vec<ImportCandidateBinding>,
    pub immutable_event_job_selectors:Vec<String>, pub digest:String,
    #[serde(skip)] raw_locators:BTreeMap<String,ImportRawLocator>,
    #[serde(skip)] candidate_locators:BTreeMap<String,String>,
}
impl SelectiveImportPartition {
    /// Valid only for a fresh same-transaction classification, never deserialized Host data.
    pub(super) fn candidate_ids(&self,retained:bool)->Result<Vec<String>,String> {
        let selectors=if retained {&self.retained_candidate_selectors} else {&self.erasable_candidate_selectors};
        selectors.iter().map(|v|self.candidate_locators.get(v).cloned().ok_or_else(unavailable)).collect()
    }
}
#[derive(Clone)]
struct Job { id:String, key:String, selector:String, key_sha256:String, kind:String, workspace:String, state:String, payload:String, current_attempt:String, input_hash:String, binding:String }
#[derive(Clone)]
struct Item { job:String, workspace:String, batch:String, root:String, stage:String, source_id:String, content_sha256:String, byte_size:u64, retry_of:Option<String>, superseded_by:Option<String>, retry:u64, payload:Value }
fn unavailable()->String {"selective import membership unavailable".into()}
fn digest(raw:&[u8])->String {repository::sha256_hex(raw)}
fn stable(v:&Value)->Result<String,String> {Ok(digest(&serde_json::to_vec(v).map_err(|_|unavailable())?))}
fn job_selector(owner:&str,job:&str)->Result<String,String> {stable(&json!(["s04.import-job-id.v1",owner,job]))}
fn key_selector(owner:&str,key:&str)->Result<String,String> {stable(&json!(["s04.import-idempotency.v1",owner,key]))}
fn canonical_upload(s:&str)->bool {s.strip_suffix(".json").is_some_and(|v|v.len()==32 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)))}
fn id(s:&str,prefix:&str)->bool {
    s.strip_prefix(prefix).is_some_and(|t|!t.is_empty() && t.len()<=128 && t.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)||b==b'_'))
}
fn hex(s:&str)->bool {s.len()==64 && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn field<'a>(v:&'a Value,k:&str)->Result<&'a str,String> {v.get(k).and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=512 && s.trim()==*s && !s.chars().any(char::is_control)).ok_or_else(unavailable)}
fn parse(raw:&str,budget:&mut ResetFenceBudget)->Result<Value,String> {
    budget.check(raw.len().saturating_add(1))?;
    crate::field_authority::parse_unique_json(raw).map_err(|_|unavailable())
}
fn optional_id(v:&Value,k:&str)->Result<Option<String>,String> {
    match v.get(k) {None|Some(Value::Null)=>Ok(None),Some(Value::String(s)) if !s.is_empty()=>Ok(Some(s.clone())),_=>Err(unavailable())}
}
fn safe_event_payload(s:&str)->bool { matches!(s.trim(),""|"null"|"{}") }

fn manifest_binding(tx:&rusqlite::Transaction<'_>, owner:&str, job:&Job, arr:&[Value], items:&BTreeMap<String,Item>, budget:&mut ResetFenceBudget, view:&repository::owner_custody::VerifiedCustodyView<'_,'_>)->Result<(),String> {
    if arr.len()!=14 {return Err(unavailable());}
    let manifest=arr[0].as_str().ok_or_else(unavailable)?;let phase=arr[1].as_str().ok_or_else(unavailable)?;
    if !matches!(phase,"selection"|"outcome") {return Err(unavailable());}
    let mut groups:Vec<Vec<String>>=Vec::new();
    for value in &arr[2..7] {
        let decoded=parse(value.as_str().ok_or_else(unavailable)?,budget)?;
        let values=decoded.as_array().ok_or_else(unavailable)?;
        if values.len()>4096 {return Err(unavailable());}
        let mut v=Vec::new();for entry in values {let entry=entry.as_str().filter(|v|!v.is_empty() && v.len()<=512).ok_or_else(unavailable)?;v.push(entry.to_string());}
        if v.windows(2).any(|p|p[0]>=p[1]) {return Err(unavailable());}groups.push(v);
    }
    let selected:BTreeSet<&str>=groups[0].iter().map(String::as_str).collect();
    let mut outcomes=BTreeSet::new();for group in &groups[1..] {for id in group {if !outcomes.insert(id.as_str()) {return Err(unavailable());}}}
    if (phase=="selection" && !outcomes.is_empty()) || (phase=="outcome" && outcomes!=selected) {return Err(unavailable());}
    if selected.iter().any(|id|job.kind!="memory_import_batch" || items.get(*id).is_none_or(|item|item.batch!=job.id)) {return Err(unavailable());}
    for value in &arr[7..11] {if value.as_str().is_none_or(|v|v.is_empty() || v.len()>512) {return Err(unavailable());}}
    let parent=match &arr[11] {Value::Null=>None,Value::String(v) if !v.is_empty() && v.len()<=512=>Some(v.as_str()),_=>return Err(unavailable())};
    if let Some(parent)=parent {
        budget.check(1)?;
        let parent_job:Option<String>=tx.query_row("SELECT job_id FROM fm_v2_job_manifests WHERE owner_id=?1 AND manifest_id=?2",params![owner,parent],|r|r.get(0)).optional().map_err(|_|unavailable())?;
        if parent_job.as_deref()!=Some(job.id.as_str()) {return Err(unavailable());}
    }
    let payload=parse(&job.payload,budget)?;
    let marked=payload.get("knowledge_snapshot_profile").and_then(Value::as_str)==Some("s10.curated-transfer.v1");
    let candidates:Vec<&str>=if marked {view.owners().iter().map(String::as_str).collect()} else {vec![owner]};
    let mut matches=0usize;
    for captured in candidates {
        budget.check(captured.len())?;
        let expected=stable(&json!({"owner_id":captured,"job_id":job.id,"phase":phase,"selected_ids":groups[0],"completed_ids":groups[1],"reused_ids":groups[2],"failed_ids":groups[3],"waived_ids":groups[4],"config_hash":arr[7],"model_hash":arr[8],"tool_hash":arr[9],"input_hash":arr[10],"parent_manifest_id":arr[11]}))?;
        if arr[12].as_str()==Some(expected.as_str()) && manifest==format!("manifest_{}",&expected[..32]) {matches+=1;}
    }
    if matches!=1 {return Err(unavailable());}
    Ok(())
}

// A released ticket is a positive Host no-effect fact, not a generic
// awaiting/error state. Its exact Native action identity must still be absent;
// unrelated later actions use a fresh key and do not affect this proof.
fn verify_released_review(tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,item:&Item,suggestion:&Value,review:&Value,budget:&mut ResetFenceBudget,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,is_transfer:bool)->Result<(),String> {
    let obj=review.as_object().ok_or_else(unavailable)?;
    let keys=["contract","state","decision","review_key_hash","actor_id","prepared_at","proposal","proposal_fingerprint","original_proposal_fingerprint","edited","last_failure","failed_at","no_effect_original"];
    let actor=field(review,"actor_id")?;
    let actor_ok=if is_transfer {let proof=view.for_captured(tx,actor).map_err(|_|unavailable())?;proof.current_owner()==owner && proof.account_id()==account} else {actor==owner};
    if obj.len()!=keys.len() || obj.keys().any(|k|!keys.contains(&k.as_str())) || field(review,"contract")?!="openclank.memory-import-review/v1" || field(review,"state")?!="awaiting_review" || field(review,"decision")?!="accept" || !actor_ok || review.get("prepared_at").and_then(Value::as_str).is_none() || review.get("failed_at").and_then(Value::as_str).is_none() || review.get("edited").and_then(Value::as_bool).is_none() {return Err(unavailable());}
    let key=field(review,"review_key_hash")?;let fingerprint=field(review,"proposal_fingerprint")?;let original=field(suggestion,"proposal_fingerprint")?;let sid=field(suggestion,"suggestion_id")?;
    if !hex(key) || !hex(fingerprint) || !hex(original) || field(review,"original_proposal_fingerprint")?!=original || !review.get("proposal").is_some_and(Value::is_object) || stable(&review["proposal"])?!=fingerprint
        || review["last_failure"]!=json!({"code":"IMPORT_REVIEW_NOT_APPLIED","message":"The original review was not applied.","retryable":true})
        || review["no_effect_original"]!=json!({"version":1,"item_id":item.job,"review_key_hash":key,"proposal_fingerprint":fingerprint}) {return Err(unavailable());}
    let action=stable(&json!(["s03.import-review-id.v1",account,item.workspace,item.batch,item.job,sid,key]))?;
    let candidate=format!("importreview_{action}");let original_id=format!("cs_import_review_{action}");
    let staged_id=repository::explicit_add::staged_import_decision_receipt_id(owner,&item.workspace,account,&candidate).map_err(|_|unavailable())?;
    let original_event=format!("outbox_{original_id}_import_review");let staged_event=format!("outbox_{staged_id}_import_staged_review");
    let initial_projection=format!("outbox_{original_id}_candidate_review");let staged_projection=format!("outbox_{staged_id}_candidate_review");let terminal=format!("outbox_s04_import_review_erased_{action}");
    budget.check(0)?;
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE change_set_id IN (?2,?3)) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE event_id IN (?4,?5,?6,?7,?8))",params![candidate,original_id,staged_id,original_event,staged_event,initial_projection,staged_projection,terminal],|r|r.get(0)).map_err(|_|unavailable())?;
    budget.check(0)?;if exists {return Err(unavailable());}
    if is_transfer {for captured in view.owners() {
        budget.check(captured.len())?;
        let id=repository::explicit_add::staged_import_decision_receipt_id(captured,&item.workspace,account,&candidate).map_err(|_|unavailable())?;
        let event=format!("outbox_{id}_import_staged_review");let projection=format!("outbox_{id}_candidate_review");
        let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE change_set_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE event_id IN (?2,?3))",params![id,event,projection],|r|r.get(0)).map_err(|_|unavailable())?;
        if present{return Err(unavailable())}
    }} Ok(())
}

/// Returns exact membership only. A successful partition is not authority to
/// mutate any row or to remove immutable events / filesystem artifacts.
pub(super) fn classify_selective_import(tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,budget:&mut ResetFenceBudget,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>)->Result<SelectiveImportPartition,String> {
    if owner.is_empty() || owner.trim()!=owner || account.is_empty() || account.trim()!=account || view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    view.for_captured(tx,owner).map_err(|_|unavailable())?;
    repository::require_guarded_epoch(tx).map_err(|_|unavailable())?;
    let mut stmt=tx.prepare("SELECT job_id,kind,workspace_key,project_key,state,COALESCE(result_json,''),COALESCE(current_attempt_id,''),attempt_count,idempotency_key,input_hash,created_at,updated_at FROM fm_v2_jobs WHERE owner_id=?1 AND kind IN ('memory_file_import','memory_import_batch','memory_import_item') ORDER BY job_id").map_err(|_|unavailable())?;
    let mut rows=stmt.query([owner]).map_err(|_|unavailable())?;
    let mut jobs:BTreeMap<String,Job>=BTreeMap::new();
    while let Some(r)=rows.next().map_err(|_|unavailable())? {
        let job:String=r.get(0).map_err(|_|unavailable())?; let kind:String=r.get(1).map_err(|_|unavailable())?;
        let workspace:String=r.get(2).map_err(|_|unavailable())?; let project:String=r.get(3).map_err(|_|unavailable())?;
        let state:String=r.get(4).map_err(|_|unavailable())?; let raw:String=r.get(5).map_err(|_|unavailable())?;
        let attempt:String=r.get(6).map_err(|_|unavailable())?; let attempt_count:i64=r.get(7).map_err(|_|unavailable())?;
        let key:String=r.get(8).map_err(|_|unavailable())?; let input:String=r.get(9).map_err(|_|unavailable())?;
        let created:String=r.get(10).map_err(|_|unavailable())?; let updated:String=r.get(11).map_err(|_|unavailable())?;
        budget.check(raw.len().saturating_add(job.len()+workspace.len()+key.len()+input.len()+created.len()+updated.len()+128))?;
        let ordinary=matches!(state.as_str(),"awaiting_review"|"succeeded"|"failed_terminal"|"cancelled");
        let marked_incomplete=if !ordinary && ((kind=="memory_import_batch" && state=="active") || (kind=="memory_import_item" && state=="queued")) && attempt.is_empty() && attempt_count==0 {
            let v=parse(&raw,budget)?;v.get("knowledge_snapshot_profile").and_then(Value::as_str)==Some("s10.curated-transfer.v1") && v.get("knowledge_snapshot").is_some()
        } else {false};
        if job.is_empty() || workspace.is_empty() || !project.is_empty() || attempt_count<0 || (!ordinary && !marked_incomplete) {return Err(unavailable());}
        let binding=stable(&json!([job,kind,workspace,project,state,raw,attempt,attempt_count,key,input,created,updated]))?;
        let selector=job_selector(owner,&job)?;let key_sha256=key_selector(owner,&key)?;
        let j=Job{id:job.clone(),key,selector,key_sha256,kind,workspace,state,payload:raw,current_attempt:attempt,input_hash:input,binding};
        if jobs.insert(job,j).is_some(){return Err(unavailable());}
    }
    drop(rows);drop(stmt);
    let mut parents:BTreeMap<String,(String,Value)>=BTreeMap::new();
    let mut items:BTreeMap<String,Item>=BTreeMap::new();
    for j in jobs.values() {
        if j.kind=="memory_file_import" {continue;}
        let payload=parse(&j.payload,budget)?;
        if payload.get("contract").and_then(Value::as_str)!=Some("openclank.memory-import-batch/v1") || !payload.is_object() {return Err(unavailable());}
        let batch=field(&payload,"batch_id")?;
        if !id(batch,"batch_") {return Err(unavailable());}
        if j.kind=="memory_import_batch" {
            let expected_key=match payload.get("action_hash") {
                None => format!("memory-import-batch:{}",j.input_hash),
                Some(Value::String(action_hash)) if hex(action_hash) => format!("memory-import-action:{action_hash}"),
                _ => return Err(unavailable()),
            };
            if !hex(&j.input_hash) || batch!=j.id || batch!=format!("batch_{}",&j.input_hash[..32]) || j.key!=expected_key || payload.get("workspace_id").and_then(Value::as_str)!=Some(j.workspace.as_str()) {return Err(unavailable());}
            parents.insert(j.id.clone(),(j.workspace.clone(),payload));
        } else {
            let item=field(&payload,"item_id")?; let root=field(&payload,"root_item_id")?; let stage=field(&payload,"staged_item_id")?;
            if item!=j.id || !id(item,"item_") || !id(root,"item_") || !id(stage,"item_") || payload.get("state").and_then(Value::as_str)!=Some(j.state.as_str()) {return Err(unavailable());}
            let source=payload.get("source_id").and_then(Value::as_str).filter(|v|!v.is_empty()).ok_or_else(unavailable)?;let content_hash=field(&payload,"content_hash")?;
            let byte_size=payload.get("byte_size").and_then(Value::as_u64).ok_or_else(unavailable)?;
            if !hex(content_hash) || byte_size>50*1024*1024 {return Err(unavailable());}
            let retry=payload.get("retry_number").and_then(Value::as_u64).ok_or_else(unavailable)?;
            let predecessor=optional_id(&payload,"retry_of")?;let successor=optional_id(&payload,"superseded_by")?;
            items.insert(j.id.clone(),Item{job:j.id.clone(),workspace:j.workspace.clone(),batch:batch.into(),root:root.into(),stage:stage.into(),source_id:source.into(),content_sha256:content_hash.into(),byte_size,retry_of:predecessor,superseded_by:successor,retry,payload});
        }
    }
    for item in items.values() {
        if parents.get(&item.batch).is_none_or(|(w,_)|w!=&item.workspace) {return Err(unavailable());}
        let root=items.get(&item.root).ok_or_else(unavailable)?;
        if root.batch!=item.batch || root.workspace!=item.workspace || root.retry!=0 || root.root!=root.job || root.stage!=root.job || item.stage!=root.stage || item.source_id!=root.source_id || item.content_sha256!=root.content_sha256 || item.byte_size!=root.byte_size {return Err(unavailable());}
        let job=jobs.get(&item.job).ok_or_else(unavailable)?;let parent=jobs.get(&item.batch).ok_or_else(unavailable)?;
        if job.input_hash!=item.content_sha256 {return Err(unavailable());}
        if item.retry==0 {
            let source_digest=stable(&json!(item.source_id))?;
            let expected_id=format!("item_{}_{}_{}",&parent.input_hash[..16],&item.content_sha256[..16],&source_digest[..8]);
            let expected_key=format!("{}:{}:{}",parent.key,item.source_id,item.job);
            if item.retry_of.is_some() || item.job!=item.root || item.job!=expected_id || job.key!=expected_key {return Err(unavailable());}
        } else {
            let prev=items.get(item.retry_of.as_ref().ok_or_else(unavailable)?).ok_or_else(unavailable)?;
            let prev_job=jobs.get(&prev.job).ok_or_else(unavailable)?;
            let retry_digest=stable(&json!({"contract":"openclank.memory-import-batch/v1","batch_id":item.batch,"root_item_id":item.root,"retry_number":item.retry}))?;
            if prev.batch!=item.batch || prev.root!=item.root || prev.stage!=item.stage || prev.retry>=item.retry || prev.superseded_by.as_deref()!=Some(item.job.as_str()) || item.job!=format!("item_{}",&retry_digest[..48]) || job.key!=format!("{}:retry:{}",prev_job.key,item.retry) {return Err(unavailable());}
        }
        if let Some(next)=&item.superseded_by {
            let n=items.get(next).ok_or_else(unavailable)?;
            if n.retry_of.as_deref()!=Some(item.job.as_str()) || n.batch!=item.batch || n.root!=item.root || n.stage!=item.stage || n.retry<=item.retry {return Err(unavailable());}
        }
    }
    for (batch,(workspace,payload)) in &parents {
        let roots:BTreeSet<&str>=items.values().filter(|i|&i.batch==batch && &i.workspace==workspace && i.retry==0).map(|i|i.root.as_str()).collect();
        let descriptors=payload.get("items").and_then(Value::as_array).ok_or_else(unavailable)?;
        if descriptors.len()>20 || roots.len()!=descriptors.len() {return Err(unavailable());}
        let mut descriptor_roots=BTreeSet::new();let mut total_bytes=0u64;
        for descriptor in descriptors {
            let root=field(descriptor,"item_id")?; if !descriptor_roots.insert(root) {return Err(unavailable());}
            let item=items.get(root).ok_or_else(unavailable)?;
            total_bytes=total_bytes.checked_add(item.byte_size).ok_or_else(unavailable)?;
            if total_bytes>50*1024*1024 {return Err(unavailable());}
            if descriptor.get("source_id").and_then(Value::as_str)!=Some(item.source_id.as_str()) || field(descriptor,"staged_item_id")?!=item.stage || field(descriptor,"content_hash")?!=item.content_sha256 || descriptor.get("byte_size").and_then(Value::as_u64)!=Some(item.byte_size) {return Err(unavailable());}
        }
        if roots!=descriptor_roots {return Err(unavailable());}
    }
    let mut approved=Vec::new();let mut snapshots=Vec::new();let mut retained_batches=BTreeSet::new();
    // Raw candidate locators never leave this transaction. A candidate can be
    // coupled to precisely one original batch/review component.
    let mut import_candidates:BTreeMap<String,(String,String,String,String)>=BTreeMap::new();
    for item in items.values() {
        let (_,parent_payload)=parents.get(&item.batch).ok_or_else(unavailable)?;
        let parent_mark=parent_payload.get("knowledge_snapshot_profile");
        let item_mark=item.payload.get("knowledge_snapshot_profile");
        let is_transfer=parent_mark.is_some() || item_mark.is_some() || parent_payload.get("knowledge_snapshot").is_some() || item.payload.get("knowledge_snapshot").is_some();
        if is_transfer {
            let profile="s10.curated-transfer.v1";
            if parent_mark.and_then(Value::as_str)!=Some(profile) || item_mark.and_then(Value::as_str)!=Some(profile) || item.retry!=0 || item.retry_of.is_some() || item.superseded_by.is_some() {return Err(unavailable())}
            let descriptors=parent_payload.get("items").and_then(Value::as_array).ok_or_else(unavailable)?;
            if descriptors.len()!=1 || items.values().filter(|v|v.batch==item.batch).count()!=1 || item.byte_size>65536 || jobs.get(&item.job).ok_or_else(unavailable)?.payload.len()>768*1024 {return Err(unavailable())}
            let identity=parent_payload.get("identity_context_hash").and_then(Value::as_str).filter(|v|hex(v)).ok_or_else(unavailable)?;
            let root=&descriptors[0];let session=parent_payload.get("session_id").and_then(Value::as_str).unwrap_or("");
            let action_hash=field(parent_payload,"action_hash")?;
            let descriptor=parent_payload.get("knowledge_snapshot").ok_or_else(unavailable)?;
            if item.payload.get("knowledge_snapshot")!=Some(descriptor){return Err(unavailable())}
            repository::knowledge_snapshot::validate_root(descriptor,account,&item.workspace,&item.content_sha256,item.byte_size,identity,budget)?;
            let action=field(descriptor,"action_id")?;
            let mut captured_count=0usize;
            for captured in view.owners() {
                budget.check(captured.len())?;
                let input=json!({"contract":"openclank.memory-import-batch/v1","owner":captured,"workspace_id":item.workspace,"session_id":session,"items":[{"item_id":item.source_id,"byte_size":item.byte_size,"content_hash":item.content_sha256,"extension":field(root,"extension")?,"document_role":field(root,"document_role")?}],"identity_context_hash":identity,"action_hash":action_hash,"knowledge_snapshot_profile":profile,"knowledge_snapshot":descriptor});
                if stable(&input)?==jobs.get(&item.batch).ok_or_else(unavailable)?.input_hash && action_hash==stable(&json!(["s04.import-action.v1",captured,action]))? {captured_count+=1;}
            }
            if captured_count!=1{return Err(unavailable())}
            match item.payload.pointer("/result/suggestions") {
                None=>{if jobs.get(&item.job).ok_or_else(unavailable)?.state!="queued" || jobs.get(&item.batch).ok_or_else(unavailable)?.state!="active" {return Err(unavailable())}},
                Some(Value::Array(suggestions)) if suggestions.len()==1=>{
                    let suggestion=&suggestions[0];let descriptor=suggestion.get("knowledge_snapshot").ok_or_else(unavailable)?;
                    if parent_payload.get("knowledge_snapshot")!=Some(descriptor){return Err(unavailable())}
                    let proposal=json!({"text":suggestion.get("text").ok_or_else(unavailable)?,"category":suggestion.get("category").ok_or_else(unavailable)?,"knowledge_snapshot":descriptor});
                    repository::knowledge_snapshot::validate_transfer(&proposal,profile,account,&item.workspace,&item.content_sha256,item.byte_size,identity,budget)?;
                    let action=field(descriptor,"action_id")?;
                    if field(suggestion,"proposal_fingerprint")?!=stable(&proposal)? {return Err(unavailable())}
                    if let Some(review)=suggestion.get("review") {
                        if review.get("proposal")!=Some(&proposal) || field(review,"review_key_hash")?!=stable(&json!({"review_key":format!("s10-knowledge-snapshot:{action}")}))? {return Err(unavailable())}
                    }
                },
                _=>return Err(unavailable()),
            }
        } else if item.payload.pointer("/result/suggestions").and_then(Value::as_array).is_some_and(|a|a.iter().any(|v|v.get("knowledge_snapshot").is_some() || v.pointer("/review/proposal/knowledge_snapshot").is_some())) {return Err(unavailable())}
        if let Some(claim)=item.payload.get("snapshot_import_claim") {
            let o=claim.as_object().ok_or_else(unavailable)?;
            if o.len()!=3 {return Err(unavailable());}
            let acct=field(claim,"account")?;let upload=field(claim,"upload_id")?;let scope=field(claim,"scope")?;
            let fingerprint=item.payload.get("snapshot_import_fingerprint").and_then(Value::as_str).filter(|v|hex(v)).ok_or_else(unavailable)?;
            let suggestions=item.payload.pointer("/result/suggestions").and_then(Value::as_array).ok_or_else(unavailable)?;
            if suggestions.len()!=1 {return Err(unavailable());}
            let descriptor=suggestions[0].get("snapshot_import").ok_or_else(unavailable)?;
            if acct!=account || item.job!=item.stage || !canonical_upload(upload) || !scope.strip_prefix("pending:ma4import:").is_some_and(hex) || descriptor.pointer("/reference/upload_id").and_then(Value::as_str)!=Some(upload) || stable(descriptor)?!=fingerprint {return Err(unavailable());}
            snapshots.push(ImportSnapshotClaim{workspace_id:item.workspace.clone(),job_selector:job_selector(owner,&item.job)?,item_id:item.stage.clone(),account:acct.into(),upload_id:upload.into(),scope:scope.into()});
        } else if item.payload.get("snapshot_import_fingerprint").is_some() {return Err(unavailable());}
        let Some(result)=item.payload.get("result") else {continue};
        let result=result.as_object().ok_or_else(unavailable)?;
        let Some(suggestions)=result.get("suggestions") else {continue};
        let suggestions=suggestions.as_array().ok_or_else(unavailable)?;
        if suggestions.len()>100 {return Err(unavailable());}
        let mut seen=BTreeSet::new();
        for suggestion in suggestions {
            if !suggestion.is_object() {return Err(unavailable());}
            let sid=field(suggestion,"suggestion_id")?;
            if !sid.strip_prefix("suggestion_").is_some_and(|v|v.len()==32 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))) || !seen.insert(sid) {return Err(unavailable());}
            let mut reviews=Vec::new();
            if let Some(history)=suggestion.get("review_history") {let a=history.as_array().ok_or_else(unavailable)?;if a.len()>20 {return Err(unavailable());} reviews.extend(a.iter());}
            if let Some(review)=suggestion.get("review") {reviews.push(review);}
            for review in reviews {
                budget.check(0)?;
                let state=field(review,"state")?;
                if state=="publishing" {return Err(unavailable());}
                if state=="awaiting_review" {verify_released_review(tx,owner,account,item,suggestion,review,budget,view,is_transfer)?;continue;}
                let captured_actor=field(review,"actor_id")?;
                let actor_ok=if is_transfer {let proof=view.for_captured(tx,captured_actor).map_err(|_|unavailable())?;proof.current_owner()==owner && proof.account_id()==account} else {captured_actor==owner};
                if !actor_ok || !matches!(state,"accepted"|"reused"|"rejected"|"deferred") || field(review,"contract")?!="openclank.memory-import-review/v1" {return Err(unavailable());}
                let decision=field(review,"decision")?;
                let review_key=field(review,"review_key_hash")?;let proposal_hash=field(review,"proposal_fingerprint")?;
                let original_hash=field(suggestion,"proposal_fingerprint")?;
                if !hex(review_key) || !hex(proposal_hash) || !hex(original_hash) || field(review,"original_proposal_fingerprint")?!=original_hash || stable(review.get("proposal").ok_or_else(unavailable)?)?!=proposal_hash {return Err(unavailable());}
                if state=="rejected" || state=="deferred" {
                    if decision!=(if state=="rejected" {"reject"} else {"defer"}) || review.get("publication").is_some_and(|v|!v.is_null()) {return Err(unavailable());}
                    continue;
                }
                if decision!="accept" {return Err(unavailable());}
                let publication=review.get("publication").ok_or_else(unavailable)?;
                if publication.as_object().is_none_or(|v|v.len()!=2) {return Err(unavailable());}
                let pid=field(publication,"id")?;let kind=field(publication,"kind")?;
                if !matches!(kind,"candidate"|"memory"|"existing") {return Err(unavailable());}
                let curated=if kind=="candidate" {
                    let row:Option<(String,Option<String>,String)>=tx.query_row("SELECT status,accepted_curated_id,source_event_id FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3",params![pid,owner,item.workspace],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(|_|unavailable())?;
                    let (candidate_state,curated,event)=row.ok_or_else(unavailable)?;
                    if event!=format!("memory-import-review:{}:{}",item.batch,sid) {return Err(unavailable());}
                    if pid.starts_with("importreview_") {
                        let identity=if is_transfer {repository::import_mirror::import_identity_for_candidate_at(tx,owner,&item.workspace,account,pid,Some(view))} else {repository::import_mirror::import_identity_for_candidate(tx,owner,&item.workspace,account,pid)}.map_err(|_|unavailable())?;
                        let (original,original_hash)=if is_transfer {repository::import_mirror::original_for_import_identity_at(tx,&identity,owner,Some(view))} else {repository::import_mirror::original_for_import_identity(tx,&identity)}.map_err(|_|unavailable())?;
                        let terminal=if is_transfer {repository::import_mirror::read_import_review_terminal_at(tx,&identity,owner,Some(view))} else {repository::import_mirror::read_import_review_terminal(tx,&identity)}.map_err(|_|unavailable())?;
                        if terminal.is_some() {return Err(unavailable());}
                        if identity.batch_id!=item.batch || identity.item_id!=item.job || identity.suggestion_id!=sid || identity.review_key_hash!=review_key || identity.proposal_fingerprint!=proposal_hash || original.mode!="review_only" || original.publication_kind!="candidate" || original.publication_id!=pid {return Err(unavailable());}
                        let proof=if candidate_state=="accepted" {
                            let d=if is_transfer {repository::explicit_add::read_staged_import_decision_receipt_custody(tx,&item.workspace,account,pid,captured_actor,view)} else {repository::explicit_add::read_staged_import_decision_receipt(tx,owner,&item.workspace,account,pid)}.map_err(|_|unavailable())?.ok_or_else(unavailable)?;
                            if d.action!="accept" || d.curated_id!=curated {return Err(unavailable());}stable(&serde_json::to_value(d).map_err(|_|unavailable())?)?
                        } else if matches!(candidate_state.as_str(),"pending"|"rejected") && curated.is_none() {
                            let (state,proof)=if is_transfer {repository::explicit_add::import_unapproved_disposition_custody(tx,&identity,view,budget)} else {repository::explicit_add::import_unapproved_disposition(tx,&identity,budget)}.map_err(|_|unavailable())?;
                            if state!=candidate_state || !hex(&proof) {return Err(unavailable());}proof
                        } else {return Err(unavailable());};
                        let proof=stable(&json!(["s04.import-candidate-proof.v1",original_hash,proof]))?;
                        if import_candidates.insert(pid.to_owned(),(item.batch.clone(),item.job.clone(),candidate_state.clone(),proof)).is_some() {return Err(unavailable());}
                    }
                    if curated.is_none() && (candidate_state=="pending" || (candidate_state=="rejected" && pid.starts_with("importreview_"))) {continue;}
                    if candidate_state!="accepted" {return Err(unavailable());}
                    curated.ok_or_else(unavailable)?
                } else {pid.to_string()};
                let context=if is_transfer {repository::current_import_mirror_context_custody(tx,&item.workspace,&curated,view)} else {repository::current_curated_mirror_context_in(tx,owner,&item.workspace,&curated)}.map_err(|_|unavailable())?;
                if context.get("reviewer_account_id").and_then(Value::as_str)!=Some(account) || (kind=="candidate" && context.get("candidate_id").and_then(Value::as_str)!=Some(pid)) {return Err(unavailable());}
                let receipt=field(&context,"prior_receipt_id")?; let receipt_sha256=stable(&context)?;
                if item.payload.get("snapshot_import_claim").is_some() {
                    let descriptor=item.payload.pointer("/result/suggestions/0/snapshot_import").ok_or_else(unavailable)?;
                    if descriptor.get("capture_origin").and_then(Value::as_str)!=Some("imported_unverified") || context.pointer("/snapshot_copy/capture_origin").and_then(Value::as_str)!=Some("imported_unverified") || context.pointer("/snapshot_copy/manifest")!=descriptor.get("reference") {return Err(unavailable());}
                } else if kind=="memory" {
                    let meta:String=tx.query_row("SELECT metadata FROM curated WHERE id=?1 AND owner=?2 AND workspace_id=?3",params![curated,owner,item.workspace],|r|r.get(0)).map_err(|_|unavailable())?;
                    let metadata=parse(&meta,budget)?;let origin=metadata.get("import_review").ok_or_else(unavailable)?;
                    if origin.get("contract").and_then(Value::as_str)!=Some("openclank.memory-import-review/v1") || origin.get("batch_id").and_then(Value::as_str)!=Some(item.batch.as_str()) || origin.get("item_id").and_then(Value::as_str)!=Some(item.job.as_str()) || origin.get("suggestion_id").and_then(Value::as_str)!=Some(sid) || origin.get("reviewed_by").and_then(Value::as_str)!=Some(captured_actor) {return Err(unavailable());}
                    if item.payload.get("knowledge_snapshot_profile").is_some() {
                        let expected=repository::knowledge_snapshot::safe_provenance(&json!({"knowledge_snapshot":item.payload.get("knowledge_snapshot").ok_or_else(unavailable)?})).map_err(|_|unavailable())?;
                        if origin.get("knowledge_snapshot")!=expected.get("knowledge_snapshot") {return Err(unavailable())}
                    }
                }
                retained_batches.insert(item.batch.clone());
                approved.push(ImportApproval{workspace_id:item.workspace.clone(),job_selector:job_selector(owner,&item.job)?,suggestion_sha256:stable(&json!(["s04.import-suggestion.v1",owner,item.batch,item.job,sid]))?,publication_sha256:stable(&json!(["s04.import-publication.v1",owner,item.workspace,kind,pid]))?,publication_kind:kind.into(),review_key_hash:review_key.into(),receipt_id_sha256:stable(&json!(["s04.import-receipt.v1",owner,receipt]))?,receipt_sha256});
            }
        }
    }
    let mut relations:BTreeMap<String,Vec<String>>=BTreeMap::new();let mut event_jobs=BTreeSet::new();let mut unsafe_events=BTreeSet::new();
    for j in jobs.values() {
        let mut hashes=Vec::new();let mut current_attempt_seen=j.current_attempt.is_empty();
        for (sql,category) in [
            ("SELECT json_array(attempt_id,job_id,attempt_number,state,lease_epoch,lease_owner,lease_expires_at,heartbeat_at,progress_watermark,error_json,created_at,updated_at) FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 ORDER BY attempt_id","attempt"),
            ("SELECT json_array(sequence,state,attempt_id,lease_epoch,result_json,error_json,change_set_id,created_at) FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2 ORDER BY sequence","event"),
            ("SELECT json_array(manifest_id,phase,selected_json,completed_json,reused_json,failed_json,waived_json,config_hash,model_hash,tool_hash,input_hash,parent_manifest_id,content_hash,sealed_at) FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 ORDER BY manifest_id","manifest"),
            ("SELECT json_array(e.sequence,c.change_set_id,c.actor_type,c.actor_id,c.reason,c.source_event_id,c.contract_version,c.content_hash,c.operation_metadata_json,c.created_at,o.event_id,o.kind,o.payload_json,o.source_revision,o.state,o.published_at,o.created_at) FROM fm_v2_job_events e LEFT JOIN fm_v2_change_sets c ON c.owner_id=e.owner_id AND c.change_set_id=e.change_set_id LEFT JOIN fm_v2_outbox o ON o.owner_id=e.owner_id AND o.change_set_id=e.change_set_id WHERE e.owner_id=?1 AND e.job_id=?2 ORDER BY e.sequence,o.event_id","event_binding")
        ] {
            let mut stmt=tx.prepare(sql).map_err(|_|unavailable())?;let mut rows=stmt.query(params![owner,j.id]).map_err(|_|unavailable())?;
            while let Some(row)=rows.next().map_err(|_|unavailable())? {
                let raw:String=row.get(0).map_err(|_|unavailable())?;let value=parse(&raw,budget)?;let arr=value.as_array().ok_or_else(unavailable)?;
                if category=="attempt" {
                    if matches!(arr.get(3).and_then(Value::as_str),Some("leased"|"running")) {return Err(unavailable());}
                    if arr.first().and_then(Value::as_str)==Some(j.current_attempt.as_str()) {current_attempt_seen=true;}
                }
                if category=="manifest" {manifest_binding(tx,owner,j,arr,&items,budget,view)?;}
                if category=="event_binding" && arr.get(1).and_then(Value::as_str).is_none() {return Err(unavailable());}
                if category=="event" {event_jobs.insert(j.id.clone());if !safe_event_payload(arr.get(4).and_then(Value::as_str).unwrap_or("")) || !safe_event_payload(arr.get(5).and_then(Value::as_str).unwrap_or("")) {unsafe_events.insert(j.id.clone());}}
                hashes.push(format!("{category}:{}",digest(raw.as_bytes())));
            }
        }
        if !current_attempt_seen {return Err(unavailable());}
        hashes.sort(); relations.insert(j.id.clone(),hashes);
    }
    let mut retained=Vec::new();let mut retained_content=Vec::new();let mut erasable=Vec::new();let mut stages:BTreeMap<(String,String,String),Vec<String>>=BTreeMap::new();
    let mut raw_locators=BTreeMap::new();
    for j in jobs.values() {
        if raw_locators.insert(j.selector.clone(),ImportRawLocator{id:j.id.clone(),key:j.key.clone()}).is_some() {return Err(unavailable());}
        let keep=if j.kind=="memory_import_batch" {retained_batches.contains(&j.id)} else if let Some(item)=items.get(&j.id) {retained_batches.contains(&item.batch)} else {false};
        if keep {
            retained.push(j.selector.clone());
            if let Some(item)=items.get(&j.id) {
                let mut content=false;
                for field_name in ["result","error"] {
                    match item.payload.get(field_name) {
                        None|Some(Value::Null)=>{},
                        Some(Value::Object(value))=>{if !value.is_empty() {content=true;}},
                        _=>return Err(unavailable()),
                    }
                }
                if content {retained_content.push(j.selector.clone());}
            }
        } else {if unsafe_events.contains(&j.id) {return Err(unavailable());} erasable.push(j.selector.clone());}
    }
    retained.sort();retained_content.sort();erasable.sort();
    for item in items.values() {stages.entry((item.workspace.clone(),item.batch.clone(),item.stage.clone())).or_default().push(item.job.clone());}
    let mut retained_staging=Vec::new();let mut erasable_staging=Vec::new();
    for ((workspace_id,batch_id,staged_item_id),job_ids) in stages {
        let first=items.get(job_ids.first().ok_or_else(unavailable)?).ok_or_else(unavailable)?;
        let mut job_selectors=job_ids.iter().map(|id|job_selector(owner,id)).collect::<Result<Vec<_>,_>>()?;job_selectors.sort();
        let keep=retained_batches.contains(&batch_id);
        let group=ImportStage{workspace_id,batch_id,staged_item_id,source_id_sha256:stable(&json!(["s04.import-source.v1",first.source_id]))?,content_sha256:first.content_sha256.clone(),byte_size:first.byte_size,job_selectors};
        if keep {retained_staging.push(group)} else {erasable_staging.push(group)}
    }
    approved.sort_by(|a,b|(&a.workspace_id,&a.job_selector,&a.suggestion_sha256,&a.publication_sha256).cmp(&(&b.workspace_id,&b.job_selector,&b.suggestion_sha256,&b.publication_sha256)));
    snapshots.sort_by(|a,b|(&a.workspace_id,&a.job_selector).cmp(&(&b.workspace_id,&b.job_selector)));
    let mut job_bindings=Vec::new();
    for j in jobs.values() {
        let relation_sha256=stable(&json!(relations.get(&j.id).ok_or_else(unavailable)?))?;
        job_bindings.push(ImportJobBinding{job_selector:j.selector.clone(),workspace_id:j.workspace.clone(),kind:j.kind.clone(),idempotency_sha256:j.key_sha256.clone(),row_sha256:j.binding.clone(),relation_sha256});
    }
    job_bindings.sort_by(|a,b|a.job_selector.cmp(&b.job_selector));
    let mut retained_candidate_selectors=Vec::new();let mut erasable_candidate_selectors=Vec::new();let mut candidate_bindings=Vec::new();let mut candidate_locators=BTreeMap::new();
    for (candidate,(batch,job,state,proof_sha256)) in import_candidates {
        let selector=stable(&json!(["s04.import-candidate.v1",owner,candidate]))?;
        if candidate_locators.insert(selector.clone(),candidate).is_some() {return Err(unavailable());}
        if retained_batches.contains(&batch) {retained_candidate_selectors.push(selector.clone());}
        else {if state=="accepted" {return Err(unavailable());}erasable_candidate_selectors.push(selector.clone());}
        candidate_bindings.push(ImportCandidateBinding{candidate_selector:selector,job_selector:job_selector(owner,&job)?,state,proof_sha256});
    }
    retained_candidate_selectors.sort();erasable_candidate_selectors.sort();candidate_bindings.sort_by(|a,b|a.candidate_selector.cmp(&b.candidate_selector));
    let mut immutable_event_job_selectors=event_jobs.iter().map(|id|job_selector(owner,id)).collect::<Result<Vec<_>,_>>()?;immutable_event_job_selectors.sort();
    let input=json!({"version":"s04.selective-import.v2","owner":owner,"account_id":account,"job_bindings":job_bindings,"retained_job_selectors":retained,"retained_content_job_selectors":retained_content,"erasable_job_selectors":erasable,"retained_staging":retained_staging,"erasable_staging":erasable_staging,"approved_refs":approved,"snapshot_claims":snapshots,"retained_candidate_selectors":retained_candidate_selectors,"erasable_candidate_selectors":erasable_candidate_selectors,"candidate_bindings":candidate_bindings,"immutable_event_job_selectors":immutable_event_job_selectors});
    let result_digest=stable(&input)?;
    Ok(SelectiveImportPartition{version:2,owner:owner.into(),account_id:account.into(),retained_job_selectors:retained,retained_content_job_selectors:retained_content,erasable_job_selectors:erasable,job_bindings,retained_staging,erasable_staging,approved_refs:approved,snapshot_claims:snapshots,retained_candidate_selectors,erasable_candidate_selectors,candidate_bindings,immutable_event_job_selectors,digest:result_digest,raw_locators,candidate_locators})
}


/// Prevent the generic erasure owner from separating an import-review
/// candidate from its original batch/retry unit. Only locators from a fresh
/// same-transaction partition may qualify an import-origin selected row.
pub(super) fn refuse_unclosed_import_candidates(tx:&rusqlite::Transaction<'_>,owner:&str,candidates:&[String],partition:&SelectiveImportPartition,budget:&mut ResetFenceBudget)->Result<(),String> {
    if partition.owner!=owner {return Err(unavailable());}
    let allowed:BTreeSet<String>=partition.candidate_ids(false)?.into_iter().collect();
    for candidate in candidates {
        budget.check(candidate.len())?;
        let row:Option<(String,Option<String>)>=tx.query_row("SELECT workspace_id,source_event_id FROM candidates WHERE owner=?1 AND id=?2",params![owner,candidate],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
        let (workspace,event)=row.ok_or_else(unavailable)?;
        budget.check(workspace.len().saturating_add(event.as_ref().map_or(0,String::len)))?;
        if event.as_deref().is_some_and(|v|v.starts_with("memory-import-review:")) && !allowed.contains(candidate) {return Err(unavailable());}
    }
    Ok(())
}

/// Host-private selector. Current named-human session and policy remain caller
/// authority; none of these strings is a grant or a supplied proposal.
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ImportReviewSelector {
    pub version:u32, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub batch_id:String, pub item_id:String, pub suggestion_id:String,
    pub review_key_hash:String, pub proposal_fingerprint:String, pub mode:String,
    pub policy_projection:repository::PolicyPublicationRefs,
}

/// Opaque original-transaction witness; only this module constructs one.
pub(crate) struct VerifiedImportReview<'a,'db> {
    tx:&'a rusqlite::Transaction<'db>, selector:ImportReviewSelector,
    proposal:Value, source_event_id:String, candidate_id:String, provenance:Value,
}
impl<'a,'db> VerifiedImportReview<'a,'db> {
    pub(crate) fn selector(&self)->&ImportReviewSelector {&self.selector}
    pub(crate) fn proposal(&self)->&Value {&self.proposal}
    pub(crate) fn source_event_id(&self)->&str {&self.source_event_id}
    pub(crate) fn candidate_id(&self)->&str {&self.candidate_id}
    pub(crate) fn provenance(&self)->&Value {&self.provenance}
    /// Re-read immediately before the owning SQLite transaction's first effect.
    pub(crate) fn revalidate(&self,tx:&rusqlite::Transaction<'_>,budget:&mut ResetFenceBudget)->Result<(),String> {
        if !std::ptr::eq(self.tx,tx) {return Err(unavailable());}
        let fresh=verify_publishing_import_review(tx,&self.selector,budget)?;
        if fresh.proposal!=self.proposal || fresh.source_event_id!=self.source_event_id || fresh.candidate_id!=self.candidate_id || fresh.provenance!=self.provenance {return Err(unavailable());}
        Ok(())
    }
}

fn publishing_job(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,id:&str,kind:&str,budget:&mut ResetFenceBudget)->Result<(String,String,String,Value),String> {
    publishing_job_at(tx,owner,&[owner],workspace,id,kind,budget)
}
fn publishing_job_at(tx:&rusqlite::Transaction<'_>,owner:&str,identity_owners:&[&str],workspace:&str,id:&str,kind:&str,budget:&mut ResetFenceBudget)->Result<(String,String,String,Value),String> {
    let raw_len:Option<i64>=tx.query_row("SELECT length(CAST(result_json AS BLOB)) FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind=?3",params![owner,id,kind],|r|r.get(0)).optional().map_err(|_|unavailable())?.flatten();
    budget.check(usize::try_from(raw_len.ok_or_else(unavailable)?).map_err(|_|unavailable())?)?;
    let row:Option<(String,String,String,String,String,String)>=tx.query_row("SELECT workspace_key,project_key,state,idempotency_key,input_hash,result_json FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind=?3",params![owner,id,kind],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|unavailable())?;
    let (w,project,state,key,input,raw)=row.ok_or_else(unavailable)?;
    budget.check(w.len().saturating_add(project.len()).saturating_add(state.len()).saturating_add(key.len()).saturating_add(input.len()).saturating_add(raw.len()))?;
    if w!=workspace || !project.is_empty() || key.is_empty() || !hex(&input) {return Err(unavailable());}
    // Deny on reserved-marker presence without interpreting or disclosing it.
    for captured in identity_owners {
        budget.check(captured.len())?;
        let id_event=job_event_id(captured,&job_selector(captured,id)?)?;
        let key_event=key_event_id(&key_selector(captured,&key)?);
        let denied:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id IN (?2,?3))",params![owner,id_event,key_event],|r|r.get(0)).map_err(|_|unavailable())?;
        if denied {return Err(unavailable());}
    }
    Ok((state,key,input,parse(&raw,budget)?))
}

/// Verify the real publishing/accept review and its current original job
/// lineage. The witness cannot outlive or be reused across this transaction.
pub(crate) fn verify_publishing_import_review<'a,'db>(tx:&'a rusqlite::Transaction<'db>,s:&ImportReviewSelector,budget:&mut ResetFenceBudget)->Result<VerifiedImportReview<'a,'db>,String> {
    verify_import_host_review(tx,s,budget,false,true)
}
/// Read-only correlation for a no-effect response. This proves the exact
/// stored ticket and live origin, but returns no witness or publication grant.
/// Current account/session authority remains the caller's responsibility.
pub(crate) fn verify_publishing_import_ticket(tx:&rusqlite::Transaction<'_>,s:&ImportReviewSelector,budget:&mut ResetFenceBudget)->Result<(),String> {
    verify_import_host_review(tx,s,budget,false,false).map(|_|())
}
fn verify_import_host_review<'a,'db>(tx:&'a rusqlite::Transaction<'db>,s:&ImportReviewSelector,budget:&mut ResetFenceBudget,finalized:bool,authorize:bool)->Result<VerifiedImportReview<'a,'db>,String> {
    verify_import_host_review_at(tx,s,&s.owner,&[&s.owner],budget,finalized,authorize)
}
fn verify_import_host_review_at<'a,'db>(tx:&'a rusqlite::Transaction<'db>,s:&ImportReviewSelector,sql_owner:&str,identity_owners:&[&str],budget:&mut ResetFenceBudget,finalized:bool,authorize:bool)->Result<VerifiedImportReview<'a,'db>,String> {
    if s.version!=1 || [&s.owner,&s.workspace_id,&s.account_id,&s.batch_id,&s.item_id,&s.suggestion_id].iter().any(|v|v.is_empty() || v.len()>512 || v.trim()!=v.as_str() || v.chars().any(char::is_control)) || !id(&s.batch_id,"batch_") || !id(&s.item_id,"item_") || !s.suggestion_id.strip_prefix("suggestion_").is_some_and(|v|v.len()==32 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))) || !hex(&s.review_key_hash) || !hex(&s.proposal_fingerprint) || !matches!(s.mode.as_str(),"manual"|"review_only") {return Err(unavailable());}
    repository::require_guarded_epoch(tx).map_err(|_|unavailable())?;
    if authorize {repository::require_policy_projection(tx,&s.account_id,&s.workspace_id,Some(&s.policy_projection)).map_err(|_|unavailable())?;}
    let (_,parent_key,parent_input,parent)=publishing_job_at(tx,sql_owner,identity_owners,&s.workspace_id,&s.batch_id,"memory_import_batch",budget)?;
    let expected_key=match parent.get("action_hash") {None=>format!("memory-import-batch:{parent_input}"),Some(Value::String(h)) if hex(h)=>format!("memory-import-action:{h}"),_=>return Err(unavailable())};
    if parent_key!=expected_key || s.batch_id!=format!("batch_{}",&parent_input[..32]) || field(&parent,"contract")?!="openclank.memory-import-batch/v1" || field(&parent,"batch_id")?!=s.batch_id || field(&parent,"workspace_id")?!=s.workspace_id {return Err(unavailable());}
    let (state,mut key,input,item)=publishing_job_at(tx,sql_owner,identity_owners,&s.workspace_id,&s.item_id,"memory_import_item",budget)?;
    if !(state=="awaiting_review" || (finalized && state=="succeeded")) || field(&item,"state")?!=state || field(&item,"contract")?!="openclank.memory-import-batch/v1" || field(&item,"item_id")?!=s.item_id || field(&item,"batch_id")?!=s.batch_id || item.get("snapshot_import_claim").is_some() {return Err(unavailable());}
    let root=field(&item,"root_item_id")?.to_owned(); let stage=field(&item,"staged_item_id")?.to_owned(); let source=field(&item,"source_id")?.to_owned(); let content=field(&item,"content_hash")?.to_owned(); let size=item.get("byte_size").and_then(Value::as_u64).ok_or_else(unavailable)?;
    if !id(&root,"item_") || stage!=root || input!=content || !hex(&content) || size>50*1024*1024 || optional_id(&item,"superseded_by")?.is_some() {return Err(unavailable());}
    let mut current_id=s.item_id.clone();let mut current=item.clone();
    loop {
        budget.check(1)?;
        let n=current.get("retry_number").and_then(Value::as_u64).ok_or_else(unavailable)?;
        if n==0 {
            let source_hash=stable(&json!(source))?;
            if current_id!=root || key!=format!("{parent_key}:{source}:{root}") || root!=format!("item_{}_{}_{}",&parent_input[..16],&content[..16],&source_hash[..8]) || optional_id(&current,"retry_of")?.is_some() {return Err(unavailable());}
            break;
        }
        let expected=stable(&json!({"contract":"openclank.memory-import-batch/v1","batch_id":s.batch_id,"root_item_id":root,"retry_number":n}))?;
        if current_id!=format!("item_{}",&expected[..48]) {return Err(unavailable());}
        let previous=field(&current,"retry_of")?.to_owned();
        let (_,prev_key,prev_input,prev)=publishing_job_at(tx,sql_owner,identity_owners,&s.workspace_id,&previous,"memory_import_item",budget)?;
        let prev_n=prev.get("retry_number").and_then(Value::as_u64).ok_or_else(unavailable)?;
        if prev_n>=n || key!=format!("{prev_key}:retry:{n}") || field(&prev,"item_id")?!=previous || field(&prev,"batch_id")?!=s.batch_id || field(&prev,"root_item_id")?!=root || field(&prev,"staged_item_id")?!=stage || field(&prev,"source_id")?!=source || field(&prev,"content_hash")?!=content || prev_input!=content || prev.get("byte_size").and_then(Value::as_u64)!=Some(size) || field(&prev,"superseded_by")?!=current_id {return Err(unavailable());}
        current_id=previous;current=prev;key=prev_key;
    }
    let descriptors=parent.get("items").and_then(Value::as_array).ok_or_else(unavailable)?;
    if descriptors.len()>20 || descriptors.iter().filter(|v|v.get("item_id").and_then(Value::as_str)==Some(root.as_str()) && v.get("staged_item_id").and_then(Value::as_str)==Some(stage.as_str()) && v.get("source_id").and_then(Value::as_str)==Some(source.as_str()) && v.get("content_hash").and_then(Value::as_str)==Some(content.as_str()) && v.get("byte_size").and_then(Value::as_u64)==Some(size)).count()!=1 {return Err(unavailable());}
    let suggestions=item.pointer("/result/suggestions").and_then(Value::as_array).ok_or_else(unavailable)?;
    if suggestions.len()>100 {return Err(unavailable());}
    // The original extraction fingerprint/ID are provenance bound to this
    // exact stored job, not independently attested content or approval.
    let matches:Vec<&Value>=suggestions.iter().filter(|v|v.get("suggestion_id").and_then(Value::as_str)==Some(s.suggestion_id.as_str())).collect();
    if matches.len()!=1 {return Err(unavailable());}
    let suggestion=matches[0];let review=suggestion.get("review").ok_or_else(unavailable)?;
    let original=field(suggestion,"proposal_fingerprint")?;let proposal=review.get("proposal").ok_or_else(unavailable)?;
    if !hex(original) || field(review,"contract")?!="openclank.memory-import-review/v1" || field(review,"state")?!=if finalized {"accepted"} else {"publishing"} || field(review,"decision")?!="accept" || field(review,"actor_id")?!=s.owner || field(review,"review_key_hash")?!=s.review_key_hash || field(review,"proposal_fingerprint")?!=s.proposal_fingerprint || field(review,"original_proposal_fingerprint")?!=original || stable(proposal)?!=s.proposal_fingerprint || !proposal.is_object() {return Err(unavailable());}
    let source_event_id=format!("memory-import-review:{}:{}",s.batch_id,s.suggestion_id);
    let candidate_id=format!("importreview_{}",stable(&json!(["s03.import-review-id.v1",s.account_id,s.workspace_id,s.batch_id,s.item_id,s.suggestion_id,s.review_key_hash]))?);
    // Filename is server-stored display provenance, never an authority. The
    // publication owner must privacy-filter it before any durable write.
    let filename=item.get("filename").and_then(Value::as_str).filter(|v|!v.is_empty() && v.chars().count()<=255).ok_or_else(unavailable)?;
    if finalized && review.get("publication")!=Some(&json!({"kind":"candidate","id":candidate_id})) {return Err(unavailable());}
    let mut provenance=json!({"contract":"openclank.memory-import-review/v1","batch_id":s.batch_id,"item_id":s.item_id,"suggestion_id":s.suggestion_id,"filename":filename,"content_hash":content,"reviewed_by":s.owner});
    if let Some(claim) = proposal.get("markdown_source").or_else(|| suggestion.get("markdown_source")) {
        let bundle = item.pointer("/result/markdown_bundle").ok_or_else(unavailable)?;
        let bundle_fields = ["state", "bundle_sha256", "origin_sha256", "foreign", "omitted_items", "omissions_at_least"];
        let hash_fields = ["source_sha256", "source_uri_sha256", "source_messages_sha256"];
        let count = |v: &Value| v.as_u64().is_some_and(|n| n <= (1u64 << 53) - 1);
        let item_text = suggestion.get("text").and_then(Value::as_str).ok_or_else(unavailable)?;
        if proposal.get("markdown_source") != Some(claim)
            || suggestion.get("markdown_source") != Some(claim)
            || item.get("extension").and_then(Value::as_str) != Some(".md")
            || bundle.as_object().map(|v| v.len()) != Some(bundle_fields.len())
            || claim.as_object().map(|v| v.len()) != Some(13)
            || bundle_fields.iter().any(|key| bundle.get(*key).is_none() || bundle.get(*key) != claim.get(*key))
            || claim["state"] != "unverified" || claim["bundle_sha256"].as_str() != Some(content.as_str())
            || !claim["origin_sha256"].as_str().is_some_and(hex) || !claim["foreign"].is_boolean()
            || !count(&claim["omitted_items"]) || !claim["omissions_at_least"].is_boolean()
            || claim["item_sha256"] != digest(item_text.as_bytes())
            || !matches!(claim["declared_state"].as_str(), Some("resolved" | "unresolved"))
            || hash_fields.iter().any(|key| !claim.get(*key).is_some_and(|v| v.is_null() || v.as_str().is_some_and(hex)))
            || !claim.get("source_revision").is_some_and(|v| v.is_null() || count(v))
            || !claim["revision_omitted"].is_boolean()
            || (claim["revision_omitted"] == true && !claim["source_revision"].is_null())
        {
            return Err(unavailable());
        }
        provenance["markdown_source"] = claim.clone();
    }
    let profile="s10.curated-transfer.v1";
    let parent_mark=parent.get("knowledge_snapshot_profile");let item_mark=item.get("knowledge_snapshot_profile");let transfer=proposal.get("knowledge_snapshot");
    if parent_mark.is_some() || item_mark.is_some() || transfer.is_some() || suggestion.get("knowledge_snapshot").is_some() {
        if parent_mark.and_then(Value::as_str)!=Some(profile) || item_mark.and_then(Value::as_str)!=Some(profile) || s.mode!="review_only" || suggestions.len()!=1 || descriptors.len()!=1 || item.get("retry_number").and_then(Value::as_u64)!=Some(0) || optional_id(&item,"superseded_by")?.is_some() || original!=s.proposal_fingerprint {return Err(unavailable());}
        if tx.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0)).map_err(|_|unavailable())?!=18 {return Err(unavailable())}
        let identity=parent.get("identity_context_hash").and_then(Value::as_str).ok_or_else(unavailable)?;
        let root_descriptor=&descriptors[0];
        let session=parent.get("session_id").and_then(Value::as_str).unwrap_or("");
        let root_value=parent.get("knowledge_snapshot").ok_or_else(unavailable)?;
        if item.get("knowledge_snapshot")!=Some(root_value) || transfer!=Some(root_value){return Err(unavailable())}
        let input=json!({"contract":"openclank.memory-import-batch/v1","owner":s.owner,"workspace_id":s.workspace_id,"session_id":session,"items":[{"item_id":source,"byte_size":size,"content_hash":content,"extension":field(root_descriptor,"extension")?,"document_role":field(root_descriptor,"document_role")?}],"identity_context_hash":identity,"action_hash":field(&parent,"action_hash")?,"knowledge_snapshot_profile":profile,"knowledge_snapshot":root_value});
        let item_bytes:i64=tx.query_row("SELECT length(CAST(result_json AS BLOB)) FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind='memory_import_item'",params![sql_owner,s.item_id],|r|r.get(0)).map_err(|_|unavailable())?;
        if item_bytes<0 || item_bytes>768*1024 || stable(&input)?!=parent_input {return Err(unavailable())}
        let descriptor=repository::knowledge_snapshot::validate_transfer(proposal,profile,&s.account_id,&s.workspace_id,&content,size,identity,budget)?;
        let action=descriptor["action_id"].as_str().ok_or_else(unavailable)?;
        if s.review_key_hash!=stable(&json!({"review_key":format!("s10-knowledge-snapshot:{action}")}))? || parent.get("action_hash").and_then(Value::as_str)!=Some(stable(&json!(["s04.import-action.v1",s.owner,action]))?.as_str()) || suggestion.get("text")!=proposal.get("text") || suggestion.get("category")!=proposal.get("category") || suggestion.get("knowledge_snapshot")!=Some(&descriptor) {return Err(unavailable())}
        provenance["knowledge_snapshot"]=descriptor;
    }
    Ok(VerifiedImportReview{tx,selector:s.clone(),proposal:proposal.clone(),source_event_id,candidate_id,provenance})
}

/// Exact current staged import under the original transaction. It is distinct
/// from the short-lived publishing ticket and cannot be constructed by callers.
pub(crate) struct VerifiedStagedImport<'a,'db> {
    tx:&'a rusqlite::Transaction<'db>, selector:ImportReviewSelector,
    receipt:repository::explicit_add::ImportReviewReceipt,
    proposal:repository::explicit_add::ImportReviewProposal,
    revision:i64, binding_sha256:String,
}
impl<'a,'db> VerifiedStagedImport<'a,'db> {
    pub(crate) fn selector(&self)->&ImportReviewSelector {&self.selector}
    pub(crate) fn receipt(&self)->&repository::explicit_add::ImportReviewReceipt {&self.receipt}
    pub(crate) fn proposal(&self)->&repository::explicit_add::ImportReviewProposal {&self.proposal}
    pub(crate) fn revision(&self)->i64 {self.revision}
    pub(crate) fn binding_sha256(&self)->&str {&self.binding_sha256}
    pub(crate) fn revalidate(&self,tx:&rusqlite::Transaction<'_>,budget:&mut ResetFenceBudget)->Result<(),String> {
        if !std::ptr::eq(self.tx,tx) {return Err(unavailable());}
        let fresh=verify_finalized_staged_import(tx,&self.selector,budget)?;
        if fresh.receipt!=self.receipt || fresh.proposal!=self.proposal || fresh.revision!=self.revision || fresh.binding_sha256!=self.binding_sha256 {return Err(unavailable());}
        Ok(())
    }
}

/// Current named-human policy and the finalized Host publication must both be
/// current; the factual original mode comes only from the original receipt.
pub(crate) fn verify_finalized_staged_import<'a,'db>(tx:&'a rusqlite::Transaction<'db>,s:&ImportReviewSelector,budget:&mut ResetFenceBudget)->Result<VerifiedStagedImport<'a,'db>,String> {
    let host=verify_import_host_review(tx,s,budget,true,true)?;
    let receipt=repository::explicit_add::read_import_review_receipt(tx,s).map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    if receipt.mode!="review_only" || receipt.publication_kind!="candidate" || receipt.publication_id!=host.candidate_id || receipt.candidate_id!=host.candidate_id || receipt.source_event_id!=host.source_event_id {return Err(unavailable());}
    let (proposal,revision,legacy_digest)=repository::explicit_add::current_import_review_proposal(tx,s,&receipt).map_err(|_|unavailable())?;
    if host.provenance.get("knowledge_snapshot").is_some() {
        let mut initial=host.proposal.clone();
        initial.as_object_mut().ok_or_else(unavailable)?.remove("knowledge_snapshot");
        if proposal.provenance!=host.provenance || stable(&initial)?!=receipt.stored_proposal_sha256 {return Err(unavailable())}
    }
    let bound=serde_json::to_value(&proposal).map_err(|_|unavailable())?;
    budget.check(serde_json::to_vec(&bound).map_err(|_|unavailable())?.len())?;
    let binding_sha256=stable(&json!(["s03.import-staged-binding.v1",receipt,revision,legacy_digest,bound]))?;
    Ok(VerifiedStagedImport{tx,selector:s.clone(),receipt,proposal,revision,binding_sha256})
}

/// Same-transaction witness for a staged import in verified current custody.
/// The selector and proposal retain their captured original identity; current
/// owner/account/policy are separately checked and never derived from it.
pub(crate) struct VerifiedStagedImportCustody<'a,'db> {
    tx:&'a rusqlite::Transaction<'db>, view:&'a repository::owner_custody::VerifiedCustodyView<'a,'db>,
    selector:ImportReviewSelector, receipt:repository::explicit_add::ImportReviewReceipt,
    proposal:repository::explicit_add::ImportReviewProposal, revision:i64, binding_sha256:String,
}
impl<'a,'db> VerifiedStagedImportCustody<'a,'db> {
    pub(crate) fn selector(&self)->&ImportReviewSelector {&self.selector}
    pub(crate) fn current_owner(&self)->&str {self.view.current_owner()}
    pub(crate) fn view(&self)->&repository::owner_custody::VerifiedCustodyView<'a,'db> {self.view}
    pub(crate) fn receipt(&self)->&repository::explicit_add::ImportReviewReceipt {&self.receipt}
    pub(crate) fn proposal(&self)->&repository::explicit_add::ImportReviewProposal {&self.proposal}
    pub(crate) fn revision(&self)->i64 {self.revision}
    pub(crate) fn binding_sha256(&self)->&str {&self.binding_sha256}
    pub(crate) fn revalidate(&self,tx:&rusqlite::Transaction<'_>,budget:&mut ResetFenceBudget)->Result<(),String> {
        if !std::ptr::eq(self.tx,tx) {return Err(unavailable());}
        let fresh=verify_finalized_staged_import_custody(self.tx,&self.selector,self.view,budget)?;
        if fresh.receipt!=self.receipt || fresh.proposal!=self.proposal || fresh.revision!=self.revision || fresh.binding_sha256!=self.binding_sha256 {return Err(unavailable());}
        Ok(())
    }
}
pub(crate) fn verify_finalized_staged_import_custody<'a,'db>(tx:&'a rusqlite::Transaction<'db>,s:&ImportReviewSelector,view:&'a repository::owner_custody::VerifiedCustodyView<'a,'db>,budget:&mut ResetFenceBudget)->Result<VerifiedStagedImportCustody<'a,'db>,String> {
    let proof=view.for_captured(tx,&s.owner).map_err(|_|unavailable())?;
    if proof.account_id()!=s.account_id {return Err(unavailable());}
    let identity_owners:Vec<&str>=view.owners().iter().map(String::as_str).collect();
    let host=verify_import_host_review_at(tx,s,proof.current_owner(),&identity_owners,budget,true,true)?;
    let receipt=repository::explicit_add::read_import_review_receipt_custody(tx,s,view).map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    if receipt.mode!="review_only" || receipt.publication_kind!="candidate" || receipt.publication_id!=host.candidate_id || receipt.candidate_id!=host.candidate_id || receipt.source_event_id!=host.source_event_id {return Err(unavailable());}
    let identity=repository::import_mirror::ImportReviewIdentity::from(s);
    if repository::import_mirror::read_import_review_terminal_at(tx,&identity,proof.current_owner(),Some(view)).map_err(|_|unavailable())?.is_some() {return Err(unavailable());}
    let (proposal,revision,legacy_digest)=repository::explicit_add::current_import_review_proposal_custody(tx,s,&receipt,view).map_err(|_|unavailable())?;
    if host.provenance.get("knowledge_snapshot").is_some() {
        let mut initial=host.proposal.clone();
        initial.as_object_mut().ok_or_else(unavailable)?.remove("knowledge_snapshot");
        if proposal.provenance!=host.provenance || stable(&initial)?!=receipt.stored_proposal_sha256 {return Err(unavailable())}
    }
    let bound=serde_json::to_value(&proposal).map_err(|_|unavailable())?;
    budget.check(serde_json::to_vec(&bound).map_err(|_|unavailable())?.len())?;
    let binding_sha256=stable(&json!(["s03.import-staged-binding.v1",receipt,revision,legacy_digest,bound]))?;
    Ok(VerifiedStagedImportCustody{tx,view,selector:s.clone(),receipt,proposal,revision,binding_sha256})
}

/// Resolve the immutable original selector from exact current custody. The
/// caller separately establishes the current authenticated human and owns the
/// transaction and its one progress budget.
pub(super) fn staged_import_selector_custody(tx:&rusqlite::Transaction<'_>,s:&super::ExplicitAddReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>)->Result<ImportReviewSelector,String> {
    if s.version!=1 || s.owner!=view.current_owner() || s.account_id!=view.account_id() || [&s.owner,&s.workspace_id,&s.account_id,&s.candidate_id].iter().any(|v|v.is_empty() || v.trim()!=v.as_str() || v.len()>512) {return Err(unavailable());}
    view.for_captured(tx,&s.owner).map_err(|_|unavailable())?;
    let identity=repository::import_mirror::import_identity_for_candidate_at(tx,&s.owner,&s.workspace_id,&s.account_id,&s.candidate_id,Some(view)).map_err(|_|unavailable())?;
    let (receipt,_)=repository::import_mirror::original_for_import_identity_at(tx,&identity,&s.owner,Some(view)).map_err(|_|unavailable())?;
    if receipt.mode!="review_only" || receipt.publication_kind!="candidate" || receipt.publication_id!=s.candidate_id {return Err(unavailable());}
    Ok(ImportReviewSelector{version:1,owner:identity.owner,workspace_id:identity.workspace_id,account_id:identity.account_id,batch_id:identity.batch_id,item_id:identity.item_id,suggestion_id:identity.suggestion_id,review_key_hash:identity.review_key_hash,proposal_fingerprint:identity.proposal_fingerprint,mode:receipt.mode,policy_projection:s.policy_projection.clone()})
}
/// Preview is a current-scope projection; original stored proposal bytes and
/// binding remain untouched. A projected context owner must be proven first.
pub(super) fn staged_import_preview_custody<'a,'db>(tx:&'a rusqlite::Transaction<'db>,s:&super::ExplicitAddReviewSelector,view:&'a repository::owner_custody::VerifiedCustodyView<'a,'db>,budget:&mut ResetFenceBudget)->Result<Value,String> {
    let original=staged_import_selector_custody(tx,s,view)?;
    let witness=verify_finalized_staged_import_custody(tx,&original,view,budget)?;
    let mut visible=witness.proposal().proposal.clone();let object=visible.as_object_mut().ok_or_else(unavailable)?;
    if let Some(context)=object.get_mut("question_context") {
        if let Some(scope)=context.get_mut("scope") {
            let captured=scope.get("owner").and_then(Value::as_str).ok_or_else(unavailable)?;
            view.for_captured(tx,captured).map_err(|_|unavailable())?;
            if *scope!=json!({"owner":captured,"workspace_id":s.workspace_id,"project_id":""}) {return Err(unavailable());}
            *scope=json!({"owner":s.owner,"workspace_id":s.workspace_id,"project_id":""});
        }
    }
    object.insert("source".into(),json!("memory_import"));object.insert("owner".into(),json!(s.owner));object.insert("workspace_id".into(),json!(s.workspace_id));object.insert("account_id".into(),json!(s.account_id));object.insert("candidate_id".into(),json!(s.candidate_id));
    Ok(json!({"state":"preview","candidate_id":s.candidate_id,"proposal":visible,"expected_revision":witness.revision(),"binding_sha256":witness.binding_sha256()}))
}

/// Factual status under current custody, independently authorized by the
/// caller; a terminal takes precedence over later decision lookup.
pub(super) fn staged_import_original_custody(tx:&rusqlite::Transaction<'_>,s:&super::ExplicitAddReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,String> {
    repository::require_guarded_epoch(tx).map_err(|_|unavailable())?;
    repository::require_policy_projection(tx,&s.account_id,&s.workspace_id,Some(&s.policy_projection)).map_err(|_|unavailable())?;
    let original=staged_import_selector_custody(tx,s,view)?;
    let identity=repository::import_mirror::ImportReviewIdentity::from(&original);
    if repository::import_mirror::read_import_review_terminal_at(tx,&identity,&s.owner,Some(view)).map_err(|_|unavailable())?.is_some() {return Ok(json!({"state":"terminal_erased","candidate_id":s.candidate_id}));}
    let mut decision=None;
    for captured in view.owners() {
        budget.check(captured.len())?;
        let item=repository::explicit_add::read_staged_import_decision_receipt_custody(tx,&s.workspace_id,&s.account_id,&s.candidate_id,captured,view).map_err(|_|unavailable())?;
        if let Some(item)=item {if decision.replace(item).is_some() {return Err(unavailable());}}
    }
    Ok(match decision {None=>json!({"state":"absent"}),Some(r)=>json!({"state":"found","original":{"candidate_id":r.candidate_id,"expected_revision":r.expected_revision,"expected_binding":r.expected_binding,"review_key":r.review_key,"action":r.action},"receipt":{"state":if r.action=="accept" {"applied"} else {"rejected"},"candidate_id":s.candidate_id,"curated_id":r.curated_id}})})
}

/// Exact outcome for an issued decision, including a content-free terminal.
pub(super) fn staged_import_status_custody(tx:&rusqlite::Transaction<'_>,request:&super::ExplicitAddReviewApplyRequest,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,String> {
    let s=&request.selector;let result=staged_import_original_custody(tx,s,view,budget)?;
    if result["state"]=="found" {
        let original=&result["original"];
        if original["expected_revision"]!=request.expected_revision || original["expected_binding"]!=request.expected_binding || original["review_key"]!=request.review_key || original["action"]!=request.action {return Err("import review identity conflict".into());}
        return Ok(result["receipt"].clone());
    }
    if result["state"]=="terminal_erased" {
        let original=staged_import_selector_custody(tx,s,view)?;let identity=repository::import_mirror::ImportReviewIdentity::from(&original);
        let marker=repository::import_mirror::read_import_review_terminal_at(tx,&identity,&s.owner,Some(view)).map_err(|_|unavailable())?.ok_or_else(unavailable)?;
        let key=repository::import_mirror::import_review_decision_key_sha256(&s.account_id,&s.workspace_id,&s.candidate_id,&request.review_key).map_err(|_|unavailable())?;
        if marker.decision_key_sha256.as_deref()!=Some(key.as_str()) || marker.decision_action.as_deref()!=Some(request.action.as_str()) || marker.decision_revision!=Some(request.expected_revision) || marker.decision_binding.as_deref()!=Some(request.expected_binding.as_str()) {return Err("import review identity conflict".into());}
    }
    Ok(result)
}

/// The parent owns the original IMMEDIATE transaction and one budget. A
/// captured proposal supplies historical identity; current policy and owner
/// authorize the new edit. Nothing is committed by this helper.
pub(super) fn staged_import_edit_custody<'a,'db>(tx:&'a rusqlite::Transaction<'db>,request:&super::ExplicitAddReviewEditRequest,view:&'a repository::owner_custody::VerifiedCustodyView<'a,'db>,budget:&mut ResetFenceBudget)->Result<Value,String> {
    let s=&request.selector;if request.source!="memory_import" {return Err(unavailable());}
    let original=staged_import_selector_custody(tx,s,view)?;
    let witness=verify_finalized_staged_import_custody(tx,&original,view,budget)?;
    let mut reviewed=witness.proposal().proposal.clone();let object=reviewed.as_object_mut().ok_or_else(unavailable)?;
    object.insert("text".into(),json!(request.text));object.insert("category".into(),json!(request.category));
    if let Some(context)=request.question_context.as_ref() {object.insert("question_context".into(),context.clone());} else {object.remove("question_context");}
    repository::explicit_add::edit_staged_import_review_custody(tx,&witness,request.expected_revision,&request.expected_binding,&reviewed,budget).map_err(|_|unavailable())?;
    drop(witness);staged_import_preview_custody(tx,s,view,budget)
}
/// Perform one exact new human decision; the caller commits only after its
/// own scope and original status checks. No partial writes escape rollback.
pub(super) fn staged_import_apply_custody<'a,'db>(tx:&'a rusqlite::Transaction<'db>,request:&super::ExplicitAddReviewApplyRequest,view:&'a repository::owner_custody::VerifiedCustodyView<'a,'db>,budget:&mut ResetFenceBudget)->Result<Value,String> {
    let s=&request.selector;
    super::SqliteStore::explicit_add_review_identity(tx,request)?;
    if repository::require_policy_projection(tx,&s.account_id,&s.workspace_id,Some(&s.policy_projection)).is_err() {
        // A stale policy is a positive no-effect only after the exact original
        // and every decision/terminal identity are proven absent in custody.
        let original=staged_import_selector_custody(tx,s,view)?;
        let identity=repository::import_mirror::ImportReviewIdentity::from(&original);
        if repository::import_mirror::read_import_review_terminal_at(tx,&identity,&s.owner,Some(view)).map_err(|_|unavailable())?.is_some() {return Err(unavailable());}
        for captured in view.owners() {
            budget.check(captured.len())?;
            if repository::explicit_add::read_staged_import_decision_receipt_custody(tx,&s.workspace_id,&s.account_id,&s.candidate_id,captured,view).map_err(|_|unavailable())?.is_some() {return Err(unavailable());}
        }
        return Ok(super::SqliteStore::explicit_add_review_not_applied(request,"policy_changed"));
    }
    if staged_import_original_custody(tx,s,view,budget)?["state"]!="absent" {return Err("import review issued; status only".into());}
    let original=staged_import_selector_custody(tx,s,view)?;
    let witness=verify_finalized_staged_import_custody(tx,&original,view,budget)?;
    if witness.revision()!=request.expected_revision || witness.binding_sha256()!=request.expected_binding {
        return Ok(json!({"state":"not_applied","candidate_id":s.candidate_id,"expected_revision":request.expected_revision,"expected_binding":request.expected_binding,"review_key":request.review_key,"action":request.action,"reason":"conflict"}));
    }
    let record=if request.action=="accept" {
        let record=repository::explicit_add::prepare_staged_import_record_custody(tx,&witness,budget).map_err(|_|unavailable())?;
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1) OR EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='curated' AND original_id=?1)",[&record.id],|r|r.get(0)).map_err(|_|unavailable())?;
        let scope=crate::graph::GraphScope::new(&s.owner,&s.workspace_id).map_err(|_|unavailable())?;let node=scope.node_id("memory",&record.id);
        let graph_collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='graph_node' AND original_id=?1) OR EXISTS(SELECT 1 FROM graph_nodes WHERE id=?1) OR EXISTS(SELECT 1 FROM graph_edges WHERE src_id=?1 OR dst_id=?1) OR EXISTS(SELECT 1 FROM graph_cues WHERE node_id=?1)",[&node],|r|r.get(0)).map_err(|_|unavailable())?;
        if collision || graph_collision {return Err(unavailable());}
        super::SqliteStore::upsert_curated_inner_scoped(tx,&record,None,false)?;
        let saved:(String,String,Option<String>,String)=tx.query_row("SELECT content,metadata,owner,workspace_id FROM curated WHERE id=?1",[&record.id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).map_err(|_|unavailable())?;
        if saved.0!=record.content || serde_json::from_str::<Value>(&saved.1).map_err(|_|unavailable())?!=record.metadata || saved.2.as_deref()!=Some(s.owner.as_str()) || saved.3!=s.workspace_id {return Err(unavailable());}Some(record)
    } else {None};
    let (receipt,_)=repository::explicit_add::finish_staged_import_decision_custody(tx,&witness,&request.review_key,request.expected_revision,&request.expected_binding,&request.action,record.as_ref(),budget).map_err(|_|unavailable())?;
    Ok(json!({"state":if receipt.action=="accept" {"applied"} else {"rejected"},"candidate_id":receipt.candidate_id,"curated_id":receipt.curated_id}))
}

/// Factual initial import result, using current account/policy and the exact
/// captured original once receipt. Absence requires a bounded orphan check.
pub(super) fn import_review_status_custody(tx:&rusqlite::Transaction<'_>,s:&ImportReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,String> {
    import_review_original_custody(tx,s,view,budget,true)
}
/// No policy grant: apply uses this only to correlate an exact pre-effect
/// outcome before separately validating the real ticket and current policy.
pub(super) fn import_review_original_custody(tx:&rusqlite::Transaction<'_>,s:&ImportReviewSelector,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,authorize:bool)->Result<Value,String> {
    if s.version!=1 || s.owner!=view.current_owner() || s.account_id!=view.account_id()
        || [&s.owner,&s.workspace_id,&s.account_id,&s.batch_id,&s.item_id,&s.suggestion_id].iter().any(|v|v.is_empty() || v.len()>512 || v.trim()!=v.as_str() || v.chars().any(char::is_control))
        || !id(&s.batch_id,"batch_") || !id(&s.item_id,"item_")
        || !s.suggestion_id.strip_prefix("suggestion_").is_some_and(|v|v.len()==32 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)))
        || !hex(&s.review_key_hash) || !hex(&s.proposal_fingerprint) || !matches!(s.mode.as_str(),"manual"|"review_only") {return Err(unavailable());}
    view.for_captured(tx,&s.owner).map_err(|_|unavailable())?;repository::require_guarded_epoch(tx).map_err(|_|unavailable())?;
    if authorize {repository::require_policy_projection(tx,&s.account_id,&s.workspace_id,Some(&s.policy_projection)).map_err(|_|unavailable())?;}
    let digest=stable(&json!(["s03.import-review-id.v1",s.account_id,s.workspace_id,s.batch_id,s.item_id,s.suggestion_id,s.review_key_hash]))?;
    let candidate=format!("importreview_{digest}");let change=format!("cs_import_review_{digest}");
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2)",params![s.owner,change],|r|r.get(0)).map_err(|_|unavailable())?;
    if exists {
        let identity=repository::import_mirror::import_identity_for_candidate_at(tx,&s.owner,&s.workspace_id,&s.account_id,&candidate,Some(view)).map_err(|_|unavailable())?;
        if identity.batch_id!=s.batch_id || identity.item_id!=s.item_id || identity.suggestion_id!=s.suggestion_id || identity.review_key_hash!=s.review_key_hash || identity.proposal_fingerprint!=s.proposal_fingerprint {return Err("import review identity conflict".into());}
        if repository::import_mirror::read_import_review_terminal_at(tx,&identity,&s.owner,Some(view)).map_err(|_|unavailable())?.is_some() {return Ok(json!({"state":"terminal_erased","candidate_id":candidate}));}
        let (receipt,_)=repository::import_mirror::original_for_import_identity_at(tx,&identity,&s.owner,Some(view)).map_err(|_|unavailable())?;
        return Ok(json!({"state":if receipt.mode=="manual" {"applied"} else {"staged"},"candidate_id":receipt.candidate_id,"publication_kind":receipt.publication_kind,"publication_id":receipt.publication_id}));
    }
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE event_id IN (?3,?4,?5))",params![candidate,change,format!("outbox_cs_import_review_{digest}_import_review"),format!("outbox_cs_import_review_{digest}_candidate_review"),format!("outbox_s04_import_review_erased_{digest}")],|r|r.get(0)).map_err(|_|unavailable())?;
    budget.check(0)?;if orphan {return Err(unavailable());}Ok(json!({"state":"absent"}))
}

/// Content-free original Native erasure commitment. It is not a grant to
/// erase staged files or a substitute for current account authority.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ImportEraseCommitment {
    pub version:String, pub owner:String, pub workspace_id:String, pub reset_workspace_id:String, pub account_id:String,
    pub job_selector:String, pub operation_id:String, pub partition_sha256:String,
    pub import_partition_sha256:String, pub reset_receipt_id:String,
    pub idempotency_sha256:String, pub job_binding_sha256:String,
}
fn reset_id(account:&str,operation:&str)->Result<String,String> {
    Ok(format!("cs_reset_{}",stable(&json!([account,operation]))?))
}
fn job_event_id(owner:&str,selector:&str)->Result<String,String> {
    Ok(format!("outbox_s04_import_erased_{}",stable(&json!([owner,selector]))?))
}
fn key_event_id(key_hash:&str)->String {format!("outbox_s04_import_key_erased_{key_hash}")}
fn verified_terminal(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,marker:&ImportEraseCommitment)->Result<Value,String> {
    if marker.version!="s04.import-erased.v2" || marker.owner!=owner || marker.workspace_id!=workspace || marker.account_id!=account
        || marker.reset_workspace_id.is_empty() || marker.reset_workspace_id.trim()!=marker.reset_workspace_id || marker.reset_workspace_id.len()>512
        || marker.operation_id.is_empty() || marker.operation_id.trim()!=marker.operation_id || marker.operation_id.len()>512
        || !hex(&marker.job_selector) || !hex(&marker.idempotency_sha256) || !hex(&marker.partition_sha256)
        || !hex(&marker.import_partition_sha256) || !hex(&marker.job_binding_sha256)
        || marker.reset_receipt_id!=reset_id(account,&marker.operation_id)? {return Err(unavailable());}
    let (stored_hash,receipt_raw,actor_type,actor_id,contract,source_event):(String,String,String,String,String,Option<String>)=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![owner,marker.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let receipt=crate::field_authority::parse_unique_json(&receipt_raw).map_err(|_|unavailable())?;
    if digest(receipt_raw.as_bytes())!=stored_hash || actor_type!="user" || actor_id!=owner || contract!=crate::ontology::CONTRACT_VERSION || source_event.is_some() || receipt.as_object().map(|v|v.len())!=Some(4)
        || receipt["version"]!="s06.guarded-reset.v1" || receipt["request_sha256"]!=stable(&receipt["request"])?
        || receipt["request"]["owner"]!=owner || receipt["request"]["workspace_id"]!=marker.reset_workspace_id || receipt["request"]["account_id"]!=account
        || !receipt["request"]["components"].as_array().is_some_and(|v|v.iter().any(|c|c.as_str()==Some("ingest")))
        || receipt["request"]["operation_id"]!=marker.operation_id || receipt["result"]["complete"]!=true || receipt["result"]["categories"]["ingest"]["state"]!="complete"
        || receipt["result"]["categories"]["ingest"]["selective"]["partition_sha256"]!=marker.partition_sha256 {return Err(unavailable());}
    let reset_out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![owner,format!("outbox_{}_s06_guarded_reset",marker.reset_receipt_id),marker.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
    if reset_out.as_ref().is_none_or(|(v,rev)|v!=&receipt_raw || *rev!=0) {return Err(unavailable());}
    let expected=serde_json::to_value(marker).map_err(|_|unavailable())?;
    for (event_id,kind) in [(job_event_id(owner,&marker.job_selector)?,"s04_import_erased"),(key_event_id(&marker.idempotency_sha256),"s04_import_key_erased")] {
        let row:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event_id,marker.reset_receipt_id,kind],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
        let Some((raw,rev))=row else {return Err(unavailable())};
        if rev!=0 || crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?!=expected {return Err(unavailable());}
    }
    Ok(json!({"state":"terminal_erased","job_selector":marker.job_selector,"idempotency_sha256":marker.idempotency_sha256,"partition_sha256":marker.partition_sha256,"import_partition_sha256":marker.import_partition_sha256,"reset_receipt_id":marker.reset_receipt_id}))
}
fn terminal_from_event(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,event_id:&str,kind:&str)->Result<Value,String> {
    let raw:String=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND kind=?3",params![owner,event_id,kind],|r|r.get(0)).optional().map_err(|_|unavailable())?.ok_or_else(unavailable)?;
    let parsed=crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?;
    match parsed.get("version").and_then(Value::as_str) {
        Some("s04.import-erased.v2")=>{let marker:ImportEraseCommitment=serde_json::from_value(parsed).map_err(|_|unavailable())?;verified_terminal(tx,owner,workspace,account,&marker)},
        Some("s04.import-erased-full.v1")=>{let marker:FullImportEraseCommitment=serde_json::from_value(parsed).map_err(|_|unavailable())?;verified_full_terminal(tx,owner,workspace,account,&marker)},
        _=>Err(unavailable()),
    }
}
/// Caller supplies current account/workspace authority. Missing or malformed
/// proof is unavailable; these helpers do not claim the action was absent.
pub(super) fn import_terminal_by_job(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,job_id:&str)->Result<Value,String> {
    let selector=job_selector(owner,job_id)?; let event=job_event_id(owner,&selector)?;
    let result=terminal_from_event(tx,owner,workspace,account,&event,"s04_import_erased")?;
    if result["job_selector"]!=selector {return Err(unavailable());} Ok(result)
}
pub(super) fn import_terminal_by_key(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,key:&str)->Result<Value,String> {
    let selector=key_selector(owner,key)?; let event=key_event_id(&selector);
    let result=terminal_from_event(tx,owner,workspace,account,&event,"s04_import_key_erased")?;
    if result["idempotency_sha256"]!=selector {return Err(unavailable());} Ok(result)
}


/// Prepare all affected removals under the caller's already-held reset claim.
/// The caller must invoke this before deleting candidate/source evidence, then
/// write the original reset receipt and call `write_selective_import_events`
/// in the same transaction. On any error the caller must roll back.
///
/// This first owner-supported slice removes only event-free jobs. Python's
/// import owners create no job events; other affected immutable event/change
/// closures require their actual erasure owner and fail before any effects.
pub(super) fn mark_selective_import(
    tx:&rusqlite::Transaction<'_>, owner:&str, reset_workspace_id:&str, account:&str, operation:&str,
    partition_sha256:&str, expected:&SelectiveImportPartition, reset_receipt_id:&str,
    budget:&mut ResetFenceBudget,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,
)->Result<Vec<ImportEraseCommitment>,String> {
    if reset_workspace_id.is_empty() || reset_workspace_id.trim()!=reset_workspace_id || reset_workspace_id.len()>512 || operation.is_empty() || operation.trim()!=operation || operation.len()>512 || !hex(partition_sha256)
        || reset_receipt_id!=reset_id(account,operation)? {return Err(unavailable());}
    let context:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND source_id IS NULL AND authorized_by=?1 AND reason='owner selective reset')",[owner],|r|r.get(0)).map_err(|_|unavailable())?;
    if !context {return Err(unavailable());}
    let current=classify_selective_import(tx,owner,account,budget,view)?;
    if serde_json::to_value(&current).map_err(|_|unavailable())?!=serde_json::to_value(expected).map_err(|_|unavailable())? {return Err(unavailable());}
    let mut markers=Vec::new();let mut key_events=BTreeSet::new();
    for selector in &current.erasable_job_selectors {
        budget.check(0)?;
        if current.immutable_event_job_selectors.binary_search(selector).is_ok() {return Err(unavailable());}
        let locator=current.raw_locators.get(selector).ok_or_else(unavailable)?;
        let binding=current.job_bindings.iter().find(|b|&b.job_selector==selector).ok_or_else(unavailable)?;
        if job_selector(owner,&locator.id)?!=*selector || key_selector(owner,&locator.key)?!=binding.idempotency_sha256 {return Err(unavailable());}
        let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=?1 AND m.job_id!=?2 AND m.parent_manifest_id IN (SELECT manifest_id FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2)) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND kind IN ('job_state','job_attempt') AND json_valid(payload_json) AND json_extract(payload_json,'$.job_id')=?2)",params![owner,locator.id],|r|r.get(0)).map_err(|_|unavailable())?;
        if linked {return Err(unavailable());}
        let job_event=job_event_id(owner,selector)?;let key_event=key_event_id(&binding.idempotency_sha256);
        if !key_events.insert(key_event.clone()) {return Err(unavailable());}
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id IN (?2,?3))",params![owner,job_event,key_event],|r|r.get(0)).map_err(|_|unavailable())?;
        if collision {return Err(unavailable());}
        markers.push(ImportEraseCommitment{version:"s04.import-erased.v2".into(),owner:owner.into(),workspace_id:binding.workspace_id.clone(),reset_workspace_id:reset_workspace_id.into(),account_id:account.into(),job_selector:selector.clone(),operation_id:operation.into(),partition_sha256:partition_sha256.into(),import_partition_sha256:current.digest.clone(),reset_receipt_id:reset_receipt_id.into(),idempotency_sha256:binding.idempotency_sha256.clone(),job_binding_sha256:stable(&serde_json::to_value(binding).map_err(|_|unavailable())?)?});
    }
    // All proof and collision checks precede the first write. Keep the raw
    // identifiers solely in the transaction-local map, never in the receipt.
    for selector in &current.erasable_job_selectors {
        budget.check(0)?;
        let locator=current.raw_locators.get(selector).ok_or_else(unavailable)?;
        if tx.execute("UPDATE fm_v2_jobs SET current_attempt_id=NULL WHERE owner_id=?1 AND job_id=?2",params![owner,locator.id]).map_err(|_|unavailable())?!=1 {return Err(unavailable());}
        tx.execute("DELETE FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2",params![owner,locator.id]).map_err(|_|unavailable())?;
        tx.execute("DELETE FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2",params![owner,locator.id]).map_err(|_|unavailable())?;
        if tx.execute("DELETE FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,locator.id]).map_err(|_|unavailable())?!=1 {return Err(unavailable());}
    }
    Ok(markers)
}

/// Commit both independently addressable denial facts to the exact original
/// reset change-set. The caller has inserted that receipt in this same tx and
/// must roll back unless every postcondition succeeds.
pub(super) fn write_selective_import_events(tx:&rusqlite::Transaction<'_>,markers:&[ImportEraseCommitment],now:&str)->Result<(),String> {
    for marker in markers {
        let raw=serde_json::to_string(marker).map_err(|_|unavailable())?;
        for (event_id,kind) in [(job_event_id(&marker.owner,&marker.job_selector)?,"s04_import_erased"),(key_event_id(&marker.idempotency_sha256),"s04_import_key_erased")] {
            tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![marker.owner,event_id,marker.reset_receipt_id,kind,raw,now]).map_err(|_|unavailable())?;
        }
        verified_terminal(tx,&marker.owner,&marker.workspace_id,&marker.account_id,marker)?;
    }
    Ok(())
}

/// Content-free full-owner import denial. It is separate from the unchanged
/// selective v2 commitment and has no approval or content claim.
#[derive(Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FullImportEraseCommitment {
    pub version:String,pub owner:String,pub workspace_id:String,pub reset_workspace_id:String,pub account_id:String,
    pub job_selector:String,pub operation_id:String,pub reset_receipt_id:String,
    pub idempotency_sha256:String,pub job_binding_sha256:String,
}
/// Snapshot exact import job/key identities before full reset deletion, inside
/// the same guarded transaction. Raw identifiers never enter returned markers.
/// The parent owns active-job fencing, snapshot, immutable-event/trigger
/// preflight and deletion; no approval inference is made here.
pub(super) fn collect_full_import_erasures(tx:&rusqlite::Transaction<'_>,owner:&str,reset_workspace:&str,account:&str,operation:&str,reset_receipt_id:&str,budget:&mut ResetFenceBudget)->Result<Vec<FullImportEraseCommitment>,String> {
    if [owner,reset_workspace,account,operation].iter().any(|v|v.is_empty() || v.trim()!=*v || v.len()>512) || reset_receipt_id!=reset_id(account,operation)? {return Err(unavailable());}
    let mut stmt=tx.prepare("SELECT job_id,idempotency_key,workspace_key,kind FROM fm_v2_jobs WHERE owner_id=?1 AND kind IN ('memory_file_import','memory_import_item','memory_import_batch') ORDER BY job_id").map_err(|_|unavailable())?;
    let mut rows=stmt.query([owner]).map_err(|_|unavailable())?;let mut markers=Vec::new();let mut seen_jobs=BTreeSet::new();let mut seen_keys=BTreeSet::new();
    while let Some(row)=rows.next().map_err(|_|unavailable())? {
        let mut size=0usize;for i in 0..4 {size=size.saturating_add(match row.get_ref(i).map_err(|_|unavailable())? {rusqlite::types::ValueRef::Text(v)=>v.len(),_=>return Err(unavailable())});}budget.check(size)?;
        let job:&str=row.get_ref(0).map_err(|_|unavailable())?.as_str().map_err(|_|unavailable())?;let key:&str=row.get_ref(1).map_err(|_|unavailable())?.as_str().map_err(|_|unavailable())?;let workspace:&str=row.get_ref(2).map_err(|_|unavailable())?.as_str().map_err(|_|unavailable())?;let kind:&str=row.get_ref(3).map_err(|_|unavailable())?.as_str().map_err(|_|unavailable())?;
        if job.is_empty() || key.is_empty() || workspace.is_empty() {return Err(unavailable());}
        let selector=job_selector(owner,job)?;let key_hash=key_selector(owner,key)?;
        if !seen_jobs.insert(selector.clone()) || !seen_keys.insert(key_hash.clone()) {return Err(unavailable());}
        let existing:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id IN (?2,?3))",params![owner,job_event_id(owner,&selector)?,key_event_id(&key_hash)],|r|r.get(0)).map_err(|_|unavailable())?;
        if existing {return Err(unavailable());}
        let binding=stable(&json!(["s04.full-import-job-binding.v1",owner,workspace,kind,selector,key_hash]))?;
        markers.push(FullImportEraseCommitment{version:"s04.import-erased-full.v1".into(),owner:owner.into(),workspace_id:workspace.into(),reset_workspace_id:reset_workspace.into(),account_id:account.into(),job_selector:selector,operation_id:operation.into(),reset_receipt_id:reset_receipt_id.into(),idempotency_sha256:key_hash,job_binding_sha256:binding});
    }
    markers.sort_by(|a,b|a.job_selector.cmp(&b.job_selector));Ok(markers)
}
fn verified_full_terminal(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,m:&FullImportEraseCommitment)->Result<Value,String> {
    if m.version!="s04.import-erased-full.v1" || m.owner!=owner || m.workspace_id!=workspace || m.account_id!=account || [m.owner.as_str(),m.workspace_id.as_str(),m.reset_workspace_id.as_str(),m.account_id.as_str(),m.operation_id.as_str()].iter().any(|v|v.is_empty() || v.trim()!=*v || v.len()>512) || !hex(&m.job_selector) || !hex(&m.idempotency_sha256) || !hex(&m.job_binding_sha256) || m.reset_receipt_id!=reset_id(account,&m.operation_id)? {return Err(unavailable());}
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=262144 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![owner,m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|unavailable())?;
    let (hash,raw,actor,actor_id,contract,source)=row.ok_or_else(unavailable)?;let receipt=crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?;
    if hash!=digest(raw.as_bytes()) || actor!="user" || actor_id!=owner || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || receipt.as_object().map(|v|v.len())!=Some(4) || receipt["version"]!="s06.guarded-reset.v1" || receipt["request_sha256"]!=stable(&receipt["request"])? || receipt["request"]["owner"]!=owner || receipt["request"]["workspace_id"]!=m.reset_workspace_id || receipt["request"]["account_id"]!=account || receipt["request"]["operation_id"]!=m.operation_id || receipt["request"].get("selective_manifest_sha256").is_some() || !receipt["request"]["components"].as_array().is_some_and(|v|v.iter().any(|c|c.as_str()==Some("memories"))) || receipt["result"]["complete"]!=true || receipt["result"]["categories"]["memories"]["state"]!="complete" || receipt["result"]["categories"]["ingest"]["state"]!="complete" {return Err(unavailable());}
    let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![owner,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id),m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
    if out.as_ref().is_none_or(|(v,r)|v!=&raw || *r!=0) {return Err(unavailable());}
    let expected=serde_json::to_value(m).map_err(|_|unavailable())?;
    for (id,kind) in [(job_event_id(owner,&m.job_selector)?,"s04_import_erased"),(key_event_id(&m.idempotency_sha256),"s04_import_key_erased")] {
        let row:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,id,m.reset_receipt_id,kind],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
        let (raw,revision)=row.ok_or_else(unavailable)?;if revision!=0 || crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?!=expected {return Err(unavailable());}
    }
    Ok(json!({"state":"terminal_erased","mode":"full","job_selector":m.job_selector,"idempotency_sha256":m.idempotency_sha256,"reset_receipt_id":m.reset_receipt_id}))
}
/// Write both lookup markers only after the original S06 full reset receipt and
/// its outbox exist. Any failure requires the caller to roll back the reset.
pub(super) fn write_full_import_events(tx:&rusqlite::Transaction<'_>,markers:&[FullImportEraseCommitment],now:&str)->Result<(),String> {
    for m in markers {
        let raw=serde_json::to_string(m).map_err(|_|unavailable())?;
        for (id,kind) in [(job_event_id(&m.owner,&m.job_selector)?,"s04_import_erased"),(key_event_id(&m.idempotency_sha256),"s04_import_key_erased")] {
            tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![m.owner,id,m.reset_receipt_id,kind,raw,now]).map_err(|_|unavailable())?;
        }
        verified_full_terminal(tx,&m.owner,&m.workspace_id,&m.account_id,m)?;
    }
    Ok(())
}
