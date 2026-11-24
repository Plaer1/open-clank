//! Inactive S02 producer. Private callers retain the original human/account authority.
use super::*;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct S02AxisPolicy {
    pub axis: String,
    pub value: Value,
    pub mode: Value,
    pub authority: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag="operation", rename_all="snake_case", deny_unknown_fields)]
pub(crate) enum S02Change {
    CreateDefinition { local_key:String, subject_entity_id:String, subject_revision:i64, label:String, definition_kind:String, policies:Vec<S02AxisPolicy> },
    Definition { block_id:String, expected_revision:i64, definition_kind:String, policies:Vec<S02AxisPolicy> },
    Edge { edge_id:String, expected_revision:i64, child_block_id:String, child_revision:i64,
           parent_block_id:String, parent_revision:i64, active:bool },
    Assignment { assignment_id:String, expected_revision:i64, target_block_id:String, target_revision:i64,
                 definition_block_id:String, definition_revision:i64, active:bool },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct S02ApplyRequest {
    pub version:u32, pub owner:String, pub account_id:String, pub workspace_id:String,
    pub project_id:Option<String>, pub request_key:String, pub expected_generation:i64,
    pub policy_projection:PolicyPublicationRefs, pub selected:Option<S07SelectedRef>, pub changes:Vec<S02Change>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct S02OverrideRequest {
    pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,pub selected:S07SelectedRef,
    pub predecessor_receipt_id:String,pub expected_revision:i64,pub request_key:String,pub axis:String,pub value:Value,
    pub policy_projection:PolicyPublicationRefs,
}
fn override_request(r:&S02OverrideRequest)->Result<MetadataAssignmentRequest,RepositoryError>{
    if r.version!=1 || !request_scope(&r.owner,&r.account_id,&r.workspace_id,&None) || !identifier(&r.request_key) || !identifier(&r.predecessor_receipt_id) || r.expected_revision<1 || r.selected.reference.revision!=r.expected_revision {return Err(RepositoryError::NotFound)}
    let valid=match r.axis.as_str(){"importance"=>r.value.is_null() || r.value.as_f64().is_some_and(|v|v.is_finite() && (0.0..=1.0).contains(&v)),"connections"|"consolidation"=>r.value.is_null() || r.value.is_boolean(),_=>false};
    if !valid {return Err(RepositoryError::NotFound)}
    let mut overrides=serde_json::Map::new();overrides.insert(r.axis.clone(),r.value.clone());
    Ok(MetadataAssignmentRequest{version:1,owner:r.owner.clone(),workspace_id:r.workspace_id.clone(),account_id:r.account_id.clone(),curated_id:r.selected.curated_id.clone(),predecessor_receipt_id:r.predecessor_receipt_id.clone(),expected_revision:r.expected_revision,request_key:r.request_key.clone(),reason:"explicit S02 override review".into(),metadata:json!({"policy_overrides":overrides}),manual:true,policy_projection:r.policy_projection.clone(),selected:Some(r.selected.clone())})
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct S02ConfigRequest {pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,pub policy_projection:PolicyPublicationRefs}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct S02SnapshotRequest {
    pub version:u32, pub owner:String, pub account_id:String, pub workspace_id:String,
    pub project_id:Option<String>, pub block_id:String, pub expected_revision:i64,
    pub policy_projection:PolicyPublicationRefs, pub selected:S07SelectedRef,
}

fn identifier(s:&str)->bool { !s.is_empty() && s.len()<=256 && s.trim()==s && !s.contains('\0') }
fn request_scope(owner:&str, account:&str, workspace:&str, project:&Option<String>)->bool {
    identifier(owner) && identifier(account) && identifier(workspace) && project.as_ref().is_none_or(|p|identifier(p))
}
fn visible(w:&str,p:&str,scope_w:&str,scope_p:&str)->bool {
    (w.is_empty() || w==scope_w) && (p.is_empty() || (!w.is_empty() && p==scope_p))
}
/// Apply the canonical offline profile overlays in their dependency order.
/// The caller supplies a base schema with domain metadata already installed.
pub(crate) fn apply_s02_schema(expected:&Connection,v2:bool)->rusqlite::Result<()> {
    expected.execute_batch(include_str!("../store/schema-v17-s02-policy.sql"))?;
    if v2 {expected.execute_batch(include_str!("../store/schema-v17-s02-policy-v2.sql"))?;}
    Ok(())
}
pub(crate) fn apply_knowledge_ref_schema(expected:&Connection)->rusqlite::Result<()> {
    expected.execute_batch(include_str!("../store/schema-v17-knowledge-ref.sql"))
}
pub(super) fn profile(tx:&Transaction<'_>)->Result<(),RepositoryError> {
    require_guarded_epoch(tx)?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.type_tag_policy.v1'",[],|r|r.get(0)).optional()?;
    let objects:i64=tx.query_row("SELECT count(*) FROM sqlite_schema WHERE (type='table' AND name LIKE 'fm_v2_s02_%') OR (type IN ('index','trigger') AND name LIKE 's02_%')",[],|r|r.get(0))?;
    let v2:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.type_tag_policy.v2'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") || !matches!((v2.as_deref(),objects),(None,39)|(Some("1"),41)) {return Err(RepositoryError::NotFound);}
    require_domain_metadata_capability(tx)?;
    let expected=Connection::open_in_memory()?;
    expected.execute_batch(include_str!("../store/current-schema.sql"))?;
    expected.execute_batch(include_str!("../store/schema-v17-additions.sql"))?;
    expected.execute_batch(include_str!("../store/schema-v17-domain-metadata.sql"))?;
    apply_s02_schema(&expected,v2.as_deref()==Some("1"))?;
    let knowledge_ref:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s01.knowledge-ref.v1'",[],|r|r.get(0)).optional()?;
    match knowledge_ref.as_deref() {
        None=>{},
        Some("1") if v2.as_deref()==Some("1")=>apply_knowledge_ref_schema(&expected)?,
        _=>return Err(RepositoryError::NotFound),
    }
    let importance:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1'",[],|r|r.get(0)).optional()?;
    let guards:i64=tx.query_row("SELECT count(*) FROM sqlite_schema WHERE type='trigger' AND name IN ('importance_metadata_guard','importance_revision_guard')",[],|r|r.get(0))?;
    let s08_model:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s08.model.v1'",[],|r|r.get(0)).optional()?;
    match importance.as_deref() {
        Some("1") if v2.as_deref()==Some("1") && s08_model.as_deref()==Some("1") && guards==2 => expected.execute_batch(include_str!("../store/schema-v17-s02-importance.sql"))?,
        None if guards==0 => {},
        _=>return Err(RepositoryError::NotFound),
    }
    if knowledge_ref.as_deref()==Some("1") && importance.as_deref()==Some("1") {
        expected.execute_batch(include_str!("../store/schema-v17-s02-importance-knowledge-ref.sql"))?;
    }
    let importance_target:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance_target.v1'",[],|r|r.get(0)).optional()?;
    let target_guard:i64=tx.query_row("SELECT count(*) FROM sqlite_schema WHERE type='trigger' AND name='importance_target_selection_guard'",[],|r|r.get(0))?;
    match importance_target.as_deref() {
        Some("1") if importance.as_deref()==Some("1") && target_guard==1 && tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s08.model.v2'",[],|r|r.get::<_,String>(0)).optional()?.as_deref()==Some("1") => expected.execute_batch(include_str!("../store/schema-v17-s02-importance-target.sql"))?,
        None if target_guard==0=>{},
        _=>return Err(RepositoryError::NotFound),
    }
    let mut statement=expected.prepare("SELECT type,name,sql FROM sqlite_schema WHERE (type='table' AND name LIKE 'fm_v2_s02_%') OR (type IN ('index','trigger') AND name LIKE 's02_%') OR (type='trigger' AND name IN ('fm_v2_revision_metadata_validate','importance_metadata_guard','importance_revision_guard','importance_target_selection_guard'))")?;
    let rows=statement.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)))?.collect::<Result<Vec<_>,_>>()?;
    if rows.len()!=(if v2.as_deref()==Some("1") {42} else {40}) + if importance.is_some() {2} else {0} + if importance_target.is_some() {1} else {0} {return Err(RepositoryError::NotFound)}
    for (kind,name,sql) in rows {
        let actual:Option<String>=tx.query_row("SELECT sql FROM sqlite_schema WHERE type=? AND name=?",params![kind,name],|r|r.get(0)).optional()?;
        if actual.as_deref()!=Some(sql.as_str()){return Err(RepositoryError::NotFound)}
    }
    Ok(())
}
/// Compose the optional target accounting profile into an existing S08-v2
/// expected schema. The guarded owner separately verifies all exact triggers.
pub(crate) fn apply_target_accounting_profile(tx:&Transaction<'_>,expected:&Connection)->Result<(),RepositoryError> {
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance_target.v1'",[],|r|r.get(0)).optional()?;
    let guard:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name='importance_target_selection_guard')",[],|r|r.get(0))?;
    match marker.as_deref() {
        None if !guard=>Ok(()),
        Some("1") if guard=>{
            let valid:bool=tx.query_row("SELECT (SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1')='1' AND (SELECT value FROM fm_meta WHERE key='openclank.s08.model.v2')='1'",[],|r|r.get(0))?;
            if !valid {return Err(RepositoryError::NotFound)}
            expected.execute_batch(if crate::store::sqlite::s05_skill_draft_profile(tx)? {include_str!("../store/schema-v17-s02-target-skill-accounting.sql")} else {include_str!("../store/schema-v17-s02-target-model-accounting.sql")})?;
            Ok(())
        },
        _=>Err(RepositoryError::NotFound),
    }
}
pub(crate) fn require_override_profile(tx:&Transaction<'_>)->Result<(),RepositoryError>{
    profile(tx)?;let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.type_tag_policy.v2'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1"){return Err(RepositoryError::NotFound)} Ok(())
}
fn owner_integrity(tx:&Transaction<'_>,owner:&str,generation:i64)->Result<(),RepositoryError> {
    let broken:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s02_edge_heads h WHERE h.owner_id=?1 AND NOT EXISTS(SELECT 1 FROM fm_v2_s02_edge_revisions r WHERE r.owner_id=h.owner_id AND r.edge_id=h.edge_id AND r.revision=h.current_revision)) OR EXISTS(SELECT 1 FROM fm_v2_s02_assignment_heads h WHERE h.owner_id=?1 AND NOT EXISTS(SELECT 1 FROM fm_v2_s02_assignment_revisions r WHERE r.owner_id=h.owner_id AND r.assignment_id=h.assignment_id AND r.revision=h.current_revision))",[owner],|r|r.get(0))?;
    if broken{return Err(RepositoryError::NotFound)}
    if generation==0 {
        let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s02_definition_policy WHERE owner_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_s02_edge_heads WHERE owner_id=?1) OR EXISTS(SELECT 1 FROM fm_v2_s02_assignment_heads WHERE owner_id=?1)",[owner],|r|r.get(0))?;
        if present{return Err(RepositoryError::NotFound)}
    }
    Ok(())
}
fn policies_valid(values:&[S02AxisPolicy], owner_only:bool)->bool {
    if values.len()>3 {return false} let mut seen=BTreeSet::new();
    for p in values {
        if !seen.insert(p.axis.as_str()) || !matches!(p.axis.as_str(),"importance"|"connections"|"consolidation")
            || !matches!(p.authority.as_str(),"owner"|"inferred") || (owner_only && p.authority!="owner") {return false}
        if p.value.is_null() {if !p.mode.is_null(){return false}}
        else {
            if !p.mode.as_str().is_some_and(|s|matches!(s,"rules"|"model"|"hybrid")){return false}
            if p.axis=="importance" {
                if !p.value.as_f64().is_some_and(|v|v.is_finite() && (0.0..=1.0).contains(&v)){return false}
            } else if !p.value.is_boolean(){return false}
        }
    }
    serde_json::to_vec(values).is_ok_and(|b|b.len()<=32768)
}
fn block(tx:&Transaction<'_>,owner:&str,id:&str,w:&str,p:&str,expected:i64)->Result<(KnowledgeIdentity,String,String,i64),RepositoryError> {
    if !identifier(id) || expected<1 {return Err(RepositoryError::NotFound)}
    let (identity,bw,bp,head)=read_knowledge_identity(tx,owner,id)?;
    if head!=expected || !visible(&bw,&bp,w,p) {return Err(RepositoryError::NotFound)}
    let revision=read_knowledge(tx,owner,&identity,head,&bw,&bp)?;
    if revision.payload.status!=KnowledgeStatus::Active {return Err(RepositoryError::NotFound)}
    check_guarded_sources(tx,&revision,owner,&bw,&bp)?;
    Ok((identity,bw,bp,head))
}
pub(crate) fn selected_identity(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,selected:&S07SelectedRef)->Result<KnowledgeIdentity,RepositoryError>{
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let mut budget=ResetFenceBudget::install(tx);
    if let Some(identity)=selected_identity_s08_in(tx,owner,account,workspace,selected,budget.deadline)? {return Ok(identity);}
    let keys=owner_custody::account_custody_keys(tx,&database,account,&mut budget)?;
    if keys.is_empty(){drop(budget);return selected_identity_legacy(tx,owner,account,workspace,selected)}
    let view=owner_custody::verified_custody_view(tx,&database,account,owner,owner,&mut budget)?;
    selected_identity_custody(tx,owner,account,workspace,selected,&view)
}

