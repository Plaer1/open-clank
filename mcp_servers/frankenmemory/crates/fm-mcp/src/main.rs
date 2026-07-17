use std::future::Future;
use std::sync::Arc;

use fm_core::config::FmConfig;
use fm_core::embed::{EmbeddingClient, HttpEmbeddingClient, NoopEmbeddingClient};
use fm_core::provider::native::NativeProvider;
use fm_core::provider::{GroomOpArgs, MemoryProvider};
use fm_core::record::*;
use fm_core::store::sqlite::SqliteStore;
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

#[derive(Clone)]
struct FrankenmemoryServer {
    provider: Arc<NativeProvider>,
    /// Concrete store handle for graph ops (they live on SqliteStore, not
    /// on the MemoryStore trait).
    graph_store: Arc<SqliteStore>,
    tool_router: rmcp::handler::server::router::tool::ToolRouter<Self>,
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
    category: Option<String>,
    #[serde(default)]
    metadata: Option<serde_json::Value>,
    /// A complete turn can be sent atomically with these fields. Legacy
    /// content+role remains accepted for deliberate/manual callers.
    #[serde(default)]
    user_text: Option<String>,
    #[serde(default)]
    assistant_text: Option<String>,
    /// raw_only | candidate | manual
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

fn parse_memory_kind(value: &str) -> Option<MemoryKind> {
    match value {
        "persona" | "identity" => Some(MemoryKind::Persona),
        "episodic" | "event" => Some(MemoryKind::Episodic),
        "instruction" | "preference" => Some(MemoryKind::Instruction),
        "fact" | "contact" | "project" | "goal" => Some(MemoryKind::Fact),
        "fabric" => Some(MemoryKind::Fabric),
        "wiki" | "reference" => Some(MemoryKind::Wiki),
        "raw" => Some(MemoryKind::Raw),
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
    path: String,
    /// For action=impact: file path relative to the codebase root.
    #[serde(default)]
    rel_path: Option<String>,
    #[serde(default)]
    max_depth: Option<usize>,
}

#[tool_router]
impl FrankenmemoryServer {
    fn new(provider: Arc<NativeProvider>, graph_store: Arc<SqliteStore>) -> Self {
        Self {
            provider,
            graph_store,
            tool_router: Self::tool_router(),
        }
    }

    #[tool(
        name = "capture",
        description = "Capture one complete conversation turn. Automatic callers use user_text+assistant_text with capture_mode=raw_only, owner, workspace_id, source_event_id, and source_message_ids. Deliberate remember operations use capture_mode=manual."
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
        if !matches!(capture_mode.as_str(), "raw_only" | "candidate" | "manual") {
            return Err(rmcp::ErrorData::invalid_params(
                "capture_mode must be raw_only|candidate|manual",
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
        name = "review_candidate",
        description = "Accept or reject one candidate. Owner and workspace are mandatory; acceptance creates the only curated record and embedding."
    )]
    async fn review_candidate(
        &self,
        Parameters(params): Parameters<CandidateReviewParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
        let result = if params.rebuild_graph_fts.unwrap_or(false) {
            self.provider.rebuild_graph_cue_fts()
        } else {
            self.provider.quality_status()
        }
        .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
        )]))
    }

    #[tool(
        name = "owner_lifecycle",
        description = "Privileged owner-scoped lifecycle operation. action=stats counts every memory tier; action=purge atomically removes the authenticated owner's tiers and graph; action=rename atomically moves them to new_owner."
    )]
    async fn owner_lifecycle(
        &self,
        Parameters(params): Parameters<OwnerLifecycleParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
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
            _ => {
                return Err(rmcp::ErrorData::invalid_params(
                    "action must be stats|purge|rename",
                    None,
                ));
            }
        }
        .map_err(|error| rmcp::ErrorData::internal_error(error, None))?;
        Ok(CallToolResult::success(vec![Content::text(
            result.to_string(),
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
                let overview = self.graph_store.graph_overview(&scope, limit).map_err(|e| {
                    rmcp::ErrorData::internal_error(format!("overview failed: {e}"), None)
                })?;
                let mut value = serde_json::to_value(&overview)
                    .unwrap_or_else(|_| serde_json::json!({}));
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

    #[tool(
        name = "code_index",
        description = "OPT-IN code graph. action=index parses a codebase (Rust/Python/TypeScript) into symbols, imports and name-matched call edges — nothing is ever indexed without this explicit call. action=status reports file/symbol counts, action=remove deletes the codebase's entire namespace, action=impact lists files transitively importing rel_path (blast radius). Explore results with graph_walk."
    )]
    async fn code_index(
        &self,
        Parameters(params): Parameters<CodeIndexParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, false)?;
        let err = |m: String| rmcp::ErrorData::invalid_params(m, None);
        let ierr = |m: String| rmcp::ErrorData::internal_error(m, None);
        let payload = match params.action.as_str() {
            "index" => {
                let root = std::path::Path::new(&params.path);
                if !root.is_absolute() || !root.is_dir() {
                    return Err(err(format!(
                        "path must be an existing absolute directory, got {}",
                        params.path
                    )));
                }
                let result = self
                    .graph_store
                    .code_index(&scope, root)
                    .map_err(|e| ierr(format!("code index failed: {e}")))?;
                serde_json::to_value(&result).unwrap_or_default()
            }
            "status" => {
                let (files, symbols, last) = self
                    .graph_store
                    .code_status(&scope, &params.path)
                    .map_err(|e| ierr(format!("status failed: {e}")))?;
                serde_json::json!({ "codebase": params.path, "files": files, "symbols": symbols, "last_indexed": last })
            }
            "stale" => {
                let root = std::path::Path::new(&params.path);
                if !root.is_absolute() || !root.is_dir() {
                    return Err(err(format!(
                        "path must be an existing absolute directory, got {}",
                        params.path
                    )));
                }
                let result = self
                    .graph_store
                    .code_stale(&scope, root)
                    .map_err(|e| ierr(format!("stale check failed: {e}")))?;
                serde_json::to_value(&result).unwrap_or_default()
            }
            "remove" => {
                let removed = self
                    .graph_store
                    .code_remove(&scope, &params.path)
                    .map_err(|e| ierr(format!("remove failed: {e}")))?;
                serde_json::json!({ "codebase": params.path, "files_removed": removed })
            }
            "impact" => {
                let rel = params
                    .rel_path
                    .ok_or_else(|| err("action=impact requires 'rel_path'".into()))?;
                let impacted = self
                    .graph_store
                    .code_impact(&scope, &params.path, &rel, params.max_depth.unwrap_or(4))
                    .map_err(|e| ierr(format!("impact failed: {e}")))?;
                serde_json::json!({ "codebase": params.path, "rel_path": rel, "impacted_files": impacted })
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
        description = "Curation dispatcher. Operations: decay (archive aged records), dedup (merge near-duplicates), reflect (adjust confidence)."
    )]
    async fn groom(
        &self,
        Parameters(params): Parameters<GroomParams>,
    ) -> Result<CallToolResult, rmcp::ErrorData> {
        let scope = request_scope(params.owner, params.workspace_id, true)?;
        let op = match params.op.as_str() {
            "decay" => GroomOp::Decay,
            "dedup" => GroomOp::Dedup,
            "reflect" => GroomOp::Reflect,
            "edge_decay" => GroomOp::EdgeDecay,
            "tag_normalize" => GroomOp::TagNormalize,
            _ => {
                return Ok(CallToolResult::error(vec![Content::text(
                    format!("Unknown groom op: {}. Use decay, dedup, reflect, edge_decay, or tag_normalize.", params.op),
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

    let config = FmConfig::default();
    let db_path = std::path::Path::new(&config.db_path);
    if !db_path.is_absolute() {
        return Err(format!("FM_DB_PATH must be absolute: {}", config.db_path).into());
    }
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let store = Arc::new(SqliteStore::new(
        &config.db_path,
        config.embedding.dimensions,
    )?);
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
    tracing::info!(database_id, schema_version, path = %config.db_path, "frankenmemory database ready");
    // Real embeddings by DEFAULT: unset FM_EMBED_API_BASE means the local
    // ollama endpoint baked into EmbeddingConfig::default(). If that isn't
    // running, per-call embed errors degrade gracefully to vectorless
    // records + FTS-only recall — honest, unlike hash pseudo-vectors.
    // FM_EMBED_API_BASE=none opts into the deterministic hash embedder
    // (offline tests, air-gapped machines).
    let embed_mode = std::env::var("FM_EMBED_API_BASE").unwrap_or_default();
    let embed: Arc<dyn EmbeddingClient> = if matches!(embed_mode.trim(), "none" | "noop" | "off") {
        tracing::info!("embeddings: deterministic noop (FM_EMBED_API_BASE={embed_mode})");
        Arc::new(NoopEmbeddingClient::new(config.embedding.dimensions))
    } else {
        tracing::info!(
            "embeddings: http {} model={} dims={}",
            config.embedding.api_base,
            config.embedding.model,
            config.embedding.dimensions
        );
        Arc::new(HttpEmbeddingClient::with_api_key(
            &config.embedding.api_base,
            &config.embedding.model,
            config.embedding.dimensions,
            config.embedding.cache_size,
            config.embedding.api_key.clone(),
        ))
    };
    let provider = Arc::new(NativeProvider::new(store.clone(), embed, config));

    let server = FrankenmemoryServer::new(provider, store);

    tracing::info!("frankenmemory MCP server starting on stdio");

    let transport = stdio();
    let service = server.serve(transport).await?;

    service.waiting().await?;
    Ok(())
}
