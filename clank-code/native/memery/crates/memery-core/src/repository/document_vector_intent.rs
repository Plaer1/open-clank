//! Guarded original for one explicitly authorised document-vector build.
//! Not dispatched until the complete lifecycle owner is joined.
use super::*;
use super::document_vector::{DocumentVectorSnapshotRequest, vector_head};

const KIND:&str="document_vector_build.v1";
const MARKER:&str="openclank.document_vector_build_intent.v1";

#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct BuildIntent {
    pub version:u8,pub account_id:String,pub captured_owner:String,pub operation_id:String,
    pub workspace_id:String,pub project_id:String,pub head:BuildHead,pub probe:Value,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub source_epoch:Option<i64>,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct BuildHead {pub source_watermark:String,pub row_count:usize,pub expected_pointer:Option<Value>}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct BuildIdentity {pub version:u8,pub account_id:String,pub owner:String,pub operation_id:String}

fn valid_identifier(s:&str)->bool {s.len()>=16&&s.len()<=128&&s.bytes().all(|c|c.is_ascii_alphanumeric()||c==b'_'||c==b'-')}
fn valid_owner(s:&str)->bool {!s.is_empty()&&s.len()<=512&&s.trim()==s&&!s.chars().any(char::is_control)}
pub(crate) fn validate(intent:&BuildIntent)->Result<String,RepositoryError>{
    if !matches!((intent.version,intent.source_epoch),(1,None)|(2,Some(1..=i64::MAX))) || !valid_owner(&intent.account_id)||!valid_owner(&intent.captured_owner)
        ||!valid_identifier(&intent.operation_id)||intent.workspace_id!=""||intent.project_id!=""
        || !valid_sha256(&intent.head.source_watermark)||intent.head.row_count==0||intent.head.row_count>100_000 {
        return Err(RepositoryError::NotFound)
    }
    if let Some(pointer)=intent.head.expected_pointer.as_ref(){
        let o=pointer.as_object().ok_or(RepositoryError::NotFound)?;
        if o.len()!=3 || ["generation_id","publication_id","publication_watermark"].iter().any(|k|o.get(*k).and_then(Value::as_str).is_none_or(|v|v.is_empty()||v.len()>512)) {return Err(RepositoryError::NotFound)}
    }
    let p=intent.probe.as_object().ok_or(RepositoryError::NotFound)?;
    let expected_key=format!("dv_{}",sha256_hex(format!("{}\0probe",intent.operation_id).as_bytes()));
    if p.len()!=7 || p.get("rootOperationID")!=Some(&json!(intent.operation_id)) || p.get("idempotencyKey")!=Some(&json!(expected_key))
        || p.get("operation")!=Some(&json!("embeddings.create")) || p.get("input")!=Some(&json!({"texts":["dimension probe"],"purpose":"index"}))
        || p.get("artifactInputs")!=Some(&json!([])) || p.get("options")!=Some(&json!({})) {return Err(RepositoryError::NotFound)}
    let routes=p.get("routes").and_then(Value::as_array).filter(|v|v.len()==1).ok_or(RepositoryError::NotFound)?;
    let route=routes[0].as_object().ok_or(RepositoryError::NotFound)?;
    let required=["connectionID","providerID","billingLane","modelRouteID","modelID"];
    let allowed=["connectionID","providerID","billingLane","modelRouteID","modelID","grantID","preferredAccountID","inheritedAccountID"];
    if route.keys().any(|key|!allowed.contains(&key.as_str())) || required.iter().any(|k|!route.contains_key(*k))
       || route.values().any(|v|v.as_str().is_none_or(|s|s.is_empty()||s.len()>512||s.trim()!=s||s.chars().any(char::is_control))) {return Err(RepositoryError::NotFound)}
    let raw=super::document_upsert::sorted_json(&serde_json::to_value(intent).map_err(|_|RepositoryError::NotFound)?,false)?;
    if raw.len()>16_384{return Err(RepositoryError::NotFound)} Ok(raw)
}
fn job_id(account:&str,operation:&str)->Result<String,RepositoryError>{
    let raw=super::document_upsert::sorted_json(&json!(["openclank.document-vector-intent.v1",account,operation]),false)?;
    Ok(format!("job_vbuild_{}",sha256_hex(raw.as_bytes())))
}
fn require_profile(tx:&Transaction<'_>)->Result<(),RepositoryError>{
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?1",[MARKER],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(RepositoryError::NotFound)} Ok(())
}
fn current_head(tx:&Transaction<'_>,intent:&BuildIntent,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    let req=DocumentVectorSnapshotRequest{version:1,owner:intent.captured_owner.clone(),account_id:intent.account_id.clone(),after:None,expected_watermark:None,limit:1};
    let head=vector_head(tx,&req,view,budget)?;
    if head.get("state")!=Some(&json!("available")) || head.get("source_watermark")!=Some(&json!(intent.head.source_watermark))
        || head.get("row_count")!=Some(&json!(intent.head.row_count)) || head.get("expected_pointer")!=Some(&json!(intent.head.expected_pointer)) {return Err(RepositoryError::NotFound)}
    Ok(())
}
pub(crate) fn prepare(tx:&Transaction<'_>,intent:&BuildIntent,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    require_profile(tx)?;require_guarded_epoch(tx)?;
    if intent.source_epoch.is_some() || !matches!(intent.version,1|2){return Err(RepositoryError::NotFound)}
    let mut retained=intent.clone();
    if retained.version==2 {
        // Full source/receipt validation and counter initialization share this
        // same Immediate transaction; a caller never supplies epoch authority.
        current_head(tx,&retained,view,budget)?;
        retained.source_epoch=Some(super::document_vector_epoch::initialize_for_original(tx,&retained.captured_owner)?);
    }
    let raw=validate(&retained)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if retained.version==1 {current_head(tx,&retained,view,budget)?;}
    let job=job_id(&retained.account_id,&retained.operation_id)?;let hash=sha256_hex(raw.as_bytes());
    let existing:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2)",params![intent.captured_owner,job],|r|r.get(0))?;
    if existing {return Err(RepositoryError::NotFound)}
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,input_hash,created_at,updated_at) VALUES(?1,?2,?3,'','',?4,'queued',?5,?6,?6)",params![intent.captured_owner,job,KIND,intent.operation_id,hash,now])?;
    tx.execute("INSERT INTO fm_v2_job_manifests(owner_id,manifest_id,job_id,phase,selected_json,config_hash,model_hash,tool_hash,input_hash,content_hash,sealed_at) VALUES(?1,?2,?3,'selection',?4,?5,?5,?5,?5,?5,?6)",params![intent.captured_owner,format!("{job}:selection"),job,raw,hash,now])?;
    if retained.version==1 {Ok(json!({"version":1,"state":"queued","operation_id":retained.operation_id}))}
    else {Ok(json!({"version":1,"state":"queued","operation_id":retained.operation_id,"original":retained}))}
}
pub(crate) fn load(tx:&Transaction<'_>,id:&BuildIdentity,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(BuildIntent,String,String),RepositoryError>{
    require_profile(tx)?;if id.version!=1||view.current_owner()!=id.owner||view.account_id()!=id.account_id{return Err(RepositoryError::NotFound)}
    let job=job_id(&id.account_id,&id.operation_id)?;
    let (state,hash,raw,ih,ch,config,model,tool):(String,String,String,String,String,String,String,String)=tx.query_row("SELECT j.state,j.input_hash,CASE WHEN length(CAST(m.selected_json AS BLOB))<=16384 THEN m.selected_json END,m.input_hash,m.content_hash,m.config_hash,m.model_hash,m.tool_hash FROM fm_v2_jobs j JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' AND m.manifest_id=j.job_id||':selection' WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind=?3 AND j.idempotency_key=?4",params![id.owner,job,KIND,id.operation_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?)))?;
    budget.check(raw.len()).map_err(RepositoryError::Validation)?;if raw.len()>16_384||sha256_hex(raw.as_bytes())!=hash||ih!=hash||ch!=hash||config!=hash||model!=hash||tool!=hash{return Err(RepositoryError::NotFound)}
    let val=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let intent:BuildIntent=serde_json::from_value(val).map_err(|_|RepositoryError::NotFound)?;
    if intent.account_id!=id.account_id||intent.operation_id!=id.operation_id||validate(&intent)?!=raw{return Err(RepositoryError::NotFound)}
    view.for_captured(tx,&intent.captured_owner)?;
    Ok((intent,state,job))
}
pub(crate) fn claim(tx:&Transaction<'_>,id:&BuildIdentity,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    require_guarded_epoch(tx)?;let (intent,state,job)=load(tx,id,view,budget)?;
    if intent.captured_owner!=id.owner{return Err(RepositoryError::NotFound)}
    if let Some(epoch)=intent.source_epoch {super::document_vector_epoch::require(tx,&id.owner,epoch)?;}
    if intent.version==2 {require_retained_pointer(tx,&intent,&id.owner)?;} else {current_head(tx,&intent,view,budget)?;}
    if state!="queued" {return Err(RepositoryError::NotFound)}
    if tx.execute("UPDATE fm_v2_jobs SET state='active',updated_at=?1 WHERE owner_id=?2 AND job_id=?3 AND state='queued'",params![chrono::Utc::now().to_rfc3339(),id.owner,job])?!=1{return Err(RepositoryError::NotFound)}
    Ok(json!({"version":1,"state":"claimed","original":intent}))
}
/// The v2 original's full canonical snapshot was checked when its epoch was
/// minted. Later status uses the current epoch and exact pointer, not a full
/// corpus scan for each resumed batch.
fn require_retained_pointer(tx:&Transaction<'_>,intent:&BuildIntent,owner:&str)->Result<(),RepositoryError>{
    let pointer:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_tx,publication_watermark FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key=''",[owner],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let projected=pointer.as_ref().map(|(g,p,w)|json!({"generation_id":g,"publication_id":p,"publication_watermark":w}));
    if projected!=intent.head.expected_pointer{return Err(RepositoryError::NotFound)}
    if let Some((generation,publication,watermark))=pointer {
        let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_index_publications p JOIN fm_v2_derived_generations g ON g.owner_id=p.owner_id AND g.generation_id=p.generation_id WHERE p.owner_id=?1 AND p.publication_id=?2 AND p.logical_space='documents_vector' AND p.workspace_key='' AND p.project_key='' AND p.generation_id=?3 AND p.publication_watermark=?4 AND p.action IN ('publish','rollback') AND g.state='ready' AND g.retention_state='retained')",params![owner,publication,generation,watermark],|r|r.get(0))?;
        if !valid{return Err(RepositoryError::NotFound)}
    }Ok(())
}

