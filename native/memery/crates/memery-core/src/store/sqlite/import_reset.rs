//! Exact import-review erasure finishing under the original reset transaction.
//! The parent has already classified membership and inserted the S06 receipt.
use super::*;
use std::collections::BTreeSet;
use crate::repository::import_mirror::{self,ImportReviewTerminal};

fn unavailable()->String {"import review reset unavailable".into()}

/// Verify only the expected indexed projection. The original receipt and
/// guarded root are separately owned; this check is not a publication grant.
fn projection(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,candidate:&str,change:&str,curated:Option<&str>,version:&str,present:bool,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>)->Result<(),String> {
    let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2 AND kind='candidate_review'",params![owner,change],|r|r.get(0)).map_err(|_|unavailable())?;
    if !present {return if count==0 {Ok(())} else {Err(unavailable())};}
    let curated=curated.ok_or_else(unavailable)?;
    if count!=1 {return Err(unavailable());}
    let row:Option<(String,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review'",params![owner,format!("outbox_{change}_candidate_review"),change],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|unavailable())?;
    let (raw,revision)=row.ok_or_else(unavailable)?;let event=crate::field_authority::parse_unique_json(&raw).map_err(|_|unavailable())?;
    let binding:crate::repository::CuratedMirrorBinding=serde_json::from_value(event.get("binding").cloned().ok_or_else(unavailable)?).map_err(|_|unavailable())?;
    let proof=view.for_captured(tx,&binding.owner).map_err(|_|unavailable())?;
    if proof.current_owner()!=owner || proof.account_id()!=account || revision<1 || binding.workspace!=workspace || !binding.project.is_empty() || binding.curated_id!=curated || binding.revision!=revision || binding.block_id.is_empty() || binding.guarded_change_set_id.is_empty() || binding.semantic_sha256.len()!=64 || !binding.semantic_sha256.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))
        || event!=serde_json::json!({"candidate_id":candidate,"curated_id":curated,"block_id":binding.block_id,"revision":revision,"reviewer_account_id":account,"receipt_version":version,"binding":binding}) {return Err(unavailable());}
    Ok(())
}

/// Writes a factual terminal and removes only the staged decision and exact
/// mirror-index projections. The caller must roll back the whole transaction
/// on any error; no Host or deserialized locators are accepted as authority.
pub(super) fn finish_import_review_erasures(
    tx:&rusqlite::Transaction<'_>,owner:&str,reset_workspace:&str,account:&str,operation:&str,
    mode:&str,partition_sha256:Option<&str>,reset_receipt_id:&str,items:&[PendingImportReviewErase],
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,
)->Result<(),String> {
    if owner.is_empty() || account.is_empty() || reset_workspace.is_empty() || operation.is_empty() || !matches!((mode,partition_sha256),("full",None)|("selective",Some(_))) {return Err(unavailable());}
    let mut seen=BTreeSet::new();let mut markers=Vec::with_capacity(items.len());
    for item in items {
        let identity=&item.identity;
        let proof=view.for_captured(tx,&identity.owner).map_err(|_|unavailable())?;
        if proof.current_owner()!=owner || proof.account_id()!=account || identity.account_id!=account || !seen.insert(item.candidate.clone()) {return Err(unavailable());}
        let (original,hash)=import_mirror::original_for_import_identity_at(tx,identity,owner,Some(view)).map_err(|_|unavailable())?;
        let suffix=item.candidate.strip_prefix("importreview_").ok_or_else(unavailable)?;
        if original.candidate_id!=item.candidate || item.original_receipt_id!=format!("cs_import_review_{suffix}") || item.original_receipt_sha256!=hash {return Err(unavailable());}
        let mut decision=None;
        for captured in view.owners() {
            if let Some(value)=crate::repository::explicit_add::read_staged_import_decision_receipt_custody(tx,&identity.workspace_id,account,&item.candidate,captured,view).map_err(|_|unavailable())? {
                if decision.replace(value).is_some(){return Err(unavailable())}
            }
        }
        let decision_owner=decision.as_ref().map(|v|v.owner.as_str()).unwrap_or(&identity.owner);
        let decision_id=crate::repository::explicit_add::staged_import_decision_receipt_id(decision_owner,&identity.workspace_id,account,&item.candidate).map_err(|_|unavailable())?;
        if decision.is_some() && original.mode!="review_only" {return Err(unavailable());}
        match (&decision,&item.decision_receipt_id,&item.decision_key_sha256,&item.decision_action,item.decision_revision,&item.decision_binding) {
            (None,None,None,None,None,None)=>(),
            (Some(d),Some(id),Some(key),Some(action),Some(revision),Some(binding)) if id==&decision_id && *key==import_mirror::import_review_decision_key_sha256(account,&identity.workspace_id,&item.candidate,&d.review_key).map_err(|_|unavailable())? && *action==d.action && revision==d.expected_revision && *binding==d.expected_binding=>(),
            _=>return Err(unavailable()),
        }
        let initial_curated=if original.mode=="manual" {Some(original.publication_id.as_str())} else {None};
        let later_curated=decision.as_ref().filter(|d|d.action=="accept").and_then(|d|d.curated_id.as_deref());
        if item.decision_receipt_id.is_none() && item.decision_projection_present {return Err(unavailable());}
        projection(tx,owner,&identity.workspace_id,account,&item.candidate,&item.original_receipt_id,initial_curated,"s03.import-review.v1",item.original_projection_present,view)?;
        projection(tx,owner,&identity.workspace_id,account,&item.candidate,&decision_id,later_curated,"s03.import-staged-review.v1",item.decision_projection_present,view)?;
        markers.push(ImportReviewTerminal{version:"s04.import-review-erased.v1".into(),owner:owner.into(),workspace_id:identity.workspace_id.clone(),reset_workspace_id:reset_workspace.into(),account_id:account.into(),candidate_id:item.candidate.clone(),original_receipt_id:item.original_receipt_id.clone(),original_receipt_sha256:item.original_receipt_sha256.clone(),decision_key_sha256:item.decision_key_sha256.clone(),decision_action:item.decision_action.clone(),decision_revision:item.decision_revision,decision_binding:item.decision_binding.clone(),operation_id:operation.into(),mode:mode.into(),partition_sha256:partition_sha256.map(str::to_owned),reset_receipt_id:reset_receipt_id.into()});
    }
    for (item,marker) in items.iter().zip(&markers) {
        import_mirror::write_import_review_terminal_custody(tx,&item.identity,marker,view).map_err(|_|unavailable())?;
        let removed=tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review'",params![owner,format!("outbox_{}_candidate_review",item.original_receipt_id),item.original_receipt_id]).map_err(|_|unavailable())?;
        if removed!=if item.original_projection_present {1} else {0} {return Err(unavailable());}
        if let Some(change)=&item.decision_receipt_id {
            let removed=tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='candidate_review'",params![owner,format!("outbox_{change}_candidate_review"),change]).map_err(|_|unavailable())?;
            if removed!=if item.decision_projection_present {1} else {0} {return Err(unavailable());}
        }
        if let Some(change)=&item.decision_receipt_id {
            if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='import_staged_review'",params![owner,format!("outbox_{change}_import_staged_review"),change]).map_err(|_|unavailable())?!=1 {return Err(unavailable());}
            if tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s03.import-staged-review.v1'",params![owner,change]).map_err(|_|unavailable())?!=1 {return Err(unavailable());}
        }
        if import_mirror::read_import_review_terminal_at(tx,&item.identity,owner,Some(view)).map_err(|_|unavailable())?.as_ref()!=Some(marker) {return Err(unavailable());}
    }
    Ok(())
}
