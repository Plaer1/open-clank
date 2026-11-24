//! Narrow immutable-fact assessment; never a rename or source grant.
use super::*;
use crate::repository::explicit_add::{ExplicitAddTerminalMarker, read_explicit_add_terminal_custody};
use crate::repository::owner_custody::{verified_or_direct_custody_view,VerifiedCustodyView};

fn scan_events(tx:&Transaction<'_>,owner:&str,prefix:&str,budget:&mut ResetFenceBudget)->Result<Vec<(String,String,i64)>,RepositoryError> {
    let upper=format!("{prefix}~");let mut after=prefix.to_owned();let mut all=Vec::new();
    loop {
        let mut stmt=tx.prepare("SELECT event_id,kind,length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND event_id<?3 ORDER BY event_id LIMIT 128")?;
        let page=stmt.query_map(params![owner,after,upper],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if page.is_empty() {break;} for (id,_,size) in &page {if !(0..=8192).contains(size) {return Err(RepositoryError::NotFound);} budget.check(id.len().saturating_add(*size as usize)).map_err(RepositoryError::Validation)?;}
        after=page.last().ok_or(RepositoryError::NotFound)?.0.clone();let done=page.len()<128;all.extend(page);if done {break;}
    } Ok(all)
}

fn scan_event_ids(tx:&Transaction<'_>,owner:&str,prefix:&str,budget:&mut ResetFenceBudget)->Result<Vec<String>,RepositoryError> {
    let upper=format!("{prefix}~");let mut after=prefix.to_owned();let mut all=Vec::new();
    loop {
        let mut stmt=tx.prepare("SELECT event_id FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND event_id<?3 ORDER BY event_id LIMIT 128")?;
        let page=stmt.query_map(params![owner,after,upper],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if page.is_empty() {break;} for id in &page {budget.check(id.len()).map_err(RepositoryError::Validation)?;}
        after=page.last().ok_or(RepositoryError::NotFound)?.clone();let done=page.len()<128;all.extend(page);if done {break;}
    } Ok(all)
}

fn assess_original_and_marker_facts(tx:&Transaction<'_>,account:&str,owner:&str,view:&VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    for (prefix,kind) in [("outbox_cs_explicit_add_","explicit_add"),("outbox_s04_ingest_erased_","s04_ingest_erased"),("outbox_s04_pair_full_source_","s04_pair_full_source_erased"),("outbox_s04_pair_full_job_","s04_pair_full_job_erased")] {
        for (id,actual,_) in scan_events(tx,owner,prefix,budget)? {
            // Review receipts have their own longer indexed prefix.
            if prefix=="outbox_cs_explicit_add_" && id.starts_with("outbox_cs_explicit_add_review_") {continue;}
            if actual!=kind {return Err(RepositoryError::NotFound);}
            match kind {
                "explicit_add"=>crate::store::sqlite::verify_original_event(tx,owner,account,&id,view,budget).map_err(RepositoryError::Validation)?,
                "s04_ingest_erased"=>super::selective_ingest_fact::verify_selective_ingest_event(tx,owner,account,&id,view,budget)?,
                _=>{crate::store::sqlite::s05_full_denial::verify_terminal_event(tx,owner,account,&id,view,budget).map_err(RepositoryError::Validation)?;},
            }
        }
    }
    Ok(())
}

fn assess_paired_publications(tx:&Transaction<'_>,account:&str,owner:&str,view:&VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let mut reserved=std::collections::BTreeSet::new();
    for id in scan_event_ids(tx,owner,"outbox_cs_v2_",budget)? {
        if let Some(digest)=id.strip_prefix("outbox_cs_v2_").and_then(|v|v.strip_suffix("_paired_candidate_original")) {
            if digest.len()==32 && digest.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) {
                super::paired_publication::verify_original_event(tx,owner,account,&id,view,budget)?;
                if !reserved.insert(id) {return Err(RepositoryError::NotFound);}
            }
        }
    }
    let mut after=String::new();
    loop {
        let mut stmt=tx.prepare("SELECT event_id,length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND kind='paired_candidate_original' AND event_id>?2 ORDER BY event_id LIMIT 128")?;
        let page=stmt.query_map(params![owner,after],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if page.is_empty() {break;}
        for (id,size) in &page {if !(0..=8192).contains(size) {return Err(RepositoryError::NotFound);} budget.check(id.len().saturating_add(*size as usize)).map_err(RepositoryError::Validation)?;
            if !reserved.remove(id) {return Err(RepositoryError::NotFound);}
            super::paired_publication::verify_original_event(tx,owner,account,id,view,budget)?;}
        after=page.last().ok_or(RepositoryError::NotFound)?.0.clone();if page.len()<128 {break;}
    }
    if !reserved.is_empty() {return Err(RepositoryError::NotFound);}
    // Every paired original writer leaves a matching revision-one candidate;
    // verify both directions so missing or contradictory events do not pass.
    budget.check(0).map_err(RepositoryError::Validation)?;
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_candidate_revisions r JOIN fm_v2_change_sets c ON c.owner_id=r.owner_id AND c.change_set_id=r.change_set_id WHERE r.owner_id=?1 AND r.revision=1 AND c.reason='unreviewed assistant interpretation' AND NOT EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=r.owner_id AND o.change_set_id=r.change_set_id AND o.kind='paired_candidate_original' AND o.event_id='outbox_'||r.change_set_id||'_paired_candidate_original')) OR EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=?1 AND o.kind='paired_candidate_original' AND NOT EXISTS(SELECT 1 FROM fm_v2_candidate_revisions r WHERE r.owner_id=o.owner_id AND r.change_set_id=o.change_set_id AND r.revision=1))",params![owner],|r|r.get(0))?;
    if orphan {return Err(RepositoryError::NotFound);} Ok(())
}

