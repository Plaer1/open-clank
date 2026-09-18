use tracing::info;

use crate::record::*;
use crate::store::{
    sqlite::{CuratedMaintenancePlan, SqliteStore},
    MemoryStore,
};

const REFLECT_STATE_KEY: &str = "_frankenmemory_reflect";

pub async fn run_reflect(
    store: &SqliteStore,
    owner: &str,
    workspace_id: &str,
    dry_run: bool,
) -> GroomResult {
    let all_records = store
        .search_curated_fts_scoped("", 100, Some(owner), Some(workspace_id))
        .await;
    let records_selected = all_records.len();

    let mut plan = CuratedMaintenancePlan::default();

    // Micro-reflection: adjust confidence based on consistency
    for scored in &all_records {
        let r = &scored.record;
        if r.archived || r.kind == MemoryKind::Unknown {
            continue;
        }

        // Search for similar records to check consistency
        let similar = store
            .search_curated_fts_scoped(&r.content, 5, Some(owner), Some(workspace_id))
            .await;
        let similar: Vec<&ScoredRecord> = similar
            .iter()
            .filter(|s| s.record.id != r.id && s.record.kind != MemoryKind::Unknown)
            .collect();

        if similar.len() < 2 {
            continue;
        }

        // Simple consistency check: if similar records have similar trust scores, boost confidence
        let avg_trust: f32 =
            similar.iter().map(|s| s.record.trust_score).sum::<f32>() / similar.len() as f32;
        let trust_variance: f32 = similar
            .iter()
            .map(|s| (s.record.trust_score - avg_trust).powi(2))
            .sum::<f32>()
            / similar.len() as f32;

        let mut corpus = similar
            .iter()
            .map(|scored| {
                format!(
                    "{}:{:08x}",
                    scored.record.id,
                    scored.record.trust_score.to_bits()
                )
            })
            .collect::<Vec<_>>();
        corpus.sort();
        let corpus_fingerprint = blake3::hash(corpus.join("\u{1f}").as_bytes())
            .to_hex()
            .to_string();
        let already_reflected = r.metadata.get(REFLECT_STATE_KEY).is_some_and(|state| {
            state
                .get("corpus_fingerprint")
                .and_then(|value| value.as_str())
                == Some(corpus_fingerprint.as_str())
                && state
                    .get("confidence_score")
                    .and_then(|value| value.as_f64())
                    .is_some_and(|value| (value as f32 - r.confidence_score).abs() <= f32::EPSILON)
        });
        if already_reflected {
            continue;
        }

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

        if (updated.confidence_score - r.confidence_score).abs() <= f32::EPSILON {
            continue;
        }
        if !updated.metadata.is_object() {
            updated.metadata = serde_json::json!({});
        }
        updated
            .metadata
            .as_object_mut()
            .expect("reflect metadata was normalized to an object")
            .insert(
                REFLECT_STATE_KEY.into(),
                serde_json::json!({
                    "version": 1,
                    "corpus_fingerprint": corpus_fingerprint,
                    "confidence_score": updated.confidence_score,
                }),
            );
        updated.updated_at = chrono::Utc::now().to_rfc3339();
        plan.upserts.push(updated);
    }

    let mut alerts = Vec::new();
    let planned = plan.upserts.len();
    let records_skipped = records_selected.saturating_sub(planned);
    let mut reflected = planned;
    let mut records_changed = planned;
    let mut records_conflicted = 0;
    if !dry_run && !plan.upserts.is_empty() {
        if let Err(error) = store.apply_curated_maintenance_plan(owner, workspace_id, &plan) {
            reflected = 0;
            records_changed = 0;
            records_conflicted = planned;
            alerts.push(format!("reflect transaction failed: {error}"));
        }
    }

    GroomResult {
        op: GroomOp::Reflect,
        records_archived: 0,
        records_merged: 0,
        records_reflected: reflected,
        records_selected,
        records_changed,
        records_skipped,
        records_conflicted,
        alerts,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn related_record(id: &str, owner: &str, workspace_id: &str) -> MemoryRecord {
        let mut record = MemoryRecord::new("amber telescope calibration");
        record.id = id.into();
        record.owner = Some(owner.into());
        record.workspace_id = workspace_id.into();
        record.confidence_score = 0.5;
        record.trust_score = 0.8;
        record
    }

    #[tokio::test]
    async fn reflect_scopes_both_targets_and_similarity_corpus() {
        let store = SqliteStore::memory(4).unwrap();
        for record in [
            related_record("alice-a-1", "alice", "ws-a"),
            related_record("alice-a-2", "alice", "ws-a"),
            related_record("alice-a-3", "alice", "ws-a"),
            {
                let mut record = MemoryRecord::new("unrelated solitary record");
                record.id = "alice-a-unrelated".into();
                record.owner = Some("alice".into());
                record.workspace_id = "ws-a".into();
                record
            },
            related_record("alice-b-1", "alice", "ws-b"),
            related_record("alice-b-2", "alice", "ws-b"),
            related_record("alice-b-3", "alice", "ws-b"),
            related_record("bob-a-1", "bob", "ws-a"),
            related_record("bob-a-2", "bob", "ws-a"),
            related_record("bob-a-3", "bob", "ws-a"),
        ] {
            assert!(store.upsert_curated(&record, None).await);
        }

        let dry = run_reflect(&store, "alice", "ws-a", true).await;
        assert_eq!(dry.records_reflected, 3);
        assert_eq!(dry.records_selected, 4);
        assert_eq!(dry.records_changed, 3);
        assert_eq!(dry.records_skipped, 1);
        assert_eq!(dry.records_conflicted, 0);
        assert_eq!(
            store
                .get_curated_record("alice-a-1", "alice", "ws-a")
                .unwrap()
                .unwrap()
                .confidence_score,
            0.5
        );

        store
            .conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER fail_reflect BEFORE UPDATE ON curated
                 BEGIN SELECT RAISE(ABORT, 'injected reflect failure'); END;",
            )
            .unwrap();
        let failed = run_reflect(&store, "alice", "ws-a", false).await;
        assert_eq!(failed.records_reflected, 0);
        assert_eq!(failed.records_selected, dry.records_selected);
        assert_eq!(failed.records_changed, 0);
        assert_eq!(failed.records_skipped, dry.records_skipped);
        assert_eq!(failed.records_conflicted, dry.records_changed);
        assert_eq!(
            store
                .get_curated_record("alice-a-1", "alice", "ws-a")
                .unwrap()
                .unwrap()
                .confidence_score,
            0.5
        );
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch("DROP TRIGGER fail_reflect;")
            .unwrap();

        let live = run_reflect(&store, "alice", "ws-a", false).await;
        assert_eq!(live.records_reflected, dry.records_reflected);
        assert_eq!(live.records_selected, dry.records_selected);
        assert_eq!(live.records_changed, dry.records_changed);
        assert_eq!(live.records_skipped, dry.records_skipped);
        assert_eq!(live.records_conflicted, 0);
        let reflected = store
            .get_curated_record("alice-a-1", "alice", "ws-a")
            .unwrap()
            .unwrap();
        assert!(reflected.confidence_score > 0.5);
        let reflected_at = reflected.updated_at;

        let retry = run_reflect(&store, "alice", "ws-a", false).await;
        assert_eq!(retry.records_reflected, 0);
        assert_eq!(retry.records_selected, 4);
        assert_eq!(retry.records_changed, 0);
        assert_eq!(retry.records_skipped, 4);
        assert_eq!(retry.records_conflicted, 0);
        let after_retry = store
            .get_curated_record("alice-a-1", "alice", "ws-a")
            .unwrap()
            .unwrap();
        assert_eq!(after_retry.confidence_score, reflected.confidence_score);
        assert_eq!(after_retry.updated_at, reflected_at);
        assert_eq!(
            store
                .get_curated_record("alice-b-1", "alice", "ws-b")
                .unwrap()
                .unwrap()
                .confidence_score,
            0.5
        );
        assert_eq!(
            store
                .get_curated_record("bob-a-1", "bob", "ws-a")
                .unwrap()
                .unwrap()
                .confidence_score,
            0.5
        );
    }
}
