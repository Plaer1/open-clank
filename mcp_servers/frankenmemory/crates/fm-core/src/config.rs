use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FmConfig {
    pub db_path: String,
    pub embedding: EmbeddingConfig,
    pub recall: RecallConfig,
    pub collapse: CollapseConfig,
    pub decay: DecayConfig,
    pub providers: ProviderConfig,
    pub workspace_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbeddingConfig {
    pub api_base: String,
    pub model: String,
    pub dimensions: usize,
    pub timeout_ms: u64,
    pub cache_size: usize,
    /// Optional bearer token for cloud OpenAI-compatible endpoints (e.g.
    /// Gemini's compat layer). Local ollama needs none.
    pub api_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallConfig {
    pub default_mode: String,
    pub top_k: usize,
    pub score_threshold: f32,
    pub timeout_ms: u64,
    pub fts_score_floor: f32,
    pub workspace_boost: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CollapseConfig {
    pub budget: usize,
    pub prune_ratio: f64,
    pub dup_overlap: f64,
    pub overlap_weight: f64,
    pub rank_decay: f64,
    pub corroboration_overlap: f64,
    pub amplify_gain: f64,
    pub amplify_cap: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecayConfig {
    pub half_life_important_days: f64,
    pub half_life_normal_days: f64,
    pub importance_threshold: f32,
    pub exempt_importance_threshold: f32,
    pub decay_threshold: f64,
    pub confidence_alert_threshold: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderConfig {
    pub native_enabled: bool,
    pub memos_enabled: bool,
    pub tencent_enabled: bool,
}

impl Default for FmConfig {
    fn default() -> Self {
        let db_path = std::env::var("FM_DB_PATH")
            .unwrap_or_else(|_| "frankenmemory.db".to_string());

        Self {
            db_path,
            embedding: EmbeddingConfig::from_env(),
            recall: RecallConfig::default(),
            collapse: CollapseConfig::default(),
            decay: DecayConfig::default(),
            providers: ProviderConfig::default(),
            workspace_id: "global".into(),
        }
    }
}

impl Default for EmbeddingConfig {
    fn default() -> Self {
        // Local-first default (management ruling 2026-07-09): a keyless local
        // ollama endpoint is the only thing that can work with ZERO
        // configuration. Cloud endpoints (OpenAI, Gemini's OpenAI-compat
        // layer) are one FM_EMBED_API_BASE + FM_EMBED_API_KEY away.
        Self {
            api_base: "http://127.0.0.1:11434/v1".into(),
            model: "qwen3-embedding:8b".into(),
            dimensions: 4096,
            timeout_ms: 20000,
            cache_size: 256,
            api_key: None,
        }
    }
}

impl EmbeddingConfig {
    /// Defaults overridable via env — the embedding endpoint must be
    /// configurable (management ruling 2026-07-08): FM_EMBED_API_BASE,
    /// FM_EMBED_MODEL, FM_EMBED_DIMENSIONS, FM_EMBED_TIMEOUT_MS.
    /// NOTE: whether an HTTP client is used AT ALL is decided by the binary
    /// (fm-mcp uses HTTP only when FM_EMBED_API_BASE is set; otherwise the
    /// deterministic Noop embedder, the pre-E1 behavior).
    pub fn from_env() -> Self {
        let mut cfg = Self::default();
        if let Ok(v) = std::env::var("FM_EMBED_API_BASE") {
            if !v.trim().is_empty() {
                cfg.api_base = v;
            }
        }
        if let Ok(v) = std::env::var("FM_EMBED_MODEL") {
            if !v.trim().is_empty() {
                cfg.model = v;
            }
        }
        if let Ok(v) = std::env::var("FM_EMBED_DIMENSIONS") {
            if let Ok(n) = v.trim().parse::<usize>() {
                cfg.dimensions = n;
            }
        }
        if let Ok(v) = std::env::var("FM_EMBED_TIMEOUT_MS") {
            if let Ok(n) = v.trim().parse::<u64>() {
                cfg.timeout_ms = n;
            }
        }
        if let Ok(v) = std::env::var("FM_EMBED_API_KEY") {
            if !v.trim().is_empty() {
                cfg.api_key = Some(v);
            }
        }
        cfg
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedding_config_reads_env_overrides() {
        std::env::set_var("FM_EMBED_API_BASE", "http://127.0.0.1:11434/v1");
        std::env::set_var("FM_EMBED_MODEL", "qwen3-embedding:8b");
        std::env::set_var("FM_EMBED_DIMENSIONS", "4096");
        std::env::set_var("FM_EMBED_TIMEOUT_MS", "15000");

        let cfg = EmbeddingConfig::from_env();
        assert_eq!(cfg.api_base, "http://127.0.0.1:11434/v1");
        assert_eq!(cfg.model, "qwen3-embedding:8b");
        assert_eq!(cfg.dimensions, 4096);
        assert_eq!(cfg.timeout_ms, 15000);

        std::env::remove_var("FM_EMBED_API_BASE");
        std::env::remove_var("FM_EMBED_MODEL");
        std::env::remove_var("FM_EMBED_DIMENSIONS");
        std::env::remove_var("FM_EMBED_TIMEOUT_MS");

        let cfg = EmbeddingConfig::from_env();
        assert_eq!(cfg.api_base, "http://127.0.0.1:11434/v1", "local-first default");
        assert_eq!(cfg.dimensions, 4096);
    }
}

impl Default for RecallConfig {
    fn default() -> Self {
        Self {
            default_mode: "layer_a".into(),
            top_k: 10,
            score_threshold: 0.3,
            timeout_ms: 5000,
            fts_score_floor: 0.15,
            workspace_boost: 1.5,
        }
    }
}

impl Default for CollapseConfig {
    fn default() -> Self {
        Self {
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
}

impl Default for DecayConfig {
    fn default() -> Self {
        Self {
            half_life_important_days: 90.0,
            half_life_normal_days: 30.0,
            importance_threshold: 0.3,
            exempt_importance_threshold: 0.7,
            decay_threshold: 0.1,
            confidence_alert_threshold: 0.7,
        }
    }
}

impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            native_enabled: true,
            memos_enabled: false,
            tencent_enabled: false,
        }
    }
}
