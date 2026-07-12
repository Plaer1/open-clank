use chrono::Utc;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryKind {
    Persona,
    Episodic,
    Instruction,
    Fact,
    Fabric,
    Wiki,
    Raw,
}

impl Default for MemoryKind {
    fn default() -> Self {
        Self::Episodic
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceType {
    Human,
    Procedural,
    Ai,
    AutoExtracted,
}

impl Default for SourceType {
    fn default() -> Self {
        Self::AutoExtracted
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecallMode {
    LayerA,
    LayerB,
}

impl Default for RecallMode {
    fn default() -> Self {
        Self::LayerA
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Tier {
    Curated,
    Raw,
}

impl Default for Tier {
    fn default() -> Self {
        Self::Curated
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GroundTruthRank {
    Terminal,
    Injected,
    Docs,
    Training,
}

impl GroundTruthRank {
    pub fn numeric(&self) -> u8 {
        match self {
            Self::Terminal => 1,
            Self::Injected => 2,
            Self::Docs => 3,
            Self::Training => 4,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryRecord {
    pub id: String,
    pub content: String,
    pub kind: MemoryKind,
    pub priority: i32,
    pub trust_score: f32,
    pub confidence_score: f32,
    pub importance_score: f32,
    pub scene_name: Option<String>,
    pub source: String,
    pub source_type: SourceType,
    pub owner: Option<String>,
    pub workspace_id: String,
    pub workspace_path: Option<String>,
    pub session_key: String,
    pub session_id: String,
    pub tags: Vec<String>,
    pub source_message_ids: Vec<String>,
    pub timestamps: Vec<String>,
    pub created_at: String,
    pub updated_at: String,
    pub archived: bool,
    pub last_accessed_at: Option<String>,
    pub exempt_from_decay: bool,
    pub exempt_from_dedup: bool,
    pub metadata: serde_json::Value,
}

impl MemoryRecord {
    pub fn new(content: impl Into<String>) -> Self {
        let now = Utc::now().to_rfc3339();
        Self {
            id: generate_id(),
            content: content.into(),
            kind: MemoryKind::default(),
            priority: 50,
            trust_score: 0.50,
            confidence_score: 0.6,
            importance_score: 0.5,
            scene_name: None,
            source: String::new(),
            source_type: SourceType::default(),
            owner: None,
            workspace_id: "global".into(),
            workspace_path: None,
            session_key: String::new(),
            session_id: String::new(),
            tags: Vec::new(),
            source_message_ids: Vec::new(),
            timestamps: Vec::new(),
            created_at: now.clone(),
            updated_at: now,
            archived: false,
            last_accessed_at: None,
            exempt_from_decay: false,
            exempt_from_dedup: false,
            metadata: serde_json::Value::Null,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RawTurn {
    pub id: String,
    pub role: String,
    pub content: String,
    pub session_key: String,
    pub session_id: String,
    pub workspace_id: String,
    pub owner: Option<String>,
    pub workspace_path: Option<String>,
    pub recorded_at: String,
    pub metadata: serde_json::Value,
}

impl RawTurn {
    pub fn new(role: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            id: generate_id(),
            role: role.into(),
            content: content.into(),
            session_key: String::new(),
            session_id: String::new(),
            workspace_id: "global".into(),
            owner: None,
            workspace_path: None,
            recorded_at: Utc::now().to_rfc3339(),
            metadata: serde_json::Value::Null,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompletedTurn {
    pub user_text: String,
    pub assistant_text: String,
    pub session_key: String,
    pub session_id: String,
    pub workspace_id: String,
    pub workspace_path: Option<String>,
    pub source: String,
    pub owner: Option<String>,
    pub category: Option<String>,
    pub metadata: serde_json::Value,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateStatus {
    Pending,
    Accepted,
    Rejected,
    Quarantined,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidateRecord {
    pub id: String,
    pub content: String,
    pub kind: MemoryKind,
    pub confidence_score: f32,
    pub importance_score: f32,
    pub owner: String,
    pub workspace_id: String,
    pub workspace_path: Option<String>,
    pub session_id: String,
    pub turn_id: String,
    pub raw_evidence_ids: Vec<String>,
    pub evidence_role: String,
    pub source: String,
    pub source_event_id: String,
    pub dedup_key: String,
    pub status: CandidateStatus,
    pub reason: String,
    pub accepted_curated_id: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallQuery {
    pub query: String,
    pub mode: RecallMode,
    pub workspace_id: Option<String>,
    pub top_k: usize,
    pub tier: Tier,
    pub owner: Option<String>,
    pub router: bool,
    pub rerank: bool,
}

impl Default for RecallQuery {
    fn default() -> Self {
        Self {
            query: String::new(),
            mode: RecallMode::default(),
            workspace_id: None,
            top_k: 10,
            tier: Tier::default(),
            owner: None,
            router: false,
            rerank: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchParams {
    pub query: String,
    pub kind: Option<MemoryKind>,
    pub scene: Option<String>,
    pub tier: Tier,
    pub limit: usize,
    pub workspace_id: Option<String>,
    pub owner: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureResult {
    pub records_captured: usize,
    #[serde(default)]
    pub record_ids: Vec<String>,
    pub vectors_written: usize,
    pub providers_succeeded: usize,
    pub providers_failed: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScoredRecord {
    pub record: MemoryRecord,
    pub score: f32,
    pub source_label: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScoredRaw {
    pub turn: RawTurn,
    pub score: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallResult {
    pub prepend_context: String,
    pub append_system_context: String,
    pub memories: Vec<ScoredRecord>,
    pub recall_strategy: String,
    pub ground_truth_preamble: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub results: Vec<ScoredRecord>,
    pub total: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GroomOp {
    Decay,
    Dedup,
    Reflect,
    /// Graph tier: decay edge weights by age (traversal-boosted), prune dead edges.
    EdgeDecay,
    /// Graph tier: merge near-duplicate edge tags into the canonical vocabulary.
    TagNormalize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroomResult {
    pub op: GroomOp,
    pub records_archived: usize,
    pub records_merged: usize,
    pub records_reflected: usize,
    pub alerts: Vec<String>,
}

pub fn generate_id() -> String {
    let epoch_ms = Utc::now().timestamp_millis();
    let rand_hex = format!("{:08x}", rand_u32());
    format!("m_{epoch_ms}_{rand_hex}")
}

fn rand_u32() -> u32 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut h = DefaultHasher::new();
    Utc::now().timestamp_nanos_opt().unwrap_or(0).hash(&mut h);
    std::thread::current().id().hash(&mut h);
    h.finish() as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_roundtrip_serde() {
        let r = MemoryRecord::new("hello world");
        let json = serde_json::to_string(&r).unwrap();
        let r2: MemoryRecord = serde_json::from_str(&json).unwrap();
        assert_eq!(r.id, r2.id);
        assert_eq!(r.content, r2.content);
        assert_eq!(r.workspace_id, "global");
    }

    #[test]
    fn id_format() {
        let id = generate_id();
        assert!(id.starts_with("m_"));
        let parts: Vec<&str> = id.split('_').collect();
        assert_eq!(parts.len(), 3);
        assert!(parts[1].parse::<i64>().is_ok());
        assert_eq!(parts[2].len(), 8);
    }

    #[test]
    fn defaults() {
        let r = MemoryRecord::new("test");
        assert_eq!(r.kind, MemoryKind::Episodic);
        assert_eq!(r.source_type, SourceType::AutoExtracted);
        assert_eq!(r.trust_score, 0.50);
        assert_eq!(r.confidence_score, 0.6);
        assert_eq!(r.importance_score, 0.5);
        assert!(!r.archived);
        assert_eq!(r.workspace_id, "global");
    }

    #[test]
    fn ground_truth_rank_ordering() {
        assert!(GroundTruthRank::Terminal.numeric() < GroundTruthRank::Injected.numeric());
        assert!(GroundTruthRank::Injected.numeric() < GroundTruthRank::Docs.numeric());
        assert!(GroundTruthRank::Docs.numeric() < GroundTruthRank::Training.numeric());
    }
}
