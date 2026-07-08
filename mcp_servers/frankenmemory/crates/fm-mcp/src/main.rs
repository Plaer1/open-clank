use std::future::Future;
use std::sync::Arc;

use fm_core::config::FmConfig;
use fm_core::embed::NoopEmbeddingClient;
use fm_core::provider::native::NativeProvider;
use fm_core::provider::{MemoryProvider, GroomOpArgs};
use fm_core::record::*;
use fm_core::store::sqlite::SqliteStore;
use rmcp::{
    handler::server::{tool::Parameters, ServerHandler, tool::ToolCallContext},
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
    tool_router: rmcp::handler::server::router::tool::ToolRouter<Self>,
}

#[derive(Debug, Deserialize, JsonSchema)]
struct CaptureParams {
    content: String,
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
}

#[derive(Debug, Deserialize, JsonSchema)]
struct GroomParams {
    op: String,
    #[serde(default)]
    workspace_id: Option<String>,
    #[serde(default)]
    dry_run: Option<bool>,
}

#[tool_router]
impl FrankenmemoryServer {
    fn new(provider: Arc<NativeProvider>) -> Self {
        Self {
            provider,
            tool_router: Self::tool_router(),
        }
    }

    #[tool(
        name = "capture",
        description = "Capture a conversation turn into memory. Dual-records to all enabled provider stores. workspace_id tags the record for workspace-scoped recall."
    )]
    async fn capture(&self, Parameters(params): Parameters<CaptureParams>) -> Result<CallToolResult, rmcp::ErrorData> {
        let turn = CompletedTurn {
            user_text: params.content.clone(),
            assistant_text: String::new(),
            session_key: params.session_key.unwrap_or_default(),
            session_id: params.session_id.unwrap_or_default(),
            workspace_id: params.workspace_id.unwrap_or_else(|| "global".to_string()),
            workspace_path: params.workspace_path,
            source: params.source.unwrap_or_else(|| "mcp_capture".to_string()),
            owner: params.owner,
        };

        let result = self.provider.capture(&turn).await;

        let response = serde_json::json!({
            "records_captured": result.records_captured,
            "vectors_written": result.vectors_written,
            "providers_succeeded": result.providers_succeeded,
            "providers_failed": result.providers_failed,
        });

        Ok(CallToolResult::success(vec![Content::text(
            serde_json::to_string_pretty(&response).unwrap_or_default(),
        )]))
    }

    #[tool(
        name = "recall",
        description = "Recall memories relevant to a query. Returns GT-tagged, provenance-tagged results. mode toggles between layer_a and layer_b. workspace_id biases ranking: current workspace boosted, global always participates."
    )]
    async fn recall(&self, Parameters(params): Parameters<RecallParams>) -> Result<CallToolResult, rmcp::ErrorData> {
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
            workspace_id: params.workspace_id,
            top_k: params.top_k.unwrap_or(10),
            tier,
            owner: params.owner,
            router: false,
            rerank: false,
        };

        let result = self.provider.recall(&query).await;

        let mut output = String::new();
        if let Some(ref gt) = result.ground_truth_preamble {
            output.push_str(gt);
            output.push('\n');
        }
        output.push_str(&result.prepend_context);
        output.push_str(&format!("\nStrategy: {}\n", result.recall_strategy));
        output.push_str(&format!("Results: {} memories\n", result.memories.len()));

        Ok(CallToolResult::success(vec![Content::text(output)]))
    }

    #[tool(
        name = "search",
        description = "Raw search over a tier (curated/raw). No GT framing. Returns matching memories."
    )]
    async fn search(&self, Parameters(params): Parameters<SearchParams>) -> Result<CallToolResult, rmcp::ErrorData> {
        let tier = match params.tier.as_deref() {
            Some("raw") => Tier::Raw,
            _ => Tier::Curated,
        };
        let kind = params.kind.and_then(|k| match k.as_str() {
            "persona" => Some(MemoryKind::Persona),
            "episodic" => Some(MemoryKind::Episodic),
            "instruction" => Some(MemoryKind::Instruction),
            "fact" => Some(MemoryKind::Fact),
            "fabric" => Some(MemoryKind::Fabric),
            "wiki" => Some(MemoryKind::Wiki),
            "raw" => Some(MemoryKind::Raw),
            _ => None,
        });

        let search_params = fm_core::record::SearchParams {
            query: params.query,
            kind,
            scene: params.scene,
            tier,
            limit: params.limit.unwrap_or(10),
            workspace_id: params.workspace_id,
        };

        let result = self.provider.search(&search_params).await;

        let mut output = format!("Found {} results:\n\n", result.total);
        for (i, r) in result.results.iter().enumerate() {
            output.push_str(&format!(
                "{}. [{}] {} (score: {:.3})\n",
                i + 1,
                format!("{:?}", r.record.kind).to_lowercase(),
                r.record.content,
                r.score,
            ));
        }

        Ok(CallToolResult::success(vec![Content::text(output)]))
    }

    #[tool(
        name = "groom",
        description = "Curation dispatcher. Operations: decay (archive aged records), dedup (merge near-duplicates), reflect (adjust confidence)."
    )]
    async fn groom(&self, Parameters(params): Parameters<GroomParams>) -> Result<CallToolResult, rmcp::ErrorData> {
        let op = match params.op.as_str() {
            "decay" => GroomOp::Decay,
            "dedup" => GroomOp::Dedup,
            "reflect" => GroomOp::Reflect,
            _ => {
                return Ok(CallToolResult::error(vec![Content::text(
                    format!("Unknown groom op: {}. Use decay, dedup, or reflect.", params.op),
                )]));
            }
        };

        let args = GroomOpArgs {
            op,
            workspace_id: params.workspace_id,
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
    let store = Arc::new(SqliteStore::new(&config.db_path, config.embedding.dimensions)?);
    let embed = Arc::new(NoopEmbeddingClient::new(config.embedding.dimensions));
    let provider = Arc::new(NativeProvider::new(store, embed, config));

    let server = FrankenmemoryServer::new(provider);

    tracing::info!("frankenmemory MCP server starting on stdio");

    let transport = stdio();
    let service = server.serve(transport).await?;

    service.waiting().await?;
    Ok(())
}
