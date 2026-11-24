//! S05 skill-draft purpose. Source selectors are evidence, never authority;
//! Host holds source locks and validates the current route/grant before issue.
use super::*;
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Part {#[serde(rename="messageID")] pub message_id:String,#[serde(rename="partID")] pub part_id:String,pub revision:i64,#[serde(rename="contentHash")] pub content_hash:String}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SelectedSource {pub source:HostArchiveSourceRef,pub parts:Vec<Part>}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Assignment {pub version:u32,pub connection_id:String,pub model_route_id:String,pub provider_grant_id:Option<String>,pub teacher_spec_sha256:String}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Original {pub version:u32,pub owner:String,pub account_id:String,pub workspace_id:String,pub operation_id:String,pub ordered_sources:Vec<SelectedSource>,pub selection_sha256:String,pub policy_projection:PolicyPublicationRefs,pub account_policy_input_json:String,pub target_fingerprint:String,pub policy_fingerprint:String,pub prompt_sha256:String,pub assignment_sha256:String,pub assignment:Assignment,pub connection_id:String,pub model_route_id:String,pub max_input_tokens:u32,pub max_output_tokens:u32,pub daily_calls:u32,pub daily_tokens:u32}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Reserve {pub version:u32,pub original:Original,pub reservation:S08RulesReservation}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Attempt {pub version:u32,pub reservation_original:Reserve,pub job_id:String,pub attempt_id:String,pub lease_epoch:i64}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Usage {pub elapsed_seconds:u32,pub input_tokens:u32,pub output_tokens:u32}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Terminal {pub version:u32,pub attempt:Attempt,pub disposition:String,pub draft_sha256:Option<String>,pub usage:Option<Usage>}
fn text(v:&str)->bool {!v.is_empty()&&v.len()<=512&&v.trim()==v&&!v.chars().any(char::is_control)}
fn digest<T:Serialize>(v:&T)->Result<String,RepositoryError>{Ok(sha256_hex(&serde_json::to_vec(v)?))}
pub(crate) fn require_capture_inputs(prefs:&serde_json::Map<String,Value>)->Result<(),RepositoryError>{
    // Reproduce the persisted compatibility capture inputs; ephemeral chat
    // choices are still independently held and checked by the Host owner.
    let entry=|key:&str|->Result<Option<&Value>,RepositoryError>{let pair=prefs.get(key).and_then(Value::as_array).filter(|v|v.len()==2).ok_or(RepositoryError::NotFound)?;match pair[0].as_bool(){Some(false) if pair[1].is_null()=>Ok(None),Some(true)=>Ok(Some(&pair[1])),_=>Err(RepositoryError::NotFound)}};
    let auto=entry("auto_memory")?.map(|v|v.as_bool().ok_or(RepositoryError::NotFound)).transpose()?;
    let _enabled=entry("memory_enabled")?.map(|v|v.as_bool().ok_or(RepositoryError::NotFound)).transpose()?;
    let capture=match entry("memory_mode")? {Some(v)=>match v.as_str().map(|s|s.trim().to_ascii_lowercase()).as_deref(){Some("manual"|"automatic")=>true,Some("off")=>false,_=>return Err(RepositoryError::NotFound)},None=>auto.unwrap_or(true)};
    if !capture{return Err(RepositoryError::NotFound)}
    Ok(())
}
fn require_capture_preferences(prefs:&serde_json::Map<String,Value>)->Result<(),RepositoryError>{
    require_capture_inputs(prefs)?;
    if prefs.get("agent_settings_distill_version")!=Some(&json!([true,1])) {return Err(RepositoryError::NotFound)}
    Ok(())
}
// Decode only genuine stored policy envelopes. The outer account projection
// proves the exact current preference bytes; each stored envelope also carries
// its own canonical fingerprint and must retain its closed shape.
pub(crate) fn accepted_policy(document:&Value)->Result<&serde_json::Map<String,Value>,RepositoryError> {
    fn fingerprinted(value:&Value, names:&[&str], schema:&str)->Result<(),RepositoryError> {
        let map=value.as_object().ok_or(RepositoryError::NotFound)?;
        if map.len()!=names.len() || names.iter().any(|key|!map.contains_key(*key))
            || map.get("schema").and_then(Value::as_str)!=Some(schema)
            || map.get("revision").and_then(Value::as_u64).is_none_or(|v|v==0)
        {return Err(RepositoryError::NotFound)}
        let expected=map.get("fingerprint").and_then(Value::as_str).filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
        let mut body=map.clone();body.remove("fingerprint");
        if sha256_hex(&serde_json::to_vec(&body)?)!=expected {return Err(RepositoryError::NotFound)}
        Ok(())
    }
    let schema=document.get("schema").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
    let accepted=match schema {
        "openclank.memory-policy.v1"=>document,
        "openclank.memory-policy.v2"|"openclank.memory-policy.v3"=>{
            let names=if schema.ends_with("v3") {vec!["schema","revision","accepted_policy","workKindAssignments","fingerprint","ratings_override"]} else {vec!["schema","revision","accepted_policy","workKindAssignments","fingerprint"]};
            fingerprinted(document,&names,schema)?;
            let assignments=document.get("workKindAssignments").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
            if assignments.len()!=3 || ["interpretation","synthesis","skill_processing"].iter().any(|key|!assignments.contains_key(*key)) {return Err(RepositoryError::NotFound)}
            for value in assignments.values() {
                let entry=value.as_object().ok_or(RepositoryError::NotFound)?;
                if entry.len()!=2 || !entry.contains_key("selection") || entry.get("revision").and_then(Value::as_u64).is_none() {return Err(RepositoryError::NotFound)}
                let selection=&entry["selection"];
                if !selection.is_null() {
                    let item=selection.as_object().ok_or(RepositoryError::NotFound)?;
                    let fields=["version","modelRouteID","connectionID","modelID","grantID","optionRouteID","catalogRevision","options"];
                    if item.len()!=fields.len() || fields.iter().any(|key|!item.contains_key(*key))
                        || item["version"].as_u64()!=Some(1) || item["catalogRevision"].as_u64().is_none_or(|v|v==0 || v>9_007_199_254_740_991)
                        || ["modelRouteID","connectionID","modelID","optionRouteID"].iter().any(|key|item[*key].as_str().is_none_or(|v|v.is_empty()||v.trim()!=v||v.chars().count()>256))
                        || (!item["grantID"].is_null() && item["grantID"].as_str().is_none_or(|v|v.is_empty()||v.trim()!=v||v.chars().count()>256))
                    {return Err(RepositoryError::NotFound)}
                    let options=item["options"].as_object().ok_or(RepositoryError::NotFound)?;
                    if options.keys().any(|key|key!="reasoning_effort") || options.values().any(|v|v.as_str().is_none_or(|s|s.is_empty()||s.chars().count()>32||matches!(s,"default"|"inherit"))) {return Err(RepositoryError::NotFound)}
                }
            }
            if schema.ends_with("v3") {
                let ratings=document.get("ratings_override").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
                if ratings.len()!=3 || ["visible","collection_enabled","explicit_submission_only"].iter().any(|key|!ratings.get(*key).is_some_and(Value::is_boolean))
                    || ratings["explicit_submission_only"]!=json!(true) {return Err(RepositoryError::NotFound)}
            }
            document.get("accepted_policy").filter(|v|!v.is_null()).ok_or(RepositoryError::NotFound)?
        },
        _=>return Err(RepositoryError::NotFound),
    };
    fingerprinted(accepted,&["schema","revision","policy","capabilities","legacy_at_acceptance","fingerprint"],"openclank.memory-policy.v1")?;
    if !accepted.get("capabilities").is_some_and(Value::is_object)
        || !accepted.get("legacy_at_acceptance").is_some_and(Value::is_object)
    {return Err(RepositoryError::NotFound)}
    accepted.get("policy").and_then(Value::as_object).ok_or(RepositoryError::NotFound)
}

fn policy(tx:&Transaction<'_>,o:&Original)->Result<(u32,u32,u32,u32,u32,u32,u32,u32,u32,u64),RepositoryError>{
    require_policy_projection(tx,&o.account_id,&o.workspace_id,Some(&o.policy_projection))?;
    if o.account_policy_input_json.len()>65536{return Err(RepositoryError::NotFound)}
    crate::retrieval::rank_learning::projected_policy(&o.account_policy_input_json,&o.policy_projection.account.fingerprint).map_err(RepositoryError::Validation)?;
    let body=crate::field_authority::parse_unique_json(&o.account_policy_input_json).map_err(RepositoryError::Validation)?;
    if body.get("can_manage_memory")!=Some(&json!(true)){return Err(RepositoryError::NotFound)}
    let prefs=body.get("preferences").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    require_capture_preferences(prefs)?;
    let distill=prefs.get("agent_settings_distill").and_then(Value::as_array).filter(|v|v.len()==2&&v[0]==json!(true)).and_then(|v|v[1].as_object()).ok_or(RepositoryError::NotFound)?;
    if distill.get("auto").and_then(Value::as_bool).unwrap_or(false)!=true{return Err(RepositoryError::NotFound)}
    let interval=match distill.get("interval_days") {None=>30,Some(v)=>v.as_u64().filter(|n|*n<=365).ok_or(RepositoryError::NotFound)?};
    let wrapped=prefs.get("memory_policy").and_then(Value::as_array).filter(|v|v.len()==2&&v[0]==json!(true)).and_then(|v|v[1].as_object()).ok_or(RepositoryError::NotFound)?;
    let doc=accepted_policy(&Value::Object(wrapped.clone()))?.clone();let schedule=doc.get("schedule").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;let budget=doc.get("budget").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    if schedule.get("mode")!=Some(&json!("idle")){return Err(RepositoryError::NotFound)}
    let read=|map:&serde_json::Map<String,Value>,name:&str|->Result<u32,RepositoryError>{map.get(name).and_then(Value::as_u64).and_then(|v|u32::try_from(v).ok()).ok_or(RepositoryError::NotFound)};
    let quiet=read(schedule,"quiet_seconds")?;let calls=read(budget,"daily_calls")?;let seconds=read(budget,"daily_seconds")?;let tokens=read(budget,"daily_tokens")?;let job=read(budget,"job_seconds")?;let output=read(budget,"job_output_tokens")?;let owner_queue=read(budget,"owner_queue")?;let global_queue=read(budget,"global_queue")?;let concurrency=read(budget,"concurrency")?;
    if quiet>86400||calls==0||calls>1_000_000||seconds<job||seconds>86400||job==0||job>300||tokens==0||tokens>10_000_000||output==0||output>32768||owner_queue==0||global_queue==0||concurrency!=1||o.daily_calls!=calls||o.daily_tokens!=tokens||o.max_output_tokens==0||o.max_output_tokens>output||o.max_input_tokens==0||o.max_input_tokens>1_000_000{return Err(RepositoryError::NotFound)}
    Ok((quiet,calls,seconds,tokens,job,output,owner_queue,global_queue,concurrency,interval))
}
fn validate_original_identity(o:&Original)->Result<(),RepositoryError>{
    if o.version!=1 || [&o.owner,&o.account_id,&o.workspace_id,&o.connection_id,&o.model_route_id].iter().any(|s|!text(s)) || o.connection_id=="local:rules" || [&o.selection_sha256,&o.target_fingerprint,&o.policy_fingerprint,&o.prompt_sha256,&o.assignment_sha256].iter().any(|s|!valid_sha256(s)) || !(2..=8).contains(&o.ordered_sources.len()) {return Err(RepositoryError::NotFound)}
    if o.assignment.version!=1 || o.assignment.connection_id!=o.connection_id || o.assignment.model_route_id!=o.model_route_id || o.assignment.provider_grant_id.as_ref().is_some_and(|x|!text(x)) || !valid_sha256(&o.assignment.teacher_spec_sha256) || digest(&serde_json::to_value(&o.assignment)?)?!=o.assignment_sha256 {return Err(RepositoryError::NotFound)}
    let mut chats=BTreeSet::new();for selected in &o.ordered_sources {selected.source.validate()?;if selected.source.version!=1 || selected.source.actor_id!="main" || selected.source.account_id!=o.account_id || !chats.insert(&selected.source.chat_id) || !(2..=8).contains(&selected.parts.len()){return Err(RepositoryError::NotFound)}let mut ids=BTreeSet::new();for part in &selected.parts {if !text(&part.message_id)||!text(&part.part_id)||part.revision<1||!valid_sha256(&part.content_hash)||!ids.insert((&part.message_id,&part.part_id)){return Err(RepositoryError::NotFound)}}}
    let selected=json!({"version":1,"ordered_sources":o.ordered_sources});let raw=serde_json::to_vec(&selected)?;if raw.len()>24576||sha256_hex(&raw)!=o.selection_sha256{return Err(RepositoryError::NotFound)}
    let expected=format!("skilldraft_{}",digest(&json!(["openclank.skill-draft.operation.v1",o.account_id,o.workspace_id,o.selection_sha256]))?);if expected!=o.operation_id{return Err(RepositoryError::NotFound)}
    Ok(())
}
fn validate(tx:&Transaction<'_>,o:&Original)->Result<(),RepositoryError>{
    validate_original_identity(o)?;if !crate::store::sqlite::s05_skill_draft_profile(tx)? {return Err(RepositoryError::NotFound)}
    for selected in &o.ordered_sources {selected.source.validate_in(tx)?;}
    let db:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let mut guard=ResetFenceBudget::install(tx);let view=owner_custody::verified_or_direct_custody_view(tx,&db,&o.account_id,&o.owner,&o.owner,&mut guard)?;view.for_captured(tx,&o.owner)?;drop(view);drop(guard);policy(tx,o)?;Ok(())
}
fn original_raw(o:&Original)->Result<(String,String),RepositoryError>{let value=serde_json::to_value(o)?;let raw=serde_json::to_string(&value)?;if raw.len()>65536{return Err(RepositoryError::NotFound)}Ok((sha256_hex(raw.as_bytes()),raw))}
fn receipt_id(kind:&str,o:&Original)->String {format!("cs_skill_{}_{}",kind,sha256_hex(o.operation_id.as_bytes()))}
fn write_job_receipt(tx:&Transaction<'_>,sql_owner:&str,o:&Original,kind:&str,payload:&Value)->Result<(),RepositoryError>{let id=receipt_id(kind,o);let raw=serde_json::to_string(payload)?;let now=chrono::Utc::now().to_rfc3339();tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','skill_draft',?3,NULL,?4,?5,?6,?7)",params![sql_owner,id,format!("skill_draft.{kind}.v1"),CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;insert_outbox(tx,sql_owner,&id,&format!("skill_draft_{kind}"),payload,0,&now)?;Ok(())}
fn write_receipt(tx:&Transaction<'_>,o:&Original,kind:&str,payload:&Value)->Result<(),RepositoryError>{write_job_receipt(tx,&o.owner,o,kind,payload)}
fn read_job_receipt(tx:&Transaction<'_>,owner:&str,job:&str,kind:&str)->Result<Option<Value>,RepositoryError>{let id=format!("cs_skill_{}_{}",kind,sha256_hex(job.as_bytes()));let row:Option<(String,String,String,String,Option<String>,String)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,source_event_id,contract_version FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason=?3",params![owner,id,format!("skill_draft.{kind}.v1")],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;let Some((hash,raw,actor,actor_id,source,contract))=row else{return Ok(None)};if raw.len()>65536||hash!=sha256_hex(raw.as_bytes())||actor!="automation"||actor_id!="skill_draft"||source.is_some()||contract!=CONTRACT_VERSION{return Err(RepositoryError::NotFound)}let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;let out:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,format!("outbox_{id}_skill_draft_{kind}"),id,format!("skill_draft_{kind}")],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;if out.is_none_or(|(body,revision)|revision!=0||crate::field_authority::parse_unique_json(&body).ok()!=Some(value.clone())){return Err(RepositoryError::NotFound)}Ok(Some(value))}
fn read_receipt(tx:&Transaction<'_>,o:&Original,kind:&str)->Result<Option<Value>,RepositoryError>{read_job_receipt(tx,&o.owner,&o.operation_id,kind)}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetiredPart {pub actor_id:String,pub message_id:String,pub part_id:String,pub revision:i64,pub content_hash:String}
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Retirement {pub version:u32,pub owner:String,pub account_id:String,pub chat_id:String,pub operation_id:String,#[serde(deserialize_with="required_retired_part")] pub part:Option<RetiredPart>}
fn required_retired_part<'de,D:serde::Deserializer<'de>>(d:D)->Result<Option<RetiredPart>,D::Error>{Option::<RetiredPart>::deserialize(d)}
fn validate_retirement(r:&Retirement)->Result<(),RepositoryError>{
    if r.version!=1 || [&r.owner,&r.account_id,&r.chat_id].iter().any(|s|!text(s)) {return Err(RepositoryError::NotFound)}
    if let Some(p)=&r.part {if p.actor_id!="main"||!text(&p.message_id)||!text(&p.part_id)||p.revision<1||!valid_sha256(&p.content_hash){return Err(RepositoryError::NotFound)}}
    let id=format!("skillretire_{}",digest(&json!(["openclank.skill-draft.retirement.v1",r.owner,r.account_id,r.chat_id,r.part]))?);
    if id!=r.operation_id{return Err(RepositoryError::NotFound)}Ok(())
}
// Transaction-local, bounded inventory for the existing source-fence owner.
// A selector is evidence only; the caller supplies the authenticated account
// and chat identity and must keep this same transaction through retirement.
pub(crate) fn source_fence_matches(tx:&Transaction<'_>,r:&Retirement,budget:&mut ResetFenceBudget)->Result<Vec<Original>,RepositoryError>{
    validate_retirement(r)?;let owner=&r.owner;let account=&r.account_id;let chat_id=&r.chat_id;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0))?;let view=owner_custody::verified_or_direct_custody_view(tx,&database,account,owner,owner,budget)?;
    let mut stmt=tx.prepare("SELECT j.job_id,j.input_hash,j.workspace_key,m.selected_json,m.input_hash FROM fm_v2_jobs j LEFT JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' WHERE j.owner_id=?1 AND j.kind='skill_draft' ORDER BY j.job_id LIMIT 10001")?;
    let mut rows=stmt.query([owner])?;let mut count=0usize;let mut found=Vec::new();
    while let Some(row)=rows.next()? {
        count+=1;if count>10000{return Err(RepositoryError::Validation("skill draft source inventory exceeds bound".into()))}
        let job:String=row.get(0)?;let hash:String=row.get(1)?;let job_workspace:Option<String>=row.get(2)?;let raw:Option<String>=row.get(3)?;let manifest_hash:Option<String>=row.get(4)?;
        if raw.is_none() {
            let erased=read_job_receipt(tx,owner,&job,"erased")?.ok_or(RepositoryError::NotFound)?;
            let captured=erased["owner"].as_str().ok_or(RepositoryError::NotFound)?;view.for_captured(tx,captured)?;
            if erased!=json!({"version":1,"account_id":r.account_id,"owner":captured,"job_id":job,"workspace_id":job_workspace,"original_sha256":hash}) || manifest_hash.is_some(){return Err(RepositoryError::NotFound)}
            continue
        }
        let raw=raw.filter(|v|v.len()<=65536).ok_or(RepositoryError::NotFound)?;
        budget.check(raw.len().saturating_add(job.len())).map_err(RepositoryError::Validation)?;
        if sha256_hex(raw.as_bytes())!=hash||manifest_hash.as_deref()!=Some(hash.as_str()){return Err(RepositoryError::NotFound)}
        let original:Original=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        validate_original_identity(&original)?;
        view.for_captured(tx,&original.owner)?;if original.operation_id!=job||job_workspace.as_deref()!=Some(original.workspace_id.as_str())||original_raw(&original)?!=(hash.clone(),raw){return Err(RepositoryError::NotFound)}
        let receipt=read_job_receipt(tx,owner,&job,"prepare")?.ok_or(RepositoryError::NotFound)?;
        if receipt!=json!({"version":1,"original_sha256":hash,"job_id":job,"account_id":original.account_id}) {return Err(RepositoryError::NotFound)}
        let mut matched=false;for selected in &original.ordered_sources {if &selected.source.account_id==account && &selected.source.chat_id==chat_id && r.part.as_ref().is_none_or(|p| selected.source.actor_id==p.actor_id && ((selected.source.message_id==p.message_id && selected.source.part_id==p.part_id && selected.source.revision==p.revision && selected.source.content_hash==p.content_hash) || selected.parts.iter().any(|part|part.message_id==p.message_id && part.part_id==p.part_id && part.revision==p.revision && part.content_hash==p.content_hash))) {matched=true;}}
        if matched {if &original.account_id!=account{return Err(RepositoryError::NotFound)}found.push(original);}
    }
    Ok(found)
}
// This staged selector retirement is called only within the existing guarded
// account/chat fence transaction. Unknown or dispatched work aborts the entire
// transaction; no model call is retried and no terminal outcome is inferred.
pub(crate) fn retire_selected(tx:&Transaction<'_>,r:&Retirement,budget:&mut ResetFenceBudget)->Result<usize,RepositoryError>{
    let selected=source_fence_matches(tx,r,budget)?;let now=chrono::Utc::now().to_rfc3339();
    for o in &selected {
        let state:String=tx.query_row("SELECT state FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind='skill_draft'",params![r.owner,o.operation_id],|row|row.get(0))?;
        match state.as_str() {
            "queued"=>{
                let clean:bool=tx.query_row("SELECT NOT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2)",params![r.owner,o.operation_id],|row|row.get(0))?;
                if !clean||read_job_receipt(tx,&r.owner,&o.operation_id,"dispatch")?.is_some()||read_job_receipt(tx,&r.owner,&o.operation_id,"prepared_cancel")?.is_some(){return Err(RepositoryError::NotFound)}
                if tx.execute("UPDATE fm_v2_jobs SET state='cancelled',updated_at=?3 WHERE owner_id=?1 AND job_id=?2 AND state='queued' AND attempt_count=0",params![r.owner,o.operation_id,now])?!=1{return Err(RepositoryError::NotFound)}
                write_job_receipt(tx,&r.owner,o,"prepared_cancel",&json!({"version":1,"state":"cancelled","job_id":o.operation_id}))?;
            },
            "active"=>{
                if read_job_receipt(tx,&r.owner,&o.operation_id,"dispatch")?.is_some(){return Err(RepositoryError::Validation("skill draft issue is unresolved".into()))}
                let reserve=read_job_receipt(tx,&r.owner,&o.operation_id,"reserve")?.ok_or(RepositoryError::NotFound)?;
                let attempt=format!("skillattempt_{}",sha256_hex(o.operation_id.as_bytes()));let epoch=1i64;
                let request_sha=reserve["request_sha256"].as_str().filter(|v|valid_sha256(v)).ok_or(RepositoryError::NotFound)?;
                let foreground=reserve["foreground_epoch"].as_i64().filter(|n|*n>=0).ok_or(RepositoryError::NotFound)?;
                if reserve!=json!({"version":1,"original_sha256":original_raw(o)?.0,"request_sha256":request_sha,"account_id":o.account_id,"owner":o.owner,"job_id":o.operation_id,"attempt_id":attempt,"lease_epoch":epoch,"foreground_epoch":foreground}){return Err(RepositoryError::NotFound)}
                let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND j.state='active' AND a.attempt_id=?3 AND a.lease_epoch=?4 AND a.state='leased' AND q.state='reserved' AND q.reserved_calls=1 AND q.connection_id=?5 AND q.foreground_epoch=?6)",params![r.owner,o.operation_id,attempt,epoch,o.connection_id,reserve["foreground_epoch"].as_i64().ok_or(RepositoryError::NotFound)?],|row|row.get(0))?;
                if !valid{return Err(RepositoryError::NotFound)}
                if tx.execute("UPDATE fm_v2_s05_reservations SET state='revoked' WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='reserved'",params![r.owner,o.operation_id,attempt,epoch])?!=1||tx.execute("UPDATE fm_v2_attempts SET state='fenced',updated_at=?5 WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='leased'",params![r.owner,o.operation_id,attempt,epoch,now])?!=1||tx.execute("UPDATE fm_v2_jobs SET state='retry_wait' WHERE owner_id=?1 AND job_id=?2 AND state='active' AND current_attempt_id=?3",params![r.owner,o.operation_id,attempt])?!=1||tx.execute("UPDATE fm_v2_jobs SET state='cancelled' WHERE owner_id=?1 AND job_id=?2 AND state='retry_wait'",params![r.owner,o.operation_id])?!=1{return Err(RepositoryError::NotFound)}
                write_job_receipt(tx,&r.owner,o,"revoke",&json!({"version":1,"state":"cancelled","job_id":o.operation_id,"attempt_id":attempt,"lease_epoch":epoch}))?;
            },
            "cancelled"=>{
                let cancel=read_job_receipt(tx,&r.owner,&o.operation_id,"prepared_cancel")?;let revoke=read_job_receipt(tx,&r.owner,&o.operation_id,"revoke")?;
                if cancel.is_some()==revoke.is_some(){return Err(RepositoryError::NotFound)}
                if let Some(value)=cancel {let empty:bool=tx.query_row("SELECT NOT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2)",params![r.owner,o.operation_id],|row|row.get(0))?;if !empty||value!=json!({"version":1,"state":"cancelled","job_id":o.operation_id}){return Err(RepositoryError::NotFound)}}
                if let Some(value)=revoke {let attempt=value["attempt_id"].as_str().ok_or(RepositoryError::NotFound)?;let epoch=value["lease_epoch"].as_i64().ok_or(RepositoryError::NotFound)?;let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND q.job_id=?2 AND q.attempt_id=?3 AND q.lease_epoch=?4 AND q.state='revoked' AND a.state='fenced')",params![r.owner,o.operation_id,attempt,epoch],|row|row.get(0))?;if !valid||value!=json!({"version":1,"state":"cancelled","job_id":o.operation_id,"attempt_id":attempt,"lease_epoch":epoch}){return Err(RepositoryError::NotFound)}}
            },
            "succeeded"|"failed_terminal"=>{
                let terminal=read_job_receipt(tx,&r.owner,&o.operation_id,"terminal")?.ok_or(RepositoryError::NotFound)?;let stored:String=tx.query_row("SELECT result_json FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![r.owner,o.operation_id],|row|row.get(0))?;
                if crate::field_authority::parse_unique_json(&stored).map_err(RepositoryError::Validation)?!=terminal{return Err(RepositoryError::NotFound)}
                let attempt=terminal["attempt_id"].as_str().ok_or(RepositoryError::NotFound)?;let epoch=terminal["lease_epoch"].as_i64().ok_or(RepositoryError::NotFound)?;
                let expected=if state=="succeeded" {"succeeded"} else {"failed_terminal"};
                let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND q.job_id=?2 AND q.attempt_id=?3 AND q.lease_epoch=?4 AND q.state='settled' AND q.settled_at_ms IS NOT NULL AND a.state=?5)",params![r.owner,o.operation_id,attempt,epoch,expected],|row|row.get(0))?;
                if !valid||!matches!(terminal["charge_basis"].as_str(),Some("actual"|"reserved_bound"))||!matches!((state.as_str(),terminal["state"].as_str()),("succeeded",Some("draft"|"no_draft"))|("failed_terminal",Some("failed"))){return Err(RepositoryError::NotFound)}
            },
            _=>return Err(RepositoryError::Validation("skill draft issue is unresolved".into())),
        }
        let original_sha=original_raw(o)?.0;
        write_job_receipt(tx,&r.owner,o,"erased",&json!({"version":1,"account_id":o.account_id,"owner":o.owner,"job_id":o.operation_id,"workspace_id":o.workspace_id,"original_sha256":original_sha}))?;
        if tx.execute("DELETE FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase='selection' AND input_hash=?3 AND content_hash=?3",params![r.owner,o.operation_id,original_sha])?!=1{return Err(RepositoryError::NotFound)}
    }
    Ok(selected.len())
}
fn skill_profile_present(tx:&Transaction<'_>)->Result<bool,RepositoryError>{
    let present=crate::store::sqlite::s05_skill_draft_profile(tx)?;
    if !present {let residue:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE kind='skill_draft' OR job_id GLOB 'skilldraft_*') OR EXISTS(SELECT 1 FROM fm_v2_job_manifests WHERE manifest_id GLOB 'skillmanifest_*') OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE reason GLOB 'skill_draft.*' OR change_set_id GLOB 'cs_skill_*') OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE kind GLOB 'skill_draft_*' OR event_id GLOB 'outbox_cs_skill_*')",[],|row|row.get(0))?;if residue{return Err(RepositoryError::NotFound)}}
    Ok(present)
}
fn retirement_scope(tx:&Transaction<'_>,r:&Retirement)->Result<bool,RepositoryError>{
    validate_retirement(r)?;require_guarded_epoch(tx)?;
    let db:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0))?;
    let mut budget=ResetFenceBudget::install(tx);let view=owner_custody::verified_or_direct_custody_view(tx,&db,&r.account_id,&r.owner,&r.owner,&mut budget)?;view.for_captured(tx,&r.owner)?;drop(view);drop(budget);
    let present=skill_profile_present(tx)?;if present {verify_profile_shape(tx)?;}
    Ok(present)
}
fn retirement_status(tx:&Transaction<'_>,r:&Retirement)->Result<Value,RepositoryError>{
    if !retirement_scope(tx,r)? {return Ok(json!({"version":1,"state":"absent","operation_id":r.operation_id,"retired_count":0}))}
    let request_sha=digest(&serde_json::to_value(r)?)?;
    let receipt=read_job_receipt(tx,&r.owner,&r.operation_id,"retire")?;
    if let Some(value)=receipt {
        let count=value["retired_count"].as_u64().filter(|n|*n<=10000).ok_or(RepositoryError::NotFound)?;
        if value!=json!({"version":1,"state":"retired","operation_id":r.operation_id,"request_sha256":request_sha,"retired_count":count}) {return Err(RepositoryError::NotFound)}
        return Ok(value)
    }
    let id=format!("cs_skill_retire_{}",sha256_hex(r.operation_id.as_bytes()));let out=format!("outbox_{id}_skill_draft_retire");
    let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![r.owner,id,out],|row|row.get(0))?;
    if collision{return Err(RepositoryError::NotFound)}
    Ok(json!({"version":1,"state":"unknown","operation_id":r.operation_id}))
}
impl<'a> V2Repository<'a> {
    pub(crate) fn skill_retire_original(&mut self,r:&Retirement)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;let result=retirement_status(&tx,r)?;tx.commit()?;Ok(result)
    }
    pub(crate) fn skill_retire(&mut self,r:&Retirement)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;let before=retirement_status(&tx,r)?;
        if before["state"]!="unknown" {tx.commit()?;return Ok(before)}
        let mut budget=ResetFenceBudget::install(&tx);let count=retire_selected(&tx,r,&mut budget)?;drop(budget);
        let result=json!({"version":1,"state":"retired","operation_id":r.operation_id,"request_sha256":digest(&serde_json::to_value(r)?)?,"retired_count":count});
        let id=format!("cs_skill_retire_{}",sha256_hex(r.operation_id.as_bytes()));let raw=serde_json::to_string(&result)?;let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','skill_draft','skill_draft.retire.v1',NULL,?3,?4,?5,?6)",params![r.owner,id,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
        insert_outbox(&tx,&r.owner,&id,"skill_draft_retire",&result,0,&now)?;tx.commit()?;Ok(result)
    }
}
fn verify_original(tx:&Transaction<'_>,o:&Original)->Result<(),RepositoryError>{validate(tx,o)?;let (hash,raw)=original_raw(o)?;let fact=read_receipt(tx,o,"prepare")?.ok_or(RepositoryError::NotFound)?;if fact!=json!({"version":1,"original_sha256":hash,"job_id":o.operation_id,"account_id":o.account_id}){return Err(RepositoryError::NotFound)}let row:Option<(String,String,String)>=tx.query_row("SELECT input_hash,workspace_key,kind FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![o.owner,o.operation_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;if row!=Some((hash.clone(),o.workspace_id.clone(),"skill_draft".into())){return Err(RepositoryError::NotFound)}let saved:Option<(String,String)>=tx.query_row("SELECT selected_json,input_hash FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2 AND phase='selection'",params![o.owner,o.operation_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;if saved!=Some((raw,hash)){return Err(RepositoryError::NotFound)}Ok(())}
fn same<T:Serialize>(a:&T,b:&T)->Result<bool,RepositoryError>{Ok(serde_json::to_value(a)?==serde_json::to_value(b)?)}
fn verify_profile_shape(tx:&Transaction<'_>)->Result<(),RepositoryError>{
    let expected=Connection::open_in_memory()?;expected.execute_batch(include_str!("../store/current-schema.sql"))?;expected.execute_batch(include_str!("../store/schema-v17-additions.sql"))?;expected.execute_batch(include_str!("../store/schema-v17-authority.sql"))?;
    if crate::store::sqlite::s05_account_chat_profile(tx)? {expected.execute_batch(&crate::store::sqlite::s05_account_chat_base_sql())?;}else{expected.execute_batch(include_str!("../store/schema-v17-s05-additions.sql"))?;}
    expected.execute_batch(include_str!("../store/schema-v17-s05-account-v4.sql"))?;expected.execute_batch(include_str!("../store/schema-v17-s08.sql"))?;
    if crate::store::sqlite::s05_account_chat_profile(tx)? {expected.execute_batch(&crate::store::sqlite::s05_account_chat_pair_sql())?;}else{expected.execute_batch(include_str!("../store/schema-v17-s05-assistant-pair.sql"))?;}
    expected.execute_batch(include_str!("../store/schema-v17-s05-assistant-pair-s08.sql"))?;crate::store::sqlite::apply_s08_model_schema(&expected,true)?;expected.execute_batch(include_str!("../store/schema-v17-s05-skill-draft.sql"))?;
    super::s02_policy::apply_target_accounting_profile(tx,&expected)?;
    for name in ["fm_v2_s05_attempt_insert_guard","fm_v2_s05_reservation_initial_guard","fm_v2_s05_job_active_guard","fm_v2_s05_attempt_transition_guard","fm_v2_s05_job_terminal_guard","fm_v2_s05_job_insert_guard","s08_reservation_shape","s08_reservation_shape_update"] {
        let want:String=expected.query_row("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?1",[name],|r|r.get(0))?;let got:Option<String>=tx.query_row("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?1",[name],|r|r.get(0)).optional()?;if got.as_deref()!=Some(want.as_str()){return Err(RepositoryError::NotFound)}
    }
    Ok(())
}
fn verify_shape(tx:&Transaction<'_>,o:&Original)->Result<(),RepositoryError>{verify_profile_shape(tx)?;validate(tx,o)}
/// Content-free skill usage and original-ID denial bound to an existing
/// guarded reset receipt. It is never a source or execution grant.
#[derive(Debug,Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResetTerminal {
    pub version:u32,pub captured_owner:String,pub account_id:String,pub workspace_id:String,
    pub job_id:String,pub original_sha256:String,pub disposition:String,pub charge_basis:String,
    pub settled_at_ms:i64,pub charged_calls:i64,pub charged_seconds:i64,pub charged_input_tokens:i64,pub charged_output_tokens:i64,
    pub reset_workspace_id:String,pub reset_operation_id:String,pub reset_receipt_id:String,
}
fn reset_terminal_event(m:&ResetTerminal)->Result<String,RepositoryError>{Ok(format!("outbox_skill_draft_reset_{}",digest(&json!([m.captured_owner,m.job_id]))?))}
fn verify_reset_terminal(tx:&Transaction<'_>,sql_owner:&str,m:&ResetTerminal,view:&owner_custody::VerifiedCustodyView<'_,'_>)->Result<(),RepositoryError>{
    if m.version!=1||view.current_owner()!=sql_owner||view.account_id()!=m.account_id||[&m.captured_owner,&m.account_id,&m.workspace_id,&m.reset_workspace_id,&m.reset_operation_id].iter().any(|v|!text(v))||!m.job_id.strip_prefix("skilldraft_").is_some_and(valid_sha256)||!valid_sha256(&m.original_sha256)||m.reset_receipt_id!=ingest_erased_receipt_id(&m.account_id,&m.reset_operation_id)? {return Err(RepositoryError::NotFound)}
    view.for_captured(tx,&m.captured_owner)?;
    if !matches!(m.disposition.as_str(),"draft"|"no_draft"|"failed"|"cancelled")||m.settled_at_ms<0||m.charged_calls<0||m.charged_calls>1||m.charged_seconds<0||m.charged_input_tokens<0||m.charged_output_tokens<0{return Err(RepositoryError::NotFound)}
    if (m.disposition=="cancelled" && m.charge_basis!="unissued") || (m.disposition!="cancelled" && !matches!(m.charge_basis.as_str(),"actual"|"reserved_bound")) {return Err(RepositoryError::NotFound)}
    if m.disposition=="cancelled" && (m.settled_at_ms!=0||m.charged_calls!=0||m.charged_seconds!=0||m.charged_input_tokens!=0||m.charged_output_tokens!=0){return Err(RepositoryError::NotFound)}
    if m.disposition!="cancelled" && (m.settled_at_ms==0||m.charged_calls!=1){return Err(RepositoryError::NotFound)}
    let row:Option<(String,String,String,String,String,Option<String>)>=tx.query_row("SELECT content_hash,operation_metadata_json,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144",params![sql_owner,m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (hash,raw,actor,actor_id,contract,source)=row.ok_or(RepositoryError::NotFound)?;let receipt=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    if hash!=sha256_hex(raw.as_bytes())||actor!="user"||actor_id!=m.captured_owner||contract!=CONTRACT_VERSION||source.is_some()||receipt.as_object().map(|v|v.len())!=Some(4)||receipt["version"]!="s06.guarded-reset.v1"||receipt["request_sha256"]!=sha256_hex(&serde_json::to_vec(&receipt["request"])? )||receipt["request"]["owner"]!=m.captured_owner||receipt["request"]["account_id"]!=m.account_id||receipt["request"]["workspace_id"]!=m.reset_workspace_id||receipt["request"]["operation_id"]!=m.reset_operation_id||receipt["result"]["complete"]!=true||receipt["result"]["categories"]["memories"]["state"]!="complete"||receipt["request"].get("selective_manifest_sha256").is_some()||!receipt["request"]["components"].as_array().is_some_and(|a|a.iter().any(|v|v=="memories")){return Err(RepositoryError::NotFound)}
    let paired:Option<(String,i64)>=tx.query_row("SELECT payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind='s06_guarded_reset'",params![sql_owner,format!("outbox_{}_s06_guarded_reset",m.reset_receipt_id),m.reset_receipt_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if paired.is_none_or(|(payload,revision)|payload!=raw||revision!=0){return Err(RepositoryError::NotFound)}Ok(())
}
/// Verify one explicitly indexed reset fact and its exact live backing.
/// The caller supplies a verified custody view and bounds index traversal. The
/// boolean reports a matching live row, never source or execution authority.
pub(crate) fn verified_reset_window_fact_in(
    tx:&Transaction<'_>,owner:&str,account:&str,event:&str,
    view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,
)->Result<(ResetTerminal,bool),RepositoryError> {
    if !text(event) || view.current_owner()!=owner || view.account_id()!=account {return Err(RepositoryError::NotFound)}
    let row:Option<(String,String,i64)>=tx.query_row("SELECT change_set_id,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND kind='skill_draft_reset_terminal' AND length(CAST(payload_json AS BLOB))<=8192",params![owner,event],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let (change,raw,revision)=row.ok_or(RepositoryError::NotFound)?;
    budget.check(raw.len().saturating_add(event.len())).map_err(RepositoryError::Validation)?;
    let marker:ResetTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if revision!=0 || marker.account_id!=account || change!=marker.reset_receipt_id || event!=reset_terminal_event(&marker)? || serde_json::to_string(&marker)?!=raw {return Err(RepositoryError::NotFound)}
    let receipt_lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 WHERE c.owner_id=?1 AND c.change_set_id=?2",params![owner,marker.reset_receipt_id,format!("outbox_{}_s06_guarded_reset",marker.reset_receipt_id)],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let (receipt_len,outbox_len)=receipt_lengths.ok_or(RepositoryError::NotFound)?;
    if !(0..=262_144).contains(&receipt_len) || !(0..=262_144).contains(&outbox_len) {return Err(RepositoryError::NotFound)}
    budget.check((receipt_len as usize).saturating_add(outbox_len as usize)).map_err(RepositoryError::Validation)?;
    verify_reset_terminal(tx,owner,&marker,view)?;
    let charge=M5ChargeRef {job_id:&marker.job_id,original_sha256:&marker.original_sha256,disposition:&marker.disposition,charge_basis:&marker.charge_basis,
        settled_at_ms:marker.settled_at_ms,charged_calls:marker.charged_calls,charged_seconds:marker.charged_seconds,
        charged_input_tokens:marker.charged_input_tokens,charged_output_tokens:marker.charged_output_tokens};
    let backed=m5_live_backed_in(tx,owner,&charge,budget)?;
    Ok((marker,backed))
}

struct M5ChargeRef<'a> {job_id:&'a str,original_sha256:&'a str,disposition:&'a str,charge_basis:&'a str,settled_at_ms:i64,
    charged_calls:i64,charged_seconds:i64,charged_input_tokens:i64,charged_output_tokens:i64}

/// Exact per-job backing only. The surrounding owner verifies the retained
/// reset/purge receipt and account custody; the current-window live scan also
/// refuses orphan reservations before these facts can contribute to totals.
fn m5_live_backed_in(tx:&Transaction<'_>,owner:&str,charge:&M5ChargeRef<'_>,budget:&mut ResetFenceBudget)->Result<bool,RepositoryError> {
    let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_attempts a CROSS JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.attempt_id=a.attempt_id AND q.job_id=a.job_id AND q.lease_epoch=a.lease_epoch WHERE a.owner_id=?1 AND a.job_id=?2",params![owner,charge.job_id],|r|r.get(0))?;
    if count>1 {return Err(RepositoryError::NotFound)}
    if count==0 {
        let live:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2)",params![owner,charge.job_id],|r|r.get(0))?;
        if live {return Err(RepositoryError::NotFound)}
        return Ok(false);
    }
    let row:Option<(String,Option<i64>,i64,i64,i64,i64,Option<String>,Option<String>,String,String,i64,Option<String>)>=tx.query_row("SELECT q.state,q.settled_at_ms,q.charged_calls,q.charged_seconds,q.charged_input_tokens,q.charged_output_tokens,j.input_hash,j.state,a.state,a.attempt_id,a.lease_epoch,j.result_json FROM fm_v2_attempts a CROSS JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch LEFT JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id WHERE a.owner_id=?1 AND a.job_id=?2 AND COALESCE(length(CAST(j.result_json AS BLOB)),0)<=65536",params![owner,charge.job_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?,r.get(10)?,r.get(11)?))).optional()?;
    let (state,settled,calls,seconds,input,output,hash,job_state,attempt_state,attempt,epoch,result)=row.ok_or(RepositoryError::NotFound)?;
    budget.check(result.as_ref().map_or(0,String::len)).map_err(RepositoryError::Validation)?;
    if attempt!=format!("skillattempt_{}",sha256_hex(charge.job_id.as_bytes())) || epoch!=1 {return Err(RepositoryError::NotFound)}
    if hash.as_deref()!=Some(charge.original_sha256) {return Err(RepositoryError::NotFound)}
    if charge.disposition=="cancelled" {if state!="revoked" || job_state.as_deref()!=Some("cancelled") || attempt_state!="fenced" {return Err(RepositoryError::NotFound)}}
    else if (charge.disposition=="failed" && (job_state.as_deref()!=Some("failed_terminal") || attempt_state!="failed_terminal"))
        || (charge.disposition!="failed" && (job_state.as_deref()!=Some("succeeded") || attempt_state!="succeeded"))
        || state!="settled" || settled!=Some(charge.settled_at_ms) || calls!=charge.charged_calls || seconds!=charge.charged_seconds || input!=charge.charged_input_tokens || output!=charge.charged_output_tokens {return Err(RepositoryError::NotFound)}
    let kind=if charge.disposition=="cancelled" {"revoke"} else {"terminal"};
    let id=format!("cs_skill_{}_{}",kind,sha256_hex(charge.job_id.as_bytes()));
    let lengths:(i64,i64)=tx.query_row("SELECT COALESCE((SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2),0),COALESCE((SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3),0)",params![owner,id,format!("outbox_{id}_skill_draft_{kind}")],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if !(1..=65_536).contains(&lengths.0) || !(1..=65_536).contains(&lengths.1) {return Err(RepositoryError::NotFound)}
    budget.check((lengths.0 as usize).saturating_add(lengths.1 as usize)).map_err(RepositoryError::Validation)?;
    let receipt=read_job_receipt(tx,owner,charge.job_id,kind)?.ok_or(RepositoryError::NotFound)?;
    if charge.disposition=="cancelled" {
        if charge.charge_basis!="unissued" || receipt!=json!({"version":1,"state":"cancelled","job_id":charge.job_id,"attempt_id":attempt,"lease_epoch":epoch}) {return Err(RepositoryError::NotFound)}
    } else if receipt.as_object().map(|v|v.len())!=Some(8) || receipt["version"]!=1
        || receipt["state"]!=charge.disposition || receipt["charge_basis"]!=charge.charge_basis || receipt["job_id"]!=charge.job_id
        || receipt["attempt_id"]!=attempt || receipt["lease_epoch"]!=epoch
        || !receipt["request_sha256"].as_str().is_some_and(valid_sha256)
        || (charge.disposition=="draft")!=receipt["draft_sha256"].as_str().is_some_and(valid_sha256)
        || (charge.disposition!="draft" && !receipt["draft_sha256"].is_null())
        || result.as_deref().and_then(|v|crate::field_authority::parse_unique_json(v).ok())!=Some(receipt) {return Err(RepositoryError::NotFound)}
    Ok(true)
}

/// The caller must already have verified the exact account-scoped purge
/// receipt/index; this helper only checks whether its charge is live-backed.
pub(crate) fn verified_purge_live_backing_in(
    tx:&Transaction<'_>,owner:&str,account:&str,item:&crate::store::sqlite::SkillUsage,
    view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,
)->Result<bool,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account || !valid_sha256(&item.operation_sha256)
        || !valid_sha256(&item.original_sha256) || !matches!(item.disposition.as_str(),"draft"|"no_draft"|"failed"|"cancelled") {return Err(RepositoryError::NotFound)}
    let job=format!("skilldraft_{}",item.operation_sha256);
    let charge=M5ChargeRef {job_id:&job,original_sha256:&item.original_sha256,disposition:&item.disposition,charge_basis:&item.charge_basis,
        settled_at_ms:item.settled_at_ms,charged_calls:item.charged_calls,charged_seconds:item.charged_seconds,
        charged_input_tokens:item.charged_input_tokens,charged_output_tokens:item.charged_output_tokens};
    m5_live_backed_in(tx,owner,&charge,budget)
}

/// Verify one live settled M5 charge under current account custody. This reads
/// only the named job, its exact paired facts and ledger row; it never hydrates
/// source text or scans the owner's historical jobs.
pub(crate) fn verified_live_charge_in(
    tx:&Transaction<'_>,owner:&str,account:&str,row:&super::s05_budget::LiveReservation,
    view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,
)->Result<super::s05_budget::LiveChargeProof,RepositoryError> {
    if view.current_owner()!=owner || view.account_id()!=account || row.job_kind!="skill_draft" || row.state!="settled"
        || row.lease_epoch!=1 || row.attempt_id!=format!("skillattempt_{}",sha256_hex(row.job_id.as_bytes()))
        || row.settled_at_ms.is_none() || row.charged.calls!=1 {return Err(RepositoryError::NotFound)}
    let job:Option<(String,String,String,Option<String>,Option<String>,String)>=tx.query_row(
        "SELECT j.state,j.workspace_key,j.input_hash,j.result_json,m.selected_json,a.state FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=?3 AND a.lease_epoch=?4 LEFT JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind='skill_draft' AND COALESCE(length(CAST(j.result_json AS BLOB)),0)<=8192 AND COALESCE(length(CAST(m.selected_json AS BLOB)),0)<=65536",
        params![owner,row.job_id,row.attempt_id,row.lease_epoch],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (job_state,workspace,input,result,manifest,attempt_state)=job.ok_or(RepositoryError::NotFound)?;
    if input!=row.original_sha256 {return Err(RepositoryError::NotFound)}
    for kind in ["prepare","terminal","erased"] {
        let id=format!("cs_skill_{}_{}",kind,sha256_hex(row.job_id.as_bytes()));
        let lengths:(i64,i64)=tx.query_row("SELECT COALESCE((SELECT length(CAST(operation_metadata_json AS BLOB)) FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2),0),COALESCE((SELECT length(CAST(payload_json AS BLOB)) FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3),0)",params![owner,id,format!("outbox_{id}_skill_draft_{kind}")],|r|Ok((r.get(0)?,r.get(1)?)))?;
        if !(0..=65_536).contains(&lengths.0) || !(0..=65_536).contains(&lengths.1) {return Err(RepositoryError::NotFound)}
        budget.check((lengths.0 as usize).saturating_add(lengths.1 as usize)).map_err(RepositoryError::Validation)?;
    }
    budget.check(workspace.len().saturating_add(result.as_ref().map_or(0,String::len)).saturating_add(manifest.as_ref().map_or(0,String::len))).map_err(RepositoryError::Validation)?;
    let captured=if let Some(raw)=manifest {
        let original:Original=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        validate_original_identity(&original)?;
        if original.operation_id!=row.job_id || original.account_id!=account || original.workspace_id!=workspace || original_raw(&original)?!=(input.clone(),raw) {return Err(RepositoryError::NotFound)}
        original.owner
    } else {
        let erased=read_job_receipt(tx,owner,&row.job_id,"erased")?.ok_or(RepositoryError::NotFound)?;
        let captured=erased["owner"].as_str().ok_or(RepositoryError::NotFound)?.to_owned();
        if erased!=json!({"version":1,"account_id":account,"owner":captured,"job_id":row.job_id,"workspace_id":workspace,"original_sha256":input}) {return Err(RepositoryError::NotFound)}
        captured
    };
    view.for_captured(tx,&captured)?;
    let prepared=read_job_receipt(tx,owner,&row.job_id,"prepare")?.ok_or(RepositoryError::NotFound)?;
    if prepared!=json!({"version":1,"original_sha256":input,"job_id":row.job_id,"account_id":account}) {return Err(RepositoryError::NotFound)}
    let terminal=read_job_receipt(tx,owner,&row.job_id,"terminal")?.ok_or(RepositoryError::NotFound)?;
    if result.as_deref().and_then(|v|crate::field_authority::parse_unique_json(v).ok())!=Some(terminal.clone()) {return Err(RepositoryError::NotFound)}
    let disposition=terminal["state"].as_str().ok_or(RepositoryError::NotFound)?;
    let basis=terminal["charge_basis"].as_str().ok_or(RepositoryError::NotFound)?;
    let expected_state=match disposition {"draft"|"no_draft"=>"succeeded","failed"=>"failed_terminal",_=>return Err(RepositoryError::NotFound)};
    if job_state!=expected_state || attempt_state!=expected_state || terminal.as_object().map(|v|v.len())!=Some(8)
        || terminal["version"]!=1 || terminal["job_id"]!=row.job_id || terminal["attempt_id"]!=row.attempt_id
        || terminal["lease_epoch"]!=row.lease_epoch || !terminal["request_sha256"].as_str().is_some_and(valid_sha256)
        || (disposition=="draft")!=terminal["draft_sha256"].as_str().is_some_and(valid_sha256)
        || (disposition!="draft" && !terminal["draft_sha256"].is_null())
        || !matches!(basis,"actual"|"reserved_bound") {return Err(RepositoryError::NotFound)}
    Ok(super::s05_budget::LiveChargeProof {basis:if basis=="actual" {"reported"}else{"reserved_bound"}.into(),reserve_original_sha256:None,reserve_result_sha256:None})
}

/// Read every original before the existing guarded reset deletes job rows.
/// The caller holds the reset transaction and writes the paired terminals only
/// after the original guarded-reset receipt has been committed in that same tx.
pub(crate) fn collect_reset_terminals(tx:&Transaction<'_>,owner:&str,account:&str,reset_workspace:&str,reset_operation:&str,budget:&mut ResetFenceBudget)->Result<Vec<ResetTerminal>,RepositoryError>{
    if !skill_profile_present(tx)? {return Ok(Vec::new())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let view=owner_custody::verified_or_direct_custody_view(tx,&database,account,owner,owner,budget)?;
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts a LEFT JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id AND j.kind='skill_draft' WHERE a.owner_id=?1 AND a.attempt_id GLOB 'skillattempt_*' AND j.job_id IS NULL) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations q LEFT JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id AND j.kind='skill_draft' WHERE q.owner_id=?1 AND q.attempt_id GLOB 'skillattempt_*' AND j.job_id IS NULL) OR EXISTS(SELECT 1 FROM fm_v2_job_manifests m LEFT JOIN fm_v2_jobs j ON j.owner_id=m.owner_id AND j.job_id=m.job_id AND j.kind='skill_draft' WHERE m.owner_id=?1 AND m.manifest_id GLOB 'skillmanifest_*' AND j.job_id IS NULL)",[owner],|r|r.get(0))?;if orphan{return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT j.job_id,j.workspace_key,j.input_hash,j.state,j.result_json,m.selected_json FROM fm_v2_jobs j LEFT JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' WHERE j.owner_id=?1 AND j.kind='skill_draft' ORDER BY j.job_id LIMIT 10001")?;let mut rows=stmt.query([owner])?;let mut output=Vec::new();
    while let Some(row)=rows.next()? {if output.len()>=10000{return Err(RepositoryError::NotFound)}let (job,workspace,hash,state,result,raw):(String,String,String,String,Option<String>,Option<String>)=(row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?);budget.check(job.len().saturating_add(raw.as_ref().map_or(0,String::len))).map_err(RepositoryError::Validation)?;
        let captured=if let Some(raw)=raw {if raw.len()>65536||sha256_hex(raw.as_bytes())!=hash{return Err(RepositoryError::NotFound)}let original:Original=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;validate_original_identity(&original)?;if original.operation_id!=job||original.account_id!=account||original.workspace_id!=workspace||original_raw(&original)?!=(hash.clone(),raw){return Err(RepositoryError::NotFound)}original.owner} else {let erased=read_job_receipt(tx,owner,&job,"erased")?.ok_or(RepositoryError::NotFound)?;let captured=erased["owner"].as_str().ok_or(RepositoryError::NotFound)?.to_owned();if erased!=json!({"version":1,"account_id":account,"owner":captured,"job_id":job,"workspace_id":workspace,"original_sha256":hash}){return Err(RepositoryError::NotFound)}captured};
        view.for_captured(tx,&captured)?;let prepare=read_job_receipt(tx,owner,&job,"prepare")?.ok_or(RepositoryError::NotFound)?;if prepare!=json!({"version":1,"original_sha256":hash,"job_id":job,"account_id":account}){return Err(RepositoryError::NotFound)}
        let counts:(i64,i64)=tx.query_row("SELECT (SELECT count(*) FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2),(SELECT count(*) FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2)",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?)))?;
        if !matches!((state.as_str(),counts),("queued",(0,0))|("cancelled",(0,0))|("cancelled",(1,1))|("succeeded",(1,1))|("failed_terminal",(1,1))){return Err(RepositoryError::NotFound)}
        let (disposition,charge_basis,settled,calls,seconds,input,output_tokens)=match state.as_str(){
            "queued"=>{let empty:bool=tx.query_row("SELECT NOT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2)",params![owner,job],|r|r.get(0))?;if !empty||read_job_receipt(tx,owner,&job,"dispatch")?.is_some(){return Err(RepositoryError::NotFound)}("cancelled".to_owned(),"unissued".to_owned(),0,0,0,0,0)},
            "cancelled"=>{let cancel=read_job_receipt(tx,owner,&job,"prepared_cancel")?;let revoke=read_job_receipt(tx,owner,&job,"revoke")?;if cancel.is_some()==revoke.is_some(){return Err(RepositoryError::NotFound)}if let Some(v)=cancel {let empty:bool=tx.query_row("SELECT NOT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2)",params![owner,job],|r|r.get(0))?;if !empty||v!=json!({"version":1,"state":"cancelled","job_id":job}){return Err(RepositoryError::NotFound)}}if let Some(v)=revoke {let attempt=v["attempt_id"].as_str().ok_or(RepositoryError::NotFound)?;let epoch=v["lease_epoch"].as_i64().ok_or(RepositoryError::NotFound)?;let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND q.job_id=?2 AND q.attempt_id=?3 AND q.lease_epoch=?4 AND q.state='revoked' AND a.state='fenced')",params![owner,job,attempt,epoch],|r|r.get(0))?;if !valid||v!=json!({"version":1,"state":"cancelled","job_id":job,"attempt_id":attempt,"lease_epoch":epoch}){return Err(RepositoryError::NotFound)}}("cancelled".to_owned(),"unissued".to_owned(),0,0,0,0,0)},
            "succeeded"|"failed_terminal"=>{let terminal=read_job_receipt(tx,owner,&job,"terminal")?.ok_or(RepositoryError::NotFound)?;if result.as_deref().and_then(|v|crate::field_authority::parse_unique_json(v).ok())!=Some(terminal.clone()){return Err(RepositoryError::NotFound)}let disposition=terminal["state"].as_str().ok_or(RepositoryError::NotFound)?.to_owned();if !matches!((state.as_str(),disposition.as_str()),("succeeded","draft"|"no_draft")|("failed_terminal","failed")){return Err(RepositoryError::NotFound)}let attempt=terminal["attempt_id"].as_str().ok_or(RepositoryError::NotFound)?;let epoch=terminal["lease_epoch"].as_i64().ok_or(RepositoryError::NotFound)?;let expected=if state=="succeeded" {"succeeded"}else{"failed_terminal"};let q:Option<(i64,i64,i64,i64,i64)>=tx.query_row("SELECT q.settled_at_ms,q.charged_calls,q.charged_seconds,q.charged_input_tokens,q.charged_output_tokens FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND q.job_id=?2 AND q.attempt_id=?3 AND q.lease_epoch=?4 AND q.state='settled' AND a.state=?5",params![owner,job,attempt,epoch,expected],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;let (at,calls,seconds,input,output_tokens)=q.ok_or(RepositoryError::NotFound)?;let basis=terminal["charge_basis"].as_str().filter(|v|matches!(*v,"actual"|"reserved_bound")).ok_or(RepositoryError::NotFound)?.to_owned();(disposition,basis,at,calls,seconds,input,output_tokens)},
            _=>return Err(RepositoryError::Validation("skill draft issue is unresolved".into())),
        };
        output.push(ResetTerminal{version:1,captured_owner:captured,account_id:account.into(),workspace_id:workspace,job_id:job,original_sha256:hash,disposition,charge_basis,settled_at_ms:settled,charged_calls:calls,charged_seconds:seconds,charged_input_tokens:input,charged_output_tokens:output_tokens,reset_workspace_id:reset_workspace.into(),reset_operation_id:reset_operation.into(),reset_receipt_id:ingest_erased_receipt_id(account,reset_operation)?});
    }
    drop(rows);drop(stmt);drop(view);Ok(output)
}
pub(crate) fn write_reset_terminals(tx:&Transaction<'_>,owner:&str,markers:&[ResetTerminal],budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;for m in markers {let view=owner_custody::verified_or_direct_custody_view(tx,&database,&m.account_id,owner,&m.captured_owner,budget)?;verify_reset_terminal(tx,owner,m,&view)?;drop(view);let event=reset_terminal_event(m)?;let raw=serde_json::to_string(m)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;let now=chrono::Utc::now().to_rfc3339();tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'skill_draft_reset_terminal',?4,0,?5)",params![owner,event,m.reset_receipt_id,raw,now])?;}Ok(())
}
pub(crate) fn retained_accounting_bounded(tx:&Transaction<'_>,owner:&str,account:&str,budget:&mut ResetFenceBudget)->Result<Vec<ResetTerminal>,RepositoryError>{
    if !skill_profile_present(tx)? {return Ok(Vec::new())}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
    let view=owner_custody::verified_or_direct_custody_view(tx,&database,account,owner,owner,budget)?;
    let mut stmt=tx.prepare("SELECT event_id,change_set_id,payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=?1 AND kind='skill_draft_reset_terminal' ORDER BY event_id LIMIT 10001")?;let mut rows=stmt.query([owner])?;let mut result=Vec::new();let mut seen=BTreeSet::new();
    while let Some(row)=rows.next()? {if result.len()>=10000{return Err(RepositoryError::NotFound)}let (event,change,raw,revision):(String,String,String,i64)=(row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?);budget.check(raw.len().saturating_add(event.len())).map_err(RepositoryError::Validation)?;if raw.len()>8192||revision!=0{return Err(RepositoryError::NotFound)}let m:ResetTerminal=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;if event!=reset_terminal_event(&m)?||change!=m.reset_receipt_id||!seen.insert(m.job_id.clone()){return Err(RepositoryError::NotFound)}verify_reset_terminal(tx,owner,&m,&view)?;result.push(m)}
    drop(rows);drop(stmt);drop(view);Ok(result)
}
/// Lifecycle-only complete retained M5 inventory. The bounded caller uses it
/// for purge/transfer fingerprints, never for the rolling admission window.
pub(crate) fn unbacked_reset_terminals_bounded(
    tx:&Transaction<'_>,owner:&str,account:&str,budget:&mut ResetFenceBudget,
)->Result<Vec<ResetTerminal>,RepositoryError> {
    let markers=retained_accounting_bounded(tx,owner,account,budget)?;
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
    let view=owner_custody::verified_or_direct_custody_view(tx,&database,account,owner,owner,budget)?;
    let mut output=Vec::new();
    for marker in markers {
        let event=reset_terminal_event(&marker)?;
        let (verified,backed)=verified_reset_window_fact_in(tx,owner,account,&event,&view,budget)?;
        if serde_json::to_value(&verified)?!=serde_json::to_value(&marker)? {return Err(RepositoryError::NotFound)}
        if !backed {output.push(verified)}
    }
    Ok(output)
}

fn retained_accounting(tx:&Transaction<'_>,owner:&str,account:&str)->Result<Vec<ResetTerminal>,RepositoryError>{let mut budget=ResetFenceBudget::install(tx);let result=retained_accounting_bounded(tx,owner,account,&mut budget);drop(budget);result}
// A retained reset fact contributes only when its matching live reservation is
// gone. A conflicting coexisting row refuses rather than double-charging.
pub(crate) fn unbacked_reset_terminals(tx:&Transaction<'_>,owner:&str,account:&str)->Result<Vec<ResetTerminal>,RepositoryError>{
    let mut result=Vec::new();for marker in retained_accounting(tx,owner,account)? {
        let row:Option<(String,Option<i64>,i64,i64,i64,i64,Option<String>)>=tx.query_row("SELECT q.state,q.settled_at_ms,q.charged_calls,q.charged_seconds,q.charged_input_tokens,q.charged_output_tokens,j.input_hash FROM fm_v2_s05_reservations q LEFT JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id WHERE q.owner_id=?1 AND q.job_id=?2",params![owner,marker.job_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
        if let Some((state,settled,calls,seconds,input,output,hash))=row {
            if hash.as_deref()!=Some(marker.original_sha256.as_str()){return Err(RepositoryError::NotFound)}
            if marker.disposition=="cancelled" {if state!="revoked"{return Err(RepositoryError::NotFound)}}
            else if state!="settled"||settled!=Some(marker.settled_at_ms)||calls!=marker.charged_calls||seconds!=marker.charged_seconds||input!=marker.charged_input_tokens||output!=marker.charged_output_tokens{return Err(RepositoryError::NotFound)}
        } else {result.push(marker)}
    }Ok(result)
}
fn verified_purge_usage(tx:&Transaction<'_>,store:&crate::store::sqlite::SqliteStore,owner:&str,account:&str)->Result<Vec<crate::store::sqlite::SkillUsage>,RepositoryError>{
    let mut budget=ResetFenceBudget::install(tx);let usage=store.skill_purge_usage_in(tx,account,&mut budget).map_err(RepositoryError::Validation)?;drop(budget);let mut result=Vec::new();
    for item in usage {let job=format!("skilldraft_{}",item.operation_sha256);
        let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2",params![owner,job],|r|r.get(0))?;if count>1{return Err(RepositoryError::NotFound)}
        if count==1 {let row:Option<(String,Option<i64>,i64,i64,i64,i64,Option<String>)>=tx.query_row("SELECT q.state,q.settled_at_ms,q.charged_calls,q.charged_seconds,q.charged_input_tokens,q.charged_output_tokens,j.input_hash FROM fm_v2_s05_reservations q LEFT JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id WHERE q.owner_id=?1 AND q.job_id=?2",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;let (state,at,calls,seconds,input,output,hash)=row.ok_or(RepositoryError::NotFound)?;if hash.as_deref()!=Some(item.original_sha256.as_str()){return Err(RepositoryError::NotFound)}if item.disposition=="cancelled" {if state!="revoked"{return Err(RepositoryError::NotFound)}}else if state!="settled"||at!=Some(item.settled_at_ms)||calls!=item.charged_calls||seconds!=item.charged_seconds||input!=item.charged_input_tokens||output!=item.charged_output_tokens{return Err(RepositoryError::NotFound)}}else {let job_exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2)",params![owner,job],|r|r.get(0))?;if job_exists{return Err(RepositoryError::NotFound)}result.push(item)}
    }Ok(result)
}
fn cadence(tx:&Transaction<'_>,o:&Original,interval:u64,purged:&[crate::store::sqlite::SkillUsage])->Result<(),RepositoryError>{
    let broken:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q LEFT JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id LEFT JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND (j.job_id IS NULL OR a.attempt_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_attempts a WHERE a.owner_id=?1 AND a.job_id GLOB 'skilldraft_*' AND NOT EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=a.owner_id AND j.job_id=a.job_id)) OR EXISTS(SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id WHERE j.owner_id=?1 AND j.kind='skill_draft' AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q WHERE q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch))",params![o.owner],|r|r.get(0))?;if broken{return Err(RepositoryError::NotFound)}
    let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;let mut budget=ResetFenceBudget::install(tx);let view=owner_custody::verified_or_direct_custody_view(tx,&database,&o.account_id,&o.owner,&o.owner,&mut budget)?;
    let now=chrono::Utc::now().timestamp_millis();let mut latest=None::<i64>;let mut count=0usize;
    let mut stmt=tx.prepare("SELECT j.job_id,j.state,j.input_hash,j.result_json,m.selected_json,j.workspace_key FROM fm_v2_jobs j LEFT JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' WHERE j.owner_id=?1 AND j.kind='skill_draft' AND j.job_id<>?2 ORDER BY j.job_id LIMIT 100001")?;
    let mut rows=stmt.query(params![o.owner,o.operation_id])?;while let Some(row)=rows.next()? {count+=1;if count>100000{return Err(RepositoryError::NotFound)}let job:String=row.get(0)?;let state:String=row.get(1)?;let input:String=row.get(2)?;let result:Option<String>=row.get(3)?;let raw:Option<String>=row.get(4)?;let job_workspace:String=row.get(5)?;
        if let Some(raw)=raw {if raw.len()>65536||sha256_hex(raw.as_bytes())!=input{return Err(RepositoryError::NotFound)}let prior:Original=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;validate_original_identity(&prior)?;view.for_captured(tx,&prior.owner)?;if prior.operation_id!=job||prior.account_id!=o.account_id||prior.workspace_id!=job_workspace{return Err(RepositoryError::NotFound)}} else {
            let erased=read_job_receipt(tx,&o.owner,&job,"erased")?.ok_or(RepositoryError::NotFound)?;
            let captured=erased["owner"].as_str().ok_or(RepositoryError::NotFound)?;view.for_captured(tx,captured)?;
            if erased!=json!({"version":1,"account_id":o.account_id,"owner":captured,"job_id":job,"workspace_id":job_workspace,"original_sha256":input}) {return Err(RepositoryError::NotFound)}
        }
        let prepare=read_job_receipt(tx,&o.owner,&job,"prepare")?.ok_or(RepositoryError::NotFound)?;if prepare!=json!({"version":1,"original_sha256":input,"job_id":job,"account_id":o.account_id}){return Err(RepositoryError::NotFound)}
        if state=="cancelled" {
            if let Some(cancel)=read_job_receipt(tx,&o.owner,&job,"prepared_cancel")? {let no_attempt:bool=tx.query_row("SELECT NOT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) AND NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2)",params![o.owner,job],|r|r.get(0))?;if cancel!=json!({"version":1,"state":"cancelled","job_id":job})||!no_attempt{return Err(RepositoryError::NotFound)}continue}
            let revoke=read_job_receipt(tx,&o.owner,&job,"revoke")?.ok_or(RepositoryError::NotFound)?;let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND q.job_id=?2 AND q.attempt_id=?3 AND q.lease_epoch=?4 AND q.state='revoked' AND a.state='fenced')",params![o.owner,job,revoke["attempt_id"].as_str().ok_or(RepositoryError::NotFound)?,revoke["lease_epoch"].as_i64().ok_or(RepositoryError::NotFound)?],|r|r.get(0))?;if revoke["version"]!=1||revoke["state"]!="cancelled"||revoke["job_id"]!=job||!valid{return Err(RepositoryError::NotFound)}continue
        }
        if !matches!(state.as_str(),"succeeded"|"failed_terminal"){return Err(RepositoryError::NotFound)}let result=result.ok_or(RepositoryError::NotFound)?;let outcome:Value=crate::field_authority::parse_unique_json(&result).map_err(RepositoryError::Validation)?;let terminal=read_job_receipt(tx,&o.owner,&job,"terminal")?.ok_or(RepositoryError::NotFound)?;if outcome!=terminal||!matches!(terminal["charge_basis"].as_str(),Some("actual"|"reserved_bound")){return Err(RepositoryError::NotFound)}
        let settled:Option<(String,i64)>=tx.query_row("SELECT q.state,q.settled_at_ms FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND q.job_id=?2 AND a.attempt_id=?3 AND a.lease_epoch=?4",params![o.owner,job,outcome["attempt_id"].as_str().ok_or(RepositoryError::NotFound)?,outcome["lease_epoch"].as_i64().ok_or(RepositoryError::NotFound)?],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;let (q,when)=settled.ok_or(RepositoryError::NotFound)?;if q!="settled"||when>now||when<0{return Err(RepositoryError::NotFound)}
        if outcome["state"]=="draft" {latest=Some(latest.map_or(when,|last|last.max(when)))}
    }
    drop(rows);drop(stmt);drop(view);drop(budget);
    for marker in unbacked_reset_terminals(tx,&o.owner,&o.account_id)? {if marker.settled_at_ms>now{return Err(RepositoryError::NotFound)}if marker.disposition=="draft" {latest=Some(latest.map_or(marker.settled_at_ms,|last|last.max(marker.settled_at_ms)))}}
    for item in purged {if item.settled_at_ms>now{return Err(RepositoryError::NotFound)}if item.disposition=="draft" {latest=Some(latest.map_or(item.settled_at_ms,|last|last.max(item.settled_at_ms)))}}
    if let Some(last)=latest {let delay=interval.checked_mul(86_400_000).and_then(|x|i64::try_from(x).ok()).ok_or(RepositoryError::NotFound)?;if now.checked_sub(last).is_none_or(|elapsed|elapsed<delay){return Err(RepositoryError::NotFound)}}Ok(())
}
impl<'a> V2Repository<'a> {
    pub(crate) fn skill_prepare(&mut self,store:&crate::store::sqlite::SqliteStore,o:&Original)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_guarded_epoch(&tx)?;verify_shape(&tx,o)?;let (hash,raw)=original_raw(o)?;
        if retained_accounting(&tx,&o.owner,&o.account_id)?.iter().any(|m|m.job_id==o.operation_id)||verified_purge_usage(&tx,store,&o.owner,&o.account_id)?.iter().any(|m|format!("skilldraft_{}",m.operation_sha256)==o.operation_id){return Err(RepositoryError::NotFound)}
        if read_receipt(&tx,o,"prepare")?.is_some(){verify_original(&tx,o)?;let state:String=tx.query_row("SELECT state FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![o.owner,o.operation_id],|r|r.get(0))?;tx.commit()?;return Ok(json!({"version":1,"state":state,"job_id":o.operation_id}))}
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND (job_id=?2 OR idempotency_key=?2)) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?3) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?4)",params![o.owner,o.operation_id,receipt_id("prepare",o),format!("outbox_{}_skill_draft_prepare",receipt_id("prepare",o))],|r|r.get(0))?;if collision{return Err(RepositoryError::NotFound)}
        let (_,_,_,_,_,_,owner_max,global_max,_,_)=policy(&tx,o)?;let (own,global):(i64,i64)=tx.query_row("SELECT (SELECT count(*) FROM fm_v2_jobs WHERE owner_id=?1 AND state IN ('queued','retry_wait')),(SELECT count(*) FROM fm_v2_jobs WHERE state IN ('queued','retry_wait'))",params![o.owner],|r|Ok((r.get(0)?,r.get(1)?)))?;if own>=i64::from(owner_max)||global>=i64::from(global_max){return Err(RepositoryError::NotFound)}
        let now=chrono::Utc::now().to_rfc3339();tx.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,current_attempt_id,attempt_count,input_hash,result_json,created_at,updated_at) VALUES(?1,?2,'skill_draft',?3,'',?2,'queued',NULL,0,?4,NULL,?5,?5)",params![o.owner,o.operation_id,o.workspace_id,hash,now])?;
        tx.execute("INSERT INTO fm_v2_job_manifests(owner_id,manifest_id,job_id,phase,selected_json,config_hash,model_hash,tool_hash,input_hash,content_hash,sealed_at) VALUES(?1,?2,?3,'selection',?4,?5,?6,?7,?8,?8,?9)",params![o.owner,format!("skillmanifest_{}",sha256_hex(o.operation_id.as_bytes())),o.operation_id,raw,o.policy_fingerprint,o.assignment_sha256,sha256_hex(b"skill_draft.v1"),hash,now])?;
        write_receipt(&tx,o,"prepare",&json!({"version":1,"original_sha256":hash,"job_id":o.operation_id,"account_id":o.account_id}))?;tx.commit()?;Ok(json!({"version":1,"state":"queued","job_id":o.operation_id}))
    }
    pub(crate) fn skill_reserve(&mut self,store:&crate::store::sqlite::SqliteStore,r:&Reserve)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_guarded_epoch(&tx)?;
        // No profile may reserve against the incomplete live-only total.
        if !super::s05_budget::shared_admission_ready() {return Err(RepositoryError::Validation("shared quota owner unavailable".into()));}let o=&r.original;verify_shape(&tx,o)?;verify_original(&tx,o)?;let q=&r.reservation;
        if r.version!=1||q.version!=1||q.owner!=o.owner||q.job_id!=o.operation_id||!text(&q.worker_id)||!text(&q.coverage_generation)||!text(&q.clock_generation)||q.now_tick<0||q.target_fingerprint!=o.target_fingerprint||q.policy_fingerprint!=o.policy_fingerprint{return Err(RepositoryError::NotFound)}
        let (quiet,calls,daily_seconds,tokens,job_seconds,_,_,_,_,interval)=policy(&tx,o)?;if q.quiet_seconds!=quiet||q.job_seconds!=job_seconds||q.daily_seconds!=daily_seconds{return Err(RepositoryError::NotFound)}
        let request_sha=digest(&serde_json::to_value(r)?)?;if let Some(prior)=read_receipt(&tx,o,"reserve")? {
            store.s05_quota_guarded_schema_in(&tx).map_err(RepositoryError::Validation)?;
            let mut replay_budget=super::ResetFenceBudget::install(&tx);
            let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0))?;
            let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&o.account_id,&o.owner,&o.owner,&mut replay_budget)?;
            crate::store::sqlite::SqliteStore::s05_quota_cutoff_in(&tx,&o.owner,&o.account_id,chrono::Utc::now().timestamp_millis(),&mut replay_budget).map_err(RepositoryError::Validation)?;
            drop(view);drop(replay_budget);
            if prior["request_sha256"]!=request_sha{return Err(RepositoryError::NotFound)}let attempt=Attempt{version:1,reservation_original:r.clone(),job_id:o.operation_id.clone(),attempt_id:format!("skillattempt_{}",sha256_hex(o.operation_id.as_bytes())),lease_epoch:1};reservation_fact(&tx,&attempt)?;let still_reserved:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.attempt_id=q.attempt_id JOIN fm_v2_jobs j ON j.owner_id=q.owner_id AND j.job_id=q.job_id WHERE q.owner_id=?1 AND q.job_id=?2 AND q.attempt_id=?3 AND q.state='reserved' AND a.state='leased' AND j.state='active' AND j.current_attempt_id=q.attempt_id)",params![o.owner,o.operation_id,attempt.attempt_id],|r|r.get(0))?;if !still_reserved{return Err(RepositoryError::NotFound)}let mut out=prior;out["state"]=json!("reserved");out["reserve_receipt_id"]=json!(receipt_id("reserve",o));tx.commit()?;return Ok(out)}
        let job:Option<(String,i64)>=tx.query_row("SELECT state,attempt_count FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind='skill_draft'",params![o.owner,o.operation_id],|x|Ok((x.get(0)?,x.get(1)?))).optional()?;if job!=Some(("queued".into(),0)){return Err(RepositoryError::NotFound)}let purged=verified_purge_usage(&tx,store,&o.owner,&o.account_id)?;if purged.iter().any(|v|format!("skilldraft_{}",v.operation_sha256)==o.operation_id){return Err(RepositoryError::NotFound)}cadence(&tx,o,interval,&purged)?;
        let occupancy:Option<(String,String,i64,i64,i64)>=tx.query_row("SELECT coverage_generation,clock_generation,verified,foreground_epoch,last_quiet_tick FROM fm_v2_s05_occupancy WHERE singleton=1",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;let (coverage,clock,verified,foreground,quiet_tick)=occupancy.ok_or(RepositoryError::NotFound)?;
        if verified!=1||coverage!=q.coverage_generation||clock!=q.clock_generation||q.now_tick<quiet_tick||q.now_tick-quiet_tick<i64::from(quiet)*1000{return Err(RepositoryError::NotFound)}
        let busy:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown')) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE state IN ('reserved','dispatched','uncertain'))",[],|r|r.get(0))?;if busy{return Err(RepositoryError::NotFound)}
        let now=chrono::Utc::now();let ms=now.timestamp_millis();
        store.s05_quota_guarded_schema_in(&tx).map_err(RepositoryError::Validation)?;
        let mut budget=super::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let view=super::owner_custody::verified_or_direct_custody_view(&tx,&database,&o.account_id,&o.owner,&o.owner,&mut budget)?;
        let used=super::s05_budget::verified_window_totals_in(&tx,&o.owner,&o.account_id,ms,&view,&mut budget)?;
        let used_tokens=used.token_total()?;
        drop(view);drop(budget);
        if used.calls.checked_add(1).is_none_or(|v|v>i64::from(calls)) || used.seconds.checked_add(i64::from(job_seconds)).is_none_or(|v|v>i64::from(daily_seconds)) || used_tokens.checked_add(i64::from(o.max_input_tokens)).and_then(|v|v.checked_add(i64::from(o.max_output_tokens))).is_none_or(|v|v>i64::from(tokens)){return Err(RepositoryError::NotFound)}
        let attempt=format!("skillattempt_{}",sha256_hex(o.operation_id.as_bytes()));let time=now.to_rfc3339();tx.execute("INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,lease_owner,lease_expires_at,heartbeat_at,progress_watermark,error_json,created_at,updated_at) VALUES(?1,?2,?3,1,'leased',1,?4,NULL,?5,NULL,NULL,?5,?5)",params![o.owner,attempt,o.operation_id,q.worker_id,time])?;
        tx.execute("INSERT INTO fm_v2_s05_reservations VALUES(?1,?2,?3,1,?4,?5,?6,?7,'reserved',?8,NULL,1,?9,?10,?11,1,?9,?10,?11)",params![o.owner,attempt,o.operation_id,o.connection_id,coverage,clock,foreground,ms,job_seconds,o.max_input_tokens,o.max_output_tokens])?;
        if tx.execute("UPDATE fm_v2_jobs SET state='active',current_attempt_id=?3,attempt_count=1,updated_at=?4 WHERE owner_id=?1 AND job_id=?2 AND state='queued' AND attempt_count=0",params![o.owner,o.operation_id,attempt,time])?!=1{return Err(RepositoryError::NotFound)}
        let fact=json!({"version":1,"original_sha256":original_raw(o)?.0,"request_sha256":request_sha,"account_id":o.account_id,"owner":o.owner,"job_id":o.operation_id,"attempt_id":attempt,"lease_epoch":1,"foreground_epoch":foreground});write_receipt(&tx,o,"reserve",&fact)?;let mut out=fact;out["state"]=json!("reserved");out["reserve_receipt_id"]=json!(receipt_id("reserve",o));tx.commit()?;Ok(out)
    }
}
fn reservation_fact(tx:&Transaction<'_>,a:&Attempt)->Result<Value,RepositoryError>{
    if a.version!=1||a.reservation_original.version!=1||a.job_id!=a.reservation_original.original.operation_id||a.reservation_original.reservation.job_id!=a.job_id||a.lease_epoch!=1||a.attempt_id!=format!("skillattempt_{}",sha256_hex(a.job_id.as_bytes())) {return Err(RepositoryError::NotFound)}
    let o=&a.reservation_original.original;verify_original(tx,o)?;let q=&a.reservation_original.reservation;let sha=digest(&serde_json::to_value(&a.reservation_original)?)?;let fact=read_receipt(tx,o,"reserve")?.ok_or(RepositoryError::NotFound)?;
    let epoch=fact["foreground_epoch"].as_i64().filter(|n|*n>=0).ok_or(RepositoryError::NotFound)?;
    if fact!=json!({"version":1,"original_sha256":original_raw(o)?.0,"request_sha256":sha,"account_id":o.account_id,"owner":o.owner,"job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"foreground_epoch":epoch}) {return Err(RepositoryError::NotFound)}
    let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_reservations q JOIN fm_v2_attempts a ON a.owner_id=q.owner_id AND a.job_id=q.job_id AND a.attempt_id=q.attempt_id AND a.lease_epoch=q.lease_epoch WHERE q.owner_id=?1 AND q.job_id=?2 AND q.attempt_id=?3 AND q.lease_epoch=?4 AND q.foreground_epoch=?5 AND q.connection_id=?6 AND q.coverage_generation=?7 AND q.clock_generation=?8 AND q.reserved_calls=1 AND q.reserved_seconds=?9 AND q.reserved_input_tokens=?10 AND q.reserved_output_tokens=?11 AND a.lease_owner=?12)",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch,epoch,o.connection_id,q.coverage_generation,q.clock_generation,q.job_seconds,o.max_input_tokens,o.max_output_tokens,q.worker_id],|r|r.get(0))?;if !valid{return Err(RepositoryError::NotFound)}Ok(fact)
}
impl<'a> V2Repository<'a> {
    pub(crate) fn skill_dispatch(&mut self,a:&Attempt)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_guarded_epoch(&tx)?;let o=&a.reservation_original.original;verify_shape(&tx,o)?;let fact=reservation_fact(&tx,a)?;
        if read_receipt(&tx,o,"dispatch")?.is_some(){return Err(RepositoryError::NotFound)}let q=&a.reservation_original.reservation;let (quiet,_,_,_,job_seconds,_,_,_,_,_)=policy(&tx,o)?;
        let now=chrono::Utc::now();let (reserved,foreground):(i64,i64)=tx.query_row("SELECT reserved_at_ms,foreground_epoch FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='reserved'",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(RepositoryError::NotFound)?;
        if now.timestamp_millis().checked_sub(reserved).is_none_or(|v|v<0||v>=i64::from(job_seconds)*1000){return Err(RepositoryError::NotFound)}
        let occupancy:Option<(String,String,i64,i64,i64)>=tx.query_row("SELECT coverage_generation,clock_generation,verified,foreground_epoch,last_quiet_tick FROM fm_v2_s05_occupancy WHERE singleton=1",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;let (coverage,clock,verified,current,quiet_tick)=occupancy.ok_or(RepositoryError::NotFound)?;
        if verified!=1||coverage!=q.coverage_generation||clock!=q.clock_generation||current!=foreground||fact["foreground_epoch"]!=current||q.now_tick<quiet_tick||q.now_tick-quiet_tick<i64::from(quiet)*1000||tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown'))",[],|r|r.get::<_,bool>(0))?{return Err(RepositoryError::NotFound)}
        if tx.execute("UPDATE fm_v2_s05_reservations SET state='dispatched' WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='reserved'",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch])?!=1{return Err(RepositoryError::NotFound)}
        if tx.execute("UPDATE fm_v2_attempts SET state='running',updated_at=?5 WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='leased'",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch,now.to_rfc3339()])?!=1{return Err(RepositoryError::NotFound)}
        let out=json!({"version":1,"state":"dispatched","job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"request_sha256":digest(&serde_json::to_value(a)?)?});write_receipt(&tx,o,"dispatch",&out)?;tx.commit()?;Ok(out)
    }
    pub(crate) fn skill_revoke(&mut self,a:&Attempt)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_guarded_epoch(&tx)?;let o=&a.reservation_original.original;verify_shape(&tx,o)?;reservation_fact(&tx,a)?;if read_receipt(&tx,o,"dispatch")?.is_some(){return Err(RepositoryError::NotFound)}
        if tx.execute("UPDATE fm_v2_s05_reservations SET state='revoked' WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='reserved'",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch])?!=1{return Err(RepositoryError::NotFound)}
        if tx.execute("UPDATE fm_v2_attempts SET state='fenced',updated_at=?5 WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='leased'",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch,chrono::Utc::now().to_rfc3339()])?!=1{return Err(RepositoryError::NotFound)}
        if tx.execute("UPDATE fm_v2_jobs SET state='retry_wait' WHERE owner_id=?1 AND job_id=?2 AND state='active' AND current_attempt_id=?3",params![o.owner,a.job_id,a.attempt_id])?!=1{return Err(RepositoryError::NotFound)}
        if tx.execute("UPDATE fm_v2_jobs SET state='cancelled' WHERE owner_id=?1 AND job_id=?2 AND state='retry_wait'",params![o.owner,a.job_id])?!=1{return Err(RepositoryError::NotFound)}
        let result=json!({"version":1,"state":"cancelled","job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch});write_receipt(&tx,o,"revoke",&result)?;tx.commit()?;Ok(result)
    }
    pub(crate) fn skill_prepared_cancel(&mut self,o:&Original)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_guarded_epoch(&tx)?;verify_shape(&tx,o)?;verify_original(&tx,o)?;
        if tx.execute("UPDATE fm_v2_jobs SET state='cancelled' WHERE owner_id=?1 AND job_id=?2 AND kind='skill_draft' AND state='queued' AND attempt_count=0 AND NOT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2)",params![o.owner,o.operation_id])?!=1{return Err(RepositoryError::NotFound)}let out=json!({"version":1,"state":"cancelled","job_id":o.operation_id});write_receipt(&tx,o,"prepared_cancel",&out)?;tx.commit()?;Ok(out)
    }
}
impl<'a> V2Repository<'a> {
    pub(crate) fn skill_terminal(&mut self,r:&Terminal)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_guarded_epoch(&tx)?;let a=&r.attempt;let o=&a.reservation_original.original;verify_shape(&tx,o)?;reservation_fact(&tx,a)?;
        if r.version!=1||!matches!(r.disposition.as_str(),"draft"|"no_draft"|"failed"|"uncertain")||(r.disposition=="draft")!=r.draft_sha256.as_ref().is_some_and(|v|valid_sha256(v)){return Err(RepositoryError::NotFound)}
        if r.disposition!="draft" && r.draft_sha256.is_some(){return Err(RepositoryError::NotFound)}
        let prior_dispatch=read_receipt(&tx,o,"dispatch")?.ok_or(RepositoryError::NotFound)?;if prior_dispatch["request_sha256"]!=digest(&serde_json::to_value(a)?)?{return Err(RepositoryError::NotFound)}
        let terminal_sha=digest(&serde_json::to_value(r)?)?;let existing=read_receipt(&tx,o,if r.disposition=="uncertain" {"uncertain"}else{"terminal"})?;if let Some(value)=existing {if value["request_sha256"]!=terminal_sha{return Err(RepositoryError::NotFound)}tx.commit()?;return Ok(value)}
        let (state,reserved,seconds):(String,i64,i64)=tx.query_row("SELECT state,reserved_at_ms,reserved_seconds FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?))).optional()?.ok_or(RepositoryError::NotFound)?;
        if !matches!(state.as_str(),"dispatched"|"uncertain") {return Err(RepositoryError::NotFound)}let now=chrono::Utc::now();let elapsed=now.timestamp_millis().checked_sub(reserved).ok_or(RepositoryError::NotFound)?;if elapsed<0{return Err(RepositoryError::NotFound)}
        if let Some(usage)=&r.usage {if u64::from(usage.elapsed_seconds)>seconds as u64||usage.input_tokens>o.max_input_tokens||usage.output_tokens>o.max_output_tokens{return Err(RepositoryError::NotFound)}}
        if r.disposition=="uncertain" {if state!="dispatched"||r.usage.is_some(){return Err(RepositoryError::NotFound)}if tx.execute("UPDATE fm_v2_s05_reservations SET state='uncertain' WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='dispatched'",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch])?!=1{return Err(RepositoryError::NotFound)}let out=json!({"version":1,"state":"uncertain","job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"draft_sha256":null,"request_sha256":terminal_sha});write_receipt(&tx,o,"uncertain",&out)?;tx.commit()?;return Ok(out)}
        let now_ms=now.timestamp_millis();let (charged_seconds,charged_input,charged_output)=match &r.usage {Some(u)=>(i64::from(u.elapsed_seconds),i64::from(u.input_tokens),i64::from(u.output_tokens)),None=>(seconds,i64::from(o.max_input_tokens),i64::from(o.max_output_tokens))};
        if tx.execute("UPDATE fm_v2_s05_reservations SET state='settled',settled_at_ms=?5,charged_seconds=?6,charged_input_tokens=?7,charged_output_tokens=?8 WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state IN ('dispatched','uncertain')",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch,now_ms,charged_seconds,charged_input,charged_output])?!=1{return Err(RepositoryError::NotFound)}
        let attempt_state=if r.disposition=="failed" {"failed_terminal"} else {"succeeded"};if tx.execute("UPDATE fm_v2_attempts SET state=?5,updated_at=?6 WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND lease_epoch=?4 AND state='running'",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch,attempt_state,now.to_rfc3339()])?!=1{return Err(RepositoryError::NotFound)}
        let job_state=if r.disposition=="failed" {"failed_terminal"} else {"succeeded"};let charge_basis=if r.usage.is_some() {"actual"}else{"reserved_bound"};let out=json!({"version":1,"state":r.disposition,"charge_basis":charge_basis,"job_id":a.job_id,"attempt_id":a.attempt_id,"lease_epoch":a.lease_epoch,"draft_sha256":r.draft_sha256,"request_sha256":terminal_sha});
        if tx.execute("UPDATE fm_v2_jobs SET state=?4,result_json=?5,updated_at=?6 WHERE owner_id=?1 AND job_id=?2 AND state='active' AND current_attempt_id=?3 AND result_json IS NULL",params![o.owner,a.job_id,a.attempt_id,job_state,serde_json::to_string(&out)?,now.to_rfc3339()])?!=1{return Err(RepositoryError::NotFound)}write_receipt(&tx,o,"terminal",&out)?;tx.commit()?;Ok(out)
    }
    pub(crate) fn skill_original(&mut self,o:&Original)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;require_guarded_epoch(&tx)?;verify_shape(&tx,o)?;
        if read_receipt(&tx,o,"prepare")?.is_none(){let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?3) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?4)",params![o.owner,o.operation_id,receipt_id("prepare",o),format!("outbox_{}_skill_draft_prepare",receipt_id("prepare",o))],|r|r.get(0))?;if collision{return Err(RepositoryError::NotFound)}tx.commit()?;return Ok(json!({"version":1,"state":"unknown"}))}
        verify_original(&tx,o)?;
        for kind in ["terminal","uncertain","revoke","prepared_cancel","dispatch","reserve"] {if let Some(fact)=read_receipt(&tx,o,kind)? {let out=if kind=="reserve" {let mut v=fact;v["state"]=json!("reserved");v["reserve_receipt_id"]=json!(receipt_id("reserve",o));v}else{fact};tx.commit()?;return Ok(out)}}
        tx.commit()?;Ok(json!({"version":1,"state":"queued","job_id":o.operation_id}))
    }
}
impl<'a> V2Repository<'a> {
    pub(crate) fn skill_prepare_original(&mut self,store:&crate::store::sqlite::SqliteStore,o:&Original)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;require_guarded_epoch(&tx)?;verify_shape(&tx,o)?;
        if retained_accounting(&tx,&o.owner,&o.account_id)?.iter().any(|m|m.job_id==o.operation_id)||verified_purge_usage(&tx,store,&o.owner,&o.account_id)?.iter().any(|m|format!("skilldraft_{}",m.operation_sha256)==o.operation_id){tx.commit()?;return Ok(json!({"version":1,"state":"erased","job_id":o.operation_id}))}
        let stored:Option<(String,bool)>=tx.query_row("SELECT j.input_hash,EXISTS(SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection') FROM fm_v2_jobs j WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind='skill_draft'",params![o.owner,o.operation_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if let Some((hash,manifest))=stored {if hash!=original_raw(o)?.0 {
            let mut budget=ResetFenceBudget::install(&tx);let inventory=collect_reset_terminals(&tx,&o.owner,&o.account_id,"status","status",&mut budget)?;drop(budget);
            if !inventory.iter().any(|m|m.job_id==o.operation_id && m.original_sha256==hash && m.workspace_id==o.workspace_id){return Err(RepositoryError::NotFound)}
            tx.commit()?;return Ok(json!({"version":1,"state":if manifest {"existing"}else{"erased"},"job_id":o.operation_id}))
        }}
        if let Some(erased)=read_receipt(&tx,o,"erased")? {
            let hash=original_raw(o)?.0;let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=?1 AND j.job_id=?2 AND j.kind='skill_draft' AND j.input_hash=?3 AND j.state IN ('succeeded','failed_terminal','cancelled') AND NOT EXISTS(SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection'))",params![o.owner,o.operation_id,hash],|r|r.get(0))?;
            if !valid||erased!=json!({"version":1,"account_id":o.account_id,"owner":o.owner,"job_id":o.operation_id,"workspace_id":o.workspace_id,"original_sha256":hash}){return Err(RepositoryError::NotFound)}
            tx.commit()?;return Ok(json!({"version":1,"state":"erased","job_id":o.operation_id}))
        }
        if read_receipt(&tx,o,"prepare")?.is_none(){let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?3) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?4)",params![o.owner,o.operation_id,receipt_id("prepare",o),format!("outbox_{}_skill_draft_prepare",receipt_id("prepare",o))],|r|r.get(0))?;if collision{return Err(RepositoryError::NotFound)}tx.commit()?;return Ok(json!({"version":1,"state":"unknown"}))}
        verify_original(&tx,o)?;let state:String=tx.query_row("SELECT state FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![o.owner,o.operation_id],|r|r.get(0))?;let out=json!({"version":1,"state":state,"job_id":o.operation_id});tx.commit()?;Ok(out)
    }
    pub(crate) fn skill_attempt_original(&mut self,a:&Attempt)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;require_guarded_epoch(&tx)?;let o=&a.reservation_original.original;verify_shape(&tx,o)?;let reserve=reservation_fact(&tx,a)?;
        let states:Option<(String,String,String,Option<String>)>=tx.query_row("SELECT j.state,a.state,q.state,j.result_json FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch WHERE j.owner_id=?1 AND j.job_id=?2 AND a.attempt_id=?3 AND a.lease_epoch=?4",params![o.owner,a.job_id,a.attempt_id,a.lease_epoch],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;let (job,attempt,reservation,result)=states.ok_or(RepositoryError::NotFound)?;
        let output=match (job.as_str(),attempt.as_str(),reservation.as_str()) {
            ("active","leased","reserved")=>{let mut value=reserve;value["state"]=json!("reserved");value["reserve_receipt_id"]=json!(receipt_id("reserve",o));value},
            ("active","running","dispatched")=>{let value=read_receipt(&tx,o,"dispatch")?.ok_or(RepositoryError::NotFound)?;if value["request_sha256"]!=digest(&serde_json::to_value(a)?)?{return Err(RepositoryError::NotFound)}value},
            ("active","running","uncertain")=>read_receipt(&tx,o,"uncertain")?.ok_or(RepositoryError::NotFound)?,
            ("cancelled","fenced","revoked")=>read_receipt(&tx,o,"revoke")?.ok_or(RepositoryError::NotFound)?,
            ("succeeded","succeeded","settled")|("failed_terminal","failed_terminal","settled")=>{let value=read_receipt(&tx,o,"terminal")?.ok_or(RepositoryError::NotFound)?;let raw=result.ok_or(RepositoryError::NotFound)?;if crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?!=value{return Err(RepositoryError::NotFound)}value},
            _=>return Err(RepositoryError::NotFound)
        };tx.commit()?;Ok(output)
    }
    pub(crate) fn skill_reserve_original(&mut self,r:&Reserve)->Result<Value,RepositoryError>{
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;require_guarded_epoch(&tx)?;verify_shape(&tx,&r.original)?;verify_original(&tx,&r.original)?;
        if read_receipt(&tx,&r.original,"reserve")?.is_none(){let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?3) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?4)",params![r.original.owner,r.original.operation_id,receipt_id("reserve",&r.original),format!("outbox_{}_skill_draft_reserve",receipt_id("reserve",&r.original))],|row|row.get(0))?;if collision{return Err(RepositoryError::NotFound)}tx.commit()?;return Ok(json!({"version":1,"state":"unknown"}))}tx.commit()?;
        let a=Attempt{version:1,reservation_original:r.clone(),job_id:r.original.operation_id.clone(),attempt_id:format!("skillattempt_{}",sha256_hex(r.original.operation_id.as_bytes())),lease_epoch:1};self.skill_attempt_original(&a)
    }
    pub(crate) fn skill_phase(&mut self,store:&crate::store::sqlite::SqliteStore,owner:&str,workspace_id:&str,phase:&str,request:&Value)->Result<Value,RepositoryError>{
        let envelope=json!({"owner":owner,"workspace_id":workspace_id,"phase":format!("skill_draft_{phase}"),"request":request});if serde_json::to_vec(&envelope)?.len()>65536{return Err(RepositoryError::NotFound)}
        if matches!(phase,"retire"|"retire_original") {
            let retirement:Retirement=serde_json::from_value(request.clone())?;
            if retirement.owner!=owner||!text(workspace_id){return Err(RepositoryError::NotFound)}
            return if phase=="retire" {self.skill_retire(&retirement)} else {self.skill_retire_original(&retirement)}
        }
        let original=match phase {"prepare"|"prepare_original"|"prepared_cancel"=>request,"reserve"|"reserve_original"=>request.get("original").ok_or(RepositoryError::NotFound)?,"dispatch"|"revoke"|"original"|"result_original"=>request.pointer("/reservation_original/original").ok_or(RepositoryError::NotFound)?,"terminal"=>request.pointer("/attempt/reservation_original/original").ok_or(RepositoryError::NotFound)?,_=>return Err(RepositoryError::NotFound)};
        let original:Original=serde_json::from_value(original.clone())?;if original.owner!=owner||original.workspace_id!=workspace_id{return Err(RepositoryError::NotFound)}
        match phase {
            "prepare"=>self.skill_prepare(store,&serde_json::from_value(request.clone())?),
            "prepare_original"=>self.skill_prepare_original(store,&serde_json::from_value(request.clone())?),
            "prepared_cancel"=>self.skill_prepared_cancel(&serde_json::from_value(request.clone())?),
            "reserve"=>self.skill_reserve(store,&serde_json::from_value(request.clone())?),
            "reserve_original"=>self.skill_reserve_original(&serde_json::from_value(request.clone())?),
            "dispatch"=>self.skill_dispatch(&serde_json::from_value(request.clone())?),
            "revoke"=>self.skill_revoke(&serde_json::from_value(request.clone())?),
            "original"|"result_original"=>self.skill_attempt_original(&serde_json::from_value(request.clone())?),
            "terminal"=>self.skill_terminal(&serde_json::from_value(request.clone())?),
            _=>Err(RepositoryError::NotFound),
        }
    }
}

#[cfg(test)]
mod retirement_tests {
    use super::*;
    #[test]
    fn genuine_store_policy_envelopes_and_malformed_refuse() {
        let directory=std::env::var("M5_GENUINE_POLICY_DIR").expect("genuine policy directory");
        for version in 1..=3 {
            let raw=std::fs::read_to_string(format!("{directory}/genuine-store-v{version}.json")).unwrap();
            let document:Value=serde_json::from_str(&raw).unwrap();
            let policy=accepted_policy(&document).expect("genuine STORE document");
            assert!(policy.get("schedule").is_some_and(Value::is_object));
            assert!(policy.get("budget").is_some_and(Value::is_object));
            let mut malformed=document.clone();
            malformed.as_object_mut().unwrap().remove("fingerprint");
            assert!(accepted_policy(&malformed).is_err());
            if version > 1 {
                let mut bad_nested=document.clone();
                bad_nested["workKindAssignments"]["interpretation"]["revision"]=json!("wrong");
                let mut body=bad_nested.as_object().unwrap().clone(); body.remove("fingerprint");
                bad_nested["fingerprint"]=json!(sha256_hex(&serde_json::to_vec(&body).unwrap()));
                assert!(accepted_policy(&bad_nested).is_err());
                if version == 3 {
                    let mut bad_rating=document.clone();bad_rating["ratings_override"]["explicit_submission_only"]=json!(false);
                    let mut body=bad_rating.as_object().unwrap().clone();body.remove("fingerprint");
                    bad_rating["fingerprint"]=json!(sha256_hex(&serde_json::to_vec(&body).unwrap()));
                    assert!(accepted_policy(&bad_rating).is_err());
                }
                let mut missing=document.clone();
                missing.as_object_mut().unwrap().insert("accepted_policy".into(),Value::Null);
                let mut body=missing.as_object().unwrap().clone(); body.remove("fingerprint");
                missing["fingerprint"]=json!(sha256_hex(&serde_json::to_vec(&body).unwrap()));
                assert!(accepted_policy(&missing).is_err());
            }
        }
    }

