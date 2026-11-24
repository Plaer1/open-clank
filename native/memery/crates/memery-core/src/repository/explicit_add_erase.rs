//! Pristine source-free Add member footprint. No mutation is performed here.
use super::*;
use super::explicit_add_batch::VerifiedEraseMember;
use super::owner_custody::VerifiedCustodyView;

/// Transaction-bound facts, never an authorization or caller-constructible selector.
pub(crate) struct MemberErasePlan<'tx,'conn> {
    tx:&'tx Transaction<'conn>, owner:String, account:String, workspace:String, captured:String,
    candidate:String, curated:String, block:String, revision:i64, entity:String, claim_slot:String,
    source:String, evidence:String, changes:Vec<String>, metadata_count:usize, authority_count:usize, input_sha256:String,
    original_key_sha256:String, review_key_sha256:String, review_revision:i64, review_binding:String,
    question:Option<S09QuestionGroupFact>,
}
impl MemberErasePlan<'_,'_> {
    pub(crate) fn candidate_id(&self)->&str {&self.candidate}
    pub(crate) fn curated_id(&self)->&str {&self.curated}
    pub(crate) fn captured_owner(&self)->&str {&self.captured}
    pub(crate) fn question_group(&self)->Option<&S09QuestionGroupFact> {self.question.as_ref()}
    pub(crate) fn input_sha256(&self)->&str {&self.input_sha256}
    pub(crate) fn original_key_sha256(&self)->&str {&self.original_key_sha256}
    pub(crate) fn review_key_sha256(&self)->&str {&self.review_key_sha256}
    pub(crate) fn review_revision(&self)->i64 {self.review_revision}
    pub(crate) fn review_binding(&self)->&str {&self.review_binding}
    pub(crate) fn assert_transaction(&self,tx:&Transaction<'_>)->Result<(),RepositoryError> {
        if !std::ptr::eq(self.tx as *const _ as *const (),tx as *const _ as *const ()) {return Err(RepositoryError::NotFound)} Ok(())
    }
}

