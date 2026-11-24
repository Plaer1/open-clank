//! Private verification of the stopped-copy quota coverage cutoff.
use super::*;
use sha2::{Digest,Sha256};

/// Verifies the immutable profile receipt and same-transaction account custody.
/// A full rolling window must have elapsed; existing caller clock/currentness
/// checks still apply. This is coverage evidence, never an access grant.
fn verified_cutoff_receipt(tx:&Transaction<'_>, owner:&str, account:&str, now_ms:i64,
    budget:&mut crate::repository::ResetFenceBudget)->Result<i64,String> {
    fn unavailable()->String {"S05 quota coverage unavailable".into()}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|unavailable())?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_purge.v1'",[],|r|r.get(0)).optional().map_err(|_|unavailable())?;
    let raw:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_cutoff.v1'",[],|r|r.get(0)).optional().map_err(|_|unavailable())?;
    let raw=raw.ok_or_else(unavailable)?;
    if marker.as_deref()!=Some("1") || raw.len()>4096 || owner.is_empty() || account.is_empty() {return Err(unavailable());}
    let parts:Vec<&str>=raw.split('|').collect();
    let dbhex=database.as_bytes().iter().map(|v|format!("{v:02x}")).collect::<String>();
    if parts.len()!=4 || parts[0]!="v1" || parts[1]!=dbhex || parts[2].is_empty() || parts[2].starts_with('0') || !parts[2].bytes().all(|v|v.is_ascii_digit()) || parts[3].len()!=64 || !parts[3].bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) {return Err(unavailable());}
    let cutoff:i64=parts[2].parse().map_err(|_|unavailable())?;
    if cutoff<=0 || now_ms<cutoff {return Err(unavailable());}
    let mut stmt=tx.prepare("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name").map_err(|_|unavailable())?;
    let structure = crate::repository::document_structure::enabled(tx).map_err(|_| unavailable())?;
    let mut rows=stmt.query([]).map_err(|_|unavailable())?;
    let mut hash=Sha256::new();
    let mut before_structure=Sha256::new();
    while let Some(row)=rows.next().map_err(|_|unavailable())? {
        let table: String = row.get(2).map_err(|_| unavailable())?;
        let added = structure && crate::repository::document_structure::owns_schema_table(&table);
        for i in 0..4 {
            let part: String = row.get(i).map_err(|_| unavailable())?;
            budget.check(part.len()).map_err(|_| unavailable())?;
            let length = (part.len() as u64).to_be_bytes();
            hash.update(length);
            hash.update(part.as_bytes());
            if !added {
                before_structure.update(length);
                before_structure.update(part.as_bytes());
            }
        }
    }
    drop(rows);drop(stmt);
    let schema = format!("{:x}", hash.finalize());
    if schema != parts[3] && (!structure || format!("{:x}", before_structure.finalize()) != parts[3]) {
        return Err(unavailable());
    }
    let view=crate::repository::owner_custody::verified_or_direct_custody_view(tx,&database,account,owner,owner,budget).map_err(|_|unavailable())?;
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    drop(view);Ok(cutoff)
}
/// The caller has already verified guarded schema, account custody and policy in this transaction.
/// Only a wholly absent optional profile is legacy; partial or malformed proof refuses.
pub(super) fn quota_coverage(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,budget:&mut crate::repository::ResetFenceBudget)->Result<S05QuotaCoverage,String> {
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_purge.v1'",[],|r|r.get(0)).optional().map_err(|_|"S05 quota coverage unavailable")?;
    let cutoff:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_cutoff.v1'",[],|r|r.get(0)).optional().map_err(|_|"S05 quota coverage unavailable")?;
    if marker.is_none() && cutoff.is_none() {return Ok(S05QuotaCoverage::Unsupported);}
    let cutoff=verified_cutoff_receipt(tx,owner,account,now_ms,budget)?;
    let ready_at=cutoff.checked_add(86_400_000).ok_or("S05 quota coverage unavailable")?;
    if now_ms<ready_at {Ok(S05QuotaCoverage::Warming)} else {Ok(S05QuotaCoverage::Ready(cutoff))}
}
/// Admission additionally requires the entire pre-profile uncertainty window to expire.
pub(super) fn verified_cutoff(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,budget:&mut crate::repository::ResetFenceBudget)->Result<i64,String>{
    let cutoff=verified_cutoff_receipt(tx,owner,account,now_ms,budget)?;
    if now_ms<cutoff.checked_add(86_400_000).ok_or("S05 quota coverage unavailable")? {return Err("S05 quota coverage unavailable".into());}
    Ok(cutoff)
}

// These helpers require the caller to have verified the selected lifecycle partition.
// Draft: validate Host-origin S05 binding and original input commitment.
fn python_ascii(raw:&str)->String {
    let mut out=String::with_capacity(raw.len());
    for ch in raw.chars() {
        if ch.is_ascii() && ch!='\u{7f}' {out.push(ch);continue;}
        let mut units=[0u16;2];for unit in ch.encode_utf16(&mut units).iter(){out.push_str(&format!("\\u{unit:04x}"));}
    }out
}
fn py_string(value:&str)->Result<String,String> {
    Ok(python_ascii(&serde_json::to_string(value).map_err(|_|"S05 original unavailable")?))
}
fn py_fragment(value:Option<&str>)->Result<String,String> {
    match value {None=>Ok("null".into()),Some(raw)=>{
        if raw.len()>65536 {return Err("S05 original unavailable".into())}
        crate::field_authority::parse_unique_json(raw).map_err(|_|"S05 original unavailable")?;
        Ok(python_ascii(raw))
    }}
}
pub(super) fn capture_host_original(tx:&Transaction<'_>, owner:&str, account:&str, job:&str,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,
    budget:&mut crate::repository::ResetFenceBudget)->Result<(String,String),String> {
    let row:Option<(String,String,String,String,String,i64,String,String,String,String,String,String,String,Option<String>,Option<String>)>=tx.query_row(
        "SELECT j.kind,j.input_hash,r.source_ref_json,r.source_account_id,r.target_workspace_id,r.owner_epoch,r.durable_epoch,r.binding_fingerprint,r.admission_id,r.policy_fingerprint,r.authority_fingerprint,r.connection_id,r.model_route_id,r.assignment_json,r.coverage_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id WHERE j.owner_id=?1 AND j.job_id=?2 AND j.project_key='' AND j.workspace_key=r.target_workspace_id AND length(CAST(r.source_ref_json AS BLOB))<=8192 AND coalesce(length(CAST(r.assignment_json AS BLOB)),0)<=16384 AND coalesce(length(CAST(r.coverage_json AS BLOB)),0)<=8192",
        params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?,r.get(10)?,r.get(11)?,r.get(12)?,r.get(13)?,r.get(14)?))).optional().map_err(|_|"S05 original unavailable")?;
    let (kind,input,source,source_account,workspace,epoch,durable,fingerprint,admission,policy,authority,connection,route,assignment,coverage)=row.ok_or("S05 original unavailable")?;
    let hash_ok=|v:&str|v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b));
    let text_ok=|v:&str|!v.is_empty() && v.len()<=512 && v.trim()==v && !v.chars().any(char::is_control);
    if view.current_owner()!=owner || view.account_id()!=account || source_account!=account || !matches!(kind.as_str(),"s05_host_user_capture"|"completed_turn_review"|"assistant_interpretation_review") || epoch<0 || durable.is_empty() || durable.len()>20 || !durable.bytes().all(|b|b.is_ascii_digit()) || (durable.len()>1 && durable.starts_with('0')) || [&input,&fingerprint,&policy,&authority].iter().any(|v|!hash_ok(v)) || [&workspace,&admission,&connection,&route].iter().any(|v|!text_ok(v)) {return Err("S05 original unavailable".into());}
    budget.check(source.len()+assignment.as_ref().map_or(0,String::len)+coverage.as_ref().map_or(0,String::len)).map_err(|_|"S05 original unavailable")?;
    let source_value=crate::field_authority::parse_unique_json(&source).map_err(|_|"S05 original unavailable")?;
    if source_value.get("account_id").and_then(|v|v.as_str())!=Some(account) {return Err("S05 original unavailable".into());}
    let mut captured=Vec::new();
    for candidate in view.owners() {
        let base=format!("{{\"durable_epoch\":{},\"memory_owner\":{},\"memory_workspace\":{},\"owner_epoch\":{},\"source_account_id\":{}}}",py_string(&durable)?,py_string(candidate)?,py_string(&workspace)?,epoch,py_string(account)?);
        if crate::repository::sha256_hex(base.as_bytes())==fingerprint {captured.push(candidate.clone());}
    }
    if captured.len()!=1{return Err("S05 original unavailable".into());}
    let captured=captured.pop().unwrap();view.for_captured(tx,&captured).map_err(|_|"S05 original unavailable")?;
    let binding=format!("{{\"durable_epoch\":{},\"fingerprint\":{},\"memory_owner\":{},\"memory_workspace\":{},\"owner_epoch\":{},\"source_account_id\":{}}}",py_string(&durable)?,py_string(&fingerprint)?,py_string(&captured)?,py_string(&workspace)?,epoch,py_string(account)?);
    let evidence=format!("{{\"admission_id\":{},\"assignment\":{},\"authority_fingerprint\":{},\"connection_id\":{},\"model_route_id\":{},\"policy_fingerprint\":{},\"source\":{}}}",py_string(&admission)?,py_fragment(assignment.as_deref())?,py_string(&authority)?,py_string(&connection)?,py_string(&route)?,py_string(&policy)?,py_fragment(Some(&source))?);
    let original=format!("{{\"binding\":{},\"coverage\":{},\"evidence\":{},\"kind\":{}}}",binding,py_fragment(coverage.as_deref())?,evidence,py_string(&kind)?);
    if crate::repository::sha256_hex(original.as_bytes())!=input {return Err("S05 original unavailable".into());}
    Ok((captured,input))
}

// Lifecycle owners run this once per account transaction, never once per job.
// The live-window owner instead validates every selected current-window row.
fn verify_owner_orphan_reservations(tx:&Transaction<'_>,owner:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    budget.check(0).map_err(|_|"S05 quota unavailable")?;
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q LEFT JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch LEFT JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id WHERE q.owner_id=?1 AND (a.attempt_id IS NULL OR j.job_id IS NULL))",[owner],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    if orphan{return Err("S05 quota unavailable".into())}Ok(())
}


/// Inventory only exact prepared completed-turn slots for guarded reset.
/// The caller must separately hold the real Host account drain during effects.
pub(super) fn prepared_reset_jobs_in(tx:&Transaction<'_>,owner:&str,account:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<(String,String)>,String> {
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_purge.v1'",[],|r|r.get(0)).optional().map_err(|_|"S05 quota unavailable")?;
    match marker.as_deref(){None=>return Ok(Vec::new()),Some("1")=>{},_=>return Err("S05 quota unavailable".into())}
    verified_cutoff_receipt(tx,owner,account,chrono::Utc::now().timestamp_millis(),budget)?;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let view=crate::repository::owner_custody::verified_or_direct_custody_view(tx,&database,account,owner,owner,budget).map_err(|_|"S05 quota unavailable")?;
    let mut stmt=tx.prepare("SELECT j.job_id,j.workspace_key,j.attempt_count FROM fm_v2_jobs j WHERE j.owner_id=?1 AND j.kind IN ('completed_turn_review','assistant_interpretation_review') AND j.state IN ('queued','retry_wait') ORDER BY j.job_id LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
    let mut rows=stmt.query([owner]).map_err(|_|"S05 quota unavailable")?;let mut out=Vec::new();
    while let Some(row)=rows.next().map_err(|_|"S05 quota unavailable")? {
        let job:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let workspace:String=row.get(1).map_err(|_|"S05 quota unavailable")?;let count:i64=row.get(2).map_err(|_|"S05 quota unavailable")?;
        budget.check(job.len()+workspace.len()+128).map_err(|_|"S05 quota unavailable")?;
        if out.len()>=100000 || !(0..3).contains(&count) {return Err("S05 quota unavailable".into())}
        let pre=count as u32;
        let fact=crate::repository::s05_budget::verify_unreserved_slot_in(tx,owner,account,&workspace,&job,pre,&view,budget).map_err(|_|"S05 quota unavailable")?;
        let (captured,_)=capture_host_ledger_binding(tx,owner,account,&job,&view,budget)?;
        let kind:String=tx.query_row("SELECT kind FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
        if kind=="assistant_interpretation_review" {
            if pre!=0 {return Err("S05 quota unavailable".into())}
            crate::repository::paired_pending::verified_pair_snapshot_in(tx,owner,account,&workspace,&job,&view,false,budget).map_err(|_|"S05 quota unavailable")?;
        }
        if fact.captured_owner!=captured || read_slot_terminal(tx,owner,account,&workspace,&job,pre,&view,budget)?.is_some() {return Err("S05 quota unavailable".into())}
        for later in pre+1..3 {
            if crate::repository::s05_budget::read_original_slot_in(tx,owner,account,&workspace,&job,later,&view,budget).map_err(|_|"S05 quota unavailable")?.is_some() || crate::repository::s05_budget::read_reserved_slot_in(tx,owner,account,&workspace,&job,later,&view,budget).map_err(|_|"S05 quota unavailable")?.is_some() {return Err("S05 quota unavailable".into())}
        }
        let hash:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,format!("cs_{}",fact.slot)],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
        out.push((job,hash));
    }
    Ok(out)
}

// Caller must supply the verified transaction-local offline cutoff and a validated lifecycle partition.
#[derive(serde::Serialize)]
pub(super) struct PendingSlot {job_id:String,pre_attempt_count:u32,captured_owner:String,workspace_id:String,slot:String,original_receipt_sha256:String,result_receipt_sha256:Option<String>,attempt_sha256:Option<String>,lease_epoch:Option<i64>,disposition:String}
pub(super) struct LedgerCapture {pub(super) charges:Vec<crate::repository::s05_budget::RetainedCharge>,pub(super) slots:Vec<PendingSlot>}
fn capture_ledger_attempts(tx:&Transaction<'_>,owner:&str,account:&str,jobs:&[String],cutoff_ms:i64,erased_receipts:Option<&std::collections::BTreeSet<String>>,allow_prepared_reset:bool,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,
    budget:&mut crate::repository::ResetFenceBudget)->Result<LedgerCapture,String> {
    use crate::repository::s05_budget::{RetainedCharge,validate_charge,charge_key};
    if cutoff_ms<=0{return Err("S05 quota unavailable".into());}
    let mut out=Vec::new();let mut slots=Vec::new();let mut seen=std::collections::BTreeSet::new();
    for job in jobs {
        budget.check(job.len()).map_err(|_|"S05 quota unavailable")?;
        let (kind,workspace):(String,String)=tx.query_row("SELECT kind,workspace_key FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND length(CAST(workspace_key AS BLOB)) BETWEEN 1 AND 512",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_|"S05 quota unavailable")?;
        let (captured,input)=if kind=="access_consolidation" {capture_s08_ledger_binding(tx,owner,account,job,view,budget)?} else {capture_host_ledger_binding(tx,owner,account,job,view,budget)?};
        let oversized:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 AND (length(CAST(attempt_id AS BLOB))=0 OR length(CAST(attempt_id AS BLOB))>512))",params![owner,job],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
        if oversized {return Err("S05 quota unavailable".into());}
        let expected_count:i64=tx.query_row("SELECT attempt_count FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
        if !(0..=3).contains(&expected_count) {return Err("S05 quota unavailable".into());}
        let mut stmt=tx.prepare("SELECT a.attempt_id,a.lease_epoch,a.state,q.state,q.reserved_at_ms,q.settled_at_ms,q.charged_calls,q.charged_seconds,q.charged_input_tokens,q.charged_output_tokens,a.attempt_number FROM fm_v2_attempts a LEFT JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2 ORDER BY a.attempt_number,a.attempt_id LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
        let mut rows=stmt.query(params![owner,job]).map_err(|_|"S05 quota unavailable")?;let mut count=0usize;
        while let Some(row)=rows.next().map_err(|_|"S05 quota unavailable")? {
            count+=1;if count>3 || row.get::<_,i64>(10).map_err(|_|"S05 quota unavailable")?!=count as i64 {return Err("S05 quota unavailable".into())}
            let attempt:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let epoch:i64=row.get(1).map_err(|_|"S05 quota unavailable")?;
            let attempt_state:String=row.get(2).map_err(|_|"S05 quota unavailable")?;let state:Option<String>=row.get(3).map_err(|_|"S05 quota unavailable")?;
            budget.check(job.len()+attempt.len()+128).map_err(|_|"S05 quota unavailable")?;
            let reserved_at_ms:i64=row.get(4).map_err(|_|"S05 quota unavailable")?;
            let mut original_digest=None;let mut result_digest=None;
            if kind!="access_consolidation" {
                let pre=(count-1) as u32;
                let original=crate::repository::s05_budget::read_original_slot_in(tx,owner,account,&workspace,job,pre,view,budget).map_err(|_|"S05 quota unavailable")?;
                if let Some(original)=original {
                    if original.captured_owner!=captured{return Err("S05 quota unavailable".into())}
                    let result=crate::repository::s05_budget::read_reserved_slot_in(tx,owner,account,&workspace,job,pre,view,budget).map_err(|_|"S05 quota unavailable")?.ok_or("S05 quota unavailable")?;
                    if result["attempt_id"]!=attempt || result["lease_epoch"]!=epoch {return Err("S05 quota unavailable".into())}
                    let original_id=format!("cs_{}",original.slot);let result_id=format!("cs_{}_reserved",original.slot);
                    let original_sha:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,original_id],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
                    let result_sha:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,result_id],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
                    let disposition=match state.as_deref(){Some("settled")=>"settled",Some("revoked") if attempt_state=="fenced"=>"revoked",_=>return Err("S05 quota unavailable".into())};
                    slots.push(PendingSlot{job_id:job.clone(),pre_attempt_count:pre,captured_owner:captured.clone(),workspace_id:workspace.clone(),slot:original.slot,original_receipt_sha256:original_sha.clone(),result_receipt_sha256:Some(result_sha.clone()),attempt_sha256:Some(crate::repository::s05_budget::attempt_digest(account,&attempt).map_err(|_|"S05 quota unavailable")?),lease_epoch:Some(epoch),disposition:disposition.into()});
                    original_digest=Some(original_sha);result_digest=Some(result_sha);
                } else {
                    if crate::repository::s05_budget::read_reserved_slot_in(tx,owner,account,&workspace,job,pre,view,budget).map_err(|_|"S05 quota unavailable")?.is_some() || reserved_at_ms>=cutoff_ms {return Err("S05 quota unavailable".into())}
                }
            }
            if state.as_deref()==Some("revoked") {if attempt_state!="fenced" {return Err("S05 quota unavailable".into())}continue;}
            if state.as_deref()!=Some("settled") || !matches!(attempt_state.as_str(),"succeeded"|"failed_retryable"|"failed_terminal"|"cancelled"|"fenced") {return Err("S05 quota unavailable".into())}
            let fact=RetainedCharge{version:1,account_id:account.into(),captured_owner:captured.clone(),job_kind:kind.clone(),job_sha256:crate::repository::s05_budget::job_digest(account,job).map_err(|_|"S05 quota unavailable")?,attempt_sha256:crate::repository::s05_budget::attempt_digest(account,&attempt).map_err(|_|"S05 quota unavailable")?,lease_epoch:epoch,original_sha256:input.clone(),reserved_at_ms,settled_at_ms:row.get(5).map_err(|_|"S05 quota unavailable")?,charged_calls:row.get(6).map_err(|_|"S05 quota unavailable")?,charged_seconds:row.get(7).map_err(|_|"S05 quota unavailable")?,charged_input_tokens:row.get(8).map_err(|_|"S05 quota unavailable")?,charged_output_tokens:row.get(9).map_err(|_|"S05 quota unavailable")?,charge_basis:"unknown".into(),reserve_original_sha256:original_digest,reserve_result_sha256:result_digest};
            validate_charge(&fact).map_err(|_|"S05 quota unavailable")?;if !seen.insert(charge_key(&fact).map_err(|_|"S05 quota unavailable")?){return Err("S05 quota unavailable".into())}out.push(fact);
        }
        if count as i64!=expected_count {
            if count!=0 || kind=="access_consolidation" {return Err("S05 quota unavailable".into())}
            let receipt=crate::repository::quota_ingest_erased_receipt_in(tx,owner,account,job,view,budget).map_err(|_|"S05 quota unavailable")?.ok_or("S05 quota unavailable")?;
            if erased_receipts.is_none_or(|set|!set.contains(&receipt)){return Err("S05 quota unavailable".into())}
            continue
        }
        if kind!="access_consolidation" {for pre in count as u32..3 {
            let original=crate::repository::s05_budget::read_original_slot_in(tx,owner,account,&workspace,job,pre,view,budget).map_err(|_|"S05 quota unavailable")?;
            if let Some(original)=original {
                if !allow_prepared_reset || !matches!(kind.as_str(),"completed_turn_review"|"assistant_interpretation_review") || pre!=count as u32 || original.captured_owner!=captured {return Err("S05 quota unavailable".into())}
                if kind=="assistant_interpretation_review" {
                    if pre!=0 {return Err("S05 quota unavailable".into())}
                    crate::repository::paired_pending::verified_pair_snapshot_in(tx,owner,account,&workspace,job,view,false,budget).map_err(|_|"S05 quota unavailable")?;
                }
                let verified=crate::repository::s05_budget::verify_unreserved_slot_in(tx,owner,account,&workspace,job,pre,view,budget).map_err(|_|"S05 quota unavailable")?;
                if verified.slot!=original.slot {return Err("S05 quota unavailable".into())}
                let id=format!("cs_{}",original.slot);
                let hash:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,id],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
                slots.push(PendingSlot{job_id:job.clone(),pre_attempt_count:pre,captured_owner:captured.clone(),workspace_id:workspace.clone(),slot:original.slot,original_receipt_sha256:hash,result_receipt_sha256:None,attempt_sha256:None,lease_epoch:None,disposition:"cancelled_before_reserve".into()});
            } else if crate::repository::s05_budget::read_reserved_slot_in(tx,owner,account,&workspace,job,pre,view,budget).map_err(|_|"S05 quota unavailable")?.is_some(){return Err("S05 quota unavailable".into())}
        }}
    }Ok(LedgerCapture{charges:out,slots})
}