// S08 summaries retain their candidate claim slot and typed object. This
// exact root proof is shared by both S02 selection and assignment owners.
fn selected_identity_s08_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,selected:&S07SelectedRef,deadline:std::time::Instant)->Result<Option<KnowledgeIdentity>,RepositoryError>{
    let is_s08:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='s08_policy' AND archived=0)",params![owner,workspace,selected.curated_id],|r|r.get(0))?;
    if !is_s08 {return Ok(None)}
    let content:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(content AS BLOB))<=16384 THEN content END FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND source='s08_policy' AND archived=0",params![owner,workspace,selected.curated_id],|r|r.get(0)).optional()?.flatten();
    let actual=super::s08_policy_review::prepared_s08_summary_in(tx,owner,account,workspace,&selected.curated_id,&content.ok_or(RepositoryError::NotFound)?,deadline)?;
    if actual!=selected.reference {return Err(RepositoryError::NotFound)}
    let (identity,w,p,head)=read_knowledge_identity(tx,owner,&actual.block_id)?;
    if w!=workspace || !p.is_empty() || head!=actual.revision {return Err(RepositoryError::NotFound)}
    Ok(Some(identity))
}

fn selected_identity_legacy(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,selected:&S07SelectedRef)->Result<KnowledgeIdentity,RepositoryError>{
    let context=current_curated_mirror_context_in(tx,owner,workspace,&selected.curated_id)?;
    if context.get("reviewer_account_id").and_then(Value::as_str)!=Some(account) {return Err(RepositoryError::NotFound)}
    let content:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(content AS BLOB))<=16384 THEN content END FROM curated WHERE owner=? AND workspace_id=? AND id=? AND archived=0",params![owner,workspace,selected.curated_id],|r|r.get(0)).optional()?.flatten();
    let actual=found_ref_for_curated(tx,owner,workspace,&selected.curated_id,&content.ok_or(RepositoryError::NotFound)?)?;
    if actual!=selected.reference {return Err(RepositoryError::NotFound)}
    let (identity,w,p,head)=read_knowledge_identity(tx,owner,&actual.block_id)?;
    if w!=workspace || !p.is_empty() || head!=actual.revision {return Err(RepositoryError::NotFound)}
    Ok(identity)
}
fn selected_identity_custody(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,selected:&S07SelectedRef,view:&owner_custody::VerifiedCustodyView<'_,'_>)->Result<KnowledgeIdentity,RepositoryError>{
    if view.current_owner()!=owner || view.account_id()!=account {return Err(RepositoryError::NotFound)}
    let context=current_import_mirror_context_custody(tx,workspace,&selected.curated_id,view)?;
    if context.get("reviewer_account_id").and_then(Value::as_str)!=Some(account) {return Err(RepositoryError::NotFound)}
    let binding:CuratedMirrorBinding=serde_json::from_value(context["binding"].clone())?;
    if binding.field_map.get("content").map(String::as_str)!=Some("/value") {return Err(RepositoryError::NotFound)}
    let content:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(content AS BLOB))<=16384 THEN content END FROM curated WHERE owner=? AND workspace_id=? AND id=? AND archived=0",params![owner,workspace,selected.curated_id],|r|r.get(0)).optional()?.flatten();
    let raw:String=tx.query_row("SELECT value_json FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? AND revision=?",params![owner,binding.block_id,binding.revision],|r|r.get(0)).optional()?.ok_or(RepositoryError::NotFound)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    if value.get("type")!=Some(&json!("string")) || value.get("value").and_then(Value::as_str)!=content.as_deref() {return Err(RepositoryError::NotFound)}
    let actual=FoundMemoryRef{version:1,provider:"fm-v2".into(),owner:owner.into(),workspace_id:workspace.into(),project_id:None,block_id:binding.block_id.clone(),revision:binding.revision,value_sha256:sha256_hex(raw.as_bytes())};
    if actual!=selected.reference {return Err(RepositoryError::NotFound)}
    let (identity,w,p,head)=read_knowledge_identity(tx,owner,&actual.block_id)?;
    if w!=workspace || !p.is_empty() || head!=actual.revision {return Err(RepositoryError::NotFound)} Ok(identity)
}

fn review_receipt(tx:&Transaction<'_>,owner:&str,account:&str,id:&str)->Result<S02ApplyRequest,RepositoryError> {
    let row:Option<(String,String,String,String)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (hash,raw,actor_type,actor_id)=row.ok_or(RepositoryError::NotFound)?;
    if raw.len()>131072 || sha256_hex(raw.as_bytes())!=hash || actor_type!="user" || actor_id!=owner {return Err(RepositoryError::NotFound)}
    let receipt=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let operation=receipt["operation_json"].as_str().filter(|s|s.len()<=65536).ok_or(RepositoryError::NotFound)?;
    let digest=sha256_hex(operation.as_bytes());
    let decision=receipt["decision_json"].as_str().ok_or(RepositoryError::NotFound)?;
    if receipt.as_object().map(|v|v.len())!=Some(5) || receipt["version"]!="s03.receipt.v2"
        || receipt["operation_sha256"]!=digest || receipt["decision_sha256"]!=sha256_hex(decision.as_bytes()) {return Err(RepositoryError::NotFound)}
    let request:S02ApplyRequest=serde_json::from_value(crate::field_authority::parse_unique_json(operation).map_err(RepositoryError::Validation)?)?;
    if request.owner!=owner || request.account_id!=account || request.expected_generation<0
        || request.expected_generation==i64::MAX || serde_json::to_string(&request)?!=operation {return Err(RepositoryError::NotFound)}
    let decision_value=crate::field_authority::parse_unique_json(decision).map_err(RepositoryError::Validation)?;
    if decision_value!=json!({"version":1,"account_id":account,"policy_generation":request.expected_generation+1}) {return Err(RepositoryError::NotFound)}
    let expected_id=format!("cs_s02_{}",sha256_hex(&serde_json::to_vec(&json!([request.owner,request.account_id,request.request_key]))?));
    if id!=expected_id {return Err(RepositoryError::NotFound)}
    let expected_event=json!({"version":1,"account_id":account,"request_sha256":digest,"generation":request.expected_generation+1}).to_string();
    let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND change_set_id=? AND event_id=? AND kind='s02_policy_review'",params![owner,id,format!("outbox_{id}_s02_policy_review")],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if event!=Some((expected_event,request.expected_generation+1)) {return Err(RepositoryError::NotFound)}
    Ok(request)
}

