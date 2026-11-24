//! Closed S02 connection proposals. This module grants no publication authority.
use super::*;
use std::collections::BTreeSet;

#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ConnectionEndpoint {pub block_id:String,pub revision:i64}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ConnectionDraft {
    pub version:u32,pub subject:ConnectionEndpoint,pub object:ConnectionEndpoint,
    pub predicate:String,pub evidence_ids:Vec<String>,
}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ConnectionProposal {
    pub version:u32,pub kind:String,pub subject:ConnectionEndpoint,pub object:ConnectionEndpoint,
    pub predicate:String,pub predicate_version:i64,pub predicate_sha256:String,
    pub relation_slot:String,pub evidence_ids:Vec<String>,
}
fn unavailable()->RepositoryError {RepositoryError::NotFound}
fn text(v:&str)->bool {!v.is_empty() && v==v.trim() && v.len()<=512 && !v.chars().any(char::is_control)}
/// The caller owns the authenticated account/scope, exact S05 job and current
/// policy snapshot. This only derives a review proposal, never a write grant.
pub(crate) fn prepare_connection(tx:&Transaction<'_>,owner:&str,workspace:&str,project:&str,job:&str,draft:&ConnectionDraft)->Result<ConnectionProposal,RepositoryError> {
    if draft.version!=1 || ![owner,workspace,job,draft.predicate.as_str(),draft.subject.block_id.as_str(),draft.object.block_id.as_str()].iter().all(|v|text(v))
        || draft.subject.revision<1 || draft.object.revision<1 || draft.subject.block_id==draft.object.block_id || draft.evidence_ids.is_empty() || draft.evidence_ids.len()>64 {return Err(unavailable());}
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s01.knowledge-ref.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(unavailable());}
    let mut unique=BTreeSet::new();for id in &draft.evidence_ids {if !text(id) || !unique.insert(id) {return Err(unavailable());}}
    if draft.evidence_ids.iter().ne(unique.iter().copied()) {return Err(unavailable());}
    for endpoint in [&draft.subject,&draft.object] {
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=?4 WHERE b.owner_id=?1 AND b.block_id=?2 AND b.workspace_key=?3 AND b.current_revision=?4 AND b.project_key=?5 AND r.status='active')",params![owner,endpoint.block_id,workspace,endpoint.revision,project],|r|r.get(0))?;
        if !exists {return Err(unavailable());}
    }
    for evidence in &draft.evidence_ids {
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_evidence e JOIN fm_v2_sources s ON s.owner_id=e.owner_id AND s.source_id=e.source_id AND s.source_revision=e.source_revision WHERE e.owner_id=?1 AND e.evidence_id=?2 AND s.workspace_key=?3 AND s.project_key=?4 AND s.forget_state='active')",params![owner,evidence,workspace,project],|r|r.get(0))?;
        if !exists {return Err(unavailable());}
    }
    let predicate:Option<(String,String,i64,i64)>=tx.query_row("SELECT value_type,cardinality,version,allow_unknown FROM fm_v2_predicates WHERE predicate=?1",params![draft.predicate],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (value_type,cardinality,version,allow_unknown)=predicate.ok_or_else(unavailable)?;
    if value_type!="knowledge_ref" || version<1 || !matches!(cardinality.as_str(),"single"|"set"|"ordered_many"|"time_scoped") || !matches!(allow_unknown,0|1) {return Err(unavailable());}
    let definition=serde_json::json!({"predicate":draft.predicate,"value_type":value_type,"cardinality":cardinality,"version":version,"allow_unknown":allow_unknown});
    let predicate_sha256=sha256_hex(&serde_json::to_vec(&definition)?);
    let relation_slot=format!("s02_connection_{}",sha256_hex(&serde_json::to_vec(&(owner,workspace,project,job,&draft.subject.block_id,draft.subject.revision,&draft.object.block_id,draft.object.revision,&draft.predicate))?));
    Ok(ConnectionProposal{version:1,kind:"s02.connection-proposal.v1".into(),subject:draft.subject.clone(),object:draft.object.clone(),predicate:draft.predicate.clone(),predicate_version:version,predicate_sha256,relation_slot,evidence_ids:draft.evidence_ids.clone()})
}