// Draft only: reset facts are children of the original guarded S06 receipt.
#[derive(Clone,serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ResetCharge {version:u32, charge:crate::repository::s05_budget::RetainedCharge, reset_receipt_id:String}
fn reset_event(f:&ResetCharge)->Result<String,String>{
    let key=crate::repository::s05_budget::charge_key(&f.charge).map_err(|_|"S05 quota unavailable")?;
    let raw=serde_json::to_vec(&("s05.quota-reset.v1",&f.charge.account_id,&f.reset_receipt_id,key)).map_err(|_|"S05 quota unavailable")?;
    Ok(format!("outbox_s05_quota_reset_{}",crate::repository::sha256_hex(&raw)))
}
fn verify_reset_receipt(tx:&Transaction<'_>,owner:&str,f:&ResetCharge,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    use crate::repository::s05_budget::validate_charge;
    if f.version!=1 || view.current_owner()!=owner || view.account_id()!=f.charge.account_id {return Err("S05 quota unavailable".into())}
    validate_charge(&f.charge).map_err(|_|"S05 quota unavailable")?;
    view.for_captured(tx,&f.charge.captured_owner).map_err(|_|"S05 quota unavailable")?;
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",params![owner,f.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|"S05 quota unavailable")?;
    let (hash,raw,actor,actor_id,contract,source)=row.ok_or("S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
    let v=crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?;
    let captured=v["request"]["owner"].as_str().ok_or("S05 quota unavailable")?;view.for_captured(tx,captured).map_err(|_|"S05 quota unavailable")?;
    let operation=v["request"]["operation_id"].as_str().ok_or("S05 quota unavailable")?;
    if f.reset_receipt_id!=crate::repository::ingest_erased_receipt_id(&f.charge.account_id,operation).map_err(|_|"S05 quota unavailable")? || hash!=crate::repository::sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=captured || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || v.as_object().map(|x|x.len())!=Some(4) || v["version"]!="s06.guarded-reset.v1" || v["request_sha256"]!=crate::repository::sha256_hex(&serde_json::to_vec(&v["request"]).map_err(|_|"S05 quota unavailable")?) || v["request"]["account_id"]!=f.charge.account_id || v["result"]["complete"]!=true || !v["request"]["components"].as_array().is_some_and(|a|a.iter().any(|x|x=="ingest"||x=="memories")){return Err("S05 quota unavailable".into())}
    let paired:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![owner,format!("outbox_{}_s06_guarded_reset",f.reset_receipt_id),f.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|"S05 quota unavailable")?;
    if paired!=Some((raw,0)){return Err("S05 quota unavailable".into())}Ok(())
}
pub(super) fn write_reset_charges(tx:&Transaction<'_>,owner:&str,charges:&[crate::repository::s05_budget::RetainedCharge],receipt:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    for charge in charges {
        let fact=ResetCharge{version:1,charge:charge.clone(),reset_receipt_id:receipt.into()};verify_reset_receipt(tx,owner,&fact,view,budget)?;
        let event=reset_event(&fact)?;let raw=serde_json::to_string(&fact).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let change=format!("cs_s05_quota_reset_{}",event.strip_prefix("outbox_s05_quota_reset_").ok_or("S05 quota unavailable")?);let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','s05_quota','s05.quota-reset.v1',NULL,?3,?4,?5,?6)",params![owner,change,crate::ontology::CONTRACT_VERSION,crate::repository::sha256_hex(raw.as_bytes()),now,raw]).map_err(|_|"S05 quota unavailable")?;
        tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s05_quota_reset',?4,0,?5)",params![owner,event,change,raw,now]).map_err(|_|"S05 quota unavailable")?;
    }Ok(())
}

// Draft: each affected reset has one immutable, account-scoped coverage pointer,
// including zero; fixed-size digests bind every exact paired charge fact.
#[derive(Clone,serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ResetCoverage {version:u32,database_id:String,account_id:String,window_account_sha256:String,reset_receipt_id:String,reset_receipt_sha256:String,count:u64,max_at_ms:i64,batch_max_at_ms:Vec<i64>,batch_sha256:Vec<String>,slot_count:u64,slot_batch_sha256:Vec<String>}
fn reset_coverage_entries(charges:&[crate::repository::s05_budget::RetainedCharge],receipt:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<String>,String>{
    let mut entries=std::collections::BTreeMap::new();
    for charge in charges {
        let fact=ResetCharge{version:1,charge:charge.clone(),reset_receipt_id:receipt.into()};
        let key=crate::repository::s05_budget::charge_key(charge).map_err(|_|"S05 quota unavailable")?;
        let raw=serde_json::to_vec(&fact).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        if entries.insert(key,crate::repository::sha256_hex(&raw)).is_some(){return Err("S05 quota unavailable".into())}
    }
    let entries:Vec<_>=entries.into_iter().collect();let mut hashes=Vec::new();
    for batch in entries.chunks(64) {let raw=serde_json::to_vec(batch).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;hashes.push(crate::repository::sha256_hex(&raw));}
    Ok(hashes)
}
fn reset_pointer(account:&str,receipt:&str)->Result<String,String>{Ok(crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-reset-request.v1",account,receipt)).map_err(|_|"S05 quota unavailable")?))}
pub(super) fn write_reset_coverage(tx:&Transaction<'_>,owner:&str,account:&str,receipt:&str,charges:&[crate::repository::s05_budget::RetainedCharge],slots:&[PendingSlot],view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    if view.current_owner()!=owner || view.account_id()!=account {return Err("S05 quota unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let (hash,raw,actor,actor_id,contract,source):(String,String,String,String,String,Option<String>)=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",params![owner,receipt],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(|_|"S05 quota unavailable")?;
    if hash!=crate::repository::sha256_hex(raw.as_bytes()){return Err("S05 quota unavailable".into())}
    // The S06 receipt must be verified even when the charge set is empty.
    let v=crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?;
    let captured=v["request"]["owner"].as_str().ok_or("S05 quota unavailable")?;view.for_captured(tx,captured).map_err(|_|"S05 quota unavailable")?;
    let operation=v["request"]["operation_id"].as_str().ok_or("S05 quota unavailable")?;
    if actor!="user" || actor_id!=captured || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || receipt!=crate::repository::ingest_erased_receipt_id(account,operation).map_err(|_|"S05 quota unavailable")? || v.as_object().map(|x|x.len())!=Some(4) || v["version"]!="s06.guarded-reset.v1" || v["request_sha256"]!=crate::repository::sha256_hex(&serde_json::to_vec(&v["request"]).map_err(|_|"S05 quota unavailable")?) || v["request"]["account_id"]!=account || v["result"]["complete"]!=true || !v["request"]["components"].as_array().is_some_and(|a|a.iter().any(|x|x=="ingest"||x=="memories")){return Err("S05 quota unavailable".into())}
    let pair:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![owner,format!("outbox_{receipt}_s06_guarded_reset"),receipt],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|"S05 quota unavailable")?;
    if pair!=Some((raw,0)){return Err("S05 quota unavailable".into())}
    let expected=reset_coverage_entries(charges,receipt,budget)?;
    let written=read_reset_charges(tx,owner,account,view,budget)?.into_iter().filter(|f|f.reset_receipt_id==receipt).map(|f|f.charge).collect::<Vec<_>>();
    if written.len()!=charges.len() || reset_coverage_entries(&written,receipt,budget)?!=expected {return Err("S05 quota unavailable".into());}
    let request=reset_pointer(account,receipt)?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Reset,&database,account)?;
    let window_hash=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota unavailable")?;
    let meta=format!("s05_quota_reset_receipt_{account_hash}_{request}");let context=format!("s05-reset-purge:{meta}");
    if slots.len()>100000 {return Err("S05 quota unavailable".into());}
    let batches=make_reset_batches(charges,receipt,&meta,&window_hash,budget)?;
    let (terminals,slot_batches)=make_slot_batches(slots,account,&meta,&window_hash,budget)?;
    let mut slot_batch_sha256=Vec::new();for batch in &slot_batches {let raw=serde_json::to_vec(batch).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;slot_batch_sha256.push(crate::repository::sha256_hex(&raw));}
    let mut batch_sha256=Vec::new();let mut batch_max_at_ms=Vec::new();
    for batch in &batches {let raw=serde_json::to_vec(batch).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;batch_sha256.push(crate::repository::sha256_hex(&raw));batch_max_at_ms.push(batch.max_at_ms);}
    let max_at_ms=batch_max_at_ms.iter().copied().max().unwrap_or(0);
    let coverage=ResetCoverage{version:1,database_id:database.clone(),account_id:account.into(),window_account_sha256:window_hash.clone(),reset_receipt_id:receipt.into(),reset_receipt_sha256:hash,count:charges.len() as u64,max_at_ms,batch_max_at_ms,batch_sha256,slot_count:slots.len() as u64,slot_batch_sha256};
    let body=serde_json::to_string(&coverage).map_err(|_|"S05 quota unavailable")?;budget.check(body.len()).map_err(|_|"S05 quota unavailable")?;
    let entries=[super::request_purge::RequestHash{request_sha256:request.clone(),body_sha256:Some(crate::repository::sha256_hex(body.as_bytes()))}];
    tx.execute("INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES(?1,?2,NULL,?1,'s05 guarded reset quota terminal',?3)",params![owner,context,chrono::Utc::now().to_rfc3339()]).map_err(|_|"S05 quota unavailable")?;
    super::request_purge::write_timed(tx,super::request_purge::Family::S05Reset,&account_hash,&meta,&meta,&context,&entries[0],max_at_ms)?;
    tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![meta,body]).map_err(|_|"S05 quota unavailable")?;
    for batch in &batches {
        let raw=serde_json::to_string(batch).map_err(|_|"S05 quota unavailable")?;let key=format!("s05_quota_batch_{}_{}_{:08}",window_hash,request,batch.ordinal);
        let batch_request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-reset-batch.v1",account,receipt,batch.ordinal)).map_err(|_|"S05 quota unavailable")?);
        let entry=super::request_purge::RequestHash{request_sha256:batch_request,body_sha256:Some(crate::repository::sha256_hex(raw.as_bytes()))};
        super::request_purge::write_timed(tx,super::request_purge::Family::S05Reset,&account_hash,&meta,&key,&context,&entry,batch.max_at_ms)?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![key,raw]).map_err(|_|"S05 quota unavailable")?;
    }
    for batch in &slot_batches {
        let raw=serde_json::to_string(batch).map_err(|_|"S05 quota unavailable")?;let key=format!("s05_quota_batch_{}_{}_slot_{:08}",window_hash,request,batch.ordinal);
        let batch_request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-reset-slot-batch.v1",account,receipt,batch.ordinal)).map_err(|_|"S05 quota unavailable")?);
        let entry=super::request_purge::RequestHash{request_sha256:batch_request,body_sha256:Some(crate::repository::sha256_hex(raw.as_bytes()))};
        super::request_purge::write_timed(tx,super::request_purge::Family::S05Reset,&account_hash,&meta,&key,&context,&entry,0)?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![key,raw]).map_err(|_|"S05 quota unavailable")?;
    }
    if tx.execute("DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND reason='s05 guarded reset quota terminal'",params![owner,context]).map_err(|_|"S05 quota unavailable")?!=1{return Err("S05 quota unavailable".into())}
    let slot_account=super::request_purge::account_hash(super::request_purge::Family::S05Slot,&database,account)?;
    for (terminal,_,_) in &terminals {
        let raw=serde_json::to_string(terminal).map_err(|_|"S05 quota unavailable")?;let slot_request=slot_pointer(account,&terminal.slot)?;
        let slot_meta=format!("s05_quota_slot_terminal_{slot_account}_{slot_request}");let slot_context=format!("s05-slot-purge:{slot_meta}");
        let entry=super::request_purge::RequestHash{request_sha256:slot_request,body_sha256:Some(crate::repository::sha256_hex(raw.as_bytes()))};
        tx.execute("INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES(?1,?2,NULL,?1,'s05 guarded reset quota terminal',?3)",params![owner,slot_context,chrono::Utc::now().to_rfc3339()]).map_err(|_|"S05 quota unavailable")?;
        super::request_purge::write(tx,super::request_purge::Family::S05Slot,&slot_account,&slot_meta,&slot_context,&[entry])?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![slot_meta,raw]).map_err(|_|"S05 quota unavailable")?;
        if tx.execute("DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND reason='s05 guarded reset quota terminal'",params![owner,slot_context]).map_err(|_|"S05 quota unavailable")?!=1{return Err("S05 quota unavailable".into())}
    }
    verify_written_coverage(tx,owner,account,&coverage,&meta,&request,&account_hash,&window_hash,&batches,&slot_batches,&terminals,view,budget)?;
    Ok(())
}

// Draft: independently verify both halves of each retained quota/reset fact.
fn read_reset_charges(tx:&Transaction<'_>,owner:&str,account:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<ResetCharge>,String>{
    if view.current_owner()!=owner || view.account_id()!=account {return Err("S05 quota unavailable".into())}
    let broken:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox o LEFT JOIN fm_v2_change_sets c ON c.owner_id=o.owner_id AND c.change_set_id=o.change_set_id AND c.reason='s05.quota-reset.v1' WHERE o.owner_id=?1 AND o.kind='s05_quota_reset' AND c.change_set_id IS NULL) OR EXISTS(SELECT 1 FROM fm_v2_change_sets c LEFT JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.kind='s05_quota_reset' WHERE c.owner_id=?1 AND c.reason='s05.quota-reset.v1' AND o.event_id IS NULL)",[owner],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    if broken {return Err("S05 quota unavailable".into());}
    let mut stmt=tx.prepare("SELECT o.event_id,o.change_set_id,length(CAST(o.payload_json AS BLOB)),o.source_revision,c.content_hash,c.actor_type,c.actor_id,c.contract_version,c.source_event_id FROM fm_v2_outbox o JOIN fm_v2_change_sets c ON c.owner_id=o.owner_id AND c.change_set_id=o.change_set_id AND c.reason='s05.quota-reset.v1' WHERE o.owner_id=?1 AND o.kind='s05_quota_reset' ORDER BY o.event_id LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
    let mut rows=stmt.query([owner]).map_err(|_|"S05 quota unavailable")?;let mut facts=Vec::new();let mut seen=std::collections::BTreeSet::new();
    while let Some(row)=rows.next().map_err(|_|"S05 quota unavailable")? {
        if facts.len()>=100000 {return Err("S05 quota unavailable".into())}
        let event:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let change:String=row.get(1).map_err(|_|"S05 quota unavailable")?;let size:i64=row.get(2).map_err(|_|"S05 quota unavailable")?;
        if !(0..=8192).contains(&size) {return Err("S05 quota unavailable".into())}budget.check(event.len()+change.len()+size as usize).map_err(|_|"S05 quota unavailable")?;
        let raw:String=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3",params![owner,event,change],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
        let fact:ResetCharge=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
        let expected_event=reset_event(&fact)?;let expected_change=format!("cs_s05_quota_reset_{}",expected_event.strip_prefix("outbox_s05_quota_reset_").ok_or("S05 quota unavailable")?);
        let hash:String=row.get(4).map_err(|_|"S05 quota unavailable")?;let actor:String=row.get(5).map_err(|_|"S05 quota unavailable")?;let actor_id:String=row.get(6).map_err(|_|"S05 quota unavailable")?;let contract:String=row.get(7).map_err(|_|"S05 quota unavailable")?;let source:Option<String>=row.get(8).map_err(|_|"S05 quota unavailable")?;
        let metadata:Option<String>=tx.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND length(CAST(operation_metadata_json AS BLOB))<=8192",params![owner,change],|r|r.get(0)).optional().map_err(|_|"S05 quota unavailable")?.flatten();
        if fact.charge.account_id!=account || event!=expected_event || change!=expected_change || row.get::<_,i64>(3).map_err(|_|"S05 quota unavailable")?!=0 || hash!=crate::repository::sha256_hex(raw.as_bytes()) || metadata.as_deref()!=Some(raw.as_str()) || serde_json::to_string(&fact).map_err(|_|"S05 quota unavailable")?!=raw || actor!="automation" || actor_id!="s05_quota" || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || !seen.insert(crate::repository::s05_budget::charge_key(&fact.charge).map_err(|_|"S05 quota unavailable")?) {return Err("S05 quota unavailable".into())}
        verify_reset_receipt(tx,owner,&fact,view,budget)?;facts.push(fact);
    }Ok(facts)
}

// Frozen account/custody proof for ledger accounting; no source-currentness grant.
fn capture_host_ledger_binding(tx:&Transaction<'_>,owner:&str,account:&str,job:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(String,String),String>{
    let (input,source_account,workspace,epoch,durable,fingerprint):(String,String,String,i64,String,String)=tx.query_row("SELECT j.input_hash,r.source_account_id,r.target_workspace_id,r.owner_epoch,r.durable_epoch,r.binding_fingerprint FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review') AND j.project_key='' AND j.workspace_key=r.target_workspace_id AND length(CAST(r.source_account_id AS BLOB))<=512 AND length(CAST(r.target_workspace_id AS BLOB))<=512 AND length(CAST(r.durable_epoch AS BLOB))<=20",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(|_|"S05 quota unavailable")?;
    let hash_ok=|v:&str|v.len()==64 && v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b));
    if view.current_owner()!=owner || view.account_id()!=account || source_account!=account || workspace.is_empty() || workspace.trim()!=workspace || epoch<0 || durable.is_empty() || !durable.bytes().all(|b|b.is_ascii_digit()) || (durable.len()>1 && durable.starts_with('0')) || !hash_ok(&input) || !hash_ok(&fingerprint) {return Err("S05 quota unavailable".into())}
    budget.check(workspace.len()+durable.len()+128).map_err(|_|"S05 quota unavailable")?;let mut captured=Vec::new();
    for candidate in view.owners() {
        let binding=format!("{{\"durable_epoch\":{},\"memory_owner\":{},\"memory_workspace\":{},\"owner_epoch\":{},\"source_account_id\":{}}}",py_string(&durable)?,py_string(candidate)?,py_string(&workspace)?,epoch,py_string(account)?);
        if crate::repository::sha256_hex(binding.as_bytes())==fingerprint {captured.push(candidate.clone());}
    }
    if captured.len()!=1{return Err("S05 quota unavailable".into())}let captured=captured.pop().unwrap();view.for_captured(tx,&captured).map_err(|_|"S05 quota unavailable")?;Ok((captured,input))
}
fn capture_s08_ledger_binding(tx:&Transaction<'_>,owner:&str,account:&str,job:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(String,String),String>{
    if view.current_owner()!=owner || view.account_id()!=account {return Err("S05 quota unavailable".into())}
    let (input,workspace):(String,String)=tx.query_row("SELECT input_hash,workspace_key FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind='access_consolidation' AND project_key='' AND length(CAST(workspace_key AS BLOB)) BETWEEN 1 AND 512",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_|"S05 quota unavailable")?;
    let mut found=Vec::new();
    for captured in view.owners() {
        let id=format!("cs_s08selection_{}",crate::repository::sha256_hex(&serde_json::to_vec(&(captured,job)).map_err(|_|"S05 quota unavailable")?));
        let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s08.selection.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|"S05 quota unavailable")?;
        let Some((hash,raw,actor,actor_id,contract,source))=row else {
            let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,format!("outbox_{id}_s08_selection")],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
            if collision {return Err("S05 quota unavailable".into());}continue
        };budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let selection:crate::repository::S08RulesSelection=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
        let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s08_selection' AND length(CAST(payload_json AS BLOB))<=262144",params![owner,format!("outbox_{id}_s08_selection"),id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|"S05 quota unavailable")?;
        if selection.version!=1 || selection.owner!=*captured || selection.source_account_id!=account || selection.workspace_id!=workspace || selection.project_id.is_some() || serde_json::to_string(&selection).map_err(|_|"S05 quota unavailable")?!=raw || hash!=input || hash!=crate::repository::sha256_hex(raw.as_bytes()) || actor!="automation" || actor_id!="s08_rules" || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || out.as_ref().is_none_or(|(body,revision)|*revision!=1 || crate::field_authority::parse_unique_json(body).ok()!=crate::field_authority::parse_unique_json(&raw).ok()) {return Err("S05 quota unavailable".into())}
        view.for_captured(tx,captured).map_err(|_|"S05 quota unavailable")?;found.push(captured.clone());
    }
    if found.len()!=1{return Err("S05 quota unavailable".into())}Ok((found.pop().unwrap(),input))
}