/// Historical S02 review bytes in verified current account custody.
fn review_receipt_custody(tx:&Transaction<'_>,account:&str,id:&str,view:&owner_custody::VerifiedCustodyView<'_ ,'_>)->Result<S02ApplyRequest,RepositoryError> {
    let owner=view.current_owner();
    let row:Option<(String,String,String,String)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (hash,raw,actor_type,actor_id)=row.ok_or(RepositoryError::NotFound)?;
    if raw.len()>131072 || sha256_hex(raw.as_bytes())!=hash || actor_type!="user" {return Err(RepositoryError::NotFound)}
    let receipt=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let operation=receipt["operation_json"].as_str().filter(|s|s.len()<=65536).ok_or(RepositoryError::NotFound)?;
    let digest=sha256_hex(operation.as_bytes());
    let decision=receipt["decision_json"].as_str().ok_or(RepositoryError::NotFound)?;
    if receipt.as_object().map(|v|v.len())!=Some(5) || receipt["version"]!="s03.receipt.v2"
        || receipt["operation_sha256"]!=digest || receipt["decision_sha256"]!=sha256_hex(decision.as_bytes()) {return Err(RepositoryError::NotFound)}
    let request:S02ApplyRequest=serde_json::from_value(crate::field_authority::parse_unique_json(operation).map_err(RepositoryError::Validation)?)?;
    if view.for_captured(tx,&request.owner)?.account_id()!=account || actor_id!=request.owner || request.account_id!=account || request.expected_generation<0
        || request.expected_generation==i64::MAX || serde_json::to_string(&request)?!=operation {return Err(RepositoryError::NotFound)}
    let decision_value=crate::field_authority::parse_unique_json(decision).map_err(RepositoryError::Validation)?;
    if decision_value!=json!({"version":1,"account_id":account,"policy_generation":request.expected_generation+1}) {return Err(RepositoryError::NotFound)}
    let expected_id=format!("cs_s02_{}",sha256_hex(&serde_json::to_vec(&json!([request.owner,request.account_id,request.request_key]))?));
    if id!=expected_id {return Err(RepositoryError::NotFound)}
    let expected_event=json!({"version":1,"account_id":account,"request_sha256":digest,"generation":request.expected_generation+1}).to_string();
    let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND change_set_id=? AND event_id=? AND kind='s02_policy_review'",params![owner,id,format!("outbox_{id}_s02_policy_review")],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if event!=Some((expected_event,request.expected_generation+1)) {return Err(RepositoryError::NotFound)}
    Ok(request)
}
fn create_parts(request:&S02ApplyRequest, local_key:&str, subject:&str, label:&str)->Result<(KnowledgeIdentity,String,String),RepositoryError> {
    if !identifier(local_key) || !identifier(subject) || label.trim()!=label || label.is_empty() || label.len()>512 {return Err(RepositoryError::NotFound)}
    let digest=sha256_hex(&serde_json::to_vec(&json!(["openclank.s02.definition.v1",request.owner,request.account_id,request.request_key,local_key]))?);
    let id=format!("s02def_{digest}"); let key=format!("s02create_{digest}");
    let identity=KnowledgeIdentity{block_id:id.clone(),subject_entity_id:subject.into(),predicate:"s02.policy_definition".into(),claim_slot:id,cardinality:PredicateCardinality::Single};
    let payload=json!({"value":{"type":"string","value":label},"expected_value_type":"string","kind":"fact","status":"active","tags":[],"confidence":0.0,"trust":0.0,"activation":"manual","activation_rationale":"","valid_from":null,"valid_to":null,"evidence_ids":[]});
    let approved:Vec<String>=["activation","activation_rationale","confidence","evidence_ids","expected_value_type","kind","status","tags","trust","valid_from","valid_to","value"].iter().map(|v|format!("/{v}")).collect();
    let operation=json!({"version":"s03.published-create.v1","owner":request.owner,"workspace":request.workspace_id,"project":request.project_id.as_deref().unwrap_or(""),"block_id":identity.block_id,"subject_entity_id":identity.subject_entity_id,"predicate":identity.predicate,"claim_slot":identity.claim_slot,"cardinality":"single","payload":payload,"request_key":key,"actor_type":"user","actor_id":request.owner,"reason":"explicit S02 policy review","source_event_id":null,"approved_paths":approved,"producer":null});
    Ok((identity,key,serde_json::to_string(&operation)?))
}
fn definition(tx:&Transaction<'_>,owner:&str,account:&str,id:&str,w:&str,p:&str,revision:i64)->Result<(String,String),RepositoryError> {
    let (identity,bw,bp,_)=block(tx,owner,id,w,p,revision)?;
    let mut original_payload:Option<KnowledgePayload>=None;
    if !(2..=257).contains(&revision) {return Err(RepositoryError::NotFound)}
    for n in 2..=revision {
        let row:Option<(String,String,String,String)>=tx.query_row("SELECT definition_kind,policies_json,policies_sha256,change_set_id FROM fm_v2_s02_definition_policy WHERE owner_id=? AND block_id=? AND revision=?",params![owner,id,n],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
        let (kind,raw,hash,change)=row.ok_or(RepositoryError::NotFound)?;
        let parsed:Vec<S02AxisPolicy>=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if !matches!(kind.as_str(),"type"|"tag") || !policies_valid(&parsed,false) || sha256_hex(raw.as_bytes())!=hash {return Err(RepositoryError::NotFound)}
        let receipt=review_receipt(tx,owner,account,&change)?;
        let revision_change:Option<(String,Option<i64>)>=tx.query_row("SELECT change_set_id,previous_revision FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? AND revision=?",params![owner,id,n],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if revision_change!=Some((change.clone(),Some(n-1))) {return Err(RepositoryError::NotFound)}
        let mut matches=0;
        for item in &receipt.changes { match item {
            S02Change::Definition{block_id,expected_revision,definition_kind,policies} if n>2 && block_id==id && *expected_revision==n-1 && definition_kind==&kind && serde_json::to_string(policies)?==raw => matches+=1,
            S02Change::CreateDefinition{local_key,subject_entity_id,label,definition_kind,policies,..} if n==2 && definition_kind==&kind && serde_json::to_string(policies)?==raw => {
                let (identity,key,operation)=create_parts(&receipt,local_key,subject_entity_id,label)?;
                if identity.block_id!=id || receipt.workspace_id!=bw || receipt.project_id.as_deref().unwrap_or("")!=bp {return Err(RepositoryError::NotFound)}
                let created=format!("cs_guard_create_{}",sha256_hex(serde_json::to_string(&json!([owner,bw,bp,id,key]))?.as_bytes()));
                let original=guarded_replay(tx,owner,&bw,&bp,id,&created,&operation,&sha256_hex(operation.as_bytes()))?.ok_or(RepositoryError::NotFound)?;
                let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND change_set_id=? AND event_id=? AND kind='knowledge_revision'",params![owner,created,format!("outbox_{created}_knowledge_revision")],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
                let operation_value=crate::field_authority::parse_unique_json(&operation).map_err(RepositoryError::Validation)?;
                let creation:Option<(String,String,String)>=tx.query_row("SELECT b.created_change_set_id,c.actor_type,c.actor_id FROM fm_v2_knowledge_blocks b JOIN fm_v2_change_sets c ON c.owner_id=b.owner_id AND c.change_set_id=b.created_change_set_id WHERE b.owner_id=? AND b.block_id=?",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
                let mut stmt=tx.prepare("SELECT field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=1 ORDER BY field_path LIMIT 13")?;
                let authority=stmt.query_map(params![owner,id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,Option<String>>(4)?,r.get::<_,Option<String>>(5)?,r.get::<_,String>(6)?,r.get::<_,String>(7)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
                let paths:Vec<String>=["activation","activation_rationale","confidence","evidence_ids","expected_value_type","kind","status","tags","trust","valid_from","valid_to","value"].iter().map(|v|format!("/{v}")).collect();
                let origins:BTreeMap<String,String>=paths.iter().map(|path|(path.clone(),"manual".into())).collect();
                let decision=json!({"outcome":"allowed","classification":"unresolved","allowed_paths":paths,"review_paths":[],"origins":origins});
                if original.1!=decision {return Err(RepositoryError::NotFound)}
                if original.0.revision!=1 || original.0.previous_revision.is_some() || original.0.identity!=identity || original.0.actor_type!="user" || original.0.actor_id!=owner || original.0.source_event_id.is_some() || original.0.change_set_id!=created || original.0.content_hash!=hash_value(&serde_json::to_value(&original.0.payload)?)? || serde_json::to_value(&original.0.payload)?!=operation_value["payload"] || creation!=Some((created.clone(),"user".into(),owner.into())) || event!=Some((json!({"block_id":id,"revision":1}).to_string(),1)) || authority.len()!=paths.len() {return Err(RepositoryError::NotFound)}
                for (row,path) in authority.iter().zip(paths.iter()) {if row.0!=*path || row.1!="manual" || row.2!="user" || row.3!=owner || row.4.as_deref()!=Some(owner) || row.5.is_some() || row.6!="[]" || row.7!=created {return Err(RepositoryError::NotFound)}}
                original_payload=Some(original.0.payload); matches+=1;
            }, _=>{} }
        }
        if matches!=1 {return Err(RepositoryError::NotFound)}
        let observed=read_knowledge(tx,owner,&identity,n,&bw,&bp)?;
        if observed.payload!=*original_payload.as_ref().ok_or(RepositoryError::NotFound)? || observed.actor_type!="user" || observed.actor_id!=owner || observed.change_set_id!=change {return Err(RepositoryError::NotFound)}
    }
    Ok((bw,bp))
}

/// Exact definition chain with per-review historical actors.
fn definition_custody(tx:&Transaction<'_>,account:&str,id:&str,w:&str,p:&str,revision:i64,view:&owner_custody::VerifiedCustodyView<'_ ,'_>)->Result<(String,String),RepositoryError> {
    if view.account_id()!=account {return Err(RepositoryError::NotFound);}
    let owner=view.current_owner();
    let (identity,bw,bp,_)=block(tx,owner,id,w,p,revision)?;
    let mut original_payload:Option<KnowledgePayload>=None;
    if !(2..=257).contains(&revision) {return Err(RepositoryError::NotFound)}
    for n in 2..=revision {
        let row:Option<(String,String,String,String)>=tx.query_row("SELECT definition_kind,policies_json,policies_sha256,change_set_id FROM fm_v2_s02_definition_policy WHERE owner_id=? AND block_id=? AND revision=?",params![owner,id,n],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
        let (kind,raw,hash,change)=row.ok_or(RepositoryError::NotFound)?;
        let parsed:Vec<S02AxisPolicy>=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if !matches!(kind.as_str(),"type"|"tag") || !policies_valid(&parsed,false) || sha256_hex(raw.as_bytes())!=hash {return Err(RepositoryError::NotFound)}
        let receipt=review_receipt_custody(tx,account,&change,view)?;
        let revision_change:Option<(String,Option<i64>)>=tx.query_row("SELECT change_set_id,previous_revision FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? AND revision=?",params![owner,id,n],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if revision_change!=Some((change.clone(),Some(n-1))) {return Err(RepositoryError::NotFound)}
        let mut matches=0;
        for item in &receipt.changes { match item {
            S02Change::Definition{block_id,expected_revision,definition_kind,policies} if n>2 && block_id==id && *expected_revision==n-1 && definition_kind==&kind && serde_json::to_string(policies)?==raw => matches+=1,
            S02Change::CreateDefinition{local_key,subject_entity_id,label,definition_kind,policies,..} if n==2 && definition_kind==&kind && serde_json::to_string(policies)?==raw => {
                let (identity,key,operation)=create_parts(&receipt,local_key,subject_entity_id,label)?;
                if identity.block_id!=id || receipt.workspace_id!=bw || receipt.project_id.as_deref().unwrap_or("")!=bp {return Err(RepositoryError::NotFound)}
                let created=format!("cs_guard_create_{}",sha256_hex(serde_json::to_string(&json!([receipt.owner,bw,bp,id,key]))?.as_bytes()));
                let original=guarded_replay_custody(tx,&bw,&bp,id,&created,&operation,&sha256_hex(operation.as_bytes()),&receipt.owner,view)?.ok_or(RepositoryError::NotFound)?;
                let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND change_set_id=? AND event_id=? AND kind='knowledge_revision'",params![owner,created,format!("outbox_{created}_knowledge_revision")],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
                let operation_value=crate::field_authority::parse_unique_json(&operation).map_err(RepositoryError::Validation)?;
                let creation:Option<(String,String,String)>=tx.query_row("SELECT b.created_change_set_id,c.actor_type,c.actor_id FROM fm_v2_knowledge_blocks b JOIN fm_v2_change_sets c ON c.owner_id=b.owner_id AND c.change_set_id=b.created_change_set_id WHERE b.owner_id=? AND b.block_id=?",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
                let mut stmt=tx.prepare("SELECT field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=1 ORDER BY field_path LIMIT 13")?;
                let authority=stmt.query_map(params![owner,id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,Option<String>>(4)?,r.get::<_,Option<String>>(5)?,r.get::<_,String>(6)?,r.get::<_,String>(7)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
                let paths:Vec<String>=["activation","activation_rationale","confidence","evidence_ids","expected_value_type","kind","status","tags","trust","valid_from","valid_to","value"].iter().map(|v|format!("/{v}")).collect();
                let origins:BTreeMap<String,String>=paths.iter().map(|path|(path.clone(),"manual".into())).collect();
                let decision=json!({"outcome":"allowed","classification":"unresolved","allowed_paths":paths,"review_paths":[],"origins":origins});
                if original.1!=decision {return Err(RepositoryError::NotFound)}
                if original.0.revision!=1 || original.0.previous_revision.is_some() || original.0.identity!=identity || original.0.actor_type!="user" || original.0.actor_id!=receipt.owner || original.0.source_event_id.is_some() || original.0.change_set_id!=created || original.0.content_hash!=hash_value(&serde_json::to_value(&original.0.payload)?)? || serde_json::to_value(&original.0.payload)?!=operation_value["payload"] || creation!=Some((created.clone(),"user".into(),receipt.owner.clone())) || event!=Some((json!({"block_id":id,"revision":1}).to_string(),1)) || authority.len()!=paths.len() {return Err(RepositoryError::NotFound)}
                for (row,path) in authority.iter().zip(paths.iter()) {if row.0!=*path || row.1!="manual" || row.2!="user" || row.3!=receipt.owner || row.4.as_deref()!=Some(receipt.owner.as_str()) || row.5.is_some() || row.6!="[]" || row.7!=created {return Err(RepositoryError::NotFound)}}
                original_payload=Some(original.0.payload); matches+=1;
            }, _=>{} }
        }
        if matches!=1 {return Err(RepositoryError::NotFound)}
        let observed=read_knowledge(tx,owner,&identity,n,&bw,&bp)?;
        if observed.payload!=*original_payload.as_ref().ok_or(RepositoryError::NotFound)? || observed.actor_type!="user" || observed.actor_id!=receipt.owner || observed.change_set_id!=change {return Err(RepositoryError::NotFound)}
    }
    Ok((bw,bp))
}
fn checked_edge(tx:&Transaction<'_>,owner:&str,account:&str,id:&str,revision:i64)->Result<(String,String,bool),RepositoryError> {
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT child_block_id,parent_block_id,active,change_set_id FROM fm_v2_s02_edge_revisions WHERE owner_id=? AND edge_id=? AND revision=?",params![owner,id,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (child,parent,active,change)=row.ok_or(RepositoryError::NotFound)?;
    if !matches!(active,0|1) {return Err(RepositoryError::NotFound)}
    let receipt=review_receipt(tx,owner,account,&change)?;
    let matches=receipt.changes.iter().filter(|item|match item {S02Change::Edge{edge_id,expected_revision,child_block_id,parent_block_id,active:a,..}=>edge_id==id && expected_revision.checked_add(1)==Some(revision) && child_block_id==&child && parent_block_id==&parent && *a==(active==1),_=>false}).count();
    if matches!=1{return Err(RepositoryError::NotFound)} Ok((child,parent,active==1))
}

fn checked_edge_custody(tx:&Transaction<'_>,owner:&str,account:&str,id:&str,revision:i64,view:&owner_custody::VerifiedCustodyView<'_ ,'_>)->Result<(String,String,bool),RepositoryError> {
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT child_block_id,parent_block_id,active,change_set_id FROM fm_v2_s02_edge_revisions WHERE owner_id=? AND edge_id=? AND revision=?",params![owner,id,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (child,parent,active,change)=row.ok_or(RepositoryError::NotFound)?;
    if !matches!(active,0|1) {return Err(RepositoryError::NotFound)}
    let receipt=review_receipt_custody(tx,account,&change,view)?;
    let matches=receipt.changes.iter().filter(|item|match item {S02Change::Edge{edge_id,expected_revision,child_block_id,parent_block_id,active:a,..}=>edge_id==id && expected_revision.checked_add(1)==Some(revision) && child_block_id==&child && parent_block_id==&parent && *a==(active==1),_=>false}).count();
    if matches!=1{return Err(RepositoryError::NotFound)} Ok((child,parent,active==1))
}
fn checked_assignment(tx:&Transaction<'_>,owner:&str,account:&str,id:&str,revision:i64)->Result<(String,String,bool),RepositoryError> {
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT target_block_id,definition_block_id,active,change_set_id FROM fm_v2_s02_assignment_revisions WHERE owner_id=? AND assignment_id=? AND revision=?",params![owner,id,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (target,definition,active,change)=row.ok_or(RepositoryError::NotFound)?;
    if !matches!(active,0|1){return Err(RepositoryError::NotFound)}
    let receipt=review_receipt(tx,owner,account,&change)?;
    let matches=receipt.changes.iter().filter(|item|match item {S02Change::Assignment{assignment_id,expected_revision,target_block_id,definition_block_id,active:a,..}=>assignment_id==id && expected_revision.checked_add(1)==Some(revision) && target_block_id==&target && definition_block_id==&definition && *a==(active==1),_=>false}).count();
    if matches!=1{return Err(RepositoryError::NotFound)} Ok((target,definition,active==1))
}

fn checked_assignment_custody(tx:&Transaction<'_>,owner:&str,account:&str,id:&str,revision:i64,view:&owner_custody::VerifiedCustodyView<'_ ,'_>)->Result<(String,String,bool),RepositoryError> {
    let row:Option<(String,String,i64,String)>=tx.query_row("SELECT target_block_id,definition_block_id,active,change_set_id FROM fm_v2_s02_assignment_revisions WHERE owner_id=? AND assignment_id=? AND revision=?",params![owner,id,revision],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (target,definition,active,change)=row.ok_or(RepositoryError::NotFound)?;
    if !matches!(active,0|1){return Err(RepositoryError::NotFound)}
    let receipt=review_receipt_custody(tx,account,&change,view)?;
    let matches=receipt.changes.iter().filter(|item|match item {S02Change::Assignment{assignment_id,expected_revision,target_block_id,definition_block_id,active:a,..}=>assignment_id==id && expected_revision.checked_add(1)==Some(revision) && target_block_id==&target && definition_block_id==&definition && *a==(active==1),_=>false}).count();
    if matches!=1{return Err(RepositoryError::NotFound)} Ok((target,definition,active==1))
}
fn cycle_check(tx:&Transaction<'_>,owner:&str,account:&str)->Result<(),RepositoryError> {
    let mut st=tx.prepare("SELECT r.child_block_id,r.parent_block_id,h.edge_id,h.current_revision FROM fm_v2_s02_edge_heads h JOIN fm_v2_s02_edge_revisions r ON r.owner_id=h.owner_id AND r.edge_id=h.edge_id AND r.revision=h.current_revision WHERE h.owner_id=? AND r.active=1 ORDER BY h.edge_id LIMIT 2049")?;
    let rows=st.query_map([owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,i64>(3)?)))?.collect::<Result<Vec<_>,_>>()?;
    if rows.len()>2048{return Err(RepositoryError::Validation("S02 graph limit".into()))}
    let mut graph:BTreeMap<String,Vec<String>>=BTreeMap::new(); let mut nodes=BTreeSet::new();
    for (c,p,id,revision) in rows {
        if checked_edge(tx,owner,account,&id,revision)? != (c.clone(),p.clone(),true) {return Err(RepositoryError::NotFound)}
        nodes.insert(c.clone()); nodes.insert(p.clone());
        if nodes.len()>256 {return Err(RepositoryError::Validation("S02 graph limit".into()))}
        graph.entry(c).or_default().push(p);
    }
    for root in graph.keys() {
        let mut work=vec![(root.clone(),vec![root.clone()])];let mut explored=0usize;
        while let Some((node,path))=work.pop() {
            for parent in graph.get(&node).into_iter().flatten() {
                explored+=1;if explored>2048 || path.len()>=32 || path.contains(parent) {return Err(RepositoryError::Validation("S02 cycle or graph limit".into()))}
                let mut next=path.clone();next.push(parent.clone());work.push((parent.clone(),next));
            }
        }
    }
    Ok(())
}

fn cycle_check_custody(tx:&Transaction<'_>,owner:&str,account:&str,view:&owner_custody::VerifiedCustodyView<'_ ,'_>)->Result<(),RepositoryError> {
    let mut st=tx.prepare("SELECT r.child_block_id,r.parent_block_id,h.edge_id,h.current_revision FROM fm_v2_s02_edge_heads h JOIN fm_v2_s02_edge_revisions r ON r.owner_id=h.owner_id AND r.edge_id=h.edge_id AND r.revision=h.current_revision WHERE h.owner_id=? AND r.active=1 ORDER BY h.edge_id LIMIT 2049")?;
    let rows=st.query_map([owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,i64>(3)?)))?.collect::<Result<Vec<_>,_>>()?;
    if rows.len()>2048{return Err(RepositoryError::Validation("S02 graph limit".into()))}
    let mut graph:BTreeMap<String,Vec<String>>=BTreeMap::new(); let mut nodes=BTreeSet::new();
    for (c,p,id,revision) in rows {
        if checked_edge_custody(tx,owner,account,&id,revision,view)? != (c.clone(),p.clone(),true) {return Err(RepositoryError::NotFound)}
        nodes.insert(c.clone()); nodes.insert(p.clone());
        if nodes.len()>256 {return Err(RepositoryError::Validation("S02 graph limit".into()))}
        graph.entry(c).or_default().push(p);
    }
    for root in graph.keys() {
        let mut work=vec![(root.clone(),vec![root.clone()])];let mut explored=0usize;
        while let Some((node,path))=work.pop() {
            for parent in graph.get(&node).into_iter().flatten() {
                explored+=1;if explored>2048 || path.len()>=32 || path.contains(parent) {return Err(RepositoryError::Validation("S02 cycle or graph limit".into()))}
                let mut next=path.clone();next.push(parent.clone());work.push((parent.clone(),next));
            }
        }
    }
    Ok(())
}

/// Immutable S02 rename facts only. The caller owns the account claim,
/// phase proofs, source fingerprint and target-empty transaction checks.
pub(crate) fn assess_owner_rename(tx:&Transaction<'_>,database_id:&str,account:&str,current_owner:&str,phase:Option<&QueuedTransitionBinding>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if let Some(phase)=phase {if phase.database_id!=database_id || phase.account_id!=account || phase.owner!=current_owner {return Err(RepositoryError::NotFound)} validate_queued_binding(tx,phase,account)?;}
    if !identifier(account) || !identifier(current_owner) {return Err(RepositoryError::NotFound)}
    let actual:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
    if actual!=database_id {return Err(RepositoryError::NotFound)}
    let marker_count:i64=tx.query_row("SELECT count(*) FROM fm_meta WHERE key IN ('openclank.type_tag_policy.v1','openclank.type_tag_policy.v2')",[],|r|r.get(0))?;
    let object_count:i64=tx.query_row("SELECT count(*) FROM sqlite_schema WHERE (type='table' AND name LIKE 'fm_v2_s02_%') OR (type IN ('index','trigger') AND name LIKE 's02_%')",[],|r|r.get(0))?;
    if marker_count==0 && object_count==0 {
        let metadata_table:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fm_v2_revision_metadata')",[],|r|r.get(0))?;
        let overrides=if metadata_table {tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_metadata WHERE owner_id=?1 AND CASE WHEN json_valid(metadata_json) THEN json_type(metadata_json,'$.policy_overrides') IS NOT NULL ELSE 1 END)",[current_owner],|r|r.get::<_,bool>(0))?} else {false};
        let review:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND (reason IN ('explicit S02 policy review','explicit S02 override review') OR CASE WHEN json_valid(operation_metadata_json) THEN json_extract(operation_metadata_json,'$.version')='s02.override-successor.v1' ELSE 0 END))",[current_owner],|r|r.get(0))?;
        if overrides || review {return Err(RepositoryError::NotFound)}
        return Ok(());
    }
    profile(tx)?;
    let view=Some(owner_custody::verified_or_direct_custody_view(tx,database_id,account,current_owner,current_owner,budget)?);
    let owner=current_owner;
    let generation:i64=tx.query_row("SELECT generation FROM fm_v2_s02_generation WHERE owner_id=?1",[owner],|r|r.get(0)).optional()?.unwrap_or(0);
    if !(0..=10000).contains(&generation) {return Err(RepositoryError::NotFound)}
    owner_integrity(tx,owner,generation)?;
    let mut seen=BTreeSet::new();let mut changes=BTreeMap::<String,usize>::new();
    let mut stmt=tx.prepare("SELECT change_set_id,length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id GLOB 'cs_s02_*' AND change_set_id NOT GLOB 'cs_s02_override_*' ORDER BY change_set_id LIMIT 10001")?;
    let rows=stmt.query_map([owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>10000 || rows.len()!=generation as usize {return Err(RepositoryError::NotFound)}
    for (id,len) in rows {
        budget.check(id.len().saturating_add(usize::try_from(len).map_err(|_|RepositoryError::NotFound)?)).map_err(RepositoryError::Validation)?;
        let receipt=match view.as_ref(){Some(v)=>review_receipt_custody(tx,account,&id,v)?,None=>review_receipt(tx,owner,account,&id)?};
        if receipt.changes.is_empty() || receipt.changes.len()>32 || !seen.insert(receipt.expected_generation) {return Err(RepositoryError::NotFound)}
        changes.insert(id,receipt.changes.len());
    }
    for (id,expected) in &changes {
        let count:i64=tx.query_row("SELECT (SELECT count(*) FROM fm_v2_s02_definition_policy WHERE owner_id=?1 AND change_set_id=?2)+(SELECT count(*) FROM fm_v2_s02_edge_revisions WHERE owner_id=?1 AND change_set_id=?2)+(SELECT count(*) FROM fm_v2_s02_assignment_revisions WHERE owner_id=?1 AND change_set_id=?2)",params![owner,id],|r|r.get(0))?;
        if usize::try_from(count).ok()!=Some(*expected) {return Err(RepositoryError::NotFound)}
    }
    if (0..generation).any(|value|!seen.contains(&value)) {return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT block_id,min(revision),max(revision),count(*) FROM fm_v2_s02_definition_policy WHERE owner_id=?1 GROUP BY block_id ORDER BY block_id LIMIT 257")?;
    let definitions=stmt.query_map([owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,i64>(2)?,r.get::<_,i64>(3)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if definitions.len()>256 {return Err(RepositoryError::NotFound)}
    for (id,min,max,count) in definitions {
        budget.check(id.len()).map_err(RepositoryError::Validation)?;
        if min!=2 || max<2 || count!=max-1 {return Err(RepositoryError::NotFound)}
        let mut refs=tx.prepare("SELECT change_set_id FROM fm_v2_s02_definition_policy WHERE owner_id=?1 AND block_id=?2 ORDER BY revision")?;
        let ids=refs.query_map(params![owner,id],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;drop(refs);
        if ids.iter().any(|change|!changes.contains_key(change)) {return Err(RepositoryError::NotFound)}
        let (_,workspace,project,head)=read_knowledge_identity(tx,owner,&id)?;
        if head!=max {return Err(RepositoryError::NotFound)}
        match view.as_ref(){Some(v)=>definition_custody(tx,account,&id,&workspace,&project,head,v)?,None=>definition(tx,owner,account,&id,&workspace,&project,head)?};
    }
    for (head_table,revision_table,kind) in [("fm_v2_s02_edge_heads","fm_v2_s02_edge_revisions",0),("fm_v2_s02_assignment_heads","fm_v2_s02_assignment_revisions",1)] {
        let key=if kind==0 {"edge_id"} else {"assignment_id"};
        let sql=format!("SELECT r.{key},r.revision,r.previous_revision,r.change_set_id,h.current_revision FROM {revision_table} r LEFT JOIN {head_table} h ON h.owner_id=r.owner_id AND h.{key}=r.{key} WHERE r.owner_id=?1 ORDER BY r.{key},r.revision LIMIT 10001");
        let mut stmt=tx.prepare(&sql)?;
        let rows=stmt.query_map([owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,Option<i64>>(2)?,r.get::<_,String>(3)?,r.get::<_,Option<i64>>(4)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if rows.len()>10000 {return Err(RepositoryError::NotFound)}
        let mut latest=BTreeMap::<String,i64>::new();
        for (id,rev,previous,change,head) in rows {
            budget.check(id.len().saturating_add(change.len())).map_err(RepositoryError::Validation)?;
            let next=latest.get(&id).copied().unwrap_or(0)+1;
            if rev!=next || previous!=if rev==1 {None}else{Some(rev-1)} || head.is_none_or(|h|h<rev) || !changes.contains_key(&change) {return Err(RepositoryError::NotFound)}
            if kind==0 {match view.as_ref(){Some(v)=>checked_edge_custody(tx,owner,account,&id,rev,v)?,None=>checked_edge(tx,owner,account,&id,rev)?};}
            else {match view.as_ref(){Some(v)=>checked_assignment_custody(tx,owner,account,&id,rev,v)?,None=>checked_assignment(tx,owner,account,&id,rev)?};}
            latest.insert(id,rev);
        }
        let heads:i64=tx.query_row(&format!("SELECT count(*) FROM {head_table} WHERE owner_id=?1"),[owner],|r|r.get(0))?;
        if usize::try_from(heads).ok()!=Some(latest.len()) {return Err(RepositoryError::NotFound)}
        for (id,revision) in latest {let head:i64=tx.query_row(&format!("SELECT current_revision FROM {head_table} WHERE owner_id=?1 AND {key}=?2"),params![owner,id],|r|r.get(0))?;if head!=revision{return Err(RepositoryError::NotFound)}}
    }
    match view.as_ref(){Some(v)=>cycle_check_custody(tx,owner,account,v)?,None=>cycle_check(tx,owner,account)?};
    // Each immutable override receipt must match its exact factual successor
    // chain. Any guarded operation without its successor remains unknown.
    let view=view.as_ref().ok_or(RepositoryError::NotFound)?;
    let mut stmt=tx.prepare("SELECT change_set_id,length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND (reason='explicit S02 override review' OR change_set_id GLOB 'cs_s02_override_*') ORDER BY change_set_id LIMIT 10001")?;
    let rows=stmt.query_map([owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>10000{return Err(RepositoryError::NotFound)}
    let mut all=BTreeSet::new();let mut verified=BTreeSet::new();let mut by_revision=BTreeMap::<(String,i64),String>::new();
    for (id,len) in &rows {
        budget.check(id.len().saturating_add(usize::try_from(*len).map_err(|_|RepositoryError::NotFound)?)).map_err(RepositoryError::Validation)?;
        all.insert(id.clone());let value=read_mirror_receipt_metadata(tx,owner,id)?;
        if value["version"]=="s03.receipt.v2" {continue;}
        let receipt:MetadataSuccessorReceipt=serde_json::from_value(value)?;
        if receipt.version!="s02.override-successor.v1" || receipt.account_id!=account {return Err(RepositoryError::NotFound)}
        let event=json!({"block_id":receipt.binding.block_id,"curated_id":receipt.binding.curated_id,"revision":receipt.binding.revision});
        let (binding,_,_,resolved_account)=resolve_value_successor_factual_phase(tx,&receipt.workspace,id,&event,view,phase,budget)?;
        if resolved_account!=account || serde_json::to_value(&binding)?!=serde_json::to_value(&receipt.binding)? || !verified.insert(id.clone()) || !verified.insert(receipt.binding.guarded_change_set_id.clone()) || by_revision.insert((receipt.binding.block_id.clone(),receipt.binding.revision),receipt.binding.guarded_change_set_id.clone()).is_some() {return Err(RepositoryError::NotFound)}
    }
    if all!=verified {return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT block_id,revision,change_set_id,length(CAST(metadata_json AS BLOB)) FROM fm_v2_revision_metadata WHERE owner_id=?1 AND CASE WHEN json_valid(metadata_json) THEN json_type(metadata_json,'$.policy_overrides') IS NOT NULL ELSE 1 END ORDER BY block_id,revision LIMIT 10001")?;
    let rows=stmt.query_map([owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,String>(2)?,r.get::<_,i64>(3)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
    if rows.len()>10000{return Err(RepositoryError::NotFound)}
    for (block,revision,change,len) in rows {
        budget.check(block.len().saturating_add(change.len()).saturating_add(usize::try_from(len).map_err(|_|RepositoryError::NotFound)?)).map_err(RepositoryError::Validation)?;
        let current=value_metadata_carry(tx,owner,&block,revision)?.ok_or(RepositoryError::NotFound)?;
        if by_revision.get(&(block.clone(),revision))==Some(&change) {continue;}
        // An S02 definition update copies metadata without modification; its
        // corresponding account-bound review was verified above.
        if changes.contains_key(&change) && revision>1 && value_metadata_carry(tx,owner,&block,revision-1)?.as_ref()==Some(&current) {continue;}
        // Other recognized guarded successor types must have one exact event
        // and the same complete factual chain, not a matching value alone.
        let mut st=tx.prepare("SELECT change_set_id,payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND kind='mirror_update' AND source_revision=?2 AND json_valid(payload_json) AND json_extract(payload_json,'$.block_id')=?3 LIMIT 2")?;
        let events=st.query_map(params![owner,revision,block],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(st);
        if events.len()!=1 {return Err(RepositoryError::NotFound)}
        let event=crate::field_authority::parse_unique_json(&events[0].1).map_err(RepositoryError::Validation)?;
        let (_,workspace,project,_)=read_knowledge_identity(tx,owner,&block)?;
        if !project.is_empty(){return Err(RepositoryError::NotFound)}
        let (binding,_,_,resolved_account)=resolve_value_successor_factual_phase(tx,&workspace,&events[0].0,&event,view,phase,budget)?;
        if resolved_account!=account || binding.block_id!=block || binding.revision!=revision || binding.guarded_change_set_id!=change {return Err(RepositoryError::NotFound)}
    }
    Ok(())
}

impl<'a> V2Repository<'a> {
    /// Private producer: the original human/account capability is held by its caller.
    pub(crate) fn apply_s02_policy(&mut self, request:&S02ApplyRequest)->Result<Value,RepositoryError> {
        if request.version!=1 || !request_scope(&request.owner,&request.account_id,&request.workspace_id,&request.project_id)
            || !identifier(&request.request_key) || request.expected_generation<0 || request.changes.is_empty() || request.changes.len()>32 {
            return Err(RepositoryError::NotFound)
        }
        let project=request.project_id.as_deref().unwrap_or("");
        crate::writer_fence::with_s02_policy_authority(self.conn,|conn| {
          crate::writer_fence::with_domain_metadata_authority(conn,|conn| {
            let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?; profile(&tx)?;
            require_policy_projection(&tx,&request.account_id,&request.workspace_id,Some(&request.policy_projection))?;
            let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let mut budget=ResetFenceBudget::install(&tx);
            let keys=owner_custody::account_custody_keys(&tx,&database,&request.account_id,&mut budget)?;
            let custody=if keys.is_empty(){None}else{Some(owner_custody::verified_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget)?)};
            let raw=serde_json::to_string(request)?;
            if raw.len()>65536 {return Err(RepositoryError::NotFound)}
            let hash=sha256_hex(raw.as_bytes());
            let change_id=format!("cs_s02_{}",sha256_hex(&serde_json::to_vec(&json!([request.owner,request.account_id,request.request_key]))?));
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?)",params![request.owner,change_id],|r|r.get(0))?;
            if exists {
                let original=review_receipt(&tx,&request.owner,&request.account_id,&change_id)?;
                if serde_json::to_string(&original)?!=raw {return Err(RepositoryError::NotFound)}
                let mut created=Vec::new(); for item in &original.changes {if let S02Change::CreateDefinition{local_key,subject_entity_id,label,..}=item {created.push(json!({"block_id":create_parts(&original,local_key,subject_entity_id,label)?.0.block_id,"revision":2}));}}
                drop(custody);drop(budget);tx.commit()?;return Ok(json!({"version":1,"state":"replayed","receipt_id":change_id,"generation":original.expected_generation+1,"created":created}));
            }
            let selected=request.selected.as_ref().map(|value|{
                if let Some(identity)=selected_identity_s08_in(&tx,&request.owner,&request.account_id,&request.workspace_id,value,budget.deadline)? {return Ok(identity)}
                match custody.as_ref(){Some(view)=>selected_identity_custody(&tx,&request.owner,&request.account_id,&request.workspace_id,value,view),None=>selected_identity_legacy(&tx,&request.owner,&request.account_id,&request.workspace_id,value)}
            }).transpose()?;
            let generation:i64=tx.query_row("SELECT generation FROM fm_v2_s02_generation WHERE owner_id=?",[&request.owner],|r|r.get(0)).optional()?.unwrap_or(0);
            if generation!=request.expected_generation || generation==i64::MAX {return Err(RepositoryError::NotFound)}
            owner_integrity(&tx,&request.owner,generation)?;
            let actor=RevisionActor{actor_type:"user".into(),actor_id:request.owner.clone(),reason:"explicit S02 policy review".into(),source_event_id:None};
            let now=chrono::Utc::now().to_rfc3339();
            insert_guarded_change_set(&tx,&request.owner,&change_id,&actor,&raw,&hash,&json!({"version":1,"account_id":request.account_id,"policy_generation":generation+1}),&now)?;
            insert_outbox(&tx,&request.owner,&change_id,"s02_policy_review",&json!({"version":1,"account_id":request.account_id,"request_sha256":hash,"generation":generation+1}),generation+1,&now)?;
            let mut unique=BTreeSet::new();
            for change in &request.changes {
                let key=match change {S02Change::CreateDefinition{local_key,..}=>format!("c:{local_key}"), S02Change::Definition{block_id,..}=>format!("d:{block_id}"), S02Change::Edge{edge_id,..}=>format!("e:{edge_id}"), S02Change::Assignment{assignment_id,..}=>format!("a:{assignment_id}")};
                if !unique.insert(key) {return Err(RepositoryError::NotFound)}
                match change {
                    S02Change::CreateDefinition{local_key,subject_entity_id,subject_revision,label,definition_kind,policies}=>{
                        if !matches!(definition_kind.as_str(),"type"|"tag") || !policies_valid(policies,true) || *subject_revision<1 {return Err(RepositoryError::NotFound)}
                        let head:Option<i64>=tx.query_row("SELECT current_revision FROM fm_v2_entities WHERE owner_id=? AND entity_id=? AND workspace_key=? AND project_key=?",params![request.owner,subject_entity_id,request.workspace_id,project],|r|r.get(0)).optional()?;
                        if head!=Some(*subject_revision) || read_entity(&tx,&request.owner,subject_entity_id,*subject_revision,&request.workspace_id,project)?.payload.status!="active" {return Err(RepositoryError::NotFound)}
                        let (identity,key,operation)=create_parts(request,local_key,subject_entity_id,label)?;
                        let approved:Vec<String>=["activation","activation_rationale","confidence","evidence_ids","expected_value_type","kind","status","tags","trust","valid_from","valid_to","value"].iter().map(|v|format!("/{v}")).collect();
                        let review=crate::field_authority::OwnerReviewContext{authenticated_owner:request.owner.clone(),reviewer_id:request.owner.clone(),expected_revision:0,proposal_key:key.clone(),payload_hash:sha256_hex(operation.as_bytes()),approved_paths:approved};
                        let scope=ScopeRef{owner_id:request.owner.clone(),workspace_id:Some(request.workspace_id.clone()),project_id:request.project_id.clone(),session_id:None};
                        let (created,_)=Self::guarded_create_knowledge_in_transaction(&tx,&scope,&identity,&actor,&key,&operation,Some(&review),None,true)?;
                        if created.revision!=1 {return Err(RepositoryError::NotFound)}
                        insert_knowledge_revision(&tx,&request.owner,&identity,2,Some(1),&created.payload,&actor,&change_id,&now)?;
                        tx.execute("INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id,created_at) SELECT owner_id,block_id,2,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,?,? FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=1",params![change_id,now,request.owner,identity.block_id])?;
                        let policy=serde_json::to_string(policies)?;
                        tx.execute("INSERT INTO fm_v2_s02_definition_policy(owner_id,block_id,revision,definition_kind,policies_json,policies_sha256,change_set_id) VALUES(?,?,2,?,?,?,?)",params![request.owner,identity.block_id,definition_kind,policy,sha256_hex(policy.as_bytes()),change_id])?;
                        if tx.execute("UPDATE fm_v2_knowledge_blocks SET current_revision=2 WHERE owner_id=? AND block_id=? AND current_revision=1",params![request.owner,identity.block_id])?!=1{return Err(RepositoryError::NotFound)}
                    }
                    S02Change::Definition{block_id,expected_revision,definition_kind,policies}=>{
                        if !matches!(definition_kind.as_str(),"type"|"tag") || !policies_valid(policies,true) {return Err(RepositoryError::NotFound)}
                        let (identity,w,p,head)=block(&tx,&request.owner,block_id,&request.workspace_id,project,*expected_revision)?;
                        let has_policy:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s02_definition_policy WHERE owner_id=? AND block_id=? AND revision=?)",params![request.owner,block_id,head],|r|r.get(0))?;
                        if !has_policy || head>=257 {return Err(RepositoryError::NotFound)}
                        match custody.as_ref(){Some(view)=>definition_custody(&tx,&request.account_id,block_id,&request.workspace_id,project,head,view)?,None=>definition(&tx,&request.owner,&request.account_id,block_id,&request.workspace_id,project,head)?};
                        let current=read_knowledge(&tx,&request.owner,&identity,head,&w,&p)?;
                        let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=?",params![request.owner,block_id,head],|r|r.get(0))?;
                        if count>256 {return Err(RepositoryError::NotFound)}
                        let carry=value_metadata_carry(&tx,&request.owner,block_id,head)?;
                        let next=head.checked_add(1).ok_or(RepositoryError::NotFound)?;
                        insert_knowledge_revision(&tx,&request.owner,&identity,next,Some(head),&current.payload,&actor,&change_id,&now)?;
                        tx.execute("INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,change_set_id,created_at) SELECT owner_id,block_id,?,field_path,origin,producer_type,producer_id,approved_by,source_event_id,evidence_json,?,? FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=?",params![next,change_id,now,request.owner,block_id,head])?;
                        if let Some(m)=carry {tx.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",params![request.owner,block_id,next,m.schema,m.json,m.hash,m.actor_type,m.actor_id,change_id,m.source_event_id,m.evidence_json,now])?;}
                        let policy=serde_json::to_string(policies)?;
                        tx.execute("INSERT INTO fm_v2_s02_definition_policy(owner_id,block_id,revision,definition_kind,policies_json,policies_sha256,change_set_id) VALUES(?,?,?,?,?,?,?)",params![request.owner,block_id,next,definition_kind,policy,sha256_hex(policy.as_bytes()),change_id])?;
                        if tx.execute("UPDATE fm_v2_knowledge_blocks SET current_revision=? WHERE owner_id=? AND block_id=? AND current_revision=?",params![next,request.owner,block_id,head])?!=1{return Err(RepositoryError::NotFound)}
                    }
                    S02Change::Edge{edge_id,expected_revision,child_block_id,child_revision,parent_block_id,parent_revision,active}=>{
                        if !identifier(edge_id) || *expected_revision<0 || child_block_id==parent_block_id {return Err(RepositoryError::NotFound)}
                        let (cw,cp)=match custody.as_ref(){Some(view)=>definition_custody(&tx,&request.account_id,child_block_id,&request.workspace_id,project,*child_revision,view)?,None=>definition(&tx,&request.owner,&request.account_id,child_block_id,&request.workspace_id,project,*child_revision)?};
                        let (pw,pp)=match custody.as_ref(){Some(view)=>definition_custody(&tx,&request.account_id,parent_block_id,&request.workspace_id,project,*parent_revision,view)?,None=>definition(&tx,&request.owner,&request.account_id,parent_block_id,&request.workspace_id,project,*parent_revision)?};
                        if !visible(&pw,&pp,&cw,&cp) {return Err(RepositoryError::NotFound)}
                        let prior:Option<i64>=tx.query_row("SELECT current_revision FROM fm_v2_s02_edge_heads WHERE owner_id=? AND edge_id=?",params![request.owner,edge_id],|r|r.get(0)).optional()?;
                        if prior.unwrap_or(0)!=*expected_revision {return Err(RepositoryError::NotFound)}
                        let next=expected_revision.checked_add(1).ok_or(RepositoryError::NotFound)?;
                        if let Some(head)=prior {
                            let (child,parent,_)=match custody.as_ref(){Some(view)=>checked_edge_custody(&tx,&request.owner,&request.account_id,edge_id,head,view)?,None=>checked_edge(&tx,&request.owner,&request.account_id,edge_id,head)?};
                            if (child,parent)!=(child_block_id.clone(),parent_block_id.clone()) {return Err(RepositoryError::NotFound)}
                            if tx.execute("UPDATE fm_v2_s02_edge_heads SET current_revision=? WHERE owner_id=? AND edge_id=? AND current_revision=?",params![next,request.owner,edge_id,head])?!=1{return Err(RepositoryError::NotFound)}
                        } else {tx.execute("INSERT INTO fm_v2_s02_edge_heads VALUES(?,?,?)",params![request.owner,edge_id,next])?;}
                        tx.execute("INSERT INTO fm_v2_s02_edge_revisions VALUES(?,?,?,?,?,?,?,?)",params![request.owner,edge_id,next,child_block_id,parent_block_id,*active as i64,change_id,prior])?;
                    }
                    S02Change::Assignment{assignment_id,expected_revision,target_block_id,target_revision,definition_block_id,definition_revision,active}=>{
                        if !identifier(assignment_id) || *expected_revision<0 || selected.as_ref().is_none_or(|v|*target_block_id!=v.block_id) || request.selected.as_ref().is_none_or(|v|*target_revision!=v.reference.revision) {return Err(RepositoryError::NotFound)}
                        let (_,tw,tp,_)=block(&tx,&request.owner,target_block_id,&request.workspace_id,project,*target_revision)?;
                        let (dw,dp)=match custody.as_ref(){Some(view)=>definition_custody(&tx,&request.account_id,definition_block_id,&request.workspace_id,project,*definition_revision,view)?,None=>definition(&tx,&request.owner,&request.account_id,definition_block_id,&request.workspace_id,project,*definition_revision)?};
                        if !visible(&dw,&dp,&tw,&tp) {return Err(RepositoryError::NotFound)}
                        let prior:Option<i64>=tx.query_row("SELECT current_revision FROM fm_v2_s02_assignment_heads WHERE owner_id=? AND assignment_id=?",params![request.owner,assignment_id],|r|r.get(0)).optional()?;
                        if prior.unwrap_or(0)!=*expected_revision {return Err(RepositoryError::NotFound)}
                        let next=expected_revision.checked_add(1).ok_or(RepositoryError::NotFound)?;
                        if let Some(head)=prior {
                            let (target,definition,_)=match custody.as_ref(){Some(view)=>checked_assignment_custody(&tx,&request.owner,&request.account_id,assignment_id,head,view)?,None=>checked_assignment(&tx,&request.owner,&request.account_id,assignment_id,head)?};
                            if (target,definition)!=(target_block_id.clone(),definition_block_id.clone()) {return Err(RepositoryError::NotFound)}
                            if tx.execute("UPDATE fm_v2_s02_assignment_heads SET current_revision=? WHERE owner_id=? AND assignment_id=? AND current_revision=?",params![next,request.owner,assignment_id,head])?!=1{return Err(RepositoryError::NotFound)}
                        } else {tx.execute("INSERT INTO fm_v2_s02_assignment_heads VALUES(?,?,?)",params![request.owner,assignment_id,next])?;}
                        tx.execute("INSERT INTO fm_v2_s02_assignment_revisions VALUES(?,?,?,?,?,?,?,?)",params![request.owner,assignment_id,next,target_block_id,definition_block_id,*active as i64,change_id,prior])?;
                    }
                }
            }
            match custody.as_ref(){Some(view)=>cycle_check_custody(&tx,&request.owner,&request.account_id,view)?,None=>cycle_check(&tx,&request.owner,&request.account_id)?};
            let definitions:i64=tx.query_row("SELECT count(*) FROM (SELECT 1 FROM fm_v2_knowledge_blocks b JOIN fm_v2_s02_definition_policy p ON p.owner_id=b.owner_id AND p.block_id=b.block_id AND p.revision=b.current_revision WHERE b.owner_id=? LIMIT 257)",[&request.owner],|r|r.get(0))?;
            if definitions>256{return Err(RepositoryError::NotFound)}
            for change in &request.changes {
                if let S02Change::Assignment{target_block_id,..}=change {
                    let count:i64=tx.query_row("SELECT count(*) FROM (SELECT 1 FROM fm_v2_s02_assignment_heads h JOIN fm_v2_s02_assignment_revisions r ON r.owner_id=h.owner_id AND r.assignment_id=h.assignment_id AND r.revision=h.current_revision WHERE h.owner_id=? AND r.target_block_id=? AND r.active=1 LIMIT 257)",params![request.owner,target_block_id],|r|r.get(0))?;
                    if count>256{return Err(RepositoryError::NotFound)}
                }
            }
            if generation==0 {tx.execute("INSERT INTO fm_v2_s02_generation VALUES(?,1)",[&request.owner])?;}
            else if tx.execute("UPDATE fm_v2_s02_generation SET generation=? WHERE owner_id=? AND generation=?",params![generation+1,request.owner,generation])?!=1{return Err(RepositoryError::NotFound)}
            let mut created=Vec::new(); for item in &request.changes {if let S02Change::CreateDefinition{local_key,subject_entity_id,label,..}=item {created.push(json!({"block_id":create_parts(request,local_key,subject_entity_id,label)?.0.block_id,"revision":2}));}}
            drop(custody);drop(budget);tx.commit()?;Ok(json!({"version":1,"state":"applied","receipt_id":change_id,"generation":generation+1,"created":created}))
          })
        })
    }
}

// Shared graph snapshot collector. Authority and exact revision qualification
// are established by its caller in the same transaction.
struct S02GraphScope {owner:String,account_id:String,workspace_id:String,project_id:Option<String>,block_id:String,expected_revision:i64}
fn s02_graph_snapshot(tx:&Transaction<'_>,scope:&S02GraphScope,custody:Option<&owner_custody::VerifiedCustodyView<'_,'_>>)->Result<Value,RepositoryError> {
    let project=scope.project_id.as_deref().unwrap_or("");
        let (_,tw,tp,head)=block(tx,&scope.owner,&scope.block_id,&scope.workspace_id,project,scope.expected_revision)?;
        let generation:i64=tx.query_row("SELECT generation FROM fm_v2_s02_generation WHERE owner_id=?",[&scope.owner],|r|r.get(0)).optional()?.unwrap_or(0);
        owner_integrity(tx,&scope.owner,generation)?;
        let mut stmt=tx.prepare("SELECT b.block_id,b.current_revision,b.workspace_key,b.project_key,p.definition_kind,p.policies_json,p.policies_sha256 FROM fm_v2_knowledge_blocks b JOIN fm_v2_s02_definition_policy p ON p.owner_id=b.owner_id AND p.block_id=b.block_id AND p.revision=b.current_revision WHERE b.owner_id=? ORDER BY b.block_id LIMIT 257")?;
        let rows=stmt.query_map([&scope.owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,String>(4)?,r.get::<_,String>(5)?,r.get::<_,String>(6)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if rows.len()>256 {return Err(RepositoryError::NotFound)}
        let mut definitions=Vec::new();let mut scopes=BTreeMap::new();let mut policy_bytes=0usize;
        for (id,revision,w,p,kind,raw,hash) in rows {
            if !visible(&w,&p,&tw,&tp) {continue}
            match custody{Some(view)=>definition_custody(tx,&scope.account_id,&id,&scope.workspace_id,project,revision,view)?,None=>definition(tx,&scope.owner,&scope.account_id,&id,&scope.workspace_id,project,revision)?};
            policy_bytes=policy_bytes.checked_add(raw.len()).ok_or(RepositoryError::NotFound)?;
            if policy_bytes>32768 || sha256_hex(raw.as_bytes())!=hash {return Err(RepositoryError::NotFound)}
            let policies:Vec<S02AxisPolicy>=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
            if !policies_valid(&policies,false) {return Err(RepositoryError::NotFound)}
            scopes.insert(id.clone(),(w.clone(),p.clone()));
            definitions.push(json!({"id":id,"revision":revision,"kind":kind,"scope":{"owner":scope.owner,"workspace":if w.is_empty(){None}else{Some(w)},"project":if p.is_empty(){None}else{Some(p)}},"policies":policies}));
        }
        let mut stmt=tx.prepare("SELECT h.edge_id,h.current_revision,r.child_block_id,r.parent_block_id FROM fm_v2_s02_edge_heads h JOIN fm_v2_s02_edge_revisions r ON r.owner_id=h.owner_id AND r.edge_id=h.edge_id AND r.revision=h.current_revision WHERE h.owner_id=? AND r.active=1 ORDER BY h.edge_id LIMIT 2049")?;
        let rows=stmt.query_map([&scope.owner],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if rows.len()>2048{return Err(RepositoryError::NotFound)}
        let mut edges=Vec::new();
        for (id,revision,child,parent) in rows {
            if let Some((w,p))=scopes.get(&child) {
                if match custody{Some(view)=>checked_edge_custody(tx,&scope.owner,&scope.account_id,&id,revision,view)?,None=>checked_edge(tx,&scope.owner,&scope.account_id,&id,revision)?} != (child.clone(),parent.clone(),true) {return Err(RepositoryError::NotFound)}
                let (pw,pp)=scopes.get(&parent).ok_or(RepositoryError::NotFound)?;
                if !visible(pw,pp,w,p){return Err(RepositoryError::NotFound)}
                edges.push(json!({"id":id,"revision":revision,"child_id":child,"parent_id":parent,
                    "scope":{"owner":scope.owner,"workspace":if w.is_empty(){None}else{Some(w)},"project":if p.is_empty(){None}else{Some(p)}}}));
            }
        }
        match custody{Some(view)=>cycle_check_custody(tx,&scope.owner,&scope.account_id,view)?,None=>cycle_check(tx,&scope.owner,&scope.account_id)?};
        let mut stmt=tx.prepare("SELECT h.assignment_id,h.current_revision,r.definition_block_id FROM fm_v2_s02_assignment_heads h JOIN fm_v2_s02_assignment_revisions r ON r.owner_id=h.owner_id AND r.assignment_id=h.assignment_id AND r.revision=h.current_revision WHERE h.owner_id=? AND r.target_block_id=? AND r.active=1 ORDER BY h.assignment_id LIMIT 257")?;
        let rows=stmt.query_map(params![scope.owner,scope.block_id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,String>(2)?)))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
        if rows.len()>256{return Err(RepositoryError::NotFound)}
        let mut assignments=Vec::new();
        for (id,revision,definition_id) in rows {
            if match custody{Some(view)=>checked_assignment_custody(tx,&scope.owner,&scope.account_id,&id,revision,view)?,None=>checked_assignment(tx,&scope.owner,&scope.account_id,&id,revision)?} != (scope.block_id.clone(),definition_id.clone(),true) {return Err(RepositoryError::NotFound)}
            if !scopes.contains_key(&definition_id){return Err(RepositoryError::NotFound)}
            assignments.push(json!({"id":id,"revision":revision,"definition_id":definition_id,
                "scope":{"owner":scope.owner,"workspace":if tw.is_empty(){None}else{Some(&tw)},"project":if tp.is_empty(){None}else{Some(&tp)}}}));
        }
        let mut manual=json!({"importance":"unknown","connections":"unknown","consolidation":"unknown"});
        let v2:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.type_tag_policy.v2'",[],|r|r.get(0)).optional()?;
        if v2.as_deref()==Some("1") {
            if let Some(metadata)=value_metadata_carry(tx,&scope.owner,&scope.block_id,head)? {
                let value=crate::field_authority::parse_unique_json(&metadata.json).map_err(RepositoryError::Validation)?;
                if let Some(overrides)=value.get("policy_overrides") {
                    let map=overrides.as_object().ok_or(RepositoryError::NotFound)?;
                    for (axis,setting) in map {
                        let path=format!("/metadata/policy_overrides/{axis}");
                        let approved:Option<String>=tx.query_row("SELECT approved_by FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=? AND field_path=? AND origin='manual'",params![scope.owner,scope.block_id,head,path],|r|r.get(0)).optional()?.flatten();
                        let approved=approved.ok_or(RepositoryError::NotFound)?;
                        match custody{Some(view)=>{view.for_captured(tx,&approved)?;},None=>if approved!=scope.owner{return Err(RepositoryError::NotFound)}}
                        manual[axis]=if setting.is_null(){json!({"state":"inherit"})}else{json!({"state":"value","value":setting})};
                    }
                }
            }
        }
        let result=json!({"version":1,"state":"ready","scope":{"owner":scope.owner,"workspace":scope.workspace_id,"project":scope.project_id},
            "generation":generation.to_string(),"memory_id":scope.block_id,"memory_revision":head,
            "definitions":definitions,"edges":edges,"assignments":assignments,"manual_authority":manual});
        Ok(result)
}

impl<'a> V2Repository<'a> {
    /// One bounded read transaction; native storage is evidence, not caller authority.
    pub(crate) fn read_s02_policy_snapshot(&mut self, request:&S02SnapshotRequest)->Result<Value,RepositoryError> {
        if request.version!=1 || !request_scope(&request.owner,&request.account_id,&request.workspace_id,&request.project_id)
            || !identifier(&request.block_id) || request.expected_revision<1 {return Err(RepositoryError::NotFound)}
        let project=request.project_id.as_deref().unwrap_or("");
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;profile(&tx)?;
        require_policy_projection(&tx,&request.account_id,&request.workspace_id,Some(&request.policy_projection))?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let mut budget=ResetFenceBudget::install(&tx);
        let keys=owner_custody::account_custody_keys(&tx,&database,&request.account_id,&mut budget)?;
        let custody=if keys.is_empty(){None}else{Some(owner_custody::verified_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget)?)};
        let selected=match custody.as_ref(){Some(view)=>selected_identity_custody(&tx,&request.owner,&request.account_id,&request.workspace_id,&request.selected,view)?,None=>selected_identity_legacy(&tx,&request.owner,&request.account_id,&request.workspace_id,&request.selected)?};
        if selected.block_id!=request.block_id || request.selected.reference.revision!=request.expected_revision {return Err(RepositoryError::NotFound)}
        let scope=S02GraphScope{owner:request.owner.clone(),account_id:request.account_id.clone(),workspace_id:request.workspace_id.clone(),project_id:request.project_id.clone(),block_id:request.block_id.clone(),expected_revision:request.expected_revision};
        let result=s02_graph_snapshot(&tx,&scope,custody.as_ref())?;
        drop(custody);drop(budget);tx.commit()?;Ok(result)
    }
}

impl<'a> V2Repository<'a> {
    pub(crate) fn s02_override_apply(&mut self,request:&S02OverrideRequest)->Result<Value,RepositoryError>{
        let operation=override_request(request)?;
        crate::writer_fence::with_s02_policy_authority(self.conn,|conn|V2Repository::new(conn)?.assign_reviewed_metadata(&operation,true))
    }
    pub(crate) fn s02_override_status(&mut self,request:&S02OverrideRequest,current_owner:&str,current_account:&str,current:&PolicyPublicationRefs)->Result<Value,RepositoryError>{
        let operation=override_request(request)?;let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;require_override_profile(&tx)?;
        require_policy_projection(&tx,&request.account_id,&request.workspace_id,Some(current))?;
        if current_account!=request.account_id || !identifier(current_owner) {return Err(RepositoryError::NotFound)}
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let mut budget=ResetFenceBudget::install(&tx);
        let keys=owner_custody::account_custody_keys(&tx,&database,current_account,&mut budget)?;
        let custody=if keys.is_empty(){if current_owner!=request.owner{return Err(RepositoryError::NotFound)} None}else{Some(owner_custody::verified_custody_view(&tx,&database,current_account,current_owner,&request.owner,&mut budget)?)};
        let id=s02_override_successor_id(&request.owner,&request.workspace_id,&request.selected.reference.block_id,&request.request_key)?;
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?)",params![current_owner,id],|r|r.get(0))?;
        if !exists {drop(custody);drop(budget);tx.commit()?;return Ok(json!({"version":1,"state":"unknown"}))}
        let raw=read_mirror_receipt_metadata(&tx,current_owner,&id)?;let receipt:MetadataSuccessorReceipt=serde_json::from_value(raw)?;
        let hash=sha256_hex(&serde_json::to_vec(&operation)?);
        if receipt.version!="s02.override-successor.v1" || receipt.owner!=request.owner || receipt.workspace!=request.workspace_id || receipt.account_id!=request.account_id || receipt.binding.block_id!=request.selected.reference.block_id || receipt.binding.curated_id!=request.selected.curated_id || receipt.predecessor_receipt_id!=request.predecessor_receipt_id || receipt.expected_revision!=request.expected_revision || receipt.request_key!=request.request_key || receipt.request_sha256!=hash || request.expected_revision.checked_add(1)!=Some(receipt.binding.revision) {return Err(RepositoryError::NotFound)}
        let expected=json!({"block_id":receipt.binding.block_id,"curated_id":receipt.binding.curated_id,"revision":receipt.binding.revision}).to_string();
        let event:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='mirror_update'",params![current_owner,format!("outbox_{id}_mirror_update"),id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if event!=Some((expected,receipt.binding.revision)){return Err(RepositoryError::NotFound)}
        let actor:Option<(String,String)>=tx.query_row("SELECT actor_type,actor_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?",params![current_owner,id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let operation_raw=serde_json::to_string(&operation)?;
        let (revision,decision)=match custody.as_ref(){Some(view)=>guarded_replay_custody(&tx,&request.workspace_id,"",&receipt.binding.block_id,&receipt.binding.guarded_change_set_id,&operation_raw,&hash,&request.owner,view)?,None=>guarded_replay(&tx,current_owner,&request.workspace_id,"",&receipt.binding.block_id,&receipt.binding.guarded_change_set_id,&operation_raw,&hash)?}.ok_or(RepositoryError::NotFound)?;
        let path=format!("/metadata/policy_overrides/{}",request.axis);
        let manual:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_field_authority WHERE owner_id=? AND block_id=? AND revision=? AND field_path=? AND origin='manual' AND approved_by=? AND change_set_id=?)",params![current_owner,receipt.binding.block_id,receipt.binding.revision,path,request.owner,receipt.binding.guarded_change_set_id],|r|r.get(0))?;
        let metadata=value_metadata_carry(&tx,current_owner,&receipt.binding.block_id,receipt.binding.revision)?.ok_or(RepositoryError::NotFound)?;
        if actor!=Some(("user".into(),request.owner.clone())) || revision.revision!=receipt.binding.revision || revision.actor_type!="user" || revision.actor_id!=request.owner || revision.content_hash!=receipt.knowledge_content_sha256 || receipt.changed_paths!=[path] || decision["outcome"]!="allowed" || decision["allowed_paths"]!=json!(receipt.changed_paths) || metadata.hash!=receipt.metadata_sha256 || !manual {return Err(RepositoryError::NotFound)}
        drop(custody);drop(budget);tx.commit()?;Ok(json!({"version":1,"state":"recorded","receipt_id":id,"revision":receipt.binding.revision}))
    }
    pub(crate) fn s02_config(&mut self,request:&S02ConfigRequest)->Result<Value,RepositoryError>{
        if request.version!=1 || !request_scope(&request.owner,&request.account_id,&request.workspace_id,&None) {return Err(RepositoryError::NotFound)}
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;profile(&tx)?;
        require_policy_projection(&tx,&request.account_id,&request.workspace_id,Some(&request.policy_projection))?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let mut budget=ResetFenceBudget::install(&tx);
        let keys=owner_custody::account_custody_keys(&tx,&database,&request.account_id,&mut budget)?;
        let custody=if keys.is_empty(){None}else{Some(owner_custody::verified_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget)?)};
        let generation:i64=tx.query_row("SELECT generation FROM fm_v2_s02_generation WHERE owner_id=?",[&request.owner],|r|r.get(0)).optional()?.unwrap_or(0);owner_integrity(&tx,&request.owner,generation)?;
        let mut st=tx.prepare("SELECT entity_id,current_revision FROM fm_v2_entities WHERE owner_id=? AND workspace_key=? AND project_key='' ORDER BY entity_id LIMIT 33")?;
        let rows=st.query_map(params![request.owner,request.workspace_id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?.collect::<Result<Vec<_>,_>>()?;drop(st);
        let partial=rows.len()>32;let mut entities=Vec::new();for (id,revision) in rows.into_iter().take(32){let v=read_entity(&tx,&request.owner,&id,revision,&request.workspace_id,"")?;if revision<=9007199254740991 && v.payload.status=="active" && v.payload.canonical_label.len()<=512 {entities.push(json!({"id":id,"revision":revision,"label":v.payload.canonical_label}));}}
        let mut st=tx.prepare("SELECT b.block_id,b.current_revision,p.definition_kind,p.policies_json FROM fm_v2_knowledge_blocks b JOIN fm_v2_s02_definition_policy p ON p.owner_id=b.owner_id AND p.block_id=b.block_id AND p.revision=b.current_revision WHERE b.owner_id=? AND b.workspace_key=? AND b.project_key='' ORDER BY b.block_id LIMIT 257")?;
        let rows=st.query_map(params![request.owner,request.workspace_id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?)))?.collect::<Result<Vec<_>,_>>()?;drop(st);if rows.len()>256{return Err(RepositoryError::NotFound)}
        let mut definitions=Vec::new();for (id,revision,kind,raw) in rows {match custody.as_ref(){Some(view)=>definition_custody(&tx,&request.account_id,&id,&request.workspace_id,"",revision,view)?,None=>definition(&tx,&request.owner,&request.account_id,&id,&request.workspace_id,"",revision)?};let (identity,w,p,_)=read_knowledge_identity(&tx,&request.owner,&id)?;let value=read_knowledge(&tx,&request.owner,&identity,revision,&w,&p)?;definitions.push(json!({"id":id,"revision":revision,"kind":kind,"label":value.payload.value,"policies":crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?}));}
        let result=json!({"version":1,"state":"ready","generation":generation.to_string(),"entities":entities,"entities_partial":partial,"definitions":definitions});drop(custody);drop(budget);tx.commit()?;Ok(result)
    }
    pub(crate) fn s02_selector(&mut self,request:&S02SnapshotRequest)->Result<Value,RepositoryError>{
        if request.version!=1 || !request_scope(&request.owner,&request.account_id,&request.workspace_id,&request.project_id) || request.project_id.is_some() {return Err(RepositoryError::NotFound)}
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;profile(&tx)?;
        require_policy_projection(&tx,&request.account_id,&request.workspace_id,Some(&request.policy_projection))?;
        let identity=selected_identity(&tx,&request.owner,&request.account_id,&request.workspace_id,&request.selected)?;
        if identity.block_id!=request.block_id || request.selected.reference.revision!=request.expected_revision {return Err(RepositoryError::NotFound)}
        let result=json!({"version":1,"state":"ready","block_id":identity.block_id,"revision":request.expected_revision,"subject_entity_id":identity.subject_entity_id});tx.commit()?;Ok(result)
    }
    pub(crate) fn s02_override_capability(&mut self)->Result<Value,RepositoryError>{let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;require_override_profile(&tx)?;tx.commit()?;Ok(json!({"version":1,"state":"ready"}))}
    pub(crate) fn s02_capability(&mut self)->Result<Value,RepositoryError>{let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;profile(&tx)?;tx.commit()?;Ok(json!({"version":1,"state":"ready"}))}
    pub(crate) fn s02_status(&mut self,original:&S02ApplyRequest,current_owner:&str,current_account:&str,current:&PolicyPublicationRefs)->Result<Value,RepositoryError>{
        if original.version!=1 || !request_scope(&original.owner,&original.account_id,&original.workspace_id,&original.project_id) || !identifier(&original.request_key) {return Err(RepositoryError::NotFound)}
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;profile(&tx)?;
        require_policy_projection(&tx,&original.account_id,&original.workspace_id,Some(current))?;
        if current_account!=original.account_id || !identifier(current_owner) {return Err(RepositoryError::NotFound)}
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let mut budget=ResetFenceBudget::install(&tx);
        let keys=owner_custody::account_custody_keys(&tx,&database,current_account,&mut budget)?;
        let custody=if keys.is_empty(){if current_owner!=original.owner{return Err(RepositoryError::NotFound)} None}else{Some(owner_custody::verified_custody_view(&tx,&database,current_account,current_owner,&original.owner,&mut budget)?)};
        let id=format!("cs_s02_{}",sha256_hex(&serde_json::to_vec(&json!([original.owner,original.account_id,original.request_key]))?));
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?)",params![current_owner,id],|r|r.get(0))?;
        if !exists {drop(custody);drop(budget);tx.commit()?;return Ok(json!({"version":1,"state":"unknown"}))}
        let saved=match custody.as_ref(){Some(view)=>review_receipt_custody(&tx,&original.account_id,&id,view)?,None=>review_receipt(&tx,current_owner,&original.account_id,&id)?};
        if serde_json::to_string(&saved)?!=serde_json::to_string(original)? {return Err(RepositoryError::NotFound)}
        let mut created=Vec::new();for item in &saved.changes {if let S02Change::CreateDefinition{local_key,subject_entity_id,label,..}=item {created.push(json!({"block_id":create_parts(&saved,local_key,subject_entity_id,label)?.0.block_id,"revision":2}));}}
        drop(custody);drop(budget);tx.commit()?;Ok(json!({"version":1,"state":"recorded","receipt_id":id,"generation":saved.expected_generation+1,"created":created}))
    }
}

/// Pre-prepare factual target offer. Its source commitment is derived only
/// from current verified root/score/value/policy facts, never the selection
/// envelope that will later contain it.
pub(crate) fn importance_target_facts_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    profile(tx)?;require_policy_projection(tx,account,workspace,Some(policy))?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance_target.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") || view.current_owner()!=owner || view.account_id()!=account {return Err(RepositoryError::NotFound)}
    let context=super::s08_policy_review::current_importance_context(tx,owner,account,workspace,candidate,view,budget)?;
    if context["importance"]["origin"]!="inferred" {return Err(RepositoryError::NotFound)}
    let block=context["block_id"].as_str().ok_or(RepositoryError::NotFound)?;
    let revision=context["revision"].as_i64().filter(|v|*v>0).ok_or(RepositoryError::NotFound)?;
    let size:Option<i64>=tx.query_row("SELECT length(CAST(value_json AS BLOB)) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,block,revision],|r|r.get(0)).optional()?;
    let size=size.filter(|n|(1..=16384).contains(n)).ok_or(RepositoryError::NotFound)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let value:String=tx.query_row("SELECT value_json FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![owner,block,revision],|r|r.get(0))?;
    let typed=crate::field_authority::parse_unique_json(&value).map_err(RepositoryError::Validation)?;
    let scope=S02GraphScope{owner:owner.into(),account_id:account.into(),workspace_id:workspace.into(),project_id:None,block_id:block.into(),expected_revision:revision};
    let mut graph=s02_graph_snapshot(tx,&scope,Some(view))?;
    // The exact verified S08 root starts with only the score member, and its
    // supported score-successor chain proves every other metadata key stayed
    // unchanged. This is positive absence, not a Human override or a claim
    // about legacy records or unsupported successors.
    if graph["manual_authority"]["importance"]!="unknown" {return Err(RepositoryError::NotFound)}
    let absence=json!(["s02.policy-inheritance-absence.v1",account,owner,workspace,block,revision,context["metadata_sha256"],context["predecessor_receipt_id"],context["predecessor_sha256"]]);
    let absence_raw=serde_json::to_vec(&absence)?;budget.check(absence_raw.len()).map_err(RepositoryError::Validation)?;
    graph["manual_authority"]["importance"]=json!({"state":"known_absent","witness_sha256":sha256_hex(&absence_raw)});
    let graph_raw=serde_json::to_vec(&graph)?;
    if graph_raw.len()>262144 {return Err(RepositoryError::NotFound)}budget.check(graph_raw.len()).map_err(RepositoryError::Validation)?;
    let value_sha=sha256_hex(value.as_bytes());let policy_sha=sha256_hex(&graph_raw);
    let source=json!(["s02.importance-target-source.v1",account,owner,workspace,block,revision,value_sha,context["metadata_sha256"],context["predecessor_receipt_id"],context["predecessor_sha256"],context["curated_id"],policy]);
    let source_raw=serde_json::to_vec(&source)?;budget.check(source_raw.len()).map_err(RepositoryError::Validation)?;
    let target=json!({"version":1,"block_id":block,"expected_revision":revision,"value_sha256":value_sha,"source_commitment_sha256":sha256_hex(&source_raw),"policy_snapshot_sha256":policy_sha});
    require_policy_projection(tx,account,workspace,Some(policy))?;
    Ok(json!({"version":1,"state":"ready","target":target,"score_context":context,"typed_value":typed,"value_json":value,"snapshot":graph}))
}

/// An owned block/revision is discovery only. Bound its identity fields before
/// hydration, then prove the exact S08 root and current score chain.
pub(crate) fn importance_target_for_block_in(tx:&Transaction<'_>,owner:&str,account:&str,workspace:&str,block:&str,expected_revision:i64,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if block.is_empty() || block.trim()!=block || block.len()>512 || expected_revision<1 {return Err(RepositoryError::NotFound)}
    let size:Option<i64>=tx.query_row("SELECT length(CAST(subject_entity_id AS BLOB))+length(CAST(predicate AS BLOB))+length(CAST(claim_slot AS BLOB))+length(CAST(cardinality AS BLOB))+length(CAST(workspace_key AS BLOB))+length(CAST(project_key AS BLOB)) FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND block_id=?2",params![owner,block],|r|r.get(0)).optional()?;
    let size=size.filter(|n|(1..=8192).contains(n)).ok_or(RepositoryError::NotFound)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
    let (identity,w,p,head)=read_knowledge_identity(tx,owner,block)?;
    if w!=workspace || !p.is_empty() || head!=expected_revision || identity.claim_slot.is_empty() {return Err(RepositoryError::NotFound)}
    let facts=importance_target_facts_in(tx,owner,account,workspace,&identity.claim_slot,policy,view,budget)?;
    if facts["target"]["block_id"]!=block || facts["target"]["expected_revision"]!=expected_revision {return Err(RepositoryError::NotFound)}
    Ok(facts)
}

/// Same-transaction factual producer/recheck; no publication authority.
pub(crate) fn s08_policy_snapshots_in(tx:&Transaction<'_>,selection:&S08RulesSelection,policy:&PolicyPublicationRefs,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
        profile(tx)?;require_policy_projection(tx,&selection.source_account_id,&selection.workspace_id,Some(policy))?;
        if view.current_owner()!=selection.owner || view.account_id()!=selection.source_account_id {return Err(RepositoryError::NotFound)}view.for_captured(tx,&selection.owner)?;
        let raw=serde_json::to_vec(selection)?;if raw.len()>262144 {return Err(RepositoryError::NotFound)}budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        // A journal selection has no block or graph snapshot: its commitment is its own selection digest, after the cited archive
        // sources pass the same custody proofs an admission carries.
        if let Some(journal)=&selection.journal {
            super::s08_journal::S08JournalDeltaSelection::custody(tx,selection,view,budget)?;
            let mut result=json!({"version":1,"state":"ready","selection_sha256":sha256_hex(&raw),"journal_sha256":journal.selection_sha256,"snapshots":[]});
            result["commitment_sha256"]=json!(journal.selection_sha256);
            return Ok(result);
        }
        let guarded=std::cell::RefCell::new(&mut *budget);
        let mut check=|captured:&str,source:Option<(&HostArchiveSourceRef,&mut ObservationOriginAuthority)>|->Result<(),RepositoryError>{
            view.for_captured(tx,captured)?;
            if let Some((source,authority))=source {
                if source.account_id!=selection.source_account_id {return Err(RepositoryError::NotFound)}
                validate_observation_origin_custody(tx,&selection.owner,&selection.owner,&selection.workspace_id,source,authority,view,&mut guarded.borrow_mut())?;
            }Ok(())
        };
        let mut target_verified=false;
        let mut prove_target=|item:&FoundMemoryRef|->Result<(),RepositoryError>{
            let target=selection.importance_target.as_ref().ok_or(RepositoryError::NotFound)?;
            if item.block_id!=target.block_id || item.revision!=target.expected_revision || item.value_sha256!=target.value_sha256 {return Err(RepositoryError::NotFound)}
            if !target_verified {
                let facts=importance_target_for_block_in(tx,&selection.owner,&selection.source_account_id,&selection.workspace_id,&item.block_id,item.revision,policy,view,&mut guarded.borrow_mut())?;
                if facts["target"]!=serde_json::to_value(target)? || facts["score_context"]["block_id"]!=item.block_id {return Err(RepositoryError::NotFound)}
                target_verified=true;
            }
            Ok(())
        };
        let proof:Option<&mut dyn FnMut(&FoundMemoryRef)->Result<(),RepositoryError>>=if selection.importance_target.is_some() {Some(&mut prove_target)} else {None};
        let admitted=V2Repository::validate_s08_rules_selection_inner(tx,selection,chrono::Utc::now(),true,Some(&mut check),proof)?;drop(prove_target);drop(check);drop(guarded);
        if let Some(target)=selection.importance_target.as_ref() {
            let item=selection.candidates.first().filter(|_|selection.candidates.len()==1).ok_or(RepositoryError::NotFound)?;
            let (identity,w,p,head)=read_knowledge_identity(tx,&selection.owner,&item.block_id)?;
            if w!=selection.workspace_id || !p.is_empty() || head!=target.expected_revision || identity.claim_slot.is_empty() {return Err(RepositoryError::NotFound)}
            let facts=importance_target_facts_in(tx,&selection.owner,&selection.source_account_id,&selection.workspace_id,&identity.claim_slot,policy,view,budget)?;
            if facts["target"]!=serde_json::to_value(target)? || facts["score_context"]["block_id"]!=target.block_id {return Err(RepositoryError::NotFound)}
            let context=&facts["score_context"];
            let mut result=json!({"version":1,"state":"ready","selection_sha256":sha256_hex(&raw),"source_sha256":target.source_commitment_sha256,"snapshots":[{"reference":item,"mirror_sha256":sha256_hex(&serde_json::to_vec(context)?),"snapshot_sha256":target.policy_snapshot_sha256,"typed_value":facts["typed_value"],"value_json":facts["value_json"],"snapshot":facts["snapshot"]}]});
            result["commitment_sha256"]=json!(sha256_hex(&serde_json::to_vec(&result)?));return Ok(result);
        }
        let mut snapshots=Vec::new();
        for item in &selection.candidates {
            budget.check(item.block_id.len()).map_err(RepositoryError::Validation)?;
            let (identity,w,p,head)=read_knowledge_identity(tx,&selection.owner,&item.block_id)?;
            if w!=selection.workspace_id || !p.is_empty() || head!=item.revision || !identifier(&identity.claim_slot) {return Err(RepositoryError::NotFound)}
            let curated=&identity.claim_slot;
            let context=current_import_mirror_context_custody(tx,&selection.workspace_id,curated,view)?;
            if context.get("reviewer_account_id").and_then(Value::as_str)!=Some(selection.source_account_id.as_str()) {return Err(RepositoryError::NotFound)}
            let binding:CuratedMirrorBinding=serde_json::from_value(context.get("binding").cloned().ok_or(RepositoryError::NotFound)?)?;
            if binding.block_id!=item.block_id || binding.revision!=item.revision || binding.curated_id!=*curated || binding.field_map.get("content").map(String::as_str)!=Some("/value") {return Err(RepositoryError::NotFound)}
            let value_len:Option<i64>=tx.query_row("SELECT length(CAST(value_json AS BLOB)) FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![selection.owner,item.block_id,item.revision],|r|r.get(0)).optional()?;let value_len=value_len.ok_or(RepositoryError::NotFound)?;if !(0..=16384).contains(&value_len) {return Err(RepositoryError::NotFound)}budget.check(value_len as usize).map_err(RepositoryError::Validation)?;
            let value:String=tx.query_row("SELECT value_json FROM fm_v2_knowledge_revisions WHERE owner_id=?1 AND block_id=?2 AND revision=?3",params![selection.owner,item.block_id,item.revision],|r|r.get(0))?;
            if sha256_hex(value.as_bytes())!=item.value_sha256 {return Err(RepositoryError::NotFound)}
            let typed=crate::field_authority::parse_unique_json(&value).map_err(RepositoryError::Validation)?;
            let content=typed.get("value").and_then(Value::as_str).filter(|v|!v.is_empty() && v.len()<=16384).ok_or(RepositoryError::NotFound)?;
            if typed.get("type")!=Some(&json!("string")) {return Err(RepositoryError::NotFound)}
            let curated_len:Option<i64>=tx.query_row("SELECT length(CAST(content AS BLOB)) FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND archived=0",params![selection.owner,selection.workspace_id,curated],|r|r.get(0)).optional()?;
            let curated_len=curated_len.ok_or(RepositoryError::NotFound)?;if !(0..=16384).contains(&curated_len) {return Err(RepositoryError::NotFound)}budget.check(curated_len as usize).map_err(RepositoryError::Validation)?;
            let stored:String=tx.query_row("SELECT content FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND archived=0",params![selection.owner,selection.workspace_id,curated],|r|r.get(0))?;
            if stored!=content {return Err(RepositoryError::NotFound)}
            let scope=S02GraphScope{owner:selection.owner.clone(),account_id:selection.source_account_id.clone(),workspace_id:selection.workspace_id.clone(),project_id:selection.project_id.clone(),block_id:item.block_id.clone(),expected_revision:item.revision};
            let graph=s02_graph_snapshot(tx,&scope,Some(view))?;let graph_raw=serde_json::to_vec(&graph)?;if graph_raw.len()>262144 {return Err(RepositoryError::NotFound)}budget.check(graph_raw.len()).map_err(RepositoryError::Validation)?;
            let mirror_sha256=sha256_hex(&serde_json::to_vec(&context)?);
            snapshots.push(json!({"reference":item,"mirror_sha256":mirror_sha256,"snapshot_sha256":sha256_hex(&graph_raw),"typed_value":typed,"snapshot":graph}));
        }
        let source_sha256=sha256_hex(&serde_json::to_vec(&admitted)?);
        let mut result=json!({"version":1,"state":"ready","selection_sha256":sha256_hex(&raw),"source_sha256":source_sha256,"snapshots":snapshots});
        let commitment=sha256_hex(&serde_json::to_vec(&result)?);result["commitment_sha256"]=json!(commitment);
        Ok(result)
}

impl<'a> V2Repository<'a> {
    /// Caller holds the explicit S02 opt-in, Host source guards and S05 authority.
    pub(crate) fn s08_policy_snapshots(&mut self,selection:&S08RulesSelection,policy:&PolicyPublicationRefs)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;let mut budget=ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&selection.source_account_id,&selection.owner,&selection.owner,&mut budget)?;
        let result=s08_policy_snapshots_in(&tx,selection,policy,&view,&mut budget)?;drop(view);drop(budget);tx.commit()?;Ok(result)
    }
}