/// Called only within the original S08 effect transaction. The witness is
/// minted by that owner after current selection, policy and attempt checks.
pub(crate) fn create_connection_candidate(tx:&Transaction<'_>,witness:&VerifiedS08PolicyAttempt<'_,'_>,draft:&ConnectionDraft,importance:Option<f64>,budget:&mut ResetFenceBudget)->Result<String,RepositoryError> {
    witness.revalidate(tx,budget)?;
    if witness.output_kind()!="connection" {return Err(unavailable());}
    let selection=witness.selection();let job=witness.job_id();let attempt=witness.attempt_id();
    let configured=witness.connection_predicate().ok_or_else(unavailable)?;
    if draft.predicate!=configured.predicate {return Err(unavailable());}
    for endpoint in [&draft.subject,&draft.object] {
        if !selection.candidates.iter().any(|v|v.block_id==endpoint.block_id && v.revision==endpoint.revision) {return Err(unavailable());}
    }
    let mut evidence=Vec::new();
    for item in &selection.admissions {evidence.push(format!("s08evidence_{}",sha256_hex(&serde_json::to_vec(&(&selection.owner,job,&item.event_id))?)));}
    evidence.sort();evidence.dedup();
    if draft.evidence_ids.is_empty() || draft.evidence_ids.iter().any(|id|evidence.binary_search(id).is_err()) {return Err(unavailable());}
    let proposal=prepare_connection(tx,&selection.owner,&selection.workspace_id,selection.project_id.as_deref().unwrap_or(""),job,draft)?;
    if proposal.predicate_version!=configured.version || proposal.predicate_sha256!=configured.sha256 {return Err(unavailable());}
    let score=importance.unwrap_or(0.0);if !score.is_finite() || !(0.0..=1.0).contains(&score) {return Err(unavailable());}
    let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(&selection.source_account_id,job))?));
    let content=serde_json::to_string(&proposal)?;if content.len()>65536 {return Err(unavailable());}budget.check(content.len()).map_err(RepositoryError::Validation)?;
    let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_candidates WHERE owner_id=?1 AND candidate_id=?2) OR EXISTS(SELECT 1 FROM candidates WHERE id=?2) OR EXISTS(SELECT 1 FROM memory_quarantine WHERE tier='candidate' AND original_id=?2)",params![selection.owner,candidate],|r|r.get(0))?;
    if collision {return Err(unavailable());}
    witness.revalidate(tx,budget)?;
    let actor=RevisionActor{actor_type:"automation".into(),actor_id:"s08_connection".into(),reason:"unreviewed connection proposal".into(),source_event_id:Some(attempt.into())};
    let mut payload=CandidatePayload{proposal:serde_json::to_value(&proposal)?,state:CandidateState::Proposed,confidence:0.0,importance:score,reason:"unreviewed connection proposal".into(),source_id:None,source_revision:None,accepted_block_id:None,evidence_ids:evidence.clone()};
    let now=chrono::Utc::now().to_rfc3339();let change=insert_change_set(tx,&selection.owner,&actor,&serde_json::to_value(&payload)?,&now)?;
    tx.execute("INSERT INTO fm_v2_candidates(owner_id,candidate_id,workspace_key,project_key,idempotency_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,0,?)",params![selection.owner,candidate,selection.workspace_id,selection.project_id.as_deref().unwrap_or(""),candidate,change,now])?;
    insert_candidate_revision(tx,&selection.owner,&candidate,1,None,&payload,&actor,&change,&now)?;
    payload.state=CandidateState::Validating;insert_candidate_revision(tx,&selection.owner,&candidate,2,Some(1),&payload,&actor,&change,&now)?;
    payload.state=CandidateState::AwaitingReview;insert_candidate_revision(tx,&selection.owner,&candidate,3,Some(2),&payload,&actor,&change,&now)?;
    if tx.execute("UPDATE fm_v2_candidates SET current_revision=3 WHERE owner_id=?1 AND candidate_id=?2 AND current_revision=0",params![selection.owner,candidate])?!=1 {return Err(unavailable());}
    tx.execute("INSERT INTO candidates(id,content,kind,confidence_score,importance_score,owner,workspace_id,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids) VALUES (?1,?2,'fact',0,?3,?4,?5,'',?6,?7,'derived','auto_extracted',?8,?1,'pending','unreviewed connection proposal',?9,?9,?10,1,?11,'[]')",params![candidate,content,score,selection.owner,selection.workspace_id,job,serde_json::to_string(&evidence)?,attempt,now,format!("s08://{job}"),crate::privacy::content_hash(&content)])?;
    insert_outbox(tx,&selection.owner,&change,"candidate_revision",&serde_json::json!({"candidate_id":candidate,"revision":3}),3,&now)?;
    Ok(candidate)
}

/// Bounded registry options; caller supplies existing named-account read
/// authority and profile, and this result grants no write or selection.
pub(crate) fn registered_connection_predicates(tx:&Transaction<'_>,budget:&mut ResetFenceBudget)->Result<Vec<serde_json::Value>,RepositoryError> {
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s01.knowledge-ref.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(unavailable());}
    let mut stmt=tx.prepare("SELECT CASE WHEN length(CAST(predicate AS BLOB))<=512 THEN predicate END,value_type,CASE WHEN length(CAST(cardinality AS BLOB))<=32 THEN cardinality END,version,allow_unknown FROM fm_v2_predicates WHERE value_type='knowledge_ref' ORDER BY predicate LIMIT 129")?;
    let rows=stmt.query_map([],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,String>(1)?,r.get::<_,Option<String>>(2)?,r.get::<_,i64>(3)?,r.get::<_,i64>(4)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>128 {return Err(unavailable());}
    let mut result=Vec::new();for (predicate,value_type,cardinality,version,allow_unknown) in rows {
        let predicate=predicate.ok_or_else(unavailable)?;let cardinality=cardinality.ok_or_else(unavailable)?;
        if !text(&predicate) || version<1 || !matches!(cardinality.as_str(),"single"|"set"|"ordered_many"|"time_scoped") || !matches!(allow_unknown,0|1) {return Err(unavailable());}
        budget.check(predicate.len()+cardinality.len()+64).map_err(RepositoryError::Validation)?;
        let definition=serde_json::json!({"predicate":predicate,"value_type":value_type,"cardinality":cardinality,"version":version,"allow_unknown":allow_unknown});
        result.push(serde_json::json!({"predicate":definition["predicate"],"version":version,"sha256":sha256_hex(&serde_json::to_vec(&definition)?)}));
    } Ok(result)
}

