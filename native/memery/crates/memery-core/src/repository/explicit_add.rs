//! Private explicit-human Add proposal owner. The caller holds the original
//! account/policy admission and the same SQLite transaction as the action.
use super::*;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ExplicitAddProposal {
    pub version: String,
    pub owner: String,
    pub workspace_id: String,
    pub account_id: String,
    pub request_key: String,
    pub input_sha256: String,
    pub stored_input_sha256: String,
    pub candidate_id: String,
    pub text: String,
    pub category: String,
    pub source: String,
    pub question_context: Option<Value>,
}

fn validate_proposal(tx: &Transaction<'_>, p: &ExplicitAddProposal, pending:bool) -> Result<(), RepositoryError> {validate_proposal_at(tx,&p.owner,p,pending)}
fn validate_proposal_at(tx:&Transaction<'_>,sql_owner:&str,p:&ExplicitAddProposal,pending:bool)->Result<(),RepositoryError> {
    require_guarded_epoch(tx)?;
    if p.version != "s03.explicit-add-proposal.v1" || p.owner.trim().is_empty()
        || p.workspace_id.trim().is_empty() || p.account_id.trim().is_empty()
        || p.request_key.trim().is_empty() || p.text.trim() != p.text || p.text.is_empty()
        || p.text.chars().count() > 5000 || !valid_sha256(&p.input_sha256) || !valid_sha256(&p.stored_input_sha256)
        || !matches!(p.category.as_str(), "fact"|"contact"|"task"|"preference"|"identity"|"project"|"goal"|"unknown") {
        return Err(RepositoryError::Validation("explicit Add proposal unavailable".into()));
    }
    let input = json!({"category":p.category,"question_context":p.question_context,"source":p.source,"text":p.text});
    if sha256_hex(serde_json::to_string(&input)?.as_bytes()) != p.stored_input_sha256 {
        return Err(RepositoryError::Validation("explicit Add input changed".into()));
    }
    let filtered=crate::privacy::filter_text(&p.text).ok_or_else(||RepositoryError::Validation("explicit Add privacy unavailable".into()))?;
    let source=crate::privacy::filter_text(&p.source).ok_or_else(||RepositoryError::Validation("explicit Add source unavailable".into()))?;
    let (context,redactions)=crate::privacy::filter_json(&serde_json::to_value(&p.question_context)?);
    if filtered.1 || filtered.0!=p.text || source.1 || source.0!=p.source || redactions!=0 || context!=serde_json::to_value(&p.question_context)? {
        return Err(RepositoryError::Validation("explicit Add filtered input changed".into()));
    }
    if p.category=="unknown" && crate::record::normalize_question(&p.text)!=p.text {return Err(RepositoryError::Validation("question normalization changed".into()));}
    let expected = format!("manualadd_{}", sha256_hex(serde_json::to_string(&json!(["s03.explicit-add-id.v1",p.account_id,p.workspace_id,p.request_key]))?.as_bytes()));
    if p.candidate_id != expected { return Err(RepositoryError::Validation("explicit Add identity changed".into())); }
    let candidate: Option<(String,String,String,String,String)> = tx.query_row(
        "SELECT content,owner,workspace_id,source,status FROM candidates WHERE id=?",
        params![p.candidate_id], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))
        .optional()?;
    if candidate.as_ref().map(|(text,owner,workspace,source,status)|
        text == &p.text && owner == sql_owner && workspace == &p.workspace_id && source == "user_created" && (status == "pending" || (!pending && (status == "accepted" || status == "rejected")))) != Some(true) {
        return Err(RepositoryError::Validation("explicit Add candidate changed".into()));
    }
    Ok(())
}

pub(crate) fn create_explicit_add_proposal(tx: &Transaction<'_>, p: &ExplicitAddProposal) -> Result<(), RepositoryError> {
    validate_proposal(tx,p,true)?;
    checked_question_context(tx,p)?;
    let actor = RevisionActor { actor_type:"user".into(), actor_id:p.owner.clone(), reason:"explicit human add".into(), source_event_id:None };
    let payload = CandidatePayload { proposal:serde_json::to_value(p)?, state:CandidateState::Proposed,
        confidence:0.95, importance:0.8, reason:"explicit human add".into(), source_id:None,
        source_revision:None, accepted_block_id:None, evidence_ids:vec![] };
    validate_candidate(&p.candidate_id,&p.request_key,&payload,true)?;
    let now=chrono::Utc::now().to_rfc3339();
    let change=insert_change_set(tx,&p.owner,&actor,&serde_json::to_value(&payload)?,&now)?;
    tx.execute("INSERT INTO fm_v2_candidates(owner_id,candidate_id,workspace_key,project_key,idempotency_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,'',?,?,0,?)",params![p.owner,p.candidate_id,p.workspace_id,p.request_key,change,now])?;
    insert_candidate_revision(tx,&p.owner,&p.candidate_id,1,None,&payload,&actor,&change,&now)?;
    if tx.execute("UPDATE fm_v2_candidates SET current_revision=1 WHERE owner_id=? AND candidate_id=? AND current_revision=0",params![p.owner,p.candidate_id])? != 1 {
        return Err(RepositoryError::Validation("explicit Add candidate head changed".into()));
    }
    insert_outbox(tx,&p.owner,&change,"candidate_revision",&json!({"candidate_id":p.candidate_id,"revision":1}),1,&now)?;
    Ok(())
}

/// Read only the exact current candidate in its original owner/workspace. A
/// caller must still apply its own current account and policy authority.
pub(crate) fn current_explicit_add_proposal(tx:&Transaction<'_>,owner:&str,workspace:&str,candidate:&str)->Result<(ExplicitAddProposal,i64),RepositoryError> {current_explicit_add_proposal_at(tx,owner,owner,workspace,candidate,None)}
pub(crate) fn current_explicit_add_proposal_custody(tx:&Transaction<'_>,workspace:&str,candidate:&str,captured:&str,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<(ExplicitAddProposal,i64),RepositoryError> {
    let proof=view.for_captured(tx,captured)?;let result=current_explicit_add_proposal_at(tx,proof.current_owner(),proof.captured_owner(),workspace,candidate,Some(view))?;
    if result.0.account_id!=proof.account_id() {return Err(RepositoryError::NotFound);} Ok(result)
}
fn current_explicit_add_proposal_at(
    tx: &Transaction<'_>, sql_owner:&str, owner: &str, workspace: &str, candidate: &str, view:Option<&super::owner_custody::VerifiedCustodyView<'_,'_>>,
) -> Result<(ExplicitAddProposal, i64), RepositoryError> {
    require_guarded_epoch(tx)?;
    let head: Option<(String,i64)> = tx.query_row(
        "SELECT idempotency_key,current_revision FROM fm_v2_candidates WHERE owner_id=? AND workspace_key=? AND project_key='' AND candidate_id=?",
        params![sql_owner,workspace,candidate], |r| Ok((r.get(0)?,r.get(1)?))).optional()?;
    let (key,revision)=head.ok_or(RepositoryError::NotFound)?;
    if revision < 1 { return Err(RepositoryError::NotFound); }
    let row=read_candidate(tx,sql_owner,candidate,&key,revision,workspace,"")?;
    let (legacy_status,legacy_curated):(String,Option<String>)=tx.query_row("SELECT status,accepted_curated_id FROM candidates WHERE id=? AND owner=? AND workspace_id=?",params![candidate,sql_owner,workspace],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
    let state_matches=match row.payload.state {
        CandidateState::Proposed=>legacy_status=="pending" && legacy_curated.is_none() && row.payload.accepted_block_id.is_none(),
        CandidateState::Activated=>legacy_status=="accepted" && legacy_curated.as_deref().is_some_and(|id|!id.is_empty()) && row.payload.accepted_block_id.as_deref().is_some_and(|id|!id.is_empty()),
        CandidateState::Rejected=>legacy_status=="rejected" && legacy_curated.is_none() && row.payload.accepted_block_id.is_none(),
        _=>false,
    };
    if !state_matches {return Err(RepositoryError::Validation("explicit Add candidate state changed".into()));}
    if row.payload.state==CandidateState::Proposed {
        verify_selected_explicit_candidate(tx,sql_owner,workspace,candidate,&key,revision,"explicit human add","edit explicit human add",None,view,None)?;
    }
    let proposal:ExplicitAddProposal=serde_json::from_value(row.payload.proposal)?;
    if proposal.owner != owner || proposal.workspace_id != workspace || proposal.candidate_id != candidate || proposal.request_key != key {
        return Err(RepositoryError::Validation("explicit Add proposal binding changed".into()));
    }
    validate_proposal_at(tx,sql_owner,&proposal,false)?;
    let original=read_candidate(tx,sql_owner,candidate,&key,1,workspace,"")?;
    let first:ExplicitAddProposal=serde_json::from_value(original.payload.proposal)?;
    if first.version!=proposal.version || first.owner!=proposal.owner || first.workspace_id!=proposal.workspace_id || first.account_id!=proposal.account_id || first.request_key!=proposal.request_key || first.input_sha256!=proposal.input_sha256 || first.candidate_id!=proposal.candidate_id {
        return Err(RepositoryError::Validation("explicit Add original binding changed".into()));
    }
    Ok((proposal,revision))
}

/// Validate the Host-normalized association as data, then re-resolve an
/// entity target under this exact transaction and tenant scope. It does not
/// turn a supplied source, association, or question into an authority grant.
fn checked_question_context_scoped(tx:&Transaction<'_>, owner:&str, workspace:&str, category:&str, context:Option<&Value>) -> Result<Option<Value>,RepositoryError> {
    checked_question_context_at(tx,owner,owner,workspace,category,context)
}
fn checked_question_context_at(tx:&Transaction<'_>,sql_owner:&str,owner:&str,workspace:&str,category:&str,context:Option<&Value>)->Result<Option<Value>,RepositoryError> {
    let Some(value)=context else { return Ok(None) };
    let obj=value.as_object().ok_or_else(||RepositoryError::Validation("question context unavailable".into()))?;
    let allowed=["contract","mode","target","predicate","predicate_version","claim_slot","expected_value_type","question_id","scope"];
    if obj.keys().any(|k| !allowed.contains(&k.as_str())) || value["contract"]!="openclank.question-context/v1"
        || !matches!(value["mode"].as_str(),Some("missing_slot"|"inquiry")) {
        return Err(RepositoryError::Validation("question context unavailable".into()));
    }
    let target=value["target"].as_object().ok_or_else(||RepositoryError::Validation("question target unavailable".into()))?;
    if target.len()!=2 {return Err(RepositoryError::Validation("question target unavailable".into()));}
    let kind=target.get("kind").and_then(Value::as_str).unwrap_or("");
    let id=target.get("id").and_then(Value::as_str).unwrap_or("");
    if !matches!(kind,"entity"|"claim"|"claim_slot"|"source"|"evidence"|"media_asset"|"associated_text") || id.is_empty() || id.trim()!=id {
        return Err(RepositoryError::Validation("question target unavailable".into()));
    }
    for key in ["predicate","predicate_version","claim_slot","expected_value_type","question_id"] {
        if let Some(v)=obj.get(key) { if !v.as_str().is_some_and(|s| s.trim()==s) { return Err(RepositoryError::Validation("question context unavailable".into())); } }
    }
    if obj.get("scope")!=Some(&json!({"owner":owner,"workspace_id":workspace,"project_id":""})) {
        return Err(RepositoryError::Validation("question scope unavailable".into()));
    }
    if category=="unknown" && kind=="entity" {
        let revision:Option<i64>=tx.query_row("SELECT current_revision FROM fm_v2_entities WHERE owner_id=? AND workspace_key=? AND project_key='' AND entity_id=?",params![sql_owner,workspace,id],|r|r.get(0)).optional()?;
        let entity=read_entity(tx,&sql_owner,id,revision.ok_or(RepositoryError::NotFound)?,&workspace,"")?;
        if entity.payload.status!="active" { return Err(RepositoryError::NotFound); }
    }
    Ok(Some(value.clone()))
}

fn checked_question_context(tx:&Transaction<'_>,p:&ExplicitAddProposal)->Result<Option<Value>,RepositoryError> {
    checked_question_context_scoped(tx,&p.owner,&p.workspace_id,&p.category,p.question_context.as_ref())
}

/// Prepared entirely under the caller's original transaction. This contains
/// private source data and must not be returned as a public receipt.
pub(crate) struct PreparedExplicitAdd {
    pub record: crate::record::MemoryRecord,
    pub candidate_digest: String,
    pub proposal: ExplicitAddProposal,
    pub candidate_revision: i64,
}

pub(crate) fn prepare_explicit_add(
    tx:&Transaction<'_>, p:&ExplicitAddProposal,
) -> Result<PreparedExplicitAdd,RepositoryError> {
    let (current,revision)=current_explicit_add_proposal(tx,&p.owner,&p.workspace_id,&p.candidate_id)?;
    if current!=*p {return Err(RepositoryError::Validation("explicit Add proposal changed".into()));}
    checked_question_context(tx,p)?;
    if p.category=="unknown" && !question_semantics_capability(tx)? {
        return Err(RepositoryError::Validation("question semantics unavailable".into()));
    }
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![p.candidate_id,p.owner,p.workspace_id],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    use crate::record::MemoryKind;
    let kind=match p.category.as_str() {"identity"=>MemoryKind::Persona,"preference"=>MemoryKind::Instruction,"task"=>MemoryKind::Episodic,"unknown"=>MemoryKind::Unknown,"fact"|"contact"|"project"|"goal"=>MemoryKind::Fact,_=>return Err(RepositoryError::NotFound)};
    if candidate.kind!=kind || candidate.content_hash!=crate::privacy::content_hash(&p.text) || candidate.accepted_curated_id.is_some() {
        return Err(RepositoryError::Validation("explicit Add candidate changed".into()));
    }
    let digest=crate::provider::native::candidate_snapshot_digest(&candidate).map_err(RepositoryError::Validation)?;
    let mut record=crate::provider::native::reviewed_candidate_record(&candidate,&p.owner,&p.workspace_id,"explicit human add");
    record.source="user_created".into();
    record.metadata["category"]=json!(p.category);
    record.metadata["question_context"]=json!(p.question_context);
    record.metadata["declared_source"]=json!(p.source);
    if p.category=="identity" { record.tags=vec!["self".into()]; }
    let (metadata,redactions)=crate::privacy::filter_json(&record.metadata);
    if redactions!=0 || metadata!=record.metadata {return Err(RepositoryError::Validation("explicit Add metadata changed".into()));}
    Ok(PreparedExplicitAdd { record,candidate_digest:digest,proposal:p.clone(),candidate_revision:revision })
}

// These values are private transaction-local inputs, constructed only after
// an origin owner has proved its own exact persisted review and policy.
struct TypedPublication<'a> {
    owner:&'a str, workspace_id:&'a str, category:&'a str, text:&'a str,
    request_key:&'a str, record:&'a crate::record::MemoryRecord,
    context:Option<Value>, reason:&'a str, source_type:&'a str,
    source_event_id:&'a str, provenance:Value,
}
struct TypedPublicationResult {
    identity:KnowledgeIdentity, source_id:String, source_event_id:String,
    evidence_id:String, operation:Value, paths:Vec<String>,
    revision:KnowledgeRevision, actor:RevisionActor, now:String,
}
fn publish_typed_core(tx:&Transaction<'_>,p:&TypedPublication<'_>)->Result<TypedPublicationResult,RepositoryError> {
    use crate::field_authority::{OwnerReviewContext,TrustedProducerContext};
    let record=p.record;
    let context=p.context.clone();
    let (_semantic,text,_,_,_)=persisted_curated_mirror(tx,&p.owner,&p.workspace_id,&record.id)?;
    if text!=p.text {return Err(RepositoryError::Validation("explicit Add persisted content changed".into()));}
    let sha=|v:&Value| -> Result<String,RepositoryError> {Ok(sha256_hex(serde_json::to_string(v)?.as_bytes()))};
    let question=p.category=="unknown";
    let target=context.as_ref().and_then(|c|c.get("target"));
    let entity_target=target.filter(|t|t["kind"]=="entity").and_then(|t|t["id"].as_str());
    let label=if p.category=="identity" {"self"} else {"memory"};
    let entity_id=match entity_target {Some(id)=>id.to_string(),None=>format!("entity_{}",&sha(&json!([p.owner,p.workspace_id,"",label]))?[..32])};
    let entity_row:Option<i64>=tx.query_row("SELECT current_revision FROM fm_v2_entities WHERE owner_id=? AND workspace_key=? AND project_key='' AND entity_id=?",params![p.owner,p.workspace_id,entity_id],|r|r.get(0)).optional()?;
    if let Some(revision)=entity_row {
        let entity=read_entity(tx,&p.owner,&entity_id,revision,&p.workspace_id,"")?;
        if entity.payload.status!="active" {return Err(RepositoryError::NotFound);}
        if entity_target.is_none() && (entity.payload.canonical_label!=label || entity.payload.entity_type!=if label=="self" {"person"} else {"concept"}) {return Err(RepositoryError::Validation("explicit Add subject changed".into()));}
    } else {
        if entity_target.is_some() {return Err(RepositoryError::NotFound);}
        let actor=RevisionActor{actor_type:"user".into(),actor_id:p.owner.to_owned(),reason:p.reason.into(),source_event_id:None};
        let entity=EntityPayload{entity_type:(if label=="self" {"person"} else {"concept"}).into(),canonical_label:label.into(),aliases:vec![],roles:if label=="self" {vec!["self".into()]} else {vec![]},tags:if label=="self" {vec!["self".into()]} else {vec![]},status:"active".into()};
        let now=chrono::Utc::now().to_rfc3339();
        let change=insert_change_set(tx,&p.owner,&actor,&serde_json::to_value(&entity)?,&now)?;
        tx.execute("INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,'',?,0,?)",params![p.owner,entity_id,p.workspace_id,change,now])?;
        insert_entity_revision(tx,&p.owner,&entity_id,1,None,&entity,&actor,&change,&now)?;
        if tx.execute("UPDATE fm_v2_entities SET current_revision=1 WHERE owner_id=? AND entity_id=? AND current_revision=0",params![p.owner,entity_id])?!=1 {return Err(RepositoryError::Validation("explicit Add subject changed".into()));}
        insert_outbox(tx,&p.owner,&change,"entity_revision",&json!({"entity_id":entity_id,"revision":1}),1,&now)?;
    }
    let slot=context.as_ref().and_then(|v|v["claim_slot"].as_str()).filter(|v|!v.is_empty()).unwrap_or(&record.id);
    let predicate=context.as_ref().and_then(|v|v["predicate"].as_str()).filter(|v|!v.is_empty()).unwrap_or(if question {"question"} else {p.category});
    let block_id=format!("block_{}",&sha(&json!([p.owner,p.workspace_id,"",entity_id,slot]))?[..32]);
    let source_id=format!("source_memory_{}",&sha(&json!([p.owner,record.id]))?[..32]);
    let evidence_id=format!("evidence_{}_1",record.id);
    let source_event_id=p.source_event_id.to_owned();
    let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![p.owner,source_id],|r|r.get(0))?;
    if collision {return Err(RepositoryError::Validation("explicit Add source unavailable".into()));}
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,created_at) VALUES (?,?,?,'',?,1,?,?,?)",params![p.owner,source_id,p.workspace_id,format!("memory://{}",record.id),sha(&json!(p.text))?,p.source_type,now])?;
    let value=if question {json!({"type":"null"})} else {json!({"type":"string","value":p.text})};
    tx.execute("INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,created_at) VALUES (?,?,?,1,'memory_record',?,?,'provider-record-v1','open-clank',?,?,?)",params![p.owner,evidence_id,source_id,serde_json::to_string(&json!({"record_id":record.id,"source_revision":1}))?,p.text,serde_json::to_string(&p.text)?,serde_json::to_string(&value)?,now])?;
    let identity=KnowledgeIdentity{block_id,subject_entity_id:entity_id,predicate:predicate.into(),claim_slot:slot.into(),cardinality:PredicateCardinality::Single};
    let kind=match p.category{"identity"=>"persona","preference"=>"instruction","task"=>"episodic","unknown"=>"open_question",_=>"fact"};
    let provenance=p.provenance.clone();
    let rationale=if question {json!({"reason":p.reason,"question_text":p.text,"question_context":context,"provenance":provenance})} else {json!({"reason":p.reason,"provenance":provenance})};
    let payload=json!({"value":value,"expected_value_type":"string","kind":kind,"status":if question {"open"} else {"active"},"tags":record.tags,"confidence":record.confidence_score,"trust":record.trust_score,"activation":"review_approved","activation_rationale":rationale,"evidence_ids":[evidence_id]});
    let actor=RevisionActor{actor_type:"user".into(),actor_id:p.owner.to_owned(),reason:p.reason.into(),source_event_id:Some(source_event_id.clone())};
    let producer=TrustedProducerContext{kind:"automatic".into(),validated_inherited_paths:vec![]};
    let paths=if question {vec!["/value".to_string(),"/activation_rationale".to_string()]} else {vec!["/value".to_string()]};
    let operation=json!({"version":"s03.published-create.v1","owner":p.owner,"workspace":p.workspace_id,"project":"","block_id":identity.block_id,"subject_entity_id":identity.subject_entity_id,"predicate":identity.predicate,"claim_slot":identity.claim_slot,"cardinality":"single","payload":payload,"request_key":p.request_key,"actor_type":"user","actor_id":p.owner,"reason":p.reason,"source_event_id":source_event_id,"approved_paths":paths,"producer":producer});
    let raw=serde_json::to_string(&operation)?;
    let review=OwnerReviewContext{authenticated_owner:p.owner.to_owned(),reviewer_id:p.owner.to_owned(),expected_revision:0,proposal_key:p.request_key.to_owned(),payload_hash:sha256_hex(raw.as_bytes()),approved_paths:paths.clone()};
    let scope=ScopeRef{owner_id:p.owner.to_owned(),workspace_id:Some(p.workspace_id.to_owned()),project_id:None,session_id:None};
    let (revision,_)=V2Repository::guarded_create_knowledge_in_transaction(tx,&scope,&identity,&actor,&p.request_key,&raw,Some(&review),Some(&producer),true)?;
    Ok(TypedPublicationResult{identity,source_id,source_event_id,evidence_id,operation,paths,revision,actor,now})
}

