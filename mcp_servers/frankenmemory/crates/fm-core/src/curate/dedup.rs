use std::collections::{HashMap, HashSet};

use tracing::info;

use crate::embed::EmbeddingClient;
use crate::record::*;
use crate::store::{
    sqlite::{CuratedMaintenancePlan, SqliteStore},
    MemoryStore,
};

const SIMILARITY_THRESHOLD: f32 = 0.92;

pub async fn run_dedup(
    store: &SqliteStore,
    embed: &dyn EmbeddingClient,
    owner: &str,
    workspace_id: &str,
    dry_run: bool,
) -> GroomResult {
    let all_records = store
        .search_curated_fts_scoped("", 1000, Some(owner), Some(workspace_id))
        .await;
    let records_selected = all_records.len();

    // Find near-duplicate pairs
    let mut candidates: Vec<(usize, usize, f32)> = Vec::new();
    let mut embeddings = Vec::with_capacity(all_records.len());
    for scored in &all_records {
        if scored.record.exempt_from_dedup
            || scored.record.archived
            || scored.record.kind == MemoryKind::Unknown
        {
            embeddings.push(None);
        } else {
            embeddings.push(embed.embed(&scored.record.content).await.ok());
        }
    }

    for i in 0..all_records.len() {
        let Some(emb_i) = embeddings[i].as_deref() else {
            continue;
        };

        for j in (i + 1)..all_records.len() {
            let Some(emb_j) = embeddings[j].as_deref() else {
                continue;
            };

            let similarity = cosine_similarity(emb_i, emb_j);
            if similarity >= SIMILARITY_THRESHOLD {
                candidates.push((i, j, similarity));
            }
        }
    }

    // Sort by similarity descending
    candidates.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));

    let mut current: HashMap<String, MemoryRecord> = all_records
        .iter()
        .map(|scored| (scored.record.id.clone(), scored.record.clone()))
        .collect();
    let mut changed = HashSet::new();
    let mut removed = HashSet::new();

    for (i, j, sim) in &candidates {
        let id_i = &all_records[*i].record.id;
        let id_j = &all_records[*j].record.id;
        if removed.contains(id_i) || removed.contains(id_j) {
            continue;
        }
        let Some(record_i) = current.get(id_i).cloned() else {
            continue;
        };
        let Some(record_j) = current.get(id_j).cloned() else {
            continue;
        };

        // Merge: keep the one with higher trust/importance, union tags
        let (keep, drop) = if record_i.trust_score >= record_j.trust_score {
            (&record_i, &record_j)
        } else {
            (&record_j, &record_i)
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

        current.insert(keep.id.clone(), merged_record);
        changed.insert(keep.id.clone());
        removed.insert(drop.id.clone());

        info!(
            "dedup planned: kept {}, dropped {} (similarity={:.3}, dry_run={dry_run})",
            keep.id, drop.id, sim,
        );
    }

    let mut upserts: Vec<_> = changed
        .iter()
        .filter(|id| !removed.contains(*id))
        .filter_map(|id| current.get(id).cloned())
        .collect();
    upserts.sort_by(|left, right| left.id.cmp(&right.id));
    let mut delete_ids: Vec<_> = removed.iter().cloned().collect();
    delete_ids.sort();
    let plan = CuratedMaintenancePlan {
        upserts,
        delete_ids,
    };
    let mut alerts = Vec::new();
    let planned = plan.upserts.len() + plan.delete_ids.len();
    let records_skipped = records_selected.saturating_sub(planned);
    let mut merged = plan.delete_ids.len();
    let mut records_changed = planned;
    let mut records_conflicted = 0;
    if !dry_run && (!plan.upserts.is_empty() || !plan.delete_ids.is_empty()) {
        if let Err(error) = store.apply_curated_maintenance_plan(owner, workspace_id, &plan) {
            merged = 0;
            records_changed = 0;
            records_conflicted = planned;
            alerts.push(format!("dedup transaction failed: {error}"));
        }
    }

    GroomResult {
        op: GroomOp::Dedup,
        records_archived: 0,
        records_merged: merged,
        records_reflected: 0,
        records_selected,
        records_changed,
        records_skipped,
        records_conflicted,
        alerts,
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
    use crate::embed::NoopEmbeddingClient;

    fn duplicate_record(id: &str, owner: &str, workspace_id: &str) -> MemoryRecord {
        let mut record = MemoryRecord::new("shared duplicate amber fact");
        record.id = id.into();
        record.owner = Some(owner.into());
        record.workspace_id = workspace_id.into();
        record
    }

    #[tokio::test]
    async fn dedup_never_compares_or_deletes_across_tenant_scope() {
        let store = SqliteStore::memory(4).unwrap();
        for record in [
            duplicate_record("alice-a-1", "alice", "ws-a"),
            duplicate_record("alice-a-2", "alice", "ws-a"),
            {
                let mut record = duplicate_record("alice-a-exempt", "alice", "ws-a");
                record.exempt_from_dedup = true;
                record
            },
            duplicate_record("alice-b", "alice", "ws-b"),
            duplicate_record("bob-a", "bob", "ws-a"),
        ] {
            assert!(store.upsert_curated(&record, None).await);
        }
        let embed = NoopEmbeddingClient::new(4);

        let dry = run_dedup(&store, &embed, "alice", "ws-a", true).await;
        assert_eq!(dry.records_merged, 1);
        assert_eq!(dry.records_selected, 3);
        assert_eq!(dry.records_changed, 2);
        assert_eq!(dry.records_skipped, 1);
        assert_eq!(dry.records_conflicted, 0);
        assert_eq!(
            store
                .search_curated_fts_scoped("", 10, Some("alice"), Some("ws-a"))
                .await
                .len(),
            3
        );

        store
            .conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER fail_dedup BEFORE UPDATE ON curated
                 BEGIN SELECT RAISE(ABORT, 'injected dedup failure'); END;",
            )
            .unwrap();
        let failed = run_dedup(&store, &embed, "alice", "ws-a", false).await;
        assert_eq!(failed.records_merged, 0);
        assert_eq!(failed.records_selected, dry.records_selected);
        assert_eq!(failed.records_changed, 0);
        assert_eq!(failed.records_skipped, dry.records_skipped);
        assert_eq!(failed.records_conflicted, dry.records_changed);
        assert_eq!(
            store
                .search_curated_fts_scoped("", 10, Some("alice"), Some("ws-a"))
                .await
                .len(),
            3
        );
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch("DROP TRIGGER fail_dedup;")
            .unwrap();

        let live = run_dedup(&store, &embed, "alice", "ws-a", false).await;
        assert_eq!(live.records_merged, dry.records_merged);
        assert_eq!(live.records_selected, dry.records_selected);
        assert_eq!(live.records_changed, dry.records_changed);
        assert_eq!(live.records_skipped, dry.records_skipped);
        assert_eq!(live.records_conflicted, 0);
        assert_eq!(
            store
                .search_curated_fts_scoped("", 10, Some("alice"), Some("ws-a"))
                .await
                .len(),
            2
        );
        assert_eq!(
            store
                .search_curated_fts_scoped("", 10, Some("alice"), Some("ws-b"))
                .await
                .len(),
            1
        );
        assert_eq!(
            store
                .search_curated_fts_scoped("", 10, Some("bob"), Some("ws-a"))
                .await
                .len(),
            1
        );
    }

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
