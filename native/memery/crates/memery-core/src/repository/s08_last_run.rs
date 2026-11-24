//! Content-free last verified retained S08 model settlement.
use super::*;

#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S08LastRetainedRequest {
    pub version:u32,pub owner:String,pub workspace_id:String,pub account_id:String,
    pub policy_projection:PolicyPublicationRefs,
    #[serde(default="default_output_kind")] pub output_kind:String,
}

fn default_output_kind()->String {"summary".into()}

/// This proves only the newest retained settlement. Erased history may have
/// contained a later run; absence or incomplete custody remains unavailable.
pub(crate) fn last_retained_in_tx(tx:&Transaction<'_>,r:&S08LastRetainedRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.version!=1 || [&r.owner,&r.workspace_id,&r.account_id].iter().any(|v|v.is_empty() || v.trim()!=v.as_str() || v.len()>512) || view.current_owner()!=r.owner || view.account_id()!=r.account_id {return Err(RepositoryError::NotFound);}
    if !matches!(r.output_kind.as_str(),"summary"|"journal_synthesis"|"connection") {return Err(RepositoryError::NotFound);}
    require_guarded_epoch(tx)?;require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    let v1:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s08.model.v1'",[],|row|row.get(0)).optional()?;
    let v2:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s08.model.v2'",[],|row|row.get(0)).optional()?;
    if !crate::store::sqlite::s08_model_state(v1.as_deref(),v2.as_deref()).is_some_and(|v|v.0) {return Ok(json!({"version":1,"state":"unavailable"}));}
    let mut stmt=tx.prepare("SELECT j.job_id,a.attempt_id,q.settled_at_ms,q.reserved_input_tokens,q.reserved_output_tokens,q.charged_input_tokens,q.charged_output_tokens,CASE WHEN j.state IN ('failed_terminal','cancelled') AND length(CAST(j.result_json AS BLOB))<=8192 THEN j.result_json END FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.workspace_key=?2 AND j.project_key='' AND j.kind='access_consolidation' AND q.state='settled' AND q.reserved_calls=1 AND q.charged_calls=1 ORDER BY q.settled_at_ms DESC,j.job_id,a.attempt_id")?;
    let mut rows=stmt.query(params![r.owner,r.workspace_id])?;
    while let Some(row)=rows.next()? {
        let job:String=row.get(0)?;let attempt:String=row.get(1)?;let timestamp:Option<i64>=row.get(2)?;
        budget.check(job.len()+attempt.len()+8).map_err(RepositoryError::Validation)?;
        let settled_at=timestamp.filter(|n|*n>0).ok_or(RepositoryError::NotFound)?;
        let fact=s08_policy_lifecycle::settled_outcome(tx,&r.owner,&r.account_id,&r.workspace_id,&job,&attempt,view,budget)?;
        if !matches!(fact.intent.mode.as_str(),"model"|"hybrid") {return Err(RepositoryError::NotFound);}
        if fact.intent.output_kind!=r.output_kind {continue;}
        if !matches!(fact.state.as_str(),"staged"|"no_proposal"|"failed"|"cancelled") {return Err(RepositoryError::NotFound);}
        let mut run=json!({"settledAtMs":settled_at,"outcome":fact.state});
        let reserved=(row.get::<_,i64>(3)?,row.get::<_,i64>(4)?);
        let charged=(row.get::<_,i64>(5)?,row.get::<_,i64>(6)?);
        if reserved==(i64::from(fact.intent.max_input_tokens),i64::from(fact.intent.max_output_tokens))
            && (0..=reserved.0).contains(&charged.0) && (0..=reserved.1).contains(&charged.1) {
            // The terminal proof above verifies the original retained outcome.
            // Successful result receipts do not expose a supplier-usage basis.
            let reported=row.get::<_,Option<String>>(7)?.and_then(|raw|
                crate::field_authority::parse_unique_json(&raw).ok()).and_then(|outcome| {
                if !matches!(fact.state.as_str(),"failed"|"cancelled") || outcome["usage"]!="reported" {return None;}
                let input=outcome["reported_usage"]["input_tokens"].as_i64()?;
                let output=outcome["reported_usage"]["output_tokens"].as_i64()?;
                ((input,output)==charged).then(||json!({"input":input,"output":output}))
            });
            run["tokens"]=json!({"reserved":{"input":reserved.0,"output":reserved.1},
                "charged":{"input":charged.0,"output":charged.1},"supplierReported":reported});
        }
        return Ok(json!({"version":1,"state":"available","coverage":"retained_history","lastVerifiedRetainedRun":run}));
    }
    // The empty answer is deliberately narrow: any owner-level S08 receipt,
    // orphan custody row, or scoped consolidation job leaves absence unproved.
    budget.check(0).map_err(RepositoryError::Validation)?;
    let unproved:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND kind='access_consolidation') OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND substr(reason,1,4)='s08.') OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND (substr(kind,1,4)='s08_' OR substr(change_set_id,1,6)='cs_s08' OR substr(event_id,1,13)='outbox_cs_s08')) OR EXISTS(SELECT 1 FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id GLOB 's08job_*') OR EXISTS(SELECT 1 FROM fm_v2_s05_job_refs WHERE owner_id=?1 AND job_id GLOB 's08job_*') OR EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND id GLOB 's08candidate_*') OR EXISTS(SELECT 1 FROM memory_quarantine WHERE owner=?1 AND original_id GLOB 's08candidate_*') OR EXISTS(SELECT 1 FROM fm_v2_candidates WHERE owner_id=?1 AND candidate_id GLOB 's08candidate_*') OR EXISTS(SELECT 1 FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id GLOB 's08candidate_*') OR EXISTS(SELECT 1 FROM fm_v2_attempts a WHERE a.owner_id=?1 AND NOT EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=a.owner_id AND j.job_id=a.job_id)) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations q WHERE q.owner_id=?1 AND (NOT EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=q.owner_id AND j.job_id=q.job_id) OR NOT EXISTS(SELECT 1 FROM fm_v2_attempts a WHERE a.owner_id=q.owner_id AND a.attempt_id=q.attempt_id AND a.job_id=q.job_id AND a.lease_epoch=q.lease_epoch)))",params![r.owner,r.workspace_id],|row|row.get(0))?;
    if unproved {return Ok(json!({"version":1,"state":"unavailable"}));}
    Ok(json!({"version":1,"state":"empty","coverage":"retained_history"}))
}