/// Publish only after the caller has performed its scoped curated upsert and
/// verified the persisted record, still in the original borrowed transaction.
/// The caller rolls the entire transaction back on any error.
/// Advance a proposed candidate through the canonical state graph in one transaction.
/// Bridge revisions retain the prior proposal; each revision has its own change
/// set/outbox and the same actual mutation actor. Only the final revision changes
/// the proposal, rejection reason, or accepted block.
fn advance_explicit_candidate(tx:&Transaction<'_>,owner:&str,candidate:&str,head:i64,prior:&CandidatePayload,final_payload:&CandidatePayload,actor:&RevisionActor,now:&str)->Result<i64,RepositoryError> {
    if prior.state!=CandidateState::Proposed || prior.accepted_block_id.is_some() {return Err(RepositoryError::NotFound)}
    let states=match final_payload.state {
        CandidateState::Proposed=>vec![CandidateState::Validating,CandidateState::AwaitingReview,CandidateState::Proposed],
        CandidateState::Rejected=>vec![CandidateState::Validating,CandidateState::Rejected],
        CandidateState::Activated=>vec![CandidateState::Validating,CandidateState::AwaitingReview,CandidateState::Activated],
        _=>return Err(RepositoryError::NotFound),
    };
    let total=states.len();let mut current=head;
    for (index,state) in states.into_iter().enumerate() {
        let mut payload=if index+1==total {final_payload.clone()} else {prior.clone()};
        payload.state=state;
        let next=current.checked_add(1).ok_or(RepositoryError::NotFound)?;
        let change=insert_change_set(tx,owner,actor,&serde_json::to_value(&payload)?,now)?;
        insert_candidate_revision(tx,owner,candidate,next,Some(current),&payload,actor,&change,now)?;
        if tx.execute("UPDATE fm_v2_candidates SET current_revision=?1 WHERE owner_id=?2 AND candidate_id=?3 AND current_revision=?4",params![next,owner,candidate,current])?!=1 {return Err(RepositoryError::NotFound)}
        insert_outbox(tx,owner,&change,"candidate_revision",&json!({"candidate_id":candidate,"revision":next}),next,now)?;
        current=next;
    }
    Ok(current)
}

/// Verify a bridge revision, its distinct immutable change set and exact outbox.
fn verify_explicit_candidate_hop(tx:&Transaction<'_>,owner:&str,workspace:&str,candidate:&str,key:&str,revision:i64,previous:Option<i64>,expected:&CandidatePayload,actor:&RevisionActor,mut budget:Option<&mut ResetFenceBudget>)->Result<CandidateRevision,RepositoryError> {
    let (size,evidence):(i64,i64)=tx.query_row("SELECT length(CAST(proposal_json AS BLOB))+length(CAST(reason AS BLOB))+length(CAST(actor_type AS BLOB))+length(CAST(actor_id AS BLOB))+coalesce(length(CAST(source_event_id AS BLOB)),0)+length(CAST(change_set_id AS BLOB))+length(CAST(content_hash AS BLOB)),(SELECT count(*) FROM fm_v2_candidate_evidence e WHERE e.owner_id=r.owner_id AND e.candidate_id=r.candidate_id AND e.revision=r.revision) FROM fm_v2_candidate_revisions r WHERE owner_id=?1 AND candidate_id=?2 AND revision=?3",params![owner,candidate,revision],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
    if !(0..=262144).contains(&size) || evidence!=0 {return Err(RepositoryError::NotFound)}
    if let Some(b)=budget.as_deref_mut(){b.check(size as usize).map_err(RepositoryError::Validation)?;}
    let row=read_candidate(tx,owner,candidate,key,revision,workspace,"")?;
    let expected_value=serde_json::to_value(expected)?;let expected_hash=hash_value(&expected_value)?;
    if row.previous_revision!=previous || serde_json::to_value(&row.payload)?!=expected_value || row.content_hash!=expected_hash || row.actor_type!=actor.actor_type || row.actor_id!=actor.actor_id || row.source_event_id!=actor.source_event_id {return Err(RepositoryError::NotFound)}
    let change:Option<(String,String,String,Option<String>,String,String)>=tx.query_row("SELECT actor_type,actor_id,reason,source_event_id,contract_version,content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,row.change_set_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    if change!=Some((actor.actor_type.clone(),actor.actor_id.clone(),actor.reason.clone(),actor.source_event_id.clone(),CONTRACT_VERSION.into(),expected_hash)) {return Err(RepositoryError::NotFound)}
    let out_len:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_revision' AND source_revision=?4",params![owner,format!("outbox_{}_candidate_revision",row.change_set_id),row.change_set_id,revision],|r|r.get(0)).optional()?;
    let out_len=out_len.filter(|n|(0..=4096).contains(n)).ok_or(RepositoryError::NotFound)?;if let Some(b)=budget.as_deref_mut(){b.check(out_len as usize).map_err(RepositoryError::Validation)?;}
    let out:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,format!("outbox_{}_candidate_revision",row.change_set_id)],|r|r.get(0))?;
    if crate::field_authority::parse_unique_json(&out).map_err(RepositoryError::Validation)?!=json!({"candidate_id":candidate,"revision":revision}) {return Err(RepositoryError::NotFound)}
    Ok(row)
}
/// Verify original creation and selected Proposed head, including the last edit path.
fn verify_selected_explicit_candidate(tx:&Transaction<'_>,owner:&str,workspace:&str,candidate:&str,key:&str,expected_revision:i64,initial_reason:&str,edit_reason:&str,source_event:Option<&str>,view:Option<&super::owner_custody::VerifiedCustodyView<'_,'_>>,mut budget:Option<&mut ResetFenceBudget>)->Result<CandidateRevision,RepositoryError> {
    if expected_revision<1 {return Err(RepositoryError::NotFound)}
    let size_at=|revision:i64|->Result<usize,RepositoryError>{
        let size:Option<i64>=tx.query_row("SELECT length(CAST(proposal_json AS BLOB)) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=?3",params![owner,candidate,revision],|r|r.get(0)).optional()?;
        Ok(size.filter(|n|(0..=262144).contains(n)).ok_or(RepositoryError::NotFound)? as usize)
    };
    let size=size_at(expected_revision)?;if let Some(b)=budget.as_deref_mut(){b.check(size).map_err(RepositoryError::Validation)?;}
    let prior=read_candidate(tx,owner,candidate,key,expected_revision,workspace,"")?;
    if prior.payload.state!=CandidateState::Proposed || prior.payload.accepted_block_id.is_some() || !prior.payload.evidence_ids.is_empty() || prior.actor_type!="user" || !valid_receipt_text(&prior.actor_id) || prior.source_event_id.as_deref()!=source_event || (expected_revision-1)%3!=0 {return Err(RepositoryError::NotFound)}
    let first=if expected_revision==1 {prior.clone()} else {
        let size=size_at(1)?;if let Some(b)=budget.as_deref_mut(){b.check(size).map_err(RepositoryError::Validation)?;}
        read_candidate(tx,owner,candidate,key,1,workspace,"")?
    };
    let original_owner=first.payload.proposal.get("owner").and_then(Value::as_str).filter(|v|valid_receipt_text(v)).ok_or(RepositoryError::NotFound)?;
    if first.payload.state!=CandidateState::Proposed || first.payload.accepted_block_id.is_some() || !first.payload.evidence_ids.is_empty() || first.actor_type!="user" || first.actor_id!=original_owner || first.source_event_id.as_deref()!=source_event || prior.payload.proposal.get("owner").and_then(Value::as_str)!=Some(original_owner) {return Err(RepositoryError::NotFound)}
    if let Some(view)=view {
        if view.current_owner()!=owner {return Err(RepositoryError::NotFound)}
        view.for_captured(tx,original_owner)?;view.for_captured(tx,&prior.actor_id)?;
    } else if original_owner!=owner || prior.actor_id!=owner {return Err(RepositoryError::NotFound)}
    let first_actor=RevisionActor{actor_type:"user".into(),actor_id:original_owner.into(),reason:initial_reason.into(),source_event_id:source_event.map(str::to_owned)};
    verify_explicit_candidate_hop(tx,owner,workspace,candidate,key,1,None,&first.payload,&first_actor,budget.as_deref_mut())?;
    let created:Option<String>=tx.query_row("SELECT created_change_set_id FROM fm_v2_candidates WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND candidate_id=?3 AND idempotency_key=?4",params![owner,workspace,candidate,key],|r|r.get(0)).optional()?;
    if created.as_deref()!=Some(first.change_set_id.as_str()) {return Err(RepositoryError::NotFound)}
    if expected_revision>1 {
        let selected_actor=RevisionActor{actor_type:"user".into(),actor_id:prior.actor_id.clone(),reason:edit_reason.into(),source_event_id:source_event.map(str::to_owned)};
        verify_explicit_candidate_hop(tx,owner,workspace,candidate,key,expected_revision,Some(expected_revision-1),&prior.payload,&selected_actor,budget.as_deref_mut())?;
        let old_revision=expected_revision.checked_sub(3).ok_or(RepositoryError::NotFound)?;
        let old_size=size_at(old_revision)?;if let Some(b)=budget.as_deref_mut(){b.check(old_size).map_err(RepositoryError::Validation)?;}
        let old=read_candidate(tx,owner,candidate,key,old_revision,workspace,"")?;
        if old.payload.state!=CandidateState::Proposed || old.payload.accepted_block_id.is_some() || !old.payload.evidence_ids.is_empty() || old.content_hash!=hash_value(&serde_json::to_value(&old.payload)?)? {return Err(RepositoryError::NotFound)}
        let mut bridge=old.payload.clone();bridge.state=CandidateState::Validating;
        verify_explicit_candidate_hop(tx,owner,workspace,candidate,key,old_revision+1,Some(old_revision),&bridge,&selected_actor,budget.as_deref_mut())?;
        bridge.state=CandidateState::AwaitingReview;
        verify_explicit_candidate_hop(tx,owner,workspace,candidate,key,old_revision+2,Some(old_revision+1),&bridge,&selected_actor,budget.as_deref_mut())?;
        let mut edited=old.payload;edited.proposal=prior.payload.proposal.clone();
        if serde_json::to_value(edited)?!=serde_json::to_value(&prior.payload)? {return Err(RepositoryError::NotFound)}
    }
    Ok(prior)
}

/// Exact Add acceptance path; caller owns account and original receipt authority.
pub(crate) fn verify_explicit_add_activation_chain(tx:&Transaction<'_>,owner:&str,workspace:&str,candidate:&str,key:&str,expected_revision:i64,actor:&RevisionActor,block:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_,'_>>,mut budget:Option<&mut ResetFenceBudget>)->Result<CandidateRevision,RepositoryError> {
    if block.is_empty(){return Err(RepositoryError::NotFound)}
    let prior=verify_selected_explicit_candidate(tx,owner,workspace,candidate,key,expected_revision,"explicit human add","edit explicit human add",None,view,budget.as_deref_mut())?;
    let mut payload=prior.payload.clone();let mut previous=expected_revision;
    for state in [CandidateState::Validating,CandidateState::AwaitingReview,CandidateState::Activated] {
        let revision=previous.checked_add(1).ok_or(RepositoryError::NotFound)?;payload.state=state;
        if state==CandidateState::Activated {payload.accepted_block_id=Some(block.into());}
        let row=verify_explicit_candidate_hop(tx,owner,workspace,candidate,key,revision,Some(previous),&payload,actor,budget.as_deref_mut())?;
        previous=revision;
        if state==CandidateState::Activated {return Ok(row)}
    }
    Err(RepositoryError::NotFound)
}

pub(crate) fn finish_explicit_add(
    tx:&Transaction<'_>, prepared:&PreparedExplicitAdd,
    policy:&PolicyPublicationRefs,
) -> Result<KnowledgeRevision,RepositoryError> {
    let p=&prepared.proposal;
    require_policy_projection(tx,&p.account_id,&p.workspace_id,Some(policy))?;
    let (current,head)=current_explicit_add_proposal(tx,&p.owner,&p.workspace_id,&p.candidate_id)?;
    if current!=*p || head!=prepared.candidate_revision {return Err(RepositoryError::Validation("explicit Add proposal changed".into()));}
    let context=if p.category=="unknown" {checked_question_context(tx,p)?} else {None};
    let record=&prepared.record;
    let source_event_id=format!("memory:{}",record.id);
    let provenance=json!({"action":"explicit_add","source":p.source,"source_type":"human","reviewed_by":p.owner});
    let typed=TypedPublication{owner:&p.owner,workspace_id:&p.workspace_id,category:&p.category,text:&p.text,request_key:&p.request_key,record,context,reason:"explicit human add",source_type:"human",source_event_id:&source_event_id,provenance};
    let TypedPublicationResult{identity,source_id,source_event_id,evidence_id,operation,paths,revision,actor,now}=publish_typed_core(tx,&typed)?;
    let question=p.category=="unknown";
    if question {
        write_explicit_question_review_receipt(tx,&p.owner,&p.workspace_id,&p.account_id,&p.candidate_id,&p.request_key,&p.input_sha256,&p.stored_input_sha256,&record.id,&serde_json::to_value(p)?,&revision)?;
    } else {
        let prepared_fact=PreparedCandidateFact {identity:identity.clone(),source_id:source_id.clone(),source_event_id:source_event_id.clone(),evidence_id:evidence_id.clone(),curated_id:record.id.clone(),record:record.clone(),projection:operation.clone()};
        let request=AccountCandidateReviewRequest {version:1,owner:p.owner.clone(),workspace_id:p.workspace_id.clone(),account_id:p.account_id.clone(),candidate_id:p.candidate_id.clone(),candidate_digest:prepared.candidate_digest.clone(),expected_v2_head:0,selected_paths:paths.clone(),request_key:p.request_key.clone(),reason:"explicit human add".into(),projection:operation.clone(),embedding:None,policy_projection:policy.clone()};
        write_account_candidate_receipt(tx,&request,&prepared_fact,&revision)?;
    }
    let existing=read_candidate(tx,&p.owner,&p.candidate_id,&p.request_key,head,&p.workspace_id,"")?;
    if existing.payload.state!=CandidateState::Proposed || existing.payload.proposal!=serde_json::to_value(p)? {return Err(RepositoryError::Validation("explicit Add proposal changed".into()));}
    let prior_payload=existing.payload.clone();let mut activated=existing.payload;
    activated.state=CandidateState::Activated;
    activated.accepted_block_id=Some(revision.identity.block_id.clone());
    advance_explicit_candidate(tx,&p.owner,&p.candidate_id,head,&prior_payload,&activated,&actor,&now)?;
    Ok(revision)
}

/// Private review snapshot; the proposal is returned only to the authenticated
/// existing S03 owner, never written into an operation receipt.
pub(crate) struct ExplicitAddReviewBinding {
    pub proposal: ExplicitAddProposal,
    pub expected_revision: i64,
    pub legacy_snapshot_digest: String,
    pub binding_sha256: String,
}

