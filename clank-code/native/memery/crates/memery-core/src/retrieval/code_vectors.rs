//! Optional declaration-vector ranking within an authorized native Code query.
//! The broker may supply an admitted embedding generation. Request data from
//! an agent or editor must never be deserialized directly into that generation.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering};

use serde::{Deserialize, Serialize};

use crate::code::WorkspaceBinding;
use crate::code_evidence::SourceLocator;
use crate::code_query::{
    Channel, CodeQuery, CodeQueryResult, Field, Operation, QueryError, QueryReceipt,
};
use crate::graph::GraphScope;
use crate::store::sqlite::SqliteStore;

use super::code_vector_index::{IndexScope, RetainedScan};
use super::vector_backend::{FlatIndex, Metric, Profile, Row, Vector};

const MAX_VECTORS: usize = 64;
const MAX_VECTOR_DIMENSION: usize = 1024;

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EmbeddingIdentity {
    pub provider: String,
    pub model: String,
    pub revision: String,
    pub dimension: usize,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeclarationVector {
    pub declaration_id: String,
    /// Exact indexed file hash and byte span that the producer encoded.
    pub source: SourceLocator,
    pub values: Vec<f32>,
}

/// Private broker-to-native input. Both model identities refer to the
/// authorized producer's actual descriptor, and the indexed receipt refers to
/// the snapshot used to read the source windows.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdmittedCodeVectors {
    pub owner: String,
    pub owner_subject_id: String,
    pub indexed_receipt: QueryReceipt,
    pub profile: EmbeddingIdentity,
    pub query_profile: EmbeddingIdentity,
    pub query: Vec<f32>,
    pub declarations: Vec<DeclarationVector>,
    #[serde(default)]
    pub published: Option<IndexScope>,
}

#[derive(Debug, Serialize)]
pub struct CodeRankingLane {
    pub channel: Channel,
    pub rank: usize,
}

#[derive(Debug, Serialize)]
pub struct CodeRankingHit {
    pub id: String,
    pub score: f64,
    pub lanes: Vec<CodeRankingLane>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub similarity: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<SourceLocator>,
}

#[derive(Debug, Serialize)]
pub struct CodeHybridRead {
    pub result: CodeQueryResult,
    /// Ranking applies only to this admitted metadata page. The native query
    /// cursor stays an immutable continuation of the original ordered lane.
    pub ranking: Vec<CodeRankingHit>,
    pub vector_status: &'static str,
    pub vector_omitted: usize,
    pub max_rows: usize,
    pub published_scanned: usize,
}

fn vector_profile(embedding: &AdmittedCodeVectors, receipt: &QueryReceipt) -> Profile {
    Profile {
        owner: embedding.owner.clone(),
        workspace: receipt.workspace_id.clone(),
        project: receipt.registration_id.clone(),
        generation_id: receipt.evidence.head_generation.clone(),
        source_watermark: receipt.source_snapshot_id.clone(),
        eligibility_epoch: format!(
            "{}:{}:{}",
            receipt.evidence.admission_epoch,
            receipt.evidence.policy_generation,
            receipt.evidence.config_fingerprint
        ),
        model: format!("{}:{}", embedding.profile.provider, embedding.profile.model),
        model_version: embedding.profile.revision.clone(),
        dimension: embedding.profile.dimension,
        metric: Metric::UnitCosine,
        normalization: "unit_l2".into(),
    }
}

