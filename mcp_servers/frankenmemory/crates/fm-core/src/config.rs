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
            embedding: EmbeddingConfig::default(),
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
        Self {
            api_base: "https://api.openai.com/v1".into(),
            model: "text-embedding-3-small".into(),
            dimensions: 1536,
            timeout_ms: 5000,
            cache_size: 256,
        }
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