pub(crate) fn explicit_add_review_binding(
    tx:&Transaction<'_>, owner:&str, workspace:&str, candidate_id:&str,
) -> Result<ExplicitAddReviewBinding,RepositoryError> {
    let (proposal,revision)=current_explicit_add_proposal(tx,owner,workspace,candidate_id)?;
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![candidate_id,owner,workspace],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    let expected_kind=match proposal.category.as_str() {"identity"=>crate::record::MemoryKind::Persona,"preference"=>crate::record::MemoryKind::Instruction,"task"=>crate::record::MemoryKind::Episodic,"unknown"=>crate::record::MemoryKind::Unknown,"fact"|"contact"|"project"|"goal"=>crate::record::MemoryKind::Fact,_=>return Err(RepositoryError::NotFound)};
    if candidate.kind!=expected_kind || candidate.accepted_curated_id.is_some() || candidate.content_hash!=crate::privacy::content_hash(&proposal.text) {return Err(RepositoryError::Validation("explicit Add candidate changed".into()));}
    let legacy_snapshot_digest=crate::provider::native::candidate_snapshot_digest(&candidate).map_err(RepositoryError::Validation)?;
    let binding_sha256=sha256_hex(serde_json::to_string(&json!(["s03.explicit-add-review-binding.v1",proposal,revision,legacy_snapshot_digest]))?.as_bytes());
    Ok(ExplicitAddReviewBinding{proposal,expected_revision:revision,legacy_snapshot_digest,binding_sha256})
}

/// Caller has freshly authenticated the human and passes current policy refs.
/// The supplied proposal is already privacy-filtered and Host-normalized.
/// Both legacy row and v2 head change in this transaction, or neither does.
pub(crate) fn edit_explicit_add_proposal(
    tx:&Transaction<'_>, next:&ExplicitAddProposal, expected_revision:i64,
    expected_binding:&str, policy:&PolicyPublicationRefs,
) -> Result<ExplicitAddReviewBinding,RepositoryError> {
    require_policy_projection(tx,&next.account_id,&next.workspace_id,Some(policy))?;
    let current=explicit_add_review_binding(tx,&next.owner,&next.workspace_id,&next.candidate_id)?;
    if expected_revision<1 || current.expected_revision!=expected_revision || !valid_sha256(expected_binding) || current.binding_sha256!=expected_binding {
        return Err(RepositoryError::Validation("explicit Add review binding changed".into()));
    }
    let prior=&current.proposal;
    if next.version!=prior.version || next.owner!=prior.owner || next.workspace_id!=prior.workspace_id || next.account_id!=prior.account_id || next.request_key!=prior.request_key || next.input_sha256!=prior.input_sha256 || next.candidate_id!=prior.candidate_id {
        return Err(RepositoryError::Validation("explicit Add original binding changed".into()));
    }
    let kind=match next.category.as_str() {"identity"=>"persona","preference"=>"instruction","task"=>"episodic","unknown"=>"unknown","fact"|"contact"|"project"|"goal"=>"fact",_=>return Err(RepositoryError::Validation("explicit Add category unavailable".into()))};
    let now=chrono::Utc::now().to_rfc3339();
    let changed=tx.execute("UPDATE candidates SET content=?1,kind=?2,content_hash=?3,updated_at=?4 WHERE id=?5 AND owner=?6 AND workspace_id=?7 AND status='pending' AND content=?8 AND content_hash=?9 AND accepted_curated_id IS NULL",params![next.text,kind,crate::privacy::content_hash(&next.text),now,next.candidate_id,next.owner,next.workspace_id,prior.text,crate::privacy::content_hash(&prior.text)])?;
    if changed!=1 {return Err(RepositoryError::Validation("explicit Add legacy candidate changed".into()));}
    validate_proposal(tx,next,true)?;
    checked_question_context(tx,next)?;
    let row=read_candidate(tx,&next.owner,&next.candidate_id,&next.request_key,expected_revision,&next.workspace_id,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(prior)? {return Err(RepositoryError::Validation("explicit Add v2 candidate changed".into()));}
    let prior_payload=row.payload.clone();let mut payload=row.payload;
    payload.proposal=serde_json::to_value(next)?;
    let actor=RevisionActor{actor_type:"user".into(),actor_id:next.owner.clone(),reason:"edit explicit human add".into(),source_event_id:None};
    advance_explicit_candidate(tx,&next.owner,&next.candidate_id,expected_revision,&prior_payload,&payload,&actor,&now)?;
    explicit_add_review_binding(tx,&next.owner,&next.workspace_id,&next.candidate_id)
}

/// Consume the exact preview binding within the review transaction; the
/// returned prepared value is private to the authenticated S03 owner.
pub(crate) fn prepare_bound_explicit_add_review(
    tx:&Transaction<'_>, owner:&str, workspace:&str, account:&str, candidate:&str,
    expected_revision:i64, expected_binding:&str, policy:&PolicyPublicationRefs,
) -> Result<PreparedExplicitAdd,RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let current=explicit_add_review_binding(tx,owner,workspace,candidate)?;
    if current.proposal.account_id!=account || current.expected_revision!=expected_revision || !valid_sha256(expected_binding) || current.binding_sha256!=expected_binding {
        return Err(RepositoryError::Validation("explicit Add review binding changed".into()));
    }
    prepare_explicit_add(tx,&current.proposal)
}

/// Content-free exact candidate association for the existing reset classifier.
/// It neither selects deletion nor treats a legacy status as approval proof.
pub(crate) struct ExplicitAddEraseAssociation {
    pub candidate_id:String,
    pub owner:String,
    pub workspace_id:String,
    pub account_id:String,
    pub original_request_key:String,
    pub original_input_sha256:String,
    pub current_revision:i64,
    pub current_proposal_sha256:String,
    pub legacy_status:String,
    pub legacy_snapshot_digest:String,
    pub accepted_curated_id:Option<String>,
}

pub(crate) fn explicit_add_erase_association(
    tx:&Transaction<'_>, owner:&str, workspace:&str, account:&str, candidate_id:&str,
) -> Result<ExplicitAddEraseAssociation,RepositoryError> {
    let (proposal,revision)=current_explicit_add_proposal(tx,owner,workspace,candidate_id)?;
    if proposal.account_id!=account {return Err(RepositoryError::NotFound);}
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=?",params![candidate_id,owner,workspace],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    if candidate.content_hash!=crate::privacy::content_hash(&proposal.text) {return Err(RepositoryError::Validation("explicit Add candidate changed".into()));}
    let legacy_snapshot_digest=crate::provider::native::candidate_snapshot_digest(&candidate).map_err(RepositoryError::Validation)?;
    let current_proposal_sha256=sha256_hex(serde_json::to_string(&serde_json::to_value(&proposal)?)?.as_bytes());
    Ok(ExplicitAddEraseAssociation{candidate_id:candidate_id.into(),owner:owner.into(),workspace_id:workspace.into(),account_id:account.into(),original_request_key:proposal.request_key,original_input_sha256:proposal.input_sha256,current_revision:revision,current_proposal_sha256,legacy_status:format!("{:?}",candidate.status).to_lowercase(),legacy_snapshot_digest,accepted_curated_id:candidate.accepted_curated_id})
}

/// Content-free original human review receipt. It is factual evidence, never
/// a grant to review, publish, inspect, or recreate a mutable proposal.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ExplicitAddReviewReceipt {
    version:String,
    pub owner:String,
    pub workspace:String,
    pub account:String,
    pub candidate:String,
    pub review_key:String,
    pub expected_revision:i64,
    pub expected_binding:String,
    pub action:String,
    pub curated_id:Option<String>,
}

fn valid_receipt_text(value:&str)->bool {
    !value.is_empty() && value==value.trim() && value.len()<=512 && !value.chars().any(char::is_control)
}

pub(crate) fn explicit_add_review_receipt_id(owner:&str, workspace:&str, account:&str, candidate:&str)->Result<String,RepositoryError> {
    if ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) || !candidate.strip_prefix("manualadd_").is_some_and(valid_sha256) {
        return Err(RepositoryError::Validation("explicit Add review identity unavailable".into()));
    }
    Ok(format!("cs_explicit_add_review_{}",sha256_hex(&serde_json::to_vec(&("s03.explicit-add-review.v1",owner,workspace,account,candidate))?)))
}

pub(crate) fn read_explicit_add_review_receipt(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<Option<ExplicitAddReviewReceipt>,RepositoryError> {read_explicit_add_review_receipt_at(tx,owner,owner,workspace,account,candidate)}
pub(crate) fn read_explicit_add_review_receipt_custody(tx:&Transaction<'_>,workspace:&str,account:&str,candidate:&str,captured:&str,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<Option<ExplicitAddReviewReceipt>,RepositoryError> {
    let proof=view.for_captured(tx,captured)?;if proof.account_id()!=account {return Err(RepositoryError::NotFound);}
    read_explicit_add_review_receipt_at(tx,proof.current_owner(),proof.captured_owner(),workspace,account,candidate)
}
fn read_explicit_add_review_receipt_at(
    tx:&Transaction<'_>, sql_owner:&str, owner:&str, workspace:&str, account:&str, candidate:&str,
)->Result<Option<ExplicitAddReviewReceipt>,RepositoryError> {
    let id=explicit_add_review_receipt_id(owner,workspace,account,candidate)?;
    let event_id=format!("outbox_{}_explicit_add_review",id);
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row(
        "SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s03.explicit-add-review.v1'",
        params![sql_owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor_type,actor_id,contract,source_event))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![sql_owner,id,event_id],|r|r.get(0))?;
        return if collision {Err(RepositoryError::NotFound)} else {Ok(None)};
    };
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let receipt:ExplicitAddReviewReceipt=serde_json::from_value(value)?;
    let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='explicit_add_review'",params![sql_owner,event_id,id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if hash!=sha256_hex(raw.as_bytes()) || actor_type!="user" || actor_id!=owner || contract!=CONTRACT_VERSION || source_event.is_some()
        || receipt.version!="s03.explicit-add-review.v1" || receipt.owner!=owner || receipt.workspace!=workspace || receipt.account!=account || receipt.candidate!=candidate
        || !valid_receipt_text(&receipt.review_key) || receipt.expected_revision<1 || !valid_sha256(&receipt.expected_binding)
        || !match (receipt.action.as_str(),receipt.curated_id.as_deref()) {("accept",Some(id))=>valid_receipt_text(id),("reject",None)=>true,_=>false}
        || event.as_ref().is_none_or(|(payload,revision)|payload!=&raw || *revision!=0) {
        return Err(RepositoryError::NotFound);
    }
    Ok(Some(receipt))
}

/// Caller has already admitted the exact human and verified original proposal,
/// review binding, policy, and actual publication in this same transaction.
pub(crate) fn write_explicit_add_review_receipt(
    tx:&Transaction<'_>, owner:&str, workspace:&str, account:&str, candidate:&str,
    review_key:&str, expected_revision:i64, expected_binding:&str, action:&str, curated_id:Option<&str>,
)->Result<String,RepositoryError> {
    let id=explicit_add_review_receipt_id(owner,workspace,account,candidate)?;
    if !valid_receipt_text(review_key) || expected_revision<1 || !valid_sha256(expected_binding)
        || !match (action,curated_id) {("accept",Some(id))=>valid_receipt_text(id),("reject",None)=>true,_=>false} {
        return Err(RepositoryError::Validation("explicit Add review receipt unavailable".into()));
    }
    if read_explicit_add_review_receipt(tx,owner,workspace,account,candidate)?.is_some() {
        return Err(RepositoryError::Validation("explicit Add review already issued; status only".into()));
    }
    let receipt=ExplicitAddReviewReceipt{version:"s03.explicit-add-review.v1".into(),owner:owner.into(),workspace:workspace.into(),account:account.into(),candidate:candidate.into(),review_key:review_key.into(),expected_revision,expected_binding:expected_binding.into(),action:action.into(),curated_id:curated_id.map(str::to_owned)};
    let raw=serde_json::to_string(&receipt)?;
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s03.explicit-add-review.v1',NULL,?,?,?,?)",params![owner,id,owner,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,'explicit_add_review',?,0,?)",params![owner,format!("outbox_{}_explicit_add_review",id),id,raw,now])?;
    Ok(id)
}

/// Reject only the exact currently reviewed pending proposal. Caller must
/// write the original action receipt in this same transaction and roll back
/// all changes on any error. This transition never creates a curated item.
pub(crate) fn reject_explicit_add_proposal(
    tx:&Transaction<'_>, owner:&str, workspace:&str, account:&str, candidate:&str,
    expected_revision:i64, expected_binding:&str, policy:&PolicyPublicationRefs,
)->Result<(),RepositoryError> {
    require_policy_projection(tx,account,workspace,Some(policy))?;
    let current=explicit_add_review_binding(tx,owner,workspace,candidate)?;
    if current.proposal.account_id!=account || current.expected_revision!=expected_revision || expected_revision<1 || !valid_sha256(expected_binding) || current.binding_sha256!=expected_binding {
        return Err(RepositoryError::Validation("explicit Add review binding changed".into()));
    }
    let row=read_candidate(tx,owner,candidate,&current.proposal.request_key,expected_revision,workspace,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(&current.proposal)? || row.payload.accepted_block_id.is_some() {
        return Err(RepositoryError::Validation("explicit Add candidate changed".into()));
    }
    let now=chrono::Utc::now().to_rfc3339();
    if tx.execute("UPDATE candidates SET status='rejected',reason='explicit human rejection',updated_at=?1 WHERE id=?2 AND owner=?3 AND workspace_id=?4 AND status='pending' AND accepted_curated_id IS NULL AND content=?5 AND content_hash=?6",params![now,candidate,owner,workspace,current.proposal.text,crate::privacy::content_hash(&current.proposal.text)])?!=1 {
        return Err(RepositoryError::Validation("explicit Add legacy candidate changed".into()));
    }
    let prior_payload=row.payload.clone();let mut payload=row.payload;
    payload.state=CandidateState::Rejected;
    payload.reason="explicit human rejection".into();
    let actor=RevisionActor{actor_type:"user".into(),actor_id:owner.into(),reason:"explicit human rejection".into(),source_event_id:None};
    advance_explicit_candidate(tx,owner,candidate,expected_revision,&prior_payload,&payload,&actor,&now)?;
    Ok(())
}

/// Deny-only per-candidate fact committed with an original guarded reset.
/// Hashes are selected by the reset owner from its validated original records;
/// no field in this marker grants access or recreates an Add/review action.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ExplicitAddTerminalMarker {
    pub version:String,
    pub mode:String,
    pub owner:String,
    pub workspace_id:String,
    pub reset_workspace_id:String,
    pub account_id:String,
    pub candidate_id:String,
    pub input_sha256:String,
    pub original_key_sha256:String,
    pub review_key_sha256:Option<String>,
    pub review_action:Option<String>,
    pub review_revision:Option<i64>,
    pub review_binding:Option<String>,
    pub operation_id:String,
    pub partition_sha256:Option<String>,
    pub reset_receipt_id:String,
}

pub(crate) fn explicit_add_key_sha256(account:&str,workspace:&str,key:&str)->Result<String,RepositoryError> {
    if ![account,workspace,key].iter().all(|v|valid_receipt_text(v)) {return Err(RepositoryError::NotFound);}
    Ok(sha256_hex(&serde_json::to_vec(&("s04.explicit-add-key.v1",account,workspace,key))?))
}

pub(crate) fn explicit_add_review_key_sha256(account:&str,workspace:&str,candidate:&str,key:&str)->Result<String,RepositoryError> {
    if ![account,workspace,key].iter().all(|v|valid_receipt_text(v)) || !candidate.strip_prefix("manualadd_").is_some_and(valid_sha256) {return Err(RepositoryError::NotFound);}
    Ok(sha256_hex(&serde_json::to_vec(&("s04.explicit-add-review-key.v1",account,workspace,candidate,key))?))
}

fn explicit_add_terminal_event_id(owner:&str,candidate:&str)->Result<String,RepositoryError> {
    Ok(format!("outbox_s04_explicit_add_erased_{}",sha256_hex(&serde_json::to_vec(&(owner,candidate))?)))
}

