//! Closed content-free S05/S08 retained accounting facts.
//! Receipt, lifecycle and captured-owner custody must be verified by the caller.
use super::{sha256_hex, RepositoryError};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RetainedCharge {
    pub version: u32,
    pub account_id: String,
    pub captured_owner: String,
    pub job_kind: String,
    pub job_sha256: String,
    pub attempt_sha256: String,
    pub lease_epoch: i64,
    pub original_sha256: String,
    pub reserve_original_sha256: Option<String>,
    pub reserve_result_sha256: Option<String>,
    pub reserved_at_ms: i64,
    pub settled_at_ms: Option<i64>,
    pub charged_calls: i64,
    pub charged_seconds: i64,
    pub charged_input_tokens: i64,
    pub charged_output_tokens: i64,
    pub charge_basis: String,
}

fn hash_ok(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
}
fn text_ok(value: &str) -> bool {
    !value.is_empty() && value.trim() == value && value.len() <= 512 && !value.chars().any(char::is_control)
}

/// Shape and numeric bounds only; the caller must independently prove the
/// receipt, account custody and any claimed reported/known-zero disposition.
pub(crate) fn validate_charge(fact: &RetainedCharge) -> Result<(), RepositoryError> {
    if fact.version != 1 || !text_ok(&fact.account_id) || !text_ok(&fact.captured_owner)
        || !matches!(fact.job_kind.as_str(), "s05_host_user_capture" | "completed_turn_review" | "assistant_interpretation_review" | "access_consolidation")
        || !hash_ok(&fact.job_sha256) || !hash_ok(&fact.attempt_sha256) || !hash_ok(&fact.original_sha256)
        || fact.reserve_original_sha256.is_some()!=fact.reserve_result_sha256.is_some()
        || fact.reserve_original_sha256.as_ref().is_some_and(|v|!hash_ok(v))
        || fact.reserve_result_sha256.as_ref().is_some_and(|v|!hash_ok(v))
        || fact.lease_epoch <= 0 || fact.reserved_at_ms < 0
        || fact.settled_at_ms.is_none_or(|v| v < fact.reserved_at_ms)
        || !(0..=1).contains(&fact.charged_calls)
        || [fact.charged_seconds, fact.charged_input_tokens, fact.charged_output_tokens].iter().any(|v| !(0..=10_000_000).contains(v))
        || !matches!(fact.charge_basis.as_str(), "reported" | "reserved_bound" | "known_zero" | "unknown")
        || (fact.charge_basis == "known_zero" && (fact.charged_calls != 0 || fact.charged_seconds != 0 || fact.charged_input_tokens != 0 || fact.charged_output_tokens != 0))
    { return Err(RepositoryError::NotFound); }
    Ok(())
}

/// Domain-separated, account-bound opaque identity for retained facts.
fn scoped_digest(domain:&str, account:&str, id:&str)->Result<String,RepositoryError> {
    if !text_ok(account) || !text_ok(id) {return Err(RepositoryError::NotFound)}
    Ok(sha256_hex(&serde_json::to_vec(&(domain,account,id))?))
}
pub(crate) fn window_account_digest(database:&str,account:&str)->Result<String,RepositoryError> {
    scoped_digest("s05.quota-window-account.v1",database,account)
}
pub(crate) fn job_digest(account:&str,id:&str)->Result<String,RepositoryError> {
    scoped_digest("s05.quota-job.v1",account,id)
}
pub(crate) fn attempt_digest(account:&str,id:&str)->Result<String,RepositoryError> {
    scoped_digest("s05.quota-attempt.v1",account,id)
}

/// Stable per-attempt identity, independent of mutable owner alias.
pub(crate) fn charge_key(fact: &RetainedCharge) -> Result<String, RepositoryError> {
    validate_charge(fact)?;
    let raw = serde_json::to_vec(&("s05.retained-charge.v1", &fact.account_id, &fact.job_sha256, &fact.attempt_sha256, fact.lease_epoch))?;
    Ok(sha256_hex(&raw))
}

/// Deduplicate only an exactly matching live row; custody and the source
/// receipt are checked independently by the transaction owner.
pub(crate) fn same_live_charge(retained: &RetainedCharge, live: &RetainedCharge) -> Result<bool, RepositoryError> {
    validate_charge(retained)?; validate_charge(live)?;
    if charge_key(retained)? != charge_key(live)? { return Ok(false); }
    if retained.job_kind != live.job_kind || retained.original_sha256 != live.original_sha256
        || (retained.reserve_original_sha256.is_some() && (retained.reserve_original_sha256 != live.reserve_original_sha256 || retained.reserve_result_sha256 != live.reserve_result_sha256))
        || retained.reserved_at_ms != live.reserved_at_ms || retained.settled_at_ms != live.settled_at_ms
        || retained.charged_calls != live.charged_calls || retained.charged_seconds != live.charged_seconds
        || retained.charged_input_tokens != live.charged_input_tokens || retained.charged_output_tokens != live.charged_output_tokens
        || (retained.charge_basis != "unknown" && live.charge_basis != "unknown" && retained.charge_basis != live.charge_basis)
    { return Err(RepositoryError::NotFound); }
    Ok(true)
}

/// Charged values, not a claim that provider-reported usage was actual.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct ChargeTotals {
    pub calls: i64,
    pub seconds: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
}
impl ChargeTotals {
    pub(crate) fn add(&mut self, other: ChargeTotals) -> Result<(), RepositoryError> {
        if [other.calls, other.seconds, other.input_tokens, other.output_tokens].iter().any(|v| *v < 0) {
            return Err(RepositoryError::NotFound);
        }
        let next=ChargeTotals {
            calls:self.calls.checked_add(other.calls).ok_or(RepositoryError::NotFound)?,
            seconds:self.seconds.checked_add(other.seconds).ok_or(RepositoryError::NotFound)?,
            input_tokens:self.input_tokens.checked_add(other.input_tokens).ok_or(RepositoryError::NotFound)?,
            output_tokens:self.output_tokens.checked_add(other.output_tokens).ok_or(RepositoryError::NotFound)?,
        };
        *self=next;Ok(())
    }
    pub(crate) fn token_total(self) -> Result<i64, RepositoryError> {
        self.input_tokens.checked_add(self.output_tokens).ok_or(RepositoryError::NotFound)
    }
}

/// Add a verified fact only in the same rolling window as a live reservation.
/// Receipt, custody and duplicate suppression are transaction-owner duties.
pub(crate) fn add_retained_window(total: &mut ChargeTotals, fact: &RetainedCharge, now_ms: i64) -> Result<(), RepositoryError> {
    validate_charge(fact)?;
    let at = fact.settled_at_ms.unwrap_or(fact.reserved_at_ms);
    if now_ms < 0 || at > now_ms { return Err(RepositoryError::NotFound); }
    let start = now_ms.checked_sub(86_400_000).ok_or(RepositoryError::NotFound)?;
    if at > start {
        total.add(ChargeTotals { calls: fact.charged_calls, seconds: fact.charged_seconds,
            input_tokens: fact.charged_input_tokens, output_tokens: fact.charged_output_tokens })?;
    }
    Ok(())
}

/// The private S05 reserve original. Identity fields are facts to compare
/// against the existing guarded job/ref, not grants supplied by a caller.
#[derive(Clone, Debug, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S05ReserveOriginal {
    pub version: u32,
    pub owner: String,
    pub account_id: String,
    pub workspace_id: String,
    pub job_id: String,
    pub pre_attempt_count: u32,
    pub policy_projection: super::PolicyPublicationRefs,
    pub account_policy_input_json: String,
    pub expected: S05ExpectedJob,
    pub input_token_bound: Option<u32>,
    pub minimum_input_bytes: u32,
    pub limits: S05ReserveLimits,
}

/// Fresh transaction witness, intentionally excluded from the prepared original.
#[derive(Clone, Debug, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S05ReserveWitness {
    pub worker_id:String,
    pub coverage_generation:String,
    pub clock_generation:String,
    pub now_tick:i64,
}

#[derive(Clone, Debug, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S05ReserveRequest {
    pub version:u32,
    pub original:S05ReserveOriginal,
    pub witness:S05ReserveWitness,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S05ExpectedJob {
    pub kind: String,
    pub idempotency_key: String,
    pub input_hash: String,
    pub source_ref_json: String,
    pub admission_id: String,
    pub binding_fingerprint: String,
    pub policy_fingerprint: String,
    pub authority_fingerprint: String,
    pub connection_id: String,
    pub model_route_id: String,
    pub assignment_json: String,
    pub coverage_json: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S05ReserveLimits {
    pub quiet_seconds: u32,
    pub daily_calls: u32,
    pub daily_seconds: u32,
    pub daily_tokens: u32,
    pub job_seconds: u32,
    pub job_output_tokens: u32,
    pub owner_queue: u32,
    pub global_queue: u32,
    pub concurrency: u32,
}

/// One slot per account/job/pre-attempt, independent of worker/tick or
/// request digest. A stored slot with a different request digest conflicts.
pub(crate) fn reserve_slot(original: &S05ReserveOriginal) -> Result<String, RepositoryError> {
    if original.version != 1 || !text_ok(&original.owner) || !text_ok(&original.account_id)
        || !text_ok(&original.workspace_id) || !text_ok(&original.job_id)
        || original.pre_attempt_count >= 3 || (original.expected.kind=="assistant_interpretation_review" && original.pre_attempt_count!=0)
        || !matches!(original.expected.kind.as_str(), "completed_turn_review" | "assistant_interpretation_review")
        || !text_ok(&original.expected.idempotency_key) || !hash_ok(&original.expected.input_hash)
        || !text_ok(&original.expected.admission_id) || !hash_ok(&original.expected.binding_fingerprint)
        || !hash_ok(&original.expected.policy_fingerprint) || !hash_ok(&original.expected.authority_fingerprint)
        || !text_ok(&original.expected.connection_id) || !text_ok(&original.expected.model_route_id)
        || original.minimum_input_bytes == 0 || original.minimum_input_bytes > 1_000_000
        || original.input_token_bound.is_some_and(|v| v < original.minimum_input_bytes || v > 1_000_000)
        || serde_json::to_vec(original)?.len() > 65_536
    { return Err(RepositoryError::NotFound); }
    reserve_slot_for(&original.account_id,&original.job_id,original.pre_attempt_count)
}

#[derive(Clone, Debug, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S05ReserveStatus {
    pub version:u32,
    pub owner:String,
    pub account_id:String,
    pub workspace_id:String,
    pub job_id:String,
    pub pre_attempt_count:u32,
    pub policy_projection:super::PolicyPublicationRefs,
}

