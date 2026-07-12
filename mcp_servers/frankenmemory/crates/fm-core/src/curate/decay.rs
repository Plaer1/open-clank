use chrono::{DateTime, Utc};
use tracing::info;

use crate::config::DecayConfig;
use crate::record::*;
use crate::store::MemoryStore;

pub async fn run_decay(
    store: &dyn MemoryStore,
    config: &DecayConfig,
    workspace_id: Option<&str>,
) -> GroomResult {
    let mut alerts = Vec::new();
    let mut archived = 0usize;

    // Search for all curated records (via FTS with empty-ish query to get all)
    let all_records = store.search_curated_fts("", 1000).await;

    let now = Utc::now();

    for scored in &all_records {
        let r = &scored.record;

        // Skip already archived
        if r.archived {
            continue;
        }

        // Workspace filter
        if let Some(ws) = workspace_id {
            if r.workspace_id != ws && r.workspace_id != "global" {
                continue;
            }
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
                store.upsert_curated(&updated, None).await;
                archived += 1;
                info!("archived record {} (decay_score={:.3})", r.id, decay_score);
            }
        }
    }

    GroomResult {
        op: GroomOp::Decay,
        records_archived: archived,
        records_merged: 0,
        records_reflected: 0,
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