fn rank_page(
    result: &CodeQueryResult,
    receipt: &QueryReceipt,
    embedding: &AdmittedCodeVectors,
) -> Result<(Vec<CodeRankingHit>, usize), &'static str> {
    let sources: BTreeMap<_, _> = result
        .hits
        .iter()
        .filter(|hit| hit.kind == "declaration")
        .filter_map(|hit| hit.source.as_ref().map(|source| (hit.id.as_str(), source)))
        .collect();
    let admitted: BTreeSet<String> = sources.keys().map(|id| id.to_string()).collect();
    let mut accepted = BTreeSet::new();
    let mut rows = Vec::new();
    for vector in &embedding.declarations {
        if sources.get(vector.declaration_id.as_str()).copied() != Some(&vector.source) {
            continue;
        }
        if !accepted.insert(&vector.declaration_id) {
            return Err("duplicate_declaration_vector");
        }
        rows.push(Row {
            id: vector.declaration_id.clone(),
            source_revision: format!(
                "{}:{}:{}",
                vector.source.source_hash,
                vector.source.span.start_byte,
                vector.source.span.end_byte
            ),
            vector: Vector::Float(vector.values.clone()),
        });
    }
    if rows.is_empty() {
        return Err("no_current_declaration_vectors");
    }
    let profile = vector_profile(embedding, receipt);
    let flat =
        FlatIndex::build(profile.clone(), rows).map_err(|_| "embedding_profile_unavailable")?;
    let similarities: BTreeMap<_, _> = flat
        .search(
            &profile,
            &admitted,
            None,
            &Vector::Float(embedding.query.clone()),
            MAX_VECTORS,
            None,
        )
        .map_err(|_| "embedding_profile_unavailable")?
        .hits
        .into_iter()
        .enumerate()
        .map(|(rank, hit)| (hit.id, (rank + 1, hit.score)))
        .collect();
    let mut ranking: Vec<_> = result
        .hits
        .iter()
        .map(|hit| {
            let mut lanes: Vec<_> = hit
                .matches
                .iter()
                .map(|matched| CodeRankingLane {
                    channel: matched.channel,
                    rank: matched.rank,
                })
                .collect();
            let similarity = similarities.get(&hit.id).map(|&(rank, score)| {
                lanes.push(CodeRankingLane {
                    channel: Channel::Semantic,
                    rank,
                });
                score
            });
            let score = lanes.iter().map(|lane| 1.0 / (60 + lane.rank) as f64).sum();
            CodeRankingHit {
                id: hit.id.clone(),
                score,
                lanes,
                similarity,
                source: hit.source.clone(),
            }
        })
        .collect();
    let exact = |hit: &CodeRankingHit| hit.lanes.iter().any(|lane| lane.channel == Channel::Exact);
    ranking.sort_by(|a, b| {
        exact(b)
            .cmp(&exact(a))
            .then_with(|| b.score.total_cmp(&a.score))
            .then_with(|| a.id.cmp(&b.id))
    });
    Ok((ranking, admitted.len() - accepted.len()))
}

