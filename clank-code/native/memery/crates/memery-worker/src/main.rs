use std::future::Future;
use std::sync::Arc;
use std::collections::{HashMap,HashSet};

use memery_core::config::FmConfig;
use memery_core::embed::{DisabledEmbeddingClient, EmbeddingClient};
use memery_core::provider::native::NativeProvider;
use memery_core::provider::{GroomOpArgs, MemoryProvider};
use memery_core::record::*;
use memery_core::store::graph::CodeIndexFailure;
use memery_core::store::sqlite::{ForgetSelector, RetentionPolicy, SqliteStore};
mod protocol;
use protocol::WorkerError;
use serde_json::Value;
use serde::Deserialize;

// Private isolated-candidate diagnostics only. No public/wire result fields,
// and disabled operations create neither a collector nor an Instant.
struct VisualTiming {
    context: memery_core::code_metrics::MetricsContext,
    started: std::time::Instant,
    lane_wait_ns: u64,
}
static VISUAL_TIMING_RECORDS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
fn visual_timings_enabled() -> bool {
    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ENABLED.get_or_init(|| std::env::var_os("FM_CODE_QUERY_TIMINGS").is_some_and(|v| v == "1"))
}
#[derive(Clone, Copy)]
enum VisualTimingPhase { Worker, Delivery }
impl VisualTiming {
    fn begin(enabled: bool, records: &std::sync::atomic::AtomicUsize) -> Option<Self> {
        use std::sync::atomic::Ordering::Relaxed;
        if !enabled || records.fetch_update(Relaxed, Relaxed, |n| if n < 128 {Some(n + 1)} else {None}).is_err() { return None; }
        Some(Self { context: Default::default(), started: std::time::Instant::now(), lane_wait_ns: 0 })
    }
    fn lane_acquired(&mut self) { self.lane_wait_ns = self.started.elapsed().as_nanos().min(u64::MAX as u128) as u64; }
    fn record(self, token: &str, phase: VisualTimingPhase) -> serde_json::Value {
        serde_json::json!({"schema":"openclank.code-visual-timing/v1","token":token,
            "phase":match phase {VisualTimingPhase::Worker=>"worker",VisualTimingPhase::Delivery=>"delivery"},
            "elapsed_ns":self.started.elapsed().as_nanos().min(u64::MAX as u128) as u64,
            "lane_wait_ns":self.lane_wait_ns,"metrics":self.context.snapshot()})
    }
    fn emit(self, token: &str, phase: VisualTimingPhase) {
        tracing::info!(target:"fm_code_query_timing", record=%self.record(token,phase));
    }
}

#[cfg(test)]
mod visual_timing_tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize,Ordering};

    #[tokio::test]
    async fn interactive_activity_survives_abandoned_worker_and_delivery_waiters() {
        // Same closure-owned lifetime as both production blocking phases. No
        // completed-token retention, browser, authority fixture or index job.
        for phase in ["worker","delivery"] {
            let scheduler=Arc::new(memery_core::code_index_scheduler::IndexScheduler::default());
            let owned=scheduler.clone();let (entered,seen)=std::sync::mpsc::channel();let (release,released)=std::sync::mpsc::channel();let (finished,finish_seen)=std::sync::mpsc::channel();
            let waiter=tokio::task::spawn_blocking(move||{
                let activity=owned.interactive_activity();entered.send(()).unwrap();released.recv().unwrap();drop(activity);finished.send(()).unwrap();
            });
            seen.recv_timeout(std::time::Duration::from_secs(2)).unwrap();waiter.abort();drop(waiter);
            let background=scheduler.clone();let (progress,progress_seen)=std::sync::mpsc::channel();
            let verification=std::thread::spawn(move||background.run(||{background.verification_checkpoint()?;progress.send(()).unwrap();Ok(())}));
            assert!(matches!(progress_seen.recv_timeout(std::time::Duration::from_millis(20)),Err(std::sync::mpsc::RecvTimeoutError::Timeout)),"{phase} guard must outlive async waiter");
            release.send(()).unwrap();finish_seen.recv_timeout(std::time::Duration::from_secs(2)).unwrap();progress_seen.recv_timeout(std::time::Duration::from_secs(2)).unwrap();verification.join().unwrap().unwrap();
            assert_eq!(scheduler.status().active,0);
        }
    }

    #[test]
    fn visual_timing_disabled_and_process_cap_are_bounded() {
        let records=AtomicUsize::new(0);
        assert!(VisualTiming::begin(false,&records).is_none());
        assert_eq!(records.load(Ordering::Relaxed),0);
        for _ in 0..128 { assert!(VisualTiming::begin(true,&records).is_some()); }
        assert!(VisualTiming::begin(true,&records).is_none());
        assert_eq!(records.load(Ordering::Relaxed),128);
        records.store(usize::MAX,Ordering::Relaxed);
        assert!(VisualTiming::begin(true,&records).is_none());
    }

    #[test]
    fn visual_timing_record_has_only_fixed_numeric_phase_fields() {
        use memery_core::code_metrics::{span,Stage};
        let records=AtomicUsize::new(0);
        let mut timing=VisualTiming::begin(true,&records).unwrap();
        timing.lane_acquired();
        timing.context.enter(|| {
            let _context=span(Stage::MapContext);
            {let _entry=span(Stage::MapPolicyEntry);}
            {let _projection=span(Stage::MapProjection);}
            {let _final=span(Stage::MapPolicyFinal);}
        });
        let row=timing.record("query_fixture",VisualTimingPhase::Worker);
        let mut fields=row.as_object().unwrap().keys().map(String::as_str).collect::<Vec<_>>();fields.sort();
        assert_eq!(fields,vec!["elapsed_ns","lane_wait_ns","metrics","phase","schema","token"]);
        assert_eq!(row["phase"],"worker");assert_eq!(row["schema"],"openclank.code-visual-timing/v1");
        assert!(row["elapsed_ns"].as_u64().unwrap()>=row["lane_wait_ns"].as_u64().unwrap());
        let stages=row["metrics"]["stages"].as_array().unwrap();assert_eq!(stages.len(),29);
        for name in ["map_context","map_policy_entry","map_policy_final","map_projection"] {
            let value=stages.iter().find(|s|s["stage"]==name).unwrap();assert_eq!(value["counters"]["calls"],1);
        }
        let delivery=VisualTiming::begin(true,&records).unwrap().record("query_fixture",VisualTimingPhase::Delivery);
        assert_eq!(delivery["phase"],"delivery");assert_eq!(delivery["lane_wait_ns"],0);
    }
}

// Open only after prepared-original reset/rename/purge closure is joined.


#[derive(Clone)]
struct MemeryWorker {
    provider: Arc<NativeProvider>,
    profile_receipt: serde_json::Value,
    /// Concrete store handle for graph ops (they live on SqliteStore, not
    /// on the MemoryStore trait).
    graph_store: Arc<SqliteStore>,
    code_tasks: Arc<std::sync::Mutex<HashMap<String,CodeTask>>>,
    code_queries: Arc<std::sync::Mutex<HashMap<String,CodeQueryTask>>>,
    s02_importance_permits: Arc<std::sync::Mutex<HashMap<String,(std::time::Instant,memery_core::repository::S02ImportancePermit)>>>,
    // The authority database has one exclusive publication fence. Queue code
    // readers cooperatively instead of racing that fence's short busy timeout.
    // Four jobs remain admitted; recall and cancellation do not take this lane.
    code_query_lane: Arc<tokio::sync::Semaphore>,
    code_query_secret: [u8;32],
}

#[derive(Default)]
struct CodeTask { running:bool, token:String, ticket:Option<memery_core::code::CodeWorkTicket>, subscribers:HashSet<String>, error:Option<CodeIndexFailure>, cancel:Arc<std::sync::atomic::AtomicBool> }

struct CodeQueryTask {
    binding_key:String,
    cancel:Arc<std::sync::atomic::AtomicBool>,
    result:Option<Result<serde_json::Value,memery_core::code_query::QueryError>>,
    completed:Option<std::time::Instant>,
    expires:Option<std::time::Instant>,
    topology_reserved:usize,
    topology:Option<memery_core::code_topology::Capture>,
}
// The ordinary fence inspects bounded keyed authority/head metadata, not graph
// records. QueryCost.work counts inspected graph records, not SQL/CPU work.
// Measure the complete returned native result using the normal query's
// fixed-point JSON byte/token upper-bound convention.
fn measured_read_fence(fence: String, metadata_bytes: usize, budget: &memery_core::code_query::QueryBudget) -> Result<serde_json::Value, memery_core::code_query::QueryError> {
    let fail = |code: &str, message: &str| memery_core::code_query::QueryError { code: code.into(), message: message.into() };
    let mut value = serde_json::json!({"private_read_fence":fence,"private_policy_fence":false,"receipt":null,"cost":memery_core::code_query::QueryCost::default()});
    value["cost"]["metadata_bytes"] = serde_json::json!(metadata_bytes);
    for _ in 0..12 {
        let size = serde_json::to_vec(&value).map_err(|_| fail("unavailable", "Code fence serialization unavailable"))?.len();
        if value["cost"]["serialized_bytes"].as_u64() == Some(size as u64)
            && value["cost"]["token_upper_bound"].as_u64() == Some(size as u64) {
            if size > budget.bytes.min(budget.tokens) { return Err(fail("budget_exceeded", "Complete code fence exceeds budget")); }
            return Ok(value);
        }
        value["cost"]["serialized_bytes"] = serde_json::json!(size);
        value["cost"]["token_upper_bound"] = serde_json::json!(size);
    }
    Err(fail("unavailable", "Unstable code fence envelope size"))
}

#[cfg(test)]
mod read_fence_cost_tests {
    use super::*;
    #[test]
    fn measured_result_respects_exact_envelope_budget() {
        let mut budget = memery_core::code_query::QueryBudget::default();
        let value = measured_read_fence("a".repeat(64), 123, &budget).unwrap();
        let n = serde_json::to_vec(&value).unwrap().len();
        assert_eq!(value["cost"]["inspected_records"], 0);
        assert_eq!(value["cost"]["metadata_bytes"], 123);
        assert_eq!(value["cost"]["serialized_bytes"], n);
        assert_eq!(value["cost"]["token_upper_bound"], n);
        budget.bytes = n - 1;
        assert_eq!(measured_read_fence("a".repeat(64), 123, &budget).unwrap_err().code, "budget_exceeded");
        budget.bytes = n;
        budget.tokens = n - 1;
        assert_eq!(measured_read_fence("a".repeat(64), 123, &budget).unwrap_err().code, "budget_exceeded");
        budget.tokens = n;
        assert!(measured_read_fence("a".repeat(64), 123, &budget).is_ok());
    }
}

fn query_binding_key(binding:&memery_core::code::WorkspaceBinding)->String{
    serde_json::to_string(&(binding.owner.as_str(),binding.owner_subject_id.as_str(),binding.workspace_id.as_str(),binding.location_id.as_str(),binding.workspace_revision,binding.policy_generation,binding.origin.as_str(),&binding.root,binding.subscriber_id.as_str())).unwrap()
}

fn lifecycle_error(message:String)->WorkerError {
    if message == "denied: directory binding changed" {
        return WorkerError::invalid_params(message,Some(serde_json::json!({"code":"code_root_changed"})));
    }
    if message.starts_with("code_node_forget_requires_workspace_erase:") {
        return WorkerError::invalid_params(message,Some(serde_json::json!({"code":"code_node_forget_requires_workspace_erase"})));
    }
    if ["denied:","erased:","ambiguous_registration:","stale_queued_ticket:"].iter().any(|prefix|message.starts_with(prefix)) {
        WorkerError::invalid_params(message,None)
    } else {WorkerError::internal_error(message,None)}
}

fn scrub_private_root(value:&mut serde_json::Value,root:&str){
    if root.is_empty(){return;}
    if let Some(text)=value.get_mut("error").and_then(|e|e.get_mut("message")).and_then(|v|v.as_str().map(str::to_string)){
        value["error"]["message"]=text.replace(root,"[root]").into();
    }
    if let Some(coverage)=value.get_mut("coverage").and_then(|v|v.as_array_mut()){
        for entry in coverage{if let Some(text)=entry.get("detail").and_then(|v|v.as_str()){entry["detail"]=text.replace(root,"[root]").into();}}
    }
}

fn code_task_key(scope:&memery_core::graph::GraphScope,checkout:&str)->String{serde_json::to_string(&[scope.owner.as_str(),scope.workspace_id.as_str(),checkout]).expect("string tuple serialization")}

#[cfg(test)]
mod s03_wire_tests{
    use super::*;
    #[test]
    fn root_redaction_preserves_canonical_coverage_and_ids(){
        for root in ["/","src"]{
            let mut value=serde_json::json!({"registration_id":"src_registration","coverage":[{"rel_path":"src/sub/file.py","source_hash":"src_hash","source_generation":"src_generation","detail":format!("failed {root}private")}],"error":{"message":format!("failed {root}private")}});
            scrub_private_root(&mut value,root);assert_eq!(value["registration_id"],"src_registration");assert_eq!(value["coverage"][0]["rel_path"],"src/sub/file.py");assert_eq!(value["coverage"][0]["source_hash"],"src_hash");assert_eq!(value["coverage"][0]["source_generation"],"src_generation");assert!(value["error"]["message"].as_str().unwrap().contains("[root]"));
        }
        let error=lifecycle_error("code_node_forget_requires_workspace_erase: use workspace erasure".into());assert_eq!(error.code,"invalid");assert_eq!(error.data.unwrap()["code"],"code_node_forget_requires_workspace_erase");
    }
}

#[cfg(feature="s03-fixtures")]
async fn fixture_queued_gate(token:&str)->Result<(),String> {
    if let Ok(directory)=std::env::var("FM_S03_FIXTURE_GATE") {
        let directory=std::path::PathBuf::from(directory);
        if !directory.is_absolute() || !directory.is_dir(){return Err("invalid fixture gate directory".into());}
        std::fs::write(directory.join(format!("{token}.held")),b"queued-before-claim").map_err(|e|e.to_string())?;
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(60);
        while !directory.join("release").exists() {
            if std::time::Instant::now()>deadline{return Err("fixture_gate_deadline".into());}
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    }
    Ok(())
}

#[cfg(feature="s03-fixtures")]
fn fixture_finished(token:&str,rejected:bool){
    if let Ok(directory)=std::env::var("FM_S03_FIXTURE_GATE"){
        let directory=std::path::PathBuf::from(directory);
        if directory.is_absolute() && directory.is_dir(){let _=std::fs::write(directory.join(format!("{token}.finished")),if rejected{b"rejected".as_slice()}else{b"completed".as_slice()});}
    }
}

fn reject_agent_bridge_internal_tool(tool: &str) -> Result<(), WorkerError> {
    let agent_bridge = std::env::var("FM_AGENT_BRIDGE").is_ok_and(|value| {
        matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes"
        )
    });
    if agent_bridge {
        return Err(WorkerError::invalid_params(
            format!("{tool} is not available through an Agent memory bridge"),
            None,
        ));
    }
    Ok(())
}