/// Resolve a pending candidate to its exact original fact, then recheck the
/// current registry and endpoint/evidence state. The caller separately owns
/// named-human review and every live source guard through mutation.
pub(crate) fn connection_review_proposal(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<ConnectionProposal,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let settled=super::s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;
    if settled.intent.output_kind!="connection" || settled.intent.selection.project_id.is_some() {return Err(unavailable());}
    let stored:ConnectionProposal=serde_json::from_value(crate::field_authority::parse_unique_json(&settled.content).map_err(RepositoryError::Validation)?)?;
    let draft=ConnectionDraft{version:1,subject:stored.subject.clone(),object:stored.object.clone(),predicate:stored.predicate.clone(),evidence_ids:stored.evidence_ids.clone()};
    for endpoint in [&draft.subject,&draft.object] {
        if !settled.intent.selection.candidates.iter().any(|v|v.block_id==endpoint.block_id && v.revision==endpoint.revision) {return Err(unavailable());}
    }
    let mut original_evidence=Vec::new();
    for item in &settled.intent.selection.admissions {original_evidence.push(format!("s08evidence_{}",sha256_hex(&serde_json::to_vec(&(&settled.intent.selection.owner,&settled.job_id,&item.event_id))?)));}
    original_evidence.sort();original_evidence.dedup();
    if draft.evidence_ids.is_empty() || draft.evidence_ids.iter().any(|id|original_evidence.binary_search(id).is_err()) {return Err(unavailable());}
    let mut actual=prepare_connection(tx,owner,workspace,"",&settled.job_id,&draft)?;
    // The slot is original provenance; it does not acquire the new owner on rename.
    actual.relation_slot=format!("s02_connection_{}",sha256_hex(&serde_json::to_vec(&(&settled.intent.selection.owner,workspace,"",&settled.job_id,&draft.subject.block_id,draft.subject.revision,&draft.object.block_id,draft.object.revision,&draft.predicate))?));
    let configured=settled.intent.connection_predicate.as_ref().ok_or_else(unavailable)?;
    if actual.predicate!=configured.predicate || actual.predicate_version!=configured.version || actual.predicate_sha256!=configured.sha256
        || serde_json::to_value(&actual)?!=serde_json::to_value(&stored)? {return Err(unavailable());}
    require_policy_projection(tx,account,workspace,Some(policy))?;
    Ok(actual)
}

/// Private factual selectors for acquiring current human-review source guards.
/// The returned sources are original intent facts, not grants or source bodies.
pub(crate) fn connection_review_manifest(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    let _proposal=connection_review_proposal(tx,owner,account,workspace,candidate,policy,view,budget)?;
    let settled=super::s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;
    let admissions=&settled.intent.selection.admissions;
    if admissions.is_empty() || admissions.len()>64 {return Err(unavailable());}
    let contributors:Vec<Value>=admissions.iter().map(|item|serde_json::json!({"event_id":item.event_id,"source":item.source})).collect();
    let selection:Vec<Value>=settled.intent.selection.candidates.iter().map(|item|serde_json::json!({"block_id":item.block_id,"revision":item.revision})).collect();
    let result=serde_json::json!({"version":1,"candidate_id":candidate,"candidate_revision":3,"candidate_sha256":sha256_hex(settled.content.as_bytes()),"contributors":contributors,"selection":selection});
    budget.check(serde_json::to_vec(&result)?.len()).map_err(RepositoryError::Validation)?;
    require_policy_projection(tx,account,workspace,Some(policy))?;
    Ok(result)
}