fn verify_explicit_add_terminal_reset(tx:&Transaction<'_>,m:&ExplicitAddTerminalMarker)->Result<(),RepositoryError> {verify_explicit_add_terminal_reset_at(tx,&m.owner,m)}
fn verify_explicit_add_terminal_reset_at(tx:&Transaction<'_>,sql_owner:&str,m:&ExplicitAddTerminalMarker)->Result<(),RepositoryError> {
    if m.version!="s04.explicit-add-erased.v2" || ![m.owner.as_str(),m.workspace_id.as_str(),m.reset_workspace_id.as_str(),m.account_id.as_str(),m.operation_id.as_str()].iter().all(|v|valid_receipt_text(v))
        || !m.candidate_id.strip_prefix("manualadd_").is_some_and(valid_sha256) || !valid_sha256(&m.input_sha256) || !valid_sha256(&m.original_key_sha256)
        || m.reset_receipt_id!=ingest_erased_receipt_id(&m.account_id,&m.operation_id)? {
        return Err(RepositoryError::NotFound);
    }
    let review_ok=match (&m.review_key_sha256,&m.review_action,m.review_revision,&m.review_binding) {
        (None,None,None,None)=>true,
        (Some(key),Some(action),Some(revision),Some(binding))=>valid_sha256(key) && matches!(action.as_str(),"accept"|"reject") && revision>0 && valid_sha256(binding),
        _=>false,
    };
    if !review_ok || !match (m.mode.as_str(),&m.partition_sha256) {("full",None)=>true,("selective",Some(hash))=>valid_sha256(hash),_=>false} {return Err(RepositoryError::NotFound);}
    let (hash,raw,actor_type,actor_id,contract,source_event):(String,String,String,String,String,Option<String>)=tx.query_row(
        "SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",
        params![sql_owner,m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?.ok_or(RepositoryError::NotFound)?;
    let receipt=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor_type!="user" || actor_id!=m.owner || contract!=CONTRACT_VERSION || source_event.is_some()
        || receipt.as_object().map(|v|v.len())!=Some(4) || receipt["version"]!="s06.guarded-reset.v1"
        || receipt["request_sha256"]!=sha256_hex(&serde_json::to_vec(&receipt["request"])? )
        || receipt["request"]["owner"]!=m.owner || receipt["request"]["workspace_id"]!=m.reset_workspace_id
        || receipt["request"]["account_id"]!=m.account_id || receipt["request"]["operation_id"]!=m.operation_id
        || receipt["result"]["complete"]!=true {return Err(RepositoryError::NotFound);}
    let components=receipt["request"]["components"].as_array().ok_or(RepositoryError::NotFound)?;
    let has=|name:&str|components.iter().any(|v|v.as_str()==Some(name));
    match m.mode.as_str() {
        "full" if has("memories") && receipt["request"].get("selective_manifest_sha256").is_none() && receipt["result"]["categories"]["memories"]["state"]=="complete" => (),
        "selective" if has("ingest") && receipt["request"]["selective_manifest_sha256"].as_str().is_some_and(valid_sha256)
            && receipt["result"]["categories"]["ingest"]["state"]=="complete"
            && receipt["result"]["categories"]["ingest"]["selective"]["partition_sha256"].as_str()==m.partition_sha256.as_deref() => (),
        _=>return Err(RepositoryError::NotFound),
    }
    let outbox:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='s06_guarded_reset'",params![sql_owner,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id),m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if outbox.as_ref().is_none_or(|(payload,revision)|payload!=&raw || *revision!=0) {return Err(RepositoryError::NotFound);}
    Ok(())
}

/// Caller has already validated the original Add/review receipt and exact
/// reset selection. Original reset receipt and outbox must already exist in tx.
pub(crate) fn write_explicit_add_terminal(tx:&Transaction<'_>,m:&ExplicitAddTerminalMarker)->Result<(),RepositoryError> {
    verify_explicit_add_terminal_reset(tx,m)?;
    let event=explicit_add_terminal_event_id(&m.owner,&m.candidate_id)?;
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=? AND event_id=?)",params![m.owner,event],|r|r.get(0))?;
    if exists {return Err(RepositoryError::Validation("explicit Add terminal already exists".into()));}
    let raw=serde_json::to_string(m)?;
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,'s04_explicit_add_erased',?,0,?)",params![m.owner,event,m.reset_receipt_id,raw,now])?;
    Ok(())
}

/// Factual terminal lookup only. Caller separately authenticates current
/// account and compares the original request/input hashes before disclosure.
pub(crate) fn read_explicit_add_terminal(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<Option<ExplicitAddTerminalMarker>,RepositoryError> {read_explicit_add_terminal_at(tx,owner,owner,workspace,account,candidate)}
pub(crate) fn read_explicit_add_terminal_custody(tx:&Transaction<'_>,workspace:&str,account:&str,candidate:&str,captured:&str,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<Option<ExplicitAddTerminalMarker>,RepositoryError> {
    let proof=view.for_captured(tx,captured)?;if proof.account_id()!=account {return Err(RepositoryError::NotFound);}
    read_explicit_add_terminal_at(tx,proof.current_owner(),proof.captured_owner(),workspace,account,candidate)
}
fn read_explicit_add_terminal_at(tx:&Transaction<'_>,sql_owner:&str,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<Option<ExplicitAddTerminalMarker>,RepositoryError> {
    if ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) || !candidate.strip_prefix("manualadd_").is_some_and(valid_sha256) {return Err(RepositoryError::NotFound);}
    let event=explicit_add_terminal_event_id(owner,candidate)?;
    let row:Option<(String,String,String,i64)>=tx.query_row("SELECT change_set_id,kind,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=?",params![sql_owner,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let Some((receipt_id,kind,raw,revision))=row else {return Ok(None)};
    if kind!="s04_explicit_add_erased" || revision!=0 || raw.len()>8192 {return Err(RepositoryError::NotFound);}
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let marker:ExplicitAddTerminalMarker=serde_json::from_value(value)?;
    if marker.owner!=owner || marker.workspace_id!=workspace || marker.account_id!=account || marker.candidate_id!=candidate || marker.reset_receipt_id!=receipt_id {return Err(RepositoryError::NotFound);}
    verify_explicit_add_terminal_reset_at(tx,sql_owner,&marker)?;
    Ok(Some(marker))
}

/// Owner-indexed receipt discovery is bounded by examined rows as well as
/// returned rows. Cursors are event IDs in the corresponding prefix domain;
/// they are selectors, never account or content authority.
fn explicit_add_missing_page(
    tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,after:Option<&str>,limit:u32,terminal:bool,
)->Result<Value,RepositoryError> {
    require_guarded_epoch(tx)?;
    if ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) || !(1..=20).contains(&limit) {return Err(RepositoryError::NotFound);}
    let prefix=if terminal {"outbox_s04_explicit_add_erased_"} else {"outbox_cs_explicit_add_review_"};
    let suffix=if terminal {""} else {"_explicit_add_review"};
    if after.is_some_and(|cursor|cursor.strip_prefix(prefix).and_then(|tail|tail.strip_suffix(suffix)).is_none_or(|digest|!valid_sha256(digest))) {
        return Err(RepositoryError::Validation("explicit Add page cursor unavailable".into()));
    }
    // The outbox primary key is (owner_id,event_id); the bounded range is
    // index-ordered and does not materialize the owner's other events.
    let mut budget=ResetFenceBudget::install_bounded(tx,std::time::Duration::from_secs(1));
    let upper=format!("{}~",prefix);
    let mut stmt=tx.prepare("SELECT event_id,kind,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND event_id<?3 ORDER BY event_id LIMIT 65")?;
    let rows=stmt.query_map(params![owner,after.unwrap_or(prefix),upper],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,i64>(3)?)))?.collect::<Result<Vec<_>,_>>()?;
    drop(stmt);
    let mut items=Vec::new();
    let mut examined=0usize;
    let mut last=None;
    for (event,kind,raw,revision) in &rows {
        if examined>=64 || items.len()>=limit as usize {break;}
        examined+=1;
        last=Some(event.clone());
        budget.check(raw.len().saturating_add(event.len()).saturating_add(1)).map_err(|_|RepositoryError::NotFound)?;
        if raw.len()>8192 || *revision!=0 || event.strip_prefix(prefix).and_then(|tail|tail.strip_suffix(suffix)).is_none_or(|digest|!valid_sha256(digest)) {return Err(RepositoryError::NotFound);}
        let value=crate::field_authority::parse_unique_json(raw).map_err(RepositoryError::Validation)?;
        if terminal {
            if kind!="s04_explicit_add_erased" {return Err(RepositoryError::NotFound);}
            let marker:ExplicitAddTerminalMarker=serde_json::from_value(value)?;
            if marker.owner!=owner || explicit_add_terminal_event_id(owner,&marker.candidate_id)?!=*event {return Err(RepositoryError::NotFound);}
            if marker.workspace_id!=workspace || marker.account_id!=account {continue;}
            let verified=read_explicit_add_terminal(tx,owner,workspace,account,&marker.candidate_id)?.ok_or(RepositoryError::NotFound)?;
            if verified!=marker {return Err(RepositoryError::NotFound);}
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1)",[&marker.candidate_id],|r|r.get(0))?;
            if !exists {items.push(json!({"candidate_id":marker.candidate_id,"state":"terminal_erased"}));}
        } else {
            if kind!="explicit_add_review" {return Err(RepositoryError::NotFound);}
            let receipt:ExplicitAddReviewReceipt=serde_json::from_value(value)?;
            if receipt.owner!=owner || format!("outbox_{}_explicit_add_review",explicit_add_review_receipt_id(owner,&receipt.workspace,&receipt.account,&receipt.candidate)?)!=*event {return Err(RepositoryError::NotFound);}
            if receipt.workspace!=workspace || receipt.account!=account {continue;}
            let verified=read_explicit_add_review_receipt(tx,owner,workspace,account,&receipt.candidate)?.ok_or(RepositoryError::NotFound)?;
            if verified!=receipt {return Err(RepositoryError::NotFound);}
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1)",[&receipt.candidate],|r|r.get(0))?;
            if !exists {
                let state=if receipt.action=="accept" {"applied"} else {"rejected"};
                items.push(json!({"candidate_id":receipt.candidate,"original":{"candidate_id":receipt.candidate,"expected_revision":receipt.expected_revision,"expected_binding":receipt.expected_binding,"review_key":receipt.review_key,"action":receipt.action},"receipt":{"state":state,"candidate_id":receipt.candidate,"curated_id":receipt.curated_id}}));
            }
        }
    }
    budget.check(0).map_err(|_|RepositoryError::NotFound)?;
    let more=examined<rows.len();
    let next=if more {last} else {None};
    if more && next.is_none() {return Err(RepositoryError::NotFound);}
    Ok(json!({"version":1,"items":items,"next_cursor":next,"partial":more}))
}

pub(crate) fn explicit_add_missing_original_page(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,after:Option<&str>,limit:u32)->Result<Value,RepositoryError> {
    explicit_add_missing_page(tx,owner,workspace,account,after,limit,false)
}
pub(crate) fn explicit_add_missing_terminal_page(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,after:Option<&str>,limit:u32)->Result<Value,RepositoryError> {
    explicit_add_missing_page(tx,owner,workspace,account,after,limit,true)
}

/// Account-scoped missing receipt discovery under the caller's already
/// verified same-transaction custody and single progress budget.
fn explicit_add_missing_page_custody(tx:&Transaction<'_>,workspace:&str,account:&str,after:Option<&str>,limit:u32,terminal:bool,view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    require_guarded_epoch(tx)?;let owner=view.current_owner();let proof=view.for_captured(tx,owner)?;
    if proof.account_id()!=account || ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) || !(1..=20).contains(&limit) {return Err(RepositoryError::NotFound);}
    let prefix=if terminal {"outbox_s04_explicit_add_erased_"} else {"outbox_cs_explicit_add_review_"};let suffix=if terminal {""} else {"_explicit_add_review"};
    if after.is_some_and(|v|v.strip_prefix(prefix).and_then(|v|v.strip_suffix(suffix)).is_none_or(|v|!valid_sha256(v))) {return Err(RepositoryError::Validation("explicit Add page cursor unavailable".into()));}
    let upper=format!("{prefix}~");
    let mut stmt=tx.prepare("SELECT event_id,kind,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND event_id<?3 ORDER BY event_id LIMIT 65")?;
    let mut rows=stmt.query(params![owner,after.unwrap_or(prefix),upper])?;let mut items=Vec::new();let mut examined=0usize;let mut last=None;let mut more=false;
    while let Some(row)=rows.next()? {
        if examined>=64 || items.len()>=limit as usize {more=true;break;}
        let event=row.get_ref(0)?.as_str().map_err(|_|RepositoryError::NotFound)?;let kind=row.get_ref(1)?.as_str().map_err(|_|RepositoryError::NotFound)?;let raw=row.get_ref(2)?.as_str().map_err(|_|RepositoryError::NotFound)?;let revision:i64=row.get(3)?;
        budget.check(event.len().saturating_add(kind.len()).saturating_add(raw.len()).saturating_add(1)).map_err(|_|RepositoryError::NotFound)?;
        if revision!=0 || event.strip_prefix(prefix).and_then(|v|v.strip_suffix(suffix)).is_none_or(|v|!valid_sha256(v)) {return Err(RepositoryError::NotFound);}
        let value=crate::field_authority::parse_unique_json(raw).map_err(RepositoryError::Validation)?;examined+=1;last=Some(event.to_owned());
        if terminal {
            if kind!="s04_explicit_add_erased" {return Err(RepositoryError::NotFound);}
            let marker:ExplicitAddTerminalMarker=serde_json::from_value(value)?;
            if explicit_add_terminal_event_id(&marker.owner,&marker.candidate_id)?!=event {return Err(RepositoryError::NotFound);}
            if marker.workspace_id!=workspace || marker.account_id!=account {continue;}
            view.for_captured(tx,&marker.owner)?;
            let sizes:(i64,i64)=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s06_guarded_reset' WHERE c.owner_id=?1 AND c.change_set_id=?2",params![owner,marker.reset_receipt_id,format!("outbox_{}_s06_guarded_reset",marker.reset_receipt_id)],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
            if !(0..=262144).contains(&sizes.0) || !(0..=262144).contains(&sizes.1) {return Err(RepositoryError::NotFound);}budget.check((sizes.0 as usize).saturating_add(sizes.1 as usize)).map_err(|_|RepositoryError::NotFound)?;
            let verified=read_explicit_add_terminal_custody(tx,workspace,account,&marker.candidate_id,&marker.owner,view)?.ok_or(RepositoryError::NotFound)?;
            if verified!=marker {return Err(RepositoryError::NotFound);}
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1)",[&marker.candidate_id],|r|r.get(0))?;
            if !exists {items.push(json!({"candidate_id":marker.candidate_id,"state":"terminal_erased"}));}
        } else {
            if kind!="explicit_add_review" {return Err(RepositoryError::NotFound);}
            let receipt:ExplicitAddReviewReceipt=serde_json::from_value(value)?;
            if format!("outbox_{}_explicit_add_review",explicit_add_review_receipt_id(&receipt.owner,&receipt.workspace,&receipt.account,&receipt.candidate)?)!=event {return Err(RepositoryError::NotFound);}
            if receipt.workspace!=workspace || receipt.account!=account {continue;}
            view.for_captured(tx,&receipt.owner)?;
            let change=explicit_add_review_receipt_id(&receipt.owner,workspace,account,&receipt.candidate)?;
            let size:i64=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,change],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
            if !(0..=8192).contains(&size) {return Err(RepositoryError::NotFound);}budget.check(size as usize).map_err(|_|RepositoryError::NotFound)?;
            let verified=read_explicit_add_review_receipt_custody(tx,workspace,account,&receipt.candidate,&receipt.owner,view)?.ok_or(RepositoryError::NotFound)?;
            if verified!=receipt {return Err(RepositoryError::NotFound);}
            for captured in view.owners() {
                budget.check(captured.len()).map_err(|_|RepositoryError::NotFound)?;
                let event_id=explicit_add_terminal_event_id(captured,&receipt.candidate)?;
                let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2)",params![owner,event_id],|r|r.get(0))?;
                if exists {return Err(RepositoryError::NotFound);}
            }
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1)",[&receipt.candidate],|r|r.get(0))?;
            if !exists {let state=if receipt.action=="accept" {"applied"} else {"rejected"};items.push(json!({"candidate_id":receipt.candidate,"original":{"candidate_id":receipt.candidate,"expected_revision":receipt.expected_revision,"expected_binding":receipt.expected_binding,"review_key":receipt.review_key,"action":receipt.action},"receipt":{"state":state,"candidate_id":receipt.candidate,"curated_id":receipt.curated_id}}));}
        }
    }
    drop(rows);drop(stmt);budget.check(0).map_err(|_|RepositoryError::NotFound)?;
    let next=if more {last} else {None};if more && next.is_none() {return Err(RepositoryError::NotFound);}Ok(json!({"version":1,"items":items,"next_cursor":next,"partial":more}))
}
pub(crate) fn explicit_add_missing_original_page_custody(tx:&Transaction<'_>,workspace:&str,account:&str,after:Option<&str>,limit:u32,view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    explicit_add_missing_page_custody(tx,workspace,account,after,limit,false,view,budget)
}
pub(crate) fn explicit_add_missing_terminal_page_custody(tx:&Transaction<'_>,workspace:&str,account:&str,after:Option<&str>,limit:u32,view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    explicit_add_missing_page_custody(tx,workspace,account,after,limit,true,view,budget)
}

/// Validate already-reviewed import data. These subject and entity hints are
/// preserved as data and never confer a principal/entity grant.
fn checked_import_proposal(tx:&Transaction<'_>,owner:&str,workspace:&str,value:&Value)->Result<Value,RepositoryError> {
    checked_import_proposal_at(tx,owner,owner,workspace,value)
}
fn checked_import_proposal_at(tx:&Transaction<'_>,sql_owner:&str,owner:&str,workspace:&str,value:&Value)->Result<Value,RepositoryError> {
    let obj=value.as_object().ok_or(RepositoryError::NotFound)?;
    let allowed=["text","category","question_context","entity_match_candidates","subject_attribution","subject_role","document_role"];
    if obj.keys().any(|key|!allowed.contains(&key.as_str())) {return Err(RepositoryError::NotFound);}
    let text=obj.get("text").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    let category=obj.get("category").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    if text.is_empty() || text.trim()!=text || text.chars().count()>5000 || !matches!(category,"fact"|"contact"|"task"|"preference"|"identity"|"project"|"goal"|"unknown") || (category=="unknown" && crate::record::normalize_question(text)!=text) {return Err(RepositoryError::NotFound);}
    if let Some(context)=obj.get("question_context") {checked_question_context_at(tx,sql_owner,owner,workspace,category,Some(context))?;}
    let no_c0=|v:&str| !v.chars().any(|c|(c as u32)<32 || c=='\u{7f}');
    let roles=["assistant_self","handler","named_external","joint","instruction","unknown"];
    let documents=["assistant_identity","handler_profile","assistant_instructions","mixed_memory","generic_document"];
    for (key,allowed) in [("subject_role",roles.as_slice()),("document_role",documents.as_slice())] {
        if let Some(value)=obj.get(key) {if !value.as_str().is_some_and(|v|allowed.contains(&v)) {return Err(RepositoryError::NotFound);}}
    }
    if let Some(candidates)=obj.get("entity_match_candidates") {
        let candidates=candidates.as_array().ok_or(RepositoryError::NotFound)?;
        if candidates.is_empty() || candidates.len()>8 {return Err(RepositoryError::NotFound);}
        let mut seen=std::collections::BTreeSet::new();
        for item in candidates {
            let entry=item.as_object().ok_or(RepositoryError::NotFound)?;
            let keys=["contract","entity_id","role","matched_alias","match_method","state","requires_review"];
            let entity=item["entity_id"].as_str().ok_or(RepositoryError::NotFound)?;
            let alias=item["matched_alias"].as_str().ok_or(RepositoryError::NotFound)?;
            if entry.len()!=keys.len() || keys.iter().any(|key|!entry.contains_key(*key)) || item["contract"]!="openclank.memory-entity-match-candidate/v1" || item["role"]!="assistant_self" || item["match_method"]!="persona_setting_exact" || item["state"]!="proposed" || item["requires_review"]!=true || !entity.strip_prefix("principal_assistant_").is_some_and(|v|v.len()==32 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))) || alias.trim().is_empty() || alias.chars().count()>120 || !no_c0(alias) || !seen.insert((entity,alias)) {return Err(RepositoryError::NotFound);}
        }
    }
    if let Some(attribution)=obj.get("subject_attribution") {
        let entry=attribution.as_object().ok_or(RepositoryError::NotFound)?;
        let keys=["contract","role","entity_id","document_role","method","state","requires_review","section","source_evidence_hash"];
        let role=attribution["role"].as_str().ok_or(RepositoryError::NotFound)?;
        let entity=attribution["entity_id"].as_str().ok_or(RepositoryError::NotFound)?;
        let prefix=match role {"handler"=>"principal_handler_","assistant_self"=>"principal_assistant_",_=>return Err(RepositoryError::NotFound)};
        let section=attribution["section"].as_str().ok_or(RepositoryError::NotFound)?;
        let document=attribution["document_role"].as_str().ok_or(RepositoryError::NotFound)?;
        let method=attribution["method"].as_str().ok_or(RepositoryError::NotFound)?;
        if entry.len()!=keys.len() || keys.iter().any(|key|!entry.contains_key(*key)) || attribution["contract"]!="openclank.memory-subject-attribution/v1" || attribution["state"]!="proposed" || attribution["requires_review"]!=true || !entity.strip_prefix(prefix).is_some_and(|v|v.len()==32 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))) || !documents.contains(&document) || !matches!(method,"document_profile"|"document_policy"|"markdown_context"|"grounded_model"|"persona_alias"|"unresolved") || section.chars().count()>120 || !no_c0(section) || !attribution["source_evidence_hash"].as_str().is_some_and(valid_sha256) || obj.get("subject_role").and_then(Value::as_str)!=Some(role) || obj.get("document_role").and_then(Value::as_str)!=Some(document) {return Err(RepositoryError::NotFound);}
    }
    Ok(value.clone())
}

