use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbeddingRequest {
    pub input: String,
    pub model: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbeddingResponse {
    pub data: Vec<EmbeddingData>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbeddingData {
    pub embedding: Vec<f32>,
}

#[async_trait]
pub trait EmbeddingClient: Send + Sync {
    async fn embed(&self, text: &str) -> Result<Vec<f32>, EmbedError>;
    fn dims(&self) -> usize;
}

#[derive(Debug, thiserror::Error)]
pub enum EmbedError {
    #[error("http error: {0}")]
    Http(String),
    #[error("dimension mismatch: expected {expected}, got {actual}")]
    DimMismatch { expected: usize, actual: usize },
    #[error("cache miss (internal)")]
    CacheMiss,
}

#[async_trait]
impl<T: EmbeddingClient + ?Sized> EmbeddingClient for std::sync::Arc<T> {
    async fn embed(&self, text: &str) -> Result<Vec<f32>, EmbedError> {
        (**self).embed(text).await
    }
    fn dims(&self) -> usize {
        (**self).dims()
    }
}

pub struct NoopEmbeddingClient {
    dims: usize,
}

impl NoopEmbeddingClient {
    pub fn new(dims: usize) -> Self {
        Self { dims }
    }
}

#[async_trait]
impl EmbeddingClient for NoopEmbeddingClient {
    async fn embed(&self, text: &str) -> Result<Vec<f32>, EmbedError> {
        // Deterministic hash-based embedding for testing
        let mut emb = vec![0.0f32; self.dims];
        let bytes = text.as_bytes();
        for (i, &b) in bytes.iter().enumerate() {
            emb[i % self.dims] += (b as f32) / 255.0;
        }
        let norm: f32 = emb.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm > 0.0 {
            for v in &mut emb {
                *v /= norm;
            }
        }
        Ok(emb)
    }

    fn dims(&self) -> usize {
        self.dims
    }
}

#[cfg(feature = "http-embed")]
pub struct HttpEmbeddingClient {
    client: reqwest::Client,
    api_base: String,
    model: String,
    dims: usize,
    cache: std::sync::Mutex<lru::LruCache<String, Vec<f32>>>,
}

#[cfg(feature = "http-embed")]
impl HttpEmbeddingClient {
    pub fn new(api_base: &str, model: &str, dims: usize, cache_size: usize) -> Self {
        Self {
            client: reqwest::Client::new(),
            api_base: api_base.trim_end_matches('/').to_string(),
            model: model.to_string(),
            dims,
            cache: std::sync::Mutex::new(lru::LruCache::new(
                std::num::NonZeroUsize::new(cache_size).unwrap(),
            )),
        }
    }
}

#[cfg(feature = "http-embed")]
#[async_trait]
impl EmbeddingClient for HttpEmbeddingClient {
    async fn embed(&self, text: &str) -> Result<Vec<f32>, EmbedError> {
        // Check cache
        {
            let mut cache = self.cache.lock().unwrap();
            if let Some(cached) = cache.get(text) {
                return Ok(cached.clone());
            }
        }

        let url = format!("{}/embeddings", self.api_base);
        let body = serde_json::json!({
            "input": text,
            "model": self.model,
            "dimensions": self.dims,
        });

        let resp = self
            .client
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(|e| EmbedError::Http(e.to_string()))?;

        let resp: EmbeddingResponse = resp
            .json()
            .await
            .map_err(|e| EmbedError::Http(e.to_string()))?;

        let embedding = resp
            .data
            .into_iter()
            .next()
            .ok_or_else(|| EmbedError::Http("no embedding data".into()))?
            .embedding;

        if embedding.len() != self.dims {
            return Err(EmbedError::DimMismatch {
                expected: self.dims,
                actual: embedding.len(),
            });
        }

        // Cache
        {
            let mut cache = self.cache.lock().unwrap();
            cache.put(text.to_string(), embedding.clone());
        }

        Ok(embedding)
    }

    fn dims(&self) -> usize {
        self.dims
    }
}

pub struct Bm25Encoder {
    doc_count: usize,
    doc_freqs: HashMap<String, usize>,
    avg_doc_len: f64,
    k1: f64,
    b: f64,
}

impl Bm25Encoder {
    pub fn new() -> Self {
        Self {
            doc_count: 0,
            doc_freqs: HashMap::new(),
            avg_doc_len: 0.0,
            k1: 1.2,
            b: 0.75,
        }
    }

    pub fn add_document(&mut self, text: &str) {
        let tokens = tokenize(text);
        let unique: std::collections::HashSet<&str> = tokens.iter().map(|s| s.as_str()).collect();
        for token in unique {
            *self.doc_freqs.entry(token.to_string()).or_insert(0) += 1;
        }
        self.doc_count += 1;
        let total_len: usize = self.doc_freqs.values().sum();
        if self.doc_count > 0 {
            self.avg_doc_len = total_len as f64 / self.doc_count as f64;
        }
    }

    pub fn encode(&self, text: &str) -> Vec<(u32, f32)> {
        let tokens = tokenize(text);
        let doc_len = tokens.len() as f64;
        let mut term_counts: HashMap<&str, u32> = HashMap::new();
        for t in &tokens {
            *term_counts.entry(t).or_insert(0) += 1;
        }

        let mut result = Vec::new();
        for (i, (term, &tf)) in term_counts.iter().enumerate() {
            let df = self.doc_freqs.get(*term).copied().unwrap_or(0) as f64;
            let n = self.doc_count.max(1) as f64;
            let idf = ((n - df + 0.5) / (df + 0.5) + 1.0).ln();
            let tf_norm = (tf as f64 * (self.k1 + 1.0))
                / (tf as f64 + self.k1 * (1.0 - self.b + self.b * doc_len / self.avg_doc_len.max(1.0)));
            let score = (idf * tf_norm) as f32;
            if score > 0.0 {
                result.push((i as u32, score));
            }
        }
        result
    }
}

impl Default for Bm25Encoder {
    fn default() -> Self {
        Self::new()
    }
}

const STOPWORDS: &[&str] = &[
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "of", "in", "to",
    "for", "with", "on", "at", "from", "by", "about", "as", "into",
    "through", "during", "before", "after", "above", "below", "between",
    "out", "off", "over", "under", "again", "further", "then", "once",
    "and", "but", "or", "nor", "not", "so", "very", "just", "than", "too",
    "it", "its", "this", "that", "these", "those", "i", "me", "my", "we",
    "our", "you", "your", "he", "him", "his", "she", "her", "they", "them",
    "their", "what", "which", "who", "whom",
];

fn tokenize(text: &str) -> Vec<String> {
    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric() && c != '_')
        .filter(|t| !t.is_empty() && !STOPWORDS.contains(t))
        .map(|t| t.to_string())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn noop_embed_produces_normalized() {
        let client = NoopEmbeddingClient::new(4);
        let emb = client.embed("hello world").await.unwrap();
        assert_eq!(emb.len(), 4);
        let norm: f32 = emb.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 0.001);
    }

    #[tokio::test]
    async fn noop_embed_deterministic() {
        let client = NoopEmbeddingClient::new(8);
        let a = client.embed("test input").await.unwrap();
        let b = client.embed("test input").await.unwrap();
        assert_eq!(a, b);
    }

    #[tokio::test]
    async fn noop_embed_different_inputs() {
        let client = NoopEmbeddingClient::new(8);
        let a = client.embed("hello").await.unwrap();
        let b = client.embed("world").await.unwrap();
        assert_ne!(a, b);
    }

    #[test]
    fn bm25_encoder_basic() {
        let mut enc = Bm25Encoder::new();
        enc.add_document("rust is a systems programming language");
        enc.add_document("python is a scripting language");
        enc.add_document("rust has a borrow checker");

        let scores = enc.encode("rust borrow checker");
        assert!(!scores.is_empty());
        // "rust" should have a score since it appears in 2/3 docs
        assert!(scores.iter().any(|(_, s)| *s > 0.0));
    }

    #[test]
    fn bm25_idf_effect() {
        let mut enc = Bm25Encoder::new();
        for _ in 0..100 {
            enc.add_document("common words here");
        }
        enc.add_document("rare unique term");

        let common_scores = enc.encode("common");
        let rare_scores = enc.encode("rare unique term");
        // Rare terms should score higher
        let common_max: f32 = common_scores.iter().map(|(_, s)| *s).sum();
        let rare_max: f32 = rare_scores.iter().map(|(_, s)| *s).sum();
        assert!(rare_max > common_max);
    }

    #[test]
    fn tokenize_strips_stopwords() {
        let tokens = tokenize("the quick brown fox is very fast");
        assert!(!tokens.contains(&"the".to_string()));
        assert!(!tokens.contains(&"is".to_string()));
        assert!(!tokens.contains(&"very".to_string()));
        assert!(tokens.contains(&"quick".to_string()));
        assert!(tokens.contains(&"fox".to_string()));
    }
}
