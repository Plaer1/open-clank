//! S08 settled-policy summary review. Source guards and named-human authority
//! are held by the existing Host review owner, not by proposal fields.
use super::*;
use super::s02_connection::ConnectionReviewRequest;
fn unavailable()->RepositoryError {RepositoryError::NotFound}
pub(super) fn key(r:&ConnectionReviewRequest)->Result<String,RepositoryError> {
    if r.version!=1 || r.candidate_revision!=3 || !matches!(r.action.as_str(),"accept"|"reject") || !valid_sha256(&r.candidate_sha256) || [&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,&r.request_key,&r.reason].iter().any(|s|s.is_empty() || s.trim()!=s.as_str() || s.len()>512 || s.chars().any(char::is_control)) || r.reason.len()>500 {return Err(unavailable());}
    Ok(format!("cs_s08policy_review_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-review.v1",&r.account_id,&r.candidate_id,&r.request_key))?)))
}
fn current_selection(tx:&Transaction<'_>,owner:&str,workspace:&str,fact:&s08_policy_lifecycle::SettledPolicyCandidate,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if fact.intent.output_kind!="summary" || fact.intent.selection.project_id.is_some() {return Err(unavailable());}
    let predicate:Option<(String,String,i64,i64)>=tx.query_row("SELECT value_type,cardinality,version,allow_unknown FROM fm_v2_predicates WHERE predicate='s08.composite'",[],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?;
    if predicate!=Some(("object".into(),"single".into(),1,0)) {return Err(unavailable());}
    for item in &fact.intent.selection.candidates {
        let size:Option<i64>=tx.query_row("SELECT length(CAST(r.value_json AS BLOB)) FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=b.current_revision WHERE b.owner_id=?1 AND b.workspace_key=?2 AND b.project_key='' AND b.block_id=?3 AND b.current_revision=?4 AND r.status IN ('active','open')",params![owner,workspace,item.block_id,item.revision],|x|x.get(0)).optional()?;
        let size=size.ok_or_else(unavailable)?;if !(1..=65536).contains(&size){return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
        let value:String=tx.query_row("SELECT value_json FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,item.block_id,item.revision],|x|x.get(0))?;
        if sha256_hex(value.as_bytes())!=item.value_sha256 {return Err(unavailable());}
    }
    Ok(())
}
pub(crate) fn summary_manifest(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let fact=s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;current_selection(tx,owner,workspace,&fact,budget)?;
    let contributors:Vec<Value>=fact.intent.selection.admissions.iter().map(|a|json!({"event_id":a.event_id,"source":a.source})).collect();
    let selection:Vec<Value>=fact.intent.selection.candidates.iter().map(|v|json!({"block_id":v.block_id,"revision":v.revision})).collect();
    let result=json!({"version":1,"candidate_id":candidate,"candidate_revision":3,"candidate_sha256":sha256_hex(fact.content.as_bytes()),"contributors":contributors,"selection":selection});
    budget.check(serde_json::to_vec(&result)?.len()).map_err(RepositoryError::Validation)?;require_policy_projection(tx,account,workspace,Some(policy))?;Ok(result)
}
/// Exact historical receipt read; absence is not permission to reissue.
pub(crate) fn summary_status(tx:&Transaction<'_>,r:&ConnectionReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    let id=key(r)?;require_guarded_epoch(tx)?;if view.current_owner()!=r.owner || view.account_id()!=r.account_id {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    let len:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,id],|x|x.get(0)).optional()?;
    let Some(len)=len else {
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2)",params![r.owner,format!("outbox_{id}_s08_policy_review")],|x|x.get(0))?;if orphan{return Err(unavailable());}
        let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3)",params![r.owner,r.workspace_id,r.candidate_id],|x|x.get(0))?;
        if !present {if s08_policy_lifecycle::candidate_erased(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,&r.candidate_sha256,view,budget)? {return Ok(json!({"state":"terminal_erased","candidate_id":r.candidate_id}));}return Err(unavailable());}
        let fact=s08_policy_lifecycle::settled_candidate(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
        if fact.intent.output_kind!="summary" || sha256_hex(fact.content.as_bytes())!=r.candidate_sha256 {return Err(unavailable());}return Ok(json!({"state":"absent"}));
    };
    if !(1..=16384).contains(&len){return Err(unavailable());}budget.check(len as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,actor,actor_id,reason):(String,String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND contract_version=?3",params![r.owner,id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?)))?;
    let v=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let captured=v["owner"].as_str().ok_or_else(unavailable)?;view.for_captured(tx,captured)?;
    if v.as_object().map(|x|x.len())!=Some(15) || v["version"]!="s08.policy-review.v1" || v["owner"]!=actor_id || actor!="user" || reason!="s08.policy-review.v1" || v["account_id"]!=r.account_id || v["workspace_id"]!=r.workspace_id || v["candidate_id"]!=r.candidate_id || v["candidate_revision"]!=3 || v["candidate_sha256"]!=r.candidate_sha256 || v["request_key"]!=r.request_key || v["reason"]!=r.reason || v["action"]!="accept" || r.action!="accept" || v["block_revision"]!=1 || !v["block_id"].is_string() || !v["curated_id"].is_string() || !v["block_change_set_id"].is_string() || !v["block_content_hash"].as_str().is_some_and(valid_sha256) || hash!=sha256_hex(raw.as_bytes()) {return Err(unavailable());}
    let event_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![r.owner,format!("outbox_{id}_s08_policy_review")],|x|x.get(0)).optional()?;let event_len=event_len.ok_or_else(unavailable)?;if !(1..=16384).contains(&event_len){return Err(unavailable());}budget.check(event_len as usize).map_err(RepositoryError::Validation)?;
    let event:Option<String>=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s08_policy_review' AND source_revision=1",params![r.owner,format!("outbox_{id}_s08_policy_review"),id],|x|x.get(0)).optional()?;if event.as_deref()!=Some(raw.as_str()) {return Err(unavailable());}
    if summary_accepted_root(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?!=v {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;Ok(json!({"state":"applied","candidate_id":r.candidate_id,"block_id":v["block_id"],"revision":1,"visibility":"not_rechecked"}))
}
/// Exact score fact from an already-verified settled S08 candidate. Missing or
/// skipped scores are unknown, never the candidate table's default zero.
fn initial_importance(fact:&s08_policy_lifecycle::SettledPolicyCandidate,candidate:&str,reviewer:&str)->Result<Option<Value>,RepositoryError> {
    let value=&fact.importance;
    if value.get("state").and_then(Value::as_str)==Some("skipped") { return Ok(None); }
    let score=value.get("value").and_then(Value::as_f64).filter(|v|v.is_finite() && (0.0..=1.0).contains(v)).ok_or_else(unavailable)?;
    let policy=value.get("provenance_sha256").and_then(Value::as_str).filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
    if value.as_object().map(|v|v.len())!=Some(4) || value["state"]!="applied" || !matches!(value["mode"].as_str(),Some("rules"|"model"|"hybrid")) || !valid_sha256(&fact.outcome_sha256) || !valid_sha256(&fact.result_receipt_sha256) || !valid_sha256(&fact.selection_sha256) {return Err(unavailable());}
    let (policy,source)=if fact.intent.output_kind=="importance_target" {
        let target=fact.intent.importance_target.as_ref().ok_or_else(unavailable)?;
        if !valid_sha256(&target.policy_snapshot_sha256) || !valid_sha256(&target.source_commitment_sha256) {return Err(unavailable());}
        (target.policy_snapshot_sha256.as_str(),target.source_commitment_sha256.as_str())
    } else {(policy,fact.selection_sha256.as_str())};
    let result=json!({"version":1,"value":score,"origin":"inferred","approved_by":reviewer,"job_id":fact.job_id,"candidate_id":candidate,"result_receipt_id":fact.result_receipt_id,"result_receipt_sha256":fact.result_receipt_sha256,"outcome_sha256":fact.outcome_sha256,"policy_snapshot_sha256":policy,"source_commitment_sha256":source});
    crate::ontology_v3::validate_revision_metadata(&json!({"importance_score":result}))?;
    Ok(Some(result))
}

pub(crate) fn accept_summary(tx:&Transaction<'_>,r:&ConnectionReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.action!="accept" {return Err(unavailable());}
    let receipt_id=key(r)?;let prior=summary_status(tx,r,view,budget)?;if matches!(prior["state"].as_str(),Some("applied"|"terminal_erased")){return Ok(prior)}
    let fact=s08_policy_lifecycle::settled_candidate(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;current_selection(tx,&r.owner,&r.workspace_id,&fact,budget)?;
    if sha256_hex(fact.content.as_bytes())!=r.candidate_sha256 {return Err(unavailable());}
    let candidate=read_candidate(tx,&r.owner,&r.candidate_id,&r.candidate_id,3,&r.workspace_id,"")?;if candidate.payload.state!=CandidateState::AwaitingReview {return Err(unavailable());}
    let block=format!("s08policyblock_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let curated=format!("s08policycurated_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let subject=format!("s08policyentity_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let occupied:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_entities WHERE owner_id=?2 AND entity_id=?3) OR EXISTS(SELECT 1 FROM memory_quarantine WHERE (tier='candidate' AND original_id=?4) OR (tier='curated' AND original_id=?1))",params![curated,r.owner,subject,r.candidate_id],|x|x.get(0))?;if occupied{return Err(unavailable());}
    let entity_actor=RevisionActor{actor_type:"automation".into(),actor_id:"s08_policy".into(),reason:"inferred summary container".into(),source_event_id:Some(fact.attempt_id.clone())};
    let entity=EntityPayload{entity_type:"concept".into(),canonical_label:"consolidated memory".into(),aliases:vec![],roles:vec![],tags:vec![],status:"active".into()};let now=chrono::Utc::now().to_rfc3339();
    let entity_change=insert_change_set(tx,&r.owner,&entity_actor,&serde_json::to_value(&entity)?,&now)?;
    tx.execute("INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,'',?,0,?)",params![r.owner,subject,r.workspace_id,entity_change,now])?;
    insert_entity_revision(tx,&r.owner,&subject,1,None,&entity,&entity_actor,&entity_change,&now)?;
    if tx.execute("UPDATE fm_v2_entities SET current_revision=1 WHERE owner_id=? AND entity_id=? AND current_revision=0",params![r.owner,subject])?!=1 {return Err(unavailable());}
    insert_outbox(tx,&r.owner,&entity_change,"entity_revision",&json!({"entity_id":subject,"revision":1}),1,&now)?;
    let actor=RevisionActor{actor_type:"user".into(),actor_id:r.owner.clone(),reason:r.reason.clone(),source_event_id:Some(fact.attempt_id.clone())};
    let identity=KnowledgeIdentity{block_id:block.clone(),subject_entity_id:subject,predicate:"s08.composite".into(),claim_slot:r.candidate_id.clone(),cardinality:PredicateCardinality::Single};
    let proposal=crate::field_authority::parse_unique_json(&fact.content).map_err(RepositoryError::Validation)?;
    let payload=json!({"value":{"type":"object","value":proposal},"expected_value_type":"object","kind":"fact","status":"active","tags":[],"confidence":0.0,"trust":0.0,"activation":"review_approved","activation_rationale":{"classification":"inferred_untrusted","reviewer":r.owner},"evidence_ids":candidate.payload.evidence_ids});
    let paths:Vec<String>=["/value","/expected_value_type","/kind","/tags","/status","/confidence","/trust","/activation","/activation_rationale","/valid_from","/valid_to","/evidence_ids"].iter().map(|x|x.to_string()).collect();
    let operation=json!({"version":"s03.published-create.v1","owner":r.owner,"workspace":r.workspace_id,"project":"","block_id":block,"subject_entity_id":identity.subject_entity_id,"predicate":identity.predicate,"claim_slot":identity.claim_slot,"cardinality":identity.cardinality,"payload":payload,"request_key":r.request_key,"actor_type":"user","actor_id":r.owner,"reason":r.reason,"source_event_id":actor.source_event_id,"approved_paths":paths,"producer":null});let operation_raw=serde_json::to_string(&operation)?;
    let review=crate::field_authority::OwnerReviewContext{authenticated_owner:r.owner.clone(),reviewer_id:r.owner.clone(),expected_revision:0,proposal_key:r.request_key.clone(),payload_hash:sha256_hex(operation_raw.as_bytes()),approved_paths:paths};let scope=ScopeRef{owner_id:r.owner.clone(),workspace_id:Some(r.workspace_id.clone()),project_id:None,session_id:None};
    let (revision,_)=V2Repository::guarded_create_knowledge_in_transaction(tx,&scope,&identity,&actor,&r.request_key,&operation_raw,Some(&review),None,true)?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1'",[],|row|row.get(0)).optional()?;
    let score_metadata=match marker.as_deref() {
        None=>None,
        Some("1")=>{super::s02_policy::require_override_profile(tx)?;initial_importance(&fact,&r.candidate_id,&r.owner)?},
        _=>return Err(unavailable()),
    };
    if let Some(value)=score_metadata.as_ref() {
        let metadata=serde_json::to_string(&json!({"importance_score":value}))?;
        let evidence=serde_json::to_string(&revision.payload.evidence_ids)?;
        tx.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES(?,?,1,'openclank.memory-revision-metadata.v1',?,?,'user',?,?,?,?,?)",params![r.owner,block,metadata,sha256_hex(metadata.as_bytes()),r.owner,revision.change_set_id,fact.attempt_id,evidence,now])?;
        tx.execute("INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id,created_at) VALUES(?,?,1,'/metadata/importance_score','inferred','user',?,NULL,?,?,?,?)",params![r.owner,block,r.owner,fact.attempt_id,evidence,revision.change_set_id,now])?;
    }
    let mut record=crate::record::MemoryRecord::new(&fact.content);record.id=curated.clone();record.kind=crate::record::MemoryKind::Fact;record.source="s08_policy".into();record.source_type=crate::record::SourceType::AutoExtracted;record.owner=Some(r.owner.clone());record.workspace_id=r.workspace_id.clone();record.metadata=json!({"candidate_id":r.candidate_id,"manually_reviewed":true});crate::store::sqlite::SqliteStore::upsert_curated_inner(tx,&record,None).map_err(RepositoryError::Validation)?;if let Some(value)=score_metadata.as_ref() {let score=value["value"].as_f64().ok_or_else(unavailable)?;if tx.execute("UPDATE curated SET importance_score=?1 WHERE id=?2 AND owner=?3 AND workspace_id=?4",params![score,curated,r.owner,r.workspace_id])?!=1{return Err(unavailable());}}
    let receipt=json!({"version":"s08.policy-review.v1","owner":r.owner,"account_id":r.account_id,"workspace_id":r.workspace_id,"candidate_id":r.candidate_id,"candidate_revision":3,"candidate_sha256":r.candidate_sha256,"request_key":r.request_key,"reason":r.reason,"action":"accept","block_id":block,"block_revision":1,"curated_id":curated,"block_change_set_id":revision.change_set_id,"block_content_hash":revision.content_hash});let raw=serde_json::to_string(&receipt)?;
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s08.policy-review.v1',NULL,?,?,?,?)",params![r.owner,receipt_id,r.owner,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    let mut activated=candidate.payload;activated.state=CandidateState::Activated;activated.accepted_block_id=Some(block.clone());insert_candidate_revision(tx,&r.owner,&r.candidate_id,4,Some(3),&activated,&actor,&receipt_id,&now)?;
    if tx.execute("UPDATE fm_v2_candidates SET current_revision=4 WHERE owner_id=? AND candidate_id=? AND current_revision=3",params![r.owner,r.candidate_id])?!=1 || tx.execute("UPDATE candidates SET status='accepted',reason=?,accepted_curated_id=?,updated_at=? WHERE owner=? AND workspace_id=? AND id=? AND status='pending' AND accepted_curated_id IS NULL",params![r.reason,curated,now,r.owner,r.workspace_id,r.candidate_id])?!=1 {return Err(unavailable());}
    insert_outbox(tx,&r.owner,&receipt_id,"candidate_revision",&json!({"candidate_id":r.candidate_id,"revision":4,"accepted_block_id":block}),4,&now)?;insert_outbox(tx,&r.owner,&receipt_id,"s08_policy_review",&receipt,1,&now)?;
    Ok(json!({"state":"applied","candidate_id":r.candidate_id,"block_id":block,"revision":1,"visibility":"current"}))
}
/// Factual accepted S08 summary root; no current source/capture grant.
pub(crate) fn summary_accepted_root(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    summary_accepted_root_in(tx,owner,account,workspace,candidate,view,budget,S08HistoricalRead::Ordinary)
}
pub(crate) fn summary_accepted_root_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,mode:S08HistoricalRead<'_, '_, '_>)->Result<Value,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    let fact=s08_policy_lifecycle::settled_candidate_accepted(tx,owner,account,workspace,candidate,view,budget)?;if fact.intent.output_kind!="summary" {return Err(unavailable());}
    let lengths:Vec<i64>={let mut q=tx.prepare("SELECT length(CAST(proposal_json AS BLOB)) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision IN (3,4) ORDER BY revision")?;let rows=q.query_map(params![owner,candidate],|x|x.get::<_,i64>(0))?.collect::<Result<Vec<_>,_>>()?;rows};
    if lengths.len()!=2 || lengths.iter().any(|n|!(1..=65536).contains(n)) {return Err(unavailable());}for n in lengths {budget.check(n as usize).map_err(RepositoryError::Validation)?;}
    let before=read_candidate_inner(tx,owner,candidate,candidate,3,workspace,"",false)?;let after=read_candidate_inner(tx,owner,candidate,candidate,4,workspace,"",false)?;
    let block=after.payload.accepted_block_id.clone().ok_or_else(unavailable)?;let mut expected=before.payload.clone();expected.state=CandidateState::Activated;expected.accepted_block_id=Some(block.clone());
    if before.payload.state!=CandidateState::AwaitingReview || before.content_hash!=hash_value(&serde_json::to_value(&before.payload)?)? || after.payload!=expected || after.previous_revision!=Some(3) || after.actor_type!="user" || after.source_event_id.as_deref()!=Some(fact.attempt_id.as_str()) || after.content_hash!=hash_value(&serde_json::to_value(&after.payload)?)? {return Err(unavailable());}view.for_captured(tx,&after.actor_id)?;
    let id=after.change_set_id.clone();let size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,id],|x|x.get(0)).optional()?;let size=size.ok_or_else(unavailable)?;if !(1..=16384).contains(&size){return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,actor,actor_id,reason):(String,String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND contract_version=?3",params![owner,id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?)))?;
    let receipt=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let request_key=receipt["request_key"].as_str().filter(|x|!x.is_empty() && x.trim()==*x && x.len()<=512).ok_or_else(unavailable)?;let review_reason=receipt["reason"].as_str().filter(|x|!x.is_empty() && x.trim()==*x && x.len()<=500).ok_or_else(unavailable)?;
    let expected_id=format!("cs_s08policy_review_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-review.v1",account,candidate,request_key))?));
    let curated=receipt["curated_id"].as_str().ok_or_else(unavailable)?;let change=receipt["block_change_set_id"].as_str().ok_or_else(unavailable)?;let block_hash=receipt["block_content_hash"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
    if receipt.as_object().map(|v|v.len())!=Some(15) || receipt["version"]!="s08.policy-review.v1" || receipt["owner"]!=after.actor_id || receipt["account_id"]!=account || receipt["workspace_id"]!=workspace || receipt["candidate_id"]!=candidate || receipt["candidate_revision"]!=3 || receipt["action"]!="accept" || receipt["candidate_sha256"]!=sha256_hex(fact.content.as_bytes()) || receipt["block_id"]!=block || receipt["block_revision"]!=1 || id!=expected_id || actor!="user" || actor_id!=after.actor_id || reason!="s08.policy-review.v1" || hash!=sha256_hex(raw.as_bytes()) || block!=format!("s08policyblock_{}",sha256_hex(&serde_json::to_vec(&(account,candidate))?)) || curated!=format!("s08policycurated_{}",sha256_hex(&serde_json::to_vec(&(account,candidate))?)) {return Err(unavailable());}
    for (event_id,event_change,kind,revision,expected) in [(format!("outbox_{id}_s08_policy_review"),id.as_str(),"s08_policy_review",1,receipt.clone()),(format!("outbox_{id}_candidate_revision"),id.as_str(),"candidate_revision",4,json!({"candidate_id":candidate,"revision":4,"accepted_block_id":block})),(format!("outbox_{change}_knowledge_revision"),change,"knowledge_revision",1,json!({"block_id":block,"revision":1}))] {
        let size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event_id],|x|x.get(0)).optional()?;let size=size.ok_or_else(unavailable)?;if !(1..=16384).contains(&size){return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
        let row:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND kind=?3 AND change_set_id=?4",params![owner,event_id,kind,event_change],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;let (raw,actual_rev)=row.ok_or_else(unavailable)?;if actual_rev!=revision || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=expected {return Err(unavailable());}
    }
    let proposal=crate::field_authority::parse_unique_json(&fact.content).map_err(RepositoryError::Validation)?;if before.payload.proposal!=proposal {return Err(unavailable());}
    let expected_payload=json!({"value":{"type":"object","value":proposal},"expected_value_type":"object","kind":"fact","status":"active","tags":[],"confidence":0.0,"trust":0.0,"activation":"review_approved","activation_rationale":{"classification":"inferred_untrusted","reviewer":after.actor_id},"valid_from":null,"valid_to":null,"evidence_ids":before.payload.evidence_ids});
    let entity=format!("s08policyentity_{}",sha256_hex(&serde_json::to_vec(&(account,candidate))?));
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=1 WHERE b.owner_id=?1 AND b.workspace_key=?2 AND b.project_key='' AND b.block_id=?3 AND b.subject_entity_id=?13 AND b.predicate='s08.composite' AND b.claim_slot=?4 AND b.cardinality='single' AND r.change_set_id=?5 AND r.content_hash=?6 AND r.actor_type='user' AND r.actor_id=?7 AND r.reason=?8 AND r.source_event_id=?9 AND r.previous_revision IS NULL AND r.value_type='object' AND r.value_json=?10 AND r.evidence_json=?11 AND r.expected_value_type='object' AND r.kind='fact' AND r.status='active' AND r.tags_json='[]' AND r.confidence=0 AND r.trust=0 AND r.activation='review_approved' AND r.activation_rationale=?12 AND r.valid_from IS NULL AND r.valid_to IS NULL)",params![owner,workspace,block,candidate,change,block_hash,after.actor_id,review_reason,fact.attempt_id,serde_json::to_string(&expected_payload["value"])?,serde_json::to_string(&expected_payload["evidence_ids"])?,serde_json::to_string(&expected_payload["activation_rationale"])?,entity],|x|x.get(0))?;
    let operation_len:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,change],|x|x.get(0)).optional()?;let operation_len=operation_len.ok_or_else(unavailable)?;if !(1..=131072).contains(&operation_len){return Err(unavailable());}budget.check(operation_len as usize).map_err(RepositoryError::Validation)?;
    let (operation_raw,operation_hash,operation_actor,operation_actor_id):(String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3 AND source_event_id=?4 AND contract_version=?5",params![owner,change,review_reason,fact.attempt_id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?)))?;
    let mut operation_payload=expected_payload.clone();operation_payload.as_object_mut().ok_or_else(unavailable)?.remove("valid_from");operation_payload.as_object_mut().ok_or_else(unavailable)?.remove("valid_to");
    let paths=json!(["/value","/expected_value_type","/kind","/tags","/status","/confidence","/trust","/activation","/activation_rationale","/valid_from","/valid_to","/evidence_ids"]);
    let entity_payload=json!({"entity_type":"concept","canonical_label":"consolidated memory","aliases":[],"roles":[],"tags":[],"status":"active"});
    let entity_change:Option<String>=tx.query_row("SELECT e.created_change_set_id FROM fm_v2_entities e JOIN fm_v2_entity_revisions r ON r.owner_id=e.owner_id AND r.entity_id=e.entity_id AND r.revision=1 JOIN fm_v2_change_sets c ON c.owner_id=e.owner_id AND c.change_set_id=e.created_change_set_id WHERE e.owner_id=?1 AND e.workspace_key=?2 AND e.project_key='' AND e.entity_id=?3 AND r.change_set_id=e.created_change_set_id AND r.previous_revision IS NULL AND r.payload=?4 AND r.content_hash=?5 AND r.actor_type='automation' AND r.actor_id='s08_policy' AND r.reason='inferred summary container' AND c.actor_type='automation' AND c.actor_id='s08_policy' AND c.reason='inferred summary container' AND c.source_event_id=?6 AND c.content_hash=?5 AND c.contract_version=?7",params![owner,workspace,entity,serde_json::to_string(&entity_payload)?,hash_value(&entity_payload)?,fact.attempt_id,CONTRACT_VERSION],|x|x.get(0)).optional()?;
    let entity_change=entity_change.ok_or_else(unavailable)?;
    let entity_event_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,format!("outbox_{entity_change}_entity_revision")],|x|x.get(0)).optional()?;let entity_event_len=entity_event_len.ok_or_else(unavailable)?;if !(1..=16384).contains(&entity_event_len){return Err(unavailable());}budget.check(entity_event_len as usize).map_err(RepositoryError::Validation)?;
    let entity_event:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='entity_revision'",params![owner,format!("outbox_{entity_change}_entity_revision"),entity_change],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;
    if entity_event.as_ref().is_none_or(|(raw,rev)|*rev!=1 || raw.as_ref().is_none_or(|raw|crate::field_authority::parse_unique_json(raw).ok()!=Some(json!({"entity_id":entity,"revision":1})))) {return Err(unavailable());}
    let operation=json!({"version":"s03.published-create.v1","owner":after.actor_id,"workspace":workspace,"project":"","block_id":block,"subject_entity_id":entity,"predicate":"s08.composite","claim_slot":candidate,"cardinality":"single","payload":operation_payload,"request_key":request_key,"actor_type":"user","actor_id":after.actor_id,"reason":review_reason,"source_event_id":fact.attempt_id,"approved_paths":paths,"producer":null});
    if operation_hash!=sha256_hex(operation_raw.as_bytes()) || operation_actor!="user" || operation_actor_id!=after.actor_id {return Err(unavailable());}
    // The guarded S03 writer stores a receipt envelope, not the operation as
    // the change-set body. Reuse its exact replay verifier for both hashes,
    // current source checks and the original operation bytes.
    let expected_operation=serde_json::to_string(&operation)?;
    let operation_hash=sha256_hex(expected_operation.as_bytes());
    let (verified_revision,decision)=match mode {
        S08HistoricalRead::Ordinary=>guarded_replay_active_custody(tx,owner,workspace,"",&block,change,&expected_operation,&operation_hash,view,budget)?,
        S08HistoricalRead::Phase(proof)=>{
            if !std::ptr::eq(tx,proof.tx) || !std::ptr::eq(view,proof.view) {return Err(unavailable())}
            let mut ctx=S08PhaseRead{proof,budget};
            guarded_replay_phase(&mut ctx,workspace,"",&block,change,&expected_operation,&operation_hash,&after.actor_id)?
        },
    }.ok_or_else(unavailable)?;
    let mut allowed:Vec<String>=serde_json::from_value(decision.get("allowed_paths").cloned().ok_or_else(unavailable)?)?;
    let mut approved:Vec<String>=serde_json::from_value(paths.clone())?;
    allowed.sort();approved.sort();
    if verified_revision.revision!=1 || verified_revision.change_set_id!=change || verified_revision.content_hash!=block_hash || decision.as_object().map(|v|v.len())!=Some(5) || decision["outcome"]!="allowed" || decision["review_paths"]!=json!([]) || allowed!=approved || allowed.windows(2).any(|v|v[0]==v[1]) {return Err(unavailable());}
    let authority:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND field_path='/value' AND origin='manual' AND producer_type='user' AND producer_id=?3 AND approved_by=?6 AND source_event_id=?4 AND change_set_id=?5)",params![owner,block,after.actor_id,fact.attempt_id,change,owner],|x|x.get(0))?;if !authority {return Err(unavailable());}
    let linked_evidence={let mut q=tx.prepare("SELECT evidence_id FROM fm_v2_revision_evidence WHERE owner_id=?1 AND block_id=?2 AND revision=1 ORDER BY evidence_id LIMIT 65")?;let rows=q.query_map(params![owner,block],|x|x.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;rows};
    let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND status='accepted' AND accepted_curated_id=?4)",params![owner,workspace,candidate,curated],|x|x.get(0))?;
    if !exists || !linked || linked_evidence!=before.payload.evidence_ids || block_hash!=hash_value(&expected_payload)? {return Err(unavailable());}
    Ok(receipt)
}
/// Verify the current initial S08 score against its settled original and
/// accepted Human root. Successors require their own exact receipt verifier;
/// unknown heads, manual values and missing provenance never become inferred.
// Private, transaction-local result: no transport or cross-read authority.
struct VerifiedSummaryRoot<'tx> { owner:&'tx str, account:&'tx str, workspace:&'tx str, candidate:&'tx str, value:Value }

