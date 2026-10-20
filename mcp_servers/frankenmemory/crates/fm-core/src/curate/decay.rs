use chrono::{DateTime, Utc};
use tracing::info;

use crate::config::DecayConfig;
use crate::record::*;
use crate::store::{
    sqlite::{CuratedMaintenancePlan, SqliteStore},
    MemoryStore,
};

pub async fn run_decay(
    store: &SqliteStore,
    config: &DecayConfig,
    owner: &str,
    workspace_id: &str,
    dry_run: bool,
) -> GroomResult {
    let mut alerts = Vec::new();
    let mut plan = CuratedMaintenancePlan::default();

    // Tenant predicates are applied in SQL before ORDER/LIMIT. Never fetch a
    // global corpus and filter it after another tenant has occupied the page.
    let all_records = store
        .search_curated_fts_scoped("", 1000, Some(owner), Some(workspace_id))
        .await;
    let records_selected = all_records.len();

    let now = Utc::now();

    for scored in &all_records {
        let r = &scored.record;

        // Skip already archived
        if r.archived || r.kind == MemoryKind::Unknown {
            continue;
        }

        // Source-type exemptions
        if r.source_type == SourceType::Human || r.source_type == SourceType::Procedural {
            continue;
        }

        // High importance exemption
        if r.importance_score >= config.exempt_importance_threshold {
            continue;
        }

        // Exempt from decay flag
        if r.exempt_from_decay {
            continue;
        }

        let decay_score = calculate_decay_score(
            r.last_accessed_at.as_deref().unwrap_or(&r.updated_at),
            r.importance_score,
            config,
        );

        if decay_score < config.decay_threshold {
            if r.confidence_score >= config.confidence_alert_threshold {
                alerts.push(format!(
                    "ALERT: record {} (confidence={:.2}) should be reviewed before archiving",
                    r.id, r.confidence_score
                ));
            } else {
                // Archive
                let mut updated = r.clone();
                updated.archived = true;
                updated.updated_at = now.to_rfc3339();
                plan.upserts.push(updated);
                info!(
                    "decay planned archive {} (decay_score={:.3}, dry_run={dry_run})",
                    r.id, decay_score
                );
            }
        }
    }

    let planned = plan.upserts.len();
    let records_skipped = records_selected.saturating_sub(planned);
    let mut archived = planned;
    let mut records_changed = planned;
    let mut records_conflicted = 0;
    if !dry_run && !plan.upserts.is_empty() {
        if let Err(error) = store.apply_curated_maintenance_plan(owner, workspace_id, &plan) {
            archived = 0;
            records_changed = 0;
            records_conflicted = planned;
            alerts.push(format!("decay transaction failed: {error}"));
        }
    }

    GroomResult {
        op: GroomOp::Decay,
        records_archived: archived,
        records_merged: 0,
        records_reflected: 0,
        records_selected,
        records_changed,
        records_skipped,
        records_conflicted,
        alerts,
    }
}

