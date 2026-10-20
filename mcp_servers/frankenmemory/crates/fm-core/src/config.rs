use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FmConfig {
    pub db_path: String,
    pub embedding: EmbeddingConfig,
    pub recall: RecallConfig,
    pub collapse: CollapseConfig,
    pub decay: DecayConfig,
    pub code_index: CodeIndexConfig,
    pub providers: ProviderConfig,
    pub workspace_id: String,
}

/// Controls how long an owner/repository code-index run may remain leased
/// before a later request can classify it as abandoned. This is a recovery
/// lease, not an indexing timeout. Configure it above the expected
/// uninterrupted run duration for large repositories so a later request does
/// not classify the active run as abandoned.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodeIndexConfig {
    pub lease_seconds: u64,
}

impl CodeIndexConfig {
    pub const DEFAULT_LEASE_SECONDS: u64 = 99_999;
    pub const MIN_LEASE_SECONDS: u64 = 60;
    pub const MAX_LEASE_SECONDS: u64 = 99_999;

    pub fn from_env() -> Self {
        let requested = std::env::var("FM_CODE_INDEX_LEASE_SECONDS")
            .ok()
            .and_then(|value| value.trim().parse::<u64>().ok())
            .unwrap_or(Self::DEFAULT_LEASE_SECONDS);
        Self {
            lease_seconds: requested.clamp(Self::MIN_LEASE_SECONDS, Self::MAX_LEASE_SECONDS),
        }
    }
}

impl Default for CodeIndexConfig {
    fn default() -> Self {
        Self::from_env()
    }
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
        let db_path = std::env::var("FM_DB_PATH").unwrap_or_else(|_| {
            let data = std::env::var("XDG_DATA_HOME")
                .ok()
                .filter(|v| !v.trim().is_empty())
                .or_else(|| {
                    std::env::var("HOME")
                        .ok()
                        .filter(|v| !v.trim().is_empty())
                        .map(|home| format!("{home}/.local/share"))
                });
            format!(
                "{}/open-clank/frankenmemory.db",
                data.unwrap_or_else(|| "/tmp".to_string())
            )
        });

        Self {
            db_path,
            embedding: EmbeddingConfig::from_env(),
            recall: RecallConfig::default(),
            collapse: CollapseConfig::default(),
            decay: DecayConfig::default(),
            code_index: CodeIndexConfig::default(),
            providers: ProviderConfig::default(),
            workspace_id: "global".into(),
        }
    }
}

impl Default for EmbeddingConfig {
    fn default() -> Self {
        // Retained only for backward-compatible store shape.  Curated-memory
        // vectors fail closed until their schema can record immutable managed
        // route/adapter/dimension generations.
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
    /// Provider-bearing environment overrides are retired.  Open Clank's
    /// normalized provider repository and managed operation router are the
    /// sole embedding authority.
    pub fn from_env() -> Self {
        Self::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedding_config_has_no_provider_environment_authority() {
        let cfg = EmbeddingConfig::from_env();
        assert_eq!(cfg.api_base, "http://127.0.0.1:11434/v1");
        assert_eq!(cfg.model, "qwen3-embedding:8b");
        assert_eq!(cfg.dimensions, 4096);
        assert_eq!(cfg.timeout_ms, 20000);
        assert!(cfg.api_key.is_none());
    }

    #[test]
    fn code_index_lease_has_large_repo_safe_defaults_and_bounds() {
        let cfg = CodeIndexConfig::default();
        assert_eq!(cfg.lease_seconds, CodeIndexConfig::DEFAULT_LEASE_SECONDS);
        assert!(cfg.lease_seconds > 5 * 60);
        assert_eq!(CodeIndexConfig::DEFAULT_LEASE_SECONDS, 99_999);
        assert_eq!(CodeIndexConfig::MAX_LEASE_SECONDS, 99_999);
        assert!(CodeIndexConfig::MIN_LEASE_SECONDS >= 60);
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