#[derive(Clone,serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct QuotaBatchEntry {key:String,fact_sha256:String}
#[derive(Clone,serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct QuotaBatch {version:u32,window_account_sha256:String,coverage_receipt_id:String,ordinal:u64,max_at_ms:i64,entries:Vec<QuotaBatchEntry>}
fn make_reset_batches(charges:&[crate::repository::s05_budget::RetainedCharge],receipt:&str,meta:&str,window_hash:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<QuotaBatch>,String>{
    let mut entries=std::collections::BTreeMap::new();
    for charge in charges {
        let key=crate::repository::s05_budget::charge_key(charge).map_err(|_|"S05 quota unavailable")?;
        let fact=ResetCharge{version:1,charge:charge.clone(),reset_receipt_id:receipt.into()};let raw=serde_json::to_vec(&fact).map_err(|_|"S05 quota unavailable")?;
        budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let at=charge.settled_at_ms.unwrap_or(charge.reserved_at_ms);
        if at<0 || entries.insert(key,(crate::repository::sha256_hex(&raw),at)).is_some(){return Err("S05 quota unavailable".into())}
    }
    let entries:Vec<_>=entries.into_iter().collect();let mut batches=Vec::new();
    for (ordinal,group) in entries.chunks(64).enumerate() {
        let max_at_ms=group.iter().map(|(_,(_,at))|*at).max().ok_or("S05 quota unavailable")?;
        let entries=group.iter().map(|(key,(hash,_))|QuotaBatchEntry{key:key.clone(),fact_sha256:hash.clone()}).collect();
        batches.push(QuotaBatch{version:1,window_account_sha256:window_hash.into(),coverage_receipt_id:meta.into(),ordinal:ordinal as u64,max_at_ms,entries});
    }Ok(batches)
}

// Draft: index and receipt discovery verifies complete, account-scoped reset coverage.
fn verify_coverage_s06(tx:&Transaction<'_>,owner:&str,account:&str,coverage:&ResetCoverage,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    let (hash,raw,actor,actor_id,contract,source):(String,String,String,String,String,Option<String>)=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",params![owner,coverage.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(|_|"S05 quota unavailable")?;
    budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
    let v=crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?;
    let captured=v["request"]["owner"].as_str().ok_or("S05 quota unavailable")?;view.for_captured(tx,captured).map_err(|_|"S05 quota unavailable")?;
    let operation=v["request"]["operation_id"].as_str().ok_or("S05 quota unavailable")?;
    if coverage.reset_receipt_id!=crate::repository::ingest_erased_receipt_id(account,operation).map_err(|_|"S05 quota unavailable")? || hash!=coverage.reset_receipt_sha256 || hash!=crate::repository::sha256_hex(raw.as_bytes()) || actor!="user" || actor_id!=captured || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || v.as_object().map(|x|x.len())!=Some(4) || v["version"]!="s06.guarded-reset.v1" || v["request_sha256"]!=crate::repository::sha256_hex(&serde_json::to_vec(&v["request"]).map_err(|_|"S05 quota unavailable")?) || v["request"]["account_id"]!=account || v["result"]["complete"]!=true || !v["request"]["components"].as_array().is_some_and(|a|a.iter().any(|x|x=="ingest"||x=="memories")){return Err("S05 quota unavailable".into())}
    let paired:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![owner,format!("outbox_{}_s06_guarded_reset",coverage.reset_receipt_id),coverage.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|"S05 quota unavailable")?;
    if paired!=Some((raw,0)){return Err("S05 quota unavailable".into())}Ok(())
}

/// Content-free lifecycle disposition; it is never authority to issue work.
#[derive(Clone,Debug,serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SlotTerminal {
    pub version:u32,pub account_id:String,pub captured_owner:String,pub workspace_id:String,pub slot:String,
    pub original_receipt_sha256:String,pub result_receipt_sha256:Option<String>,pub attempt_sha256:Option<String>,pub lease_epoch:Option<i64>,
    pub disposition:String,pub coverage_receipt_id:String,pub slot_batch_ordinal:u64,
}
fn slot_pointer(account:&str,slot:&str)->Result<String,String>{Ok(crate::repository::sha256_hex(&serde_json::to_vec(&("s05.reserve-terminal.v1",account,slot)).map_err(|_|"S05 quota unavailable")?))}
pub(super) fn read_slot_terminal(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,pre_attempt_count:u32,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Option<SlotTerminal>,String>{
    if view.current_owner()!=owner || view.account_id()!=account {return Err("S05 quota unavailable".into())}
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_purge.v1'",[],|r|r.get(0)).optional().map_err(|_|"S05 quota unavailable")?;
    if marker.as_deref()!=Some("1"){return Err("S05 quota unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let slot=crate::repository::s05_budget::reserve_slot_for(account,job,pre_attempt_count).map_err(|_|"S05 quota unavailable")?;
    let request=slot_pointer(account,&slot)?;let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Slot,&database,account)?;
    let meta=format!("s05_quota_slot_terminal_{account_hash}_{request}");
    let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_reserve_terminal_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota unavailable")?;
    let Some((body_hash,receipt_id,context,at))=pointer else {
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key=?1)",[&meta],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
        if orphan{return Err("S05 quota unavailable".into())}return Ok(None)
    };
    if receipt_id!=meta || context!=format!("s05-slot-purge:{meta}") || at.is_some(){return Err("S05 quota unavailable".into())}
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=8192",[&meta],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
    let terminal:SlotTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
    let valid_hash=|v:&str|v.len()==64 && v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
    if body_hash.as_deref()!=Some(crate::repository::sha256_hex(raw.as_bytes()).as_str()) || serde_json::to_string(&terminal).map_err(|_|"S05 quota unavailable")?!=raw || terminal.version!=1 || terminal.account_id!=account || terminal.workspace_id!=workspace || terminal.slot!=slot || !valid_hash(&terminal.original_receipt_sha256) {return Err("S05 quota unavailable".into())}
    match terminal.disposition.as_str() {
        "settled"|"revoked" if terminal.result_receipt_sha256.as_deref().is_some_and(valid_hash) && terminal.attempt_sha256.as_deref().is_some_and(valid_hash) && terminal.lease_epoch.is_some_and(|v|v>0)=>{},
        "cancelled_before_reserve" if terminal.result_receipt_sha256.is_none() && terminal.attempt_sha256.is_none() && terminal.lease_epoch.is_none()=>{},
        _=>return Err("S05 quota unavailable".into()),
    }
    let schema_hash=purge_schema_hash(tx,budget)?;
    if terminal.coverage_receipt_id.starts_with("s05_quota_purge_receipt_") {
        verify_purge_slot(tx,account,&terminal,&raw,&schema_hash,budget)?;return Ok(Some(terminal))
    }
    let header_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&terminal.coverage_receipt_id],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(header_raw.len()).map_err(|_|"S05 quota unavailable")?;
    let coverage:ResetCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&header_raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
    let reset_hash=super::request_purge::account_hash(super::request_purge::Family::S05Reset,&database,account)?;let reset_request=reset_pointer(account,&coverage.reset_receipt_id)?;
    let expected_header=format!("s05_quota_reset_receipt_{reset_hash}_{reset_request}");
    let header_pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_reset_v1' AND account_sha256=?1 AND request_sha256=?2",params![reset_hash,reset_request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota unavailable")?;
    let expected_window=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota unavailable")?;
    let header_sha=crate::repository::sha256_hex(header_raw.as_bytes());
    if coverage.version!=1 || coverage.database_id!=database || coverage.account_id!=account || coverage.window_account_sha256!=expected_window || terminal.coverage_receipt_id!=expected_header || serde_json::to_string(&coverage).map_err(|_|"S05 quota unavailable")?!=header_raw || header_pointer!=Some((Some(header_sha.clone()),expected_header.clone(),format!("s05-reset-purge:{expected_header}"),Some(coverage.max_at_ms))) || coverage.slot_count>100000 || coverage.slot_batch_sha256.len() as u64!=(coverage.slot_count+63)/64 || terminal.slot_batch_ordinal>=coverage.slot_batch_sha256.len() as u64 {return Err("S05 quota unavailable".into())}
    if !verify_purge_transfer(tx,account,&expected_header,&header_sha,&schema_hash,budget)? {
        view.for_captured(tx,&terminal.captured_owner).map_err(|_|"S05 quota unavailable")?;
        verify_coverage_s06(tx,owner,account,&coverage,view,budget)?;
    }
    let batch_key=format!("s05_quota_batch_{}_{}_slot_{:08}",expected_window,reset_request,terminal.slot_batch_ordinal);
    let batch_request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-reset-slot-batch.v1",account,&coverage.reset_receipt_id,terminal.slot_batch_ordinal)).map_err(|_|"S05 quota unavailable")?);
    let batch_pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_reset_v1' AND account_sha256=?1 AND request_sha256=?2",params![reset_hash,batch_request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota unavailable")?;
    let batch_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&batch_key],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(batch_raw.len()).map_err(|_|"S05 quota unavailable")?;
    let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&batch_raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
    let batch_sha=crate::repository::sha256_hex(batch_raw.as_bytes());let expected_len=if terminal.slot_batch_ordinal+1==coverage.slot_batch_sha256.len() as u64 {((coverage.slot_count.checked_sub(1).ok_or("S05 quota unavailable")?%64)+1) as usize}else{64};
    if batch_pointer!=Some((Some(batch_sha.clone()),batch_key,format!("s05-reset-purge:{expected_header}"),Some(0))) || batch_sha!=coverage.slot_batch_sha256[terminal.slot_batch_ordinal as usize] || serde_json::to_string(&batch).map_err(|_|"S05 quota unavailable")?!=batch_raw || batch.version!=1 || batch.window_account_sha256!=expected_window || batch.coverage_receipt_id!=expected_header || batch.ordinal!=terminal.slot_batch_ordinal || batch.max_at_ms!=0 || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key) || batch.entries.iter().any(|v|!valid_hash(&v.fact_sha256)) || batch.entries.iter().filter(|v|v.key==slot && v.fact_sha256==crate::repository::sha256_hex(raw.as_bytes())).count()!=1 {return Err("S05 quota unavailable".into())}
    Ok(Some(terminal))
}


/// Verify the retained paired erased sentinel, never granting source mutation.
pub(super) fn paired_erased_fact_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<serde_json::Value,String> {
    let (source_key,pair_sha256,input_sha256)=crate::repository::paired_pending::verified_pair_snapshot_in(tx,owner,account,workspace,job,view,true,budget).map_err(|_|"paired erased source unavailable")?;
    let receipt=crate::repository::quota_ingest_erased_receipt_in(tx,owner,account,job,view,budget).map_err(|_|"paired erased source unavailable")?.ok_or("paired erased source unavailable")?;
    let terminal=read_slot_terminal(tx,owner,account,workspace,job,0,view,budget)?.ok_or("paired erased source unavailable")?;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"paired erased source unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Reset,&database,account)?;
    let expected=format!("s05_quota_reset_receipt_{}_{}",account_hash,reset_pointer(account,&receipt)?);
    let (captured,input)=capture_host_original(tx,owner,account,job,view,budget)?;
    if terminal.disposition!="cancelled_before_reserve" || terminal.result_receipt_sha256.is_some() || terminal.attempt_sha256.is_some() || terminal.lease_epoch.is_some() || terminal.coverage_receipt_id!=expected || terminal.captured_owner!=captured || input!=input_sha256 {return Err("paired erased source unavailable".into())}
    Ok(serde_json::json!({"version":1,"state":"erased","source_key":source_key,"pair_sha256":pair_sha256,"input_sha256":input_sha256,"original_receipt_sha256":terminal.original_receipt_sha256}))
}

// Caller has verified every original/result slot against its live ledger row.
fn make_slot_batches(slots:&[PendingSlot],account:&str,meta:&str,window_hash:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<(Vec<(SlotTerminal,String,u32)>,Vec<QuotaBatch>),String>{
    let mut ordered:Vec<&PendingSlot>=slots.iter().collect();ordered.sort_by(|a,b|a.slot.cmp(&b.slot));
    if ordered.windows(2).any(|v|v[0].slot>=v[1].slot){return Err("S05 quota unavailable".into())}
    let mut terminals=Vec::new();let mut batches=Vec::new();
    for (ordinal,group) in ordered.chunks(64).enumerate() {
        let mut entries=Vec::new();
        for pending in group {
            let terminal=SlotTerminal{version:1,account_id:account.into(),captured_owner:pending.captured_owner.clone(),workspace_id:pending.workspace_id.clone(),slot:pending.slot.clone(),original_receipt_sha256:pending.original_receipt_sha256.clone(),result_receipt_sha256:pending.result_receipt_sha256.clone(),attempt_sha256:pending.attempt_sha256.clone(),lease_epoch:pending.lease_epoch,disposition:pending.disposition.clone(),coverage_receipt_id:meta.into(),slot_batch_ordinal:ordinal as u64};
            let raw=serde_json::to_vec(&terminal).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
            entries.push(QuotaBatchEntry{key:terminal.slot.clone(),fact_sha256:crate::repository::sha256_hex(&raw)});
            terminals.push((terminal,pending.job_id.clone(),pending.pre_attempt_count));
        }
        batches.push(QuotaBatch{version:1,window_account_sha256:window_hash.into(),coverage_receipt_id:meta.into(),ordinal:ordinal as u64,max_at_ms:0,entries});
    }Ok((terminals,batches))
}

// Draft: pointer-first reset-window proof; no table/history scan.
pub(super) fn read_reset_window(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<crate::repository::s05_budget::RetainedCharge>,String>{
    if view.current_owner()!=owner || view.account_id()!=account{return Err("S05 quota unavailable".into())}
    let start=now_ms.checked_sub(86_400_000).ok_or("S05 quota unavailable")?;
    if start<0{return Err("S05 quota unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Reset,&database,account)?;
    let window_hash=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota unavailable")?;
    let schema_hash=purge_schema_hash(tx,budget)?;
    let mut stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='s05_s08_quota_reset_v1' AND account_sha256=?1 AND effective_at_ms>?2 ORDER BY effective_at_ms,request_sha256 LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
    let mut rows=stmt.query(params![account_hash,start]).map_err(|_|"S05 quota unavailable")?;
    let mut pointers=std::collections::BTreeMap::<String,(String,String,String,i64)>::new();let mut headers=Vec::new();
    while let Some(row)=rows.next().map_err(|_|"S05 quota unavailable")? {
        if pointers.len()>=100000{return Err("S05 quota unavailable".into())}
        let request:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"S05 quota unavailable")?;let key:String=row.get(2).map_err(|_|"S05 quota unavailable")?;let context:String=row.get(3).map_err(|_|"S05 quota unavailable")?;let at:i64=row.get(4).map_err(|_|"S05 quota unavailable")?;
        budget.check(request.len()+key.len()+context.len()+64).map_err(|_|"S05 quota unavailable")?;
        let hash=hash.ok_or("S05 quota unavailable")?;
        if at>now_ms || hash.len()!=64 || !hash.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || request.len()!=64 || !request.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || (!key.starts_with("s05_quota_reset_receipt_") && !key.starts_with("s05_quota_batch_")) {return Err("S05 quota unavailable".into())}
        if key.starts_with("s05_quota_reset_receipt_"){headers.push(key.clone());}
        if pointers.insert(key,(request,hash,context,at)).is_some(){return Err("S05 quota unavailable".into())}
    }
    drop(rows);drop(stmt);let mut consumed=std::collections::BTreeSet::new();let mut seen=std::collections::BTreeSet::new();let mut output=Vec::new();
    for header in headers {
        let (request,body_hash,context,at)=pointers.get(&header).ok_or("S05 quota unavailable")?.clone();consumed.insert(header.clone());
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&header],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let coverage:ResetCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
        if coverage.version!=1 || coverage.database_id!=database || coverage.account_id!=account || coverage.window_account_sha256!=window_hash || header!=format!("s05_quota_reset_receipt_{account_hash}_{request}") || context!=format!("s05-reset-purge:{header}") || request!=reset_pointer(account,&coverage.reset_receipt_id)? || body_hash!=crate::repository::sha256_hex(raw.as_bytes()) || serde_json::to_string(&coverage).map_err(|_|"S05 quota unavailable")?!=raw || coverage.count>100000 || coverage.batch_sha256.len() as u64!=(coverage.count+63)/64 || coverage.batch_max_at_ms.len()!=coverage.batch_sha256.len() || coverage.batch_max_at_ms.iter().any(|v|*v<0) || coverage.max_at_ms!=coverage.batch_max_at_ms.iter().copied().max().unwrap_or(0) || at!=coverage.max_at_ms {return Err("S05 quota unavailable".into())}
        let transferred=verify_purge_transfer(tx,account,&header,&body_hash,&schema_hash,budget)?;
        if !transferred {verify_coverage_s06(tx,owner,account,&coverage,view,budget)?;}
        for (ordinal,max_at) in coverage.batch_max_at_ms.iter().enumerate() {
            if *max_at<=start {continue}
            let batch_key=format!("s05_quota_batch_{}_{}_{:08}",window_hash,request,ordinal);
            let (batch_request,batch_hash,batch_context,batch_at)=pointers.get(&batch_key).ok_or("S05 quota unavailable")?.clone();consumed.insert(batch_key.clone());
            let expected_request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-reset-batch.v1",account,&coverage.reset_receipt_id,ordinal as u64)).map_err(|_|"S05 quota unavailable")?);
            if batch_request!=expected_request || batch_context!=context || batch_at!=*max_at {return Err("S05 quota unavailable".into())}
            let batch_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&batch_key],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(batch_raw.len()).map_err(|_|"S05 quota unavailable")?;
            let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&batch_raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
            let expected_len=if ordinal+1==coverage.batch_sha256.len(){((coverage.count.checked_sub(1).ok_or("S05 quota unavailable")?%64)+1) as usize}else{64};
            if batch_hash!=crate::repository::sha256_hex(batch_raw.as_bytes()) || batch_hash!=coverage.batch_sha256[ordinal] || serde_json::to_string(&batch).map_err(|_|"S05 quota unavailable")?!=batch_raw || batch.version!=1 || batch.window_account_sha256!=window_hash || batch.coverage_receipt_id!=header || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=*max_at || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota unavailable".into())}
            if transferred {continue}
            let mut actual_max=0i64;
            for entry in batch.entries {
                if entry.key.len()!=64 || !entry.key.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || entry.fact_sha256.len()!=64 || !entry.fact_sha256.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)){return Err("S05 quota unavailable".into())}
                let event=format!("outbox_s05_quota_reset_{}",crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-reset.v1",account,&coverage.reset_receipt_id,&entry.key)).map_err(|_|"S05 quota unavailable")?));let change=format!("cs_s05_quota_reset_{}",event.strip_prefix("outbox_s05_quota_reset_").ok_or("S05 quota unavailable")?);
                let row:Option<(String,String,String,String,String,Option<String>,i64)>=tx.query_row("SELECT o.payload_json,c.content_hash,c.operation_metadata_json,c.actor_type,c.actor_id,c.source_event_id,o.source_revision FROM fm_v2_outbox o JOIN fm_v2_change_sets c ON c.owner_id=o.owner_id AND c.change_set_id=o.change_set_id WHERE o.owner_id=?1 AND o.event_id=?2 AND o.change_set_id=?3 AND o.kind='s05_quota_reset' AND c.reason='s05.quota-reset.v1' AND c.contract_version=?4 AND length(CAST(o.payload_json AS BLOB))<=8192 AND length(CAST(c.operation_metadata_json AS BLOB))<=8192",params![owner,event,change,crate::ontology::CONTRACT_VERSION],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional().map_err(|_|"S05 quota unavailable")?;
                let (fact_raw,hash,metadata,actor,actor_id,source,revision)=row.ok_or("S05 quota unavailable")?;budget.check(fact_raw.len()+metadata.len()).map_err(|_|"S05 quota unavailable")?;
                let fact:ResetCharge=serde_json::from_value(crate::field_authority::parse_unique_json(&fact_raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
                if metadata!=fact_raw || hash!=entry.fact_sha256 || hash!=crate::repository::sha256_hex(fact_raw.as_bytes()) || serde_json::to_string(&fact).map_err(|_|"S05 quota unavailable")?!=fact_raw || actor!="automation" || actor_id!="s05_quota" || source.is_some() || revision!=0 || fact.reset_receipt_id!=coverage.reset_receipt_id || fact.charge.account_id!=account || crate::repository::s05_budget::charge_key(&fact.charge).map_err(|_|"S05 quota unavailable")?!=entry.key || !seen.insert(entry.key){return Err("S05 quota unavailable".into())}
                verify_reset_receipt(tx,owner,&fact,view,budget)?;let effective=fact.charge.settled_at_ms.unwrap_or(fact.charge.reserved_at_ms);if effective>now_ms {return Err("S05 quota unavailable".into())}actual_max=actual_max.max(effective);if effective>start{output.push(fact.charge);}
            }
            if actual_max!=*max_at{return Err("S05 quota unavailable".into())}
        }
    }
    if consumed.len()!=pointers.len(){return Err("S05 quota unavailable".into())}Ok(output)
}

// Verify one header, each fixed-size batch and each slot pointer once.
fn verify_written_coverage(tx:&Transaction<'_>,owner:&str,account:&str,coverage:&ResetCoverage,meta:&str,request:&str,account_hash:&str,window_hash:&str,batches:&[QuotaBatch],slot_batches:&[QuotaBatch],terminals:&[(SlotTerminal,String,u32)],view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    let body=serde_json::to_string(coverage).map_err(|_|"S05 quota unavailable")?;budget.check(body.len()).map_err(|_|"S05 quota unavailable")?;
    let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_reset_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota unavailable")?;
    let stored:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[meta],|r|r.get(0)).optional().map_err(|_|"S05 quota unavailable")?;
    if pointer!=Some((Some(crate::repository::sha256_hex(body.as_bytes())),meta.into(),format!("s05-reset-purge:{meta}"),Some(coverage.max_at_ms))) || stored.as_deref()!=Some(body.as_str()) || coverage.count!=batches.iter().map(|v|v.entries.len() as u64).sum::<u64>() || coverage.slot_count!=terminals.len() as u64 || coverage.slot_count!=slot_batches.iter().map(|v|v.entries.len() as u64).sum::<u64>() || coverage.batch_sha256.len()!=batches.len() || coverage.batch_max_at_ms.len()!=batches.len() || coverage.max_at_ms!=coverage.batch_max_at_ms.iter().copied().max().unwrap_or(0) || coverage.slot_batch_sha256.len()!=slot_batches.len() {return Err("S05 quota unavailable".into())}
    verify_coverage_s06(tx,owner,account,coverage,view,budget)?;
    for (is_slot,items) in [(false,batches),(true,slot_batches)] {for (position,batch) in items.iter().enumerate() {
        let raw=serde_json::to_string(batch).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;let hash=crate::repository::sha256_hex(raw.as_bytes());
        let ordinal=usize::try_from(batch.ordinal).map_err(|_|"S05 quota unavailable")?;
        let expected_hash=if is_slot {coverage.slot_batch_sha256.get(ordinal)} else {coverage.batch_sha256.get(ordinal)}.ok_or("S05 quota unavailable")?;
        let suffix=if is_slot {format!("slot_{:08}",batch.ordinal)} else {format!("{:08}",batch.ordinal)};
        let key=format!("s05_quota_batch_{}_{}_{}",window_hash,request,suffix);
        let domain=if is_slot {"s05.quota-reset-slot-batch.v1"} else {"s05.quota-reset-batch.v1"};
        let batch_request=crate::repository::sha256_hex(&serde_json::to_vec(&(domain,account,&coverage.reset_receipt_id,batch.ordinal)).map_err(|_|"S05 quota unavailable")?);
        let p:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_reset_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,batch_request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota unavailable")?;
        let stored:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&key],|r|r.get(0)).optional().map_err(|_|"S05 quota unavailable")?;
        if batch.ordinal!=position as u64 || batch.version!=1 || batch.window_account_sha256!=window_hash || batch.coverage_receipt_id!=meta || batch.entries.is_empty() || batch.entries.len()>64 || batch.entries.windows(2).any(|v|v[0].key>=v[1].key) || (is_slot && batch.max_at_ms!=0) || (!is_slot && coverage.batch_max_at_ms.get(ordinal)!=Some(&batch.max_at_ms)) || expected_hash!=&hash || p!=Some((Some(hash),key,format!("s05-reset-purge:{meta}"),Some(batch.max_at_ms))) || stored.as_deref()!=Some(raw.as_str()) {return Err("S05 quota unavailable".into())}
    }}
    let database=&coverage.database_id;let slot_account=super::request_purge::account_hash(super::request_purge::Family::S05Slot,database,account)?;
    for (terminal,_,_) in terminals {
        view.for_captured(tx,&terminal.captured_owner).map_err(|_|"S05 quota unavailable")?;
        let raw=serde_json::to_string(terminal).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;let hash=crate::repository::sha256_hex(raw.as_bytes());
        let slot_request=slot_pointer(account,&terminal.slot)?;let key=format!("s05_quota_slot_terminal_{slot_account}_{slot_request}");
        let p:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_reserve_terminal_v1' AND account_sha256=?1 AND request_sha256=?2",params![slot_account,slot_request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota unavailable")?;
        let stored:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=8192",[&key],|r|r.get(0)).optional().map_err(|_|"S05 quota unavailable")?;
        let batch=slot_batches.get(terminal.slot_batch_ordinal as usize).ok_or("S05 quota unavailable")?;
        if terminal.account_id!=account || terminal.coverage_receipt_id!=meta || batch.entries.iter().filter(|v|v.key==terminal.slot && v.fact_sha256==hash).count()!=1 || p!=Some((Some(hash),key.clone(),format!("s05-slot-purge:{key}"),None)) || stored.as_deref()!=Some(raw.as_str()) {return Err("S05 quota unavailable".into())}
    }Ok(())
}