fn assess_denials_and_membership(tx:&Transaction<'_>,account:&str,owner:&str,view:&VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    use std::collections::BTreeSet;
    let mut expected=BTreeSet::new();
    for (prefix,kind) in [("outbox_cs_explicit_add_","explicit_add"),("outbox_cs_explicit_add_review_","explicit_add_review"),("outbox_cs_import_review_","import_review"),("outbox_cs_import_staged_review_","import_staged_review"),("outbox_s04_explicit_add_erased_","s04_explicit_add_erased"),("outbox_s04_import_review_erased_","s04_import_review_erased"),("outbox_s04_ingest_erased_","s04_ingest_erased"),("outbox_s04_pair_full_source_","s04_pair_full_source_erased"),("outbox_s04_pair_full_job_","s04_pair_full_job_erased")] {
        for (id,actual,_) in scan_events(tx,owner,prefix,budget)? {
            if kind=="explicit_add" && id.starts_with("outbox_cs_explicit_add_review_") {continue;}
            if actual!=kind || !expected.insert(id) {return Err(RepositoryError::NotFound);}
        }
    }
    let mut denial_events=BTreeSet::new();
    for id in scan_event_ids(tx,owner,"outbox_cs_reset_",budget)? {
        if let Some((receipt,tail))=id.strip_prefix("outbox_cs_reset_").and_then(|v|v.split_once("_deny_")) {
            if valid_sha256(receipt) && valid_sha256(tail) {
                crate::store::sqlite::ingest_denial_fact::verify_event(tx,owner,account,&id,view,budget).map_err(RepositoryError::Validation)?;
                if !denial_events.insert(id) {return Err(RepositoryError::NotFound);}
            }
        }
    }
    let mut after=String::new();let mut denial_ids=BTreeSet::new();
    loop {
        let mut stmt=tx.prepare("SELECT event_id,kind,length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id>?2 AND kind IN ('explicit_add','explicit_add_review','import_review','import_staged_review','s04_explicit_add_erased','s04_import_review_erased','s04_ingest_erased','s04_pair_full_source_erased','s04_pair_full_job_erased','s04_ingest_denial') ORDER BY event_id LIMIT 128")?;
        let page=stmt.query_map(params![owner,after],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if page.is_empty() {break;}
        for (id,kind,size) in &page {if !(0..=8192).contains(size) {return Err(RepositoryError::NotFound);}budget.check(id.len().saturating_add(*size as usize)).map_err(RepositoryError::Validation)?;
            if kind=="s04_ingest_denial" {if !denial_events.remove(id) {return Err(RepositoryError::NotFound);}let tomb=crate::store::sqlite::ingest_denial_fact::verify_event(tx,owner,account,id,view,budget).map_err(RepositoryError::Validation)?;if !denial_ids.insert(tomb) {return Err(RepositoryError::NotFound);}}
            else if !expected.remove(id) {return Err(RepositoryError::NotFound);}}
        after=page.last().ok_or(RepositoryError::NotFound)?.0.clone();if page.len()<128 {break;}
    }
    if !expected.is_empty() || !denial_events.is_empty() {return Err(RepositoryError::NotFound);}
    let mut after=String::new();
    loop {
        let mut stmt=tx.prepare("SELECT id FROM memory_tombstones WHERE owner=?1 AND id>?2 AND substr(id,1,8)='s04deny_' ORDER BY id LIMIT 128")?;
        let page=stmt.query_map(params![owner,after],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if page.is_empty() {break;}
        for id in &page {budget.check(id.len()).map_err(RepositoryError::Validation)?;if !denial_ids.remove(id) {return Err(RepositoryError::NotFound);}crate::store::sqlite::ingest_denial_fact::verify_tombstone(tx,owner,account,id,view,budget).map_err(RepositoryError::Validation)?;}
        after=page.last().ok_or(RepositoryError::NotFound)?.clone();if page.len()<128 {break;}
    }
    if !denial_ids.is_empty() {return Err(RepositoryError::NotFound);}
    // Original change sets and live candidate rows must have their exact event.
    budget.check(0).map_err(RepositoryError::Validation)?;
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets c WHERE c.owner_id=?1 AND c.reason IN ('s03.explicit-add.v1','s03.explicit-add-review.v1','s03.import-review.v1','s03.import-staged-review.v1') AND NOT EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id='outbox_'||c.change_set_id||CASE c.reason WHEN 's03.explicit-add.v1' THEN '_explicit_add' WHEN 's03.explicit-add-review.v1' THEN '_explicit_add_review' WHEN 's03.import-review.v1' THEN '_import_review' ELSE '_import_staged_review' END)) OR EXISTS(SELECT 1 FROM fm_v2_candidates c WHERE c.owner_id=?1 AND ((substr(c.candidate_id,1,10)='manualadd_' AND NOT EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=c.owner_id AND o.event_id='outbox_cs_explicit_add_'||substr(c.candidate_id,11)||'_explicit_add' AND o.kind='explicit_add')) OR (substr(c.candidate_id,1,13)='importreview_' AND NOT EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=c.owner_id AND o.event_id='outbox_cs_import_review_'||substr(c.candidate_id,14)||'_import_review' AND o.kind='import_review')))) OR EXISTS(SELECT 1 FROM candidates c WHERE c.owner=?1 AND ((substr(c.id,1,10)='manualadd_' AND NOT EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=c.owner AND o.event_id='outbox_cs_explicit_add_'||substr(c.id,11)||'_explicit_add' AND o.kind='explicit_add')) OR (substr(c.id,1,13)='importreview_' AND NOT EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=c.owner AND o.event_id='outbox_cs_import_review_'||substr(c.id,14)||'_import_review' AND o.kind='import_review'))))",params![owner],|r|r.get(0))?;
    if orphan {return Err(RepositoryError::NotFound);} Ok(())
}

