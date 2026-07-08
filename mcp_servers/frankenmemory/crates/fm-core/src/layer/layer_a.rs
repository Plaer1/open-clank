use async_trait::async_trait;

use crate::config::{CollapseConfig, RecallConfig};
use crate::embed::EmbeddingClient;
use crate::layer::RecallLayer;
use crate::record::*;
use crate::retrieval;
use crate::store::MemoryStore;

pub struct LayerA {
    pub recall_config: RecallConfig,
    pub collapse_config: CollapseConfig,
}

#[async_trait]
impl RecallLayer for LayerA {
    async fn recall(
        &self,
        query: &RecallQuery,
        store: &dyn MemoryStore,
        embed: &dyn EmbeddingClient,
    ) -> RecallResult {
        let workspace_id = query
            .workspace_id
            .clone()
            .unwrap_or_else(|| "global".to_string());

        let query_emb = embed.embed(&query.query).await.ok();

        let mut results = retrieval::hybrid_recall(
            store,
            embed,
            &query.query,
            query_emb.as_deref(),
            query.top_k,
            &workspace_id,
            self.recall_config.workspace_boost,
            &self.collapse_config,
        )
        .await;

        // Tag with Ground-Truth
        retrieval::ground_truth::tag_ground_truth(&mut results);

        let (prepend_context, gt_preamble) =
            retrieval::ground_truth::format_recall_output(&results, &query.query);

        RecallResult {
            prepend_context,
            append_system_context: String::new(),
            memories: results,
            recall_strategy: "layer_a_hybrid".into(),
            ground_truth_preamble: gt_preamble,
        }
    }
}