#[derive(Clone, Debug, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S05ReserveReadiness {
    pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,
    pub policy_projection:super::PolicyPublicationRefs,
}

/// Factual lookup uses the stable slot without requiring the original body;
/// a result is never authority to issue a new reservation.
pub(crate) fn status_slot(status:&S05ReserveStatus)->Result<String,RepositoryError> {
    if status.version!=1 || !text_ok(&status.owner) || !text_ok(&status.workspace_id) {return Err(RepositoryError::NotFound)}
    reserve_slot_for(&status.account_id,&status.job_id,status.pre_attempt_count)
}

pub(crate) fn reserve_slot_for(account:&str,job:&str,pre_attempt_count:u32)->Result<String,RepositoryError> {
    if !text_ok(account) || !text_ok(job) || pre_attempt_count>=3 {return Err(RepositoryError::NotFound)}
    let raw=serde_json::to_vec(&("s05.reserve.slot.v1",account,job,pre_attempt_count))?;
    Ok(format!("s05reserve_{}",sha256_hex(&raw)))
}

/// Transaction-local facts only. Account/receipt provenance must be verified
/// by the consumer before any value is included in an admission decision.
#[derive(Clone, Debug)]
pub(crate) struct LiveReservation {
    pub job_id: String,
    pub attempt_id: String,
    pub job_kind: String,
    pub original_sha256: String,
    pub lease_epoch: i64,
    pub state: String,
    pub reserved_at_ms: i64,
    pub settled_at_ms: Option<i64>,
    pub charged: ChargeTotals,
}

/// Use the exact offline-profile window index; late settlements are included.
/// Work is bounded by the transaction's progress budget and a hard row cap.
pub(crate) fn live_window_in(
    tx: &rusqlite::Transaction<'_>, owner: &str, now_ms: i64,
    budget: &mut super::ResetFenceBudget,
) -> Result<Vec<LiveReservation>, RepositoryError> {
    use rusqlite::{params, OptionalExtension};
    if !text_ok(owner) || now_ms < 0 { return Err(RepositoryError::NotFound); }
    let start = now_ms.checked_sub(86_400_000).ok_or(RepositoryError::NotFound)?;
    let future: Option<i64> = tx.query_row(
        "SELECT 1 FROM fm_v2_s05_reservations INDEXED BY s05_quota_window_v1 WHERE owner_id=?1 AND COALESCE(settled_at_ms,reserved_at_ms)>?2 LIMIT 1",
        params![owner, now_ms], |row| row.get(0)).optional()?;
    if future.is_some() { return Err(RepositoryError::NotFound); }
    let mut stmt = tx.prepare(
        "SELECT q.job_id,q.attempt_id,j.kind,j.input_hash,q.lease_epoch,q.state,q.reserved_at_ms,q.settled_at_ms,q.charged_calls,q.charged_seconds,q.charged_input_tokens,q.charged_output_tokens,a.attempt_id FROM fm_v2_s05_reservations q INDEXED BY s05_quota_window_v1 LEFT JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id LEFT JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND COALESCE(q.settled_at_ms,q.reserved_at_ms)>?2 AND q.state<>'revoked' ORDER BY COALESCE(q.settled_at_ms,q.reserved_at_ms),q.attempt_id LIMIT 10001")?;
    let mut rows = stmt.query(params![owner, start])?;
    let mut result = Vec::new();
    while let Some(row) = rows.next()? {
        if result.len() >= 10_000 { return Err(RepositoryError::NotFound); }
        let job_id: String = row.get(0)?; let attempt_id: String = row.get(1)?;
        let kind: Option<String> = row.get(2)?; let original: Option<String> = row.get(3)?;
        let joined_attempt: Option<String> = row.get(12)?;
        let item = LiveReservation { job_id, attempt_id, job_kind: kind.ok_or(RepositoryError::NotFound)?,
            original_sha256: original.ok_or(RepositoryError::NotFound)?, lease_epoch: row.get(4)?,
            state: row.get(5)?, reserved_at_ms: row.get(6)?, settled_at_ms: row.get(7)?,
            charged: ChargeTotals { calls: row.get(8)?, seconds: row.get(9)?, input_tokens: row.get(10)?, output_tokens: row.get(11)? } };
        budget.check(item.job_id.len().saturating_add(item.attempt_id.len()).saturating_add(item.job_kind.len()).saturating_add(128)).map_err(RepositoryError::Validation)?;
        if !text_ok(&item.job_id) || !text_ok(&item.attempt_id) || !hash_ok(&item.original_sha256)
            || joined_attempt.as_deref() != Some(item.attempt_id.as_str()) || item.lease_epoch <= 0
            || item.reserved_at_ms < 0 || item.reserved_at_ms > now_ms
            || item.settled_at_ms.is_some_and(|v| v < item.reserved_at_ms || v > now_ms)
            || !matches!(item.state.as_str(), "reserved" | "dispatched" | "uncertain" | "settled")
            || (item.state == "settled") != item.settled_at_ms.is_some()
            || !(0..=1).contains(&item.charged.calls)
            || [item.charged.seconds,item.charged.input_tokens,item.charged.output_tokens].iter().any(|v| !(0..=10_000_000).contains(v))
        { return Err(RepositoryError::NotFound); }
        result.push(item);
    }
    Ok(result)
}

/// Independently bind the numerical limits to the exact authenticated policy
/// projection in this held transaction. This does not grant source authority.
pub(crate) fn verified_policy_limits_in(tx:&rusqlite::Transaction<'_>, original:&S05ReserveOriginal) -> Result<S05ReserveLimits,RepositoryError> {
    use serde_json::Value;
    super::require_policy_projection(tx,&original.account_id,&original.workspace_id,Some(&original.policy_projection))?;
    crate::retrieval::rank_learning::projected_policy(&original.account_policy_input_json,&original.policy_projection.account.fingerprint).map_err(RepositoryError::Validation)?;
    let root=crate::field_authority::parse_unique_json(&original.account_policy_input_json).map_err(RepositoryError::Validation)?;
    if root.get("can_manage_memory")!=Some(&serde_json::json!(true)) {return Err(RepositoryError::NotFound)}
    let prefs=root.get("preferences").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    super::s05_skill_draft::require_capture_inputs(prefs)?;
    let entry=root.pointer("/preferences/memory_policy").and_then(Value::as_array).filter(|v|v.len()==2 && v[0]==serde_json::json!(true)).ok_or(RepositoryError::NotFound)?;
    let policy=super::s05_skill_draft::accepted_policy(&entry[1])?;
    let schedule=policy.get("schedule").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    let budget=policy.get("budget").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    if schedule.get("mode")!=Some(&serde_json::json!("idle")) {return Err(RepositoryError::NotFound)}
    let read=|map:&serde_json::Map<String,Value>,key:&str| -> Result<u32,RepositoryError> {
        map.get(key).and_then(Value::as_u64).and_then(|v|u32::try_from(v).ok()).ok_or(RepositoryError::NotFound)
    };
    let limits=S05ReserveLimits {quiet_seconds:read(schedule,"quiet_seconds")?, daily_calls:read(budget,"daily_calls")?,
        daily_seconds:read(budget,"daily_seconds")?,daily_tokens:read(budget,"daily_tokens")?,job_seconds:read(budget,"job_seconds")?,
        job_output_tokens:read(budget,"job_output_tokens")?,owner_queue:read(budget,"owner_queue")?,
        global_queue:read(budget,"global_queue")?,concurrency:read(budget,"concurrency")?};
    if !(5..=3600).contains(&limits.quiet_seconds) || limits.daily_calls>1000 || limits.daily_seconds>86400 || limits.daily_tokens>1_000_000
        || !(1..=300).contains(&limits.job_seconds) || !(1..=32768).contains(&limits.job_output_tokens)
        || !(1..=500).contains(&limits.owner_queue) || limits.global_queue!=500 || limits.concurrency!=1
        || (limits.daily_seconds>0 && limits.job_seconds>limits.daily_seconds)
        || (limits.daily_tokens>0 && limits.job_output_tokens>limits.daily_tokens)
        || limits != original.limits
    {return Err(RepositoryError::NotFound)}
    Ok(limits)
}

