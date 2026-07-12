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
    hybrid_recall_scoped(
        store,
        _embed,
        query_text,
        query_embedding,
        top_k,
        workspace_id,
        workspace_boost,
        collapse_config,
        None,
        None,
    )
    .await
}

pub async fn hybrid_recall_scoped(
    store: &dyn MemoryStore,
    _embed: &dyn EmbeddingClient,
    query_text: &str,
    query_embedding: Option<&[f32]>,
    top_k: usize,
    workspace_id: &str,
    workspace_boost: f32,
    collapse_config: &CollapseConfig,
    owner: Option<&str>,
    requested_workspace: Option<&str>,
) -> Vec<ScoredRecord> {
    // 4-level fallback cascade. Scope is applied inside the store before
    // ranking/limit, so another tenant cannot crowd a user's top-k results.
    let results = try_hybrid_scoped(
        store,
        query_text,
        query_embedding,
        top_k,
        owner,
        requested_workspace,
    )
    .await;
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
        let results = store
            .search_curated_vector_scoped(emb, top_k, owner, requested_workspace)
            .await;
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
    let results = store
        .search_curated_fts_scoped(query_text, top_k, owner, requested_workspace)
        .await;
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
    let results = store
        .search_curated_fts_scoped(query_text, top_k, owner, requested_workspace)
        .await;
    if !results.is_empty() {
        return results;
    }

    // Fail-open: empty
    Vec::new()
}

async fn try_hybrid_scoped(
    store: &dyn MemoryStore,
    query_text: &str,
    query_embedding: Option<&[f32]>,
    top_k: usize,
    owner: Option<&str>,
    workspace_id: Option<&str>,
) -> Vec<ScoredRecord> {
    store
        .search_curated_hybrid_scoped(
            crate::store::HybridQuery {
                query_text: Some(query_text.to_string()),
                query_embedding: query_embedding.map(|e| e.to_vec()),
                sparse_vector: None,
                top_k,
                workspace_id: None,
            },
            owner,
            workspace_id,
        )
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

    ranked.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    ranked
}