fn calculate_decay_score(
    last_accessed_at: &str,
    importance_score: f32,
    config: &DecayConfig,
) -> f64 {
    let last_accessed = match DateTime::parse_from_rfc3339(last_accessed_at) {
        Ok(dt) => dt.with_timezone(&Utc),
        Err(_) => return 1.0, // Invalid timestamp = no decay
    };

    let age_days = (Utc::now() - last_accessed).num_days().max(0) as f64;
    let half_life = if importance_score >= config.importance_threshold {
        config.half_life_important_days
    } else {
        config.half_life_normal_days
    };

    // decay = exp(-ln(2) * age / half_life) = 2^(-age/half_life)
    (-std::f64::consts::LN_2 * age_days / half_life).exp()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stale_record(id: &str, owner: &str, workspace_id: &str) -> MemoryRecord {
        let stale = (Utc::now() - chrono::Duration::days(1_000)).to_rfc3339();
        let mut record = MemoryRecord::new(format!("stale memory {id}"));
        record.id = id.into();
        record.owner = Some(owner.into());
        record.workspace_id = workspace_id.into();
        record.updated_at = stale.clone();
        record.last_accessed_at = Some(stale);
        record.importance_score = 0.1;
        record.confidence_score = 0.1;
        record
    }

    fn archived(store: &SqliteStore, id: &str) -> bool {
        store
            .conn
            .lock()
            .unwrap()
            .query_row(
                "SELECT archived FROM curated WHERE id=?1",
                rusqlite::params![id],
                |row| row.get::<_, bool>(0),
            )
            .unwrap()
    }

    #[tokio::test]
    async fn decay_is_owner_workspace_scoped_and_dry_run_is_truthful() {
        let store = SqliteStore::memory(4).unwrap();
        for record in [
            stale_record("alice-a", "alice", "ws-a"),
            stale_record("alice-global", "alice", "global"),
            {
                let mut record = stale_record("alice-exempt", "alice", "ws-a");
                record.exempt_from_decay = true;
                record
            },
            stale_record("alice-b", "alice", "ws-b"),
            stale_record("bob-a", "bob", "ws-a"),
        ] {
            assert!(store.upsert_curated(&record, None).await);
        }

        let dry = run_decay(&store, &DecayConfig::default(), "alice", "ws-a", true).await;
        assert_eq!(dry.records_archived, 2);
        assert_eq!(dry.records_selected, 3);
        assert_eq!(dry.records_changed, 2);
        assert_eq!(dry.records_skipped, 1);
        assert_eq!(dry.records_conflicted, 0);
        assert!(dry.alerts.is_empty());
        assert!(!archived(&store, "alice-a"));
        assert!(!archived(&store, "alice-global"));

        store
            .conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER fail_decay BEFORE UPDATE ON curated
                 BEGIN SELECT RAISE(ABORT, 'injected decay failure'); END;",
            )
            .unwrap();
        let failed = run_decay(&store, &DecayConfig::default(), "alice", "ws-a", false).await;
        assert_eq!(failed.records_archived, 0);
        assert_eq!(failed.records_selected, dry.records_selected);
        assert_eq!(failed.records_changed, 0);
        assert_eq!(failed.records_skipped, dry.records_skipped);
        assert_eq!(failed.records_conflicted, dry.records_changed);
        assert!(!archived(&store, "alice-a"));
        assert!(!archived(&store, "alice-global"));
        store
            .conn
            .lock()
            .unwrap()
            .execute_batch("DROP TRIGGER fail_decay;")
            .unwrap();

        let live = run_decay(&store, &DecayConfig::default(), "alice", "ws-a", false).await;
        assert_eq!(live.records_archived, dry.records_archived);
        assert_eq!(live.records_selected, dry.records_selected);
        assert_eq!(live.records_changed, dry.records_changed);
        assert_eq!(live.records_skipped, dry.records_skipped);
        assert_eq!(live.records_conflicted, 0);
        assert!(archived(&store, "alice-a"));
        assert!(archived(&store, "alice-global"));
        assert!(!archived(&store, "alice-exempt"));
        assert!(!archived(&store, "alice-b"));
        assert!(!archived(&store, "bob-a"));
    }

    #[test]
    fn decay_formula_basic() {
        let config = DecayConfig::default();
        // 0 days = no decay
        let score = calculate_decay_score_from_days(0, 0.5, &config);
        assert!((score - 1.0).abs() < 0.001);

        // 90 days with importance >= 0.3 => half-life 90 => score ~0.5
        let score = calculate_decay_score_from_days(90, 0.5, &config);
        assert!((score - 0.5).abs() < 0.01);

        // 30 days with importance < 0.3 => half-life 30 => score ~0.5
        let score = calculate_decay_score_from_days(30, 0.2, &config);
        assert!((score - 0.5).abs() < 0.01);
    }

    fn calculate_decay_score_from_days(
        age_days: i64,
        importance: f32,
        config: &DecayConfig,
    ) -> f64 {
        let half_life = if importance >= config.importance_threshold {
            config.half_life_important_days
        } else {
            config.half_life_normal_days
        };
        (-std::f64::consts::LN_2 * age_days as f64 / half_life).exp()
    }
}