/// Facts independently verified against the live job and paired reserve receipts.
#[derive(Clone,Debug)]
pub(crate) struct LiveChargeProof {
    pub basis:String,
    pub reserve_original_sha256:Option<String>,
    pub reserve_result_sha256:Option<String>,
}

/// Sum only transaction-local live rows that the caller has individually
/// verified, plus receipt/custody-verified generic retained facts. M5 facts
/// are deliberately separate and must be added by their existing owner.
pub(crate) fn total_verified_window<F>(
    account:&str, owner:&str, now_ms:i64, live:&[LiveReservation], retained:&[RetainedCharge], mut verify_live:F,
) -> Result<ChargeTotals,RepositoryError>
where F:FnMut(&LiveReservation)->Result<LiveChargeProof,RepositoryError> {
    use std::collections::{BTreeMap,BTreeSet};
    if !text_ok(account) || !text_ok(owner) {return Err(RepositoryError::NotFound)}
    let mut total=ChargeTotals::default();
    let mut live_keys=BTreeMap::new();
    for row in live {
        let proof=verify_live(row)?;
        if !matches!(proof.basis.as_str(),"reported"|"reserved_bound"|"known_zero"|"unknown")
            || proof.reserve_original_sha256.is_some()!=proof.reserve_result_sha256.is_some()
            || proof.reserve_original_sha256.as_ref().is_some_and(|v|!hash_ok(v))
            || proof.reserve_result_sha256.as_ref().is_some_and(|v|!hash_ok(v))
            || (proof.basis=="known_zero" && row.charged!=ChargeTotals::default()) {return Err(RepositoryError::NotFound)}
        total.add(row.charged)?;
        let key=(job_digest(account,&row.job_id)?,attempt_digest(account,&row.attempt_id)?,row.lease_epoch);
        if live_keys.insert(key,(row,proof)).is_some() {return Err(RepositoryError::NotFound)}
    }
    let mut seen=BTreeSet::new();
    for fact in retained {
        validate_charge(fact)?;
        if fact.account_id!=account || !seen.insert(charge_key(fact)?) {return Err(RepositoryError::NotFound)}
        let key=(fact.job_sha256.clone(),fact.attempt_sha256.clone(),fact.lease_epoch);
        if let Some((row,proof))=live_keys.get(&key) {
            let observed=RetainedCharge {version:1,account_id:account.into(),captured_owner:owner.into(),job_kind:row.job_kind.clone(),
                job_sha256:fact.job_sha256.clone(),attempt_sha256:fact.attempt_sha256.clone(),lease_epoch:row.lease_epoch,
                original_sha256:row.original_sha256.clone(),reserve_original_sha256:proof.reserve_original_sha256.clone(),
                reserve_result_sha256:proof.reserve_result_sha256.clone(),reserved_at_ms:row.reserved_at_ms,settled_at_ms:row.settled_at_ms,
                charged_calls:row.charged.calls,charged_seconds:row.charged.seconds,charged_input_tokens:row.charged.input_tokens,
                charged_output_tokens:row.charged.output_tokens,charge_basis:proof.basis.clone()};
            if !same_live_charge(fact,&observed)? {return Err(RepositoryError::NotFound)}
        } else {add_retained_window(&mut total,fact,now_ms)?;}
    }
    Ok(total)
}

/// Verify the exact stored, account-scoped Host job and its retry sequence.
/// This is factual matching only; the caller must separately verify the
/// existing source-original receipt, policy, cutoff, and live authority.
pub(crate) fn verify_expected_job_in(
    tx:&rusqlite::Transaction<'_>, request:&S05ReserveOriginal, budget:&mut super::ResetFenceBudget,
)->Result<(),RepositoryError> {
    use rusqlite::{params,OptionalExtension};
    let row:Option<(S05ExpectedJob,String,String,i64,Option<String>,String)>=tx.query_row(
        "SELECT j.kind,j.idempotency_key,j.input_hash,r.source_ref_json,r.admission_id,r.binding_fingerprint,r.policy_fingerprint,r.authority_fingerprint,r.connection_id,r.model_route_id,r.assignment_json,r.coverage_json,r.source_account_id,r.target_workspace_id,j.attempt_count,j.current_attempt_id,j.state FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r ON r.owner_id=j.owner_id AND r.job_id=j.job_id WHERE j.owner_id=?1 AND j.job_id=?2 AND j.project_key='' AND j.workspace_key=r.target_workspace_id",
        params![request.owner,request.job_id],|r|Ok((S05ExpectedJob {kind:r.get(0)?,idempotency_key:r.get(1)?,input_hash:r.get(2)?,source_ref_json:r.get(3)?,admission_id:r.get(4)?,binding_fingerprint:r.get(5)?,policy_fingerprint:r.get(6)?,authority_fingerprint:r.get(7)?,connection_id:r.get(8)?,model_route_id:r.get(9)?,assignment_json:r.get(10)?,coverage_json:r.get(11)?},r.get(12)?,r.get(13)?,r.get(14)?,r.get(15)?,r.get::<_,String>(16)?)))
        .optional()?;
    let (stored,account,workspace,count,current,state)=row.ok_or(RepositoryError::NotFound)?;
    budget.check(serde_json::to_vec(&stored)?.len()).map_err(RepositoryError::Validation)?;
    if stored != request.expected || account != request.account_id || workspace != request.workspace_id
        || count != i64::from(request.pre_attempt_count) || count < 0 || count >= 3
        || !matches!(state.as_str(),"queued"|"retry_wait")
    {return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT a.attempt_number,a.attempt_id,a.state,q.state FROM fm_v2_attempts a LEFT JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2 ORDER BY a.attempt_number LIMIT 4")?;
    let mut rows=stmt.query(params![request.owner,request.job_id])?;let mut seen=0i64;let mut last=None;
    while let Some(row)=rows.next()? {
        seen+=1;let number:i64=row.get(0)?;let id:String=row.get(1)?;let attempt:String=row.get(2)?;let reservation:Option<String>=row.get(3)?;
        budget.check(id.len().saturating_add(64)).map_err(RepositoryError::Validation)?;
        if number!=seen || seen>3 || !text_ok(&id) || !matches!((attempt.as_str(),reservation.as_deref()),("failed_retryable",Some("settled"))|("fenced",Some("revoked"))) {return Err(RepositoryError::NotFound)}
        last=Some(id);
    }
    if seen!=count || (count==0 && (current.is_some() || state!="queued")) || (count>0 && (current!=last || state!="retry_wait")) {return Err(RepositoryError::NotFound)}
    Ok(())
}

/// Content-free original identity to be retained under the existing guarded
/// change-set/outbox owner before a reservation can be issued.
#[derive(Clone,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReserveOriginalFact {
    pub version:u32,
    pub account_id:String,
    pub captured_owner:String,
    pub workspace_id:String,
    pub job_id:String,
    pub pre_attempt_count:u32,
    pub slot:String,
    pub request_sha256:String,
}

/// Produces a commitment only; account custody, current job, source, policy,
/// clock coverage and profile proof must be verified by the transaction owner.
pub(crate) fn original_fact(request:&S05ReserveOriginal,captured_owner:&str)->Result<ReserveOriginalFact,RepositoryError> {
    let slot=reserve_slot(request)?;
    if !text_ok(captured_owner) {return Err(RepositoryError::NotFound)}
    let value=serde_json::to_value(request)?;
    let request_sha256=sha256_hex(&serde_json::to_vec(&value)?);
    Ok(ReserveOriginalFact {version:1,account_id:request.account_id.clone(),captured_owner:captured_owner.into(),
        workspace_id:request.workspace_id.clone(),job_id:request.job_id.clone(),pre_attempt_count:request.pre_attempt_count,
        slot,request_sha256})
}

pub(crate) fn validate_original_fact(fact:&ReserveOriginalFact)->Result<(),RepositoryError> {
    if fact.version!=1 || !text_ok(&fact.account_id) || !text_ok(&fact.captured_owner)
        || !text_ok(&fact.workspace_id) || !text_ok(&fact.job_id) || !hash_ok(&fact.request_sha256)
        || fact.slot!=reserve_slot_for(&fact.account_id,&fact.job_id,fact.pre_attempt_count)?
    {return Err(RepositoryError::NotFound)}
    Ok(())
}

/// Verify the paired original receipt for a scoped factual status lookup.
/// `None` is unknown, not permission to issue or remint an original.
pub(crate) fn read_original_fact_in(
    tx:&rusqlite::Transaction<'_>, status:&S05ReserveStatus,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>, budget:&mut super::ResetFenceBudget,
)->Result<Option<ReserveOriginalFact>,RepositoryError> {
    status_slot(status)?;
    read_original_slot_in(tx,&status.owner,&status.account_id,&status.workspace_id,&status.job_id,status.pre_attempt_count,view,budget)
}