/// Content-free original import publication fact. Current policy and the
/// exact import origin are independently admitted by the owning transaction.
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportReviewReceipt {
    pub version:String, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub batch_id:String, pub item_id:String, pub suggestion_id:String,
    pub review_key_hash:String, pub proposal_fingerprint:String, pub stored_proposal_sha256:String, pub stored_provenance_sha256:String,
    pub candidate_id:String, pub source_event_id:String, pub mode:String,
    pub publication_kind:String, pub publication_id:String,
}

fn import_review_ids(s:&crate::store::sqlite::selective_import::ImportReviewSelector)->Result<(String,String,String),RepositoryError> {
    if s.version!=1 || ![s.owner.as_str(),s.workspace_id.as_str(),s.account_id.as_str(),s.batch_id.as_str(),s.item_id.as_str(),s.suggestion_id.as_str()].iter().all(|v|valid_receipt_text(v)) || !valid_sha256(&s.review_key_hash) || !valid_sha256(&s.proposal_fingerprint) {return Err(RepositoryError::NotFound);}
    let digest=sha256_hex(&serde_json::to_vec(&("s03.import-review-id.v1",&s.account_id,&s.workspace_id,&s.batch_id,&s.item_id,&s.suggestion_id,&s.review_key_hash))?);
    Ok((format!("importreview_{digest}"),format!("cs_import_review_{digest}"),format!("memory-import-review:{}:{}",s.batch_id,s.suggestion_id)))
}

/// Factual original lookup, independent of current mode and policy inputs.
/// Caller must authenticate/re-authorize the exact current account separately.
pub(crate) fn read_import_review_receipt(tx:&Transaction<'_>,s:&crate::store::sqlite::selective_import::ImportReviewSelector)->Result<Option<ImportReviewReceipt>,RepositoryError> {read_import_review_receipt_at(tx,&s.owner,s)}
pub(crate) fn read_import_review_receipt_custody(tx:&Transaction<'_>,s:&crate::store::sqlite::selective_import::ImportReviewSelector,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<Option<ImportReviewReceipt>,RepositoryError> {
    let proof=view.for_captured(tx,&s.owner)?;if proof.account_id()!=s.account_id {return Err(RepositoryError::NotFound);}
    read_import_review_receipt_at(tx,proof.current_owner(),s)
}
fn read_import_review_receipt_at(tx:&Transaction<'_>,sql_owner:&str,s:&crate::store::sqlite::selective_import::ImportReviewSelector)->Result<Option<ImportReviewReceipt>,RepositoryError> {
    let (candidate,id,event_source)=import_review_ids(s)?;
    let event=format!("outbox_{}_import_review",id);
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s03.import-review.v1'",params![sql_owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor_type,actor_id,contract,source_event))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![sql_owner,id,event],|r|r.get(0))?;
        return if collision {Err(RepositoryError::NotFound)} else {Ok(None)};
    };
    if raw.len()>8192 {return Err(RepositoryError::NotFound);}
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let receipt:ImportReviewReceipt=serde_json::from_value(value)?;
    let outbox:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='import_review'",params![sql_owner,event,id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if hash!=sha256_hex(raw.as_bytes()) || actor_type!="user" || actor_id!=s.owner || contract!=CONTRACT_VERSION || source_event.as_deref()!=Some(event_source.as_str())
        || receipt.version!="s03.import-review.v1" || receipt.owner!=s.owner || receipt.workspace_id!=s.workspace_id || receipt.account_id!=s.account_id
        || receipt.batch_id!=s.batch_id || receipt.item_id!=s.item_id || receipt.suggestion_id!=s.suggestion_id || receipt.review_key_hash!=s.review_key_hash || receipt.proposal_fingerprint!=s.proposal_fingerprint
        || receipt.candidate_id!=candidate || receipt.source_event_id!=event_source || !valid_sha256(&receipt.stored_proposal_sha256) || !valid_sha256(&receipt.stored_provenance_sha256)
        || !match (receipt.mode.as_str(),receipt.publication_kind.as_str()) {("review_only","candidate")=>receipt.publication_id==candidate,("manual","memory")=>valid_receipt_text(&receipt.publication_id),_=>false}
        || outbox.as_ref().is_none_or(|(payload,revision)|payload!=&raw || *revision!=0) {return Err(RepositoryError::NotFound);}
    Ok(Some(receipt))
}

/// Private filtered staged import data. Initial identities/hashes never change;
/// the current proposal/hash may advance only through the import review owner.
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportReviewProposal {
    pub version:String, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub batch_id:String, pub item_id:String, pub suggestion_id:String,
    pub review_key_hash:String, pub proposal_fingerprint:String,
    pub initial_stored_proposal_sha256:String, pub stored_proposal_sha256:String,
    pub stored_provenance_sha256:String, pub candidate_id:String, pub source_event_id:String,
    pub proposal:Value, pub provenance:Value,
}

/// Caller must separately verify the finalized Host import decision and current
/// named-human policy. This reads only an exact still-pending staged candidate.
pub(crate) fn current_import_review_proposal(tx:&Transaction<'_>,s:&crate::store::sqlite::selective_import::ImportReviewSelector,original:&ImportReviewReceipt)->Result<(ImportReviewProposal,i64,String),RepositoryError> {current_import_review_proposal_at(tx,&s.owner,s,original,None)}
pub(crate) fn current_import_review_proposal_custody(tx:&Transaction<'_>,s:&crate::store::sqlite::selective_import::ImportReviewSelector,original:&ImportReviewReceipt,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<(ImportReviewProposal,i64,String),RepositoryError> {
    let proof=view.for_captured(tx,&s.owner)?;if proof.account_id()!=s.account_id {return Err(RepositoryError::NotFound);}
    current_import_review_proposal_at(tx,proof.current_owner(),s,original,Some(view))
}
fn checked_import_proposal_custody_scope(tx:&Transaction<'_>,sql_owner:&str,captured:&str,workspace:&str,proposal:&Value,view:Option<&super::owner_custody::VerifiedCustodyView<'_,'_>>)->Result<Value,RepositoryError> {
    let scope_owner=proposal.get("question_context").and_then(|c|c.get("scope")).and_then(|v|v.get("owner")).and_then(Value::as_str).unwrap_or(captured);
    match view {Some(view)=>{view.for_captured(tx,scope_owner)?;},None if scope_owner!=captured=>return Err(RepositoryError::NotFound),None=>{}}
    checked_import_proposal_at(tx,sql_owner,scope_owner,workspace,proposal)
}
fn current_import_review_proposal_at(
    tx:&Transaction<'_>, sql_owner:&str, s:&crate::store::sqlite::selective_import::ImportReviewSelector,
    original:&ImportReviewReceipt, view:Option<&super::owner_custody::VerifiedCustodyView<'_,'_>>,
)->Result<(ImportReviewProposal,i64,String),RepositoryError> {
    require_guarded_epoch(tx)?;
    let receipt=read_import_review_receipt_at(tx,sql_owner,s)?.ok_or(RepositoryError::NotFound)?;
    if receipt!=*original || receipt.mode!="review_only" || receipt.publication_kind!="candidate" || receipt.publication_id!=receipt.candidate_id {return Err(RepositoryError::NotFound);}
    let revision:i64=tx.query_row("SELECT current_revision FROM fm_v2_candidates WHERE owner_id=? AND workspace_key=? AND project_key='' AND candidate_id=? AND idempotency_key=?",params![sql_owner,s.workspace_id,receipt.candidate_id,receipt.candidate_id],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    if revision<1 {return Err(RepositoryError::NotFound);}
    let read=|rev|read_candidate(tx,sql_owner,&receipt.candidate_id,&receipt.candidate_id,rev,&s.workspace_id,"");
    let current=read(revision)?;let first=read(1)?;
    verify_selected_explicit_candidate(tx,sql_owner,&s.workspace_id,&receipt.candidate_id,&receipt.candidate_id,revision,"reviewed import","edit reviewed import",Some(&receipt.source_event_id),view,None)?;
    if current.payload.state!=CandidateState::Proposed || current.payload.accepted_block_id.is_some() || first.payload.state!=CandidateState::Proposed {return Err(RepositoryError::NotFound);}
    let proposal:ImportReviewProposal=serde_json::from_value(current.payload.proposal)?;
    let initial:ImportReviewProposal=serde_json::from_value(first.payload.proposal)?;
    let same=|p:&ImportReviewProposal| p.version=="s03.import-review-proposal.v1" && p.owner==receipt.owner && p.workspace_id==receipt.workspace_id && p.account_id==receipt.account_id && p.batch_id==receipt.batch_id && p.item_id==receipt.item_id && p.suggestion_id==receipt.suggestion_id && p.review_key_hash==receipt.review_key_hash && p.proposal_fingerprint==receipt.proposal_fingerprint && p.initial_stored_proposal_sha256==receipt.stored_proposal_sha256 && p.stored_provenance_sha256==receipt.stored_provenance_sha256 && p.candidate_id==receipt.candidate_id && p.source_event_id==receipt.source_event_id;
    if !same(&proposal) || !same(&initial) || initial.stored_proposal_sha256!=initial.initial_stored_proposal_sha256 || sha256_hex(serde_json::to_string(&initial.proposal)?.as_bytes())!=initial.stored_proposal_sha256 || proposal.provenance!=initial.provenance || !valid_sha256(&proposal.stored_proposal_sha256) || sha256_hex(serde_json::to_string(&proposal.proposal)?.as_bytes())!=proposal.stored_proposal_sha256 || sha256_hex(serde_json::to_string(&proposal.provenance)?.as_bytes())!=proposal.stored_provenance_sha256 {return Err(RepositoryError::NotFound);}
    checked_import_proposal_custody_scope(tx,sql_owner,&s.owner,&s.workspace_id,&proposal.proposal,view)?;
    let (filtered_proposal,proposal_redactions)=crate::privacy::filter_json(&proposal.proposal);
    let (filtered,redactions)=crate::privacy::filter_json(&proposal.provenance);
    if proposal_redactions!=0 || filtered_proposal!=proposal.proposal || redactions!=0 || filtered!=proposal.provenance || contains_import_not_stored(&proposal.proposal) || contains_import_not_stored(&proposal.provenance) {return Err(RepositoryError::NotFound);}
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![receipt.candidate_id,sql_owner,s.workspace_id],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    let text=proposal.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?;
    let kind=match proposal.proposal["category"].as_str() {Some("identity")=>crate::record::MemoryKind::Persona,Some("preference")=>crate::record::MemoryKind::Instruction,Some("task")=>crate::record::MemoryKind::Episodic,Some("unknown")=>crate::record::MemoryKind::Unknown,Some("fact"|"contact"|"project"|"goal")=>crate::record::MemoryKind::Fact,_=>return Err(RepositoryError::NotFound)};
    if candidate.content!=text || candidate.content_hash!=crate::privacy::content_hash(text) || candidate.kind!=kind || candidate.source!="memory_import" || candidate.source_event_id!=receipt.source_event_id || candidate.accepted_curated_id.is_some() {return Err(RepositoryError::NotFound);}
    let digest=crate::provider::native::candidate_snapshot_digest(&candidate).map_err(RepositoryError::Validation)?;
    Ok((proposal,revision,digest))
}

fn contains_import_not_stored(value:&Value)->bool {
    match value {Value::String(s)=>s==crate::privacy::NOT_STORED,Value::Array(a)=>a.iter().any(contains_import_not_stored),Value::Object(o)=>o.values().any(contains_import_not_stored),_=>false}
}

/// Filtered import preparation is private data. It is not itself an authority;
/// every consumer must revalidate the original same-transaction witness.
#[derive(Debug,Clone,PartialEq)]
pub(crate) struct PreparedImportReview {
    proposal:Value, provenance:Value, stored_proposal_sha256:String,
    stored_provenance_sha256:String, candidate_id:String, source_event_id:String,
}
impl PreparedImportReview {
    pub(crate) fn text(&self)->&str {self.proposal["text"].as_str().expect("validated import text")}
    pub(crate) fn category(&self)->&str {self.proposal["category"].as_str().expect("validated import category")}
    pub(crate) fn candidate_id(&self)->&str {&self.candidate_id}
    pub(crate) fn source_event_id(&self)->&str {&self.source_event_id}
    pub(crate) fn kind(&self)->crate::record::MemoryKind {import_category_kind(self.category()).expect("validated import category")}
}
fn import_category_kind(category:&str)->Option<crate::record::MemoryKind> {
    use crate::record::MemoryKind;
    Some(match category {"identity"=>MemoryKind::Persona,"preference"=>MemoryKind::Instruction,"task"=>MemoryKind::Episodic,"unknown"=>MemoryKind::Unknown,"fact"|"contact"|"project"|"goal"=>MemoryKind::Fact,_=>return None})
}

pub(crate) fn prepare_import_review(
    tx:&Transaction<'_>, witness:&crate::store::sqlite::selective_import::VerifiedImportReview<'_,'_>,
    budget:&mut ResetFenceBudget,
)->Result<PreparedImportReview,RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    let selector=witness.selector();
    if sha256_hex(serde_json::to_string(witness.proposal())?.as_bytes())!=selector.proposal_fingerprint {return Err(RepositoryError::NotFound);}
    let (mut proposal,proposal_redactions)=crate::privacy::filter_json(witness.proposal());
    let (provenance,provenance_redactions)=crate::privacy::filter_json(witness.provenance());
    if witness.provenance().get("knowledge_snapshot").is_some() {
        // Transfer evidence stays immutable in provenance; later separately
        // authorized edits affect only the ordinary destination proposal.
        if proposal_redactions!=0 || provenance_redactions!=0 || proposal!=*witness.proposal() || provenance!=*witness.provenance() || proposal.get("knowledge_snapshot")!=provenance.get("knowledge_snapshot") {return Err(RepositoryError::NotFound);}
        proposal.as_object_mut().ok_or(RepositoryError::NotFound)?.remove("knowledge_snapshot");
    }
    if let Some(claim) = witness.provenance().get("markdown_source") {
        if proposal.get("markdown_source") != Some(claim) || provenance.get("markdown_source") != Some(claim) {
            return Err(RepositoryError::NotFound);
        }
        // Preserve the exact claim in the original receipt provenance so future
        // edits can change reviewed text/category without changing its origin.
        proposal.as_object_mut().ok_or(RepositoryError::NotFound)?.remove("markdown_source");
    }
    if contains_import_not_stored(&proposal) || contains_import_not_stored(&provenance) {return Err(RepositoryError::NotFound);}
    checked_import_proposal(tx,&selector.owner,&selector.workspace_id,&proposal)?;
    if provenance["contract"]!="openclank.memory-import-review/v1" || provenance["batch_id"]!=selector.batch_id || provenance["item_id"]!=selector.item_id || provenance["suggestion_id"]!=selector.suggestion_id || provenance["reviewed_by"]!=selector.owner || !provenance["content_hash"].as_str().is_some_and(valid_sha256) || provenance["filename"].as_str().is_none() {return Err(RepositoryError::NotFound);}
    let stored_proposal_sha256=sha256_hex(serde_json::to_string(&proposal)?.as_bytes());
    let stored_provenance_sha256=sha256_hex(serde_json::to_string(&provenance)?.as_bytes());
    Ok(PreparedImportReview{proposal,provenance,stored_proposal_sha256,stored_provenance_sha256,candidate_id:witness.candidate_id().into(),source_event_id:witness.source_event_id().into()})
}

/// Read the owner's newly inserted private candidate and derive exactly one
/// reviewed record. The caller performs collision/upsert/readback for manual.
pub(crate) fn materialize_import_review(
    tx:&Transaction<'_>, witness:&crate::store::sqlite::selective_import::VerifiedImportReview<'_,'_>,
    prepared:&PreparedImportReview, budget:&mut ResetFenceBudget,
)->Result<crate::record::MemoryRecord,RepositoryError> {
    if prepare_import_review(tx,witness,budget)?!=*prepared {return Err(RepositoryError::NotFound);}
    let selector=witness.selector();
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![prepared.candidate_id,selector.owner,selector.workspace_id],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    if candidate.content!=prepared.text() || candidate.kind!=prepared.kind() || candidate.content_hash!=crate::privacy::content_hash(prepared.text()) || candidate.source!="memory_import" || candidate.evidence_role!="user" || candidate.source_event_id!=prepared.source_event_id || candidate.dedup_key!=prepared.candidate_id || candidate.accepted_curated_id.is_some() || !candidate.raw_evidence_ids.is_empty() || !candidate.source_message_ids.is_empty() || !candidate.source_uri.is_empty() {return Err(RepositoryError::NotFound);}
    let mut record=crate::provider::native::reviewed_candidate_record(&candidate,&selector.owner,&selector.workspace_id,"reviewed import");
    record.source="memory_import".into();record.source_type=crate::record::SourceType::AutoExtracted;
    record.metadata["category"]=json!(prepared.category());
    record.metadata["source_event_id"]=json!(prepared.source_event_id);
    record.metadata["import_review"]=super::knowledge_snapshot::safe_provenance(&prepared.provenance).map_err(RepositoryError::Validation)?;
    for key in ["question_context","entity_match_candidates","subject_attribution","subject_role"] {if let Some(value)=prepared.proposal.get(key) {record.metadata[key]=value.clone();}}
    if let Some(value)=prepared.proposal.get("document_role") {record.metadata["source_document_role"]=value.clone();}
    if prepared.category()=="identity" {record.tags=vec!["self".into()];}
    let (filtered,redactions)=crate::privacy::filter_json(&record.metadata);
    if redactions!=0 || filtered!=record.metadata || contains_import_not_stored(&record.metadata) {return Err(RepositoryError::NotFound);}
    Ok(record)
}

/// Commit the prepared import to the existing candidate/knowledge owners and
/// write its content-free once receipt. Caller has inserted the private pending
/// candidate; for manual mode it also upserts and exactly reads back `record`.
/// The original transaction must roll back on every error.
pub(crate) fn finish_import_review(
    tx:&Transaction<'_>, witness:&crate::store::sqlite::selective_import::VerifiedImportReview<'_,'_>,
    prepared:&PreparedImportReview, record:&crate::record::MemoryRecord, budget:&mut ResetFenceBudget,
)->Result<Value,RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    if prepare_import_review(tx,witness,budget)?!=*prepared {return Err(RepositoryError::NotFound);}
    let selector=witness.selector();
    if read_import_review_receipt(tx,selector)?.is_some() {return Err(RepositoryError::Validation("import review already issued; status only".into()));}
    let expected=materialize_import_review(tx,witness,prepared,budget)?;
    if record.id!=expected.id || record.content!=expected.content || record.kind!=expected.kind || record.owner!=expected.owner || record.workspace_id!=expected.workspace_id || record.source!=expected.source || record.source_type!=expected.source_type || record.metadata!=expected.metadata || record.tags!=expected.tags || record.confidence_score!=expected.confidence_score || record.trust_score!=expected.trust_score {return Err(RepositoryError::NotFound);}
    let (candidate,id,source_event)=import_review_ids(selector)?;
    if candidate!=prepared.candidate_id || source_event!=prepared.source_event_id {return Err(RepositoryError::NotFound);}
    let envelope=ImportReviewProposal {version:"s03.import-review-proposal.v1".into(),owner:selector.owner.clone(),workspace_id:selector.workspace_id.clone(),account_id:selector.account_id.clone(),batch_id:selector.batch_id.clone(),item_id:selector.item_id.clone(),suggestion_id:selector.suggestion_id.clone(),review_key_hash:selector.review_key_hash.clone(),proposal_fingerprint:selector.proposal_fingerprint.clone(),initial_stored_proposal_sha256:prepared.stored_proposal_sha256.clone(),stored_proposal_sha256:prepared.stored_proposal_sha256.clone(),stored_provenance_sha256:prepared.stored_provenance_sha256.clone(),candidate_id:candidate.clone(),source_event_id:source_event.clone(),proposal:prepared.proposal.clone(),provenance:prepared.provenance.clone()};
    let actor=RevisionActor{actor_type:"user".into(),actor_id:selector.owner.clone(),reason:"reviewed import".into(),source_event_id:Some(source_event.clone())};
    let payload=CandidatePayload{proposal:serde_json::to_value(&envelope)?,state:CandidateState::Proposed,confidence:f64::from(record.confidence_score),importance:f64::from(record.importance_score),reason:"reviewed import".into(),source_id:None,source_revision:None,accepted_block_id:None,evidence_ids:vec![]};
    validate_candidate(&candidate,&candidate,&payload,true)?;
    let now=chrono::Utc::now().to_rfc3339();
    let change=insert_change_set(tx,&selector.owner,&actor,&serde_json::to_value(&payload)?,&now)?;
    tx.execute("INSERT INTO fm_v2_candidates(owner_id,candidate_id,workspace_key,project_key,idempotency_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,'',?,?,0,?)",params![selector.owner,candidate,selector.workspace_id,candidate,change,now])?;
    insert_candidate_revision(tx,&selector.owner,&candidate,1,None,&payload,&actor,&change,&now)?;
    if tx.execute("UPDATE fm_v2_candidates SET current_revision=1 WHERE owner_id=? AND candidate_id=? AND current_revision=0",params![selector.owner,candidate])?!=1 {return Err(RepositoryError::NotFound);}
    insert_outbox(tx,&selector.owner,&change,"candidate_revision",&json!({"candidate_id":candidate,"revision":1}),1,&now)?;
    let mut published_revision=None;
    let (state,publication_kind,publication_id)=match selector.mode.as_str() {
        "review_only" => ("staged","candidate",candidate.clone()),
        "manual" => {
            let context=if prepared.category()=="unknown" {checked_question_context_scoped(tx,&selector.owner,&selector.workspace_id,prepared.category(),prepared.proposal.get("question_context"))?} else {None};
            if prepared.category()=="unknown" && !question_semantics_capability(tx)? {return Err(RepositoryError::NotFound);}
            let provenance=json!({"action":"import_review","source":"memory_import","source_type":"auto_extracted","reviewed_by":selector.owner,"import_review":super::knowledge_snapshot::safe_provenance(&prepared.provenance).map_err(RepositoryError::Validation)?});
            let typed=TypedPublication{owner:&selector.owner,workspace_id:&selector.workspace_id,category:prepared.category(),text:prepared.text(),request_key:&id,record,context,reason:"reviewed import",source_type:"auto_extracted",source_event_id:&source_event,provenance};
            let result=publish_typed_core(tx,&typed)?;
            published_revision=Some(result.revision.clone());
            let mut activated=payload.clone();activated.state=CandidateState::Activated;activated.accepted_block_id=Some(result.revision.identity.block_id.clone());
            advance_explicit_candidate(tx,&selector.owner,&candidate,1,&payload,&activated,&actor,&now)?;
            ("applied","memory",record.id.clone())
        },
        _=>return Err(RepositoryError::NotFound),
    };
    let receipt=ImportReviewReceipt{version:"s03.import-review.v1".into(),owner:selector.owner.clone(),workspace_id:selector.workspace_id.clone(),account_id:selector.account_id.clone(),batch_id:selector.batch_id.clone(),item_id:selector.item_id.clone(),suggestion_id:selector.suggestion_id.clone(),review_key_hash:selector.review_key_hash.clone(),proposal_fingerprint:selector.proposal_fingerprint.clone(),stored_proposal_sha256:prepared.stored_proposal_sha256.clone(),stored_provenance_sha256:prepared.stored_provenance_sha256.clone(),candidate_id:candidate.clone(),source_event_id:source_event.clone(),mode:selector.mode.clone(),publication_kind:publication_kind.into(),publication_id:publication_id.clone()};
    let raw=serde_json::to_string(&receipt)?;
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s03.import-review.v1',?,?,?,?,?)",params![selector.owner,id,selector.owner,source_event,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,'import_review',?,0,?)",params![selector.owner,format!("outbox_{}_import_review",id),id,raw,now])?;
    if read_import_review_receipt(tx,selector)?.as_ref()!=Some(&receipt) {return Err(RepositoryError::NotFound);}
    if let Some(revision)=published_revision.as_ref() {
        super::import_mirror::write_import_typed_review_receipt(tx,witness,&envelope,revision,budget)?;
    }
    Ok(json!({"state":state,"candidate_id":candidate,"publication_kind":publication_kind,"publication_id":publication_id}))
}

