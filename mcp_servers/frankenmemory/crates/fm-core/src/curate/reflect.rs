use tracing::info;

use crate::record::*;
use crate::store::MemoryStore;

pub async fn run_reflect(store: &dyn MemoryStore) -> GroomResult {
    let all_records = store.search_curated_fts("", 100).await;

    let mut reflected = 0usize;

    // Micro-reflection: adjust confidence based on consistency
    for scored in &all_records {
        let r = &scored.record;
        if r.archived {
            continue;
        }

        // Search for similar records to check consistency
        let similar = store.search_curated_fts(&r.content, 5).await;
        let similar: Vec<&ScoredRecord> = similar
            .iter()
            .filter(|s| s.record.id != r.id)
            .collect();

        if similar.len() < 2 {
            continue;
        }

        // Simple consistency check: if similar records have similar trust scores, boost confidence
        let avg_trust: f32 = similar.iter().map(|s| s.record.trust_score).sum::<f32>()
            / similar.len() as f32;
        let trust_variance: f32 = similar
            .iter()
            .map(|s| (s.record.trust_score - avg_trust).powi(2))
            .sum::<f32>()
            / similar.len() as f32;

        let mut updated = r.clone();
        if trust_variance < 0.1 {
            // Consistent: boost confidence
            updated.confidence_score = (updated.confidence_score + 0.05).min(1.0);
            info!(
                "reflect: consistent records around {}, boosted confidence to {:.2}",
                r.id, updated.confidence_score
            );
        } else {
            // Inconsistent: lower confidence
            let severity = if trust_variance > 0.3 { 0.20 } else { 0.10 };
            updated.confidence_score = (updated.confidence_score - severity).max(0.0);
            info!(
                "reflect: inconsistent records around {}, lowered confidence to {:.2}",
                r.id, updated.confidence_score
            );
        }

        updated.updated_at = chrono::Utc::now().to_rfc3339();
        store.upsert_curated(&updated, None).await;
        reflected += 1;
    }

    GroomResult {
        op: GroomOp::Reflect,
        records_archived: 0,
        records_merged: 0,
        records_reflected: reflected,
        alerts: Vec::new(),
    }
}
