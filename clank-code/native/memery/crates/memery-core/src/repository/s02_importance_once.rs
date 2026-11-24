//! Durable facts for the existing S02 Human score-review original.
//!
//! The authenticated Host owns its original ticket and the Native server holds
//! a non-cloneable live permit. A stored prepare or missing claim never grants issue.
use super::*;
use super::s08_policy_review::{ImportanceReviewRequest, validate_importance_review};

const PREPARE_REASON: &str = "s02.importance-prepare.v1";
const CLAIM_REASON: &str = "s02.importance-claim.v1";
const PREPARE_KIND: &str = "s02_importance_prepared";
const CLAIM_KIND: &str = "s02_importance_claimed";
const FACT_LIMIT: usize = 65_536;

fn unavailable() -> RepositoryError { RepositoryError::NotFound }

/// Immutable original and verified root facts. Lifecycle collection must
/// independently reprove the root relation before using these as denial facts.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportancePrepareFact {
    pub version: u32,
    pub request: ImportanceReviewRequest,
    pub request_sha256: String,
    pub selector_sha256: String,
    pub root_receipt_id: String,
    pub root_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ImportanceClaimFact {
    pub version: u32,
    pub prepare_id: String,
    pub prepare_sha256: String,
    pub request_sha256: String,
}

#[derive(Debug, Clone)]
pub(crate) struct VerifiedClaim {
    pub claim_id:String,
    pub claim_sha256:String,
    pub fact:ImportanceClaimFact,
}

#[derive(Debug, Clone)]
pub(crate) struct PreparedOriginal {
    pub prepare_id: String,
    pub prepare_sha256: String,
    pub fact: ImportancePrepareFact,
}

/// The only constructor source for a live permit is a newly inserted prepare.
/// The Native wrapper must commit its transaction before consuming this value.
pub(crate) struct NewPreparedOriginal { original: PreparedOriginal }
impl NewPreparedOriginal {
    pub(crate) fn original(&self) -> &PreparedOriginal { &self.original }
    pub(crate) fn into_committed_permit(self) -> S02ImportancePermit {
        let p=self.original;
        S02ImportancePermit { prepare_id:p.prepare_id, prepare_sha256:p.prepare_sha256,
            request_sha256:p.fact.request_sha256, selector_sha256:p.fact.selector_sha256,
            root_receipt_id:p.fact.root_receipt_id, root_sha256:p.fact.root_sha256 }
    }
}
/// Process-local, non-cloneable and non-deserializable authority. The server
/// retains it only after the `Created` prepare transaction commits, then moves
/// it exactly once into the claim/effect transaction.
pub struct S02ImportancePermit {
    prepare_id:String, prepare_sha256:String, request_sha256:String,
    selector_sha256:String, root_receipt_id:String, root_sha256:String,
}
impl S02ImportancePermit {
    pub fn lookup_key(&self) -> &str { &self.prepare_id }
    pub fn request_sha256(&self) -> &str { &self.request_sha256 }
}
/// Existing prepare is status-only; missing claim never means unissued.
pub(crate) enum PrepareOutcome { Created(NewPreparedOriginal), Existing(PreparedOriginal) }

fn ids(r: &ImportanceReviewRequest) -> Result<(String, String, String), RepositoryError> {
    for value in [&r.owner, &r.account_id, &r.workspace_id, &r.candidate_id, &r.block_id, &r.request_key] {
        if value.is_empty() || value.trim() != value || value.len() > 512 || value.chars().any(char::is_control) {
            return Err(unavailable());
        }
    }
    let material=serde_json::to_vec(&("s02.importance-once.v1",&r.account_id,&r.workspace_id,&r.block_id,&r.candidate_id,&r.request_key))?;
    let digest=sha256_hex(&material);
    let receipt_digest=sha256_hex(&serde_json::to_vec(&("s02.importance-review.v1",&r.account_id,&r.candidate_id,&r.request_key))?);
    Ok((format!("cs_s02importanceprepare_{digest}"), format!("cs_s02importanceclaim_{digest}"), format!("cs_s02importance_{receipt_digest}")))
}