pub(crate) fn current_initial_importance(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    current_initial_importance_in(tx,owner,account,workspace,candidate,view,budget,None,S08HistoricalRead::Ordinary)
}
fn current_initial_importance_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,verified:Option<&VerifiedSummaryRoot<'_>>,mode:S08HistoricalRead<'_, '_, '_>)->Result<Value,RepositoryError> {
    super::s02_policy::require_override_profile(tx)?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(unavailable());}
    let root=if let Some(v)=verified {
        if v.owner!=owner || v.account!=account || v.workspace!=workspace || v.candidate!=candidate {return Err(unavailable())}
        v.value.clone()
    } else {summary_accepted_root_in(tx,owner,account,workspace,candidate,view,budget,mode)?};
    let block=root["block_id"].as_str().ok_or_else(unavailable)?;
    let curated=root["curated_id"].as_str().ok_or_else(unavailable)?;
    let captured=root["owner"].as_str().ok_or_else(unavailable)?;
    view.for_captured(tx,captured)?;
    let head:Option<i64>=tx.query_row("SELECT current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND block_id=?3 AND claim_slot=?4",params![owner,workspace,block,candidate],|r|r.get(0)).optional()?;
    if head.is_none_or(|n|n<1) {return Err(unavailable());}
    let fact=s08_policy_lifecycle::settled_candidate_accepted(tx,owner,account,workspace,candidate,view,budget)?;
    let expected=initial_importance(&fact,candidate,captured)?.ok_or_else(unavailable)?;
    let size:Option<i64>=tx.query_row("SELECT length(CAST(metadata_json AS BLOB)) FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2 AND revision=1",params![owner,block],|r|r.get(0)).optional()?;
    let size=size.filter(|n|(1..=16384).contains(n)).ok_or_else(unavailable)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,change,actor,actor_id,source):(String,String,String,String,String,Option<String>)=tx.query_row("SELECT metadata_json,metadata_hash,change_set_id,actor_type,actor_id,source_event_id FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND metadata_schema='openclank.memory-revision-metadata.v1'",params![owner,block],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?.ok_or_else(unavailable)?;
    if sha256_hex(raw.as_bytes())!=hash || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=json!({"importance_score":expected}) || actor!="user" || actor_id!=captured || source.as_deref()!=Some(fact.attempt_id.as_str()) || root["block_change_set_id"]!=change {return Err(unavailable());}
    let authority:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND field_path='/metadata/importance_score' AND origin='inferred' AND producer_type='user' AND producer_id=?3 AND approved_by IS NULL AND source_event_id=?4 AND change_set_id=?5)",params![owner,block,captured,fact.attempt_id,change],|r|r.get(0))?;
    let projected:Option<f64>=tx.query_row("SELECT importance_score FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='s08_policy' AND archived=0",params![owner,workspace,curated],|r|r.get(0)).optional()?;
    if !authority || (head==Some(1) && projected!=expected["value"].as_f64()) {return Err(unavailable());}
    Ok(json!({"version":1,"state":"ready","candidate_id":candidate,"block_id":block,"curated_id":curated,"revision":1,"metadata_sha256":hash,"importance":expected}))
}