impl SqliteStore {
    /// Preserve metadata results when no compatible producer is present.
    /// Rank only current admitted declarations, and never promote similarities
    /// to source proof or to edges in the existing Code Graph result.
    pub fn code_search_with_vectors(
        &self,
        scope: &GraphScope,
        binding: &WorkspaceBinding,
        query: &CodeQuery,
        vectors: Option<&AdmittedCodeVectors>,
        cancel: &AtomicBool,
        cursor_secret: &[u8; 32],
    ) -> Result<CodeHybridRead, QueryError> {
        let should_rank = query.operation == Operation::Search
            && query.channels.contains(&Channel::Semantic)
            && query.fields.contains(&Field::Source);
        let mut baseline = query.clone();
        if should_rank
            && !baseline.channels.iter().any(|channel| {
                matches!(
                    channel,
                    Channel::Exact | Channel::Identifier | Channel::Fuzzy | Channel::Lexical
                )
            })
        {
            baseline
                .channels
                .extend([Channel::Exact, Channel::Identifier, Channel::Lexical]);
        }
        let mut result = self.code_query(scope, binding, &baseline, cancel, cursor_secret)?;
        let missing = |result, state| CodeHybridRead {
            result,
            ranking: Vec::new(),
            vector_status: state,
            vector_omitted: 0,
            max_rows: baseline.budgets.rows,
            published_scanned: 0,
        };
        let (Some(vectors), Some(receipt)) = (vectors, result.receipt.clone()) else {
            return Ok(missing(result, "embedding_producer_unavailable"));
        };
        if !should_rank || cancel.load(Ordering::Relaxed) {
            return Ok(missing(result, "embedding_lane_unavailable"));
        }
        if result.error.is_some()
            || vectors.owner != scope.owner
            || vectors.owner_subject_id != binding.owner_subject_id
            || vectors.indexed_receipt != receipt
            || vectors.profile != vectors.query_profile
            || [
                &vectors.profile.provider,
                &vectors.profile.model,
                &vectors.profile.revision,
            ]
            .into_iter()
            .any(|value| value.trim().is_empty() || value.len() > 512)
            || !(1..=MAX_VECTOR_DIMENSION).contains(&vectors.profile.dimension)
            || vectors.query.len() != vectors.profile.dimension
            || vectors.declarations.len() > MAX_VECTORS
            || (vectors.published.is_none() && result.hits.len() > MAX_VECTORS)
            || vectors
                .declarations
                .iter()
                .any(|vector| vector.values.len() != vectors.profile.dimension)
        {
            return Ok(missing(result, "embedding_profile_unavailable"));
        }
        let mut published_scanned = 0;
        let mut published_omitted = 0;
        let is_published = vectors.published.is_some();
        let mut loaded;
        let vectors = if let Some(index) = &vectors.published {
            if !vectors.declarations.is_empty()
                || index.receipt != receipt
                || binding.origin != "app"
                || baseline.cursor.is_some()
                || (baseline.filter != Default::default()
                    && baseline.filter
                        != crate::code_query::QueryFilter {
                            kinds: vec!["declaration".into()],
                            ..Default::default()
                        })
            {
                return Ok(missing(result, "semantic_published_scope_unavailable"));
            }
            let work = baseline
                .budgets
                .work
                .saturating_sub(result.cost.inspected_records);
            match self.scan_code_vector_index(
                scope,
                binding,
                index,
                &vectors.profile,
                &vectors.query,
                work,
                cancel,
            ) {
                Ok(RetainedScan::Ranked {
                    candidates,
                    visited,
                    omitted,
                }) => {
                    loaded = vectors.clone();
                    loaded.declarations = candidates;
                    published_scanned = visited;
                    published_omitted = omitted;
                    result.cost.inspected_records =
                        result.cost.inspected_records.saturating_add(visited);
                    &loaded
                }
                Ok(RetainedScan::Budget) => {
                    return Ok(missing(result, "semantic_published_index_budget"))
                }
                Ok(RetainedScan::Missing) => {
                    return Ok(missing(result, "semantic_published_profile_unavailable"))
                }
                Err(error) if error.code == "cancelled" => return Err(error),
                Err(_) => return Ok(missing(result, "semantic_published_index_unavailable")),
            }
        } else {
            vectors
        };
        // The protected Host may acquire samples from a bounded outline when
        // no lexical declarations match. Re-inspect only its supplied IDs
        // against the same filters and immutable receipt before scoring.
        let mut baseline_without_discovery = None;
        if baseline.cursor.is_none()
            && (is_published || !result.hits.iter().any(|hit| hit.kind == "declaration"))
        {
            let spare = if is_published {
                baseline.budgets.rows
            } else {
                baseline.budgets.rows.saturating_sub(result.hits.len())
            };
            let existing: BTreeSet<_> = result.hits.iter().map(|hit| hit.id.as_str()).collect();
            let work = baseline
                .budgets
                .work
                .saturating_sub(result.cost.inspected_records);
            if spare > 0 && work > 0 && !vectors.declarations.is_empty() {
                let mut inspect = baseline.clone();
                inspect.operation = Operation::Inspect;
                inspect.query = None;
                inspect.channels.clear();
                inspect.seed_ids = vectors
                    .declarations
                    .iter()
                    .filter(|row| !existing.contains(row.declaration_id.as_str()))
                    .take(spare)
                    .map(|row| row.declaration_id.clone())
                    .collect();
                if !inspect.seed_ids.is_empty() {
                    inspect.expected_receipt = Some(receipt.clone());
                    inspect.budgets.rows = inspect.seed_ids.len();
                    inspect.budgets.work = work;
                    let candidates =
                        self.code_query(scope, binding, &inspect, cancel, cursor_secret)?;
                    if candidates.receipt.as_ref() != Some(&receipt) {
                        return Ok(missing(result, "embedding_profile_unavailable"));
                    }
                    baseline_without_discovery = Some(result.clone());
                    result.cost.inspected_records = result
                        .cost
                        .inspected_records
                        .saturating_add(candidates.cost.inspected_records);
                    result.cost.metadata_bytes = result
                        .cost
                        .metadata_bytes
                        .saturating_add(candidates.cost.metadata_bytes);
                    result.hits.extend(
                        candidates
                            .hits
                            .into_iter()
                            .filter(|hit| hit.kind == "declaration"),
                    );
                    if !is_published {
                        result.reasons.push("semantic_discovery_page".into());
                    }
                    for count in &mut result.counts {
                        if count.name == "returned" {
                            count.value = Some(result.hits.len());
                        }
                    }
                }
            }
        }
        match rank_page(&result, &receipt, vectors) {
            Ok((ranking, vector_omitted)) => {
                self.code_query_check(scope, binding, &receipt)?;
                if cancel.load(Ordering::Relaxed) {
                    return Err(QueryError {
                        code: "cancelled".into(),
                        message: "Code query cancelled".into(),
                    });
                }
                Ok(CodeHybridRead {
                    result,
                    ranking,
                    vector_status: if is_published {
                        "ranked_published_generation"
                    } else {
                        "ranked_current_page"
                    },
                    vector_omitted: vector_omitted.saturating_add(published_omitted),
                    max_rows: baseline.budgets.rows,
                    published_scanned,
                })
            }
            Err(reason) => Ok(missing(
                baseline_without_discovery.unwrap_or(result),
                reason,
            )),
        }
    }
}