/// Verify one content-free original without requiring a historical policy
/// projection. Current account custody is still mandatory; absence is unknown.
pub(crate) fn read_original_slot_in(
    tx:&rusqlite::Transaction<'_>, owner:&str, account:&str, workspace:&str, job:&str, pre_attempt_count:u32,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>, budget:&mut super::ResetFenceBudget,
)->Result<Option<ReserveOriginalFact>,RepositoryError> {
    use rusqlite::{params,OptionalExtension};
    if !text_ok(owner) || !text_ok(workspace) {return Err(RepositoryError::NotFound)}
    let slot=reserve_slot_for(account,job,pre_attempt_count)?;
    if view.current_owner()!=owner || view.account_id()!=account {return Err(RepositoryError::NotFound)}
    let id=format!("cs_{slot}");
    let row:Option<(String,String,String,String,Option<String>,String)>=tx.query_row(
        "SELECT content_hash,operation_metadata_json,actor_type,actor_id,source_event_id,contract_version FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s05.reserve-original.v1' AND length(CAST(operation_metadata_json AS BLOB))<=8192",
        params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,source,contract))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,format!("outbox_{id}_s05_reserve_original")],|r|r.get(0))?;
        if collision {return Err(RepositoryError::NotFound)}
        return Ok(None);
    };
    budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="automation" || actor_id!="s05_reservation"
        || source.as_deref()!=Some(job) || contract!=super::CONTRACT_VERSION {return Err(RepositoryError::NotFound)}
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let fact:ReserveOriginalFact=serde_json::from_value(value.clone())?;
    validate_original_fact(&fact)?;view.for_captured(tx,&fact.captured_owner)?;
    if fact.account_id!=account || fact.workspace_id!=workspace || fact.job_id!=job
        || fact.pre_attempt_count!=pre_attempt_count || fact.slot!=slot || serde_json::to_string(&value)?!=raw
    {return Err(RepositoryError::NotFound)}
    let outbox:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s05_reserve_original' AND length(CAST(payload_json AS BLOB))<=8192",params![owner,format!("outbox_{id}_s05_reserve_original"),id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if outbox.is_none_or(|(body,revision)|revision!=0 || crate::field_authority::parse_unique_json(&body).ok()!=Some(value)) {return Err(RepositoryError::NotFound)}
    Ok(Some(fact))
}

/// Persist the exact already-verified content-free original under the existing
/// change-set/outbox transaction. Caller must first check current job/source,
/// account custody, accepted policy, cutoff/clock and original request scope.
/// A repeated exact fact is status-only; a different fact for the slot fails.
fn write_original_fact_in(
    tx:&rusqlite::Transaction<'_>,status:&S05ReserveStatus,fact:&ReserveOriginalFact,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<bool,RepositoryError> {
    use rusqlite::params;
    validate_original_fact(fact)?;
    if fact.account_id!=status.account_id || fact.workspace_id!=status.workspace_id || fact.job_id!=status.job_id
        || fact.pre_attempt_count!=status.pre_attempt_count || fact.slot!=status_slot(status)?
    {return Err(RepositoryError::NotFound)}
    view.for_captured(tx,&fact.captured_owner)?;
    if let Some(prior)=read_original_fact_in(tx,status,view,budget)? {
        if prior!=*fact {return Err(RepositoryError::NotFound)}
        return Ok(false);
    }
    let id=format!("cs_{}",fact.slot);
    let value=serde_json::to_value(fact)?;let raw=serde_json::to_string(&value)?;
    budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if raw.len()>8192 {return Err(RepositoryError::NotFound)}
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','s05_reservation','s05.reserve-original.v1',?3,?4,?5,?6,?7)",params![status.owner,id,status.job_id,super::CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    super::insert_outbox(tx,&status.owner,&id,"s05_reserve_original",&value,0,&now)?;
    Ok(true)
}


/// Complete the insertion only after the enclosing immediate transaction has
/// independently verified current authority, original, clock, cutoff and all
/// live/retained usage. This helper is deliberately private and unregistered.
fn insert_verified_reservation_in(
    tx:&rusqlite::Transaction<'_>, request:&S05ReserveRequest,
    fact:&ReserveOriginalFact, used:ChargeTotals, now_ms:i64,
)->Result<serde_json::Value,RepositoryError> {
    use rusqlite::{params,OptionalExtension};
    let original=&request.original;let witness=&request.witness;
    validate_original_fact(fact)?;
    if request.version!=1 || !text_ok(&witness.worker_id) || !text_ok(&witness.coverage_generation)
        || !text_ok(&witness.clock_generation) || witness.now_tick<0
        || fact!=&original_fact(original,&fact.captured_owner)? || now_ms<0 {return Err(RepositoryError::NotFound)}
    let limits=&original.limits;
    let occupancy:Option<(String,String,i64,i64,i64)>=tx.query_row(
        "SELECT coverage_generation,clock_generation,verified,foreground_epoch,last_quiet_tick FROM fm_v2_s05_occupancy WHERE singleton=1",[],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let (coverage,clock,verified,foreground,quiet_tick)=occupancy.ok_or(RepositoryError::NotFound)?;
    let quiet_ms=i64::from(limits.quiet_seconds).checked_mul(1000).ok_or(RepositoryError::NotFound)?;
    if verified!=1 || coverage!=witness.coverage_generation || clock!=witness.clock_generation || foreground<0
        || witness.now_tick.checked_sub(quiet_tick).is_none_or(|v|v<quiet_ms)
    {return Err(RepositoryError::NotFound)}
    let busy:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown')) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE state IN ('reserved','dispatched','uncertain'))",[],|r|r.get(0))?;
    if busy {return Err(RepositoryError::NotFound)}
    let tokens=used.token_total()?;
    let input=match original.input_token_bound {
        Some(v)=>i64::from(v),
        None=>i64::from(limits.daily_tokens).checked_sub(tokens).and_then(|v|v.checked_sub(i64::from(limits.job_output_tokens))).ok_or(RepositoryError::NotFound)?.min(1_000_000),
    };
    if input<i64::from(original.minimum_input_bytes)
        || used.calls.checked_add(1).is_none_or(|v|v>i64::from(limits.daily_calls))
        || used.seconds.checked_add(i64::from(limits.job_seconds)).is_none_or(|v|v>i64::from(limits.daily_seconds))
        || tokens.checked_add(input).and_then(|v|v.checked_add(i64::from(limits.job_output_tokens))).is_none_or(|v|v>i64::from(limits.daily_tokens))
    {return Err(RepositoryError::NotFound)}
    let now=chrono::DateTime::from_timestamp_millis(now_ms).ok_or(RepositoryError::NotFound)?.to_rfc3339();
    let attempt=format!("s05_{}",sha256_hex(fact.slot.as_bytes()));
    let epoch:i64=tx.query_row("SELECT COALESCE(MAX(lease_epoch),0)+1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2",params![original.owner,original.job_id],|r|r.get(0))?;
    if epoch<=0 {return Err(RepositoryError::NotFound)}
    let changed=tx.execute("UPDATE fm_v2_jobs SET state='queued',updated_at=?3 WHERE owner_id=?1 AND job_id=?2 AND state='retry_wait' AND attempt_count=?4",params![original.owner,original.job_id,now,original.pre_attempt_count])?;
    if changed>1 {return Err(RepositoryError::NotFound)}
    tx.execute("INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,lease_owner,lease_expires_at,heartbeat_at,progress_watermark,error_json,created_at,updated_at) VALUES(?1,?2,?3,?4,'leased',?5,?6,NULL,?7,NULL,NULL,?7,?7)",params![original.owner,attempt,original.job_id,original.pre_attempt_count+1,epoch,witness.worker_id,now])?;
    tx.execute("INSERT INTO fm_v2_s05_reservations VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'reserved',?9,NULL,1,?10,?11,?12,1,?10,?11,?12)",params![original.owner,attempt,original.job_id,epoch,original.expected.connection_id,coverage,clock,foreground,now_ms,limits.job_seconds,input,limits.job_output_tokens])?;
    if tx.execute("UPDATE fm_v2_jobs SET state='active',current_attempt_id=?3,attempt_count=attempt_count+1,updated_at=?4 WHERE owner_id=?1 AND job_id=?2 AND state='queued' AND attempt_count=?5",params![original.owner,original.job_id,attempt,now,original.pre_attempt_count])?!=1 {return Err(RepositoryError::NotFound)}
    let deadline=witness.now_tick.checked_add(i64::from(limits.job_seconds).checked_mul(1000).ok_or(RepositoryError::NotFound)?).ok_or(RepositoryError::NotFound)?;
    let receipt=serde_json::json!({"version":1,"account_id":original.account_id,"captured_owner":fact.captured_owner,
        "workspace_id":original.workspace_id,"job_id":original.job_id,"slot":fact.slot,"original_sha256":fact.request_sha256,
        "reserve_sha256":sha256_hex(&serde_json::to_vec(&serde_json::to_value(request)?)?),
        "attempt_id":attempt,"lease_epoch":epoch,"foreground_epoch":foreground,"reservation_deadline_tick":deadline,
        "worker_id":witness.worker_id,"coverage_generation":witness.coverage_generation,"clock_generation":witness.clock_generation,"now_tick":witness.now_tick,
        "connection_id":original.expected.connection_id,"reserved_at_ms":now_ms,"reserved_seconds":limits.job_seconds,
        "reserved_input_tokens":input,"reserved_output_tokens":limits.job_output_tokens});
    let raw=serde_json::to_string(&receipt)?;
    if raw.len()>8192 {return Err(RepositoryError::NotFound)}
    let id=format!("cs_{}_reserved",fact.slot);
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','s05_reservation','s05.reserve-result.v1',?3,?4,?5,?6,?7)",params![original.owner,id,original.job_id,super::CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    super::insert_outbox(tx,&original.owner,&id,"s05_reserve_result",&receipt,0,&now)?;
    Ok(receipt)
}

/// Compiled support; each caller must still verify profile, custody and coverage.
pub(crate) fn shared_admission_ready()->bool { true }


/// Read a committed reservation result only with its exact paired original,
/// current custody and matching durable attempt/reservation rows. Missing is
/// unknown; this function does not authorize another reservation.
fn read_reserved_result_in(
    tx:&rusqlite::Transaction<'_>, status:&S05ReserveStatus,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>, budget:&mut super::ResetFenceBudget,
)->Result<Option<serde_json::Value>,RepositoryError> {
    status_slot(status)?;
    read_reserved_slot_in(tx,&status.owner,&status.account_id,&status.workspace_id,&status.job_id,status.pre_attempt_count,view,budget)
}

pub(crate) fn read_reserved_slot_in(
    tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,pre_attempt_count:u32,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<Option<serde_json::Value>,RepositoryError> {
    use rusqlite::{params,OptionalExtension};
    let slot=reserve_slot_for(account,job,pre_attempt_count)?;
    let id=format!("cs_{slot}_reserved");
    let original=match read_original_slot_in(tx,owner,account,workspace,job,pre_attempt_count,view,budget)? {
        Some(v)=>v,
        None=>{
            let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,format!("outbox_{id}_s05_reserve_result")],|r|r.get(0))?;
            if orphan {return Err(RepositoryError::NotFound)}
            return Ok(None);
        }
    };
    let row:Option<(String,String,String,String,Option<String>,String)>=tx.query_row(
        "SELECT content_hash,operation_metadata_json,actor_type,actor_id,source_event_id,contract_version FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s05.reserve-result.v1' AND length(CAST(operation_metadata_json AS BLOB))<=8192",
        params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let Some((hash,raw,actor,actor_id,source,contract))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,format!("outbox_{id}_s05_reserve_result")],|r|r.get(0))?;
        if collision {return Err(RepositoryError::NotFound)}
        return Ok(None);
    };
    budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    if hash!=sha256_hex(raw.as_bytes()) || actor!="automation" || actor_id!="s05_reservation"
        || source.as_deref()!=Some(job) || contract!=super::CONTRACT_VERSION {return Err(RepositoryError::NotFound)}
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let attempt=format!("s05_{}",sha256_hex(original.slot.as_bytes()));
    let epoch=value["lease_epoch"].as_i64().filter(|v|*v>0).ok_or(RepositoryError::NotFound)?;
    let foreground=value["foreground_epoch"].as_i64().filter(|v|*v>=0).ok_or(RepositoryError::NotFound)?;
    let worker=value["worker_id"].as_str().filter(|v|text_ok(v)).ok_or(RepositoryError::NotFound)?;
    let coverage=value["coverage_generation"].as_str().filter(|v|text_ok(v)).ok_or(RepositoryError::NotFound)?;
    let clock=value["clock_generation"].as_str().filter(|v|text_ok(v)).ok_or(RepositoryError::NotFound)?;
    let tick=value["now_tick"].as_i64().filter(|v|*v>=0).ok_or(RepositoryError::NotFound)?;
    let deadline=value["reservation_deadline_tick"].as_i64().filter(|v|*v>=tick).ok_or(RepositoryError::NotFound)?;
    let reserve_sha=value["reserve_sha256"].as_str().filter(|v|hash_ok(v)).ok_or(RepositoryError::NotFound)?;
    let connection=value["connection_id"].as_str().filter(|v|text_ok(v)).ok_or(RepositoryError::NotFound)?;
    let reserved_at=value["reserved_at_ms"].as_i64().filter(|v|*v>=0).ok_or(RepositoryError::NotFound)?;
    let seconds=value["reserved_seconds"].as_i64().filter(|v|(1..=300).contains(v)).ok_or(RepositoryError::NotFound)?;
    let input=value["reserved_input_tokens"].as_i64().filter(|v|(0..=1_000_000).contains(v)).ok_or(RepositoryError::NotFound)?;
    let output=value["reserved_output_tokens"].as_i64().filter(|v|(1..=32_768).contains(v)).ok_or(RepositoryError::NotFound)?;
    if seconds.checked_mul(1000).and_then(|v|tick.checked_add(v))!=Some(deadline) {return Err(RepositoryError::NotFound)}
    let expected=serde_json::json!({"version":1,"account_id":original.account_id,"captured_owner":original.captured_owner,
        "workspace_id":original.workspace_id,"job_id":original.job_id,"slot":original.slot,"original_sha256":original.request_sha256,
        "reserve_sha256":reserve_sha,"attempt_id":attempt,"lease_epoch":epoch,"foreground_epoch":foreground,
        "reservation_deadline_tick":deadline,"worker_id":worker,"coverage_generation":coverage,"clock_generation":clock,"now_tick":tick,
        "connection_id":connection,"reserved_at_ms":reserved_at,"reserved_seconds":seconds,"reserved_input_tokens":input,"reserved_output_tokens":output});
    if value!=expected || serde_json::to_string(&value)?!=raw {return Err(RepositoryError::NotFound)}
    let outbox:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s05_reserve_result' AND length(CAST(payload_json AS BLOB))<=8192",params![owner,format!("outbox_{id}_s05_reserve_result"),id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if outbox.is_none_or(|(body,revision)|revision!=0 || crate::field_authority::parse_unique_json(&body).ok()!=Some(value.clone())) {return Err(RepositoryError::NotFound)}
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2 AND a.attempt_id=?3 AND a.attempt_number=?4 AND a.lease_epoch=?5 AND a.lease_owner=?6 AND q.foreground_epoch=?7 AND q.coverage_generation=?8 AND q.clock_generation=?9 AND q.connection_id=?10 AND q.reserved_at_ms=?11 AND q.reserved_calls=1 AND q.reserved_seconds=?12 AND q.reserved_input_tokens=?13 AND q.reserved_output_tokens=?14)",params![owner,job,attempt,pre_attempt_count+1,epoch,worker,foreground,coverage,clock,connection,reserved_at,seconds,input,output],|r|r.get(0))?;
    if !exists {return Err(RepositoryError::NotFound)}
    Ok(Some(value))
}