/// Closed Host-originated human review identity. The policy is fresh authority;
/// it is never part of the immutable original receipt digest.
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ConnectionReviewRequest {
    pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,
    pub candidate_id:String,pub candidate_revision:i64,pub candidate_sha256:String,
    pub request_key:String,pub reason:String,pub action:String,pub policy_projection:PolicyPublicationRefs,
}
fn review_identity(r:&ConnectionReviewRequest)->Result<String,RepositoryError> {
    if r.version!=1 || r.candidate_revision!=3 || !matches!(r.action.as_str(),"accept"|"reject") || ![r.owner.as_str(),r.account_id.as_str(),r.workspace_id.as_str(),r.candidate_id.as_str(),r.request_key.as_str(),r.reason.as_str()].iter().all(|v|text(v)) || r.reason.len()>500 || !valid_sha256(&r.candidate_sha256) {return Err(unavailable());}
    Ok(format!("cs_s02connection_review_{}",sha256_hex(&serde_json::to_vec(&("s02.connection-review.v1",&r.account_id,&r.candidate_id,&r.request_key))?)))
}
/// Read only an exact original receipt; no current candidate or source is read.
pub(crate) fn connection_review_status(tx:&Transaction<'_>,r:&ConnectionReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    let id=review_identity(r)?;require_guarded_epoch(tx)?;
    if view.current_owner()!=r.owner || view.account_id()!=r.account_id {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    let size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![r.owner,id],|x|x.get(0)).optional()?;
    let Some(size)=size else {
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2)",params![r.owner,format!("outbox_{id}_s02_connection_review")],|x|x.get(0))?;
        if orphan {return Err(unavailable());}
        let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3)",params![r.owner,r.workspace_id,r.candidate_id],|x|x.get(0))?;
        if !present {
            if super::s08_policy_lifecycle::candidate_erased(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,&r.candidate_sha256,view,budget)? {return Ok(json!({"state":"terminal_erased","candidate_id":r.candidate_id}));}
            return Err(unavailable());
        }
        let fact=super::s08_policy_lifecycle::settled_candidate(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
        if fact.intent.output_kind!="connection" || sha256_hex(fact.content.as_bytes())!=r.candidate_sha256 {return Err(unavailable());}
        return Ok(json!({"state":"absent"}));
    };
    if !(1..=16384).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,actor_type,actor_id,reason):(String,String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND contract_version=?3",params![r.owner,id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?)))?;
    let v=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let captured=v["owner"].as_str().ok_or_else(unavailable)?;view.for_captured(tx,captured)?;
    if v.as_object().map(|o|o.len())!=Some(16) || v["version"]!="s02.connection-review.v1" || v["account_id"]!=r.account_id || v["workspace_id"]!=r.workspace_id || v["candidate_id"]!=r.candidate_id || v["candidate_revision"]!=r.candidate_revision || v["candidate_sha256"]!=r.candidate_sha256 || v["request_key"]!=r.request_key || v["reason"]!=r.reason || v["action"]!="accept" || r.action!="accept" || !v["relation_block_id"].is_string() || v["relation_revision"]!=1 || !v["curated_id"].is_string() || !v["relation_change_set_id"].is_string() || !v["relation_content_hash"].as_str().is_some_and(valid_sha256) || !v["metadata_sha256"].as_str().is_some_and(valid_sha256) || hash!=sha256_hex(raw.as_bytes()) || actor_type!="user" || actor_id!=captured || reason!="s02.connection-review.v1" {return Err(unavailable());}
    let event_size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![r.owner,format!("outbox_{id}_s02_connection_review")],|x|x.get(0)).optional()?;
    let event_size=event_size.ok_or_else(unavailable)?;if !(1..=16384).contains(&event_size) {return Err(unavailable());}budget.check(event_size as usize).map_err(RepositoryError::Validation)?;
    let event:Option<String>=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s02_connection_review' AND source_revision=1",params![r.owner,format!("outbox_{id}_s02_connection_review"),id],|x|x.get(0)).optional()?;
    if event.as_deref()!=Some(raw.as_str()) {return Err(unavailable());}
    if connection_accepted_root(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?!=v {return Err(unavailable());}
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    Ok(json!({"state":"applied","candidate_id":r.candidate_id,"relation_block_id":v["relation_block_id"],"revision":1,"visibility":"not_rechecked"}))
}
/// Existing guarded S03 transaction; caller holds all original contributor
/// guards and domain-metadata writer authority until this transaction settles.
pub(crate) fn accept_connection_review(tx:&Transaction<'_>,r:&ConnectionReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.action!="accept" {return Err(unavailable());}
    let receipt_id=review_identity(r)?;
    let prior=connection_review_status(tx,r,view,budget)?;if matches!(prior["state"].as_str(),Some("applied"|"terminal_erased")) {return Ok(prior)}
    let proposal=connection_review_proposal(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,&r.policy_projection,view,budget)?;
    let settled=super::s08_policy_lifecycle::settled_candidate(tx,&r.owner,&r.account_id,&r.workspace_id,&r.candidate_id,view,budget)?;
    if sha256_hex(settled.content.as_bytes())!=r.candidate_sha256 {return Err(unavailable());}
    let subject_entity:String=tx.query_row("SELECT subject_entity_id FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND block_id=?3 AND current_revision=?4",params![r.owner,r.workspace_id,proposal.subject.block_id,proposal.subject.revision],|x|x.get(0))?;
    let cardinality:String=tx.query_row("SELECT cardinality FROM fm_v2_predicates WHERE predicate=?1",[&proposal.predicate],|x|x.get(0))?;
    let cardinality:PredicateCardinality=serde_json::from_value(json!(cardinality))?;
    let block=format!("s02connection_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let curated=format!("s02connection_curated_{}",sha256_hex(&serde_json::to_vec(&(&r.account_id,&r.candidate_id))?));
    let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1) OR EXISTS(SELECT 1 FROM memory_quarantine WHERE (tier='candidate' AND original_id=?2) OR (tier='curated' AND original_id=?1))",params![curated,r.candidate_id],|x|x.get(0))?;if collision{return Err(unavailable());}
    let canonical=read_candidate(tx,&r.owner,&r.candidate_id,&r.candidate_id,3,&r.workspace_id,"")?;
    if canonical.payload.state!=CandidateState::AwaitingReview {return Err(unavailable());}
    let actor=RevisionActor{actor_type:"user".into(),actor_id:r.owner.clone(),reason:r.reason.clone(),source_event_id:Some(settled.attempt_id.clone())};
    let identity=KnowledgeIdentity{block_id:block.clone(),subject_entity_id:subject_entity,predicate:proposal.predicate.clone(),claim_slot:proposal.relation_slot.clone(),cardinality};
    let payload=json!({"value":{"type":"knowledge_ref","value":{"block_id":proposal.object.block_id,"revision":proposal.object.revision}},"expected_value_type":"knowledge_ref","kind":"fact","status":"active","tags":[],"confidence":0.0,"trust":0.0,"activation":"review_approved","activation_rationale":{"classification":"inferred_untrusted","reviewer":r.owner},"evidence_ids":proposal.evidence_ids});
    let paths:Vec<String>=["/value","/expected_value_type","/kind","/tags","/status","/confidence","/trust","/activation","/activation_rationale","/valid_from","/valid_to","/evidence_ids"].iter().map(|v|v.to_string()).collect();
    let operation=json!({"version":"s03.published-create.v1","owner":r.owner,"workspace":r.workspace_id,"project":"","block_id":block,"subject_entity_id":identity.subject_entity_id,"predicate":identity.predicate,"claim_slot":identity.claim_slot,"cardinality":identity.cardinality,"payload":payload,"request_key":r.request_key,"actor_type":"user","actor_id":r.owner,"reason":r.reason,"source_event_id":actor.source_event_id,"approved_paths":paths,"producer":null});
    let operation_raw=serde_json::to_string(&operation)?;
    let review=crate::field_authority::OwnerReviewContext{authenticated_owner:r.owner.clone(),reviewer_id:r.owner.clone(),expected_revision:0,proposal_key:r.request_key.clone(),payload_hash:sha256_hex(operation_raw.as_bytes()),approved_paths:paths};
    let scope=ScopeRef{owner_id:r.owner.clone(),workspace_id:Some(r.workspace_id.clone()),project_id:None,session_id:None};
    let (revision,_)=V2Repository::guarded_create_knowledge_in_transaction(tx,&scope,&identity,&actor,&r.request_key,&operation_raw,Some(&review),None,true)?;
    let metadata=json!({"connection":{"version":1,"subject":proposal.subject,"predicate_version":proposal.predicate_version,"predicate_sha256":proposal.predicate_sha256}});
    crate::ontology_v3::validate_revision_metadata(&metadata)?;let metadata_raw=serde_json::to_string(&metadata)?;let now=chrono::Utc::now().to_rfc3339();
    let evidence=serde_json::to_string(&revision.payload.evidence_ids)?;
    tx.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES(?,?,1,'openclank.memory-revision-metadata.v1',?,?,?,?,?,?,?,?)",params![r.owner,block,metadata_raw,sha256_hex(metadata_raw.as_bytes()),"user",r.owner,revision.change_set_id,actor.source_event_id,evidence,now])?;
    tx.execute("INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id,created_at) VALUES(?,?,1,'/metadata/connection','manual','user',?,?,?,?,?,?)",params![r.owner,block,r.owner,r.owner,actor.source_event_id,evidence,revision.change_set_id,now])?;
    let mut record=crate::record::MemoryRecord::new(&settled.content);record.id=curated.clone();record.kind=crate::record::MemoryKind::Fact;record.source="s08_connection".into();record.source_type=crate::record::SourceType::AutoExtracted;record.owner=Some(r.owner.clone());record.workspace_id=r.workspace_id.clone();record.metadata=json!({"candidate_id":r.candidate_id,"manually_reviewed":true});
    crate::store::sqlite::SqliteStore::upsert_curated_inner(tx,&record,None).map_err(RepositoryError::Validation)?;
    let receipt=json!({"version":"s02.connection-review.v1","owner":r.owner,"account_id":r.account_id,"workspace_id":r.workspace_id,"candidate_id":r.candidate_id,"candidate_revision":3,"candidate_sha256":r.candidate_sha256,"request_key":r.request_key,"reason":r.reason,"action":"accept","relation_block_id":block,"relation_revision":1,"curated_id":curated,"relation_change_set_id":revision.change_set_id,"relation_content_hash":revision.content_hash,"metadata_sha256":sha256_hex(metadata_raw.as_bytes())});let raw=serde_json::to_string(&receipt)?;
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s02.connection-review.v1',NULL,?,?,?,?)",params![r.owner,receipt_id,r.owner,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    let mut activated=canonical.payload;activated.state=CandidateState::Activated;activated.accepted_block_id=Some(block.clone());
    insert_candidate_revision(tx,&r.owner,&r.candidate_id,4,Some(3),&activated,&actor,&receipt_id,&now)?;
    if tx.execute("UPDATE fm_v2_candidates SET current_revision=4 WHERE owner_id=? AND candidate_id=? AND current_revision=3",params![r.owner,r.candidate_id])?!=1 || tx.execute("UPDATE candidates SET status='accepted',reason=?,accepted_curated_id=?,updated_at=? WHERE owner=? AND workspace_id=? AND id=? AND status='pending' AND accepted_curated_id IS NULL",params![r.reason,curated,now,r.owner,r.workspace_id,r.candidate_id])?!=1 {return Err(unavailable());}
    insert_outbox(tx,&r.owner,&receipt_id,"candidate_revision",&json!({"candidate_id":r.candidate_id,"revision":4,"accepted_block_id":block}),4,&now)?;
    insert_outbox(tx,&r.owner,&receipt_id,"s02_connection_review",&receipt,1,&now)?;
    Ok(json!({"state":"applied","candidate_id":r.candidate_id,"relation_block_id":block,"revision":1,"visibility":"current"}))
}

