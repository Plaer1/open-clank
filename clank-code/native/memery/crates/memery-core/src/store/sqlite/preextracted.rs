//! C4-F8 pre-extracted facts: typed candidates handed to Memory with an exact source reference. They are staged pending and
//! inferred, never become a record without a named human's review (decided by the Host, never by a key), and are removed by
//! source (the erase path is NOT built: removed 11-12 against guarded-forget.md; `erase_source` refuses this source type). No model runs. The wire is frozen in the C4-F8 robonote ("Wire (frozen)").
//!
//! Rows used, all existing tables: legacy `candidates` (family `preextracted_<hex>`), one `fm_v2_sources` row per
//! `(uri, revision)` of type `external_fact_source.v1`, one `fm_v2_evidence` row per candidate, `fm_v2_conflicts` (+ events,
//! change set, outbox) for a contradiction of a protected path. Nothing here writes a receipt family of its own.
use super::*;
use crate::repository::{self, PolicyPublicationRefs};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub(crate) const SOURCE_TYPE: &str = "external_fact_source.v1";
pub(crate) const CANDIDATE_PREFIX: &str = "preextracted_";
pub(crate) const CONFLICT_PREFIX: &str = "preextracted_conflict_";
const INVALID: &str = "preextracted request invalid";
const KEY_CONFLICT: &str = "preextracted request key conflict";
const UNAVAILABLE: &str = "preextracted unavailable";

#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreextractedSource { pub uri: String, pub revision: i64, pub content_sha256: String }
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreextractedTarget { pub predicate: String, pub claim_slot: String }
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreextractedItem {
    pub kind: String, pub text: String, pub source: PreextractedSource,
    #[serde(default)] pub target: Option<PreextractedTarget>,
}
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreextractedStageRequest {
    pub version: u32, pub owner: String, pub workspace_id: String, pub account_id: String,
    pub policy_projection: PolicyPublicationRefs, pub request_key: String, pub items: Vec<PreextractedItem>,
}
/// Private Host-admitted source variant. The reference is the existing
/// canonical archive identity; it is not an external URI or model metadata.
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct HostAnchorSource {
    pub kind:String, pub reference:repository::HostArchiveSourceRef, pub coverage:String,
}
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct HostAnchorItem {
    pub kind:String, pub text:String, pub source:HostAnchorSource,
    #[serde(default)] pub target:Option<PreextractedTarget>,
}
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct HostAnchorStageRequest {
    pub version:u32, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub policy_projection:PolicyPublicationRefs, pub request_key:String, pub items:Vec<HostAnchorItem>,
}
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct HostAnchorStatusRequest {
    pub version:u32, pub owner:String, pub workspace_id:String, pub account_id:String,
    pub policy_projection:PolicyPublicationRefs, pub request_key:String,
}

#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreextractedListRequest {
    pub version: u32, pub owner: String, pub workspace_id: String, pub account_id: String,
    pub policy_projection: PolicyPublicationRefs, #[serde(default)] pub after: Option<String>, pub limit: u32,
}
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreextractedSelector {
    pub version: u32, pub owner: String, pub workspace_id: String, pub account_id: String,
    pub policy_projection: PolicyPublicationRefs, pub candidate_id: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreextractedReviewRequest {
    pub version: u32, pub owner: String, pub workspace_id: String, pub account_id: String,
    pub policy_projection: PolicyPublicationRefs, pub candidate_id: String, pub candidate_digest: String,
    pub action: String, pub request_key: String, pub reason: String,
}

fn hash(v: &[u8]) -> String { repository::sha256_hex(v) }
fn valid_text(s: &str, max: usize) -> bool { !s.is_empty() && s == s.trim() && s.len() <= max && !s.chars().any(char::is_control) }
fn hex64(s: &str) -> bool { s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) }
fn normalize(text: &str) -> String { text.split_whitespace().collect::<Vec<_>>().join(" ") }
pub(crate) fn kind_of(category: &str) -> Option<MemoryKind> {
    Some(match category {
        "fact" | "contact" | "project" | "goal" => MemoryKind::Fact,
        "identity" => MemoryKind::Persona, "preference" => MemoryKind::Instruction,
        "task" => MemoryKind::Episodic, "unknown" => MemoryKind::Unknown,
        _ => return None,
    })
}
fn scope_ok(version: u32, owner: &str, workspace: &str, account: &str) -> bool {
    version == 1 && valid_text(owner, 512) && valid_text(workspace, 512) && valid_text(account, 512)
}
pub(crate) fn item_hash(account: &str, workspace: &str, uri: &str, revision: i64, category: &str, normalized: &str) -> Result<String, String> {
    Ok(hash(&serde_json::to_vec(&("s03.preextracted-item.v1", account, workspace, uri, revision, category, normalized)).map_err(|e| e.to_string())?))
}
fn request_hash(account: &str, workspace: &str, key: &str) -> Result<String, String> {
    Ok(hash(&serde_json::to_vec(&("s03.preextracted-request.v1", account, workspace, key)).map_err(|e| e.to_string())?))
}
pub(crate) fn source_id(account: &str, workspace: &str, uri: &str) -> Result<String, String> {
    Ok(format!("preextracted_source_{}", &hash(&serde_json::to_vec(&("s03.preextracted-source.v1", account, workspace, uri)).map_err(|e| e.to_string())?)[..32]))
}
pub(crate) fn evidence_id(candidate: &str) -> Result<String, String> {
    let digest = candidate.strip_prefix(CANDIDATE_PREFIX).filter(|v| hex64(v)).ok_or_else(|| UNAVAILABLE.to_string())?;
    Ok(format!("preextracted_evidence_{digest}"))
}
pub(crate) fn conflict_id(candidate: &str) -> Result<String, String> {
    let digest = candidate.strip_prefix(CANDIDATE_PREFIX).filter(|v| hex64(v)).ok_or_else(|| UNAVAILABLE.to_string())?;
    Ok(format!("{CONFLICT_PREFIX}{digest}"))
}

struct Prepared { id: String, hash: String, kind: MemoryKind, category: String, text: String, uri: String, revision: i64, content_sha256: String, target: Option<PreextractedTarget>, host_reference:Option<repository::HostArchiveSourceRef> }

fn prepare(r: &PreextractedStageRequest) -> Result<Vec<Prepared>, String> {
    if !scope_ok(r.version, &r.owner, &r.workspace_id, &r.account_id) || !valid_text(&r.request_key, 512) || r.items.is_empty() || r.items.len() > 100 { return Err(INVALID.into()); }
    let mut out = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for item in &r.items {
        let kind = kind_of(&item.kind).ok_or(INVALID)?;
        let normalized = normalize(&item.text);
        if normalized.is_empty() || normalized.len() > 4000 || item.text != item.text.trim() || item.text.chars().any(|c| c.is_control() && c != '\n' && c != '\t') { return Err(INVALID.into()); }
        let (filtered, _) = crate::privacy::filter_text(&normalized).ok_or(INVALID)?;
        if filtered != normalized { return Err(INVALID.into()); }
        let s = &item.source;
        if !valid_text(&s.uri, 512) || s.uri.starts_with("memory://") || s.uri.starts_with("host-archive://") || s.revision < 1 || !hex64(&s.content_sha256) { return Err(INVALID.into()); }
        if let Some(t) = &item.target { if !valid_text(&t.predicate, 256) || !valid_text(&t.claim_slot, 512) { return Err(INVALID.into()); } }
        let h = item_hash(&r.account_id, &r.workspace_id, &s.uri, s.revision, &item.kind, &normalized)?;
        if !seen.insert(h.clone()) { return Err(INVALID.into()); }
        out.push(Prepared { id: format!("{CANDIDATE_PREFIX}{h}"), hash: h, kind, category: item.kind.clone(), text: normalized, uri: s.uri.clone(), revision: s.revision, content_sha256: s.content_sha256.clone(), target: item.target.clone(), host_reference:None });
    }
    // One (uri, revision) names one content hash within the request.
    let mut contents = std::collections::BTreeMap::new();
    for p in &out { if contents.insert((p.uri.clone(), p.revision), p.content_sha256.clone()).is_some_and(|prev| prev != p.content_sha256) { return Err(INVALID.into()); } }
    Ok(out)
}

fn prepare_host(r:&HostAnchorStageRequest)->Result<(PreextractedStageRequest,Vec<Prepared>,Vec<String>,String),String> {
    if !scope_ok(r.version,&r.owner,&r.workspace_id,&r.account_id) || !r.request_key.strip_prefix("m1op_").is_some_and(hex64) || r.items.len()!=1 {return Err(INVALID.into());}
    let mut prepared=Vec::new();let mut inputs=Vec::new();let mut seen=std::collections::BTreeSet::new();
    for item in &r.items {
        let source=&item.source;source.reference.validate().map_err(|_|INVALID.to_string())?;
        if source.kind!="host_user_body_anchor" || source.coverage!="host_user_body_anchor_only" || source.reference.account_id!=r.account_id {return Err(INVALID.into());}
        let kind=kind_of(&item.kind).ok_or(INVALID)?;let normalized=normalize(&item.text);
        if normalized.is_empty() || normalized.len()>4000 || item.text!=item.text.trim() || item.text.chars().any(|c|c.is_control() && c!='\n' && c!='\t') {return Err(INVALID.into());}
        let (filtered,_)=crate::privacy::filter_text(&normalized).ok_or(INVALID)?;if filtered!=normalized {return Err(INVALID.into());}
        if let Some(target)=&item.target {if !valid_text(&target.predicate,256) || !valid_text(&target.claim_slot,512) {return Err(INVALID.into());}}
        let sid=repository::host_archive_source_id(&source.reference).map_err(|_|INVALID.to_string())?;let uri=format!("host-archive://{sid}");
        let h=item_hash(&r.account_id,&r.workspace_id,&uri,source.reference.revision,&item.kind,&normalized)?;
        if !seen.insert(h.clone()) {return Err(INVALID.into());}
        inputs.push(hash(&serde_json::to_vec(&("s03.host-anchor-stage-item.v1",item)).map_err(|_|UNAVAILABLE.to_string())?));
        prepared.push(Prepared{id:format!("{CANDIDATE_PREFIX}{h}"),hash:h,kind,category:item.kind.clone(),text:normalized,uri,revision:source.reference.revision,content_sha256:source.reference.content_hash.clone(),target:item.target.clone(),host_reference:Some(source.reference.clone())});
    }
    let body=hash(&serde_json::to_vec(&("s03.host-anchor-stage-body.v1",r.version,&r.workspace_id,&r.account_id,&r.policy_projection,&r.request_key,&r.items)).map_err(|_|UNAVAILABLE.to_string())?);
    let shim=PreextractedStageRequest{version:r.version,owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),account_id:r.account_id.clone(),policy_projection:r.policy_projection.clone(),request_key:r.request_key.clone(),items:Vec::new()};
    Ok((shim,prepared,inputs,body))
}

/// The block a target names (it must exist exactly once), its head revision, whether its current value path is protected
/// (`manual` / `legacy_unknown`) and its current text. Recorded at staging and rechecked at approval.
struct Resolved { block: String, revision: i64, protected: bool, before: Option<String> }

fn resolve_target(tx: &rusqlite::Transaction<'_>, owner: &str, workspace: &str, target: &PreextractedTarget) -> Result<Option<Resolved>, String> {
    let mut stmt = tx.prepare("SELECT block_id,current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND predicate=?3 AND claim_slot=?4 ORDER BY block_id LIMIT 2").map_err(|e| e.to_string())?;
    let blocks = stmt.query_map(params![owner, workspace, target.predicate, target.claim_slot], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))).map_err(|e| e.to_string())?.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())?;
    drop(stmt);
    if blocks.len() > 1 { return Err(INVALID.into()); }
    let Some((block, revision)) = blocks.into_iter().next() else { return Ok(None) };
    let protected: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=?1 AND block_id=?2 AND revision=?3 AND field_path IN ('/value','/value/value') AND origin IN ('manual','legacy_unknown'))", params![owner, block, revision], |r| r.get(0)).map_err(|e| e.to_string())?;
    let value: Option<String> = tx.query_row("SELECT value_json FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3", params![owner, block, revision], |r| r.get(0)).optional().map_err(|e| e.to_string())?;
    let before = value.and_then(|v| serde_json::from_str::<Value>(&v).ok()).and_then(|v| v.as_str().map(str::to_owned).or_else(|| v.get("value").and_then(Value::as_str).map(str::to_owned)));
    Ok(Some(Resolved { block, revision, protected, before }))
}

/// The evidence locator of a staged candidate: its own identity, optionally the target block and head recorded at staging.
pub(crate) fn preextracted_locator_ok(locator: &Value, candidate: &str) -> bool {
    let Some(o) = locator.as_object() else { return false };
    if o.get("kind").and_then(Value::as_str) != Some("preextracted_fact") || o.get("candidate_id").and_then(Value::as_str) != Some(candidate) || o.keys().any(|k| !matches!(k.as_str(), "kind" | "candidate_id" | "target" | "host_anchor")) { return false; }
    if let Some(anchor)=o.get("host_anchor") {
        let Ok(anchor)=serde_json::from_value::<HostAnchorSource>(anchor.clone()) else {return false};
        if anchor.kind!="host_user_body_anchor" || anchor.coverage!="host_user_body_anchor_only" || anchor.reference.validate().is_err() {return false;}
    }
    match o.get("target") {
        None => true,
        Some(Value::Object(t)) => t.get("block_id").is_some_and(Value::is_string) && t.get("expected_revision").is_some_and(Value::is_i64) && t.get("predicate").is_some_and(Value::is_string) && t.get("claim_slot").is_some_and(Value::is_string) && t.keys().all(|k| matches!(k.as_str(), "block_id" | "expected_revision" | "predicate" | "claim_slot" | "before")),
        _ => false,
    }
}

