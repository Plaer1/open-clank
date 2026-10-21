use std::future::Future;
use std::sync::Arc;
use std::collections::{HashMap,HashSet};

use fm_core::config::FmConfig;
use fm_core::embed::{DisabledEmbeddingClient, EmbeddingClient};
use fm_core::provider::native::NativeProvider;
use fm_core::provider::{GroomOpArgs, MemoryProvider};
use fm_core::record::*;
use fm_core::store::sqlite::{ForgetSelector, RetentionPolicy, SqliteStore};
use rmcp::{
    handler::server::{tool::Parameters, tool::ToolCallContext, ServerHandler},
    model::*,
    service::RequestContext,
    tool, tool_router,
    transport::stdio,
    ServiceExt,
};
use schemars::JsonSchema;
use serde::Deserialize;

// Private isolated-candidate diagnostics only. No public/wire result fields,
// and disabled operations create neither a collector nor an Instant.
struct VisualTiming {
    context: fm_core::code_metrics::MetricsContext,
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
            let scheduler=Arc::new(fm_core::code_index_scheduler::IndexScheduler::default());
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
        use fm_core::code_metrics::{span,Stage};
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

#[derive(Clone)]
struct FrankenmemoryServer {
    provider: Arc<NativeProvider>,
    /// Concrete store handle for graph ops (they live on SqliteStore, not
    /// on the MemoryStore trait).
    graph_store: Arc<SqliteStore>,
    code_tasks: Arc<std::sync::Mutex<HashMap<String,CodeTask>>>,
    code_queries: Arc<std::sync::Mutex<HashMap<String,CodeQueryTask>>>,
    // The authority database has one exclusive publication fence. Queue code
    // readers cooperatively instead of racing that fence's short busy timeout.
    // Four jobs remain admitted; recall and cancellation do not take this lane.
    code_query_lane: Arc<tokio::sync::Semaphore>,
    code_query_secret: [u8;32],
    tool_router: rmcp::handler::server::router::tool::ToolRouter<Self>,
}

#[derive(Default)]
struct CodeTask { running:bool, token:String, ticket:Option<fm_core::code::CodeWorkTicket>, subscribers:HashSet<String>, error:Option<String>, cancel:Arc<std::sync::atomic::AtomicBool> }

struct CodeQueryTask {
    binding_key:String,
    cancel:Arc<std::sync::atomic::AtomicBool>,
    result:Option<Result<serde_json::Value,fm_core::code_query::QueryError>>,
    completed:Option<std::time::Instant>,
}
fn query_binding_key(binding:&fm_core::code::WorkspaceBinding)->String{
    serde_json::to_string(&(binding.owner.as_str(),binding.owner_subject_id.as_str(),binding.workspace_id.as_str(),binding.location_id.as_str(),binding.workspace_revision,binding.policy_generation,binding.origin.as_str(),&binding.root,binding.subscriber_id.as_str())).unwrap()
}

fn lifecycle_error(message:String)->rmcp::ErrorData {
    if message.starts_with("code_node_forget_requires_workspace_erase:") {
        return rmcp::ErrorData::invalid_params(message,Some(serde_json::json!({"code":"code_node_forget_requires_workspace_erase"})));
    }
    if ["denied:","erased:","ambiguous_registration:","stale_queued_ticket:"].iter().any(|prefix|message.starts_with(prefix)) {
        rmcp::ErrorData::invalid_params(message,None)
    } else {rmcp::ErrorData::internal_error(message,None)}
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

fn code_task_key(scope:&fm_core::graph::GraphScope,checkout:&str)->String{serde_json::to_string(&[scope.owner.as_str(),scope.workspace_id.as_str(),checkout]).expect("string tuple serialization")}

#[cfg(test)]
mod s03_wire_tests{
    use super::*;
    #[test]
    fn root_redaction_preserves_canonical_coverage_and_ids(){
        for root in ["/","src"]{
            let mut value=serde_json::json!({"registration_id":"src_registration","coverage":[{"rel_path":"src/sub/file.py","source_hash":"src_hash","source_generation":"src_generation","detail":format!("failed {root}private")}],"error":{"message":format!("failed {root}private")}});
            scrub_private_root(&mut value,root);assert_eq!(value["registration_id"],"src_registration");assert_eq!(value["coverage"][0]["rel_path"],"src/sub/file.py");assert_eq!(value["coverage"][0]["source_hash"],"src_hash");assert_eq!(value["coverage"][0]["source_generation"],"src_generation");assert!(value["error"]["message"].as_str().unwrap().contains("[root]"));
        }
        let error=lifecycle_error("code_node_forget_requires_workspace_erase: use workspace erasure".into());assert_eq!(error.code,rmcp::model::ErrorCode::INVALID_PARAMS);assert_eq!(error.data.unwrap()["code"],"code_node_forget_requires_workspace_erase");
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

fn reject_agent_bridge_internal_tool(tool: &str) -> Result<(), rmcp::ErrorData> {
    let agent_bridge = std::env::var("FM_AGENT_BRIDGE").is_ok_and(|value| {
        matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes"
        )
    });
    if agent_bridge {
        return Err(rmcp::ErrorData::invalid_params(
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
) -> Result<fm_core::graph::GraphScope, rmcp::ErrorData> {
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
            return Err(rmcp::ErrorData::invalid_params(
                "request scope conflicts with authenticated process scope",
                None,
            ));
        }
        (env_owner, env_workspace)
    } else if trusted_caller {
        (
            owner.ok_or_else(|| {
                rmcp::ErrorData::invalid_params("authenticated owner is required", None)
            })?,
            workspace_id
                .ok_or_else(|| rmcp::ErrorData::invalid_params("workspace_id is required", None))?,
        )
    } else if legacy {
        (
            owner.unwrap_or_else(|| "legacy".into()),
            workspace_id.unwrap_or_else(|| "global".into()),
        )
    } else {
        return Err(rmcp::ErrorData::invalid_params(
            "fm-mcp requires FM_OWNER+FM_WORKSPACE_ID or FM_SCOPE_AUTHORITY=trusted-caller",
            None,
        ));
    };

    let mut scope = fm_core::graph::GraphScope::new(owner, workspace_id)
        .map_err(|message| rmcp::ErrorData::invalid_params(message, None))?;
    scope.include_global = include_global;
    Ok(scope)
}

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
struct GroomParams {
    op: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    dry_run: Option<bool>,
}

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
struct MemoryGetParams {
    id: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
struct MemoryAccessParams {
    ids: Vec<String>,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
struct CandidateReviewParams {
    id: String,
    accept: bool,
    reason: String,
    owner: String,
    workspace_id: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
struct DigestParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
struct AuthoredSection {
    #[serde(default)]
    anchor: String,
    content: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
struct MemoryQualityParams {
    #[serde(default)]
    rebuild_graph_fts: Option<bool>,
}

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
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

#[derive(Debug, Deserialize, JsonSchema)]
struct ScopedExportParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
struct MemoryExplainParams {
    id: String,
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
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
) -> Result<ForgetSelector, rmcp::ErrorData> {
    let value = value
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| rmcp::ErrorData::invalid_params("selector is required", None))?;
    match kind.unwrap_or("record_id") {
        "record_id" => Ok(ForgetSelector::RecordId(value)),
        "source_uri" => Ok(ForgetSelector::SourceUri(value)),
        "source_message_id" => Ok(ForgetSelector::SourceMessageId(value)),
        _ => Err(rmcp::ErrorData::invalid_params(
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

#[derive(Debug, Deserialize, JsonSchema)]
struct GraphUpsertParams {
    #[serde(default)]
    owner: Option<String>,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    nodes: Vec<fm_core::graph::GraphNodeInput>,
    #[serde(default)]
    edges: Vec<fm_core::graph::GraphEdgeInput>,
    #[serde(default)]
    cues: Vec<fm_core::graph::GraphCueInput>,
}

#[derive(Debug, Deserialize, JsonSchema)]
struct GraphWalkParams {
    /// One of: overview | cues | tags | expand | fetch | trace | rank
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
}

#[derive(Debug, Deserialize, JsonSchema)]
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
    /// For action=impact: file path relative to the codebase root.
    #[serde(default)]
    rel_path: Option<String>,
    #[serde(default)]
    max_depth: Option<usize>,
    #[serde(default)]
    limit: Option<usize>,
}

#[derive(Debug,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
struct CodeIndexLimitsParams {max_files:usize,max_source_bytes:usize,max_file_bytes:usize,#[serde(default="fm_core::code_index_limits::default_generation_bytes")]max_generation_bytes:u64}
#[derive(Debug,Deserialize,JsonSchema)]
#[serde(tag="action",rename_all="snake_case",deny_unknown_fields)]
enum CodeIndexSchedulerParams{Status{origin:String},Configure{origin:String,revision:u64,max_threads:u8,#[serde(default)] limits:Option<CodeIndexLimitsParams>}}

impl CodeIndexSchedulerParams {
    fn request(self) -> fm_core::code_index_scheduler::SchedulerRequest {
        match self {
            Self::Status { origin } => fm_core::code_index_scheduler::SchedulerRequest::Status { origin },
            Self::Configure { origin, revision, max_threads, limits } => match limits {
                Some(limits)=>fm_core::code_index_scheduler::SchedulerRequest::ConfigureResources{origin,revision,max_threads,
                    limits:fm_core::code_index_limits::IndexLimits{max_files:limits.max_files,max_source_bytes:limits.max_source_bytes,max_file_bytes:limits.max_file_bytes,max_generation_bytes:limits.max_generation_bytes}},
                None=>fm_core::code_index_scheduler::SchedulerRequest::Configure { origin, revision, max_threads },
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
        let scheduler=fm_core::code_index_scheduler::IndexScheduler::default();
        let params:CodeIndexSchedulerParams=serde_json::from_value(serde_json::json!({"action":"status","origin":"app"})).unwrap();
        let status=serde_json::to_value(scheduler.request(&params.request()).unwrap()).unwrap();
        assert_eq!(status,serde_json::json!({"schema":"open-clank.index-scheduler/v1","revision":0,"effective_revision":0,"desired":2,"effective":2,"active":0,"queued":0,"drain_pending":false}));
    }
}

#[derive(Debug, Deserialize, JsonSchema)]
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
}

fn visual_request(params:&CodeQueryParams,origin:&str)->Result<Option<fm_core::code_visual::VisualRequest>,rmcp::ErrorData>{
    let Some(raw)=&params.visual_request else{return Ok(None)};
    let invalid=||rmcp::ErrorData::invalid_params("Invalid private visual request",None);
    if params.action!="start"||origin!="app"||params.query.is_some()||params.source_claim.is_some()||params.read_fence_only||params.selection_policy_fence{return Err(invalid());}
    // Count before typed deserialization without allocating an unbounded copy.
    struct Bound(usize);
    impl std::io::Write for Bound {
        fn write(&mut self,bytes:&[u8])->std::io::Result<usize>{
            self.0=self.0.checked_add(bytes.len()).filter(|n|*n<=fm_core::code_visual::MAX_REQUEST_BYTES).ok_or_else(||std::io::Error::other("visual request too large"))?;Ok(bytes.len())
        }
        fn flush(&mut self)->std::io::Result<()>{Ok(())}
    }
    serde_json::to_writer(&mut Bound(0),raw).map_err(|_|invalid())?;
    let request:fm_core::code_visual::VisualRequest=serde_json::from_value(raw.clone()).map_err(|_|invalid())?;
    match &request{fm_core::code_visual::VisualRequest::Map(q)=>q.validate(),fm_core::code_visual::VisualRequest::Selection(q)=>q.validate()}.map_err(|_|invalid())?;
    Ok(Some(request))
}

fn visual_delivery(token:&str,value:serde_json::Value,checked:Result<(),fm_core::code_query::QueryError>,cancel:&std::sync::atomic::AtomicBool)->serde_json::Value{
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
        for raw in [serde_json::json!({"mode":"other","request":{}}),serde_json::json!({"mode":"map","request":{},"extra":true}),serde_json::json!({"mode":"map","request":{"schema":"open-clank.code-map/v1","operation":"overview","root":{"kind":"visual","id":"x"}}}),serde_json::json!({"mode":"map","request":{"schema":"open-clank.code-map/v1","operation":"overview","cursor":"x".repeat(fm_core::code_visual::MAX_REQUEST_BYTES)}})]{let mut p=params();p.visual_request=Some(raw);assert!(visual_request(&p,"app").is_err());}
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
        let error=fm_core::code_query::QueryError{code:"stale_cursor".into(),message:"changed".into()};
        assert_eq!(visual_delivery("owned",serde_json::json!({}),Err(error),&cancel)["error"]["code"],"stale_cursor");
    }
}

#[tool_router]
impl FrankenmemoryServer {
    fn new(provider: Arc<NativeProvider>, graph_store: Arc<SqliteStore>) -> Self {
        Self {
            provider,
            graph_store,
            code_tasks:Arc::new(std::sync::Mutex::new(HashMap::new())),
            code_queries:Arc::new(std::sync::Mutex::new(HashMap::new())),
            code_query_lane:Arc::new(tokio::sync::Semaphore::new(1)),
            code_query_secret:fm_core::code_query::new_query_secret(),
            tool_router: Self::tool_router(),
        }
    }

    #[tool(
        name = "capture",
        description = "Capture one complete conversation turn. Automatic callers use user_text+assistant_text with capture_mode=raw_only, candidate, or review_only plus owner/workspace provenance. review_only always leaves eligible content pending for human approval. Deliberate remember operations use capture_mode=manual."
    )]
    async fn capture(
        &self,
        Parameters(params): Parameters<CaptureParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
            return Err(rmcp::ErrorData::invalid_params(
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
            source: params.source.unwrap_or_else(|| "mcp_capture".to_string()),
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

        Ok(CallToolResult::success(vec![Content::text(
            serde_json::to_string_pretty(&response).unwrap_or_default(),
        )]))
    }

    #[tool(
        name = "list_candidates",
        description = "List admission candidates in an owner/workspace scope. status may be pending, accepted, rejected, or quarantined."
    )]
    async fn list_candidates(
        &self,
        Parameters(params): Parameters<CandidateListParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let rows = self
            .provider
            .list_candidates(
                Some(&scope.owner),
                Some(&scope.workspace_id),
                params.status.as_deref(),
                params.limit.unwrap_or(100).min(1000),
            )
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"candidates": rows}).to_string(),
        )]))
    }

    #[tool(
        name = "update_candidate",
        description = "Edit one still-pending admission candidate in its exact owner/workspace scope without publishing it."
    )]
    async fn update_candidate(
        &self,
        Parameters(params): Parameters<CandidateUpdateParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        reject_agent_bridge_internal_tool("update_candidate")?;
        let scope = request_scope(Some(params.owner), Some(params.workspace_id), false)?;
        let kind = params.category.as_deref().and_then(parse_memory_kind);
        if params.category.is_some() && kind.is_none() {
            return Err(rmcp::ErrorData::invalid_params(
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
            .map_err(|error| rmcp::ErrorData::invalid_params(error, None))?;
        let candidate = candidate.ok_or_else(|| {
            rmcp::ErrorData::invalid_params(
                "pending candidate not found in this scope or edit was rejected",
                None,
            )
        })?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"updated": true, "candidate": candidate}).to_string(),
        )]))
    }

