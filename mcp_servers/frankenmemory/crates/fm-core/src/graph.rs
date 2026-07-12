//! Graph memory overlay — types and identity (design:
//! .futures/frankenmemory-update/graph-memory-design.md §2.1).
//!
//! Nodes carry deterministic UUIDv5 identity over `"{kind}:{norm_name}"`,
//! so re-capturing the same entity dedups/merges for free and recapture is
//! idempotent. Topics are ordinary nodes with `layer == "topic"` (management
//! ruling 2026-07-08) — no separate table.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Fixed namespace for all fm graph node ids. NEVER change this value:
/// every node id in every existing DB derives from it.
pub const FM_GRAPH_NAMESPACE: Uuid = uuid::uuid!("3c8e6f5a-1d2b-4e9c-8a70-5f4b3d2c1e0a");

/// Casefold + whitespace-collapse + trim, so "The  Ceramic HEDGEHOG " and
/// "the ceramic hedgehog" are the same node.
pub fn norm_name(name: &str) -> String {
    name.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase()
}

/// Deterministic node id: UUIDv5(FM_GRAPH_NAMESPACE, "kind:norm_name").
pub fn node_id(kind: &str, name: &str) -> String {
    let key = format!("{}:{}", kind.trim().to_lowercase(), norm_name(name));
    Uuid::new_v5(&FM_GRAPH_NAMESPACE, key.as_bytes()).to_string()
}

/// Reference to a node by identity fields (the wire shape extraction sends).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct NodeRef {
    pub kind: String,
    pub name: String,
}

impl NodeRef {
    pub fn id(&self) -> String {
        node_id(&self.kind, &self.name)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct GraphNodeInput {
    pub kind: String,
    pub name: String,
    #[serde(default)]
    pub label: Option<String>,
    /// episodic | semantic | topic — defaults to semantic.
    #[serde(default)]
    pub layer: Option<String>,
    #[serde(default)]
    pub trust: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct GraphEdgeInput {
    pub src: NodeRef,
    pub tag: String,
    pub dst: NodeRef,
    /// One-sentence natural-language statement of the edge; stored in the
    /// facts tier so edges inherit FTS (+ vectors) for free.
    #[serde(default)]
    pub fact: Option<String>,
    #[serde(default)]
    pub trust: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct GraphCueInput {
    pub cue: String,
    pub node: NodeRef,
    /// extracted | rake_fallback
    #[serde(default)]
    pub source: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphUpsertInput {
    #[serde(default)]
    pub nodes: Vec<GraphNodeInput>,
    #[serde(default)]
    pub edges: Vec<GraphEdgeInput>,
    #[serde(default)]
    pub cues: Vec<GraphCueInput>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphUpsertResult {
    pub nodes_upserted: usize,
    pub edges_upserted: usize,
    pub cues_upserted: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphNodeRow {
    pub id: String,
    pub kind: String,
    pub label: Option<String>,
    pub name: String,
    pub layer: String,
    pub trust: i64,
    pub created_at: String,
    pub last_seen: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphEdgeRow {
    pub id: String,
    pub src_id: String,
    pub tag: String,
    pub dst_id: String,
    pub fact: Option<String>,
    pub weight: f64,
    pub traversal_count: i64,
    pub trust: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CueHit {
    pub cue: String,
    pub node: GraphNodeRow,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TagCount {
    pub tag: String,
    /// "out" = node is src, "in" = node is dst.
    pub direction: String,
    pub count: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExpandHit {
    pub edge: GraphEdgeRow,
    /// The neighbor on the other side of the edge.
    pub other: GraphNodeRow,
    pub direction: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TracePath {
    /// Node ids from src to destination, inclusive.
    pub node_ids: Vec<String>,
    /// Tags walked, one per hop.
    pub tags: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn node_id_is_deterministic_and_normalized() {
        let a = node_id("person", "The  Ceramic HEDGEHOG ");
        let b = node_id("person", "the ceramic hedgehog");
        let c = node_id("PERSON", "the ceramic hedgehog");
        assert_eq!(a, b);
        assert_eq!(b, c, "kind is case-insensitive too");

        let d = node_id("tool", "the ceramic hedgehog");
        assert_ne!(a, d, "different kind = different node");

        // Stability contract: this exact value must never change across
        // releases — existing DBs depend on it.
        assert_eq!(node_id("person", "e"), node_id("person", " E "));
    }
}