/// Edit the existing staged import under its exact finalized Host decision and
/// current preview binding. Original receipt and initial hashes stay immutable.
pub(crate) fn edit_staged_import_review(
    tx:&Transaction<'_>, witness:&crate::store::sqlite::selective_import::VerifiedStagedImport<'_,'_>,
    expected_revision:i64, expected_binding:&str, reviewed:&Value, budget:&mut ResetFenceBudget,
)->Result<(),RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    if expected_revision!=witness.revision() || !valid_sha256(expected_binding) || expected_binding!=witness.binding_sha256() {return Err(RepositoryError::Validation("import review binding changed".into()));}
    let selector=witness.selector();
    let (filtered,_)=crate::privacy::filter_json(reviewed);
    if contains_import_not_stored(&filtered) {return Err(RepositoryError::NotFound);}
    checked_import_proposal(tx,&selector.owner,&selector.workspace_id,&filtered)?;
    let mut next=witness.proposal().clone();
    let old_text=next.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?.to_owned();
    let text=filtered["text"].as_str().ok_or(RepositoryError::NotFound)?;
    let category=filtered["category"].as_str().ok_or(RepositoryError::NotFound)?;
    let kind=match category {"identity"=>"persona","preference"=>"instruction","task"=>"episodic","unknown"=>"unknown","fact"|"contact"|"project"|"goal"=>"fact",_=>return Err(RepositoryError::NotFound)};
    let now=chrono::Utc::now().to_rfc3339();
    if tx.execute("UPDATE candidates SET content=?1,kind=?2,content_hash=?3,updated_at=?4 WHERE id=?5 AND owner=?6 AND workspace_id=?7 AND status='pending' AND content=?8 AND content_hash=?9 AND accepted_curated_id IS NULL",params![text,kind,crate::privacy::content_hash(text),now,next.candidate_id,selector.owner,selector.workspace_id,old_text,crate::privacy::content_hash(&old_text)])?!=1 {return Err(RepositoryError::NotFound);}
    let row=read_candidate(tx,&selector.owner,&next.candidate_id,&next.candidate_id,expected_revision,&selector.workspace_id,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(&next)? {return Err(RepositoryError::NotFound);}
    next.stored_proposal_sha256=sha256_hex(serde_json::to_string(&filtered)?.as_bytes()); next.proposal=filtered;
    let prior_payload=row.payload.clone();let mut payload=row.payload; payload.proposal=serde_json::to_value(&next)?;
    let actor=RevisionActor{actor_type:"user".into(),actor_id:selector.owner.clone(),reason:"edit reviewed import".into(),source_event_id:Some(next.source_event_id.clone())};
    advance_explicit_candidate(tx,&selector.owner,&next.candidate_id,expected_revision,&prior_payload,&payload,&actor,&now)?;
    Ok(())
}

/// Content-free later human decision, independent of the immutable import
/// publication receipt. Reading it never recreates or grants a candidate.
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct StagedImportDecisionReceipt {
    pub version:String, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub candidate_id:String, pub review_key:String, pub expected_revision:i64,
    pub expected_binding:String, pub current_proposal_sha256:String,
    pub action:String, pub curated_id:Option<String>,
}
pub(crate) fn staged_import_decision_receipt_id(owner:&str,workspace:&str,account:&str,candidate:&str)->Result<String,RepositoryError> {
    if ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) || !candidate.strip_prefix("importreview_").is_some_and(valid_sha256) {return Err(RepositoryError::NotFound);}
    Ok(format!("cs_import_staged_review_{}",sha256_hex(&serde_json::to_vec(&("s03.import-staged-review.v1",owner,workspace,account,candidate))?)))
}
pub(crate) fn read_staged_import_decision_receipt(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<Option<StagedImportDecisionReceipt>,RepositoryError> {read_staged_import_decision_receipt_at(tx,owner,owner,workspace,account,candidate)}
pub(crate) fn read_staged_import_decision_receipt_custody(tx:&Transaction<'_>,workspace:&str,account:&str,candidate:&str,captured:&str,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<Option<StagedImportDecisionReceipt>,RepositoryError> {
    let proof=view.for_captured(tx,captured)?;if proof.account_id()!=account {return Err(RepositoryError::NotFound);}
    read_staged_import_decision_receipt_at(tx,proof.current_owner(),proof.captured_owner(),workspace,account,candidate)
}
fn read_staged_import_decision_receipt_at(tx:&Transaction<'_>,sql_owner:&str,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<Option<StagedImportDecisionReceipt>,RepositoryError> {
    let id=staged_import_decision_receipt_id(owner,workspace,account,candidate)?;
    let event=format!("outbox_{}_import_staged_review",id);
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s03.import-staged-review.v1'",params![sql_owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,contract,source_event))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![sql_owner,id,event],|r|r.get(0))?;
        return if collision {Err(RepositoryError::NotFound)} else {Ok(None)};
    };
    let receipt:StagedImportDecisionReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    let outbox:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='import_staged_review'",params![sql_owner,event,id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=owner || contract!=CONTRACT_VERSION || source_event.is_some() || receipt.version!="s03.import-staged-review.v1" || receipt.owner!=owner || receipt.workspace_id!=workspace || receipt.account_id!=account || receipt.candidate_id!=candidate || !valid_receipt_text(&receipt.review_key) || receipt.expected_revision<1 || !valid_sha256(&receipt.expected_binding) || !valid_sha256(&receipt.current_proposal_sha256) || !match (receipt.action.as_str(),receipt.curated_id.as_deref()) {("accept",Some(v))=>valid_receipt_text(v),("reject",None)=>true,_=>false} || outbox.as_ref().is_none_or(|(v,r)|v!=&raw || *r!=0) {return Err(RepositoryError::NotFound);}
    Ok(Some(receipt))
}
/// Caller must revalidate the exact staged witness before the transition and
/// write this receipt in the same transaction; failures require rollback.
fn write_staged_import_decision_receipt(tx:&Transaction<'_>,receipt:&StagedImportDecisionReceipt)->Result<String,RepositoryError> {
    let id=staged_import_decision_receipt_id(&receipt.owner,&receipt.workspace_id,&receipt.account_id,&receipt.candidate_id)?;
    if read_staged_import_decision_receipt(tx,&receipt.owner,&receipt.workspace_id,&receipt.account_id,&receipt.candidate_id)?.is_some() || receipt.version!="s03.import-staged-review.v1" || !valid_receipt_text(&receipt.review_key) || receipt.expected_revision<1 || !valid_sha256(&receipt.expected_binding) || !valid_sha256(&receipt.current_proposal_sha256) || !match (receipt.action.as_str(),receipt.curated_id.as_deref()) {("accept",Some(v))=>valid_receipt_text(v),("reject",None)=>true,_=>false} {return Err(RepositoryError::NotFound);}
    let raw=serde_json::to_string(receipt)?; let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,'s03.import-staged-review.v1',NULL,?,?,?,?)",params![receipt.owner,id,receipt.owner,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,'import_staged_review',?,0,?)",params![receipt.owner,format!("outbox_{}_import_staged_review",id),id,raw,now])?;
    Ok(id)
}

/// Derive a reviewed record only from a freshly revalidated staged witness.
/// The SQLite owner performs scoped collision checks, upsert, and exact readback.
pub(crate) fn prepare_staged_import_record(tx:&Transaction<'_>,witness:&crate::store::sqlite::selective_import::VerifiedStagedImport<'_,'_>,budget:&mut ResetFenceBudget)->Result<crate::record::MemoryRecord,RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    let s=witness.selector(); let p=witness.proposal();
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![p.candidate_id,s.owner,s.workspace_id],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    let mut record=crate::provider::native::reviewed_candidate_record(&candidate,&s.owner,&s.workspace_id,"reviewed staged import");
    record.source="memory_import".into(); record.source_type=crate::record::SourceType::AutoExtracted;
    record.metadata["category"]=p.proposal["category"].clone(); record.metadata["source_event_id"]=json!(p.source_event_id); record.metadata["import_review"]=super::knowledge_snapshot::safe_provenance(&p.provenance).map_err(RepositoryError::Validation)?;
    for key in ["question_context","entity_match_candidates","subject_attribution","subject_role"] {if let Some(value)=p.proposal.get(key) {record.metadata[key]=value.clone();}}
    if let Some(value)=p.proposal.get("document_role") {record.metadata["source_document_role"]=value.clone();}
    if p.proposal["category"]=="identity" {record.tags=vec!["self".into()];}
    let (filtered,redactions)=crate::privacy::filter_json(&record.metadata);
    if redactions!=0 || filtered!=record.metadata || contains_import_not_stored(&record.metadata) {return Err(RepositoryError::NotFound);}
    Ok(record)
}
/// Apply exactly one later human decision. The caller checks original status
/// first and rolls back the entire borrowed transaction on any error. For an
/// acceptance it has already upserted and exactly read back `record`.
pub(crate) fn finish_staged_import_decision(tx:&Transaction<'_>,witness:&crate::store::sqlite::selective_import::VerifiedStagedImport<'_,'_>,review_key:&str,expected_revision:i64,expected_binding:&str,action:&str,record:Option<&crate::record::MemoryRecord>,budget:&mut ResetFenceBudget)->Result<(StagedImportDecisionReceipt,Option<KnowledgeRevision>),RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    let s=witness.selector(); let p=witness.proposal();
    if !valid_receipt_text(review_key) || expected_revision!=witness.revision() || !valid_sha256(expected_binding) || expected_binding!=witness.binding_sha256() || read_staged_import_decision_receipt(tx,&s.owner,&s.workspace_id,&s.account_id,&p.candidate_id)?.is_some() {return Err(RepositoryError::NotFound);}
    let row=read_candidate(tx,&s.owner,&p.candidate_id,&p.candidate_id,expected_revision,&s.workspace_id,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(p)? || row.payload.accepted_block_id.is_some() {return Err(RepositoryError::NotFound);}
    let id=staged_import_decision_receipt_id(&s.owner,&s.workspace_id,&s.account_id,&p.candidate_id)?;
    let prior_payload=row.payload.clone();let mut payload=row.payload;
    let (curated,knowledge)=match (action,record) {
        ("reject",None)=>{payload.state=CandidateState::Rejected; payload.reason="human rejected staged import".into();(None,None)},
        ("accept",Some(record))=>{
            let expected=prepare_staged_import_record(tx,witness,budget)?;
            if record.id!=expected.id || record.content!=expected.content || record.kind!=expected.kind || record.owner!=expected.owner || record.workspace_id!=expected.workspace_id || record.source!=expected.source || record.source_type!=expected.source_type || record.metadata!=expected.metadata || record.tags!=expected.tags || record.confidence_score!=expected.confidence_score || record.trust_score!=expected.trust_score {return Err(RepositoryError::NotFound);}
            let category=p.proposal["category"].as_str().ok_or(RepositoryError::NotFound)?;
            let text=p.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?;
            let context=if category=="unknown" {checked_question_context_scoped(tx,&s.owner,&s.workspace_id,category,p.proposal.get("question_context"))?} else {None};
            if category=="unknown" && !question_semantics_capability(tx)? {return Err(RepositoryError::NotFound);}
            let provenance=json!({"action":"import_staged_review","source":"memory_import","source_type":"auto_extracted","reviewed_by":s.owner,"import_review":super::knowledge_snapshot::safe_provenance(&p.provenance).map_err(RepositoryError::Validation)?});
            let typed=TypedPublication{owner:&s.owner,workspace_id:&s.workspace_id,category,text,request_key:&id,record,context,reason:"reviewed staged import",source_type:"auto_extracted",source_event_id:&p.source_event_id,provenance};
            let result=publish_typed_core(tx,&typed)?; payload.state=CandidateState::Activated; payload.accepted_block_id=Some(result.revision.identity.block_id.clone());
            (Some(record.id.clone()),Some(result.revision))
        },_=>return Err(RepositoryError::NotFound),
    };
    let receipt=StagedImportDecisionReceipt{version:"s03.import-staged-review.v1".into(),owner:s.owner.clone(),workspace_id:s.workspace_id.clone(),account_id:s.account_id.clone(),candidate_id:p.candidate_id.clone(),review_key:review_key.into(),expected_revision,expected_binding:expected_binding.into(),current_proposal_sha256:p.stored_proposal_sha256.clone(),action:action.into(),curated_id:curated.clone()};
    write_staged_import_decision_receipt(tx,&receipt)?;
    if let Some(revision)=knowledge.as_ref() {
        super::import_mirror::write_staged_import_typed_review_receipt(tx,witness,&receipt,revision,budget)?;
    }
    let now=chrono::Utc::now().to_rfc3339(); let text=p.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?;
    if tx.execute("UPDATE candidates SET status=?1,reason=?2,accepted_curated_id=?3,updated_at=?4 WHERE id=?5 AND owner=?6 AND workspace_id=?7 AND status='pending' AND accepted_curated_id IS NULL AND content=?8 AND content_hash=?9",params![if action=="accept" {"accepted"} else {"rejected"},if action=="accept" {"reviewed staged import"} else {"human rejected staged import"},curated,now,p.candidate_id,s.owner,s.workspace_id,text,crate::privacy::content_hash(text)])?!=1 {return Err(RepositoryError::NotFound);}
    let actor=RevisionActor{actor_type:"user".into(),actor_id:s.owner.clone(),reason:if action=="accept" {"reviewed staged import"} else {"human rejected staged import"}.into(),source_event_id:Some(p.source_event_id.clone())};
    advance_explicit_candidate(tx,&s.owner,&p.candidate_id,expected_revision,&prior_payload,&payload,&actor,&now)?;
    Ok((receipt,knowledge))
}

/// Resolve an inspector candidate only through its bounded, verified original
/// receipt. The returned selector is a lookup fact, never a policy grant.
pub(crate) fn import_selector_for_candidate(
    tx:&Transaction<'_>, owner:&str, workspace:&str, account:&str, candidate:&str,
    policy:&PolicyPublicationRefs,
)->Result<crate::store::sqlite::selective_import::ImportReviewSelector,RepositoryError> {
    let digest=candidate.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
    if ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) {return Err(RepositoryError::NotFound);}
    let id=format!("cs_import_review_{digest}");
    let raw:Option<String>=tx.query_row("SELECT CASE WHEN length(operation_metadata_json)<=8192 THEN operation_metadata_json ELSE NULL END FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s03.import-review.v1'",params![owner,id],|r|r.get(0)).optional()?.flatten();
    let raw=raw.ok_or(RepositoryError::NotFound)?;
    let receipt:ImportReviewReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if receipt.owner!=owner || receipt.workspace_id!=workspace || receipt.account_id!=account || receipt.candidate_id!=candidate {return Err(RepositoryError::NotFound);}
    let selector=crate::store::sqlite::selective_import::ImportReviewSelector{version:1,owner:owner.into(),workspace_id:workspace.into(),account_id:account.into(),batch_id:receipt.batch_id.clone(),item_id:receipt.item_id.clone(),suggestion_id:receipt.suggestion_id.clone(),review_key_hash:receipt.review_key_hash.clone(),proposal_fingerprint:receipt.proposal_fingerprint.clone(),mode:receipt.mode.clone(),policy_projection:policy.clone()};
    if read_import_review_receipt(tx,&selector)?.as_ref()!=Some(&receipt) {return Err(RepositoryError::NotFound);}
    Ok(selector)
}