// The terminal pointer is already written. Remove only its exact preverified
// original/result pairs; no request or worker body survives reset.
pub(super) fn delete_slot_original_pairs(tx:&Transaction<'_>,owner:&str,slots:&[PendingSlot],budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    for slot in slots {
        let mut pairs=vec![(format!("cs_{}",slot.slot),"s05.reserve-original.v1","s05_reserve_original",&slot.original_receipt_sha256)];
        if let Some(hash)=slot.result_receipt_sha256.as_ref() {pairs.push((format!("cs_{}_reserved",slot.slot),"s05.reserve-result.v1","s05_reserve_result",hash));}
        for (id,reason,kind,hash) in pairs {
            let event=format!("outbox_{id}_{kind}");
            let row:Option<(String,String,String,String,Option<String>,String,String,i64)>=tx.query_row("SELECT c.content_hash,c.operation_metadata_json,c.actor_type,c.actor_id,c.source_event_id,c.contract_version,o.payload_json,o.source_revision FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id WHERE c.owner_id=?1 AND c.change_set_id=?2 AND c.reason=?3 AND o.event_id=?4 AND o.kind=?5 AND length(CAST(c.operation_metadata_json AS BLOB))<=8192 AND length(CAST(o.payload_json AS BLOB))<=8192",params![owner,id,reason,event,kind],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?))).optional().map_err(|_|"S05 quota unavailable")?;
            let (actual,raw,actor,actor_id,source,contract,outbox,revision)=row.ok_or("S05 quota unavailable")?;budget.check(raw.len()+outbox.len()).map_err(|_|"S05 quota unavailable")?;
            let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?2 AND (event_id<>?3 OR kind<>?4))",params![owner,id,event,kind],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
            let value=crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?;let paired=crate::field_authority::parse_unique_json(&outbox).map_err(|_|"S05 quota unavailable")?;
            if extra || actual!=*hash || actual!=crate::repository::sha256_hex(raw.as_bytes()) || actor!="automation" || actor_id!="s05_reservation" || source.as_deref()!=Some(slot.job_id.as_str()) || contract!=crate::ontology::CONTRACT_VERSION || revision!=0 || value!=paired{return Err("S05 quota unavailable".into())}
            if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,kind]).map_err(|_|"S05 quota unavailable")?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3 AND content_hash=?4",params![owner,id,reason,hash]).map_err(|_|"S05 quota unavailable")?!=1{return Err("S05 quota unavailable".into())}
        }
    }Ok(())
}

// Draft: lifecycle-only bounded complete account inventory. Quota admission
// uses the separate pointer-first rolling reader, not this full scan.
fn read_reset_all(tx:&Transaction<'_>,owner:&str,account:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(Vec<crate::repository::s05_budget::RetainedCharge>,Vec<SlotTerminal>,Vec<ResetCoverage>),String>{
    if view.current_owner()!=owner || view.account_id()!=account{return Err("S05 quota unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Reset,&database,account)?;
    let slot_account=super::request_purge::account_hash(super::request_purge::Family::S05Slot,&database,account)?;
    let window_hash=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota unavailable")?;
    let schema_hash=purge_schema_hash(tx,budget)?;
    let mut grouped=std::collections::BTreeMap::<String,Vec<crate::repository::s05_budget::RetainedCharge>>::new();
    for fact in read_reset_charges(tx,owner,account,view,budget)? {grouped.entry(fact.reset_receipt_id).or_default().push(fact.charge);}
    let mut pointer_stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_reset_v1' AND account_sha256=?1 ORDER BY request_sha256 LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
    let mut pointer_rows=pointer_stmt.query([&account_hash]).map_err(|_|"S05 quota unavailable")?;
    let mut pointers=std::collections::BTreeMap::<String,(String,String,String,i64)>::new();let mut headers=Vec::new();
    while let Some(row)=pointer_rows.next().map_err(|_|"S05 quota unavailable")? {
        if pointers.len()>=100000{return Err("S05 quota unavailable".into())}
        let request:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"S05 quota unavailable")?;let key:String=row.get(2).map_err(|_|"S05 quota unavailable")?;let context:String=row.get(3).map_err(|_|"S05 quota unavailable")?;let at:i64=row.get(4).map_err(|_|"S05 quota unavailable")?;
        budget.check(request.len()+key.len()+context.len()+64).map_err(|_|"S05 quota unavailable")?;
        let hash=hash.ok_or("S05 quota unavailable")?;
        if at<0 || hash.len()!=64 || !hash.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || request.len()!=64 || !request.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || (!key.starts_with("s05_quota_reset_receipt_") && !key.starts_with("s05_quota_batch_")){return Err("S05 quota unavailable".into())}
        if key.starts_with("s05_quota_reset_receipt_"){headers.push(key.clone());}
        if pointers.insert(key,(request,hash,context,at)).is_some(){return Err("S05 quota unavailable".into())}
    }
    drop(pointer_rows);drop(pointer_stmt);
    let mut slot_stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_reserve_terminal_v1' AND account_sha256=?1 ORDER BY request_sha256 LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
    let mut slot_rows=slot_stmt.query([&slot_account]).map_err(|_|"S05 quota unavailable")?;let mut slot_groups=std::collections::BTreeMap::<String,Vec<SlotTerminal>>::new();let mut direct_slots=Vec::new();let mut seen_slots=std::collections::BTreeSet::new();
    while let Some(row)=slot_rows.next().map_err(|_|"S05 quota unavailable")? {
        if seen_slots.len()>=100000{return Err("S05 quota unavailable".into())}
        let request:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"S05 quota unavailable")?;let key:String=row.get(2).map_err(|_|"S05 quota unavailable")?;let context:String=row.get(3).map_err(|_|"S05 quota unavailable")?;let at:Option<i64>=row.get(4).map_err(|_|"S05 quota unavailable")?;
        budget.check(request.len()+key.len()+context.len()+64).map_err(|_|"S05 quota unavailable")?;
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=8192",[&key],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let terminal:SlotTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
        let valid_hash=|v:&str|v.len()==64 && v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
        if terminal.version!=1 || terminal.account_id!=account || terminal.workspace_id.is_empty() || terminal.workspace_id.len()>512 || terminal.workspace_id.chars().any(char::is_control) || terminal.slot.len()!=75 || !terminal.slot.starts_with("s05reserve_") || !valid_hash(&terminal.slot[11..]) || !valid_hash(&terminal.original_receipt_sha256) || !seen_slots.insert(terminal.slot.clone()) || request!=slot_pointer(account,&terminal.slot)? || key!=format!("s05_quota_slot_terminal_{slot_account}_{request}") || context!=format!("s05-slot-purge:{key}") || at.is_some() || hash.as_deref()!=Some(crate::repository::sha256_hex(raw.as_bytes()).as_str()) || serde_json::to_string(&terminal).map_err(|_|"S05 quota unavailable")?!=raw {return Err("S05 quota unavailable".into())}
        match terminal.disposition.as_str() {"settled"|"revoked" if terminal.result_receipt_sha256.as_deref().is_some_and(valid_hash) && terminal.attempt_sha256.as_deref().is_some_and(valid_hash) && terminal.lease_epoch.is_some_and(|v|v>0)=>{},"cancelled_before_reserve" if terminal.result_receipt_sha256.is_none() && terminal.attempt_sha256.is_none() && terminal.lease_epoch.is_none()=>{},_=>return Err("S05 quota unavailable".into())}
        if terminal.coverage_receipt_id.starts_with("s05_quota_purge_receipt_") {verify_purge_slot(tx,account,&terminal,&raw,&schema_hash,budget)?;direct_slots.push(terminal)} else {slot_groups.entry(terminal.coverage_receipt_id.clone()).or_default().push(terminal)}
    }
    drop(slot_rows);drop(slot_stmt);let mut consumed=std::collections::BTreeSet::new();let mut charges_out=Vec::new();let mut slots_out=direct_slots;let mut coverage_out=Vec::new();
    for header in headers {
        let (request,body_hash,context,at)=pointers.get(&header).ok_or("S05 quota unavailable")?.clone();consumed.insert(header.clone());
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&header],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let coverage:ResetCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
        if coverage.version!=1 || coverage.database_id!=database || coverage.account_id!=account || coverage.window_account_sha256!=window_hash || header!=format!("s05_quota_reset_receipt_{account_hash}_{request}") || context!=format!("s05-reset-purge:{header}") || request!=reset_pointer(account,&coverage.reset_receipt_id)? || body_hash!=crate::repository::sha256_hex(raw.as_bytes()) || serde_json::to_string(&coverage).map_err(|_|"S05 quota unavailable")?!=raw || coverage.count>100000 || coverage.slot_count>100000 || coverage.batch_sha256.len() as u64!=(coverage.count+63)/64 || coverage.batch_max_at_ms.len()!=coverage.batch_sha256.len() || coverage.slot_batch_sha256.len() as u64!=(coverage.slot_count+63)/64 || coverage.max_at_ms!=coverage.batch_max_at_ms.iter().copied().max().unwrap_or(0) || at!=coverage.max_at_ms {return Err("S05 quota unavailable".into())}
        let transferred=verify_purge_transfer(tx,account,&header,&body_hash,&schema_hash,budget)?;
        if !transferred {verify_coverage_s06(tx,owner,account,&coverage,view,budget)?;}
        let charges=grouped.remove(&coverage.reset_receipt_id).unwrap_or_default();
        if (transferred && !charges.is_empty()) || (!transferred && coverage.count!=charges.len() as u64){return Err("S05 quota unavailable".into())}
        let batches=if transferred {
            let mut result=Vec::new();for ordinal in 0..coverage.batch_sha256.len(){
                let key=format!("s05_quota_batch_{}_{}_{:08}",window_hash,request,ordinal);
                let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&key],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
                let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
                let expected_len=if ordinal+1==coverage.batch_sha256.len(){((coverage.count.checked_sub(1).ok_or("S05 quota unavailable")?%64)+1) as usize}else{64};
                if serde_json::to_string(&batch).map_err(|_|"S05 quota unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=window_hash || batch.coverage_receipt_id!=header || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=coverage.batch_max_at_ms[ordinal] || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota unavailable".into())}
                result.push(batch);
            }result
        } else {make_reset_batches(&charges,&coverage.reset_receipt_id,&header,&window_hash,budget)?};
        let mut slots=slot_groups.remove(&header).unwrap_or_default();slots.sort_by(|a,b|a.slot.cmp(&b.slot));if coverage.slot_count!=slots.len() as u64{return Err("S05 quota unavailable".into())}
        if !transferred {for terminal in &slots {view.for_captured(tx,&terminal.captured_owner).map_err(|_|"S05 quota unavailable")?;}}
        let mut slot_batches=Vec::new();for (ordinal,group) in slots.chunks(64).enumerate() {let mut entries=Vec::new();for terminal in group {if terminal.slot_batch_ordinal!=ordinal as u64{return Err("S05 quota unavailable".into())}let raw=serde_json::to_vec(terminal).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;entries.push(QuotaBatchEntry{key:terminal.slot.clone(),fact_sha256:crate::repository::sha256_hex(&raw)});}slot_batches.push(QuotaBatch{version:1,window_account_sha256:window_hash.clone(),coverage_receipt_id:header.clone(),ordinal:ordinal as u64,max_at_ms:0,entries});}
        if batches.len()!=coverage.batch_sha256.len() || slot_batches.len()!=coverage.slot_batch_sha256.len(){return Err("S05 quota unavailable".into())}
        for (is_slot,items) in [(false,batches.as_slice()),(true,slot_batches.as_slice())] {for (ordinal,batch) in items.iter().enumerate() {
            let raw=serde_json::to_string(batch).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;let hash=crate::repository::sha256_hex(raw.as_bytes());
            let expected=if is_slot {&coverage.slot_batch_sha256[ordinal]} else {&coverage.batch_sha256[ordinal]};
            let suffix=if is_slot {format!("slot_{ordinal:08}")} else {format!("{ordinal:08}")};let key=format!("s05_quota_batch_{}_{}_{}",window_hash,request,suffix);
            let domain=if is_slot {"s05.quota-reset-slot-batch.v1"}else{"s05.quota-reset-batch.v1"};let request_hash=crate::repository::sha256_hex(&serde_json::to_vec(&(domain,account,&coverage.reset_receipt_id,ordinal as u64)).map_err(|_|"S05 quota unavailable")?);
            let pointer=pointers.get(&key).ok_or("S05 quota unavailable")?;let stored:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&key],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
            if hash!=*expected || pointer!=&(request_hash,hash,context.clone(),batch.max_at_ms) || stored!=raw || (!is_slot && coverage.batch_max_at_ms[ordinal]!=batch.max_at_ms){return Err("S05 quota unavailable".into())}consumed.insert(key);
        }}
        charges_out.extend(charges);slots_out.extend(slots);coverage_out.push(coverage);
    }
    if !grouped.is_empty() || !slot_groups.is_empty() || consumed.len()!=pointers.len(){return Err("S05 quota unavailable".into())}Ok((charges_out,slots_out,coverage_out))
}

// Lifecycle preflight only: bind the selected attempts and every verified prior
// reset fact to the same preview/commit snapshot. This does not grant admission.
pub(super) fn capture_reset_snapshot(tx:&Transaction<'_>,owner:&str,account:&str,jobs:&[String],now_ms:i64,include_m5:bool,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(LedgerCapture,String),String>{
    let cutoff=verified_cutoff_receipt(tx,owner,account,now_ms,budget)?;
    verify_owner_orphan_reservations(tx,owner,budget)?;
    let prior_purges=prior_purge_binding(tx,account,now_ms,budget)?;
    let (retained,slots,coverage)=read_reset_all(tx,owner,account,view,budget)?;
    let m5=if include_m5 {Some(verified_m5_lifecycle_inventory(tx,owner,account,now_ms,view,budget)?)} else {None};
    let mut selected=jobs.to_vec();selected.sort();
    if selected.len()>100000 || selected.windows(2).any(|w|w[0]==w[1]) {return Err("S05 quota unavailable".into())}
    let erased_receipts:std::collections::BTreeSet<String>=coverage.iter().map(|v|v.reset_receipt_id.clone()).collect();
    let capture=capture_ledger_attempts(tx,owner,account,&selected,cutoff,Some(&erased_receipts),true,view,budget)?;
    if coverage.iter().any(|v|v.max_at_ms>now_ms) || retained.iter().chain(&capture.charges).any(|v|v.reserved_at_ms>now_ms || v.settled_at_ms.is_some_and(|at|at>now_ms)){return Err("S05 quota unavailable".into())}
    let bytes=serde_json::to_vec(&("s05.quota-reset-snapshot.v1",account,cutoff,&selected,&prior_purges,&retained,&slots,&coverage,&m5,&capture.charges,&capture.slots)).map_err(|_|"S05 quota unavailable")?;
    budget.check(bytes.len()).map_err(|_|"S05 quota unavailable")?;
    Ok((capture,crate::repository::sha256_hex(&bytes)))
}