pub(crate) fn assess_known_rename_facts(tx:&Transaction<'_>,database:&str,account:&str,owner:&str,phase:Option<&QueuedTransitionBinding>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if let Some(p)=phase {if p.database_id!=database || p.account_id!=account || p.owner!=owner {return Err(RepositoryError::NotFound);}}
    let view=verified_or_direct_custody_view(tx,database,account,owner,owner,budget)?;
    super::explicit_add_batch::collect_batch_facts(tx,owner,account,&view,budget)?;
    super::guarded_batch_forget::collect_forget_receipts(tx,owner,account,&view,budget)?;
    crate::store::sqlite::preextracted::assess_rename_f8(tx,owner,account,&view,budget).map_err(RepositoryError::Validation)?;
    crate::store::sqlite::knowledge_update_batch::assess_rename_f1(tx,owner,account,&view,budget).map_err(RepositoryError::Validation)?;
    super::guarded_batch_forget::assess_reset_forget_facts(tx,owner,account,&view,budget)?;
    assess_paired_publications(tx,account,owner,&view,budget)?;
    assess_original_and_marker_facts(tx,account,owner,&view,budget)?;
    assess_terminal_add_facts(tx,account,owner,&view,budget)?;assess_review_facts(tx,account,owner,&view,budget)?;
    assess_import_original_facts(tx,account,owner,&view,budget)?;assess_import_terminal_facts(tx,account,owner,&view,budget)?;
    assess_denials_and_membership(tx,account,owner,&view,budget)?;
    Ok(())
}