/// Classify only an exact, initial source-free human Add. Unsupported or shared
/// derivatives refuse before any mutation; the generic memory entity may be shared.
#[allow(clippy::too_many_arguments)]
pub(crate) fn plan_explicit_add_member_erasure<'tx,'conn>(
    tx:&'tx Transaction<'conn>, account:&str, owner:&str, workspace:&str,
    candidate:&str, curated:&str, member:&VerifiedEraseMember<'tx,'conn>,
    view:&VerifiedCustodyView<'_,'_>, budget:&mut ResetFenceBudget,
)->Result<MemberErasePlan<'tx,'conn>,RepositoryError> {
    member.assert_transaction(tx)?;
    crate::writer_fence::require_matched_guarded_epoch(tx).map_err(|_|RepositoryError::NotFound)?;
    let epoch:i64=tx.query_row("PRAGMA user_version",[],|r|r.get(0))?;
    if epoch!=18 || member.current_owner()!=owner || member.account()!=account || member.workspace()!=workspace
        || member.candidate_id()!=candidate || member.curated_id()!=curated || member.revision()!=1
        || view.current_owner()!=owner || view.account_id()!=account {return Err(RepositoryError::NotFound)}
    let captured=member.captured_owner(); let proof=view.for_captured(tx,captured)?;
    if proof.current_owner()!=owner || proof.account_id()!=account {return Err(RepositoryError::NotFound)}
    let block=member.block_id(); let (identity,actual_workspace,project,head)=read_knowledge_identity(tx,owner,block)?;
    if actual_workspace!=workspace || !project.is_empty() || head<1 {return Err(RepositoryError::NotFound)}
    let revision=read_knowledge(tx,owner,&identity,1,workspace,"")?;
    let question=if revision.payload.kind=="open_question" {
        let group=s09_question_group_in(tx,workspace,block,view,budget)?;
        if group.account_id!=account || group.root_kind!="sourced" || group.candidate_id.as_deref()!=Some(candidate) || group.curated_id.as_deref()!=Some(curated) || group.block_id!=block || head!=1+i64::try_from(group.children.len()).map_err(|_|RepositoryError::NotFound)? {return Err(RepositoryError::NotFound)}
        Some(group)
    } else {if head!=1 || identity.claim_slot!=curated{return Err(RepositoryError::NotFound)}None};
    if revision.payload.evidence_ids.len()!=1 || revision.source_event_id.as_deref()!=Some(format!("memory:{curated}").as_str()) {return Err(RepositoryError::NotFound)}
    // A contextual question can target a pre-existing entity; its identity is
    // verified by the root/chain owner and is preserved by this erase.
    let expected_entity=if question.is_some() {identity.subject_entity_id.clone()} else {
        let entity=read_entity(tx,owner,&identity.subject_entity_id,1,workspace,"")?;
        let label=if revision.payload.kind=="persona" {"self"} else {"memory"};
        let exact_anchor=if label=="self" {
            entity.payload.entity_type=="person" && entity.payload.roles==["self"] && entity.payload.tags==["self"]
        } else {entity.payload.entity_type=="concept" && entity.payload.roles.is_empty() && entity.payload.tags.is_empty()};
        if !exact_anchor || entity.payload.canonical_label!=label || entity.payload.status!="active" || !entity.payload.aliases.is_empty() {return Err(RepositoryError::NotFound)}
        let expected_entity=format!("entity_{}", &sha256_hex(&serde_json::to_vec(&json!([captured,workspace,"",label]))?)[..32]);
        if identity.subject_entity_id!=expected_entity {return Err(RepositoryError::NotFound)}
        let entity_head:i64=tx.query_row("SELECT current_revision FROM fm_v2_entities WHERE owner_id=?1 AND entity_id=?2 AND workspace_key=?3 AND project_key=''",params![owner,expected_entity,workspace],|r|r.get(0))?;
        if entity_head!=1 {return Err(RepositoryError::NotFound)}
        expected_entity
    };
    let evidence=revision.payload.evidence_ids[0].clone();
    let (source,uri,source_rev,locator,contract):(String,String,i64,String,String)=tx.query_row(
        "SELECT e.source_id,s.source_uri,e.source_revision,e.locator_json,e.extraction_contract FROM fm_v2_evidence e JOIN fm_v2_sources s ON s.owner_id=e.owner_id AND s.source_id=e.source_id AND s.source_revision=e.source_revision WHERE e.owner_id=?1 AND e.evidence_id=?2 AND e.locator_type='memory_record' AND s.workspace_key=?3 AND s.project_key=''",
        params![owner,evidence,workspace],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?.ok_or(RepositoryError::NotFound)?;
    budget.check(source.len()+uri.len()+locator.len()+contract.len()).map_err(RepositoryError::Validation)?;
    let expected_source=format!("source_memory_{}", &sha256_hex(&serde_json::to_vec(&json!([captured,curated]))?)[..32]);
    if source!=expected_source || uri!=format!("memory://{curated}") || source_rev!=1 || contract!="provider-record-v1"
        || crate::field_authority::parse_unique_json(&locator).map_err(RepositoryError::Validation)?!=json!({"record_id":curated,"source_revision":1}) {
        return Err(RepositoryError::NotFound)
    }
    let review=explicit_add::read_explicit_add_review_receipt_custody(tx,workspace,account,candidate,captured,view)?.ok_or(RepositoryError::NotFound)?;
    if review.action!="accept" || review.curated_id.as_deref()!=Some(curated) {return Err(RepositoryError::NotFound)}
    if review.expected_revision!=1 {return Err(RepositoryError::NotFound)}
    let (candidate_head,candidate_count):(i64,i64)=tx.query_row("SELECT current_revision,(SELECT count(*) FROM fm_v2_candidate_revisions r WHERE r.owner_id=c.owner_id AND r.candidate_id=c.candidate_id) FROM fm_v2_candidates c WHERE c.owner_id=?1 AND c.candidate_id=?2 AND c.workspace_key=?3 AND c.project_key=''",params![owner,candidate,workspace],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
    if (candidate_head,candidate_count)!=(4,4) {return Err(RepositoryError::NotFound)}
    let actor=RevisionActor{actor_type:"user".into(),actor_id:review.owner.clone(),reason:"explicit human add".into(),source_event_id:Some(format!("memory:{curated}"))};
    if question.is_none() {explicit_add::verify_explicit_add_activation_chain(tx,owner,workspace,candidate,member.origin_request_key(),1,&actor,block,Some(view),Some(budget))?;}
    let original_id=format!("cs_explicit_add_{}",candidate.strip_prefix("manualadd_").filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?);
    let (digest,raw):(String,Option<String>)=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s03.explicit-add.v1'",params![owner,original_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
    let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let original=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let input=original["input_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
    if digest!=sha256_hex(raw.as_bytes()) || original["owner"]!=captured || original["account_id"]!=account || original["workspace_id"]!=workspace
        || original["candidate_id"]!=candidate || original["request_key"]!=member.origin_request_key() {return Err(RepositoryError::NotFound)}
    let original_key=explicit_add::explicit_add_key_sha256(account,workspace,member.origin_request_key())?;
    let review_key=explicit_add::explicit_add_review_key_sha256(account,workspace,candidate,&review.review_key)?;
    // Inverse foreign-key closure must be exactly this selected pristine member.
    let (source_evidence,docs,episodes,source_candidates):(i64,i64,i64,i64)=tx.query_row(
        "SELECT (SELECT count(*) FROM fm_v2_evidence WHERE owner_id=?1 AND source_id=?2),(SELECT count(*) FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2),(SELECT count(*) FROM fm_v2_episodes WHERE owner_id=?1 AND source_id=?2),(SELECT count(*) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND source_id=?2)",params![owner,source],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
    let (ev_revisions,ev_candidates,block_revisions,block_conflicts,other_candidates):(i64,i64,i64,i64,i64)=tx.query_row(
        "SELECT (SELECT count(*) FROM fm_v2_revision_evidence WHERE owner_id=?1 AND evidence_id=?2),(SELECT count(*) FROM fm_v2_candidate_evidence WHERE owner_id=?1 AND evidence_id=?2),(SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?3),(SELECT count(*) FROM fm_v2_conflicts WHERE owner_id=?1 AND block_id=?3),(SELECT count(*) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND accepted_block_id=?3 AND candidate_id<>?4)",params![owner,evidence,block,candidate],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
    if (source_evidence,docs,episodes,source_candidates)!=(1,0,0,0) || (ev_revisions,ev_candidates,block_revisions,block_conflicts,other_candidates)!=(head,0,head,0,0) {return Err(RepositoryError::NotFound)}
    let invalid_candidate:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND (source_id IS NOT NULL OR (accepted_block_id IS NOT NULL AND accepted_block_id<>?3))) OR EXISTS(SELECT 1 FROM fm_v2_candidate_evidence WHERE owner_id=?1 AND candidate_id=?2)",params![owner,candidate,block],|r|r.get(0))?;
    if invalid_candidate {return Err(RepositoryError::NotFound)}
    let exclusive_source:bool=tx.query_row("SELECT (SELECT count(*) FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2)=1 AND (SELECT count(*) FROM fm_v2_revision_evidence WHERE owner_id=?1 AND evidence_id=?3 AND block_id=?4 AND revision=1)=1",params![owner,source,evidence,block],|r|r.get(0))?;
    if !exclusive_source {return Err(RepositoryError::NotFound)}
    // Fixed owner-scoped denial-only probes. A known observation, derivative,
    // graph edge or association must be handled by its own lifecycle owner.
    let unsafe_outbox:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND kind IN ('s06_found','s06_admitted','s06_helped','s08_selection','derived_compaction','mirror_update') AND (kind!='mirror_update' OR ?7=0) AND (NOT json_valid(payload_json) OR EXISTS(SELECT 1 FROM json_tree(payload_json) WHERE type='text' AND value IN (?2,?3,?4,?5,?6))))",params![owner,candidate,curated,block,source,evidence,question.is_some()],|r|r.get(0))?;
    if unsafe_outbox {return Err(RepositoryError::NotFound)}
    let has_jobs:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fm_v2_s05_job_refs')",[],|r|r.get(0))?;
    if has_jobs && tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND candidate_id=?2)",params![owner,candidate],|r|r.get::<_,bool>(0))? {return Err(RepositoryError::NotFound)}
    let mut changes=BTreeSet::new();
    for sql in ["SELECT change_set_id FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2", "SELECT created_change_set_id FROM fm_v2_candidates WHERE owner_id=?1 AND candidate_id=?2"] {
        let mut stmt=tx.prepare(sql)?;let rows=stmt.query_map(params![owner,candidate],|r|r.get::<_,String>(0))?;
        for row in rows {let id=row?;budget.check(id.len()).map_err(RepositoryError::Validation)?;changes.insert(id);}
    }
    for sql in ["SELECT change_set_id FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2", "SELECT created_change_set_id FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND block_id=?2", "SELECT change_set_id FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2", "SELECT change_set_id FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2"] {
        let mut stmt=tx.prepare(sql)?;let rows=stmt.query_map(params![owner,block],|r|r.get::<_,String>(0))?;
        for row in rows {let id=row?;budget.check(id.len()).map_err(RepositoryError::Validation)?;changes.insert(id);}
    }
    changes.insert(original_id); changes.insert(candidate_receipt_id(captured,workspace,candidate,member.origin_request_key())?);
    changes.insert(explicit_add::explicit_add_review_receipt_id(captured,workspace,account,candidate)?);
    if changes.len()>128 {return Err(RepositoryError::NotFound)}
    let (metadata_count,authority_count):(usize,usize)=tx.query_row("SELECT (SELECT count(*) FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2),(SELECT count(*) FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2)",params![owner,block],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if metadata_count>65 || authority_count>65*256 {return Err(RepositoryError::NotFound)}
    // Any additional selected identity in an owner outbox is unsupported.
    // Only the exact already-verified member changes and its batch are exempt.
    let mut refs=tx.prepare("SELECT change_set_id,event_id,kind FROM fm_v2_outbox WHERE owner_id=?1 AND CASE WHEN json_valid(payload_json) THEN EXISTS(SELECT 1 FROM json_tree(payload_json) WHERE type='text' AND value IN (?2,?3,?4,?5,?6)) ELSE 1 END LIMIT 130")?;
    let found=refs.query_map(params![owner,candidate,curated,block,source,evidence],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)))?;
    let mut ref_count=0usize;
    for entry in found {let (id,event,kind)=entry?; ref_count+=1; budget.check(id.len()+event.len()+kind.len()).map_err(RepositoryError::Validation)?; let batch=id==member.batch_id() && kind=="explicit_add_review_batch" && event==format!("outbox_{id}_{kind}"); let child=question.as_ref().is_some_and(|g|g.children.iter().any(|c|c.receipt_id==id && c.outbox_id==event)); if ref_count>128 || (!changes.contains(&id) && !batch && !child) {return Err(RepositoryError::NotFound)}}
    let dependent:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_metadata m, json_tree(m.metadata_json) j WHERE m.owner_id=?1 AND m.block_id<>?2 AND j.type='text' AND j.value=?2) OR EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions r, json_tree(r.value_json) j WHERE r.owner_id=?1 AND r.block_id<>?2 AND j.type='text' AND j.value=?2)",params![owner,block],|r|r.get(0))?;
    if dependent {return Err(RepositoryError::NotFound)}
    let evidence_ref:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_metadata m,json_tree(m.evidence_json) j WHERE m.owner_id=?1 AND m.block_id<>?2 AND j.type='text' AND j.value=?3) OR EXISTS(SELECT 1 FROM fm_v2_revision_field_authority f,json_tree(f.evidence_json) j WHERE f.owner_id=?1 AND f.block_id<>?2 AND j.type='text' AND j.value=?3) OR EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions r,json_tree(r.evidence_json) j WHERE r.owner_id=?1 AND r.block_id<>?2 AND j.type='text' AND j.value=?3)",params![owner,block,evidence],|r|r.get(0))?;
    if evidence_ref {return Err(RepositoryError::NotFound)}
    if question.is_none() {crate::store::graph::verify_pristine_curated_projection(tx,owner,workspace,candidate,curated,budget)?;}
    // A selected change must have exactly one canonical, recognized outbox.
    // This is a denial check: existing typed receipt verifiers remain the authority.
    for change in &changes {
        let row:Option<(String,String)>=tx.query_row("SELECT kind,event_id FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2 AND (SELECT count(*) FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2)=1",params![owner,change],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let Some((kind,event_id))=row else {return Err(RepositoryError::NotFound)};
        budget.check(kind.len()+event_id.len()).map_err(RepositoryError::Validation)?;
        if !matches!(kind.as_str(),"explicit_add"|"candidate_revision"|"knowledge_revision"|"candidate_review"|"explicit_add_review") || event_id!=format!("outbox_{change}_{kind}") {return Err(RepositoryError::NotFound)}
    }
    Ok(MemberErasePlan {tx,owner:owner.into(),account:account.into(),workspace:workspace.into(),captured:captured.into(),candidate:candidate.into(),curated:curated.into(),block:block.into(),revision:head,entity:expected_entity,claim_slot:identity.claim_slot.clone(),source,evidence,changes:changes.into_iter().collect(),metadata_count,authority_count,input_sha256:input.into(),original_key_sha256:original_key,review_key_sha256:review_key,review_revision:review.expected_revision,review_binding:review.expected_binding,question})
}