// Derive only the jobs the existing reset partition will actually erase.
// The partition itself must already have passed its normal custody validation.
pub(super) fn reset_jobs(tx:&Transaction<'_>,owner:&str,selection:Option<&crate::repository::S05IngestPartition>,full_memory:bool,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<String>,String>{
    let mut jobs=std::collections::BTreeSet::new();
    if let Some(selection)=selection {
        for job in selection.erasable_jobs.iter().chain(selection.s08_erasable.iter().map(|v|&v.0)).chain(selection.s08_policy_locators.iter().map(|v|&v.job_id)) {
            budget.check(job.len()).map_err(|_|"S05 quota unavailable")?;
            if job.is_empty() || job.len()>512 {return Err("S05 quota unavailable".into())}
            jobs.insert(job.clone());
        }
    } else {
        let mut stmt=tx.prepare("SELECT job_id FROM fm_v2_jobs WHERE owner_id=?1 AND (kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review') OR (?2 AND kind='access_consolidation')) ORDER BY job_id LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
        let mut rows=stmt.query(params![owner,full_memory]).map_err(|_|"S05 quota unavailable")?;
        while let Some(row)=rows.next().map_err(|_|"S05 quota unavailable")? {
            let job:String=row.get(0).map_err(|_|"S05 quota unavailable")?;budget.check(job.len()).map_err(|_|"S05 quota unavailable")?;
            if job.is_empty() || job.len()>512 || jobs.len()>=100000 || !jobs.insert(job) {return Err("S05 quota unavailable".into())}
        }
    }
    if jobs.len()>100000 {return Err("S05 quota unavailable".into())} Ok(jobs.into_iter().collect())
}

// Index unchanged M5 reset facts under the same protected lifecycle owner.
// The M5 fact remains distinct from per-attempt S05/S08 charges.
pub(super) fn write_m5_reset_coverage(tx:&Transaction<'_>,owner:&str,account:&str,receipt:&str,markers:&[crate::repository::s05_skill_draft::ResetTerminal],view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    if view.current_owner()!=owner || view.account_id()!=account || markers.len()>100000 {return Err("M5 quota unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"M5 quota unavailable")?;
    let receipt_hash:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![owner,receipt],|r|r.get(0)).map_err(|_|"M5 quota unavailable")?;
    let window_hash=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"M5 quota unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::M5Quota,&database,account)?;
    let request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-window.v1",account,receipt)).map_err(|_|"M5 quota unavailable")?);
    let meta=format!("s05_quota_m5_receipt_{account_hash}_{request}");let context=format!("m5-quota-purge:{meta}");
    let mut entries=std::collections::BTreeMap::new();
    for marker in markers {
        if marker.account_id!=account || marker.reset_receipt_id!=receipt || marker.settled_at_ms<0 {return Err("M5 quota unavailable".into())}
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&serde_json::json!([marker.captured_owner,marker.job_id])).map_err(|_|"M5 quota unavailable")?);
        let event=format!("outbox_skill_draft_reset_{id}");
        let (actual,backed)=crate::repository::s05_skill_draft::verified_reset_window_fact_in(tx,owner,account,&event,view,budget).map_err(|_|"M5 quota unavailable")?;
        let raw=serde_json::to_string(&actual).map_err(|_|"M5 quota unavailable")?;budget.check(raw.len()+event.len()).map_err(|_|"M5 quota unavailable")?;
        if backed || raw!=serde_json::to_string(marker).map_err(|_|"M5 quota unavailable")? || entries.insert(event,(crate::repository::sha256_hex(raw.as_bytes()),marker.settled_at_ms)).is_some(){return Err("M5 quota unavailable".into())}
    }
    let entries:Vec<_>=entries.into_iter().collect();let mut batches=Vec::new();
    for (ordinal,group) in entries.chunks(64).enumerate(){
        let max=group.iter().map(|(_,(_,at))|*at).max().ok_or("M5 quota unavailable")?;
        batches.push(QuotaBatch{version:1,window_account_sha256:window_hash.clone(),coverage_receipt_id:meta.clone(),ordinal:ordinal as u64,max_at_ms:max,entries:group.iter().map(|(key,(hash,_))|QuotaBatchEntry{key:key.clone(),fact_sha256:hash.clone()}).collect()});
    }
    let mut batch_sha256=Vec::new();let mut batch_max_at_ms=Vec::new();for batch in &batches {let raw=serde_json::to_vec(batch).map_err(|_|"M5 quota unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota unavailable")?;batch_sha256.push(crate::repository::sha256_hex(&raw));batch_max_at_ms.push(batch.max_at_ms);}
    let max_at_ms=batch_max_at_ms.iter().copied().max().unwrap_or(0);
    let coverage=ResetCoverage{version:1,database_id:database,account_id:account.into(),window_account_sha256:window_hash.clone(),reset_receipt_id:receipt.into(),reset_receipt_sha256:receipt_hash,count:markers.len() as u64,max_at_ms,batch_max_at_ms,batch_sha256,slot_count:0,slot_batch_sha256:Vec::new()};
    verify_coverage_s06(tx,owner,account,&coverage,view,budget)?;
    let body=serde_json::to_string(&coverage).map_err(|_|"M5 quota unavailable")?;budget.check(body.len()).map_err(|_|"M5 quota unavailable")?;
    tx.execute("INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES(?1,?2,NULL,?1,'s05 guarded m5 quota terminal',?3)",params![owner,context,chrono::Utc::now().to_rfc3339()]).map_err(|_|"M5 quota unavailable")?;
    let pointer=super::request_purge::RequestHash{request_sha256:request.clone(),body_sha256:Some(crate::repository::sha256_hex(body.as_bytes()))};
    super::request_purge::write_timed(tx,super::request_purge::Family::M5Quota,&account_hash,&meta,&meta,&context,&pointer,max_at_ms)?;
    tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![meta,body]).map_err(|_|"M5 quota unavailable")?;
    for batch in &batches {
        let raw=serde_json::to_string(batch).map_err(|_|"M5 quota unavailable")?;let key=format!("s05_quota_batch_{}_{}_m5_{:08}",window_hash,request,batch.ordinal);
        let hash=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-batch.v1",account,receipt,batch.ordinal)).map_err(|_|"M5 quota unavailable")?);
        let pointer=super::request_purge::RequestHash{request_sha256:hash,body_sha256:Some(crate::repository::sha256_hex(raw.as_bytes()))};
        super::request_purge::write_timed(tx,super::request_purge::Family::M5Quota,&account_hash,&meta,&key,&context,&pointer,batch.max_at_ms)?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![key,raw]).map_err(|_|"M5 quota unavailable")?;
    }
    // The same transaction must prove the header and every bounded batch it
    // just published; the prevalidated unchanged facts remain the authority.
    let stored:Option<(String,Option<String>,String,Option<i64>)>=tx.query_row("SELECT m.value,t.body_sha256,t.purge_context_id,t.effective_at_ms FROM fm_meta m JOIN native_request_purge_terminal t ON t.purge_receipt_id=m.key WHERE m.key=?1 AND t.family='m5_quota_window_v1' AND t.account_sha256=?2 AND t.request_sha256=?3",params![meta,account_hash,request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"M5 quota unavailable")?;
    if stored!=Some((body.clone(),Some(crate::repository::sha256_hex(body.as_bytes())),context.clone(),Some(max_at_ms))){return Err("M5 quota unavailable".into())}
    for batch in &batches {
        let expected=serde_json::to_string(batch).map_err(|_|"M5 quota unavailable")?;budget.check(expected.len()).map_err(|_|"M5 quota unavailable")?;
        let key=format!("s05_quota_batch_{}_{}_m5_{:08}",window_hash,request,batch.ordinal);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-batch.v1",account,receipt,batch.ordinal)).map_err(|_|"M5 quota unavailable")?);
        let stored:Option<(String,Option<String>,String,Option<i64>)>=tx.query_row("SELECT m.value,t.body_sha256,t.purge_context_id,t.effective_at_ms FROM fm_meta m JOIN native_request_purge_terminal t ON t.purge_receipt_id=m.key WHERE m.key=?1 AND t.family='m5_quota_window_v1' AND t.account_sha256=?2 AND t.request_sha256=?3",params![key,account_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"M5 quota unavailable")?;
        if stored!=Some((expected.clone(),Some(crate::repository::sha256_hex(expected.as_bytes())),context.clone(),Some(batch.max_at_ms))){return Err("M5 quota unavailable".into())}
    }
    if tx.execute("DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND reason='s05 guarded m5 quota terminal'",params![owner,context]).map_err(|_|"M5 quota unavailable")?!=1{return Err("M5 quota unavailable".into())}Ok(())
}

pub(super) fn read_m5_reset_window(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<crate::repository::s05_skill_draft::ResetTerminal>,String>{
    if view.current_owner()!=owner || view.account_id()!=account{return Err("S05 quota unavailable".into())}
    let start=now_ms.checked_sub(86_400_000).ok_or("S05 quota unavailable")?;
    if start<0{return Err("S05 quota unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::M5Quota,&database,account)?;
    let window_hash=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota unavailable")?;
    let schema_hash=purge_schema_hash(tx,budget)?;
    let mut stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='m5_quota_window_v1' AND account_sha256=?1 AND effective_at_ms>?2 AND (purge_receipt_id GLOB 's05_quota_m5_receipt_*' OR purge_receipt_id GLOB 's05_quota_batch_*_m5_[0-9]*') ORDER BY effective_at_ms,request_sha256 LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
    let mut rows=stmt.query(params![account_hash,start]).map_err(|_|"S05 quota unavailable")?;
    let mut pointers=std::collections::BTreeMap::<String,(String,String,String,i64)>::new();let mut headers=Vec::new();
    while let Some(row)=rows.next().map_err(|_|"S05 quota unavailable")? {
        if pointers.len()>=100000{return Err("S05 quota unavailable".into())}
        let request:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"S05 quota unavailable")?;let key:String=row.get(2).map_err(|_|"S05 quota unavailable")?;let context:String=row.get(3).map_err(|_|"S05 quota unavailable")?;let at:i64=row.get(4).map_err(|_|"S05 quota unavailable")?;
        budget.check(request.len()+key.len()+context.len()+64).map_err(|_|"S05 quota unavailable")?;
        let hash=hash.ok_or("S05 quota unavailable")?;
        if at>now_ms || hash.len()!=64 || !hash.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || request.len()!=64 || !request.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || (!key.starts_with("s05_quota_m5_receipt_") && !key.starts_with("s05_quota_batch_")) {return Err("S05 quota unavailable".into())}
        if key.starts_with("s05_quota_m5_receipt_"){headers.push(key.clone());}
        if pointers.insert(key,(request,hash,context,at)).is_some(){return Err("S05 quota unavailable".into())}
    }
    drop(rows);drop(stmt);let mut consumed=std::collections::BTreeSet::new();let mut seen=std::collections::BTreeSet::new();let mut output=Vec::new();
    for header in headers {
        let (request,body_hash,context,at)=pointers.get(&header).ok_or("S05 quota unavailable")?.clone();consumed.insert(header.clone());
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&header],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let coverage:ResetCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
        if coverage.version!=1 || coverage.database_id!=database || coverage.account_id!=account || coverage.window_account_sha256!=window_hash || header!=format!("s05_quota_m5_receipt_{account_hash}_{request}") || context!=format!("m5-quota-purge:{header}") || request!=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-window.v1",account,&coverage.reset_receipt_id)).map_err(|_|"M5 quota unavailable")?) || body_hash!=crate::repository::sha256_hex(raw.as_bytes()) || serde_json::to_string(&coverage).map_err(|_|"S05 quota unavailable")?!=raw || coverage.count>100000 || coverage.slot_count!=0 || !coverage.slot_batch_sha256.is_empty() || coverage.batch_sha256.len() as u64!=(coverage.count+63)/64 || coverage.batch_max_at_ms.len()!=coverage.batch_sha256.len() || coverage.batch_max_at_ms.iter().any(|v|*v<0) || coverage.max_at_ms!=coverage.batch_max_at_ms.iter().copied().max().unwrap_or(0) || at!=coverage.max_at_ms {return Err("S05 quota unavailable".into())}
        let transferred=verify_purge_transfer(tx,account,&header,&body_hash,&schema_hash,budget)?;
        if !transferred {verify_coverage_s06(tx,owner,account,&coverage,view,budget)?;}
        for (ordinal,max_at) in coverage.batch_max_at_ms.iter().enumerate() {
            if *max_at<=start {continue}
            let batch_key=format!("s05_quota_batch_{}_{}_m5_{:08}",window_hash,request,ordinal);
            let (batch_request,batch_hash,batch_context,batch_at)=pointers.get(&batch_key).ok_or("S05 quota unavailable")?.clone();consumed.insert(batch_key.clone());
            let expected_request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-batch.v1",account,&coverage.reset_receipt_id,ordinal as u64)).map_err(|_|"S05 quota unavailable")?);
            if batch_request!=expected_request || batch_context!=context || batch_at!=*max_at {return Err("S05 quota unavailable".into())}
            let batch_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&batch_key],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(batch_raw.len()).map_err(|_|"S05 quota unavailable")?;
            let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&batch_raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
            let expected_len=if ordinal+1==coverage.batch_sha256.len(){((coverage.count.checked_sub(1).ok_or("S05 quota unavailable")?%64)+1) as usize}else{64};
            if batch_hash!=crate::repository::sha256_hex(batch_raw.as_bytes()) || batch_hash!=coverage.batch_sha256[ordinal] || serde_json::to_string(&batch).map_err(|_|"S05 quota unavailable")?!=batch_raw || batch.version!=1 || batch.window_account_sha256!=window_hash || batch.coverage_receipt_id!=header || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=*max_at || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota unavailable".into())}
            if transferred {continue}
            let mut actual_max=0i64;
            for entry in batch.entries {
                if !entry.key.starts_with("outbox_skill_draft_reset_") || entry.key.len()!=89 || entry.fact_sha256.len()!=64 || !entry.fact_sha256.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || !seen.insert(entry.key.clone()){return Err("M5 quota unavailable".into())}
                let (fact,backed)=crate::repository::s05_skill_draft::verified_reset_window_fact_in(tx,owner,account,&entry.key,view,budget).map_err(|_|"M5 quota unavailable")?;
                let raw=serde_json::to_string(&fact).map_err(|_|"M5 quota unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota unavailable")?;
                if fact.reset_receipt_id!=coverage.reset_receipt_id || crate::repository::sha256_hex(raw.as_bytes())!=entry.fact_sha256 || fact.settled_at_ms>now_ms {return Err("M5 quota unavailable".into())}
                actual_max=actual_max.max(fact.settled_at_ms);if !backed && fact.settled_at_ms>start {output.push(fact);}

            }
            if actual_max!=*max_at{return Err("S05 quota unavailable".into())}
        }
    }
    if consumed.len()!=pointers.len(){return Err("S05 quota unavailable".into())}Ok(output)
}


// The caller already proved the immutable cutoff and global settled/quiet
// state in this same transaction. This returns factual ledger evidence only.
pub(super) fn verify_live_charge(tx:&Transaction<'_>,owner:&str,account:&str,cutoff:i64,row:&crate::repository::s05_budget::LiveReservation,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<crate::repository::s05_budget::LiveChargeProof,String>{
    if row.state!="settled" || row.job_kind=="skill_draft" {return Err("S05 quota unavailable".into())}
    let capture=capture_ledger_attempts(tx,owner,account,std::slice::from_ref(&row.job_id),cutoff,None,false,view,budget)?;
    let job=crate::repository::s05_budget::job_digest(account,&row.job_id).map_err(|_|"S05 quota unavailable")?;
    let attempt=crate::repository::s05_budget::attempt_digest(account,&row.attempt_id).map_err(|_|"S05 quota unavailable")?;
    let mut matches=capture.charges.into_iter().filter(|f|f.job_sha256==job && f.attempt_sha256==attempt && f.lease_epoch==row.lease_epoch);
    let fact=matches.next().ok_or("S05 quota unavailable")?;if matches.next().is_some() || fact.job_kind!=row.job_kind || fact.original_sha256!=row.original_sha256 || fact.reserved_at_ms!=row.reserved_at_ms || fact.settled_at_ms!=row.settled_at_ms || fact.charged_calls!=row.charged.calls || fact.charged_seconds!=row.charged.seconds || fact.charged_input_tokens!=row.charged.input_tokens || fact.charged_output_tokens!=row.charged.output_tokens{return Err("S05 quota unavailable".into())}
    Ok(crate::repository::s05_budget::LiveChargeProof{basis:fact.charge_basis,reserve_original_sha256:fact.reserve_original_sha256,reserve_result_sha256:fact.reserve_result_sha256})
}

// Lifecycle-only bounded complete M5 pointer inventory; admission uses the window reader.
fn read_m5_reset_all(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(Vec<crate::repository::s05_skill_draft::ResetTerminal>,Vec<ResetCoverage>),String>{
    if view.current_owner()!=owner || view.account_id()!=account{return Err("S05 quota unavailable".into())}
    if now_ms<0{return Err("M5 quota unavailable".into())}
    let start=-1i64;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::M5Quota,&database,account)?;
    let window_hash=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota unavailable")?;
    let schema_hash=purge_schema_hash(tx,budget)?;
    let mut stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='m5_quota_window_v1' AND account_sha256=?1 AND effective_at_ms>?2 AND (purge_receipt_id GLOB 's05_quota_m5_receipt_*' OR purge_receipt_id GLOB 's05_quota_batch_*_m5_[0-9]*') ORDER BY effective_at_ms,request_sha256 LIMIT 100001").map_err(|_|"S05 quota unavailable")?;
    let mut rows=stmt.query(params![account_hash,start]).map_err(|_|"S05 quota unavailable")?;
    let mut pointers=std::collections::BTreeMap::<String,(String,String,String,i64)>::new();let mut headers=Vec::new();
    while let Some(row)=rows.next().map_err(|_|"S05 quota unavailable")? {
        if pointers.len()>=100000{return Err("S05 quota unavailable".into())}
        let request:String=row.get(0).map_err(|_|"S05 quota unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"S05 quota unavailable")?;let key:String=row.get(2).map_err(|_|"S05 quota unavailable")?;let context:String=row.get(3).map_err(|_|"S05 quota unavailable")?;let at:i64=row.get(4).map_err(|_|"S05 quota unavailable")?;
        budget.check(request.len()+key.len()+context.len()+64).map_err(|_|"S05 quota unavailable")?;
        let hash=hash.ok_or("S05 quota unavailable")?;
        if at>now_ms || hash.len()!=64 || !hash.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || request.len()!=64 || !request.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || (!key.starts_with("s05_quota_m5_receipt_") && !key.starts_with("s05_quota_batch_")) {return Err("S05 quota unavailable".into())}
        if key.starts_with("s05_quota_m5_receipt_"){headers.push(key.clone());}
        if pointers.insert(key,(request,hash,context,at)).is_some(){return Err("S05 quota unavailable".into())}
    }
    drop(rows);drop(stmt);let mut consumed=std::collections::BTreeSet::new();let mut seen=std::collections::BTreeSet::new();let mut output=Vec::new();let mut coverages=Vec::new();
    for header in headers {
        let (request,body_hash,context,at)=pointers.get(&header).ok_or("S05 quota unavailable")?.clone();consumed.insert(header.clone());
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&header],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota unavailable")?;
        let coverage:ResetCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
        if coverage.version!=1 || coverage.database_id!=database || coverage.account_id!=account || coverage.window_account_sha256!=window_hash || header!=format!("s05_quota_m5_receipt_{account_hash}_{request}") || context!=format!("m5-quota-purge:{header}") || request!=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-window.v1",account,&coverage.reset_receipt_id)).map_err(|_|"M5 quota unavailable")?) || body_hash!=crate::repository::sha256_hex(raw.as_bytes()) || serde_json::to_string(&coverage).map_err(|_|"S05 quota unavailable")?!=raw || coverage.count>100000 || coverage.slot_count!=0 || !coverage.slot_batch_sha256.is_empty() || coverage.batch_sha256.len() as u64!=(coverage.count+63)/64 || coverage.batch_max_at_ms.len()!=coverage.batch_sha256.len() || coverage.batch_max_at_ms.iter().any(|v|*v<0) || coverage.max_at_ms!=coverage.batch_max_at_ms.iter().copied().max().unwrap_or(0) || at!=coverage.max_at_ms {return Err("S05 quota unavailable".into())}
        let transferred=verify_purge_transfer(tx,account,&header,&body_hash,&schema_hash,budget)?;
        if !transferred {verify_coverage_s06(tx,owner,account,&coverage,view,budget)?;}
        for (ordinal,max_at) in coverage.batch_max_at_ms.iter().enumerate() {
            if *max_at<=start {continue}
            let batch_key=format!("s05_quota_batch_{}_{}_m5_{:08}",window_hash,request,ordinal);
            let (batch_request,batch_hash,batch_context,batch_at)=pointers.get(&batch_key).ok_or("S05 quota unavailable")?.clone();consumed.insert(batch_key.clone());
            let expected_request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-batch.v1",account,&coverage.reset_receipt_id,ordinal as u64)).map_err(|_|"S05 quota unavailable")?);
            if batch_request!=expected_request || batch_context!=context || batch_at!=*max_at {return Err("S05 quota unavailable".into())}
            let batch_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&batch_key],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;budget.check(batch_raw.len()).map_err(|_|"S05 quota unavailable")?;
            let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&batch_raw).map_err(|_|"S05 quota unavailable")?).map_err(|_|"S05 quota unavailable")?;
            let expected_len=if ordinal+1==coverage.batch_sha256.len(){((coverage.count.checked_sub(1).ok_or("S05 quota unavailable")?%64)+1) as usize}else{64};
            if batch_hash!=crate::repository::sha256_hex(batch_raw.as_bytes()) || batch_hash!=coverage.batch_sha256[ordinal] || serde_json::to_string(&batch).map_err(|_|"S05 quota unavailable")?!=batch_raw || batch.version!=1 || batch.window_account_sha256!=window_hash || batch.coverage_receipt_id!=header || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=*max_at || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota unavailable".into())}
            if transferred {continue}
            let mut actual_max=0i64;
            for entry in batch.entries {
                if !entry.key.starts_with("outbox_skill_draft_reset_") || entry.key.len()!=89 || entry.fact_sha256.len()!=64 || !entry.fact_sha256.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) || !seen.insert(entry.key.clone()){return Err("M5 quota unavailable".into())}
                let (fact,backed)=crate::repository::s05_skill_draft::verified_reset_window_fact_in(tx,owner,account,&entry.key,view,budget).map_err(|_|"M5 quota unavailable")?;
                let raw=serde_json::to_string(&fact).map_err(|_|"M5 quota unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota unavailable")?;
                if fact.reset_receipt_id!=coverage.reset_receipt_id || crate::repository::sha256_hex(raw.as_bytes())!=entry.fact_sha256 || fact.settled_at_ms>now_ms {return Err("M5 quota unavailable".into())}
                actual_max=actual_max.max(fact.settled_at_ms);if !backed && fact.settled_at_ms>start {output.push(fact);}

            }
            if actual_max!=*max_at{return Err("S05 quota unavailable".into())}
        }
        coverages.push(coverage);
    }
    if consumed.len()!=pointers.len(){return Err("S05 quota unavailable".into())}Ok((output,coverages))
}