/// Checks the complete bounded terminal-Add event range only. Other fact
/// families and orphan checks remain the caller's responsibility; this is not
/// an owner-wide rename eligibility result.
fn assess_terminal_add_facts(tx:&Transaction<'_>, account:&str, owner:&str, view:&VerifiedCustodyView<'_, '_>, budget:&mut ResetFenceBudget) -> Result<usize,RepositoryError> {
    let prefix="outbox_s04_explicit_add_erased_";
    let rows=scan_events(tx,owner,prefix,budget)?;
    for (id,kind,size) in &rows {
        if kind!="s04_explicit_add_erased" || !(0..=8192).contains(size) {return Err(RepositoryError::NotFound);}
        let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,id],|r|r.get(0))?;
        let marker:ExplicitAddTerminalMarker=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if marker.account_id!=account || !view.owners().iter().any(|x|x==&marker.owner) || *id!=format!("outbox_s04_explicit_add_erased_{}",sha256_hex(&serde_json::to_vec(&(&marker.owner,&marker.candidate_id))?)) {return Err(RepositoryError::NotFound);}
        let checked=read_explicit_add_terminal_custody(tx,&marker.workspace_id,account,&marker.candidate_id,&marker.owner,view)?.ok_or(RepositoryError::NotFound)?;
        if checked!=marker {return Err(RepositoryError::NotFound);}
    }
    Ok(rows.len())
}

/// Independently verify the two immutable review receipt families. Does not
/// assert the absence of other families or orphan records.
fn assess_review_facts(tx:&Transaction<'_>, account:&str, owner:&str, view:&VerifiedCustodyView<'_, '_>,
    budget:&mut ResetFenceBudget) -> Result<usize,RepositoryError> {
    use crate::repository::explicit_add::{ExplicitAddReviewReceipt,StagedImportDecisionReceipt,
        read_explicit_add_review_receipt_custody,read_staged_import_decision_receipt_custody,
        explicit_add_review_receipt_id,staged_import_decision_receipt_id};
    let mut count=0usize;
    for (prefix,kind) in [("outbox_cs_explicit_add_review_","explicit_add_review"),("outbox_cs_import_staged_review_","import_staged_review")] {
        let rows=scan_events(tx,owner,prefix,budget)?;
        for (id,k,size) in rows {
            if k!=kind || !(0..=8192).contains(&size) {return Err(RepositoryError::NotFound);}
            let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,id],|r|r.get(0))?;
            let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
            if kind=="explicit_add_review" {
                let item:ExplicitAddReviewReceipt=serde_json::from_value(value)?;
                if item.account!=account || id!=format!("outbox_{}_explicit_add_review",explicit_add_review_receipt_id(&item.owner,&item.workspace,account,&item.candidate)?) {return Err(RepositoryError::NotFound);}
                let verified=read_explicit_add_review_receipt_custody(tx,&item.workspace,account,&item.candidate,&item.owner,view)?.ok_or(RepositoryError::NotFound)?;
                if verified!=item {return Err(RepositoryError::NotFound);}
            } else {
                let item:StagedImportDecisionReceipt=serde_json::from_value(value)?;
                if item.account_id!=account || id!=format!("outbox_{}_import_staged_review",staged_import_decision_receipt_id(&item.owner,&item.workspace_id,account,&item.candidate_id)?) {return Err(RepositoryError::NotFound);}
                let verified=read_staged_import_decision_receipt_custody(tx,&item.workspace_id,account,&item.candidate_id,&item.owner,view)?.ok_or(RepositoryError::NotFound)?;
                if verified!=item {return Err(RepositoryError::NotFound);}
            }
            count+=1;
        }
    }
    Ok(count)
}