fn effect_slot_empty(tx:&Transaction<'_>,owner:&str,receipt_id:&str)->Result<bool,RepositoryError> {
    let review_event=format!("outbox_{receipt_id}_s02_importance_review");
    let candidate_event=format!("outbox_{receipt_id}_candidate_revision");
    let occupied:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id IN (?3,?4))",params![owner,receipt_id,review_event,candidate_event],|row|row.get(0))?;
    Ok(!occupied)
}

fn check_scope(tx:&Transaction<'_>, r:&ImportanceReviewRequest, view:&owner_custody::VerifiedCustodyView<'_,'_>) -> Result<(),RepositoryError> {
    require_guarded_epoch(tx)?;
    if r.version != 1 || view.current_owner() != r.owner || view.account_id() != r.account_id { return Err(unavailable()); }
    require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    Ok(())
}

fn load_fact<T: for<'de> Deserialize<'de> + Serialize>(
    tx:&Transaction<'_>, owner:&str, captured_actor:&str, id:&str, reason:&str, kind:&str, budget:&mut ResetFenceBudget,
) -> Result<Option<(T,String)>,RepositoryError> {
    let size:Option<i64>=tx.query_row(
        "SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",
        params![owner,id],|row|row.get(0)).optional()?.flatten();
    let event_id=format!("outbox_{id}_{kind}");
    let Some(size)=size else {
        let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2)",params![owner,event_id],|row|row.get(0))?;
        return if orphan {Err(unavailable())} else {Ok(None)};
    };
    if size<1 || size as usize>FACT_LIMIT {return Err(unavailable());}
    budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let (raw,hash,actor,actor_id,actual_reason,source,contract):(String,String,String,String,String,Option<String>,String)=tx.query_row(
        "SELECT operation_metadata_json,content_hash,actor_type,actor_id,reason,source_event_id,contract_version FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",
        params![owner,id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?)))?;
    if actor!="user" || actor_id!=captured_actor || actual_reason!=reason || source.is_some() || contract!=CONTRACT_VERSION || sha256_hex(raw.as_bytes())!=hash {return Err(unavailable());}
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let fact:T=serde_json::from_value(value.clone())?;
    if serde_json::to_value(&fact)?!=value {return Err(unavailable());}
    let event_size:Option<i64>=tx.query_row("SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",params![owner,event_id],|row|row.get(0)).optional()?.flatten();
    let event_size=event_size.filter(|n|*n>=1 && (*n as usize)<=FACT_LIMIT).ok_or_else(unavailable)?;
    budget.check(event_size as usize).map_err(RepositoryError::Validation)?;
    let outbox:Option<(String,String,String,i64)>=tx.query_row(
        "SELECT change_set_id,kind,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2",
        params![owner,event_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?))).optional()?;
    let Some((linked,actual_kind,payload,revision))=outbox else {return Err(unavailable());};
    if linked!=id || actual_kind!=kind || revision!=0 || crate::field_authority::parse_unique_json(&payload).map_err(RepositoryError::Validation)?!=value {return Err(unavailable());}
    Ok(Some((fact,hash)))
}

fn insert_fact<T:Serialize>(tx:&Transaction<'_>,owner:&str,id:&str,reason:&str,kind:&str,fact:&T)->Result<String,RepositoryError> {
    let value=serde_json::to_value(fact)?;
    let raw=serde_json::to_string(&value)?;
    if raw.is_empty() || raw.len()>FACT_LIMIT {return Err(unavailable());}
    let hash=sha256_hex(raw.as_bytes());let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES (?,?,'user',?,?,NULL,?,?,?,?)",
        params![owner,id,owner,reason,CONTRACT_VERSION,hash,now,raw])?;
    insert_outbox(tx,owner,id,kind,&value,0,&now)?;
    Ok(hash)
}