    #[tool(
        name = "review_candidate",
        description = "Accept or reject one candidate. Owner and workspace are mandatory; acceptance creates the only curated record and embedding."
    )]
    async fn review_candidate(
        &self,
        Parameters(params): Parameters<CandidateReviewParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
            .map_err(|error| rmcp::ErrorData::invalid_params(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"reviewed": true, "accepted": params.accept, "curated_id": curated_id}).to_string(),
        )]))
    }

    #[tool(
        name = "ingest_authored",
        description = "Project an agent-authored memory file into curated records, one per section. Idempotent: unchanged sections skip by content hash, edited sections replace their record, removed sections (or an empty sections list) delete theirs. The file remains the source of truth."
    )]
    async fn ingest_authored(
        &self,
        Parameters(params): Parameters<IngestAuthoredParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "digest",
        description = "Return a small index-card digest of the memory bank for the caller's scope: tier/kind counts, pinned headlines, top relationship clusters, and newest cue topics. Read-only and cheap — meant for per-turn injection; follow up with recall/search for depth."
    )]
    async fn digest(
        &self,
        Parameters(params): Parameters<DigestParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let result = self
            .graph_store
            .digest(&scope.owner, &scope.workspace_id, scope.include_global)
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "memory_quality",
        description = "Return memory admission counters and graph/base FTS integrity. Set rebuild_graph_fts=true to repair cue index parity."
    )]
    async fn memory_quality(
        &self,
        Parameters(params): Parameters<MemoryQualityParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        if params.rebuild_graph_fts.unwrap_or(false) {
            reject_agent_bridge_internal_tool("memory_quality rebuild")?;
        }
        let result = if params.rebuild_graph_fts.unwrap_or(false) {
            self.provider.rebuild_graph_cue_fts()
        } else {
            self.provider.quality_status()
        }
        .map_err(|error| rmcp::ErrorData::internal_error(error.to_string(), None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "owner_lifecycle",
        description = "Privileged owner-scoped lifecycle operation. reset_preview returns exact count/fingerprint snapshots for selected memories, graph, or ingest components; reset_commit atomically validates that snapshot and erases it. Memories defensively expands to its graph and ingest dependency closure."
    )]
    async fn owner_lifecycle(
        &self,
        Parameters(params): Parameters<OwnerLifecycleParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        reject_agent_bridge_internal_tool("owner_lifecycle")?;
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = match params.action.as_str() {
            "stats" => self.graph_store.owner_counts(&scope.owner),
            "purge" => self.graph_store.purge_owner(&scope.owner),
            "rename" => {
                let new_owner = params.new_owner.as_deref().ok_or_else(|| {
                    rmcp::ErrorData::invalid_params("rename requires new_owner", None)
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
                return Err(rmcp::ErrorData::invalid_params(
                    "action must be stats|purge|rename|reset_preview|reset_commit",
                    None,
                ));
            }
        }
        .map_err(|error| rmcp::ErrorData::internal_error(error.to_string(), None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "memory_retention",
        description = "Get, set, preview expiry, expire, or query expiry status for bounded owner/workspace memory retention. preview_expire returns the exact current closure and token. Token-checked expire also requires a client-generated 32-hex operation_id for exact retry recovery; status returns absent or the committed closure. Raw, candidate, curated, and graph tiers are independent; omitted curated/graph retention means keep. Mutations are unavailable through an Agent bridge."
    )]
    async fn memory_retention(
        &self,
        Parameters(params): Parameters<MemoryRetentionParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = match params.action.as_str() {
            "get" => serde_json::to_value(
                self.graph_store
                    .retention_policy(&scope.owner, &scope.workspace_id)
                    .map_err(|error| rmcp::ErrorData::internal_error(error, None))?,
            ),
            "set" => {
                reject_agent_bridge_internal_tool("memory_retention set")?;
                let current = self
                    .graph_store
                    .retention_policy(&scope.owner, &scope.workspace_id)
                    .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
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
                        .map_err(|error| rmcp::ErrorData::invalid_params(error, None))?,
                )
            }
            "preview_expire" => serde_json::to_value(
                self.graph_store
                    .preview_expire_retention(&scope.owner, &scope.workspace_id)
                    .map_err(|error| rmcp::ErrorData::internal_error(error, None))?,
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
                        .map_err(|error| rmcp::ErrorData::invalid_params(error, None))?,
                    (None, None) => self
                        .graph_store
                        .expire_retention(&scope.owner, &scope.workspace_id)
                        .map_err(|error| rmcp::ErrorData::internal_error(error, None))?,
                    (Some(_), None) => {
                        return Err(rmcp::ErrorData::invalid_params(
                            "operation_id is required with preview_token",
                            None,
                        ));
                    }
                    (None, Some(_)) => {
                        return Err(rmcp::ErrorData::invalid_params(
                            "preview_token is required with operation_id",
                            None,
                        ));
                    }
                };
                serde_json::to_value(closure)
            }
            "status" => {
                let operation_id = params.operation_id.as_deref().ok_or_else(|| {
                    rmcp::ErrorData::invalid_params("operation_id is required", None)
                })?;
                serde_json::to_value(
                    self.graph_store
                        .retention_operation_status(&scope.owner, &scope.workspace_id, operation_id)
                        .map_err(|error| rmcp::ErrorData::invalid_params(error, None))?,
                )
            }
            _ => {
                return Err(rmcp::ErrorData::invalid_params(
                    "action must be get|set|preview_expire|expire|status",
                    None,
                ));
            }
        }
        .map_err(|error| rmcp::ErrorData::internal_error(error.to_string(), None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "memory_forget",
        description = "Preview or commit a provenance-closure forget, restore it within the configured recovery window, or query an operation status. Commit requires the exact preview token plus a client-generated 32-hex operation_id, making transport retries idempotent. All actions are unavailable through an Agent bridge."
    )]
    async fn memory_forget(
        &self,
        Parameters(params): Parameters<MemoryForgetParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
                    rmcp::ErrorData::invalid_params("preview_token is required", None)
                })?;
                let operation_id = params.operation_id.ok_or_else(|| {
                    rmcp::ErrorData::invalid_params("operation_id is required", None)
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
                    rmcp::ErrorData::invalid_params("operation_id is required", None)
                })?;
                serde_json::to_value(
                    self.graph_store
                        .forget_operation_status(&scope.owner, &scope.workspace_id, &operation_id)
                        .map_err(|error| rmcp::ErrorData::invalid_params(error, None))?,
                )
            }
            "restore" => {
                let tombstone_id = params.tombstone_id.ok_or_else(|| {
                    rmcp::ErrorData::invalid_params("tombstone_id is required", None)
                })?;
                serde_json::to_value(
                    self.graph_store
                        .restore_forget(&scope.owner, &scope.workspace_id, &tombstone_id)
                        .map_err(|error| rmcp::ErrorData::invalid_params(error, None))?,
                )
            }
            _ => {
                return Err(rmcp::ErrorData::invalid_params(
                    "action must be preview|commit|status|restore",
                    None,
                ));
            }
        }
        .map_err(|error| rmcp::ErrorData::internal_error(error.to_string(), None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "memory_export",
        description = "Export the authenticated owner/workspace memory tiers with provenance, trust, policy, graph nodes, and non-sensitive tombstone metadata. Unavailable through an Agent bridge."
    )]
    async fn memory_export(
        &self,
        Parameters(params): Parameters<ScopedExportParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        reject_agent_bridge_internal_tool("memory_export")?;
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = self
            .graph_store
            .export_scope(&scope.owner, &scope.workspace_id)
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "memory_explain",
        description = "Explain one curated memory's source identity, revision, trust, evidence, and graph projection in the authenticated scope."
    )]
    async fn memory_explain(
        &self,
        Parameters(params): Parameters<MemoryExplainParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let result = self
            .graph_store
            .explain_memory(&params.id, &scope.owner, &scope.workspace_id)
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"explanation": result}).to_string(),
        )]))
    }

    #[tool(
        name = "list_quarantine",
        description = "List quarantined memory/graph evidence in an owner/workspace scope."
    )]
    async fn list_quarantine(
        &self,
        Parameters(params): Parameters<CandidateListParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let rows = self
            .provider
            .list_quarantine(
                Some(&scope.owner),
                Some(&scope.workspace_id),
                params.limit.unwrap_or(100).min(1000),
            )
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"quarantine": rows}).to_string(),
        )]))
    }

    #[tool(
        name = "quarantine_legacy_state",
        description = "Dry-run or execute the one-time quarantine of pre-admission curated and semantic graph state. Defaults to dry_run=true."
    )]
    async fn quarantine_legacy_state(
        &self,
        Parameters(params): Parameters<QuarantineMigrationParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "recall",
        description = "Recall memories relevant to a query. Returns GT-tagged, provenance-tagged results. mode toggles between layer_a and layer_b. workspace_id biases ranking: current workspace boosted, global always participates."
    )]
    async fn recall(
        &self,
        Parameters(params): Parameters<RecallParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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

        Ok(CallToolResult::success(vec![Content::text(
            payload.to_string(),
        )]))
    }

    #[tool(
        name = "search",
        description = "Raw search over a tier (curated/raw). No GT framing. Returns matching memories."
    )]
    async fn search(
        &self,
        Parameters(params): Parameters<SearchParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let tier = match params.tier.as_deref() {
            Some("raw") => Tier::Raw,
            _ => Tier::Curated,
        };
        let kind = params.kind.as_deref().and_then(parse_memory_kind);

        let search_params = fm_core::record::SearchParams {
            query: params.query,
            kind,
            scene: params.scene,
            tier,
            limit: params.limit.unwrap_or(10),
            workspace_id: Some(scope.workspace_id),
            owner: Some(scope.owner),
        };

        let result = self.provider.search(&search_params).await;

        Ok(CallToolResult::success(vec![Content::text(
            serde_json::to_string(&result).unwrap_or_default(),
        )]))
    }

    #[tool(
        name = "get_memory",
        description = "Read one curated memory by exact stable id in the authenticated owner/workspace scope."
    )]
    async fn get_memory(
        &self,
        Parameters(params): Parameters<MemoryGetParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let record = self
            .provider
            .get_curated_record(&params.id, &scope.owner, &scope.workspace_id)
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"record": record}).to_string(),
        )]))
    }

    #[tool(
        name = "list_memories",
        description = "List curated memories with an opaque numeric cursor in the authenticated owner/workspace scope."
    )]
    async fn list_memories(
        &self,
        Parameters(params): Parameters<MemoryListParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let limit = params.limit.unwrap_or(100).clamp(1, 1000);
        let offset = params
            .cursor
            .as_deref()
            .unwrap_or("0")
            .parse::<usize>()
            .map_err(|_| rmcp::ErrorData::invalid_params("invalid memory cursor", None))?;
        let records = self
            .provider
            .list_curated_records(&scope.owner, &scope.workspace_id, limit, offset)
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        let next_cursor = (records.len() == limit).then(|| (offset + records.len()).to_string());
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"records": records, "next_cursor": next_cursor}).to_string(),
        )]))
    }

    #[tool(
        name = "record_memory_access",
        description = "Increment usage once for curated memories actually injected into a response."
    )]
    async fn record_memory_access(
        &self,
        Parameters(params): Parameters<MemoryAccessParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        if params.ids.len() > 1000 {
            return Err(rmcp::ErrorData::invalid_params(
                "at most 1000 memory ids may be accounted",
                None,
            ));
        }
        let updated = self
            .provider
            .record_curated_access(&params.ids, &scope.owner, &scope.workspace_id)
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"updated": updated}).to_string(),
        )]))
    }

    #[tool(
        name = "update_memory",
        description = "Update one curated memory's content and/or pinned metadata. The id and owner/workspace scope are required for safe mutation."
    )]
    async fn update_memory(
        &self,
        Parameters(params): Parameters<MemoryMutationParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let kind = params.category.as_deref().and_then(parse_memory_kind);
        if params.category.is_some() && kind.is_none() {
            return Err(rmcp::ErrorData::invalid_params(
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
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"id": params.id, "updated": updated}).to_string(),
        )]))
    }

    #[tool(
        name = "resolve_memory",
        description = "Close one open question (kind=unknown): archives it with resolved_by/resolved_at provenance. Only unknown-kind records resolve; use resolved_by to link the memory that answered it."
    )]
    async fn resolve_memory(
        &self,
        Parameters(params): Parameters<ResolveMemoryParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let resolved = self
            .provider
            .resolve_unknown(
                &params.id,
                params.resolved_by.as_deref(),
                &scope.owner,
                &scope.workspace_id,
            )
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"id": params.id, "resolved": resolved}).to_string(),
        )]))
    }

    #[tool(
        name = "reopen_memory",
        description = "Reopen one previously resolved question in the exact owner/workspace scope. Only archived unknown-kind records are eligible."
    )]
    async fn reopen_memory(
        &self,
        Parameters(params): Parameters<MemoryGetParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let reopened = self
            .provider
            .reopen_unknown(&params.id, &scope.owner, &scope.workspace_id)
            .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"id": params.id, "reopened": reopened}).to_string(),
        )]))
    }

    #[tool(
        name = "delete_memory",
        description = "Delete one curated memory by id within the owner/workspace scope."
    )]
    async fn delete_memory(
        &self,
        Parameters(params): Parameters<MemoryMutationParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let deleted = self
            .provider
            .delete_curated_record(&params.id, Some(&scope.owner), Some(&scope.workspace_id))
            .await;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::json!({"id": params.id, "deleted": deleted}).to_string(),
        )]))
    }

    #[tool(
        name = "graph_upsert",
        description = "Insert or update graph memory: nodes {kind,name,label?,layer?}, edges {src:{kind,name},tag,dst:{kind,name},fact?}, cues {cue,node:{kind,name}}. Idempotent: node identity is deterministic from kind+name, so re-sending the same entities merges instead of duplicating. Edge facts are stored as searchable one-sentence statements."
    )]
    async fn graph_upsert(
        &self,
        Parameters(params): Parameters<GraphUpsertParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        reject_agent_bridge_internal_tool("graph_upsert")?;
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let input = fm_core::graph::GraphUpsertInput {
            nodes: params.nodes,
            edges: params.edges,
            cues: params.cues,
        };
        let result = self.graph_store.graph_upsert(&scope, &input).map_err(|e| {
            rmcp::ErrorData::internal_error(format!("graph_upsert failed: {e}"), None)
        })?;
        Ok(CallToolResult::success(vec![Content::text(
            serde_json::to_string(&result).unwrap_or_default(),
        )]))
    }

    #[tool(
        name = "graph_walk",
        description = "Walk graph memory one step at a time. Reconstruct answers instead of retrieving: start with op=cues (query text -> entry nodes), read op=tags on a node BEFORE expanding to see which relations exist cheaply, then op=expand (optionally filtered by tag/direction) to get neighbors with their edge facts, op=fetch for one node's full detail, op=trace for a path between two nodes. op=overview returns a scoped nodes+edges seed (no starting node needed; for UIs/cold starts, not for answering questions). Fetch only what survives your pruning."
    )]
    async fn graph_walk(
        &self,
        Parameters(params): Parameters<GraphWalkParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, params.include_global)?;
        let limit = params.limit.unwrap_or(10);
        let err = |m: String| rmcp::ErrorData::invalid_params(m, None);
        let payload = match params.op.as_str() {
            "overview" => {
                let overview = self
                    .graph_store
                    .graph_overview(&scope, limit)
                    .map_err(|e| {
                        rmcp::ErrorData::internal_error(format!("overview failed: {e}"), None)
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
                        rmcp::ErrorData::internal_error(format!("cues failed: {e}"), None)
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
                        rmcp::ErrorData::internal_error(format!("rank failed: {e}"), None)
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
                    rmcp::ErrorData::internal_error(format!("tags failed: {e}"), None)
                })?;
                serde_json::json!({ "op": "tags", "node_id": n, "tags": tags })
            }
            "expand" => {
                let n = params
                    .node_id
                    .ok_or_else(|| err("op=expand requires 'node_id'".into()))?;
                let hits = self
                    .graph_store
                    .graph_expand(
                        &scope,
                        &n,
                        params.tag.as_deref(),
                        params.direction.as_deref(),
                        limit,
                    )
                    .map_err(|e| {
                        rmcp::ErrorData::internal_error(format!("expand failed: {e}"), None)
                    })?;
                serde_json::json!({ "op": "expand", "node_id": n, "hits": hits })
            }
            "fetch" => {
                let n = params
                    .node_id
                    .ok_or_else(|| err("op=fetch requires 'node_id'".into()))?;
                let hit = self.graph_store.graph_fetch(&scope, &n).map_err(|e| {
                    rmcp::ErrorData::internal_error(format!("fetch failed: {e}"), None)
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
                let paths = self
                    .graph_store
                    .graph_trace(
                        &scope,
                        &n,
                        params.dst_id.as_deref(),
                        params.max_depth.unwrap_or(4),
                        limit,
                    )
                    .map_err(|e| {
                        rmcp::ErrorData::internal_error(format!("trace failed: {e}"), None)
                    })?;
                serde_json::json!({ "op": "trace", "paths": paths })
            }
            other => {
                return Err(err(format!(
                    "unknown op '{other}' — expected cues|tags|expand|fetch|trace|rank"
                )))
            }
        };
        Ok(CallToolResult::success(vec![Content::text(
            payload.to_string(),
        )]))
    }

    #[tool(name="code_query",description="Private app-broker code-only bounded read jobs: start/poll/cancel. Never admits, rebuilds, erases or changes Trust. Not exposed through the Agent memory bridge.")]
    async fn code_query(&self,Parameters(params):Parameters<CodeQueryParams>)->Result<CallToolResult,rmcp::ErrorData>{
        reject_agent_bridge_internal_tool("code_query")?;
        let invalid=|message:&str|rmcp::ErrorData::invalid_params(message.to_owned(),None);
        let authority_error=|error:String|if error.starts_with("denied:"){invalid("Code workspace authority unavailable")}else{rmcp::ErrorData::internal_error("Code workspace authority unavailable",None)};
        let binding:fm_core::code::WorkspaceBinding=serde_json::from_value(params.binding.clone()).map_err(|_|invalid("Invalid trusted code binding"))?;
        let visual=visual_request(&params,&binding.origin)?;
        let scope=request_scope(Some(binding.owner.clone()),Some(binding.workspace_id.clone()),false)?;
        let binding_key=query_binding_key(&binding);
        let payload=match params.action.as_str(){
            "start"=>{
                let query:Option<fm_core::code_query::CodeQuery>=if visual.is_some(){None}else{
                    let query=serde_json::from_value(params.query.ok_or_else(||invalid("Code query required"))?).map_err(|_|invalid("Invalid public code query"))?;
                    fm_core::code_query::validate_query(&query).map_err(|_|invalid("Code query exceeds its finite contract"))?;Some(query)
                };
                let source_claim:Option<fm_core::code_evidence::SourceClaim>=params.source_claim.map(serde_json::from_value).transpose().map_err(|_|invalid("Invalid private source claim"))?;
                if params.read_fence_only && source_claim.is_some(){return Err(invalid("Read fence cannot carry source claims"));}
                if params.selection_policy_fence && !params.read_fence_only{return Err(invalid("Selection policy fence requires read fence mode"));}
                let selection_policy_fence=params.selection_policy_fence;
                let read_fence_only=params.read_fence_only;
                if let Some(claim)=&source_claim{
                    if query.as_ref().is_none_or(|q|q.expected_receipt.is_none()) || !matches!(&claim.predicate,fm_core::code_evidence::SourcePredicate::ExactText{text} if text.len()<=32768){return Err(invalid("Expected receipt and bounded Files ExactText claim required"));}
                }
                // Scheduling discloses no evidence. The worker reacquires the
                // original exclusive authority fence before any snapshot/read.
                binding.validate().map_err(authority_error)?;
                let token=format!("query_{}",generate_id());let cancel=Arc::new(std::sync::atomic::AtomicBool::new(false));
                {
                    let mut tasks=self.code_queries.lock().unwrap();
                    if tasks.values().filter(|t|t.result.is_none()).count()>=4{return Err(rmcp::ErrorData::invalid_params("Code query capacity exhausted",Some(serde_json::json!({"code":"busy"}))));}
                    let mut completed:Vec<_>=tasks.iter().filter_map(|(k,t)|t.completed.map(|at|(at,k.clone()))).collect();completed.sort();
                    let remove=completed.len().saturating_sub(31);for(_,key)in completed.into_iter().take(remove){tasks.remove(&key);}
                    tasks.insert(token.clone(),CodeQueryTask{binding_key,cancel:cancel.clone(),result:None,completed:None});
                }
                let store=self.graph_store.clone();let tasks=self.code_queries.clone();let job_token=token.clone();let secret=self.code_query_secret;let lane=self.code_query_lane.clone();
                tokio::spawn(async move{
                    #[cfg(feature="s03-fixtures")]
                    let gate=fixture_queued_gate(&job_token).await;
                    #[cfg(not(feature="s03-fixtures"))]
                    let gate:Result<(),String>=Ok(());
                    let result=if gate.is_err(){Err(fm_core::code_query::QueryError{code:"unavailable".into(),message:"Code fixture gate failed".into()})}else{
                        // Owned by the blocking worker, even if its async waiter
                        // is dropped. No task-map lock or RPC queue is held.
                        let mut visual_timing=visual.as_ref().and_then(|_|VisualTiming::begin(visual_timings_enabled(),&VISUAL_TIMING_RECORDS));
                        let permit=lane.acquire_owned().await.expect("code query lane remains open");
                        if let Some(timing)=&mut visual_timing{timing.lane_acquired();}
                        let timing_token=job_token.clone();
                        match tokio::task::spawn_blocking(move||{
                            let _permit=permit;
                            let _interactive=store.index_scheduler().interactive_activity();
                            if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(fm_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()});}
                            if let Some(request)=visual{
                                let work=||store.code_visual(&scope,&binding,&request,&cancel,&secret).and_then(|result|serde_json::to_value(result).map_err(|_|fm_core::code_query::QueryError{code:"unavailable".into(),message:"Code map serialization unavailable".into()}));
                                let result=if let Some(timing)=&visual_timing{timing.context.enter(work)}else{work()};
                                if let Some(timing)=visual_timing{timing.emit(&timing_token,VisualTimingPhase::Worker);}
                                return result;
                            }
                            let query=query.expect("validated evidence query or visual request");
                            if read_fence_only{let fence=if selection_policy_fence{store.code_policy_fence(&scope,&binding,query.expected_receipt.as_ref())}else{store.code_read_fence(&scope,&binding,query.expected_receipt.as_ref())};return fence.map(|fence|serde_json::json!({"private_read_fence":fence,"private_policy_fence":selection_policy_fence,"receipt":null}));}
                            let mut result=store.code_query(&scope,&binding,&query,&cancel,&secret)?;
                            let mut value=serde_json::to_value(&result).map_err(|_|fm_core::code_query::QueryError{code:"unavailable".into(),message:"Code serialization unavailable".into()})?;
                            if let Some(claim)=source_claim{
                                if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(fm_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()});}
                                let(validation,bytes)=store.code_validate_source_measured(&scope,&binding,&result.receipt.as_ref().unwrap().evidence,&claim);
                                result.cost.validation_bytes_read=bytes;
                                value["cost"]["validation_bytes_read"]=serde_json::json!(bytes);
                                value["source_validation"]=match validation{Ok(v)=>serde_json::to_value(v).unwrap(),Err(e)=>serde_json::json!({"state":"unavailable","reason":"Current source validation unavailable","error_code":e.code})};
                                store.code_query_check(&scope,&binding,result.receipt.as_ref().unwrap())?;
                            }
                            if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err(fm_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()});}
                            Ok(value)
                        }).await{
                            Ok(result)=>result.and_then(|v|serde_json::to_value(v).map_err(|_|fm_core::code_query::QueryError{code:"unavailable".into(),message:"Code query serialization failed".into()})),
                            Err(_)=>Err(fm_core::code_query::QueryError{code:"unavailable".into(),message:"Code query worker failed".into()}),
                        }
                    };
                    #[cfg(feature="s03-fixtures")]
                    let rejected=result.is_err();
                    {
                        let mut tasks=tasks.lock().unwrap();
                        if let Some(task)=tasks.get_mut(&job_token){
                            task.result=Some(if task.cancel.load(std::sync::atomic::Ordering::Relaxed){Err(fm_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()})}else{result});task.completed=Some(std::time::Instant::now());
                        }
                        let mut completed:Vec<_>=tasks.iter().filter_map(|(k,t)|t.completed.map(|at|(at,k.clone()))).collect();completed.sort();
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
                    let result=if params.action=="cancel"{task.cancel.store(true,std::sync::atomic::Ordering::Relaxed);None}else if task.cancel.load(std::sync::atomic::Ordering::Relaxed){Some(Err(fm_core::code_query::QueryError{code:"cancelled".into(),message:"Code query cancelled".into()}))}else{task.result.clone()};(result,task.cancel.clone())
                };
                if params.action=="cancel"{
                    serde_json::json!({"state":"cancelled","token":token,"error":{"code":"cancelled","message":"Code query cancelled"}})
                }else{
                    // Completed disclosure also uses the lane: never wait for
                    // a worker's exclusive fence inside the single RPC owner.
                    // While occupied, disclose only authenticated running state.
                    let permit=if result.is_some(){match self.code_query_lane.clone().try_acquire_owned(){
                        Ok(permit)=>Some(permit),
                        Err(_)=>{binding.validate().map_err(authority_error)?;return Ok(CallToolResult::success(vec![Content::text(serde_json::json!({"state":"running","token":token}).to_string())]));}
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
                                let result:fm_core::code_visual::VisualResult=serde_json::from_value(value.clone()).map_err(|_|invalid("Code map result unavailable"))?;
                                let authority=match result{fm_core::code_visual::VisualResult::Map(r)=>r.authority,fm_core::code_visual::VisualResult::Selection(r)=>r.authority};
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
                                }).await.unwrap_or_else(|_|Err(fm_core::code_query::QueryError{code:"unavailable".into(),message:"Code map delivery unavailable".into()}));
                                return Ok(CallToolResult::success(vec![Content::text(visual_delivery(&token,value,checked,&cancel).to_string())]));
                            }
                            let _permit=permit;
                            let _interactive=self.graph_store.index_scheduler().interactive_activity();
                            if let Some(expected_fence)=value["private_read_fence"].as_str(){
                                let current=if value["private_policy_fence"].as_bool()==Some(true){self.graph_store.code_policy_fence(&scope,&binding,None)}else{self.graph_store.code_read_fence(&scope,&binding,None)}.map_err(|_|invalid("Code read authority or admission changed"))?;
                                if current!=expected_fence{return Err(invalid("Code read admission or head changed"));}
                                return Ok(CallToolResult::success(vec![Content::text(serde_json::json!({"state":"completed","token":token,"result":value}).to_string())]));
                            }
                            let receipt:fm_core::code_query::QueryReceipt=serde_json::from_value(value["receipt"].clone()).map_err(|_|invalid("Code result receipt unavailable"))?;
                            self.graph_store.code_query_check(&scope,&binding,&receipt).map_err(|_|invalid("Code result scope or generation changed"))?;
                            serde_json::json!({"state":"completed","token":token,"result":value})
                        }
                    }
                }
            }
            _=>return Err(invalid("Unknown read-only code job action")),
        };
        Ok(CallToolResult::success(vec![Content::text(payload.to_string())]))
    }

    #[tool(name="code_index_scheduler",description="Private app-owner indexing CPU/source resource ceilings and effective readback. Status/configuration never starts an index or worker. Not an agent operation.")]
    async fn code_index_scheduler(&self,Parameters(params):Parameters<CodeIndexSchedulerParams>)->Result<CallToolResult,rmcp::ErrorData>{
        reject_agent_bridge_internal_tool("code_index_scheduler")?;
        let request=params.request();
        let status=self.graph_store.index_scheduler().request(&request).map_err(|error|rmcp::ErrorData::invalid_params(error,None))?;
        Ok(CallToolResult::success(vec![Content::text(serde_json::to_string(&status).map_err(|_|rmcp::ErrorData::internal_error("Scheduler serialization unavailable",None))?)]))
    }

    #[tool(
        name = "code_index",
        description = "App-only admitted code index lifecycle: ensure queues one coalesced background verification/build, status reads progress and prior ready head, release drops one subscriber, cancel explicitly fences a job, remove atomically erases the workspace code scope, and admit explicitly renews erased admission. Source languages Rust/Python/TypeScript/TSX/JavaScript/JSX/MJS/CJS; failed prior files are excluded with coverage. Legacy index/stale actions are unavailable; legacy status/impact are bounded metadata reads. Never available on Agent memory bridge."
    )]
    async fn code_index(
        &self,
        Parameters(params): Parameters<CodeIndexParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        reject_agent_bridge_internal_tool("code_index")?;
        let scope = request_scope(params.owner.clone(), params.workspace_id.clone(), false)?;
        let err = |m: String| rmcp::ErrorData::invalid_params(m, None);
        let ierr = |m: String| rmcp::ErrorData::internal_error(m, None);
        if matches!(params.action.as_str(),"ensure"|"admit"|"release"|"cancel") || params.root.is_some() || params.owner_subject_id.is_some() {
            let cleanup=matches!(params.action.as_str(),"release"|"cancel"|"remove");
            let binding=fm_core::code::WorkspaceBinding {
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
                        if let Some(task)=self.code_tasks.lock().unwrap().get_mut(&key){task.cancel.store(true,std::sync::atomic::Ordering::Release);task.running=false;task.token=format!("cancelled_{}",generate_id());task.ticket=None;}
                    } else {
                        if let Some(task)=self.code_tasks.lock().unwrap().get_mut(&key){task.cancel.store(true,std::sync::atomic::Ordering::Release);task.running=false;task.token=format!("erased_{}",generate_id());task.ticket=None;task.subscribers.clear();}
                    }
                }
                self.graph_store.index_scheduler().wake();
                serde_json::json!({"lifecycle":if params.action=="remove"{"erased"}else if params.action=="cancel"{"cancelled"}else{"released"},"checkout_id":if checkouts.len()==1{checkouts.first()}else{None},"error":null})
            } else if params.action=="status" {
                self.graph_store.code_workspace_status(&scope,&binding).map_err(lifecycle_error)?
            } else if params.action=="ensure" || params.action=="admit" {
                let payload=self.graph_store.code_workspace_prepare(&scope,&binding,params.action=="admit").map_err(lifecycle_error)?;
                let checkout=payload["checkout_id"].as_str().ok_or_else(||ierr("missing checkout receipt".into()))?.to_string();
                let key=code_task_key(&scope,&checkout);
                let ticket=fm_core::code::CodeWorkTicket{checkout_id:checkout,admission_epoch:payload["admission_epoch"].as_i64().ok_or_else(||ierr("missing epoch".into()))?,config_fingerprint:payload["config_fingerprint"].as_str().ok_or_else(||ierr("missing fingerprint".into()))?.into(),policy_generation:binding.policy_generation,workspace_revision:binding.workspace_revision};
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
                    let admission=match scheduler.reserve(descriptor_bytes){Ok(admission)=>admission,Err(error)=>{if let Some(task)=self.code_tasks.lock().unwrap().get_mut(&key){if task.token==token{task.running=false;task.error=Some(error.clone());}}return Err(lifecycle_error(error));}};
                    let store=self.graph_store.clone();let tasks=self.code_tasks.clone();let job_scope=scope.clone();let job_binding=binding.clone();
                    tokio::spawn(async move {
                        #[cfg(feature="s03-fixtures")]
                        let gate_error=fixture_queued_gate(&token).await.err();
                        #[cfg(not(feature="s03-fixtures"))]
                        let gate_error:Option<String>=None;
                        let private_root=job_binding.root.to_string_lossy().to_string();
                        let error=if gate_error.is_some(){gate_error}else {
                            match admission.wait(&cancel).await{
                                Err(error)=>Some(error),
                                Ok(cohort)=>{let result=tokio::task::spawn_blocking(move||cohort.run(||{if cancel.load(std::sync::atomic::Ordering::Acquire){return Err("cancelled: queued indexing".into());}store.code_index_bound(&job_scope,&job_binding,&ticket)})).await;
                                    match result{Ok(Ok(_))=>None,Ok(Err(error))=>Some(error.replace(&private_root,"[root]")),Err(_)=>Some("index_worker_failed: coordinator panic".into())}
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
                    if task.running && (matches!(params.action.as_str(),"ensure"|"admit") || !matches!(payload["lifecycle"].as_str(),Some("erased"|"released"|"cancelled"|"building"))) {payload["lifecycle"]=serde_json::json!("queued");payload["freshness"]=serde_json::json!(if payload["head_generation"].is_null(){"missing"}else{"stale"});}
                    payload["task_token"]=serde_json::json!(task.token);
                    if let Some(error)=&task.error {payload["error"]=serde_json::json!({"code":"index_job_failed","message":error});}
                    payload["subscribers"]=serde_json::json!(task.subscribers.len());
                }
            }
            scrub_private_root(&mut payload,binding.root.to_str().unwrap_or(""));
            return Ok(CallToolResult::success(vec![Content::text(payload.to_string())]));
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
                    .then(|| fm_core::code::repository_id(std::path::Path::new(&params.path)).ok())
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
                    .then(|| fm_core::code::repository_id(std::path::Path::new(&params.path)).ok())
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
        Ok(CallToolResult::success(vec![Content::text(
            payload.to_string(),
        )]))
    }

    #[tool(
        name = "groom",
        description = "Curation dispatcher. Operations: decay (archive aged records), dedup (merge near-duplicates), reflect (adjust confidence), promote_unknowns (merge the same open question from >=3 workspaces into one global)."
    )]
    async fn groom(
        &self,
        Parameters(params): Parameters<GroomParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
                return Ok(CallToolResult::error(vec![Content::text(
                    format!("Unknown groom op: {}. Use decay, dedup, reflect, edge_decay, tag_normalize, or promote_unknowns.", params.op),
                )]));
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

        Ok(CallToolResult::success(vec![Content::text(
            serde_json::to_string_pretty(&response).unwrap_or_default(),
        )]))
    }
}