/// Resolve an F8 evidence source by its closed locator and current source row.
/// A Host anchor can become stale; it never grants archive-body access.
pub(crate) fn source_projection(tx:&Connection,owner:&str,workspace:&str,account:&str,candidate:&str,uri:&str,revision:i64)->Result<Value,String> {
    source_projection_inner(tx,owner,workspace,account,candidate,uri,revision)
}
fn source_projection_inner(tx:&Connection,owner:&str,workspace:&str,account:&str,candidate:&str,uri:&str,revision:i64)->Result<Value,String> {
    let evidence=evidence_id(candidate)?;
    let row:Option<(String,String,i64,Option<String>,Option<String>,Option<String>,Option<String>,Option<String>,Option<String>)>=tx.query_row("SELECT e.source_id,e.locator_json,e.source_revision,s.source_type,s.source_uri,s.content_hash,s.forget_state,s.workspace_key,s.project_key FROM fm_v2_evidence e LEFT JOIN fm_v2_sources s ON s.owner_id=e.owner_id AND s.source_id=e.source_id AND s.source_revision=e.source_revision WHERE e.owner_id=?1 AND e.evidence_id=?2 AND e.locator_type='preextracted_fact'",params![owner,evidence],|r|Ok((repository::s08_bounded_first(r,0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?))).optional().map_err(|_|UNAVAILABLE.to_string())?;
    let (sid,raw,rev,kind,stored_uri,content,state,source_workspace,project)=row.ok_or_else(||UNAVAILABLE.to_string())?;
    let locator=crate::field_authority::parse_unique_json(&raw).map_err(|_|UNAVAILABLE.to_string())?;
    if !preextracted_locator_ok(&locator,candidate) || rev!=revision {return Err(UNAVAILABLE.into());}
    if let Some(anchor)=locator.get("host_anchor") {
        let source:HostAnchorSource=serde_json::from_value(anchor.clone()).map_err(|_|UNAVAILABLE.to_string())?;
        source.reference.validate_in(tx).map_err(|_|UNAVAILABLE.to_string())?;
        let expected=repository::host_archive_source_id(&source.reference).map_err(|_|UNAVAILABLE.to_string())?;
        if source.reference.account_id!=account || expected!=sid || uri!=format!("host-archive://{sid}") || source.reference.revision!=revision {return Err(UNAVAILABLE.into());}
        let present=kind.is_some();
        if present && (kind.as_deref()!=Some("host_archive_part.v1") || stored_uri.as_deref()!=Some(uri) || content.as_deref()!=Some(source.reference.content_hash.as_str()) || source_workspace.as_deref()!=Some(workspace) || project.as_deref()!=Some("") || !matches!(state.as_deref(),Some("active"|"forget_requested"|"forgotten"))) {return Err(UNAVAILABLE.into());}
        if !present && (stored_uri.is_some() || content.is_some() || state.is_some() || source_workspace.is_some() || project.is_some()) {return Err(UNAVAILABLE.into());}
        let latest:Option<i64>=tx.query_row("SELECT MAX(source_revision) FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2",params![owner,sid],|r|repository::s08_bounded_first(r,0)).map_err(|_|UNAVAILABLE.to_string())?;
        let erased:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL))",params![owner,sid],|r|repository::s08_bounded_first(r,0)).map_err(|_|UNAVAILABLE.to_string())?;
        let navigation=if !present || erased || state.as_deref()!=Some("active") {"unavailable"} else if latest!=Some(revision) {"stale"} else {"unverified"};
        let mut result=anchor.clone();result["navigation"]=json!(navigation);Ok(result)
    } else {
        let content=content.ok_or_else(||UNAVAILABLE.to_string())?;
        if kind.as_deref()!=Some(SOURCE_TYPE) || stored_uri.as_deref()!=Some(uri) || source_workspace.as_deref()!=Some(workspace) || project.as_deref()!=Some("") || !hex64(&content) || source_id(account,workspace,uri)?!=sid  {return Err(UNAVAILABLE.into());}
        Ok(json!({"uri":uri,"revision":revision,"content_sha256":content}))
    }
}

fn verify_host_projection(tx:&rusqlite::Transaction<'_>,view:&repository::owner_custody::VerifiedCustodyView<'_,'_>,owner:&str,workspace:&str,account:&str,candidate:&str,source:&Value)->Result<(),String> {
    if source.get("kind").and_then(Value::as_str)==Some("host_user_body_anchor") {
        let captured=host_anchor_live_proof(tx,owner,workspace,account,candidate)?;
        let proof=view.for_captured(tx,&captured).map_err(|_|UNAVAILABLE.to_string())?;
        if proof.current_owner()!=owner || proof.account_id()!=account {return Err(UNAVAILABLE.into());}
    }
    Ok(())
}

/// target info for list / preview / approval: recorded block + head, the head now, and whether the proposal is stale.
fn target_view(tx: &rusqlite::Transaction<'_>, owner: &str, candidate: &str) -> Result<Option<Value>, String> {
    let locator: Option<String> = tx.query_row("SELECT locator_json FROM fm_v2_evidence WHERE owner_id=?1 AND evidence_id=?2", params![owner, evidence_id(candidate)?], |r| r.get(0)).optional().map_err(|e| e.to_string())?;
    let locator = locator.and_then(|l| crate::field_authority::parse_unique_json(&l).ok()).ok_or_else(|| UNAVAILABLE.to_string())?;
    if !preextracted_locator_ok(&locator, candidate) { return Err(UNAVAILABLE.into()); }
    let Some(target) = locator.get("target") else { return Ok(None) };
    let block = target["block_id"].as_str().unwrap_or_default().to_owned();
    let expected = target["expected_revision"].as_i64().unwrap_or(0);
    let current: Option<i64> = tx.query_row("SELECT current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND block_id=?2", params![owner, block], |r| r.get(0)).optional().map_err(|e| e.to_string())?;
    Ok(Some(json!({"block_id": block, "expected_revision": expected, "current_revision": current, "predicate": target["predicate"], "claim_slot": target["claim_slot"],
        "before": target.get("before").cloned().unwrap_or(Value::Null), "stale": current != Some(expected)})))
}

/// Read-only, transaction-bound facts for the guarded-forget owner.
/// This is not erasure authority: the caller must verify account custody, the
/// complete closure and zero recovery, then revalidate in the same transaction.
#[allow(dead_code)]
pub(crate) struct PreextractedEraseMember<'tx,'conn> {
    tx: &'tx rusqlite::Transaction<'conn>,
    pub owner: String,
    pub workspace: String,
    pub candidate: String,
    pub curated: Option<String>,
    pub evidence: String,
    pub source: String,
    pub source_revision: i64,
    pub retain_source: bool,
    pub block: Option<String>,
    pub conflict: Option<String>,
    pub conflict_target: Option<String>,
}

impl PreextractedEraseMember<'_,'_> {
    #[allow(dead_code)]
    fn assert_transaction(&self, tx:&rusqlite::Transaction<'_>) -> Result<(),String> {
        if !std::ptr::eq(self.tx as *const _ as *const (),tx as *const _ as *const ()) {return Err(UNAVAILABLE.into());}
        Ok(())
    }
}

#[allow(dead_code)]
pub(crate) fn plan_preextracted_erase_member<'tx,'conn>(
    tx: &'tx rusqlite::Transaction<'conn>, owner: &str, workspace: &str, account: &str, candidate: &str,
) -> Result<PreextractedEraseMember<'tx,'conn>, String> {
    if !candidate.strip_prefix(CANDIDATE_PREFIX).is_some_and(hex64) { return Err(UNAVAILABLE.into()); }
    let row: Option<(String,String,i64,String,Option<String>,String,String,String,String,String)> = tx.query_row(
        "SELECT source,source_uri,source_revision,evidence_role,accepted_curated_id,status,content,content_hash,source_event_id,dedup_key FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3",
        params![candidate,owner,workspace], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?)),
    ).optional().map_err(|_| UNAVAILABLE.to_string())?;
    let (kind,uri,revision,role,curated,status,content,content_hash,event,dedup) = row.ok_or_else(|| UNAVAILABLE.to_string())?;
    let suffix=candidate.strip_prefix(CANDIDATE_PREFIX).ok_or_else(||UNAVAILABLE.to_string())?;
    if kind != "preextracted" || role != "inferred" || revision < 1 || content_hash!=crate::privacy::content_hash(&content) || event!=format!("preextracted:{suffix}") || dedup!=format!("preextracted:{workspace}:{suffix}") ||
        !matches!((status.as_str(),curated.as_ref()), ("pending" | "rejected", None) | ("accepted", Some(_))) {
        return Err(UNAVAILABLE.into());
    }
    let evidence=evidence_id(candidate)?;
    let projection=source_projection(tx,owner,workspace,account,candidate,&uri,revision)?;
    let retain_source=projection.get("kind").and_then(Value::as_str)==Some("host_user_body_anchor");
    let source=if retain_source {let reference:repository::HostArchiveSourceRef=serde_json::from_value(projection.get("reference").cloned().ok_or_else(||UNAVAILABLE.to_string())?).map_err(|_|UNAVAILABLE.to_string())?;repository::host_archive_source_id(&reference).map_err(|_|UNAVAILABLE.to_string())?} else {source_id(account,workspace,&uri)?};
    let row: Option<(String,String,String,i64,String,String)> = tx.query_row(
        "SELECT e.source_id,e.locator_json,s.source_type,s.source_revision,s.source_uri,s.content_hash FROM fm_v2_evidence e JOIN fm_v2_sources s ON s.owner_id=e.owner_id AND s.source_id=e.source_id AND s.source_revision=e.source_revision WHERE e.owner_id=?1 AND e.evidence_id=?2 AND e.locator_type='preextracted_fact' AND s.workspace_key=?3 AND s.project_key=''",
        params![owner,evidence,workspace], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)),
    ).optional().map_err(|_| UNAVAILABLE.to_string())?;
    let (actual,locator,source_type,source_revision,source_uri,source_content_hash)=row.ok_or_else(|| UNAVAILABLE.to_string())?;
    let locator=crate::field_authority::parse_unique_json(&locator).map_err(|_| UNAVAILABLE.to_string())?;
    if actual!=source || source_type!=if retain_source {"host_archive_part.v1"} else {SOURCE_TYPE} || source_revision!=revision || source_uri!=uri || !hex64(&source_content_hash) || !preextracted_locator_ok(&locator,candidate) {return Err(UNAVAILABLE.into());}
    let mut stmt=tx.prepare("SELECT b.block_id,b.current_revision,b.claim_slot,b.workspace_key,b.project_key FROM fm_v2_revision_evidence e JOIN fm_v2_knowledge_blocks b ON b.owner_id=e.owner_id AND b.block_id=e.block_id WHERE e.owner_id=?1 AND e.evidence_id=?2 ORDER BY b.block_id LIMIT 2").map_err(|_| UNAVAILABLE.to_string())?;
    let blocks=stmt.query_map(params![owner,evidence],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,String>(4)?))).map_err(|_| UNAVAILABLE.to_string())?.collect::<Result<Vec<_>,_>>().map_err(|_| UNAVAILABLE.to_string())?;
    drop(stmt);
    let block=match (&curated,blocks.as_slice()) {
        (None,[]) => None,
        (Some(record),[(id,1,slot,ws,project)]) if record==slot && ws==workspace && project.is_empty() => Some(id.clone()),
        _ => return Err(UNAVAILABLE.into()),
    };
    if let Some(record)=&curated {
        let expected_record=format!("m_{}", &blake3::hash(candidate.as_bytes()).to_hex()[..24]);
        if record!=&expected_record {return Err(UNAVAILABLE.into());}
        let (exists,links):(bool,i64)=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE id=?1 AND owner=?2 AND workspace_id=?3),(SELECT count(*) FROM candidates WHERE accepted_curated_id=?1)",params![record,owner,workspace],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_| UNAVAILABLE.to_string())?;
        if !exists || links!=1 {return Err(UNAVAILABLE.into());}
        let (saved,saved_source,raw):(String,String,String)=tx.query_row("SELECT content,source,metadata FROM curated WHERE id=?1 AND owner=?2 AND workspace_id=?3",params![record,owner,workspace],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).map_err(|_|UNAVAILABLE.to_string())?;
        let metadata=crate::field_authority::parse_unique_json(&raw).map_err(|_|UNAVAILABLE.to_string())?;
        if saved!=content || saved_source!="preextracted" || metadata.get("candidate_id").and_then(Value::as_str)!=Some(candidate) || metadata.get("source_uri").and_then(Value::as_str)!=Some(uri.as_str()) || metadata.get("source_revision").and_then(Value::as_i64)!=Some(revision) || metadata.get("content_hash").and_then(Value::as_str)!=Some(content_hash.as_str()) || metadata.get("manually_reviewed")!=Some(&json!(true)) {return Err(UNAVAILABLE.into());}
        let id=block.as_ref().ok_or_else(|| UNAVAILABLE.to_string())?;
        let (revisions,evidence_refs):(i64,i64)=tx.query_row("SELECT (SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2),(SELECT count(*) FROM fm_v2_revision_evidence WHERE owner_id=?1 AND block_id=?2 AND revision=1)",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_| UNAVAILABLE.to_string())?;
        let (reason,turn,value,event,actor):(String,String,String,Option<String>,String)=tx.query_row("SELECT r.reason,c.turn_id,json_extract(r.value_json,'$.value'),r.source_event_id,r.actor_type FROM fm_v2_knowledge_revisions r JOIN candidates c ON c.owner=r.owner_id AND c.id=?3 WHERE r.owner_id=?1 AND r.block_id=?2 AND r.revision=1",params![owner,id,candidate],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).map_err(|_| UNAVAILABLE.to_string())?;
        let Some(meta)=metadata.as_object() else {return Err(UNAVAILABLE.into())};
        if revisions!=1 || evidence_refs!=1 || value!=content || event.as_deref()!=Some(format!("memory:{record}").as_str()) || actor!="user" || meta.get("admission_reason").and_then(Value::as_str)!=Some(reason.as_str()) || meta.get("turn_id").and_then(Value::as_str)!=Some(turn.as_str()) || meta.get("raw_evidence_ids")!=Some(&json!([])) || meta.keys().any(|k|!matches!(k.as_str(),"candidate_id"|"turn_id"|"raw_evidence_ids"|"admission_reason"|"manually_reviewed"|"source_uri"|"source_revision"|"content_hash"|"uses")) {return Err(UNAVAILABLE.into());}
    }
    let conflict=conflict_id(candidate)?;
    let candidate_refs:i64=tx.query_row("SELECT count(*) FROM fm_v2_candidate_evidence WHERE owner_id=?1 AND evidence_id=?2",params![owner,evidence],|r|r.get(0)).map_err(|_| UNAVAILABLE.to_string())?;
    if candidate_refs!=0 {return Err(UNAVAILABLE.into());}
    let conflict_row:Option<(String,String,String,Option<String>)>=tx.query_row("SELECT block_id,competing_payload,state,resolved_change_set_id FROM fm_v2_conflicts WHERE owner_id=?1 AND conflict_id=?2",params![owner,conflict],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_| UNAVAILABLE.to_string())?;
    if let Some((target,raw,state,resolved))=&conflict_row {
        let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_conflict_events WHERE owner_id=?1 AND conflict_id=?2 AND state='open'",params![owner,conflict],|r|r.get(0)).map_err(|_| UNAVAILABLE.to_string())?;
        let total:i64=tx.query_row("SELECT count(*) FROM fm_v2_conflict_events WHERE owner_id=?1 AND conflict_id=?2",params![owner,conflict],|r|r.get(0)).map_err(|_| UNAVAILABLE.to_string())?;
        if state!="open" || resolved.is_some() || count!=1 || total!=1 {return Err(UNAVAILABLE.into());}
        let payload=crate::field_authority::parse_unique_json(raw).map_err(|_| UNAVAILABLE.to_string())?;
        if locator.get("target").and_then(|v|v.get("block_id")).and_then(Value::as_str)!=Some(target.as_str()) || payload.get("candidate_id").and_then(Value::as_str)!=Some(candidate) || payload.get("evidence_id").and_then(Value::as_str)!=Some(evidence.as_str()) || block.as_deref()==Some(target.as_str()) {return Err(UNAVAILABLE.into());}
    }
    let conflict_target=conflict_row.as_ref().map(|r|r.0.clone());
    Ok(PreextractedEraseMember { tx,owner:owner.into(),workspace:workspace.into(),candidate:candidate.into(),curated,evidence,source,source_revision:revision,retain_source,block,conflict:conflict_row.map(|_|conflict),conflict_target })
}