/// Prepare in the existing checked IMMEDIATE transaction; a duplicate cannot
/// mint another live issue permission. The caller commits before replying.
pub(crate) fn prepare_in(tx:&Transaction<'_>,r:&ImportanceReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<PrepareOutcome,RepositoryError> {
    check_scope(tx,r,view)?;
    if crate::store::sqlite::s02_importance_terminal::probe(tx,&r.owner,&r.account_id,&r.workspace_id,&r.block_id,&r.request_key,view,budget).map_err(|_|unavailable())? {return Err(unavailable());}
    let (prepare_id,claim_id,receipt_id)=ids(r)?;
    let request_sha256=sha256_hex(&serde_json::to_vec(r)?);
    if let Some((fact,prepare_sha256))=load_fact::<ImportancePrepareFact>(tx,&r.owner,&r.owner,&prepare_id,PREPARE_REASON,PREPARE_KIND,budget)? {
        if fact.version!=1 || serde_json::to_vec(&fact.request)?!=serde_json::to_vec(r)? || fact.request_sha256!=request_sha256 || !valid_sha256(&fact.selector_sha256)
            || fact.root_receipt_id.is_empty() || !valid_sha256(&fact.root_sha256) {return Err(unavailable());}
        return Ok(PrepareOutcome::Existing(PreparedOriginal{prepare_id,prepare_sha256,fact}));
    }
    if load_fact::<ImportanceClaimFact>(tx,&r.owner,&r.owner,&claim_id,CLAIM_REASON,CLAIM_KIND,budget)?.is_some() {return Err(unavailable());}
    if !effect_slot_empty(tx,&r.owner,&receipt_id)? {return Err(unavailable());}
    let verified=validate_importance_review(tx,r,view,budget)?;
    if verified.request_sha256!=request_sha256 {return Err(unavailable());}
    let fact=ImportancePrepareFact{version:1,request:r.clone(),request_sha256,selector_sha256:verified.selector_sha256,
        root_receipt_id:verified.root_receipt_id,root_sha256:verified.root_sha256};
    let prepare_sha256=insert_fact(tx,&r.owner,&prepare_id,PREPARE_REASON,PREPARE_KIND,&fact)?;
    Ok(PrepareOutcome::Created(NewPreparedOriginal{original:PreparedOriginal{prepare_id,prepare_sha256,fact}}))
}

/// Insert the unique claim immediately before the score effect and receipt in
/// the SAME IMMEDIATE transaction. Any failure must roll back the transaction.
pub(crate) fn claim_in(tx:&Transaction<'_>,r:&ImportanceReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,permit:S02ImportancePermit)->Result<ImportanceClaimFact,RepositoryError> {
    check_scope(tx,r,view)?;
    let (prepare_id,claim_id,receipt_id)=ids(r)?;
    let (prepared,prepare_sha256)=load_fact::<ImportancePrepareFact>(tx,&r.owner,&r.owner,&prepare_id,PREPARE_REASON,PREPARE_KIND,budget)?.ok_or_else(unavailable)?;
    let request_sha256=sha256_hex(&serde_json::to_vec(r)?);
    if prepared.version!=1 || serde_json::to_vec(&prepared.request)?!=serde_json::to_vec(r)? || prepared.request_sha256!=request_sha256
        || permit.prepare_id!=prepare_id || permit.prepare_sha256!=prepare_sha256 || permit.request_sha256!=request_sha256
        || permit.selector_sha256!=prepared.selector_sha256 || permit.root_receipt_id!=prepared.root_receipt_id || permit.root_sha256!=prepared.root_sha256 {return Err(unavailable());}
    if load_fact::<ImportanceClaimFact>(tx,&r.owner,&r.owner,&claim_id,CLAIM_REASON,CLAIM_KIND,budget)?.is_some() {return Err(unavailable());}
    if !effect_slot_empty(tx,&r.owner,&receipt_id)? {return Err(unavailable());}
    let verified=validate_importance_review(tx,r,view,budget)?;
    if verified.request_sha256!=request_sha256 || verified.root_receipt_id!=prepared.root_receipt_id
        || verified.root_sha256!=prepared.root_sha256 || verified.selector_sha256!=prepared.selector_sha256 {return Err(unavailable());}
    let claim=ImportanceClaimFact{version:1,prepare_id,prepare_sha256,request_sha256};
    insert_fact(tx,&r.owner,&claim_id,CLAIM_REASON,CLAIM_KIND,&claim)?;
    Ok(claim)
}

