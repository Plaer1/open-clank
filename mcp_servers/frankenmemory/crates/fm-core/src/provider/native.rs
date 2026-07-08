use async_trait::async_trait;
use tracing::info;

use crate::config::FmConfig;
use crate::curate;
use crate::embed::EmbeddingClient;
use crate::provider::*;
use crate::record::*;
use crate::retrieval;
use crate::store::MemoryStore;
use std::sync::Arc;

pub struct NativeProvider {
    store: Arc<dyn MemoryStore>,
    embed: Arc<dyn EmbeddingClient>,
    config: FmConfig,
}

impl NativeProvider {
    pub fn new(
        store: Arc<dyn MemoryStore>,
        embed: Arc<dyn EmbeddingClient>,
        config: FmConfig,
    ) -> Self {
        Self {
            store,
            embed,
            config,
        }
    }
}

#[async_trait]
impl MemoryProvider for NativeProvider {
    fn id(&self) -> &str {
        "native"
    }

    async fn capture(&self, turn: &CompletedTurn) -> CaptureResult {
        let workspace_id = if turn.workspace_id.is_empty() {
            "global".to_string()
        } else {
            turn.workspace_id.clone()
        };

        let mut records_captured = 0usize;
        let mut vectors_written = 0usize;

        // Capture user turn as raw
        let mut raw_user = RawTurn::new("user", &turn.user_text);
        raw_user.session_key = turn.session_key.clone();
        raw_user.session_id = turn.session_id.clone();
        raw_user.workspace_id = workspace_id.clone();
        raw_user.workspace_path = turn.workspace_path.clone();

        let user_emb = self.embed.embed(&turn.user_text).await.ok();
        if self
            .store
            .upsert_raw(&raw_user, user_emb.as_deref())
            .await
        {
            records_captured += 1;
            if user_emb.is_some() {
                vectors_written += 1;
            }
        }

        // Capture assistant turn as raw
        let mut raw_asst = RawTurn::new("assistant", &turn.assistant_text);
        raw_asst.session_key = turn.session_key.clone();
        raw_asst.session_id = turn.session_id.clone();
        raw_asst.workspace_id = workspace_id.clone();
        raw_asst.workspace_path = turn.workspace_path.clone();

        let asst_emb = self.embed.embed(&turn.assistant_text).await.ok();
        if self
            .store
            .upsert_raw(&raw_asst, asst_emb.as_deref())
            .await
        {
            records_captured += 1;
            if asst_emb.is_some() {
                vectors_written += 1;
            }
        }

        // Extract a curated record from the turn
        let content = format!("User: {}\nAssistant: {}", turn.user_text, turn.assistant_text);
        let mut record = MemoryRecord::new(&content);
        record.source = turn.source.clone();
        record.source_type = SourceType::AutoExtracted;
        record.owner = turn.owner.clone();
        record.workspace_id = workspace_id.clone();
        record.workspace_path = turn.workspace_path.clone();
        record.session_key = turn.session_key.clone();
        record.session_id = turn.session_id.clone();

        let rec_emb = self.embed.embed(&content).await.ok();
        if self
            .store
            .upsert_curated(&record, rec_emb.as_deref())
            .await
        {
            records_captured += 1;
            if rec_emb.is_some() {
                vectors_written += 1;
            }
        }

        info!(
            "native capture: {} records, {} vectors",
            records_captured, vectors_written
        );

        CaptureResult {
            records_captured,
            vectors_written,
            providers_succeeded: 1,
            providers_failed: 0,
        }
    }

    async fn recall(&self, q: &RecallQuery) -> RecallResult {
        let workspace_id = q
            .workspace_id
            .clone()
            .unwrap_or_else(|| self.config.workspace_id.clone());

        let query_emb = self.embed.embed(&q.query).await.ok();

        let results = retrieval::hybrid_recall(
            self.store.as_ref(),
            &self.embed,
            &q.query,
            query_emb.as_deref(),
            q.top_k,
            &workspace_id,
            self.config.recall.workspace_boost,
            &self.config.collapse,
        )
        .await;

        let (prepend_context, gt_preamble) =
            retrieval::ground_truth::format_recall_output(&results, &q.query);

        RecallResult {
            prepend_context,
            append_system_context: String::new(),
            memories: results.clone(),
            recall_strategy: "hybrid".into(),
            ground_truth_preamble: gt_preamble,
        }
    }

    async fn search(&self, p: &SearchParams) -> SearchResult {
        let results = match p.tier {
            Tier::Raw => {
                let raw_results = self.store.search_raw_fts(&p.query, p.limit).await;
                raw_results
                    .into_iter()
                    .map(|sr| ScoredRecord {
                        record: MemoryRecord {
                            id: sr.turn.id,
                            content: sr.turn.content,
                            kind: MemoryKind::Raw,
                            workspace_id: sr.turn.workspace_id,
                            session_key: sr.turn.session_key,
                            session_id: sr.turn.session_id,
                            ..MemoryRecord::new("")
                        },
                        score: sr.score,
                        source_label: "raw_fts".into(),
                    })
                    .collect()
            }
            Tier::Curated => self.store.search_curated_fts(&p.query, p.limit).await,
        };

        let total = results.len();
        SearchResult { results, total }
    }

    async fn groom(&self, op: &GroomOpArgs) -> GroomResult {
        match op.op {
            GroomOp::Decay => {
                curate::decay::run_decay(
                    self.store.as_ref(),
                    &self.config.decay,
                    op.workspace_id.as_deref(),
                )
                .await
            }
            GroomOp::Dedup => {
                curate::dedup::run_dedup(self.store.as_ref(), &self.embed, op.dry_run).await
            }
            GroomOp::Reflect => curate::reflect::run_reflect(self.store.as_ref()).await,
        }
    }
}