/// Verify a closed F8-only selection; any mixed, shared or unexplained
/// footprint fails before erasure. Account custody is checked by the caller.
#[allow(dead_code)]
pub(crate) fn plan_preextracted_erase_closure<'tx,'conn>(
    tx:&'tx rusqlite::Transaction<'conn>, owner:&str, workspace:&str, account:&str, closure:&ForgetClosure,
) -> Result<Vec<PreextractedEraseMember<'tx,'conn>>,String> {
    if closure.candidate_ids.is_empty() || closure.candidate_ids.len()>1000 || !closure.raw_ids.is_empty() || !closure.native_event_links.is_empty() {return Err(UNAVAILABLE.into());}
    let ids:std::collections::BTreeSet<_>=closure.candidate_ids.iter().collect();
    if ids.len()!=closure.candidate_ids.len() {return Err(UNAVAILABLE.into());}
    let mut members=Vec::with_capacity(ids.len());
    for id in ids {members.push(plan_preextracted_erase_member(tx,owner,workspace,account,id)?);}
    let actual:std::collections::BTreeSet<_>=members.iter().filter_map(|m|m.curated.as_ref()).collect();
    let selected:std::collections::BTreeSet<_>=closure.curated_ids.iter().collect();
    if actual!=selected || selected.len()!=closure.curated_ids.len() {return Err(UNAVAILABLE.into());}
    // Generic graph cleanup may touch only the selected curated projection and
    // its ordinary outgoing category edge; other adjacency is not F8 custody.
    let mut graph_curated=std::collections::BTreeSet::new();
    for node in &closure.graph_node_ids {
        let row:Option<(Option<String>,Option<String>,Option<String>,String)>=tx.query_row("SELECT ref_table,ref_id,candidate_id,kind FROM graph_nodes WHERE id=?1 AND owner=?2 AND workspace_id=?3",params![node,owner,workspace],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(|_|UNAVAILABLE.to_string())?;
        let (table,id,candidate,kind)=row.ok_or_else(||UNAVAILABLE.to_string())?;let id=id.ok_or_else(||UNAVAILABLE.to_string())?;
        if table.as_deref()!=Some("curated") || kind!="memory" || candidate.is_some() || !actual.contains(&id) || !graph_curated.insert(id) {return Err(UNAVAILABLE.into());}
        let invalid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM graph_edges e LEFT JOIN graph_nodes d ON d.id=e.dst_id WHERE (e.src_id=?1 OR e.dst_id=?1) AND NOT(e.src_id=?1 AND e.owner=?2 AND e.workspace_id=?3 AND e.tag='is' AND e.fact_id IS NULL AND e.candidate_id IS NULL AND d.owner=?2 AND d.workspace_id=?3 AND d.kind='category'))",params![node,owner,workspace],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        if invalid {return Err(UNAVAILABLE.into());}
    }
    if graph_curated.len()!=actual.len() {return Err(UNAVAILABLE.into());}
    let protected:std::collections::BTreeSet<_>=members.iter().filter_map(|m|m.conflict_target.as_deref()).collect();
    if members.iter().filter_map(|m|m.block.as_deref()).any(|id|protected.contains(id)) {return Err(UNAVAILABLE.into());}
    let mut sources=std::collections::BTreeMap::<(&str,i64),usize>::new();
    for m in &members {*sources.entry((&m.source,m.source_revision)).or_default()+=1;}
    for ((source,revision),selected_count) in sources {
        let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_evidence WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3",params![owner,source,revision],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        if count!=selected_count as i64 {return Err(UNAVAILABLE.into());}
    }
    Ok(members)
}

/// Delete only the V2 portion of a previously verified F8-only closure.
/// The caller holds account custody and a guarded zero-recovery erasure
/// context, and writes the durable terminal before commit.
#[allow(dead_code)]
pub(crate) fn apply_preextracted_erase<'tx,'conn>(
    tx:&'tx rusqlite::Transaction<'conn>, members:&[PreextractedEraseMember<'tx,'conn>], erasure_id:&str,
) -> Result<(),String> {
    if members.is_empty() {return Err(UNAVAILABLE.into());}
    let owner=&members[0].owner; let workspace=&members[0].workspace;
    if members.iter().any(|m|m.owner!=*owner || m.workspace!=*workspace || m.assert_transaction(tx).is_err()) {return Err(UNAVAILABLE.into());}
    let authorized:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND erasure_id=?2 AND source_id IS NULL AND authorized_by=?1 AND reason IN ('account-bound guarded batch forget','owner selective reset'))",params![owner,erasure_id],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
    if !authorized {return Err(UNAVAILABLE.into());}
    let mut changes=std::collections::BTreeMap::<String,&str>::new();
    for m in members {
        // Preflight all externally dependent references before the first delete.
        let v2_candidate:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_candidates WHERE owner_id=?1 AND candidate_id=?2)",params![owner,m.candidate],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        if v2_candidate {return Err(UNAVAILABLE.into());}
        if let Some(block)=&m.block {
            let (created,revision):(String,String)=tx.query_row("SELECT b.created_change_set_id,r.change_set_id FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=1 WHERE b.owner_id=?1 AND b.block_id=?2 AND b.current_revision=1 AND b.workspace_key=?3 AND b.project_key=''",params![owner,block,workspace],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_|UNAVAILABLE.to_string())?;
            if created!=revision || changes.insert(created,"knowledge_revision").is_some() {return Err(UNAVAILABLE.into());}
            let foreign:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_conflicts WHERE owner_id=?1 AND block_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions r,json_tree(r.value_json) j WHERE r.owner_id=?1 AND r.block_id<>?2 AND j.type='text' AND j.value=?2) OR EXISTS(SELECT 1 FROM fm_v2_revision_metadata m,json_tree(m.metadata_json) j WHERE m.owner_id=?1 AND m.block_id<>?2 AND j.type='text' AND j.value=?2) OR EXISTS(SELECT 1 FROM fm_v2_candidate_revisions c,json_tree(c.proposal_json) j WHERE c.owner_id=?1 AND j.type='text' AND j.value=?2) OR EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions r,json_tree(r.activation_rationale) j WHERE r.owner_id=?1 AND r.block_id<>?2 AND j.type='text' AND j.value=?2)",params![owner,block],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
            if foreign {return Err(UNAVAILABLE.into());}
        }
        if let Some(conflict)=&m.conflict {
            let (created,event):(String,String)=tx.query_row("SELECT c.created_change_set_id,e.change_set_id FROM fm_v2_conflicts c JOIN fm_v2_conflict_events e ON e.owner_id=c.owner_id AND e.conflict_id=c.conflict_id AND e.state='open' WHERE c.owner_id=?1 AND c.conflict_id=?2 AND c.state='open' AND c.resolved_change_set_id IS NULL",params![owner,conflict],|r|Ok((r.get(0)?,r.get(1)?))).map_err(|_|UNAVAILABLE.to_string())?;
            if created!=event || changes.insert(created,"conflict_recorded").is_some() {return Err(UNAVAILABLE.into());}
        }
    }
    for (id,kind) in &changes {
        let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4) AND (SELECT count(*) FROM fm_v2_outbox WHERE owner_id=?1 AND change_set_id=?3)=1",params![owner,format!("outbox_{id}_{kind}"),id,kind],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        if !valid {return Err(UNAVAILABLE.into());}
    }
    for m in members {
        if let Some(conflict)=&m.conflict {
            if tx.execute("DELETE FROM fm_v2_conflict_events WHERE owner_id=?1 AND conflict_id=?2 AND state='open'",params![owner,conflict]).map_err(|_|UNAVAILABLE.to_string())?!=1 || tx.execute("DELETE FROM fm_v2_conflicts WHERE owner_id=?1 AND conflict_id=?2 AND state='open'",params![owner,conflict]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
        }
        if let Some(block)=&m.block {
            for table in ["fm_v2_revision_field_authority","fm_v2_revision_metadata"] {
                tx.execute(&format!("DELETE FROM {table} WHERE owner_id=?1 AND block_id=?2 AND revision=1"),params![owner,block]).map_err(|_|UNAVAILABLE.to_string())?;
            }
            if tx.execute("DELETE FROM fm_v2_revision_evidence WHERE owner_id=?1 AND block_id=?2 AND revision=1 AND evidence_id=?3",params![owner,block,m.evidence]).map_err(|_|UNAVAILABLE.to_string())?!=1 || tx.execute("DELETE FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=1",params![owner,block]).map_err(|_|UNAVAILABLE.to_string())?!=1 || tx.execute("DELETE FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND block_id=?2 AND current_revision=1",params![owner,block]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
        }
        if tx.execute("DELETE FROM fm_v2_evidence WHERE owner_id=?1 AND evidence_id=?2 AND source_id=?3 AND source_revision=?4",params![owner,m.evidence,m.source,m.source_revision]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
    }
    let sources:std::collections::BTreeSet<_>=members.iter().filter(|m|!m.retain_source).map(|m|(&m.source,m.source_revision)).collect();
    for (source,revision) in sources {
        if tx.execute("DELETE FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 AND NOT EXISTS(SELECT 1 FROM fm_v2_evidence WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3)",params![owner,source,revision]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
    }
    for (id,kind) in changes {
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,format!("outbox_{id}_{kind}"),id,kind]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
        // Foreign keys and this explicit reference fence both refuse unexplained
        // sharing rather than extending the deletion by adjacency.
        if tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND NOT EXISTS(SELECT 1 FROM fm_v2_entities WHERE owner_id=?1 AND created_change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_entity_revisions WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND created_change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_candidates WHERE owner_id=?1 AND created_change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_candidate_revisions WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_conflicts WHERE owner_id=?1 AND (created_change_set_id=?2 OR resolved_change_set_id=?2)) AND NOT EXISTS(SELECT 1 FROM fm_v2_conflict_events WHERE owner_id=?1 AND change_set_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND change_set_id=?2)",params![owner,id]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
    }
    Ok(())
}

// Closed F8 operation proof uses the existing change-set/outbox owner. These
// helpers are intentionally not called until forget/reset/rename custody is
// wired; recording a live proof without its terminal lifecycle is unsafe.
const STAGE_RECEIPT: &str = "s03.preextracted-stage-receipt.v1";
const STAGE_EVENT: &str = "preextracted_stage_receipt";
#[allow(dead_code)]
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct StageReceipt {
    version: String, owner: String, workspace_id: String, account_id: String,
    request_sha256: String, body_sha256: String, response: Value, items: Vec<StageItemBinding>,
    #[serde(default,skip_serializing_if="Option::is_none")] origin:Option<String>,
}
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct StageItemBinding { candidate_id:String, input_sha256:String }
fn stage_receipt_valid(r:&StageReceipt) -> bool {
    if r.origin.as_deref().is_some_and(|v|v!="host_user_body_anchor.v1") || r.version!=STAGE_RECEIPT || !valid_text(&r.owner,512) || !valid_text(&r.workspace_id,512) || !valid_text(&r.account_id,512) || !hex64(&r.request_sha256) || !hex64(&r.body_sha256) {return false;}
    let Some(response)=r.response.as_object() else {return false};
    if response.len()!=2 || response.get("version")!=Some(&json!(1)) {return false;}
    let Some(items)=response.get("items").and_then(Value::as_array) else {return false};
    if items.is_empty() || items.len()>100 || r.items.len()!=items.len() || (r.origin.is_some() && items.len()!=1) {return false;}
    let mut seen=std::collections::BTreeSet::new();
    for (item,binding) in items.iter().zip(&r.items) {
        let Some(o)=item.as_object() else {return false};
        let Some(id)=o.get("candidate_id").and_then(Value::as_str) else {return false};
        if !id.strip_prefix(CANDIDATE_PREFIX).is_some_and(hex64) || !seen.insert(id) || binding.candidate_id!=id || !hex64(&binding.input_sha256) {return false;}
        match o.get("state").and_then(Value::as_str) {
            Some("staged" | "duplicate") if o.len()==2 => (),
            Some("conflict") if o.len()==3 && o.get("conflict_id").and_then(Value::as_str).is_some_and(|v| conflict_id(id).ok().as_deref()==Some(v)) => (),
            _ => return false,
        }
    }
    true
}
#[allow(dead_code)]
fn stage_receipt_id(account: &str, workspace: &str, key: &str) -> Result<String, String> {
    Ok(format!("cs_f8_stage_{}", request_hash(account, workspace, key)?))
}
fn stage_item_hash(item:&PreextractedItem)->Result<String,String> {
    Ok(hash(&serde_json::to_vec(&("s03.preextracted-stage-item.v1",item)).map_err(|_|UNAVAILABLE.to_string())?))
}
#[allow(dead_code)]
fn stage_body_hash(request:&PreextractedStageRequest) -> Result<String,String> {
    // Bind ordered typed inputs, key, account, workspace and policy. The
    // captured owner is stored separately and verified through custody, so an
    // authorized rename can preserve the same logical request identity.
    prepare(request)?;
    let bytes=serde_json::to_vec(&("s03.preextracted-stage-body.v1",request.version,&request.workspace_id,&request.account_id,&request.policy_projection,&request.request_key,&request.items)).map_err(|_|UNAVAILABLE.to_string())?;
    Ok(hash(&bytes))
}
#[allow(dead_code)]
fn read_stage_receipt(tx: &Connection, owner: &str, id: &str) -> Result<Option<StageReceipt>, String> {
    let event = format!("outbox_{id}_{STAGE_EVENT}");
    let row:Option<(String,Option<String>,String,String,String,Option<String>)> = tx.query_row(
        "SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",
        params![owner,id,STAGE_RECEIPT],|r|Ok((repository::s08_bounded_first(r,0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)),
    ).optional().map_err(|_|UNAVAILABLE.to_string())?;
    let Some((digest,raw,actor,actor_id,contract,source))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,event],|r|repository::s08_bounded_first(r,0)).map_err(|_|UNAVAILABLE.to_string())?;
        return if collision {Err(UNAVAILABLE.into())} else {Ok(None)};
    };
    let raw=raw.ok_or_else(||UNAVAILABLE.to_string())?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,STAGE_EVENT],|r|Ok((repository::s08_bounded_first(r,0)?,r.get(1)?))).optional().map_err(|_|UNAVAILABLE.to_string())?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(|_|UNAVAILABLE.to_string())?;
    let receipt:StageReceipt=serde_json::from_value(value).map_err(|_|UNAVAILABLE.to_string())?;
    if digest!=hash(raw.as_bytes()) || actor!="automation" || actor_id!="preextracted" || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || out!=Some((Some(raw),0)) || !stage_receipt_valid(&receipt) || id!=format!("cs_f8_stage_{}",receipt.request_sha256) {return Err(UNAVAILABLE.into());}
    Ok(Some(receipt))
}
#[allow(dead_code)]
fn write_stage_receipt(tx:&rusqlite::Transaction<'_>, id:&str, receipt:&StageReceipt) -> Result<(),String> {
    if read_stage_receipt(tx,&receipt.owner,id)?.is_some() {return Err(KEY_CONFLICT.into());}
    let raw=serde_json::to_string(receipt).map_err(|_|UNAVAILABLE.to_string())?;
    if raw.len()>65536 || !stage_receipt_valid(receipt) || id!=format!("cs_f8_stage_{}",receipt.request_sha256) {return Err(UNAVAILABLE.into());}
    let now=chrono::Utc::now().to_rfc3339(); let event=format!("outbox_{id}_{STAGE_EVENT}");
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','preextracted',?3,NULL,?4,?5,?6,?7)",params![receipt.owner,id,STAGE_RECEIPT,crate::ontology::CONTRACT_VERSION,hash(raw.as_bytes()),now,raw]).map_err(|_|UNAVAILABLE.to_string())?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![receipt.owner,event,id,STAGE_EVENT,raw,now]).map_err(|_|UNAVAILABLE.to_string())?;
    Ok(())
}