/// Select the existing S08 review owner only from the candidate's exact indexed
/// job/result and verified original settlement; malformed/unknown never falls through.
pub(crate) fn s08_review_kind(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let row:Option<(String,i64)>=tx.query_row("SELECT c.turn_id,length(CAST(j.result_json AS BLOB)) FROM candidates c JOIN fm_v2_jobs j ON j.owner_id=c.owner AND j.job_id=c.turn_id AND j.workspace_key=c.workspace_id AND j.kind='access_consolidation' WHERE c.owner=?1 AND c.workspace_id=?2 AND c.id=?3 AND c.source_uri='s08://'||c.turn_id AND c.source_revision=1",params![owner,workspace,candidate],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;
    let (job,size)=row.ok_or_else(unavailable)?;if !(1..=65536).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT result_json FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|x|x.get(0))?;
    let result=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let kind=match result.get("version").and_then(Value::as_str) {
        Some("s08.rules-candidate.v1")=>{super::require_live_s08_candidate(tx,owner,workspace,candidate)?;"legacy_rules"},
        Some("s08.policy-outcome.v1")=>{
            let (attempt,status,size):(String,String,i64)=tx.query_row("SELECT source_event_id,status,length(CAST(content AS BLOB)) FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?)))?;
            if !(1..=65536).contains(&size) || !matches!(status.as_str(),"pending"|"accepted"|"rejected") {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
            let content:String=tx.query_row("SELECT content FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|x|x.get(0))?;
            let fact=super::s08_policy_lifecycle::settled_outcome(tx,owner,account,workspace,&job,&attempt,view,budget)?;
            if fact.candidate_id.as_deref()!=Some(candidate) || fact.content_sha256.as_deref()!=Some(sha256_hex(content.as_bytes()).as_str()) {return Err(unavailable());}
            if status=="pending" {super::s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;}
            match fact.intent.output_kind.as_str() {"connection"=>"connection","summary"=>"policy_summary","journal_synthesis"=>"journal_synthesis",_=>return Err(unavailable())}
        },
        _=>return Err(unavailable()),
    };
    require_policy_projection(tx,account,workspace,Some(policy))?;
    Ok(json!({"version":1,"candidate_id":candidate,"kind":kind}))
}

/// Factual accepted-root proof for existing S03/reset owners; it never grants
/// review or current access to the original contributors or relation.
pub(crate) fn connection_accepted_root(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    let settled=super::s08_policy_lifecycle::settled_candidate_accepted(tx,owner,account,workspace,candidate,view,budget)?;
    if settled.intent.output_kind!="connection" {return Err(unavailable());}
    let lengths:Vec<i64>={let mut q=tx.prepare("SELECT length(CAST(proposal_json AS BLOB)) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision IN (3,4) ORDER BY revision")?;let rows=q.query_map(params![owner,candidate],|x|x.get::<_,i64>(0))?.collect::<Result<Vec<_>,_>>()?;rows};
    if lengths.len()!=2 || lengths.iter().any(|n|!(1..=65536).contains(n)) {return Err(unavailable());}for n in lengths {budget.check(n as usize).map_err(RepositoryError::Validation)?;}
    let before=read_candidate_inner(tx,owner,candidate,candidate,3,workspace,"",false)?;
    let after=read_candidate_inner(tx,owner,candidate,candidate,4,workspace,"",false)?;
    let block=after.payload.accepted_block_id.clone().ok_or_else(unavailable)?;
    let mut expected=before.payload.clone();expected.state=CandidateState::Activated;expected.accepted_block_id=Some(block.clone());
    if before.payload.state!=CandidateState::AwaitingReview || before.content_hash!=hash_value(&serde_json::to_value(&before.payload)?)? || after.payload!=expected || after.previous_revision!=Some(3) || after.actor_type!="user" || after.source_event_id.as_deref()!=Some(settled.attempt_id.as_str()) || after.content_hash!=hash_value(&serde_json::to_value(&after.payload)?)? {return Err(unavailable());}
    view.for_captured(tx,&after.actor_id)?;
    let id=after.change_set_id.clone();
    let size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,id],|x|x.get(0)).optional()?;
    let size=size.ok_or_else(unavailable)?;if !(1..=16384).contains(&size) {return Err(unavailable());}budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,actor,actor_id,reason):(String,String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND contract_version=?3",params![owner,id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?)))?;
    let receipt=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let key=receipt["request_key"].as_str().filter(|v|text(v)).ok_or_else(unavailable)?;
    let reason_text=receipt["reason"].as_str().filter(|v|text(v) && v.len()<=500).ok_or_else(unavailable)?;
    let expected_id=format!("cs_s02connection_review_{}",sha256_hex(&serde_json::to_vec(&("s02.connection-review.v1",account,candidate,key))?));
    let curated=receipt["curated_id"].as_str().filter(|v|text(v)).ok_or_else(unavailable)?;
    let rel_change=receipt["relation_change_set_id"].as_str().filter(|v|text(v)).ok_or_else(unavailable)?;
    let rel_hash=receipt["relation_content_hash"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
    let metadata_hash=receipt["metadata_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
    if receipt.as_object().map(|v|v.len())!=Some(16) || receipt["version"]!="s02.connection-review.v1" || receipt["owner"]!=after.actor_id || receipt["account_id"]!=account || receipt["workspace_id"]!=workspace || receipt["candidate_id"]!=candidate || receipt["candidate_revision"]!=3 || receipt["action"]!="accept" || receipt["candidate_sha256"]!=sha256_hex(settled.content.as_bytes()) || receipt["relation_block_id"]!=block || receipt["relation_revision"]!=1 || id!=expected_id || hash!=sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=after.actor_id || reason!="s02.connection-review.v1" {return Err(unavailable());}
    let event_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,format!("outbox_{id}_s02_connection_review")],|x|x.get(0)).optional()?;let event_len=event_len.ok_or_else(unavailable)?;if !(1..=16384).contains(&event_len){return Err(unavailable());}budget.check(event_len as usize).map_err(RepositoryError::Validation)?;
    let event:Option<String>=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s02_connection_review' AND source_revision=1",params![owner,format!("outbox_{id}_s02_connection_review"),id],|x|x.get(0)).optional()?;if event.as_deref()!=Some(raw.as_str()){return Err(unavailable());}
    let event_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,format!("outbox_{id}_candidate_revision")],|x|x.get(0)).optional()?;let event_len=event_len.ok_or_else(unavailable)?;if !(1..=16384).contains(&event_len){return Err(unavailable());}budget.check(event_len as usize).map_err(RepositoryError::Validation)?;
    let candidate_event:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_revision'",params![owner,format!("outbox_{id}_candidate_revision"),id],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;
    if candidate_event.as_ref().is_none_or(|(raw,rev)|*rev!=4 || raw.len()>16384 || crate::field_authority::parse_unique_json(raw).ok()!=Some(json!({"candidate_id":candidate,"revision":4,"accepted_block_id":block}))) {return Err(unavailable());}
    let proposal:ConnectionProposal=serde_json::from_value(crate::field_authority::parse_unique_json(&settled.content).map_err(RepositoryError::Validation)?)?;
    if before.payload.proposal!=serde_json::to_value(&proposal)? || ![&proposal.subject,&proposal.object].iter().all(|e|settled.intent.selection.candidates.iter().any(|v|v.block_id==e.block_id && v.revision==e.revision)) {return Err(unavailable());}
    let configured=settled.intent.connection_predicate.as_ref().ok_or_else(unavailable)?;
    if proposal.predicate!=configured.predicate || proposal.predicate_version!=configured.version || proposal.predicate_sha256!=configured.sha256 {return Err(unavailable());}
    let mut evidence=Vec::new();for a in &settled.intent.selection.admissions {evidence.push(format!("s08evidence_{}",sha256_hex(&serde_json::to_vec(&(&settled.intent.selection.owner,&settled.job_id,&a.event_id))?)));}evidence.sort();evidence.dedup();
    if proposal.evidence_ids.is_empty() || proposal.evidence_ids.iter().any(|id|evidence.binary_search(id).is_err()) || before.payload.evidence_ids!=evidence {return Err(unavailable());}
    let expected_slot=format!("s02_connection_{}",sha256_hex(&serde_json::to_vec(&(&settled.intent.selection.owner,workspace,"",&settled.job_id,&proposal.subject.block_id,proposal.subject.revision,&proposal.object.block_id,proposal.object.revision,&proposal.predicate))?));
    if proposal.relation_slot!=expected_slot {return Err(unavailable());}
    let (predicate,slot,subject_entity,cardinality):(String,String,String,String)=tx.query_row("SELECT predicate,claim_slot,subject_entity_id,cardinality FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND block_id=?3",params![owner,workspace,block],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?))).optional()?.ok_or_else(unavailable)?;
    if predicate!=proposal.predicate || slot!=proposal.relation_slot || block!=format!("s02connection_{}",sha256_hex(&serde_json::to_vec(&(account,candidate))?)) || curated!=format!("s02connection_curated_{}",sha256_hex(&serde_json::to_vec(&(account,candidate))?)) {return Err(unavailable());}
    let lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(value_json AS BLOB)),length(CAST(evidence_json AS BLOB)) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=1",params![owner,block],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;let (value_len,evidence_len)=lengths.ok_or_else(unavailable)?;if !(1..=65536).contains(&value_len) || !(1..=65536).contains(&evidence_len){return Err(unavailable());}budget.check((value_len+evidence_len) as usize).map_err(RepositoryError::Validation)?;
    let (value_raw,evidence_raw,stored_hash,change,revision_actor,revision_actor_id,source,revision_reason):(String,String,String,String,String,String,Option<String>,String)=tx.query_row("SELECT value_json,evidence_json,content_hash,change_set_id,actor_type,actor_id,source_event_id,reason FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND previous_revision IS NULL AND value_type='knowledge_ref' AND expected_value_type='knowledge_ref' AND kind='fact' AND tags_json='[]' AND status='active' AND confidence=0 AND trust=0 AND activation='review_approved' AND activation_rationale=?3 AND valid_from IS NULL AND valid_to IS NULL",params![owner,block,serde_json::to_string(&json!({"classification":"inferred_untrusted","reviewer":after.actor_id}))?],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?,x.get(4)?,x.get(5)?,x.get(6)?,x.get(7)?))).optional()?.ok_or_else(unavailable)?;
    if value_raw.len()>65536 || evidence_raw.len()>65536 {return Err(unavailable());}budget.check(value_raw.len()+evidence_raw.len()).map_err(RepositoryError::Validation)?;
    let value=crate::field_authority::parse_unique_json(&value_raw).map_err(RepositoryError::Validation)?;
    let evidence_value=crate::field_authority::parse_unique_json(&evidence_raw).map_err(RepositoryError::Validation)?;
    let expected_payload=json!({"value":{"type":"knowledge_ref","value":{"block_id":proposal.object.block_id,"revision":proposal.object.revision}},"expected_value_type":"knowledge_ref","kind":"fact","status":"active","tags":[],"confidence":0.0,"trust":0.0,"activation":"review_approved","activation_rationale":{"classification":"inferred_untrusted","reviewer":after.actor_id},"valid_from":null,"valid_to":null,"evidence_ids":proposal.evidence_ids});
    let relation_evidence={let mut q=tx.prepare("SELECT evidence_id FROM fm_v2_revision_evidence WHERE owner_id=?1 AND block_id=?2 AND revision=1 ORDER BY evidence_id LIMIT 65")?;let rows=q.query_map(params![owner,block],|x|x.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;rows};
    if value!=expected_payload["value"] || evidence_value!=expected_payload["evidence_ids"] || relation_evidence!=proposal.evidence_ids || stored_hash!=rel_hash || stored_hash!=hash_value(&expected_payload)? || change!=rel_change || revision_actor!="user" || revision_actor_id!=after.actor_id || source.as_deref()!=Some(settled.attempt_id.as_str()) || revision_reason!=reason_text {return Err(unavailable());}
    let operation_len:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,rel_change],|x|x.get(0)).optional()?;let operation_len=operation_len.ok_or_else(unavailable)?;if !(1..=131072).contains(&operation_len){return Err(unavailable());}budget.check(operation_len as usize).map_err(RepositoryError::Validation)?;
    let (operation_raw,operation_hash,operation_actor,operation_actor_id):(String,String,String,String)=tx.query_row("SELECT operation_metadata_json,content_hash,actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3 AND source_event_id=?4 AND contract_version=?5",params![owner,rel_change,reason_text,settled.attempt_id,CONTRACT_VERSION],|x|Ok((x.get(0)?,x.get(1)?,x.get(2)?,x.get(3)?)))?;
    let mut operation_payload=expected_payload.clone();operation_payload.as_object_mut().ok_or_else(unavailable)?.remove("valid_from");operation_payload.as_object_mut().ok_or_else(unavailable)?.remove("valid_to");
    let paths=json!(["/value","/expected_value_type","/kind","/tags","/status","/confidence","/trust","/activation","/activation_rationale","/valid_from","/valid_to","/evidence_ids"]);
    let operation=json!({"version":"s03.published-create.v1","owner":after.actor_id,"workspace":workspace,"project":"","block_id":block,"subject_entity_id":subject_entity,"predicate":predicate,"claim_slot":slot,"cardinality":cardinality,"payload":operation_payload,"request_key":key,"actor_type":"user","actor_id":after.actor_id,"reason":reason_text,"source_event_id":settled.attempt_id,"approved_paths":paths,"producer":null});
    if operation_hash!=sha256_hex(operation_raw.as_bytes()) || operation_actor!="user" || operation_actor_id!=after.actor_id || crate::field_authority::parse_unique_json(&operation_raw).map_err(RepositoryError::Validation)?!=operation {return Err(unavailable());}
    let value_authority:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND field_path='/value' AND origin='manual' AND producer_type='user' AND producer_id=?3 AND approved_by=?3 AND source_event_id=?4 AND change_set_id=?5)",params![owner,block,after.actor_id,settled.attempt_id,rel_change],|x|x.get(0))?;if !value_authority {return Err(unavailable());}
    let event_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,format!("outbox_{rel_change}_knowledge_revision")],|x|x.get(0)).optional()?;let event_len=event_len.ok_or_else(unavailable)?;if !(1..=16384).contains(&event_len){return Err(unavailable());}budget.check(event_len as usize).map_err(RepositoryError::Validation)?;
    let relation_event:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='knowledge_revision'",params![owner,format!("outbox_{rel_change}_knowledge_revision"),rel_change],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;
    if relation_event.as_ref().is_none_or(|(raw,rev)|*rev!=1 || raw.len()>16384 || crate::field_authority::parse_unique_json(raw).ok()!=Some(json!({"block_id":block,"revision":1}))) {return Err(unavailable());}
    let meta_len:Option<i64>=tx.query_row("SELECT length(CAST(metadata_json AS BLOB)) FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2 AND revision=1",params![owner,block],|x|x.get(0)).optional()?;let meta_len=meta_len.ok_or_else(unavailable)?;if !(1..=65536).contains(&meta_len){return Err(unavailable());}budget.check(meta_len as usize).map_err(RepositoryError::Validation)?;
    let (metadata,meta_hash):(String,String)=tx.query_row("SELECT metadata_json,metadata_hash FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND metadata_schema='openclank.memory-revision-metadata.v1' AND actor_type='user' AND actor_id=?3 AND change_set_id=?4 AND source_event_id=?5 AND evidence_json=?6",params![owner,block,after.actor_id,rel_change,settled.attempt_id,evidence_raw],|x|Ok((x.get(0)?,x.get(1)?))).optional()?.ok_or_else(unavailable)?;
    if meta_hash!=metadata_hash || sha256_hex(metadata.as_bytes())!=metadata_hash || crate::field_authority::parse_unique_json(&metadata).map_err(RepositoryError::Validation)?!=json!({"connection":{"version":1,"subject":proposal.subject,"predicate_version":proposal.predicate_version,"predicate_sha256":proposal.predicate_sha256}}) {return Err(unavailable());}
    let authority:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND field_path='/metadata/connection' AND origin='manual' AND producer_type='user' AND producer_id=?3 AND approved_by=?3 AND source_event_id=?4 AND evidence_json=?5 AND change_set_id=?6)",params![owner,block,after.actor_id,settled.attempt_id,evidence_raw,rel_change],|x|x.get(0))?;
    let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates c WHERE c.owner=?1 AND c.workspace_id=?2 AND c.id=?3 AND c.status='accepted' AND c.accepted_curated_id=?4 AND c.reason=?5)",params![owner,workspace,candidate,curated,reason_text],|x|x.get(0))?;
    if !authority || !linked {return Err(unavailable());}
    Ok(receipt)
}