// Complete same-transaction lifecycle inventory before account purge. The
// caller must preserve or transfer every header and slot before deleting rows.
pub(super) struct PurgeCapture {
    charges:Vec<crate::repository::s05_budget::RetainedCharge>,
    reset_headers:Vec<ResetCoverage>,reset_slots:Vec<SlotTerminal>,new_slots:Vec<PendingSlot>,
    m5_facts:Vec<crate::repository::s05_skill_draft::ResetTerminal>,m5_headers:Vec<ResetCoverage>,prior_purge_binding:String,
}
pub(super) fn capture_purge(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<PurgeCapture,String>{
    let cutoff=verified_cutoff_receipt(tx,owner,account,now_ms,budget)?;
    verify_owner_orphan_reservations(tx,owner,budget)?;
    let prior_purge_binding=prior_purge_binding(tx,account,now_ms,budget)?;
    let (prior,mut reset_slots,reset_headers)=read_reset_all(tx,owner,account,view,budget)?;
    if reset_headers.iter().any(|v|v.max_at_ms>now_ms) || prior.iter().any(|v|v.reserved_at_ms>now_ms || v.settled_at_ms.is_some_and(|at|at>now_ms)){return Err("S05 quota unavailable".into())}
    let erased_receipts:std::collections::BTreeSet<String>=reset_headers.iter().map(|v|v.reset_receipt_id.clone()).collect();
    let (m5_facts,m5_headers)=verified_m5_lifecycle_inventory(tx,owner,account,now_ms,view,budget)?;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota unavailable")?;
    let schema_hash=purge_schema_hash(tx,budget)?;let mut remaining_headers=Vec::new();let mut remaining_m5=Vec::new();let mut remaining_keys=std::collections::BTreeSet::new();
    for (family,headers) in [(super::request_purge::Family::S05Reset,reset_headers),(super::request_purge::Family::M5Quota,m5_headers)] {
        let account_hash=super::request_purge::account_hash(family,&database,account)?;
        for header in headers {
            let request=match family {super::request_purge::Family::S05Reset=>reset_pointer(account,&header.reset_receipt_id)?,super::request_purge::Family::M5Quota=>crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-window.v1",account,&header.reset_receipt_id)).map_err(|_|"S05 quota unavailable")?),_=>return Err("S05 quota unavailable".into())};
            let key=match family {super::request_purge::Family::S05Reset=>format!("s05_quota_reset_receipt_{account_hash}_{request}"),super::request_purge::Family::M5Quota=>format!("s05_quota_m5_receipt_{account_hash}_{request}"),_=>return Err("S05 quota unavailable".into())};
            let raw=serde_json::to_vec(&header).map_err(|_|"S05 quota unavailable")?;budget.check(raw.len()+key.len()).map_err(|_|"S05 quota unavailable")?;
            if !verify_purge_transfer(tx,account,&key,&crate::repository::sha256_hex(&raw),&schema_hash,budget)? {
                remaining_keys.insert(key);match family {super::request_purge::Family::S05Reset=>remaining_headers.push(header),super::request_purge::Family::M5Quota=>remaining_m5.push(header),_=>return Err("S05 quota unavailable".into())}
            }
        }
    }
    reset_slots.retain(|slot|remaining_keys.contains(&slot.coverage_receipt_id));
    let reset_headers=remaining_headers;let m5_headers=remaining_m5;
    let jobs=reset_jobs(tx,owner,None,true,budget)?;
    let current=capture_ledger_attempts(tx,owner,account,&jobs,cutoff,Some(&erased_receipts),false,view,budget)?;
    if current.charges.iter().any(|v|v.reserved_at_ms>now_ms || v.settled_at_ms.is_some_and(|at|at>now_ms)){return Err("S05 quota unavailable".into())}
    let mut charges=std::collections::BTreeMap::new();
    for fact in prior {let key=crate::repository::s05_budget::charge_key(&fact).map_err(|_|"S05 quota unavailable")?;if charges.insert(key,fact).is_some(){return Err("S05 quota unavailable".into())}}
    for fact in current.charges {
        let key=crate::repository::s05_budget::charge_key(&fact).map_err(|_|"S05 quota unavailable")?;
        if let Some(prior)=charges.get(&key) {if !crate::repository::s05_budget::same_live_charge(prior,&fact).map_err(|_|"S05 quota unavailable")? {return Err("S05 quota unavailable".into())}} else {charges.insert(key,fact);}
    }
    let old_slots:std::collections::BTreeSet<&str>=reset_slots.iter().map(|s|s.slot.as_str()).collect();
    if current.slots.iter().any(|s|old_slots.contains(s.slot.as_str())) {return Err("S05 quota unavailable".into())}
    Ok(PurgeCapture{charges:charges.into_values().collect(),reset_headers,reset_slots,new_slots:current.slots,m5_facts,m5_headers,prior_purge_binding})
}

#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PurgeChargeEntry {key:String,charge:crate::repository::s05_budget::RetainedCharge}
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PurgeChargeBatch {version:u32,window_account_sha256:String,coverage_receipt_id:String,ordinal:u64,max_at_ms:i64,entries:Vec<PurgeChargeEntry>}
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PurgeCoverage {
    version:u32,database_id:String,account_id:String,window_account_sha256:String,
    purge_receipt_id:String,purge_request_sha256:String,m5_header_id:String,m5_header_sha256:String,count:u64,max_at_ms:i64,
    batch_max_at_ms:Vec<i64>,batch_sha256:Vec<String>,
    slot_count:u64,slot_batch_sha256:Vec<String>,
    transfer_count:u64,transfer_batch_sha256:Vec<String>,
}
pub(super) struct PurgePlan {
    meta:String,request:String,account_hash:String,context:String,coverage:PurgeCoverage,
    batches:Vec<PurgeChargeBatch>,transfers:Vec<QuotaBatch>,slots:Vec<(SlotTerminal,String,u32)>,slot_batches:Vec<QuotaBatch>,
    m5:M5PurgePlan,
}
// Pure preparation after full custody inventory. No pointer is published here.
pub(super) fn prepare_purge_plan(database:&str,account:&str,receipt:&str,request_hash:&str,capture:PurgeCapture,m5_usage:&[super::request_purge::SkillUsage],budget:&mut crate::repository::ResetFenceBudget)->Result<PurgePlan,String>{
    let valid_hash=|v:&str|v.len()==64 && v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
    if !valid_hash(request_hash) || capture.charges.len()>100000 || capture.new_slots.len()>100000 {return Err("S05 quota purge unavailable".into())}
    let window_hash=crate::repository::s05_budget::window_account_digest(database,account).map_err(|_|"S05 quota purge unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,database,account)?;
    let request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-window.v1",account,receipt)).map_err(|_|"S05 quota purge unavailable")?);
    let meta=format!("s05_quota_purge_receipt_{account_hash}_{request}");let context=format!("s05-purge-purge:{meta}");
    let mut entries=Vec::new();for charge in capture.charges {
        if charge.account_id!=account {return Err("S05 quota purge unavailable".into())}
        let key=crate::repository::s05_budget::charge_key(&charge).map_err(|_|"S05 quota purge unavailable")?;entries.push(PurgeChargeEntry{key,charge});
    }
    entries.sort_by(|a,b|a.key.cmp(&b.key));if entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota purge unavailable".into())}
    let mut batches=Vec::new();for (ordinal,group) in entries.chunks(64).enumerate(){
        let max=group.iter().map(|e|e.charge.settled_at_ms.unwrap_or(e.charge.reserved_at_ms)).max().ok_or("S05 quota purge unavailable")?;
        let raw=serde_json::to_vec(group).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
        let owned:Vec<PurgeChargeEntry>=serde_json::from_slice(&raw).map_err(|_|"S05 quota purge unavailable")?;
        batches.push(PurgeChargeBatch{version:1,window_account_sha256:window_hash.clone(),coverage_receipt_id:meta.clone(),ordinal:ordinal as u64,max_at_ms:max,entries:owned});
    }
    let mut batch_sha256=Vec::new();let mut batch_max_at_ms=Vec::new();for batch in &batches {let raw=serde_json::to_vec(batch).map_err(|_|"S05 quota purge unavailable")?;if raw.len()>131072{return Err("S05 quota purge unavailable".into())}budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;batch_sha256.push(crate::repository::sha256_hex(&raw));batch_max_at_ms.push(batch.max_at_ms);}
    let mut transfers=std::collections::BTreeMap::new();
    for (family,headers) in [(super::request_purge::Family::S05Reset,capture.reset_headers),(super::request_purge::Family::M5Quota,capture.m5_headers)] {
        let source_account=super::request_purge::account_hash(family,database,account)?;
        for header in headers {
            let source_request=match family {
                super::request_purge::Family::S05Reset=>reset_pointer(account,&header.reset_receipt_id)?,
                super::request_purge::Family::M5Quota=>crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-window.v1",account,&header.reset_receipt_id)).map_err(|_|"S05 quota purge unavailable")?),
                _=>return Err("S05 quota purge unavailable".into()),
            };
            let source_key=match family {super::request_purge::Family::S05Reset=>format!("s05_quota_reset_receipt_{source_account}_{source_request}"),super::request_purge::Family::M5Quota=>format!("s05_quota_m5_receipt_{source_account}_{source_request}"),_=>return Err("S05 quota purge unavailable".into())};
            let raw=serde_json::to_vec(&header).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()+source_key.len()).map_err(|_|"S05 quota purge unavailable")?;
            if transfers.insert(source_key,crate::repository::sha256_hex(&raw)).is_some(){return Err("S05 quota purge unavailable".into())}
        }
    }
    if transfers.len()>100000{return Err("S05 quota purge unavailable".into())}
    let transfer_count=transfers.len() as u64;let transfers:Vec<_>=transfers.into_iter().collect();let mut transfer_batches=Vec::new();
    for (ordinal,group) in transfers.chunks(64).enumerate(){transfer_batches.push(QuotaBatch{version:1,window_account_sha256:window_hash.clone(),coverage_receipt_id:meta.clone(),ordinal:ordinal as u64,max_at_ms:0,entries:group.iter().map(|(key,hash)|QuotaBatchEntry{key:key.clone(),fact_sha256:hash.clone()}).collect()});}
    let mut transfer_batch_sha256=Vec::new();for batch in &transfer_batches {let raw=serde_json::to_vec(batch).map_err(|_|"S05 quota purge unavailable")?;if raw.len()>16384{return Err("S05 quota purge unavailable".into())}budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;transfer_batch_sha256.push(crate::repository::sha256_hex(&raw));}
    let (slots,slot_batches)=make_slot_batches(&capture.new_slots,account,&meta,&window_hash,budget)?;
    let mut slot_batch_sha256=Vec::new();for batch in &slot_batches {let raw=serde_json::to_vec(batch).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;slot_batch_sha256.push(crate::repository::sha256_hex(&raw));}
    let mut m5_by_id=std::collections::BTreeMap::new();for item in m5_usage {if m5_by_id.insert(item.operation_sha256.as_str(),item).is_some(){return Err("M5 quota purge unavailable".into())}}
    for fact in &capture.m5_facts {
        let id=fact.job_id.strip_prefix("skilldraft_").ok_or("M5 quota purge unavailable")?;let item=m5_by_id.get(id).ok_or("M5 quota purge unavailable")?;
        if item.original_sha256!=fact.original_sha256 || item.disposition!=fact.disposition || item.charge_basis!=fact.charge_basis || item.settled_at_ms!=fact.settled_at_ms || item.charged_calls!=fact.charged_calls || item.charged_seconds!=fact.charged_seconds || item.charged_input_tokens!=fact.charged_input_tokens || item.charged_output_tokens!=fact.charged_output_tokens{return Err("M5 quota purge unavailable".into())}
    }
    let m5=prepare_m5_purge_plan(database,account,receipt,request_hash,&meta,m5_usage,budget)?;
    let m5_raw=serde_json::to_vec(&m5.coverage).map_err(|_|"M5 quota purge unavailable")?;budget.check(m5_raw.len()).map_err(|_|"M5 quota purge unavailable")?;
    let max_at_ms=batch_max_at_ms.iter().copied().max().unwrap_or(0);
    let coverage=PurgeCoverage{version:1,database_id:database.into(),account_id:account.into(),window_account_sha256:window_hash,purge_receipt_id:receipt.into(),purge_request_sha256:request_hash.into(),m5_header_id:m5.meta.clone(),m5_header_sha256:crate::repository::sha256_hex(&m5_raw),count:entries.len() as u64,max_at_ms,batch_max_at_ms,batch_sha256,slot_count:slots.len() as u64,slot_batch_sha256,transfer_count,transfer_batch_sha256};
    // Existing slots remain anchored to their source header; the transfer
    // batches must preserve those exact header hashes.
    let transfer_keys:std::collections::BTreeSet<&str>=transfers.iter().map(|(key,_)|key.as_str()).collect();
    for slot in &capture.reset_slots {if !transfer_keys.contains(slot.coverage_receipt_id.as_str()){return Err("S05 quota purge unavailable".into())}}
    Ok(PurgePlan{meta,request,account_hash,context,coverage,batches,transfers:transfer_batches,slots,slot_batches,m5})
}

// Write only under the existing guarded purge transaction. The caller must
// bind the returned header hash into the actual guarded purge receipt before
// committing; no standalone pointer is admission authority.
pub(super) fn write_purge_plan(tx:&Transaction<'_>,owner:&str,account:&str,plan:&PurgePlan,m5_manifest:Option<&serde_json::Value>,budget:&mut crate::repository::ResetFenceBudget)->Result<serde_json::Value,String>{
    if plan.coverage.account_id!=account || plan.coverage.version!=1{return Err("S05 quota purge unavailable".into())}
    let body=serde_json::to_string(&plan.coverage).map_err(|_|"S05 quota purge unavailable")?;if body.len()>262144{return Err("S05 quota purge unavailable".into())}budget.check(body.len()).map_err(|_|"S05 quota purge unavailable")?;
    tx.execute("INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES(?1,?2,NULL,?1,'native request guarded owner purge terminal',?3)",params![owner,plan.context,chrono::Utc::now().to_rfc3339()]).map_err(|_|"S05 quota purge unavailable")?;
    let pointer=super::request_purge::RequestHash{request_sha256:plan.request.clone(),body_sha256:Some(crate::repository::sha256_hex(body.as_bytes()))};
    super::request_purge::write_timed(tx,super::request_purge::Family::S05Purge,&plan.account_hash,&plan.meta,&plan.meta,&plan.context,&pointer,plan.coverage.max_at_ms)?;
    tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![plan.meta,body]).map_err(|_|"S05 quota purge unavailable")?;
    let put=|key:&str,id:String,raw:String,at:i64|->Result<(),String>{
        let entry=super::request_purge::RequestHash{request_sha256:id,body_sha256:Some(crate::repository::sha256_hex(raw.as_bytes()))};
        super::request_purge::write_timed(tx,super::request_purge::Family::S05Purge,&plan.account_hash,&plan.meta,key,&plan.context,&entry,at)?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![key,raw]).map_err(|_|"S05 quota purge unavailable")?;Ok(())
    };
    for batch in &plan.batches {
        let raw=serde_json::to_string(batch).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
        let key=format!("s05_quota_batch_{}_{}_purge_{:08}",plan.coverage.window_account_sha256,plan.request,batch.ordinal);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-batch.v1",account,&plan.coverage.purge_receipt_id,batch.ordinal)).map_err(|_|"S05 quota purge unavailable")?);put(&key,id,raw,batch.max_at_ms)?;
    }
    for batch in &plan.transfers {
        let raw=serde_json::to_string(batch).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
        let key=format!("s05_quota_batch_{}_{}_transfer_{:08}",plan.coverage.window_account_sha256,plan.request,batch.ordinal);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-transfer-batch.v1",account,&plan.coverage.purge_receipt_id,batch.ordinal)).map_err(|_|"S05 quota purge unavailable")?);put(&key,id,raw,0)?;
        for entry in &batch.entries {
            let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-transfer.v1",account,&entry.key)).map_err(|_|"S05 quota purge unavailable")?);
            let key=format!("s05_quota_batch_{}_{}_ref",plan.coverage.window_account_sha256,id);
            let direct=QuotaBatch{version:1,window_account_sha256:plan.coverage.window_account_sha256.clone(),coverage_receipt_id:plan.meta.clone(),ordinal:batch.ordinal,max_at_ms:0,entries:vec![QuotaBatchEntry{key:entry.key.clone(),fact_sha256:entry.fact_sha256.clone()}]};
            let raw=serde_json::to_string(&direct).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;put(&key,id,raw,0)?;
        }
    }
    for batch in &plan.slot_batches {
        let raw=serde_json::to_string(batch).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
        let key=format!("s05_quota_batch_{}_{}_slot_{:08}",plan.coverage.window_account_sha256,plan.request,batch.ordinal);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-slot-batch.v1",account,&plan.coverage.purge_receipt_id,batch.ordinal)).map_err(|_|"S05 quota purge unavailable")?);put(&key,id,raw,0)?;
    }
    if tx.execute("DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND reason='native request guarded owner purge terminal'",params![owner,plan.context]).map_err(|_|"S05 quota purge unavailable")?!=1{return Err("S05 quota purge unavailable".into())}
    let slot_account=super::request_purge::account_hash(super::request_purge::Family::S05Slot,&plan.coverage.database_id,account)?;
    for (terminal,_,_) in &plan.slots {
        let raw=serde_json::to_string(terminal).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;let id=slot_pointer(account,&terminal.slot)?;
        let key=format!("s05_quota_slot_terminal_{slot_account}_{id}");let context=format!("s05-slot-purge:{key}");
        tx.execute("INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES(?1,?2,NULL,?1,'native request guarded owner purge terminal',?3)",params![owner,context,chrono::Utc::now().to_rfc3339()]).map_err(|_|"S05 quota purge unavailable")?;
        let pointer=super::request_purge::RequestHash{request_sha256:id,body_sha256:Some(crate::repository::sha256_hex(raw.as_bytes()))};
        super::request_purge::write(tx,super::request_purge::Family::S05Slot,&slot_account,&key,&context,&[pointer])?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![key,raw]).map_err(|_|"S05 quota purge unavailable")?;
        if tx.execute("DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND reason='native request guarded owner purge terminal'",params![owner,context]).map_err(|_|"S05 quota purge unavailable")?!=1{return Err("S05 quota purge unavailable".into())}
    }
    write_m5_purge_plan(tx,owner,account,&plan.m5,m5_manifest,budget)?;
    Ok(serde_json::json!({"version":1,"header_id":plan.meta,"header_request_sha256":plan.request,"header_sha256":crate::repository::sha256_hex(body.as_bytes())}))
}

// A purge pointer is factual only when its exact guarded owner receipt and
// existing schema-stamp fact are present and bind the same header.
fn purge_schema_hash(tx:&Transaction<'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<String>,String>{
    let schema=SqliteStore::schema_definitions(tx)?;
    let raw=serde_json::to_vec(&schema).map_err(|_|"S05 quota purge unavailable")?;
    budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    SqliteStore::structure_compatible_schema_digests(tx,&schema)
}
fn verify_purge_header(tx:&Transaction<'_>,account:&str,meta:&str,schema_hash:&[String],budget:&mut crate::repository::ResetFenceBudget)->Result<(PurgeCoverage,String),String>{
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[meta],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    let coverage:PurgeCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,&database,account)?;
    let window=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota purge unavailable")?;
    let request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-window.v1",account,&coverage.purge_receipt_id)).map_err(|_|"S05 quota purge unavailable")?);
    let hash=crate::repository::sha256_hex(raw.as_bytes());let context=format!("s05-purge-purge:{meta}");
    let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
    let valid_hash=|v:&str|v.len()==64 && v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
    if coverage.version!=1 || coverage.database_id!=database || coverage.account_id!=account || coverage.window_account_sha256!=window || !valid_hash(&coverage.purge_request_sha256) || !valid_hash(&coverage.m5_header_sha256) || !coverage.m5_header_id.starts_with("s05_quota_m5purge_receipt_") || !coverage.purge_receipt_id.starts_with("owner_transition_") || coverage.purge_receipt_id.len()!=81 || !valid_hash(&coverage.purge_receipt_id[17..]) || meta!=format!("s05_quota_purge_receipt_{account_hash}_{request}") || serde_json::to_string(&coverage).map_err(|_|"S05 quota purge unavailable")?!=raw || pointer!=Some((Some(hash.clone()),meta.into(),context,Some(coverage.max_at_ms))) || coverage.count>100000 || coverage.slot_count>100000 || coverage.transfer_count>100000 || coverage.batch_sha256.len() as u64!=(coverage.count+63)/64 || coverage.batch_max_at_ms.len()!=coverage.batch_sha256.len() || coverage.batch_max_at_ms.iter().any(|v|*v<0) || coverage.max_at_ms!=coverage.batch_max_at_ms.iter().copied().max().unwrap_or(0) || coverage.slot_batch_sha256.len() as u64!=(coverage.slot_count+63)/64 || coverage.transfer_batch_sha256.len() as u64!=(coverage.transfer_count+63)/64 || coverage.batch_sha256.iter().chain(&coverage.slot_batch_sha256).chain(&coverage.transfer_batch_sha256).any(|v|!valid_hash(v)){return Err("S05 quota purge unavailable".into())}
    let receipt_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=65536",[&coverage.purge_receipt_id],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(receipt_raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    let receipt=crate::field_authority::parse_unique_json(&receipt_raw).map_err(|_|"S05 quota purge unavailable")?;
    let expected=serde_json::json!({"version":1,"header_id":meta,"header_request_sha256":request,"header_sha256":hash});
    if receipt.as_object().map(|v|v.len())!=Some(8) || receipt["version"]!=5 || receipt["receipt_id"]!=coverage.purge_receipt_id || receipt["request_sha256"]!=coverage.purge_request_sha256 || receipt["result"]["action"]!="purge" || receipt["s05_quota"]!=expected || receipt.get("m5").is_none() || receipt.get("f1").is_none() || receipt.get("f8").is_none() {return Err("S05 quota purge unavailable".into())}
    let stamp_key=format!("owner_schema_stamp_{}",coverage.purge_receipt_id);
    let stamp_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=8192",[stamp_key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(stamp_raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    let stamp=crate::field_authority::parse_unique_json(&stamp_raw).map_err(|_|"S05 quota purge unavailable")?;
    if stamp.as_object().map(|v|v.len())!=Some(8) || stamp["version"]!=1 || stamp["receipt_id"]!=coverage.purge_receipt_id || stamp["receipt_hash"]!=crate::repository::sha256_hex(receipt_raw.as_bytes()) || stamp["request_sha256"]!=coverage.purge_request_sha256 || stamp["database_id"]!=database || stamp["before_cookie"].as_i64().is_none_or(|v|v<0) || stamp["after_cookie"].as_i64().is_none_or(|v|v<0) || !schema_hash.iter().any(|digest| stamp["schema_sha256"]==*digest) || serde_json::to_string(&stamp).map_err(|_|"S05 quota purge unavailable")?!=stamp_raw {return Err("S05 quota purge unavailable".into())}
    verify_m5_purge_header(tx,account,meta,&coverage,budget)?;
    Ok((coverage,request))
}