/// Verify the paired, typed live stage proof for a candidate. The caller
/// must additionally verify captured-owner custody under the same snapshot.
pub(crate) fn host_anchor_live_proof(conn:&Connection,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<String,String> {
    let turn:Option<String>=conn.query_row("SELECT turn_id FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3 AND source='preextracted' AND evidence_role='inferred'",params![candidate,owner,workspace],|r|repository::s08_bounded_first(r,0)).optional().map_err(|_|UNAVAILABLE.to_string())?;
    let turn=turn.filter(|v|hex64(v)).ok_or_else(||UNAVAILABLE.to_string())?;
    let receipt=read_stage_receipt(conn,owner,&format!("cs_f8_stage_{turn}"))?.ok_or_else(||UNAVAILABLE.to_string())?;
    if receipt.request_sha256!=turn || receipt.workspace_id!=workspace || receipt.account_id!=account || receipt.origin.as_deref()!=Some("host_user_body_anchor.v1") || !receipt.items.iter().any(|v|v.candidate_id==candidate) {return Err(UNAVAILABLE.into());}
    Ok(receipt.owner)
}

// After guarded forget, keep only bounded request hashes and the verified
// forget receipt identity. A terminal never returns a live candidate result.
const STAGE_TERMINAL: &str = "s03.preextracted-stage-terminal.v1";
const TERMINAL_EVENT: &str = "preextracted_stage_terminal";
#[allow(dead_code)]
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct StageTerminal {
    version:String, owner:String, workspace_id:String, account_id:String,
    request_sha256:String, body_sha256:String, lifecycle_kind:String, lifecycle_receipt_id:String, survivors:Vec<StageItemBinding>,
    #[serde(default,skip_serializing_if="Option::is_none")] selective_partition_sha256:Option<String>,
    #[serde(default,skip_serializing_if="Option::is_none")] origin:Option<String>,
}
#[allow(dead_code)]
fn stage_terminal_id(request_sha256:&str)->Result<String,String> {
    if !hex64(request_sha256) {return Err(UNAVAILABLE.into());}
    Ok(format!("cs_f8_terminal_{request_sha256}"))
}
#[allow(dead_code)]
fn read_stage_terminal(tx:&rusqlite::Transaction<'_>,owner:&str,request_sha256:&str)->Result<Option<StageTerminal>,String> {
    let id=stage_terminal_id(request_sha256)?;let event=format!("outbox_{id}_{TERMINAL_EVENT}");
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,STAGE_TERMINAL],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|UNAVAILABLE.to_string())?;
    let Some((digest,raw,actor,actor_id,contract,source))=row else {
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![owner,id,event],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        return if collision {Err(UNAVAILABLE.into())} else {Ok(None)};
    };
    let raw=raw.ok_or_else(||UNAVAILABLE.to_string())?;
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,TERMINAL_EVENT],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|UNAVAILABLE.to_string())?;
    let terminal:StageTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(|_|UNAVAILABLE.to_string())?).map_err(|_|UNAVAILABLE.to_string())?;
    if digest!=hash(raw.as_bytes()) || actor!="user" || actor_id!=terminal.owner || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || out!=Some((Some(raw),0)) || terminal.origin.as_deref().is_some_and(|v|v!="host_user_body_anchor.v1") || terminal.version!=STAGE_TERMINAL || !valid_text(&terminal.owner,512) || terminal.request_sha256!=request_sha256 || !hex64(&terminal.body_sha256) || !valid_text(&terminal.account_id,512) || !valid_text(&terminal.workspace_id,512) || !matches!(terminal.lifecycle_kind.as_str(),"forget"|"reset"|"selective") || !valid_text(&terminal.lifecycle_receipt_id,512) || terminal.survivors.len()>100 || (terminal.origin.is_some() && !terminal.survivors.is_empty()) || (terminal.lifecycle_kind=="reset" && !terminal.survivors.is_empty()) || if terminal.lifecycle_kind=="selective" {!terminal.selective_partition_sha256.as_deref().is_some_and(hex64)} else {terminal.selective_partition_sha256.is_some()} {return Err(UNAVAILABLE.into());}
    let mut seen=std::collections::BTreeSet::new();
    if terminal.survivors.iter().any(|v|!v.candidate_id.strip_prefix(CANDIDATE_PREFIX).is_some_and(hex64) || !hex64(&v.input_sha256) || !seen.insert(&v.candidate_id)) {return Err(UNAVAILABLE.into());}
    Ok(Some(terminal))
}

fn verify_forget_candidates(
    tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,receipt_id:&str,candidates:&[String],
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget,
)->Result<(),String> {
    let receipt=crate::repository::guarded_batch_forget::verify_receipt_by_id(tx,owner,account,workspace,receipt_id,view,budget).map_err(|_|UNAVAILABLE.to_string())?;
    let raw:String=tx.query_row("SELECT affected_ids FROM memory_tombstones WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![owner,workspace,receipt.tombstone_id],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
    budget.check(raw.len()).map_err(|_|UNAVAILABLE.to_string())?;
    if hash(raw.as_bytes())!=receipt.closure_sha256 {return Err(UNAVAILABLE.into());}
    let closure:ForgetClosure=serde_json::from_str(&raw).map_err(|_|UNAVAILABLE.to_string())?;
    let selected:std::collections::BTreeSet<_>=candidates.iter().collect();let actual:std::collections::BTreeSet<_>=closure.candidate_ids.iter().collect();
    if selected.len()!=candidates.len() || selected!=actual {return Err(UNAVAILABLE.into());}
    Ok(())
}

/// Terminalize one proven stage receipt after the guarded forget receipt is
/// written in this transaction. The caller must enumerate all intersecting
/// receipts; this helper is not a closure or erasure authority.
#[allow(dead_code)]
fn terminalize_stage_receipt(
    tx:&rusqlite::Transaction<'_>, owner:&str, workspace:&str, account:&str,
    request_sha256:&str, forget_receipt_id:&str, candidates:&[String],
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,
    budget:&mut crate::repository::ResetFenceBudget,
)->Result<(),String> {
    let id=format!("cs_f8_stage_{request_sha256}");
    let receipt=read_stage_receipt(tx,owner,&id)?.ok_or_else(||UNAVAILABLE.to_string())?;
    if receipt.workspace_id!=workspace || receipt.account_id!=account || receipt.request_sha256!=request_sha256 || read_stage_terminal(tx,owner,request_sha256)?.is_some() {return Err(UNAVAILABLE.into());}
    let proof=view.for_captured(tx,&receipt.owner).map_err(|_|UNAVAILABLE.to_string())?;
    if proof.current_owner()!=owner || proof.account_id()!=account {return Err(UNAVAILABLE.into());}
    let selected=receipt.response["items"].as_array().ok_or_else(||UNAVAILABLE.to_string())?.iter().filter_map(|v|v["candidate_id"].as_str()).any(|id|candidates.iter().any(|c|c==id));
    if !selected {return Err(UNAVAILABLE.into());}
    verify_forget_candidates(tx,owner,workspace,account,forget_receipt_id,candidates,view,budget)?;
    let survivors:Vec<_>=receipt.items.into_iter().filter(|item|!candidates.contains(&item.candidate_id)).collect();
    for item in &survivors {
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3 AND source='preextracted')",params![item.candidate_id,owner,workspace],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        if !exists {return Err(UNAVAILABLE.into());}
    }
    let terminal=StageTerminal{version:STAGE_TERMINAL.into(),owner:receipt.owner.clone(),workspace_id:workspace.into(),account_id:account.into(),request_sha256:request_sha256.into(),body_sha256:receipt.body_sha256,lifecycle_kind:"forget".into(),lifecycle_receipt_id:forget_receipt_id.into(),survivors,selective_partition_sha256:None,origin:receipt.origin};
    let raw=serde_json::to_string(&terminal).map_err(|_|UNAVAILABLE.to_string())?;if raw.len()>65536 {return Err(UNAVAILABLE.into());}
    let event=format!("outbox_{id}_{STAGE_EVENT}");
    if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,STAGE_EVENT]).map_err(|_|UNAVAILABLE.to_string())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,STAGE_RECEIPT]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
    let terminal_id=stage_terminal_id(request_sha256)?;let terminal_event=format!("outbox_{terminal_id}_{TERMINAL_EVENT}");let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![owner,terminal_id,terminal.owner,STAGE_TERMINAL,crate::ontology::CONTRACT_VERSION,hash(raw.as_bytes()),now,raw]).map_err(|_|UNAVAILABLE.to_string())?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![owner,terminal_event,terminal_id,TERMINAL_EVENT,raw,now]).map_err(|_|UNAVAILABLE.to_string())?;
    Ok(())
}

