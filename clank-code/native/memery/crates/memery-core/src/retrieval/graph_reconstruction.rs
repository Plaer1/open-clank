use std::collections::{BTreeMap, BTreeSet, VecDeque};

use rusqlite::{params, Connection, Error, TransactionBehavior};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::graph::{norm_name, GraphScope};
use crate::store::graph::{
    graph_cues_in, retain_graph_cue_evidence, withheld_graph_edges_mode, withheld_graph_nodes_mode,
    GraphReadMode,
};
use crate::store::sqlite::SqliteStore;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Intent {
    General,
    Temporal,
    Causal,
    Entity,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum CommunityMode {
    Off,
    #[default]
    Focal,
    Broad,
}

fn enabled() -> bool {
    true
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ReconstructionRequest {
    pub query: Option<String>,
    #[serde(default)]
    pub seed_ids: Vec<String>,
    pub intent: Intent,
    pub tag: Option<String>,
    #[serde(default)]
    pub community: CommunityMode,
    #[serde(default = "enabled")]
    pub adaptive: bool,
    pub max_steps: usize,
    pub max_nodes: usize,
    pub max_edges: usize,
    pub per_kind: usize,
}

impl ReconstructionRequest {
    pub fn validate(&self) -> Result<(), String> {
        let valid_text = |value: &str, max: usize| {
            !value.trim().is_empty() && value.len() <= max && !value.chars().any(char::is_control)
        };
        if self
            .query
            .as_ref()
            .is_some_and(|query| !valid_text(query, 1024))
            || self.tag.as_ref().is_some_and(|tag| !valid_text(tag, 128))
            || (self.query.is_none()
                && self.seed_ids.is_empty()
                && self.community != CommunityMode::Broad)
            || self.seed_ids.len() > 16
            || self
                .seed_ids
                .iter()
                .any(|id| uuid::Uuid::parse_str(id).is_err())
            || self.seed_ids.iter().collect::<BTreeSet<_>>().len() != self.seed_ids.len()
            || !(1..=6).contains(&self.max_steps)
            || !(1..=96).contains(&self.max_nodes)
            || !(1..=384).contains(&self.max_edges)
            || !(1..=32).contains(&self.per_kind)
        {
            return Err("invalid graph reconstruction request".into());
        }
        Ok(())
    }
}

#[derive(Debug, Serialize)]
pub struct EvidenceLane {
    pub lane: &'static str,
    pub rank: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct EdgeWitness {
    pub edge_id: String,
    pub from: String,
    pub to: String,
    pub tag: String,
    pub fact_id: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct RankedNode {
    pub id: String,
    pub kind: String,
    pub score: f64,
    pub lanes: Vec<EvidenceLane>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witness: Option<EdgeWitness>,
}

#[derive(Debug, Serialize)]
pub struct ReconstructionStep {
    pub step: usize,
    pub frontier: Vec<String>,
    pub discovered: Vec<String>,
    pub new_witnesses: usize,
    pub new_intent_witnesses: usize,
    pub new_evidence_kinds: usize,
}

#[derive(Debug, Serialize)]
pub struct ReverseCue {
    pub node_id: String,
    pub cue: String,
}

#[derive(Debug, Serialize)]
pub struct ReconstructionResult {
    pub version: u8,
    pub nodes: Vec<RankedNode>,
    pub steps: Vec<ReconstructionStep>,
    pub reverse_cues: Vec<ReverseCue>,
    pub omitted: Vec<&'static str>,
    pub stop_reason: &'static str,
    pub scanned_edges: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub communities: Option<CommunityEvidence>,
}

#[derive(Debug, Serialize)]
pub struct CommunityRegion {
    pub id: String,
    pub members: Vec<String>,
    pub anchor_ids: Vec<String>,
    pub witnesses: Vec<EdgeWitness>,
}

#[derive(Debug, Serialize)]
pub struct CommunityEvidence {
    pub mode: CommunityMode,
    pub algorithm: &'static str,
    pub iterations: usize,
    pub converged: bool,
    pub groups: Vec<CommunityRegion>,
}

#[derive(Default)]
struct Candidate {
    kind: String,
    lanes: Vec<EvidenceLane>,
    graph_score: f64,
    witness: Option<EdgeWitness>,
}

fn edge_weight(intent: Intent, tag: &str) -> f64 {
    match (intent, tag) {
        (Intent::Temporal, "before" | "after" | "during" | "supersedes" | "follows")
        | (Intent::Causal, "causes" | "because" | "depends_on" | "enables" | "blocks")
        | (Intent::Entity, "about" | "mentions" | "related_to" | "member_of" | "part_of") => 2.0,
        _ => 1.0,
    }
}

fn load_edges(
    conn: &Connection,
    scope: &GraphScope,
    node_id: &str,
    tag: Option<&str>,
    limit: usize,
) -> rusqlite::Result<Vec<(EdgeWitness, String, String)>> {
    let mut stmt = conn.prepare_cached(
        "SELECT e.id,e.src_id,e.dst_id,e.tag,e.fact_id,n.id,n.kind FROM graph_edges e
         JOIN graph_nodes n ON n.id=CASE WHEN e.src_id=?1 THEN e.dst_id ELSE e.src_id END
         WHERE (e.src_id=?1 OR e.dst_id=?1) AND e.owner=?2 AND e.status='active'
           AND (e.workspace_id=?3 OR (?4 AND e.workspace_id='global'))
           AND n.owner=?2 AND n.status='active'
           AND (n.workspace_id=?3 OR (?4 AND n.workspace_id='global'))
           AND (?5 IS NULL OR e.tag=?5) AND length(CAST(e.tag AS BLOB))<=128
           AND length(CAST(n.kind AS BLOB))<=128
         ORDER BY e.id LIMIT ?6",
    )?;
    let rows = stmt
        .query_map(
            params![
                node_id,
                scope.owner,
                scope.workspace_id,
                scope.include_global,
                tag,
                limit
            ],
            |row| {
                Ok((
                    EdgeWitness {
                        edge_id: row.get(0)?,
                        from: row.get(1)?,
                        to: row.get(2)?,
                        tag: row.get(3)?,
                        fact_id: row.get(4)?,
                    },
                    row.get(5)?,
                    row.get(6)?,
                ))
            },
        )?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(rows)
}

fn reverse_cues(
    conn: &Connection,
    scope: &GraphScope,
    ids: &[String],
    omitted: &mut BTreeSet<&'static str>,
) -> rusqlite::Result<Vec<ReverseCue>> {
    let mut stmt = conn.prepare_cached(
        "SELECT cue FROM graph_cues WHERE node_id=?1 AND owner=?2 AND status='active'
         AND (workspace_id=?3 OR (?4 AND workspace_id='global'))
         AND length(CAST(cue AS BLOB))<=256 ORDER BY cue LIMIT 5",
    )?;
    let mut pairs = Vec::new();
    for id in ids {
        let mut rows = stmt
            .query_map(
                params![id, scope.owner, scope.workspace_id, scope.include_global],
                |row| row.get::<_, String>(0),
            )?
            .collect::<Result<Vec<_>, _>>()?;
        if rows.len() > 4 {
            omitted.insert("reverse_cue_limit");
            rows.truncate(4);
        }
        pairs.extend(rows.into_iter().map(|cue| (id.clone(), cue)));
    }
    retain_graph_cue_evidence(conn, scope, &mut pairs)?;
    Ok(pairs
        .into_iter()
        .map(|(node_id, cue)| ReverseCue { node_id, cue })
        .collect())
}

fn broad_evidence(
    conn: &Connection,
    scope: &GraphScope,
    local: &BTreeMap<String, Candidate>,
    tag: Option<&str>,
    node_room: usize,
    edge_room: usize,
    omitted: &mut BTreeSet<&'static str>,
) -> rusqlite::Result<Vec<EdgeWitness>> {
    if node_room == 0 || edge_room == 0 {
        omitted.insert("broad_sample_budget");
        return Ok(Vec::new());
    }
    let keys: Vec<&str> = local.keys().map(String::as_str).collect();
    let occupied = serde_json::to_string(&keys)
        .map_err(|error| Error::ToSqlConversionFailure(Box::new(error)))?;
    let mut stmt = conn.prepare_cached(
        "SELECT n.id FROM graph_nodes n WHERE n.owner=?1 AND n.status='active'
         AND (n.workspace_id=?2 OR (?3 AND n.workspace_id='global'))
         AND n.id NOT IN (SELECT value FROM json_each(?4))
         ORDER BY n.id LIMIT ?5",
    )?;
    let mut sample = stmt
        .query_map(
            params![
                scope.owner,
                scope.workspace_id,
                scope.include_global,
                occupied,
                node_room + 1
            ],
            |row| row.get::<_, String>(0),
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if sample.len() > node_room {
        omitted.insert("broad_node_sample_limited");
        sample.truncate(node_room);
    }
    let withheld = withheld_graph_nodes_mode(conn, scope, &sample, GraphReadMode::Recall)?;
    sample.retain(|id| !withheld.contains(id));
    if sample.is_empty() {
        return Ok(Vec::new());
    }
    let new = serde_json::to_string(&sample)
        .map_err(|error| Error::ToSqlConversionFailure(Box::new(error)))?;
    let mut stmt = conn.prepare_cached(
        "WITH extras AS (SELECT value id FROM json_each(?4)),
         admitted AS (SELECT value id FROM json_each(?5) UNION ALL SELECT id FROM extras),
         links AS (
           SELECT e.id,e.src_id,e.dst_id,e.tag,e.fact_id FROM extras x JOIN graph_edges e ON e.src_id=x.id
             JOIN admitted n ON n.id=e.dst_id
             WHERE e.owner=?1 AND e.status='active' AND (e.workspace_id=?2 OR (?3 AND e.workspace_id='global'))
             AND e.src_id<>e.dst_id AND (?6 IS NULL OR e.tag=?6) AND length(CAST(e.tag AS BLOB))<=128
           UNION ALL
           SELECT e.id,e.src_id,e.dst_id,e.tag,e.fact_id FROM extras x JOIN graph_edges e ON e.dst_id=x.id
             JOIN admitted n ON n.id=e.src_id
             WHERE NOT EXISTS(SELECT 1 FROM extras z WHERE z.id=e.src_id)
             AND e.owner=?1 AND e.status='active' AND (e.workspace_id=?2 OR (?3 AND e.workspace_id='global'))
             AND e.src_id<>e.dst_id AND (?6 IS NULL OR e.tag=?6) AND length(CAST(e.tag AS BLOB))<=128
         ) SELECT id,src_id,dst_id,tag,fact_id FROM links ORDER BY id LIMIT ?7",
    )?;
    let mut rows = stmt
        .query_map(
            params![
                scope.owner,
                scope.workspace_id,
                scope.include_global,
                new,
                occupied,
                tag,
                edge_room + 1
            ],
            |row| {
                Ok(EdgeWitness {
                    edge_id: row.get(0)?,
                    from: row.get(1)?,
                    to: row.get(2)?,
                    tag: row.get(3)?,
                    fact_id: row.get(4)?,
                })
            },
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if rows.len() > edge_room {
        omitted.insert("broad_edge_sample_limited");
        rows.truncate(edge_room);
    }
    let ids: Vec<_> = rows.iter().map(|edge| edge.edge_id.clone()).collect();
    let blocked = withheld_graph_edges_mode(conn, scope, &ids, GraphReadMode::Recall)?;
    rows.retain(|edge| !blocked.contains(&edge.edge_id));
    Ok(rows)
}

fn bounded_communities(
    scope: &GraphScope,
    mode: CommunityMode,
    edges: BTreeMap<String, EdgeWitness>,
    anchors: &BTreeSet<String>,
    omitted: &mut BTreeSet<&'static str>,
) -> CommunityEvidence {
    let mut adjacent: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    for edge in edges.values() {
        adjacent.entry(&edge.from).or_default().insert(&edge.to);
        adjacent.entry(&edge.to).or_default().insert(&edge.from);
    }
    let mut labels: BTreeMap<&str, &str> = adjacent.keys().map(|&id| (id, id)).collect();
    let mut converged = adjacent.is_empty();
    let mut iterations = 0;
    for _ in 0..12 {
        if converged {
            break;
        }
        converged = true;
        for (&id, neighbors) in &adjacent {
            let mut votes = BTreeMap::<&str, usize>::new();
            for neighbor in neighbors {
                *votes.entry(labels[neighbor]).or_default() += 1;
            }
            let best = votes
                .into_iter()
                .max_by(|(a_id, a), (b_id, b)| a.cmp(b).then_with(|| b_id.cmp(a_id)))
                .map(|(label, _)| label)
                .unwrap_or(id);
            if labels[id] != best {
                labels.insert(id, best);
                converged = false;
            }
        }
        iterations += 1;
    }
    let mut groups = BTreeMap::<&str, Vec<String>>::new();
    for (&id, &label) in &labels {
        groups.entry(label).or_default().push(id.to_owned());
    }
    let mut support = BTreeMap::<&str, Vec<EdgeWitness>>::new();
    for edge in edges.values() {
        let label = labels[edge.from.as_str()];
        if label == labels[edge.to.as_str()] {
            support.entry(label).or_default().push(edge.clone());
        }
    }
    let mut ranked = Vec::new();
    for (label, members) in groups {
        let witnesses = support.remove(label).unwrap_or_default();
        let anchor_ids: Vec<_> = members
            .iter()
            .filter(|id| anchors.contains(*id))
            .cloned()
            .collect();
        if witnesses.is_empty() || (mode == CommunityMode::Focal && anchor_ids.is_empty()) {
            continue;
        }
        ranked.push((anchor_ids, members, witnesses));
    }
    ranked.sort_by(|a, b| {
        b.0.len()
            .cmp(&a.0.len())
            .then_with(|| b.2.len().cmp(&a.2.len()))
            .then_with(|| a.1.cmp(&b.1))
    });
    if ranked.len() > 6 {
        omitted.insert("community_group_limit");
    }
    if !converged {
        omitted.insert("community_iteration_limit");
    }
    let groups = ranked
        .into_iter()
        .take(6)
        .map(|(anchor_ids, members, witnesses)| {
            let mut hash =
                blake3::Hasher::new_derive_key("Open Clank retained memory community v1");
            let mut field = |text: &str| {
                hash.update(&(text.len() as u64).to_le_bytes());
                hash.update(text.as_bytes());
            };
            field(&scope.owner);
            field(&scope.workspace_id);
            field(if scope.include_global {
                "global"
            } else {
                "workspace"
            });
            for id in &members {
                field(id);
            }
            for edge in &witnesses {
                field(&edge.edge_id);
            }
            CommunityRegion {
                id: hash.finalize().to_hex().to_string(),
                anchor_ids,
                members,
                witnesses,
            }
        })
        .collect();
    CommunityEvidence {
        mode,
        algorithm: "admitted_witness_label_propagation_v1",
        iterations,
        converged,
        groups,
    }
}

impl SqliteStore {
    /// Read-only, bounded native reconstruction. The caller supplies a trusted
    /// scope; request selectors do not confer access. One snapshot covers both
    /// traversal and the lineage checks used to admit evidence into ranking.
    pub fn graph_reconstruct(
        &self,
        scope: &GraphScope,
        request: &ReconstructionRequest,
    ) -> rusqlite::Result<ReconstructionResult> {
        request.validate().map_err(Error::InvalidParameterName)?;
        let mut conn = self.conn.lock().unwrap();
        let read = conn.transaction_with_behavior(TransactionBehavior::Deferred)?;
        let mut candidates = BTreeMap::<String, Candidate>::new();
        let mut frontier = VecDeque::new();
        let mut omitted = BTreeSet::new();
        let broad = request.community == CommunityMode::Broad;
        let local_node_limit = request.max_nodes - if broad { request.max_nodes / 3 } else { 0 };
        let local_edge_limit = request.max_edges - if broad { request.max_edges / 3 } else { 0 };
        let withheld =
            withheld_graph_nodes_mode(&read, scope, &request.seed_ids, GraphReadMode::Recall)?;
        let mut kind_query = read.prepare_cached(
            "SELECT CASE WHEN length(CAST(kind AS BLOB))<=128 THEN kind END
             FROM graph_nodes WHERE id=?1 AND owner=?2 AND status='active'
             AND (workspace_id=?3 OR (?4 AND workspace_id='global'))",
        )?;
        for id in request
            .seed_ids
            .iter()
            .filter(|id| !withheld.contains(*id))
            .take(local_node_limit)
        {
            let kind: Option<String> = kind_query.query_row(
                params![id, scope.owner, scope.workspace_id, scope.include_global],
                |row| row.get(0),
            )?;
            if let Some(kind) = kind {
                candidates.insert(
                    id.clone(),
                    Candidate {
                        kind,
                        lanes: vec![EvidenceLane {
                            lane: "explicit_seed",
                            rank: 1,
                        }],
                        ..Candidate::default()
                    },
                );
                frontier.push_back(id.clone());
            }
        }
        drop(kind_query);

        let mut cue_terms = BTreeSet::new();
        let mut covered_terms = BTreeSet::new();
        if let Some(query) = &request.query {
            let normalized = norm_name(query);
            cue_terms.extend(normalized.split_whitespace().map(str::to_owned));
            let cue_limit = local_node_limit.min(32);
            let hits = graph_cues_in(&read, scope, query, cue_limit + 1)?;
            if hits.len() > cue_limit {
                omitted.insert("cue_limit");
            }
            for (rank, hit) in hits.into_iter().take(cue_limit).enumerate() {
                if hit.node.kind.len() > 128 {
                    continue;
                }
                let normalized_cue = norm_name(&hit.cue);
                let normalized_name = norm_name(&hit.node.name);
                let id = hit.node.id;
                if !candidates.contains_key(&id) && candidates.len() == local_node_limit {
                    omitted.insert("node_limit");
                    break;
                }
                let candidate = candidates.entry(id.clone()).or_insert_with(|| {
                    frontier.push_back(id);
                    Candidate {
                        kind: hit.node.kind,
                        ..Candidate::default()
                    }
                });
                if candidate.lanes.iter().all(|lane| lane.lane != "cue") {
                    candidate.lanes.push(EvidenceLane {
                        lane: "cue",
                        rank: rank + 1,
                    });
                }
                for term in normalized_cue
                    .split_whitespace()
                    .chain(normalized_name.split_whitespace())
                {
                    if cue_terms.contains(term) {
                        covered_terms.insert(term.to_owned());
                    }
                }
                if (normalized_cue == normalized || normalized_name == normalized)
                    && candidate.lanes.iter().all(|lane| lane.lane != "exact")
                {
                    candidate.lanes.push(EvidenceLane {
                        lane: "exact",
                        rank: 1,
                    });
                }
            }
        }
        if request.seed_ids.len() - withheld.len() > local_node_limit {
            omitted.insert("node_limit");
        }

        let tag = request
            .tag
            .as_deref()
            .map(|tag| norm_name(tag).replace(' ', "_"));
        let mut steps = Vec::new();
        let mut examined_edges = 0;
        let mut scanned_edges = 0;
        let mut graph_rank = BTreeMap::<String, f64>::new();
        let mut anchors: BTreeSet<_> = candidates
            .iter()
            .filter(|(_, candidate)| {
                candidate
                    .lanes
                    .iter()
                    .any(|lane| matches!(lane.lane, "exact" | "explicit_seed"))
            })
            .map(|(id, _)| id.clone())
            .collect();
        let strong_cue =
            !anchors.is_empty() || (cue_terms.len() >= 2 && covered_terms.len() == cue_terms.len());
        if anchors.is_empty() {
            anchors.extend(
                candidates
                    .iter()
                    .filter(|(_, candidate)| {
                        candidate
                            .lanes
                            .iter()
                            .any(|lane| lane.lane == "cue" && lane.rank <= 2)
                    })
                    .map(|(id, _)| id.clone()),
            );
        }
        let mut evidence_kinds: BTreeSet<_> =
            candidates.values().map(|row| row.kind.clone()).collect();
        let mut retained = BTreeMap::<String, EdgeWitness>::new();
        let mut typed_support = 0;
        let mut anchored_support = BTreeSet::new();
        let mut low_gain_rounds = 0;
        let mut strongest_gain: f64 = 0.0;
        let mut adaptive_stop = false;
        for step in 1..=request.max_steps {
            if frontier.is_empty() || examined_edges == local_edge_limit {
                break;
            }
            let current: Vec<_> = frontier.drain(..).collect();
            let mut discovered = Vec::new();
            let mut new_witnesses = 0;
            let mut new_intent_witnesses = 0;
            let mut new_evidence_kinds = 0;
            let mut gain = 0.0;
            for id in &current {
                let remaining = local_edge_limit - examined_edges;
                if remaining == 0 {
                    omitted.insert("edge_limit");
                    break;
                }
                let mut edges = load_edges(&read, scope, id, tag.as_deref(), remaining + 1)?;
                if edges.len() > remaining {
                    omitted.insert("edge_limit");
                    edges.truncate(remaining);
                }
                examined_edges += edges.len();
                let ids: Vec<_> = edges
                    .iter()
                    .map(|(edge, _, _)| edge.edge_id.clone())
                    .collect();
                let withheld =
                    withheld_graph_edges_mode(&read, scope, &ids, GraphReadMode::Recall)?;
                edges.retain(|(edge, _, _)| !withheld.contains(&edge.edge_id));
                scanned_edges += edges.len();
                let hub_dampening = (edges.len().max(1) as f64).sqrt();
                for (edge, other_id, kind) in edges {
                    if &other_id == id {
                        continue;
                    }
                    if !candidates.contains_key(&other_id) && candidates.len() == local_node_limit {
                        omitted.insert("node_limit");
                        continue;
                    }
                    let candidate = candidates.entry(other_id.clone()).or_insert_with(|| {
                        frontier.push_back(other_id.clone());
                        discovered.push(other_id.clone());
                        if evidence_kinds.insert(kind.clone()) {
                            new_evidence_kinds += 1;
                        }
                        Candidate {
                            kind,
                            ..Candidate::default()
                        }
                    });
                    let weight =
                        edge_weight(request.intent, &edge.tag) / (hub_dampening * step as f64);
                    if !retained.contains_key(&edge.edge_id) {
                        new_witnesses += 1;
                        gain += weight;
                        if edge_weight(request.intent, &edge.tag) > 1.0 {
                            new_intent_witnesses += 1;
                        }
                        if anchors.contains(&edge.from) || anchors.contains(&edge.to) {
                            anchored_support.insert(edge.edge_id.clone());
                        }
                        retained.insert(edge.edge_id.clone(), edge.clone());
                    }
                    *graph_rank.entry(other_id).or_default() += weight;
                    if weight > candidate.graph_score {
                        candidate.graph_score = weight;
                        candidate.witness = Some(edge);
                    }
                }
            }
            steps.push(ReconstructionStep {
                step,
                frontier: current,
                discovered,
                new_witnesses,
                new_intent_witnesses,
                new_evidence_kinds,
            });
            typed_support += new_intent_witnesses;
            let sufficient_support = strong_cue
                && anchored_support.len() >= 2
                && (matches!(request.intent, Intent::General) || typed_support > 0);
            let low_gain = new_evidence_kinds == 0
                && new_intent_witnesses == 0
                && gain <= strongest_gain * 0.45;
            strongest_gain = strongest_gain.max(gain);
            low_gain_rounds = if low_gain { low_gain_rounds + 1 } else { 0 };
            if request.adaptive
                && sufficient_support
                && low_gain_rounds >= 2
                && !frontier.is_empty()
                && step < request.max_steps
                && examined_edges < local_edge_limit
            {
                adaptive_stop = true;
                omitted.insert("adaptive_frontier_unvisited");
                break;
            }
        }

        if !frontier.is_empty() && !adaptive_stop {
            omitted.insert(if examined_edges == local_edge_limit {
                "edge_limit"
            } else {
                "step_limit"
            });
        }
        let communities = if request.community == CommunityMode::Off {
            None
        } else {
            if broad {
                let extra = broad_evidence(
                    &read,
                    scope,
                    &candidates,
                    tag.as_deref(),
                    request.max_nodes.saturating_sub(candidates.len()),
                    request.max_edges.saturating_sub(examined_edges),
                    &mut omitted,
                )
                .unwrap_or_else(|_| {
                    omitted.insert("broad_sample_unavailable");
                    Vec::new()
                });
                if extra.is_empty() && !retained.is_empty() {
                    omitted.insert("broad_local_evidence_only");
                }
                scanned_edges += extra.len();
                retained.extend(extra.into_iter().map(|edge| (edge.edge_id.clone(), edge)));
            }
            let community =
                bounded_communities(scope, request.community, retained, &anchors, &mut omitted);
            if community.groups.is_empty() {
                omitted.insert("community_witnesses_unavailable");
            }
            Some(community)
        };
        let mut graph_rank: Vec<_> = graph_rank.into_iter().collect();
        graph_rank.sort_by(|(a_id, a), (b_id, b)| b.total_cmp(a).then_with(|| a_id.cmp(b_id)));
        for (rank, (id, _)) in graph_rank.into_iter().enumerate() {
            if let Some(candidate) = candidates.get_mut(&id) {
                candidate.lanes.push(EvidenceLane {
                    lane: "witnessed_graph",
                    rank: rank + 1,
                });
            }
        }
        let mut nodes: Vec<_> = candidates
            .into_iter()
            .map(|(id, candidate)| {
                let score = candidate
                    .lanes
                    .iter()
                    .map(|lane| 1.0 / (60 + lane.rank) as f64)
                    .sum();
                RankedNode {
                    id,
                    kind: candidate.kind,
                    score,
                    lanes: candidate.lanes,
                    witness: candidate.witness,
                }
            })
            .collect();
        let exact = |node: &RankedNode| node.lanes.iter().any(|lane| lane.lane == "exact");
        nodes.sort_by(|a, b| {
            exact(b)
                .cmp(&exact(a))
                .then_with(|| b.score.total_cmp(&a.score))
                .then_with(|| a.id.cmp(&b.id))
        });
        let mut by_kind = BTreeMap::<String, usize>::new();
        nodes.retain(|node| {
            let count = by_kind.entry(node.kind.clone()).or_default();
            *count += 1;
            let eligible = exact(node) || *count <= request.per_kind;
            if !eligible {
                omitted.insert("evidence_kind_limit");
            }
            eligible
        });
        let reverse_ids: Vec<_> = nodes.iter().map(|node| node.id.clone()).collect();
        let reverse_cues = reverse_cues(&read, scope, &reverse_ids, &mut omitted)?;
        let stop_reason = if omitted.contains("edge_limit") {
            "edge_limit"
        } else if omitted.contains("step_limit") {
            "step_limit"
        } else if omitted.contains("node_limit") {
            "node_limit"
        } else if adaptive_stop {
            "adaptive_evidence_stable"
        } else if nodes.is_empty()
            && communities
                .as_ref()
                .is_none_or(|analysis| analysis.groups.is_empty())
        {
            "no_evidence"
        } else {
            "frontier_exhausted"
        };
        read.commit()?;
        Ok(ReconstructionResult {
            version: 1,
            nodes,
            steps,
            reverse_cues,
            omitted: omitted.into_iter().collect(),
            stop_reason,
            scanned_edges,
            communities,
        })
    }
}
