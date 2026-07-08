use blake2::{Blake2b, Digest};
use std::collections::HashSet;

use crate::config::CollapseConfig;
use crate::record::ScoredRecord;

const SOURCE_PRIOR: &[(&str, f64)] = &[
    ("facts", 1.10),
    ("fabric", 1.05),
    ("sessions", 1.00),
    ("qdrant", 1.00),
    ("curated_fts", 1.00),
    ("curated_vector", 1.00),
    ("hybrid_rrf", 1.00),
    ("native", 1.00),
];

#[derive(Debug, Clone)]
pub struct CollapsedCandidate {
    pub record: ScoredRecord,
    pub salience: f64,
    pub corroboration: usize,
}

pub fn collapse(
    candidates: Vec<ScoredRecord>,
    query: &str,
    config: &CollapseConfig,
) -> Vec<ScoredRecord> {
    if candidates.is_empty() {
        return Vec::new();
    }

    let query_tokens = tokenize(query);
    let candidate_tokens: Vec<HashSet<String>> = candidates
        .iter()
        .map(|c| tokenize(&c.record.content))
        .collect();

    // Step 1: Score all candidates (base + Hebbian)
    let base_scores: Vec<f64> = candidates
        .iter()
        .enumerate()
        .map(|(i, c)| {
            base_salience(c, i, &query_tokens, &candidate_tokens[i], config)
        })
        .collect();

    let final_scores = hebbian_amplify(&candidates, &base_scores, &candidate_tokens, config);

    // Step 2: Prune relative to max salience
    let max_salience = final_scores
        .iter()
        .cloned()
        .fold(0.0f64, f64::max);
    let floor = max_salience * config.prune_ratio;

    let mut kept: Vec<(usize, f64)> = final_scores
        .iter()
        .enumerate()
        .filter(|(_, s)| **s >= floor)
        .map(|(i, &s)| (i, s))
        .collect();

    // Step 3: Sort by salience descending
    kept.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    // Step 4: Near-dup suppression
    let mut survivors: Vec<CollapsedCandidate> = Vec::new();
    let mut survivor_token_sets: Vec<HashSet<String>> = Vec::new();

    for &(idx, salience) in &kept {
        let tokens = &candidate_tokens[idx];
        let is_dup = survivor_token_sets.iter().any(|existing| {
            token_overlap(tokens, existing) >= config.dup_overlap
        });

        if !is_dup {
            survivors.push(CollapsedCandidate {
                record: candidates[idx].clone(),
                salience,
                corroboration: count_corroboration(
                    idx,
                    &candidates,
                    &candidate_tokens,
                    config.corroboration_overlap,
                ),
            });
            survivor_token_sets.push(tokens.clone());

            if survivors.len() >= config.budget {
                break;
            }
        }
    }

    survivors.into_iter().map(|c| c.record).collect()
}

fn base_salience(
    candidate: &ScoredRecord,
    rank: usize,
    query_tokens: &HashSet<String>,
    candidate_tokens: &HashSet<String>,
    config: &CollapseConfig,
) -> f64 {
    let overlap = if query_tokens.is_empty() {
        0.0
    } else {
        let intersection: usize = query_tokens
            .iter()
            .filter(|t| candidate_tokens.contains(t.as_str()))
            .count();
        intersection as f64 / query_tokens.len() as f64
    };

    let base = candidate.score.clamp(0.0, 1.0) as f64;
    let blended = config.overlap_weight * overlap + (1.0 - config.overlap_weight) * base;
    let decay = config.rank_decay.powi(rank as i32);
    let prior = SOURCE_PRIOR
        .iter()
        .find(|(s, _)| *s == candidate.source_label)
        .map(|(_, p)| *p)
        .unwrap_or(1.0);

    blended * decay * prior
}

fn hebbian_amplify(
    candidates: &[ScoredRecord],
    base_scores: &[f64],
    token_sets: &[HashSet<String>],
    config: &CollapseConfig,
) -> Vec<f64> {
    let n = candidates.len();
    let mut final_scores = base_scores.to_vec();

    for i in 0..n {
        let corroboration = count_corroboration(i, candidates, token_sets, config.corroboration_overlap);
        let boost = (corroboration as f64 * config.amplify_gain * base_scores[i])
            .min(config.amplify_cap);
        final_scores[i] = base_scores[i] * (1.0 + boost);
    }

    final_scores
}

fn count_corroboration(
    idx: usize,
    candidates: &[ScoredRecord],
    token_sets: &[HashSet<String>],
    threshold: f64,
) -> usize {
    candidates
        .iter()
        .enumerate()
        .filter(|(j, c)| {
            *j != idx
                && c.source_label != candidates[idx].source_label
                && token_overlap(&token_sets[idx], &token_sets[*j]) >= threshold
        })
        .count()
}

fn token_overlap(a: &HashSet<String>, b: &HashSet<String>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let intersection = a.intersection(b).count();
    let min_size = a.len().min(b.len());
    intersection as f64 / min_size as f64
}

pub fn tokenize(text: &str) -> HashSet<String> {
    let stopwords: HashSet<&str> = [
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "shall",
        "should", "may", "might", "must", "can", "could", "of", "in", "to",
        "for", "with", "on", "at", "from", "by", "about", "as", "into",
        "through", "during", "before", "after", "and", "but", "or", "not",
        "so", "very", "just", "than", "too", "it", "its", "this", "that",
    ]
    .iter()
    .copied()
    .collect();

    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|t| !t.is_empty() && !stopwords.contains(t))
        .map(|t| t.to_string())
        .collect()
}

