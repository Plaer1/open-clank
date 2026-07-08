use crate::record::{GroundTruthRank, ScoredRecord};

pub fn tag_ground_truth(records: &mut [ScoredRecord]) {
    for r in records.iter_mut() {
        let rank = classify_record(&r.record);
        r.record.metadata = serde_json::json!({
            "ground_truth_rank": format!("{:?}", rank).to_lowercase(),
            "ground_truth_numeric": rank.numeric(),
        });
    }
}

fn classify_record(record: &crate::record::MemoryRecord) -> GroundTruthRank {
    // Terminal: user-created, high-trust, pinned
    if record.source_type == crate::record::SourceType::Human
        || record.exempt_from_decay
        || record.trust_score >= 0.8
    {
        return GroundTruthRank::Terminal;
    }

    // Injected: agent-explicit captures
    if record.source_type == crate::record::SourceType::Ai && record.trust_score >= 0.6 {
        return GroundTruthRank::Injected;
    }

    // Docs: procedural/system
    if record.source_type == crate::record::SourceType::Procedural {
        return GroundTruthRank::Docs;
    }

    // Training: everything else (auto-extracted)
    GroundTruthRank::Training
}

pub fn format_recall_output(
    records: &[ScoredRecord],
    query: &str,
) -> (String, Option<String>) {
    if records.is_empty() {
        return (String::new(), None);
    }

    let mut preamble = String::from("## Ground-Truth Memory Authority\n\n");
    preamble.push_str("Memory authority levels (highest to lowest):\n");
    preamble.push_str("1. **Terminal** — user-created, pinned, high-trust memories\n");
    preamble.push_str("2. **Injected** — agent-explicit captures with high confidence\n");
    preamble.push_str("3. **Docs** — procedural and system-generated knowledge\n");
    preamble.push_str("4. **Training** — auto-extracted, lower authority\n\n");
    preamble.push_str("When memories conflict, higher authority wins.\n\n");

    let mut context = format!("## Relevant Memories for: \"{query}\"\n\n");

    for (i, r) in records.iter().enumerate() {
        let rank_str = r
            .record
            .metadata
            .get("ground_truth_rank")
            .and_then(|v| v.as_str())
            .unwrap_or("training");

        let trust = if r.record.trust_score >= 0.8 {
            "HIGH-TRUST"
        } else if r.record.trust_score >= 0.5 {
            "MEDIUM-TRUST"
        } else {
            "LOW-TRUST"
        };

        context.push_str(&format!(
            "{}. [{}|{}] {} (score: {:.3}, trust: {})\n",
            i + 1,
            rank_str,
            format!("{:?}", r.record.kind).to_lowercase(),
            r.record.content,
            r.score,
            trust,
        ));
    }

    (context, Some(preamble))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::record::*;

    fn record_with_type(source_type: SourceType, trust: f32) -> MemoryRecord {
        let mut r = MemoryRecord::new("test");
        r.source_type = source_type;
        r.trust_score = trust;
        r
    }

    #[test]
    fn classify_human_as_terminal() {
        let r = record_with_type(SourceType::Human, 0.5);
        assert_eq!(classify_record(&r), GroundTruthRank::Terminal);
    }

    #[test]
    fn classify_high_trust_as_terminal() {
        let r = record_with_type(SourceType::Ai, 0.9);
        assert_eq!(classify_record(&r), GroundTruthRank::Terminal);
    }

    #[test]
    fn classify_ai_as_injected() {
        let r = record_with_type(SourceType::Ai, 0.7);
        assert_eq!(classify_record(&r), GroundTruthRank::Injected);
    }

    #[test]
    fn classify_procedural_as_docs() {
        let r = record_with_type(SourceType::Procedural, 0.4);
        assert_eq!(classify_record(&r), GroundTruthRank::Docs);
    }

    #[test]
    fn classify_auto_as_training() {
        let r = record_with_type(SourceType::AutoExtracted, 0.3);
        assert_eq!(classify_record(&r), GroundTruthRank::Training);
    }

    #[test]
    fn format_output_non_empty() {
        let mut r = ScoredRecord {
            record: MemoryRecord::new("test memory"),
            score: 0.85,
            source_label: "test".into(),
        };
        r.record.trust_score = 0.9;
        r.record.metadata = serde_json::json!({"ground_truth_rank": "terminal"});

        let (ctx, preamble) = format_recall_output(&[r], "test query");
        assert!(ctx.contains("test memory"));
        assert!(preamble.is_some());
        assert!(preamble.unwrap().contains("Ground-Truth"));
    }
}