/// Positive account-purge evidence only. A missing pointer is not historical
/// coverage and cannot authorize an operation. The caller must first validate
/// the exact guarded profile in this same transaction.
pub(super) fn account_purge_positive_in(tx:&Transaction<'_>,account:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<bool,String>{
    if account.is_empty() || account.len()>512 {return Err("S09 purge proof unavailable".into())}
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_purge.v1'",[],|r|r.get(0)).optional().map_err(|_|"S09 purge proof unavailable")?;
    if marker.as_deref()!=Some("1") {return Err("S09 purge proof unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S09 purge proof unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,&database,account)?;
    // The quota purge family is written only by the timed owner. Refuse an
    // invalid pointer instead of interpreting its filtered absence as clean.
    // NULL entries are excluded from the partial time index. The table's
    // family/account primary-key prefix is the bounded path for that probe.
    budget.check(0).map_err(|_|"S09 purge proof unavailable")?;
    let null_pointer:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND effective_at_ms IS NULL)",[&account_hash],|r|r.get(0)).map_err(|_|"S09 purge proof unavailable")?;
    if null_pointer {return Err("S09 purge proof unavailable".into())}
    budget.check(0).map_err(|_|"S09 purge proof unavailable")?;
    let negative_pointer:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND effective_at_ms<0)",[&account_hash],|r|r.get(0)).map_err(|_|"S09 purge proof unavailable")?;
    if negative_pointer {return Err("S09 purge proof unavailable".into())}
    let meta:Option<String>=tx.query_row("SELECT purge_receipt_id FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND effective_at_ms>=0 AND purge_receipt_id GLOB 's05_quota_purge_receipt_*' LIMIT 1",[account_hash],|r|r.get(0)).optional().map_err(|_|"S09 purge proof unavailable")?;
    let Some(meta)=meta else {return Ok(false)};
    budget.check(meta.len()).map_err(|_|"S09 purge proof unavailable")?;
    let schema=purge_schema_hash(tx,budget)?;
    verify_purge_header(tx,account,&meta,&schema,budget)?;
    Ok(true)
}

// An absent locator is not proof of transfer: callers must still verify the
// original S06 custody. A present locator must bind its header and group.
fn verify_purge_transfer(tx:&Transaction<'_>,account:&str,source:&str,source_hash:&str,schema_hash:&[String],budget:&mut crate::repository::ResetFenceBudget)->Result<bool,String>{
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;
    let window=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota purge unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,&database,account)?;
    let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-transfer.v1",account,source)).map_err(|_|"S05 quota purge unavailable")?);
    let key=format!("s05_quota_batch_{}_{}_ref",window,id);
    let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
    let Some((hash,actual,context,at))=pointer else{return Ok(false)};
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    let direct:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;
    if actual!=key || at!=Some(0) || hash.as_deref()!=Some(crate::repository::sha256_hex(raw.as_bytes()).as_str()) || serde_json::to_string(&direct).map_err(|_|"S05 quota purge unavailable")?!=raw || direct.version!=1 || direct.window_account_sha256!=window || direct.max_at_ms!=0 || direct.entries.len()!=1 || direct.entries[0].key!=source || direct.entries[0].fact_sha256!=source_hash || context!=format!("s05-purge-purge:{}",direct.coverage_receipt_id){return Err("S05 quota purge unavailable".into())}
    let (coverage,request)=verify_purge_header(tx,account,&direct.coverage_receipt_id,schema_hash,budget)?;
    let ordinal=usize::try_from(direct.ordinal).map_err(|_|"S05 quota purge unavailable")?;
    let expected_hash=coverage.transfer_batch_sha256.get(ordinal).ok_or("S05 quota purge unavailable")?;
    let group_key=format!("s05_quota_batch_{}_{}_transfer_{:08}",window,request,direct.ordinal);
    let group_id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-transfer-batch.v1",account,&coverage.purge_receipt_id,direct.ordinal)).map_err(|_|"S05 quota purge unavailable")?);
    let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,group_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
    let group_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&group_key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(group_raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    let group:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&group_raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;
    let expected_len=if ordinal+1==coverage.transfer_batch_sha256.len(){((coverage.transfer_count.checked_sub(1).ok_or("S05 quota purge unavailable")?%64)+1) as usize}else{64};
    if pointer!=Some((Some(crate::repository::sha256_hex(group_raw.as_bytes())),group_key,context,Some(0))) || expected_hash!=&crate::repository::sha256_hex(group_raw.as_bytes()) || serde_json::to_string(&group).map_err(|_|"S05 quota purge unavailable")?!=group_raw || group.version!=1 || group.window_account_sha256!=window || group.coverage_receipt_id!=direct.coverage_receipt_id || group.ordinal!=direct.ordinal || group.max_at_ms!=0 || group.entries.len()!=expected_len || group.entries.windows(2).any(|v|v[0].key>=v[1].key) || group.entries.iter().filter(|e|e.key==source && e.fact_sha256==source_hash).count()!=1{return Err("S05 quota purge unavailable".into())}
    Ok(true)
}

// Account/time index is consulted before any purge metadata; only intersecting
// charge batches are read, and every selected batch must match its receipt.
pub(super) fn read_purge_window(tx:&Transaction<'_>,account:&str,now_ms:i64,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<crate::repository::s05_budget::RetainedCharge>,String>{
    let start=now_ms.checked_sub(86_400_000).ok_or("S05 quota purge unavailable")?;if start<0{return Err("S05 quota purge unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,&database,account)?;
    let window=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"S05 quota purge unavailable")?;
    let schema_hash=purge_schema_hash(tx,budget)?;
    let mut stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND effective_at_ms>?2 ORDER BY effective_at_ms,request_sha256 LIMIT 100001").map_err(|_|"S05 quota purge unavailable")?;
    let mut rows=stmt.query(params![account_hash,start]).map_err(|_|"S05 quota purge unavailable")?;let mut pointers=std::collections::BTreeMap::<String,(String,String,String,i64)>::new();let mut headers=Vec::new();
    while let Some(row)=rows.next().map_err(|_|"S05 quota purge unavailable")? {
        if pointers.len()>=100000{return Err("S05 quota purge unavailable".into())}
        let request:String=row.get(0).map_err(|_|"S05 quota purge unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"S05 quota purge unavailable")?;let key:String=row.get(2).map_err(|_|"S05 quota purge unavailable")?;let context:String=row.get(3).map_err(|_|"S05 quota purge unavailable")?;let at:i64=row.get(4).map_err(|_|"S05 quota purge unavailable")?;
        budget.check(request.len()+key.len()+context.len()+64).map_err(|_|"S05 quota purge unavailable")?;let hash=hash.ok_or("S05 quota purge unavailable")?;
        let valid=|v:&str|v.len()==64 && v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
        if at>now_ms || !valid(&request) || !valid(&hash) || (!key.starts_with("s05_quota_purge_receipt_") && !key.starts_with("s05_quota_batch_")){return Err("S05 quota purge unavailable".into())}
        if key.starts_with("s05_quota_purge_receipt_"){headers.push(key.clone())}if pointers.insert(key,(request,hash,context,at)).is_some(){return Err("S05 quota purge unavailable".into())}
    }
    drop(rows);drop(stmt);let mut consumed=std::collections::BTreeSet::new();let mut seen=std::collections::BTreeSet::new();let mut output=Vec::new();
    for meta in headers {
        let (request,header_hash,context,at)=pointers.get(&meta).ok_or("S05 quota purge unavailable")?.clone();consumed.insert(meta.clone());
        let (header,verified_request)=verify_purge_header(tx,account,&meta,&schema_hash,budget)?;
        let header_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&meta],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;
        if verified_request!=request || header_hash!=crate::repository::sha256_hex(header_raw.as_bytes()) || context!=format!("s05-purge-purge:{meta}") || at!=header.max_at_ms{return Err("S05 quota purge unavailable".into())}
        for (ordinal,max) in header.batch_max_at_ms.iter().enumerate(){
            if *max<=start {continue}
            let key=format!("s05_quota_batch_{}_{}_purge_{:08}",window,request,ordinal);
            let (actual_id,hash,batch_context,batch_at)=pointers.get(&key).ok_or("S05 quota purge unavailable")?.clone();consumed.insert(key.clone());
            let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-batch.v1",account,&header.purge_receipt_id,ordinal as u64)).map_err(|_|"S05 quota purge unavailable")?);
            let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=131072",[&key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
            let batch:PurgeChargeBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;
            let expected_len=if ordinal+1==header.batch_sha256.len(){((header.count.checked_sub(1).ok_or("S05 quota purge unavailable")?%64)+1) as usize}else{64};
            if actual_id!=id || batch_context!=context || batch_at!=*max || hash!=crate::repository::sha256_hex(raw.as_bytes()) || hash!=header.batch_sha256[ordinal] || serde_json::to_string(&batch).map_err(|_|"S05 quota purge unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=window || batch.coverage_receipt_id!=meta || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=*max || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota purge unavailable".into())}
            let mut actual_max=0;for entry in batch.entries {
                crate::repository::s05_budget::validate_charge(&entry.charge).map_err(|_|"S05 quota purge unavailable")?;
                if entry.charge.account_id!=account || crate::repository::s05_budget::charge_key(&entry.charge).map_err(|_|"S05 quota purge unavailable")?!=entry.key || !seen.insert(entry.key){return Err("S05 quota purge unavailable".into())}
                let effective=entry.charge.settled_at_ms.unwrap_or(entry.charge.reserved_at_ms);if effective>now_ms{return Err("S05 quota purge unavailable".into())}actual_max=actual_max.max(effective);if effective>start {output.push(entry.charge)}
            }
            if actual_max!=*max{return Err("S05 quota purge unavailable".into())}
        }
    }
    if consumed.len()!=pointers.len(){return Err("S05 quota purge unavailable".into())}Ok(output)
}

fn verify_purge_slot(tx:&Transaction<'_>,account:&str,terminal:&SlotTerminal,terminal_raw:&str,schema_hash:&[String],budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    let (coverage,request)=verify_purge_header(tx,account,&terminal.coverage_receipt_id,schema_hash,budget)?;
    let ordinal=usize::try_from(terminal.slot_batch_ordinal).map_err(|_|"S05 quota purge unavailable")?;
    let expected=coverage.slot_batch_sha256.get(ordinal).ok_or("S05 quota purge unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,&coverage.database_id,account)?;
    let key=format!("s05_quota_batch_{}_{}_slot_{:08}",coverage.window_account_sha256,request,terminal.slot_batch_ordinal);
    let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-slot-batch.v1",account,&coverage.purge_receipt_id,terminal.slot_batch_ordinal)).map_err(|_|"S05 quota purge unavailable")?);
    let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;
    let hash=crate::repository::sha256_hex(raw.as_bytes());let expected_len=if ordinal+1==coverage.slot_batch_sha256.len(){((coverage.slot_count.checked_sub(1).ok_or("S05 quota purge unavailable")?%64)+1) as usize}else{64};
    if pointer!=Some((Some(hash.clone()),key,format!("s05-purge-purge:{}",terminal.coverage_receipt_id),Some(0))) || &hash!=expected || serde_json::to_string(&batch).map_err(|_|"S05 quota purge unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=coverage.window_account_sha256 || batch.coverage_receipt_id!=terminal.coverage_receipt_id || batch.ordinal!=terminal.slot_batch_ordinal || batch.max_at_ms!=0 || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key) || batch.entries.iter().filter(|v|v.key==terminal.slot && v.fact_sha256==crate::repository::sha256_hex(terminal_raw.as_bytes())).count()!=1{return Err("S05 quota purge unavailable".into())}
    Ok(())
}

#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct M5PurgeCoverage {
    version:u32,database_id:String,account_id:String,window_account_sha256:String,
    purge_receipt_id:String,purge_request_sha256:String,parent_header_id:String,
    count:u64,max_at_ms:i64,batch_max_at_ms:Vec<i64>,batch_sha256:Vec<String>,
    slot_count:u64,slot_batch_sha256:Vec<String>,
}
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct M5PurgeBatch {
    version:u32,window_account_sha256:String,coverage_receipt_id:String,ordinal:u64,max_at_ms:i64,
    entries:Vec<super::request_purge::SkillUsage>,
}
struct M5PurgePlan {
    meta:String,request:String,account_hash:String,context:String,coverage:M5PurgeCoverage,
    batches:Vec<M5PurgeBatch>,
}
// The actual existing M5 manifest is independently checked before this plan
// is written; this function prepares only its deterministic bounded index.
fn prepare_m5_purge_plan(database:&str,account:&str,receipt:&str,request_hash:&str,parent:&str,usage:&[super::request_purge::SkillUsage],budget:&mut crate::repository::ResetFenceBudget)->Result<M5PurgePlan,String>{
    if usage.len()>128 || usage.windows(2).any(|v|v[0].operation_sha256>=v[1].operation_sha256){return Err("M5 quota purge unavailable".into())}
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::M5Quota,database,account)?;
    let window_hash=crate::repository::s05_budget::window_account_digest(database,account).map_err(|_|"M5 quota purge unavailable")?;
    let request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-purge-window.v1",account,receipt)).map_err(|_|"M5 quota purge unavailable")?);
    let meta=format!("s05_quota_m5purge_receipt_{account_hash}_{request}");let context=format!("m5-quota-purge:{meta}");
    let mut batches=Vec::new();let mut batch_sha256=Vec::new();let mut batch_max_at_ms=Vec::new();
    for (ordinal,group) in usage.chunks(64).enumerate(){
        let max=group.iter().map(|v|v.settled_at_ms).max().ok_or("M5 quota purge unavailable")?;
        if max<0{return Err("M5 quota purge unavailable".into())}
        let batch=M5PurgeBatch{version:1,window_account_sha256:window_hash.clone(),coverage_receipt_id:meta.clone(),ordinal:ordinal as u64,max_at_ms:max,entries:group.to_vec()};
        let raw=serde_json::to_vec(&batch).map_err(|_|"M5 quota purge unavailable")?;if raw.len()>131072{return Err("M5 quota purge unavailable".into())}budget.check(raw.len()).map_err(|_|"M5 quota purge unavailable")?;
        batch_sha256.push(crate::repository::sha256_hex(&raw));batch_max_at_ms.push(max);batches.push(batch);
    }
    let max_at_ms=batch_max_at_ms.iter().copied().max().unwrap_or(0);
    let coverage=M5PurgeCoverage{version:1,database_id:database.into(),account_id:account.into(),window_account_sha256:window_hash,purge_receipt_id:receipt.into(),purge_request_sha256:request_hash.into(),parent_header_id:parent.into(),count:usage.len() as u64,max_at_ms,batch_max_at_ms,batch_sha256,slot_count:0,slot_batch_sha256:Vec::new()};
    Ok(M5PurgePlan{meta,request,account_hash,context,coverage,batches})
}
fn write_m5_purge_plan(tx:&Transaction<'_>,owner:&str,account:&str,plan:&M5PurgePlan,manifest:Option<&serde_json::Value>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    let expected:Vec<_>=plan.batches.iter().flat_map(|v|v.entries.iter().cloned()).collect();
    match (expected.is_empty(),manifest) {
        (true,None)=>{},
        (false,Some(value))=>{let actual=super::request_purge::verify_skill(tx,&plan.coverage.database_id,account,&plan.coverage.purge_receipt_id,value,budget)?;if actual!=expected{return Err("M5 quota purge unavailable".into())}},
        _=>return Err("M5 quota purge unavailable".into()),
    }
    let body=serde_json::to_string(&plan.coverage).map_err(|_|"M5 quota purge unavailable")?;budget.check(body.len()).map_err(|_|"M5 quota purge unavailable")?;
    tx.execute("INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES(?1,?2,NULL,?1,'s05 guarded m5 quota terminal',?3)",params![owner,plan.context,chrono::Utc::now().to_rfc3339()]).map_err(|_|"M5 quota purge unavailable")?;
    let pointer=super::request_purge::RequestHash{request_sha256:plan.request.clone(),body_sha256:Some(crate::repository::sha256_hex(body.as_bytes()))};
    super::request_purge::write_timed(tx,super::request_purge::Family::M5Quota,&plan.account_hash,&plan.meta,&plan.meta,&plan.context,&pointer,plan.coverage.max_at_ms)?;
    tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![plan.meta,body]).map_err(|_|"M5 quota purge unavailable")?;
    for batch in &plan.batches {
        let raw=serde_json::to_string(batch).map_err(|_|"M5 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota purge unavailable")?;
        let key=format!("s05_quota_batch_{}_{}_m5purge_{:08}",plan.coverage.window_account_sha256,plan.request,batch.ordinal);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-purge-batch.v1",account,&plan.coverage.purge_receipt_id,batch.ordinal)).map_err(|_|"M5 quota purge unavailable")?);
        let pointer=super::request_purge::RequestHash{request_sha256:id,body_sha256:Some(crate::repository::sha256_hex(raw.as_bytes()))};
        super::request_purge::write_timed(tx,super::request_purge::Family::M5Quota,&plan.account_hash,&plan.meta,&key,&plan.context,&pointer,batch.max_at_ms)?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![key,raw]).map_err(|_|"M5 quota purge unavailable")?;
    }
    if tx.execute("DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND reason='s05 guarded m5 quota terminal'",params![owner,plan.context]).map_err(|_|"M5 quota purge unavailable")?!=1{return Err("M5 quota purge unavailable".into())}Ok(())
}


fn verify_m5_purge_header(tx:&Transaction<'_>,account:&str,parent:&str,coverage:&PurgeCoverage,budget:&mut crate::repository::ResetFenceBudget)->Result<(M5PurgeCoverage,String),String>{
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::M5Quota,&coverage.database_id,account)?;
    let request=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-purge-window.v1",account,&coverage.purge_receipt_id)).map_err(|_|"M5 quota purge unavailable")?);
    let meta=format!("s05_quota_m5purge_receipt_{account_hash}_{request}");let context=format!("m5-quota-purge:{meta}");
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&meta],|r|r.get(0)).map_err(|_|"M5 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota purge unavailable")?;
    let header:M5PurgeCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"M5 quota purge unavailable")?).map_err(|_|"M5 quota purge unavailable")?;
    let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='m5_quota_window_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"M5 quota purge unavailable")?;
    let hash=crate::repository::sha256_hex(raw.as_bytes());let valid_hash=|v:&str|v.len()==64&&v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
    if coverage.m5_header_id!=meta || coverage.m5_header_sha256!=hash || pointer!=Some((Some(hash),meta.clone(),context,Some(header.max_at_ms))) || serde_json::to_string(&header).map_err(|_|"M5 quota purge unavailable")?!=raw || header.version!=1 || header.database_id!=coverage.database_id || header.account_id!=account || header.window_account_sha256!=coverage.window_account_sha256 || header.purge_receipt_id!=coverage.purge_receipt_id || header.purge_request_sha256!=coverage.purge_request_sha256 || header.parent_header_id!=parent || header.count>128 || header.slot_count!=0 || !header.slot_batch_sha256.is_empty() || header.batch_sha256.len() as u64!=(header.count+63)/64 || header.batch_max_at_ms.len()!=header.batch_sha256.len() || header.batch_max_at_ms.iter().any(|v|*v<0) || header.max_at_ms!=header.batch_max_at_ms.iter().copied().max().unwrap_or(0) || header.batch_sha256.iter().any(|v|!valid_hash(v)){return Err("M5 quota purge unavailable".into())}
    Ok((header,request))
}