impl CodeHybridRead {
    pub fn into_public_result(mut self) -> CodeQueryResult {
        use crate::code_query::ChannelMatch;

        let published = self.vector_status == "ranked_published_generation";
        if !published && self.vector_status != "ranked_current_page" {
            self.result.degraded = true;
            self.result.reasons.push(self.vector_status.into());
            return self.result;
        }

        let mut hits: BTreeMap<_, _> = std::mem::take(&mut self.result.hits)
            .into_iter()
            .map(|hit| (hit.id.clone(), hit))
            .collect();
        let mut semantic_count = 0;
        let truncated = self.ranking.len() > self.max_rows;
        for ranked in self.ranking.into_iter().take(self.max_rows) {
            let Some(mut hit) = hits.remove(&ranked.id) else {
                continue;
            };
            if let Some((lane, similarity)) = ranked
                .lanes
                .iter()
                .find(|lane| lane.channel == Channel::Semantic)
                .zip(ranked.similarity)
            {
                hit.matches.push(ChannelMatch {
                    channel: Channel::Semantic,
                    rank: lane.rank,
                    ordering_score: (similarity * 1_000_000.0).round() as i64,
                    explanation: if published {
                        "Semantic similarity within the current published declaration generation"
                    } else {
                        "Semantic similarity among admitted declarations on this page"
                    }
                    .into(),
                });
                semantic_count += 1;
            }
            self.result.hits.push(hit);
        }
        for cap in &mut self.result.capabilities {
            if cap.channel == Channel::Semantic {
                cap.available = true;
                cap.reason = None;
            }
        }
        for lane in &mut self.result.channels {
            if lane.channel == Channel::Semantic {
                lane.enabled = true;
                lane.executed = true;
                lane.error = None;
                lane.producer = Some(
                    if published {
                        "native_code_published_declaration_vectors_v1"
                    } else {
                        "native_code_declaration_vectors_v1"
                    }
                    .into(),
                );
                lane.candidate_count = Some(semantic_count);
                lane.inspected_records = if published {
                    self.published_scanned
                } else {
                    semantic_count
                };
            }
        }
        self.result.partial |= !published
            || truncated
            || self.vector_omitted != 0
            || self.published_scanned > semantic_count;
        let retained: BTreeSet<_> = self.result.hits.iter().map(|hit| hit.id.as_str()).collect();
        self.result.edges.retain(|edge| {
            retained.contains(edge.from.as_str()) && retained.contains(edge.to.as_str())
        });
        for count in &mut self.result.counts {
            if count.name == "returned" {
                count.value = Some(self.result.hits.len());
            }
        }
        self.result
            .reasons
            .retain(|reason| reason != "configured_semantic_producer_not_admitted");
        self.result.degraded = self
            .result
            .channels
            .iter()
            .any(|lane| lane.requested && (!lane.enabled || lane.error.is_some()));
        self.result.reasons.push(
            if published {
                "semantic_ranked_published_generation"
            } else {
                "semantic_ranked_current_page"
            }
            .into(),
        );
        if self.vector_omitted != 0 {
            self.result
                .reasons
                .push("semantic_declarations_omitted".into());
        }
        self.result
    }
}