/// Prepare the immutable slot after the caller has independently established
/// guarded profile, cutoff coverage, current clock and held Host authority.
/// This helper is unregistered and does not itself establish those proofs.
fn prepare_verified_original_in(
    tx:&rusqlite::Transaction<'_>,request:&S05ReserveOriginal,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<bool,RepositoryError> {
    reserve_slot(request)?;
    if view.current_owner()!=request.owner || view.account_id()!=request.account_id {return Err(RepositoryError::NotFound)}
    if crate::store::sqlite::SqliteStore::s05_slot_terminal_in(tx,&request.owner,&request.account_id,&request.workspace_id,&request.job_id,request.pre_attempt_count,view,budget).map_err(RepositoryError::Validation)?.is_some() {return Err(RepositoryError::NotFound)}
    verified_policy_limits_in(tx,request)?;
    let (captured,input)=crate::store::sqlite::SqliteStore::s05_host_original_in(tx,&request.owner,&request.account_id,&request.job_id,view,budget).map_err(RepositoryError::Validation)?;
    if input!=request.expected.input_hash {return Err(RepositoryError::NotFound)}
    let fact=original_fact(request,&captured)?;
    let status=S05ReserveStatus {version:1,owner:request.owner.clone(),account_id:request.account_id.clone(),
        workspace_id:request.workspace_id.clone(),job_id:request.job_id.clone(),pre_attempt_count:request.pre_attempt_count,
        policy_projection:request.policy_projection.clone()};
    if let Some(prior)=read_original_fact_in(tx,&status,view,budget)? {
        if prior!=fact {return Err(RepositoryError::NotFound)}
        return Ok(false);
    }
    verify_expected_job_in(tx,request,budget)?;
    write_original_fact_in(tx,&status,&fact,view,budget)
}


/// Factual original reconciliation under current policy/custody. A prepared
/// result is returned only when the slot has no corresponding attempt; an
/// advanced or retired attempt is status-only and cannot grant reissue.
fn original_status_in(
    tx:&rusqlite::Transaction<'_>,status:&S05ReserveStatus,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<serde_json::Value,RepositoryError> {
    use rusqlite::params;
    status_slot(status)?;
    super::require_policy_projection(tx,&status.account_id,&status.workspace_id,Some(&status.policy_projection))?;
    if let Some(terminal)=crate::store::sqlite::SqliteStore::s05_slot_terminal_in(tx,&status.owner,&status.account_id,&status.workspace_id,&status.job_id,status.pre_attempt_count,view,budget).map_err(RepositoryError::Validation)? {
        return Ok(serde_json::json!({"version":1,"state":"terminal","disposition":terminal.disposition}));
    }
    let fact=match read_original_fact_in(tx,status,view,budget)? {
        Some(v)=>v,
        None=>{
            // Even unknown status must reject a surviving orphan result.
            read_reserved_slot_in(tx,&status.owner,&status.account_id,&status.workspace_id,&status.job_id,status.pre_attempt_count,view,budget)?;
            return Ok(serde_json::json!({"version":1,"state":"unknown"}));
        }
    };
    let result=read_reserved_result_in(tx,status,view,budget)?;
    let attempt=format!("s05_{}",sha256_hex(fact.slot.as_bytes()));
    if let Some(result)=result {
        let active:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id WHERE a.owner_id=?1 AND a.job_id=?2 AND a.attempt_id=?3 AND a.state='leased' AND q.state='reserved' AND j.state='active' AND j.current_attempt_id=a.attempt_id)",params![status.owner,status.job_id,attempt],|r|r.get(0))?;
        return Ok(serde_json::json!({"version":1,"state":if active {"reserved"} else {"existing"},"result":result}));
    }
    let proved=verify_unreserved_slot_in(tx,&status.owner,&status.account_id,&status.workspace_id,&status.job_id,status.pre_attempt_count,view,budget)?;
    if proved!=fact {return Err(RepositoryError::NotFound)}
    Ok(serde_json::json!({"version":1,"state":"prepared","request_sha256":fact.request_sha256}))
}