// The M5 purge adapter points at the unchanged M5 receipt. Only indexed
// intersecting batches are counted and live backing is positively checked.
pub(super) fn read_m5_purge_window(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Vec<super::request_purge::SkillUsage>,String>{
    if view.current_owner()!=owner || view.account_id()!=account{return Err("M5 quota purge unavailable".into())}
    let start=now_ms.checked_sub(86_400_000).ok_or("M5 quota purge unavailable")?;if start<0{return Err("M5 quota purge unavailable".into())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"M5 quota purge unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::M5Quota,&database,account)?;
    let window=crate::repository::s05_budget::window_account_digest(&database,account).map_err(|_|"M5 quota purge unavailable")?;let schema_hash=purge_schema_hash(tx,budget)?;
    let mut stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='m5_quota_window_v1' AND account_sha256=?1 AND effective_at_ms>?2 AND (purge_receipt_id GLOB 's05_quota_m5purge_receipt_*' OR purge_receipt_id GLOB 's05_quota_batch_*_m5purge_[0-9]*') ORDER BY effective_at_ms,request_sha256 LIMIT 100001").map_err(|_|"M5 quota purge unavailable")?;
    let mut rows=stmt.query(params![account_hash,start]).map_err(|_|"M5 quota purge unavailable")?;let mut pointers=std::collections::BTreeMap::<String,(String,String,String,i64)>::new();let mut headers=Vec::new();
    while let Some(row)=rows.next().map_err(|_|"M5 quota purge unavailable")? {
        if pointers.len()>=100000{return Err("M5 quota purge unavailable".into())}
        let request:String=row.get(0).map_err(|_|"M5 quota purge unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"M5 quota purge unavailable")?;let key:String=row.get(2).map_err(|_|"M5 quota purge unavailable")?;let context:String=row.get(3).map_err(|_|"M5 quota purge unavailable")?;let at:i64=row.get(4).map_err(|_|"M5 quota purge unavailable")?;budget.check(request.len()+key.len()+context.len()+64).map_err(|_|"M5 quota purge unavailable")?;
        let hash=hash.ok_or("M5 quota purge unavailable")?;let valid=|v:&str|v.len()==64&&v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
        if at>now_ms || !valid(&request) || !valid(&hash) || (!key.starts_with("s05_quota_m5purge_receipt_") && !key.starts_with("s05_quota_batch_")){return Err("M5 quota purge unavailable".into())}
        if key.starts_with("s05_quota_m5purge_receipt_"){headers.push(key.clone())}if pointers.insert(key,(request,hash,context,at)).is_some(){return Err("M5 quota purge unavailable".into())}
    }
    drop(rows);drop(stmt);let mut consumed=std::collections::BTreeSet::new();let mut seen=std::collections::BTreeSet::new();let mut output=Vec::new();
    for meta in headers {
        let (request,hash,context,at)=pointers.get(&meta).ok_or("M5 quota purge unavailable")?.clone();consumed.insert(meta.clone());
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&meta],|r|r.get(0)).map_err(|_|"M5 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota purge unavailable")?;
        let untrusted:M5PurgeCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"M5 quota purge unavailable")?).map_err(|_|"M5 quota purge unavailable")?;
        let (parent,_)=verify_purge_header(tx,account,&untrusted.parent_header_id,&schema_hash,budget)?;
        let (header,expected_request)=verify_m5_purge_header(tx,account,&untrusted.parent_header_id,&parent,budget)?;
        if expected_request!=request || parent.m5_header_id!=meta || hash!=crate::repository::sha256_hex(raw.as_bytes()) || context!=format!("m5-quota-purge:{meta}") || at!=header.max_at_ms{return Err("M5 quota purge unavailable".into())}
        let receipt_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=65536",[&header.purge_receipt_id],|r|r.get(0)).map_err(|_|"M5 quota purge unavailable")?;budget.check(receipt_raw.len()).map_err(|_|"M5 quota purge unavailable")?;
        let receipt=crate::field_authority::parse_unique_json(&receipt_raw).map_err(|_|"M5 quota purge unavailable")?;
        let usage=super::request_purge::verify_skill(tx,&database,account,&header.purge_receipt_id,&receipt["m5"],budget)?;
        if usage.len()!=header.count as usize{return Err("M5 quota purge unavailable".into())}
        let mut expected=std::collections::BTreeMap::new();for item in usage {let id=item.operation_sha256.clone();if expected.insert(id,item).is_some(){return Err("M5 quota purge unavailable".into())}}
        for (ordinal,max) in header.batch_max_at_ms.iter().enumerate(){
            if *max<=start {continue}
            let key=format!("s05_quota_batch_{}_{}_m5purge_{:08}",window,request,ordinal);let (actual_id,hash,batch_context,batch_at)=pointers.get(&key).ok_or("M5 quota purge unavailable")?.clone();consumed.insert(key.clone());
            let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-purge-batch.v1",account,&header.purge_receipt_id,ordinal as u64)).map_err(|_|"M5 quota purge unavailable")?);
            let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=131072",[&key],|r|r.get(0)).map_err(|_|"M5 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota purge unavailable")?;
            let batch:M5PurgeBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"M5 quota purge unavailable")?).map_err(|_|"M5 quota purge unavailable")?;
            let expected_len=if ordinal+1==header.batch_sha256.len(){((header.count.checked_sub(1).ok_or("M5 quota purge unavailable")?%64)+1) as usize}else{64};
            if actual_id!=id || batch_context!=context || batch_at!=*max || hash!=crate::repository::sha256_hex(raw.as_bytes()) || hash!=header.batch_sha256[ordinal] || serde_json::to_string(&batch).map_err(|_|"M5 quota purge unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=window || batch.coverage_receipt_id!=meta || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=*max || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].operation_sha256>=v[1].operation_sha256){return Err("M5 quota purge unavailable".into())}
            let mut actual_max=0;for item in batch.entries {
                if expected.get(&item.operation_sha256)!=Some(&item) || !seen.insert(item.operation_sha256.clone()) || item.settled_at_ms>now_ms{return Err("M5 quota purge unavailable".into())}
                actual_max=actual_max.max(item.settled_at_ms);let backed=crate::repository::s05_skill_draft::verified_purge_live_backing_in(tx,owner,account,&item,view,budget).map_err(|_|"M5 quota purge unavailable")?;
                if !backed && item.settled_at_ms>start {output.push(item)}
            }
            if actual_max!=*max{return Err("M5 quota purge unavailable".into())}
        }
    }
    if consumed.len()!=pointers.len(){return Err("M5 quota purge unavailable".into())}Ok(output)
}

// Full lifecycle/replay verification. Admission uses the separate indexed
// window reader, never this account-history closure.
pub(super) fn verify_purge_manifest_all(tx:&Transaction<'_>,account:&str,meta:&str,now_ms:i64,schema_hash:&[String],budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    let (coverage,request)=verify_purge_header(tx,account,meta,schema_hash,budget)?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,&coverage.database_id,account)?;
    let context=format!("s05-purge-purge:{meta}");let window=&coverage.window_account_sha256;
    let mut seen=std::collections::BTreeSet::new();
    for (ordinal,expected_hash) in coverage.batch_sha256.iter().enumerate(){
        let key=format!("s05_quota_batch_{}_{}_purge_{ordinal:08}",window,request);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-batch.v1",account,&coverage.purge_receipt_id,ordinal as u64)).map_err(|_|"S05 quota purge unavailable")?);
        let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=131072",[&key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
        let batch:PurgeChargeBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;
        let expected_len=if ordinal+1==coverage.batch_sha256.len(){((coverage.count.checked_sub(1).ok_or("S05 quota purge unavailable")?%64)+1) as usize}else{64};
        let hash=crate::repository::sha256_hex(raw.as_bytes());let mut max=0i64;
        if pointer!=Some((Some(hash.clone()),key,context.clone(),Some(batch.max_at_ms))) || &hash!=expected_hash || serde_json::to_string(&batch).map_err(|_|"S05 quota purge unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=*window || batch.coverage_receipt_id!=meta || batch.ordinal!=ordinal as u64 || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota purge unavailable".into())}
        for entry in batch.entries {crate::repository::s05_budget::validate_charge(&entry.charge).map_err(|_|"S05 quota purge unavailable")?;let at=entry.charge.settled_at_ms.unwrap_or(entry.charge.reserved_at_ms);if at>now_ms || entry.charge.account_id!=account || crate::repository::s05_budget::charge_key(&entry.charge).map_err(|_|"S05 quota purge unavailable")?!=entry.key || !seen.insert(entry.key){return Err("S05 quota purge unavailable".into())}max=max.max(at);}
        if max!=batch.max_at_ms || coverage.batch_max_at_ms[ordinal]!=max{return Err("S05 quota purge unavailable".into())}
    }
    let mut sources=std::collections::BTreeSet::new();
    for (ordinal,expected_hash) in coverage.transfer_batch_sha256.iter().enumerate(){
        let key=format!("s05_quota_batch_{}_{}_transfer_{ordinal:08}",window,request);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-transfer-batch.v1",account,&coverage.purge_receipt_id,ordinal as u64)).map_err(|_|"S05 quota purge unavailable")?);
        let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
        let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;let hash=crate::repository::sha256_hex(raw.as_bytes());
        let expected_len=if ordinal+1==coverage.transfer_batch_sha256.len(){((coverage.transfer_count.checked_sub(1).ok_or("S05 quota purge unavailable")?%64)+1) as usize}else{64};
        if pointer!=Some((Some(hash.clone()),key,context.clone(),Some(0))) || &hash!=expected_hash || serde_json::to_string(&batch).map_err(|_|"S05 quota purge unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=*window || batch.coverage_receipt_id!=meta || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=0 || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota purge unavailable".into())}
        for entry in batch.entries {
            if !sources.insert(entry.key.clone()){return Err("S05 quota purge unavailable".into())}
            let source_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=262144",[&entry.key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(source_raw.len()).map_err(|_|"S05 quota purge unavailable")?;
            let source:ResetCoverage=serde_json::from_value(crate::field_authority::parse_unique_json(&source_raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;
            let family=if entry.key.starts_with("s05_quota_reset_receipt_"){super::request_purge::Family::S05Reset}else if entry.key.starts_with("s05_quota_m5_receipt_"){super::request_purge::Family::M5Quota}else{return Err("S05 quota purge unavailable".into())};
            let source_account=super::request_purge::account_hash(family,&coverage.database_id,account)?;
            let source_request=match family {super::request_purge::Family::S05Reset=>reset_pointer(account,&source.reset_receipt_id)?,super::request_purge::Family::M5Quota=>crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-reset-window.v1",account,&source.reset_receipt_id)).map_err(|_|"S05 quota purge unavailable")?),_=>return Err("S05 quota purge unavailable".into())};
            let expected_key=match family {super::request_purge::Family::S05Reset=>format!("s05_quota_reset_receipt_{source_account}_{source_request}"),super::request_purge::Family::M5Quota=>format!("s05_quota_m5_receipt_{source_account}_{source_request}"),_=>return Err("S05 quota purge unavailable".into())};
            let (family_name,source_context)=match family {super::request_purge::Family::S05Reset=>("s05_s08_quota_reset_v1",format!("s05-reset-purge:{expected_key}")),super::request_purge::Family::M5Quota=>("m5_quota_window_v1",format!("m5-quota-purge:{expected_key}")),_=>return Err("S05 quota purge unavailable".into())};
            let source_pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family=?1 AND account_sha256=?2 AND request_sha256=?3",params![family_name,source_account,source_request],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
            if entry.key!=expected_key || source.version!=1 || source.database_id!=coverage.database_id || source.account_id!=account || source.window_account_sha256!=*window || serde_json::to_string(&source).map_err(|_|"S05 quota purge unavailable")?!=source_raw || crate::repository::sha256_hex(source_raw.as_bytes())!=entry.fact_sha256 || source_pointer!=Some((Some(entry.fact_sha256.clone()),expected_key,source_context,Some(source.max_at_ms))){return Err("S05 quota purge unavailable".into())}
            let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-transfer.v1",account,&entry.key)).map_err(|_|"S05 quota purge unavailable")?);
            let direct_key=format!("s05_quota_batch_{}_{}_ref",window,id);
            let direct:QuotaBatch=QuotaBatch{version:1,window_account_sha256:window.clone(),coverage_receipt_id:meta.into(),ordinal:ordinal as u64,max_at_ms:0,entries:vec![QuotaBatchEntry{key:entry.key,fact_sha256:entry.fact_sha256}]};
            let direct_raw=serde_json::to_string(&direct).map_err(|_|"S05 quota purge unavailable")?;budget.check(direct_raw.len()).map_err(|_|"S05 quota purge unavailable")?;
            let ptr:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
            let stored:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&direct_key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;
            if ptr!=Some((Some(crate::repository::sha256_hex(direct_raw.as_bytes())),direct_key,context.clone(),Some(0))) || stored!=direct_raw{return Err("S05 quota purge unavailable".into())}
        }
    }
    let slot_account=super::request_purge::account_hash(super::request_purge::Family::S05Slot,&coverage.database_id,account)?;
    let mut seen_slots=std::collections::BTreeSet::new();
    for (ordinal,expected_hash) in coverage.slot_batch_sha256.iter().enumerate(){
        let key=format!("s05_quota_batch_{}_{}_slot_{ordinal:08}",window,request);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.quota-purge-slot-batch.v1",account,&coverage.purge_receipt_id,ordinal as u64)).map_err(|_|"S05 quota purge unavailable")?);
        let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND request_sha256=?2",params![account_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=16384",[&key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
        let batch:QuotaBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;let hash=crate::repository::sha256_hex(raw.as_bytes());
        let expected_len=if ordinal+1==coverage.slot_batch_sha256.len(){((coverage.slot_count.checked_sub(1).ok_or("S05 quota purge unavailable")?%64)+1) as usize}else{64};
        if pointer!=Some((Some(hash.clone()),key,context.clone(),Some(0))) || &hash!=expected_hash || serde_json::to_string(&batch).map_err(|_|"S05 quota purge unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=*window || batch.coverage_receipt_id!=meta || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=0 || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].key>=v[1].key){return Err("S05 quota purge unavailable".into())}
        for entry in batch.entries {
            if !seen_slots.insert(entry.key.clone()){return Err("S05 quota purge unavailable".into())}
            let id=slot_pointer(account,&entry.key)?;let slot_key=format!("s05_quota_slot_terminal_{slot_account}_{id}");
            let ptr:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='s05_reserve_terminal_v1' AND account_sha256=?1 AND request_sha256=?2",params![slot_account,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"S05 quota purge unavailable")?;
            let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=8192",[&slot_key],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;
            let terminal:SlotTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"S05 quota purge unavailable")?).map_err(|_|"S05 quota purge unavailable")?;let hash=crate::repository::sha256_hex(raw.as_bytes());
            let valid=|v:&str|v.len()==64&&v.bytes().all(|x|x.is_ascii_digit()||(b'a'..=b'f').contains(&x));
            if ptr!=Some((Some(hash.clone()),slot_key.clone(),format!("s05-slot-purge:{slot_key}"),None)) || hash!=entry.fact_sha256 || serde_json::to_string(&terminal).map_err(|_|"S05 quota purge unavailable")?!=raw || terminal.version!=1 || terminal.account_id!=account || terminal.workspace_id.is_empty() || terminal.workspace_id.len()>512 || terminal.workspace_id.chars().any(char::is_control) || terminal.captured_owner.is_empty() || terminal.captured_owner.len()>512 || terminal.captured_owner.chars().any(char::is_control) || terminal.slot.len()!=75 || !terminal.slot.starts_with("s05reserve_") || !valid(&terminal.slot[11..]) || terminal.slot!=entry.key || terminal.coverage_receipt_id!=meta || terminal.slot_batch_ordinal!=ordinal as u64 || !valid(&terminal.original_receipt_sha256) || !terminal.result_receipt_sha256.as_deref().is_some_and(valid) || !terminal.attempt_sha256.as_deref().is_some_and(valid) || !terminal.lease_epoch.is_some_and(|v|v>0) || !matches!(terminal.disposition.as_str(),"settled"|"revoked"){return Err("S05 quota purge unavailable".into())}
        }
    }
    let (m5,m5_request)=verify_m5_purge_header(tx,account,meta,&coverage,budget)?;
    let receipt_raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=65536",[&coverage.purge_receipt_id],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;budget.check(receipt_raw.len()).map_err(|_|"S05 quota purge unavailable")?;
    let receipt=crate::field_authority::parse_unique_json(&receipt_raw).map_err(|_|"S05 quota purge unavailable")?;
    let expected=if m5.count==0 {if !receipt["m5"].is_null(){return Err("M5 quota purge unavailable".into())}Vec::new()} else {super::request_purge::verify_skill(tx,&coverage.database_id,account,&coverage.purge_receipt_id,&receipt["m5"],budget)?};
    if expected.len()!=m5.count as usize{return Err("M5 quota purge unavailable".into())}
    let m5_hash=super::request_purge::account_hash(super::request_purge::Family::M5Quota,&coverage.database_id,account)?;let m5_context=format!("m5-quota-purge:{}",coverage.m5_header_id);let mut found=Vec::new();
    for (ordinal,expected_hash) in m5.batch_sha256.iter().enumerate(){
        let key=format!("s05_quota_batch_{}_{}_m5purge_{ordinal:08}",window,m5_request);
        let id=crate::repository::sha256_hex(&serde_json::to_vec(&("s05.m5-purge-batch.v1",account,&coverage.purge_receipt_id,ordinal as u64)).map_err(|_|"M5 quota purge unavailable")?);
        let pointer:Option<(Option<String>,String,String,Option<i64>)>=tx.query_row("SELECT body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal WHERE family='m5_quota_window_v1' AND account_sha256=?1 AND request_sha256=?2",params![m5_hash,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|"M5 quota purge unavailable")?;
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=131072",[&key],|r|r.get(0)).map_err(|_|"M5 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota purge unavailable")?;
        let batch:M5PurgeBatch=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|"M5 quota purge unavailable")?).map_err(|_|"M5 quota purge unavailable")?;let hash=crate::repository::sha256_hex(raw.as_bytes());
        let expected_len=if ordinal+1==m5.batch_sha256.len(){((m5.count.checked_sub(1).ok_or("M5 quota purge unavailable")?%64)+1) as usize}else{64};
        let max=batch.entries.iter().map(|v|v.settled_at_ms).max().ok_or("M5 quota purge unavailable")?;
        if pointer!=Some((Some(hash.clone()),key,m5_context.clone(),Some(max))) || &hash!=expected_hash || serde_json::to_string(&batch).map_err(|_|"M5 quota purge unavailable")?!=raw || batch.version!=1 || batch.window_account_sha256!=*window || batch.coverage_receipt_id!=coverage.m5_header_id || batch.ordinal!=ordinal as u64 || batch.max_at_ms!=max || max>now_ms || m5.batch_max_at_ms[ordinal]!=max || batch.entries.len()!=expected_len || batch.entries.windows(2).any(|v|v[0].operation_sha256>=v[1].operation_sha256){return Err("M5 quota purge unavailable".into())}found.extend(batch.entries);
    }
    if found!=expected{return Err("M5 quota purge unavailable".into())}Ok(())
}


fn prior_purge_binding(tx:&Transaction<'_>,account:&str,now_ms:i64,budget:&mut crate::repository::ResetFenceBudget)->Result<String,String>{
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S05 quota purge unavailable")?;
    let account_hash=super::request_purge::account_hash(super::request_purge::Family::S05Purge,&database,account)?;let schema_hash=purge_schema_hash(tx,budget)?;
    let mut stmt=tx.prepare("SELECT request_sha256,body_sha256,purge_receipt_id,purge_context_id,effective_at_ms FROM native_request_purge_terminal INDEXED BY s05_quota_terminal_window_v1 WHERE family='s05_s08_quota_v1' AND account_sha256=?1 AND effective_at_ms>=0 AND purge_receipt_id GLOB 's05_quota_purge_receipt_*' ORDER BY request_sha256 LIMIT 100001").map_err(|_|"S05 quota purge unavailable")?;
    let mut rows=stmt.query([&account_hash]).map_err(|_|"S05 quota purge unavailable")?;let mut headers=std::collections::BTreeMap::new();
    while let Some(row)=rows.next().map_err(|_|"S05 quota purge unavailable")? {
        if headers.len()>=100000{return Err("S05 quota purge unavailable".into())}
        let request:String=row.get(0).map_err(|_|"S05 quota purge unavailable")?;let hash:Option<String>=row.get(1).map_err(|_|"S05 quota purge unavailable")?;let meta:String=row.get(2).map_err(|_|"S05 quota purge unavailable")?;let context:String=row.get(3).map_err(|_|"S05 quota purge unavailable")?;let at:i64=row.get(4).map_err(|_|"S05 quota purge unavailable")?;budget.check(request.len()+meta.len()+context.len()+64).map_err(|_|"S05 quota purge unavailable")?;
        let hash=hash.ok_or("S05 quota purge unavailable")?;if at>now_ms || context!=format!("s05-purge-purge:{meta}") || headers.insert(meta,(request,hash,at)).is_some(){return Err("S05 quota purge unavailable".into())}
    }
    drop(rows);drop(stmt);
    let mut contexts=tx.prepare("SELECT DISTINCT purge_context_id FROM native_request_purge_terminal WHERE family='s05_s08_quota_v1' AND account_sha256=?1 ORDER BY purge_context_id LIMIT 100001").map_err(|_|"S05 quota purge unavailable")?;
    let mut rows=contexts.query([&account_hash]).map_err(|_|"S05 quota purge unavailable")?;let mut count=0usize;while let Some(row)=rows.next().map_err(|_|"S05 quota purge unavailable")? {
        count+=1;if count>100000{return Err("S05 quota purge unavailable".into())}let context:String=row.get(0).map_err(|_|"S05 quota purge unavailable")?;budget.check(context.len()).map_err(|_|"S05 quota purge unavailable")?;let meta=context.strip_prefix("s05-purge-purge:").ok_or("S05 quota purge unavailable")?;if !headers.contains_key(meta){return Err("S05 quota purge unavailable".into())}
    }drop(rows);drop(contexts);
    for meta in headers.keys(){verify_purge_manifest_all(tx,account,meta,now_ms,&schema_hash,budget)?;}
    let raw=serde_json::to_vec(&("s05.quota-prior-purge.v1",account,&headers)).map_err(|_|"S05 quota purge unavailable")?;budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;Ok(crate::repository::sha256_hex(&raw))
}
pub(super) fn purge_capture_digest(account:&str,capture:&PurgeCapture,budget:&mut crate::repository::ResetFenceBudget)->Result<String,String>{
    let raw=serde_json::to_vec(&("s05.quota-purge-inventory.v1",account,&capture.prior_purge_binding,&capture.charges,&capture.reset_headers,&capture.reset_slots,&capture.new_slots,&capture.m5_facts,&capture.m5_headers)).map_err(|_|"S05 quota purge unavailable")?;
    budget.check(raw.len()).map_err(|_|"S05 quota purge unavailable")?;Ok(crate::repository::sha256_hex(&raw))
}

pub(super) fn verify_purge_manifest_in(tx:&Transaction<'_>,account:&str,meta:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String>{
    let schema=purge_schema_hash(tx,budget)?;verify_purge_manifest_all(tx,account,meta,chrono::Utc::now().timestamp_millis(),&schema,budget)
}

// Lifecycle-only complete M5 inventory, including pre-profile retained facts.
// The indexed subset must be exactly backed by the existing M5 fact owner.
fn verified_m5_lifecycle_inventory(tx:&Transaction<'_>,owner:&str,account:&str,now_ms:i64,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(Vec<crate::repository::s05_skill_draft::ResetTerminal>,Vec<ResetCoverage>),String>{
    let (indexed,headers)=read_m5_reset_all(tx,owner,account,now_ms,view,budget)?;
    let all=crate::repository::s05_skill_draft::unbacked_reset_terminals_bounded(tx,owner,account,budget).map_err(|_|"M5 quota unavailable")?;
    let mut known=std::collections::BTreeMap::new();for fact in &all {let raw=serde_json::to_vec(fact).map_err(|_|"M5 quota unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota unavailable")?;if known.insert(fact.job_id.clone(),crate::repository::sha256_hex(&raw)).is_some(){return Err("M5 quota unavailable".into())}}
    for fact in indexed {let raw=serde_json::to_vec(&fact).map_err(|_|"M5 quota unavailable")?;budget.check(raw.len()).map_err(|_|"M5 quota unavailable")?;if known.get(&fact.job_id)!=Some(&crate::repository::sha256_hex(&raw)){return Err("M5 quota unavailable".into())}}
    Ok((all,headers))
}