/// Lifecycle-only checked facts. The captured Human remains immutable through
/// rename; current custody is proved separately, and a stale expected head does
/// not erase an older prepared original. The caller compares a claim with its
/// exact applied child before retiring it.
pub(crate) fn collect_prepared_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,prepare_id:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(PreparedOriginal,Option<VerifiedClaim>),RepositoryError> {
    collect_prepared_mode_in(tx,owner,account,workspace,prepare_id,view,budget,super::S08HistoricalRead::Ordinary)
}

pub(crate) fn collect_prepared_mode_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,prepare_id:&str,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,mode:super::S08HistoricalRead<'_, '_, '_>)->Result<(PreparedOriginal,Option<VerifiedClaim>),RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account {return Err(unavailable());}
    let size:Option<i64>=tx.query_row("SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,prepare_id],|row|row.get(0)).optional()?.flatten();
    let size=size.filter(|n|*n>=1 && (*n as usize)<=FACT_LIMIT).ok_or_else(unavailable)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,prepare_id],|row|row.get(0))?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let captured:ImportancePrepareFact=serde_json::from_value(value)?;
    let request=&captured.request;
    if request.account_id!=account || request.workspace_id!=workspace {return Err(unavailable());}
    view.for_captured(tx,&request.owner)?;
    let (expected_id,claim_id,_)=ids(request)?;
    if expected_id!=prepare_id {return Err(unavailable());}
    let (fact,prepare_sha256)=load_fact::<ImportancePrepareFact>(tx,owner,&request.owner,prepare_id,PREPARE_REASON,PREPARE_KIND,budget)?.ok_or_else(unavailable)?;
    if fact.version!=1 || fact.request_sha256!=sha256_hex(&serde_json::to_vec(&fact.request)?)
        || fact.selector_sha256!=super::s08_policy_review::importance_selector(account,workspace,&request.block_id,&request.request_key)? {return Err(unavailable());}
    let root=super::s08_policy_review::summary_accepted_root_in(tx,owner,account,workspace,&request.candidate_id,view,budget,mode)?;
    let root_id:String=tx.query_row("SELECT change_set_id FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND candidate_id=?2 AND revision=4",params![owner,request.candidate_id],|row|row.get(0))?;
    let root_sha:String=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![owner,root_id],|row|row.get(0))?;
    if root["block_id"]!=request.block_id || root["curated_id"]!=request.curated_id || root_id!=fact.root_receipt_id || root_sha!=fact.root_sha256 || !valid_sha256(&root_sha) {return Err(unavailable());}
    let claim=load_fact::<ImportanceClaimFact>(tx,owner,&request.owner,&claim_id,CLAIM_REASON,CLAIM_KIND,budget)?;
    let claim=match claim {Some((claim,claim_sha256))=>{
        if claim.version!=1 || claim.prepare_id!=prepare_id || claim.prepare_sha256!=prepare_sha256 || claim.request_sha256!=fact.request_sha256 {return Err(unavailable());}
        Some(VerifiedClaim{claim_id,claim_sha256,fact:claim})
    },None=>None};
    Ok((PreparedOriginal{prepare_id:prepare_id.to_owned(),prepare_sha256,fact},claim))
}

/// Status never manufactures permission from an absent claim. Positive
/// recorded results additionally require this exact prepare and claim chain;
/// a verified terminal denial can outlive the scrubbed original.
pub(crate) fn status_in(tx:&Transaction<'_>,owner:&str,policy:&PolicyPublicationRefs,r:&ImportanceReviewRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    let status=super::s08_policy_review::importance_review_original(tx,owner,r,policy,view,budget)?;
    match status.get("state").and_then(Value::as_str) {
        Some("unknown"|"terminal_erased"|"account_purged")=>Ok(status),
        Some("recorded")=>{
            let (prepare_id,_,_)=ids(r)?;
            let (original,claim)=collect_prepared_in(tx,owner,&r.account_id,&r.workspace_id,&prepare_id,view,budget)?;
            if serde_json::to_vec(&original.fact.request)?!=serde_json::to_vec(r)? || claim.is_none() {return Err(unavailable());}
            Ok(status)
        },
        _=>Err(unavailable()),
    }
}