/// The surrounding immediate transaction must establish the bounded common
/// quota totals, cutoff/clock and held Host authority before calling this.
/// Repeated exact originals resolve the durable result without reinsertion.
fn reserve_verified_in(
    tx:&rusqlite::Transaction<'_>,reserve:&S05ReserveRequest,used:ChargeTotals,now_ms:i64,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<serde_json::Value,RepositoryError> {
    let request=&reserve.original;
    reserve_slot(request)?;
    verified_policy_limits_in(tx,request)?;
    let status=S05ReserveStatus {version:1,owner:request.owner.clone(),account_id:request.account_id.clone(),
        workspace_id:request.workspace_id.clone(),job_id:request.job_id.clone(),pre_attempt_count:request.pre_attempt_count,
        policy_projection:request.policy_projection.clone()};
    if let Some(terminal)=crate::store::sqlite::SqliteStore::s05_slot_terminal_in(tx,&status.owner,&status.account_id,&status.workspace_id,&status.job_id,status.pre_attempt_count,view,budget).map_err(RepositoryError::Validation)? {
        return Ok(serde_json::json!({"version":1,"state":"terminal","disposition":terminal.disposition}));
    }
    let fact=read_original_fact_in(tx,&status,view,budget)?.ok_or(RepositoryError::NotFound)?;
    if fact!=original_fact(request,&fact.captured_owner)? {return Err(RepositoryError::NotFound)}
    if let Some(result)=read_reserved_result_in(tx,&status,view,budget)? {
        let requested=sha256_hex(&serde_json::to_vec(&serde_json::to_value(reserve)?)?);
        if result["reserve_sha256"]!=requested {
            return Ok(serde_json::json!({"version":1,"state":"existing"}));
        }
        return original_status_in(tx,&status,view,budget);
    }
    verify_expected_job_in(tx,request,budget)?;
    let (captured,input)=crate::store::sqlite::SqliteStore::s05_host_original_in(tx,&request.owner,&request.account_id,&request.job_id,view,budget).map_err(RepositoryError::Validation)?;
    if captured!=fact.captured_owner || input!=request.expected.input_hash {return Err(RepositoryError::NotFound)}
    let result=insert_verified_reservation_in(tx,reserve,&fact,used,now_ms)?;
    Ok(serde_json::json!({"version":1,"state":"reserved","result":result}))
}


/// Transaction-local proof for a prepared slot with no reserve outcome.
/// The reset owner must separately hold the live account drain and exact
/// provider-generation fence through this transaction, then atomically write
/// the immutable cancelled-before-reserve terminal before deleting custody.
pub(crate) fn verify_unreserved_slot_in(
    tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,workspace:&str,job:&str,pre_attempt_count:u32,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<ReserveOriginalFact,RepositoryError> {
    use rusqlite::{params,OptionalExtension};
    let fact=read_original_slot_in(tx,owner,account,workspace,job,pre_attempt_count,view,budget)?.ok_or(RepositoryError::NotFound)?;
    if read_reserved_slot_in(tx,owner,account,workspace,job,pre_attempt_count,view,budget)?.is_some() {return Err(RepositoryError::NotFound)}
    let attempt=format!("s05_{}",sha256_hex(fact.slot.as_bytes()));
    let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 AND (attempt_id=?3 OR attempt_number=?4)) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations q WHERE q.owner_id=?1 AND q.job_id=?2 AND (q.attempt_id=?3 OR NOT EXISTS(SELECT 1 FROM fm_v2_attempts a WHERE a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch)))",params![owner,job,attempt,pre_attempt_count+1],|r|r.get(0))?;
    if present {return Err(RepositoryError::NotFound)}
    let state:Option<(String,i64,Option<String>)>=tx.query_row("SELECT state,attempt_count,current_attempt_id FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let (state,count,current)=state.ok_or(RepositoryError::NotFound)?;
    if count!=i64::from(pre_attempt_count) {return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT a.attempt_number,a.attempt_id,a.state,q.state FROM fm_v2_attempts a LEFT JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2 ORDER BY a.attempt_number LIMIT 4")?;
    let mut rows=stmt.query(params![owner,job])?;let mut seen=0i64;let mut last=None;
    while let Some(row)=rows.next()? {
        seen+=1;let number:i64=row.get(0)?;let id:String=row.get(1)?;let a:String=row.get(2)?;let q:Option<String>=row.get(3)?;
        budget.check(id.len().saturating_add(64)).map_err(RepositoryError::Validation)?;
        if number!=seen || seen>3 || !text_ok(&id) || !matches!((a.as_str(),q.as_deref()),("failed_retryable",Some("settled"))|("fenced",Some("revoked"))) {return Err(RepositoryError::NotFound)}
        last=Some(id);
    }
    if seen!=count || (count==0 && (current.is_some() || state!="queued")) || (count>0 && (current!=last || state!="retry_wait")) {return Err(RepositoryError::NotFound)}
    Ok(fact)
}


impl<'a> super::V2Repository<'a> {
    /// Private prepared-original owner; per-call profile and authority proofs remain required.
    fn s05_prepare_private(&mut self,store:&crate::store::sqlite::SqliteStore,request:&S05ReserveOriginal)->Result<serde_json::Value,RepositoryError> {
        if !shared_admission_ready() {return Err(RepositoryError::NotFound)}
        let tx=self.conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        super::require_guarded_epoch(&tx)?;
        store.s05_quota_guarded_schema_in(&tx).map_err(RepositoryError::Validation)?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let mut budget=super::ResetFenceBudget::install(&tx);
        let now_ms=chrono::Utc::now().timestamp_millis();
        crate::store::sqlite::SqliteStore::s05_quota_cutoff_in(&tx,&request.owner,&request.account_id,now_ms,&mut budget).map_err(RepositoryError::Validation)?;
        let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget)?;
        let created=prepare_verified_original_in(&tx,request,&view,&mut budget)?;
        let status=S05ReserveStatus {version:1,owner:request.owner.clone(),account_id:request.account_id.clone(),workspace_id:request.workspace_id.clone(),job_id:request.job_id.clone(),pre_attempt_count:request.pre_attempt_count,policy_projection:request.policy_projection.clone()};
        let mut result=original_status_in(&tx,&status,&view,&mut budget)?;
        if created {
            if result.get("state").and_then(serde_json::Value::as_str)!=Some("prepared") {return Err(RepositoryError::NotFound)}
            result["state"]=serde_json::json!("created");
        }
        drop(view);drop(budget);tx.commit()?;Ok(result)
    }

    fn s05_original_private(&mut self,store:&crate::store::sqlite::SqliteStore,status:&S05ReserveStatus)->Result<serde_json::Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred)?;
        super::require_guarded_epoch(&tx)?;
        store.s05_quota_guarded_schema_in(&tx).map_err(RepositoryError::Validation)?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let mut budget=super::ResetFenceBudget::install(&tx);
        let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&status.account_id,&status.owner,&status.owner,&mut budget)?;
        let result=original_status_in(&tx,status,&view,&mut budget)?;
        drop(view);drop(budget);tx.commit()?;Ok(result)
    }
}


/// Compose the indexed live, reset and purge families in one held transaction.
/// This source remains gated until lifecycle conservation and the Host join
/// have been qualified against the actual owners.
pub(crate) fn verified_window_totals_in(
    tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,now_ms:i64,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<ChargeTotals,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(RepositoryError::NotFound)}
    let busy:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown')) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE state IN ('reserved','dispatched','uncertain'))",[],|r|r.get(0))?;
    if busy {return Err(RepositoryError::NotFound)}
    let cutoff=crate::store::sqlite::SqliteStore::s05_quota_cutoff_in(tx,owner,account,now_ms,budget).map_err(RepositoryError::Validation)?;
    verified_window_totals_after_cutoff_in(tx,owner,account,now_ms,cutoff,view,budget)
}