impl ServerHandler for FrankenmemoryServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo {
            protocol_version: ProtocolVersion::V_2025_03_26,
            capabilities: ServerCapabilities {
                tools: Some(ToolsCapability {
                    list_changed: Some(false),
                }),
                ..Default::default()
            },
            server_info: Implementation {
                name: "frankenmemory".into(),
                version: "0.1.0".into(),
            },
            instructions: Some(
                "Frankenmemory: a memory engine that captures, recalls, searches, and curates memories. \
                 Use capture to store memories, recall for GT-tagged retrieval, search for raw queries, \
                 groom for curation (decay/dedup/reflect)."
                    .into(),
            ),
        }
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParam,
        context: RequestContext<rmcp::RoleServer>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let tcc = ToolCallContext::new(self, request, context);
        self.tool_router.call(tcc).await
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParam>,
        _context: RequestContext<rmcp::RoleServer>,
    ) -> Result<ListToolsResult, rmcp::ErrorData> {
        let items = self.tool_router.list_all();
        Ok(ListToolsResult::with_all_items(items))
    }

    async fn list_resources(
        &self,
        _request: Option<PaginatedRequestParam>,
        _context: RequestContext<rmcp::RoleServer>,
    ) -> Result<ListResourcesResult, rmcp::ErrorData> {
        Ok(ListResourcesResult::default())
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
        return Err("usage: fm-mcp [--prepare-database]; FM_DB_PATH selects the absolute database".into());
    }
    let config = FmConfig::default();
    let db_path = std::path::Path::new(&config.db_path);
    if !db_path.is_absolute() {
        return Err(format!("FM_DB_PATH must be absolute: {}", config.db_path).into());
    }
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut store = SqliteStore::new(&config.db_path, config.embedding.dimensions)?;
    store
        .set_code_index_lease_seconds(config.code_index.lease_seconds)
        .map_err(|error| format!("invalid code-index configuration: {error}"))?;
    let store = Arc::new(store);
    let (database_id, schema_version) = store.database_identity()?;
    if let Ok(expected) = std::env::var("FM_DB_ID") {
        if !expected.trim().is_empty() && expected.trim() != database_id {
            return Err(format!(
                "frankenmemory database identity mismatch: expected {}, opened {}",
                expected.trim(),
                database_id
            )
            .into());
        }
    }
    if prepare_only {
        println!("{}", serde_json::json!({
            "database_id": database_id,
            "schema_version": schema_version,
            "database_path": config.db_path,
        }));
        return Ok(());
    }
    tracing::info!(database_id, schema_version, path = %config.db_path, "frankenmemory database ready");
    // Curated/raw memory rows currently lack the immutable route, adapter,
    // model, and dimension generation metadata required for safe provider
    // rotation.  Fail closed and retain exact/FTS recall.  Document vectors
    // use the fingerprinted Python generation schema and Open Clank's managed
    // ``embeddings.create`` operation instead.
    tracing::info!("embeddings: disabled for unfingerprinted curated-memory rows");
    let embed: Arc<dyn EmbeddingClient> =
        Arc::new(DisabledEmbeddingClient::new(config.embedding.dimensions));
    let provider = Arc::new(NativeProvider::new(store.clone(), embed, config));

    let server = FrankenmemoryServer::new(provider, store);

    tracing::info!("frankenmemory MCP server starting on stdio");

    let transport = stdio();
    let service = server.serve(transport).await?;

    service.waiting().await?;
    Ok(())
}