/// Discover a completed original through the existing candidate revision link.
/// The Host first consults its owned in-flight ticket; missing/erased candidate
/// is unavailable, never evidence that a previous request was unsent.
pub(crate) fn connection_review_original(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,candidate_sha256:Option<&str>,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let status:Option<String>=tx.query_row("SELECT status FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,candidate],|x|x.get(0)).optional()?;
    let result=match status.as_deref() {
        Some("pending")=>{let fact=super::s08_policy_lifecycle::settled_candidate(tx,owner,account,workspace,candidate,view,budget)?;if fact.intent.output_kind!="connection" {return Err(unavailable());}json!({"state":"absent"})},
        Some("accepted")=>{let receipt=connection_accepted_root(tx,owner,account,workspace,candidate,view,budget)?;json!({"state":"found","original":{"candidate_id":candidate,"candidate_revision":receipt["candidate_revision"],"candidate_sha256":receipt["candidate_sha256"],"request_key":receipt["request_key"],"reason":receipt["reason"],"action":"accept"},"receipt":{"state":"applied","candidate_id":candidate,"relation_block_id":receipt["relation_block_id"],"revision":1,"visibility":"not_rechecked"}})},
        Some("rejected")=>super::s08_policy_review::rejected_original(tx,owner,account,workspace,candidate,"connection",view,budget)?,
        None=>{let digest=candidate_sha256.filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;if super::s08_policy_lifecycle::candidate_erased(tx,owner,account,workspace,candidate,digest,view,budget)? {json!({"state":"terminal_erased","candidate_id":candidate})} else {return Err(unavailable());}},
        _=>return Err(unavailable()),
    };
    require_policy_projection(tx,account,workspace,Some(policy))?;Ok(result)
}
