pub mod collapse;
pub mod ground_truth;
pub mod rrf;

use crate::config::CollapseConfig;
use crate::embed::EmbeddingClient;
use crate::record::*;
use crate::store::MemoryStore;

pub async fn hybrid_recall(
    store: &dyn MemoryStore,
    _embed: &dyn EmbeddingClient,
    query_text: &str,
    query_embedding: Option<&[f32]>,
    top_k: usize,
    workspace_id: &str,
    workspace_boost: f32,
    collapse_config: &CollapseConfig,
) -> Vec<ScoredRecord> {
    // 4-level fallback cascade
    let results = try_hybrid(store, query_text, query_embedding, top_k).await;
    if !results.is_empty() {
        return apply_collapse_and_rank(
            results,
            query_text,
            workspace_id,
            workspace_boost,
            collapse_config,
        );
    }

    // Fallback: dense-only
    if let Some(emb) = query_embedding {
        let results = store.search_curated_vector(emb, top_k).await;
        if !results.is_empty() {
            return apply_collapse_and_rank(
                results,
                query_text,
                workspace_id,
                workspace_boost,
                collapse_config,
            );
        }
    }

    // Fallback: FTS
    let results = store.search_curated_fts(query_text, top_k).await;
    if !results.is_empty() {
        return apply_collapse_and_rank(
            results,
            query_text,
            workspace_id,
            workspace_boost,
            collapse_config,
        );
    }

    // Fallback: keyword (substring match via FTS with individual words)
    let results = store.search_curated_fts(query_text, top_k).await;
    if !results.is_empty() {
        return results;
    }

    // Fail-open: empty
    Vec::new()
}

async fn try_hybrid(
    store: &dyn MemoryStore,
    query_text: &str,
    query_embedding: Option<&[f32]>,
    top_k: usize,
) -> Vec<ScoredRecord> {
    store
        .search_curated_hybrid(crate::store::HybridQuery {
            query_text: Some(query_text.to_string()),
            query_embedding: query_embedding.map(|e| e.to_vec()),
            sparse_vector: None,
            top_k,
            workspace_id: None,
        })
        .await
}

fn apply_collapse_and_rank(
    results: Vec<ScoredRecord>,
    query_text: &str,
    workspace_id: &str,
    workspace_boost: f32,
    config: &CollapseConfig,
) -> Vec<ScoredRecord> {
    // Apply collapse
    let collapsed = collapse::collapse(results, query_text, config);

    // Apply workspace-aware ranking
    let mut ranked: Vec<ScoredRecord> = collapsed
        .into_iter()
        .map(|mut r| {
            // Workspace boost
            if r.record.workspace_id == workspace_id {
                r.score *= workspace_boost;
            }
            // Global workspace always at normal weight (no demotion)
            r
        })
        .collect();

    ranked.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    ranked
}