/// A terminal fact is not a claim that its old source or publication is current.
fn retained_terminal(tx:&Transaction<'_>,id:&BuildIdentity,intent:&BuildIntent,state:&str,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    if intent.version!=2 || !matches!(state,"succeeded"|"cancelled") {return Err(RepositoryError::NotFound)}
    let ids=inventory(tx,&id.owner,&id.account_id,view,budget)?;
    if !ids.iter().any(|v|v.operation_id==id.operation_id) {return Err(RepositoryError::NotFound)}
    Ok(json!({"version":1,"state":"terminal","operation_id":id.operation_id,"terminal":if state=="succeeded" {"published"} else {"cancelled"}}))
}
pub(crate) fn status(tx:&Transaction<'_>,id:&BuildIdentity,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let (intent,state,job)=load(tx,id,view,budget)?;
    if intent.version==2 && matches!(state.as_str(),"succeeded"|"cancelled") {
        let current=super::document_vector_epoch::read(tx,&id.owner)?;
        if intent.captured_owner!=id.owner || current!=intent.source_epoch {
            return retained_terminal(tx,id,&intent,&state,view,budget)
        }
    }
    if intent.captured_owner!=id.owner{return Err(RepositoryError::NotFound)}
    if let Some(epoch)=intent.source_epoch {super::document_vector_epoch::require(tx,&id.owner,epoch)?;}
    if state=="cancelled" {
        let (raw,hash,input_hash):(String,String,String)=tx.query_row("SELECT CASE WHEN length(CAST(selected_json AS BLOB))<=16384 THEN selected_json END,content_hash,input_hash FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase='outcome' AND manifest_id=job_id||':outcome'",params![id.owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        if raw.len()>16_384||sha256_hex(raw.as_bytes())!=hash||input_hash!=sha256_hex(validate(&intent)?.as_bytes()){return Err(RepositoryError::NotFound)}
        let outcome=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let object=outcome.as_object().filter(|o|o.len()==5).ok_or(RepositoryError::NotFound)?;
        if object.get("version")!=Some(&json!(1))||object.get("state")!=Some(&json!("cancelled"))
            || object.get("operation_id")!=Some(&json!(id.operation_id))||object.get("paid_outcome")!=Some(&json!("unknown"))
            || !matches!(object.get("claim_state").and_then(Value::as_str),Some("unclaimed"|"claimed"))
            || super::document_upsert::sorted_json(&outcome,false)?!=raw {return Err(RepositoryError::NotFound)}
        return Ok(outcome)
    }
    if state=="succeeded" {
        let (raw,hash,input_hash):(String,String,String)=tx.query_row("SELECT CASE WHEN length(CAST(selected_json AS BLOB))<=16384 THEN selected_json END,content_hash,input_hash FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase='outcome' AND manifest_id=job_id||':outcome'",params![id.owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let original_hash=sha256_hex(validate(&intent)?.as_bytes());
        if raw.len()>16_384||sha256_hex(raw.as_bytes())!=hash||input_hash!=original_hash{return Err(RepositoryError::NotFound)}
        let outcome=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let object=outcome.as_object().filter(|o|o.len()==6).ok_or(RepositoryError::NotFound)?;
        if object.get("version")!=Some(&json!(1)) || object.get("state")!=Some(&json!("published"))
            || object.get("operation_id")!=Some(&json!(id.operation_id)) || object.get("source_watermark")!=Some(&json!(intent.head.source_watermark))
            || ["generation_id","publication_id"].iter().any(|k|object.get(*k).and_then(Value::as_str).is_none_or(|v|v.is_empty()||v.len()>512))
            || super::document_upsert::sorted_json(&outcome,false)?!=raw {return Err(RepositoryError::NotFound)}
        let snapshot=super::document_vector::canonical_snapshot(tx,&id.owner,&id.account_id,view,budget)?;
        if snapshot.source_watermark!=intent.head.source_watermark||snapshot.rows.len()!=intent.head.row_count{return Err(RepositoryError::NotFound)}
        let pointer:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_tx,publication_watermark FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key=''",[&id.owner],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        if pointer!=Some((object["generation_id"].as_str().unwrap().to_owned(),object["publication_id"].as_str().unwrap().to_owned(),intent.head.source_watermark.clone())) {
            if intent.version==2 {return retained_terminal(tx,id,&intent,&state,view,budget)}
            return Err(RepositoryError::NotFound)
        }
        return Ok(json!({"version":1,"state":"published","original":intent,"outcome":outcome}))
    }
    if intent.version==2 {require_retained_pointer(tx,&intent,&id.owner)?;} else {current_head(tx,&intent,view,budget)?;}
    if !matches!(state.as_str(),"queued"|"active") {return Err(RepositoryError::NotFound)}
    Ok(json!({"version":1,"state":if state=="active" {"unknown"} else {"queued"},"original":intent}))
}

/// A v2 stage is writable/readable only in its exact batch owner context.
/// Generic and v1 phases cannot bypass the immutable paid-request claim.
pub(crate) fn require_stage_context(tx:&Transaction<'_>,original:&super::document_vector::DocumentVectorBuildOriginal,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    let id=BuildIdentity{version:1,account_id:original.account_id.clone(),owner:original.owner.clone(),operation_id:original.operation_id.clone()};
    let job=job_id(&id.account_id,&id.operation_id)?;
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2)",params![id.owner,job],|r|r.get(0))?;
    if !exists{return Ok(())}
    let (intent,_,_)=load(tx,&id,view,budget)?;
    if intent.captured_owner!=id.owner{return Err(RepositoryError::NotFound)}
    if intent.version==2 {
        let allowed:i64=tx.query_row("SELECT fm_document_vector_batch_authorized()",[],|r|r.get(0))?;
        if allowed!=1{return Err(RepositoryError::NotFound)}
    }
    Ok(())
}

/// Bind all staging phases to the exact active original and current source.
pub(crate) fn require_stage(tx:&Transaction<'_>,original:&super::document_vector::DocumentVectorBuildOriginal,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    let id=BuildIdentity{version:1,account_id:original.account_id.clone(),owner:original.owner.clone(),operation_id:original.operation_id.clone()};
    let (intent,state,_)=load(tx,&id,view,budget)?;
    if state!="active" || intent.head.source_watermark!=original.source_watermark || intent.head.row_count!=original.row_count
        || json!(intent.head.expected_pointer)!=serde_json::to_value(&original.expected_pointer).map_err(|_|RepositoryError::NotFound)? {return Err(RepositoryError::NotFound)}
    if let Some(epoch)=intent.source_epoch {super::document_vector_epoch::require(tx,&id.owner,epoch)?;}
    current_head(tx,&intent,view,budget)
}

/// Bind the existing publication to its original job in the same transaction.
pub(crate) fn finish(tx:&Transaction<'_>,original:&super::document_vector::DocumentVectorBuildOriginal,result:&Value,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    let id=BuildIdentity{version:1,account_id:original.account_id.clone(),owner:original.owner.clone(),operation_id:original.operation_id.clone()};
    let (intent,state,job)=load(tx,&id,view,budget)?;
    let pointer=serde_json::to_value(&original.expected_pointer).map_err(|_|RepositoryError::NotFound)?;
    if state!="active" || intent.head.source_watermark!=original.source_watermark || intent.head.row_count!=original.row_count
        || json!(intent.head.expected_pointer)!=pointer || result.get("version")!=Some(&json!(1))
        || result.get("state")!=Some(&json!("published")) || result.get("current")!=Some(&json!(true))
        || result.get("source_watermark")!=Some(&json!(original.source_watermark)) {return Err(RepositoryError::NotFound)}
    let outcome=json!({"version":1,"state":"published","operation_id":id.operation_id,
        "generation_id":result.get("generation_id").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?,
        "publication_id":result.get("publication_id").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?,
        "source_watermark":original.source_watermark});
    let raw=super::document_upsert::sorted_json(&outcome,false)?;let hash=sha256_hex(raw.as_bytes());let now=chrono::Utc::now().to_rfc3339();
    let original_hash:String=tx.query_row("SELECT input_hash FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![id.owner,job],|r|r.get(0))?;
    tx.execute("INSERT INTO fm_v2_job_manifests(owner_id,manifest_id,job_id,phase,selected_json,config_hash,model_hash,tool_hash,input_hash,content_hash,sealed_at) VALUES(?1,?2,?3,'outcome',?4,?5,?5,?5,?6,?5,?7)",params![id.owner,format!("{job}:outcome"),job,raw,hash,original_hash,now])?;
    if tx.execute("UPDATE fm_v2_jobs SET state='succeeded',updated_at=?1 WHERE owner_id=?2 AND job_id=?3 AND state='active'",params![now,id.owner,job])?!=1{return Err(RepositoryError::NotFound)}
    Ok(())
}

/// Cancel future work; an active operation may still have an unknown paid outcome.
pub(crate) fn cancel(tx:&Transaction<'_>,id:&BuildIdentity,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    require_guarded_epoch(tx)?;let (intent,state,job)=load(tx,id,view,budget)?;
    if !matches!(state.as_str(),"queued"|"active") {return Err(RepositoryError::NotFound)}
    let outcome=json!({"version":1,"state":"cancelled","operation_id":id.operation_id,
        "paid_outcome":"unknown","claim_state":if state=="queued" {"unclaimed"} else {"claimed"}});
    let raw=super::document_upsert::sorted_json(&outcome,false)?;let hash=sha256_hex(raw.as_bytes());let original_hash=sha256_hex(validate(&intent)?.as_bytes());let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_job_manifests(owner_id,manifest_id,job_id,phase,selected_json,config_hash,model_hash,tool_hash,input_hash,content_hash,sealed_at) VALUES(?1,?2,?3,'outcome',?4,?5,?5,?5,?6,?5,?7)",params![id.owner,format!("{job}:outcome"),job,raw,hash,original_hash,now])?;
    if tx.execute("UPDATE fm_v2_jobs SET state='cancelled',updated_at=?1 WHERE owner_id=?2 AND job_id=?3 AND state=?4",params![now,id.owner,job,state])?!=1{return Err(RepositoryError::NotFound)}
    Ok(outcome)
}

pub(crate) fn cancel_owned(tx:&Transaction<'_>,id:&BuildIdentity,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let (intent,_,_)=load(tx,id,view,budget)?;
    if intent.version==2 {super::document_vector_batch::retire(tx,id,view,budget)} else {cancel(tx,id,view,budget)}
}

fn verify_retained_outcome(tx:&Transaction<'_>,id:&BuildIdentity,original:&BuildIntent,state:&str,job:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    let (raw,ih,ch,config,model,tool):(String,String,String,String,String,String)=tx.query_row("SELECT CASE WHEN length(CAST(selected_json AS BLOB))<=16384 THEN selected_json END,input_hash,content_hash,config_hash,model_hash,tool_hash FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase='outcome' AND manifest_id=job_id||':outcome'",params![id.owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)))?;
    budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let digest=sha256_hex(raw.as_bytes());
    if raw.len()>16_384||ih!=sha256_hex(validate(original)?.as_bytes())||ch!=digest||config!=digest||model!=digest||tool!=digest{return Err(RepositoryError::NotFound)}
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let obj=value.as_object().ok_or(RepositoryError::NotFound)?;
    if obj.get("version")!=Some(&json!(1))||obj.get("operation_id")!=Some(&json!(id.operation_id))||super::document_upsert::sorted_json(&value,false)?!=raw{return Err(RepositoryError::NotFound)}
    match state {
        "succeeded" if obj.len()==6 && obj.get("state")==Some(&json!("published")) && obj.get("source_watermark")==Some(&json!(original.head.source_watermark))
            && ["generation_id","publication_id"].iter().all(|k|obj.get(*k).and_then(Value::as_str).is_some_and(|v|!v.is_empty()&&v.len()<=512)) => Ok(()),
        "cancelled" if obj.len()==5 && obj.get("state")==Some(&json!("cancelled")) && obj.get("paid_outcome")==Some(&json!("unknown"))
            && matches!(obj.get("claim_state").and_then(Value::as_str),Some("unclaimed"|"claimed")) => Ok(()),
        _=>Err(RepositoryError::NotFound),
    }
}

/// Inventory only the exact family, refusing orphan or inconsistent lineage.
/// Callers retain the account custody view and transaction through retirement.
pub(crate) fn inventory(tx:&Transaction<'_>,owner:&str,account:&str,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Vec<BuildIdentity>,RepositoryError>{
    require_profile(tx)?;if view.current_owner()!=owner||view.account_id()!=account{return Err(RepositoryError::NotFound)}
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_job_manifests m LEFT JOIN fm_v2_jobs j ON j.owner_id=m.owner_id AND j.job_id=m.job_id WHERE m.owner_id=?1 AND (substr(m.job_id,1,11)='job_vbuild_' OR substr(m.manifest_id,1,11)='job_vbuild_' OR j.kind=?2) AND (j.job_id IS NULL OR j.kind<>?2 OR m.manifest_id<>m.job_id||':'||m.phase))",params![owner,KIND],|r|r.get(0))?;
    if orphan{return Err(RepositoryError::NotFound)}
    super::document_vector_batch::require_no_orphan_receipts(tx,owner)?;
    let mut stmt=tx.prepare("SELECT job_id,idempotency_key,kind FROM fm_v2_jobs WHERE owner_id=?1 AND (kind=?2 OR substr(job_id,1,11)='job_vbuild_') ORDER BY job_id")?;
    let mut rows=stmt.query(params![owner,KIND])?;let mut ids=Vec::new();
    while let Some(row)=rows.next()? {
        let job:String=row.get(0)?;let operation:String=row.get(1)?;let kind:String=row.get(2)?;
        budget.check(job.len()+operation.len()+kind.len()).map_err(RepositoryError::Validation)?;
        if kind!=KIND||job!=job_id(account,&operation)?||ids.len()>=100_000{return Err(RepositoryError::NotFound)}
        ids.push(BuildIdentity{version:1,account_id:account.to_owned(),owner:owner.to_owned(),operation_id:operation});
    }
    drop(rows);drop(stmt);
    for id in &ids {
        let (original,state,job)=load(tx,id,view,budget)?;
        if !matches!(state.as_str(),"queued"|"active"|"succeeded"|"cancelled") {return Err(RepositoryError::NotFound)}
        let outcomes:i64=tx.query_row("SELECT count(*) FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase='outcome' AND manifest_id=job_id||':outcome'",params![owner,job],|r|r.get(0))?;
        if outcomes!=if matches!(state.as_str(),"succeeded"|"cancelled") {1}else{0} {return Err(RepositoryError::NotFound)}
        if outcomes==1 {verify_retained_outcome(tx,id,&original,&state,&job,budget)?;}
        if original.version==2 {super::document_vector_epoch::require_if_v2(tx,owner)?;super::document_vector_batch::audit_intent(tx,owner,&original,&job,view,budget)?;}
    }
    Ok(ids)
}

/// Bind preserved intent jobs without adding them to reset deletion counts.
pub(crate) fn preserved_binding(tx:&Transaction<'_>,owner:&str,account:&str,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Option<String>,RepositoryError>{
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?1",[MARKER],|r|r.get(0)).optional()?;
    let residue:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND (kind=?2 OR substr(job_id,1,11)='job_vbuild_')) OR EXISTS(SELECT 1 FROM fm_v2_job_manifests WHERE owner_id=?1 AND (substr(job_id,1,11)='job_vbuild_' OR substr(manifest_id,1,11)='job_vbuild_')) OR EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND substr(job_id,1,11)='job_vbuild_') OR EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND substr(job_id,1,11)='job_vbuild_') OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND reason='document_vector.batch.v2') OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND kind='document_vector_batch_v2')",params![owner,KIND],|r|r.get(0))?;
    match marker.as_deref(){None if !residue=>return Ok(None),Some("1")=>{},_=>return Err(RepositoryError::NotFound)}
    let ids=inventory(tx,owner,account,view,budget)?;if ids.is_empty(){return Ok(None)}
    let mut records=Vec::with_capacity(ids.len());
    for id in ids {
        let (intent,state,job)=load(tx,&id,view,budget)?;
        let lifecycle=if intent.version==2 {Some(super::document_vector_batch::audit_intent(tx,owner,&intent,&job,view,budget)?)} else {None};
        let input:String=tx.query_row("SELECT input_hash FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0))?;
        let outcome:Option<String>=tx.query_row("SELECT content_hash FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase='outcome' AND manifest_id=job_id||':outcome'",params![owner,job],|r|r.get(0)).optional()?;
        budget.check(job.len()+state.len()+input.len()+outcome.as_ref().map_or(0,String::len)).map_err(RepositoryError::Validation)?;
        if let Some(binding)=lifecycle {records.push(json!([job,state,input,outcome,binding]));} else {records.push(json!([job,state,input,outcome]));}
    }
    let value=json!(["openclank.document-vector-preserved.v1",account,records]);
    Ok(Some(sha256_hex(super::document_upsert::sorted_json(&value,false)?.as_bytes())))
}

/// Retire unfinished originals before an already-authorized account purge.
/// The caller still owns account-lifecycle cancellation and deletion ordering.
pub(crate) fn retire_for_purge(tx:&Transaction<'_>,owner:&str,account:&str,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    if preserved_binding(tx,owner,account,view,budget)?.is_none(){return Ok(())}
    let ids=inventory(tx,owner,account,view,budget)?;
    for id in ids {
        let (original,state,_job)=load(tx,&id,view,budget)?;
        if matches!(state.as_str(),"queued"|"active") {if original.version==2 {super::document_vector_batch::retire(tx,&id,view,budget)?;} else {cancel(tx,&id,view,budget)?;}}
    }
    inventory(tx,owner,account,view,budget)?;
    Ok(())
}
