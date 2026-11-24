//! Transaction-local journal cadence from the retained S05 usage ledger.
//! This is a scheduling fact, not a memory, erasure, or absolute-history proof.
use super::*;

pub(crate) fn enforce(tx:&Transaction<'_>, m:&S08ModelPrepareRequest, input:&S08CadencePolicy,
    budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if m.output_kind!="journal_synthesis" || m.mode!="model" || input.version!=1 || input.accepted_policy_input_json.len()>65536 {
        return Err(RepositoryError::NotFound)
    }
    budget.check(input.accepted_policy_input_json.len()).map_err(RepositoryError::Validation)?;
    crate::retrieval::rank_learning::projected_policy(&input.accepted_policy_input_json,&m.policy_projection.account.fingerprint)
        .map_err(RepositoryError::Validation)?;
    let body=crate::field_authority::parse_unique_json(&input.accepted_policy_input_json).map_err(RepositoryError::Validation)?;
    if body.get("can_manage_memory")!=Some(&json!(true)) {return Err(RepositoryError::NotFound)}
    let prefs=body.get("preferences").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    if prefs.get("memory_policy_consolidation_enabled")!=Some(&json!([true,true])) {return Err(RepositoryError::NotFound)}
    let days=prefs.get("memory_policy_consolidation_interval_days").and_then(Value::as_array)
        .filter(|v|v.len()==2 && v[0]==json!(true)).and_then(|v|v[1].as_u64())
        .filter(|v|(1..=365).contains(v)).ok_or(RepositoryError::NotFound)?;
    let owner=&m.selection.owner;let account=&m.selection.source_account_id;let workspace=&m.selection.workspace_id;
    // Any orphan reservation or relevant orphan attempt makes usage unknown.
    let broken:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q LEFT JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id LEFT JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND (j.job_id IS NULL OR a.attempt_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_attempts a WHERE a.owner_id=?1 AND a.job_id GLOB 's08job_*' AND NOT EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=a.owner_id AND j.job_id=a.job_id)) OR EXISTS(SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id WHERE a.owner_id=?1 AND j.kind='access_consolidation' AND j.workspace_key=?2 AND j.project_key='' AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q WHERE q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch))",params![owner,workspace],|r|r.get(0))?;
    if broken {return Err(RepositoryError::NotFound)}
    let now=chrono::Utc::now().timestamp_millis();if now<0 {return Err(RepositoryError::NotFound)}
    let mut latest=None::<i64>;
    let mut stmt=tx.prepare("SELECT q.job_id,q.attempt_id,q.state,q.settled_at_ms,q.reserved_calls,q.charged_calls,a.state FROM fm_v2_s05_reservations q JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND j.kind='access_consolidation' AND j.workspace_key=?2 AND j.project_key='' ORDER BY q.reserved_at_ms,q.attempt_id LIMIT 100001")?;
    let mut rows=stmt.query(params![owner,workspace])?;let mut count=0usize;
    while let Some(row)=rows.next()? {
        count+=1;if count>100000 {return Err(RepositoryError::NotFound)}
        let job:String=row.get(0)?;let attempt:String=row.get(1)?;let state:String=row.get(2)?;
        let settled:Option<i64>=row.get(3)?;let reserved:i64=row.get(4)?;let charged:i64=row.get(5)?;let attempt_state:String=row.get(6)?;
        budget.check(job.len()+attempt.len()+state.len()+attempt_state.len()+32).map_err(RepositoryError::Validation)?;
        if !(0..=1).contains(&reserved) || !(0..=1).contains(&charged) || charged>reserved {return Err(RepositoryError::NotFound)}
        if reserved==0 { continue } // rules-only: no model reservation
        if charged==0 && state=="settled" && settled.is_some() && matches!(attempt_state.as_str(),"cancelled"|"fenced") {
            continue // positively settled without a model charge
        }
        let id=format!("cs_s08model_{}",sha256_hex(&serde_json::to_vec(&(account,&job))?));
        let raw:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=262144 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s08.model-intent.v1'",params![owner,id],|r|r.get(0)).optional()?.flatten();
        let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let intent:S08ModelPrepareRequest=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if intent.selection.source_account_id!=*account || intent.selection.workspace_id!=*workspace || !matches!(intent.mode.as_str(),"model"|"hybrid") {return Err(RepositoryError::NotFound)}
        V2Repository::s08_model_intent_fact_scoped(tx,&intent,&job,owner,budget)?;
        if intent.output_kind!="journal_synthesis" {continue}
        // A second journal reservation can still issue before it becomes charged.
        // Defer until it settles (or is positively known unsent), rather than race it.
        if charged==0 || state!="settled" || !matches!(attempt_state.as_str(),"succeeded"|"failed_retryable"|"failed_terminal"|"cancelled"|"fenced") {return Err(RepositoryError::NotFound)}
        let timestamp=settled.filter(|n|*n>0 && *n<=now).ok_or(RepositoryError::NotFound)?;
        latest=Some(latest.map_or(timestamp,|v|v.max(timestamp)));
    }
    if let Some(last)=latest {
        let interval=i64::try_from(days).map_err(|_|RepositoryError::NotFound)?.checked_mul(86_400_000).ok_or(RepositoryError::NotFound)?;
        if now.checked_sub(last).ok_or(RepositoryError::NotFound)? < interval {return Err(RepositoryError::Validation("journal cadence not due".into()))}
    }
    // No matching retained reservation is a scoped empty ledger, not proof of never running.
    Ok(())
}
