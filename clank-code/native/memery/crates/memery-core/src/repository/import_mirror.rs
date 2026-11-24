//! Import-origin typed mirror proof. Original import receipt and guarded create
//! are the authorities; the candidate_review outbox is only an index projection.
use super::*;
use crate::store::sqlite::selective_import::{ImportReviewSelector,VerifiedImportReview,VerifiedStagedImport,VerifiedStagedImportCustody};
use super::explicit_add::{ImportReviewProposal,ImportReviewReceipt,StagedImportDecisionReceipt,read_import_review_receipt,read_staged_import_decision_receipt,read_staged_import_decision_receipt_custody,staged_import_decision_receipt_id};

/// Prove the exact canonical human activation chain, including each bounded
/// revision, its captured actor/change set, and its matching outbox projection.
fn verify_import_activation_chain(tx:&Transaction<'_>,current:&str,workspace:&str,candidate:&str,selected:&CandidateRevision,actor:&str,reason:&str,source_event:&str,block:&str)->Result<(),RepositoryError> {
    if selected.payload.state!=CandidateState::Proposed || selected.payload.accepted_block_id.is_some() || selected.content_hash!=hash_value(&serde_json::to_value(&selected.payload)?)? {return Err(RepositoryError::NotFound);}
    let head:i64=tx.query_row("SELECT current_revision FROM fm_v2_candidates WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND candidate_id=?3 AND idempotency_key=?3",params![current,workspace,candidate],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    if selected.revision.checked_add(3)!=Some(head) {return Err(RepositoryError::NotFound);}
    let mut prior=selected.revision;let mut changes=std::collections::BTreeSet::new();changes.insert(selected.change_set_id.clone());
    for state in [CandidateState::Validating,CandidateState::AwaitingReview,CandidateState::Activated] {
        let next=prior.checked_add(1).ok_or(RepositoryError::NotFound)?;
        let size:Option<i64>=tx.query_row("SELECT length(CAST(proposal_json AS BLOB)) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=?3",params![current,candidate,next],|r|r.get(0)).optional()?;
        if !size.is_some_and(|n|(1..=262144).contains(&n)) {return Err(RepositoryError::NotFound);}
        let row=read_candidate(tx,current,candidate,candidate,next,workspace,"")?;
        let mut expected=selected.payload.clone();expected.state=state.clone();if state==CandidateState::Activated {expected.accepted_block_id=Some(block.to_owned());}
        if row.previous_revision!=Some(prior) || row.payload!=expected || row.actor_type!="user" || row.actor_id!=actor || row.source_event_id.as_deref()!=Some(source_event) || row.content_hash!=hash_value(&serde_json::to_value(&row.payload)?)? || !changes.insert(row.change_set_id.clone()) {return Err(RepositoryError::NotFound);}
        let change:Option<(String,String,String,Option<String>,String,String)>=tx.query_row("SELECT actor_type,actor_id,reason,source_event_id,contract_version,content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![current,row.change_set_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
        if change!=Some(("user".into(),actor.into(),reason.into(),Some(source_event.into()),CONTRACT_VERSION.into(),row.content_hash.clone())) {return Err(RepositoryError::NotFound);}
        let event:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_revision'",params![current,format!("outbox_{}_candidate_revision",row.change_set_id),row.change_set_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let (raw,event_revision)=event.ok_or(RepositoryError::NotFound)?;if event_revision!=next || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=json!({"candidate_id":candidate,"revision":next}) {return Err(RepositoryError::NotFound);}
        prior=next;
    }
    Ok(())
}

fn root_binding(tx:&Transaction<'_>,receipt:&ImportReviewReceipt,decision:Option<&StagedImportDecisionReceipt>,proposal:&ImportReviewProposal,revision:&KnowledgeRevision,historical:Option<&CuratedMirrorBinding>)->Result<CuratedMirrorBinding,RepositoryError> {
    root_binding_at(tx,receipt,decision,proposal,revision,historical,&receipt.owner,None)
}
fn root_binding_at(tx:&Transaction<'_>,receipt:&ImportReviewReceipt,decision:Option<&StagedImportDecisionReceipt>,proposal:&ImportReviewProposal,revision:&KnowledgeRevision,historical:Option<&CuratedMirrorBinding>,current:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_, '_>>)->Result<CuratedMirrorBinding,RepositoryError> {
    let original_owner=&receipt.owner;let owner=decision.map(|d|&d.owner).unwrap_or(original_owner);let workspace=&receipt.workspace_id;
    custody_owner(tx,current,original_owner,&receipt.account_id,view)?;
    custody_owner(tx,current,owner,&receipt.account_id,view)?;
    let (curated,stored_hash,reason,action,request_key)=if let Some(d)=decision {
        if receipt.mode!="review_only" || receipt.publication_kind!="candidate" || receipt.publication_id!=receipt.candidate_id || d.action!="accept" || d.workspace_id!=*workspace || d.account_id!=receipt.account_id || d.candidate_id!=receipt.candidate_id {return Err(RepositoryError::NotFound);}
        (d.curated_id.as_ref().ok_or(RepositoryError::NotFound)?,d.current_proposal_sha256.as_str(),"reviewed staged import","import_staged_review",staged_import_decision_receipt_id(owner,workspace,&receipt.account_id,&receipt.candidate_id)?)
    } else {
        if receipt.mode!="manual" || receipt.publication_kind!="memory" {return Err(RepositoryError::NotFound);}
        let suffix=receipt.candidate_id.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
        (&receipt.publication_id,receipt.stored_proposal_sha256.as_str(),"reviewed import","import_review",format!("cs_import_review_{suffix}"))
    };
    if proposal.version!="s03.import-review-proposal.v1"
        || proposal.owner!=*original_owner || proposal.workspace_id!=*workspace || proposal.account_id!=receipt.account_id || proposal.batch_id!=receipt.batch_id || proposal.item_id!=receipt.item_id || proposal.suggestion_id!=receipt.suggestion_id || proposal.review_key_hash!=receipt.review_key_hash || proposal.proposal_fingerprint!=receipt.proposal_fingerprint || proposal.initial_stored_proposal_sha256!=receipt.stored_proposal_sha256 || proposal.stored_proposal_sha256!=stored_hash || proposal.stored_provenance_sha256!=receipt.stored_provenance_sha256 || proposal.candidate_id!=receipt.candidate_id || proposal.source_event_id!=receipt.source_event_id
        || sha256_hex(&serde_json::to_vec(&proposal.proposal)?)!=proposal.stored_proposal_sha256 || sha256_hex(&serde_json::to_vec(&proposal.provenance)?)!=proposal.stored_provenance_sha256 {return Err(RepositoryError::NotFound);}
    let text=proposal.proposal.get("text").and_then(Value::as_str).filter(|v|!v.is_empty()).ok_or(RepositoryError::NotFound)?;
    let category=proposal.proposal.get("category").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    let question=category=="unknown";
    let expected_kind=match category {"identity"=>"persona","preference"=>"instruction","task"=>"episodic","unknown"=>"open_question","fact"|"contact"|"project"|"goal"=>"fact",_=>return Err(RepositoryError::NotFound)};
    if revision.payload.kind!=expected_kind || (historical.is_some() && !question) {return Err(RepositoryError::NotFound);}
    let source=format!("source_memory_{}",&sha256_hex(&serde_json::to_vec(&json!([owner,curated]))?)[..32]);let evidence=format!("evidence_{}_1",curated);
    if revision.scope.owner_id!=current || revision.scope.workspace_id.as_deref()!=Some(workspace.as_str()) || revision.scope.project_id.as_deref().is_some_and(|v|!v.is_empty()) || revision.actor_type!="user" || revision.actor_id!=*owner || revision.source_event_id.as_deref()!=Some(receipt.source_event_id.as_str()) || revision.payload.evidence_ids!=vec![evidence.clone()] {return Err(RepositoryError::NotFound);}
    let normalized=if question {json!({"type":"null"})} else {json!({"type":"string","value":text})};
    if serde_json::to_value(&revision.payload.value)?!=normalized || revision.payload.status!=if question {KnowledgeStatus::Open} else {KnowledgeStatus::Active} {return Err(RepositoryError::NotFound);}
    let context=proposal.proposal.get("question_context").cloned().unwrap_or(Value::Null);
    if question {
        let slot=if context.is_null() {curated.as_str()} else {
            let c=context.as_object().ok_or(RepositoryError::NotFound)?;
            if let Some(scope)=c.get("scope") {
                let captured=scope.get("owner").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
                custody_owner(tx,current,captured,&receipt.account_id,view)?;
                if *scope!=json!({"owner":captured,"workspace_id":workspace,"project_id":""}) {return Err(RepositoryError::NotFound);}
            }
            if let Some(predicate)=c.get("predicate").and_then(Value::as_str).filter(|v|!v.is_empty()) {if predicate!=revision.identity.predicate {return Err(RepositoryError::NotFound);}}
            let target=c.get("target").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
            let kind=target.get("kind").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;let id=target.get("id").and_then(Value::as_str).filter(|v|!v.is_empty() && *v==v.trim() && v.len()<=512).ok_or(RepositoryError::NotFound)?;
            if target.len()!=2 || !matches!(kind,"entity"|"claim"|"claim_slot"|"source"|"evidence"|"media_asset"|"associated_text") || (kind=="entity" && id!=revision.identity.subject_entity_id) {return Err(RepositoryError::NotFound);}
            c.get("claim_slot").and_then(Value::as_str).filter(|v|!v.is_empty()).unwrap_or(curated)
        };
        if revision.identity.claim_slot!=slot {return Err(RepositoryError::NotFound);}
        let head:Option<i64>=tx.query_row("SELECT current_revision FROM fm_v2_entities WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND entity_id=?3",params![current,workspace,revision.identity.subject_entity_id],|r|r.get(0)).optional()?;
        let subject=read_entity(tx,current,&revision.identity.subject_entity_id,head.ok_or(RepositoryError::NotFound)?,workspace,"")?;
        if subject.payload.status!="active" {return Err(RepositoryError::NotFound);}
    }
    let visible=super::knowledge_snapshot::safe_provenance(&proposal.provenance).map_err(RepositoryError::Validation)?;
    let provenance=json!({"action":action,"source":"memory_import","source_type":"auto_extracted","reviewed_by":owner,"import_review":visible});
    let rationale=if question {json!({"reason":reason,"question_text":text,"question_context":context,"provenance":provenance})} else {json!({"reason":reason,"provenance":provenance})};
    if revision.payload.activation_rationale!=rationale {return Err(RepositoryError::NotFound);}
    let (semantic,curated_text,confidence,trust,tags)=if let Some(old)=historical {(old.semantic_sha256.clone(),text.to_owned(),revision.payload.confidence,revision.payload.trust,revision.payload.tags.clone())} else {match view {Some(v)=>persisted_curated_mirror_custody(tx,workspace,curated,owner,v)?,None=>persisted_curated_mirror(tx,owner,workspace,curated)?}};
    if curated_text!=text {return Err(RepositoryError::NotFound);}
    if historical.is_none() {
        let raw:String=tx.query_row("SELECT metadata FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![current,workspace,curated],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
        let metadata=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        if metadata["candidate_id"]!=receipt.candidate_id || metadata["source_event_id"]!=receipt.source_event_id || metadata["import_review"]!=visible || metadata.get("question_context").cloned().unwrap_or(Value::Null)!=context {return Err(RepositoryError::NotFound);}
    }
    let source_row:Option<(String,String,String,String,String)>=tx.query_row("SELECT workspace_key,project_key,source_uri,content_hash,source_type FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND source_revision=1 AND forget_state='active'",params![current,source],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    if source_row!=Some((workspace.clone(),String::new(),format!("memory://{curated}"),sha256_hex(&serde_json::to_vec(&text)?),"auto_extracted".into())) {return Err(RepositoryError::NotFound);}
    let erow:Option<(String,i64,String,String,Option<String>,String,String,String,String)>=tx.query_row("SELECT source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json FROM fm_v2_evidence WHERE owner_id=?1 AND evidence_id=?2",params![current,evidence],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?))).optional()?;
    let (es,er,lt,locator,quote,contract,parser,raw,value)=erow.ok_or(RepositoryError::NotFound)?;
    if es!=source || er!=1 || lt!="memory_record" || crate::field_authority::parse_unique_json(&locator).map_err(RepositoryError::Validation)?!=json!({"record_id":curated,"source_revision":1}) || quote.as_deref()!=Some(text) || contract!="provider-record-v1" || parser!="open-clank" || crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=json!(text) || crate::field_authority::parse_unique_json(&value).map_err(RepositoryError::Validation)?!=normalized {return Err(RepositoryError::NotFound);}
    let guarded=read_mirror_receipt_metadata(tx,current,&revision.change_set_id)?;let operation_raw=guarded["operation_json"].as_str().ok_or(RepositoryError::NotFound)?;let operation=crate::field_authority::parse_unique_json(operation_raw).map_err(RepositoryError::Validation)?;
    let paths=if question {json!(["/value","/activation_rationale"])} else {json!(["/value"])};
    if operation["version"]!="s03.published-create.v1" || operation["owner"]!=*owner || operation["workspace"]!=*workspace || operation["project"]!="" || operation["block_id"]!=revision.identity.block_id || operation["subject_entity_id"]!=revision.identity.subject_entity_id || operation["predicate"]!=revision.identity.predicate || operation["claim_slot"]!=revision.identity.claim_slot || operation["request_key"]!=request_key || operation["actor_type"]!="user" || operation["actor_id"]!=*owner || operation["reason"]!=reason || operation["source_event_id"]!=receipt.source_event_id || operation["approved_paths"]!=paths || operation["payload"]!=serde_json::to_value(&revision.payload)? {return Err(RepositoryError::NotFound);}
    let (replayed,_)=match view {Some(v)=>guarded_replay_custody(tx,workspace,"",&revision.identity.block_id,&revision.change_set_id,operation_raw,&sha256_hex(operation_raw.as_bytes()),owner,v)?,None=>guarded_replay(tx,owner,workspace,"",&revision.identity.block_id,&revision.change_set_id,operation_raw,&sha256_hex(operation_raw.as_bytes()))?}.ok_or(RepositoryError::NotFound)?;
    if serde_json::to_value(&replayed)?!=serde_json::to_value(revision)? {return Err(RepositoryError::NotFound);}
    for path in if question {vec!["/value","/activation_rationale"]} else {vec!["/value"]} {
        let origin:Option<String>=tx.query_row("SELECT origin FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=?3 AND field_path=?4",params![current,revision.identity.block_id,revision.revision,path],|r|r.get(0)).optional()?;
        if origin.as_deref()!=Some("manual") {return Err(RepositoryError::NotFound);}
    }
    let mut map=BTreeMap::new();map.insert("content".into(),if question {"/activation_rationale/question_text"} else {"/value"}.into());
    for (name,path,equal) in [("confidence_score","/confidence",revision.payload.confidence==confidence),("trust_score","/trust",revision.payload.trust==trust),("tags","/tags",revision.payload.tags==tags)] {let origin:Option<String>=tx.query_row("SELECT origin FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=?3 AND field_path=?4",params![current,revision.identity.block_id,revision.revision,path],|r|r.get(0)).optional()?;if equal && origin.as_deref()==Some("manual"){map.insert(name.into(),path.into());}}
    Ok(CuratedMirrorBinding{version:if question {"s03.import-question-mirror.v1"} else {"s03.curated-mirror.v1"}.into(),owner:owner.clone(),workspace:workspace.clone(),project:String::new(),curated_id:curated.clone(),block_id:revision.identity.block_id.clone(),revision:revision.revision,guarded_change_set_id:revision.change_set_id.clone(),semantic_sha256:semantic,field_map:map})
}

/// Write only the indexed projection, after the original once receipt exists.
/// The witness, receipt, guarded create and source are checked in the same tx.
pub(crate) fn write_import_typed_review_receipt(tx:&Transaction<'_>,witness:&VerifiedImportReview<'_,'_>,proposal:&ImportReviewProposal,revision:&KnowledgeRevision,budget:&mut ResetFenceBudget)->Result<String,RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;let s=witness.selector();
    let receipt=read_import_review_receipt(tx,s)?.ok_or(RepositoryError::NotFound)?;
    if receipt.mode!="manual" || receipt.candidate_id!=witness.candidate_id() || receipt.source_event_id!=witness.source_event_id() {return Err(RepositoryError::NotFound);}
    let (filtered,_)=crate::privacy::filter_json(witness.proposal());let (provenance,_)=crate::privacy::filter_json(witness.provenance());
    if filtered!=proposal.proposal || provenance!=proposal.provenance {return Err(RepositoryError::NotFound);}
    let first:String=tx.query_row("SELECT proposal_json FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=1",params![s.owner,receipt.candidate_id],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    if crate::field_authority::parse_unique_json(&first).map_err(RepositoryError::Validation)?!=serde_json::to_value(proposal)? {return Err(RepositoryError::NotFound);}
    let binding=root_binding(tx,&receipt,None,proposal,revision,None)?;
    let suffix=receipt.candidate_id.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;let once=format!("cs_import_review_{suffix}");let event_id=format!("outbox_{once}_candidate_review");
    let event=json!({"candidate_id":receipt.candidate_id,"curated_id":receipt.publication_id,"block_id":revision.identity.block_id,"revision":revision.revision,"reviewer_account_id":receipt.account_id,"receipt_version":"s03.import-review.v1","binding":binding});
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?1,?2,?3,'candidate_review',?4,?5,?6)",params![s.owner,event_id,once,serde_json::to_string(&event)?,revision.revision,chrono::Utc::now().to_rfc3339()])?;
    Ok(event_id)
}

/// Resolve the original imported root with a freshly authorized selector.
/// `historical` checks stored root evidence, never claims old curated bytes are
/// still current; the caller must independently prove the S09 successor.
pub(crate) fn resolve_import_typed_root(tx:&Transaction<'_>,selector:&ImportReviewIdentity,revision:&KnowledgeRevision,historical:bool)->Result<(CuratedMirrorBinding,String,String),RepositoryError> {
    let (receipt,_)=original_for_import_identity(tx,selector)?;
    let suffix=receipt.candidate_id.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;let once=format!("cs_import_review_{suffix}");
    let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review' AND source_revision=?4",params![selector.owner,format!("outbox_{once}_candidate_review"),once,revision.revision],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let event=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let recorded:CuratedMirrorBinding=serde_json::from_value(event["binding"].clone())?;
    if event!=json!({"candidate_id":receipt.candidate_id,"curated_id":receipt.publication_id,"block_id":revision.identity.block_id,"revision":revision.revision,"reviewer_account_id":receipt.account_id,"receipt_version":"s03.import-review.v1","binding":recorded}) {return Err(RepositoryError::NotFound);}
    let raw:String=tx.query_row("SELECT proposal_json FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=1",params![selector.owner,receipt.candidate_id],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let proposal:ImportReviewProposal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    let selected=read_candidate(tx,&selector.owner,&receipt.candidate_id,&receipt.candidate_id,1,&selector.workspace_id,"")?;
    verify_import_activation_chain(tx,&selector.owner,&selector.workspace_id,&receipt.candidate_id,&selected,&receipt.owner,"reviewed import",&receipt.source_event_id,&revision.identity.block_id)?;
    let actual=root_binding(tx,&receipt,None,&proposal,revision,if historical {Some(&recorded)} else {None})?;
    if serde_json::to_value(&actual)?!=serde_json::to_value(&recorded)? {return Err(RepositoryError::NotFound);}
    Ok((actual,receipt.candidate_id,receipt.account_id))
}

pub(crate) fn resolve_import_typed_root_custody(tx:&Transaction<'_>,selector:&ImportReviewIdentity,revision:&KnowledgeRevision,historical:bool,view:&super::owner_custody::VerifiedCustodyView<'_, '_>)->Result<(CuratedMirrorBinding,String,String),RepositoryError> {
    let (receipt,_)=original_for_import_identity_at(tx,selector,view.current_owner(),Some(view))?;
    let suffix=receipt.candidate_id.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;let once=format!("cs_import_review_{suffix}");
    let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review' AND source_revision=?4",params![view.current_owner(),format!("outbox_{once}_candidate_review"),once,revision.revision],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let event=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let recorded:CuratedMirrorBinding=serde_json::from_value(event["binding"].clone())?;
    if event!=json!({"candidate_id":receipt.candidate_id,"curated_id":receipt.publication_id,"block_id":revision.identity.block_id,"revision":revision.revision,"reviewer_account_id":receipt.account_id,"receipt_version":"s03.import-review.v1","binding":recorded}) {return Err(RepositoryError::NotFound);}
    let raw:String=tx.query_row("SELECT proposal_json FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=1",params![view.current_owner(),receipt.candidate_id],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let proposal:ImportReviewProposal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    let selected=read_candidate(tx,view.current_owner(),&receipt.candidate_id,&receipt.candidate_id,1,&selector.workspace_id,"")?;
    verify_import_activation_chain(tx,view.current_owner(),&selector.workspace_id,&receipt.candidate_id,&selected,&receipt.owner,"reviewed import",&receipt.source_event_id,&revision.identity.block_id)?;
    let actual=root_binding_at(tx,&receipt,None,&proposal,revision,if historical {Some(&recorded)} else {None},view.current_owner(),Some(view))?;
    if serde_json::to_value(&actual)?!=serde_json::to_value(&recorded)? {return Err(RepositoryError::NotFound);}
    Ok((actual,receipt.candidate_id,receipt.account_id))
}


/// Project a later human acceptance on its own decision change set; the
/// original review-only receipt remains unchanged. Caller has not yet CASed
/// the staged candidate, so the witness is still revalidatable.
pub(crate) fn write_staged_import_typed_review_receipt(tx:&Transaction<'_>,witness:&VerifiedStagedImport<'_,'_>,decision:&StagedImportDecisionReceipt,revision:&KnowledgeRevision,budget:&mut ResetFenceBudget)->Result<String,RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    let s=witness.selector();let original=witness.receipt();let proposal=witness.proposal();
    let stored=read_staged_import_decision_receipt(tx,&s.owner,&s.workspace_id,&s.account_id,&original.candidate_id)?.ok_or(RepositoryError::NotFound)?;
    if &stored!=decision || decision.action!="accept" || decision.expected_revision!=witness.revision() || decision.expected_binding!=witness.binding_sha256() || decision.current_proposal_sha256!=proposal.stored_proposal_sha256 {return Err(RepositoryError::NotFound);}
    let binding=root_binding(tx,original,Some(decision),proposal,revision,None)?;
    let id=staged_import_decision_receipt_id(&s.owner,&s.workspace_id,&s.account_id,&original.candidate_id)?;let event_id=format!("outbox_{id}_candidate_review");
    let event=json!({"candidate_id":original.candidate_id,"curated_id":decision.curated_id,"block_id":revision.identity.block_id,"revision":revision.revision,"reviewer_account_id":original.account_id,"receipt_version":"s03.import-staged-review.v1","binding":binding});
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?1,?2,?3,'candidate_review',?4,?5,?6)",params![s.owner,event_id,id,serde_json::to_string(&event)?,revision.revision,chrono::Utc::now().to_rfc3339()])?;
    Ok(event_id)
}

/// Project the current human decision without changing the original import
/// receipt. The same transaction and custody witness remain current until CAS.
pub(crate) fn write_staged_import_typed_review_receipt_custody(tx:&Transaction<'_>,witness:&VerifiedStagedImportCustody<'_,'_>,decision:&StagedImportDecisionReceipt,revision:&KnowledgeRevision,budget:&mut ResetFenceBudget)->Result<String,RepositoryError> {
    witness.revalidate(tx,budget).map_err(RepositoryError::Validation)?;
    let s=witness.selector();let original=witness.receipt();let proposal=witness.proposal();let current=witness.current_owner();
    let stored=read_staged_import_decision_receipt_custody(tx,&s.workspace_id,&s.account_id,&original.candidate_id,current,witness.view())?.ok_or(RepositoryError::NotFound)?;
    if &stored!=decision || decision.owner!=current || decision.action!="accept" || decision.expected_revision!=witness.revision() || decision.expected_binding!=witness.binding_sha256() || decision.current_proposal_sha256!=proposal.stored_proposal_sha256 {return Err(RepositoryError::NotFound);}
    let binding=root_binding_at(tx,original,Some(decision),proposal,revision,None,current,Some(witness.view()))?;
    let id=staged_import_decision_receipt_id(current,&s.workspace_id,&s.account_id,&original.candidate_id)?;let event_id=format!("outbox_{id}_candidate_review");
    let event=json!({"candidate_id":original.candidate_id,"curated_id":decision.curated_id,"block_id":revision.identity.block_id,"revision":revision.revision,"reviewer_account_id":original.account_id,"receipt_version":"s03.import-staged-review.v1","binding":binding});
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?1,?2,?3,'candidate_review',?4,?5,?6)",params![current,event_id,id,serde_json::to_string(&event)?,revision.revision,chrono::Utc::now().to_rfc3339()])?;
    Ok(event_id)
}

/// Read later accepted root; the original import once receipt remains the
/// factual staged result. Historical question roots require a separately
/// verified S09 successor before a caller treats the answer as current.
pub(crate) fn resolve_staged_import_typed_root(tx:&Transaction<'_>,selector:&ImportReviewIdentity,revision:&KnowledgeRevision,historical:bool)->Result<(CuratedMirrorBinding,String,String),RepositoryError> {
    let (original,_)=original_for_import_identity(tx,selector)?;
    let decision=read_staged_import_decision_receipt(tx,&selector.owner,&selector.workspace_id,&selector.account_id,&original.candidate_id)?.ok_or(RepositoryError::NotFound)?;
    if decision.action!="accept" {return Err(RepositoryError::NotFound);}
    let id=staged_import_decision_receipt_id(&selector.owner,&selector.workspace_id,&selector.account_id,&original.candidate_id)?;
    let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review' AND source_revision=?4",params![selector.owner,format!("outbox_{id}_candidate_review"),id,revision.revision],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let event=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let recorded:CuratedMirrorBinding=serde_json::from_value(event["binding"].clone())?;
    if event!=json!({"candidate_id":original.candidate_id,"curated_id":decision.curated_id,"block_id":revision.identity.block_id,"revision":revision.revision,"reviewer_account_id":original.account_id,"receipt_version":"s03.import-staged-review.v1","binding":recorded}) {return Err(RepositoryError::NotFound);}
    let selected=read_candidate(tx,&selector.owner,&original.candidate_id,&original.candidate_id,decision.expected_revision,&selector.workspace_id,"")?;
    if selected.payload.state!=CandidateState::Proposed || selected.payload.accepted_block_id.is_some() {return Err(RepositoryError::NotFound);}
    let proposal:ImportReviewProposal=serde_json::from_value(selected.payload.proposal.clone())?;
    verify_import_activation_chain(tx,&selector.owner,&selector.workspace_id,&original.candidate_id,&selected,&decision.owner,"reviewed staged import",&original.source_event_id,&revision.identity.block_id)?;
    let legacy:Option<(String,Option<String>)>=tx.query_row("SELECT status,accepted_curated_id FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![selector.owner,selector.workspace_id,original.candidate_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if legacy!=Some(("accepted".into(),decision.curated_id.clone())) {return Err(RepositoryError::NotFound);}
    let actual=root_binding(tx,&original,Some(&decision),&proposal,revision,if historical {Some(&recorded)} else {None})?;
    if serde_json::to_value(&actual)?!=serde_json::to_value(&recorded)? {return Err(RepositoryError::NotFound);}
    Ok((actual,original.candidate_id,original.account_id))
}

pub(crate) fn resolve_staged_import_typed_root_custody(tx:&Transaction<'_>,selector:&ImportReviewIdentity,revision:&KnowledgeRevision,historical:bool,view:&super::owner_custody::VerifiedCustodyView<'_, '_>)->Result<(CuratedMirrorBinding,String,String),RepositoryError> {
    let (original,_)=original_for_import_identity_at(tx,selector,view.current_owner(),Some(view))?;
    let decision=read_staged_import_decision_receipt_custody(tx,&selector.workspace_id,&selector.account_id,&original.candidate_id,&revision.actor_id,view)?.ok_or(RepositoryError::NotFound)?;
    if decision.action!="accept" {return Err(RepositoryError::NotFound);}
    let id=staged_import_decision_receipt_id(&decision.owner,&selector.workspace_id,&selector.account_id,&original.candidate_id)?;
    let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review' AND source_revision=?4",params![view.current_owner(),format!("outbox_{id}_candidate_review"),id,revision.revision],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let event=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let recorded:CuratedMirrorBinding=serde_json::from_value(event["binding"].clone())?;
    if event!=json!({"candidate_id":original.candidate_id,"curated_id":decision.curated_id,"block_id":revision.identity.block_id,"revision":revision.revision,"reviewer_account_id":original.account_id,"receipt_version":"s03.import-staged-review.v1","binding":recorded}) {return Err(RepositoryError::NotFound);}
    let selected=read_candidate(tx,view.current_owner(),&original.candidate_id,&original.candidate_id,decision.expected_revision,&selector.workspace_id,"")?;
    if selected.payload.state!=CandidateState::Proposed || selected.payload.accepted_block_id.is_some() {return Err(RepositoryError::NotFound);}
    let proposal:ImportReviewProposal=serde_json::from_value(selected.payload.proposal.clone())?;
    verify_import_activation_chain(tx,view.current_owner(),&selector.workspace_id,&original.candidate_id,&selected,&decision.owner,"reviewed staged import",&original.source_event_id,&revision.identity.block_id)?;
    let legacy:Option<(String,Option<String>)>=tx.query_row("SELECT status,accepted_curated_id FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![view.current_owner(),selector.workspace_id,original.candidate_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if legacy!=Some(("accepted".into(),decision.curated_id.clone())) {return Err(RepositoryError::NotFound);}
    let actual=root_binding_at(tx,&original,Some(&decision),&proposal,revision,if historical {Some(&recorded)} else {None},view.current_owner(),Some(view))?;
    if serde_json::to_value(&actual)?!=serde_json::to_value(&recorded)? {return Err(RepositoryError::NotFound);}
    Ok((actual,original.candidate_id,original.account_id))
}


fn valid_receipt_text(value:&str)->bool {
    !value.is_empty() && value==value.trim() && value.len()<=512 && !value.chars().any(char::is_control)
}

/// Content-free original identity for reset/status, independent of policy and
/// mode. Current account and reset authority remain the caller's obligation.
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportReviewIdentity {
    pub owner:String,pub workspace_id:String,pub account_id:String,pub batch_id:String,pub item_id:String,
    pub suggestion_id:String,pub review_key_hash:String,pub proposal_fingerprint:String,
}
impl From<&ImportReviewSelector> for ImportReviewIdentity {
    fn from(s:&ImportReviewSelector)->Self {Self{owner:s.owner.clone(),workspace_id:s.workspace_id.clone(),account_id:s.account_id.clone(),batch_id:s.batch_id.clone(),item_id:s.item_id.clone(),suggestion_id:s.suggestion_id.clone(),review_key_hash:s.review_key_hash.clone(),proposal_fingerprint:s.proposal_fingerprint.clone()}}
}
pub(crate) fn original_for_import_identity(tx:&Transaction<'_>,s:&ImportReviewIdentity)->Result<(ImportReviewReceipt,String),RepositoryError> {
    original_for_import_identity_at(tx,s,&s.owner,None)
}
fn custody_owner(tx:&Transaction<'_>,current:&str,captured:&str,account:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_, '_>>)->Result<(),RepositoryError> {
    match view {
        Some(v) if v.current_owner()==current && v.account_id()==account => {v.for_captured(tx,captured)?; Ok(())},
        None if current==captured => Ok(()),
        _=>Err(RepositoryError::NotFound),
    }
}
pub(crate) fn original_for_import_identity_at(tx:&Transaction<'_>,s:&ImportReviewIdentity,current:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_, '_>>)->Result<(ImportReviewReceipt,String),RepositoryError> {
    custody_owner(tx,current,&s.owner,&s.account_id,view)?;
    if ![s.owner.as_str(),s.workspace_id.as_str(),s.account_id.as_str(),s.batch_id.as_str(),s.item_id.as_str(),s.suggestion_id.as_str()].iter().all(|v|valid_receipt_text(v)) || !valid_sha256(&s.review_key_hash) || !valid_sha256(&s.proposal_fingerprint) {return Err(RepositoryError::NotFound);}
    let digest=sha256_hex(&serde_json::to_vec(&("s03.import-review-id.v1",&s.account_id,&s.workspace_id,&s.batch_id,&s.item_id,&s.suggestion_id,&s.review_key_hash))?);
    let candidate=format!("importreview_{digest}");let id=format!("cs_import_review_{digest}");let source_event=format!("memory-import-review:{}:{}",s.batch_id,s.suggestion_id);
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s03.import-review.v1'",params![current,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (hash,raw,actor,actor_id,contract,source)=row.ok_or(RepositoryError::NotFound)?;
    let item:ImportReviewReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=s.owner || contract!=CONTRACT_VERSION || source.as_deref()!=Some(source_event.as_str()) || item.version!="s03.import-review.v1" || item.owner!=s.owner || item.workspace_id!=s.workspace_id || item.account_id!=s.account_id || item.batch_id!=s.batch_id || item.item_id!=s.item_id || item.suggestion_id!=s.suggestion_id || item.review_key_hash!=s.review_key_hash || item.proposal_fingerprint!=s.proposal_fingerprint || item.candidate_id!=candidate || item.source_event_id!=source_event || !valid_sha256(&item.stored_proposal_sha256) || !valid_sha256(&item.stored_provenance_sha256) || !match (item.mode.as_str(),item.publication_kind.as_str()) {("review_only","candidate")=>item.publication_id==candidate,("manual","memory")=>valid_receipt_text(&item.publication_id),_=>false} {return Err(RepositoryError::NotFound);}
    let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='import_review'",params![current,format!("outbox_{id}_import_review"),id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if out.as_ref().is_none_or(|(payload,rev)|payload!=&raw || *rev!=0) {return Err(RepositoryError::NotFound);} Ok((item,hash))
}

/// Content-free original import erasure fact. It grants no write or recovery.
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportReviewTerminal {
    pub version:String,pub owner:String,pub workspace_id:String,pub reset_workspace_id:String,pub account_id:String,
    pub candidate_id:String,pub original_receipt_id:String,pub original_receipt_sha256:String,
    pub decision_key_sha256:Option<String>,pub decision_action:Option<String>,pub decision_revision:Option<i64>,pub decision_binding:Option<String>,
    pub operation_id:String,pub mode:String,pub partition_sha256:Option<String>,pub reset_receipt_id:String,
}
pub(crate) fn import_review_decision_key_sha256(account:&str,workspace:&str,candidate:&str,key:&str)->Result<String,RepositoryError> {
    if ![account,workspace,key].iter().all(|v|valid_receipt_text(v)) || !candidate.strip_prefix("importreview_").is_some_and(valid_sha256) {return Err(RepositoryError::NotFound);}
    Ok(sha256_hex(&serde_json::to_vec(&("s04.import-staged-review-key.v1",account,workspace,candidate,key))?))
}
fn import_terminal_event(candidate:&str)->Result<String,RepositoryError> {
    let suffix=candidate.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
    Ok(format!("outbox_s04_import_review_erased_{suffix}"))
}
fn verify_import_terminal_base(tx:&Transaction<'_>,s:&ImportReviewIdentity,m:&ImportReviewTerminal)->Result<(),RepositoryError> {
    verify_import_terminal_base_at(tx,s,m,&s.owner,None)
}
fn verify_import_terminal_base_at(tx:&Transaction<'_>,s:&ImportReviewIdentity,m:&ImportReviewTerminal,current:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_, '_>>)->Result<(),RepositoryError> {
    custody_owner(tx,current,&s.owner,&s.account_id,view)?;
    custody_owner(tx,current,&m.owner,&m.account_id,view)?;
    if m.version!="s04.import-review-erased.v1" || ![m.owner.as_str(),m.workspace_id.as_str(),m.reset_workspace_id.as_str(),m.account_id.as_str(),m.operation_id.as_str()].iter().all(|v|valid_receipt_text(v)) || m.workspace_id!=s.workspace_id || m.account_id!=s.account_id || !m.candidate_id.strip_prefix("importreview_").is_some_and(valid_sha256) || !valid_sha256(&m.original_receipt_sha256) || m.reset_receipt_id!=ingest_erased_receipt_id(&m.account_id,&m.operation_id)? {return Err(RepositoryError::NotFound);}
    let (original,original_hash)=original_for_import_identity_at(tx,s,current,view)?;
    let suffix=original.candidate_id.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
    if original.candidate_id!=m.candidate_id || m.original_receipt_id!=format!("cs_import_review_{suffix}") || m.original_receipt_sha256!=original_hash {return Err(RepositoryError::NotFound);}
    let decision_ok=match (&m.decision_key_sha256,&m.decision_action,m.decision_revision,&m.decision_binding) {
        (None,None,None,None)=>true,
        (Some(key),Some(action),Some(revision),Some(binding))=>valid_sha256(key) && matches!(action.as_str(),"accept"|"reject") && revision>0 && valid_sha256(binding),
        _=>false,
    };
    if !decision_ok || !match (m.mode.as_str(),&m.partition_sha256) {("full",None)=>true,("selective",Some(hash))=>valid_sha256(hash),_=>false} {return Err(RepositoryError::NotFound);}
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",params![current,m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (hash,raw,actor,actor_id,contract,source)=row.ok_or(RepositoryError::NotFound)?;let reset=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=m.owner || contract!=CONTRACT_VERSION || source.is_some() || reset.as_object().map(|v|v.len())!=Some(4) || reset["version"]!="s06.guarded-reset.v1" || reset["request_sha256"]!=sha256_hex(&serde_json::to_vec(&reset["request"])?) || reset["request"]["owner"]!=m.owner || reset["request"]["workspace_id"]!=m.reset_workspace_id || reset["request"]["account_id"]!=m.account_id || reset["request"]["operation_id"]!=m.operation_id || reset["result"]["complete"]!=true {return Err(RepositoryError::NotFound);}
    let components=reset["request"]["components"].as_array().ok_or(RepositoryError::NotFound)?;let has=|name:&str|components.iter().any(|v|v.as_str()==Some(name));
    match m.mode.as_str() {
        "full" if has("memories") && reset["request"].get("selective_manifest_sha256").is_none() && reset["result"]["categories"]["memories"]["state"]=="complete"=>(),
        "selective" if has("ingest") && reset["request"]["selective_manifest_sha256"].as_str().is_some_and(valid_sha256) && reset["result"]["categories"]["ingest"]["state"]=="complete" && reset["result"]["categories"]["ingest"]["selective"]["partition_sha256"].as_str()==m.partition_sha256.as_deref()=>(),
        _=>return Err(RepositoryError::NotFound),
    }
    let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![current,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id),m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if out.as_ref().is_none_or(|(payload,rev)|payload!=&raw || *rev!=0) {return Err(RepositoryError::NotFound);} Ok(())
}
/// Called by the existing reset owner after original S06 receipt is inserted,
/// while the original staged decision (if any) still exists in this same tx.
pub(crate) fn write_import_review_terminal(tx:&Transaction<'_>,s:&ImportReviewIdentity,m:&ImportReviewTerminal)->Result<(),RepositoryError> {
    write_import_review_terminal_at(tx,s,m,&s.owner,None)
}
pub(crate) fn write_import_review_terminal_custody(tx:&Transaction<'_>,s:&ImportReviewIdentity,m:&ImportReviewTerminal,view:&super::owner_custody::VerifiedCustodyView<'_,'_>)->Result<(),RepositoryError> {
    write_import_review_terminal_at(tx,s,m,view.current_owner(),Some(view))
}
fn write_import_review_terminal_at(tx:&Transaction<'_>,s:&ImportReviewIdentity,m:&ImportReviewTerminal,current:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_,'_>>)->Result<(),RepositoryError> {
    verify_import_terminal_base_at(tx,s,m,current,view)?;
    let decision=if let Some(view)=view {
        let mut found=None;
        for captured in view.owners() {
            if let Some(value)=read_staged_import_decision_receipt_custody(tx,&m.workspace_id,&m.account_id,&m.candidate_id,captured,view)? {
                if found.replace(value).is_some(){return Err(RepositoryError::NotFound)}
            }
        }
        found
    } else {read_staged_import_decision_receipt(tx,&m.owner,&m.workspace_id,&m.account_id,&m.candidate_id)?};
    match (decision,&m.decision_key_sha256,&m.decision_action,m.decision_revision,&m.decision_binding) {
        (None,None,None,None,None)=>(),
        (Some(d),Some(key),Some(action),Some(revision),Some(binding)) if *key==import_review_decision_key_sha256(&m.account_id,&m.workspace_id,&m.candidate_id,&d.review_key)? && *action==d.action && revision==d.expected_revision && *binding==d.expected_binding=>(),
        _=>return Err(RepositoryError::NotFound),
    }
    let event=import_terminal_event(&m.candidate_id)?;let raw=serde_json::to_string(m)?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?1,?2,?3,'s04_import_review_erased',?4,0,?5)",params![current,event,m.reset_receipt_id,raw,chrono::Utc::now().to_rfc3339()])?;
    if read_import_review_terminal_at(tx,s,current,view)?.as_ref()!=Some(m) {return Err(RepositoryError::NotFound);} Ok(())
}
/// Factual/deny-only read; absence is not a grant. Caller separately applies
/// fresh human/account authority and never infers current candidate content.
pub(crate) fn read_import_review_terminal(tx:&Transaction<'_>,s:&ImportReviewIdentity)->Result<Option<ImportReviewTerminal>,RepositoryError> {
    read_import_review_terminal_at(tx,s,&s.owner,None)
}
pub(crate) fn read_import_review_terminal_at(tx:&Transaction<'_>,s:&ImportReviewIdentity,current:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_, '_>>)->Result<Option<ImportReviewTerminal>,RepositoryError> {
    custody_owner(tx,current,&s.owner,&s.account_id,view)?;
    let digest=sha256_hex(&serde_json::to_vec(&("s03.import-review-id.v1",&s.account_id,&s.workspace_id,&s.batch_id,&s.item_id,&s.suggestion_id,&s.review_key_hash))?);
    let candidate=format!("importreview_{digest}");let event=import_terminal_event(&candidate)?;
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT kind,change_set_id,source_revision,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![current,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let Some((kind,change,revision,raw))=row else {return Ok(None)};
    if kind!="s04_import_review_erased" || revision!=0 {return Err(RepositoryError::NotFound);}
    let marker:ImportReviewTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if marker.candidate_id!=candidate || marker.reset_receipt_id!=change {return Err(RepositoryError::NotFound);}
    verify_import_terminal_base_at(tx,s,&marker,current,view)?;Ok(Some(marker))
}

/// Resolve a factual original identity without inventing a policy value.
/// The named-human/session authorization belongs to the caller.
pub(crate) fn import_identity_for_candidate(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<ImportReviewIdentity,RepositoryError> {
    import_identity_for_candidate_at(tx,owner,workspace,account,candidate,None)
}
pub(crate) fn import_identity_for_candidate_at(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str,view:Option<&super::owner_custody::VerifiedCustodyView<'_, '_>>)->Result<ImportReviewIdentity,RepositoryError> {
    let suffix=candidate.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
    if ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) {return Err(RepositoryError::NotFound);}
    let id=format!("cs_import_review_{suffix}");
    let raw:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s03.import-review.v1'",params![owner,id],|r|r.get(0)).optional()?.flatten();
    let receipt:ImportReviewReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw.ok_or(RepositoryError::NotFound)?).map_err(RepositoryError::Validation)?)?;
    custody_owner(tx,owner,&receipt.owner,account,view)?;
    if receipt.workspace_id!=workspace || receipt.account_id!=account || receipt.candidate_id!=candidate {return Err(RepositoryError::NotFound);}
    let identity=ImportReviewIdentity{owner:receipt.owner.clone(),workspace_id:workspace.into(),account_id:account.into(),batch_id:receipt.batch_id.clone(),item_id:receipt.item_id.clone(),suggestion_id:receipt.suggestion_id.clone(),review_key_hash:receipt.review_key_hash.clone(),proposal_fingerprint:receipt.proposal_fingerprint.clone()};
    let (verified,_)=original_for_import_identity_at(tx,&identity,owner,view)?;if verified!=receipt {return Err(RepositoryError::NotFound);}Ok(identity)
}
/// Exact factual root for current-mirror/S09 owners; never grants a new write.
pub(crate) fn resolve_import_root_fact(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str,revision:&KnowledgeRevision,historical:bool)->Result<(CuratedMirrorBinding,String,String),RepositoryError> {
    let identity=import_identity_for_candidate(tx,owner,workspace,account,candidate)?;
    let (original,_)=original_for_import_identity(tx,&identity)?;
    match original.mode.as_str() {
        "manual"=>resolve_import_typed_root(tx,&identity,revision,historical),
        "review_only"=>resolve_staged_import_typed_root(tx,&identity,revision,historical),
        _=>Err(RepositoryError::NotFound),
    }
}

/// SQL-side byte-length preflight. Table/alias/query tails are fixed internal
/// call-site constants; no row content is hydrated into Rust by this helper.
pub(crate) fn s09_charge_table_rows(
    tx:&Transaction<'_>,table:&str,alias:&str,tail:&str,args:&[&dyn rusqlite::ToSql],
    maximum:usize,budget:&mut ResetFenceBudget,
)->Result<(),RepositoryError>{
    if !matches!(table,"fm_v2_candidates"|"fm_v2_candidate_revisions"|"fm_v2_candidate_evidence"|"fm_v2_evidence"|"fm_v2_sources"|"candidates"|"fm_v2_knowledge_blocks"|"fm_v2_knowledge_revisions"|"fm_v2_revision_metadata"|"fm_v2_revision_field_authority"|"fm_v2_revision_evidence"|"fm_v2_entities"|"fm_v2_entity_revisions"|"curated"|"fm_v2_change_sets"|"fm_v2_outbox") || !alias.bytes().all(|v|v.is_ascii_lowercase()) || alias.is_empty() {return Err(RepositoryError::NotFound)}
    let mut schema=tx.prepare("SELECT name FROM pragma_table_info(?1) ORDER BY cid")?;
    let names=schema.query_map([table],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;drop(schema);
    if names.is_empty() || names.len()>128 || names.iter().any(|v|v.is_empty() || !v.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'_')) {return Err(RepositoryError::NotFound)}
    let mut terms=Vec::new();for name in &names {if table=="curated" && name=="embedding" {continue}terms.push(format!("coalesce(length(CAST({alias}.\"{name}\" AS BLOB)),0)"));}
    if terms.is_empty(){return Err(RepositoryError::NotFound)}
    let sql=format!("SELECT {} {tail}",terms.join("+"));let mut stmt=tx.prepare(&sql)?;let mut rows=stmt.query(args)?;let mut count=0usize;
    while let Some(row)=rows.next()? {count=count.checked_add(1).ok_or(RepositoryError::NotFound)?;if count>maximum{return Err(RepositoryError::NotFound)}let bytes:i64=row.get(0)?;if !(0..=524288).contains(&bytes){return Err(RepositoryError::NotFound)}budget.check((bytes as usize).saturating_add(names.len()*8)).map_err(RepositoryError::Validation)?;}
    Ok(())
}

/// Bound exact import-root dependencies before the unchanged custody verifier
/// hydrates them; the caller supplies an already bounded root revision and
/// invokes this immediately before that verifier in the same held transaction.
pub(crate) fn preflight_s09_import_root_in(
    tx:&Transaction<'_>,workspace:&str,account:&str,candidate:&str,curated:&str,
    revision:&KnowledgeRevision,view:&super::owner_custody::VerifiedCustodyView<'_,'_>,
    budget:&mut ResetFenceBudget,
)->Result<(),RepositoryError> {
    require_guarded_epoch(tx)?;let owner=view.current_owner();
    if view.account_id()!=account || ![owner,workspace,account,candidate,curated].iter().all(|v|valid_receipt_text(v)) || revision.scope.owner_id!=owner || revision.scope.workspace_id.as_deref()!=Some(workspace) || revision.scope.project_id.as_deref().is_some_and(|v|!v.is_empty()) || revision.payload.evidence_ids.len()!=1 || revision.revision<1 {return Err(RepositoryError::NotFound)}
    view.for_captured(tx,&revision.actor_id)?;
    let suffix=candidate.strip_prefix("importreview_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;let original=format!("cs_import_review_{suffix}");
    let staged=staged_import_decision_receipt_id(&revision.actor_id,workspace,account,candidate)?;
    let block=&revision.identity.block_id;let entity=&revision.identity.subject_entity_id;let evidence=&revision.payload.evidence_ids[0];let guard=&revision.change_set_id;let root_revision=revision.revision;
    budget.check(owner.len()+workspace.len()+account.len()+candidate.len()+curated.len()+block.len()+entity.len()+evidence.len()+guard.len()).map_err(RepositoryError::Validation)?;
    let two:&[&dyn rusqlite::ToSql]=&[&owner,&candidate];
    s09_charge_table_rows(tx,"fm_v2_candidates","t","FROM fm_v2_candidates t WHERE t.owner_id=?1 AND t.candidate_id=?2",two,1,budget)?;
    s09_charge_table_rows(tx,"fm_v2_candidate_revisions","t","FROM fm_v2_candidate_revisions t WHERE t.owner_id=?1 AND t.candidate_id=?2 ORDER BY t.revision LIMIT 66",two,65,budget)?;
    s09_charge_table_rows(tx,"fm_v2_candidate_evidence","t","FROM fm_v2_candidate_evidence t WHERE t.owner_id=?1 AND t.candidate_id=?2 LIMIT 257",two,256,budget)?;
    s09_charge_table_rows(tx,"fm_v2_evidence","t","FROM fm_v2_evidence t JOIN fm_v2_candidate_evidence c ON c.owner_id=t.owner_id AND c.evidence_id=t.evidence_id WHERE c.owner_id=?1 AND c.candidate_id=?2 LIMIT 257",two,256,budget)?;
    s09_charge_table_rows(tx,"fm_v2_sources","t","FROM fm_v2_sources t JOIN fm_v2_evidence e ON e.owner_id=t.owner_id AND e.source_id=t.source_id AND e.source_revision=t.source_revision JOIN fm_v2_candidate_evidence c ON c.owner_id=e.owner_id AND c.evidence_id=e.evidence_id WHERE c.owner_id=?1 AND c.candidate_id=?2 LIMIT 257",two,256,budget)?;
    s09_charge_table_rows(tx,"fm_v2_sources","t","FROM fm_v2_sources t JOIN fm_v2_candidate_revisions r ON r.owner_id=t.owner_id AND r.source_id=t.source_id AND r.source_revision=t.source_revision WHERE r.owner_id=?1 AND r.candidate_id=?2 LIMIT 66",two,65,budget)?;
    s09_charge_table_rows(tx,"candidates","t","FROM candidates t WHERE t.owner=?1 AND t.id=?2",two,1,budget)?;
    let three:&[&dyn rusqlite::ToSql]=&[&owner,&block,&root_revision];
    s09_charge_table_rows(tx,"fm_v2_knowledge_blocks","t","FROM fm_v2_knowledge_blocks t WHERE t.owner_id=?1 AND t.block_id=?2",&[&owner,&block],1,budget)?;
    s09_charge_table_rows(tx,"fm_v2_knowledge_revisions","t","FROM fm_v2_knowledge_revisions t WHERE t.owner_id=?1 AND t.block_id=?2 AND t.revision=?3",three,1,budget)?;
    s09_charge_table_rows(tx,"fm_v2_revision_metadata","t","FROM fm_v2_revision_metadata t WHERE t.owner_id=?1 AND t.block_id=?2 AND t.revision=?3 LIMIT 257",three,256,budget)?;
    s09_charge_table_rows(tx,"fm_v2_revision_field_authority","t","FROM fm_v2_revision_field_authority t WHERE t.owner_id=?1 AND t.block_id=?2 AND t.revision=?3 LIMIT 257",three,256,budget)?;
    s09_charge_table_rows(tx,"fm_v2_revision_evidence","t","FROM fm_v2_revision_evidence t WHERE t.owner_id=?1 AND t.block_id=?2 AND t.revision=?3 LIMIT 257",three,256,budget)?;
    s09_charge_table_rows(tx,"fm_v2_evidence","t","FROM fm_v2_evidence t WHERE t.owner_id=?1 AND t.evidence_id=?2",&[&owner,&evidence],1,budget)?;
    s09_charge_table_rows(tx,"fm_v2_sources","t","FROM fm_v2_sources t JOIN fm_v2_evidence e ON e.owner_id=t.owner_id AND e.source_id=t.source_id AND e.source_revision=t.source_revision WHERE e.owner_id=?1 AND e.evidence_id=?2",&[&owner,&evidence],1,budget)?;
    s09_charge_table_rows(tx,"fm_v2_entities","t","FROM fm_v2_entities t WHERE t.owner_id=?1 AND t.entity_id=?2",&[&owner,&entity],1,budget)?;
    s09_charge_table_rows(tx,"fm_v2_entity_revisions","t","FROM fm_v2_entity_revisions t JOIN fm_v2_entities e ON e.owner_id=t.owner_id AND e.entity_id=t.entity_id AND e.current_revision=t.revision WHERE e.owner_id=?1 AND e.entity_id=?2",&[&owner,&entity],1,budget)?;
    s09_charge_table_rows(tx,"curated","t","FROM curated t WHERE t.owner=?1 AND t.workspace_id=?2 AND t.id=?3",&[&owner,&workspace,&curated],1,budget)?;
    let five:&[&dyn rusqlite::ToSql]=&[&owner,&candidate,&original,&staged,&guard];
    s09_charge_table_rows(tx,"fm_v2_change_sets","t","FROM fm_v2_change_sets t WHERE t.owner_id=?1 AND (t.change_set_id IN (?3,?4,?5) OR t.change_set_id IN (SELECT change_set_id FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2)) LIMIT 69",five,68,budget)?;
    s09_charge_table_rows(tx,"fm_v2_outbox","t","FROM fm_v2_outbox t WHERE t.owner_id=?1 AND (t.change_set_id IN (?3,?4,?5) OR t.change_set_id IN (SELECT change_set_id FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2)) LIMIT 137",five,136,budget)?;
    Ok(())
}

/// Bound the existing indexed current-context discovery and its paired
/// change-sets before that owner hydrates either body. This is not authority.
pub(crate) fn s09_preflight_context_in(tx:&Transaction<'_>,owner:&str,workspace:&str,curated:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if ![owner,workspace,curated].iter().all(|v|valid_receipt_text(v)) {return Err(RepositoryError::NotFound)}
    budget.check(owner.len()+workspace.len()+curated.len()).map_err(RepositoryError::Validation)?;
    // The supported guarded writer maintains valid JSON. Malformed unrelated
    // rows excluded from this partial index cannot be attributed to this root.
    // This exact expression-index range is measured before the context join
    // can extract a block ID from any selected payload. Historical overflow
    // refuses rather than silently dropping matching entries.
    let index_scope:&[&dyn rusqlite::ToSql]=&[&owner,&curated];
    s09_charge_table_rows(tx,"fm_v2_outbox","t","FROM fm_v2_outbox t INDEXED BY s03_mirror_receipt_context WHERE t.owner_id=?1 AND t.kind IN ('candidate_review','mirror_update') AND json_valid(t.payload_json) AND json_extract(t.payload_json,'$.curated_id')=?2 LIMIT 33",index_scope,32,budget)?;
    let scope:&[&dyn rusqlite::ToSql]=&[&owner,&curated,&workspace];
    let context="FROM fm_v2_outbox AS t INDEXED BY s03_mirror_receipt_context JOIN fm_v2_knowledge_blocks AS b ON b.owner_id=t.owner_id AND b.block_id=json_extract(t.payload_json,'$.block_id') AND b.workspace_key=?3 AND b.project_key='' AND b.current_revision=t.source_revision WHERE t.owner_id=?1 AND t.kind IN ('candidate_review','mirror_update') AND json_valid(t.payload_json) AND json_extract(t.payload_json,'$.curated_id')=?2 LIMIT 33";
    s09_charge_table_rows(tx,"fm_v2_outbox","t",context,scope,32,budget)?;
    let paired="FROM fm_v2_outbox AS o INDEXED BY s03_mirror_receipt_context JOIN fm_v2_knowledge_blocks AS b ON b.owner_id=o.owner_id AND b.block_id=json_extract(o.payload_json,'$.block_id') AND b.workspace_key=?3 AND b.project_key='' AND b.current_revision=o.source_revision JOIN fm_v2_change_sets t ON t.owner_id=o.owner_id AND t.change_set_id=o.change_set_id WHERE o.owner_id=?1 AND o.kind IN ('candidate_review','mirror_update') AND json_valid(o.payload_json) AND json_extract(o.payload_json,'$.curated_id')=?2 LIMIT 33";
    s09_charge_table_rows(tx,"fm_v2_change_sets","t",paired,scope,32,budget)?;
    Ok(())
}

/// Factual root under a verified account chain; this never authorizes a write.
pub(crate) fn resolve_import_root_fact_custody(tx:&Transaction<'_>,workspace:&str,account:&str,candidate:&str,revision:&KnowledgeRevision,historical:bool,view:&super::owner_custody::VerifiedCustodyView<'_, '_>)->Result<(CuratedMirrorBinding,String,String),RepositoryError> {
    let identity=import_identity_for_candidate_at(tx,view.current_owner(),workspace,account,candidate,Some(view))?;
    let (original,_)=original_for_import_identity_at(tx,&identity,view.current_owner(),Some(view))?;
    match original.mode.as_str() {
        "manual"=>resolve_import_typed_root_custody(tx,&identity,revision,historical,view),
        "review_only"=>resolve_staged_import_typed_root_custody(tx,&identity,revision,historical,view),
        _=>Err(RepositoryError::NotFound),
    }
}

/// Indexed owner-scoped discovery of missing staged decisions or terminals.
/// Cursors select only their own event domain and never confer authority.
fn import_missing_page(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,after:Option<&str>,limit:u32,terminal:bool,view:Option<&super::owner_custody::VerifiedCustodyView<'_, '_>>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    custody_owner(tx,owner,owner,account,view)?;
    require_guarded_epoch(tx)?;
    if ![owner,workspace,account].iter().all(|v|valid_receipt_text(v)) || !(1..=20).contains(&limit) {return Err(RepositoryError::NotFound);}
    let prefix=if terminal {"outbox_s04_import_review_erased_"} else {"outbox_cs_import_staged_review_"};
    let suffix=if terminal {""} else {"_import_staged_review"};
    if after.is_some_and(|v|v.strip_prefix(prefix).and_then(|v|v.strip_suffix(suffix)).is_none_or(|v|!valid_sha256(v))) {return Err(RepositoryError::Validation("import review page cursor unavailable".into()));}
    // Same bounded progress owner as the existing Add inspector; caller must
    // not install another SQLite progress handler around this operation.
    let upper=format!("{prefix}~");
    let mut stmt=tx.prepare("SELECT event_id,kind,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND event_id<?3 ORDER BY event_id LIMIT 65")?;
    let mut rows=stmt.query(params![owner,after.unwrap_or(prefix),upper])?;
    let mut items=Vec::new();let mut examined=0usize;let mut last=None;let mut more=false;
    while let Some(row)=rows.next()? {
        if examined>=64 || items.len()>=limit as usize {more=true;break;}
        let event=row.get_ref(0)?.as_str().map_err(|_|RepositoryError::NotFound)?;
        let kind=row.get_ref(1)?.as_str().map_err(|_|RepositoryError::NotFound)?;
        let raw=row.get_ref(2)?.as_str().map_err(|_|RepositoryError::NotFound)?;
        let revision:i64=row.get(3)?;
        budget.check(event.len().saturating_add(kind.len()).saturating_add(raw.len()).saturating_add(1)).map_err(|_|RepositoryError::NotFound)?;
        if revision!=0 || event.strip_prefix(prefix).and_then(|v|v.strip_suffix(suffix)).is_none_or(|v|!valid_sha256(v)) {return Err(RepositoryError::NotFound);}
        let value=crate::field_authority::parse_unique_json(raw).map_err(RepositoryError::Validation)?;
        examined+=1;last=Some(event.to_owned());
        if terminal {
            if kind!="s04_import_review_erased" {return Err(RepositoryError::NotFound);}
            let marker:ImportReviewTerminal=serde_json::from_value(value)?;
            if import_terminal_event(&marker.candidate_id)?!=event {return Err(RepositoryError::NotFound);}
            if marker.workspace_id!=workspace || marker.account_id!=account {continue;}
            custody_owner(tx,owner,&marker.owner,account,view)?;
            let identity=import_identity_for_candidate_at(tx,owner,workspace,account,&marker.candidate_id,view)?;
            let verified=read_import_review_terminal_at(tx,&identity,owner,view)?.ok_or(RepositoryError::NotFound)?;
            if verified!=marker {return Err(RepositoryError::NotFound);}
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1)",[&marker.candidate_id],|r|r.get(0))?;
            if !exists {items.push(json!({"candidate_id":marker.candidate_id,"state":"terminal_erased"}));}
        } else {
            if kind!="import_staged_review" {return Err(RepositoryError::NotFound);}
            let decision:StagedImportDecisionReceipt=serde_json::from_value(value)?;
            if format!("outbox_{}_import_staged_review",staged_import_decision_receipt_id(&decision.owner,&decision.workspace_id,&decision.account_id,&decision.candidate_id)?)!=event {return Err(RepositoryError::NotFound);}
            if decision.workspace_id!=workspace || decision.account_id!=account {continue;}
            custody_owner(tx,owner,&decision.owner,account,view)?;
            let identity=import_identity_for_candidate_at(tx,owner,workspace,account,&decision.candidate_id,view)?;
            if read_import_review_terminal_at(tx,&identity,owner,view)?.is_some() {return Err(RepositoryError::NotFound);}
            let verified=match view {Some(v)=>read_staged_import_decision_receipt_custody(tx,workspace,account,&decision.candidate_id,&decision.owner,v)?,None=>read_staged_import_decision_receipt(tx,owner,workspace,account,&decision.candidate_id)?}.ok_or(RepositoryError::NotFound)?;
            if verified!=decision {return Err(RepositoryError::NotFound);}
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1)",[&decision.candidate_id],|r|r.get(0))?;
            if !exists {
                let state=if decision.action=="accept" {"applied"} else {"rejected"};
                items.push(json!({"candidate_id":decision.candidate_id,"original":{"candidate_id":decision.candidate_id,"expected_revision":decision.expected_revision,"expected_binding":decision.expected_binding,"review_key":decision.review_key,"action":decision.action},"receipt":{"state":state,"candidate_id":decision.candidate_id,"curated_id":decision.curated_id}}));
            }
        }
    }
    drop(rows);drop(stmt);budget.check(0).map_err(|_|RepositoryError::NotFound)?;
    let next=if more {last} else {None};if more && next.is_none() {return Err(RepositoryError::NotFound);}
    Ok(json!({"version":1,"items":items,"next_cursor":next,"partial":more}))
}
pub(crate) fn import_missing_original_page(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,after:Option<&str>,limit:u32)->Result<Value,RepositoryError> {
    let mut budget=ResetFenceBudget::install_bounded(tx,std::time::Duration::from_secs(1));
    import_missing_page(tx,owner,workspace,account,after,limit,false,None,&mut budget)
}
pub(crate) fn import_missing_terminal_page(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,after:Option<&str>,limit:u32)->Result<Value,RepositoryError> {
    let mut budget=ResetFenceBudget::install_bounded(tx,std::time::Duration::from_secs(1));
    import_missing_page(tx,owner,workspace,account,after,limit,true,None,&mut budget)
}

/// Caller supplies the same transaction-bound verified chain and existing budget.
pub(crate) fn import_missing_original_page_custody(tx:&Transaction<'_>,workspace:&str,account:&str,after:Option<&str>,limit:u32,view:&super::owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    import_missing_page(tx,view.current_owner(),workspace,account,after,limit,false,Some(view),budget)
}
pub(crate) fn import_missing_terminal_page_custody(tx:&Transaction<'_>,workspace:&str,account:&str,after:Option<&str>,limit:u32,view:&super::owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    import_missing_page(tx,view.current_owner(),workspace,account,after,limit,true,Some(view),budget)
}