/// Advance a verified forgotten-stage terminal when another surviving member
/// is forgotten. Existing proof is verified by the caller's inventory first.
#[allow(dead_code)]
fn terminalize_stage_survivor(
    tx:&rusqlite::Transaction<'_>, owner:&str, workspace:&str, account:&str,
    request_sha256:&str, forget_receipt_id:&str, candidates:&[String],
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,
    budget:&mut crate::repository::ResetFenceBudget,
)->Result<(),String> {
    let mut terminal=read_stage_terminal(tx,owner,request_sha256)?.ok_or_else(||UNAVAILABLE.to_string())?;
    let proof=view.for_captured(tx,&terminal.owner).map_err(|_|UNAVAILABLE.to_string())?;
    if !matches!(terminal.lifecycle_kind.as_str(),"forget"|"selective") || terminal.workspace_id!=workspace || terminal.account_id!=account || proof.current_owner()!=owner || proof.account_id()!=account || !terminal.survivors.iter().any(|v|candidates.contains(&v.candidate_id)) {return Err(UNAVAILABLE.into());}
    verify_terminal_lifecycle(tx,owner,account,&terminal,view,budget)?;
    verify_forget_candidates(tx,owner,workspace,account,forget_receipt_id,candidates,view,budget)?;
    terminal.survivors.retain(|v|!candidates.contains(&v.candidate_id));
    for item in &terminal.survivors {
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3 AND source='preextracted')",params![item.candidate_id,owner,workspace],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        if !exists {return Err(UNAVAILABLE.into());}
    }
    terminal.lifecycle_kind="forget".into();terminal.selective_partition_sha256=None;terminal.lifecycle_receipt_id=forget_receipt_id.into();
    let raw=serde_json::to_string(&terminal).map_err(|_|UNAVAILABLE.to_string())?;if raw.len()>65536{return Err(UNAVAILABLE.into());}
    let id=stage_terminal_id(request_sha256)?;let event=format!("outbox_{id}_{TERMINAL_EVENT}");
    if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,id,TERMINAL_EVENT]).map_err(|_|UNAVAILABLE.to_string())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,STAGE_TERMINAL]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![owner,id,terminal.owner,STAGE_TERMINAL,crate::ontology::CONTRACT_VERSION,hash(raw.as_bytes()),now,raw]).map_err(|_|UNAVAILABLE.to_string())?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![owner,event,id,TERMINAL_EVENT,raw,now]).map_err(|_|UNAVAILABLE.to_string())?;
    Ok(())
}

/// Enumerate and verify both sides of the F8 receipt family under custody.
/// Unknown, unpaired, oversized or cross-account facts fail closed.
#[allow(dead_code)]
pub(super) fn collect_stage_proofs(
    tx:&rusqlite::Transaction<'_>, owner:&str, account:&str,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,
    budget:&mut crate::repository::ResetFenceBudget,
)->Result<(Vec<(String,StageReceipt)>,Vec<StageTerminal>),String> {
    let mut stmt=tx.prepare("SELECT change_set_id,reason FROM fm_v2_change_sets WHERE owner_id=?1 AND reason IN (?2,?3) ORDER BY change_set_id").map_err(|_|UNAVAILABLE.to_string())?;
    let mut cursor=stmt.query(params![owner,STAGE_RECEIPT,STAGE_TERMINAL]).map_err(|_|UNAVAILABLE.to_string())?;
    let mut rows=Vec::new();
    while let Some(row)=cursor.next().map_err(|_|UNAVAILABLE.to_string())? {
        let id:String=row.get(0).map_err(|_|UNAVAILABLE.to_string())?;let reason:String=row.get(1).map_err(|_|UNAVAILABLE.to_string())?;
        budget.check(id.len().saturating_add(reason.len())).map_err(|_|UNAVAILABLE.to_string())?;rows.push((id,reason));
    }
    drop(cursor);drop(stmt);
    let mut live=Vec::new();let mut terminal=Vec::new();let mut expected=std::collections::BTreeSet::new();let mut requests=std::collections::BTreeSet::new();
    let mut bindings=std::collections::BTreeMap::<String,String>::new();
    for (id,reason) in rows {
        budget.check(id.len()).map_err(|_|UNAVAILABLE.to_string())?;
        if reason==STAGE_RECEIPT {
            let receipt=read_stage_receipt(tx,owner,&id)?.ok_or_else(||UNAVAILABLE.to_string())?;
            let size=serde_json::to_vec(&receipt).map_err(|_|UNAVAILABLE.to_string())?.len();budget.check(size).map_err(|_|UNAVAILABLE.to_string())?;
            let proof=view.for_captured(tx,&receipt.owner).map_err(|_|UNAVAILABLE.to_string())?;
            if proof.current_owner()!=owner || proof.account_id()!=account || receipt.account_id!=account || !requests.insert(receipt.request_sha256.clone()) {return Err(UNAVAILABLE.into());}
            for item in &receipt.items {
                if bindings.insert(item.candidate_id.clone(),item.input_sha256.clone()).is_some_and(|old|old!=item.input_sha256) {return Err(UNAVAILABLE.into());}
            }
            expected.insert(format!("outbox_{id}_{STAGE_EVENT}"));live.push((id,receipt));
        } else {
            let hash=id.strip_prefix("cs_f8_terminal_").ok_or_else(||UNAVAILABLE.to_string())?;
            let value=read_stage_terminal(tx,owner,hash)?.ok_or_else(||UNAVAILABLE.to_string())?;
            budget.check(serde_json::to_vec(&value).map_err(|_|UNAVAILABLE.to_string())?.len()).map_err(|_|UNAVAILABLE.to_string())?;
            let proof=view.for_captured(tx,&value.owner).map_err(|_|UNAVAILABLE.to_string())?;
            if proof.current_owner()!=owner || proof.account_id()!=account || value.account_id!=account || !requests.insert(value.request_sha256.clone()) {return Err(UNAVAILABLE.into());}
            verify_terminal_lifecycle(tx,owner,account,&value,view,budget)?;
            for item in &value.survivors {
                if bindings.insert(item.candidate_id.clone(),item.input_sha256.clone()).is_some_and(|old|old!=item.input_sha256) {return Err(UNAVAILABLE.into());}
            }
            expected.insert(format!("outbox_{id}_{TERMINAL_EVENT}"));terminal.push(value);
        }
    }
    let mut stmt=tx.prepare("SELECT event_id FROM fm_v2_outbox WHERE owner_id=?1 AND kind IN (?2,?3) ORDER BY event_id").map_err(|_|UNAVAILABLE.to_string())?;
    let mut cursor=stmt.query(params![owner,STAGE_EVENT,TERMINAL_EVENT]).map_err(|_|UNAVAILABLE.to_string())?;
    while let Some(row)=cursor.next().map_err(|_|UNAVAILABLE.to_string())? {
        let event:String=row.get(0).map_err(|_|UNAVAILABLE.to_string())?;
        budget.check(event.len()).map_err(|_|UNAVAILABLE.to_string())?;if !expected.remove(&event) {return Err(UNAVAILABLE.into());}
    }
    drop(cursor);drop(stmt);
    if !expected.is_empty() {return Err(UNAVAILABLE.into());}
    Ok((live,terminal))
}

/// Stage with a fully verified receipt inventory and lifecycle terminals.
#[allow(dead_code)]
fn stage_with_proof_in_tx(
    tx:&rusqlite::Transaction<'_>, request:&PreextractedStageRequest, items:&[Prepared],
    live:&[(String,StageReceipt)], terminals:&[StageTerminal], inputs:&[String], digest:&str, origin:Option<&str>,
)->Result<Value,String> {
    let request_sha=request_hash(&request.account_id,&request.workspace_id,&request.request_key)?;
    if inputs.len()!=items.len() {return Err(UNAVAILABLE.into());}
    if terminals.iter().any(|t|t.request_sha256==request_sha) {return Err(UNAVAILABLE.into());}
    if let Some((_,receipt))=live.iter().find(|(_,r)|r.request_sha256==request_sha) {
        if receipt.account_id!=request.account_id || receipt.workspace_id!=request.workspace_id || receipt.body_sha256!=digest || receipt.origin.as_deref()!=origin {return Err(KEY_CONFLICT.into());}
        for item in &receipt.items {
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3 AND source='preextracted')",params![item.candidate_id,request.owner,request.workspace_id],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
            if !exists {return Err(UNAVAILABLE.into());}
        }
        return Ok(receipt.response.clone());
    }
    let old_key:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND source='preextracted' AND turn_id=?3)",params![request.owner,request.workspace_id,request_sha],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
    if old_key {return Err(UNAVAILABLE.into());} // legacy request lacks body proof
    let mut bindings=Vec::with_capacity(items.len());
    for (item,input) in items.iter().zip(inputs) {
        let input=input.clone();
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3 AND source='preextracted')",params![item.id,request.owner,request.workspace_id],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        let proofs:Vec<_>=live.iter().flat_map(|(_,r)|r.items.iter()).chain(terminals.iter().flat_map(|r|r.survivors.iter())).filter(|v|v.candidate_id==item.id).collect();
        if exists && proofs.is_empty() {return Err(UNAVAILABLE.into());} // never infer old input
        if !exists && !proofs.is_empty() {return Err(UNAVAILABLE.into());} // unexplained missing live member
        if proofs.iter().any(|proof|proof.input_sha256!=input) {return Err(KEY_CONFLICT.into());}
        bindings.push(StageItemBinding{candidate_id:item.id.clone(),input_sha256:input});
    }
    let response=stage_in_tx(tx,request,items)?;
    let receipt=StageReceipt{version:STAGE_RECEIPT.into(),owner:request.owner.clone(),workspace_id:request.workspace_id.clone(),account_id:request.account_id.clone(),request_sha256:request_sha,body_sha256:digest.into(),response:response.clone(),items:bindings,origin:origin.map(str::to_owned)};
    let id=stage_receipt_id(&request.account_id,&request.workspace_id,&request.request_key)?;
    write_stage_receipt(tx,&id,&receipt)?;
    Ok(response)
}

/// Rebase verified F8 request proofs to a full owner memory-reset receipt.
/// Call only inside the same guarded reset transaction before owner rows are
/// deleted. Selective reset and owner purge have their own custody paths.
#[allow(dead_code)]
pub(super) fn rebase_stage_proofs_for_full_reset(
    tx:&rusqlite::Transaction<'_>, owner:&str, account:&str, reset_receipt_id:&str,
    live:Vec<(String,StageReceipt)>, terminals:Vec<StageTerminal>,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,
    budget:&mut crate::repository::ResetFenceBudget,
)->Result<(),String> {
    crate::repository::guarded_batch_forget::verify_full_reset_receipt_by_id(tx,owner,account,reset_receipt_id,view,budget).map_err(|_|UNAVAILABLE.to_string())?;
    let mut entries=Vec::new();
    for (id,r) in live {entries.push((id,STAGE_RECEIPT,STAGE_EVENT,r.owner,r.workspace_id,r.account_id,r.request_sha256,r.body_sha256,r.origin));}
    for r in terminals {entries.push((stage_terminal_id(&r.request_sha256)?,STAGE_TERMINAL,TERMINAL_EVENT,r.owner,r.workspace_id,r.account_id,r.request_sha256,r.body_sha256,r.origin));}
    for (old_id,old_reason,old_kind,captured,workspace,entry_account,request_sha256,body_sha256,origin) in entries {
        let proof=view.for_captured(tx,&captured).map_err(|_|UNAVAILABLE.to_string())?;
        if proof.current_owner()!=owner || proof.account_id()!=account || entry_account!=account {return Err(UNAVAILABLE.into());}
        let event=format!("outbox_{old_id}_{old_kind}");
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,event,old_id,old_kind]).map_err(|_|UNAVAILABLE.to_string())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,old_id,old_reason]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
        let terminal=StageTerminal{version:STAGE_TERMINAL.into(),owner:captured,workspace_id:workspace,account_id:account.into(),request_sha256:request_sha256.clone(),body_sha256,lifecycle_kind:"reset".into(),lifecycle_receipt_id:reset_receipt_id.into(),survivors:Vec::new(),selective_partition_sha256:None,origin};
        let raw=serde_json::to_string(&terminal).map_err(|_|UNAVAILABLE.to_string())?;if raw.len()>65536{return Err(UNAVAILABLE.into());}
        let id=stage_terminal_id(&request_sha256)?;let event=format!("outbox_{id}_{TERMINAL_EVENT}");let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![owner,id,terminal.owner,STAGE_TERMINAL,crate::ontology::CONTRACT_VERSION,hash(raw.as_bytes()),now,raw]).map_err(|_|UNAVAILABLE.to_string())?;
        tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![owner,event,id,TERMINAL_EVENT,raw,now]).map_err(|_|UNAVAILABLE.to_string())?;
    }
    Ok(())
}