// The caller has already proved the cutoff in the same transaction.
fn verified_window_totals_after_cutoff_in(
    tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,now_ms:i64,cutoff:i64,
    view:&super::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut super::ResetFenceBudget,
)->Result<ChargeTotals,RepositoryError> {
    let live=live_window_in(tx,owner,now_ms,budget)?;
    let mut generic=crate::store::sqlite::SqliteStore::s05_quota_reset_window_in(tx,owner,account,now_ms,view,budget).map_err(RepositoryError::Validation)?;
    let purged=crate::store::sqlite::SqliteStore::s05_quota_purge_window_in(tx,account,now_ms,budget).map_err(RepositoryError::Validation)?;
    generic.extend(purged);
    let m5=crate::store::sqlite::SqliteStore::s05_quota_m5_reset_window_in(tx,owner,account,now_ms,view,budget).map_err(RepositoryError::Validation)?;
    let m5_purged=crate::store::sqlite::SqliteStore::s05_quota_m5_purge_window_in(tx,owner,account,now_ms,view,budget).map_err(RepositoryError::Validation)?;
    let mut total=total_verified_window(account,owner,now_ms,&live,&generic,|row| {
        if row.job_kind=="skill_draft" {super::s05_skill_draft::verified_live_charge_in(tx,owner,account,row,view,budget)}
        else {crate::store::sqlite::SqliteStore::s05_quota_live_charge_in(tx,owner,account,cutoff,row,view,budget).map_err(RepositoryError::Validation)}
    })?;
    let start=now_ms.checked_sub(86_400_000).ok_or(RepositoryError::NotFound)?;
    let mut seen=std::collections::BTreeSet::new();
    for fact in m5 {
        if fact.account_id!=account || fact.settled_at_ms<=start || fact.settled_at_ms>now_ms || !seen.insert(fact.job_id.clone()) {return Err(RepositoryError::NotFound)}
        total.add(ChargeTotals {calls:fact.charged_calls,seconds:fact.charged_seconds,input_tokens:fact.charged_input_tokens,output_tokens:fact.charged_output_tokens})?;
    }
    for fact in m5_purged {
        let job=format!("skilldraft_{}",fact.operation_sha256);
        if fact.settled_at_ms<=start || fact.settled_at_ms>now_ms || !seen.insert(job) {return Err(RepositoryError::NotFound)}
        total.add(ChargeTotals {calls:fact.charged_calls,seconds:fact.charged_seconds,input_tokens:fact.charged_input_tokens,output_tokens:fact.charged_output_tokens})?;
    }
    Ok(total)
}

impl<'a> super::V2Repository<'a> {
    /// Reserve in the guarded transaction after current account and quota proof.
    fn s05_reserve_private(&mut self,store:&crate::store::sqlite::SqliteStore,request:&S05ReserveRequest)->Result<serde_json::Value,RepositoryError> {
        if !shared_admission_ready() {return Err(RepositoryError::NotFound)}
        let original=&request.original;
        let tx=self.conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        super::require_guarded_epoch(&tx)?;
        store.s05_quota_guarded_schema_in(&tx).map_err(RepositoryError::Validation)?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let mut budget=super::ResetFenceBudget::install(&tx);
        let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&original.account_id,&original.owner,&original.owner,&mut budget)?;
        let status=S05ReserveStatus {version:1,owner:original.owner.clone(),account_id:original.account_id.clone(),workspace_id:original.workspace_id.clone(),job_id:original.job_id.clone(),pre_attempt_count:original.pre_attempt_count,policy_projection:original.policy_projection.clone()};
        let observed=original_status_in(&tx,&status,&view,&mut budget)?;
        match observed["state"].as_str() {
            Some("terminal")=>{drop(view);drop(budget);tx.commit()?;return Ok(observed)},
            Some("reserved"|"existing")=>{
                let result=&observed["result"];
                let expected=sha256_hex(&serde_json::to_vec(&serde_json::to_value(request)?)?);
                let response=if result["reserve_sha256"]==expected {observed} else {serde_json::json!({"version":1,"state":"existing"})};
                drop(view);drop(budget);tx.commit()?;return Ok(response)
            },
            Some("prepared")=>{},
            _=>return Err(RepositoryError::NotFound),
        }
        let fact=read_original_fact_in(&tx,&status,&view,&mut budget)?.ok_or(RepositoryError::NotFound)?;
        if fact!=original_fact(original,&fact.captured_owner)? {return Err(RepositoryError::NotFound)}
        // Recheck the shared window in the same transaction before issue.
        let now_ms=chrono::Utc::now().timestamp_millis();
        let used=verified_window_totals_in(&tx,&original.owner,&original.account_id,now_ms,&view,&mut budget)?;
        let result=reserve_verified_in(&tx,request,used,now_ms,&view,&mut budget)?;
        drop(view);drop(budget);tx.commit()?;Ok(result)
    }
}


impl<'a> super::V2Repository<'a> {
    /// Closed private transport owner with per-call coverage and custody checks.
    pub(crate) fn s05_reserve_phase(&mut self,store:&crate::store::sqlite::SqliteStore,
        owner:&str,workspace:&str,phase:&str,request:&serde_json::Value)->Result<serde_json::Value,RepositoryError> {
        let full=serde_json::json!({"owner":owner,"workspace_id":workspace,"phase":phase,"request":request});
        if serde_json::to_vec(&full)?.len()>65_536 {return Err(RepositoryError::NotFound)}
        match phase {
            "s05_reserve_readiness"=>{
                let r:S05ReserveReadiness=serde_json::from_value(request.clone())?;
                if r.version!=1 || !text_ok(&r.account_id) || r.owner!=owner || r.workspace_id!=workspace {return Err(RepositoryError::NotFound)}
                let unavailable=||serde_json::json!({"version":1,"state":"unavailable","owner":owner,"account_id":r.account_id,"workspace_id":workspace});
                if !shared_admission_ready() {return Ok(unavailable())}
                let tx=self.conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred)?;
                super::require_guarded_epoch(&tx)?;
                store.s05_quota_guarded_schema_in(&tx).map_err(RepositoryError::Validation)?;
                let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0))?;
                let cookie:i64=tx.query_row("PRAGMA schema_version",[],|row|row.get(0))?;
                let mut budget=super::ResetFenceBudget::install(&tx);
                let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget)?;
                super::require_policy_projection(&tx,&r.account_id,workspace,Some(&r.policy_projection))?;
                let now=chrono::Utc::now().timestamp_millis();
                match crate::store::sqlite::SqliteStore::s05_quota_coverage_in(&tx,owner,&r.account_id,now,&mut budget).map_err(RepositoryError::Validation)? {
                    crate::store::sqlite::S05QuotaCoverage::Unsupported | crate::store::sqlite::S05QuotaCoverage::Warming => {
                        drop(view);drop(budget);tx.commit()?;return Ok(unavailable());
                    },
                    crate::store::sqlite::S05QuotaCoverage::Ready(_) => {},
                }
                let result=serde_json::json!({"version":1,"state":"ready","owner":owner,"account_id":r.account_id,
                    "workspace_id":workspace,"database_id":database,"schema_cookie":cookie,"policy_projection":r.policy_projection});
                drop(view);drop(budget);tx.commit()?;Ok(result)
            },
            "s05_reserve_prepare"=>{
                let original:S05ReserveOriginal=serde_json::from_value(request.clone())?;
                if original.owner!=owner || original.workspace_id!=workspace {return Err(RepositoryError::NotFound)}
                self.s05_prepare_private(store,&original)
            },
            "s05_reserve"=>{
                let reserve:S05ReserveRequest=serde_json::from_value(request.clone())?;
                if reserve.original.owner!=owner || reserve.original.workspace_id!=workspace {return Err(RepositoryError::NotFound)}
                self.s05_reserve_private(store,&reserve)
            },
            "s05_reserve_original"=>{
                let status:S05ReserveStatus=serde_json::from_value(request.clone())?;
                if status.owner!=owner || status.workspace_id!=workspace {return Err(RepositoryError::NotFound)}
                self.s05_original_private(store,&status)
            },
            _=>Err(RepositoryError::NotFound),
        }
    }
}

#[cfg(test)]
#[test]
fn s05_conservation_disposable_window() {
    let Ok(path)=std::env::var("S05_CONSERVATION_DB") else { return };
    let database=std::env::var("S05_CONSERVATION_DATABASE").unwrap();
    let owner=std::env::var("S05_CONSERVATION_OWNER").unwrap();
    let account=std::env::var("S05_CONSERVATION_ACCOUNT").unwrap();
    let expected:Vec<i64>=std::env::var("S05_CONSERVATION_EXPECTED").unwrap().split(',').map(|s|s.parse().unwrap()).collect();
    assert_eq!(expected.len(),4);
    let now:i64=std::env::var("S05_CONSERVATION_NOW").unwrap().parse().unwrap();
    let (store,_,_,_,_,_)=crate::store::sqlite::SqliteStore::open_guarded(&path,4,&database).unwrap();
    let mut conn=store.conn.lock().unwrap();
    let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();
    store.s05_quota_guarded_schema_in(&tx).unwrap();
    let mut budget=super::ResetFenceBudget::install(&tx);
    if std::env::var_os("S05_CONSERVATION_POST_PURGE").is_some() {
        let facts=crate::store::sqlite::SqliteStore::s05_quota_purge_window_in(&tx,&account,now,&mut budget).unwrap();
        assert_eq!(facts.len(),1);
        let mut total=ChargeTotals::default();
        for fact in &facts { add_retained_window(&mut total,fact,now).unwrap(); }
        assert_eq!(total,ChargeTotals{calls:expected[0],seconds:expected[1],input_tokens:expected[2],output_tokens:expected[3]});
        drop(budget);tx.rollback().unwrap();return;
    }
    let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&account,&owner,&owner,&mut budget).unwrap();
    // Synthetic clock intentionally observes a pre-cutoff seed. This test does
    // not invoke or authorize admission, whose real cutoff check remains intact.
    let receipt:String=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_cutoff.v1'",[],|r|r.get(0)).unwrap();
    let fields:Vec<&str>=receipt.split('|').collect();assert_eq!(fields.len(),4);assert_eq!(fields[0],"v1");
    let cutoff:i64=fields[2].parse().unwrap();assert!(cutoff>0);
    let total=verified_window_totals_after_cutoff_in(&tx,&owner,&account,now,cutoff,&view,&mut budget).unwrap();
    assert_eq!(total,ChargeTotals{calls:expected[0],seconds:expected[1],input_tokens:expected[2],output_tokens:expected[3]});
    drop(view);drop(budget);tx.rollback().unwrap();
}