/// Apply only the previously classified member in the same immediate transaction.
/// All errors must propagate to the transaction owner so the entire forget rolls back.
pub(crate) fn apply_explicit_add_member_erasure(tx:&Transaction<'_>, plan:&MemberErasePlan<'_,'_>, erasure_id:&str, budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    plan.assert_transaction(tx)?;
    crate::writer_fence::require_matched_guarded_epoch(tx).map_err(|_|RepositoryError::NotFound)?;
    let owner=&plan.owner; let block=&plan.block; let candidate=&plan.candidate;
    let context:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND source_id IS NULL AND authorized_by=?1 AND reason='account-bound guarded batch forget')",params![owner,erasure_id],|r|r.get(0))?;
    if !context {return Err(RepositoryError::NotFound)}
    if plan.question.is_none() {crate::store::graph::verify_pristine_curated_projection(tx,owner,&plan.workspace,candidate,&plan.curated,budget)?;}
    // Recheck the selected fixed row footprint. Plans for siblings were captured
    // before effects, and are not revalidated via the now-changing batch receipt.
    let live:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_candidates WHERE owner_id=?1 AND candidate_id=?2 AND workspace_key=?3 AND project_key='' AND current_revision=4) AND EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND block_id=?4 AND workspace_key=?3 AND project_key='' AND subject_entity_id=?5 AND claim_slot=?6 AND current_revision=?10) AND EXISTS(SELECT 1 FROM fm_v2_evidence WHERE owner_id=?1 AND evidence_id=?7 AND source_id=?8 AND source_revision=1 AND locator_type='memory_record') AND EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?8 AND workspace_key=?3 AND project_key='' AND source_revision=1 AND source_uri=?9)",params![owner,candidate,plan.workspace,block,plan.entity,plan.claim_slot,plan.evidence,plan.source,format!("memory://{}",plan.curated),plan.revision],|r|r.get(0))?;
    if !live {return Err(RepositoryError::NotFound)}
    let counts:(usize,usize,usize,usize,usize,usize,usize)=tx.query_row("SELECT (SELECT count(*) FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2),(SELECT count(*) FROM fm_v2_candidate_evidence WHERE owner_id=?1 AND candidate_id=?2),(SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?3),(SELECT count(*) FROM fm_v2_revision_evidence WHERE owner_id=?1 AND block_id=?3 AND revision=1 AND evidence_id=?4),(SELECT count(*) FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?3 AND revision=1),(SELECT count(*) FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?3 AND revision=1),(SELECT count(*) FROM fm_v2_evidence WHERE owner_id=?1 AND source_id=?5)",params![owner,candidate,block,plan.evidence,plan.source],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?)))?;
    if counts!=(4,0,plan.revision as usize,1,plan.metadata_count,plan.authority_count,1) {return Err(RepositoryError::NotFound)}
    budget.check(plan.changes.iter().map(String::len).sum()).map_err(RepositoryError::Validation)?;
    let delete=|sql:&str,args:&[&dyn rusqlite::ToSql],expected:usize|->Result<(),RepositoryError>{if tx.execute(sql,args)?!=expected {return Err(RepositoryError::NotFound)} Ok(())};
    delete("DELETE FROM fm_v2_candidate_revisions WHERE owner_id=? AND candidate_id=?",&[owner,candidate],4)?;
    delete("DELETE FROM fm_v2_candidates WHERE owner_id=? AND candidate_id=?",&[owner,candidate],1)?;
    delete("DELETE FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=?",&[owner,block],plan.authority_count)?;
    delete("DELETE FROM fm_v2_revision_metadata WHERE owner_id=? AND block_id=?",&[owner,block],plan.metadata_count)?;
    delete("DELETE FROM fm_v2_revision_evidence WHERE owner_id=? AND block_id=? AND evidence_id=?",&[owner,block,&plan.evidence],plan.revision as usize)?;
    delete("DELETE FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=?",&[owner,block],plan.revision as usize)?;
    delete("DELETE FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",&[owner,block],1)?;
    delete("DELETE FROM fm_v2_evidence WHERE owner_id=? AND evidence_id=? AND source_id=? AND source_revision=1",&[owner,&plan.evidence,&plan.source],1)?;
    delete("DELETE FROM fm_v2_sources WHERE owner_id=? AND source_id=? AND source_revision=1",&[owner,&plan.source],1)?;
    // Shared deterministic anchors survive until the final referencing block.
    let shared:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND subject_entity_id=?2)",params![owner,plan.entity],|r|r.get(0))?;
    let mut changes=plan.changes.clone();
    if !shared && plan.question.is_none() {
        let (genesis,revision,roles,tags):(String,i64,usize,usize)=tx.query_row("SELECT created_change_set_id,current_revision,(SELECT count(*) FROM fm_v2_entity_roles r WHERE r.owner_id=e.owner_id AND r.entity_id=e.entity_id AND r.revision=1),(SELECT count(*) FROM fm_v2_entity_tags t WHERE t.owner_id=e.owner_id AND t.entity_id=e.entity_id AND t.revision=1) FROM fm_v2_entities e WHERE owner_id=?1 AND entity_id=?2 AND workspace_key=?3 AND project_key=''",params![owner,plan.entity,plan.workspace],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?.ok_or(RepositoryError::NotFound)?;
        let entity=read_entity(tx,owner,&plan.entity,1,&plan.workspace,"")?;
        if revision!=1 || entity.change_set_id!=genesis || !entity.payload.aliases.is_empty() || roles!=entity.payload.roles.len() || tags!=entity.payload.tags.len() {return Err(RepositoryError::NotFound)}
        if roles==1 {
            let exact:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_entity_roles WHERE owner_id=?1 AND entity_id=?2 AND revision=1 AND workspace_key=?3 AND project_key='' AND role='self' AND is_current=1) AND EXISTS(SELECT 1 FROM fm_v2_entity_tags WHERE owner_id=?1 AND entity_id=?2 AND revision=1 AND tag='self')",params![owner,plan.entity,plan.workspace],|r|r.get(0))?;
            if !exact || tags!=1 {return Err(RepositoryError::NotFound)}
        } else if roles!=0 || tags!=0 {return Err(RepositoryError::NotFound)}
        let incoming:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions r,json_tree(r.value_json) j WHERE r.owner_id=?1 AND j.type='text' AND j.value=?2) OR EXISTS(SELECT 1 FROM fm_v2_revision_metadata m,json_tree(m.metadata_json) j WHERE m.owner_id=?1 AND j.type='text' AND j.value=?2) OR EXISTS(SELECT 1 FROM fm_v2_candidate_revisions c,json_tree(c.proposal_json) j WHERE c.owner_id=?1 AND j.type='text' AND j.value=?2) OR EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions k,json_tree(k.activation_rationale) j WHERE k.owner_id=?1 AND j.type='text' AND j.value=?2)",params![owner,plan.entity],|r|r.get(0))?;
        if incoming {return Err(RepositoryError::NotFound)}
        delete("DELETE FROM fm_v2_entity_aliases WHERE owner_id=? AND entity_id=?",&[owner,&plan.entity],0)?;
        delete("DELETE FROM fm_v2_entity_roles WHERE owner_id=? AND entity_id=? AND revision=1",&[owner,&plan.entity],roles)?;
        delete("DELETE FROM fm_v2_entity_tags WHERE owner_id=? AND entity_id=? AND revision=1",&[owner,&plan.entity],tags)?;
        delete("DELETE FROM fm_v2_entity_revisions WHERE owner_id=? AND entity_id=? AND revision=1",&[owner,&plan.entity],1)?;
        delete("DELETE FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",&[owner,&plan.entity],1)?;
        changes.push(genesis);
    }
    for id in changes {
        budget.check(id.len()).map_err(RepositoryError::Validation)?;
        let kind:String=tx.query_row("SELECT kind FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2 AND (SELECT count(*) FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2)=1",params![owner,id],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
        if !matches!(kind.as_str(),"explicit_add"|"candidate_revision"|"knowledge_revision"|"candidate_review"|"explicit_add_review"|"entity_revision") {return Err(RepositoryError::NotFound)}
        let event=format!("outbox_{id}_{kind}");
        delete("DELETE FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind=?",&[owner,&event,&id,&kind],1)?;
        delete("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND NOT EXISTS(SELECT 1 FROM fm_v2_entities WHERE owner_id=?1 AND created_change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_entity_revisions WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND created_change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_candidates WHERE owner_id=?1 AND created_change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_conflicts WHERE owner_id=?1 AND (created_change_set_id=?2 OR resolved_change_set_id=?2)) AND NOT EXISTS(SELECT 1 FROM fm_v2_conflict_events WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND change_set_id=?2)",&[owner,&id],1)?;
    }
    Ok(())
}