/// Factual classification of an unapproved staged import. This neither grants
/// policy nor selects deletion; the reset owner must prove its own scope.
pub(crate) fn import_unapproved_disposition(tx:&Transaction<'_>,identity:&super::import_mirror::ImportReviewIdentity,budget:&mut ResetFenceBudget)->Result<(String,String),RepositoryError> {
    import_unapproved_disposition_at(tx,identity,&identity.owner,None,budget)
}
pub(crate) fn import_unapproved_disposition_custody(tx:&Transaction<'_>,identity:&super::import_mirror::ImportReviewIdentity,view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(String,String),RepositoryError> {
    import_unapproved_disposition_at(tx,identity,view.current_owner(),Some(view),budget)
}
fn import_unapproved_disposition_at(tx:&Transaction<'_>,identity:&super::import_mirror::ImportReviewIdentity,current_owner:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_,'_>>,budget:&mut ResetFenceBudget)->Result<(String,String),RepositoryError> {
    require_guarded_epoch(tx)?;
    let (original,original_hash)=super::import_mirror::original_for_import_identity_at(tx,identity,current_owner,view)?;
    if original.mode!="review_only" || original.publication_kind!="candidate" || original.publication_id!=original.candidate_id {return Err(RepositoryError::NotFound);}
    let owner=current_owner;let workspace=&identity.workspace_id;let candidate=&original.candidate_id;
    let head:i64=tx.query_row("SELECT current_revision FROM fm_v2_candidates WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND candidate_id=?3 AND idempotency_key=?3",params![owner,workspace,candidate],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    if head<1 {return Err(RepositoryError::NotFound);}
    // Charge all variable-sized revision fields before read_candidate decodes
    // them. This owner never produces candidate evidence or source references.
    let read=|revision:i64,budget:&mut ResetFenceBudget|->Result<CandidateRevision,RepositoryError> {
        let (bytes,has_source,evidence):(i64,bool,i64)=tx.query_row("SELECT length(CAST(proposal_json AS BLOB))+length(CAST(state AS BLOB))+length(CAST(reason AS BLOB))+coalesce(length(CAST(source_id AS BLOB)),0)+coalesce(length(CAST(accepted_block_id AS BLOB)),0)+length(CAST(actor_type AS BLOB))+length(CAST(actor_id AS BLOB))+coalesce(length(CAST(source_event_id AS BLOB)),0)+length(CAST(change_set_id AS BLOB))+length(CAST(content_hash AS BLOB))+length(CAST(created_at AS BLOB)),source_id IS NOT NULL,(SELECT count(*) FROM fm_v2_candidate_evidence e WHERE e.owner_id=r.owner_id AND e.candidate_id=r.candidate_id AND e.revision=r.revision) FROM fm_v2_candidate_revisions r WHERE owner_id=?1 AND candidate_id=?2 AND revision=?3",params![owner,candidate,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?.ok_or(RepositoryError::NotFound)?;
        if has_source || evidence!=0 {return Err(RepositoryError::NotFound);}
        budget.check(usize::try_from(bytes).map_err(|_|RepositoryError::NotFound)?).map_err(RepositoryError::Validation)?;
        read_candidate(tx,owner,candidate,candidate,revision,workspace,"")
    };
    let first=read(1,budget)?;let current=read(head,budget)?;
    let initial:ImportReviewProposal=serde_json::from_value(first.payload.proposal.clone())?;
    let proposal:ImportReviewProposal=serde_json::from_value(current.payload.proposal.clone())?;
    let same=|p:&ImportReviewProposal|p.version=="s03.import-review-proposal.v1" && p.owner==original.owner && p.workspace_id==original.workspace_id && p.account_id==original.account_id && p.batch_id==original.batch_id && p.item_id==original.item_id && p.suggestion_id==original.suggestion_id && p.review_key_hash==original.review_key_hash && p.proposal_fingerprint==original.proposal_fingerprint && p.initial_stored_proposal_sha256==original.stored_proposal_sha256 && p.stored_provenance_sha256==original.stored_provenance_sha256 && p.candidate_id==original.candidate_id && p.source_event_id==original.source_event_id;
    if first.payload.state!=CandidateState::Proposed || first.payload.accepted_block_id.is_some() || !same(&initial) || !same(&proposal) || initial.stored_proposal_sha256!=original.stored_proposal_sha256 || sha256_hex(serde_json::to_string(&initial.proposal)?.as_bytes())!=initial.stored_proposal_sha256 || sha256_hex(serde_json::to_string(&proposal.proposal)?.as_bytes())!=proposal.stored_proposal_sha256 || initial.provenance!=proposal.provenance || sha256_hex(serde_json::to_string(&proposal.provenance)?.as_bytes())!=proposal.stored_provenance_sha256 {return Err(RepositoryError::NotFound);}
    checked_import_proposal_custody_scope(tx,owner,&identity.owner,workspace,&proposal.proposal,view)?;
    let (filtered,n)=crate::privacy::filter_json(&proposal.proposal);let (provenance,m)=crate::privacy::filter_json(&proposal.provenance);
    if n!=0 || m!=0 || filtered!=proposal.proposal || provenance!=proposal.provenance || contains_import_not_stored(&filtered) || contains_import_not_stored(&provenance) {return Err(RepositoryError::NotFound);}
    let legacy_bytes:i64=tx.query_row("SELECT coalesce(length(CAST(id AS BLOB)),0)+coalesce(length(CAST(content AS BLOB)),0)+coalesce(length(CAST(kind AS BLOB)),0)+coalesce(length(CAST(owner AS BLOB)),0)+coalesce(length(CAST(workspace_id AS BLOB)),0)+coalesce(length(CAST(workspace_path AS BLOB)),0)+coalesce(length(CAST(session_id AS BLOB)),0)+coalesce(length(CAST(turn_id AS BLOB)),0)+coalesce(length(CAST(raw_evidence_ids AS BLOB)),0)+coalesce(length(CAST(evidence_role AS BLOB)),0)+coalesce(length(CAST(source AS BLOB)),0)+coalesce(length(CAST(source_event_id AS BLOB)),0)+coalesce(length(CAST(dedup_key AS BLOB)),0)+coalesce(length(CAST(status AS BLOB)),0)+coalesce(length(CAST(reason AS BLOB)),0)+coalesce(length(CAST(accepted_curated_id AS BLOB)),0)+coalesce(length(CAST(created_at AS BLOB)),0)+coalesce(length(CAST(updated_at AS BLOB)),0)+coalesce(length(CAST(source_uri AS BLOB)),0)+coalesce(length(CAST(source_revision AS BLOB)),0)+coalesce(length(CAST(content_hash AS BLOB)),0)+coalesce(length(CAST(source_message_ids AS BLOB)),0) FROM candidates WHERE id=? AND owner=? AND workspace_id=?",params![candidate,owner,workspace],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    budget.check(usize::try_from(legacy_bytes).map_err(|_|RepositoryError::NotFound)?).map_err(RepositoryError::Validation)?;
    let row=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=?",params![candidate,owner,workspace],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    let text=proposal.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?;let category=proposal.proposal["category"].as_str().ok_or(RepositoryError::NotFound)?;
    if row.content!=text || row.content_hash!=crate::privacy::content_hash(text) || Some(row.kind)!=import_category_kind(category) || row.source!="memory_import" || row.source_event_id!=original.source_event_id || row.accepted_curated_id.is_some() || current.payload.accepted_block_id.is_some() {return Err(RepositoryError::NotFound);}
    let status:String=tx.query_row("SELECT status FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3",params![candidate,owner,workspace],|r|r.get(0))?;
    let decision=if let Some(view)=view {
        let mut found=None;for captured in view.owners(){
            budget.check(captured.len()).map_err(RepositoryError::Validation)?;
            if let Some(value)=read_staged_import_decision_receipt_custody(tx,workspace,&identity.account_id,candidate,captured,view)? {if found.replace(value).is_some(){return Err(RepositoryError::NotFound)}}
        } found
    } else {read_staged_import_decision_receipt(tx,owner,workspace,&identity.account_id,candidate)?};
    match (status.as_str(),current.payload.state,decision.as_ref()) {
        ("pending",CandidateState::Proposed,None)=>(),
        ("rejected",CandidateState::Rejected,Some(d))=>{
            let bridge_revision=d.expected_revision.checked_add(1).ok_or(RepositoryError::NotFound)?;
            if d.action!="reject" || d.curated_id.is_some() || d.expected_revision.checked_add(2)!=Some(head) || d.current_proposal_sha256!=proposal.stored_proposal_sha256 || current.previous_revision!=Some(bridge_revision) || current.actor_type!="user" || current.actor_id!=d.owner || current.source_event_id.as_deref()!=Some(original.source_event_id.as_str()) {return Err(RepositoryError::NotFound);}
            let prior=read(d.expected_revision,budget)?;
            if prior.payload.state!=CandidateState::Proposed || prior.payload.accepted_block_id.is_some() || prior.payload.proposal!=current.payload.proposal {return Err(RepositoryError::NotFound);}
            let actor=RevisionActor{actor_type:"user".into(),actor_id:d.owner.clone(),reason:"human rejected staged import".into(),source_event_id:Some(original.source_event_id.clone())};
            let mut bridge=prior.payload.clone();bridge.state=CandidateState::Validating;
            verify_explicit_candidate_hop(tx,owner,workspace,candidate,candidate,bridge_revision,Some(d.expected_revision),&bridge,&actor,Some(&mut *budget))?;
            let mut rejected=prior.payload.clone();rejected.state=CandidateState::Rejected;rejected.reason="human rejected staged import".into();
            verify_explicit_candidate_hop(tx,owner,workspace,candidate,candidate,head,Some(bridge_revision),&rejected,&actor,Some(&mut *budget))?;
        },_=>return Err(RepositoryError::NotFound),
    }
    let legacy=crate::provider::native::candidate_snapshot_digest(&row).map_err(RepositoryError::Validation)?;
    let digest=sha256_hex(&serde_json::to_vec(&json!(["s04.import-unapproved-disposition.v1",original_hash,status,head,proposal,legacy,decision]))?);
    Ok((status,digest))
}

/// A current human review of an original proposal remains bound to its
/// captured actor, immutable input and current legacy/V2 snapshot.
pub(crate) fn explicit_add_review_binding_custody(tx:&Transaction<'_>,workspace:&str,candidate_id:&str,captured:&str,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<ExplicitAddReviewBinding,RepositoryError> {
    let proof=view.for_captured(tx,captured)?;let (proposal,revision)=current_explicit_add_proposal_custody(tx,workspace,candidate_id,captured,view)?;
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![candidate_id,proof.current_owner(),workspace],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    let kind=import_category_kind(&proposal.category).ok_or(RepositoryError::NotFound)?;
    if candidate.kind!=kind || candidate.accepted_curated_id.is_some() || candidate.content_hash!=crate::privacy::content_hash(&proposal.text) {return Err(RepositoryError::NotFound);}
    let legacy_snapshot_digest=crate::provider::native::candidate_snapshot_digest(&candidate).map_err(RepositoryError::Validation)?;
    let binding_sha256=sha256_hex(serde_json::to_string(&json!(["s03.explicit-add-review-binding.v1",proposal,revision,legacy_snapshot_digest]))?.as_bytes());
    Ok(ExplicitAddReviewBinding{proposal,expected_revision:revision,legacy_snapshot_digest,binding_sha256})
}
pub(crate) fn prepare_bound_explicit_add_review_custody(tx:&Transaction<'_>,workspace:&str,candidate:&str,captured:&str,expected_revision:i64,expected_binding:&str,policy:&PolicyPublicationRefs,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<PreparedExplicitAdd,RepositoryError> {
    let proof=view.for_captured(tx,captured)?;require_policy_projection(tx,proof.account_id(),workspace,Some(policy))?;
    let current=explicit_add_review_binding_custody(tx,workspace,candidate,captured,view)?;
    if current.expected_revision!=expected_revision || !valid_sha256(expected_binding) || current.binding_sha256!=expected_binding || (current.proposal.category=="unknown" && !question_semantics_capability(tx)?) {return Err(RepositoryError::NotFound);}
    let p=&current.proposal;
    let context_owner=p.question_context.as_ref().and_then(|c|c.get("scope")).and_then(|v|v.get("owner")).and_then(Value::as_str).unwrap_or(captured);
    view.for_captured(tx,context_owner)?;checked_question_context_at(tx,proof.current_owner(),context_owner,workspace,&p.category,p.question_context.as_ref())?;
    let candidate_row=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![candidate,proof.current_owner(),workspace],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    let mut record=crate::provider::native::reviewed_candidate_record(&candidate_row,proof.current_owner(),workspace,"explicit human add");record.source="user_created".into();record.metadata["category"]=json!(p.category);record.metadata["question_context"]=json!(p.question_context);record.metadata["declared_source"]=json!(p.source);if p.category=="identity" {record.tags=vec!["self".into()];}
    let (metadata,redactions)=crate::privacy::filter_json(&record.metadata);if redactions!=0 || metadata!=record.metadata {return Err(RepositoryError::NotFound);}
    Ok(PreparedExplicitAdd{record,candidate_digest:current.legacy_snapshot_digest,proposal:current.proposal,candidate_revision:current.expected_revision})
}
/// Caller upserts and exactly reads back the prepared record, then calls this
/// before writing the later human decision in the same borrowed transaction.
pub(crate) fn finish_explicit_add_custody(tx:&Transaction<'_>,prepared:&PreparedExplicitAdd,policy:&PolicyPublicationRefs,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<KnowledgeRevision,RepositoryError> {
    let p=&prepared.proposal;let proof=view.for_captured(tx,&p.owner)?;let current=proof.current_owner();require_policy_projection(tx,proof.account_id(),&p.workspace_id,Some(policy))?;
    let (proposal,head)=current_explicit_add_proposal_custody(tx,&p.workspace_id,&p.candidate_id,&p.owner,view)?;
    if proposal!=*p || head!=prepared.candidate_revision {return Err(RepositoryError::NotFound);}
    let context_owner=p.question_context.as_ref().and_then(|c|c.get("scope")).and_then(|v|v.get("owner")).and_then(Value::as_str).unwrap_or(&p.owner);
    view.for_captured(tx,context_owner)?;let context=checked_question_context_at(tx,current,context_owner,&p.workspace_id,&p.category,p.question_context.as_ref())?;
    let source_event_id=format!("memory:{}",prepared.record.id);let provenance=json!({"action":"explicit_add","source":p.source,"source_type":"human","reviewed_by":current});
    let typed=TypedPublication{owner:current,workspace_id:&p.workspace_id,category:&p.category,text:&p.text,request_key:&p.request_key,record:&prepared.record,context,reason:"explicit human add",source_type:"human",source_event_id:&source_event_id,provenance};
    let result=publish_typed_core(tx,&typed)?;let now=chrono::Utc::now().to_rfc3339();
    if p.category=="unknown" {
    let binding=verified_explicit_question_root_custody(tx,current,&p.workspace_id,&prepared.record.id,&p.candidate_id,&serde_json::to_value(p)?,&result.revision,None,view)?;
    let id=candidate_receipt_id(current,&p.workspace_id,&p.candidate_id,&p.request_key)?;let proposal_sha256=sha256_hex(&serde_json::to_vec(&serde_json::to_value(p)?)?);
    let value=json!({"version":"s03.explicit-question-receipt.v1","owner":current,"workspace":p.workspace_id,"account_id":p.account_id,"candidate_id":p.candidate_id,"request_key":p.request_key,"input_sha256":p.input_sha256,"stored_input_sha256":p.stored_input_sha256,"proposal_revision":head,"proposal_sha256":proposal_sha256,"curated_id":prepared.record.id,"block_id":result.revision.identity.block_id,"revision":result.revision.revision,"binding":binding});
    let raw=serde_json::to_string(&value)?;
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?1,'s03.explicit-question-review.v1',NULL,?3,?4,?5,?6)",params![current,id,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    insert_outbox(tx,current,&id,"candidate_review",&json!({"candidate_id":p.candidate_id,"curated_id":prepared.record.id,"block_id":result.revision.identity.block_id,"revision":result.revision.revision,"reviewer_account_id":p.account_id,"input_sha256":p.input_sha256,"proposal_revision":head,"proposal_sha256":proposal_sha256,"receipt_version":"s03.explicit-question-receipt.v1"}),result.revision.revision,&now)?;
    } else {
        let fact=PreparedCandidateFact{identity:result.identity.clone(),source_id:result.source_id.clone(),source_event_id:result.source_event_id.clone(),evidence_id:result.evidence_id.clone(),curated_id:prepared.record.id.clone(),record:prepared.record.clone(),projection:result.operation.clone()};
        let request=AccountCandidateReviewRequest{version:1,owner:current.into(),workspace_id:p.workspace_id.clone(),account_id:p.account_id.clone(),candidate_id:p.candidate_id.clone(),candidate_digest:prepared.candidate_digest.clone(),expected_v2_head:0,selected_paths:result.paths.clone(),request_key:p.request_key.clone(),reason:"explicit human add".into(),projection:result.operation.clone(),embedding:None,policy_projection:policy.clone()};
        write_account_candidate_receipt(tx,&request,&fact,&result.revision)?;
    }
    let row=read_candidate(tx,current,&p.candidate_id,&p.request_key,head,&p.workspace_id,"")?;if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(p)? {return Err(RepositoryError::NotFound);}
    let prior_payload=row.payload.clone();let mut activated=row.payload;activated.state=CandidateState::Activated;activated.accepted_block_id=Some(result.revision.identity.block_id.clone());
    let actor=RevisionActor{actor_type:"user".into(),actor_id:current.into(),reason:"explicit human add".into(),source_event_id:Some(source_event_id)};advance_explicit_candidate(tx,current,&p.candidate_id,head,&prior_payload,&activated,&actor,&now)?;Ok(result.revision)
}

/// Current account edit creates a new revision; prior captured proposal bytes
/// remain unchanged. Any newly supplied question scope is the current owner.
pub(crate) fn edit_explicit_add_proposal_custody(tx:&Transaction<'_>,next:&ExplicitAddProposal,expected_revision:i64,expected_binding:&str,policy:&PolicyPublicationRefs,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<ExplicitAddReviewBinding,RepositoryError> {
    let proof=view.for_captured(tx,&next.owner)?;let current_owner=proof.current_owner();if proof.account_id()!=next.account_id {return Err(RepositoryError::NotFound);}
    require_policy_projection(tx,proof.account_id(),&next.workspace_id,Some(policy))?;
    let current=explicit_add_review_binding_custody(tx,&next.workspace_id,&next.candidate_id,&next.owner,view)?;
    if expected_revision<1 || current.expected_revision!=expected_revision || !valid_sha256(expected_binding) || current.binding_sha256!=expected_binding {return Err(RepositoryError::NotFound);}
    let prior=&current.proposal;
    if next.version!=prior.version || next.owner!=prior.owner || next.workspace_id!=prior.workspace_id || next.account_id!=prior.account_id || next.request_key!=prior.request_key || next.input_sha256!=prior.input_sha256 || next.candidate_id!=prior.candidate_id {return Err(RepositoryError::NotFound);}
    let kind=match next.category.as_str() {"identity"=>"persona","preference"=>"instruction","task"=>"episodic","unknown"=>"unknown","fact"|"contact"|"project"|"goal"=>"fact",_=>return Err(RepositoryError::NotFound)};
    let now=chrono::Utc::now().to_rfc3339();
    if tx.execute("UPDATE candidates SET content=?1,kind=?2,content_hash=?3,updated_at=?4 WHERE id=?5 AND owner=?6 AND workspace_id=?7 AND status='pending' AND content=?8 AND content_hash=?9 AND accepted_curated_id IS NULL",params![next.text,kind,crate::privacy::content_hash(&next.text),now,next.candidate_id,current_owner,next.workspace_id,prior.text,crate::privacy::content_hash(&prior.text)])?!=1 {return Err(RepositoryError::NotFound);}
    validate_proposal_at(tx,current_owner,next,true)?;checked_question_context_at(tx,current_owner,current_owner,&next.workspace_id,&next.category,next.question_context.as_ref())?;
    let row=read_candidate(tx,current_owner,&next.candidate_id,&next.request_key,expected_revision,&next.workspace_id,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(prior)? {return Err(RepositoryError::NotFound);}
    let prior_payload=row.payload.clone();let mut payload=row.payload;payload.proposal=serde_json::to_value(next)?;
    let actor=RevisionActor{actor_type:"user".into(),actor_id:current_owner.into(),reason:"edit explicit human add".into(),source_event_id:None};advance_explicit_candidate(tx,current_owner,&next.candidate_id,expected_revision,&prior_payload,&payload,&actor,&now)?;
    explicit_add_review_binding_custody(tx,&next.workspace_id,&next.candidate_id,&next.owner,view)
}
pub(crate) fn reject_explicit_add_proposal_custody(tx:&Transaction<'_>,workspace:&str,candidate:&str,captured:&str,expected_revision:i64,expected_binding:&str,policy:&PolicyPublicationRefs,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<(),RepositoryError> {
    let proof=view.for_captured(tx,captured)?;let owner=proof.current_owner();require_policy_projection(tx,proof.account_id(),workspace,Some(policy))?;
    let current=explicit_add_review_binding_custody(tx,workspace,candidate,captured,view)?;
    if current.expected_revision!=expected_revision || expected_revision<1 || !valid_sha256(expected_binding) || current.binding_sha256!=expected_binding {return Err(RepositoryError::NotFound);}
    let row=read_candidate(tx,owner,candidate,&current.proposal.request_key,expected_revision,workspace,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(&current.proposal)? || row.payload.accepted_block_id.is_some() {return Err(RepositoryError::NotFound);}
    let now=chrono::Utc::now().to_rfc3339();if tx.execute("UPDATE candidates SET status='rejected',reason='explicit human rejection',updated_at=?1 WHERE id=?2 AND owner=?3 AND workspace_id=?4 AND status='pending' AND accepted_curated_id IS NULL AND content=?5 AND content_hash=?6",params![now,candidate,owner,workspace,current.proposal.text,crate::privacy::content_hash(&current.proposal.text)])?!=1 {return Err(RepositoryError::NotFound);}
    let prior_payload=row.payload.clone();let mut payload=row.payload;payload.state=CandidateState::Rejected;payload.reason="explicit human rejection".into();let actor=RevisionActor{actor_type:"user".into(),actor_id:owner.into(),reason:"explicit human rejection".into(),source_event_id:None};advance_explicit_candidate(tx,owner,candidate,expected_revision,&prior_payload,&payload,&actor,&now)?;Ok(())
}

/// Produce an unpersisted current-scope editor view after the exact stored
/// context and account custody are verified. Binding still names stored bytes.
pub(crate) fn preview_explicit_add_proposal_custody(tx:&Transaction<'_>,workspace:&str,candidate:&str,captured:&str,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<(ExplicitAddReviewBinding,ExplicitAddProposal),RepositoryError> {
    let proof=view.for_captured(tx,captured)?;let binding=explicit_add_review_binding_custody(tx,workspace,candidate,captured,view)?;
    if let Some(context)=binding.proposal.question_context.as_ref() {
        let scope_owner=context.get("scope").and_then(|s|s.get("owner")).and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
        view.for_captured(tx,scope_owner)?;
        checked_question_context_at(tx,proof.current_owner(),scope_owner,workspace,&binding.proposal.category,Some(context))?;
    }
    let mut display=binding.proposal.clone();
    if let Some(context)=display.question_context.as_mut() {
        if let Some(scope)=context.get_mut("scope") {
            let old=scope.get("owner").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?.to_owned();view.for_captured(tx,&old)?;
            if *scope!=json!({"owner":old,"workspace_id":workspace,"project_id":""}) {return Err(RepositoryError::NotFound);}
            scope["owner"]=json!(proof.current_owner());
        }
    }
    Ok((binding,display))
}


pub(crate) fn edit_staged_import_review_custody(
    tx:&Transaction<'_>, witness:&crate::store::sqlite::selective_import::VerifiedStagedImportCustody<'_,'_>,
    expected_revision:i64, expected_binding:&str, reviewed:&Value, budget:&mut ResetFenceBudget,
)->Result<(),RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    if expected_revision!=witness.revision() || !valid_sha256(expected_binding) || expected_binding!=witness.binding_sha256() {return Err(RepositoryError::Validation("import review binding changed".into()));}
    let selector=witness.selector();let current_owner=witness.current_owner();
    let (filtered,_)=crate::privacy::filter_json(reviewed);
    if contains_import_not_stored(&filtered) {return Err(RepositoryError::NotFound);}
    checked_import_proposal_at(tx,current_owner,current_owner,&selector.workspace_id,&filtered)?;
    let mut next=witness.proposal().clone();
    let old_text=next.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?.to_owned();
    let text=filtered["text"].as_str().ok_or(RepositoryError::NotFound)?;
    let category=filtered["category"].as_str().ok_or(RepositoryError::NotFound)?;
    let kind=match category {"identity"=>"persona","preference"=>"instruction","task"=>"episodic","unknown"=>"unknown","fact"|"contact"|"project"|"goal"=>"fact",_=>return Err(RepositoryError::NotFound)};
    let now=chrono::Utc::now().to_rfc3339();
    if tx.execute("UPDATE candidates SET content=?1,kind=?2,content_hash=?3,updated_at=?4 WHERE id=?5 AND owner=?6 AND workspace_id=?7 AND status='pending' AND content=?8 AND content_hash=?9 AND accepted_curated_id IS NULL",params![text,kind,crate::privacy::content_hash(text),now,next.candidate_id,current_owner,selector.workspace_id,old_text,crate::privacy::content_hash(&old_text)])?!=1 {return Err(RepositoryError::NotFound);}
    let row=read_candidate(tx,current_owner,&next.candidate_id,&next.candidate_id,expected_revision,&selector.workspace_id,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(&next)? {return Err(RepositoryError::NotFound);}
    next.stored_proposal_sha256=sha256_hex(serde_json::to_string(&filtered)?.as_bytes()); next.proposal=filtered;
    let prior_payload=row.payload.clone();let mut payload=row.payload; payload.proposal=serde_json::to_value(&next)?;
    let actor=RevisionActor{actor_type:"user".into(),actor_id:current_owner.to_owned(),reason:"edit reviewed import".into(),source_event_id:Some(next.source_event_id.clone())};
    advance_explicit_candidate(tx,current_owner,&next.candidate_id,expected_revision,&prior_payload,&payload,&actor,&now)?;
    Ok(())
}


pub(crate) fn prepare_staged_import_record_custody(tx:&Transaction<'_>,witness:&crate::store::sqlite::selective_import::VerifiedStagedImportCustody<'_,'_>,budget:&mut ResetFenceBudget)->Result<crate::record::MemoryRecord,RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    let s=witness.selector(); let p=witness.proposal();let current_owner=witness.current_owner();
    let candidate=tx.query_row("SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=? AND owner=? AND workspace_id=? AND status='pending'",params![p.candidate_id,current_owner,s.workspace_id],crate::store::sqlite::SqliteStore::candidate_row).optional()?.ok_or(RepositoryError::NotFound)?;
    let mut record=crate::provider::native::reviewed_candidate_record(&candidate,current_owner,&s.workspace_id,"reviewed staged import");
    record.source="memory_import".into(); record.source_type=crate::record::SourceType::AutoExtracted;
    record.metadata["category"]=p.proposal["category"].clone(); record.metadata["source_event_id"]=json!(p.source_event_id); record.metadata["import_review"]=super::knowledge_snapshot::safe_provenance(&p.provenance).map_err(RepositoryError::Validation)?;
    for key in ["question_context","entity_match_candidates","subject_attribution","subject_role"] {if let Some(value)=p.proposal.get(key) {record.metadata[key]=value.clone();}}
    if let Some(value)=p.proposal.get("document_role") {record.metadata["source_document_role"]=value.clone();}
    if p.proposal["category"]=="identity" {record.tags=vec!["self".into()];}
    let (filtered,redactions)=crate::privacy::filter_json(&record.metadata);
    if redactions!=0 || filtered!=record.metadata || contains_import_not_stored(&record.metadata) {return Err(RepositoryError::NotFound);}
    Ok(record)
}
/// Apply exactly one later human decision. The caller checks original status
/// first and rolls back the entire borrowed transaction on any error. For an
/// acceptance it has already upserted and exactly read back `record`.
pub(crate) fn finish_staged_import_decision_custody(tx:&Transaction<'_>,witness:&crate::store::sqlite::selective_import::VerifiedStagedImportCustody<'_,'_>,review_key:&str,expected_revision:i64,expected_binding:&str,action:&str,record:Option<&crate::record::MemoryRecord>,budget:&mut ResetFenceBudget)->Result<(StagedImportDecisionReceipt,Option<KnowledgeRevision>),RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    let s=witness.selector(); let p=witness.proposal();let current_owner=witness.current_owner();
    if !valid_receipt_text(review_key) || expected_revision!=witness.revision() || !valid_sha256(expected_binding) || expected_binding!=witness.binding_sha256() || read_staged_import_decision_receipt(tx,current_owner,&s.workspace_id,&s.account_id,&p.candidate_id)?.is_some() {return Err(RepositoryError::NotFound);}
    let row=read_candidate(tx,current_owner,&p.candidate_id,&p.candidate_id,expected_revision,&s.workspace_id,"")?;
    if row.payload.state!=CandidateState::Proposed || row.payload.proposal!=serde_json::to_value(p)? || row.payload.accepted_block_id.is_some() {return Err(RepositoryError::NotFound);}
    let id=staged_import_decision_receipt_id(current_owner,&s.workspace_id,&s.account_id,&p.candidate_id)?;
    let prior_payload=row.payload.clone();let mut payload=row.payload;
    let (curated,knowledge)=match (action,record) {
        ("reject",None)=>{payload.state=CandidateState::Rejected; payload.reason="human rejected staged import".into();(None,None)},
        ("accept",Some(record))=>{
            let expected=prepare_staged_import_record_custody(tx,witness,budget)?;
            if record.id!=expected.id || record.content!=expected.content || record.kind!=expected.kind || record.owner!=expected.owner || record.workspace_id!=expected.workspace_id || record.source!=expected.source || record.source_type!=expected.source_type || record.metadata!=expected.metadata || record.tags!=expected.tags || record.confidence_score!=expected.confidence_score || record.trust_score!=expected.trust_score {return Err(RepositoryError::NotFound);}
            let category=p.proposal["category"].as_str().ok_or(RepositoryError::NotFound)?;
            let text=p.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?;
            let context=if category=="unknown" {
                let context=p.proposal.get("question_context");
                let context_owner=context.and_then(|c|c.get("scope")).and_then(|v|v.get("owner")).and_then(Value::as_str).unwrap_or(&s.owner);
                witness.view().for_captured(tx,context_owner)?;
                checked_question_context_at(tx,current_owner,context_owner,&s.workspace_id,category,context)?
            } else {None};
            if category=="unknown" && !question_semantics_capability(tx)? {return Err(RepositoryError::NotFound);}
            let provenance=json!({"action":"import_staged_review","source":"memory_import","source_type":"auto_extracted","reviewed_by":current_owner,"import_review":super::knowledge_snapshot::safe_provenance(&p.provenance).map_err(RepositoryError::Validation)?});
            let typed=TypedPublication{owner:current_owner,workspace_id:&s.workspace_id,category,text,request_key:&id,record,context,reason:"reviewed staged import",source_type:"auto_extracted",source_event_id:&p.source_event_id,provenance};
            let result=publish_typed_core(tx,&typed)?; payload.state=CandidateState::Activated; payload.accepted_block_id=Some(result.revision.identity.block_id.clone());
            (Some(record.id.clone()),Some(result.revision))
        },_=>return Err(RepositoryError::NotFound),
    };
    let receipt=StagedImportDecisionReceipt{version:"s03.import-staged-review.v1".into(),owner:current_owner.to_owned(),workspace_id:s.workspace_id.clone(),account_id:s.account_id.clone(),candidate_id:p.candidate_id.clone(),review_key:review_key.into(),expected_revision,expected_binding:expected_binding.into(),current_proposal_sha256:p.stored_proposal_sha256.clone(),action:action.into(),curated_id:curated.clone()};
    write_staged_import_decision_receipt(tx,&receipt)?;
    if let Some(revision)=knowledge.as_ref() {
        super::import_mirror::write_staged_import_typed_review_receipt_custody(tx,witness,&receipt,revision,budget)?;
    }
    let now=chrono::Utc::now().to_rfc3339(); let text=p.proposal["text"].as_str().ok_or(RepositoryError::NotFound)?;
    if tx.execute("UPDATE candidates SET status=?1,reason=?2,accepted_curated_id=?3,updated_at=?4 WHERE id=?5 AND owner=?6 AND workspace_id=?7 AND status='pending' AND accepted_curated_id IS NULL AND content=?8 AND content_hash=?9",params![if action=="accept" {"accepted"} else {"rejected"},if action=="accept" {"reviewed staged import"} else {"human rejected staged import"},curated,now,p.candidate_id,current_owner,s.workspace_id,text,crate::privacy::content_hash(text)])?!=1 {return Err(RepositoryError::NotFound);}
    let actor=RevisionActor{actor_type:"user".into(),actor_id:current_owner.to_owned(),reason:if action=="accept" {"reviewed staged import"} else {"human rejected staged import"}.into(),source_event_id:Some(p.source_event_id.clone())};
    advance_explicit_candidate(tx,current_owner,&p.candidate_id,expected_revision,&prior_payload,&payload,&actor,&now)?;
    Ok((receipt,knowledge))
}
