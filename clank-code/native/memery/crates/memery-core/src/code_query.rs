//! Bounded code-only read contract. No paths are opened or authority inferred here.
use crate::code_evidence::SourceLocator;
use crate::store::code_evidence::CodeEvidenceReceipt;
use serde::{Deserialize, Serialize};

#[path = "code_import_analysis.rs"]
mod import_analysis;

pub const QUERY_SCHEMA: &str = "open-clank.code-query/v1";
pub const QUERY_VERSION: u32 = 1;
/// Finite complete-metadata + ranking envelope, qualified on real source.
pub const MAX_QUERY_WORK: usize = 4_000_000;
pub fn new_query_secret() -> [u8; 32] {
    let mut key = [0u8; 32];
    key[..16].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
    key[16..].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
    key
}
pub fn query_fingerprint() -> String {
    let mut h = blake3::Hasher::new();
    h.update(&QUERY_VERSION.to_le_bytes());
    h.update(include_str!("code_query.rs").as_bytes());
    h.update(include_str!("store/code_query.rs").as_bytes());
    h.update(include_str!("code_import_analysis.rs").as_bytes());
    h.update(include_str!("store/code_partitioned.rs").as_bytes());
    h.finalize().to_hex().to_string()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Capabilities,
    Search,
    Overview,
    Catalog,
    Facets,
    Outline,
    Inspect,
    Neighbors,
    Path,
    Impact,
    ImportAnalysis,
    DeadCodeCandidates,
    GraphCommunities,
    StructuralTwins,
    BackwardSlice,
    ErrorBoundaries,
    Read,
    Pipeline,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Channel {
    Exact,
    Identifier,
    Fuzzy,
    Lexical,
    Structural,
    Raw,
    Semantic,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Incoming,
    Outgoing,
    Both,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Field {
    Label,
    Source,
    Signature,
    Documentation,
    Occurrences,
    Resolution,
    Properties,
    Excerpt,
    Witnesses,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueryReceipt {
    pub workspace_id: String,
    pub registration_id: String,
    pub source_snapshot_id: String,
    #[serde(flatten)]
    pub evidence: CodeEvidenceReceipt,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadWindow {
    pub rel_path: String,
    pub start_byte: u64,
    pub length: usize,
    pub continuation: Option<String>,
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct QueryFilter {
    pub rel_path: Option<String>,
    pub path_prefix: Option<String>,
    pub kinds: Vec<String>,
    pub symbol_kinds: Vec<String>,
    pub languages: Vec<String>,
    pub states: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub relation_kinds: Vec<String>,
}

/// Occurrence-kind codes are stable, one-based positions in this registry.
const RELATION_KINDS: &[&str] = &[
    "call", "import", "value_reference", "type_reference", "inherits", "implements",
    "render", "route", "overrides", "manifest_dependency", "sql_table_reference",
    "callback_registration", "event_publish", "event_consume",
];
pub(crate) fn relation_kind_code(name:&str)->Option<i64> {
    RELATION_KINDS.iter().position(|&kind|kind==name).map(|index|index as i64+1)
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct QueryBudget {
    pub rows: usize,
    pub bytes: usize,
    pub tokens: usize,
    pub hops: usize,
    pub work: usize,
}
impl Default for QueryBudget {
    fn default() -> Self {
        Self {
            rows: 50,
            bytes: 65536,
            tokens: 8192,
            hops: 4,
            work: MAX_QUERY_WORK,
        }
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StageOperation {
    Find,
    Filter,
    Deps,
    Outline,
    Read,
    Limit,
    Union,
    Intersection,
    Difference,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueryStage {
    pub operation: StageOperation,
    #[serde(default)]
    pub query: Option<String>,
    #[serde(default)]
    pub filter: QueryFilter,
    #[serde(default)]
    pub with_stage: Option<usize>,
    #[serde(default)]
    pub limit: Option<usize>,
}
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommunityAlgorithm {
    #[default]
    LabelPropagation,
    ModularityRefinement,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CodeQuery {
    pub schema: String,
    pub operation: Operation,
    pub query: Option<String>,
    pub seed_ids: Vec<String>,
    pub target_id: Option<String>,
    pub channels: Vec<Channel>,
    pub fields: Vec<Field>,
    pub filter: QueryFilter,
    pub budgets: QueryBudget,
    pub expected_receipt: Option<QueryReceipt>,
    pub read: Option<ReadWindow>,
    pub direction: Option<Direction>,
    pub cursor: Option<String>,
    pub stages: Vec<QueryStage>,
    #[serde(skip_serializing_if="Option::is_none")]
    pub community_algorithm: Option<CommunityAlgorithm>,
    pub request_id: Option<String>,
}
impl Default for CodeQuery {
    fn default() -> Self {
        Self {
            schema: QUERY_SCHEMA.into(),
            operation: Operation::Search,
            query: None,
            seed_ids: vec![],
            target_id: None,
            channels: vec![Channel::Exact, Channel::Identifier],
            fields: vec![Field::Label, Field::Source, Field::Properties],
            filter: QueryFilter::default(),
            budgets: QueryBudget::default(),
            expected_receipt: None,
            read: None,
            direction: None,
            cursor: None,
            stages: vec![],
            community_algorithm: None,
            request_id: None,
        }
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelStatus {
    pub channel: Channel,
    pub available: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperationCapability {
    pub operation: Operation,
    pub available: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelExecution {
    pub channel: Channel,
    pub requested: bool,
    pub enabled: bool,
    pub executed: bool,
    pub cancelled: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<QueryError>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub producer: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub config_fingerprint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub candidate_count: Option<usize>,
    pub inspected_records: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub elapsed_micros: Option<u64>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelMatch {
    pub channel: Channel,
    pub rank: usize,
    /// Integer lane-local ordering key, NOT relevance probability or Trust.
    pub ordering_score: i64,
    pub explanation: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueryError {
    pub code: String,
    pub message: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Omission {
    pub field: Field,
    pub reason: String,
}
/// SHA256 observations belong to the Files read owner. Never equate them with
/// the canonical S04 BLAKE3 source_hash or claim a range hash covers a file.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawObservation {
    pub rel_path: String,
    pub start_byte: u64,
    pub end_byte: u64,
    pub hash_algorithm: String,
    pub hash_scope: String,
    pub hash: String,
    pub state: String,
    pub file_size: Option<u64>,
    pub continuation: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WindowContinuity {
    FullSha256,
    ProgressOnlyNonatomic,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceWindow {
    pub rel_path: String,
    pub start_byte: u64,
    pub end_byte: u64,
    pub continuation: Option<String>,
    pub continuity: WindowContinuity,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QueryHit {
    pub id: String,
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<SourceLocator>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub documentation: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub occurrences: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub properties: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub excerpt: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_source: Option<RawObservation>,
    pub matches: Vec<ChannelMatch>,
    pub evidence_state: String,
    pub current_source_verified: bool,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueryEdge {
    pub from: String,
    pub to: String,
    pub occurrence_id: String,
    pub relation_kind: String,
    pub resolution_state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<SourceLocator>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witnesses: Option<Vec<serde_json::Value>>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueryPath {
    pub node_ids: Vec<String>,
    pub edges: Vec<QueryEdge>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueryCount {
    pub name: String,
    pub value: Option<usize>,
    pub unit: String,
    pub exactness: String,
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueryCost {
    pub inspected_records: usize,
    pub serialized_bytes: usize,
    pub token_upper_bound: usize,
    pub metadata_bytes: usize,
    pub files_bytes_read: usize,
    pub validation_bytes_read: usize,
    #[serde(default)]
    pub metadata_records_validated: usize,
    #[serde(default)]
    pub native_rpc_calls_completed: usize,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ImportAnalysisUnit {
    Cycle { node_ids: Vec<String>, occurrence_ids: Vec<String> },
    Hub { node_id: String, in_degree: usize, out_degree: usize },
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImportAnalysis {
    pub policy: String,
    pub file_count: usize,
    pub edge_count: usize,
    pub inferred: usize,
    pub ambiguous: usize,
    pub unresolved: usize,
    pub unsupported: usize,
    pub excluded: usize,
    pub missing: usize,
    pub units: Vec<ImportAnalysisUnit>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphCommunity {
    pub members: Vec<String>,
    pub edges: Vec<QueryEdge>,
    #[serde(default,skip_serializing_if="String::is_empty")]
    pub region_id: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommunityAnalysis {
    pub algorithm:String,pub iterations:usize,pub converged:bool,pub isolates:usize,
    pub omitted_groups:usize,pub cross_group_edges:usize,pub ignored_uncertain:usize,
    pub unresolved_resolutions:usize,pub ambiguous_resolutions:usize,pub inferred_resolutions:usize,
    pub unsupported_resolutions:usize,pub excluded_exact_resolutions:usize,pub missing_resolutions:usize,
    pub unavailable_languages:usize,pub incomplete_file_coverage:usize,pub groups:Vec<GraphCommunity>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutlineCompleteness { pub version:u32, pub file_id:String, pub rel_path:String, pub source_hash:String, pub declaration_count:usize, pub scope:String }

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SliceAnalysis {pub version:u32,pub profile:String,pub function_id:String,pub seed_occurrence_id:String,pub source:SourceLocator,pub return_span:crate::code_evidence::SourceSpan,pub root_definition_id:String,pub units:Vec<crate::code_evidence::local_slice::LocalDefinition>}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorBoundaryAnalysis {pub version:u32,pub profile:String,pub function_id:String,pub source:SourceLocator,pub regions:Vec<crate::code_evidence::error_boundaries::ErrorRegion>}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CodeQueryResult {
    pub schema: String,
    pub operation: Operation,
    pub receipt: Option<QueryReceipt>,
    pub capabilities: Vec<ChannelStatus>,
    pub channels: Vec<ChannelExecution>,
    #[serde(default)]
    pub operation_capabilities: Vec<OperationCapability>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_window: Option<SourceWindow>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outline_completeness: Option<OutlineCompleteness>,
    pub hits: Vec<QueryHit>,
    pub edges: Vec<QueryEdge>,
    pub paths: Vec<QueryPath>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub analysis: Option<ImportAnalysis>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub community_analysis: Option<CommunityAnalysis>,
    #[serde(default, skip_serializing_if="Option::is_none")]
    pub slice_analysis: Option<SliceAnalysis>,
    #[serde(default, skip_serializing_if="Option::is_none")]
    pub error_boundary_analysis: Option<ErrorBoundaryAnalysis>,
    pub counts: Vec<QueryCount>,
    pub facets: serde_json::Value,
    pub omissions: Vec<Omission>,
    pub partial: bool,
    pub degraded: bool,
    pub reasons: Vec<String>,
    pub next_cursor: Option<String>,
    pub error: Option<QueryError>,
    pub cost: QueryCost,
}

use crate::code_evidence::{FileEvidence, ResolutionEvidence, ResolutionState};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};

/// Private scope_key is a trusted serialized owner/subject/workspace/binding
/// tuple. It never crosses the public result boundary.
pub struct QuerySnapshot {
    pub receipt: QueryReceipt,
    pub scope_key: String,
    pub files: Vec<FileEvidence>,
    pub resolutions: Vec<ResolutionEvidence>,
    pub coverage: Vec<crate::code::ScanCoverage>,
    pub metadata_bytes: usize,
    pub metadata_records: usize,
    /// Exact resource count after the query's filter, before search matching.
    /// Indexed readers may materialize only matching resources; legacy readers
    /// leave this unset and count their complete materialized candidate set.
    pub eligible_records: Option<usize>,
    pub outline_candidate: Option<OutlineCompleteness>,
}

pub(crate) fn outline_eligible(query:&CodeQuery, file:&FileEvidence, coverage:&[crate::code::ScanCoverage])->Option<OutlineCompleteness> {
    if query.operation!=Operation::Outline || query.expected_receipt.is_none() || query.cursor.is_some() || query.seed_ids.len()!=1 || query.seed_ids[0]!=format!("file:{}",file.file_id) || query.filter.rel_path.as_deref()!=Some(file.rel_path.as_str()) || query.filter.path_prefix.is_some() || !query.filter.kinds.is_empty() || !query.filter.symbol_kinds.is_empty() || !query.filter.languages.is_empty() || !query.filter.states.is_empty() || !query.filter.relation_kinds.is_empty() || !query.fields.contains(&Field::Source) || !query.fields.contains(&Field::Properties) || query.query.is_some() || query.target_id.is_some() || query.direction.is_some() || !query.stages.is_empty() || !matches!(file.language.as_str(),"rust"|"python"|"typescript"|"javascript") || !file.capability.available || file.diagnostics.iter().any(|d|!matches!(d.code.as_str(),"conditional_import"|"unsupported_sql_code"|"unsupported_callback_registration")) {return None;}
    let mut matching=coverage.iter().filter(|c|c.rel_path==file.rel_path);let row=matching.next()?;
    if matching.next().is_some() || !matches!(row.status.as_str(),"indexed"|"unchanged"|"no_source_units") || row.source_hash.as_deref()!=Some(file.source_hash.as_str()) {return None;}
    Some(OutlineCompleteness{version:1,file_id:format!("file:{}",file.file_id),rel_path:file.rel_path.clone(),source_hash:file.source_hash.clone(),declaration_count:file.declarations.len(),scope:"supported_indexed_file_declarations".into()})
}

fn error(code: &str, message: &str) -> QueryError {
    QueryError {
        code: code.into(),
        message: message.into(),
    }
}
pub fn validate_query(query: &CodeQuery) -> Result<(), QueryError> {
    if query.operation == Operation::Pipeline || !query.stages.is_empty() {
        return Err(error(
            "unsupported",
            "Composed pipelines are optional and not admitted",
        ));
    }
    let b = &query.budgets;
    if query.schema != QUERY_SCHEMA
        || b.rows == 0
        || b.rows > 200
        || b.bytes < 512
        || b.bytes > 262144
        || b.tokens < 512
        || b.tokens > 8192
        || b.hops > 8
        || b.work == 0
        || b.work > MAX_QUERY_WORK
        || query.query.as_ref().is_some_and(|s| s.len() > 1024)
        || query.seed_ids.len() > 200
        || query.channels.len() > 7
        || query.fields.len() > 9
        || query.stages.len() > 8
        || query.cursor.as_ref().is_some_and(|s| s.len() > 4096)
        || query.request_id.as_ref().is_some_and(|s| s.len() > 128)
    {
        return Err(error(
            "invalid",
            "Code query exceeds its declared finite limits",
        ));
    }
    if query.community_algorithm.is_some() && query.operation != Operation::GraphCommunities {
        return Err(error("unsupported", "Community algorithm requires graph_communities"));
    }
    if matches!(query.operation,Operation::ImportAnalysis|Operation::DeadCodeCandidates|Operation::GraphCommunities) && (query.filter != QueryFilter::default() || query.query.is_some()
        || !query.seed_ids.is_empty() || query.target_id.is_some() || query.direction.is_some()
        || query.read.is_some() || !query.stages.is_empty()) {
        return Err(error("unsupported", "This analysis requires the full admitted snapshot"));
    }
    if query.operation==Operation::ErrorBoundaries && (query.filter!=QueryFilter::default() || query.query.is_some() || query.seed_ids.len()!=1 || !query.seed_ids[0].starts_with("decl:") || query.expected_receipt.is_none() || query.target_id.is_some() || query.direction.is_some() || query.read.is_some() || !query.stages.is_empty()) {return Err(error("unsupported","Error boundaries require one current-receipt function"));}
    if query.operation==Operation::BackwardSlice && (query.filter!=QueryFilter::default() || query.query.is_some() || query.seed_ids.len()!=1 || !query.seed_ids[0].starts_with("occ:") || query.expected_receipt.is_none() || query.target_id.is_some() || query.direction.is_some() || query.read.is_some() || !query.stages.is_empty()) {return Err(error("unsupported","Backward slice requires one current-receipt return-use occurrence"));}
    if query.operation == Operation::StructuralTwins && (query.filter != QueryFilter::default() || query.query.is_some()
        || query.seed_ids.len() != 1 || !query.seed_ids[0].starts_with("decl:") || query.target_id.is_some() || query.direction.is_some()
        || query.read.is_some() || !query.stages.is_empty()) {
        return Err(error("unsupported", "Structural twins require one declaration seed and the full admitted snapshot"));
    }
    for path in [&query.filter.rel_path, &query.filter.path_prefix]
        .into_iter()
        .flatten()
    {
        validate_relative(path)?;
    }
    validate_filter(&query.filter)?;
    if !query.filter.relation_kinds.is_empty() && !matches!(query.operation,Operation::Neighbors|Operation::Path|Operation::Impact) {
        return Err(error("unsupported", "Relation filters require neighbors, path or impact"));
    }
    if query.expected_receipt.as_ref().is_some_and(|r| {
        [
            r.workspace_id.as_str(),
            r.registration_id.as_str(),
            r.source_snapshot_id.as_str(),
            r.evidence.checkout_id.as_str(),
            r.evidence.head_generation.as_str(),
            r.evidence.config_fingerprint.as_str(),
        ]
        .iter()
        .any(|s| s.is_empty() || s.len() > 256)
            || r.evidence.admission_epoch < 0
            || r.evidence.policy_generation < 0
    }) {
        return Err(error("invalid", "Invalid bounded expected receipt"));
    }
    if let Some(read) = &query.read {
        validate_relative(&read.rel_path)?;
        if read.length == 0
            || read.length > 32768
            || read.start_byte.checked_add(read.length as u64).is_none()
            || read.continuation.as_ref().is_some_and(|s| s.len() > 4096)
        {
            return Err(error("invalid", "Invalid bounded source window"));
        }
    }
    for id in query.seed_ids.iter().chain(query.target_id.iter()) {
        if !["file:", "decl:", "occ:"].iter().any(|p| {
            id.strip_prefix(p).is_some_and(|s| {
                !s.is_empty() && s.len() <= 128 && s.bytes().all(|b| b.is_ascii_hexdigit())
            })
        }) {
            return Err(error("invalid", "Expected a typed code resource identity"));
        }
    }
    let mut read = false;
    for (index, stage) in query.stages.iter().enumerate() {
        if stage.query.as_ref().is_some_and(|s| s.len() > 1024)
            || stage.with_stage.is_some_and(|n| n >= index)
            || stage.limit.is_some_and(|n| n > 200)
            || (read && stage.operation != StageOperation::Limit)
        {
            return Err(error("invalid", "Invalid bounded exploration stage"));
        }
        for path in [&stage.filter.rel_path, &stage.filter.path_prefix]
            .into_iter()
            .flatten()
        {
            validate_relative(path)?;
        }
        validate_filter(&stage.filter)?;
        read |= stage.operation == StageOperation::Read;
    }
    Ok(())
}
fn validate_filter(filter: &QueryFilter) -> Result<(), QueryError> {
    if filter.relation_kinds.len()>RELATION_KINDS.len() || filter.relation_kinds.iter().any(|name|relation_kind_code(name).is_none()) {
        return Err(error("invalid", "Unknown code relation kind"));
    }
    if [&filter.kinds, &filter.symbol_kinds, &filter.languages, &filter.states]
        .iter()
        .any(|values| values.len() > 64 || values.iter().any(|v| v.len() > 128))
    {
        return Err(error("invalid", "Code filter exceeds finite limits"));
    }
    Ok(())
}
pub fn validate_relative(path: &str) -> Result<(), QueryError> {
    if path.is_empty()
        || path.len() > 4096
        || path.contains('\0')
        || path.contains('\\')
        || std::path::Path::new(path).is_absolute()
        || path
            .trim_end_matches('/')
            .split('/')
            .any(|p| p.is_empty() || p == ".." || p == ".")
    {
        return Err(error("invalid", "Invalid relative code resource"));
    }
    Ok(())
}

/// Shared file ID contract: same length-prefixed BLAKE3 tuple as S04. Coverage
/// entries without grammar evidence retain the same file ID after reindex.
pub fn file_id(checkout: &str, rel: &str) -> String {
    let mut hash = blake3::Hasher::new();
    for part in ["file", checkout, rel] {
        hash.update(&(part.len() as u64).to_le_bytes());
        hash.update(part.as_bytes());
    }
    hash.finalize().to_hex().to_string()
}

/// Unicode casefold-by-lowercase, acronym and camel/snake boundaries. This is
/// a deterministic identifier lane, not a natural-language relevance model.
pub fn identifier_terms(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let mut term = String::new();
    for (i, &c) in chars.iter().enumerate() {
        if !c.is_alphanumeric() {
            if !term.is_empty() {
                out.push(std::mem::take(&mut term));
            }
            continue;
        }
        let boundary = c.is_uppercase()
            && i > 0
            && (chars[i - 1].is_lowercase()
                || (chars[i - 1].is_uppercase()
                    && chars.get(i + 1).is_some_and(|n| n.is_lowercase())));
        if boundary && !term.is_empty() {
            out.push(std::mem::take(&mut term));
        }
        term.extend(c.to_lowercase());
    }
    if !term.is_empty() {
        out.push(term);
    }
    out.sort();
    out.dedup();
    out
}
struct Work<'a> {
    used: usize,
    max: usize,
    cancel: &'a AtomicBool,
}
impl Work<'_> {
    fn tick(&mut self, n: usize) -> Result<(), QueryError> {
        if self.cancel.load(Ordering::Relaxed) {
            return Err(error("cancelled", "Code query cancelled"));
        }
        self.used = self
            .used
            .checked_add(n)
            .ok_or_else(|| error("budget_exceeded", "Query work budget exceeded"))?;
        if self.used > self.max {
            return Err(error("budget_exceeded", "Query work budget exceeded"));
        }
        Ok(())
    }
}
const COMMUNITY_PASSES: usize = 16;
type CommunityAdjacency = BTreeMap<String, BTreeSet<String>>;
type CommunityLabels = (BTreeMap<String, String>, usize, bool);

fn label_propagation(adjacency: &CommunityAdjacency, work: &mut Work<'_>) -> Result<CommunityLabels, QueryError> {
    let mut labels: BTreeMap<_, _> = adjacency.keys().map(|node| (node.clone(), node.clone())).collect();
    let mut iterations = 0;
    let mut converged = adjacency.is_empty();
    while iterations < COMMUNITY_PASSES && !converged {
        let mut changed = false;
        for (node, neighbors) in adjacency {
            work.tick(1 + neighbors.len())?;
            let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
            for neighbor in neighbors {
                let label = labels.get(neighbor).ok_or_else(|| error("invalid_evidence", "Community neighbor unavailable"))?;
                *counts.entry(label).or_default() += 1;
            }
            let best = counts.into_iter().max_by(|a,b|a.1.cmp(&b.1).then_with(||b.0.cmp(a.0)))
                .map(|(label,_)|label.to_owned()).ok_or_else(||error("invalid_evidence","Community neighborhood unavailable"))?;
            if labels.get(node) != Some(&best) {
                labels.insert(node.clone(),best);
                changed = true;
            }
        }
        iterations += 1;
        converged = !changed;
    }
    Ok((labels, iterations, converged))
}

/// Single-level deterministic modularity moves followed by connected
/// refinement. The objective uses unique undirected endpoint pairs, with
/// self-references retained only as reported relationship witnesses.
fn modularity_refinement(adjacency: &CommunityAdjacency, work: &mut Work<'_>) -> Result<CommunityLabels, QueryError> {
    let nodes: Vec<_> = adjacency.keys().collect();
    let positions: BTreeMap<_, _> = nodes.iter().enumerate().map(|(i,node)|(node.as_str(),i)).collect();
    let mut neighbors = Vec::with_capacity(nodes.len());
    for node in &nodes {
        let links = &adjacency[*node];
        work.tick(1 + links.len())?;
        neighbors.push(links.iter().filter(|other| *other != *node)
            .map(|other| positions[other.as_str()]).collect::<Vec<_>>());
    }
    let mut community: Vec<_> = (0..nodes.len()).collect();
    let mut degree_totals: Vec<i128> = neighbors.iter().map(|links| links.len() as i128).collect();
    let total_degree: i128 = degree_totals.iter().sum();
    let mut iterations = 0;
    let mut converged = total_degree == 0;
    while iterations < COMMUNITY_PASSES && !converged {
        converged = true;
        for (node, links) in neighbors.iter().enumerate() {
            work.tick(1 + links.len())?;
            let degree = links.len() as i128;
            let source = community[node];
            degree_totals[source] -= degree;
            let mut weights = BTreeMap::<usize, i128>::new();
            for &neighbor in links {
                *weights.entry(community[neighbor]).or_default() += 1;
            }
            work.tick(weights.len())?;
            let score = |label: usize, weight: i128| total_degree * weight - degree * degree_totals[label];
            let original = score(source, weights.get(&source).copied().unwrap_or(0));
            let mut best = (source, original);
            for (label, weight) in weights {
                let gain = score(label, weight);
                if gain > best.1 || (gain == best.1 && gain > original && label < best.0) {
                    best = (label, gain);
                }
            }
            community[node] = best.0;
            degree_totals[best.0] += degree;
            converged &= source == best.0;
        }
        iterations += 1;
    }

    // A positive move can disconnect the source group. Split each group
    // into its connected regions, naming the component by its first member.
    let mut region = vec![None; nodes.len()];
    let mut queue = VecDeque::new();
    for root in 0..nodes.len() {
        if region[root].is_some(){continue;}
        region[root] = Some(root);
        queue.push_back(root);
        while let Some(node) = queue.pop_front() {
            work.tick(1 + neighbors[node].len())?;
            for &neighbor in &neighbors[node] {
                if community[neighbor] == community[root] && region[neighbor].is_none() {
                    region[neighbor] = Some(root);
                    queue.push_back(neighbor);
                }
            }
        }
    }
    let labels = nodes.iter().enumerate().map(|(i,node)|
        (node.to_string(), nodes[region[i].unwrap_or(i)].to_string())).collect();
    Ok((labels, iterations, converged))
}

fn community_region_id(members: &[String], work: &mut Work<'_>) -> Result<String, QueryError> {
    let mut hash = blake3::Hasher::new();
    hash.update(b"openclank-code-community-region-v1\0");
    // Membership entries arrive in canonical sorted order.
    for member in members {
        work.tick(1)?;
        hash.update(&(member.len() as u64).to_le_bytes());
        hash.update(member.as_bytes());
    }
    Ok(format!("community:{}", hash.finalize().to_hex()))
}

#[derive(Clone)]
struct Candidate {
    hit: QueryHit,
    path: String,
    language: String,
    state: String,
    name: String,
    terms: Vec<String>,
}
fn matches_filter(candidate: &Candidate, filter: &QueryFilter) -> bool {
    filter
        .rel_path
        .as_ref()
        .is_none_or(|p| p == &candidate.path)
        && filter
            .path_prefix
            .as_ref()
            .is_none_or(|p| candidate.path.starts_with(p))
        && (filter.kinds.is_empty() || filter.kinds.contains(&candidate.hit.kind))
        && (filter.symbol_kinds.is_empty() || candidate.hit.properties.as_ref()
            .and_then(|p|p.get("declaration_kind")).and_then(|v|v.as_str())
            .is_some_and(|kind|filter.symbol_kinds.iter().any(|wanted|wanted==kind)))
        && (filter.languages.is_empty() || filter.languages.contains(&candidate.language))
        && (filter.states.is_empty() || filter.states.contains(&candidate.state))
}
fn base_hit(
    id: String,
    kind: &str,
    label: String,
    source: Option<SourceLocator>,
    properties: serde_json::Value,
) -> QueryHit {
    QueryHit {
        id,
        kind: kind.into(),
        label: Some(label),
        source,
        signature: None,
        documentation: None,
        occurrences: None,
        resolution: None,
        properties: Some(properties),
        excerpt: None,
        raw_source: None,
        matches: vec![],
        evidence_state: "metadata".into(),
        current_source_verified: false,
    }
}
fn candidates(
    snapshot: &QuerySnapshot,
    filter: &QueryFilter,
    work: &mut Work<'_>,
) -> Result<BTreeMap<String, Candidate>, QueryError> {
    let mut out = BTreeMap::new();
    let mut coverage = BTreeMap::new();
    for row in &snapshot.coverage {
        work.tick(1)?;
        coverage.insert(row.rel_path.as_str(), row);
    }
    for file in &snapshot.files {
        work.tick(1)?;
        let status = coverage
            .get(file.rel_path.as_str())
            .map_or("indexed", |c| c.status.as_str());
        let props = serde_json::json!({"rel_path":file.rel_path,"language":file.language,"state":status,"source_hash":file.source_hash,"capability":file.capability});
        let hit = base_hit(
            format!("file:{}", file.file_id),
            "file",
            file.rel_path.clone(),
            None,
            props,
        );
        let c = Candidate {
            hit,
            path: file.rel_path.clone(),
            language: file.language.clone(),
            state: status.into(),
            name: file.rel_path.clone(),
            terms: identifier_terms(&file.rel_path),
        };
        if matches_filter(&c, filter) {
            out.insert(c.hit.id.clone(), c);
        }
        for decl in &file.declarations {
            work.tick(1)?;
            let mut hit = base_hit(
                format!("decl:{}", decl.id),
                "declaration",
                decl.qualified_name.clone(),
                Some(decl.source.clone()),
                serde_json::json!({"rel_path":file.rel_path,"language":file.language,"declaration_kind":decl.kind,"identity_state":decl.identity_state,"conditional":decl.conditional,"source_hash":file.source_hash}),
            );
            if file.language=="python" && decl.kind=="function" {
                let profile=file.local_value_slices.as_ref().and_then(|v|v.iter().find(|p|p.function_id==decl.id));
                let data=match profile {Some(p) if p.status=="complete"=>serde_json::json!({"version":1,"status":"available","seed":p.return_occurrence_id.as_ref().map(|id|format!("occ:{id}"))}),Some(_)=>serde_json::json!({"version":1,"status":"unsupported"}),None=>serde_json::json!({"version":1,"status":"unavailable"})};
                if let Some(props)=hit.properties.as_mut().and_then(|v|v.as_object_mut()){props.insert("backwardSlice".into(),data);}
            }
            hit.signature = Some(decl.signature.clone());
            hit.documentation = serde_json::to_value(&decl.documentation)
                .ok()
                .filter(|v| !v.is_null());
            let c = Candidate {
                hit,
                path: file.rel_path.clone(),
                language: file.language.clone(),
                state: "metadata".into(),
                name: decl.name.clone(),
                terms: identifier_terms(&decl.qualified_name),
            };
            if matches_filter(&c, filter) {
                out.insert(c.hit.id.clone(), c);
            }
        }
        for occ in &file.occurrences {
            work.tick(1)?;
            let hit = base_hit(
                format!("occ:{}", occ.id),
                "occurrence",
                occ.symbol_text.clone(),
                Some(occ.source.clone()),
                serde_json::json!({"rel_path":file.rel_path,"language":file.language,"occurrence_kind":occ.kind,"caller_id":occ.caller_id,"source_hash":file.source_hash}),
            );
            let c = Candidate {
                hit,
                path: file.rel_path.clone(),
                language: file.language.clone(),
                state: "metadata".into(),
                name: occ.symbol_text.clone(),
                terms: identifier_terms(&occ.symbol_text),
            };
            if matches_filter(&c, filter) {
                out.insert(c.hit.id.clone(), c);
            }
        }
    }
    for row in &snapshot.coverage {
        work.tick(1)?;
        let id = format!(
            "file:{}",
            file_id(&snapshot.receipt.evidence.checkout_id, &row.rel_path)
        );
        if out.contains_key(&id) {
            continue;
        }
        let hit = base_hit(
            id,
            "file",
            row.rel_path.clone(),
            None,
            serde_json::json!({"rel_path":row.rel_path,"state":row.status,"source_hash":row.source_hash,"source_generation":row.source_generation}),
        );
        let c = Candidate {
            hit,
            path: row.rel_path.clone(),
            language: String::new(),
            state: row.status.clone(),
            name: row.rel_path.clone(),
            terms: identifier_terms(&row.rel_path),
        };
        if matches_filter(&c, filter) {
            out.insert(c.hit.id.clone(), c);
        }
    }
    Ok(out)
}
fn edit_one(a: &str, b: &str, work: &mut Work<'_>) -> Result<bool, QueryError> {
    let a: Vec<_> = a.chars().collect();
    let b: Vec<_> = b.chars().collect();
    work.tick(a.len() + b.len())?;
    if a.len() < 4 || a.len().abs_diff(b.len()) > 1 || a.first() != b.first() {
        return Ok(false);
    }
    let (mut i, mut j, mut edits) = (0, 0, 0);
    while i < a.len() && j < b.len() {
        work.tick(1)?;
        if a[i] == b[j] {
            i += 1;
            j += 1;
            continue;
        }
        edits += 1;
        if edits > 1 {
            return Ok(false);
        }
        if a.len() <= b.len() {
            j += 1;
        }
        if a.len() >= b.len() {
            i += 1;
        }
    }
    Ok(edits + usize::from(i < a.len() || j < b.len()) == 1)
}

/// Candidate-key admission for the private disk index. Roles are the union of
/// literal name/label/path (1), segmented identifier (2), and fuzzy name (4).
/// This is only a prefilter: `find` remains the lane/ranking authority after
/// materialization, including suppressing fuzzy when another lane matched.
pub(crate) fn index_key_match(
    text: &str,
    key: &str,
    roles: i64,
    channels: &[Channel],
    max_work: usize,
) -> Result<(bool, usize), QueryError> {
    if roles <= 0 || roles & !7 != 0 || max_work > MAX_QUERY_WORK {
        return Err(error("invalid", "Invalid bounded index-key request"));
    }
    let cancel = AtomicBool::new(false);
    let mut work = Work { used: 0, max: max_work, cancel: &cancel };
    // Charge bytes before allocating Unicode segmentation/lowercase buffers.
    work.tick(1)?;
    work.tick(text.len())?;
    work.tick(key.len())?;
    if text.is_empty() {
        return Ok((false, work.used));
    }
    let mut matched = roles & 1 != 0 && channels.contains(&Channel::Exact) && text == key;
    if !matched && roles & 2 != 0 && channels.contains(&Channel::Identifier) {
        let terms = identifier_terms(text);
        let key_terms = identifier_terms(key);
        matched = !terms.is_empty() && terms.iter().all(|term| key_terms.binary_search(term).is_ok());
    }
    if !matched && roles & 4 != 0 && channels.contains(&Channel::Fuzzy) {
        matched = edit_one(&text.to_lowercase(), &key.to_lowercase(), &mut work)?;
    }
    Ok((matched, work.used))
}
fn project(hit: &mut QueryHit, fields: &[Field]) {
    if !fields.contains(&Field::Label) {
        hit.label = None;
    }
    if !fields.contains(&Field::Source) {
        hit.source = None;
    }
    if !fields.contains(&Field::Signature) {
        hit.signature = None;
    }
    if !fields.contains(&Field::Documentation) {
        hit.documentation = None;
    }
    if !fields.contains(&Field::Occurrences) {
        hit.occurrences = None;
    }
    if !fields.contains(&Field::Resolution) {
        hit.resolution = None;
    }
    if !fields.contains(&Field::Properties) {
        hit.properties = None;
    }
    if !fields.contains(&Field::Excerpt) {
        hit.excerpt = None;
        hit.raw_source = None;
    }
    // Documentation carries a locator; source omission applies recursively.
    if !fields.contains(&Field::Source) {
        if let Some(doc) = hit.documentation.as_mut().and_then(|v| v.as_object_mut()) {
            doc.remove("source");
        }
    }
    if let Some(resolution) = hit.resolution.as_mut().and_then(|v| v.as_object_mut()) {
        if !fields.contains(&Field::Source) {
            resolution.remove("source");
        }
        if !fields.contains(&Field::Witnesses) {
            resolution.remove("witnesses");
        } else if !fields.contains(&Field::Source) {
            if let Some(witnesses) = resolution
                .get_mut("witnesses")
                .and_then(|v| v.as_array_mut())
            {
                for witness in witnesses {
                    if let Some(w) = witness.as_object_mut() {
                        w.remove("source");
                    }
                }
            }
        }
    }
}
fn project_edge(edge: &mut QueryEdge, fields: &[Field]) {
    if !fields.contains(&Field::Source) {
        edge.source = None;
    }
    if !fields.contains(&Field::Witnesses) {
        edge.witnesses = None;
    } else if !fields.contains(&Field::Source) {
        if let Some(w) = &mut edge.witnesses {
            for v in w {
                if let Some(o) = v.as_object_mut() {
                    o.remove("source");
                }
            }
        }
    }
}
fn cursor_fingerprint(snapshot: &QuerySnapshot, query: &CodeQuery) -> String {
    let mut q = query.clone();
    q.cursor = None;
    // The actual receipt is already bound below and any explicit expectation
    // is checked before decoding. A first page commonly has no expectation;
    // Graph then supplies that returned receipt with Load more.
    q.expected_receipt = None;
    blake3::hash(
        &serde_json::to_vec(&(
            &snapshot.scope_key,
            &snapshot.receipt,
            query_fingerprint(),
            q,
        ))
        .unwrap(),
    )
    .to_hex()
    .to_string()
}
fn encode_cursor(fingerprint: &str, offset: usize, key: &[u8; 32]) -> String {
    let body = format!("{fingerprint}:{offset}");
    format!(
        "{}:{}",
        body,
        blake3::keyed_hash(key, body.as_bytes()).to_hex()
    )
}
fn decode_cursor(cursor: &str, fingerprint: &str, key: &[u8; 32]) -> Result<usize, QueryError> {
    let invalid = || error("stale_cursor", "Invalid or stale code continuation");
    let (body, mac) = cursor.rsplit_once(':').ok_or_else(invalid)?;
    let (fp, n) = body.split_once(':').ok_or_else(invalid)?;
    let actual = blake3::keyed_hash(key, body.as_bytes())
        .to_hex()
        .to_string();
    if fp != fingerprint
        || mac.len() != actual.len()
        || mac
            .bytes()
            .zip(actual.bytes())
            .fold(0u8, |v, (a, b)| v | (a ^ b))
            != 0
    {
        return Err(invalid());
    }
    n.parse().map_err(|_| invalid())
}
fn measured(result: &mut CodeQueryResult) -> Result<usize, QueryError> {
    let mut timing = crate::code_metrics::span(crate::code_metrics::Stage::Serialization);
    for _ in 0..12 {
        let n = serde_json::to_vec(result)
            .map_err(|_| error("unavailable", "Cannot serialize code result"))?
            .len();
        timing.add(1, n);
        if n == result.cost.serialized_bytes && n == result.cost.token_upper_bound {
            return Ok(n);
        }
        result.cost.serialized_bytes = n;
        result.cost.token_upper_bound = n;
    }
    Err(error("unavailable", "Unstable code envelope size"))
}

/// Metadata query only. Files windows and independent current-source
/// validation are added by the trusted Python read owner, never inferred here.
pub fn run_query(
    snapshot: &QuerySnapshot,
    query: &CodeQuery,
    cancel: &AtomicBool,
    cursor_secret: &[u8; 32],
) -> Result<CodeQueryResult, QueryError> {
    let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Projection);
    validate_query(query)?;
    if query
        .expected_receipt
        .as_ref()
        .is_some_and(|r| r != &snapshot.receipt)
    {
        return Err(error("stale_cursor", "Code query receipt changed"));
    }
    let mut work = Work {
        used: 0,
        max: query.budgets.work,
        cancel,
    };
    work.tick(snapshot.metadata_records)?;
    let fingerprint = cursor_fingerprint(snapshot, query);
    let offset = query
        .cursor
        .as_ref()
        .map(|v| decode_cursor(v, &fingerprint, cursor_secret))
        .transpose()?
        .unwrap_or(0);
    let mut result = CodeQueryResult {
        schema: QUERY_SCHEMA.into(),
        operation: query.operation,
        receipt: Some(snapshot.receipt.clone()),
        capabilities: vec![],
        channels: vec![],
        operation_capabilities: vec![],
        source_window: None,
        outline_completeness: None,
        hits: vec![],
        edges: vec![],
        paths: vec![],
        analysis: None,
        community_analysis: None,
        slice_analysis: None,
        error_boundary_analysis: None,
        counts: vec![],
        facets: serde_json::json!({}),
        omissions: vec![],
        partial: false,
        degraded: false,
        reasons: vec![],
        next_cursor: None,
        error: None,
        cost: QueryCost {
            metadata_bytes: snapshot.metadata_bytes,
            metadata_records_validated: snapshot.metadata_records,
            ..Default::default()
        },
    };
    for operation in [
        Operation::Capabilities,
        Operation::Search,
        Operation::Overview,
        Operation::Catalog,
        Operation::Facets,
        Operation::Outline,
        Operation::Inspect,
        Operation::Neighbors,
        Operation::Path,
        Operation::Impact,
        Operation::ImportAnalysis,
        Operation::DeadCodeCandidates,
        Operation::GraphCommunities,
        Operation::StructuralTwins,
        Operation::BackwardSlice,
        Operation::ErrorBoundaries,
        Operation::Read,
        Operation::Pipeline,
    ] {
        result.operation_capabilities.push(OperationCapability {
            operation,
            available: !matches!(operation, Operation::Read | Operation::Pipeline),
            reason: if operation == Operation::Read {
                Some("authorized_files_owner_required".into())
            } else if operation == Operation::Pipeline {
                Some("optional_not_admitted".into())
            } else {
                None
            },
        });
    }
    for channel in [
        Channel::Exact,
        Channel::Identifier,
        Channel::Fuzzy,
        Channel::Lexical,
        Channel::Structural,
        Channel::Raw,
        Channel::Semantic,
    ] {
        let available = !matches!(channel, Channel::Raw | Channel::Semantic);
        let reason = if channel == Channel::Raw {
            Some("authorized_files_owner_required".into())
        } else if channel == Channel::Semantic {
            Some("configured_semantic_producer_not_admitted".into())
        } else {
            None
        };
        result.capabilities.push(ChannelStatus {
            channel,
            available,
            reason: reason.clone(),
        });
        let requested = query.channels.contains(&channel);
        if requested && !available {
            result.degraded = true;
            result.reasons.push(reason.unwrap());
        }
        result.channels.push(ChannelExecution {
            channel,
            requested,
            enabled: available,
            executed: false,
            cancelled: false,
            error: None,
            producer: available.then(|| "s05-deterministic-1".into()),
            config_fingerprint: available.then(query_fingerprint),
            candidate_count: None,
            inspected_records: 0,
            elapsed_micros: None,
        });
    }
    for field in [
        Field::Label,
        Field::Source,
        Field::Signature,
        Field::Documentation,
        Field::Occurrences,
        Field::Resolution,
        Field::Properties,
        Field::Excerpt,
        Field::Witnesses,
    ] {
        if !query.fields.contains(&field) && !(query.operation == Operation::StructuralTwins && matches!(field, Field::Label | Field::Source | Field::Properties)) {
            result.omissions.push(Omission {
                field,
                reason: "not_requested".into(),
            });
        }
    }
    if query.operation == Operation::Capabilities {
        return finish(result, query);
    }
    let mut prefilter = query.filter.clone();
    prefilter.states.clear();
    let mut all = candidates(snapshot, &prefilter, &mut work)?;
    let mut edges = Vec::new();
    let mut occurrence_kinds = BTreeMap::new();
    for file in &snapshot.files {
        for occurrence in &file.occurrences {
            work.tick(1)?;
            occurrence_kinds.insert(occurrence.id.as_str(), (crate::code_evidence::relation_name(occurrence), file.file_id.as_str(), matches!(occurrence.kind, crate::code_evidence::OccurrenceKind::ImportReference) || matches!(occurrence.syntax_kind.as_str(),"sql_table_reference"|"callback_registration"|"js_event_dispatch")));
        }
    }
    for resolution in &snapshot.resolutions {
        work.tick(1)?;
        let occ = format!("occ:{}", resolution.occurrence_id);
        if let Some(c) = all.get_mut(&occ) {
            c.hit.evidence_state = serde_json::to_value(&resolution.state)
                .unwrap()
                .as_str()
                .unwrap()
                .into();
            c.state = c.hit.evidence_state.clone();
            c.hit.resolution = Some(serde_json::to_value(resolution).unwrap());
        }
        if let Some(target) = &resolution.target_id {
            let (relation, file_id, is_import) = occurrence_kinds.get(resolution.occurrence_id.as_str()).ok_or_else(|| error("invalid_evidence", "Missing typed occurrence"))?;
            let from = if let Some(caller) = &resolution.caller_id { format!("decl:{caller}") } else if *is_import { format!("file:{file_id}") } else { continue };
            let to = format!("decl:{target}");
            if all.contains_key(&from)
                && all.contains_key(&to)
                && matches!(
                    resolution.state,
                    ResolutionState::Exact | ResolutionState::Inferred
                )
            {
                if !query.filter.relation_kinds.is_empty() && !query.filter.relation_kinds.iter().any(|wanted|wanted==*relation) {continue;}
                edges.push(QueryEdge {
                    from,
                    to,
                    occurrence_id: occ.clone(),
                    relation_kind: relation.to_string(),
                    resolution_state: serde_json::to_value(&resolution.state)
                        .unwrap()
                        .as_str()
                        .unwrap()
                        .into(),
                    source: Some(resolution.source.clone()),
                    witnesses: Some(
                        resolution
                            .witnesses
                            .iter()
                            .map(|v| serde_json::to_value(v).unwrap())
                            .collect(),
                    ),
                });
            }
        }
    }
    all.retain(|_, c| matches_filter(c, &query.filter));
    edges.retain(|e| all.contains_key(&e.from) && all.contains_key(&e.to));
    edges.sort_by(|a, b| {
        (&a.from, &a.to, &a.occurrence_id).cmp(&(&b.from, &b.to, &b.occurrence_id))
    });
    let eligible = snapshot.eligible_records.unwrap_or(all.len());
    if eligible < all.len() {
        return Err(error("invalid", "Indexed eligible count is smaller than its candidate set"));
    }
    let mut selected = BTreeSet::new();
    let mut analysis_counts:Vec<QueryCount>=Vec::new();
    match query.operation {
        Operation::Search => {
            selected = find(
                &mut all,
                query.query.as_deref().unwrap_or(""),
                &query.channels,
                &mut work,
            )?;
        }
        Operation::Catalog | Operation::Overview | Operation::Facets => {
            for (id, c) in &all {
                work.tick(1)?;
                if c.hit.kind == "file" {
                    selected.insert(id.clone());
                }
            }
        }
        Operation::Outline | Operation::Read => {
            let mut selected_paths = BTreeSet::new();
            for id in &query.seed_ids {
                work.tick(1)?;
                let c = all
                    .get(id)
                    .ok_or_else(|| error("missing", "Selected code resource is unavailable"))?;
                selected_paths.insert(c.path.clone());
            }
            for (id, c) in &all {
                work.tick(1)?;
                if (selected_paths.is_empty() || selected_paths.contains(&c.path))
                    && (c.hit.kind == "declaration" || query.operation == Operation::Read)
                {
                    selected.insert(id.clone());
                }
            }
        }
        Operation::Inspect => {
            for id in &query.seed_ids {
                work.tick(1)?;
                let candidate = all.get_mut(id)
                    .ok_or_else(|| error("missing", "Selected code resource is unavailable"))?;
                selected.insert(id.clone());
                if query.fields.contains(&Field::Properties) && query.fields.contains(&Field::Source) {
                    if let Some(declaration_id) = id.strip_prefix("decl:") {
                        for file in snapshot.files.iter().filter(|file| file.rel_path == candidate.path && file.language == "python") {
                            work.tick(file.declarations.len())?;
                            if let Some(declaration) = file.declarations.iter().find(|d| d.id == declaration_id) {
                                if matches!(declaration.kind.as_str(), "function" | "method" | "route") {
                                    let unavailable = || crate::code_evidence::function_flow::FunctionFlow::unavailable("indexed_profile_unavailable");
                                    let flow = declaration.function_flow.clone().unwrap_or_else(unavailable);
                                    work.tick(flow.records())?;
                                    if let Some(props) = candidate.hit.properties.as_mut().and_then(|value| value.as_object_mut()) {
                                        props.insert("functionFlow".into(), serde_json::to_value(flow).map_err(|_| error("internal", "Function flow encoding failed"))?);
                                    }
                                }
                                break;
                            }
                        }
                    }
                }
            }
        }
        Operation::Neighbors | Operation::Impact | Operation::Path => {
            let hops = if query.operation == Operation::Neighbors {
                1
            } else {
                query.budgets.hops
            };
            let direction = query
                .direction
                .unwrap_or(if query.operation == Operation::Impact {
                    Direction::Incoming
                } else {
                    Direction::Outgoing
                });
            if query.operation == Operation::Path && direction != Direction::Outgoing {
                return Err(error(
                    "unsupported",
                    "Directed paths require outgoing direction",
                ));
            }
            let mut adjacency: BTreeMap<&str, Vec<(&QueryEdge, &str)>> = BTreeMap::new();
            for edge in &edges {
                work.tick(1)?;
                if direction != Direction::Incoming {
                    adjacency
                        .entry(&edge.from)
                        .or_default()
                        .push((edge, &edge.to));
                }
                if direction != Direction::Outgoing {
                    adjacency
                        .entry(&edge.to)
                        .or_default()
                        .push((edge, &edge.from));
                }
            }
            let mut queue = VecDeque::new();
            let mut visited = BTreeSet::new();
            for seed in &query.seed_ids {
                if !all.contains_key(seed) {
                    return Err(error("missing", "Selected code resource is unavailable"));
                }
                visited.insert(seed.clone());
                queue.push_back((seed.clone(), Vec::<QueryEdge>::new()));
            }
            while let Some((id, path)) = queue.pop_front() {
                work.tick(1)?;
                selected.insert(id.clone());
                if query.operation == Operation::Path && query.target_id.as_ref() == Some(&id) {
                    result.paths.push(QueryPath {
                        node_ids: std::iter::once(
                            path.first().map_or(id.clone(), |e| e.from.clone()),
                        )
                        .chain(path.iter().map(|e| e.to.clone()))
                        .collect(),
                        edges: path,
                    });
                    break;
                }
                if path.len() >= hops {
                    continue;
                }
                for (edge, next_id) in adjacency.get(id.as_str()).into_iter().flatten() {
                    work.tick(1)?;
                    result.edges.push((*edge).clone());
                    if visited.insert((*next_id).into()) {
                        let mut next = path.clone();
                        next.push((*edge).clone());
                        queue.push_back(((*next_id).into(), next));
                    }
                }
            }
            if query.operation == Operation::Impact {
                let seed_files: BTreeSet<_> = query.seed_ids.iter().map(|id| all[id].path.as_str()).collect();
                let mut reached_files = BTreeSet::new();
                for id in &selected {
                    work.tick(1)?;
                    reached_files.insert(all[id].path.as_str());
                }
                let additional_files = reached_files.difference(&seed_files).count();
                for (name, value) in [
                    ("impact_seed_files", seed_files.len()),
                    ("impact_reached_files", reached_files.len()),
                    ("impact_additional_files", additional_files),
                ] {
                    analysis_counts.push(QueryCount {
                        name: name.into(),
                        value: Some(value),
                        unit: "file_in_bounded_indexed_traversal".into(),
                        exactness: "exact".into(),
                    });
                }
                // Optional structural facts are scoped to the same typed,
                // filtered and hop-bounded adjacency already traversed.
                let estimate = (selected.len().saturating_add(result.edges.len()))
                    .saturating_mul(20);
                if estimate <= work.max.saturating_sub(work.used) {
                    let mut pairs = BTreeMap::<(String, String), BTreeSet<String>>::new();
                    for edge in &result.edges {
                        work.tick(1)?;
                        pairs.entry((edge.from.clone(), edge.to.clone())).or_default()
                            .insert(edge.occurrence_id.clone());
                    }
                    let (cycles, degrees) = import_analysis::analyze(&selected, &pairs, |n| work.tick(n))?;
                    let bridges = import_analysis::bridges(
                        pairs.keys().map(|(from, to)| (from.as_str(), to.as_str())), |n| work.tick(n),
                    )?;
                    for (name, value, unit) in [
                        ("impact_cyclic_components", cycles.len(), "directed_cyclic_component"),
                        ("impact_undirected_bridges", bridges.len(), "bridge_in_simple_undirected_projection"),
                    ] {
                        analysis_counts.push(QueryCount {name:name.into(), value:Some(value),
                            unit:unit.into(), exactness:"exact".into()});
                    }
                    if query.fields.contains(&Field::Properties) {
                        let cyclic: BTreeSet<_> = cycles.iter().flat_map(|cycle| cycle.nodes.iter()).collect();
                        let mut bridge_degree = BTreeMap::new();
                        for (a, b) in bridges {
                            work.tick(1)?;
                            *bridge_degree.entry(a).or_insert(0usize) += 1;
                            *bridge_degree.entry(b).or_insert(0usize) += 1;
                        }
                        for hub in degrees {
                            work.tick(1)?;
                            if let Some(props) = all.get_mut(&hub.node).and_then(|candidate|
                                candidate.hit.properties.as_mut().and_then(|value| value.as_object_mut()))
                            {
                                props.insert("impactTopology".into(), serde_json::json!({
                                    "incoming":hub.incoming,"outgoing":hub.outgoing,
                                    "cyclic":cyclic.contains(&hub.node),
                                    "undirected_bridges":bridge_degree.get(hub.node.as_str()).copied().unwrap_or(0),
                                    "scope":"filtered_hop_bounded_indexed_traversal",
                                }));
                            }
                        }
                    }
                } else {
                    result.reasons.push("impact_topology_work_budget".into());
                }
            }
            if query.operation == Operation::Path && result.paths.is_empty() {
                result
                    .reasons
                    .push("no_path_within_hop_and_work_bounds".into());
            }
        }
        Operation::ErrorBoundaries => {
            let id=query.seed_ids[0].strip_prefix("decl:").ok_or_else(||error("invalid","Invalid function seed"))?;
            let file=snapshot.files.iter().find(|f|f.declarations.iter().any(|d|d.id==id)).ok_or_else(||error("missing","Function unavailable"))?;
            let function=file.declarations.iter().find(|d|d.id==id).ok_or_else(||error("missing","Function unavailable"))?;
            let profile=file.error_boundaries.as_ref().and_then(|v|v.iter().find(|p|p.function_id==id && p.status=="complete")).ok_or_else(||error("unsupported","Complete error boundary evidence unavailable"))?;
            work.tick(profile.regions.len())?;
            result.error_boundary_analysis=Some(ErrorBoundaryAnalysis{version:1,profile:"python_try_except_v1".into(),function_id:id.into(),source:function.source.clone(),regions:profile.regions.clone()});
            selected.insert(query.seed_ids[0].clone());
            result.reasons.push("syntactic_error_boundaries_only".into());
        }
        Operation::BackwardSlice => {
            let seed=query.seed_ids[0].strip_prefix("occ:").ok_or_else(||error("invalid","Invalid slice seed"))?;
            let file=snapshot.files.iter().find(|f|f.occurrences.iter().any(|o|o.id==seed)).ok_or_else(||error("missing","Slice seed unavailable"))?;
            let profile=file.local_value_slices.as_ref().and_then(|v|v.iter().find(|p|p.return_occurrence_id.as_deref()==Some(seed) && p.status=="complete")).ok_or_else(||error("unsupported","No complete supported local slice for this occurrence"))?;
            let function=file.declarations.iter().find(|d|d.id==profile.function_id).ok_or_else(||error("invalid_evidence","Slice function unavailable"))?;
            let seed_occ=file.occurrences.iter().find(|o|o.id==seed && o.caller_id.as_deref()==Some(function.id.as_str())).ok_or_else(||error("invalid_evidence","Slice occurrence unavailable"))?;
            let root=profile.root_definition_id.as_ref().ok_or_else(||error("invalid_evidence","Slice root unavailable"))?;
            let defs:BTreeMap<_,_>=profile.definitions.iter().map(|d|(d.id.as_str(),d)).collect();
            let mut queue=VecDeque::from([(root.as_str(),0usize)]);let mut seen=BTreeSet::new();let mut units=Vec::new();
            while let Some((id,depth))=queue.pop_front(){work.tick(1)?;if !seen.insert(id.to_owned()){continue;}let def=defs.get(id).ok_or_else(||error("invalid_evidence","Missing local definition"))?;work.tick(def.uses.len())?;if depth>=query.budgets.hops && !def.uses.is_empty(){return Err(error("budget_exceeded","Slice dependency depth exceeds hop budget"));}for use_ in &def.uses{queue.push_back((&use_.definition_id,depth+1));}units.push((*def).clone());}
            result.slice_analysis=Some(SliceAnalysis{version:1,profile:"python_straight_line_v1".into(),function_id:function.id.clone(),seed_occurrence_id:seed.into(),source:function.source.clone(),return_span:seed_occ.source.span.clone(),root_definition_id:root.clone(),units});
            selected.insert(query.seed_ids[0].clone());
            result.reasons.push("syntactic_straight_line_only".into());
        }
        Operation::StructuralTwins => {
            let seed_id = query.seed_ids[0].strip_prefix("decl:").unwrap();
            let mut seed = None;
            for file in &snapshot.files {
                for declaration in &file.declarations {
                    work.tick(1)?;
                    if declaration.id == seed_id { seed = Some((file, declaration)); }
                }
            }
            let (seed_file, seed_decl) = seed.ok_or_else(|| error("missing", "Selected declaration is unavailable"))?;
            let shape = seed_decl.structural_shape.as_ref().filter(|s| s.version == 1 && s.status == "complete")
                .ok_or_else(|| error("unavailable", "Selected declaration has no complete structural shape"))?;
            let mut skipped = 0usize;
            let mut other_dialect = 0usize;
            let mut twins = 0usize;
            for file in &snapshot.files {
                for declaration in &file.declarations {
                    work.tick(1)?;
                    if !matches!(declaration.kind.as_str(), "function" | "method" | "function_value" | "route" | "component") { continue; }
                    if file.capability.dialect != seed_file.capability.dialect || file.capability.grammar_version != seed_file.capability.grammar_version { other_dialect += 1; continue; }
                    let Some(candidate) = declaration.structural_shape.as_ref() else { skipped += 1; continue; };
                    if candidate.version != 1 || candidate.status != "complete" { skipped += 1; continue; }
                    if candidate.sha256 != shape.sha256 { continue; }
                    work.tick(candidate.nodes.len().saturating_add(shape.nodes.len()))?;
                    if candidate.nodes != shape.nodes { continue; }
                    let id = format!("decl:{}", declaration.id);
                    let Some(item) = all.get_mut(&id) else { return Err(error("invalid_evidence", "Structural declaration missing from admitted snapshot")); };
                    let mut witnesses = Vec::new();
                    for occurrence in &file.occurrences {
                        work.tick(1)?;
                        if occurrence.caller_id.as_deref() == Some(declaration.id.as_str()) && candidate.body.as_ref().is_some_and(|body|
                            occurrence.source.span.start_byte >= body.start_byte && occurrence.source.span.end_byte <= body.end_byte) {
                            if witnesses.len() >= 200 { return Err(error("budget_exceeded", "Structural member exceeds complete witness limit")); }
                            witnesses.push(serde_json::json!({"occurrenceID":occurrence.id,"kind":occurrence.kind,"syntaxKind":occurrence.syntax_kind,"span":occurrence.source.span}));
                        }
                    }
                    witnesses.sort_by(|a,b| a["occurrenceID"].as_str().cmp(&b["occurrenceID"].as_str()));
                    if let Some(props) = item.hit.properties.as_mut().and_then(|v|v.as_object_mut()) {
                        props.insert("structuralTwin".into(),serde_json::json!({"version":1,"role":if declaration.id == seed_id {"seed"} else {"candidate"},"dialect":file.capability.dialect,"grammarVersion":file.capability.grammar_version,"shapeSHA256":candidate.sha256,"occurrences":witnesses}));
                    }
                    selected.insert(id);
                    if declaration.id != seed_id { twins += 1; }
                }
            }
            if skipped > 0 { result.reasons.push("structural_shape_coverage_partial".into()); }
            if other_dialect > 0 { result.reasons.push("structural_other_dialects_not_compared".into()); }
            result.reasons.push("structural_twins_heuristic_not_semantic_equivalence".into());
            result.reasons.push("structural_occurrence_spans_inherit_declaration_source".into());
            analysis_counts.push(QueryCount {name:"structural_candidates".into(),value:Some(twins),unit:"indexed_complete_shape_declaration".into(),exactness:"exact".into()});
            analysis_counts.push(QueryCount {name:"structural_other_dialects".into(),value:Some(other_dialect),unit:"indexed_declaration".into(),exactness:"exact".into()});
            analysis_counts.push(QueryCount {name:"structural_shapes_unavailable".into(),value:Some(skipped),unit:"indexed_declaration".into(),exactness:"exact".into()});
        }
        Operation::GraphCommunities => {
            let mut adjacency:BTreeMap<String,BTreeSet<String>>=BTreeMap::new();
            let mut exact_edges=Vec::new();
            for edge in &edges {
                work.tick(1)?;
                if edge.resolution_state!="exact" {continue;}
                if edge.source.is_none() || edge.witnesses.as_ref().is_none_or(Vec::is_empty) {continue;}
                adjacency.entry(edge.from.clone()).or_default().insert(edge.to.clone());
                adjacency.entry(edge.to.clone()).or_default().insert(edge.from.clone());
                exact_edges.push(edge.clone());
            }
            let declarations=all.values().filter(|item|item.hit.kind=="declaration").count();
            let isolates=declarations.saturating_sub(adjacency.len());
            let (algorithm, (labels, iterations, converged)) = match query.community_algorithm.unwrap_or_default() {
                CommunityAlgorithm::LabelPropagation => ("exact_witness_label_propagation_v1", label_propagation(&adjacency, &mut work)?),
                CommunityAlgorithm::ModularityRefinement => ("exact_witness_modularity_refinement_v1", modularity_refinement(&adjacency, &mut work)?),
            };
            let mut grouped:BTreeMap<String,Vec<String>>=BTreeMap::new();
            for (node,label) in &labels {work.tick(1)?;grouped.entry(label.clone()).or_default().push(node.clone());}
            let eligible_occurrences:BTreeSet<String>=exact_edges.iter().map(|edge|edge.occurrence_id.trim_start_matches("occ:").to_owned()).collect();
            work.tick(exact_edges.len())?;
            let mut internal:BTreeMap<String,Vec<QueryEdge>>=BTreeMap::new();let mut cross=0usize;
            for edge in exact_edges {
                work.tick(1)?;
                let left=labels.get(&edge.from).ok_or_else(||error("invalid_evidence","Community endpoint unavailable"))?;
                let right=labels.get(&edge.to).ok_or_else(||error("invalid_evidence","Community endpoint unavailable"))?;
                if left==right {internal.entry(left.clone()).or_default().push(edge);} else {cross+=1;}
            }
            let mut groups=Vec::new();
            for (label,members) in grouped {
                work.tick(1)?;let mut witnesses=internal.remove(&label).unwrap_or_default();
                witnesses.sort_by(|a,b|(&a.from,&a.to,&a.occurrence_id).cmp(&(&b.from,&b.to,&b.occurrence_id)));
                if members.len()>200 || witnesses.len()>200 {return Err(error("budget_exceeded","Community exceeds complete member or witness limit"));}
                let region_id=community_region_id(&members,&mut work)?;
                groups.push(GraphCommunity {members,edges:witnesses,region_id});
            }
            let observed:BTreeSet<&str>=snapshot.resolutions.iter().map(|item|item.occurrence_id.as_str()).collect();
            let (mut unresolved,mut ambiguous,mut inferred,mut unsupported,mut excluded_exact)=(0usize,0usize,0usize,0usize,0usize);
            for item in &snapshot.resolutions {
                work.tick(1)?;
                match item.state {
                    ResolutionState::Exact if !eligible_occurrences.contains(&item.occurrence_id)=>excluded_exact+=1,
                    ResolutionState::Exact=>{},ResolutionState::Inferred=>inferred+=1,ResolutionState::Ambiguous=>ambiguous+=1,
                    ResolutionState::Unresolved=>unresolved+=1,ResolutionState::Unsupported=>unsupported+=1,
                }
            }
            let mut missing=0usize;let mut unavailable_languages=0usize;let mut incomplete_file_coverage=0usize;
            for file in &snapshot.files {work.tick(1)?;if !file.capability.available {unavailable_languages+=1;}
                for occurrence in &file.occurrences {work.tick(1)?;if !observed.contains(occurrence.id.as_str()){missing+=1;}}}
            for coverage in &snapshot.coverage {work.tick(1)?;if !matches!(coverage.status.as_str(),"indexed"|"unchanged"|"ignored"|"deleted"|"no_units"|"no_source_units") {incomplete_file_coverage+=1;}}
            let uncertain=unresolved+ambiguous+inferred+unsupported+excluded_exact;
            result.community_analysis=Some(CommunityAnalysis {algorithm:algorithm.into(),iterations,converged,isolates,
                omitted_groups:0,cross_group_edges:cross,ignored_uncertain:uncertain,unresolved_resolutions:unresolved,ambiguous_resolutions:ambiguous,
                inferred_resolutions:inferred,unsupported_resolutions:unsupported,excluded_exact_resolutions:excluded_exact,
                missing_resolutions:missing,unavailable_languages,incomplete_file_coverage,groups});
            result.reasons.push("community_heuristic_not_architectural_proof".into());
            result.reasons.push("dynamic_or_unindexed_references_unknown".into());
            if !converged {result.reasons.push("community_iteration_limit".into());}
        }
        Operation::DeadCodeCandidates => {
            // Negative evidence is scoped to this complete admitted snapshot.
            // Dynamic/unresolved references can still target any candidate.
            let mut incoming=BTreeSet::new();
            let mut possible:BTreeMap<&str,usize>=BTreeMap::new();
            let (mut ambiguous,mut unresolved,mut unsupported,mut missing_target)=(0usize,0usize,0usize,0usize);
            let mut observed=BTreeSet::new();
            for resolution in &snapshot.resolutions {
                work.tick(1)?;observed.insert(resolution.occurrence_id.as_str());
                match resolution.state {
                    ResolutionState::Exact | ResolutionState::Inferred => {
                        if let Some(target)=resolution.target_id.as_deref(){incoming.insert(target);}
                        else {missing_target+=1;}
                    },
                    ResolutionState::Ambiguous | ResolutionState::Unresolved | ResolutionState::Unsupported => {
                        match resolution.state {ResolutionState::Ambiguous=>ambiguous+=1,ResolutionState::Unresolved=>unresolved+=1,ResolutionState::Unsupported=>unsupported+=1,_=>unreachable!()}
                        for candidate in &resolution.candidate_ids {work.tick(1)?;*possible.entry(candidate.as_str()).or_default()+=1;}
                    },
                }
            }
            let mut missing=0usize;let mut unavailable_languages=0usize;
            for file in &snapshot.files {
                work.tick(1)?;
                if !file.capability.available {unavailable_languages+=1;}
                for occurrence in &file.occurrences {work.tick(1)?;if !observed.contains(occurrence.id.as_str()){missing+=1;}}
            }
            for file in &snapshot.files {
                work.tick(1)?;
                for decl in &file.declarations {
                    work.tick(1)?;
                    if !matches!(decl.kind.as_str(),"function"|"function_value"|"method") || incoming.contains(decl.id.as_str()) {continue;}
                    let id=format!("decl:{}",decl.id);
                    let Some(candidate)=all.get_mut(&id) else {return Err(error("invalid_evidence","Candidate declaration unavailable"));};
                    let props=candidate.hit.properties.as_mut().and_then(serde_json::Value::as_object_mut).ok_or_else(||error("invalid_evidence","Candidate metadata unavailable"))?;
                    props.insert("analysis_policy".into(),serde_json::json!("no_known_incoming_static_references_v1"));
                    props.insert("known_incoming".into(),serde_json::json!(0));
                    props.insert("known_callers".into(),serde_json::json!(0));
                    props.insert("language_pass".into(),serde_json::json!(if file.capability.available {"partial_static"}else{"unavailable"}));
                    props.insert("explicit_export".into(),serde_json::json!(if decl.explicit_export {"observed"}else{"not_observed_unknown"}));
                    props.insert("test_reference_status".into(),serde_json::json!("unknown"));
                    props.insert("possible_ambiguous_incoming".into(),serde_json::json!(possible.get(decl.id.as_str()).copied().unwrap_or(0)));
                    props.insert("unresolved_target_coverage".into(),serde_json::json!("unknown"));
                    props.insert("freshness".into(),serde_json::json!("indexed_source_not_live"));
                    selected.insert(id);
                }
            }
            let mut incomplete_files=0usize;
            for coverage in &snapshot.coverage {
                work.tick(1)?;
                if !matches!(coverage.status.as_str(),"indexed"|"unchanged"|"ignored"|"deleted"|"no_units"|"no_source_units") {incomplete_files+=1;}
            }
            for (name,value) in [("ambiguous_resolutions",ambiguous),("unresolved_resolutions",unresolved),("unsupported_resolutions",unsupported),
                ("resolved_without_target",missing_target),("missing_resolutions",missing),("unavailable_languages",unavailable_languages),("incomplete_file_coverage",incomplete_files)] {
                analysis_counts.push(QueryCount{name:name.into(),value:Some(value),unit:"observation".into(),exactness:"exact_in_indexed_scope".into()});
            }
            result.reasons.push("static_candidate_not_deletion_proof".into());
            result.reasons.push("dynamic_or_unindexed_references_unknown".into());
        }
        Operation::ImportAnalysis => {
            let mut file_nodes = BTreeSet::new();
            let mut targets = BTreeMap::new();
            let mut imports = BTreeMap::new();
            for file in &snapshot.files {
                work.tick(1)?;
                let file_id = format!("file:{}", file.file_id);
                file_nodes.insert(file_id.clone());
                for declaration in &file.declarations {
                    work.tick(1)?;
                    targets.insert(declaration.id.as_str(), file_id.clone());
                }
                for occurrence in &file.occurrences {
                    work.tick(1)?;
                    if crate::code_evidence::relation_name(occurrence) == "import" {
                        imports.insert(occurrence.id.as_str(), file_id.clone());
                    }
                }
            }
            let mut analysis = ImportAnalysis { policy: "exact_imports_v1".into(), file_count: file_nodes.len(), edge_count: 0,
                inferred: 0, ambiguous: 0, unresolved: 0, unsupported: 0, excluded: 0, missing: 0, units: vec![] };
            let mut pairs: BTreeMap<(String,String), BTreeSet<String>> = BTreeMap::new();
            let mut observed = BTreeSet::new();
            for resolution in &snapshot.resolutions {
                work.tick(1)?;
                let Some(from) = imports.get(resolution.occurrence_id.as_str()) else { continue; };
                observed.insert(resolution.occurrence_id.as_str());
                match &resolution.state {
                    ResolutionState::Inferred => { analysis.inferred += 1; continue; },
                    ResolutionState::Ambiguous => { analysis.ambiguous += 1; continue; },
                    ResolutionState::Unresolved => { analysis.unresolved += 1; continue; },
                    ResolutionState::Unsupported => { analysis.unsupported += 1; continue; },
                    ResolutionState::Exact => {},
                }
                let Some(target) = resolution.target_id.as_deref().and_then(|id| targets.get(id)) else { analysis.missing += 1; continue; };
                if !file_nodes.contains(from) || !file_nodes.contains(target) { analysis.excluded += 1; continue; }
                pairs.entry((from.clone(), target.clone())).or_default().insert(format!("occ:{}", resolution.occurrence_id));
            }
            analysis.missing += imports.len().saturating_sub(observed.len());
            analysis.edge_count = pairs.len();
            let (cycles, hubs) = import_analysis::analyze(&file_nodes, &pairs, |n| work.tick(n))?;
            if cycles.iter().any(|c| c.nodes.len() > 200 || c.occurrences.len() > 200) {
                return Err(error("budget_exceeded", "Import cycle exceeds the complete component member or witness limit"));
            }
            analysis.units.extend(cycles.into_iter().map(|c| ImportAnalysisUnit::Cycle { node_ids: c.nodes, occurrence_ids: c.occurrences }));
            analysis.units.extend(hubs.into_iter().map(|h| ImportAnalysisUnit::Hub { node_id: h.node, in_degree: h.incoming, out_degree: h.outgoing }));
            if analysis.inferred + analysis.ambiguous + analysis.unresolved + analysis.unsupported + analysis.excluded + analysis.missing > 0 {
                result.reasons.push("import_resolution_incomplete".into());
            }
            result.analysis = Some(analysis);
        }
        Operation::Pipeline => {
            let mut outputs: Vec<BTreeSet<String>> = Vec::new();
            for stage in &query.stages {
                work.tick(1)?;
                match stage.operation {
                    StageOperation::Find => {
                        selected = find(
                            &mut all,
                            stage.query.as_deref().unwrap_or(""),
                            &query.channels,
                            &mut work,
                        )?
                    }
                    StageOperation::Filter => {
                        let mut next = BTreeSet::new();
                        for id in &selected {
                            work.tick(1)?;
                            if matches_filter(&all[id], &stage.filter) {
                                next.insert(id.clone());
                            }
                        }
                        selected = next;
                    }
                    StageOperation::Deps => {
                        let mut next = selected.clone();
                        for edge in &edges {
                            work.tick(1)?;
                            if selected.contains(&edge.from) {
                                next.insert(edge.to.clone());
                            }
                        }
                        selected = next;
                    }
                    StageOperation::Outline => {
                        let paths: BTreeSet<_> =
                            selected.iter().map(|id| all[id].path.clone()).collect();
                        selected.clear();
                        for (id, c) in &all {
                            work.tick(1)?;
                            if c.hit.kind == "declaration" && paths.contains(&c.path) {
                                selected.insert(id.clone());
                            }
                        }
                    }
                    StageOperation::Read => result
                        .reasons
                        .push("source_windows_require_authorized_files_owner".into()),
                    StageOperation::Limit => {
                        selected = selected
                            .into_iter()
                            .take(stage.limit.unwrap_or(query.budgets.rows))
                            .collect()
                    }
                    StageOperation::Union
                    | StageOperation::Intersection
                    | StageOperation::Difference => {
                        let other =
                            stage
                                .with_stage
                                .and_then(|i| outputs.get(i))
                                .ok_or_else(|| {
                                    error("invalid", "Set operation requires a previous stage")
                                })?;
                        work.tick(selected.len() + other.len())?;
                        selected = match stage.operation {
                            StageOperation::Union => selected.union(other).cloned().collect(),
                            StageOperation::Intersection => {
                                selected.intersection(other).cloned().collect()
                            }
                            _ => selected.difference(other).cloned().collect(),
                        };
                    }
                }
                outputs.push(selected.clone());
            }
        }
        Operation::Capabilities => unreachable!(),
    }
    let mut facets: BTreeMap<String, BTreeMap<String, usize>> = BTreeMap::new();
    for id in &selected {
        work.tick(1)?;
        let c = &all[id];
        if c.hit.kind != "file" {
            continue;
        }
        let folder = c.path.rsplit_once('/').map_or("", |(parent, _)| parent);
        for (key, value) in [
            ("kinds", c.hit.kind.as_str()),
            ("languages", c.language.as_str()),
            ("states", c.state.as_str()),
            ("folders", folder),
        ] {
            *facets
                .entry(key.into())
                .or_default()
                .entry(value.into())
                .or_default() += 1;
        }
    }
    let with_facets = matches!(query.operation, Operation::Facets | Operation::Overview);
    if with_facets {
        result.facets = serde_json::json!({"kinds":[],"languages":[],"states":[],"folders":[]});
    }
    let matched = selected.len();
    let mut ordered: Vec<_> = selected
        .into_iter()
        .map(|id| all.remove(&id).unwrap().hit)
        .collect();
    ordered.sort_by(|a, b| match (a.matches.first(), b.matches.first()) {
        (Some(x), Some(y)) => y
            .ordering_score
            .cmp(&x.ordering_score)
            .then(a.id.cmp(&b.id)),
        _ => a.id.cmp(&b.id),
    });
    let mut lane_ranks = BTreeMap::<String, usize>::new();
    for hit in &mut ordered {
        for m in &mut hit.matches {
            let key = serde_json::to_string(&m.channel).unwrap();
            let rank = lane_ranks.entry(key).or_default();
            *rank += 1;
            m.rank = *rank;
        }
    }
    if query.operation != Operation::Facets {
        result.hits = ordered;
    }
    if query.operation == Operation::Path {
        result.edges.clear();
    }
    result.edges.sort_by(|a, b| {
        (&a.from, &a.to, &a.occurrence_id).cmp(&(&b.from, &b.to, &b.occurrence_id))
    });
    result.edges.dedup();
    for hit in &mut result.hits {
        if matches!(query.operation,Operation::DeadCodeCandidates|Operation::StructuralTwins) {project(hit,&[Field::Label,Field::Source,Field::Properties]);}
        else {project(hit, &query.fields);}
    }
    for edge in &mut result.edges {
        project_edge(edge, &query.fields);
    }
    for path in &mut result.paths {
        for edge in &mut path.edges {
            project_edge(edge, &query.fields);
        }
    }
    if query.operation == Operation::Read {
        result
            .reasons
            .push("source_windows_require_authorized_files_owner".into());
    }
    result.counts = vec![
        QueryCount {
            name: "eligible".into(),
            value: Some(eligible),
            unit: "resource".into(),
            exactness: "exact".into(),
        },
        QueryCount {
            name: "matched".into(),
            value: Some(matched),
            unit: "resource".into(),
            exactness: if query.operation==Operation::DeadCodeCandidates {"exact_in_indexed_scope"} else {"exact"}.into(),
        },
    ];
    result.counts.extend(analysis_counts);
    result.cost.inspected_records = work.used;
    let search_executed = query.operation == Operation::Search
        || query
            .stages
            .iter()
            .any(|s| s.operation == StageOperation::Find);
    let structural_executed = matches!(
        query.operation,
        Operation::Neighbors | Operation::Impact | Operation::Path | Operation::ImportAnalysis | Operation::DeadCodeCandidates | Operation::GraphCommunities | Operation::StructuralTwins | Operation::BackwardSlice | Operation::ErrorBoundaries
    ) || query
        .stages
        .iter()
        .any(|s| s.operation == StageOperation::Deps);
    for lane in &mut result.channels {
        if lane.requested
            && lane.enabled
            && ((search_executed
                && matches!(
                    lane.channel,
                    Channel::Exact | Channel::Identifier | Channel::Fuzzy | Channel::Lexical
                ))
                || (structural_executed && lane.channel == Channel::Structural))
        {
            lane.executed = true;
            // Aggregate inspected work is measured in result.cost. Do not invent
            // per-lane timings or candidate counts from that shared work.
        }
    }
    if result.edges.iter().any(|e| e.relation_kind == "manifest_dependency") { result.reasons.push("manifest_dependency_inferred_partial".into()); }
    if query.filter.relation_kinds.iter().any(|v|v=="sql_table_reference") || query.filter.languages.iter().any(|v|v=="sql_static") || query.filter.symbol_kinds.iter().any(|v|v=="sql_table") || result.edges.iter().any(|e| e.relation_kind == "sql_table_reference") || snapshot.coverage.iter().any(|c|matches!(c.status.as_str(),"indexed"|"no_source_units"|"unchanged") && c.detail.as_deref()==Some("sql_static_profile_partial")) || snapshot.files.iter().any(|f|f.diagnostics.iter().any(|d|matches!(d.code.as_str(),"unsupported_sql_statement"|"unsupported_sql_code"))) {result.reasons.push("sql_static_coverage_partial".into());}
    if query.filter.relation_kinds.iter().any(|v|v=="callback_registration") || result.edges.iter().any(|e|e.relation_kind=="callback_registration") || snapshot.coverage.iter().any(|c|matches!(c.status.as_str(),"indexed"|"no_source_units"|"unchanged") && c.detail.as_deref()==Some("callback_registration_partial")) || snapshot.files.iter().any(|f|f.diagnostics.iter().any(|d|d.code=="unsupported_callback_registration")) {result.reasons.push("callback_registration_partial".into());}
    if query.filter.relation_kinds.iter().any(|v| matches!(v.as_str(),"event_publish"|"event_consume"))
        || result.edges.iter().any(|edge| matches!(edge.relation_kind.as_str(),"event_publish"|"event_consume"))
        || result.paths.iter().any(|path| path.edges.iter().any(|edge| matches!(edge.relation_kind.as_str(),"event_publish"|"event_consume")))
    {
        result.reasons.push("static_event_syntax_only".into());
    }
    let coverage_partial = result.reasons.iter().any(|r| r == "import_resolution_incomplete" || r == "structural_shape_coverage_partial" || r == "manifest_dependency_inferred_partial" || r == "sql_static_coverage_partial" || r == "callback_registration_partial" || r == "static_event_syntax_only") || snapshot.coverage.iter().any(|c| {
        !matches!(
            c.status.as_str(),
            "indexed" | "unchanged" | "ignored" | "deleted" | "no_units" | "no_source_units"
        )
    });
    if coverage_partial {
        result.reasons.push("source_coverage_partial".into());
    }
    // One deterministic unit stream (hits, edges, paths), not silently capped
    // edge arrays. A cursor always advances at least one complete returned unit.
    enum Unit {
        Analysis(ImportAnalysisUnit),
        Community(GraphCommunity),
        Slice(crate::code_evidence::local_slice::LocalDefinition),
        Boundary(crate::code_evidence::error_boundaries::ErrorRegion),
        Hit(QueryHit),
        Edge(QueryEdge),
        Path(QueryPath),
        Facet(&'static str, serde_json::Value),
    }
    let hit_units = std::mem::take(&mut result.hits)
        .into_iter()
        .map(Unit::Hit);
    let mut facet_units = Vec::new();
    if with_facets {
        for axis in ["kinds", "languages", "states", "folders"] {
            if let Some(buckets) = facets.remove(axis) {
                for (value, count) in buckets {
                    facet_units.push(Unit::Facet(axis, serde_json::json!({"value":value,"count":count,"unit":"file","exactness":"exact"})));
                }
            }
        }
    }
    // Overview starts with a file, then alternates complete facet buckets and
    // files. Neither high-cardinality axis metadata nor later files disappear.
    let mut units = Vec::new();
    let mut hits = hit_units;
    let mut buckets = facet_units.into_iter();
    loop {
        let hit = hits.next();
        let bucket = buckets.next();
        if hit.is_none() && bucket.is_none() { break; }
        if let Some(hit) = hit { units.push(hit); }
        if let Some(bucket) = bucket { units.push(bucket); }
    }
    units.extend(
            std::mem::take(&mut result.edges)
                .into_iter()
                .map(Unit::Edge),
    );
    units.extend(
            std::mem::take(&mut result.paths)
                .into_iter()
                .map(Unit::Path),
    );
    if let Some(analysis) = result.analysis.as_mut() {
        units.extend(std::mem::take(&mut analysis.units).into_iter().map(Unit::Analysis));
    }
    if let Some(analysis)=result.community_analysis.as_mut() {
        units.extend(std::mem::take(&mut analysis.groups).into_iter().map(Unit::Community));
    }
    if let Some(analysis)=result.slice_analysis.as_mut(){units.extend(std::mem::take(&mut analysis.units).into_iter().map(Unit::Slice));}
    if let Some(analysis)=result.error_boundary_analysis.as_mut(){units.extend(std::mem::take(&mut analysis.regions).into_iter().map(Unit::Boundary));}
    let total = units.len();
    if offset > total {
        return Err(error(
            "stale_cursor",
            "Code continuation exceeds result set",
        ));
    }
    let unit_name = if query.operation == Operation::GraphCommunities { "community" } else if query.operation == Operation::ImportAnalysis { "import_analysis_unit" } else if with_facets { "resource_or_edge_or_path_or_facet_bucket" } else { "resource_or_edge_or_path" };
    result.counts.push(QueryCount {
        name: "matched_units".into(),
        value: Some(total),
        unit: unit_name.into(),
        exactness: if query.operation==Operation::DeadCodeCandidates {"exact_in_indexed_scope"} else {"exact"}.into(),
    });
    let mut consumed = offset;
    let limit = query.budgets.bytes.min(query.budgets.tokens);
    for unit in units.into_iter().skip(offset).take(query.budgets.rows) {
        let added_analysis = matches!(&unit, Unit::Analysis(_));
        let added_community = matches!(&unit, Unit::Community(_));
        let added_slice = matches!(&unit, Unit::Slice(_));
        let added_boundary = matches!(&unit, Unit::Boundary(_));
        let added_facet = match unit {
            Unit::Community(value) => {result.community_analysis.as_mut().unwrap().groups.push(value); None},
            Unit::Boundary(value) => {result.error_boundary_analysis.as_mut().unwrap().regions.push(value); None},
            Unit::Slice(value) => {result.slice_analysis.as_mut().unwrap().units.push(value); None},
            Unit::Analysis(value) => { result.analysis.as_mut().unwrap().units.push(value); None },
            Unit::Hit(h) => { result.hits.push(h); None },
            Unit::Edge(e) => { result.edges.push(e); None },
            Unit::Path(p) => { result.paths.push(p); None },
            Unit::Facet(axis, bucket) => { result.facets[axis].as_array_mut().unwrap().push(bucket); Some(axis) },
        };
        consumed += 1;
        result.next_cursor =
            (consumed < total).then(|| encode_cursor(&fingerprint, consumed, cursor_secret));
        result.partial = coverage_partial || matches!(query.operation,Operation::DeadCodeCandidates|Operation::GraphCommunities|Operation::StructuralTwins|Operation::BackwardSlice|Operation::ErrorBoundaries) || consumed < total;
        set_returned_counts(&mut result, total, consumed - offset, unit_name);
        if measured(&mut result)? > limit {
            if query.operation == Operation::Inspect {
                if let Some(flow) = result.hits.last_mut()
                    .and_then(|hit| hit.properties.as_mut())
                    .and_then(|props| props.get_mut("functionFlow"))
                    .filter(|flow| flow["status"] == "complete")
                {
                    let omitted = crate::code_evidence::function_flow::FunctionFlow::unavailable("response_budget");
                    *flow = serde_json::to_value(omitted).map_err(|_| error("internal", "Function flow encoding failed"))?;
                    if measured(&mut result)? <= limit {
                        continue;
                    }
                }
            }
            if added_boundary {result.error_boundary_analysis.as_mut().unwrap().regions.pop();} else if added_slice { result.slice_analysis.as_mut().unwrap().units.pop(); } else if added_analysis {
                result.analysis.as_mut().unwrap().units.pop();
            } else if added_community {
                result.community_analysis.as_mut().unwrap().groups.pop();
            } else if let Some(axis) = added_facet {
                result.facets[axis].as_array_mut().unwrap().pop();
            } else if !result.paths.is_empty() {
                result.paths.pop();
            } else if !result.edges.is_empty() {
                result.edges.pop();
            } else {
                result.hits.pop();
            }
            consumed -= 1;
            if consumed == offset {
                return Err(error(
                    "budget_exceeded",
                    "First result unit exceeds complete-envelope budget",
                ));
            }
            break;
        }
    }
    result.next_cursor =
        (consumed < total).then(|| encode_cursor(&fingerprint, consumed, cursor_secret));
    result.partial = coverage_partial || matches!(query.operation,Operation::DeadCodeCandidates|Operation::GraphCommunities|Operation::StructuralTwins|Operation::BackwardSlice|Operation::ErrorBoundaries) || consumed < total;
    set_returned_counts(&mut result, total, consumed - offset, unit_name);
    if offset==0 && consumed==total && result.next_cursor.is_none() && !result.degraded {
        let candidate=if snapshot.eligible_records.is_none() {snapshot.files.iter().find_map(|f|outline_eligible(query,f,&snapshot.coverage))} else {snapshot.outline_candidate.clone()};
        if let Some(candidate)=candidate {
            let ids:BTreeSet<_>=result.hits.iter().filter(|h|h.kind=="declaration").map(|h|h.id.as_str()).collect();
            if ids.len()==candidate.declaration_count && result.hits.len()==candidate.declaration_count {
                result.outline_completeness=Some(candidate);
                if measured(&mut result)? > query.budgets.bytes.min(query.budgets.tokens) {result.outline_completeness=None;}
            }
        }
    }
    finish(result, query)
}
fn find(
    all: &mut BTreeMap<String, Candidate>,
    text: &str,
    channels: &[Channel],
    work: &mut Work<'_>,
) -> Result<BTreeSet<String>, QueryError> {
    let terms = identifier_terms(text);
    let mut out = BTreeSet::new();
    if text.is_empty() {
        return Ok(out);
    }
    for (id, c) in all {
        work.tick(1 + text.len() + c.name.len())?;
        let mut matches = Vec::new();
        if c.hit.kind == "declaration" && channels.contains(&Channel::Lexical) && !terms.is_empty() {
            let documentation=c.hit.documentation.as_ref().and_then(|v|v.get("text")).and_then(|v|v.as_str()).unwrap_or("");
            let metadata=format!("{} {} {} {} {}",c.name,c.hit.label.as_deref().unwrap_or(""),c.path,c.hit.signature.as_deref().unwrap_or(""),documentation);
            work.tick(metadata.len())?;
            let searchable=identifier_terms(&metadata);
            if terms.iter().all(|term|searchable.iter().any(|word|word==term)) {
                matches.push(ChannelMatch{channel:Channel::Lexical,rank:0,ordering_score:150,explanation:"lexical declaration name/qualified name/signature/documentation/path terms".into()});
            }
        }
        if channels.contains(&Channel::Exact)
            && (c.name == text || c.path == text || c.hit.label.as_deref() == Some(text))
        {
            matches.push(ChannelMatch {
                channel: Channel::Exact,
                rank: 0,
                ordering_score: 300,
                explanation: "literal case-sensitive identifier/path equality".into(),
            });
        }
        if channels.contains(&Channel::Identifier)
            && !terms.is_empty()
            && terms.iter().all(|t| c.terms.contains(t))
        {
            matches.push(ChannelMatch {
                channel: Channel::Identifier,
                rank: 0,
                ordering_score: 200,
                explanation: "all segmented identifier terms match; not calibrated relevance"
                    .into(),
            });
        }
        if matches.is_empty()
            && channels.contains(&Channel::Fuzzy)
            && edit_one(&text.to_lowercase(), &c.name.to_lowercase(), work)?
        {
            matches.push(ChannelMatch {
                channel: Channel::Fuzzy,
                rank: 0,
                ordering_score: 100,
                explanation: "suggestion only: one edit, same initial, minimum four characters"
                    .into(),
            });
            c.hit.evidence_state = "suggestion".into();
        }
        if !matches.is_empty() {
            matches.sort_by(|a,b|b.ordering_score.cmp(&a.ordering_score));
            c.hit.matches = matches;
            out.insert(id.clone());
        }
    }
    Ok(out)
}
fn finish(mut result: CodeQueryResult, query: &CodeQuery) -> Result<CodeQueryResult, QueryError> {
    if measured(&mut result)? > query.budgets.bytes.min(query.budgets.tokens) {
        return Err(error(
            "budget_exceeded",
            "Complete code envelope exceeds budget",
        ));
    }
    Ok(result)
}
fn set_returned_counts(result: &mut CodeQueryResult, total: usize, returned: usize, unit: &str) {
    result
        .counts
        .retain(|c| c.name != "returned" && c.name != "omitted");
    result.counts.push(QueryCount {
        name: "returned".into(),
        value: Some(returned),
        unit: unit.into(),
        exactness: "exact".into(),
    });
    result.counts.push(QueryCount {
        name: "omitted".into(),
        value: Some(total - returned),
        unit: unit.into(),
        exactness: "exact".into(),
    });
}

#[cfg(test)]
mod s05_query_tests {
    use super::*;
    use crate::code_evidence::{
        extract_evidence, resolve_evidence, CorpusInput, ExtractionBudget, ResolutionBudget,
    };
    fn snapshot(sources: &[(&str, &str)]) -> QuerySnapshot {
        let files: Vec<_> = sources
            .iter()
            .map(|(path, text)| {
                extract_evidence(
                    "checkout",
                    path,
                    text.as_bytes(),
                    &ExtractionBudget::default(),
                )
                .unwrap()
            })
            .collect();
        let corpus = resolve_evidence(
            &CorpusInput {
                checkout_id: "checkout".into(),
                files: files.clone(),
                unavailable_files: vec![],
            },
            &ResolutionBudget::default(),
        )
        .unwrap();
        QuerySnapshot {
            receipt: QueryReceipt {
                workspace_id: "workspace".into(),
                registration_id: "registration".into(),
                source_snapshot_id: "snapshot".into(),
                evidence: CodeEvidenceReceipt {
                    checkout_id: "checkout".into(),
                    head_generation: "head".into(),
                    admission_epoch: 0,
                    policy_generation: 1,
                    config_fingerprint: crate::code::producer_fingerprint(),
                },
            },
            scope_key: "owner/workspace/immutable".into(),
            files,
            resolutions: corpus.resolutions,
            coverage: vec![],
            metadata_bytes: 0,
            metadata_records: 0,
            eligible_records: None,
            outline_candidate: None,
        }
    }
    fn run(s: &QuerySnapshot, q: &CodeQuery) -> CodeQueryResult {
        run_query(s, q, &AtomicBool::new(false), &[3; 32]).unwrap()
    }
    #[test]
    fn manifest_dependency_keys_resolve_as_inferred_file_edges() {
        let manifest = r#"{"dependencies":{"react":"VERSION_SECRET","@scope/lib":"^1"},"scripts":{"test":"SCRIPT_SECRET"}}"#;
        let source = "import React from \"react\";\nexport function useIt() { return React; }\n";
        let s = snapshot(&[("package.json", manifest), ("src/main.ts", source)]);
        assert!(!serde_json::to_string(&s.files).unwrap().contains("VERSION_SECRET"));
        assert!(!serde_json::to_string(&s.files).unwrap().contains("SCRIPT_SECRET"));
        let source_file = s.files.iter().find(|f| f.rel_path == "src/main.ts").unwrap();
        let manifest_file = s.files.iter().find(|f| f.rel_path == "package.json").unwrap();
        let occurrence = source_file.occurrences.iter().find(|o| o.syntax_kind == "manifest_dependency").unwrap();
        let resolution = s.resolutions.iter().find(|r| r.occurrence_id == occurrence.id).unwrap();
        assert_eq!(resolution.state, ResolutionState::Inferred);
        let key = manifest_file.declarations.iter().find(|d| d.name == "react").unwrap();
        assert_eq!(resolution.target_id.as_deref(), Some(key.id.as_str()));
        assert_eq!(&manifest[key.source.span.start_byte as usize..key.source.span.end_byte as usize], "\"react\"");
        assert_eq!(&source[occurrence.source.span.start_byte as usize..occurrence.source.span.end_byte as usize], "\"react\"");
        let q = CodeQuery { operation: Operation::Neighbors, seed_ids: vec![format!("file:{}", source_file.file_id)], ..Default::default() };
        let result = run(&s, &q);
        assert!(result.edges.iter().any(|e| e.relation_kind == "manifest_dependency" && e.to == format!("decl:{}", key.id)));
        assert!(result.partial && result.reasons.iter().any(|v|v=="manifest_dependency_inferred_partial"));
        let duplicate = br#"{"dependencies":{"react":"1","\u0072eact":"2"}}"#;
        assert!(extract_evidence("checkout", "package.json", duplicate, &ExtractionBudget::default()).is_err());
        let input = CorpusInput { checkout_id: "checkout".into(), files: s.files.clone(), unavailable_files: vec![crate::code_evidence::UnavailableFile { rel_path:"src/package.json".into(),source_hash:None,reason:"ignored".into() }] };
        let blocked = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
        let resolution = blocked.resolutions.iter().find(|r| r.occurrence_id == occurrence.id).unwrap();
        assert_eq!(resolution.state, ResolutionState::Unsupported);
        assert!(resolution.target_id.is_none());
        let alias = snapshot(&[("package.json", r#"{"dependencies":{"react":"npm:other@1"}}"#), ("src/main.ts", source)]);
        let alias_manifest=alias.files.iter().find(|f|f.rel_path=="package.json").unwrap();
        let projected=crate::code_evidence::storage::module_interface(alias_manifest);
        assert!(projected.diagnostics.iter().any(|d|d.code=="unsupported_manifest_dependency"));
        assert!(alias.resolutions.iter().any(|r|r.state==ResolutionState::Unsupported && r.reason.contains("unsupported dependency")));
        assert!(!serde_json::to_string(&projected).unwrap().contains("npm:other@1"));
    }
    #[test]
    fn structural_twins_compare_complete_shapes_and_preserve_witnesses() {
        let mut s = snapshot(&[("a.py", "def first(x):\n    if x:\n        return call(x + 1)\n    return call(x + 2)\n\ndef renamed(y):\n    if y:\n        return other(y + 9)\n    return other(y + 8)\n\ndef different(y):\n    if y:\n        return other(y - 9)\n    return other(y + 8)\n")]);
        let file = &s.files[0];
        let first = file.declarations.iter().find(|d| d.name == "first").unwrap();
        let second = file.declarations.iter().find(|d| d.name == "renamed").unwrap();
        let different = file.declarations.iter().find(|d| d.name == "different").unwrap();
        assert_eq!(first.structural_shape.as_ref().unwrap().nodes, second.structural_shape.as_ref().unwrap().nodes);
        assert_ne!(first.structural_shape.as_ref().unwrap().nodes, different.structural_shape.as_ref().unwrap().nodes);
        let q = CodeQuery { operation: Operation::StructuralTwins, seed_ids: vec![format!("decl:{}",first.id)], ..Default::default() };
        let result = run(&s, &q);
        let wire = serde_json::to_value(&result).unwrap();
        assert_eq!(wire["operation_capabilities"].as_array().unwrap().len(), result.operation_capabilities.len());
        assert!(wire["operation_capabilities"].as_array().unwrap().iter().any(|row|row["operation"]=="read" && row["reason"]=="authorized_files_owner_required"));
        assert!(wire["operation_capabilities"].as_array().unwrap().iter().any(|row|row["operation"]=="neighbors" && row.get("reason").is_none()));
        let roundtrip:CodeQueryResult=serde_json::from_value(wire).unwrap();assert_eq!(roundtrip,result);
        assert_eq!(result.counts.iter().find(|c|c.name=="structural_candidates").unwrap().value,Some(1));
        assert!(result.counts.iter().all(|c|matches!(c.exactness.as_str(), "exact" | "unknown")));
        let mut returned = result.hits.clone();
        let mut cursor = result.next_cursor.clone();
        while let Some(next) = cursor {
            let page = run(&s, &CodeQuery { cursor:Some(next), ..q.clone() });
            returned.extend(page.hits); cursor = page.next_cursor;
        }
        assert_eq!(returned.len(), 2);
        assert!(returned.iter().all(|h|h.source.is_some() && h.properties.as_ref().unwrap()["structuralTwin"]["occurrences"].as_array().unwrap().iter().all(|w|!w["span"].is_null())));
        assert!(result.partial && result.reasons.iter().any(|r|r=="structural_twins_heuristic_not_semantic_equivalence"));
        let stale = CodeQuery { expected_receipt: result.receipt.clone(), ..q.clone() };
        s.receipt.evidence.head_generation = "new".into();
        assert_eq!(run_query(&s,&stale,&AtomicBool::new(false),&[3;32]).unwrap_err().code,"stale_cursor");
        s.files[0].declarations.iter_mut().find(|d|d.name=="renamed").unwrap().structural_shape=None;
        assert!(run(&s,&q).reasons.iter().any(|r|r=="structural_shape_coverage_partial"));
        let tiny = CodeQuery { budgets: QueryBudget {work:1,..Default::default()},..q };
        assert_eq!(run_query(&s,&tiny,&AtomicBool::new(false),&[3;32]).unwrap_err().code,"budget_exceeded");
        let literals = snapshot(&[("literals.py", "def first(x):\n    if x:\n        return call(True, 1)\n    return call(False, 2)\ndef second(y):\n    if y:\n        return other(False, 3)\n    return other(True, 4)\ndef float_variant(y):\n    if y:\n        return other(False, 3.0)\n    return other(True, 4)\n")]);
        let literal_shape=|name:&str| &literals.files[0].declarations.iter().find(|d|d.name==name).unwrap().structural_shape.as_ref().unwrap().nodes;
        assert_eq!(literal_shape("first"),literal_shape("second"));
        assert_ne!(literal_shape("first"),literal_shape("float_variant"));
        let nested = snapshot(&[("nested.py", "def outer_a(x):\n    def inner():\n        return 1\n    return run(x)\ndef outer_b(y):\n    def other():\n        if y:\n            return 9\n    return run(y)\n")]);
        let shapes:Vec<_>=nested.files[0].declarations.iter().filter(|d|d.name.starts_with("outer_")).map(|d|&d.structural_shape.as_ref().unwrap().nodes).collect();
        assert_eq!(shapes.len(),2);assert_eq!(shapes[0],shapes[1]);
        let oversized=format!("def huge():\n{}", "    call()\n".repeat(2500));
        let bounded=extract_evidence("checkout","huge.py",oversized.as_bytes(),&ExtractionBudget::default()).unwrap();
        assert_eq!(bounded.declarations[0].structural_shape.as_ref().unwrap().status,"skipped_budget");
        for (path, code) in [
            ("t.rs", "fn first(x:i32)->i32 { if x>0 {f(x+1)} else {f(x+2)} } fn second(y:i32)->i32 { if y>0 {g(y+9)} else {g(y+8)} }"),
            ("t.ts", "function first(x:number) { if (x) {return f(x+1)} return f(x+2) } function second(y:number) { if (y) {return g(y+9)} return g(y+8) }"),
            ("t.js", "function first(x) { if (x) {return f(x+1)} return f(x+2) } function second(y) { if (y) {return g(y+9)} return g(y+8) }"),
        ] {
            let file=extract_evidence("checkout",path,code.as_bytes(),&ExtractionBudget::default()).unwrap();
            assert_eq!(file.declarations[0].structural_shape.as_ref().unwrap().nodes,file.declarations[1].structural_shape.as_ref().unwrap().nodes,"{path}");
        }
    }
    #[test]
    fn import_analysis_reports_witnessed_cycles_and_incomplete_coverage() {
        let mut s = snapshot(&[("a.py", "from b import bf\ndef af(): pass\n"), ("b.py", "from a import af\ndef bf(): pass\n"), ("c.py", "from missing import thing\n")]);
        let q = CodeQuery { operation: Operation::ImportAnalysis, ..Default::default() };
        let result = run(&s, &q);
        let analysis = result.analysis.unwrap();
        assert_eq!(analysis.policy, "exact_imports_v1");
        assert!(analysis.units.iter().any(|unit| matches!(unit, ImportAnalysisUnit::Cycle { node_ids, occurrence_ids } if node_ids.len() == 2 && occurrence_ids.len() == 2)));
        assert!(analysis.units.iter().any(|unit| matches!(unit, ImportAnalysisUnit::Hub { in_degree: 1, out_degree: 1, .. })));
        assert!(result.partial && analysis.unresolved + analysis.unsupported + analysis.missing > 0);
        s.receipt.evidence.head_generation = "new-head".into();
        let stale = CodeQuery { expected_receipt: result.receipt, ..q.clone() };
        assert_eq!(run_query(&s, &stale, &AtomicBool::new(false), &[3;32]).unwrap_err().code, "stale_cursor");
        let tiny = CodeQuery { budgets: QueryBudget { work: 1, ..Default::default() }, ..q };
        assert_eq!(run_query(&s, &tiny, &AtomicBool::new(false), &[3;32]).unwrap_err().code, "budget_exceeded");
    }
    #[test]
    fn indexed_key_roles_agree_with_candidate_scorer_exhaustively() {
        let mut words = vec![String::new()];
        for length in 1..=5 {
            for bits in 0..(1usize << length) {
                words.push((0..length).map(|i| if bits & (1 << i) == 0 { 'a' } else { 'B' }).collect());
            }
        }
        words.extend([
            "target", "xarget", "xtarget", "arget", "targat", "targe", "targetx",
            "parseHTTPResponse", "http response", "Δέντρο", "δέντρο", "δέντρα",
            "İstanbul", "i\u{307}stanbul", "Éclair", "éclair", "éclais", "straße",
            "STRASSE", "snake_case", "snake case", "::", "pkg/name.py",
        ].into_iter().map(str::to_owned));
        for name in &words {
            let label = format!("namespace.{name}");
            let path = format!("module/{name}.py");
            let candidate = Candidate {
                hit: base_hit("decl:01".into(), "declaration", label.clone(), None, serde_json::json!({})),
                path: path.clone(), language: "python".into(), state: "metadata".into(),
                name: name.clone(), terms: identifier_terms(&label),
            };
            let mut queries = words.clone();
            queries.extend([label.clone(), path.clone(), "namespace".into()]);
            for text in &queries {
                for mask in 0..8 {
                    let channels: Vec<_> = [Channel::Exact, Channel::Identifier, Channel::Fuzzy]
                        .into_iter().enumerate().filter(|(i, _)| mask & (1 << i) != 0)
                        .map(|(_, channel)| channel).collect();
                    let mut all = BTreeMap::from([("decl:01".into(), candidate.clone())]);
                    let cancel = AtomicBool::new(false);
                    let mut work = Work { used: 0, max: MAX_QUERY_WORK, cancel: &cancel };
                    let expected = !find(&mut all, text, &channels, &mut work).unwrap().is_empty();
                    let mut admitted = false;
                    for (key, roles) in [(name.as_str(), 5), (label.as_str(), 3), (path.as_str(), 1)] {
                        admitted |= index_key_match(text, key, roles, &channels, MAX_QUERY_WORK).unwrap().0;
                    }
                    assert_eq!(admitted, expected, "query={text:?} name={name:?} channels={channels:?}");
                }
            }
        }
    }
    #[test]
    fn indexed_key_prefilter_is_bounded_and_preserves_existing_fuzzy_contract() {
        for text in ["xarget", "xtarget", "arget", "target"] {
            assert!(!index_key_match(text, "target", 4, &[Channel::Fuzzy], 100).unwrap().0);
        }
        for text in ["targat", "targetx", "targe"] {
            let (matched, used) = index_key_match(text, "target", 4, &[Channel::Fuzzy], 100).unwrap();
            assert!(matched);
            assert!(used > text.len() + "target".len());
            assert_eq!(index_key_match(text, "target", 4, &[Channel::Fuzzy], used - 1).unwrap_err().code, "budget_exceeded");
        }
        assert!(!index_key_match("target", "target", 7, &[Channel::Semantic, Channel::Structural], 100).unwrap().0);
        assert!(!index_key_match("", "target", 7, &[Channel::Exact, Channel::Identifier], 100).unwrap().0);
        assert_eq!(index_key_match("target", "target", 1, &[Channel::Exact], 0).unwrap_err().code, "budget_exceeded");
        for roles in [-1, 0, 8] {
            assert_eq!(index_key_match("target", "target", roles, &[Channel::Exact], 100).unwrap_err().code, "invalid");
        }
        assert_eq!(index_key_match("target", "target", 1, &[Channel::Exact], MAX_QUERY_WORK + 1).unwrap_err().code, "invalid");
        assert_eq!(index_key_match("x", &"x".repeat(10_000), 2, &[Channel::Identifier], 100).unwrap_err().code, "budget_exceeded");
    }
    #[test]
    fn empty_valid_source_coverage_is_complete_in_both_legacy_spellings() {
        let mut s=snapshot(&[("empty.py", "")]);
        for status in ["no_units","no_source_units"] {
            s.coverage=vec![crate::code::ScanCoverage{rel_path:"empty.py".into(),status:status.into(),..Default::default()}];
            let result=run(&s,&CodeQuery{operation:Operation::Overview,..Default::default()});
            assert!(!result.reasons.iter().any(|r|r=="source_coverage_partial"));
        }
        s.coverage[0].status="parse_failed".into();
        assert!(run(&s,&CodeQuery{operation:Operation::Overview,..Default::default()}).reasons.iter().any(|r|r=="source_coverage_partial"));
    }
    #[test]
    fn indexed_snapshot_counts_eligible_separately_from_materialized_matches() {
        let mut s = snapshot(&[("code.py", "def target(): pass\n")]);
        let q = CodeQuery { query: Some("target".into()), ..Default::default() };
        let baseline = run(&s, &q);
        s.eligible_records = Some(10_000);
        let indexed = run(&s, &q);
        assert_eq!(indexed.hits, baseline.hits);
        assert_eq!(indexed.counts.iter().find(|count| count.name == "eligible").unwrap().value, Some(10_000));
        assert_eq!(indexed.counts.iter().find(|count| count.name == "matched").unwrap().value,
            baseline.counts.iter().find(|count| count.name == "matched").unwrap().value);
        s.eligible_records = Some(0);
        assert_eq!(run_query(&s, &q, &AtomicBool::new(false), &[3; 32]).unwrap_err().code, "invalid");
    }
    #[test]
    fn acronym_unicode_identifier_terms_and_literal_case_are_distinct() {
        assert_eq!(
            identifier_terms("parseHTTPResponse_Δέντρο"),
            vec!["http", "parse", "response", "δέντρο"]
        );
        let s = snapshot(&[(
            "code.py",
            "def parseHTTPResponse(): pass\ndef Δέντρο(): pass\n",
        )]);
        for text in ["http response", "Δέντρο"] {
            let q = CodeQuery {
                query: Some(text.into()),
                ..Default::default()
            };
            let r = run(&s, &q);
            assert!(!r.hits.is_empty());
            assert!(r.hits.iter().all(|h| !h.current_source_verified));
        }
        let q = CodeQuery {
            query: Some("PARSEHTTPRESPONSE".into()),
            channels: vec![Channel::Exact],
            ..Default::default()
        };
        assert!(run(&s, &q).hits.is_empty());
    }
    #[test]
    fn typo_suggestions_never_become_semantic_or_resolution_evidence() {
        let s = snapshot(&[("code.py", "def target(): pass\ndef budget(): pass\n")]);
        let q = CodeQuery {
            query: Some("targat".into()),
            channels: vec![Channel::Fuzzy, Channel::Semantic],
            ..Default::default()
        };
        let r = run(&s, &q);
        assert!(r.degraded);
        assert_eq!(r.hits.len(), 1);
        assert_eq!(r.hits[0].evidence_state, "suggestion");
        assert_eq!(r.hits[0].matches[0].channel, Channel::Fuzzy);
        let q = CodeQuery {
            query: Some("market".into()),
            channels: vec![Channel::Fuzzy],
            ..Default::default()
        };
        assert!(run(&s, &q).hits.is_empty());
    }
    #[test]
    fn path_has_each_directed_occurrence_witness_and_recursion_is_not_deduplicated() {
        let s = snapshot(&[(
            "code.py",
            "def target(): target()\ndef caller():\n target()\n target()\n",
        )]);
        let caller = s.files[0]
            .declarations
            .iter()
            .find(|d| d.name == "caller")
            .unwrap();
        let target = s.files[0]
            .declarations
            .iter()
            .find(|d| d.name == "target")
            .unwrap();
        let mut q = CodeQuery {
            operation: Operation::Neighbors,
            seed_ids: vec![format!("decl:{}", caller.id)],
            fields: vec![Field::Witnesses, Field::Source],
            ..Default::default()
        };
        let mut edges = Vec::new();
        loop {
            let r = run(&s, &q);
            edges.extend(r.edges);
            if r.next_cursor.is_none() {
                break;
            }
            q.cursor = r.next_cursor;
        }
        assert_eq!(edges.len(), 2);
        assert_ne!(edges[0].occurrence_id, edges[1].occurrence_id);
        let q = CodeQuery {
            operation: Operation::Path,
            seed_ids: vec![format!("decl:{}", caller.id)],
            target_id: Some(format!("decl:{}", target.id)),
            fields: vec![Field::Witnesses, Field::Source],
            ..Default::default()
        };
        let r = run(&s, &q);
        assert_eq!(r.paths.len(), 1);
        assert_eq!(r.paths[0].edges.len(), 1);
        assert!(!r.paths[0].edges[0].witnesses.as_ref().unwrap().is_empty());
    }
    #[test]
    fn catalog_coverage_and_file_id_survive_unsupported_evidence() {
        let mut s = snapshot(&[("code.py", "def target(): pass\n")]);
        s.coverage.push(crate::code::ScanCoverage {
            rel_path: "notes.js".into(),
            status: "unsupported".into(),
            ..Default::default()
        });
        let q = CodeQuery {
            operation: Operation::Facets,
            ..Default::default()
        };
        let r = run(&s, &q);
        assert!(r.partial);
        assert!(r.facets["kinds"]
            .as_array()
            .unwrap()
            .iter()
            .any(|b| b["count"] == 2));
        assert_eq!(file_id("checkout", "code.py"), s.files[0].file_id);
    }
    fn many_folder_snapshot() -> QuerySnapshot {
        let mut s = snapshot(&[("code.py", "def target(): pass\n")]);
        for n in 0..240 {
            s.coverage.push(crate::code::ScanCoverage {
                rel_path: format!("folder_{n:04}_with_a_long_but_valid_first_party_directory_name/notes.js"),
                status: "unsupported".into(),
                ..Default::default()
            });
        }
        s
    }
    #[test]
    fn facet_bucket_pages_reconstruct_complete_coverage_under_native_and_ui_envelopes() {
        let s = many_folder_snapshot();
        for limit in [7168, 8192] {
            let mut q = CodeQuery { operation: Operation::Facets, budgets: QueryBudget { tokens: limit, ..Default::default() }, ..Default::default() };
            let mut collected: BTreeMap<String, BTreeMap<String, usize>> = BTreeMap::new();
            let mut pages = 0;
            let mut total_units = None;
            loop {
                let r = run(&s, &q);
                pages += 1;
                assert!(pages <= 250, "continuation must make bounded progress");
                assert!(r.hits.is_empty() && r.edges.is_empty() && r.paths.is_empty());
                assert!(r.cost.serialized_bytes <= limit);
                assert_eq!(serde_json::to_vec(&r).unwrap().len(), r.cost.serialized_bytes);
                let mut returned = 0;
                for axis in ["kinds", "languages", "states", "folders"] {
                    for bucket in r.facets[axis].as_array().unwrap() {
                        assert_eq!(bucket["unit"], "file");
                        assert_eq!(bucket["exactness"], "exact");
                        let previous = collected.entry(axis.into()).or_default().insert(bucket["value"].as_str().unwrap().into(), bucket["count"].as_u64().unwrap() as usize);
                        assert!(previous.is_none(), "a bucket must not recur on another page");
                        returned += 1;
                    }
                }
                assert!(returned > 0 && returned <= q.budgets.rows);
                let count = |name: &str| r.counts.iter().find(|c| c.name == name).unwrap();
                assert_eq!(count("matched").value, Some(241));
                assert_eq!(count("returned").value, Some(returned));
                assert_eq!(count("returned").unit, "resource_or_edge_or_path_or_facet_bucket");
                if let Some(total) = total_units { assert_eq!(count("matched_units").value, Some(total)); }
                else { total_units = count("matched_units").value; }
                assert_eq!(count("returned").value.unwrap() + count("omitted").value.unwrap(), total_units.unwrap());
                if r.next_cursor.is_none() { break; }
                q.cursor = r.next_cursor;
                q.expected_receipt = r.receipt;
            }
            assert!(pages > 1);
            assert_eq!(collected["folders"].len(), 241);
            assert_eq!(collected["folders"][""], 1);
            for axis in ["kinds", "languages", "states", "folders"] {
                assert_eq!(collected[axis].values().sum::<usize>(), 241);
            }
            assert_eq!(collected.values().map(|b| b.len()).sum::<usize>(), total_units.unwrap());
        }
    }
    #[test]
    fn overview_interleaves_first_file_and_first_facet_with_truthful_unit_counts() {
        let s = many_folder_snapshot();
        let q = CodeQuery { operation: Operation::Overview, budgets: QueryBudget { rows: 2, tokens: 7168, ..Default::default() }, ..Default::default() };
        let r = run(&s, &q);
        assert_eq!(r.hits.len(), 1);
        assert_eq!(r.facets["kinds"].as_array().unwrap().len(), 1);
        assert_eq!(r.facets["kinds"][0]["count"], 241);
        assert!(r.next_cursor.is_some());
        let returned = r.counts.iter().find(|c| c.name == "returned").unwrap();
        assert_eq!(returned.value, Some(2));
        assert!(returned.unit.ends_with("facet_bucket"));
        assert!(r.cost.serialized_bytes <= 7168);
    }
    #[test]
    fn facet_continuations_bind_filter_head_and_complete_bucket_budget() {
        let s = many_folder_snapshot();
        let q = CodeQuery { operation: Operation::Facets, budgets: QueryBudget { rows: 1, tokens: 7168, ..Default::default() }, ..Default::default() };
        let first = run(&s, &q);
        assert!(first.hits.is_empty() && first.next_cursor.is_some());
        let mut more = q.clone();
        more.cursor = first.next_cursor;
        more.expected_receipt = first.receipt;
        let second = run(&s, &more);
        assert!(second.hits.is_empty());
        assert_eq!(second.facets["kinds"].as_array().unwrap().len(), 0);
        assert_eq!(second.facets["languages"].as_array().unwrap().len(), 1);
        let mut changed_filter = more.clone();
        changed_filter.filter.path_prefix = Some("folder_0000_with_a_long_but_valid_first_party_directory_name".into());
        assert_eq!(run_query(&s, &changed_filter, &AtomicBool::new(false), &[3;32]).unwrap_err().code, "stale_cursor");
        let mut changed_head = many_folder_snapshot();
        changed_head.receipt.evidence.head_generation = "new-head".into();
        assert_eq!(run_query(&changed_head, &more, &AtomicBool::new(false), &[3;32]).unwrap_err().code, "stale_cursor");
        let tiny = CodeQuery { operation: Operation::Facets, budgets: QueryBudget { bytes:512, tokens:512, ..Default::default() }, ..Default::default() };
        assert_eq!(run_query(&s, &tiny, &AtomicBool::new(false), &[3;32]).unwrap_err().code, "budget_exceeded");
    }
    #[test]
    fn public_pipeline_draft_is_not_admitted_and_cancel_is_typed() {
        let s = snapshot(&[]);
        let q = CodeQuery {
            operation: Operation::Pipeline,
            ..Default::default()
        };
        assert_eq!(
            run_query(&s, &q, &AtomicBool::new(false), &[0; 32])
                .unwrap_err()
                .code,
            "unsupported"
        );
        assert_eq!(
            run_query(&s, &CodeQuery::default(), &AtomicBool::new(true), &[0; 32])
                .unwrap_err()
                .code,
            "cancelled"
        );
    }
}
