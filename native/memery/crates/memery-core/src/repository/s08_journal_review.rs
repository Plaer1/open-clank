//! BQ-8: review of a settled `journal_synthesis` proposal in the EXISTING S08 review lane. The receipt is the existing
//! `s08.policy-review.v1` (fixed 15 keys, unchanged) / `s08.policy-reject.v1`; these functions are derived from the summary ones and
//! differ ONLY where the producer differs (actor `s08_journal`, container label, evidence = journal evidence, producer provenance kept
//! in the activation rationale beside the approving owner). `accept_summary` and its readers are not edited.
use super::*;
use super::s02_connection::ConnectionReviewRequest;
use super::s08_policy_review::key;
fn unavailable()->RepositoryError {RepositoryError::NotFound}
/// Producer provenance, kept separate from the approving owner: the producing actor, job, role and the transform that ran.
fn producer(fact:&s08_policy_lifecycle::SettledPolicyCandidate)->Value {
    json!({"actor":s08_journal::ACTOR,"job_id":fact.job_id,"role":s08_journal::OUTPUT_KIND,"mode":fact.intent.mode,"model_route_id":fact.intent.model_route_id,"assignment_sha256":fact.intent.assignment_sha256})
}
/// Explainable lineage kept on the curated record the readers surface (`explain`): what was cited, who produced it, who approved it.
fn lineage(fact:&s08_policy_lifecycle::SettledPolicyCandidate,reviewer:&str,proposal:&Value)->Value {
    json!({"kind":s08_journal::OUTPUT_KIND,"reviewer":reviewer,"producer":producer(fact),"journal":proposal["journal"]})
}
/// The stored selection must still be exactly what the Host described and its evidence rows intact.
fn current_journal_selection(tx:&Transaction<'_>,owner:&str,workspace:&str,fact:&s08_policy_lifecycle::SettledPolicyCandidate,_budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let selection=&fact.intent.selection;
    if fact.intent.output_kind!=s08_journal::OUTPUT_KIND || selection.project_id.is_some() || selection.workspace_id!=workspace {return Err(unavailable());}
    let predicate:Option<(String,String,i64,i64)>=tx.query_row("SELECT value_type,cardinality,version,allow_unknown FROM fm_v2_predicates WHERE predicate='s08.composite'",[],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?;
    if predicate!=Some(("object".into(),"single".into(),1,0)) {return Err(unavailable());}
    let journal=selection.journal.as_ref().ok_or_else(unavailable)?;journal.validate(selection)?;
    let input=sha256_hex(&serde_json::to_vec(selection)?);
    V2Repository::verify_s08_evidence(tx,owner,selection,&fact.job_id,&input)
}
/// Preview: the proposed text, the cited archive part selectors from the stored selection (never source text), the producer
/// provenance, and the digest (`candidate_sha256`) the apply must echo.
pub(crate) fn journal_manifest(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let fact=s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;current_journal_selection(tx,owner,workspace,&fact,budget)?;
    let journal=fact.intent.selection.journal.as_ref().ok_or_else(unavailable)?;
    let stored=crate::field_authority::parse_unique_json(&fact.content).map_err(RepositoryError::Validation)?;
    let text=stored["content"].as_str().ok_or_else(unavailable)?;
    let rows:Vec<Value>=journal.rows.iter().filter(|r|r.class=="included").map(|r|json!({"seq":r.seq,"actor_id":r.actor_id,"message_id":r.message_id,"part_id":r.part_id,"revision":r.revision,"content_hash":r.content_hash,"selector":r.selector})).collect();
    let result=json!({"version":1,"candidate_id":candidate,"candidate_revision":3,"candidate_sha256":sha256_hex(fact.content.as_bytes()),"output_kind":s08_journal::OUTPUT_KIND,"content":text,
        "journal":{"chat_id":journal.chat_id,"selection_sha256":journal.selection_sha256,"page_sha256":journal.page_sha256,"prior_cursor":journal.prior_cursor,"end_seq":journal.end_seq,"high_seq":journal.high_seq,"cited":rows},
        "producer":{"actor":s08_journal::ACTOR,"job_id":fact.job_id,"attempt_id":fact.attempt_id,"mode":fact.intent.mode,"model_route_id":fact.intent.model_route_id,"assignment_sha256":fact.intent.assignment_sha256}});
    budget.check(serde_json::to_vec(&result)?.len()).map_err(RepositoryError::Validation)?;require_policy_projection(tx,account,workspace,Some(policy))?;Ok(result)
}
/// Exact historical receipt read for a journal review; absence is not permission to reissue.
pub(crate) fn journal_status(tx:&Transaction<'_>,r:&ConnectionReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    let id=key(r)?;require_guarded_epoch(tx)?;if view.current_owner()!=r.owner || view.account_id()!=r.account_id {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    let len:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,id],|x|x.get(0)).optional()?;
    let Some(len)=len else {
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2)",params![r.owner,format!("outbox_{id}_s08_policy_review")],|x|x.get(0))?;if orphan{return Err(unavailable());}
        let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3)",params![r.owner,r.workspace_id,r.candidate_id],|x|x.get(0))?;
        if !present {if s08_policy_lifecycle::candidate_erased(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,&r.candidate_sha256,view,budget)? {return Ok(json!({"state":"terminal_erased","candidate_id":r.candidate_id}));}return Err(unavailable());}
        let fact=s08_policy_lifecycle::settled_candidate(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
        if fact.intent.output_kind!=s08_journal::OUTPUT_KIND || sha256_hex(fact.content.as_bytes())!=r.candidate_sha256 {return Err(unavailable());}return Ok(json!({"state":"absent"}));
    };
    if !(1..=16384).contains(&len){return Err(unavailable());}budget.check(len as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,actor,actor_id,reason):(String,String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND contract_version=?3",params![r.owner,id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?)))?;
    let v=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let captured=v["owner"].as_str().ok_or_else(unavailable)?;view.for_captured(tx,captured)?;
    if v.as_object().map(|x|x.len())!=Some(15) || v["version"]!="s08.policy-review.v1" || v["owner"]!=actor_id || actor!="user" || reason!="s08.policy-review.v1" || v["account_id"]!=r.account_id || v["workspace_id"]!=r.workspace_id || v["candidate_id"]!=r.candidate_id || v["candidate_revision"]!=3 || v["candidate_sha256"]!=r.candidate_sha256 || v["request_key"]!=r.request_key || v["reason"]!=r.reason || v["action"]!="accept" || r.action!="accept" || v["block_revision"]!=1 || !v["block_id"].is_string() || !v["curated_id"].is_string() || !v["block_change_set_id"].is_string() || !v["block_content_hash"].as_str().is_some_and(valid_sha256) || hash!=sha256_hex(raw.as_bytes()) {return Err(unavailable());}
    let event_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![r.owner,format!("outbox_{id}_s08_policy_review")],|x|x.get(0)).optional()?;let event_len=event_len.ok_or_else(unavailable)?;if !(1..=16384).contains(&event_len){return Err(unavailable());}budget.check(event_len as usize).map_err(RepositoryError::Validation)?;
    let event:Option<String>=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s08_policy_review' AND source_revision=1",params![r.owner,format!("outbox_{id}_s08_policy_review"),id],|x|x.get(0)).optional()?;if event.as_deref()!=Some(raw.as_str()) {return Err(unavailable());}
    if journal_accepted_root(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?!=v {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;Ok(json!({"state":"applied","candidate_id":r.candidate_id,"block_id":v["block_id"],"revision":1,"visibility":"not_rechecked"}))
}
pub(crate) fn accept_journal(tx:&Transaction<'_>,r:&ConnectionReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.action!="accept" {return Err(unavailable());}
    let receipt_id=key(r)?;let prior=journal_status(tx,r,view,budget)?;if matches!(prior["state"].as_str(),Some("applied"|"terminal_erased")){return Ok(prior)}
    let fact=s08_policy_lifecycle::settled_candidate(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;current_journal_selection(tx,&r.owner,&r.workspace_id,&fact,budget)?;
    if sha256_hex(fact.content.as_bytes())!=r.candidate_sha256 {return Err(unavailable());}
    let candidate=read_candidate(tx,&r.owner,&r.candidate_id,&r.candidate_id,3,&r.workspace_id,"")?;if candidate.payload.state!=CandidateState::AwaitingReview {return Err(unavailable());}
    let block=format!("s08policyblock_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let curated=format!("s08policycurated_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let subject=format!("s08policyentity_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let occupied:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1) OR EXISTS(SELECT 1 FROM fm_v2_entities WHERE owner_id=?2 AND entity_id=?3) OR EXISTS(SELECT 1 FROM memory_quarantine WHERE (tier='candidate' AND original_id=?4) OR (tier='curated' AND original_id=?1))",params![curated,r.owner,subject,r.candidate_id],|x|x.get(0))?;if occupied{return Err(unavailable());}
    let entity_actor=RevisionActor{actor_type:"automation".into(),actor_id:s08_journal::ACTOR.into(),reason:"inferred journal synthesis container".into(),source_event_id:Some(fact.attempt_id.clone())};
    let entity=EntityPayload{entity_type:"concept".into(),canonical_label:"journal synthesis".into(),aliases:vec![],roles:vec![],tags:vec![],status:"active".into()};let now=chrono::Utc::now().to_rfc3339();
    let entity_change=insert_change_set(tx,&r.owner,&entity_actor,&serde_json::to_value(&entity)?,&now)?;
    tx.execute("INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,'',?,0,?)",params![r.owner,subject,r.workspace_id,entity_change,now])?;
    insert_entity_revision(tx,&r.owner,&subject,1,None,&entity,&entity_actor,&entity_change,&now)?;
    if tx.execute("UPDATE fm_v2_entities SET current_revision=1 WHERE owner_id=? AND entity_id=? AND current_revision=0",params![r.owner,subject])?!=1 {return Err(unavailable());}
    insert_outbox(tx,&r.owner,&entity_change,"entity_revision",&json!({"entity_id":subject,"revision":1}),1,&now)?;
    let actor=RevisionActor{actor_type:"user".into(),actor_id:r.owner.clone(),reason:r.reason.clone(),source_event_id:Some(fact.attempt_id.clone())};
    let identity=KnowledgeIdentity{block_id:block.clone(),subject_entity_id:subject,predicate:"s08.composite".into(),claim_slot:r.candidate_id.clone(),cardinality:PredicateCardinality::Single};
    let proposal=crate::field_authority::parse_unique_json(&fact.content).map_err(RepositoryError::Validation)?;
    let payload=json!({"value":{"type":"object","value":proposal},"expected_value_type":"object","kind":"fact","status":"active","tags":[],"confidence":0.0,"trust":0.0,"activation":"review_approved","activation_rationale":{"classification":"inferred_untrusted","reviewer":r.owner,"producer":producer(&fact)},"evidence_ids":candidate.payload.evidence_ids});
    let paths:Vec<String>=["/value","/expected_value_type","/kind","/tags","/status","/confidence","/trust","/activation","/activation_rationale","/valid_from","/valid_to","/evidence_ids"].iter().map(|x|x.to_string()).collect();
    let operation=json!({"version":"s03.published-create.v1","owner":r.owner,"workspace":r.workspace_id,"project":"","block_id":block,"subject_entity_id":identity.subject_entity_id,"predicate":identity.predicate,"claim_slot":identity.claim_slot,"cardinality":identity.cardinality,"payload":payload,"request_key":r.request_key,"actor_type":"user","actor_id":r.owner,"reason":r.reason,"source_event_id":actor.source_event_id,"approved_paths":paths,"producer":null});let operation_raw=serde_json::to_string(&operation)?;
    let review=crate::field_authority::OwnerReviewContext{authenticated_owner:r.owner.clone(),reviewer_id:r.owner.clone(),expected_revision:0,proposal_key:r.request_key.clone(),payload_hash:sha256_hex(operation_raw.as_bytes()),approved_paths:paths};let scope=ScopeRef{owner_id:r.owner.clone(),workspace_id:Some(r.workspace_id.clone()),project_id:None,session_id:None};
    let (revision,_)=V2Repository::guarded_create_knowledge_in_transaction(tx,&scope,&identity,&actor,&r.request_key,&operation_raw,Some(&review),None,true)?;
    let mut record=crate::record::MemoryRecord::new(&fact.content);record.id=curated.clone();record.kind=crate::record::MemoryKind::Fact;record.source=s08_journal::ACTOR.into();record.source_type=crate::record::SourceType::AutoExtracted;record.owner=Some(r.owner.clone());record.workspace_id=r.workspace_id.clone();record.metadata=json!({"candidate_id":r.candidate_id,"manually_reviewed":true,"lineage":lineage(&fact,&r.owner,&proposal)});crate::store::sqlite::SqliteStore::upsert_curated_inner(tx,&record,None).map_err(RepositoryError::Validation)?;
    let receipt=json!({"version":"s08.policy-review.v1","owner":r.owner,"account_id":r.account_id,"workspace_id":r.workspace_id,"candidate_id":r.candidate_id,"candidate_revision":3,"candidate_sha256":r.candidate_sha256,"request_key":r.request_key,"reason":r.reason,"action":"accept","block_id":block,"block_revision":1,"curated_id":curated,"block_change_set_id":revision.change_set_id,"block_content_hash":revision.content_hash});let raw=serde_json::to_string(&receipt)?;
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s08.policy-review.v1',NULL,?,?,?,?)",params![r.owner,receipt_id,r.owner,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    let mut activated=candidate.payload;activated.state=CandidateState::Activated;activated.accepted_block_id=Some(block.clone());insert_candidate_revision(tx,&r.owner,&r.candidate_id,4,Some(3),&activated,&actor,&receipt_id,&now)?;
    if tx.execute("UPDATE fm_v2_candidates SET current_revision=4 WHERE owner_id=? AND candidate_id=? AND current_revision=3",params![r.owner,r.candidate_id])?!=1 || tx.execute("UPDATE candidates SET status='accepted',reason=?,accepted_curated_id=?,updated_at=? WHERE owner=? AND workspace_id=? AND id=? AND status='pending' AND accepted_curated_id IS NULL",params![r.reason,curated,now,r.owner,r.workspace_id,r.candidate_id])?!=1 {return Err(unavailable());}
    insert_outbox(tx,&r.owner,&receipt_id,"candidate_revision",&json!({"candidate_id":r.candidate_id,"revision":4,"accepted_block_id":block}),4,&now)?;insert_outbox(tx,&r.owner,&receipt_id,"s08_policy_review",&receipt,1,&now)?;
    Ok(json!({"state":"applied","candidate_id":r.candidate_id,"block_id":block,"revision":1,"visibility":"current"}))
}
/// Factual accepted S08 journal-synthesis root; no current source/capture grant.
pub(crate) fn journal_accepted_root(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    let fact=s08_policy_lifecycle::settled_candidate_accepted(tx,owner,account,workspace,candidate,view,budget)?;if fact.intent.output_kind!=s08_journal::OUTPUT_KIND {return Err(unavailable());}
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
    let expected_payload=json!({"value":{"type":"object","value":proposal},"expected_value_type":"object","kind":"fact","status":"active","tags":[],"confidence":0.0,"trust":0.0,"activation":"review_approved","activation_rationale":{"classification":"inferred_untrusted","reviewer":after.actor_id,"producer":producer(&fact)},"valid_from":null,"valid_to":null,"evidence_ids":before.payload.evidence_ids});
    let entity=format!("s08policyentity_{}",sha256_hex(&serde_json::to_vec(&(account,candidate))?));
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=1 WHERE b.owner_id=?1 AND b.workspace_key=?2 AND b.project_key='' AND b.block_id=?3 AND b.subject_entity_id=?13 AND b.predicate='s08.composite' AND b.claim_slot=?4 AND b.cardinality='single' AND r.change_set_id=?5 AND r.content_hash=?6 AND r.actor_type='user' AND r.actor_id=?7 AND r.reason=?8 AND r.source_event_id=?9 AND r.previous_revision IS NULL AND r.value_type='object' AND r.value_json=?10 AND r.evidence_json=?11 AND r.expected_value_type='object' AND r.kind='fact' AND r.status='active' AND r.tags_json='[]' AND r.confidence=0 AND r.trust=0 AND r.activation='review_approved' AND r.activation_rationale=?12 AND r.valid_from IS NULL AND r.valid_to IS NULL)",params![owner,workspace,block,candidate,change,block_hash,after.actor_id,review_reason,fact.attempt_id,serde_json::to_string(&expected_payload["value"])?,serde_json::to_string(&expected_payload["evidence_ids"])?,serde_json::to_string(&expected_payload["activation_rationale"])?,entity],|x|x.get(0))?;
    let operation_len:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,change],|x|x.get(0)).optional()?;let operation_len=operation_len.ok_or_else(unavailable)?;if !(1..=131072).contains(&operation_len){return Err(unavailable());}budget.check(operation_len as usize).map_err(RepositoryError::Validation)?;
    let (operation_raw,operation_hash,operation_actor,operation_actor_id):(String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3 AND source_event_id=?4 AND contract_version=?5",params![owner,change,review_reason,fact.attempt_id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?)))?;
    let mut operation_payload=expected_payload.clone();operation_payload.as_object_mut().ok_or_else(unavailable)?.remove("valid_from");operation_payload.as_object_mut().ok_or_else(unavailable)?.remove("valid_to");
    let paths=json!(["/value","/expected_value_type","/kind","/tags","/status","/confidence","/trust","/activation","/activation_rationale","/valid_from","/valid_to","/evidence_ids"]);
    let entity_payload=json!({"entity_type":"concept","canonical_label":"journal synthesis","aliases":[],"roles":[],"tags":[],"status":"active"});
    let entity_change:Option<String>=tx.query_row("SELECT e.created_change_set_id FROM fm_v2_entities e JOIN fm_v2_entity_revisions r ON r.owner_id=e.owner_id AND r.entity_id=e.entity_id AND r.revision=1 JOIN fm_v2_change_sets c ON c.owner_id=e.owner_id AND c.change_set_id=e.created_change_set_id WHERE e.owner_id=?1 AND e.workspace_key=?2 AND e.project_key='' AND e.entity_id=?3 AND r.change_set_id=e.created_change_set_id AND r.previous_revision IS NULL AND r.payload=?4 AND r.content_hash=?5 AND r.actor_type='automation' AND r.actor_id='s08_journal' AND r.reason='inferred journal synthesis container' AND c.actor_type='automation' AND c.actor_id='s08_journal' AND c.reason='inferred journal synthesis container' AND c.source_event_id=?6 AND c.content_hash=?5 AND c.contract_version=?7",params![owner,workspace,entity,serde_json::to_string(&entity_payload)?,hash_value(&entity_payload)?,fact.attempt_id,CONTRACT_VERSION],|x|x.get(0)).optional()?;
    let entity_change=entity_change.ok_or_else(unavailable)?;
    let entity_event_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,format!("outbox_{entity_change}_entity_revision")],|x|x.get(0)).optional()?;let entity_event_len=entity_event_len.ok_or_else(unavailable)?;if !(1..=16384).contains(&entity_event_len){return Err(unavailable());}budget.check(entity_event_len as usize).map_err(RepositoryError::Validation)?;
    let entity_event:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='entity_revision'",params![owner,format!("outbox_{entity_change}_entity_revision"),entity_change],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;
    if entity_event.as_ref().is_none_or(|(raw,rev)|*rev!=1 || raw.as_ref().is_none_or(|raw|crate::field_authority::parse_unique_json(raw).ok()!=Some(json!({"entity_id":entity,"revision":1})))) {return Err(unavailable());}
    let operation=json!({"version":"s03.published-create.v1","owner":after.actor_id,"workspace":workspace,"project":"","block_id":block,"subject_entity_id":entity,"predicate":"s08.composite","claim_slot":candidate,"cardinality":"single","payload":operation_payload,"request_key":request_key,"actor_type":"user","actor_id":after.actor_id,"reason":review_reason,"source_event_id":fact.attempt_id,"approved_paths":paths,"producer":null});
    // The guarded create stores the operation inside an `s03.receipt.v2` wrapper (operation, decision and their digests); the plain
    // operation is also accepted. Either way the operation must be exactly the one this decision implies.
    let stored=crate::field_authority::parse_unique_json(&operation_raw).map_err(RepositoryError::Validation)?;
    let wrapped_ok=match (stored["operation_json"].as_str(),stored["decision_json"].as_str()) {
        (Some(op),Some(decision))=>stored.as_object().map(|v|v.len())==Some(5) && stored["version"]=="s03.receipt.v2" && stored["operation_sha256"]==sha256_hex(op.as_bytes()) && stored["decision_sha256"]==sha256_hex(decision.as_bytes())
            && crate::field_authority::parse_unique_json(op).ok().as_ref()==Some(&operation) && crate::field_authority::parse_unique_json(decision).ok().is_some_and(|d|d.get("outcome").and_then(Value::as_str)==Some("allowed")),
        _=>false,
    };
    if operation_hash!=sha256_hex(operation_raw.as_bytes()) || operation_actor!="user" || operation_actor_id!=after.actor_id || !(wrapped_ok || stored==operation) {return Err(unavailable());}
    let authority:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND field_path='/value' AND origin='manual' AND producer_type='user' AND producer_id=?3 AND approved_by=?3 AND source_event_id=?4 AND change_set_id=?5)",params![owner,block,after.actor_id,fact.attempt_id,change],|x|x.get(0))?;if !authority {return Err(unavailable());}
    let linked_evidence={let mut q=tx.prepare("SELECT evidence_id FROM fm_v2_revision_evidence WHERE owner_id=?1 AND block_id=?2 AND revision=1 ORDER BY evidence_id LIMIT 65")?;let rows=q.query_map(params![owner,block],|x|x.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;rows};
    let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND status='accepted' AND accepted_curated_id=?4)",params![owner,workspace,candidate,curated],|x|x.get(0))?;
    if !exists || !linked || linked_evidence!=before.payload.evidence_ids || block_hash!=hash_value(&expected_payload)? {return Err(unavailable());}
    Ok(receipt)
}
pub(crate) fn journal_original(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,digest:Option<&str>,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let status:Option<String>=tx.query_row("SELECT status FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|x|x.get(0)).optional()?;
    let result=match status.as_deref() {
        Some("pending")=>{let fact=s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;if fact.intent.output_kind!=s08_journal::OUTPUT_KIND{return Err(unavailable());}json!({"state":"absent"})},
        Some("accepted")=>{let receipt=journal_accepted_root(tx,owner,account,workspace,candidate,view,budget)?;json!({"state":"found","original":{"candidate_id":candidate,"candidate_revision":receipt["candidate_revision"],"candidate_sha256":receipt["candidate_sha256"],"request_key":receipt["request_key"],"reason":receipt["reason"],"action":"accept"},"receipt":{"state":"applied","candidate_id":candidate,"block_id":receipt["block_id"],"revision":1,"visibility":"not_rechecked"}})},
        Some("rejected")=>super::s08_policy_review::rejected_original(tx,owner,account,workspace,candidate,s08_journal::OUTPUT_KIND,view,budget)?,
        None=>{let digest=digest.filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;if s08_policy_lifecycle::candidate_erased(tx,owner,account,workspace,candidate,digest,view,budget)? {json!({"state":"terminal_erased","candidate_id":candidate})} else {return Err(unavailable());}},
        _=>return Err(unavailable()),
    };require_policy_projection(tx,account,workspace,Some(policy))?;Ok(result)
}