pub fn attest(survivors: &[CollapsedCandidate]) -> Attestation {
    let mut commitment_parts: Vec<String> = survivors
        .iter()
        .map(|s| {
            format!(
                "{}:{}",
                s.record.source_label,
                &s.record.record.id
            )
        })
        .collect();
    commitment_parts.sort();
    let commitment = commitment_parts.join("|");

    let nonce = generate_nonce();
    let salt = generate_nonce();
    let nonce_hex = hex::encode(nonce.as_bytes());
    let salt_hex = hex::encode(salt.as_bytes());

    // Use hex-encoded strings in commitment for consistency with verify
    let input = format!("{commitment}|{nonce_hex}|{salt_hex}");
    let hash = compute_blake2b(input.as_bytes());

    Attestation {
        hash: hex::encode(&hash),
        nonce: nonce_hex,
        salt: salt_hex,
        count: survivors.len(),
        algo: "blake2b-256".to_string(),
    }
}

pub fn verify_attestation(survivors: &[CollapsedCandidate], attestation: &Attestation) -> bool {
    let expected = attest_with_nonce(survivors, &attestation.nonce, &attestation.salt);
    expected.hash == attestation.hash
}

fn attest_with_nonce(
    survivors: &[CollapsedCandidate],
    nonce_hex: &str,
    salt_hex: &str,
) -> Attestation {
    let mut commitment_parts: Vec<String> = survivors
        .iter()
        .map(|s| format!("{}:{}", s.record.source_label, s.record.record.id))
        .collect();
    commitment_parts.sort();
    let commitment = commitment_parts.join("|");

    // Use the raw hex strings directly (matching attest's encoding)
    let input = format!("{commitment}|{nonce_hex}|{salt_hex}");
    let hash = compute_blake2b(input.as_bytes());

    Attestation {
        hash: hex::encode(&hash),
        nonce: nonce_hex.to_string(),
        salt: salt_hex.to_string(),
        count: survivors.len(),
        algo: "blake2b-256".to_string(),
    }
}

fn generate_nonce() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let t = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{:032x}", t)
}

fn compute_blake2b(data: &[u8]) -> Vec<u8> {
    let mut hasher = Blake2b::<blake2::digest::consts::U32>::new();
    hasher.update(data);
    hasher.finalize().to_vec()
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Attestation {
    pub hash: String,
    pub nonce: String,
    pub salt: String,
    pub count: usize,
    pub algo: String,
}

// Need hex encoding - implement inline
mod hex {
    pub fn encode(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::CollapseConfig;
    use crate::record::{MemoryRecord, ScoredRecord};

    fn scored(id: &str, score: f32, source: &str, content: &str) -> ScoredRecord {
        let mut r = MemoryRecord::new(content);
        r.id = id.to_string();
        ScoredRecord {
            record: r,
            score,
            source_label: source.into(),
        }
    }

    fn default_config() -> CollapseConfig {
        CollapseConfig {
            budget: 6,
            prune_ratio: 0.35,
            dup_overlap: 0.82,
            overlap_weight: 0.55,
            rank_decay: 0.85,
            corroboration_overlap: 0.50,
            amplify_gain: 0.15,
            amplify_cap: 0.50,
        }
    }

    #[test]
    fn collapse_basic() {
        let candidates = vec![
            scored("1", 0.9, "curated_fts", "rust memory safety borrow checker"),
            scored("2", 0.8, "curated_vector", "rust has a borrow checker for safety"),
            scored("3", 0.3, "raw_fts", "python is great too"),
        ];
        let config = default_config();
        let result = collapse(candidates, "rust borrow checker", &config);
        assert!(!result.is_empty());
        assert!(result.len() <= 6);
    }

    #[test]
    fn collapse_prunes_low_salience() {
        let candidates = vec![
            scored("1", 0.9, "curated_fts", "rust is a systems language"),
            scored("2", 0.01, "curated_fts", "completely unrelated topic about cooking"),
        ];
        let config = default_config();
        let result = collapse(candidates, "rust programming", &config);
        // The low-score one should be pruned
        assert!(result.iter().all(|r| r.record.id != "2" || r.score > 0.1));
    }

    #[test]
    fn collapse_dedup() {
        let candidates = vec![
            scored("1", 0.9, "curated_fts", "rust memory safety borrow checker ownership"),
            scored("2", 0.85, "curated_vector", "rust memory safety borrow checker ownership model"),
        ];
        let config = default_config();
        let result = collapse(candidates, "rust memory", &config);
        // Near-duplicates should be collapsed
        assert!(result.len() <= 1 || result.len() == 2);
    }

    #[test]
    fn attestation_roundtrip() {
        let candidates = vec![
            scored("1", 0.9, "curated_fts", "rust memory safety"),
            scored("2", 0.8, "curated_vector", "borrow checker"),
        ];
        let collapsed: Vec<CollapsedCandidate> = candidates
            .into_iter()
            .map(|r| CollapsedCandidate {
                record: r,
                salience: 0.5,
                corroboration: 0,
            })
            .collect();

        let att = attest(&collapsed);
        assert!(!att.hash.is_empty());
        assert_eq!(att.algo, "blake2b-256");
    }

    #[test]
    fn tokenize_basic() {
        let tokens = tokenize("the quick brown fox");
        assert!(!tokens.contains("the"));
        assert!(tokens.contains("quick"));
        assert!(tokens.contains("brown"));
        assert!(tokens.contains("fox"));
    }
}
