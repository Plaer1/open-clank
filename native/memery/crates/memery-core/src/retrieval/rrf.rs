use crate::record::ScoredRecord;

pub fn rrf_merge(
    list_a: Vec<ScoredRecord>,
    list_b: Vec<ScoredRecord>,
    k: f32,
) -> Vec<ScoredRecord> {
    let mut score_map: std::collections::HashMap<String, (ScoredRecord, f32)> =
        std::collections::HashMap::new();

    for (rank, r) in list_a.iter().enumerate() {
        let rrf_score = 1.0 / (k + rank as f32);
        let entry = score_map
            .entry(r.record.id.clone())
            .or_insert_with(|| (r.clone(), 0.0));
        entry.1 += rrf_score;
    }

    for (rank, r) in list_b.iter().enumerate() {
        let rrf_score = 1.0 / (k + rank as f32);
        let entry = score_map
            .entry(r.record.id.clone())
            .or_insert_with(|| (r.clone(), 0.0));
        entry.1 += rrf_score;
    }

    let mut merged: Vec<ScoredRecord> = score_map
        .into_values()
        .map(|(mut r, score)| {
            r.score = score;
            r.source_label = "rrf_merged".into();
            r
        })
        .collect();

    merged.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    merged
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::record::MemoryRecord;

    fn scored(id: &str, score: f32) -> ScoredRecord {
        let mut r = MemoryRecord::new(format!("content {id}"));
        r.id = id.to_string();
        ScoredRecord {
            record: r,
            score,
            source_label: "test".into(),
        }
    }

    #[test]
    fn rrf_basic_merge() {
        let a = vec![scored("1", 0.9), scored("2", 0.8)];
        let b = vec![scored("2", 0.95), scored("3", 0.7)];
        let merged = rrf_merge(a, b, 60.0);
        assert_eq!(merged.len(), 3);
        // "2" appears in both lists, should rank highest
        assert_eq!(merged[0].record.id, "2");
    }

    #[test]
    fn rrf_preserves_ordering() {
        let a = vec![scored("1", 0.9), scored("2", 0.5)];
        let b = vec![scored("3", 0.9), scored("4", 0.5)];
        let merged = rrf_merge(a, b, 60.0);
        // All items present
        assert_eq!(merged.len(), 4);
    }
}