fn verify_selective_reset(tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,id:&str,partition:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    if !hex64(partition) {return Err(UNAVAILABLE.into());}
    let row:Option<(String,Option<String>,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1'",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(|_|UNAVAILABLE.to_string())?;
    let (digest,raw,actor,actor_id,contract,source)=row.ok_or_else(||UNAVAILABLE.to_string())?;let raw=raw.ok_or_else(||UNAVAILABLE.to_string())?;budget.check(raw.len()).map_err(|_|UNAVAILABLE.to_string())?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(|_|UNAVAILABLE.to_string())?;let req=&value["request"];
    let captured=req["owner"].as_str().filter(|v|valid_text(v,512)).ok_or_else(||UNAVAILABLE.to_string())?;
    let operation=req["operation_id"].as_str().filter(|v|valid_text(v,512)).ok_or_else(||UNAVAILABLE.to_string())?;
    let proof=view.for_captured(tx,captured).map_err(|_|UNAVAILABLE.to_string())?;
    let expected_id=format!("cs_reset_{}",hash(&serde_json::to_vec(&(account,operation)).map_err(|_|UNAVAILABLE.to_string())?));
    let out:Option<(Option<String>,i64)>=tx.query_row("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![owner,format!("outbox_{id}_s06_guarded_reset"),id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|UNAVAILABLE.to_string())?;
    if proof.current_owner()!=owner || proof.account_id()!=account || req["account_id"]!=account || !req["workspace_id"].as_str().is_some_and(|v|valid_text(v,512)) || expected_id!=id || actor!="user" || actor_id!=captured || contract!=crate::ontology::CONTRACT_VERSION || source.is_some() || digest!=hash(raw.as_bytes()) || out!=Some((Some(raw.clone()),0)) || value.as_object().map(|v|v.len())!=Some(4) || value["version"]!="s06.guarded-reset.v1" || value["request_sha256"]!=hash(&serde_json::to_vec(req).map_err(|_|UNAVAILABLE.to_string())?) || value["result"]["complete"]!=true || value["result"]["categories"]["ingest"]["state"]!="complete" || value["result"]["categories"]["ingest"]["selective"]["partition_sha256"]!=partition || !req["components"].as_array().is_some_and(|v|v.iter().any(|c|c=="ingest") && !v.iter().any(|c|c=="memories")) || !req["selective_manifest_sha256"].as_str().is_some_and(hex64) {return Err(UNAVAILABLE.into());}
    Ok(())
}
fn verify_terminal_lifecycle(tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,t:&StageTerminal,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    match t.lifecycle_kind.as_str() {
        "forget"=>{crate::repository::guarded_batch_forget::verify_receipt_by_id(tx,owner,account,&t.workspace_id,&t.lifecycle_receipt_id,view,budget).map_err(|_|UNAVAILABLE.to_string())?;},
        "reset"=>{crate::repository::guarded_batch_forget::verify_full_reset_receipt_by_id(tx,owner,account,&t.lifecycle_receipt_id,view,budget).map_err(|_|UNAVAILABLE.to_string())?;},
        "selective"=>verify_selective_reset(tx,owner,account,&t.lifecycle_receipt_id,t.selective_partition_sha256.as_deref().ok_or_else(||UNAVAILABLE.to_string())?,view,budget)?,
        _=>return Err(UNAVAILABLE.into()),
    } Ok(())
}

/// Replace only selected requests with a paired S06 terminal; retain every
/// surviving item binding so later requests cannot reuse altered input.
pub(super) fn rebase_stage_proofs_for_selective_reset(tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,reset_id:&str,partition:&str,selected:&std::collections::BTreeSet<String>,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    verify_selective_reset(tx,owner,account,reset_id,partition,view,budget)?;
    let (live,terminals)=collect_stage_proofs(tx,owner,account,view,budget)?;
    let mut witnessed=std::collections::BTreeSet::new();let mut entries=Vec::new();
    for (id,r) in live {if r.items.iter().any(|v|selected.contains(&v.candidate_id)) {entries.push((id,STAGE_RECEIPT,STAGE_EVENT,r.owner,r.workspace_id,r.request_sha256,r.body_sha256,r.items,r.origin));}}
    for r in terminals {if r.survivors.iter().any(|v|selected.contains(&v.candidate_id)) {entries.push((stage_terminal_id(&r.request_sha256)?,STAGE_TERMINAL,TERMINAL_EVENT,r.owner,r.workspace_id,r.request_sha256,r.body_sha256,r.survivors,r.origin));}}
    for (old_id,reason,kind,captured,workspace,key,body,items,origin) in entries {
        for item in &items {if selected.contains(&item.candidate_id) {witnessed.insert(item.candidate_id.clone());}}
        let survivors:Vec<_>=items.into_iter().filter(|v|!selected.contains(&v.candidate_id)).collect();
        let terminal=StageTerminal{version:STAGE_TERMINAL.into(),owner:captured,workspace_id:workspace,account_id:account.into(),request_sha256:key.clone(),body_sha256:body,lifecycle_kind:"selective".into(),lifecycle_receipt_id:reset_id.into(),survivors,selective_partition_sha256:Some(partition.into()),origin};
        let raw=serde_json::to_string(&terminal).map_err(|_|UNAVAILABLE.to_string())?;budget.check(raw.len()).map_err(|_|UNAVAILABLE.to_string())?;if raw.len()>65536{return Err(UNAVAILABLE.into());}
        if tx.execute("DELETE FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,format!("outbox_{old_id}_{kind}"),old_id,kind]).map_err(|_|UNAVAILABLE.to_string())?!=1 || tx.execute("DELETE FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,old_id,reason]).map_err(|_|UNAVAILABLE.to_string())?!=1 {return Err(UNAVAILABLE.into());}
        let id=stage_terminal_id(&key)?;let event=format!("outbox_{id}_{TERMINAL_EVENT}");let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,?4,NULL,?5,?6,?7,?8)",params![owner,id,terminal.owner,STAGE_TERMINAL,crate::ontology::CONTRACT_VERSION,hash(raw.as_bytes()),now,raw]).map_err(|_|UNAVAILABLE.to_string())?;
        tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,0,?6)",params![owner,event,id,TERMINAL_EVENT,raw,now]).map_err(|_|UNAVAILABLE.to_string())?;
    }
    if witnessed!=*selected {return Err(UNAVAILABLE.into());} Ok(())
}

/// Bind a retained, pristine approved F8 fact to its verified stage and
/// account custody before selective ingest classifies it as retained.
pub(crate) fn selective_retained_context(tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,id:&str,curated:&str,view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget)->Result<Value,String> {
    if !SqliteStore::f8_profile_present(tx)? {return Err(UNAVAILABLE.into());}
    let (live,terminals)=collect_stage_proofs(tx,owner,account,view,budget)?;
    let binding=live.iter().flat_map(|(_,r)|r.items.iter()).chain(terminals.iter().flat_map(|r|r.survivors.iter())).find(|v|v.candidate_id==id).ok_or_else(||UNAVAILABLE.to_string())?;
    let member=plan_preextracted_erase_member(tx,owner,workspace,account,id)?;
    if member.curated.as_deref()!=Some(curated) || member.block.is_none() {return Err(UNAVAILABLE.into());}
    Ok(json!({"candidate_id":id,"curated_id":curated,"block_id":member.block,"input_sha256":binding.input_sha256,"account_id":account}))
}

/// Purge retains only exact request/body hashes under its account-bound
/// lifecycle receipt. Legacy stage keys have no provable body and keep None.
pub(super) use super::request_purge::RequestHash as PurgeRequestHash;
#[allow(dead_code)]
pub(super) fn collect_purge_request_hashes(
    tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget,
)->Result<Vec<PurgeRequestHash>,String> {
    let (live,terminal)=collect_stage_proofs(tx,owner,account,view,budget)?;
    let mut hashes=std::collections::BTreeMap::<String,Option<String>>::new();
    for (_,r) in live {if hashes.insert(r.request_sha256,Some(r.body_sha256)).is_some(){return Err(UNAVAILABLE.into());}}
    for r in terminal {if hashes.insert(r.request_sha256,Some(r.body_sha256)).is_some(){return Err(UNAVAILABLE.into());}}
    let mut stmt=tx.prepare("SELECT id,workspace_id,turn_id,source_uri,source_revision FROM candidates WHERE owner=?1 AND source='preextracted' ORDER BY id").map_err(|_|UNAVAILABLE.to_string())?;
    let mut rows=stmt.query([owner]).map_err(|_|UNAVAILABLE.to_string())?;
    while let Some(row)=rows.next().map_err(|_|UNAVAILABLE.to_string())? {
        let id:String=row.get(0).map_err(|_|UNAVAILABLE.to_string())?;let workspace:String=row.get(1).map_err(|_|UNAVAILABLE.to_string())?;
        let key:String=row.get(2).map_err(|_|UNAVAILABLE.to_string())?;let uri:String=row.get(3).map_err(|_|UNAVAILABLE.to_string())?;let revision:i64=row.get(4).map_err(|_|UNAVAILABLE.to_string())?;
        budget.check(id.len()+workspace.len()+key.len()+uri.len()).map_err(|_|UNAVAILABLE.to_string())?;
        if !hex64(&key) || !id.strip_prefix(CANDIDATE_PREFIX).is_some_and(hex64) || !valid_text(&workspace,512) || !valid_text(&uri,512) || revision<1 {return Err(UNAVAILABLE.into());}
        if uri.starts_with("host-archive://") {
            let source=source_projection_inner(tx,owner,&workspace,account,&id,&uri,revision)?;
            if source.get("kind").and_then(Value::as_str)!=Some("host_user_body_anchor") || !hashes.get(&key).is_some_and(Option::is_some) {return Err(UNAVAILABLE.into());}
            let captured=host_anchor_live_proof(tx,owner,&workspace,account,&id)?;
            let proof=view.for_captured(tx,&captured).map_err(|_|UNAVAILABLE.to_string())?;
            if proof.current_owner()!=owner || proof.account_id()!=account {return Err(UNAVAILABLE.into());}
            continue;
        }
        let evidence=evidence_id(&id)?;let source=source_id(account,&workspace,&uri)?;
        let locator:Option<String>=tx.query_row("SELECT e.locator_json FROM fm_v2_evidence e JOIN fm_v2_sources s ON s.owner_id=e.owner_id AND s.source_id=e.source_id AND s.source_revision=e.source_revision WHERE e.owner_id=?1 AND e.evidence_id=?2 AND e.source_id=?3 AND e.source_revision=?4 AND e.locator_type='preextracted_fact' AND s.workspace_key=?5 AND s.project_key='' AND s.source_type=?6 AND s.source_uri=?7",params![owner,evidence,source,revision,workspace,SOURCE_TYPE,uri],|r|r.get(0)).optional().map_err(|_|UNAVAILABLE.to_string())?;
        let locator=locator.ok_or_else(||UNAVAILABLE.to_string())?;budget.check(locator.len()).map_err(|_|UNAVAILABLE.to_string())?;
        let parsed=crate::field_authority::parse_unique_json(&locator).map_err(|_|UNAVAILABLE.to_string())?;
        if !preextracted_locator_ok(&parsed,&id) {return Err(UNAVAILABLE.into());}
        hashes.entry(key).or_insert(None);
    }
    drop(rows);drop(stmt);
    Ok(hashes.into_iter().map(|(request_sha256,body_sha256)|PurgeRequestHash{request_sha256,body_sha256}).collect())
}

/// Rename preflight validates F8 receipt custody and refuses unproved legacy
/// candidates; it does not mutate F8 facts or grant rename authority.
pub(crate) fn assess_rename_f8(
    tx:&rusqlite::Transaction<'_>,owner:&str,account:&str,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget,
)->Result<(),String> {
    let profile=SqliteStore::f8_profile_present(tx)?;
    if !profile {
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND (source='preextracted' OR id GLOB 'preextracted_*')) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND reason IN (?2,?3)) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND kind IN (?4,?5))",params![owner,STAGE_RECEIPT,STAGE_TERMINAL,STAGE_EVENT,TERMINAL_EVENT],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        return if exists {Err(UNAVAILABLE.into())} else {Ok(())};
    }
    let hashes=collect_purge_request_hashes(tx,owner,account,view,budget)?;
    if hashes.iter().any(|v|v.body_sha256.is_none()) {return Err(UNAVAILABLE.into());}
    Ok(())
}

pub(super) fn purge_account_hash(database:&str,account:&str)->Result<String,String> {
    super::request_purge::account_hash(super::request_purge::Family::F8,database,account)
}
pub(super) fn prepare_purge_request_terminals(tx:&rusqlite::Transaction<'_>,database:&str,account:&str,entries:&[PurgeRequestHash],budget:&mut crate::repository::ResetFenceBudget)->Result<Value,String> {
    super::request_purge::prepare(tx,super::request_purge::Family::F8,database,account,entries,budget)
}
pub(super) fn write_purge_request_terminals(tx:&rusqlite::Transaction<'_>,account_sha256:&str,receipt_id:&str,context_id:&str,entries:&[PurgeRequestHash])->Result<(),String> {
    super::request_purge::write(tx,super::request_purge::Family::F8,account_sha256,receipt_id,context_id,entries)
}
pub(super) fn verify_purge_manifest(tx:&rusqlite::Transaction<'_>,database:&str,account:&str,receipt_id:&str,manifest:&Value,budget:&mut crate::repository::ResetFenceBudget)->Result<(),String> {
    super::request_purge::verify(tx,super::request_purge::Family::F8,database,account,receipt_id,manifest,budget)
}

impl SqliteStore {
    /// Exact account/request lookup; absence is meaningful only under the
    /// admitted append-only profile and guarded schema stamp.
    #[allow(dead_code)]
    fn f8_purge_terminal_exists(&self,tx:&rusqlite::Transaction<'_>,database:&str,account:&str,request_sha256:&str,budget:&mut crate::repository::ResetFenceBudget)->Result<bool,String> {
        if !Self::f8_profile_present(tx)? || !hex64(request_sha256) {return Err(UNAVAILABLE.into());}
        self.guarded_schema_snapshot(tx).map_err(|_|UNAVAILABLE.to_string())?;
        let account_hash=purge_account_hash(database,account)?;
        let id:Option<String>=tx.query_row("SELECT purge_receipt_id FROM f8_purge_request_terminal WHERE account_sha256=?1 AND request_sha256=?2",params![account_hash,request_sha256],|r|r.get(0)).optional().map_err(|_|UNAVAILABLE.to_string())?;
        let Some(id)=id else {return Ok(false)};
        if !id.strip_prefix("owner_transition_").is_some_and(hex64) {return Err(UNAVAILABLE.into());}
        let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1 AND length(CAST(value AS BLOB))<=65536",[&id],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        let root=crate::field_authority::parse_unique_json(&raw).map_err(|_|UNAVAILABLE.to_string())?;
        let request_hash=root.get("request_sha256").and_then(Value::as_str).filter(|v|hex64(v)).ok_or_else(||UNAVAILABLE.to_string())?;
        if root.get("f8").is_none_or(Value::is_null) {return Err(UNAVAILABLE.into());}
        SqliteStore::validate_purge_receipt_record_with_budget(tx,&root,&id,request_hash,database,account,budget).map_err(|_|UNAVAILABLE.to_string())?;
        self.read_owned_schema_fact(tx,&id,&hash(raw.as_bytes()),request_hash,database).map_err(|_|UNAVAILABLE.to_string())?;
        verify_purge_manifest(tx,database,account,&id,root.get("f8").ok_or_else(||UNAVAILABLE.to_string())?,budget)?;
        Ok(true)
    }
}

