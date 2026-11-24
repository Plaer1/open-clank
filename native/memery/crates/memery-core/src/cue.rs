//! RAKE-style cue extraction — the pure-Rust fallback that keeps the graph
//! tier non-empty when no LLM extraction is available (design §2.3,
//! "degraded pure mode"). Zero dependencies, deterministic.

use std::collections::HashMap;

const STOPWORDS: &[&str] = &[
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "while", "for", "to", "of",
    "in", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "being", "it", "its", "this", "that", "these", "those", "i", "you", "he", "she", "we", "they",
    "them", "his", "her", "their", "my", "your", "our", "me", "him", "us", "do", "does", "did",
    "done", "will", "would", "can", "could", "should", "shall", "may", "might", "must", "have",
    "has", "had", "not", "no", "yes", "so", "too", "very", "just", "also", "about", "into", "over",
    "under", "again", "there", "here", "what", "which", "who", "whom", "how", "why", "where",
    "all", "any", "both", "each", "more", "most", "some", "such", "only", "own", "same", "than",
    "up", "down", "out", "off", "please", "remember", "reply", "sentence", "short", "exactly",
    "one", "verbatim", "token",
];

fn is_stopword(word: &str) -> bool {
    STOPWORDS.contains(&word)
}

/// Extract up to `max` candidate cue phrases from free text.
///
/// Classic RAKE shape: split into phrases at stopwords/punctuation, score
/// each word by degree (co-occurrence within phrases) over frequency, rank
/// phrases by the sum of their word scores. Ties break lexicographically so
/// output is fully deterministic.
pub fn rake_cues(text: &str, max: usize) -> Vec<String> {
    let lowered = text.to_lowercase();
    let mut phrases: Vec<Vec<&str>> = Vec::new();
    let mut current: Vec<&str> = Vec::new();

    for raw in lowered.split(|c: char| !c.is_alphanumeric() && c != '-' && c != '_') {
        let word = raw.trim_matches('-');
        if word.is_empty() || is_stopword(word) || word.chars().all(|c| c.is_numeric()) {
            if !current.is_empty() {
                phrases.push(std::mem::take(&mut current));
            }
            continue;
        }
        current.push(word);
        if current.len() >= 4 {
            phrases.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        phrases.push(current);
    }

    let mut degree: HashMap<&str, f64> = HashMap::new();
    let mut freq: HashMap<&str, f64> = HashMap::new();
    for phrase in &phrases {
        for &word in phrase {
            *degree.entry(word).or_default() += (phrase.len() - 1) as f64;
            *freq.entry(word).or_default() += 1.0;
        }
    }

    let mut scored: Vec<(f64, String)> = phrases
        .iter()
        .map(|phrase| {
            let score: f64 = phrase.iter().map(|w| (degree[w] + freq[w]) / freq[w]).sum();
            (score, phrase.join(" "))
        })
        .collect();

    scored.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.1.cmp(&b.1))
    });
    let mut seen = std::collections::HashSet::new();
    scored
        .into_iter()
        .filter_map(|(_, p)| {
            if seen.insert(p.clone()) {
                Some(p)
            } else {
                None
            }
        })
        .filter(|p| p.len() > 2)
        .take(max)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_multiword_phrases_over_stopwords() {
        let cues = rake_cues(
            "Elena maintains the telescope-scheduler project and the project uses the astropy library",
            5,
        );
        assert!(!cues.is_empty());
        assert!(cues.iter().any(|c| c.contains("telescope-scheduler")));
        assert!(cues.iter().all(|c| !c.contains(" the ")));
    }

    #[test]
    fn deterministic_and_bounded() {
        let text = "the quick brown fox jumps over the lazy dog while the fox keeps jumping";
        let a = rake_cues(text, 3);
        let b = rake_cues(text, 3);
        assert_eq!(a, b);
        assert!(a.len() <= 3);
    }

    #[test]
    fn empty_and_stopword_only_texts_yield_nothing() {
        assert!(rake_cues("", 5).is_empty());
        assert!(rake_cues("the and or but if", 5).is_empty());
    }
}