    #[test]
    fn m5_genuine_settlement_fixture() {
        let Ok(path)=std::env::var("M5_PURGE_FIXTURE_DB") else {return};
        let database=std::env::var("M5_PURGE_FIXTURE_ID").unwrap();
        let original:Original=serde_json::from_str(&std::env::var("M5_SEED_ORIGINAL").unwrap()).unwrap();
        let reservation:S08RulesReservation=serde_json::from_str(&std::env::var("M5_SEED_RESERVATION").unwrap()).unwrap();
        let (store,_,_,_,_,_)=crate::store::sqlite::SqliteStore::open_guarded(&path,4,&database).unwrap();
        let mut conn=store.conn.lock().unwrap();let mut repo=V2Repository::new(&mut conn).unwrap();
        assert_eq!(repo.skill_prepare(&store,&original).unwrap()["state"],"queued");
        let reserve=Reserve{version:1,original:original.clone(),reservation};
        assert_eq!(repo.skill_reserve(&store,&reserve).unwrap()["state"],"reserved");
        let attempt=Attempt{version:1,reservation_original:reserve,job_id:original.operation_id.clone(),attempt_id:format!("skillattempt_{}",sha256_hex(original.operation_id.as_bytes())),lease_epoch:1};
        assert_eq!(repo.skill_dispatch(&attempt).unwrap()["state"],"dispatched");
        let terminal=Terminal{version:1,attempt,disposition:"draft".into(),draft_sha256:Some("d".repeat(64)),usage:Some(Usage{elapsed_seconds:2,input_tokens:3,output_tokens:4})};
        assert_eq!(repo.skill_terminal(&terminal).unwrap()["charge_basis"],"actual");
        let part=RetiredPart{actor_id:"main".into(),message_id:"root".into(),part_id:"body".into(),revision:1,content_hash:"a".repeat(64)};
        let mut retirement=Retirement{version:1,owner:"alice".into(),account_id:"acct-a".into(),chat_id:"chat-1".into(),operation_id:String::new(),part:Some(part)};
        retirement.operation_id=format!("skillretire_{}",digest(&json!(["openclank.skill-draft.retirement.v1",retirement.owner,retirement.account_id,retirement.chat_id,retirement.part])).unwrap());
        let result=repo.skill_retire(&retirement).unwrap();assert_eq!(result["state"],"retired");assert_eq!(result["retired_count"],1);
        assert_eq!(repo.skill_retire_original(&retirement).unwrap(),result);
        assert_eq!(repo.skill_prepare_original(&store,&original).unwrap()["state"],"erased");
    }
    #[test]
    fn reset_usage_terminal_is_bound_and_not_double_counted(){
        let mut conn=Connection::open_in_memory().unwrap();conn.execute_batch(include_str!("../store/current-schema.sql")).unwrap();conn.execute_batch("ALTER TABLE fm_v2_change_sets ADD COLUMN operation_metadata_json TEXT; CREATE TABLE fm_v2_s05_reservations(owner_id TEXT,job_id TEXT,state TEXT,settled_at_ms INTEGER,charged_calls INTEGER,charged_seconds INTEGER,charged_input_tokens INTEGER,charged_output_tokens INTEGER); UPDATE fm_meta SET value='test-database' WHERE key='database_id'; CREATE TABLE fm_v2_s05_schema(version INTEGER); INSERT INTO fm_v2_s05_schema VALUES(4); INSERT INTO fm_meta(key,value) VALUES('openclank.assistant_interpretation.v1','1'),('openclank.s08.model.v2','1'),('openclank.s05.skill_draft.v1','1')").unwrap();let tx=conn.transaction().unwrap();
        let req=json!({"owner":"alice","account_id":"account-uuid","workspace_id":"reset-workspace","operation_id":"reset-operation","components":["memories"]});let reset_id=ingest_erased_receipt_id("account-uuid","reset-operation").unwrap();let body=json!({"version":"s06.guarded-reset.v1","request_sha256":sha256_hex(&serde_json::to_vec(&req).unwrap()),"request":req,"result":{"complete":true,"categories":{"memories":{"state":"complete"}}}});let raw=serde_json::to_string(&body).unwrap();tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES('alice',?1,'user','alice','s06.guarded-reset.v1',NULL,?2,?3,'now',?4)",params![reset_id,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),raw]).unwrap();insert_outbox(&tx,"alice",&reset_id,"s06_guarded_reset",&body,0,"now").unwrap();
        let marker=ResetTerminal{version:1,captured_owner:"alice".into(),account_id:"account-uuid".into(),workspace_id:"memory-workspace".into(),job_id:format!("skilldraft_{}","b".repeat(64)),original_sha256:"a".repeat(64),disposition:"draft".into(),charge_basis:"actual".into(),settled_at_ms:1000,charged_calls:1,charged_seconds:2,charged_input_tokens:3,charged_output_tokens:4,reset_workspace_id:"reset-workspace".into(),reset_operation_id:"reset-operation".into(),reset_receipt_id:reset_id};
        let mut budget=ResetFenceBudget::install(&tx);write_reset_terminals(&tx,"alice",&[marker.clone()],&mut budget).unwrap();drop(budget);assert_eq!(unbacked_reset_terminals(&tx,"alice","account-uuid").unwrap().len(),1);
        tx.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,attempt_count,input_hash,created_at,updated_at) VALUES('alice',?1,'skill_draft','memory-workspace','',?1,'succeeded',1,?2,'now','now')",params![marker.job_id,marker.original_sha256]).unwrap();tx.execute("INSERT INTO fm_v2_s05_reservations VALUES('alice',?1,'settled',1000,1,2,3,4)",params![marker.job_id]).unwrap();assert!(unbacked_reset_terminals(&tx,"alice","account-uuid").unwrap().is_empty());tx.execute("UPDATE fm_v2_s05_reservations SET charged_seconds=9",[]).unwrap();assert!(unbacked_reset_terminals(&tx,"alice","account-uuid").is_err());tx.rollback().unwrap();
    }
    #[test]
    fn capture_preferences_require_explicit_opt_in_and_respect_off(){
        let prefs=|mode:Value,auto:Value,enabled:Value,version:Value|json!({"memory_mode":mode,"auto_memory":auto,"memory_enabled":enabled,"agent_settings_distill_version":version}).as_object().unwrap().clone();
        let absent=json!([false,null]);let yes=json!([true,true]);let opted=json!([true,1]);
        assert!(require_capture_preferences(&prefs(absent.clone(),yes.clone(),yes.clone(),opted.clone())).is_ok());
        assert!(require_capture_preferences(&prefs(json!([true,"off"]),yes.clone(),yes.clone(),opted.clone())).is_err());
        assert!(require_capture_preferences(&prefs(absent.clone(),json!([true,false]),yes.clone(),opted.clone())).is_err());
        assert!(require_capture_preferences(&prefs(json!([true,"manual"]),json!([true,false]),yes.clone(),opted.clone())).is_ok());
        assert!(require_capture_preferences(&prefs(absent.clone(),yes.clone(),yes.clone(),json!([true,true]))).is_err());
        assert!(require_capture_preferences(&prefs(absent,yes,json!([true,"yes"]),opted)).is_err());
    }
    #[test]
    fn exact_part_retirement_removes_selector_and_preserves_original_custody(){
        let mut conn=Connection::open_in_memory().unwrap();conn.execute_batch(include_str!("../store/current-schema.sql")).unwrap();conn.execute_batch("ALTER TABLE fm_v2_change_sets ADD COLUMN operation_metadata_json TEXT; CREATE TABLE fm_v2_s05_reservations(owner_id TEXT,job_id TEXT)").unwrap();
        let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();
        let hash="a".repeat(64);let source=HostArchiveSourceRef{version:1,kind:"host_user_body".into(),account_id:"account-uuid".into(),chat_id:"chat-one".into(),actor_id:"main".into(),message_id:"root".into(),part_id:"body".into(),revision:1,content_hash:hash.clone(),workspace_id:Some("chat-workspace".into()),project_id:None};
        let part=Part{message_id:"assistant".into(),part_id:"tool".into(),revision:3,content_hash:hash.clone()};let policy=PolicyProjectionRef{epoch:1,fingerprint:hash.clone(),transition_id:"transition".into()};
        let mut o=Original{version:1,owner:"owner-name".into(),account_id:"account-uuid".into(),workspace_id:"memory-workspace".into(),operation_id:"original-job".into(),ordered_sources:vec![SelectedSource{source:source.clone(),parts:vec![part.clone(),Part{part_id:"tool-two".into(),..part.clone()}]},SelectedSource{source:HostArchiveSourceRef{chat_id:"chat-two".into(),..source},parts:vec![part.clone(),Part{part_id:"tool-two".into(),..part}]}],selection_sha256:hash.clone(),policy_projection:PolicyPublicationRefs{account:policy.clone(),global:policy},account_policy_input_json:"{}".into(),target_fingerprint:hash.clone(),policy_fingerprint:hash.clone(),prompt_sha256:hash.clone(),assignment_sha256:hash.clone(),assignment:Assignment{version:1,connection_id:"connection".into(),model_route_id:"route".into(),provider_grant_id:None,teacher_spec_sha256:hash.clone()},connection_id:"connection".into(),model_route_id:"route".into(),max_input_tokens:10,max_output_tokens:10,daily_calls:1,daily_tokens:100};
        o.assignment_sha256=digest(&serde_json::to_value(&o.assignment).unwrap()).unwrap();o.selection_sha256=digest(&json!({"version":1,"ordered_sources":o.ordered_sources})).unwrap();o.operation_id=format!("skilldraft_{}",digest(&json!(["openclank.skill-draft.operation.v1",o.account_id,o.workspace_id,o.selection_sha256])).unwrap());
        let (input,raw)=original_raw(&o).unwrap();tx.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,attempt_count,input_hash,created_at,updated_at) VALUES(?1,?2,'skill_draft',?3,'',?2,'queued',0,?4,'now','now')",params![o.owner,o.operation_id,o.workspace_id,input]).unwrap();
        tx.execute("INSERT INTO fm_v2_job_manifests(owner_id,manifest_id,job_id,phase,selected_json,config_hash,model_hash,tool_hash,input_hash,content_hash,sealed_at) VALUES(?1,'manifest',?2,'selection',?3,?4,?4,?4,?5,?5,'now')",params![o.owner,o.operation_id,raw,hash,input]).unwrap();write_receipt(&tx,&o,"prepare",&json!({"version":1,"original_sha256":input,"job_id":o.operation_id,"account_id":o.account_id})).unwrap();
        let mut r=Retirement{version:1,owner:o.owner.clone(),account_id:o.account_id.clone(),chat_id:"chat-one".into(),operation_id:String::new(),part:Some(RetiredPart{actor_id:"main".into(),message_id:"assistant".into(),part_id:"tool".into(),revision:2,content_hash:hash})};
        let set_id=|r:&mut Retirement|{r.operation_id=format!("skillretire_{}",digest(&json!(["openclank.skill-draft.retirement.v1",r.owner,r.account_id,r.chat_id,r.part])).unwrap())};set_id(&mut r);
        let mut budget=ResetFenceBudget::install(&tx);assert!(source_fence_matches(&tx,&r,&mut budget).unwrap().is_empty());let mut root=Retirement{version:1,owner:r.owner.clone(),account_id:r.account_id.clone(),chat_id:r.chat_id.clone(),operation_id:String::new(),part:Some(RetiredPart{actor_id:"main".into(),message_id:"root".into(),part_id:"body".into(),revision:1,content_hash:"a".repeat(64)})};set_id(&mut root);assert_eq!(source_fence_matches(&tx,&root,&mut budget).unwrap().len(),1);r.part.as_mut().unwrap().revision=3;set_id(&mut r);assert_eq!(retire_selected(&tx,&r,&mut budget).unwrap(),1);assert!(source_fence_matches(&tx,&r,&mut budget).unwrap().is_empty());drop(budget);
        assert!(read_job_receipt(&tx,&o.owner,&o.operation_id,"erased").unwrap().is_some());let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2",params![o.owner,o.operation_id],|row|row.get(0)).unwrap();assert_eq!(count,0);tx.commit().unwrap();
    }
}