pub(super) struct F8ForgetPlan<'tx,'conn> {
    members:Vec<PreextractedEraseMember<'tx,'conn>>, candidates:Vec<String>,
    live_requests:Vec<String>, survivor_requests:Vec<String>,
}
/// Account-bound proof plan for one F8-only guarded forget closure.
pub(super) fn plan_f8_guarded_forget<'tx,'conn>(
    tx:&'tx rusqlite::Transaction<'conn>,owner:&str,workspace:&str,account:&str,closure:&ForgetClosure,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget,
)->Result<Option<F8ForgetPlan<'tx,'conn>>,String> {
    let mut contains=false;
    for id in &closure.candidate_ids {
        let f8:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='preextracted')",params![owner,workspace,id],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        contains|=f8 || id.starts_with(CANDIDATE_PREFIX);
    }
    for id in &closure.curated_ids {
        let f8:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='preextracted')",params![owner,workspace,id],|r|r.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
        contains|=f8;
    }
    if !contains {return Ok(None);}
    if !SqliteStore::f8_profile_present(tx)? {return Err(UNAVAILABLE.into());}
    let members=plan_preextracted_erase_closure(tx,owner,workspace,account,closure)?;
    let (live,terminals)=collect_stage_proofs(tx,owner,account,view,budget)?;
    let selected:std::collections::BTreeSet<_>=closure.candidate_ids.iter().collect();
    let mut witnessed=std::collections::BTreeSet::new();let mut live_requests=Vec::new();let mut survivor_requests=Vec::new();
    for (_,r) in live {
        let hit=r.items.iter().any(|v|selected.contains(&v.candidate_id));
        if hit {if r.workspace_id!=workspace {return Err(UNAVAILABLE.into());} for item in &r.items {if selected.contains(&item.candidate_id) {witnessed.insert(item.candidate_id.clone());}} live_requests.push(r.request_sha256);}
    }
    for r in terminals {
        let hit=r.survivors.iter().any(|v|selected.contains(&v.candidate_id));
        if hit {if r.workspace_id!=workspace || !matches!(r.lifecycle_kind.as_str(),"forget"|"selective") {return Err(UNAVAILABLE.into());} for item in &r.survivors {if selected.contains(&item.candidate_id) {witnessed.insert(item.candidate_id.clone());}} survivor_requests.push(r.request_sha256);}
    }
    if witnessed.len()!=selected.len() {return Err(UNAVAILABLE.into());}
    Ok(Some(F8ForgetPlan{members,candidates:closure.candidate_ids.clone(),live_requests,survivor_requests}))
}
pub(super) fn apply_f8_guarded_forget<'tx,'conn>(tx:&'tx rusqlite::Transaction<'conn>,plan:&F8ForgetPlan<'tx,'conn>,erasure_id:&str)->Result<(),String> {
    apply_preextracted_erase(tx,&plan.members,erasure_id)
}
pub(super) fn finish_f8_guarded_forget(
    tx:&rusqlite::Transaction<'_>,owner:&str,workspace:&str,account:&str,plan:F8ForgetPlan<'_,'_>,receipt_id:&str,
    view:&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut crate::repository::ResetFenceBudget,
)->Result<(),String> {
    for key in plan.live_requests {terminalize_stage_receipt(tx,owner,workspace,account,&key,receipt_id,&plan.candidates,view,budget)?;}
    for key in plan.survivor_requests {terminalize_stage_survivor(tx,owner,workspace,account,&key,receipt_id,&plan.candidates,view,budget)?;}
    Ok(())
}

fn state_of(tx: &rusqlite::Transaction<'_>, owner: &str, candidate: &str) -> Result<Value, String> {
    let cid = conflict_id(candidate)?;
    let open: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_conflicts WHERE owner_id=?1 AND conflict_id=?2)", params![owner, cid], |r| r.get(0)).map_err(|e| e.to_string())?;
    Ok(if open { json!({"candidate_id": candidate, "state": "conflict", "conflict_id": cid}) } else { json!({"candidate_id": candidate, "state": "staged"}) })
}

fn stage_in_tx(tx: &rusqlite::Transaction<'_>, r: &PreextractedStageRequest, items: &[Prepared]) -> Result<Value, String> {
    let key = request_hash(&r.account_id, &r.workspace_id, &r.request_key)?;
    // Rows already carrying this request identity: a replay when they are all among this request's items and nothing else is new.
    let mut stmt = tx.prepare("SELECT id FROM candidates WHERE owner=?1 AND workspace_id=?2 AND source='preextracted' AND turn_id=?3 ORDER BY id LIMIT 101").map_err(|e| e.to_string())?;
    let own: std::collections::BTreeSet<String> = stmt.query_map(params![r.owner, r.workspace_id, key], |row| row.get::<_, String>(0)).map_err(|e| e.to_string())?.collect::<Result<_, _>>().map_err(|e| e.to_string())?;
    drop(stmt);
    let ids: std::collections::BTreeSet<&str> = items.iter().map(|p| p.id.as_str()).collect();
    if own.iter().any(|id| !ids.contains(id.as_str())) { return Err(KEY_CONFLICT.into()); }
    let mut response = Vec::new();
    let now = chrono::Utc::now().to_rfc3339();
    for p in items {
        let exists: Option<String> = tx.query_row("SELECT turn_id FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3", params![p.id, r.owner, r.workspace_id], |row| row.get(0)).optional().map_err(|e| e.to_string())?;
        if let Some(turn) = exists {
            // Same request: a replay of the original answer. Another request: the item was already staged, nothing is written.
            response.push(if turn == key { state_of(tx, &r.owner, &p.id)? } else { json!({"candidate_id": p.id, "state": "duplicate"}) });
            continue;
        }
        // An item new to this scope but belonging to a key that already staged other items is a different request.
        if !own.is_empty() { return Err(KEY_CONFLICT.into()); }
        let sid = if let Some(reference)=&p.host_reference {
            let (id,uri)=repository::register_host_anchor_source(tx,&r.owner,&r.workspace_id,&r.account_id,reference).map_err(|_|UNAVAILABLE.to_string())?;
            if uri!=p.uri {return Err(UNAVAILABLE.into());} id
        } else {source_id(&r.account_id, &r.workspace_id, &p.uri)?};
        let existing: Option<(String, String, String)> = tx.query_row("SELECT source_type,content_hash,source_uri FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3", params![r.owner, sid, p.revision], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?))).optional().map_err(|e| e.to_string())?;
        match existing {
            Some((ty, content, uri)) => { if ty != if p.host_reference.is_some() {"host_archive_part.v1"} else {SOURCE_TYPE} || content != p.content_sha256 || uri != p.uri { return Err(INVALID.into()); } }
            None if p.host_reference.is_some() => return Err(UNAVAILABLE.into()),
            None => { tx.execute("INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,created_at) VALUES (?,?,?,'',?,?,?,?,?)", params![r.owner, sid, r.workspace_id, p.uri, p.revision, p.content_sha256, SOURCE_TYPE, now]).map_err(|e| e.to_string())?; }
        }
        let eid = evidence_id(&p.id)?;
        // Target block and head, recorded for the review (scope, target block, expected head revision, before value).
        let resolved = match &p.target { Some(target) => resolve_target(tx, &r.owner, &r.workspace_id, target)?, None => None };
        let mut locator = json!({"kind": "preextracted_fact", "candidate_id": p.id});
        if let Some(reference)=&p.host_reference {locator["host_anchor"]=json!({"kind":"host_user_body_anchor","reference":reference,"coverage":"host_user_body_anchor_only"});}
        if let (Some(target), Some(found)) = (&p.target, &resolved) {
            locator["target"] = json!({"block_id": found.block, "expected_revision": found.revision, "predicate": target.predicate, "claim_slot": target.claim_slot, "before": found.before});
        }
        tx.execute("INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,created_at) VALUES (?,?,?,?,'preextracted_fact',?,NULL,'preextracted-fact-v1','open-clank','null','null',?)", params![r.owner, eid, sid, p.revision, serde_json::to_string(&locator).map_err(|e| e.to_string())?, now]).map_err(|e| e.to_string())?;
        let candidate = CandidateRecord {
            id: p.id.clone(), content: p.text.clone(), kind: p.kind, confidence_score: 0.5, importance_score: 0.5,
            owner: r.owner.clone(), workspace_id: r.workspace_id.clone(), workspace_path: None, session_id: String::new(), turn_id: key.clone(),
            raw_evidence_ids: vec![], evidence_role: "inferred".into(), source: "preextracted".into(), source_event_id: format!("preextracted:{}", p.hash),
            dedup_key: format!("preextracted:{}:{}", r.workspace_id, p.hash), source_uri: p.uri.clone(), source_revision: p.revision,
            content_hash: crate::privacy::content_hash(&p.text), source_message_ids: vec![], status: CandidateStatus::Pending,
            reason: "pre-extracted fact awaiting review".into(), accepted_curated_id: None, created_at: now.clone(), updated_at: now.clone(),
        };
        if !SqliteStore::insert_preextracted_candidate_inner(tx, &candidate).map_err(|e| e.to_string())? { return Err(UNAVAILABLE.into()); }
        let mut state = json!({"candidate_id": p.id, "state": "staged"});
        if let Some(found) = &resolved {
            if found.protected && found.before.as_deref().is_some_and(|before| normalize(before) != p.text) {
                let (block, revision) = (found.block.clone(), found.revision);
                let cid = conflict_id(&p.id)?;
                // Classification is `unresolved`: both claims and the competing evidence are kept; nothing here resolves or reclassifies it.
                let payload = json!({"kind": "preextracted", "classification": "unresolved", "candidate_id": p.id, "text": p.text, "evidence_id": eid,
                    "source": {"uri": p.uri, "revision": p.revision, "content_sha256": p.content_sha256}, "before": found.before});
                let rationale = "a pre-extracted fact differs from a protected manual or legacy value; nothing was overwritten";
                let actor = repository::RevisionActor { actor_type: "system".into(), actor_id: "preextracted".into(), reason: "pre-extracted fact conflicts with a protected path".into(), source_event_id: Some(format!("preextracted:{}", p.hash)) };
                let cs_payload = json!({"conflict_id": cid, "block_id": block, "current_revision": revision, "competing_payload": payload, "rationale": rationale});
                let cs = repository::insert_change_set(tx, &r.owner, &actor, &cs_payload, &now).map_err(|e| e.to_string())?;
                tx.execute("INSERT INTO fm_v2_conflicts(owner_id,conflict_id,block_id,current_revision,competing_payload,state,rationale,created_change_set_id,resolved_change_set_id,created_at,resolved_at) VALUES (?,?,?,?,?,'open',?,?,NULL,?,NULL)", params![r.owner, cid, block, revision, serde_json::to_string(&payload).map_err(|e| e.to_string())?, rationale, cs, now]).map_err(|e| e.to_string())?;
                repository::insert_conflict_event(tx, &r.owner, &cid, "open", rationale, &cs, &now).map_err(|e| e.to_string())?;
                repository::insert_outbox(tx, &r.owner, &cs, "conflict_recorded", &json!({"conflict_id": cid, "block_id": block, "current_revision": revision}), revision, &now).map_err(|e| e.to_string())?;
                state = json!({"candidate_id": p.id, "state": "conflict", "conflict_id": cid});
            }
        }
        response.push(state);
    }
    Ok(json!({"version": 1, "items": response}))
}

impl SqliteStore {
    /// The only insert path of the `preextracted_` family (the generic and private inserts refuse it).
    pub(crate) fn insert_preextracted_candidate_inner(conn: &Connection, c: &CandidateRecord) -> Result<bool, rusqlite::Error> {
        if !c.id.starts_with(CANDIDATE_PREFIX) || c.source != "preextracted" || c.evidence_role != "inferred" || c.status != CandidateStatus::Pending || c.accepted_curated_id.is_some() { return Err(rusqlite::Error::InvalidQuery); }
        Self::insert_candidate_inner_impl(conn, c, false, false, false, false, true)
    }