/// Closed original request and receipt for a Human-reviewed importance successor.
/// `expected_*` binds the prior fact; selectors never grant account authority.
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportanceReviewRequest {
    pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,
    pub candidate_id:String,pub block_id:String,pub curated_id:String,pub expected_revision:i64,
    pub expected_metadata_sha256:String,pub predecessor_receipt_id:String,pub predecessor_sha256:String,
    pub request_key:String,pub reason:String,pub importance:Value,pub policy_projection:PolicyPublicationRefs,
}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportanceReviewReceipt {
    pub version:String,pub request:ImportanceReviewRequest,pub request_sha256:String,
    pub revision:i64,pub metadata_sha256:String,pub knowledge_change_set_id:String,
    pub knowledge_content_sha256:String,
}
fn target_score_fact(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,accepted:bool,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<s08_policy_lifecycle::SettledPolicyCandidate,RepositoryError> {
    let fact=if accepted {s08_policy_lifecycle::settled_candidate_accepted(tx,owner,account,workspace,candidate,view,budget)?} else {s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?};
    if fact.intent.output_kind!="importance_target" || fact.intent.importance_target.is_none() {return Err(unavailable());}Ok(fact)
}
fn importance_receipt_id(account:&str,candidate:&str,key:&str)->Result<String,RepositoryError> {
    if [account,candidate,key].iter().any(|v|v.is_empty() || v.trim()!=*v || v.len()>512 || v.chars().any(char::is_control)) {return Err(unavailable());}
    Ok(format!("cs_s02importance_{}",sha256_hex(&serde_json::to_vec(&("s02.importance-review.v1",account,candidate,key))?)))
}

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportanceChildFact {
    pub captured_owner:String,pub account_id:String,pub workspace_id:String,pub block_id:String,
    pub selector_sha256:String,pub request_sha256:String,pub root_sha256:String,
    pub receipt_id:String,pub receipt_sha256:String,pub revision_change_id:String,pub revision_change_sha256:String,
    pub proposal_candidate_id:Option<String>,pub revision:i64,
}
pub(crate) fn importance_selector(account:&str,workspace:&str,block:&str,key:&str)->Result<String,RepositoryError> {
    use sha2::{Digest,Sha256};
    let mut digest=Sha256::new();digest.update(b"s02.importance-child-terminal.v1\0");
    for v in [account,workspace,block,key] {if v.is_empty() || v.trim()!=v || v.len()>512 || v.chars().any(char::is_control) {return Err(unavailable());}digest.update((v.len() as u64).to_be_bytes());digest.update(v.as_bytes());}
    Ok(digest.finalize().iter().map(|v|format!("{v:02x}")).collect())
}

/// Verified S08-specific root and bounded Human successor chain. Only the
/// exact receipt-paired revisions are accepted; absence never grants issue.
pub(crate) fn importance_chain_facts(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(Value,Vec<ImportanceChildFact>),RepositoryError> {
    importance_chain_facts_in(tx,owner,account,workspace,candidate,view,budget,None,S08HistoricalRead::Ordinary)
}
fn importance_chain_facts_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,verified:Option<&VerifiedSummaryRoot<'_>>,mode:S08HistoricalRead<'_, '_, '_>)->Result<(Value,Vec<ImportanceChildFact>),RepositoryError> {
    let root=current_initial_importance_in(tx,owner,account,workspace,candidate,view,budget,verified,mode)?;
    let block=root["block_id"].as_str().ok_or_else(unavailable)?;
    let curated=root["curated_id"].as_str().ok_or_else(unavailable)?;
    let head:i64=tx.query_row("SELECT current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND block_id=?3 AND claim_slot=?4",params![owner,workspace,block,candidate],|r|r.get(0))?;
    if !(1..=65).contains(&head) {return Err(unavailable());}
    let root_receipt:String=tx.query_row("SELECT change_set_id FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=4",params![owner,candidate],|r|r.get(0))?;
    let root_hash:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,root_receipt],|r|r.get(0))?;
    if !valid_sha256(&root_hash) {return Err(unavailable());}
    let original_content:String=tx.query_row("SELECT content_hash FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=1",params![owner,block],|r|r.get(0))?;
    let root_sha256=root_hash.clone();let mut children=Vec::new();
    let mut previous_id=root_receipt;let mut previous_hash=root_hash;
    let mut metadata_hash=root["metadata_sha256"].as_str().ok_or_else(unavailable)?.to_owned();
    let mut current=root["importance"].clone();
    let mut current_metadata=json!({"importance_score":current});
    for revision in 2..=head {
        budget.check(0).map_err(RepositoryError::Validation)?;
        let revision_size:Option<i64>=tx.query_row("SELECT length(CAST(content_hash AS BLOB))+length(CAST(actor_type AS BLOB))+length(CAST(actor_id AS BLOB))+length(CAST(source_event_id AS BLOB))+length(CAST(change_set_id AS BLOB)) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,block,revision],|r|r.get(0)).optional()?.flatten();
        let revision_size=revision_size.filter(|n|(1..=4096).contains(n)).ok_or_else(unavailable)?;budget.check(revision_size as usize).map_err(RepositoryError::Validation)?;
        let row:Option<(i64,String,String,String,String,String)>=tx.query_row("SELECT previous_revision,content_hash,actor_type,actor_id,source_event_id,change_set_id FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,block,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
        let (prior,content,actor,reviewer,receipt_id,change)=row.ok_or_else(unavailable)?;
        view.for_captured(tx,&reviewer)?;
        if prior!=revision-1 || content!=original_content || actor!="user" {return Err(unavailable());}
        let size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,receipt_id],|r|r.get(0)).optional()?;
        let size=size.filter(|n|(1..=65536).contains(n)).ok_or_else(unavailable)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
        let (raw,hash,receipt_actor,receipt_user):(String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s02.importance-review.v1' AND contract_version=?3",params![owner,receipt_id,CONTRACT_VERSION],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?.ok_or_else(unavailable)?;
        let parsed=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let receipt:ImportanceReviewReceipt=serde_json::from_value(parsed)?;let request=&receipt.request;
        if sha256_hex(raw.as_bytes())!=hash || receipt.version!="s02.importance-review.v1" || receipt_actor!="user" || receipt_user!=reviewer || receipt_id!=importance_receipt_id(account,candidate,&request.request_key)? || request.version!=1 || request.owner!=reviewer || request.account_id!=account || request.workspace_id!=workspace || request.candidate_id!=candidate || request.block_id!=block || request.curated_id!=curated || request.expected_revision!=prior || request.expected_metadata_sha256!=metadata_hash || request.predecessor_receipt_id!=previous_id || request.predecessor_sha256!=previous_hash || receipt.revision!=revision || receipt.knowledge_change_set_id!=change || receipt.knowledge_content_sha256!=content || receipt.request_sha256!=sha256_hex(&serde_json::to_vec(request)?) {return Err(unavailable());}
        if request.reason.is_empty() || request.reason.trim()!=request.reason || request.reason.len()>500 || request.reason.chars().any(char::is_control) {return Err(unavailable());}
        let expected_change=format!("cs_s02importance_revision_{}",sha256_hex(&serde_json::to_vec(&("s02.importance-revision.v1",account,candidate,&request.request_key))?));
        if change!=expected_change {return Err(unavailable());}
        let guard_size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,change],|r|r.get(0)).optional()?;
        let guard_size=guard_size.filter(|n|(1..=131072).contains(n)).ok_or_else(unavailable)?;budget.check(guard_size as usize).map_err(RepositoryError::Validation)?;
        let (guard_raw,guard_hash):(String,String)=tx.query_row("SELECT operation_metadata_json,content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND actor_type='user' AND actor_id=?3 AND reason=?4 AND source_event_id=?5 AND contract_version=?6",params![owner,change,reviewer,request.reason,receipt_id,CONTRACT_VERSION],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or_else(unavailable)?;
        let guard=crate::field_authority::parse_unique_json(&guard_raw).map_err(RepositoryError::Validation)?;
        let operation=serde_json::to_string(request)?;
        let before=json!({"metadata":{"importance_score":current}});let after=json!({"metadata":{"importance_score":request.importance}});
        let paths:Vec<String>=crate::field_authority::proposal_fields(&before,&after).map_err(RepositoryError::Validation)?.iter().map(|x|x["path"].as_str().map(str::to_owned).ok_or_else(unavailable)).collect::<Result<_,_>>()?;
        let provenance=std::collections::BTreeMap::from([("/metadata/importance_score".to_owned(),current["origin"].as_str().ok_or_else(unavailable)?.to_owned())]);
        let review=crate::field_authority::OwnerReviewContext{authenticated_owner:reviewer.clone(),reviewer_id:reviewer.clone(),expected_revision:prior,proposal_key:request.request_key.clone(),payload_hash:receipt.request_sha256.clone(),approved_paths:paths};
        let decision=crate::field_authority::decide_fields(&reviewer,&before,&after,prior,prior,&provenance,&request.request_key,&receipt.request_sha256,None,Some(&review),None,None).map_err(RepositoryError::Validation)?;
        if decision.outcome!="allowed" {return Err(unavailable());}
        let decision_raw=serde_json::to_string(&serde_json::to_value(&decision)?)?;
        if sha256_hex(guard_raw.as_bytes())!=guard_hash || guard!=json!({"version":"s03.receipt.v2","operation_json":operation,"operation_sha256":receipt.request_sha256,"decision_json":decision_raw,"decision_sha256":sha256_hex(decision_raw.as_bytes())}) {return Err(unavailable());}
        let knowledge_event:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='knowledge_revision'",params![owner,format!("outbox_{change}_knowledge_revision"),change],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if knowledge_event.as_ref().is_none_or(|(raw,n)|*n!=revision || raw.as_ref().and_then(|v|crate::field_authority::parse_unique_json(v).ok())!=Some(json!({"block_id":block,"revision":revision}))) {return Err(unavailable());}
        budget.check(knowledge_event.as_ref().and_then(|(body,_)|body.as_ref()).ok_or_else(unavailable)?.len()).map_err(RepositoryError::Validation)?;
        let event:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s02_importance_review'",params![owner,format!("outbox_{receipt_id}_s02_importance_review"),receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if event.as_ref().is_none_or(|(body,n)|*n!=revision || body.as_deref()!=Some(raw.as_str())) {return Err(unavailable());}
        budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let meta_size:Option<i64>=tx.query_row("SELECT length(CAST(metadata_json AS BLOB))+length(CAST(metadata_hash AS BLOB))+length(CAST(change_set_id AS BLOB))+length(CAST(actor_id AS BLOB))+length(CAST(source_event_id AS BLOB)) FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,block,revision],|r|r.get(0)).optional()?.flatten();
        let meta_size=meta_size.filter(|n|(1..=20480).contains(n)).ok_or_else(unavailable)?;budget.check(meta_size as usize).map_err(RepositoryError::Validation)?;
        let row:Option<(Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT CASE WHEN length(CAST(metadata_json AS BLOB))<=16384 THEN metadata_json END,metadata_hash,change_set_id,actor_id,source_event_id FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2 AND revision=?3 AND metadata_schema='openclank.memory-revision-metadata.v1' AND actor_type='user'",params![owner,block,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
        let (body,actual_hash,actual_change,metadata_actor,source)=row.ok_or_else(unavailable)?;let body=body.ok_or_else(unavailable)?;budget.check(body.len()).map_err(RepositoryError::Validation)?;
        let metadata=crate::field_authority::parse_unique_json(&body).map_err(RepositoryError::Validation)?;crate::ontology_v3::validate_revision_metadata(&metadata)?;
        if metadata["importance_score"]!=request.importance || sha256_hex(body.as_bytes())!=actual_hash || receipt.metadata_sha256!=actual_hash || actual_change!=change || metadata_actor!=reviewer || source.as_deref()!=Some(receipt_id.as_str()) || request.importance["approved_by"]!=reviewer {return Err(unavailable());}
        let mut before_other=current_metadata.clone();let mut after_other=metadata.clone();
        before_other.as_object_mut().ok_or_else(unavailable)?.remove("importance_score");after_other.as_object_mut().ok_or_else(unavailable)?.remove("importance_score");
        if before_other!=after_other {return Err(unavailable());}
        let origin=request.importance["origin"].as_str().ok_or_else(unavailable)?;
        match origin {
            "manual"=>{
                let mut prior_score=current.clone();let prior_obj=prior_score.as_object_mut().ok_or_else(unavailable)?;
                for key in ["value","origin","approved_by"] {prior_obj.remove(key);}
                let mut new_score=request.importance.clone();let new_obj=new_score.as_object_mut().ok_or_else(unavailable)?;
                for key in ["value","origin","approved_by"] {new_obj.remove(key);}
                if prior_score!=new_score {return Err(unavailable());}
            },
            "inferred"=>{
                if current["origin"]=="manual" {return Err(unavailable());}
                let proposed=request.importance["candidate_id"].as_str().ok_or_else(unavailable)?;
                let fact=target_score_fact(tx,owner,account,workspace,proposed,true,view,budget)?;
                let target=fact.intent.importance_target.as_ref().ok_or_else(unavailable)?;
                let value_size:Option<i64>=tx.query_row("SELECT length(CAST(value_json AS BLOB)) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,block,prior],|r|r.get(0)).optional()?;
                let value_size=value_size.filter(|n|(1..=16384).contains(n)).ok_or_else(unavailable)?;budget.check(value_size as usize).map_err(RepositoryError::Validation)?;
                let prior_value:String=tx.query_row("SELECT value_json FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,block,prior],|r|r.get(0))?;
                if target.block_id!=block || target.expected_revision!=prior || target.value_sha256!=sha256_hex(prior_value.as_bytes()) || initial_importance(&fact,proposed,&reviewer)?.as_ref()!=Some(&request.importance) {return Err(unavailable());}
                let before=read_candidate_inner(tx,owner,proposed,proposed,3,workspace,"",false)?;
                let after=read_candidate_inner(tx,owner,proposed,proposed,4,workspace,"",false)?;
                let mut expected=before.payload.clone();expected.state=CandidateState::Activated;expected.accepted_block_id=Some(block.to_owned());
                if before.payload.state!=CandidateState::AwaitingReview || after.payload!=expected || after.previous_revision!=Some(3) || after.actor_type!="user" || after.actor_id!=reviewer || after.change_set_id!=receipt_id || after.source_event_id.as_deref()!=Some(receipt_id.as_str()) || after.content_hash!=hash_value(&serde_json::to_value(&after.payload)?)? {return Err(unavailable());}
                let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND status='accepted' AND accepted_curated_id=?4)",params![owner,workspace,proposed,curated],|r|r.get(0))?;
                let event:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_revision'",params![owner,format!("outbox_{receipt_id}_candidate_revision"),receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
                if !linked || event.as_ref().is_none_or(|(raw,n)|*n!=4 || raw.as_ref().and_then(|v|crate::field_authority::parse_unique_json(v).ok())!=Some(json!({"candidate_id":proposed,"revision":4,"accepted_block_id":block}))) {return Err(unavailable());}
                budget.check(event.as_ref().and_then(|(raw,_)|raw.as_ref()).ok_or_else(unavailable)?.len()).map_err(RepositoryError::Validation)?;
            },
            _=>return Err(unavailable()),
        }
        let auth:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=?3 AND field_path='/metadata/importance_score' AND origin=?7 AND producer_type='user' AND producer_id=?4 AND ((?7='manual' AND approved_by=?8) OR (?7='inferred' AND approved_by IS NULL)) AND source_event_id=?5 AND change_set_id=?6)",params![owner,block,revision,reviewer,receipt_id,change,origin,owner],|r|r.get(0))?;
        if !auth {return Err(unavailable());}
        children.push(ImportanceChildFact{captured_owner:reviewer.clone(),account_id:account.into(),workspace_id:workspace.into(),block_id:block.into(),selector_sha256:importance_selector(account,workspace,block,&request.request_key)?,request_sha256:receipt.request_sha256.clone(),root_sha256:root_sha256.clone(),receipt_id:receipt_id.clone(),receipt_sha256:hash.clone(),revision_change_id:change.clone(),revision_change_sha256:guard_hash.clone(),proposal_candidate_id:if origin=="inferred" {request.importance["candidate_id"].as_str().map(str::to_owned)} else {None},revision});
        current=request.importance.clone();current_metadata=metadata;metadata_hash=actual_hash;previous_id=receipt_id;previous_hash=hash;
    }
    let projected:Option<f64>=tx.query_row("SELECT importance_score FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='s08_policy' AND archived=0",params![owner,workspace,curated],|r|r.get(0)).optional()?;
    if projected!=current["value"].as_f64() {return Err(unavailable());}
    Ok((json!({"version":1,"state":"ready","candidate_id":candidate,"block_id":block,"curated_id":curated,"revision":head,"metadata_sha256":metadata_hash,"predecessor_receipt_id":previous_id,"predecessor_sha256":previous_hash,"importance":current}),children))
}

pub(crate) fn importance_chain_facts_phase(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,proof:&S08PhaseProof<'_, '_>)->Result<(Value,Vec<ImportanceChildFact>),RepositoryError> {
    importance_chain_facts_in(tx,owner,account,workspace,candidate,view,budget,None,S08HistoricalRead::Phase(proof))
}

pub(crate) fn current_importance_context(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    Ok(importance_chain_facts(tx,owner,account,workspace,candidate,view,budget)?.0)
}

/// Resolve an authorized curated selector only through its exact S08 accepted
/// root and current score chain. The curated metadata is discovery, not proof.
pub(crate) fn current_importance_for_curated(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,curated:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if curated.is_empty() || curated.trim()!=curated || curated.len()>512 {return Err(unavailable());}
    let size:Option<i64>=tx.query_row("SELECT length(CAST(metadata AS BLOB)) FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='s08_policy' AND archived=0",params![owner,workspace,curated],|r|r.get(0)).optional()?;
    let size=size.filter(|n|(1..=16384).contains(n)).ok_or_else(unavailable)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT metadata FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='s08_policy' AND archived=0",params![owner,workspace,curated],|r|r.get(0))?;
    let metadata=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let candidate=metadata.get("candidate_id").and_then(Value::as_str).filter(|v|!v.is_empty() && v.len()<=512).ok_or_else(unavailable)?;
    let result=current_importance_context(tx,owner,account,workspace,candidate,view,budget)?;
    if result["curated_id"]!=curated {return Err(unavailable());}Ok(result)
}

/// Exact target-score candidate preview, under the caller's current account,
/// source and policy authority. The Human sees a score proposal, never a fact.
pub(crate) fn importance_target_manifest(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,proposal:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let fact=target_score_fact(tx,owner,account,workspace,proposal,false,view,budget)?;
    let target=fact.intent.importance_target.as_ref().ok_or_else(unavailable)?;
    let (identity,w,p,head)=read_knowledge_identity(tx,owner,&target.block_id)?;
    if w!=workspace || !p.is_empty() || head!=target.expected_revision || identity.claim_slot.is_empty() {return Err(unavailable());}
    let facts=super::s02_policy::importance_target_facts_in(tx,owner,account,workspace,&identity.claim_slot,policy,view,budget)?;
    let snapshots=super::s02_policy::s08_policy_snapshots_in(tx,&fact.intent.selection,policy,view,budget)?;
    if facts["target"]!=serde_json::to_value(target)? || snapshots["commitment_sha256"]!=fact.intent.snapshots_sha256 {return Err(unavailable());}
    let importance=initial_importance(&fact,proposal,owner)?.ok_or_else(unavailable)?;
    let content_sha256=sha256_hex(fact.content.as_bytes());
    require_policy_projection(tx,account,workspace,Some(policy))?;
    Ok(json!({"version":1,"state":"ready","proposal_candidate_id":proposal,"proposal_sha256":content_sha256,"root_candidate_id":identity.claim_slot,"target":facts["target"],"score_context":facts["score_context"],"importance":importance}))
}

/// Read the exact original score review; an absent receipt is unknown, never
/// permission to send the request again.
pub(crate) fn importance_review_original(tx:&Transaction<'_>,owner:&str,r:&ImportanceReviewRequest,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.version!=1 || view.current_owner()!=owner || view.account_id()!=r.account_id {return Err(unavailable());}
    view.for_captured(tx,&r.owner)?;
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(policy))?;
    if crate::store::sqlite::s02_importance_terminal::probe(tx,owner,&r.account_id,&r.workspace_id,&r.block_id,&r.request_key,view,budget).map_err(|_|unavailable())? {return Ok(json!({"version":1,"state":"terminal_erased"}));}
    let id=importance_receipt_id(&r.account_id,&r.candidate_id,&r.request_key)?;
    let row:Option<(Option<String>,String)>=tx.query_row("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s02.importance-review.v1'",params![owner,id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
    let Some((raw,hash))=row else {
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2)",params![owner,format!("outbox_{id}_s02_importance_review")],|row|row.get(0))?;
        if orphan {return Err(unavailable());}return Ok(json!({"version":1,"state":"unknown"}));
    };
    let raw=raw.ok_or_else(unavailable)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if sha256_hex(raw.as_bytes())!=hash {return Err(unavailable());}
    let saved:ImportanceReviewReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if saved.request_sha256!=sha256_hex(&serde_json::to_vec(r)?) || saved.revision!=r.expected_revision.checked_add(1).ok_or_else(unavailable)? {return Err(unavailable());}
    let current=current_importance_context(tx,owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
    if current["revision"].as_i64().is_none_or(|v|v<saved.revision) {return Err(unavailable());}
    let link:Option<String>=tx.query_row("SELECT source_event_id FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,r.block_id,saved.revision],|row|row.get(0)).optional()?.flatten();
    if link.as_deref()!=Some(id.as_str()) {return Err(unavailable());}
    view.for_captured(tx,&r.owner)?;
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(policy))?;
    Ok(json!({"version":1,"state":"recorded","revision":saved.revision}))
}

/// Fresh exact Human review facts. Preparing a durable original never trusts
/// caller-supplied root, selector, score proposal or policy assertions.
pub(crate) struct ImportanceReviewValidation {
    pub root_receipt_id:String,pub root_sha256:String,pub selector_sha256:String,pub request_sha256:String,
    current:Value,accepted_target:Option<(String,s08_policy_lifecycle::SettledPolicyCandidate)>,
    decision:crate::field_authority::Decision,origin:String,
}
pub(crate) fn validate_importance_review(tx:&Transaction<'_>,r:&ImportanceReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<ImportanceReviewValidation,RepositoryError> {
    if r.version!=1 || view.current_owner()!=r.owner || view.account_id()!=r.account_id || r.expected_revision<1 || r.reason.is_empty() || r.reason.trim()!=r.reason || r.reason.len()>500 || r.reason.chars().any(char::is_control) || !valid_sha256(&r.expected_metadata_sha256) || !valid_sha256(&r.predecessor_sha256) {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    if crate::store::sqlite::s02_importance_terminal::probe(tx,&r.owner,&r.account_id,&r.workspace_id,&r.block_id,&r.request_key,view,budget).map_err(|_|unavailable())? {return Err(unavailable());}
    let (current,_)=importance_chain_facts(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
    let request_hash=sha256_hex(&serde_json::to_vec(r)?);
    if current["block_id"]!=r.block_id || current["curated_id"]!=r.curated_id || current["revision"]!=r.expected_revision || current["metadata_sha256"]!=r.expected_metadata_sha256 || current["predecessor_receipt_id"]!=r.predecessor_receipt_id || current["predecessor_sha256"]!=r.predecessor_sha256 {return Err(unavailable());}
    crate::ontology_v3::validate_revision_metadata(&json!({"importance_score":r.importance}))?;
    if r.importance["approved_by"]!=r.owner {return Err(unavailable());}
    let origin=r.importance["origin"].as_str().ok_or_else(unavailable)?.to_owned();
    let mut accepted_target:Option<(String,s08_policy_lifecycle::SettledPolicyCandidate)>=None;
    match origin.as_str() {
        "manual"=>{
            let mut previous=current["importance"].clone();let mut proposed=r.importance.clone();
            for key in ["value","origin","approved_by"] {previous.as_object_mut().ok_or_else(unavailable)?.remove(key);proposed.as_object_mut().ok_or_else(unavailable)?.remove(key);}
            if previous!=proposed {return Err(unavailable());}
        },
        "inferred"=>{
            if current["importance"]["origin"]=="manual" {return Err(unavailable());}
            let proposed=r.importance["candidate_id"].as_str().ok_or_else(unavailable)?;
            let fact=target_score_fact(tx,&r.owner,&r.account_id,&r.workspace_id,proposed,false,view,budget)?;
            let target=fact.intent.importance_target.as_ref().ok_or_else(unavailable)?;
            let facts=super::s02_policy::importance_target_facts_in(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,&r.policy_projection,view,budget)?;
            let snapshots=super::s02_policy::s08_policy_snapshots_in(tx,&fact.intent.selection,&r.policy_projection,view,budget)?;
            if target.block_id!=r.block_id || target.expected_revision!=r.expected_revision || facts["target"]!=serde_json::to_value(target)? || snapshots["commitment_sha256"]!=fact.intent.snapshots_sha256 || initial_importance(&fact,proposed,&r.owner)?.as_ref()!=Some(&r.importance) {return Err(unavailable());}
            accepted_target=Some((proposed.to_owned(),fact));
        },
        _=>return Err(unavailable()),
    }
    let before=json!({"metadata":{"importance_score":current["importance"]}});let after=json!({"metadata":{"importance_score":r.importance}});
    let paths:Vec<String>=crate::field_authority::proposal_fields(&before,&after).map_err(RepositoryError::Validation)?.iter().map(|v|v["path"].as_str().map(str::to_owned).ok_or_else(unavailable)).collect::<Result<_,_>>()?;
    let provenance=std::collections::BTreeMap::from([("/metadata/importance_score".to_owned(),current["importance"]["origin"].as_str().ok_or_else(unavailable)?.to_owned())]);
    let review=crate::field_authority::OwnerReviewContext{authenticated_owner:r.owner.clone(),reviewer_id:r.owner.clone(),expected_revision:r.expected_revision,proposal_key:r.request_key.clone(),payload_hash:request_hash.clone(),approved_paths:paths};
    let decision=crate::field_authority::decide_fields(&r.owner,&before,&after,r.expected_revision,r.expected_revision,&provenance,&r.request_key,&request_hash,None,Some(&review),None,None).map_err(RepositoryError::Validation)?;
    if decision.outcome!="allowed" {return Err(unavailable());}
    if current["importance"]["origin"]==r.importance["origin"] && current["importance"]["value"]==r.importance["value"] {return Err(unavailable());}
    let root_receipt_id:String=tx.query_row("SELECT change_set_id FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=4",params![r.owner,r.candidate_id],|row|row.get(0))?;
    let root_sha256:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,root_receipt_id],|row|row.get(0))?;
    if !valid_sha256(&root_sha256) {return Err(unavailable());}
    let selector_sha256=importance_selector(&r.account_id,&r.workspace_id,&r.block_id,&r.request_key)?;
    Ok(ImportanceReviewValidation{root_receipt_id,root_sha256,selector_sha256,request_sha256:request_hash,current,accepted_target,decision,origin})
}

/// Apply a named-Human score change in the caller's existing guarded transaction.
/// Inferred replacements remain unavailable until an S08 proposal binds the
/// exact target and score purpose; summary-input selection is insufficient.
pub(crate) fn apply_importance_review(tx:&Transaction<'_>,r:&ImportanceReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.version!=1 || view.current_owner()!=r.owner || view.account_id()!=r.account_id || r.expected_revision<1 || r.reason.is_empty() || r.reason.trim()!=r.reason || r.reason.len()>500 || r.reason.chars().any(char::is_control) || !valid_sha256(&r.expected_metadata_sha256) || !valid_sha256(&r.predecessor_sha256) {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    if crate::store::sqlite::s02_importance_terminal::probe(tx,&r.owner,&r.account_id,&r.workspace_id,&r.block_id,&r.request_key,view,budget).map_err(|_|unavailable())? {return Err(unavailable());}
    let id=importance_receipt_id(&r.account_id,&r.candidate_id,&r.request_key)?;
    let current=current_importance_context(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
    let request_hash=sha256_hex(&serde_json::to_vec(r)?);
    let replay:Option<(Option<String>,String)>=tx.query_row("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s02.importance-review.v1'",params![r.owner,id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
    if let Some((raw,hash))=replay {
        let raw=raw.ok_or_else(unavailable)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        if sha256_hex(raw.as_bytes())!=hash {return Err(unavailable());}
        let stored:ImportanceReviewReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if stored.request_sha256!=request_hash || stored.request.expected_revision!=r.expected_revision || stored.revision!=r.expected_revision.checked_add(1).ok_or_else(unavailable)? || current["revision"].as_i64().is_none_or(|v|v<stored.revision) {return Err(unavailable());}
        let link:Option<String>=tx.query_row("SELECT source_event_id FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![r.owner,r.block_id,stored.revision],|row|row.get(0)).optional()?.flatten();
        if link.as_deref()!=Some(id.as_str()) {return Err(unavailable());}
        return Ok(json!({"version":1,"state":"already_applied","revision":stored.revision}));
    }
    let verified=validate_importance_review(tx,r,view,budget)?;
    let ImportanceReviewValidation{current,accepted_target,decision,origin,..}=verified;
    let origin=origin.as_str();
    let (identity,w,p,head)=read_knowledge_identity(tx,&r.owner,&r.block_id)?;
    if w!=r.workspace_id || !p.is_empty() || head!=r.expected_revision {return Err(unavailable());}
    let prior=read_knowledge(tx,&r.owner,&identity,head,&w,&p)?;
    let next=head.checked_add(1).ok_or_else(unavailable)?;if next>65 {return Err(unavailable());}
    let change=format!("cs_s02importance_revision_{}",sha256_hex(&serde_json::to_vec(&("s02.importance-revision.v1",&r.account_id,&r.candidate_id,&r.request_key))?));
    let now=chrono::Utc::now().to_rfc3339();let actor=RevisionActor{actor_type:"user".into(),actor_id:r.owner.clone(),reason:r.reason.clone(),source_event_id:Some(id.clone())};
    let operation=serde_json::to_string(r)?;
    insert_guarded_change_set(tx,&r.owner,&change,&actor,&operation,&request_hash,&serde_json::to_value(&decision)?,&now)?;
    insert_knowledge_revision(tx,&r.owner,&identity,next,Some(head),&prior.payload,&actor,&change,&now)?;
    let authority_count:i64=tx.query_row("SELECT count(*) FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![r.owner,r.block_id,head],|row|row.get(0))?;
    if !(1..=256).contains(&authority_count) {return Err(unavailable());}
    tx.execute("INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id,created_at) SELECT owner_id,block_id,?1,field_path,origin,'user',?2,approved_by,?3,evidence_json,?4,?5 FROM fm_v2_revision_field_authority WHERE owner_id=?2 AND block_id=?6 AND revision=?7 AND field_path<>'/metadata/importance_score'",params![next,r.owner,id,change,now,r.block_id,head])?;
    let evidence=serde_json::to_string(&prior.payload.evidence_ids)?;
    let approved=if origin=="manual" {Some(r.owner.as_str())} else {None};
    tx.execute("INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id,created_at) VALUES(?,?,?,'/metadata/importance_score',?,'user',?,?,?,?,?,?)",params![r.owner,r.block_id,next,origin,r.owner,approved,id,evidence,change,now])?;
    let previous_metadata=value_metadata_carry(tx,&r.owner,&r.block_id,head)?.ok_or_else(unavailable)?;
    if previous_metadata.hash!=r.expected_metadata_sha256 {return Err(unavailable());}
    let mut next_metadata=crate::field_authority::parse_unique_json(&previous_metadata.json).map_err(RepositoryError::Validation)?;
    next_metadata.as_object_mut().ok_or_else(unavailable)?.insert("importance_score".into(),r.importance.clone());
    crate::ontology_v3::validate_revision_metadata(&next_metadata)?;
    let metadata=serde_json::to_string(&next_metadata)?;let metadata_sha=sha256_hex(metadata.as_bytes());
    tx.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES(?,?,?,'openclank.memory-revision-metadata.v1',?,?,'user',?,?,?,?,?)",params![r.owner,r.block_id,next,metadata,metadata_sha,r.owner,change,id,evidence,now])?;
    if tx.execute("UPDATE fm_v2_knowledge_blocks SET current_revision=?1 WHERE owner_id=?2 AND block_id=?3 AND current_revision=?4",params![next,r.owner,r.block_id,head])?!=1 {return Err(unavailable());}
    let old=current["importance"]["value"].as_f64().ok_or_else(unavailable)?;let score=r.importance["value"].as_f64().ok_or_else(unavailable)?;
    if tx.execute("UPDATE curated SET importance_score=?1 WHERE owner=?2 AND workspace_id=?3 AND id=?4 AND source='s08_policy' AND archived=0 AND importance_score=?5",params![score,r.owner,r.workspace_id,r.curated_id,old])?!=1 {return Err(unavailable());}
    insert_outbox(tx,&r.owner,&change,"knowledge_revision",&json!({"block_id":r.block_id,"revision":next}),next,&now)?;
    let receipt=ImportanceReviewReceipt{version:"s02.importance-review.v1".into(),request:r.clone(),request_sha256:request_hash,revision:next,metadata_sha256:metadata_sha,knowledge_change_set_id:change,knowledge_content_sha256:prior.content_hash};
    let raw=serde_json::to_string(&receipt)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s02.importance-review.v1',NULL,?,?,?,?)",params![r.owner,id,r.owner,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?,?,?,'s02_importance_review',?,?,?)",params![r.owner,format!("outbox_{id}_s02_importance_review"),id,raw,next,now])?;
    if let Some((proposal,_fact))=accepted_target {
        let before=read_candidate(tx,&r.owner,&proposal,&proposal,3,&r.workspace_id,"")?;
        if before.payload.state!=CandidateState::AwaitingReview {return Err(unavailable());}
        let mut accepted=before.payload;accepted.state=CandidateState::Activated;accepted.accepted_block_id=Some(r.block_id.clone());
        insert_candidate_revision(tx,&r.owner,&proposal,4,Some(3),&accepted,&actor,&id,&now)?;
        if tx.execute("UPDATE fm_v2_candidates SET current_revision=4 WHERE owner_id=?1 AND candidate_id=?2 AND current_revision=3",params![r.owner,proposal])?!=1 || tx.execute("UPDATE candidates SET status='accepted',reason=?1,accepted_curated_id=?2,updated_at=?3 WHERE owner=?4 AND workspace_id=?5 AND id=?6 AND status='pending' AND accepted_curated_id IS NULL",params![r.reason,r.curated_id,now,r.owner,r.workspace_id,proposal])?!=1 {return Err(unavailable());}
        insert_outbox(tx,&r.owner,&id,"candidate_revision",&json!({"candidate_id":proposal,"revision":4,"accepted_block_id":r.block_id}),4,&now)?;
    }
    let verified=current_importance_context(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
    if verified["revision"]!=next || verified["predecessor_receipt_id"]!=id {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    Ok(json!({"version":1,"state":"applied","revision":next}))
}

pub(crate) fn summary_original(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,digest:Option<&str>,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let status:Option<String>=tx.query_row("SELECT status FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|x|x.get(0)).optional()?;
    let result=match status.as_deref() {
        Some("pending")=>{let fact=s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;if fact.intent.output_kind!="summary"{return Err(unavailable());}json!({"state":"absent"})},
        Some("accepted")=>{let receipt=summary_accepted_root(tx,owner,account,workspace,candidate,view,budget)?;json!({"state":"found","original":{"candidate_id":candidate,"candidate_revision":receipt["candidate_revision"],"candidate_sha256":receipt["candidate_sha256"],"request_key":receipt["request_key"],"reason":receipt["reason"],"action":"accept"},"receipt":{"state":"applied","candidate_id":candidate,"block_id":receipt["block_id"],"revision":1,"visibility":"not_rechecked"}})},
        Some("rejected")=>super::s08_policy_review::rejected_original(tx,owner,account,workspace,candidate,"summary",view,budget)?,
        None=>{let digest=digest.filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;if s08_policy_lifecycle::candidate_erased(tx,owner,account,workspace,candidate,digest,view,budget)? {json!({"state":"terminal_erased","candidate_id":candidate})} else {return Err(unavailable());}},
        _=>return Err(unavailable()),
    };require_policy_projection(tx,account,workspace,Some(policy))?;Ok(result)
}

fn reject_id(account:&str,candidate:&str,key:&str)->Result<String,RepositoryError> {
    Ok(format!("cs_s08policy_reject_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-reject.v1",account,candidate,key))?)))
}
/// Content-free original selector; contributor bodies and capture eligibility
/// are deliberately not needed to reject an already settled proposal.
pub(crate) fn reject_preview(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,output_kind:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let fact=s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;
    if !matches!(output_kind,"summary"|"connection"|"journal_synthesis") || fact.intent.output_kind!=output_kind {return Err(unavailable());}
    require_policy_projection(tx,account,workspace,Some(policy))?;
    Ok(json!({"version":1,"candidate_id":candidate,"candidate_revision":3,"candidate_sha256":sha256_hex(fact.content.as_bytes())}))
}
pub(crate) fn rejected_root(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,output_kind:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    let fact=s08_policy_lifecycle::settled_candidate_rejected(tx,owner,account,workspace,candidate,view,budget)?;
    if !matches!(output_kind,"summary"|"connection"|"journal_synthesis") || fact.intent.output_kind!=output_kind {return Err(unavailable());}
    let sizes:Vec<i64>={let mut q=tx.prepare("SELECT length(CAST(proposal_json AS BLOB)) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision IN (3,4) ORDER BY revision")?;let rows=q.query_map(params![owner,candidate],|x|x.get::<_,i64>(0))?.collect::<Result<Vec<_>,_>>()?;rows};
    if sizes.len()!=2 || sizes.iter().any(|n|!(1..=65536).contains(n)){return Err(unavailable());}for n in sizes {budget.check(n as usize).map_err(RepositoryError::Validation)?;}
    let before=read_candidate_inner(tx,owner,candidate,candidate,3,workspace,"",false)?;let after=read_candidate_inner(tx,owner,candidate,candidate,4,workspace,"",false)?;
    let mut expected=before.payload.clone();expected.state=CandidateState::Rejected;
    if before.payload.state!=CandidateState::AwaitingReview || before.content_hash!=hash_value(&serde_json::to_value(&before.payload)?)? || after.payload!=expected || after.previous_revision!=Some(3) || after.actor_type!="user" || after.source_event_id.as_deref()!=Some(fact.attempt_id.as_str()) || after.content_hash!=hash_value(&serde_json::to_value(&after.payload)?)? {return Err(unavailable());}view.for_captured(tx,&after.actor_id)?;
    let id=after.change_set_id.clone();let len:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,id],|x|x.get(0)).optional()?;let len=len.ok_or_else(unavailable)?;if !(1..=16384).contains(&len){return Err(unavailable());}budget.check(len as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,actor,actor_id,reason):(String,String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND contract_version=?3",params![owner,id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?)))?;
    let receipt=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let key=receipt["request_key"].as_str().filter(|x|!x.is_empty() && x.trim()==*x && x.len()<=512).ok_or_else(unavailable)?;let why=receipt["reason"].as_str().filter(|x|!x.is_empty() && x.trim()==*x && x.len()<=500).ok_or_else(unavailable)?;
    if receipt.as_object().map(|v|v.len())!=Some(10) || receipt["version"]!="s08.policy-reject.v1" || receipt["owner"]!=after.actor_id || receipt["account_id"]!=account || receipt["workspace_id"]!=workspace || receipt["candidate_id"]!=candidate || receipt["candidate_revision"]!=3 || receipt["candidate_sha256"]!=sha256_hex(fact.content.as_bytes()) || receipt["action"]!="reject" || id!=reject_id(account,candidate,key)? || hash!=sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=after.actor_id || reason!="s08.policy-reject.v1" {return Err(unavailable());}
    for (kind,revision,expected) in [("s08_policy_reject",1,receipt.clone()),("candidate_revision",4,json!({"candidate_id":candidate,"revision":4,"accepted_block_id":null}))] {
        let event=format!("outbox_{id}_{kind}");let size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event],|x|x.get(0)).optional()?;let size=size.ok_or_else(unavailable)?;if !(1..=16384).contains(&size){return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
        let (raw,rev):(String,i64)=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,kind],|x|Ok((x.get(0)?,x.get(1)?))).optional()?.ok_or_else(unavailable)?;if rev!=revision || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=expected{return Err(unavailable());}
    }
    let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND status='rejected' AND accepted_curated_id IS NULL AND reason=?4)",params![owner,workspace,candidate,why],|x|x.get(0))?;if !linked{return Err(unavailable());}Ok(receipt)
}
pub(crate) fn rejected_original(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,output_kind:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    let receipt=rejected_root(tx,owner,account,workspace,candidate,output_kind,view,budget)?;
    Ok(json!({"state":"found","original":{"candidate_id":candidate,"candidate_revision":3,"candidate_sha256":receipt["candidate_sha256"],"request_key":receipt["request_key"],"reason":receipt["reason"],"action":"reject"},"receipt":{"state":"rejected","candidate_id":candidate,"visibility":"not_rechecked"}}))
}
pub(crate) fn reject_apply(tx:&Transaction<'_>,r:&ConnectionReviewRequest,output_kind:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.action!="reject" || !matches!(output_kind,"summary"|"connection"|"journal_synthesis") {return Err(unavailable());}key(r)?;require_guarded_epoch(tx)?;
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    let status:Option<String>=tx.query_row("SELECT status FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![r.owner,r.workspace_id,r.candidate_id],|x|x.get(0)).optional()?;
    match status.as_deref() {
        Some("rejected")=>{let found=rejected_root(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,output_kind,view,budget)?;if found["request_key"]!=r.request_key || found["candidate_sha256"]!=r.candidate_sha256 || found["reason"]!=r.reason {return Err(unavailable());}return Ok(json!({"state":"rejected","candidate_id":r.candidate_id,"visibility":"not_rechecked"}));},
        // A missing candidate never proves this exact rejection was issued.
        // The separate original-status path may report candidate erasure.
        None=>return Err(unavailable()),
        Some("pending")=>{},_=>return Err(unavailable()),
    }
    let fact=s08_policy_lifecycle::settled_candidate(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
    if fact.intent.output_kind!=output_kind || sha256_hex(fact.content.as_bytes())!=r.candidate_sha256 {return Err(unavailable());}
    let original=read_candidate_inner(tx,&r.owner,&r.candidate_id,&r.candidate_id,3,&r.workspace_id,"",false)?;if original.payload.state!=CandidateState::AwaitingReview {return Err(unavailable());}
    let receipt_id=reject_id(&r.account_id,&r.candidate_id,&r.request_key)?;let receipt=json!({"version":"s08.policy-reject.v1","owner":r.owner,"account_id":r.account_id,"workspace_id":r.workspace_id,"candidate_id":r.candidate_id,"candidate_revision":3,"candidate_sha256":r.candidate_sha256,"request_key":r.request_key,"reason":r.reason,"action":"reject"});let raw=serde_json::to_string(&receipt)?;let now=chrono::Utc::now().to_rfc3339();
    let actor=RevisionActor{actor_type:"user".into(),actor_id:r.owner.clone(),reason:r.reason.clone(),source_event_id:Some(fact.attempt_id.clone())};
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s08.policy-reject.v1',NULL,?,?,?,?)",params![r.owner,receipt_id,r.owner,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    let mut rejected=original.payload;rejected.state=CandidateState::Rejected;insert_candidate_revision(tx,&r.owner,&r.candidate_id,4,Some(3),&rejected,&actor,&receipt_id,&now)?;
    if tx.execute("UPDATE fm_v2_candidates SET current_revision=4 WHERE owner_id=? AND candidate_id=? AND current_revision=3",params![r.owner,r.candidate_id])?!=1 || tx.execute("UPDATE candidates SET status='rejected',reason=?,updated_at=? WHERE owner=? AND workspace_id=? AND id=? AND status='pending' AND accepted_curated_id IS NULL",params![r.reason,now,r.owner,r.workspace_id,r.candidate_id])?!=1 {return Err(unavailable());}
    insert_outbox(tx,&r.owner,&receipt_id,"candidate_revision",&json!({"candidate_id":r.candidate_id,"revision":4,"accepted_block_id":null}),4,&now)?;insert_outbox(tx,&r.owner,&receipt_id,"s08_policy_reject",&receipt,1,&now)?;
    Ok(json!({"state":"rejected","candidate_id":r.candidate_id,"visibility":"current"}))
}

/// Explainable lineage of an ACCEPTED summary, read from what `accept_summary` already stored (candidate proposal, the stored selection,
/// the review receipt and the block's evidence links); nothing is written. None when any part is not there.
pub(crate) fn summary_lineage(conn:&Connection,owner:&str,workspace:&str,candidate:&str)->Option<Value> {
    let (job,content):(String,String)=conn.query_row("SELECT turn_id,content FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND status='accepted'",params![owner,workspace,candidate],|x|Ok((x.get(0)?,x.get(1)?))).optional().ok()??;
    let proposal:Value=serde_json::from_str(&content).ok()?;if proposal["version"]!="s08.policy-summary.v1" {return None;}
    let input_hash:String=conn.query_row("SELECT input_hash FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND workspace_key=?3",params![owner,job,workspace],|x|x.get(0)).optional().ok()??;
    let selection:Option<String>=conn.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND reason='s08.selection.v1' AND content_hash=?2",params![owner,input_hash],|x|x.get(0)).optional().ok()?;
    let selection:Value=serde_json::from_str(&selection?).ok()?;
    let sources:Vec<Value>=selection["candidates"].as_array()?.iter().map(|c|json!({"block_id":c["block_id"],"revision":c["revision"],"value_sha256":c["value_sha256"]})).collect();
    let receipt:Option<String>=conn.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND reason='s08.policy-review.v1' AND json_extract(operation_metadata_json,'$.candidate_id')=?2 AND json_extract(operation_metadata_json,'$.action')='accept'",params![owner,candidate],|x|x.get(0)).optional().ok()?;
    let receipt:Value=serde_json::from_str(&receipt?).ok()?;let block=receipt["block_id"].as_str()?;
    let evidence:Vec<String>={let mut q=conn.prepare("SELECT evidence_id FROM fm_v2_revision_evidence WHERE owner_id=?1 AND block_id=?2 AND revision=1 ORDER BY evidence_id LIMIT 65").ok()?;let rows=q.query_map(params![owner,block],|x|x.get::<_,String>(0)).ok()?.collect::<Result<Vec<_>,_>>().ok()?;rows};
    Some(json!({"kind":"summary","reviewer":receipt["owner"],"producer":{"actor":"s08_policy","job_id":job},"summary":{"source_block_ids":proposal["source_block_ids"],"sources":sources,"importance":proposal["importance"]},"evidence_ids":evidence,"block_id":block}))
}

/// Exact current, Human-accepted S08 summary reference for prepared discovery.
/// The curated metadata is only a locator; the held transaction proves the
/// S08 root, account custody, current head, typed value and score chain.
pub(crate) fn prepared_s08_summary_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,curated:&str,content:&str,deadline:std::time::Instant)->Result<FoundMemoryRef,RepositoryError> {
    if std::time::Instant::now()>=deadline || tx.is_autocommit() || [owner,account,workspace,curated].iter().any(|v|v.is_empty() || v.trim()!=*v || v.len()>512 || v.chars().any(char::is_control)) || content.is_empty() || content.len()>16384 {return Err(unavailable())}
    // The caller owns the connection's 250ms progress handler and length
    // limit. A detached counter never replaces either outer guard.
    let mut budget=ResetFenceBudget::detached();budget.check(content.len()).map_err(RepositoryError::Validation)?;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|s08_bounded_first(r,0)).optional()?.ok_or_else(unavailable)?;
    let view=owner_custody::verified_or_direct_custody_view(tx,&database,account,owner,owner,&mut budget)?;
    let meta:Option<(Option<String>,Option<String>)>=tx.query_row("SELECT CASE WHEN length(CAST(metadata AS BLOB))<=16384 THEN metadata END,CASE WHEN length(CAST(content AS BLOB))<=16384 THEN content END FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='s08_policy' AND archived=0",params![owner,workspace,curated],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let (metadata,stored)=meta.ok_or_else(unavailable)?;let metadata=metadata.ok_or_else(unavailable)?;let stored=stored.ok_or_else(unavailable)?;
    budget.check(metadata.len()+stored.len()).map_err(RepositoryError::Validation)?;if stored!=content {return Err(unavailable())}
    let locator=crate::field_authority::parse_unique_json(&metadata).map_err(RepositoryError::Validation)?;
    let candidate=locator.get("candidate_id").and_then(Value::as_str).filter(|v|!v.is_empty() && v.trim()==*v && v.len()<=512 && !v.chars().any(char::is_control)).ok_or_else(unavailable)?;
    if locator.get("manually_reviewed")!=Some(&json!(true)) {return Err(unavailable())}
    let root=summary_accepted_root(tx,owner,account,workspace,candidate,&view,&mut budget)?;
    let verified=VerifiedSummaryRoot{owner,account,workspace,candidate,value:root};
    let root=&verified.value;
    if root["curated_id"]!=curated || root["candidate_id"]!=candidate || root["candidate_sha256"]!=sha256_hex(content.as_bytes()) {return Err(unavailable())}
    let block=root["block_id"].as_str().ok_or_else(unavailable)?;
    let (identity,w,p,head)=read_knowledge_identity(tx,owner,block)?;
    if w!=workspace || !p.is_empty() || identity.claim_slot!=candidate || identity.predicate!="s08.composite" || head<1{return Err(unavailable())}
    let row:Option<(Option<String>,Option<String>,bool)>=tx.query_row("SELECT CASE WHEN length(CAST(value_json AS BLOB))<=16384 THEN value_json END, (SELECT CASE WHEN length(CAST(metadata_json AS BLOB))<=16384 THEN metadata_json END FROM fm_v2_revision_metadata m WHERE m.owner_id=r.owner_id AND m.block_id=r.block_id AND m.revision=r.revision), EXISTS(SELECT 1 FROM fm_v2_revision_metadata m WHERE m.owner_id=r.owner_id AND m.block_id=r.block_id AND m.revision=r.revision) FROM fm_v2_knowledge_revisions r WHERE r.owner_id=?1 AND r.block_id=?2 AND r.revision=?3",params![owner,block,head],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let (raw,metadata,metadata_exists)=row.ok_or_else(unavailable)?;if metadata_exists && metadata.is_none(){return Err(unavailable())}let raw=raw.ok_or_else(unavailable)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let typed=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let original=crate::field_authority::parse_unique_json(content).map_err(RepositoryError::Validation)?;
    if typed!=json!({"type":"object","value":original}) {return Err(unavailable())}
    let score=if let Some(raw)=metadata {budget.check(raw.len()).map_err(RepositoryError::Validation)?;let v=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;v.as_object().ok_or_else(unavailable)?.contains_key("importance_score")} else {false};
    if score {
        let context=importance_chain_facts_in(tx,owner,account,workspace,candidate,&view,&mut budget,Some(&verified),S08HistoricalRead::Ordinary)?.0;
        if context["block_id"]!=block || context["curated_id"]!=curated || context["revision"]!=head{return Err(unavailable())}
    } else if head!=1 {return Err(unavailable())}
    if std::time::Instant::now()>=deadline {return Err(unavailable())}
    Ok(FoundMemoryRef{version:1,provider:"fm-v2".into(),owner:owner.into(),workspace_id:workspace.into(),project_id:None,block_id:block.into(),revision:head,value_sha256:sha256_hex(raw.as_bytes())})
}
