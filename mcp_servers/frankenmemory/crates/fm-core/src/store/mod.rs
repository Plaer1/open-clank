pub mod graph;
pub mod sqlite;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::record::*;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoreCapabilities {
    pub vector_search: bool,
    pub fts_search: bool,
    pub native_hybrid: bool,
    pub sparse_vectors: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HybridQuery {
    pub query_text: Option<String>,
    pub query_embedding: Option<Vec<f32>>,
    pub sparse_vector: Option<Vec<(u32, f32)>>,
    pub top_k: usize,
    pub workspace_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReindexCounts {
    pub curated_count: usize,
    pub raw_count: usize,
}

pub type EmbedFn = Box<dyn Fn(&str) -> Option<Vec<f32>> + Send + Sync>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum FactOp {
    Add {
        content: String,
        entities: Vec<String>,
    },
    Search {
        query: String,
        limit: usize,
    },
    Probe {
        entity: String,
    },
    Update {
        id: String,
        content: String,
    },
    Remove {
        id: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum FactResult {
    Added { id: String },
    SearchResults { results: Vec<ScoredRecord> },
    Probe { results: Vec<ScoredRecord> },
    Updated { success: bool },
    Removed { success: bool },
}

#[async_trait]
pub trait MemoryStore: Send + Sync {
    /// Concrete-store escape hatch: graph operations live on SqliteStore
    /// (they are not part of the provider-agnostic trait surface).
    fn as_sqlite(&self) -> Option<&crate::store::sqlite::SqliteStore> {
        None
    }

    fn capabilities(&self) -> StoreCapabilities;
    fn is_degraded(&self) -> bool;

    async fn upsert_curated(&self, r: &MemoryRecord, embedding: Option<&[f32]>) -> bool;
    async fn delete_curated_batch(&self, ids: &[String]) -> bool;
    async fn update_curated_record(
        &self,
        id: &str,
        content: Option<&str>,
        kind: Option<MemoryKind>,
        category: Option<&str>,
        pinned: Option<bool>,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        let _ = (id, content, kind, category, pinned, owner, workspace_id);
        false
    }
    async fn delete_curated_record(
        &self,
        id: &str,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        let _ = (id, owner, workspace_id);
        false
    }
    async fn delete_curated_expired(&self, cutoff_iso: &str) -> usize;
    async fn search_curated_vector(&self, q: &[f32], top_k: usize) -> Vec<ScoredRecord>;
    async fn search_curated_fts(&self, fts_query: &str, limit: usize) -> Vec<ScoredRecord>;
    async fn search_curated_hybrid(&self, q: HybridQuery) -> Vec<ScoredRecord>;

    /// Scoped variants apply tenant predicates before ranking and LIMIT. The
    /// default keeps non-SQL providers source-compatible; providers with
    /// durable scope columns should override these methods.
    async fn search_curated_vector_scoped(
        &self,
        q: &[f32],
        top_k: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRecord> {
        let _ = (owner, workspace_id);
        self.search_curated_vector(q, top_k).await
    }
    async fn search_curated_fts_scoped(
        &self,
        fts_query: &str,
        limit: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRecord> {
        let _ = (owner, workspace_id);
        self.search_curated_fts(fts_query, limit).await
    }
    async fn search_curated_hybrid_scoped(
        &self,
        q: HybridQuery,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRecord> {
        let _ = (owner, workspace_id);
        self.search_curated_hybrid(q).await
    }

    async fn upsert_raw(&self, r: &RawTurn, embedding: Option<&[f32]>) -> bool;
    async fn search_raw_vector(&self, q: &[f32], top_k: usize) -> Vec<ScoredRaw>;
    async fn search_raw_fts(&self, fts_query: &str, limit: usize) -> Vec<ScoredRaw>;
    async fn search_raw_vector_scoped(
        &self,
        q: &[f32],
        top_k: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRaw> {
        let _ = (owner, workspace_id);
        self.search_raw_vector(q, top_k).await
    }
    async fn search_raw_fts_scoped(
        &self,
        fts_query: &str,
        limit: usize,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> Vec<ScoredRaw> {
        let _ = (owner, workspace_id);
        self.search_raw_fts(fts_query, limit).await
    }

    async fn reindex_all(&self, embed: &EmbedFn) -> ReindexCounts;
    async fn fact(&self, op: FactOp) -> FactResult;
}
