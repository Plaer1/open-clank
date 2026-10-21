//! Bounded code-only read contract. No paths are opened or authority inferred here.
use crate::code_evidence::SourceLocator;
use crate::store::code_evidence::CodeEvidenceReceipt;
use serde::{Deserialize, Serialize};

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
            request_id: None,
        }
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelStatus {
    pub channel: Channel,
    pub available: bool,
    pub reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperationCapability {
    pub operation: Operation,
    pub available: bool,
    pub reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelExecution {
    pub channel: Channel,
    pub requested: bool,
    pub enabled: bool,
    pub executed: bool,
    pub cancelled: bool,
    pub error: Option<QueryError>,
    pub producer: Option<String>,
    pub config_fingerprint: Option<String>,
    pub candidate_count: Option<usize>,
    pub inspected_records: usize,
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
    pub hits: Vec<QueryHit>,
    pub edges: Vec<QueryEdge>,
    pub paths: Vec<QueryPath>,
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
    for path in [&query.filter.rel_path, &query.filter.path_prefix]
        .into_iter()
        .flatten()
    {
        validate_relative(path)?;
    }
    validate_filter(&query.filter)?;
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
        hits: vec![],
        edges: vec![],
        paths: vec![],
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
        if !query.fields.contains(&field) {
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
    for occurrence in snapshot.files.iter().flat_map(|file| &file.occurrences) {
        work.tick(1)?;
        occurrence_kinds.insert(occurrence.id.as_str(), crate::code_evidence::relation_name(occurrence));
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
        if let (Some(caller), Some(target)) = (&resolution.caller_id, &resolution.target_id) {
            let from = format!("decl:{caller}");
            let to = format!("decl:{target}");
            if all.contains_key(&from)
                && all.contains_key(&to)
                && matches!(
                    resolution.state,
                    ResolutionState::Exact | ResolutionState::Inferred
                )
            {
                edges.push(QueryEdge {
                    from,
                    to,
                    occurrence_id: occ.clone(),
                    relation_kind: occurrence_kinds.get(resolution.occurrence_id.as_str()).ok_or_else(|| error("invalid_evidence", "Missing typed occurrence"))?.to_string(),
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
                if all.contains_key(id) {
                    selected.insert(id.clone());
                } else {
                    return Err(error("missing", "Selected code resource is unavailable"));
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
            if query.operation == Operation::Path && result.paths.is_empty() {
                result
                    .reasons
                    .push("no_path_within_hop_and_work_bounds".into());
            }
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
        project(hit, &query.fields);
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
            exactness: "exact".into(),
        },
    ];
    result.cost.inspected_records = work.used;
    let search_executed = query.operation == Operation::Search
        || query
            .stages
            .iter()
            .any(|s| s.operation == StageOperation::Find);
    let structural_executed = matches!(
        query.operation,
        Operation::Neighbors | Operation::Impact | Operation::Path
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
    let coverage_partial = snapshot.coverage.iter().any(|c| {
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
    let total = units.len();
    if offset > total {
        return Err(error(
            "stale_cursor",
            "Code continuation exceeds result set",
        ));
    }
    let unit_name = if with_facets { "resource_or_edge_or_path_or_facet_bucket" } else { "resource_or_edge_or_path" };
    result.counts.push(QueryCount {
        name: "matched_units".into(),
        value: Some(total),
        unit: unit_name.into(),
        exactness: "exact".into(),
    });
    let mut consumed = offset;
    let limit = query.budgets.bytes.min(query.budgets.tokens);
    for unit in units.into_iter().skip(offset).take(query.budgets.rows) {
        let added_facet = match unit {
            Unit::Hit(h) => { result.hits.push(h); None },
            Unit::Edge(e) => { result.edges.push(e); None },
            Unit::Path(p) => { result.paths.push(p); None },
            Unit::Facet(axis, bucket) => { result.facets[axis].as_array_mut().unwrap().push(bucket); Some(axis) },
        };
        consumed += 1;
        result.next_cursor =
            (consumed < total).then(|| encode_cursor(&fingerprint, consumed, cursor_secret));
        result.partial = coverage_partial || consumed < total;
        set_returned_counts(&mut result, total, consumed - offset, unit_name);
        if measured(&mut result)? > limit {
            if let Some(axis) = added_facet {
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
    result.partial = coverage_partial || consumed < total;
    set_returned_counts(&mut result, total, consumed - offset, unit_name);
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
        }
    }
    fn run(s: &QuerySnapshot, q: &CodeQuery) -> CodeQueryResult {
        run_query(s, q, &AtomicBool::new(false), &[3; 32]).unwrap()
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