/// Original import publication receipts, verified independently of current proposal.
fn assess_import_original_facts(tx:&Transaction<'_>,account:&str,owner:&str,view:&VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<usize,RepositoryError> {
    use crate::repository::explicit_add::ImportReviewReceipt;
    use crate::repository::import_mirror::{ImportReviewIdentity,original_for_import_identity_at};
    let prefix="outbox_cs_import_review_";
    let rows=scan_events(tx,owner,prefix,budget)?;
    for (id,kind,size) in &rows {
        if kind!="import_review" || !(0..=8192).contains(size) {return Err(RepositoryError::NotFound);}
        let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,id],|r|r.get(0))?;
        let receipt:ImportReviewReceipt=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if receipt.account_id!=account {return Err(RepositoryError::NotFound);}
        let identity=ImportReviewIdentity{owner:receipt.owner.clone(),workspace_id:receipt.workspace_id.clone(),account_id:receipt.account_id.clone(),batch_id:receipt.batch_id.clone(),item_id:receipt.item_id.clone(),suggestion_id:receipt.suggestion_id.clone(),review_key_hash:receipt.review_key_hash.clone(),proposal_fingerprint:receipt.proposal_fingerprint.clone()};
        let (verified,_)=original_for_import_identity_at(tx,&identity,owner,Some(view))?;
        let suffix=verified.candidate_id.strip_prefix("importreview_").ok_or(RepositoryError::NotFound)?;
        if *id!=format!("outbox_cs_import_review_{suffix}_import_review") || verified!=receipt {return Err(RepositoryError::NotFound);}
    }
    Ok(rows.len())
}

/// Exact erased import receipts retain their original publication identity.
fn assess_import_terminal_facts(tx:&Transaction<'_>,account:&str,owner:&str,view:&VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<usize,RepositoryError> {
    use crate::repository::import_mirror::{ImportReviewTerminal,import_identity_for_candidate_at,read_import_review_terminal_at};
    let prefix="outbox_s04_import_review_erased_";
    let rows=scan_events(tx,owner,prefix,budget)?;
    for (id,kind,size) in &rows {
        if kind!="s04_import_review_erased" || !(0..=8192).contains(size) {return Err(RepositoryError::NotFound);}
        let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,id],|r|r.get(0))?;
        let marker:ImportReviewTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if marker.account_id!=account {return Err(RepositoryError::NotFound);}
        let identity=import_identity_for_candidate_at(tx,owner,&marker.workspace_id,account,&marker.candidate_id,Some(view))?;
        let verified=read_import_review_terminal_at(tx,&identity,owner,Some(view))?.ok_or(RepositoryError::NotFound)?;
        if verified!=marker || *id!=format!("outbox_s04_import_review_erased_{}",marker.candidate_id.strip_prefix("importreview_").ok_or(RepositoryError::NotFound)?) {return Err(RepositoryError::NotFound);}
    }
    Ok(rows.len())
}