// Uses an explicitly supplied disposable stopped copy; the savepoint rolls back
// the deliberate malformed-receipt probe and never enables public admission.
#[cfg(test)]
#[test]
fn s05_coverage_disposable_facts() {
    let path=std::env::var("S05_PREPARED_DB").expect("disposable database required");
    let database=std::env::var("S05_PREPARED_DATABASE").unwrap();
    let input=std::env::var("S05_PREPARED_ORIGINAL").unwrap();
    let request:S05ReserveOriginal=serde_json::from_str(&std::fs::read_to_string(input).unwrap()).unwrap();
    let (store,_,_,_,_,_)=crate::store::sqlite::SqliteStore::open_guarded(&path,4,&database).unwrap();
    let mut conn=store.conn.lock().unwrap();
    let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();
    store.s05_quota_guarded_schema_in(&tx).unwrap();
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_cutoff.v1'",[],|r|r.get(0)).unwrap();
    let cutoff:i64=raw.split('|').nth(2).unwrap().parse().unwrap();
    let mut budget=super::ResetFenceBudget::install(&tx);
    use crate::store::sqlite::{S05QuotaCoverage,SqliteStore};
    assert_eq!(SqliteStore::s05_quota_coverage_in(&tx,&request.owner,&request.account_id,cutoff,&mut budget).unwrap(),S05QuotaCoverage::Warming);
    assert_eq!(SqliteStore::s05_quota_coverage_in(&tx,&request.owner,&request.account_id,cutoff+86_400_000,&mut budget).unwrap(),S05QuotaCoverage::Ready(cutoff));
    assert!(SqliteStore::s05_quota_coverage_in(&tx,&request.owner,&request.account_id,cutoff-1,&mut budget).is_err());
    assert!(SqliteStore::s05_quota_coverage_in(&tx,&request.owner,"",cutoff,&mut budget).is_err());
    tx.execute_batch("SAVEPOINT malformed_probe; DROP TRIGGER s05_quota_cutoff_no_update; UPDATE fm_meta SET value='invalid' WHERE key='openclank.s05.quota_cutoff.v1';").unwrap();
    assert!(SqliteStore::s05_quota_coverage_in(&tx,&request.owner,&request.account_id,cutoff,&mut budget).is_err());
    tx.execute_batch("ROLLBACK TO malformed_probe; RELEASE malformed_probe;").unwrap();
    drop(budget);tx.rollback().unwrap();
    // The real clock observes this freshly prepared profile as warming.
    let mut repo=super::V2Repository::new(&mut conn).unwrap();
    let readiness=repo.s05_reserve_phase(&store,&request.owner,&request.workspace_id,"s05_reserve_readiness",&serde_json::json!({"version":1,"owner":request.owner,"account_id":request.account_id,"workspace_id":request.workspace_id,"policy_projection":request.policy_projection})).unwrap();
    assert_eq!(readiness["state"],"unavailable");
}

// Disposable held-store qualification: the caller supplies the stopped copy and
// the exact original produced by the real Host staging owner. Synthetic time
// proves the offline cutoff only; this helper never enables the public phase.
#[cfg(test)]
#[test]
fn s05_prepared_terminal_disposable_seed() {
    let Ok(path)=std::env::var("S05_PREPARED_DB") else { return };
    let database=std::env::var("S05_PREPARED_DATABASE").unwrap();
    let input=std::env::var("S05_PREPARED_ORIGINAL").unwrap();
    let now:i64=std::env::var("S05_PREPARED_NOW").unwrap().parse().unwrap();
    let raw=std::fs::read_to_string(input).unwrap();
    let request:S05ReserveOriginal=serde_json::from_str(&raw).unwrap();
    assert!(shared_admission_ready());
    let (store,_,_,_,_,_)=crate::store::sqlite::SqliteStore::open_guarded(&path,4,&database).unwrap();
    let mut conn=store.conn.lock().unwrap();
    let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();
    super::require_guarded_epoch(&tx).unwrap();
    store.s05_quota_guarded_schema_in(&tx).unwrap();
    let mut budget=super::ResetFenceBudget::install(&tx);
    crate::store::sqlite::SqliteStore::s05_quota_cutoff_in(&tx,&request.owner,&request.account_id,now,&mut budget).unwrap();
    let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget).unwrap();
    assert!(prepare_verified_original_in(&tx,&request,&view,&mut budget).unwrap());
    let fact=verify_unreserved_slot_in(&tx,&request.owner,&request.account_id,&request.workspace_id,&request.job_id,request.pre_attempt_count,&view,&mut budget).unwrap();
    assert_eq!(fact.slot,reserve_slot(&request).unwrap());
    drop(view);drop(budget);tx.commit().unwrap();
}

// Test the actual terminal-before-insert owner on a separately reset copy.
#[cfg(test)]
#[test]
fn s05_prepared_terminal_disposable_late_reserve() {
    let Ok(path)=std::env::var("S05_PREPARED_DB") else { return };
    let database=std::env::var("S05_PREPARED_DATABASE").unwrap();
    let input=std::env::var("S05_PREPARED_ORIGINAL").unwrap();
    let now:i64=std::env::var("S05_PREPARED_NOW").unwrap().parse().unwrap();
    let request:S05ReserveOriginal=serde_json::from_str(&std::fs::read_to_string(input).unwrap()).unwrap();
    assert!(shared_admission_ready());
    let (store,_,_,_,_,_)=crate::store::sqlite::SqliteStore::open_guarded(&path,4,&database).unwrap();
    let mut conn=store.conn.lock().unwrap();
    let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();
    super::require_guarded_epoch(&tx).unwrap();store.s05_quota_guarded_schema_in(&tx).unwrap();
    let mut budget=super::ResetFenceBudget::install(&tx);
    let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget).unwrap();
    let reserve=S05ReserveRequest {version:1,original:request.clone(),witness:S05ReserveWitness {worker_id:"late-worker".into(),coverage_generation:"late-coverage".into(),clock_generation:"late-clock".into(),now_tick:1}};
    let result=reserve_verified_in(&tx,&reserve,ChargeTotals::default(),now,&view,&mut budget).unwrap();
    assert_eq!(result["state"],"terminal");assert_eq!(result["disposition"],"cancelled_before_reserve");
    let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2",rusqlite::params![request.owner,request.job_id],|r|r.get(0)).unwrap();assert_eq!(count,0);
    drop(view);drop(budget);tx.rollback().unwrap();
}

// One controlled-time post-cutoff transaction against a genuine staged job.
// The public readiness gate remains closed; Host/model authority is not implied.
#[cfg(test)]
#[test]
fn s05_post_cutoff_disposable_reserve_window() {
    let Ok(path)=std::env::var("S05_PREPARED_DB") else { return };
    let database=std::env::var("S05_PREPARED_DATABASE").unwrap();
    let input=std::env::var("S05_PREPARED_ORIGINAL").unwrap();
    let now:i64=std::env::var("S05_PREPARED_NOW").unwrap().parse().unwrap();
    let step=std::env::var("S05_POST_CUTOFF_STEP").unwrap();
    let request:S05ReserveOriginal=serde_json::from_str(&std::fs::read_to_string(input).unwrap()).unwrap();
    assert!(shared_admission_ready());
    let (store,_,_,_,_,_)=crate::store::sqlite::SqliteStore::open_guarded(&path,4,&database).unwrap();
    let mut conn=store.conn.lock().unwrap();let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();
    super::require_guarded_epoch(&tx).unwrap();store.s05_quota_guarded_schema_in(&tx).unwrap();
    let mut budget=super::ResetFenceBudget::install(&tx);
    let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget).unwrap();
    let total=verified_window_totals_in(&tx,&request.owner,&request.account_id,now,&view,&mut budget).unwrap();
    if step=="reserve" {
        assert_eq!(total,ChargeTotals::default());
        let (coverage,clock,quiet):(String,String,i64)=tx.query_row("SELECT coverage_generation,clock_generation,last_quiet_tick FROM fm_v2_s05_occupancy WHERE singleton=1",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
        let tick=quiet.checked_add(i64::from(request.limits.quiet_seconds)*1000).unwrap();
        let reserve=S05ReserveRequest{version:1,original:request.clone(),witness:S05ReserveWitness{worker_id:"synthetic-worker".into(),coverage_generation:coverage,clock_generation:clock,now_tick:tick}};
        let result=reserve_verified_in(&tx,&reserve,total,now,&view,&mut budget).unwrap();assert_eq!(result["state"],"reserved");
        println!("S05_POST_CUTOFF_RESERVATION={}",serde_json::to_string(&result).unwrap());
        drop(view);drop(budget);tx.commit().unwrap();
    } else {
        assert_eq!(step,"read");assert_eq!(total,ChargeTotals{calls:1,seconds:2,input_tokens:10,output_tokens:4});
        println!("S05_POST_CUTOFF_TOTAL=1,2,10,4");drop(view);drop(budget);tx.rollback().unwrap();
    }
}