    fn preextracted_tx<T>(&self, owner: &str, workspace: &str, account: &str, policy: &PolicyPublicationRefs, immediate: bool,
        work: impl FnOnce(&rusqlite::Transaction<'_>,&crate::repository::owner_custody::VerifiedCustodyView<'_,'_>,&mut crate::repository::ResetFenceBudget) -> Result<T, String>) -> Result<T, String> {
        if self.guarded_epoch != Some(18) { return Err(UNAVAILABLE.into()); }
        let mut conn = self.conn.lock().map_err(|_| UNAVAILABLE.to_string())?;
        let tx = conn.transaction_with_behavior(if immediate { TransactionBehavior::Immediate } else { TransactionBehavior::Deferred }).map_err(|_| UNAVAILABLE.to_string())?;
        self.guarded_schema_snapshot(&tx).map_err(|_| UNAVAILABLE.to_string())?;
        if !Self::f8_profile_present(&tx).map_err(|_|UNAVAILABLE.to_string())? {return Err(UNAVAILABLE.into());}
        repository::require_guarded_epoch(&tx).map_err(|_| UNAVAILABLE.to_string())?;
        repository::require_policy_projection(&tx, account, workspace, Some(policy)).map_err(|_| UNAVAILABLE.to_string())?;
        let database: String = tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'", [], |row| row.get(0)).map_err(|_| UNAVAILABLE.to_string())?;
        let mut budget = repository::ResetFenceBudget::install(&tx);
        let view = repository::owner_custody::verified_or_direct_custody_view(&tx, &database, account, owner, owner, &mut budget).map_err(|_| UNAVAILABLE.to_string())?;
        let value = work(&tx,&view,&mut budget);
        drop(view); drop(budget);
        let value = value?;
        tx.commit().map_err(|_| UNAVAILABLE.to_string())?;
        Ok(value)
    }

    pub fn preextracted_stage(&self, r: &PreextractedStageRequest) -> Result<Value, String> {
        // This namespace is reserved for the sealed Host anchor path; the key
        // itself never proves that a request originated there.
        if r.request_key.starts_with("m1op_") { return Err(INVALID.into()); }
        let items = prepare(r)?;
        self.preextracted_tx(&r.owner, &r.workspace_id, &r.account_id, &r.policy_projection, true, |tx,view,budget| {
            let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
            let key=request_hash(&r.account_id,&r.workspace_id,&r.request_key)?;
            if self.f8_purge_terminal_exists(tx,&database,&r.account_id,&key,budget)? {return Err(UNAVAILABLE.into());}
            let (live,terminals)=collect_stage_proofs(tx,&r.owner,&r.account_id,view,budget)?;
            let inputs=r.items.iter().map(stage_item_hash).collect::<Result<Vec<_>,_>>()?;
            let digest=stage_body_hash(r)?;
            stage_with_proof_in_tx(tx,r,&items,&live,&terminals,&inputs,&digest,None)
        })
    }

    pub fn preextracted_host_anchor_stage(&self,r:&HostAnchorStageRequest)->Result<Value,String> {
        let (shim,items,inputs,digest)=prepare_host(r)?;
        self.preextracted_tx(&r.owner,&r.workspace_id,&r.account_id,&r.policy_projection,true,|tx,view,budget| {
            let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
            let key=request_hash(&r.account_id,&r.workspace_id,&r.request_key)?;
            if self.f8_purge_terminal_exists(tx,&database,&r.account_id,&key,budget)? {return Err(UNAVAILABLE.into());}
            let (live,terminals)=collect_stage_proofs(tx,&r.owner,&r.account_id,view,budget)?;
            stage_with_proof_in_tx(tx,&shim,&items,&live,&terminals,&inputs,&digest,Some("host_user_body_anchor.v1"))
        })
    }

    /// Read only the original sealed Host operation proof, never a caller's
    /// reconstructed body. Purged proof is unavailable, not unknown.
    pub fn preextracted_host_anchor_status(&self, r:&HostAnchorStatusRequest)->Result<Value,String> {
        if !scope_ok(r.version,&r.owner,&r.workspace_id,&r.account_id)
            || !r.request_key.strip_prefix("m1op_").is_some_and(hex64) {return Err(UNAVAILABLE.into());}
        self.preextracted_tx(&r.owner,&r.workspace_id,&r.account_id,&r.policy_projection,false,|tx,view,budget| {
            let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
            let key=request_hash(&r.account_id,&r.workspace_id,&r.request_key)?;
            if self.f8_purge_terminal_exists(tx,&database,&r.account_id,&key,budget)? {return Err(UNAVAILABLE.into());}
            let (live,terminals)=collect_stage_proofs(tx,&r.owner,&r.account_id,view,budget)?;
            if let Some((_,receipt))=live.iter().find(|(_,v)|v.request_sha256==key) {
                if receipt.workspace_id!=r.workspace_id || receipt.account_id!=r.account_id || receipt.origin.as_deref()!=Some("host_user_body_anchor.v1") {return Err(UNAVAILABLE.into());}
                for item in &receipt.items {
                    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3 AND source='preextracted')",params![item.candidate_id,r.owner,r.workspace_id],|row|row.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
                    if !exists {return Err(UNAVAILABLE.into());}
                }
                return Ok(json!({"state":"committed","body_sha256":receipt.body_sha256,"result":receipt.response}));
            }
            if let Some(terminal)=terminals.iter().find(|v|v.request_sha256==key) {
                if terminal.workspace_id!=r.workspace_id || terminal.account_id!=r.account_id || terminal.origin.as_deref()!=Some("host_user_body_anchor.v1") {return Err(UNAVAILABLE.into());}
                return Ok(json!({"state":"terminal"}));
            }
            let legacy:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM candidates WHERE owner=?1 AND workspace_id=?2 AND source='preextracted' AND turn_id=?3)",params![r.owner,r.workspace_id,key],|row|row.get(0)).map_err(|_|UNAVAILABLE.to_string())?;
            if legacy {return Err(UNAVAILABLE.into());}
            Ok(json!({"state":"unknown"}))
        })
    }

    pub fn preextracted_list(&self, r: &PreextractedListRequest) -> Result<Value, String> {
        if !scope_ok(r.version, &r.owner, &r.workspace_id, &r.account_id) || !(1..=50).contains(&r.limit) || r.after.as_deref().is_some_and(|a| !a.starts_with(CANDIDATE_PREFIX)) { return Err(UNAVAILABLE.into()); }
        self.preextracted_tx(&r.owner, &r.workspace_id, &r.account_id, &r.policy_projection, false, |tx,view,_| {
            let after = r.after.clone().unwrap_or_default();
            let mut stmt = tx.prepare("SELECT id,content,kind,source_uri,source_revision,created_at FROM candidates WHERE owner=?1 AND workspace_id=?2 AND source='preextracted' AND status='pending' AND id>?3 ORDER BY id LIMIT ?4").map_err(|e| e.to_string())?;
            let rows = stmt.query_map(params![r.owner, r.workspace_id, after, r.limit as i64 + 1], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, String>(2)?, row.get::<_, String>(3)?, row.get::<_, i64>(4)?, row.get::<_, String>(5)?))).map_err(|e| e.to_string())?.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())?;
            drop(stmt);
            let more = rows.len() > r.limit as usize;
            let mut items = Vec::new();
            for (id, content, kind, uri, revision, created) in rows.into_iter().take(r.limit as usize) {
                let source=source_projection(tx,&r.owner,&r.workspace_id,&r.account_id,&id,&uri,revision)?;
                verify_host_projection(tx,view,&r.owner,&r.workspace_id,&r.account_id,&id,&source)?;
                let cid = conflict_id(&id)?;
                let conflict: Option<String> = tx.query_row("SELECT block_id FROM fm_v2_conflicts WHERE owner_id=?1 AND conflict_id=?2", params![r.owner, cid], |row| row.get(0)).optional().map_err(|e| e.to_string())?;
                items.push(json!({"candidate_id": id, "kind": kind, "text": content, "state": "pending", "evidence_role": "inferred",
                    "source": source,
                    "conflict": conflict.map(|block| json!({"conflict_id": cid, "block_id": block, "classification": "unresolved"})),
                    "target": target_view(tx, &r.owner, &id)?, "created_at": created}));
            }
            let next = if more { items.last().and_then(|v| v["candidate_id"].as_str()).map(str::to_owned) } else { None };
            Ok(json!({"version": 1, "items": items, "next": next}))
        })
    }

    /// Read the pending pre-extracted candidate this selector names, or refuse exactly as for an unknown id.
    fn preextracted_candidate(tx: &rusqlite::Transaction<'_>, owner: &str, workspace: &str, candidate_id: &str) -> Result<CandidateRecord, String> {
        if !candidate_id.starts_with(CANDIDATE_PREFIX) { return Err(UNAVAILABLE.into()); }
        let candidate = tx.query_row(
            "SELECT id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids FROM candidates WHERE id=?1 AND owner=?2 AND workspace_id=?3",
            params![candidate_id, owner, workspace], SqliteStore::candidate_row).optional().map_err(|_| UNAVAILABLE.to_string())?.ok_or_else(|| UNAVAILABLE.to_string())?;
        if candidate.source != "preextracted" || candidate.evidence_role != "inferred" { return Err(UNAVAILABLE.into()); }
        Ok(candidate)
    }

    pub fn preextracted_review_preview(&self, r: &PreextractedSelector) -> Result<Value, String> {
        if !scope_ok(r.version, &r.owner, &r.workspace_id, &r.account_id) { return Err(UNAVAILABLE.into()); }
        self.preextracted_tx(&r.owner, &r.workspace_id, &r.account_id, &r.policy_projection, false, |tx,view,_| {
            let c = Self::preextracted_candidate(tx, &r.owner, &r.workspace_id, &r.candidate_id)?;
            if c.status != CandidateStatus::Pending { return Err(UNAVAILABLE.into()); }
            let digest = crate::provider::native::candidate_snapshot_digest(&c).map_err(|_| UNAVAILABLE.to_string())?;
            let source=source_projection(tx,&r.owner,&r.workspace_id,&r.account_id,&c.id,&c.source_uri,c.source_revision)?;
            verify_host_projection(tx,view,&r.owner,&r.workspace_id,&r.account_id,&c.id,&source)?;
            let cid = conflict_id(&c.id)?;
            let conflict: Option<String> = tx.query_row("SELECT block_id FROM fm_v2_conflicts WHERE owner_id=?1 AND conflict_id=?2", params![r.owner, cid], |row| row.get(0)).optional().map_err(|e| e.to_string())?;
            Ok(json!({"version": 1, "state": "preview", "candidate_id": c.id, "candidate_digest": digest, "kind": format!("{:?}", c.kind).to_lowercase(),
                "text": c.content, "evidence_role": "inferred", "source": source,
                "conflict": conflict.map(|block| json!({"conflict_id": cid, "block_id": block, "classification": "unresolved"})),
                "target": target_view(tx, &r.owner, &c.id)?}))
        })
    }

    pub fn preextracted_review(&self, r: &PreextractedReviewRequest) -> Result<Value, String> {
        if !scope_ok(r.version, &r.owner, &r.workspace_id, &r.account_id) || !hex64(&r.candidate_digest) || !matches!(r.action.as_str(), "accept" | "reject")
            || !valid_text(&r.request_key, 512) || !valid_text(&r.reason, 500) { return Err(UNAVAILABLE.into()); }
        self.preextracted_tx(&r.owner, &r.workspace_id, &r.account_id, &r.policy_projection, true, |tx,view,_| {
            let c = Self::preextracted_candidate(tx, &r.owner, &r.workspace_id, &r.candidate_id)?;
            // v2 binds the exact review body. On rename, only an owner in the
            // verified custody chain can match the original captured marker.
            let marker_for = |owner:&str| -> Result<String,String> {Ok(format!("preextracted-review-v2:{}", hash(&serde_json::to_vec(&("s03.preextracted-review.v2", owner, &r.account_id, &r.workspace_id, &r.policy_projection, &r.candidate_id, &r.candidate_digest, &r.request_key, &r.action, &r.reason)).map_err(|e| e.to_string())?)))};
            let marker=marker_for(&r.owner)?;
            if c.status != CandidateStatus::Pending {
                let mut exact=false;
                for owner in view.owners() {if c.reason==marker_for(owner)? {exact=true;break;}}
                return match (c.status, r.action.as_str(), c.accepted_curated_id.as_deref()) {
                    (CandidateStatus::Accepted, "accept", Some(curated)) if exact => Ok(json!({"version": 1, "state": "applied", "candidate_id": c.id, "curated_id": curated, "applied_revision": 1})),
                    (CandidateStatus::Rejected, "reject", None) if exact => Ok(json!({"version": 1, "state": "rejected", "candidate_id": c.id})),
                    _ => Err(UNAVAILABLE.into()),
                };
            }
            if crate::provider::native::candidate_snapshot_digest(&c).map_err(|_| UNAVAILABLE.to_string())? != r.candidate_digest { return Err(UNAVAILABLE.into()); }
            let source=source_projection(tx,&r.owner,&r.workspace_id,&r.account_id,&c.id,&c.source_uri,c.source_revision)?;
            verify_host_projection(tx,view,&r.owner,&r.workspace_id,&r.account_id,&c.id,&source)?;
            let now = chrono::Utc::now().to_rfc3339();
            if r.action == "reject" {
                if tx.execute("UPDATE candidates SET status='rejected',reason=?,updated_at=? WHERE id=? AND owner=? AND workspace_id=? AND status='pending'", params![marker, now, c.id, r.owner, r.workspace_id]).map_err(|e| e.to_string())? != 1 { return Err(UNAVAILABLE.into()); }
                return Ok(json!({"version": 1, "state": "rejected", "candidate_id": c.id}));
            }
            // Approval rechecks the target head: a proposal staged against a block that has since moved is stale, never rebased.
            if target_view(tx, &r.owner, &c.id)?.is_some_and(|v| v["stale"] == json!(true)) { return Err(UNAVAILABLE.into()); }
            let prepared = repository::prepare_preextracted_candidate_fact(tx, &r.owner, &r.workspace_id, &c.id, &r.candidate_digest, &r.reason).map_err(|_| UNAVAILABLE.to_string())?;
            Self::upsert_curated_inner(tx, &prepared.record, None).map_err(|_| UNAVAILABLE.to_string())?;
            let (content, metadata, saved_owner, saved_workspace): (String, String, Option<String>, String) = tx.query_row("SELECT content,metadata,owner,workspace_id FROM curated WHERE id=?", params![prepared.curated_id], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))).map_err(|_| UNAVAILABLE.to_string())?;
            let saved: Value = serde_json::from_str(&metadata).map_err(|_| UNAVAILABLE.to_string())?;
            if content != prepared.record.content || saved != prepared.record.metadata || saved_owner.as_deref() != Some(r.owner.as_str()) || saved_workspace != r.workspace_id { return Err(UNAVAILABLE.into()); }
            let (revision, _) = repository::create_candidate_fact_revision(tx, &r.owner, &r.workspace_id, &r.request_key, &r.reason, &prepared, &prepared.projection).map_err(|_| UNAVAILABLE.to_string())?;
            if tx.execute("UPDATE candidates SET status='accepted',reason=?,accepted_curated_id=?,updated_at=? WHERE id=? AND owner=? AND workspace_id=? AND status='pending'", params![marker, prepared.curated_id, now, c.id, r.owner, r.workspace_id]).map_err(|e| e.to_string())? != 1 { return Err(UNAVAILABLE.into()); }
            Ok(json!({"version": 1, "state": "applied", "candidate_id": c.id, "curated_id": prepared.curated_id, "applied_revision": revision.revision}))
        })
    }
}

#[cfg(test)]
#[path = "m1_anchor_tests.rs"]
mod m1_anchor_tests;

#[cfg(test)]
#[path = "f8_tests.rs"]
mod f8_tests;