fn request_scope(
    owner: Option<String>,
    workspace_id: Option<String>,
    include_global: bool,
) -> Result<memery_core::graph::GraphScope, WorkerError> {
    let env_owner = std::env::var("FM_OWNER")
        .ok()
        .filter(|value| !value.trim().is_empty());
    let env_workspace = std::env::var("FM_WORKSPACE_ID")
        .ok()
        .filter(|value| !value.trim().is_empty());
    let trusted_caller = std::env::var("FM_SCOPE_AUTHORITY")
        .is_ok_and(|value| value.eq_ignore_ascii_case("trusted-caller"));
    let legacy = std::env::var("FM_LEGACY_SINGLE_USER")
        .is_ok_and(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes"));

    let (owner, workspace_id) = if let (Some(env_owner), Some(env_workspace)) =
        (env_owner, env_workspace)
    {
        if owner
            .as_deref()
            .is_some_and(|value| value.trim() != env_owner)
            || workspace_id
                .as_deref()
                .is_some_and(|value| value.trim() != env_workspace)
        {
            return Err(WorkerError::invalid_params(
                "request scope conflicts with authenticated process scope",
                None,
            ));
        }
        (env_owner, env_workspace)
    } else if trusted_caller {
        (
            owner.ok_or_else(|| {
                WorkerError::invalid_params("authenticated owner is required", None)
            })?,
            workspace_id
                .ok_or_else(|| WorkerError::invalid_params("workspace_id is required", None))?,
        )
    } else if legacy {
        (
            owner.unwrap_or_else(|| "legacy".into()),
            workspace_id.unwrap_or_else(|| "global".into()),
        )
    } else {
        return Err(WorkerError::invalid_params(
            "memery-worker requires FM_OWNER+FM_WORKSPACE_ID or FM_SCOPE_AUTHORITY=trusted-caller",
            None,
        ));
    };

    let mut scope = memery_core::graph::GraphScope::new(owner, workspace_id)
        .map_err(|message| WorkerError::invalid_params(message, None))?;
    scope.include_global = include_global;
    Ok(scope)
}


// App Code workspaces are independently admitted by the existing Files
// authority; the fixed process scope authenticates the owner, not that path.
fn code_binding_scope(binding:&memery_core::code::WorkspaceBinding,cleanup:bool)->Result<memery_core::graph::GraphScope,WorkerError>{
    let fixed=std::env::var("FM_OWNER").ok().is_some_and(|v|!v.trim().is_empty())
        && std::env::var("FM_WORKSPACE_ID").ok().is_some_and(|v|!v.trim().is_empty());
    if binding.origin!="app" || !fixed {return request_scope(Some(binding.owner.clone()),Some(binding.workspace_id.clone()),false);}
    let unavailable=||WorkerError::invalid_params("Code workspace authority unavailable",None);
    let outer=request_scope(Some(binding.owner.clone()),None,false)?;
    if outer.owner!=binding.owner{return Err(unavailable());}
    if cleanup {binding.validate_cleanup().map_err(|_|unavailable())?;} else {binding.validate().map_err(|_|unavailable())?;}
    let mut scope=memery_core::graph::GraphScope::new(outer.owner,binding.workspace_id.clone()).map_err(|_|unavailable())?;
    scope.include_global=false;
    Ok(scope)
}

#[derive(Debug, Deserialize)]
struct CaptureParams {
    #[serde(default)]
    content: Option<String>,
    /// Which side of the conversation `content` belongs to: "user"
    /// (default) or "assistant". Callers that capture per-message (mimo)
    /// set this so assistant text is never mislabeled as user text.
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    session_key: Option<String>,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    workspace_path: Option<String>,
    #[serde(default)]
    source: Option<String>,
    #[serde(default)]
    source_type: Option<String>,
    #[serde(default)]
    category: Option<String>,
    #[serde(default)]
    metadata: Option<serde_json::Value>,
    /// A complete turn can be sent atomically with these fields. Legacy
    /// content+role remains accepted for deliberate/manual callers.
    #[serde(default)]
    user_text: Option<String>,
    #[serde(default)]
    assistant_text: Option<String>,
    /// raw_only | candidate | review_only | manual
    #[serde(default)]
    capture_mode: Option<String>,
    #[serde(default)]
    source_event_id: Option<String>,
    #[serde(default)]
    source_message_ids: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct RecallParams {
    query: String,
    #[serde(default)]
    mode: Option<String>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    top_k: Option<usize>,
    #[serde(default)]
    tier: Option<String>,
}

#[derive(Debug, Deserialize)]
struct SearchParams {
    query: String,
    #[serde(default)]
    kind: Option<String>,
    #[serde(default)]
    scene: Option<String>,
    #[serde(default)]
    tier: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    owner: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GroomParams {
    op: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    dry_run: Option<bool>,
}

#[derive(Debug, Deserialize)]
struct MemoryMutationParams {
    id: String,
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    category: Option<String>,
    #[serde(default)]
    pinned: Option<bool>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ResolveMemoryParams {
    /// The kind=unknown record to close.
    id: String,
    /// Curated record id whose content answers the question, if known.
    #[serde(default)]
    resolved_by: Option<String>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MemoryGetParams {
    id: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MemoryListParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    cursor: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MemoryAccessParams {
    ids: Vec<String>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CandidateListParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnerPurgeStatsRequest {version:u32,owner:String,account_id:String}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct QuestionDigestRequest {version:u32,owner:String,workspace_id:String,account_id:String}

#[derive(Debug, Deserialize)]
#[serde(tag = "phase", rename_all = "snake_case", deny_unknown_fields)]
enum HostCandidateReviewParams {
    OwnerPurgeStats {owner:String,workspace_id:String,request:OwnerPurgeStatsRequest},
    QuestionDigest {owner:String,workspace_id:String,request:QuestionDigestRequest},
    PairedErasedFact {owner:String,workspace_id:String,request:memery_core::repository::PairedErasedFactRequest},
    ErasedPairFence {request:memery_core::repository::PendingPairFenceRequest},
    CodeLibraryObserve {owner:String,workspace_id:String,request:serde_json::Value},
    CodeManifestCompare {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentSegments {owner:String,workspace_id:String,texts:Vec<String>},
    DocumentStructureWrite {owner:String,workspace_id:String,request:memery_core::repository::DocumentStructureRequest},
    DocumentStructureRead {owner:String,workspace_id:String,request:memery_core::repository::DocumentStructureRequest},
    DocumentUpsert {owner:String,workspace_id:String,request:memery_core::repository::DocumentUpsertRequest},
    DocumentUpsertOriginal {owner:String,workspace_id:String,request:memery_core::repository::DocumentOriginalRequest},
    DocumentSourceLookup {owner:String,workspace_id:String,request:memery_core::repository::DocumentSourceLookupRequest},
    DocumentVectorSnapshot {owner:String,workspace_id:String,request:memery_core::repository::DocumentVectorSnapshotRequest},
    DocumentVectorProfileSearch {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorSelectedStart {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorSelectedStatus {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorSelectedCommit {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorSelectedSearch {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorProfileStart {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorProfileStatus {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorProfileCommit {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorSearch {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorHead {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorText {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentPrepare {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentClaim {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentStatus {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentCancel {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorBatchBegin {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorBatchClaim {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorBatchStatus {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorBatchComplete {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorBatchFinalize {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentBegin {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentAppend {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentText {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorIntentFinalize {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorBegin {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorAppend {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentVectorFinalize {owner:String,workspace_id:String,request:serde_json::Value},
    DocumentRemove {owner:String,workspace_id:String,request:memery_core::repository::DocumentRemoveRequest},
    DocumentRemoveOriginal {owner:String,workspace_id:String,request:memery_core::repository::DocumentOriginalRequest},
    S02Capability {owner:String,workspace_id:String},
    S02OverrideCapability {owner:String,workspace_id:String},
    S02OverrideApply {owner:String,workspace_id:String,request:serde_json::Value},
    S02OverrideStatus {owner:String,workspace_id:String,account_id:String,request:serde_json::Value,current_policy:serde_json::Value},
    S02Config {owner:String,workspace_id:String,request:serde_json::Value},
    S02Selector {owner:String,workspace_id:String,request:serde_json::Value},
    S02Snapshot {owner:String,workspace_id:String,request:serde_json::Value},
    S02Apply {owner:String,workspace_id:String,request:serde_json::Value},
    S02Status {owner:String,workspace_id:String,account_id:String,request:serde_json::Value,current_policy:serde_json::Value},
    S02RenameAssessment {request:memery_core::store::sqlite::GuardedS02RenameAssessment},
    ProfileStatus,
    NativeMemoryOpsCapability {owner:String,workspace_id:String},
    GuardedBatchForgetPreview {request:memery_core::store::sqlite::GuardedBatchForgetRequest},
    GuardedBatchForget {request:memery_core::store::sqlite::GuardedBatchForgetRequest},
    GuardedBatchForgetStatus {request:memery_core::store::sqlite::GuardedBatchForgetRequest},
    GuardedBatchForgetRestore {request:memery_core::store::sqlite::GuardedBatchForgetRequest},
    NativeMemoryOpsBatchPreview {request:memery_core::store::sqlite::BatchSelector},
    NativeMemoryOpsBatchApply {request:memery_core::store::sqlite::BatchApplyRequest},
    NativeMemoryOpsBatchStatus {request:memery_core::store::sqlite::BatchStatusRequest},
    HelpedCapability {owner:String,workspace_id:String},
    FoundFeaturesCapability {owner:String,workspace_id:String},
    FoundFeaturesPage {request:memery_core::repository::HelpedSelectorRequest},
    ExplicitAddCapability {owner:String,workspace_id:String},
    HelpedSelectorPage {request:memery_core::repository::HelpedSelectorRequest},
    HelpedObservation {request:memery_core::repository::HelpedObservation},
    HelpedStatus {request:memery_core::repository::HelpedObservation,current_authority:memery_core::repository::ObservationOriginAuthority},
    ResetRetirementCapability {owner:String,workspace_id:String},
    ResetTerminalS08Capability {owner:String,workspace_id:String},
    AnalysisSnapshotCapability {owner:String,workspace_id:String,request_version:Option<u32>},
    AnalysisSnapshotPublish {request:memery_core::repository::AnalysisSnapshotWireRequest},
    AnalysisSnapshotStatus {request:memery_core::repository::AnalysisSnapshotStatusRequest},
    AccountValueSuccessorCapability {owner:String,workspace_id:String},
    AccountValueSuccessorPreview {request:memery_core::repository::AccountValueSuccessorPreview},
    AccountValueSuccessorApply {request:memery_core::repository::AccountValueSuccessorRequest},
    AccountValueSuccessorStatus {request:memery_core::repository::AccountValueSuccessorStatus},
    StandingGuidanceCapability {owner:String,workspace_id:String},
    StandingGuidancePreview {request:memery_core::repository::StandingGuidancePreview},
    StandingGuidanceRead {request:memery_core::repository::StandingGuidancePreview},
    StandingGuidanceApply {request:memery_core::repository::StandingGuidanceReviewRequest},
    StandingGuidanceStatus {request:memery_core::repository::StandingGuidanceStatus},
    StandingGuidanceSelect {request:memery_core::repository::StandingGuidanceSelection},
    StandingGuidanceRevalidate {request:memery_core::repository::StandingGuidanceRevalidation},
    AccountCandidateApply { request:memery_core::repository::AccountCandidateReviewRequest },
    DomainDefinitionReview { request:memery_core::repository::DomainDefinitionReviewRequest },
    DomainDefinitionStatus { owner:String, workspace_id:String },
    MetadataAssignmentStatus { owner:String,workspace_id:String },
    MetadataAssignmentApply { request:memery_core::repository::MetadataAssignmentApply },
    MetadataRead { request:memery_core::repository::MetadataReadRequest },
    DomainDefinitionList { request:memery_core::repository::MetadataReadRequest },
    AccountCandidateStatus { owner:String, workspace_id:String },
    PreparedKnowledgeRead { request:memery_core::repository::PreparedKnowledgeRead },
    PolicyPublicationStatus { owner:String, workspace_id:String },
    S08ReviewPreview { owner:String, workspace_id:String, candidate_id:String },
    S08ReviewApply { owner:String, workspace_id:String, candidate_id:String, candidate_digest:String, request_key:String, reason:String, policy_projection:Option<memery_core::repository::PolicyPublicationRefs> },
    S08ReviewStatus { owner:String, workspace_id:String, candidate_id:String, candidate_digest:String, request_key:String, reason:String },
    S08RulesOnce { owner:String, workspace_id:String, request:memery_core::repository::S08RulesOnce },
    S08PolicySnapshots { owner:String, workspace_id:String, selection:memery_core::repository::S08RulesSelection, policy:memery_core::repository::PolicyPublicationRefs },
    S08PolicyCapability {owner:String,workspace_id:String},
    S08LastRetainedRun {request:memery_core::repository::S08LastRetainedRequest},
    M3Rank {request:memery_core::repository::M3RankRequest},
    S08OriginalScoped {owner:String,workspace_id:String,account_id:String,policy:memery_core::repository::PolicyPublicationRefs,kind:String,request:serde_json::Value},
    ConnectionPredicateOptions {owner:String,workspace_id:String,account_id:String,policy:memery_core::repository::PolicyPublicationRefs},
    ConnectionReviewManifest {owner:String,workspace_id:String,account_id:String,candidate_id:String,policy:memery_core::repository::PolicyPublicationRefs},
    ConnectionReviewOriginal {owner:String,workspace_id:String,account_id:String,candidate_id:String,#[serde(default)] candidate_sha256:Option<String>,policy:memery_core::repository::PolicyPublicationRefs},
    ConnectionReviewApply {owner:String,workspace_id:String,request:serde_json::Value},
    S08PolicyReviewManifest {owner:String,workspace_id:String,account_id:String,candidate_id:String,output_kind:String,policy:memery_core::repository::PolicyPublicationRefs},
    S08PolicyReviewRejectPreview {owner:String,workspace_id:String,account_id:String,candidate_id:String,output_kind:String,policy:memery_core::repository::PolicyPublicationRefs},
    S08PolicyReviewOriginal {owner:String,workspace_id:String,account_id:String,candidate_id:String,#[serde(default)] candidate_sha256:Option<String>,output_kind:String,policy:memery_core::repository::PolicyPublicationRefs},
    S08PolicyReviewApply {owner:String,workspace_id:String,output_kind:String,request:serde_json::Value},
    S02ImportanceResolve {owner:String,workspace_id:String,account_id:String,#[serde(default)] curated_id:Option<String>,#[serde(default)] block_id:Option<String>,#[serde(default)] expected_revision:Option<i64>,policy:memery_core::repository::PolicyPublicationRefs},
    S02ImportanceContext {owner:String,workspace_id:String,account_id:String,candidate_id:String,policy:memery_core::repository::PolicyPublicationRefs},
    S02ImportanceReviewContext {owner:String,workspace_id:String,account_id:String,candidate_id:String,policy:memery_core::repository::PolicyPublicationRefs},
    S02ImportanceTargetManifest {owner:String,workspace_id:String,account_id:String,candidate_id:String,policy:memery_core::repository::PolicyPublicationRefs},
    S02ImportanceOriginal {owner:String,workspace_id:String,policy:memery_core::repository::PolicyPublicationRefs,request:serde_json::Value},
    S02ImportanceApply {owner:String,workspace_id:String,request:serde_json::Value},
    S02ImportancePrepare {owner:String,workspace_id:String,request:serde_json::Value},
    S02ImportanceCapability {owner:String,workspace_id:String},
    S08ReviewKind {owner:String,workspace_id:String,account_id:String,candidate_id:String,policy:memery_core::repository::PolicyPublicationRefs},
    S05ReserveReadiness {owner:String,workspace_id:String,request:serde_json::Value},
    S05ReservePrepare {owner:String,workspace_id:String,request:serde_json::Value},
    S05Reserve {owner:String,workspace_id:String,request:serde_json::Value},
    S05ReserveOriginal {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftCapability {version:u32,owner:String,workspace_id:String,account_id:String},
    SkillDraftPrepare {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftPrepareOriginal {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftReserve {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftReserveOriginal {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftDispatch {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftOriginal {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftPreparedCancel {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftTerminal {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftRevoke {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftResultOriginal {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftRetire {owner:String,workspace_id:String,request:serde_json::Value},
    SkillDraftRetireOriginal {owner:String,workspace_id:String,request:serde_json::Value},
    S08ModelPrepare {owner:String,workspace_id:String,request:memery_core::repository::S08ModelPrepareRequest},
    S08ModelPrepareOriginal {owner:String,workspace_id:String,request:memery_core::repository::S08ModelPrepareRequest},
    S08ModelReserve {owner:String,workspace_id:String,request:memery_core::repository::S08ModelReserveRequest},
    S08ModelReserveOriginal {owner:String,workspace_id:String,request:memery_core::repository::S08ModelReserveRequest},
    S08ModelDispatch {owner:String,workspace_id:String,request:memery_core::repository::S08ModelAttemptRequest},
    S08ModelOriginal {owner:String,workspace_id:String,request:memery_core::repository::S08ModelAttemptRequest},
    S08PolicyCommit {owner:String,workspace_id:String,request:memery_core::repository::S08PolicyCommitRequest},
    S08PolicyTerminal {owner:String,workspace_id:String,request:memery_core::repository::S08PolicyTerminalRequest},
    S08PolicyPreparedCancel {owner:String,workspace_id:String,request:memery_core::repository::S08ModelPrepareRequest},
    S08PolicyResultOriginal {owner:String,workspace_id:String,request:memery_core::repository::S08PolicyCommitRequest},
    JournalDeltaStatus {request:memery_core::repository::JournalDeltaStatusRequest},
    DerivedCompaction { request:memery_core::repository::DerivedCompactionRequest },
    GuardedResetCommit { version:u32, owner:String, workspace_id:String, operation_id:String,
        account_id:String, components:Vec<String>, expected_counts:serde_json::Value, #[serde(default)] queued_retired:bool, #[serde(default)] selective_manifest_sha256:Option<String> },
    GuardedResetStamp {version:u32,owner:String,workspace_id:String,operation_id:String,account_id:String,components:Vec<String>,expected_counts:serde_json::Value,#[serde(default)] queued_retired:bool,#[serde(default)] selective_manifest_sha256:Option<String>},
    GuardedResetCleanup {version:u32,owner:String,workspace_id:String,operation_id:String,account_id:String,
        components:Vec<String>,expected_counts:serde_json::Value,#[serde(default)] queued_retired:bool,#[serde(default)] selective_manifest_sha256:Option<String>,
        path:String,device:u64,inode:u64,database_id:String},
    QualifiedSearch { owner:String, workspace_id:String, query:String, limit:usize },
    S07RecallStart { owner:String, workspace_id:String, invocation:memery_core::repository::FoundInvocation, query:String, limit:usize, budget_ms:u64 },
    S07RevalidateStart { owner:String, workspace_id:String, invocation:memery_core::repository::FoundInvocation, selected:Vec<memery_core::repository::S07SelectedRef>, budget_ms:u64 },
    S07RecallPoll { owner:String, workspace_id:String, invocation:memery_core::repository::FoundInvocation, token:String },
    S07RecallCancel { owner:String, workspace_id:String, invocation:memery_core::repository::FoundInvocation, token:String },
    ConsolidationAdmissionsPage { owner:String, workspace_id:String, source:memery_core::repository::HostArchiveSourceRef, authority:memery_core::repository::ObservationOriginAuthority, after:Option<String>, limit:usize },
    ObservationExportPage { owner:String, workspace_id:String, source:memery_core::repository::HostArchiveSourceRef, authority:memery_core::repository::ObservationOriginAuthority, kind:String, after:Option<String>, limit:usize },
    ExternalHistoryClaim { owner:String, workspace_id:String, claim:memery_core::repository::ExternalHistoryClaim },
    ExternalHistoryPage { owner:String, workspace_id:String, after:Option<String>, limit:usize },
    OwnerPurgeApply { request:memery_core::store::sqlite::GuardedOwnerPurgeRequest },
    OwnerPurgeStatus { request:memery_core::store::sqlite::GuardedOwnerPurgeRequest },
    OwnerPurgeStamp { request:memery_core::store::sqlite::GuardedOwnerPurgeRequest },
    OwnerTransitionApply { request:memery_core::store::sqlite::GuardedOwnerRenameRequest },
    OwnerTransitionStatus { request:memery_core::store::sqlite::GuardedOwnerRenameRequest },
    OwnerTransitionStamp { request:memery_core::store::sqlite::GuardedOwnerRenameRequest },
    OwnerTransitionCacheCleanup { request:memery_core::store::sqlite::GuardedOwnerRenameRequest },
    OwnerPurgeCacheCleanup { request:memery_core::store::sqlite::GuardedOwnerPurgeRequest },
    ContextAdmittedObservation { request:memery_core::repository::ContextAdmittedObservation },
    ContextAdmittedObservationRead { owner:String, workspace_id:String, event_id:String, authority:memery_core::repository::ObservationOriginAuthority },
    AdmittedObservation { request:memery_core::repository::AdmittedObservation },
    AdmittedObservationRead { owner:String, workspace_id:String, event_id:String, authority:memery_core::repository::ObservationOriginAuthority },
    FoundObservation { request:memery_core::repository::FoundObservation },
    FoundObservationRead { owner:String, workspace_id:String, event_id:String, authority:memery_core::repository::ObservationOriginAuthority },
    ObservationRenew { request:memery_core::repository::HostObservationRenewRequest },
    ObservationFence { version:u32, owner:String, workspace_id:String, authority:memery_core::repository::ObservationOriginAuthority,
        source:memery_core::repository::HostArchiveSourceRef, #[serde(default)] rename_operation_id:Option<String> },
    GuardedFullMemoryPreview {version:u32,owner:String,workspace_id:String,account_id:String,components:Vec<String>},
    GuardedSelectiveIngestPreview {version:u32,owner:String,workspace_id:String,account_id:String,components:Vec<String>},
    SelectiveIngestPartition {version:u32,owner:String,workspace_id:String,account_id:String,partition_sha256:String},
    PairedReviewPage {owner:String,workspace_id:String,#[serde(default)] after:Option<String>,limit:u32},
    PairedReviewPreview {owner:String,workspace_id:String,candidate_id:String},
    PairedOriginalPage {owner:String,workspace_id:String,account_id:String,#[serde(default)] after:Option<String>,limit:u32},
    PairedReviewOriginal {owner:String,workspace_id:String,account_id:String,candidate_id:String},
    PairedReviewApply {request:memery_core::repository::AssistantInterpretationReviewRequest},
    PairedReviewStatus {request:memery_core::repository::AssistantInterpretationReviewRequest},
    PairedSourceCandidate { request:memery_core::repository::AssistantInterpretationCandidateRequest },
    PairedSourceFence { request:memery_core::repository::AssistantInterpretationFenceRequest },
    PairedResetFence { request:memery_core::repository::AssistantInterpretationFenceRequest, reset:memery_core::repository::ResetFenceBinding },
    PairedResetFenceRead { request:memery_core::repository::AssistantInterpretationFenceRequest, reset:memery_core::repository::ResetFenceBinding },
    SourceCandidate { request:memery_core::repository::HostArchiveCandidateRequest },
    SourcePublicationOriginal { request:memery_core::repository::HostSourcePublicationOriginalRequest },
    SourceRenew { request:memery_core::repository::HostArchiveRenewRequest },
    SourceProject { version:u32, owner:String, workspace_id:String, job_id:String, source:memery_core::repository::HostArchiveSourceRef, policy_fingerprint:String },
    PublishedLifecycleApply { request:memery_core::repository::PublishedLifecycleRequest },
    QuestionSemanticsStatus { owner:String, workspace_id:String },
    ExplicitAdd { request:memery_core::store::sqlite::ExplicitAddRequest },
    KnowledgeUpdateBatchPreview {request:memery_core::store::sqlite::UpdateBatchPreview},
    KnowledgeUpdateBatchApply {request:memery_core::store::sqlite::UpdateBatchApply},
    KnowledgeUpdateBatchStatus {request:memery_core::store::sqlite::UpdateBatchStatus},
    PreextractedHostAnchorStage {request:memery_core::store::sqlite::HostAnchorStageRequest},
    PreextractedHostAnchorStatus {request:memery_core::store::sqlite::HostAnchorStatusRequest},
    PreextractedStage { request:memery_core::store::sqlite::PreextractedStageRequest },
    PreextractedList { request:memery_core::store::sqlite::PreextractedListRequest },
    PreextractedReviewPreview { request:memery_core::store::sqlite::PreextractedSelector },
    PreextractedReview { request:memery_core::store::sqlite::PreextractedReviewRequest },
    ExplicitAddStatus { request:memery_core::store::sqlite::ExplicitAddStatusRequest },
    ExplicitAddReviewPreview { request:memery_core::store::sqlite::ExplicitAddReviewSelector },
    ExplicitAddReviewEdit { request:memery_core::store::sqlite::ExplicitAddReviewEditRequest },
    ExplicitAddReviewApply { request:memery_core::store::sqlite::ExplicitAddReviewApplyRequest },
    ExplicitAddReviewStatus { request:memery_core::store::sqlite::ExplicitAddReviewApplyRequest },
    ExplicitAddReviewOriginal { request:memery_core::store::sqlite::ExplicitAddReviewSelector },
    ImportStagedReviewPreview { request:memery_core::store::sqlite::ExplicitAddReviewSelector },
    ImportStagedReviewEdit { request:memery_core::store::sqlite::ExplicitAddReviewEditRequest },
    ImportStagedReviewOriginal { request:memery_core::store::sqlite::ExplicitAddReviewSelector },
    ImportStagedReviewStatus { request:memery_core::store::sqlite::ExplicitAddReviewApplyRequest },
    ImportStagedReviewApply { request:memery_core::store::sqlite::ExplicitAddReviewApplyRequest },
    ImportReviewApply { request:memery_core::store::sqlite::ImportReviewSelector },
    ImportReviewStatus { request:memery_core::store::sqlite::ImportReviewSelector },
    ExplicitAddMissingOriginalPage { request:memery_core::store::sqlite::ExplicitAddMissingPageRequest },
    ExplicitAddMissingTerminalPage { request:memery_core::store::sqlite::ExplicitAddMissingPageRequest },
    ImportMissingOriginalPage { request:memery_core::store::sqlite::ExplicitAddMissingPageRequest },
    ImportMissingTerminalPage { request:memery_core::store::sqlite::ExplicitAddMissingPageRequest },
    PhaseRootObservationFence { request:memery_core::repository::PhaseRootObservationFenceRequest },
    PhaseRootObservationRenew { request:memery_core::repository::PhaseRootObservationRenewRequest },
    PhaseRootFence { request:memery_core::repository::PhaseRootFenceRequest },
    PhaseRootRenew { request:memery_core::repository::PhaseRootRenewRequest },
    PendingPairFence { request:memery_core::repository::PendingPairFenceRequest },
    TerminalPairFence { request:memery_core::repository::PendingPairFenceRequest },
    PendingPairRebind { request:memery_core::repository::PendingPairRebindRequest },
    PendingPairOriginal { request:memery_core::repository::PendingPairRebindRequest },
    PairedPublicationOriginal { request:memery_core::repository::PairedPublicationStatusRequest },
    PairedRenameFence { request:memery_core::repository::PairedRenameFenceRequest },
    PairedRenameRenew { request:memery_core::repository::PairedRenameRenewRequest },
    ConfirmedQuestionApply { request:memery_core::repository::ConfirmedQuestionRequest },
    QuestionSeedBootstrap { request:memery_core::repository::QuestionSeedBootstrapRequest },
    QuestionSeedStatus { request:memery_core::repository::QuestionSeedStatusRequest },
    QueuedOriginPreflight { version:u32, owner:String, workspace_id:String, account_id:String, sources:Vec<memery_core::repository::QueuedArchiveSourceRef> },
    QueuedOriginFence { version:u32, owner:String, workspace_id:String, source:memery_core::repository::QueuedArchiveSourceRef, #[serde(default)] reset:Option<memery_core::repository::ResetFenceBinding> },
    QueuedOriginResetFenceRead {version:u32,owner:String,workspace_id:String,source:memery_core::repository::QueuedArchiveSourceRef,reset:memery_core::repository::ResetFenceBinding},
    SourceFence { version:u32, owner:String, workspace_id:String, job_id:String, source:memery_core::repository::HostArchiveSourceRef, policy_fingerprint:String, #[serde(default)] rename_operation_id:Option<String> },
    SourceResetFence { version:u32, owner:String, workspace_id:String, job_id:String, source:memery_core::repository::HostArchiveSourceRef, policy_fingerprint:String, reset_operation_id:String, reset:memery_core::repository::ResetFenceBinding },
    ObservationResetFence { version:u32, owner:String, workspace_id:String, authority:memery_core::repository::ObservationOriginAuthority, source:memery_core::repository::HostArchiveSourceRef, reset_operation_id:String, reset:memery_core::repository::ResetFenceBinding },
    SourceResetFenceRead {version:u32,owner:String,workspace_id:String,job_id:String,policy_fingerprint:String,source:memery_core::repository::HostArchiveSourceRef,reset:memery_core::repository::ResetFenceBinding},
    ObservationResetFenceRead {version:u32,owner:String,workspace_id:String,authority:memery_core::repository::ObservationOriginAuthority,source:memery_core::repository::HostArchiveSourceRef,reset:memery_core::repository::ResetFenceBinding},
    MirrorContext { owner:String, workspace_id:String, curated_id:String, #[serde(default)] account_id:Option<String>, #[serde(default)] question_answer_block_id:Option<String> },
    Preview { owner: String, workspace_id: String, candidate_id: String, reason: String },
    CandidateSource { owner:String, workspace_id:String, candidate_id:String },
    CandidateEmbedding { owner:String, workspace_id:String, candidate_id:String, candidate_digest:String },
    CandidateApplyPrepared { owner:String, workspace_id:String, candidate_id:String, candidate_digest:String,
        expected_v2_head:i64, selected_paths:Vec<String>, request_key:String, reason:String,
        projection:serde_json::Value, embedding:Option<Vec<f32>> },
    RejectPreview { owner:String, workspace_id:String, candidate_id:String },
    RejectApply { owner:String, workspace_id:String, candidate_id:String, candidate_digest:String,
                  association_sha256:String, source_account_id:String, source_chat_id:String, request_key:String, reason:String },
    Replay { owner: String, workspace_id: String, candidate_id: String, candidate_digest: String,
             expected_v2_head: i64, selected_paths: Vec<String>, request_key: String, reason: String },
    ReceiptStatus { owner: String, workspace_id: String, candidate_id: String, candidate_digest: String,
             expected_v2_head: i64, selected_paths: Vec<String>, request_key: String, reason: String },
    Apply { owner: String, workspace_id: String, candidate_id: String, candidate_digest: String,
            expected_v2_head: i64, selected_paths: Vec<String>, request_key: String, reason: String,
            projection: serde_json::Value },
    MirrorPreview { owner:String, workspace_id:String, candidate_id:String, origin_request_key:String,
                    prior_receipt_id:String, expected_revision:i64, request_key:String, reason:String,
                    selected_paths:Vec<String>, payload:serde_json::Value },
    MirrorApply { owner:String, workspace_id:String, candidate_id:String, origin_request_key:String,
                  prior_receipt_id:String, expected_revision:i64, request_key:String, reason:String,
                  selected_paths:Vec<String>, operation_json:String, operation_sha256:String },
    MirrorReplay { owner:String, workspace_id:String, candidate_id:String, origin_request_key:String,
                   prior_receipt_id:String, expected_revision:i64, request_key:String, operation_json:String },
}

#[derive(Debug, Deserialize)]
struct CandidateReviewParams {
    id: String,
    accept: bool,
    reason: String,
    owner: String,
    workspace_id: String,
}

#[derive(Debug, Deserialize)]
struct CandidateUpdateParams {
    id: String,
    content: String,
    #[serde(default)]
    category: Option<String>,
    #[serde(default)]
    reason: Option<String>,
    owner: String,
    workspace_id: String,
}

#[derive(Debug, Deserialize)]
struct DigestParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct AuthoredSection {
    #[serde(default)]
    anchor: String,
    content: String,
}

#[derive(Debug, Deserialize)]
struct IngestAuthoredParams {
    /// Absolute path of the authored memory file this projection mirrors.
    source_path: String,
    /// Current sections of the file; an empty list deletes the projection.
    #[serde(default)]
    sections: Vec<AuthoredSection>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MemoryQualityParams {
    #[serde(default)]
    rebuild_graph_fts: Option<bool>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct OwnerLifecycleParams {
    action: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    new_owner: Option<String>,
    #[serde(default)]
    components: Vec<String>,
    #[serde(default)]
    expected_counts: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
struct MemoryRetentionParams {
    action: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    raw_days: Option<u32>,
    #[serde(default)]
    candidate_days: Option<u32>,
    #[serde(default)]
    curated_days: Option<u32>,
    #[serde(default)]
    clear_curated_days: bool,
    #[serde(default)]
    graph_days: Option<u32>,
    #[serde(default)]
    clear_graph_days: bool,
    #[serde(default)]
    recovery_seconds: Option<u32>,
    #[serde(default)]
    preview_token: Option<String>,
    #[serde(default)]
    operation_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MemoryForgetParams {
    action: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    selector_kind: Option<String>,
    #[serde(default)]
    selector: Option<String>,
    #[serde(default)]
    preview_token: Option<String>,
    #[serde(default)]
    tombstone_id: Option<String>,
    #[serde(default)]
    operation_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ScopedExportParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MemoryExplainParams {
    id: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct QuarantineMigrationParams {
    #[serde(default = "default_true")]
    dry_run: bool,
    #[serde(default)]
    reason: Option<String>,
}

fn default_true() -> bool {
    true
}

fn forget_selector(
    kind: Option<&str>,
    value: Option<String>,
) -> Result<ForgetSelector, WorkerError> {
    let value = value
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| WorkerError::invalid_params("selector is required", None))?;
    match kind.unwrap_or("record_id") {
        "record_id" => Ok(ForgetSelector::RecordId(value)),
        "source_uri" => Ok(ForgetSelector::SourceUri(value)),
        "source_message_id" => Ok(ForgetSelector::SourceMessageId(value)),
        _ => Err(WorkerError::invalid_params(
            "selector_kind must be record_id|source_uri|source_message_id",
            None,
        )),
    }
}

fn parse_memory_kind(value: &str) -> Option<MemoryKind> {
    match value {
        "persona" | "identity" => Some(MemoryKind::Persona),
        "episodic" | "event" => Some(MemoryKind::Episodic),
        "instruction" | "preference" => Some(MemoryKind::Instruction),
        "fact" | "contact" | "project" | "goal" => Some(MemoryKind::Fact),
        "fabric" => Some(MemoryKind::Fabric),
        "wiki" | "reference" => Some(MemoryKind::Wiki),
        "raw" => Some(MemoryKind::Raw),
        "unknown" | "question" => Some(MemoryKind::Unknown),
        _ => None,
    }
}

#[derive(Debug, Deserialize)]
struct GraphUpsertParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    nodes: Vec<memery_core::graph::GraphNodeInput>,
    #[serde(default)]
    edges: Vec<memery_core::graph::GraphEdgeInput>,
    #[serde(default)]
    cues: Vec<memery_core::graph::GraphCueInput>,
}

#[derive(Debug, Deserialize)]
struct GraphWalkParams {
    /// Evidence budgets, seeds and intent for op=reconstruct.
    #[serde(default)]
    reconstruction: Option<memery_core::retrieval::graph_reconstruction::ReconstructionRequest>,
    /// One of: overview | cues | tags | expand | fetch | trace | rank | reconstruct
    op: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default = "default_true")]
    include_global: bool,
    /// For op=cues: free text to match against cue keywords.
    #[serde(default)]
    query: Option<String>,
    /// For tags/expand/fetch/trace: the node id to operate on.
    #[serde(default)]
    node_id: Option<String>,
    /// For expand: optional tag filter. For trace: unused.
    #[serde(default)]
    tag: Option<String>,
    /// For expand: "out" | "in" | omitted for both.
    #[serde(default)]
    direction: Option<String>,
    /// For trace: optional destination node id.
    #[serde(default)]
    dst_id: Option<String>,
    #[serde(default)]
    max_depth: Option<usize>,
    /// For action=impact: maximum returned files; the response always carries
    /// the exact total and a truncated flag.
    #[serde(default)]
    limit: Option<usize>,
    /// For op=entities: alias (or canonical label) to look up; at least one of alias/entity_type.
    #[serde(default)]
    alias: Option<String>,
    /// For op=entities: entity type to look up.
    #[serde(default)]
    entity_type: Option<String>,
    /// For op=expand: identify the node by kind+name instead of node_id (the id is derived inside the caller's scope).
    #[serde(default)]
    node_kind: Option<String>,
    #[serde(default)]
    node_name: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CodeIndexParams {
    /// index | status | stale | remove | impact
    action: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    /// Absolute path of the codebase root (the opt-in namespace).
    #[serde(default)]
    path: String,
    #[serde(default)] root: Option<String>,
    #[serde(default)] owner_subject_id: Option<String>,
    #[serde(default)] location_id: Option<String>,
    #[serde(default)] workspace_revision: Option<i64>,
    #[serde(default)] policy_generation: Option<i64>,
    #[serde(default)] origin: Option<String>,
    #[serde(default)] subscriber_id: Option<String>,
    #[serde(default)] authority_expires_unix_ms: Option<i64>,
    #[serde(default)] removal_confirmation: Option<String>,
    #[serde(default)] replace_stale_root: bool,
    /// For action=impact: file path relative to the codebase root.
    #[serde(default)]
    rel_path: Option<String>,
    #[serde(default)]
    max_depth: Option<usize>,
    #[serde(default)]
    limit: Option<usize>,
}

#[derive(Debug,Deserialize)]
#[serde(deny_unknown_fields)]
struct CodeIndexLimitsParams {max_files:usize,max_source_bytes:usize,max_file_bytes:usize,#[serde(default="memery_core::code_index_limits::default_generation_bytes")]max_generation_bytes:u64}
#[derive(Debug,Deserialize)]
#[serde(tag="action",rename_all="snake_case",deny_unknown_fields)]
enum CodeIndexSchedulerParams{Status{origin:String},Configure{origin:String,revision:u64,max_threads:u8,#[serde(default)] limits:Option<CodeIndexLimitsParams>}}

impl CodeIndexSchedulerParams {
    fn request(self) -> memery_core::code_index_scheduler::SchedulerRequest {
        match self {
            Self::Status { origin } => memery_core::code_index_scheduler::SchedulerRequest::Status { origin },
            Self::Configure { origin, revision, max_threads, limits } => match limits {
                Some(limits)=>memery_core::code_index_scheduler::SchedulerRequest::ConfigureResources{origin,revision,max_threads,
                    limits:memery_core::code_index_limits::IndexLimits{max_files:limits.max_files,max_source_bytes:limits.max_source_bytes,max_file_bytes:limits.max_file_bytes,max_generation_bytes:limits.max_generation_bytes}},
                None=>memery_core::code_index_scheduler::SchedulerRequest::Configure { origin, revision, max_threads },
            },
        }
    }
}

#[cfg(test)]
mod index_scheduler_wire_tests {
    use super::*;
    #[test]
    fn scheduler_wire_rejects_unknown_fields_and_non_integer_limits() {
        for raw in [
            serde_json::json!({"action":"status","origin":"app","revision":1}),
            serde_json::json!({"action":"configure","origin":"app","revision":1,"max_threads":true}),
            serde_json::json!({"action":"configure","origin":"app","revision":1,"max_threads":1.5}),
            serde_json::json!({"action":"configure","origin":"app","revision":1,"max_threads":null}),
            serde_json::json!({"action":"configure","origin":"app","revision":1,"max_threads":"2"}),
        ] { assert!(serde_json::from_value::<CodeIndexSchedulerParams>(raw).is_err()); }
    }
    #[test]
    fn scheduler_app_only_and_strict_ranges_are_checked_before_mutation() {
        for (origin, revision, max_threads) in [("agent",1,2),("broker",1,2),("app",0,2),("app",1,0),("app",1,33),("app",1u64<<53,2)] {
            let params:CodeIndexSchedulerParams=serde_json::from_value(serde_json::json!({"action":"configure","origin":origin,"revision":revision,"max_threads":max_threads})).unwrap();
            assert!(params.request().validate().is_err());
        }
        for max_threads in [1,2,32] {
            let params:CodeIndexSchedulerParams=serde_json::from_value(serde_json::json!({"action":"configure","origin":"app","revision":1,"max_threads":max_threads})).unwrap();
            assert!(params.request().validate().is_ok());
        }
    }
    #[test]
    fn scheduler_status_wire_has_exact_python_readback_contract() {
        let scheduler=memery_core::code_index_scheduler::IndexScheduler::default();
        let params:CodeIndexSchedulerParams=serde_json::from_value(serde_json::json!({"action":"status","origin":"app"})).unwrap();
        let status=serde_json::to_value(scheduler.request(&params.request()).unwrap()).unwrap();
        assert_eq!(status,serde_json::json!({"schema":"open-clank.index-scheduler/v1","revision":0,"effective_revision":0,"desired":2,"effective":2,"active":0,"queued":0,"drain_pending":false}));
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CodeQueryParams {
    action:String,
    binding:serde_json::Value,
    #[serde(default)] query:Option<serde_json::Value>,
    #[serde(default)] token:Option<String>,
    /// Private Files-byte ExactText validation, never a model-visible input.
    #[serde(default)] source_claim:Option<serde_json::Value>,
    #[serde(default)] read_fence_only:bool,
    #[serde(default)] selection_policy_fence:bool,
    /// App-only map/selection on the existing owned read-job lifecycle.
    #[serde(default)] visual_request:Option<serde_json::Value>,
    #[serde(default)] retrospective_request:Option<serde_json::Value>,
    #[serde(default)] topology_request:Option<serde_json::Value>,
    #[serde(default)] topology_cursor:Option<String>,
    #[serde(default, skip_serializing)] admitted_code_vectors:Option<serde_json::Value>,
    #[serde(default, skip_serializing)] code_vector_index:Option<serde_json::Value>,
    #[serde(default, skip_serializing)] scip_request:Option<serde_json::Value>,
}

fn topology_base64(data:&[u8])->String {const TABLE:&[u8;64]=b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";let mut out=String::with_capacity(data.len().div_ceil(3)*4);for c in data.chunks(3){let a=c[0];let b=c.get(1).copied().unwrap_or(0);let d=c.get(2).copied().unwrap_or(0);out.push(TABLE[(a>>2) as usize] as char);out.push(TABLE[(((a&3)<<4)|(b>>4)) as usize] as char);out.push(if c.len()>1 {TABLE[(((b&15)<<2)|(d>>6)) as usize] as char}else{'='});out.push(if c.len()>2 {TABLE[(d&63) as usize] as char}else{'='});}out}

fn visual_request(params:&CodeQueryParams,origin:&str)->Result<Option<memery_core::code_visual::VisualRequest>,WorkerError>{
    let Some(raw)=&params.visual_request else{return Ok(None)};
    let invalid=||WorkerError::invalid_params("Invalid private visual request",None);
    if params.action!="start"||origin!="app"||params.query.is_some()||params.source_claim.is_some()||params.read_fence_only||params.selection_policy_fence||params.topology_request.is_some(){return Err(invalid());}
    // Count before typed deserialization without allocating an unbounded copy.
    struct Bound(usize);
    impl std::io::Write for Bound {
        fn write(&mut self,bytes:&[u8])->std::io::Result<usize>{
            self.0=self.0.checked_add(bytes.len()).filter(|n|*n<=memery_core::code_visual::MAX_REQUEST_BYTES).ok_or_else(||std::io::Error::other("visual request too large"))?;Ok(bytes.len())
        }
        fn flush(&mut self)->std::io::Result<()>{Ok(())}
    }
    serde_json::to_writer(&mut Bound(0),raw).map_err(|_|invalid())?;
    let request:memery_core::code_visual::VisualRequest=serde_json::from_value(raw.clone()).map_err(|_|invalid())?;
    match &request{memery_core::code_visual::VisualRequest::Map(q)=>q.validate(),memery_core::code_visual::VisualRequest::Selection(q)=>q.validate()}.map_err(|_|invalid())?;
    Ok(Some(request))
}

fn visual_delivery(token:&str,value:serde_json::Value,checked:Result<(),memery_core::code_query::QueryError>,cancel:&std::sync::atomic::AtomicBool)->serde_json::Value{
    // A cancel RPC can arrive during the awaited final check. Do not disclose
    // its payload just because the worker observed an earlier uncancelled flag.
    if cancel.load(std::sync::atomic::Ordering::Relaxed){return serde_json::json!({"state":"failed","token":token,"error":{"code":"cancelled","message":"Code query cancelled"}});}
    match checked{
        Ok(())=>serde_json::json!({"state":"completed","token":token,"result":value}),
        Err(error)=>serde_json::json!({"state":"failed","token":token,"error":error}),
    }
}

#[cfg(test)]
mod visual_wire_tests {
    use super::*;
    fn params()->CodeQueryParams{serde_json::from_value(serde_json::json!({"action":"start","binding":{},"visual_request":{"mode":"map","request":{"schema":"open-clank.code-map/v1","operation":"overview"}}})).unwrap()}
    #[test]
    fn visual_start_is_app_only_and_exclusive(){
        assert!(visual_request(&params(),"app").unwrap().is_some());
        for origin in ["agent","","broker"]{assert!(visual_request(&params(),origin).is_err());}
        for variant in 0..5{let mut p=params();match variant{0=>p.query=Some(serde_json::json!({})),1=>p.source_claim=Some(serde_json::json!({})),2=>p.read_fence_only=true,3=>p.action="poll".into(),_=>p.action="cancel".into()};assert!(visual_request(&p,"app").is_err());}
        let mut p=params();p.visual_request=None;assert!(visual_request(&p,"agent").unwrap().is_none());
    }
    #[test]
    fn visual_input_rejects_unknown_malformed_and_oversized_contracts(){
        for raw in [serde_json::json!({"mode":"other","request":{}}),serde_json::json!({"mode":"map","request":{},"extra":true}),serde_json::json!({"mode":"map","request":{"schema":"open-clank.code-map/v1","operation":"overview","root":{"kind":"visual","id":"x"}}}),serde_json::json!({"mode":"map","request":{"schema":"open-clank.code-map/v1","operation":"overview","cursor":"x".repeat(memery_core::code_visual::MAX_REQUEST_BYTES)}})]{let mut p=params();p.visual_request=Some(raw);assert!(visual_request(&p,"app").is_err());}
    }
    #[tokio::test]
    async fn completed_visual_poll_cancel_during_final_check_never_discloses(){
        let cancel=Arc::new(std::sync::atomic::AtomicBool::new(false));
        let(started_tx,started_rx)=tokio::sync::oneshot::channel();
        let(release_tx,release_rx)=std::sync::mpsc::channel();
        let worker=tokio::task::spawn_blocking(move||{started_tx.send(()).unwrap();release_rx.recv().unwrap();Ok(())});
        started_rx.await.unwrap();cancel.store(true,std::sync::atomic::Ordering::Relaxed);release_tx.send(()).unwrap();
        let value=visual_delivery("owned",serde_json::json!({"must_not_disclose":true}),worker.await.unwrap(),&cancel);
        assert_eq!(value["error"]["code"],"cancelled");assert!(value.get("result").is_none());
        cancel.store(false,std::sync::atomic::Ordering::Relaxed);
        assert_eq!(visual_delivery("owned",serde_json::json!({"mode":"map"}),Ok(()),&cancel)["state"],"completed");
        let error=memery_core::code_query::QueryError{code:"stale_cursor".into(),message:"changed".into()};
        assert_eq!(visual_delivery("owned",serde_json::json!({}),Err(error),&cancel)["error"]["code"],"stale_cursor");
    }
}

/// Operations advertised under `native_memory_ops_v1`. Order is part of the Host exact-equality check
/// (`NATIVE_MEMORY_OPS` in src/memery_provider.py); tests/test_native_ops_parity.py compares them.
/// C4-F8: only the three closed reasons of the frozen wire reach the Host; every other failure is the generic one.
fn preextracted_reason(e:&str)->&'static str {
    match e {"preextracted request invalid"=>"preextracted request invalid","preextracted request key conflict"=>"preextracted request key conflict",_=>"preextracted unavailable"}
}
const NATIVE_MEMORY_OPS:[&str;2]=["explicit_add_review_batch_v1","curated_knowledge_snapshot_review_v1"];

/// The one guard every native-memory-ops handler runs: guarded-v18 profile receipt, live database identity, epoch 18.
/// Return the admitted file identity and its current verified schema cookie.
fn require_native_ops(v:&serde_json::Value,store:&SqliteStore)->Result<(String,u64,u64,i64),WorkerError> {
    let no=||WorkerError::invalid_params("native memory operations unavailable",None);
    if v["profile"]!="guarded-v18" || v["schema_version"]!=18 {return Err(no());}
    let path=v["database_path"].as_str().ok_or_else(no)?;
    let id=v["database_id"].as_str().ok_or_else(no)?;
    let device=v["device"].as_u64().ok_or_else(no)?;
    let inode=v["inode"].as_u64().ok_or_else(no)?;
    store.guarded_identity_current(path,device,inode,id).map_err(|_|no())?;
    if store.guarded_epoch().ok()!=Some(18) {return Err(no());}
    let (cookie, _) = store.guarded_pair_profile().map_err(|_|no())?;
    Ok((id.to_string(),device,inode,cookie))
}

impl MemeryWorker {
    fn new(provider: Arc<NativeProvider>, graph_store: Arc<SqliteStore>, profile_receipt: serde_json::Value) -> Self {
        Self {
            provider, profile_receipt,
            graph_store,
            code_tasks:Arc::new(std::sync::Mutex::new(HashMap::new())),
            code_queries:Arc::new(std::sync::Mutex::new(HashMap::new())),
            s02_importance_permits:Arc::new(std::sync::Mutex::new(HashMap::new())),
            code_query_lane:Arc::new(tokio::sync::Semaphore::new(1)),
            code_query_secret:memery_core::code_query::new_query_secret(),
        }
    }

    async fn capture(
        &self,
        params: CaptureParams,
    ) -> Result<Value, WorkerError> {
        if self.provider.generic_capture_unavailable().unwrap_or(true) {
            return Err(WorkerError::invalid_params("generic capture unavailable under guarded memory profile", None));
        }
        let explicit_turn = params.user_text.is_some() || params.assistant_text.is_some();
        let legacy_content = params.content.unwrap_or_default();
        let (user_text, assistant_text) = if explicit_turn {
            (
                params.user_text.unwrap_or_default(),
                params.assistant_text.unwrap_or_default(),
            )
        } else {
            match params.role.as_deref() {
                Some("assistant") => (String::new(), legacy_content),
                _ => (legacy_content, String::new()),
            }
        };
        let capture_mode = params.capture_mode.unwrap_or_else(|| {
            if explicit_turn {
                "raw_only".into()
            } else {
                "candidate".into()
            }
        });
        if !matches!(
            capture_mode.as_str(),
            "raw_only" | "candidate" | "review_only" | "manual"
        ) {
            return Err(WorkerError::invalid_params(
                "capture_mode must be raw_only|candidate|review_only|manual",
                None,
            ));
        }
        // request_scope guarantees a non-empty owner on every successful
        // path; automatic capture into the "global" workspace is the
        // canonical convention for conversational memory, not an error.
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let owner = Some(scope.owner.clone());
        let workspace_id = scope.workspace_id;
        let mut metadata = match params.metadata.unwrap_or_else(|| serde_json::json!({})) {
            serde_json::Value::String(value) => {
                serde_json::from_str(&value).unwrap_or_else(|_| serde_json::json!({}))
            }
            value if value.is_object() => value,
            _ => serde_json::json!({}),
        };
        if let Some(object) = metadata.as_object_mut() {
            object.insert("capture_mode".into(), capture_mode.into());
            if let Some(source_type) = params.source_type {
                object.insert("source_type".into(), source_type.into());
            }
            if let Some(category) = params.category.as_deref() {
                object.insert("category".into(), category.into());
            }
            if let Some(event_id) = params.source_event_id {
                object.insert("source_event_id".into(), event_id.into());
            }
            object.insert(
                "source_message_ids".into(),
                serde_json::json!(params.source_message_ids),
            );
        }
        let turn = CompletedTurn {
            user_text,
            assistant_text,
            session_key: params.session_key.unwrap_or_default(),
            session_id: params.session_id.unwrap_or_default(),
            workspace_id,
            workspace_path: params.workspace_path,
            source: params.source.unwrap_or_else(|| "memery_capture".to_string()),
            owner,
            category: params.category,
            metadata,
        };

        let result = self.provider.capture(&turn).await;

        let response = serde_json::json!({
            "records_captured": result.records_captured,
            "record_ids": result.record_ids,
            "vectors_written": result.vectors_written,
            "providers_succeeded": result.providers_succeeded,
            "providers_failed": result.providers_failed,
            "unknowns_resolved": result.unknowns_resolved,
        });

        Ok(response)
    }

    async fn list_candidates(
        &self,
        params: CandidateListParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let rows = self
            .provider
            .list_candidates(
                Some(&scope.owner),
                Some(&scope.workspace_id),
                params.status.as_deref(),
                params.limit.unwrap_or(100).min(1000),
            )
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!({"candidates": rows}))
    }

    async fn update_candidate(
        &self,
        params: CandidateUpdateParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("update_candidate")?;
        let scope = request_scope(Some(params.owner), Some(params.workspace_id), false)?;
        let kind = params.category.as_deref().and_then(parse_memory_kind);
        if params.category.is_some() && kind.is_none() {
            return Err(WorkerError::invalid_params(
                "unsupported memory category",
                None,
            ));
        }
        let candidate = self
            .provider
            .update_candidate(
                &params.id,
                &scope.owner,
                &scope.workspace_id,
                &params.content,
                kind,
                params.reason.as_deref().unwrap_or("edited_by_user"),
            )
            .map_err(|error| WorkerError::invalid_params(error, None))?;
        let candidate = candidate.ok_or_else(|| {
            WorkerError::invalid_params(
                "pending candidate not found in this scope or edit was rejected",
                None,
            )
        })?;
        Ok(serde_json::json!({"updated": true, "candidate": candidate}))
    }

    async fn host_candidate_review(&self, params: HostCandidateReviewParams) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("host candidate review")?;
        if std::env::var("FM_HOST_PRIVATE_REVIEW").as_deref() != Ok("1") {
            return Err(WorkerError::invalid_params("host-private review unavailable", None));
        }
        match params {
            HostCandidateReviewParams::PairedErasedFact {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.version!=1 || request.owner!=scope.owner || request.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("paired erased fact unavailable",None));}
                let value=self.provider.paired_erased_fact(&request).map_err(|_|WorkerError::invalid_params("paired erased fact unavailable",None))?;
                Ok(serde_json::json!(value))
            },
            HostCandidateReviewParams::ErasedPairFence {request} => {
                let scope=request_scope(Some(request.phase.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.phase.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("erased pair scope unavailable",None));}
                let value=self.provider.erased_pair_fence(&request).map_err(|_|WorkerError::invalid_params("erased pair unavailable",None))?;
                Ok(serde_json::json!(value))
            },
            HostCandidateReviewParams::QuestionDigest {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),true)?;
                if request.version!=1 || request.owner!=scope.owner || request.workspace_id!=scope.workspace_id || request.account_id.is_empty() || request.account_id.trim()!=request.account_id || request.account_id.len()>512 {return Err(WorkerError::invalid_params("question digest unavailable",None));}
                let result=self.provider.question_digest(&scope.owner,&scope.workspace_id,scope.include_global,&request.account_id).map_err(|_|WorkerError::invalid_params("question digest unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::OwnerPurgeStats {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.version!=1 || request.owner!=scope.owner {return Err(WorkerError::invalid_params("owner purge stats unavailable",None));}
                let result=self.provider.owner_purge_stats(&scope.owner,&request.account_id).map_err(|_|WorkerError::invalid_params("owner purge stats unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::CodeLibraryObserve {owner,workspace_id,request} => {
                let unavailable=||WorkerError::invalid_params("library source unavailable",None);
                let outer=request_scope(Some(owner),Some(workspace_id),false)?;
                let object=request.as_object().ok_or_else(unavailable)?;
                if object.len()!=2 || !object.contains_key("binding") || !object.contains_key("package") {return Err(unavailable());}
                let binding:memery_core::code::WorkspaceBinding=serde_json::from_value(object["binding"].clone()).map_err(|_|unavailable())?;
                let package=object["package"].as_str().ok_or_else(unavailable)?.to_owned();
                if binding.owner!=outer.owner || !matches!(binding.origin.as_str(),"app"|"agent") {return Err(unavailable());}
                let lane=self.code_query_lane.clone();let permit=lane.acquire_owned().await.map_err(|_|unavailable())?;
                let result=tokio::task::spawn_blocking(move||{let _permit=permit;memery_core::code_evidence::observe_on_disk_library(&binding,&package)})
                    .await.map_err(|_|unavailable())?.map_err(|_|unavailable())?;
                let value=serde_json::to_string(&result).map_err(|_|unavailable())?;
                if value.len()>65536{return Err(unavailable());}
                Ok(serde_json::from_str(&value).map_err(|_|WorkerError::internal_error("invalid encoded result", None))?)
            },
            HostCandidateReviewParams::CodeManifestCompare {owner,workspace_id,request} => {
                let unavailable=||WorkerError::invalid_params("code comparison unavailable",None);
                // The outer scope authenticates the owned Memery process; the separately
                // captured Code binding is checked by the Code authority owner.
                let outer=request_scope(Some(owner.clone()),Some(workspace_id),false)?;
                let object=request.as_object().ok_or_else(unavailable)?;
                if object.len()!=4 || !["binding","receipt","source","expected_spec_sha256"].iter().all(|k|object.contains_key(*k)) {return Err(unavailable());}
                let binding:memery_core::code::WorkspaceBinding=serde_json::from_value(object["binding"].clone()).map_err(|_|unavailable())?;
                let receipt:memery_core::code_query::QueryReceipt=serde_json::from_value(object["receipt"].clone()).map_err(|_|unavailable())?;
                let source:memery_core::code_evidence::SourceLocator=serde_json::from_value(object["source"].clone()).map_err(|_|unavailable())?;
                let expected=object["expected_spec_sha256"].as_str().ok_or_else(unavailable)?.to_owned();
                if binding.owner!=outer.owner || binding.origin!="app" || expected.len()!=64 || !expected.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {return Err(unavailable());}
                let mut scope=memery_core::graph::GraphScope::new(outer.owner.clone(),binding.workspace_id.clone()).map_err(|_|unavailable())?;
                scope.include_global=false;
                let claim=memery_core::code_evidence::SourceClaim {source,predicate:memery_core::code_evidence::SourcePredicate::ManifestFileDependency {expected_spec_sha256:expected}};
                let store=self.graph_store.clone(); let lane=self.code_query_lane.clone();
                let permit=lane.acquire_owned().await.map_err(|_|unavailable())?;
                let result=tokio::task::spawn_blocking(move || {
                    let _permit=permit;
                    store.code_query_check(&scope,&binding,&receipt).map_err(|_|())?;
                    let (result,_bytes)=store.code_validate_source_measured(&scope,&binding,&receipt.evidence,&claim);
                    let result=result.map_err(|_|())?;
                    store.code_query_check(&scope,&binding,&receipt).map_err(|_|())?;
                    Ok::<_,()>(result)
                }).await.map_err(|_|unavailable())?.map_err(|_|unavailable())?;
                let value=serde_json::to_string(&result).map_err(|_|unavailable())?;
                Ok(serde_json::from_str(&value).map_err(|_|WorkerError::internal_error("invalid encoded result", None))?)
            },
            HostCandidateReviewParams::DocumentSegments {owner,workspace_id,texts} => {
                let unavailable = || WorkerError::invalid_params("document structure unavailable",None);
                let _scope = request_scope(Some(owner),Some(workspace_id),false)?;
                let permit = self.code_query_lane.clone().acquire_owned().await.map_err(|_| unavailable())?;
                let result = tokio::task::spawn_blocking(move || {
                    let _permit = permit;
                    memery_core::repository::document_code_mentions(&texts)
                }).await.map_err(|_| unavailable())?.map_err(|_| unavailable())?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentStructureWrite {owner,workspace_id,request} if request.writes() => {
                let unavailable = || WorkerError::invalid_params("document structure unavailable",None);
                let scope = request_scope(Some(owner),Some(workspace_id),false)?;
                let provider = self.provider.clone();
                let result = tokio::task::spawn_blocking(move || provider.document_structure_private(&scope.owner, request))
                    .await.map_err(|_| unavailable())?.map_err(|_| unavailable())?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentStructureRead {owner,workspace_id,request} if !request.writes() => {
                let unavailable = || WorkerError::invalid_params("document structure unavailable",None);
                let scope = request_scope(Some(owner),Some(workspace_id),false)?;
                let provider = self.provider.clone();
                let result = tokio::task::spawn_blocking(move || provider.document_structure_private(&scope.owner, request))
                    .await.map_err(|_| unavailable())?.map_err(|_| unavailable())?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentStructureWrite {..} | HostCandidateReviewParams::DocumentStructureRead {..} =>
                Err(WorkerError::invalid_params("document structure phase mismatch",None)),
            HostCandidateReviewParams::DocumentUpsert {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.owner!=scope.owner || !request.workspace_id.is_empty() || !request.project_id.is_empty() {return Err(WorkerError::invalid_params("document upsert unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("document upsert unavailable",None))?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_upsert",&value).map_err(|_|WorkerError::invalid_params("document upsert unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentRemove {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.owner!=scope.owner {return Err(WorkerError::invalid_params("document removal unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("document removal unavailable",None))?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_remove",&value).map_err(|_|WorkerError::invalid_params("document removal unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentRemoveOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.current_owner!=scope.owner {return Err(WorkerError::invalid_params("document removal unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("document removal unavailable",None))?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_remove_original",&value).map_err(|_|WorkerError::invalid_params("document removal unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorProfileSearch {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_profile_search",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorSelectedStart {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_selected_start",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorSelectedStatus {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_selected_status",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorSelectedCommit {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_selected_commit",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorSelectedSearch {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_selected_search",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorProfileStart {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_profile_start",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorProfileStatus {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_profile_status",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorProfileCommit {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_vector_profile_private(&scope.owner,"document_vector_profile_commit",&request).map_err(|_|WorkerError::invalid_params("document vector profile unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorSearch {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_search",&request).map_err(|_|WorkerError::invalid_params("document vector unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorHead {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_head",&request).map_err(|_|WorkerError::invalid_params("document vector unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorText {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_text",&request).map_err(|_|WorkerError::invalid_params("document vector unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentPrepare {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_prepare",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentClaim {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_claim",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentStatus {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_status",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentCancel {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_cancel",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorBatchBegin {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_batch_begin",&request).map_err(|_|WorkerError::invalid_params("document vector batch unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorBatchClaim {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_batch_claim",&request).map_err(|_|WorkerError::invalid_params("document vector batch unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorBatchStatus {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_batch_status",&request).map_err(|_|WorkerError::invalid_params("document vector batch unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorBatchComplete {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_batch_complete",&request).map_err(|_|WorkerError::invalid_params("document vector batch unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorBatchFinalize {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_batch_finalize",&request).map_err(|_|WorkerError::invalid_params("document vector batch unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentBegin {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_begin",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentAppend {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_append",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentText {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_text",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorIntentFinalize {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_intent_finalize",&request).map_err(|_|WorkerError::invalid_params("document vector intent unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorBegin {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_begin",&request).map_err(|_|WorkerError::invalid_params("document vector unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorAppend {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_append",&request).map_err(|_|WorkerError::invalid_params("document vector unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorFinalize {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_finalize",&request).map_err(|_|WorkerError::invalid_params("document vector unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentVectorSnapshot {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.owner!=scope.owner {return Err(WorkerError::invalid_params("document vector snapshot unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("document vector snapshot unavailable",None))?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_vector_snapshot",&value).map_err(|_|WorkerError::invalid_params("document vector snapshot unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentSourceLookup {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.current_owner!=scope.owner {return Err(WorkerError::invalid_params("document upsert unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("document upsert unavailable",None))?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_source_lookup",&value).map_err(|_|WorkerError::invalid_params("document upsert unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DocumentUpsertOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.current_owner!=scope.owner {return Err(WorkerError::invalid_params("document upsert unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("document upsert unavailable",None))?;
                let result=self.provider.document_upsert_private(&scope.owner,"document_upsert_original",&value).map_err(|_|WorkerError::invalid_params("document upsert unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02OverrideCapability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s02_private("override_capability",&serde_json::Value::Null,None).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02Capability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s02_private("capability",&serde_json::Value::Null,None).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02Config {owner,workspace_id,request} => {
                let phase="config";
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.get("owner").and_then(|v|v.as_str())!=Some(scope.owner.as_str()) || request.get("workspace_id").and_then(|v|v.as_str())!=Some(scope.workspace_id.as_str()) {return Err(WorkerError::invalid_params("S02 unavailable",None));}
                let result=self.provider.s02_private(phase,&request,None).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02Selector {owner,workspace_id,request} => {
                let phase="selector";
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.get("owner").and_then(|v|v.as_str())!=Some(scope.owner.as_str()) || request.get("workspace_id").and_then(|v|v.as_str())!=Some(scope.workspace_id.as_str()) {return Err(WorkerError::invalid_params("S02 unavailable",None));}
                let result=self.provider.s02_private(phase,&request,None).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02Snapshot {owner,workspace_id,request} => {
                let phase="snapshot";
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.get("owner").and_then(|v|v.as_str())!=Some(scope.owner.as_str()) || request.get("workspace_id").and_then(|v|v.as_str())!=Some(scope.workspace_id.as_str()) {return Err(WorkerError::invalid_params("S02 unavailable",None));}
                let result=self.provider.s02_private(phase,&request,None).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02OverrideApply {owner,workspace_id,request} => {
                let phase="override_apply";
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.get("owner").and_then(|v|v.as_str())!=Some(scope.owner.as_str()) || request.get("workspace_id").and_then(|v|v.as_str())!=Some(scope.workspace_id.as_str()) {return Err(WorkerError::invalid_params("S02 unavailable",None));}
                let result=self.provider.s02_private(phase,&request,None).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02Apply {owner,workspace_id,request} => {
                let phase="apply";
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.get("owner").and_then(|v|v.as_str())!=Some(scope.owner.as_str()) || request.get("workspace_id").and_then(|v|v.as_str())!=Some(scope.workspace_id.as_str()) {return Err(WorkerError::invalid_params("S02 unavailable",None));}
                let result=self.provider.s02_private(phase,&request,None).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02RenameAssessment {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("S02 assessment unavailable",None));}
                let value=self.provider.s02_rename_assessment(&request).map_err(|_|WorkerError::invalid_params("S02 assessment unavailable",None))?;
                Ok(serde_json::json!(value))
            },
            HostCandidateReviewParams::S02Status {owner,workspace_id,account_id,request,current_policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.get("workspace_id").and_then(|v|v.as_str())!=Some(scope.workspace_id.as_str()) {return Err(WorkerError::invalid_params("S02 unavailable",None));}
                let result=self.provider.s02_original_status("status",&request,&scope.owner,&account_id,&current_policy).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::S02OverrideStatus {owner,workspace_id,account_id,request,current_policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.get("workspace_id").and_then(|v|v.as_str())!=Some(scope.workspace_id.as_str()) {return Err(WorkerError::invalid_params("S02 unavailable",None));}
                let result=self.provider.s02_original_status("override_status",&request,&scope.owner,&account_id,&current_policy).map_err(|_|WorkerError::invalid_params("S02 unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::HelpedCapability {owner,workspace_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.helped_capability_unexposed(&scope.owner).map_err(|_|WorkerError::invalid_params("helped unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::FoundFeaturesCapability {owner,workspace_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.found_features_capability(&scope.owner).map_err(|_|WorkerError::invalid_params("found features unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::FoundFeaturesPage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("found features unavailable",None));}
                let result=self.provider.found_features_page(&request).map_err(|_|WorkerError::invalid_params("found features unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ExplicitAddCapability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let v=&self.profile_receipt;
                let epoch=match (v["profile"].as_str(),v["schema_version"].as_i64()) {
                    (Some("guarded-v17"),Some(17))=>17,
                    (Some("guarded-v18"),Some(18))=>18,
                    _=>return Err(WorkerError::invalid_params("explicit Add capability unavailable",None)),
                };
                if self.graph_store.guarded_epoch().ok()!=Some(epoch) {return Err(WorkerError::invalid_params("explicit Add capability unavailable",None));}
                let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("explicit Add capability unavailable",None))?;
                let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("explicit Add capability unavailable",None))?;
                let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("explicit Add capability unavailable",None))?;
                let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("explicit Add capability unavailable",None))?;
                let (cookie, _) = self.graph_store.guarded_pair_profile().map_err(|_|WorkerError::invalid_params("explicit Add capability unavailable",None))?;
                self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("explicit Add capability unavailable",None))?;
                Ok(serde_json::json!({"version":1,"feature":"explicit-add-import-review-v1","database_id":id,"device":device,"inode":inode,"schema_cookie":cookie,"operations":["add","add_status","review","review_edit","review_reject","question_answer","import_review","import_status","import_staged_review","missing_original_pages","missing_terminal_pages","reset_terminal"]}))
            }
            HostCandidateReviewParams::HelpedSelectorPage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if request.version!=1 || scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("helped unavailable",None));}
                let result=self.provider.helped_selector_unexposed(&scope.owner,&scope.workspace_id,&request.account_id,&request.source,&request.authority,request.after.as_deref(),request.limit as usize).map_err(|_|WorkerError::invalid_params("helped unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::HelpedObservation {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("helped unavailable",None));}
                let result=self.provider.append_helped_unexposed(&request).map_err(|_|WorkerError::invalid_params("helped unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::HelpedStatus {request,current_authority} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("helped unavailable",None));}
                let result=self.provider.helped_status_unexposed(&request,&current_authority).map_err(|_|WorkerError::invalid_params("helped unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::NativeMemoryOpsCapability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let (id,device,inode,cookie)=require_native_ops(&self.profile_receipt,&self.graph_store)?;
                let mut operations=NATIVE_MEMORY_OPS.to_vec();
                let f8=self.graph_store.guarded_f8_profile_capability().map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                if f8 {operations.push("preextracted_candidates_v1");}
                if self.graph_store.guarded_f1_profile_capability().map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))? {operations.push("knowledge_update_batch_v1");}
                if f8 {operations.push("preextracted_host_anchor_v1");}
                operations.push("rank_learning_v1");
                if self.provider.guarded_vector_profiles_capability().map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))? {operations.push("document_vector_profiles_v1");}
                Ok(serde_json::json!({"version":1,"feature":"native_memory_ops_v1","database_id":id,"device":device,"inode":inode,"schema_cookie":cookie,"operations":operations}))
            }
            HostCandidateReviewParams::NativeMemoryOpsBatchPreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                require_native_ops(&self.profile_receipt,&self.graph_store)?;
                let value=self.graph_store.native_memory_ops_batch_preview(&request).map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::NativeMemoryOpsBatchApply {request} => {
                let scope=request_scope(Some(request.selector.owner.clone()),Some(request.selector.workspace_id.clone()),false)?;
                if scope.owner!=request.selector.owner || scope.workspace_id!=request.selector.workspace_id {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                require_native_ops(&self.profile_receipt,&self.graph_store)?;
                let value=self.graph_store.native_memory_ops_batch_apply(&request).map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::NativeMemoryOpsBatchStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                require_native_ops(&self.profile_receipt,&self.graph_store)?;
                let value=self.graph_store.native_memory_ops_batch_status(&request).map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::GuardedBatchForgetPreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if request.action!="preview" || scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("guarded forget unavailable",None));}
                let v=&self.profile_receipt;
                if v["profile"]!="guarded-v18" || v["schema_version"]!=18 {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                if self.graph_store.guarded_epoch().ok()!=Some(18) {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let value=self.graph_store.guarded_batch_forget_preview(&request).map_err(|_|WorkerError::invalid_params("guarded forget unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::GuardedBatchForget {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if request.action!="commit" || scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("guarded forget unavailable",None));}
                let v=&self.profile_receipt;
                if v["profile"]!="guarded-v18" || v["schema_version"]!=18 {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                if self.graph_store.guarded_epoch().ok()!=Some(18) {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let value=self.graph_store.guarded_batch_forget_commit(&request).map_err(|_|WorkerError::invalid_params("guarded forget unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::GuardedBatchForgetStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if request.action!="status" || scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("guarded forget unavailable",None));}
                let v=&self.profile_receipt;
                if v["profile"]!="guarded-v18" || v["schema_version"]!=18 {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                if self.graph_store.guarded_epoch().ok()!=Some(18) {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let value=self.graph_store.guarded_batch_forget_status(&request).map_err(|_|WorkerError::invalid_params("guarded forget unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::GuardedBatchForgetRestore {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if request.action!="restore" || scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("guarded forget unavailable",None));}
                let v=&self.profile_receipt;
                if v["profile"]!="guarded-v18" || v["schema_version"]!=18 {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("native memory operations unavailable",None))?;
                self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("native memory operations unavailable",None))?;
                if self.graph_store.guarded_epoch().ok()!=Some(18) {return Err(WorkerError::invalid_params("native memory operations unavailable",None));}
                let value=self.graph_store.guarded_batch_forget_restore(&request).map_err(|_|WorkerError::invalid_params("guarded forget unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ProfileStatus => {
                let mut v=self.profile_receipt.clone();
                if v["profile"]=="guarded-v17" || v["profile"]=="guarded-v18" {
                    let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("profile unavailable",None))?;
                    let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("profile unavailable",None))?;
                    let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("profile unavailable",None))?;
                    let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("profile unavailable",None))?;
                    self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("profile unavailable",None))?;
                    let (cookie, _) = self.graph_store.guarded_pair_profile().map_err(|_|WorkerError::invalid_params("profile unavailable",None))?;
                    v["schema_cookie"] = cookie.into();
                }
                Ok(v)
            },
            HostCandidateReviewParams::ResetRetirementCapability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let v=&self.profile_receipt;
                let epoch=match (v["profile"].as_str(),v["schema_version"].as_i64()) {
                    (Some("guarded-v17"),Some(17))=>17,
                    (Some("guarded-v18"),Some(18))=>18,
                    _=>return Err(WorkerError::invalid_params("reset capability unavailable",None)),
                };
                if self.graph_store.guarded_epoch().ok()!=Some(epoch) {return Err(WorkerError::invalid_params("reset capability unavailable",None));}
                let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("reset capability unavailable",None))?;
                Ok(serde_json::json!({"version":1,"feature":"queued-reset-single-holder-v1","database_id":id,"device":device,"inode":inode}))
            },
            HostCandidateReviewParams::ResetTerminalS08Capability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let v=&self.profile_receipt;
                let epoch=match (v["profile"].as_str(),v["schema_version"].as_i64()) {
                    (Some("guarded-v17"),Some(17))=>17,
                    (Some("guarded-v18"),Some(18))=>18,
                    _=>return Err(WorkerError::invalid_params("reset capability unavailable",None)),
                };
                if self.graph_store.guarded_epoch().ok()!=Some(epoch) {return Err(WorkerError::invalid_params("reset capability unavailable",None));}
                let path=v["database_path"].as_str().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                let id=v["database_id"].as_str().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                let device=v["device"].as_u64().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                let inode=v["inode"].as_u64().ok_or_else(||WorkerError::invalid_params("reset capability unavailable",None))?;
                self.graph_store.guarded_identity_current(path,device,inode,id).map_err(|_|WorkerError::invalid_params("reset capability unavailable",None))?;
                Ok(serde_json::json!({"version":1,"feature":"nonqueued-memories-terminal-s08-v1","database_id":id,"device":device,"inode":inode}))
            },
            HostCandidateReviewParams::MetadataAssignmentStatus {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.metadata_status().map_err(|_|WorkerError::invalid_params("metadata unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::MetadataAssignmentApply {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id{return Err(WorkerError::invalid_params("metadata unavailable",None));}
                let result=self.provider.apply_manual_metadata(&request).map_err(|_|WorkerError::invalid_params("metadata unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::MetadataRead {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id || request.curated_id.is_none(){return Err(WorkerError::invalid_params("metadata unavailable",None));}
                let result=self.provider.metadata_read(&request,false).map_err(|_|WorkerError::invalid_params("metadata unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DomainDefinitionList {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id || request.curated_id.is_some(){return Err(WorkerError::invalid_params("metadata unavailable",None));}
                let result=self.provider.metadata_read(&request,true).map_err(|_|WorkerError::invalid_params("metadata unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DomainDefinitionStatus {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.domain_definition_status().map_err(|_|WorkerError::invalid_params("domain review unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::DomainDefinitionReview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("domain review unavailable",None));}
                let result=self.provider.review_domain_definition(&request).map_err(|_|WorkerError::invalid_params("domain review unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AnalysisSnapshotCapability {owner,workspace_id,request_version} => {
                if !matches!(request_version,None|Some(1)|Some(2)) {return Err(WorkerError::invalid_params("snapshot unavailable",None));}
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.analysis_snapshot_capability().map_err(|_|WorkerError::invalid_params("snapshot unavailable",None))?;
                let result=if request_version==Some(2) {
                    if result!=serde_json::json!({"version":1,"state":"ready","phase":"analysis_snapshot_publish"}) {return Err(WorkerError::invalid_params("snapshot unavailable",None));}
                    serde_json::json!({"version":2,"state":"ready","phase":"analysis_snapshot_publish"})
                } else {result};
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AnalysisSnapshotPublish {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("snapshot unavailable",None));}
                let result=self.provider.publish_analysis_snapshot(&request).map_err(|_|WorkerError::invalid_params("snapshot unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AnalysisSnapshotStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("snapshot unavailable",None));}
                let result=self.provider.analysis_snapshot_status(&request).map_err(|_|WorkerError::invalid_params("snapshot unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::StandingGuidanceSelect {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id{return Err(WorkerError::invalid_params("guidance unavailable",None));}
                let result=self.provider.select_standing_guidance(&request).map_err(|_|WorkerError::invalid_params("guidance unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::StandingGuidanceRevalidate {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id{return Err(WorkerError::invalid_params("guidance unavailable",None));}
                let result=self.provider.revalidate_standing_guidance(&request).map_err(|_|WorkerError::invalid_params("guidance unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::StandingGuidanceCapability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.standing_guidance_capability().map_err(|_|WorkerError::invalid_params("purpose unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::StandingGuidancePreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id{return Err(WorkerError::invalid_params("purpose unavailable",None));}
                let result=self.provider.preview_standing_guidance(&request).map_err(|_|WorkerError::invalid_params("purpose unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::StandingGuidanceRead {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id{return Err(WorkerError::invalid_params("purpose unavailable",None));}
                let result=self.provider.read_standing_guidance(&request).map_err(|_|WorkerError::invalid_params("purpose unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::StandingGuidanceApply {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id{return Err(WorkerError::invalid_params("purpose unavailable",None));}
                let result=self.provider.apply_standing_guidance(&request).map_err(|_|WorkerError::invalid_params("purpose unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::StandingGuidanceStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id{return Err(WorkerError::invalid_params("purpose unavailable",None));}
                let result=self.provider.standing_guidance_status(&request).map_err(|_|WorkerError::invalid_params("purpose unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AccountValueSuccessorCapability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.account_value_successor_readiness().map_err(|_|WorkerError::invalid_params("successor unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AccountValueSuccessorPreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("successor unavailable",None));}
                let result=self.provider.preview_account_value_successor(&request).map_err(|_|WorkerError::invalid_params("successor unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AccountValueSuccessorApply {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("successor unavailable",None));}
                let result=self.provider.apply_account_value_successor(&request).map_err(|_|WorkerError::invalid_params("successor unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AccountValueSuccessorStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("successor unavailable",None));}
                let result=self.provider.account_value_successor_status(&request).map_err(|_|WorkerError::invalid_params("successor unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AccountCandidateApply {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {
                    return Err(WorkerError::invalid_params("account candidate review unavailable",None));
                }
                let result=self.provider.apply_account_candidate_review(&request)
                    .map_err(|_|WorkerError::invalid_params("account candidate review unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::AccountCandidateStatus {owner,workspace_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let status=self.provider.policy_publication_status(&scope.owner).map_err(|_|WorkerError::invalid_params("account candidate capability unavailable",None))?;
                if status["policy_writer"]!=true {return Err(WorkerError::invalid_params("account candidate capability unavailable",None));}
                Ok(serde_json::json!({"version":1,"state":"ready","phase":"account_candidate_apply"}))
            },
            HostCandidateReviewParams::PreparedKnowledgeRead {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("prepared knowledge unavailable",None));}
                let result=self.provider.prepared_knowledge_read(&request).map_err(|_|WorkerError::invalid_params("prepared knowledge unavailable",None))?;
                Ok(serde_json::json!(result))
            },
            HostCandidateReviewParams::GuardedResetCommit {version,owner,workspace_id,operation_id,account_id,components,expected_counts,queued_retired,selective_manifest_sha256} => {
                if version!=1 || operation_id.trim().is_empty() || operation_id.len()>512 || account_id.trim().is_empty() || account_id.len()>512 || components.is_empty() || components.len()>3 || !expected_counts.is_object() {
                    return Err(WorkerError::invalid_params("guarded reset unavailable",None));
                }
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=if let Some(manifest)=selective_manifest_sha256.as_deref() {
                    if queued_retired {return Err(WorkerError::invalid_params("guarded reset unavailable",None));}
                    self.provider.guarded_selective_ingest_reset(&scope.owner,&scope.workspace_id,&account_id,&operation_id,&components,&expected_counts,manifest)
                } else {self.provider.guarded_reset_owner(&scope.owner,&scope.workspace_id,&account_id,&operation_id,&components,&expected_counts,queued_retired)}
                    .map_err(|_|WorkerError::invalid_params("guarded reset unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::GuardedResetStamp {version,owner,workspace_id,operation_id,account_id,components,expected_counts,queued_retired,selective_manifest_sha256} => {
                if version!=1 || operation_id.trim().is_empty() || operation_id.len()>512 || account_id.trim().is_empty() || account_id.len()>512 || components.is_empty() || components.len()>3 || !expected_counts.is_object() {return Err(WorkerError::invalid_params("guarded reset unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.guarded_reset_stamp(&scope.owner,&scope.workspace_id,&account_id,&operation_id,&components,&expected_counts,queued_retired,selective_manifest_sha256.as_deref()).map_err(|_|WorkerError::invalid_params("guarded reset unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::GuardedResetCleanup {version,owner,workspace_id,operation_id,account_id,components,expected_counts,queued_retired,selective_manifest_sha256,path,device,inode,database_id} => {
                if version!=1 || operation_id.trim().is_empty() || operation_id.len()>512 || account_id.trim().is_empty() || account_id.len()>512 || components.is_empty() || components.len()>3 || !expected_counts.is_object() {
                    return Err(WorkerError::invalid_params("guarded reset cleanup unavailable",None));
                }
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                self.provider.reconcile_guarded_reset_cache(&scope.owner,&scope.workspace_id,&account_id,&operation_id,&components,&expected_counts,queued_retired,&path,device,inode,&database_id,selective_manifest_sha256.as_deref())
                    .map_err(|_|WorkerError::invalid_params("guarded reset cleanup unavailable",None))?;
                Ok(serde_json::json!({"state":"cleaned"}))
            }
            HostCandidateReviewParams::ReceiptStatus {owner,workspace_id,candidate_id,candidate_digest,expected_v2_head,selected_paths,request_key,reason} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.lookup_host_candidate_receipt_status(&scope.owner,&scope.workspace_id,&candidate_id,&request_key,&candidate_digest,expected_v2_head,&selected_paths,&reason)
                    .map_err(|_|WorkerError::invalid_params("candidate receipt unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ObservationRenew {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("observation renewal scope changed",None));}
                let receipt=self.provider.renew_observation_source(&request).map_err(|_|WorkerError::invalid_params("observation renewal unavailable",None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("observation renewal unavailable",None))?)
            }
            HostCandidateReviewParams::S07RecallStart {owner,workspace_id,invocation,query,limit,budget_ms} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let ids=[&invocation.session_id,&invocation.root_operation_id,&invocation.actor_id,&invocation.inner_turn_id,&invocation.recall_attempt_id];
                if ids.iter().any(|v|v.is_empty() || v.len()>512 || v.trim()!=v.as_str()) || invocation.execution_revision<1 || query.trim().is_empty() || query.len()>8192 || !(1..=20).contains(&limit) || !(1..=200).contains(&budget_ms) {return Err(WorkerError::invalid_params("bounded recall unavailable",None));}
                let binding_key=format!("s07:{}",serde_json::to_string(&(&scope.owner,&scope.workspace_id,&invocation)).map_err(|_|WorkerError::invalid_params("bounded recall unavailable",None))?);
                let token=format!("s07_{}",generate_id());
                let cancel=Arc::new(std::sync::atomic::AtomicBool::new(false));
                let deadline=std::time::Instant::now()+std::time::Duration::from_millis(budget_ms);
                {
                    let mut tasks=self.code_queries.lock().map_err(|_|WorkerError::internal_error("bounded recall unavailable",None))?;
                    if tasks.values().filter(|t|t.result.is_none() || t.topology_reserved>0).count()>=4 || tasks.values().any(|t|t.binding_key.starts_with("s07:") && t.result.is_none()) {return Err(WorkerError::invalid_params("bounded recall busy",None));}
                    let mut completed:Vec<_>=tasks.iter().filter_map(|(k,t)|t.completed.map(|at|(at,k.clone()))).collect(); completed.sort();
                    let remove=completed.len().saturating_sub(31); for (_,key) in completed.into_iter().take(remove) {tasks.remove(&key);}
                    tasks.insert(token.clone(),CodeQueryTask {binding_key,cancel:cancel.clone(),result:None,completed:None,expires:Some(deadline),topology_reserved:0,topology:None});
                }
                let tasks=self.code_queries.clone(); let store=self.graph_store.clone(); let job=token.clone(); let worker_cancel=cancel.clone();
                tokio::spawn(async move {
                    let result=tokio::task::spawn_blocking(move || {
                        store.bounded_qualified_curated_fts(&query,limit,&scope.owner,&scope.workspace_id,deadline,&worker_cancel)
                            .map(|(rows,refs)|{let total=rows.len();serde_json::json!({"result":memery_core::record::SearchResult{results:rows,total},"refs":refs})})
                            .map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Bounded recall unavailable".into()})
                    }).await.unwrap_or_else(|_|Err(memery_core::code_query::QueryError{code:"unavailable".into(),message:"Bounded recall unavailable".into()}));
                    if let Ok(mut map)=tasks.lock() {if let Some(task)=map.get_mut(&job) {task.result=Some(result);task.completed=Some(std::time::Instant::now());}}
                });
                let tasks=self.code_queries.clone(); let expiry_token=token.clone();
                tokio::spawn(async move {
                    tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)).await;
                    if let Ok(mut map)=tasks.lock() {if let Some(task)=map.remove(&expiry_token) {task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);}}
                });
                Ok(serde_json::json!({"version":1,"state":"running","token":token}))
            }
            HostCandidateReviewParams::S07RevalidateStart {owner,workspace_id,invocation,selected,budget_ms} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let ids=[&invocation.session_id,&invocation.root_operation_id,&invocation.actor_id,&invocation.inner_turn_id,&invocation.recall_attempt_id];
                if ids.iter().any(|v|v.is_empty() || v.len()>512 || v.trim()!=v.as_str()) || invocation.execution_revision<1 || selected.is_empty() || selected.len()>3 || !(1..=200).contains(&budget_ms) {return Err(WorkerError::invalid_params("bounded recall unavailable",None));}
                let binding_key=format!("s07:{}",serde_json::to_string(&(&scope.owner,&scope.workspace_id,&invocation)).map_err(|_|WorkerError::invalid_params("bounded recall unavailable",None))?);
                let token=format!("s07_{}",generate_id()); let cancel=Arc::new(std::sync::atomic::AtomicBool::new(false));
                let deadline=std::time::Instant::now()+std::time::Duration::from_millis(budget_ms);
                {
                    let mut tasks=self.code_queries.lock().map_err(|_|WorkerError::internal_error("bounded recall unavailable",None))?;
                    if tasks.values().filter(|t|t.result.is_none() || t.topology_reserved>0).count()>=4 || tasks.values().any(|t|t.binding_key.starts_with("s07:") && t.result.is_none()) {return Err(WorkerError::invalid_params("bounded recall busy",None));}
                    let mut completed:Vec<_>=tasks.iter().filter(|(_,t)|t.topology_reserved==0).filter_map(|(k,t)|t.completed.map(|at|(at,k.clone()))).collect();completed.sort();
                    let remove=completed.len().saturating_sub(31);for(_,key)in completed.into_iter().take(remove){tasks.remove(&key);}
                    tasks.insert(token.clone(),CodeQueryTask{binding_key,cancel:cancel.clone(),result:None,completed:None,expires:Some(deadline),topology_reserved:0,topology:None});
                }
                let tasks=self.code_queries.clone();let store=self.graph_store.clone();let job=token.clone();let worker_cancel=cancel.clone();
                tokio::spawn(async move {
                    let result=tokio::task::spawn_blocking(move || store.bounded_revalidate_curated_refs(&scope.owner,&scope.workspace_id,&selected,deadline,&worker_cancel)
                        .map(|_|serde_json::json!({"valid":true})).map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Bounded recall unavailable".into()}))
                        .await.unwrap_or_else(|_|Err(memery_core::code_query::QueryError{code:"unavailable".into(),message:"Bounded recall unavailable".into()}));
                    if let Ok(mut map)=tasks.lock(){if let Some(task)=map.get_mut(&job){task.result=Some(result);task.completed=Some(std::time::Instant::now());}}
                });
                let tasks=self.code_queries.clone();let expiry_token=token.clone();
                tokio::spawn(async move {tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)).await;if let Ok(mut map)=tasks.lock(){if let Some(task)=map.remove(&expiry_token){task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);}}});
                Ok(serde_json::json!({"version":1,"state":"running","token":token}))
            }
            HostCandidateReviewParams::S07RecallPoll {owner,workspace_id,invocation,token} => {
                if token.len()>128 {return Err(WorkerError::invalid_params("bounded recall unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let binding_key=format!("s07:{}",serde_json::to_string(&(&scope.owner,&scope.workspace_id,&invocation)).map_err(|_|WorkerError::invalid_params("bounded recall unavailable",None))?);
                // Wait briefly only for this already-bound task; never retain the map lock across await.
                let wait = {
                    let tasks=self.code_queries.lock().map_err(|_|WorkerError::internal_error("bounded recall unavailable",None))?;
                    let task=tasks.get(&token).filter(|task|task.binding_key==binding_key && !task.cancel.load(std::sync::atomic::Ordering::Relaxed) && task.expires.is_some_and(|deadline|std::time::Instant::now()<deadline)).ok_or_else(||WorkerError::invalid_params("bounded recall unavailable",None))?;
                    if task.result.is_none() { task.expires.map(|deadline| deadline.saturating_duration_since(std::time::Instant::now()).min(std::time::Duration::from_millis(5))) } else { None }
                };
                if let Some(wait)=wait { tokio::time::sleep(wait).await; }
                let mut tasks=self.code_queries.lock().map_err(|_|WorkerError::internal_error("bounded recall unavailable",None))?;
                let task=tasks.get(&token).filter(|task|task.binding_key==binding_key && !task.cancel.load(std::sync::atomic::Ordering::Relaxed) && task.expires.is_some_and(|deadline|std::time::Instant::now()<deadline)).ok_or_else(||WorkerError::invalid_params("bounded recall unavailable",None))?;
                let result=task.result.clone();
                if let Some(result)=result {
                    tasks.remove(&token);
                    let payload=result.map_err(|_|WorkerError::invalid_params("bounded recall unavailable",None))?;
                    Ok(serde_json::json!({"version":1,"state":"complete","result":payload}))
                } else { Ok(serde_json::json!({"version":1,"state":"running"})) }
            }
            HostCandidateReviewParams::S07RecallCancel {owner,workspace_id,invocation,token} => {
                if token.len()>128 {return Err(WorkerError::invalid_params("bounded recall unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let binding_key=format!("s07:{}",serde_json::to_string(&(&scope.owner,&scope.workspace_id,&invocation)).map_err(|_|WorkerError::invalid_params("bounded recall unavailable",None))?);
                let mut tasks=self.code_queries.lock().map_err(|_|WorkerError::internal_error("bounded recall unavailable",None))?;
                let task=tasks.get(&token).filter(|task|task.binding_key==binding_key).ok_or_else(||WorkerError::invalid_params("bounded recall unavailable",None))?;
                task.cancel.store(true,std::sync::atomic::Ordering::Relaxed); tasks.remove(&token);
                Ok(serde_json::json!({"version":1,"state":"cancelled"}))
            }
            HostCandidateReviewParams::QualifiedSearch {owner,workspace_id,query,limit} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.qualified_curated_search(&scope.owner,&scope.workspace_id,&query,limit)
                    .map_err(|_|WorkerError::invalid_params("qualified search unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ExternalHistoryClaim { owner,workspace_id,claim } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let id=self.provider.append_external_history_claim(&scope.owner,&scope.workspace_id,&claim).map_err(|_|WorkerError::invalid_params("external history unavailable",None))?;
                Ok(serde_json::json!({"version":1,"receipt_id":id}))
            }
            HostCandidateReviewParams::ExternalHistoryPage { owner,workspace_id,after,limit } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.external_history_claim_page(&scope.owner,&scope.workspace_id,after.as_deref(),limit).map_err(|_|WorkerError::invalid_params("external history unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerPurgeApply { request } => {
                let result=self.provider.guarded_owner_purge(&request).map_err(|_|WorkerError::invalid_params("owner purge unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerPurgeStatus { request } => {
                let result=self.provider.guarded_owner_purge_status(&request).map_err(|_|WorkerError::invalid_params("owner purge unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerPurgeStamp { request } => {
                let result=self.provider.guarded_owner_purge_stamp(&request).map_err(|_|WorkerError::invalid_params("owner purge unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerTransitionApply { request } => {
                let result=self.provider.guarded_owner_rename(&request).map_err(|error|{
                    let phase=match error.as_str() {
                        "guarded rename request unavailable"=>"request",
                        "guarded rename inventory changed"=>"inventory",
                        "guarded rename epoch unavailable"|"guarded schema unavailable"=>"schema",
                        "rename custody unavailable"|"guarded rename custody unavailable"=>"custody",
                        "question chain rename unavailable"=>"question_chain_before",
                        "question chain rename custody changed"=>"question_chain_after",
                        "question terminal rename custody unavailable"|"question terminal rename custody changed"=>"question_terminal",
                        "rename fact assessment unavailable"=>"rename_fact",
                        "S02 assessment unavailable"=>"s02",
                        "paired rename fence unavailable"|"queued source rename fence unavailable"=>"source_fence",
                        "owner transition target unavailable"|"queued source rename target unavailable"=>"target",
                        "skill draft rename custody unavailable"|"skill draft rename postcondition unavailable"|"skill draft rename custody changed"=>"skill",
                        _=>"other",
                    };
                    tracing::debug!(target:"memery_core::s09",phase);
                    WorkerError::invalid_params("owner transition unavailable",None)
                })?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerTransitionStatus { request } => {
                let result=self.provider.guarded_owner_rename_status(&request).map_err(|_|WorkerError::invalid_params("owner transition unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerTransitionStamp { request } => {
                let result=self.provider.guarded_owner_rename_stamp(&request).map_err(|_|WorkerError::invalid_params("owner transition unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerTransitionCacheCleanup { request } => {
                let result=self.provider.guarded_owner_rename_cache_cleanup(&request).map_err(|_|WorkerError::invalid_params("owner cache cleanup unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::OwnerPurgeCacheCleanup { request } => {
                let result=self.provider.guarded_owner_purge_cache_cleanup(&request).map_err(|_|WorkerError::invalid_params("owner cache cleanup unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ConsolidationAdmissionsPage {owner,workspace_id,source,authority,after,limit} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let page=self.provider.consolidation_admissions_page(&scope.owner,&scope.workspace_id,&source,&authority,after.as_deref(),limit).map_err(|_|WorkerError::invalid_params("consolidation evidence unavailable",None))?;
                Ok(serde_json::json!(page))
            }
            HostCandidateReviewParams::ObservationExportPage {owner,workspace_id,source,authority,kind,after,limit} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let page=self.provider.export_observation_page(&scope.owner,&scope.workspace_id,&source,&authority,&kind,after.as_deref(),limit).map_err(|_|WorkerError::invalid_params("observation export unavailable",None))?;
                Ok(serde_json::json!(page))
            }
            HostCandidateReviewParams::AdmittedObservation {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("admitted scope unavailable",None));}
                let event_id=self.provider.append_admitted_observation(&request).map_err(|_|WorkerError::invalid_params("admitted observation unavailable",None))?;
                Ok(serde_json::json!({"version":1,"event_id":event_id}))
            }
            HostCandidateReviewParams::AdmittedObservationRead {owner,workspace_id,event_id,authority} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let observation=self.provider.read_admitted_observation(&scope.owner,&scope.workspace_id,&event_id,&authority).map_err(|_|WorkerError::invalid_params("admitted observation unavailable",None))?;
                Ok(serde_json::to_value(&observation).map_err(|_|WorkerError::internal_error("admitted observation unavailable",None))?)
            }
            HostCandidateReviewParams::ContextAdmittedObservation {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("admitted scope unavailable",None));}
                let event_id=self.provider.append_context_admitted_observation(&request).map_err(|_|WorkerError::invalid_params("admitted observation unavailable",None))?;
                Ok(serde_json::json!({"version":1,"event_id":event_id}))
            }
            HostCandidateReviewParams::ContextAdmittedObservationRead {owner,workspace_id,event_id,authority} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let observation=self.provider.read_context_admitted_observation(&scope.owner,&scope.workspace_id,&event_id,&authority).map_err(|_|WorkerError::invalid_params("admitted observation unavailable",None))?;
                Ok(serde_json::to_value(&observation).map_err(|_|WorkerError::internal_error("admitted observation unavailable",None))?)
            }
            HostCandidateReviewParams::FoundObservationRead {owner,workspace_id,event_id,authority} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let observation=self.provider.read_found_observation(&scope.owner,&scope.workspace_id,&event_id,&authority)
                    .map_err(|_|WorkerError::invalid_params("found observation unavailable",None))?;
                Ok(serde_json::to_value(&observation).map_err(|_|WorkerError::internal_error("found observation unavailable",None))?)
            }
            HostCandidateReviewParams::FoundObservation {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("found scope changed",None));}
                let event_id=self.provider.append_found_observation(&request).map_err(|e|WorkerError::invalid_params(e,None))?;
                Ok(serde_json::json!({"version":1,"event_id":event_id}))
            }
            HostCandidateReviewParams::ObservationFence {version,owner,workspace_id,authority,source,rename_operation_id} => {
                if version!=1 {return Err(WorkerError::invalid_params("host observation version unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.fence_observation_origin(&scope.owner,&scope.workspace_id,&authority,&source,rename_operation_id.as_deref())
                    .map_err(|e|WorkerError::invalid_params(e,None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("host observation receipt unavailable",None))?)
            }
            HostCandidateReviewParams::SourceRenew {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("host source renewal scope changed",None));}
                let receipt=self.provider.renew_host_archive_source(&request)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("host source renewal receipt unavailable",None))?)
            }
            HostCandidateReviewParams::PairedReviewPage {owner,workspace_id,after,limit} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let value=self.provider.paired_review_page(&scope.owner,&scope.workspace_id,after.as_deref(),limit).map_err(|_|WorkerError::invalid_params("paired review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedReviewPreview {owner,workspace_id,candidate_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let value=self.provider.paired_review_preview(&scope.owner,&scope.workspace_id,&candidate_id).map_err(|_|WorkerError::invalid_params("paired review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedOriginalPage {owner,workspace_id,account_id,after,limit} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let value=self.provider.paired_review_original_page(&scope.owner,&scope.workspace_id,&account_id,after.as_deref(),limit).map_err(|_|WorkerError::invalid_params("paired original unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedReviewOriginal {owner,workspace_id,account_id,candidate_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let value=self.provider.paired_review_original(&scope.owner,&scope.workspace_id,&account_id,&candidate_id).map_err(|_|WorkerError::invalid_params("paired original unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedReviewApply {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired review scope changed",None));}
                let value=self.provider.paired_review_apply(&request).map_err(|_|WorkerError::invalid_params("paired review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedReviewStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired review scope changed",None));}
                let value=self.provider.paired_review_status(&request).map_err(|_|WorkerError::invalid_params("paired review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedSourceCandidate {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired candidate scope changed",None));}
                let candidate_id=self.provider.stage_assistant_interpretation_candidate(&request).map_err(|_|WorkerError::invalid_params("paired candidate unavailable",None))?;
                Ok(serde_json::json!({"version":1,"candidate_id":candidate_id}))
            }
            HostCandidateReviewParams::SelectiveIngestPartition {version,owner,workspace_id,account_id,partition_sha256} => {
                if version!=1 || account_id.is_empty() || partition_sha256.len()!=64 || !partition_sha256.bytes().all(|v|v.is_ascii_hexdigit() && !v.is_ascii_uppercase()) {return Err(WorkerError::invalid_params("selective ingest unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.selective_ingest_partition(&scope.owner,&account_id,&partition_sha256).map_err(|_|WorkerError::invalid_params("selective ingest unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::PairedSourceFence {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired fence scope changed",None));}
                let state=self.provider.fence_assistant_interpretation(&request).map_err(|_|WorkerError::invalid_params("paired source fence unavailable",None))?;
                Ok(serde_json::json!({"version":1,"state":state}))
            }
            HostCandidateReviewParams::PairedResetFence {request,reset} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired reset scope changed",None));}
                let state=self.provider.reset_fence_assistant_interpretation(&request,&reset).map_err(|_|WorkerError::invalid_params("paired reset fence unavailable",None))?;
                Ok(serde_json::json!({"version":1,"state":state}))
            }
            HostCandidateReviewParams::PairedResetFenceRead {request,reset} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired reset scope changed",None));}
                let state=self.provider.read_reset_assistant_fence(&request,&reset).map_err(|_|WorkerError::invalid_params("paired reset fence unavailable",None))?;
                Ok(serde_json::json!({"version":1,"state":state}))
            }
            HostCandidateReviewParams::SourceCandidate {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("host candidate scope changed",None));}
                let candidate_id=self.provider.stage_host_archive_candidate(&request)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!({"version":1,"candidate_id":candidate_id}))
            }
            HostCandidateReviewParams::SourcePublicationOriginal {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("host publication scope changed",None));}
                let value=self.provider.source_publication_original(&request).map_err(|_|WorkerError::invalid_params("host publication unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::SourceProject {version,owner,workspace_id,job_id,source,policy_fingerprint} => {
                if version!=1 {return Err(WorkerError::invalid_params("host source version unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.project_host_archive_source(&scope.owner,&scope.workspace_id,&job_id,&source,&policy_fingerprint,false,None)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("host source receipt unavailable",None))?)
            }
            HostCandidateReviewParams::SourceResetFence {version,owner,workspace_id,job_id,source,policy_fingerprint,reset_operation_id,reset} => {
                if version!=1 {return Err(WorkerError::invalid_params("host source version unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.reset_fence_host_source(&scope.owner,&scope.workspace_id,&job_id,&source,&policy_fingerprint,&reset_operation_id,&reset).map_err(|_|WorkerError::invalid_params("reset source fence unavailable",None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("host source receipt unavailable",None))?)
            }
            HostCandidateReviewParams::ObservationResetFence {version,owner,workspace_id,authority,source,reset_operation_id,reset} => {
                if version!=1 {return Err(WorkerError::invalid_params("host observation version unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.reset_fence_observation_origin(&scope.owner,&scope.workspace_id,&authority,&source,&reset_operation_id,&reset).map_err(|_|WorkerError::invalid_params("reset observation fence unavailable",None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("host observation receipt unavailable",None))?)
            }
            HostCandidateReviewParams::SourceResetFenceRead {version,owner,workspace_id,job_id,policy_fingerprint,source,reset} => {
                if version!=1 {return Err(WorkerError::invalid_params("reset fence proof unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.read_reset_host_fence(&scope.owner,&scope.workspace_id,Some((&job_id,&policy_fingerprint)),None,&source,&reset).map_err(|_|WorkerError::invalid_params("reset fence proof unavailable",None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("reset fence proof unavailable",None))?)
            }
            HostCandidateReviewParams::ObservationResetFenceRead {version,owner,workspace_id,authority,source,reset} => {
                if version!=1 {return Err(WorkerError::invalid_params("reset fence proof unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.read_reset_host_fence(&scope.owner,&scope.workspace_id,None,Some(&authority),&source,&reset).map_err(|_|WorkerError::invalid_params("reset fence proof unavailable",None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("reset fence proof unavailable",None))?)
            }
            HostCandidateReviewParams::PublishedLifecycleApply {request} => {
                if matches!(request.action.as_str(),"question_defer"|"question_dismiss"|"question_reopen") {tracing::debug!(target:"memery_core::s09",phase="dispatch_entry");}
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("published review scope unavailable",None));}
                let result=self.provider.apply_published_lifecycle(&request).map_err(|_|WorkerError::invalid_params("published review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::QuestionSemanticsStatus {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.question_semantics_status().map_err(|_|WorkerError::invalid_params("question status unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::QuestionSeedStatus {request} => {
                let scope=request_scope(Some(request.request.owner.clone()),Some(request.request.workspace_id.clone()),false)?;
                if scope.owner!=request.request.owner || scope.workspace_id!=request.request.workspace_id {return Err(WorkerError::invalid_params("question scope unavailable",None));}
                let result=self.provider.question_seed_status(&request).map_err(|_|WorkerError::invalid_params("question status unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::QuestionSeedBootstrap {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("question scope unavailable",None));}
                let result=self.provider.bootstrap_question_seeds(&request).map_err(|_|WorkerError::invalid_params("question bootstrap unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::GuardedFullMemoryPreview {version,owner,workspace_id,account_id,components} => {
                if version!=1 || account_id.is_empty() || account_id.trim()!=account_id || account_id.len()>512 || !components.iter().any(|v|v=="memories") {return Err(WorkerError::invalid_params("full memory preview unavailable",None));}
                let scope=request_scope(Some(owner.clone()),Some(workspace_id.clone()),false)?;
                if scope.owner!=owner || scope.workspace_id!=workspace_id {return Err(WorkerError::invalid_params("full memory preview scope changed",None));}
                let value=self.provider.guarded_full_memory_preview(&owner,&workspace_id,&account_id,&components).map_err(|_|WorkerError::invalid_params("full memory preview unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::GuardedSelectiveIngestPreview {version,owner,workspace_id,account_id,components} => {
                if version!=1 || account_id.is_empty() || account_id.trim()!=account_id || components!=["ingest"] {return Err(WorkerError::invalid_params("selective ingest preview unavailable",None));}
                let scope=request_scope(Some(owner.clone()),Some(workspace_id.clone()),false)?;
                if scope.owner!=owner || scope.workspace_id!=workspace_id {return Err(WorkerError::invalid_params("selective ingest scope changed",None));}
                let value=self.provider.guarded_selective_ingest_preview(&owner,&workspace_id,&account_id,&components).map_err(|_|WorkerError::invalid_params("selective ingest preview unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAdd {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("explicit add scope changed",None));}
                let value=self.provider.explicit_add(&request).map_err(|_|WorkerError::invalid_params("explicit add unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::KnowledgeUpdateBatchPreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("atomic update batch unavailable",None));}
                require_native_ops(&self.profile_receipt,&self.graph_store)?;
                let value=self.provider.knowledge_update_batch_preview(&request).map_err(|_|WorkerError::invalid_params("atomic update batch unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::KnowledgeUpdateBatchApply {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("atomic update batch unavailable",None));}
                require_native_ops(&self.profile_receipt,&self.graph_store)?;
                let value=self.provider.knowledge_update_batch_apply(&request).map_err(|_|WorkerError::invalid_params("atomic update batch unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::KnowledgeUpdateBatchStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("atomic update batch unavailable",None));}
                require_native_ops(&self.profile_receipt,&self.graph_store)?;
                let value=self.provider.knowledge_update_batch_status(&request).map_err(|_|WorkerError::invalid_params("atomic update batch unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PreextractedHostAnchorStage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("preextracted unavailable",None));}
                let value=self.provider.preextracted_host_anchor_stage(&request).map_err(|_|WorkerError::invalid_params("preextracted unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PreextractedHostAnchorStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("preextracted unavailable",None));}
                let value=self.provider.preextracted_host_anchor_status(&request).map_err(|_|WorkerError::invalid_params("preextracted unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PreextractedStage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("preextracted unavailable",None));}
                let value=self.provider.preextracted_stage(&request).map_err(|e|WorkerError::invalid_params(preextracted_reason(&e),None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PreextractedReviewPreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("preextracted unavailable",None));}
                let value=self.provider.preextracted_review_preview(&request).map_err(|e|WorkerError::invalid_params(preextracted_reason(&e),None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PreextractedReview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("preextracted unavailable",None));}
                let value=self.provider.preextracted_review(&request).map_err(|e|WorkerError::invalid_params(preextracted_reason(&e),None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PreextractedList {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("preextracted unavailable",None));}
                let value=self.provider.preextracted_list(&request).map_err(|e|WorkerError::invalid_params(preextracted_reason(&e),None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("explicit add scope changed",None));}
                let value=self.provider.explicit_add_status(&request).map_err(|_|WorkerError::invalid_params("explicit add status unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddReviewPreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("explicit Add review scope changed",None));}
                let value=self.provider.explicit_add_review_preview(&request).map_err(|_|WorkerError::invalid_params("explicit Add review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddReviewEdit {request} => {
                let scope=request_scope(Some(request.selector.owner.clone()),Some(request.selector.workspace_id.clone()),false)?;
                if scope.owner!=request.selector.owner || scope.workspace_id!=request.selector.workspace_id {return Err(WorkerError::invalid_params("explicit Add review scope changed",None));}
                let value=self.provider.explicit_add_review_edit(&request).map_err(|_|WorkerError::invalid_params("explicit Add review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddReviewApply {request} => {
                let scope=request_scope(Some(request.selector.owner.clone()),Some(request.selector.workspace_id.clone()),false)?;
                if scope.owner!=request.selector.owner || scope.workspace_id!=request.selector.workspace_id {return Err(WorkerError::invalid_params("explicit Add review scope changed",None));}
                let value=self.provider.explicit_add_review_apply(&request).map_err(|_|WorkerError::invalid_params("explicit Add review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddReviewOriginal {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("explicit Add review scope changed",None));}
                let value=self.provider.explicit_add_review_original(&request).map_err(|_|WorkerError::invalid_params("explicit Add review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddMissingOriginalPage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("explicit Add page scope changed",None));}
                let value=self.provider.explicit_add_missing_page(&request,false).map_err(|_|WorkerError::invalid_params("explicit Add page unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddMissingTerminalPage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("explicit Add page scope changed",None));}
                let value=self.provider.explicit_add_missing_page(&request,true).map_err(|_|WorkerError::invalid_params("explicit Add page unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportMissingOriginalPage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("import review page scope changed",None));}
                let value=self.provider.import_missing_page(&request,false).map_err(|_|WorkerError::invalid_params("import review page unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportMissingTerminalPage {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("import review page scope changed",None));}
                let value=self.provider.import_missing_page(&request,true).map_err(|_|WorkerError::invalid_params("import review page unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportStagedReviewPreview {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("import review scope changed",None));}
                let value=self.provider.import_staged_review_preview(&request).map_err(|_|WorkerError::invalid_params("import review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportStagedReviewEdit {request} => {
                let scope=request_scope(Some(request.selector.owner.clone()),Some(request.selector.workspace_id.clone()),false)?;
                if scope.owner!=request.selector.owner || scope.workspace_id!=request.selector.workspace_id {return Err(WorkerError::invalid_params("import review scope changed",None));}
                let value=self.provider.import_staged_review_edit(&request).map_err(|_|WorkerError::invalid_params("import review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportStagedReviewOriginal {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("import review scope changed",None));}
                let value=self.provider.import_staged_review_original(&request).map_err(|_|WorkerError::invalid_params("import review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportStagedReviewStatus {request} => {
                let scope=request_scope(Some(request.selector.owner.clone()),Some(request.selector.workspace_id.clone()),false)?;
                if scope.owner!=request.selector.owner || scope.workspace_id!=request.selector.workspace_id {return Err(WorkerError::invalid_params("import review scope changed",None));}
                let value=self.provider.import_staged_review_status(&request).map_err(|_|WorkerError::invalid_params("import review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportStagedReviewApply {request} => {
                let scope=request_scope(Some(request.selector.owner.clone()),Some(request.selector.workspace_id.clone()),false)?;
                if scope.owner!=request.selector.owner || scope.workspace_id!=request.selector.workspace_id {return Err(WorkerError::invalid_params("import review scope changed",None));}
                let value=self.provider.import_staged_review_apply(&request).map_err(|_|WorkerError::invalid_params("import review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportReviewApply {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("import review scope changed",None));}
                let value=self.provider.import_review_apply(&request).map_err(|_|WorkerError::invalid_params("import review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ImportReviewStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("import review scope changed",None));}
                let value=self.provider.import_review_status(&request).map_err(|_|WorkerError::invalid_params("import review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ExplicitAddReviewStatus {request} => {
                let scope=request_scope(Some(request.selector.owner.clone()),Some(request.selector.workspace_id.clone()),false)?;
                if scope.owner!=request.selector.owner || scope.workspace_id!=request.selector.workspace_id {return Err(WorkerError::invalid_params("explicit Add review scope changed",None));}
                let value=self.provider.explicit_add_review_status(&request).map_err(|_|WorkerError::invalid_params("explicit Add review unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::ConfirmedQuestionApply {request} => {
                tracing::debug!(target:"memery_core::s09",phase="answer_dispatch_entry");
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("question scope unavailable",None));}
                let result=self.provider.apply_confirmed_question(&request).map_err(|_|WorkerError::invalid_params("question review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::QueuedOriginPreflight {version,owner,workspace_id,account_id,sources} => {
                if version!=1 || sources.is_empty() || sources.len()>128 {return Err(WorkerError::invalid_params("queued preflight unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.preflight_queued_origins(&scope.owner,&scope.workspace_id,&account_id,&sources)
                    .map_err(|_|WorkerError::invalid_params("queued preflight unavailable",None))?;
                Ok(serde_json::json!(receipt))
            }
            HostCandidateReviewParams::QueuedOriginFence {version,owner,workspace_id,source,reset} => {
                if version!=1 {return Err(WorkerError::invalid_params("queued source version unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=if let Some(ref binding)=reset {self.provider.fence_queued_origin_reset(&scope.owner,&scope.workspace_id,&source,binding)} else {self.provider.fence_queued_origin(&scope.owner,&scope.workspace_id,&source)}
                    .map_err(|_|WorkerError::invalid_params("queued source fence unavailable",None))?;
                Ok(serde_json::json!(receipt))
            }
            HostCandidateReviewParams::QueuedOriginResetFenceRead {version,owner,workspace_id,source,reset} => {
                if version!=1 {return Err(WorkerError::invalid_params("queued fence proof unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.read_queued_reset_fence(&scope.owner,&scope.workspace_id,&source,&reset)
                    .map_err(|_|WorkerError::invalid_params("queued fence proof unavailable",None))?;
                Ok(serde_json::json!(receipt))
            }
            HostCandidateReviewParams::SourceFence {version,owner,workspace_id,job_id,source,policy_fingerprint,rename_operation_id} => {
                if version!=1 {return Err(WorkerError::invalid_params("host source version unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let receipt=self.provider.project_host_archive_source(&scope.owner,&scope.workspace_id,&job_id,&source,&policy_fingerprint,true,rename_operation_id.as_deref())
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::to_value(&receipt).map_err(|_|WorkerError::internal_error("host source receipt unavailable",None))?)
            }
            HostCandidateReviewParams::PhaseRootRenew {request} => {
                let scope=request_scope(Some(request.transition.new_owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.transition.new_owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("phase root scope unavailable",None));}
                let value=self.provider.phase_root_renew(&request).map_err(|_|WorkerError::invalid_params("phase root unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PhaseRootObservationFence {request} => {
                let scope=request_scope(Some(request.phase.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.phase.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("phase observation scope unavailable",None));}
                let value=self.provider.phase_root_observation_fence(&request).map_err(|_|WorkerError::invalid_params("phase observation unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PhaseRootObservationRenew {request} => {
                let scope=request_scope(Some(request.transition.new_owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.transition.new_owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("phase observation scope unavailable",None));}
                let value=self.provider.phase_root_observation_renew(&request).map_err(|_|WorkerError::invalid_params("phase observation unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PhaseRootFence {request} => {
                let scope=request_scope(Some(request.phase.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.phase.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("phase root scope unavailable",None));}
                let value=self.provider.phase_root_fence(&request).map_err(|_|WorkerError::invalid_params("phase root unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::TerminalPairFence {request} => {
                let scope=request_scope(Some(request.phase.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.phase.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("terminal pair scope unavailable",None));}
                let value=self.provider.terminal_pair_fence(&request).map_err(|_|WorkerError::invalid_params("terminal pair unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PendingPairFence {request} => {
                let scope=request_scope(Some(request.phase.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.phase.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("pending pair scope unavailable",None));}
                let value=self.provider.pending_pair_fence(&request).map_err(|_|WorkerError::invalid_params("pending pair unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedPublicationOriginal {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired publication scope unavailable",None));}
                let value=self.provider.paired_publication_original(&request).map_err(|_|WorkerError::invalid_params("paired publication unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PendingPairOriginal {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("pending pair scope unavailable",None));}
                let value=self.provider.pending_pair_original(&request).map_err(|_|WorkerError::invalid_params("pending pair unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PendingPairRebind {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("pending pair scope unavailable",None));}
                let value=self.provider.pending_pair_rebind(&request).map_err(|_|WorkerError::invalid_params("pending pair unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedRenameFence {request} => {
                let scope=request_scope(Some(request.phase.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.phase.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired rename scope unavailable",None));}
                let value=self.provider.paired_rename_fence(&request).map_err(|_|WorkerError::invalid_params("paired rename unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::PairedRenameRenew {request} => {
                let scope=request_scope(Some(request.transition.new_owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.transition.new_owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("paired rename scope unavailable",None));}
                let value=self.provider.paired_rename_renew(&request).map_err(|_|WorkerError::invalid_params("paired rename unavailable",None))?;
                Ok(serde_json::json!(value))
            }
            HostCandidateReviewParams::MirrorContext {owner,workspace_id,curated_id,account_id,question_answer_block_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=if let Some(block)=question_answer_block_id.as_deref(){
                    let account=account_id.as_deref().ok_or_else(||WorkerError::invalid_params("question context unavailable",None))?;
                    self.provider.current_host_question_mirror_context(&scope.owner,&scope.workspace_id,&curated_id,block,account)
                        .map_err(|_|WorkerError::invalid_params("question context unavailable",None))?
                }else{self.provider.current_host_mirror_context(&scope.owner,&scope.workspace_id,&curated_id,account_id.as_deref())
                        .map_err(|error|WorkerError::invalid_params(error,None))?};
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::CandidateSource { owner,workspace_id,candidate_id } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.host_candidate_source_reference(&scope.owner,&scope.workspace_id,&candidate_id)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::PolicyPublicationStatus {owner,workspace_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.policy_publication_status(&scope.owner)
                    .map_err(|_|WorkerError::invalid_params("policy publication unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ReviewPreview {owner,workspace_id,candidate_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.preview_s08_review(&scope.owner,&scope.workspace_id,&candidate_id)
                    .map_err(|_|WorkerError::invalid_params("S08 review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ReviewApply {owner,workspace_id,candidate_id,candidate_digest,request_key,reason,policy_projection} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.accept_s08_rules(&scope.owner,&scope.workspace_id,&candidate_id,&candidate_digest,&request_key,&reason,policy_projection.as_ref())
                    .map_err(|_|WorkerError::invalid_params("S08 review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ReviewStatus {owner,workspace_id,candidate_id,candidate_digest,request_key,reason} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_review_receipt_status(&scope.owner,&scope.workspace_id,&candidate_id,&candidate_digest,&request_key,&reason)
                    .map_err(|_|WorkerError::invalid_params("S08 review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::M3Rank {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("ranking unavailable",None));}
                let result=self.provider.m3_rank(&request).map_err(|_|WorkerError::invalid_params("ranking unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08LastRetainedRun {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("S08 retained run unavailable",None));}
                let result=self.provider.s08_last_retained_run(&request).map_err(|_|WorkerError::invalid_params("S08 retained run unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyCapability {owner,workspace_id} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_policy_capability(&scope.owner).map_err(|_|WorkerError::invalid_params("S08 capability unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicySnapshots {owner,workspace_id,selection,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if selection.owner!=scope.owner || selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let result=self.provider.s08_policy_snapshots(&selection,&policy).map_err(|_|WorkerError::invalid_params("S08 policy unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ConnectionPredicateOptions {owner,workspace_id,account_id,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.registered_connection_predicates(&account_id,&scope.workspace_id,&policy).map_err(|_|WorkerError::invalid_params("connection registry unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ConnectionReviewManifest {owner,workspace_id,account_id,candidate_id,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.connection_review_manifest(&scope.owner,&account_id,&scope.workspace_id,&candidate_id,&policy).map_err(|_|WorkerError::invalid_params("connection review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ConnectionReviewOriginal {owner,workspace_id,account_id,candidate_id,candidate_sha256,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.connection_review_original(&scope.owner,&account_id,&scope.workspace_id,&candidate_id,candidate_sha256.as_deref(),&policy).map_err(|_|WorkerError::invalid_params("connection review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::ConnectionReviewApply {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.connection_review_apply(&scope.owner,&scope.workspace_id,&request).map_err(|_|WorkerError::invalid_params("connection review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ReviewKind {owner,workspace_id,account_id,candidate_id,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_review_kind(&scope.owner,&account_id,&scope.workspace_id,&candidate_id,&policy).map_err(|_|WorkerError::invalid_params("connection review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyReviewManifest {owner,workspace_id,account_id,candidate_id,output_kind,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_policy_review_manifest(&output_kind,&scope.owner,&account_id,&scope.workspace_id,&candidate_id,&policy).map_err(|_|WorkerError::invalid_params("S08 review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyReviewRejectPreview {owner,workspace_id,account_id,candidate_id,output_kind,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_policy_review_reject_preview(&output_kind,&scope.owner,&account_id,&scope.workspace_id,&candidate_id,&policy).map_err(|_|WorkerError::invalid_params("S08 review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyReviewOriginal {owner,workspace_id,account_id,candidate_id,candidate_sha256,output_kind,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_policy_review_original(&output_kind,&scope.owner,&account_id,&scope.workspace_id,&candidate_id,candidate_sha256.as_deref(),&policy).map_err(|_|WorkerError::invalid_params("S08 review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyReviewApply {owner,workspace_id,output_kind,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_policy_review_apply(&output_kind,&scope.owner,&scope.workspace_id,&request).map_err(|_|WorkerError::invalid_params("S08 review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S02ImportanceResolve {owner,workspace_id,account_id,curated_id,block_id,expected_revision,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s02_importance_resolve(&scope.owner,&account_id,&scope.workspace_id,curated_id.as_deref(),block_id.as_deref(),expected_revision,&policy).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S02ImportanceContext {owner,workspace_id,account_id,candidate_id,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s02_importance_context(&scope.owner,&account_id,&scope.workspace_id,&candidate_id,&policy).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S02ImportanceReviewContext {owner,workspace_id,account_id,candidate_id,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s02_importance_review_context(&scope.owner,&account_id,&scope.workspace_id,&candidate_id,&policy).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S02ImportanceTargetManifest {owner,workspace_id,account_id,candidate_id,policy} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s02_importance_target_manifest(&scope.owner,&account_id,&scope.workspace_id,&candidate_id,&policy).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S02ImportanceCapability {owner,workspace_id} => {
                let _scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let ready=self.provider.s02_importance_once_ready().map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                Ok(serde_json::json!({"version":1,"state":if ready {"ready"} else {"unavailable"},"profile":"s02.importance-once.v1"}))
            }
            HostCandidateReviewParams::S02ImportancePrepare {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if !self.provider.s02_importance_once_ready().map_err(|_|WorkerError::invalid_params("importance review unavailable",None))? {return Err(WorkerError::invalid_params("importance review unavailable",None));}
                let key=NativeProvider::s02_importance_request_sha256(&request).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                let mut permits=self.s02_importance_permits.lock().map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                let now=std::time::Instant::now();permits.retain(|_,(expires,_)|*expires>now);
                // A duplicate prepare must never restore an earlier issue grant.
                permits.remove(&key);
                if permits.len()>=64 {return Err(WorkerError::invalid_params("importance review unavailable",None));}
                let (result,permit)=self.provider.s02_importance_prepare(&scope.owner,&scope.workspace_id,&request).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                if let Some(permit)=permit {
                    if permit.request_sha256()!=key || result["state"]!="created" {return Err(WorkerError::invalid_params("importance review unavailable",None));}
                    permits.insert(key,(now+std::time::Duration::from_secs(300),permit));
                } else if result["state"]!="existing" {return Err(WorkerError::invalid_params("importance review unavailable",None));}
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S02ImportanceApply {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;

                let key=NativeProvider::s02_importance_request_sha256(&request).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                let permit={let mut permits=self.s02_importance_permits.lock().map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                    let (expires,permit)=permits.remove(&key).ok_or_else(||WorkerError::invalid_params("importance review unavailable",None))?;
                    if expires<=std::time::Instant::now() {return Err(WorkerError::invalid_params("importance review unavailable",None));}permit};
                if !self.provider.s02_importance_once_ready().map_err(|_|WorkerError::invalid_params("importance review unavailable",None))? {return Err(WorkerError::invalid_params("importance review unavailable",None));}
                let result=self.provider.s02_importance_apply(&scope.owner,&scope.workspace_id,&request,permit).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S02ImportanceOriginal {owner,workspace_id,policy,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s02_importance_original(&scope.owner,&scope.workspace_id,&policy,&request).map_err(|_|WorkerError::invalid_params("importance review unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08OriginalScoped {owner,workspace_id,account_id,policy,kind,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s08_original_scoped(&scope.owner,&account_id,&scope.workspace_id,&policy,&kind,&request).map_err(|_|WorkerError::invalid_params("S08 original unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S05ReserveReadiness {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s05_reserve_private(&scope.owner,&scope.workspace_id,"s05_reserve_readiness",&request).map_err(|_|WorkerError::invalid_params("S05 reservation unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S05ReservePrepare {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s05_reserve_private(&scope.owner,&scope.workspace_id,"s05_reserve_prepare",&request).map_err(|_|WorkerError::invalid_params("S05 reservation unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S05Reserve {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s05_reserve_private(&scope.owner,&scope.workspace_id,"s05_reserve",&request).map_err(|_|WorkerError::invalid_params("S05 reservation unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S05ReserveOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.s05_reserve_private(&scope.owner,&scope.workspace_id,"s05_reserve_original",&request).map_err(|_|WorkerError::invalid_params("S05 reservation unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftCapability {version,owner,workspace_id,account_id} => {
                if version!=1 {return Err(WorkerError::invalid_params("skill draft unavailable",None));}
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_capability(&scope.owner,&scope.workspace_id,&account_id).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftPrepare {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"prepare",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftPrepareOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"prepare_original",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftReserve {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"reserve",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftReserveOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"reserve_original",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftDispatch {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"dispatch",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"original",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftPreparedCancel {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"prepared_cancel",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftTerminal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"terminal",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftRevoke {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"revoke",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftResultOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"result_original",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftRetire {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"retire",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::SkillDraftRetireOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.skill_draft_private(&scope.owner,&scope.workspace_id,"retire_original",&request).map_err(|_|WorkerError::invalid_params("skill draft unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ModelPrepare {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.selection.owner!=scope.owner || request.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("prepare",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ModelPrepareOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.selection.owner!=scope.owner || request.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("prepare_original",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ModelReserve {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.original.selection.owner!=scope.owner || request.original.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("reserve",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ModelReserveOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.original.selection.owner!=scope.owner || request.original.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("reserve_original",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ModelDispatch {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.original.selection.owner!=scope.owner || request.original.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("dispatch",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08ModelOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.original.selection.owner!=scope.owner || request.original.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("original",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyPreparedCancel {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.selection.owner!=scope.owner || request.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 cancellation unavailable",None))?;
                let result=self.provider.s08_model_private("prepared_cancel",&value).map_err(|_|WorkerError::invalid_params("S08 cancellation unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyTerminal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.attempt.original.selection.owner!=scope.owner || request.attempt.original.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 terminal unavailable",None))?;
                let result=self.provider.s08_model_private("terminal",&value).map_err(|_|WorkerError::invalid_params("S08 terminal unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyCommit {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.attempt.original.selection.owner!=scope.owner || request.attempt.original.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("commit",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08PolicyResultOriginal {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.attempt.original.selection.owner!=scope.owner || request.attempt.original.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let value=serde_json::to_value(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                let result=self.provider.s08_model_private("result_original",&value).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::JournalDeltaStatus {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let result=self.provider.journal_delta_status(&request).map_err(|_|WorkerError::invalid_params("S08 model unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::S08RulesOnce {owner,workspace_id,request} => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                if request.selection.owner!=scope.owner || request.selection.workspace_id!=scope.workspace_id {return Err(WorkerError::invalid_params("S08 scope unavailable",None));}
                let result=self.provider.s08_rules_once(&request).map_err(|_|WorkerError::invalid_params("S08 rules unavailable",None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::DerivedCompaction {request} => {
                let scope=request_scope(Some(request.owner.clone()),Some(request.workspace_id.clone()),false)?;
                if scope.owner!=request.owner || scope.workspace_id!=request.workspace_id {return Err(WorkerError::invalid_params("maintenance scope unavailable",None));}
                let evidence_id=self.provider.append_derived_compaction(&request)
                    .map_err(|_|WorkerError::invalid_params("maintenance artifact unavailable",None))?;
                Ok(serde_json::json!({"version":1,"evidence_id":evidence_id}))
            }
            HostCandidateReviewParams::CandidateEmbedding { owner,workspace_id,candidate_id,candidate_digest } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.prepare_host_candidate_embedding(&scope.owner,&scope.workspace_id,&candidate_id,&candidate_digest).await
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::CandidateApplyPrepared { owner,workspace_id,candidate_id,candidate_digest,expected_v2_head,selected_paths,request_key,reason,projection,embedding } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.apply_prepared_host_candidate_review(&scope.owner,&scope.workspace_id,&candidate_id,&candidate_digest,expected_v2_head,&selected_paths,&request_key,&reason,&projection,embedding.as_deref())
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::RejectPreview { owner, workspace_id, candidate_id } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let preview=self.provider.preview_host_candidate_rejection(&scope.owner,&scope.workspace_id,&candidate_id)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(preview))
            }
            HostCandidateReviewParams::RejectApply { owner,workspace_id,candidate_id,candidate_digest,association_sha256,source_account_id,source_chat_id,request_key,reason } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.reject_host_candidate(&scope.owner,&scope.workspace_id,&candidate_id,&candidate_digest,&association_sha256,&source_account_id,&source_chat_id,&request_key,&reason)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::Preview { owner, workspace_id, candidate_id, reason } => {
                let scope = request_scope(Some(owner), Some(workspace_id), false)?;
                let preview = self.provider.preview_host_candidate_review(&scope.owner, &scope.workspace_id, &candidate_id, &reason)
                    .map_err(|error| WorkerError::invalid_params(error, None))?;
                Ok(serde_json::json!(preview))
            }
            HostCandidateReviewParams::Replay { owner, workspace_id, candidate_id, candidate_digest, expected_v2_head, selected_paths, request_key, reason } => {
                let scope = request_scope(Some(owner), Some(workspace_id), false)?;
                let receipt = self.provider.lookup_host_candidate_receipt(&scope.owner, &scope.workspace_id, &candidate_id,
                    &request_key, &candidate_digest, expected_v2_head, &selected_paths, &reason)
                    .map_err(|error| WorkerError::invalid_params(error, None))?;
                Ok(serde_json::json!(receipt))
            }
            HostCandidateReviewParams::Apply { owner, workspace_id, candidate_id, candidate_digest, expected_v2_head, selected_paths, request_key, reason, projection } => {
                let scope = request_scope(Some(owner), Some(workspace_id), false)?;
                let outcome = self.provider.apply_host_candidate_review(&scope.owner, &scope.workspace_id, &candidate_id,
                    &candidate_digest, expected_v2_head, &selected_paths, &request_key, &reason, &projection).await
                    .map_err(|error| WorkerError::invalid_params(error, None))?;
                Ok(serde_json::json!(outcome))
            }
            HostCandidateReviewParams::MirrorPreview { owner,workspace_id,candidate_id,origin_request_key,prior_receipt_id,expected_revision,request_key,reason,selected_paths,payload } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.preview_host_mirror_update(&scope.owner,&scope.workspace_id,&candidate_id,&origin_request_key,&prior_receipt_id,expected_revision,&request_key,&reason,&selected_paths,payload)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::MirrorApply { owner,workspace_id,candidate_id,origin_request_key,prior_receipt_id,expected_revision,request_key,reason,selected_paths,operation_json,operation_sha256 } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.apply_host_mirror_update(&scope.owner,&scope.workspace_id,&candidate_id,&origin_request_key,&prior_receipt_id,expected_revision,&request_key,&selected_paths,&reason,&operation_json,&operation_sha256)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(result))
            }
            HostCandidateReviewParams::MirrorReplay { owner,workspace_id,candidate_id,origin_request_key,prior_receipt_id,expected_revision,request_key,operation_json } => {
                let scope=request_scope(Some(owner),Some(workspace_id),false)?;
                let result=self.provider.replay_host_mirror_update(&scope.owner,&scope.workspace_id,&candidate_id,&origin_request_key,&prior_receipt_id,expected_revision,&request_key,&operation_json)
                    .map_err(|error|WorkerError::invalid_params(error,None))?;
                Ok(serde_json::json!(result))
            }
        }
    }

    async fn review_candidate(
        &self,
        params: CandidateReviewParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("review_candidate")?;
        let scope = request_scope(Some(params.owner), Some(params.workspace_id), false)?;
        let curated_id = self
            .provider
            .review_candidate(
                &params.id,
                params.accept,
                &params.reason,
                &scope.owner,
                &scope.workspace_id,
            )
            .await
            .map_err(|error| WorkerError::invalid_params(error, None))?;
        Ok(serde_json::json!({"reviewed": true, "accepted": params.accept, "curated_id": curated_id}))
    }

    async fn ingest_authored(
        &self,
        params: IngestAuthoredParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("ingest_authored")?;
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let sections: Vec<(String, String)> = params
            .sections
            .into_iter()
            .map(|section| (section.anchor, section.content))
            .collect();
        let result = self
            .provider
            .ingest_authored(
                &scope.owner,
                &scope.workspace_id,
                &params.source_path,
                &sections,
            )
            .await
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!(result))
    }

    async fn digest(
        &self,
        params: DigestParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let result = self
            .graph_store
            .digest(&scope.owner, &scope.workspace_id, scope.include_global)
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!(result))
    }

    async fn memory_quality(
        &self,
        params: MemoryQualityParams,
    ) -> Result<Value, WorkerError> {
        let agent_bridge=std::env::var("FM_AGENT_BRIDGE").is_ok_and(|v|matches!(v.trim().to_ascii_lowercase().as_str(),"1"|"true"|"yes"));
        let fixed_scope=std::env::var("FM_OWNER").is_ok_and(|v|!v.trim().is_empty()) && std::env::var("FM_WORKSPACE_ID").is_ok_and(|v|!v.trim().is_empty());
        let trusted_caller=std::env::var("FM_SCOPE_AUTHORITY").is_ok_and(|v|v.eq_ignore_ascii_case("trusted-caller"));
        if (agent_bridge && !fixed_scope) || (!fixed_scope && (!trusted_caller || params.owner.as_deref().is_none_or(|v|v.trim().is_empty()) || params.workspace_id.as_deref().is_none_or(|v|v.trim().is_empty()))) {
            return Err(WorkerError::invalid_params("authenticated memory scope unavailable",None));
        }
        let scope=request_scope(params.owner,params.workspace_id,false)?;
        if params.rebuild_graph_fts.unwrap_or(false) {
            reject_agent_bridge_internal_tool("memory_quality rebuild")?;
            self.provider.rebuild_graph_cue_fts().map_err(|error|WorkerError::internal_error(error.to_string(),None))?;
        }
        let result = self.provider.scoped_quality_status(&scope.owner,&scope.workspace_id)
        .map_err(|error| WorkerError::internal_error(error.to_string(), None))?;
        Ok(serde_json::json!(result))
    }

    async fn owner_lifecycle(
        &self,
        params: OwnerLifecycleParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("owner_lifecycle")?;
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = match params.action.as_str() {
            "stats" => self.graph_store.owner_counts(&scope.owner),
            "purge" => self.graph_store.purge_owner(&scope.owner),
            "rename" => {
                let new_owner = params.new_owner.as_deref().ok_or_else(|| {
                    WorkerError::invalid_params("rename requires new_owner", None)
                })?;
                self.graph_store.rename_owner(&scope.owner, new_owner)
            }
            "reset_preview" | "reset_commit" => self.graph_store.reset_owner(
                &params.action,
                &scope.owner,
                &params.components,
                params.expected_counts.as_ref(),
            ),
            _ => {
                return Err(WorkerError::invalid_params(
                    "action must be stats|purge|rename|reset_preview|reset_commit",
                    None,
                ));
            }
        }
        .map_err(|error| WorkerError::internal_error(error.to_string(), None))?;
        Ok(serde_json::json!(result))
    }

    async fn memory_retention(
        &self,
        params: MemoryRetentionParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = match params.action.as_str() {
            "get" => serde_json::to_value(
                self.graph_store
                    .retention_policy(&scope.owner, &scope.workspace_id)
                    .map_err(|error| WorkerError::internal_error(error, None))?,
            ),
            "set" => {
                reject_agent_bridge_internal_tool("memory_retention set")?;
                let current = self
                    .graph_store
                    .retention_policy(&scope.owner, &scope.workspace_id)
                    .map_err(|error| WorkerError::internal_error(error, None))?;
                let policy = RetentionPolicy {
                    raw_days: params.raw_days.unwrap_or(current.raw_days),
                    candidate_days: params.candidate_days.unwrap_or(current.candidate_days),
                    curated_days: if params.clear_curated_days {
                        None
                    } else {
                        params.curated_days.or(current.curated_days)
                    },
                    graph_days: if params.clear_graph_days {
                        None
                    } else {
                        params.graph_days.or(current.graph_days)
                    },
                    recovery_seconds: params.recovery_seconds.unwrap_or(current.recovery_seconds),
                };
                serde_json::to_value(
                    self.graph_store
                        .set_retention_policy(&scope.owner, &scope.workspace_id, &policy)
                        .map_err(|error| WorkerError::invalid_params(error, None))?,
                )
            }
            "preview_expire" => serde_json::to_value(
                self.graph_store
                    .preview_expire_retention(&scope.owner, &scope.workspace_id)
                    .map_err(|error| WorkerError::internal_error(error, None))?,
            ),
            "expire" => {
                reject_agent_bridge_internal_tool("memory_retention expire")?;
                let closure = match (
                    params.preview_token.as_deref(),
                    params.operation_id.as_deref(),
                ) {
                    (Some(token), Some(operation_id)) => self
                        .graph_store
                        .expire_retention_with_operation(
                            &scope.owner,
                            &scope.workspace_id,
                            token,
                            operation_id,
                        )
                        .map_err(|error| WorkerError::invalid_params(error, None))?,
                    (None, None) => self
                        .graph_store
                        .expire_retention(&scope.owner, &scope.workspace_id)
                        .map_err(|error| WorkerError::internal_error(error, None))?,
                    (Some(_), None) => {
                        return Err(WorkerError::invalid_params(
                            "operation_id is required with preview_token",
                            None,
                        ));
                    }
                    (None, Some(_)) => {
                        return Err(WorkerError::invalid_params(
                            "preview_token is required with operation_id",
                            None,
                        ));
                    }
                };
                serde_json::to_value(closure)
            }
            "status" => {
                let operation_id = params.operation_id.as_deref().ok_or_else(|| {
                    WorkerError::invalid_params("operation_id is required", None)
                })?;
                serde_json::to_value(
                    self.graph_store
                        .retention_operation_status(&scope.owner, &scope.workspace_id, operation_id)
                        .map_err(|error| WorkerError::invalid_params(error, None))?,
                )
            }
            _ => {
                return Err(WorkerError::invalid_params(
                    "action must be get|set|preview_expire|expire|status",
                    None,
                ));
            }
        }
        .map_err(|error| WorkerError::internal_error(error.to_string(), None))?;
        Ok(serde_json::json!(result))
    }

    async fn memory_forget(
        &self,
        params: MemoryForgetParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("memory_forget")?;
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = match params.action.as_str() {
            "preview" => {
                let selector = forget_selector(params.selector_kind.as_deref(), params.selector)?;
                serde_json::to_value(
                    self.graph_store
                        .preview_forget(&scope.owner, &scope.workspace_id, selector)
                        .map_err(lifecycle_error)?,
                )
            }
            "commit" => {
                let selector = forget_selector(params.selector_kind.as_deref(), params.selector)?;
                let token = params.preview_token.ok_or_else(|| {
                    WorkerError::invalid_params("preview_token is required", None)
                })?;
                let operation_id = params.operation_id.ok_or_else(|| {
                    WorkerError::invalid_params("operation_id is required", None)
                })?;
                serde_json::to_value(
                    self.graph_store
                        .commit_forget_with_operation(
                            &scope.owner,
                            &scope.workspace_id,
                            selector,
                            &token,
                            &operation_id,
                        )
                        .map_err(lifecycle_error)?,
                )
            }
            "status" => {
                let operation_id = params.operation_id.ok_or_else(|| {
                    WorkerError::invalid_params("operation_id is required", None)
                })?;
                serde_json::to_value(
                    self.graph_store
                        .forget_operation_status(&scope.owner, &scope.workspace_id, &operation_id)
                        .map_err(|error| WorkerError::invalid_params(error, None))?,
                )
            }
            "restore" => {
                let tombstone_id = params.tombstone_id.ok_or_else(|| {
                    WorkerError::invalid_params("tombstone_id is required", None)
                })?;
                serde_json::to_value(
                    self.graph_store
                        .restore_forget(&scope.owner, &scope.workspace_id, &tombstone_id)
                        .map_err(|error| WorkerError::invalid_params(error, None))?,
                )
            }
            _ => {
                return Err(WorkerError::invalid_params(
                    "action must be preview|commit|status|restore",
                    None,
                ));
            }
        }
        .map_err(|error| WorkerError::internal_error(error.to_string(), None))?;
        Ok(serde_json::json!(result))
    }

    async fn memory_export(
        &self,
        params: ScopedExportParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("memory_export")?;
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = self
            .graph_store
            .export_scope(&scope.owner, &scope.workspace_id)
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!(result))
    }

    async fn memory_explain(
        &self,
        params: MemoryExplainParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = self
            .graph_store
            .explain_memory(&params.id, &scope.owner, &scope.workspace_id)
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!({"explanation": result}))
    }

    async fn list_quarantine(
        &self,
        params: CandidateListParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let rows = self
            .provider
            .list_quarantine(
                Some(&scope.owner),
                Some(&scope.workspace_id),
                params.limit.unwrap_or(100).min(1000),
            )
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!({"quarantine": rows}))
    }

    async fn quarantine_legacy_state(
        &self,
        params: QuarantineMigrationParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("quarantine_legacy_state")?;
        let result = self
            .provider
            .quarantine_legacy_state(
                params.dry_run,
                params
                    .reason
                    .as_deref()
                    .unwrap_or("pre_admission_pipeline_untrusted"),
            )
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!(result))
    }

    async fn recall(
        &self,
        params: RecallParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let mode = match params.mode.as_deref() {
            Some("layer_b") => RecallMode::LayerB,
            _ => RecallMode::LayerA,
        };
        let tier = match params.tier.as_deref() {
            Some("raw") => Tier::Raw,
            _ => Tier::Curated,
        };

        let query = RecallQuery {
            query: params.query,
            mode,
            workspace_id: Some(scope.workspace_id),
            top_k: params.top_k.unwrap_or(10),
            tier,
            owner: Some(scope.owner),
            router: false,
            rerank: false,
        };

        let result = self.provider.recall(&query).await;

        // Structured output: consumers (the Python provider, future graph
        // tools) parse this as JSON. The record is flattened with its score
        // and source label; the GT preamble and prepend context ride along
        // so prose-oriented consumers can still render them.
        let memories: Vec<serde_json::Value> = result
            .memories
            .iter()
            .map(|m| {
                let mut v =
                    serde_json::to_value(&m.record).unwrap_or_else(|_| serde_json::json!({}));
                if let Some(obj) = v.as_object_mut() {
                    obj.insert("score".into(), serde_json::json!(m.score));
                    obj.insert("source_label".into(), serde_json::json!(m.source_label));
                }
                v
            })
            .collect();
        let payload = serde_json::json!({
            "memories": memories,
            "strategy": result.recall_strategy,
            "ground_truth_preamble": result.ground_truth_preamble,
            "prepend_context": result.prepend_context,
        });

        Ok(serde_json::json!(payload))
    }

    async fn search(
        &self,
        params: SearchParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let tier = match params.tier.as_deref() {
            Some("raw") => Tier::Raw,
            _ => Tier::Curated,
        };
        let kind = params.kind.as_deref().and_then(parse_memory_kind);

        let search_params = memery_core::record::SearchParams {
            query: params.query,
            kind,
            scene: params.scene,
            tier,
            limit: params.limit.unwrap_or(10),
            workspace_id: Some(scope.workspace_id),
            owner: Some(scope.owner),
        };

        let result = self.provider.search(&search_params).await;

        Ok(serde_json::to_value(&result).unwrap_or_default())
    }

    async fn get_memory(
        &self,
        params: MemoryGetParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let record = self
            .provider
            .get_curated_record(&params.id, &scope.owner, &scope.workspace_id)
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!({"record": record}))
    }

    async fn list_memories(
        &self,
        params: MemoryListParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let limit = params.limit.unwrap_or(100).clamp(1, 1000);
        let offset = params
            .cursor
            .as_deref()
            .unwrap_or("0")
            .parse::<usize>()
            .map_err(|_| WorkerError::invalid_params("invalid memory cursor", None))?;
        let records = self
            .provider
            .list_curated_records(&scope.owner, &scope.workspace_id, limit, offset)
            .map_err(|error| WorkerError::internal_error(error, None))?;
        let next_cursor = (records.len() == limit).then(|| (offset + records.len()).to_string());
        Ok(serde_json::json!({"records": records, "next_cursor": next_cursor}))
    }

    async fn record_memory_access(
        &self,
        params: MemoryAccessParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        if params.ids.len() > 1000 {
            return Err(WorkerError::invalid_params(
                "at most 1000 memory ids may be accounted",
                None,
            ));
        }
        let updated = self
            .provider
            .record_curated_access(&params.ids, &scope.owner, &scope.workspace_id)
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!({"updated": updated}))
    }

    async fn update_memory(
        &self,
        params: MemoryMutationParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let kind = params.category.as_deref().and_then(parse_memory_kind);
        if params.category.is_some() && kind.is_none() {
            return Err(WorkerError::invalid_params(
                "unsupported memory category",
                None,
            ));
        }
        let updated = self
            .provider
            .update_curated_record(
                &params.id,
                params.content.as_deref(),
                kind,
                params.category.as_deref(),
                params.pinned,
                Some(&scope.owner),
                Some(&scope.workspace_id),
            )
            .await;
        Ok(serde_json::json!({"id": params.id, "updated": updated}))
    }

    async fn resolve_memory(
        &self,
        params: ResolveMemoryParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let resolved = self
            .provider
            .resolve_unknown(
                &params.id,
                params.resolved_by.as_deref(),
                &scope.owner,
                &scope.workspace_id,
            )
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!({"id": params.id, "resolved": resolved}))
    }

    async fn reopen_memory(
        &self,
        params: MemoryGetParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let reopened = self
            .provider
            .reopen_unknown(&params.id, &scope.owner, &scope.workspace_id)
            .map_err(|error| WorkerError::internal_error(error, None))?;
        Ok(serde_json::json!({"id": params.id, "reopened": reopened}))
    }

    async fn delete_memory(
        &self,
        params: MemoryMutationParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let deleted = self
            .provider
            .delete_curated_record(&params.id, Some(&scope.owner), Some(&scope.workspace_id))
            .await;
        Ok(serde_json::json!({"id": params.id, "deleted": deleted}))
    }

    async fn graph_upsert(
        &self,
        params: GraphUpsertParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("graph_upsert")?;
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let input = memery_core::graph::GraphUpsertInput {
            nodes: params.nodes,
            edges: params.edges,
            cues: params.cues,
        };
        let result = self.graph_store.graph_upsert(&scope, &input).map_err(|e| {
            WorkerError::internal_error(format!("graph_upsert failed: {e}"), None)
        })?;
        Ok(serde_json::to_value(&result).unwrap_or_default())
    }

    async fn graph_walk(
        &self,
        params: GraphWalkParams,
    ) -> Result<Value, WorkerError> {
        let scope = request_scope(params.owner, params.workspace_id, params.include_global)?;
        let limit = params.limit.unwrap_or(10);
        let err = |m: String| WorkerError::invalid_params(m, None);
        if params.reconstruction.is_some() && params.op != "reconstruct" {
            return Err(err("reconstruction requires op=reconstruct".into()));
        }
        let payload = match params.op.as_str() {
            "reconstruct" => {
                let request = params.reconstruction.ok_or_else(|| err("missing reconstruction parameters".into()))?;
                request.validate().map_err(err)?;
                let result = self.graph_store.graph_reconstruct(&scope, &request)
                    .map_err(|_| WorkerError::internal_error("Graph reconstruction unavailable", None))?;
                serde_json::to_value(result)
                    .map_err(|_| WorkerError::internal_error("Graph result unavailable", None))?
            }
            "overview" => {
                let overview = self
                    .graph_store
                    .graph_overview(&scope, limit)
                    .map_err(|e| {
                        WorkerError::internal_error(format!("overview failed: {e}"), None)
                    })?;
                let mut value =
                    serde_json::to_value(&overview).unwrap_or_else(|_| serde_json::json!({}));
                if let Some(obj) = value.as_object_mut() {
                    obj.insert("op".into(), serde_json::json!("overview"));
                }
                value
            }
            "cues" => {
                let q = params
                    .query
                    .ok_or_else(|| err("op=cues requires 'query'".into()))?;
                let mut hits = self
                    .graph_store
                    .graph_cues(&scope, &q, limit)
                    .map_err(|e| {
                        WorkerError::internal_error(format!("cues failed: {e}"), None)
                    })?;
                // Structural re-rank (RWR): well-connected candidates beat
                // lexically-equal but isolated ones. FTS order breaks ties.
                if hits.len() > 1 {
                    let seeds: Vec<String> = hits.iter().map(|h| h.node.id.clone()).collect();
                    if let Ok(scores) = self.graph_store.graph_rwr(&scope, &seeds, 0.25, 20) {
                        let mut indexed: Vec<(usize, _)> = hits.drain(..).enumerate().collect();
                        indexed.sort_by(|(ia, a), (ib, b)| {
                            let sa = scores.get(&a.node.id).copied().unwrap_or(0.0);
                            let sb = scores.get(&b.node.id).copied().unwrap_or(0.0);
                            sb.partial_cmp(&sa)
                                .unwrap_or(std::cmp::Ordering::Equal)
                                .then(ia.cmp(ib))
                        });
                        hits = indexed.into_iter().map(|(_, h)| h).collect();
                    }
                }
                serde_json::json!({ "op": "cues", "hits": hits })
            }
            "rank" => {
                let seeds: Vec<String> = params
                    .node_id
                    .into_iter()
                    .chain(params.dst_id.into_iter())
                    .collect();
                if seeds.is_empty() {
                    return Err(err(
                        "op=rank requires 'node_id' (and optionally 'dst_id') as seeds".into(),
                    ));
                }
                let scores = self
                    .graph_store
                    .graph_rwr(&scope, &seeds, 0.25, 20)
                    .map_err(|e| {
                        WorkerError::internal_error(format!("rank failed: {e}"), None)
                    })?;
                let mut ranked: Vec<(String, f64)> = scores.into_iter().collect();
                ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
                ranked.truncate(limit);
                serde_json::json!({ "op": "rank", "scores": ranked })
            }
            "tags" => {
                let n = params
                    .node_id
                    .ok_or_else(|| err("op=tags requires 'node_id'".into()))?;
                let tags = self.graph_store.graph_tags(&scope, &n).map_err(|e| {
                    WorkerError::internal_error(format!("tags failed: {e}"), None)
                })?;
                serde_json::json!({ "op": "tags", "node_id": n, "tags": tags })
            }
            "expand" => {
                let n = match (params.node_id, params.node_kind.as_deref(), params.node_name.as_deref()) {
                    (Some(id), None, None) => id,
                    (None, Some(kind), Some(name)) => scope.node_id(kind, name),
                    _ => return Err(err("op=expand requires 'node_id' or both 'node_kind' and 'node_name'".into())),
                };
                let (hits, receipt) = self
                    .graph_store
                    .graph_expand_receipt(
                        &scope,
                        &n,
                        params.tag.as_deref(),
                        params.direction.as_deref(),
                        limit,
                    )
                    .map_err(|e| {
                        WorkerError::internal_error(format!("expand failed: {e}"), None)
                    })?;
                serde_json::json!({ "op": "expand", "node_id": n, "hits": hits, "receipt": receipt })
            }
            "entities" => {
                let mut value = self
                    .graph_store
                    .entity_lookup(&scope, params.alias.as_deref(), params.entity_type.as_deref(), limit)
                    .map_err(|e| err(format!("entities failed: {e}")))?;
                if let Some(obj) = value.as_object_mut() {
                    obj.insert("op".into(), serde_json::json!("entities"));
                }
                value
            }
            "fetch" => {
                let n = params
                    .node_id
                    .ok_or_else(|| err("op=fetch requires 'node_id'".into()))?;
                let hit = self.graph_store.graph_fetch(&scope, &n).map_err(|e| {
                    WorkerError::internal_error(format!("fetch failed: {e}"), None)
                })?;
                match hit {
                    Some((node, content)) => {
                        serde_json::json!({ "op": "fetch", "node": node, "ref_content": content })
                    }
                    None => serde_json::json!({ "op": "fetch", "node": null }),
                }
            }
            "trace" => {
                let n = params
                    .node_id
                    .ok_or_else(|| err("op=trace requires 'node_id'".into()))?;
                let (paths, receipt) = self
                    .graph_store
                    .graph_trace_receipt(
                        &scope,
                        &n,
                        params.dst_id.as_deref(),
                        params.max_depth.unwrap_or(4),
                        limit,
                    )
                    .map_err(|e| {
                        WorkerError::internal_error(format!("trace failed: {e}"), None)
                    })?;
                serde_json::json!({ "op": "trace", "paths": paths, "receipt": receipt })
            }
            other => {
                return Err(err(format!(
                    "unknown op '{other}' — expected overview|cues|tags|expand|fetch|trace|rank|entities|reconstruct"
                )))
            }
        };
        Ok(serde_json::json!(payload))
    }

    async fn code_query(&self,params: CodeQueryParams)->Result<Value,WorkerError>{
        reject_agent_bridge_internal_tool("code_query")?;
        let invalid=|message:&str|WorkerError::invalid_params(message.to_owned(),None);
        let authority_error=|error:String|if error.starts_with("denied:"){invalid("Code workspace authority unavailable")}else{WorkerError::internal_error("Code workspace authority unavailable",None)};
        let binding:memery_core::code::WorkspaceBinding=serde_json::from_value(params.binding.clone()).map_err(|_|invalid("Invalid trusted code binding"))?;
        let visual=visual_request(&params,&binding.origin)?;
        let scope=code_binding_scope(&binding,false)?;
        let binding_key=query_binding_key(&binding);
        if params.scip_request.is_some() && (params.action!="start" || !matches!(binding.origin.as_str(),"app"|"agent")
            || params.query.is_some() || params.token.is_some() || params.source_claim.is_some()
            || params.read_fence_only || params.selection_policy_fence || params.visual_request.is_some()
            || params.retrospective_request.is_some() || params.topology_request.is_some()
            || params.topology_cursor.is_some() || params.admitted_code_vectors.is_some()
            || params.code_vector_index.is_some()) {
            return Err(invalid("Invalid private SCIP observation request"));
        }
        if params.code_vector_index.is_some() && (params.action!="start" || binding.origin!="app"
            || params.query.is_some() || params.token.is_some() || params.source_claim.is_some()
            || params.read_fence_only || params.selection_policy_fence || params.visual_request.is_some()
            || params.retrospective_request.is_some() || params.topology_request.is_some()
            || params.topology_cursor.is_some() || params.admitted_code_vectors.is_some()) {
            return Err(invalid("Invalid private Code vector index request"));
        }
        // Private topology pages are app transport bytes, not model query tokens.
        // They use the same binding, task map, lane and cancellation owner.
        if params.action=="start" && params.topology_request.is_some() {
            if binding.origin!="app" || visual.is_some() || params.query.is_some() || params.source_claim.is_some() || params.read_fence_only || params.selection_policy_fence || params.retrospective_request.is_some() || params.token.is_some() || params.topology_cursor.is_some() {return Err(invalid("Invalid private topology request"));}
            let raw=params.topology_request.as_ref().unwrap();
            if serde_json::to_vec(raw).map_or(true,|v|v.len()>196*1024){return Err(invalid("Invalid private topology request"));}
            let request:memery_core::code_topology::Request=serde_json::from_value(raw.clone()).map_err(|_|invalid("Invalid private topology request"))?;
            request.validate().map_err(|_|invalid("Invalid private topology request"))?;binding.validate().map_err(authority_error)?;
            let token=format!("query_{}",generate_id());let cancel=Arc::new(std::sync::atomic::AtomicBool::new(false));let deadline=std::time::Instant::now()+std::time::Duration::from_secs(120);
            {
                let mut tasks=self.code_queries.lock().unwrap();
                let active=tasks.values().filter(|t|t.result.is_none()||t.topology_reserved>0).count();
                let reserved=tasks.values().try_fold(0usize,|n,t|n.checked_add(t.topology_reserved)).ok_or_else(||invalid("Topology capacity unavailable"))?;
                if active>=4 || reserved.checked_add(request.limits.bytes).is_none_or(|n|n>memery_core::code_topology::MAX_BYTES){return Err(invalid("Topology capacity unavailable"));}
                tasks.insert(token.clone(),CodeQueryTask{binding_key,cancel:cancel.clone(),result:None,completed:None,expires:Some(deadline),topology_reserved:request.limits.bytes,topology:None});
            }
            let tasks=self.code_queries.clone();let expiry_tasks=tasks.clone();let expiry_token=token.clone();
            tokio::spawn(async move {tokio::time::sleep(std::time::Duration::from_secs(120)).await;let mut tasks=expiry_tasks.lock().unwrap();if let Some(task)=tasks.get_mut(&expiry_token){task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);if task.completed.is_some(){tasks.remove(&expiry_token);}}});
            let store=self.graph_store.clone();let lane=self.code_query_lane.clone();let job_token=token.clone();
            tokio::spawn(async move {
                let permit=lane.acquire_owned().await.expect("code query lane remains open");
                let result=tokio::task::spawn_blocking(move || {let _permit=permit;let _interactive=store.index_scheduler().interactive_activity();store.code_indexed_topology(&scope,&binding,&request,&cancel)}).await.unwrap_or_else(|_|Err(memery_core::code_topology::unavailable()));
                let mut tasks=tasks.lock().unwrap();if let Some(task)=tasks.get_mut(&job_token){
                    task.completed=Some(std::time::Instant::now());
                    if task.expires.is_none_or(|at|std::time::Instant::now()>=at){tasks.remove(&job_token);} else if task.cancel.load(std::sync::atomic::Ordering::Relaxed) {task.result=Some(Err(memery_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()}));} else {match result {Ok(capture)=>{task.topology=Some(capture);task.result=Some(Ok(serde_json::json!({})));},Err(error)=>task.result=Some(Err(error))}}
                }
            });
            return Ok(serde_json::json!({"state":"running","token":token}));
        }
        if matches!(params.action.as_str(),"poll"|"cancel"|"release") {
            if let Some(token)=params.token.as_ref() {
                let (is_topology,missing)={let tasks=self.code_queries.lock().unwrap();(tasks.get(token).is_some_and(|t|t.topology_reserved>0),!tasks.contains_key(token))};
                if params.action=="release" && missing {binding.validate_cleanup().map_err(authority_error)?;return Ok(serde_json::json!({"state":"released","token":token}));}
                if is_topology {
                    if params.action!="poll" {binding.validate_cleanup().map_err(authority_error)?;}
                    let mut tasks=self.code_queries.lock().unwrap();
                    if params.action=="release" && !tasks.contains_key(token) {return Ok(serde_json::json!({"state":"released","token":token}));}
                    let task=tasks.get_mut(token).filter(|t|t.binding_key==binding_key).ok_or_else(||invalid("Code query token unavailable"))?;
                    if params.action=="release" {if task.completed.is_none(){task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);return Ok(serde_json::json!({"state":"running","token":token}));}tasks.remove(token);return Ok(serde_json::json!({"state":"released","token":token}));}
                    if task.expires.is_none_or(|at|std::time::Instant::now()>=at) {task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);return Err(invalid("Topology capture expired"));}
                    if params.action=="cancel" {task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);return Ok(serde_json::json!({"state":"cancelled","token":token}));}
                    if task.cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(invalid("Topology capture cancelled"));}
                    if task.result.is_none(){drop(tasks);binding.validate().map_err(authority_error)?;return Ok(serde_json::json!({"state":"running","token":token}));}
                    if let Some(Err(error))=task.result.as_ref(){let error=error.clone();drop(tasks);return Ok(serde_json::json!({"state":"failed","token":token,"error":error}));}
                    let capture=task.topology.as_ref().ok_or_else(||invalid("Topology capture unavailable"))?;
                    let sequence=match params.topology_cursor.as_deref(){None=>0,Some(v)=>{let n:usize=v.parse().map_err(|_|invalid("Invalid topology cursor"))?;if n==0||n.to_string()!=v{return Err(invalid("Invalid topology cursor"));}n}};
                    let total_pages=capture.bytes.len().div_ceil(capture.page_bytes);if sequence>=total_pages{return Err(invalid("Invalid topology cursor"));}
                    let start=sequence.checked_mul(capture.page_bytes).ok_or_else(||invalid("Invalid topology cursor"))?;let end=(start+capture.page_bytes).min(capture.bytes.len());let data=&capture.bytes[start..end];let digest=capture.result_sha256.clone();
                    let terminal=if sequence+1==total_pages {serde_json::json!({"enumeration_complete":true,"total_bytes":capture.bytes.len(),"total_pages":total_pages,"file_count":capture.file_count,"node_count":capture.node_count,"relation_count":capture.relation_count,"result_sha256":digest,"scope_sha256":capture.scope_sha256})}else{serde_json::Value::Null};
                    let receipt=capture.receipt.clone();let value=serde_json::json!({"version":1,"profile":memery_core::code_topology::PROFILE,"capture_id":digest,"sequence":sequence,"cursor":params.topology_cursor,"next_cursor":if sequence+1<total_pages{Some((sequence+1).to_string())}else{None},"page_sha256":memery_core::code_topology::sha(data),"data_base64":topology_base64(data),"receipt":receipt,"scope_sha256":capture.scope_sha256,"producer_fingerprint":capture.producer_fingerprint,"terminal":terminal});drop(tasks);
                    let permit=match self.code_query_lane.clone().try_acquire_owned(){Ok(v)=>v,Err(_)=>{binding.validate().map_err(authority_error)?;return Ok(serde_json::json!({"state":"running","token":token}));}};
                    self.graph_store.code_query_check(&scope,&binding,&receipt).map_err(|_|invalid("Topology receipt changed"))?;self.graph_store.code_read_fence_measured(&scope,&binding,Some(&receipt)).map_err(|_|invalid("Topology authority changed"))?;drop(permit);
                    let tasks=self.code_queries.lock().unwrap();let valid=tasks.get(token).is_some_and(|t|t.binding_key==binding_key&&!t.cancel.load(std::sync::atomic::Ordering::Relaxed)&&t.expires.is_some_and(|at|std::time::Instant::now()<at));drop(tasks);if !valid{return Err(invalid("Topology capture expired"));}
                    let envelope=serde_json::json!({"state":"completed","token":token,"result":value});let encoded=serde_json::to_string(&envelope).map_err(|_|invalid("Topology page unavailable"))?;if encoded.len()>memery_core::code_topology::MAX_ENVELOPE_BYTES{return Err(invalid("Topology page unavailable"));}return Ok(serde_json::from_str(&encoded).map_err(|_|WorkerError::internal_error("invalid encoded result", None))?);
                }
            }
        }
        let payload=match params.action.as_str(){
            "start"=>{
                let retrospective:Option<memery_core::code_retrospective::Request>=if let Some(raw)=params.retrospective_request {
                    if binding.origin!="app" || visual.is_some() || params.query.is_some() || params.source_claim.is_some() || params.read_fence_only || params.selection_policy_fence || serde_json::to_vec(&raw).map_or(true,|v|v.len()>196*1024) {return Err(invalid("Invalid private retrospective request"));}
                    Some(serde_json::from_value(raw).map_err(|_|invalid("Invalid private retrospective request"))?)
                } else {None};
                let index_request:Option<memery_core::retrieval::code_vector_index::IndexRequest>=if let Some(raw)=params.code_vector_index {
                    if serde_json::to_vec(&raw).map_or(true,|bytes|bytes.len()>196*1024) {return Err(invalid("Invalid private Code vector index request"));}
                    Some(serde_json::from_value(raw).map_err(|_|invalid("Invalid private Code vector index request"))?)
                } else {None};
                let scip: Option<memery_core::code_scip::Request> = if let Some(raw) = params.scip_request {
                    if raw.get("index_base64").and_then(serde_json::Value::as_str)
                        .is_none_or(|data| data.len() > memery_core::code_scip::MAX_ENCODED_BYTES)
                        || serde_json::to_vec(&raw).map_or(true, |bytes| bytes.len() > memery_core::code_scip::MAX_ENCODED_BYTES + 16384)
                    {
                        return Err(invalid("Invalid private SCIP observation request"));
                    }
                    Some(serde_json::from_value(raw).map_err(|_| invalid("Invalid private SCIP observation request"))?)
                } else { None };
                let query:Option<memery_core::code_query::CodeQuery>=if visual.is_some() || retrospective.is_some() || index_request.is_some() || scip.is_some(){None}else{
                    let query=serde_json::from_value(params.query.ok_or_else(||invalid("Code query required"))?).map_err(|_|invalid("Invalid public code query"))?;
                    memery_core::code_query::validate_query(&query).map_err(|_|invalid("Code query exceeds its finite contract"))?;Some(query)
                };
                let source_claim:Option<memery_core::code_evidence::SourceClaim>=params.source_claim.map(serde_json::from_value).transpose().map_err(|_|invalid("Invalid private source claim"))?;
                let admitted_code_vectors:Option<memery_core::retrieval::code_vectors::AdmittedCodeVectors> =
                    if let Some(raw)=params.admitted_code_vectors {
                        if visual.is_some() || retrospective.is_some() || source_claim.is_some()
                            || params.read_fence_only || params.selection_policy_fence
                            || query.as_ref().is_none_or(|q| q.operation!=memery_core::code_query::Operation::Search
                                || q.expected_receipt.is_none() || !q.channels.contains(&memery_core::code_query::Channel::Semantic))
                            || serde_json::to_vec(&raw).map_or(true,|bytes|bytes.len()>196*1024)
                        {return Err(invalid("Invalid private Code vector inputs"));}
                        Some(serde_json::from_value(raw).map_err(|_| invalid("Invalid private Code vector inputs"))?)
                    } else {None};
                if params.read_fence_only && source_claim.is_some(){return Err(invalid("Read fence cannot carry source claims"));}
                if params.selection_policy_fence && !params.read_fence_only{return Err(invalid("Selection policy fence requires read fence mode"));}
                let selection_policy_fence=params.selection_policy_fence;
                let read_fence_only=params.read_fence_only;
                if let Some(claim)=&source_claim{
                    if query.as_ref().is_none_or(|q|q.expected_receipt.is_none()) || !matches!(&claim.predicate,memery_core::code_evidence::SourcePredicate::ExactText{text} if text.len()<=32768){return Err(invalid("Expected receipt and bounded Files ExactText claim required"));}
                }
                // Scheduling discloses no evidence. The worker reacquires the
                // original exclusive authority fence before any snapshot/read.
                binding.validate().map_err(authority_error)?;
                let token=format!("query_{}",generate_id());let cancel=Arc::new(std::sync::atomic::AtomicBool::new(false));
                {
                    let mut tasks=self.code_queries.lock().unwrap();
                    if tasks.values().filter(|t|t.result.is_none() || t.topology_reserved>0).count()>=4{return Err(WorkerError::invalid_params("Code query capacity exhausted",Some(serde_json::json!({"code":"busy"}))));}
                    let mut completed:Vec<_>=tasks.iter().filter(|(_,t)|t.topology_reserved==0).filter_map(|(k,t)|t.completed.map(|at|(at,k.clone()))).collect();completed.sort();
                    let remove=completed.len().saturating_sub(31);for(_,key)in completed.into_iter().take(remove){tasks.remove(&key);}
                    tasks.insert(token.clone(),CodeQueryTask{binding_key,cancel:cancel.clone(),result:None,completed:None,expires:None,topology_reserved:0,topology:None});
                }
                let store=self.graph_store.clone();let tasks=self.code_queries.clone();let job_token=token.clone();let secret=self.code_query_secret;let lane=self.code_query_lane.clone();
                tokio::spawn(async move{
                    #[cfg(feature="s03-fixtures")]
                    let gate=fixture_queued_gate(&job_token).await;
                    #[cfg(not(feature="s03-fixtures"))]
                    let gate:Result<(),String>=Ok(());
                    let result=if gate.is_err(){Err(memery_core::code_query::QueryError{code:"unavailable".into(),message:"Code fixture gate failed".into()})}else{
                        // Owned by the blocking worker, even if its async waiter
                        // is dropped. No task-map lock or RPC queue is held.
                        let mut visual_timing=visual.as_ref().and_then(|_|VisualTiming::begin(visual_timings_enabled(),&VISUAL_TIMING_RECORDS));
                        let permit=lane.acquire_owned().await.expect("code query lane remains open");
                        if let Some(timing)=&mut visual_timing{timing.lane_acquired();}
                        let timing_token=job_token.clone();
                        match tokio::task::spawn_blocking(move||{
                            let _permit=permit;
                            let _interactive=store.index_scheduler().interactive_activity();
                            if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(memery_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()});}
                            if let Some(request)=visual{
                                let work=||store.code_visual(&scope,&binding,&request,&cancel,&secret).and_then(|result|serde_json::to_value(result).map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Code map serialization unavailable".into()}));
                                let result=if let Some(timing)=&visual_timing{timing.context.enter(work)}else{work()};
                                if let Some(timing)=visual_timing{timing.emit(&timing_token,VisualTimingPhase::Worker);}
                                return result;
                            }
                            if let Some(request)=retrospective {
                                store.code_read_fence_measured(&scope,&binding,Some(&request.receipt))?;
                                store.code_query_check(&scope,&binding,&request.receipt)?;
                                let receipt=request.receipt.clone();
                                let result=memery_core::code_retrospective::extract(request).map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Retrospective evidence unavailable".into()})?;
                                store.code_query_check(&scope,&binding,&receipt)?;
                                store.code_read_fence_measured(&scope,&binding,Some(&receipt))?;
                                if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(memery_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()});}
                                return serde_json::to_value(result).map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Retrospective evidence unavailable".into()});
                            }
                            if let Some(request)=index_request {
                                return store.code_vector_index(&scope,&binding,request).and_then(|result|serde_json::to_value(result).map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Code vector index serialization unavailable".into()}));
                            }
                            if let Some(request) = scip {
                                let receipt = request.receipt.clone();
                                let before = store.code_read_fence(&scope, &binding, Some(&receipt))?;
                                let result = memery_core::code_scip::run(request, &cancel)?;
                                if store.code_read_fence(&scope, &binding, Some(&receipt))? != before {
                                    return Err(memery_core::code_query::QueryError {
                                        code:"stale_cursor".into(), message:"Code authority changed during SCIP observation".into(),
                                    });
                                }
                                return serde_json::to_value(result).map_err(|_| memery_core::code_query::QueryError {
                                    code:"unavailable".into(), message:"SCIP observation unavailable".into(),
                                });
                            }
                            let query=query.expect("validated evidence query or visual request");
                            if read_fence_only {
                                if selection_policy_fence {
                                    // The selection-policy scan has separate file-I/O accounting;
                                    // retain its existing contract without claiming zero scan work.
                                    return store.code_policy_fence(&scope,&binding,query.expected_receipt.as_ref()).map(|fence|serde_json::json!({"private_read_fence":fence,"private_policy_fence":true,"receipt":null}));
                                }
                                let (fence,metadata_bytes)=store.code_read_fence_measured(&scope,&binding,query.expected_receipt.as_ref())?;
                                return measured_read_fence(fence,metadata_bytes,&query.budgets);
                            }
                            let mut result=if let Some(vectors)=admitted_code_vectors.as_ref(){
                                store.code_search_with_vectors(&scope,&binding,&query,Some(vectors),&cancel,&secret)?.into_public_result()
                            } else { store.code_query(&scope,&binding,&query,&cancel,&secret)? };
                            let mut value=serde_json::to_value(&result).map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Code serialization unavailable".into()})?;
                            if let Some(claim)=source_claim{
                                if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(memery_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()});}
                                let(validation,bytes)=store.code_validate_source_measured(&scope,&binding,&result.receipt.as_ref().unwrap().evidence,&claim);
                                result.cost.validation_bytes_read=bytes;
                                value["cost"]["validation_bytes_read"]=serde_json::json!(bytes);
                                value["source_validation"]=match validation{Ok(v)=>serde_json::to_value(v).unwrap(),Err(e)=>serde_json::json!({"state":"unavailable","reason":"Current source validation unavailable","error_code":e.code})};
                                store.code_query_check(&scope,&binding,result.receipt.as_ref().unwrap())?;
                            }
                            if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(memery_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()});}
                            Ok(value)
                        }).await{
                            Ok(result)=>result.and_then(|v|serde_json::to_value(v).map_err(|_|memery_core::code_query::QueryError{code:"unavailable".into(),message:"Code query serialization failed".into()})),
                            Err(_)=>Err(memery_core::code_query::QueryError{code:"unavailable".into(),message:"Code query worker failed".into()}),
                        }
                    };
                    #[cfg(feature="s03-fixtures")]
                    let rejected=result.is_err();
                    {
                        let mut tasks=tasks.lock().unwrap();
                        if let Some(task)=tasks.get_mut(&job_token){
                            task.result=Some(if task.cancel.load(std::sync::atomic::Ordering::Relaxed){Err(memery_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()})}else{result});task.completed=Some(std::time::Instant::now());
                        }
                        let mut completed:Vec<_>=tasks.iter().filter(|(_,t)|t.topology_reserved==0).filter_map(|(k,t)|t.completed.map(|at|(at,k.clone()))).collect();completed.sort();
                        let remove=completed.len().saturating_sub(32);for(_,key)in completed.into_iter().take(remove){tasks.remove(&key);}
                    }
                    #[cfg(feature="s03-fixtures")]
                    fixture_finished(&job_token,rejected);
                });
                serde_json::json!({"state":"running","token":token})
            }
            "poll"|"cancel"=>{
                let token=params.token.ok_or_else(||invalid("Code query token required"))?;
                if token.len()>128{return Err(invalid("Invalid code query token"));}
                if params.action=="cancel"{binding.validate_cleanup().map_err(authority_error)?;}
                // Clone bounded completed payload, then release task lock before
                // policy/SQLite checks. No lock order inversion with a worker.
                let(result,cancel)={let mut tasks=self.code_queries.lock().unwrap();let task=tasks.get_mut(&token).filter(|t|t.binding_key==binding_key).ok_or_else(||invalid("Code query token unavailable"))?;
                    let result=if params.action=="cancel"{task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);None}else if task.cancel.load(std::sync::atomic::Ordering::Relaxed){Some(Err(memery_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()}))}else{task.result.clone()};(result,task.cancel.clone())
                };
                if params.action=="cancel"{
                    serde_json::json!({"state":"cancelled","token":token,"error":{"code":"cancelled","message":"Code query cancelled"}})
                }else{
                    // Completed disclosure also uses the lane: never wait for
                    // a worker's exclusive fence inside the single RPC owner.
                    // While occupied, disclose only authenticated running state.
                    let permit=if result.is_some(){match self.code_query_lane.clone().try_acquire_owned(){
                        Ok(permit)=>Some(permit),
                        Err(_)=>{binding.validate().map_err(authority_error)?;return Ok(serde_json::json!({"state":"running","token":token}));}
                    }}else{None};
                    if result.is_none() {
                        // A running status carries no evidence. Validate current
                        // access without contending with the worker's exclusive
                        // authority fence while it captures a bounded snapshot.
                        // Completed payloads still require the original fence
                        // and receipt/head recheck below before disclosure.
                        binding.validate().map_err(authority_error)?;
                    } else if !matches!(&result,Some(Ok(value)) if value.get("mode").is_some()) {
                        let guard=binding.publication_guard().map_err(authority_error)?;drop(guard);
                    }
                    match result{
                        None=>serde_json::json!({"state":"running","token":token}),
                        Some(Err(error))=>serde_json::json!({"state":"failed","token":token,"error":error}),
                        Some(Ok(value))=>{
                            if value.get("mode").is_some(){
                                // Preview has no evidence receipt. Its bounded
                                // inventory fence must run on a blocking worker,
                                // not the async RPC executor or task-map lock.
                                let result:memery_core::code_visual::VisualResult=serde_json::from_value(value.clone()).map_err(|_|invalid("Code map result unavailable"))?;
                                let authority=match result{memery_core::code_visual::VisualResult::Map(r)=>r.authority,memery_core::code_visual::VisualResult::Selection(r)=>r.authority};
                                let store=self.graph_store.clone();let current_binding=binding.clone();let current_scope=scope.clone();
                                let check_cancel=cancel.clone();
                                // Delivery uses try_acquire above: it never
                                // waits inside this RPC for a busy lane. This
                                // record's lane_wait_ns is therefore zero;
                                // running polls/backoff are not hidden here.
                                let timing=VisualTiming::begin(visual_timings_enabled(),&VISUAL_TIMING_RECORDS);
                                let timing_token=token.clone();
                                let checked=tokio::task::spawn_blocking(move||{
                                    let _permit=permit;
                                    let _interactive=store.index_scheduler().interactive_activity();
                                    let work=||store.code_visual_check(&current_scope,&current_binding,&authority,&check_cancel);
                                    let result=if let Some(timing)=&timing{timing.context.enter(work)}else{work()};
                                    if let Some(timing)=timing{timing.emit(&timing_token,VisualTimingPhase::Delivery);}
                                    result
                                }).await.unwrap_or_else(|_|Err(memery_core::code_query::QueryError{code:"unavailable".into(),message:"Code map delivery unavailable".into()}));
                                return Ok(serde_json::json!(visual_delivery(&token,value,checked,&cancel)));
                            }
                            let _permit=permit;
                            let _interactive=self.graph_store.index_scheduler().interactive_activity();
                            if let Some(expected_fence)=value["private_read_fence"].as_str(){
                                let current=if value["private_policy_fence"].as_bool()==Some(true){self.graph_store.code_policy_fence(&scope,&binding,None)}else{self.graph_store.code_read_fence(&scope,&binding,None)}.map_err(|_|invalid("Code read authority or admission changed"))?;
                                if current!=expected_fence{return Err(invalid("Code read admission or head changed"));}
                                return Ok(serde_json::json!({"state":"completed","token":token,"result":value}));
                            }
                            let receipt:memery_core::code_query::QueryReceipt=serde_json::from_value(value["receipt"].clone()).map_err(|_|invalid("Code result receipt unavailable"))?;
                            self.graph_store.code_query_check(&scope,&binding,&receipt).map_err(|_|invalid("Code result scope or generation changed"))?;
                            if value.get("extractor_fingerprint").is_some() {self.graph_store.code_read_fence_measured(&scope,&binding,Some(&receipt)).map_err(|_|invalid("Code result receipt changed"))?;}
                            serde_json::json!({"state":"completed","token":token,"result":value})
                        }
                    }
                }
            }
            _=>return Err(invalid("Unknown read-only code job action")),
        };
        Ok(serde_json::json!(payload))
    }

    async fn code_index_scheduler(&self,params: CodeIndexSchedulerParams)->Result<Value,WorkerError>{
        reject_agent_bridge_internal_tool("code_index_scheduler")?;
        let request=params.request();
        let status=self.graph_store.index_scheduler().request(&request).map_err(|error|WorkerError::invalid_params(error,None))?;
        Ok(serde_json::to_value(&status).map_err(|_|WorkerError::internal_error("Scheduler serialization unavailable",None))?)
    }

    async fn code_index(
        &self,
        params: CodeIndexParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("code_index")?;
        let err = |m: String| WorkerError::invalid_params(m, None);
        let ierr = |m: String| WorkerError::internal_error(m, None);
        if (params.action=="confirm_removals")!=params.removal_confirmation.is_some()
            ||params.removal_confirmation.as_ref().is_some_and(|value|value.len()!=64||!value.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))){
            return Err(err("Invalid index removal confirmation".into()));
        }
        if params.replace_stale_root && (params.action!="ensure" || params.origin.as_deref()!=Some("app")) {
            return Err(err("Root replacement requires an explicit app folder open".into()));
        }
        let bound=matches!(params.action.as_str(),"ensure"|"admit"|"release"|"cancel"|"preview_removals"|"confirm_removals") || params.root.is_some() || params.owner_subject_id.is_some();
        let fixed=std::env::var("FM_OWNER").ok().is_some_and(|v|!v.trim().is_empty()) && std::env::var("FM_WORKSPACE_ID").ok().is_some_and(|v|!v.trim().is_empty());
        let scope=if bound && fixed && params.origin.as_deref()==Some("app") {
            let cleanup=matches!(params.action.as_str(),"release"|"cancel"|"remove");
            let binding=memery_core::code::WorkspaceBinding {
                owner:params.owner.clone().or_else(||std::env::var("FM_OWNER").ok()).ok_or_else(||err("owner required".into()))?,
                workspace_id:params.workspace_id.clone().or_else(||std::env::var("FM_WORKSPACE_ID").ok()).ok_or_else(||err("workspace_id required".into()))?,
                owner_subject_id:params.owner_subject_id.clone().ok_or_else(||err("owner_subject_id required".into()))?,
                location_id:params.location_id.clone().ok_or_else(||err("location_id required".into()))?,
                workspace_revision:params.workspace_revision.ok_or_else(||err("workspace_revision required".into()))?,
                policy_generation:params.policy_generation.ok_or_else(||err("policy_generation required".into()))?,
                root:if cleanup {params.root.clone().unwrap_or_default()} else {params.root.clone().ok_or_else(||err("root required".into()))?}.into(),
                origin:"app".into(),subscriber_id:params.subscriber_id.clone().ok_or_else(||err("subscriber_id required".into()))?,
                authority_expires_unix_ms:params.authority_expires_unix_ms,
            };
            code_binding_scope(&binding,cleanup)?
        } else {request_scope(params.owner.clone(),params.workspace_id.clone(),false)?};
        if bound {
            let cleanup=matches!(params.action.as_str(),"release"|"cancel"|"remove");
            let binding=memery_core::code::WorkspaceBinding {
                owner:scope.owner.clone(),workspace_id:scope.workspace_id.clone(),
                owner_subject_id:params.owner_subject_id.ok_or_else(||err("owner_subject_id required".into()))?,
                location_id:params.location_id.ok_or_else(||err("location_id required".into()))?,
                workspace_revision:params.workspace_revision.ok_or_else(||err("workspace_revision required".into()))?,
                policy_generation:params.policy_generation.ok_or_else(||err("policy_generation required".into()))?,
                root:if cleanup {params.root.unwrap_or_default()} else {params.root.ok_or_else(||err("root required".into()))?}.into(),
                origin:params.origin.ok_or_else(||err("origin required".into()))?,
                subscriber_id:params.subscriber_id.ok_or_else(||err("subscriber_id required".into()))?,
                authority_expires_unix_ms:params.authority_expires_unix_ms,
            };
            let mut payload=if cleanup {
                let checkouts=self.graph_store.code_workspace_checkouts(&scope,&binding).map_err(lifecycle_error)?;
                if params.action=="remove" {
                    self.graph_store.code_workspace_remove(&scope,&binding).map_err(lifecycle_error)?;
                }
                for checkout in &checkouts {
                    let key=code_task_key(&scope,checkout);
                    if params.action=="release" {
                        if let Some(task)=self.code_tasks.lock().unwrap().get_mut(&key){task.subscribers.remove(&binding.subscriber_id);}
                    } else if params.action=="cancel" {
                        self.graph_store.code_workspace_cancel(&scope,checkout).map_err(lifecycle_error)?;
                        if let Some(task)=self.code_tasks.lock().unwrap().get_mut(&key){task.cancel.store(true,std::sync::atomic::Ordering::Release);task.running=false;task.token=format!("cancelled_{}",generate_id());task.ticket=None;task.error=None;}
                    } else {
                        if let Some(task)=self.code_tasks.lock().unwrap().get_mut(&key){task.cancel.store(true,std::sync::atomic::Ordering::Release);task.running=false;task.token=format!("erased_{}",generate_id());task.ticket=None;task.error=None;task.subscribers.clear();}
                    }
                }
                self.graph_store.index_scheduler().wake();
                serde_json::json!({"lifecycle":if params.action=="remove"{"erased"}else if params.action=="cancel"{"cancelled"}else{"released"},"checkout_id":if checkouts.len()==1{checkouts.first()}else{None},"error":null})
            } else if params.action=="status" {
                self.graph_store.code_workspace_status(&scope,&binding).map_err(lifecycle_error)?
            } else if params.action=="preview_removals" {
                let store=self.graph_store.clone();let preview_scope=scope.clone();let preview_binding=binding.clone();
                let review=tokio::task::spawn_blocking(move||store.code_workspace_removal_preview(&preview_scope,&preview_binding))
                    .await.map_err(|_|ierr("Index removal preview unavailable".into()))?.map_err(lifecycle_error)?;
                let mut status=self.graph_store.code_workspace_status(&scope,&binding).map_err(lifecycle_error)?;
                status["removal_review"]=review;
                status
            } else if matches!(params.action.as_str(),"ensure"|"admit"|"confirm_removals") {
                if params.action=="confirm_removals"&&binding.origin!="app"{return Err(err("Index removal confirmation requires the app".into()));}
                let payload=if params.replace_stale_root {
                    self.graph_store.code_workspace_prepare_app_rebind(&scope,&binding)
                } else {
                    self.graph_store.code_workspace_prepare(&scope,&binding,params.action=="admit")
                }.map_err(lifecycle_error)?;
                let checkout=payload["checkout_id"].as_str().ok_or_else(||ierr("missing checkout receipt".into()))?.to_string();
                let key=code_task_key(&scope,&checkout);
                let ticket=memery_core::code::CodeWorkTicket{checkout_id:checkout,admission_epoch:payload["admission_epoch"].as_i64().ok_or_else(||ierr("missing epoch".into()))?,config_fingerprint:payload["config_fingerprint"].as_str().ok_or_else(||ierr("missing fingerprint".into()))?.into(),policy_generation:binding.policy_generation,workspace_revision:binding.workspace_revision};
                let token=format!("task_{}",generate_id());
                let start={
                    let mut tasks=self.code_tasks.lock().unwrap();
                    let task=tasks.entry(key.clone()).or_default();
                    task.subscribers.insert(binding.subscriber_id.clone());
                    if task.running && task.ticket.as_ref()==Some(&ticket){None}else{
                        task.cancel.store(true,std::sync::atomic::Ordering::Release);
                        task.cancel=Arc::new(std::sync::atomic::AtomicBool::new(false));
                        task.running=true;task.token=token.clone();task.ticket=Some(ticket.clone());task.error=None;
                        // Snapshot the token's cancellation handle under the
                        // same lock; a concurrent successor must not lend its
                        // uncancelled handle to this older admission.
                        Some(task.cancel.clone())
                    }
                };
                if let Some(cancel)=start {
                    let scheduler=self.graph_store.index_scheduler();scheduler.wake();
                    let descriptor_bytes=binding.owner.len()+binding.owner_subject_id.len()+binding.workspace_id.len()+binding.location_id.len()+binding.subscriber_id.len()+binding.root.as_os_str().len()+ticket.checkout_id.len()+ticket.config_fingerprint.len()+256;
                    let admission=match scheduler.reserve(descriptor_bytes){Ok(admission)=>admission,Err(error)=>{if let Some(task)=self.code_tasks.lock().unwrap().get_mut(&key){if task.token==token{task.running=false;task.error=Some(CodeIndexFailure::before_run(&error));}}return Err(lifecycle_error(error));}};
                    let store=self.graph_store.clone();let tasks=self.code_tasks.clone();let job_scope=scope.clone();let job_binding=binding.clone();
                    let removal_confirmation=params.removal_confirmation.clone();
                    tokio::spawn(async move {
                        #[cfg(feature="s03-fixtures")]
                        let gate_error=fixture_queued_gate(&token).await.err();
                        #[cfg(not(feature="s03-fixtures"))]
                        let gate_error:Option<String>=None;
                        let error=if let Some(error)=gate_error{Some(CodeIndexFailure::before_run(&error))}else {
                            match admission.wait(&cancel).await{
                                Err(error)=>Some(CodeIndexFailure::before_run(&error)),
                                Ok(cohort)=>{let result=tokio::task::spawn_blocking(move||{
                                    let failure = std::cell::Cell::new(None);
                                    let outcome = cohort.run(|| {
                                        if cancel.load(std::sync::atomic::Ordering::Acquire) {return Err("cancelled: queued indexing".into());}
                                        store.code_index_reviewed_diagnostic(&job_scope,&job_binding,&ticket,removal_confirmation.as_deref())
                                            .map_err(|error| {failure.set(Some(error)); "index job failed".into()})
                                    });
                                    outcome.map_err(|error| failure.get().unwrap_or_else(|| CodeIndexFailure::before_run(&error)))
                                }).await;
                                    match result{Ok(Ok(_))=>None,Ok(Err(error))=>Some(error),Err(_)=>Some(CodeIndexFailure::before_run("index_worker_failed"))}
                                }
                            }
                        };
                        #[cfg(feature="s03-fixtures")]
                        let rejected=error.is_some();
                        if let Some(task)=tasks.lock().unwrap().get_mut(&key){if task.token==token {task.running=false;task.error=error;}}
                        #[cfg(feature="s03-fixtures")]
                        fixture_finished(&token,rejected);
                    });
                }
                payload
            } else {return Err(err("invalid lifecycle action".into()));};
            if let Some(checkout)=payload["checkout_id"].as_str().map(str::to_owned) {
                let key=code_task_key(&scope,&checkout);
                if let Some(task)=self.code_tasks.lock().unwrap().get(&key) {
                    if task.running && (matches!(params.action.as_str(),"ensure"|"admit"|"confirm_removals") || !matches!(payload["lifecycle"].as_str(),Some("erased"|"released"|"cancelled"|"building"))) {payload["lifecycle"]=serde_json::json!("queued");payload["freshness"]=serde_json::json!(if payload["head_generation"].is_null(){"missing"}else{"stale"});}
                    payload["task_token"]=serde_json::json!(task.token);
                    if let Some(error)=task.error {
                        payload["error"]=error.public_status();
                    }
                    payload["subscribers"]=serde_json::json!(task.subscribers.len());
                }
            }
            scrub_private_root(&mut payload,binding.root.to_str().unwrap_or(""));
            return Ok(serde_json::json!(payload));
        }
        let payload = match params.action.as_str() {
            "index" => {
                return Err(err("legacy index unavailable: use admitted ensure lifecycle".into()));
            }
            "status" => {
                let (files, symbols, last) = self
                    .graph_store
                    .code_status(&scope, &params.path)
                    .map_err(|e| ierr(format!("status failed: {e}")))?;
                let head = self
                    .graph_store
                    .code_index_head(&scope, &params.path)
                    .map_err(|e| ierr(format!("status head failed: {e}")))?;
                serde_json::json!({
                    "repository_id": head.as_ref().map(|h|h.source_snapshot.repository_id.clone()),
                    "files": files,
                    "symbols": symbols,
                    "last_indexed": last,
                    "ready_generation": head,
                    "code_index_lease_seconds": self.graph_store.code_index_lease_seconds(),
                })
            }
            "stale" => {
                return Err(err("legacy stale unavailable: use admitted ensure/status lifecycle".into()));
            }
            "remove" => {
                let removed = self
                    .graph_store
                    .code_remove(&scope, &params.path)
                    .map_err(|e| ierr(format!("remove failed: {e}")))?;
                let repository_id = std::path::Path::new(&params.path)
                    .is_dir()
                    .then(|| memery_core::code::repository_id(std::path::Path::new(&params.path)).ok())
                    .flatten()
                    .unwrap_or_else(|| params.path.clone());
                serde_json::json!({ "repository_id": repository_id, "files_removed": removed })
            }
            "impact" => {
                let rel = params
                    .rel_path
                    .ok_or_else(|| err("action=impact requires 'rel_path'".into()))?;
                let impacted = self
                    .graph_store
                    .code_impact_bounded(
                        &scope,
                        &params.path,
                        &rel,
                        params.max_depth.unwrap_or(4),
                        params.limit.unwrap_or(100).min(1000),
                    )
                    .map_err(|e| ierr(format!("impact failed: {e}")))?;
                let repository_id = std::path::Path::new(&params.path)
                    .is_dir()
                    .then(|| memery_core::code::repository_id(std::path::Path::new(&params.path)).ok())
                    .flatten()
                    .unwrap_or_else(|| params.path.clone());
                serde_json::json!({ "repository_id": repository_id, "rel_path": rel, "impacted_files": impacted.impacted_files, "total": impacted.total, "truncated": impacted.truncated })
            }
            other => {
                return Err(err(format!(
                    "unknown action '{other}' — expected index|status|stale|remove|impact"
                )))
            }
        };
        Ok(serde_json::json!(payload))
    }

    async fn groom(
        &self,
        params: GroomParams,
    ) -> Result<Value, WorkerError> {
        reject_agent_bridge_internal_tool("groom")?;
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let op = match params.op.as_str() {
            "decay" => GroomOp::Decay,
            "dedup" => GroomOp::Dedup,
            "reflect" => GroomOp::Reflect,
            "edge_decay" => GroomOp::EdgeDecay,
            "tag_normalize" => GroomOp::TagNormalize,
            "promote_unknowns" => GroomOp::PromoteUnknowns,
            _ => {
                return Err(WorkerError::invalid_params(format!("Unknown groom op: {}. Use decay, dedup, reflect, edge_decay, tag_normalize, or promote_unknowns.", params.op), None));
            }
        };

        let args = GroomOpArgs {
            op,
            owner: Some(scope.owner),
            workspace_id: Some(scope.workspace_id),
            dry_run: params.dry_run.unwrap_or(false),
        };

        let result = self.provider.groom(&args).await;

        let response = serde_json::json!({
            "op": format!("{:?}", result.op).to_lowercase(),
            "records_archived": result.records_archived,
            "records_merged": result.records_merged,
            "records_reflected": result.records_reflected,
            "records_selected": result.records_selected,
            "records_changed": result.records_changed,
            "records_skipped": result.records_skipped,
            "records_conflicted": result.records_conflicted,
            "alerts": result.alerts,
        });

        Ok(response)
    }
}

impl MemeryWorker {
    async fn dispatch(&self, operation: &str, arguments: Value) -> Result<Value, WorkerError> {
        fn decode<T: serde::de::DeserializeOwned>(value: Value) -> Result<T, WorkerError> {
            serde_json::from_value(value).map_err(|_| WorkerError::invalid_params("invalid operation arguments", None))
        }
        match operation {
            "capture" => self.capture(decode(arguments)?).await,
            "list_candidates" => self.list_candidates(decode(arguments)?).await,
            "update_candidate" => self.update_candidate(decode(arguments)?).await,
            "_host_candidate_review" => self.host_candidate_review(decode(arguments)?).await,
            "review_candidate" => self.review_candidate(decode(arguments)?).await,
            "ingest_authored" => self.ingest_authored(decode(arguments)?).await,
            "digest" => self.digest(decode(arguments)?).await,
            "memory_quality" => self.memory_quality(decode(arguments)?).await,
            "owner_lifecycle" => self.owner_lifecycle(decode(arguments)?).await,
            "memory_retention" => self.memory_retention(decode(arguments)?).await,
            "memory_forget" => self.memory_forget(decode(arguments)?).await,
            "memory_export" => self.memory_export(decode(arguments)?).await,
            "memory_explain" => self.memory_explain(decode(arguments)?).await,
            "list_quarantine" => self.list_quarantine(decode(arguments)?).await,
            "quarantine_legacy_state" => self.quarantine_legacy_state(decode(arguments)?).await,
            "recall" => self.recall(decode(arguments)?).await,
            "search" => self.search(decode(arguments)?).await,
            "get_memory" => self.get_memory(decode(arguments)?).await,
            "list_memories" => self.list_memories(decode(arguments)?).await,
            "record_memory_access" => self.record_memory_access(decode(arguments)?).await,
            "update_memory" => self.update_memory(decode(arguments)?).await,
            "resolve_memory" => self.resolve_memory(decode(arguments)?).await,
            "reopen_memory" => self.reopen_memory(decode(arguments)?).await,
            "delete_memory" => self.delete_memory(decode(arguments)?).await,
            "graph_upsert" => self.graph_upsert(decode(arguments)?).await,
            "graph_walk" => self.graph_walk(decode(arguments)?).await,
            "code_query" => self.code_query(decode(arguments)?).await,
            "code_index_scheduler" => self.code_index_scheduler(decode(arguments)?).await,
            "code_index" => self.code_index(decode(arguments)?).await,
            "groom" => self.groom(decode(arguments)?).await,
            _ => Err(WorkerError::invalid_params("unknown operation", None)),
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::INFO.into()),
        )
        .init();

    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let prepare_only = args.as_slice() == ["--prepare-database"];
    if !args.is_empty() && !prepare_only {
        return Err("usage: memery-worker [--prepare-database]; FM_DB_PATH selects the absolute database".into());
    }
    let config = FmConfig::from_environment()?;
    let expected_id = std::env::var("FM_DB_ID").ok().filter(|id| !id.trim().is_empty());
    if !prepare_only && expected_id.is_none() { return Err("prepared Memery database identity required".into()); }
    let db_path = std::path::Path::new(&config.db_path);
    if !db_path.is_absolute() {
        return Err(format!("FM_DB_PATH must be absolute: {}", config.db_path).into());
    }
    let profile = std::env::var("FM_STORE_PROFILE").unwrap_or_else(|_| "legacy-v16".into());
    let guarded = match profile.as_str() { "legacy-v16" => false, "guarded-v17" | "guarded-v18" => true, _ => return Err("unknown memory store profile".into()) };
    if guarded && (std::env::var("FM_AGENT_BRIDGE").is_ok_and(|v| matches!(v.trim().to_ascii_lowercase().as_str(),"1"|"true"|"yes")) || std::env::var("FM_HOST_PRIVATE_REVIEW").as_deref()!=Ok("1")) { return Err("guarded host profile unavailable".into()); }
    // The host always prepares the selected store before serving. Resolve symlinks
    // before deriving the persistent lock name. Never unlink an unlocked sidecar:
    // a waiter may already hold a descriptor referring to its inode.
    if !prepare_only {
        // Tokio may still be joining a synchronous SQLite/index operation after
        // the pipe closes. Keep custody until process exit, including that interval.
        static WORKER_LOCK: std::sync::OnceLock<std::fs::File> = std::sync::OnceLock::new();
        let canonical = db_path.canonicalize()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if canonical.metadata()?.nlink() != 1 {
                return Err("hard-linked Memery databases cannot share a SQLite WAL safely".into());
            }
        }
        #[cfg(windows)]
        memery_core::writer_fence::require_single_database_link(&canonical)?;
        let mut lock_path = canonical.into_os_string();
        lock_path.push(".memery-worker.lock");
        let lock = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(lock_path)?;
        lock.try_lock().map_err(|e| format!("cannot acquire Memery worker ownership: {e}"))?;
        WORKER_LOCK.set(lock).map_err(|_| "Memery worker ownership already initialized")?;
    }
    let (mut store,feedback,admitted,source_erase,device,inode) = if guarded {
        let expected=std::env::var("FM_DB_ID").map_err(|_| "guarded database identity required")?;
        SqliteStore::open_guarded(&config.db_path,config.embedding.dimensions,&expected)?
    } else {
        if let Some(parent) = db_path.parent() { std::fs::create_dir_all(parent)?; }
        (SqliteStore::open_legacy(&config.db_path, config.embedding.dimensions, expected_id.as_deref())?,false,false,false,0,0)
    };
    if guarded && store.guarded_epoch()? != if profile == "guarded-v18" { 18 } else { 17 } { return Err("guarded profile epoch mismatch".into()); }
    store
        .set_code_index_lease_seconds(config.code_index.lease_seconds)
        .map_err(|error| format!("invalid code-index configuration: {error}"))?;
    let store = Arc::new(store);
    let (database_id, schema_version) = store.database_identity()?;
    if let Ok(expected) = std::env::var("FM_DB_ID") {
        if !expected.trim().is_empty() && expected.trim() != database_id {
            return Err(format!(
                "Memery database identity mismatch: expected {}, opened {}",
                expected.trim(),
                database_id
            )
            .into());
        }
    }
    let mut profile_receipt=serde_json::json!({"database_id":database_id,"schema_version":schema_version,"database_path":config.db_path,"profile":profile,"device":device,"inode":inode,"capabilities":{"guarded_review":guarded,"source_lifecycle":guarded,"feedback":feedback,"admitted":admitted,"source_erase":source_erase,"owner_transition":guarded}});
    if guarded {
        let (cookie,paired)=store.guarded_pair_profile()?;
        profile_receipt["schema_cookie"]=serde_json::json!(cookie);
        profile_receipt["capabilities"]["assistant_interpretation"]=serde_json::json!(paired);
        profile_receipt["capabilities"]["account_chat"]=serde_json::json!(store.guarded_account_chat_profile()?);
        profile_receipt["capabilities"]["document_fts"]=serde_json::json!(store.guarded_document_fts_profile()?);
        // Binary protocol support plus the exact admitted document-FTS profile;
        // this is capability negotiation, not file or mutation authority.
        profile_receipt["capabilities"]["document_import_v1"]=serde_json::json!(store.guarded_document_fts_profile()?);
    }
    if prepare_only {
        println!("{}", profile_receipt);
        return Ok(());
    }
    tracing::info!(database_id, schema_version, path = %config.db_path, "Memery database ready");
    // Curated/raw memory rows currently lack the immutable route, adapter,
    // model, and dimension generation metadata required for safe provider
    // rotation.  Fail closed and retain exact/FTS recall.  Document vectors
    // use the fingerprinted Python generation schema and Open Clank's managed
    // ``embeddings.create`` operation instead.
    tracing::info!("embeddings: disabled for unfingerprinted curated-memory rows");
    let embed: Arc<dyn EmbeddingClient> =
        Arc::new(DisabledEmbeddingClient::new(config.embedding.dimensions));
    let provider = Arc::new(NativeProvider::new(store.clone(), embed, config));

    let server = MemeryWorker::new(provider, store, profile_receipt);

    protocol::serve(server).await?;
    Ok(())
}
