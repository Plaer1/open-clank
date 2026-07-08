use tracing::info;

use crate::embed::EmbeddingClient;
use crate::record::*;
use crate::store::MemoryStore;

const SIMILARITY_THRESHOLD: f32 = 0.92;

pub async fn run_dedup(
    store: &dyn MemoryStore,
    embed: &dyn EmbeddingClient,
    dry_run: bool,
) -> GroomResult {
    let all_records = store.search_curated_fts("", 1000).await;

    // Find near-duplicate pairs
    let mut candidates: Vec<(usize, usize, f32)> = Vec::new();

    for i in 0..all_records.len() {
        if all_records[i].record.exempt_from_dedup || all_records[i].record.archived {
            continue;
        }

        let emb_i = match embed.embed(&all_records[i].record.content).await {
            Ok(e) => e,
            Err(_) => continue,
        };

        for j in (i + 1)..all_records.len() {
            if all_records[j].record.exempt_from_dedup || all_records[j].record.archived {
                continue;
            }

            let emb_j = match embed.embed(&all_records[j].record.content).await {
                Ok(e) => e,
                Err(_) => continue,
            };

            let similarity = cosine_similarity(&emb_i, &emb_j);
            if similarity >= SIMILARITY_THRESHOLD {
                candidates.push((i, j, similarity));
            }
        }
    }

    // Sort by similarity descending
    candidates.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));

    let mut merged = 0usize;

    for (i, j, sim) in &candidates {
        let record_i = &all_records[*i].record;
        let record_j = &all_records[*j].record;

        if dry_run {
            info!(
                "dedup candidate: {} <-> {} (similarity={:.3})",
                record_i.id, record_j.id, sim
            );
            merged += 1;
            continue;
        }

        // Merge: keep the one with higher trust/importance, union tags
        let (keep, drop) = if record_i.trust_score >= record_j.trust_score {
            (record_i, record_j)
        } else {
            (record_j, record_i)
        };

        let mut merged_record = keep.clone();
        let mut tags: Vec<String> = keep.tags.clone();
        for tag in &drop.tags {
            if !tags.contains(tag) {
                tags.push(tag.clone());
            }
        }
        merged_record.tags = tags;
        merged_record.updated_at = chrono::Utc::now().to_rfc3339();

        // Merge timestamps
        let mut timestamps = keep.timestamps.clone();
        for ts in &drop.timestamps {
            if !timestamps.contains(ts) {
                timestamps.push(ts.clone());
            }
        }
        merged_record.timestamps = timestamps;

        store.upsert_curated(&merged_record, None).await;
        store.delete_curated_batch(&[drop.id.clone()]).await;
        merged += 1;

        info!(
            "dedup merged: kept {}, dropped {} (similarity={:.3})",
            keep.id, drop.id, sim
        );
    }

    GroomResult {
        op: GroomOp::Dedup,
        records_archived: 0,
        records_merged: merged,
        records_reflected: 0,
        alerts: Vec::new(),
    }
}

fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm_a == 0.0 || norm_b == 0.0 {
        0.0
    } else {
        dot / (norm_a * norm_b)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cosine_sim_identical() {
        let a = vec![1.0, 0.0, 0.0];
        let b = vec![1.0, 0.0, 0.0];
        assert!((cosine_similarity(&a, &b) - 1.0).abs() < 0.001);
    }

    #[test]
    fn cosine_sim_orthogonal() {
        let a = vec![1.0, 0.0, 0.0];
        let b = vec![0.0, 1.0, 0.0];
        assert!((cosine_similarity(&a, &b)).abs() < 0.001);
    }
}
