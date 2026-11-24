//! Source-only static evidence. Callers supply already-admitted bytes; this
//! module never opens a path, authorizes a resource, executes source, or calls
//! a language server/model. `exact` means a static binding, NOT runtime dispatch.

use serde::{Deserialize, Serialize};

#[path = "code_storage.rs"]
pub mod storage;
#[path = "code_manifest.rs"]
mod manifest;
#[path = "code_sql.rs"]
mod sql;
#[path = "code_error_boundaries.rs"]
pub mod error_boundaries;
#[path = "code_local_slice.rs"]
pub mod local_slice;
#[path = "code_flow.rs"]
pub mod function_flow;
#[path = "code_go.rs"]
mod go;
#[path = "code_java.rs"]
mod java;
#[path = "code_js_events.rs"]
mod js_events;

pub const EVIDENCE_FORMAT_VERSION: u32 = 1;
pub const EXTRACTOR_VERSION: &str = "graph-static-10";
/// The resolver loads only module interfaces and one active source at a time.
/// This bounds the serialized interface window independently of active evidence.
pub const MAX_MODULE_INTERFACE_BYTES: usize = 512 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExtractionBudget {
    pub max_source_bytes: usize,
    pub max_ast_nodes: usize,
    pub max_depth: usize,
    pub max_declarations: usize,
    pub max_occurrences: usize,
    pub max_bindings: usize,
    pub max_imports: usize,
    pub max_serialized_bytes: usize,
}
impl Default for ExtractionBudget {
    fn default() -> Self {
        Self {
            max_source_bytes: crate::code_index_limits::HARD_LIMITS.max_file_bytes,
            max_ast_nodes: 2_400_000,
            max_depth: 256,
            max_declarations: 20_000,
            max_occurrences: 100_000,
            max_bindings: 100_000,
            max_imports: 20_000,
            max_serialized_bytes: 8 * 1024 * 1024,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResolutionBudget {
    pub max_files: usize,
    pub max_corpus_bytes: usize,
    pub max_records: usize,
    pub max_alias_hops: usize,
    pub max_candidates: usize,
    pub max_work: usize,
    pub max_output_bytes: usize,
    pub max_path_hops: usize,
}
impl Default for ResolutionBudget {
    fn default() -> Self {
        Self {
            max_files: 10_000,
            max_corpus_bytes: 64 * 1024 * 1024,
            max_records: 1_000_000,
            max_alias_hops: 32,
            max_candidates: 128,
            max_work: 4_000_000,
            max_output_bytes: 64 * 1024 * 1024,
            max_path_hops: 64,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceErrorCode {
    UnsupportedLanguage,
    SyntaxError,
    InvalidSource,
    InvalidCorpus,
    InvalidEvidence,
    BudgetExceeded,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvidenceError {
    pub code: EvidenceErrorCode,
    pub message: String,
    pub incomplete: bool,
}

/// Byte offsets are UTF-8 offsets; end is exclusive. Lines are one-based;
/// columns are zero-based byte columns (not Unicode/display columns).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceSpan {
    pub start_byte: u64,
    pub end_byte: u64,
    pub start_line: u32,
    pub start_column: u32,
    pub end_line: u32,
    pub end_column: u32,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceLocator {
    pub file_id: String,
    pub rel_path: String,
    pub source_hash: String,
    pub span: SourceSpan,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LanguageCapability {
    pub language: String,
    pub dialect: String,
    pub parser: String,
    pub grammar_version: String,
    pub extractor_version: String,
    pub available: bool,
    pub binding_level: String,
    pub limitations: Vec<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvidenceDiagnostic {
    pub code: String,
    pub message: String,
    pub source: Option<SourceLocator>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IdentityState {
    Stable,
    AmbiguousSnapshotLocal,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DocumentationEvidence {
    pub text: String,
    pub source: SourceLocator,
}
/// Versioned syntactic grouping evidence, never behavioral equivalence.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructuralShape {
    pub version: u32,
    pub status: String,
    pub body: Option<SourceSpan>,
    pub nodes: Vec<String>,
    pub sha256: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeclarationEvidence {
    pub id: String,
    pub identity_key: String,
    pub identity_state: IdentityState,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub lexical_parent_id: Option<String>,
    pub scope_id: String,
    pub source: SourceLocator,
    pub name_span: SourceSpan,
    pub signature: String,
    pub signature_hash: String,
    pub content_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structural_shape: Option<StructuralShape>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub function_flow: Option<function_flow::FunctionFlow>,
    /// Only an actually parsed export/pub modifier; not a naming convention.
    pub explicit_export: bool,
    pub conditional: bool,
    pub documentation: Option<DocumentationEvidence>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LexicalScope {
    pub id: String,
    pub kind: String,
    pub parent_id: Option<String>,
    pub declaration_id: Option<String>,
    pub source: SourceLocator,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BindingKind {
    Declaration,
    Import,
    Parameter,
    Local,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindingEvidence {
    pub id: String,
    pub name: String,
    pub kind: BindingKind,
    pub scope_id: String,
    pub source: SourceLocator,
    pub declaration_id: Option<String>,
    pub import_id: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OccurrenceKind {
    Call,
    ValueReference,
    TypeReference,
    ImportReference,
}
/// Typed syntax rules share the existing occurrence/resolution witnesses.
pub fn relation_code(o:&OccurrenceEvidence)->i64 {
    match o.syntax_kind.as_str() { "inherits"=>5,"implements"|"go_interface_assertion"=>6,"jsx_render"=>7,"fastapi_route_registration"=>8,"overrides"=>9,"manifest_dependency"=>10,"sql_table_reference"=>11,"callback_registration"=>12,"java_event_publish"|"js_event_dispatch"=>13,"java_event_consume"=>14,_=>match o.kind {OccurrenceKind::Call=>1,OccurrenceKind::ImportReference=>2,OccurrenceKind::ValueReference=>3,OccurrenceKind::TypeReference=>4} }
}
pub fn relation_name(o:&OccurrenceEvidence)->&'static str {
    match relation_code(o) {1=>"call",2=>"import",3=>"value_reference",4=>"type_reference",5=>"inherits",6=>"implements",7=>"render",8=>"route",9=>"overrides",10=>"manifest_dependency",11=>"sql_table_reference",12=>"callback_registration",13=>"event_publish",14=>"event_consume",_=>"unsupported"}
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OccurrenceEvidence {
    pub id: String,
    pub kind: OccurrenceKind,
    pub symbol_text: String,
    pub source: SourceLocator,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_source: Option<SourceLocator>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub related_source: Option<SourceLocator>,
    pub caller_id: Option<String>,
    pub scope_id: String,
    pub syntax_kind: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImportEvidence {
    pub id: String,
    pub scope_id: String,
    pub local_name: String,
    pub module_path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub module_source: Option<SourceLocator>,
    pub imported_name: Option<String>,
    pub alias: Option<String>,
    pub reexport: bool,
    pub type_only: bool,
    pub is_glob: bool,
    pub source: SourceLocator,
    pub syntax_kind: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileEvidence {
    pub format_version: u32,
    pub checkout_id: String,
    pub file_id: String,
    pub rel_path: String,
    pub source_hash: String,
    pub source_len: u64,
    pub language: String,
    pub capability: LanguageCapability,
    pub producer_fingerprint: String,
    pub root_scope_id: String,
    pub scopes: Vec<LexicalScope>,
    pub bindings: Vec<BindingEvidence>,
    pub declarations: Vec<DeclarationEvidence>,
    pub occurrences: Vec<OccurrenceEvidence>,
    pub imports: Vec<ImportEvidence>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_value_slices: Option<Vec<local_slice::LocalValueSlice>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error_boundaries: Option<Vec<error_boundaries::ErrorProfile>>,
    pub public_surface_fingerprint: String,
    pub diagnostics: Vec<EvidenceDiagnostic>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnavailableFile {
    pub rel_path: String,
    pub source_hash: Option<String>,
    pub reason: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CorpusInput {
    pub checkout_id: String,
    pub files: Vec<FileEvidence>,
    pub unavailable_files: Vec<UnavailableFile>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResolutionState {
    Exact,
    Inferred,
    Ambiguous,
    Unresolved,
    Unsupported,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolutionWitness {
    pub kind: String,
    pub source: SourceLocator,
    pub description: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileDependency {
    pub rel_path: String,
    pub source_hash: String,
    pub public_surface_fingerprint: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResolutionEvidence {
    pub occurrence_id: String,
    pub source: SourceLocator,
    pub caller_id: Option<String>,
    pub target_id: Option<String>,
    pub candidate_ids: Vec<String>,
    pub state: ResolutionState,
    pub reason: String,
    pub witnesses: Vec<ResolutionWitness>,
    pub dependencies: Vec<FileDependency>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CorpusEvidence {
    pub format_version: u32,
    pub checkout_id: String,
    pub producer_fingerprint: String,
    /// False if caller reported unavailable source, even though traversal completed.
    pub complete: bool,
    pub resolutions: Vec<ResolutionEvidence>,
    pub dependencies: Vec<FileDependency>,
    pub unavailable_files: Vec<UnavailableFile>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SourcePredicate {
    File,
    Declaration {
        declaration_id: String,
        declaration_kind: String,
        qualified_name: String,
        signature_hash: String,
    },
    Occurrence {
        occurrence_id: String,
        occurrence_kind: OccurrenceKind,
        symbol_text: String,
    },
    ImportBinding {
        import_id: String,
        local_name: String,
        module_path: String,
        imported_name: Option<String>,
    },
    ExactText {
        text: String,
    },
    /// Private Host phase only; public code-query admission rejects this predicate.
    ManifestFileDependency { expected_spec_sha256: String },
    Unsupported {
        predicate: String,
    },
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceClaim {
    pub source: SourceLocator,
    pub predicate: SourcePredicate,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationState {
    Current,
    Stale,
    Invalid,
    Unsupported,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvidenceValidation {
    pub state: ValidationState,
    pub reason: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub manifest_comparison: Option<ManifestValidation>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag="state", rename_all="snake_case", deny_unknown_fields)]
pub enum ManifestValidation {
    Match { key_start: u64, key_end: u64, value_start: u64, value_end: u64 },
    NoMatch,
    Unsupported,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContinuityCandidate {
    pub before_id: String,
    pub after_id: String,
    pub state: ResolutionState,
    pub reason: String,
}

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use tree_sitter::Node;

impl std::fmt::Display for EvidenceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}: {}", self.code, self.message)
    }
}
impl std::error::Error for EvidenceError {}
fn error(code: EvidenceErrorCode, message: impl Into<String>) -> EvidenceError {
    EvidenceError {
        incomplete: matches!(code, EvidenceErrorCode::BudgetExceeded),
        code,
        message: message.into(),
    }
}
fn limit(ok: bool, what: &str) -> Result<(), EvidenceError> {
    if ok {
        Ok(())
    } else {
        Err(error(EvidenceErrorCode::BudgetExceeded, what))
    }
}
fn digest(parts: &[&str]) -> String {
    let mut h = blake3::Hasher::new();
    for part in parts {
        h.update(&(part.len() as u64).to_le_bytes());
        h.update(part.as_bytes());
    }
    h.finalize().to_hex().to_string()
}
fn valid_path(path: &str) -> bool {
    !path.is_empty()
        && !path.starts_with('/')
        && !path.contains('\0')
        && !path.split('/').any(|p| p == ".." || p.is_empty())
}
fn bounded_json<T: Serialize>(value: &T, maximum: usize) -> Result<usize, EvidenceError> {
    struct Counter {
        bytes: usize,
        maximum: usize,
    }
    impl std::io::Write for Counter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.bytes = self
                .bytes
                .checked_add(bytes.len())
                .ok_or_else(|| std::io::Error::other("size overflow"))?;
            if self.bytes > self.maximum {
                return Err(std::io::Error::other("serialized evidence limit"));
            }
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut counter = Counter { bytes: 0, maximum };
    serde_json::to_writer(&mut counter, value).map_err(|_| {
        error(
            EvidenceErrorCode::BudgetExceeded,
            "serialized evidence limit",
        )
    })?;
    Ok(counter.bytes)
}

pub fn language_capability(rel_path: &str) -> LanguageCapability {
    if rel_path.ends_with(".sql") { return LanguageCapability {language:"sql_static".into(),dialect:"sqlite_shaped_subset".into(),parser:"bounded_lexer".into(),grammar_version:"sql-static-v2".into(),extractor_version:EXTRACTOR_VERSION.into(),available:true,binding_level:"syntactic_declaration_only".into(),limitations:vec!["No SQL execution or database identity".into(),"Unsupported statements make coverage partial".into()]}; }
    if rel_path.rsplit('/').next() == Some("package.json") {
        return LanguageCapability {language:"package_manifest".into(),dialect:"package_json".into(),parser:"serde_json+bounded_key_spans".into(),grammar_version:"serde_json@1.0.150".into(),extractor_version:EXTRACTOR_VERSION.into(),available:true,binding_level:"static_manifest_keys_only".into(),limitations:vec!["Dependency keys and bounded literal local import aliases; scripts and arbitrary values are excluded".into(),"Declared package association is inferred, not runtime resolution".into()]};
    }
    let extension = rel_path.rsplit('.').next().unwrap_or("");
    if extension == "go" {
        return LanguageCapability {
            language: "go".into(), dialect: "go".into(), parser: "tree-sitter".into(),
            grammar_version: "tree-sitter-go@0.25.0".into(), extractor_version: EXTRACTOR_VERSION.into(),
            available: true, binding_level: "explicit_same_file_assertions".into(),
            limitations: vec![
                "Go profile covers named declarations and explicit same-file pointer conformance assertions".into(),
                "Package loading, calls, implicit method sets and compiler verification are outside this profile".into(),
            ],
        };
    }
    if extension == "java" {
        return LanguageCapability {
            language: "java".into(), dialect: "java".into(), parser: "tree-sitter".into(),
            grammar_version: "tree-sitter-java@0.23.5".into(), extractor_version: EXTRACTOR_VERSION.into(),
            available: true, binding_level: "explicit_same_file_spring_events".into(),
            limitations: vec![
                "Java profile covers named types and direct qualified Spring event publication and listener syntax".into(),
                "Constructor binding, framework dispatch, external definitions and dynamic events are outside this profile".into(),
            ],
        };
    }
    let (language, dialect, grammar, available) = match extension {
        "rs" => ("rust", "rust", "tree-sitter-rust@0.24.2", true),
        "py" => ("python", "python", "tree-sitter-python@0.25.0", true),
        "ts" | "mts" | "cts" => (
            "typescript",
            "typescript",
            "tree-sitter-typescript@0.23.2",
            true,
        ),
        "tsx" => ("typescript", "tsx", "tree-sitter-typescript@0.23.2", true),
        "js" | "jsx" | "mjs" | "cjs" => ("javascript", "javascript", "tree-sitter-javascript@0.25.0", true),
        _ => ("unsupported", extension, "unavailable", false),
    };
    LanguageCapability { language:language.into(), dialect:dialect.into(), parser:"tree-sitter".into(), grammar_version:grammar.into(), extractor_version:EXTRACTOR_VERSION.into(), available, binding_level:"static_lexical_only".into(), limitations:vec!["No runtime dispatch, typechecker, package-loader or macro expansion; framework rules cover only named static FastAPI/JSX syntax".into(), "Dynamic receivers and runtime-assigned callable values are not exact".into()] }
}
fn span(node: Node<'_>) -> SourceSpan {
    SourceSpan {
        start_byte: node.start_byte() as u64,
        end_byte: node.end_byte() as u64,
        start_line: node.start_position().row as u32 + 1,
        start_column: node.start_position().column as u32,
        end_line: node.end_position().row as u32 + 1,
        end_column: node.end_position().column as u32,
    }
}
fn text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
    &source[node.byte_range()]
}
fn children(node: Node<'_>) -> Vec<Node<'_>> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor).collect()
}
fn is_identifier(node: Node<'_>) -> bool {
    matches!(
        node.kind(),
        "identifier"
            | "type_identifier"
            | "field_identifier"
            | "shorthand_property_identifier"
            | "shorthand_property_identifier_pattern"
    )
}

struct Builder<'a> {
    source: &'a str,
    file: FileEvidence,
    budget: &'a ExtractionBudget,
    contexts: HashMap<usize, (String, Option<String>)>,
    skip: HashSet<usize>,
    declaration_nodes: HashMap<usize, usize>,
    scope_indexes: HashMap<String, usize>,
    work: usize,
    output_bytes: usize,
}
impl Builder<'_> {
    fn structural_shape(&mut self, body: Node<'_>, depth: usize) -> Result<StructuralShape, EvidenceError> {
        const MAX_NODES: usize = 4096;
        const MAX_BYTES: usize = 65536;
        const MAX_DEPTH: usize = 64;
        let mut nodes = Vec::new();
        let mut bytes = 0usize;
        let mut stack = vec![(body, None::<String>, false, 0usize)];
        while let Some((node, field, closing, local_depth)) = stack.pop() {
            if node.kind().contains("comment") { continue; }
            self.tick(depth.saturating_add(local_depth))?;
            if local_depth > MAX_DEPTH || nodes.len() >= MAX_NODES {
                return Ok(StructuralShape { version: 1, status: "skipped_budget".into(), body: Some(span(body)), nodes: vec![], sha256: None });
            }
            let kind = node.kind();
            let nested = node.id() != body.id() && (self.declaration_kind(node).is_some() || matches!(node.kind(), "lambda" | "arrow_function" | "function_expression" | "generator_function"));
            let category = if nested { "nested_declaration" }
                else if is_identifier(node) || matches!(kind, "property_identifier" | "private_property_identifier" | "shorthand_property_identifier_pattern") { "identifier" }
                else if matches!(kind, "integer" | "integer_literal") { "integer_literal" }
                else if matches!(kind, "float" | "float_literal") { "float_literal" }
                else if kind == "number" { "number_literal" }
                else if matches!(kind, "true" | "false") { "boolean_literal" }
                else if matches!(kind, "none" | "null") { "null_literal" }
                else if matches!(kind, "string_content" | "string_fragment" | "escape_sequence") { "string_fragment" }
                else { kind };
            let token = if closing { ")".to_string() } else { format!("({}:{}", field.as_deref().unwrap_or(""), category) };
            bytes = bytes.saturating_add(token.len());
            if bytes > MAX_BYTES { return Ok(StructuralShape { version: 1, status: "skipped_budget".into(), body: Some(span(body)), nodes: vec![], sha256: None }); }
            nodes.push(token);
            if closing || nested || category == "identifier" || matches!(category, "integer_literal" | "float_literal" | "number_literal" | "boolean_literal" | "null_literal" | "string_fragment") { continue; }
            stack.push((node, None, true, local_depth));
            for i in (0..node.child_count()).rev() {
                if let Some(child) = node.child(i as u32) {
                    let field = node.field_name_for_child(i as u32).map(str::to_owned);
                    stack.push((child, field, false, local_depth + 1));
                }
            }
        }
        if nodes.len() < 12 { return Ok(StructuralShape { version: 1, status: "skipped_trivial".into(), body: Some(span(body)), nodes: vec![], sha256: None }); }
        let encoded = serde_json::to_vec(&nodes).map_err(|_| error(EvidenceErrorCode::InvalidEvidence, "shape encoding"))?;
        use sha2::Digest;
        let hash = format!("{:x}", sha2::Sha256::digest(encoded));
        Ok(StructuralShape { version: 1, status: "complete".into(), body: Some(span(body)), nodes, sha256: Some(hash) })
    }
    fn locator(&self, node: Node<'_>) -> SourceLocator {
        SourceLocator {
            file_id: self.file.file_id.clone(),
            rel_path: self.file.rel_path.clone(),
            source_hash: self.file.source_hash.clone(),
            span: span(node),
        }
    }
    fn tick(&mut self, depth: usize) -> Result<(), EvidenceError> {
        self.work += 1;
        limit(
            self.work <= self.budget.max_ast_nodes && depth <= self.budget.max_depth,
            "AST traversal/depth limit",
        )
    }
    fn account<T: Serialize>(&mut self, value: &T) -> Result<(), EvidenceError> {
        self.output_bytes = self
            .output_bytes
            .checked_add(bounded_json(value, self.budget.max_serialized_bytes)?)
            .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "output overflow"))?;
        limit(
            self.output_bytes <= self.budget.max_serialized_bytes,
            "extraction output limit",
        )
    }
    fn scope(&mut self, scope: LexicalScope) -> Result<(), EvidenceError> {
        self.account(&scope)?;
        self.scope_indexes
            .insert(scope.id.clone(), self.file.scopes.len());
        self.file.scopes.push(scope);
        Ok(())
    }
    fn variable_binding_scope(
        &mut self,
        node: Node<'_>,
        scope: &str,
    ) -> Result<String, EvidenceError> {
        let declared_var = node
            .parent()
            .is_some_and(|p| p.kind() == "variable_declaration")
            || node.kind() == "for_in_statement"
                && node
                    .child_by_field_name("kind")
                    .is_some_and(|kind| kind.kind() == "var");
        if !matches!(self.file.language.as_str(), "typescript" | "javascript") || !declared_var {
            return Ok(scope.into());
        }
        self.callable_binding_scope(scope)
    }
    fn callable_binding_scope(&mut self, scope: &str) -> Result<String, EvidenceError> {
        let mut current = scope.to_string();
        for depth in 0..=self.budget.max_depth {
            self.tick(depth)?;
            let index = *self.scope_indexes.get(&current).ok_or_else(|| {
                error(
                    EvidenceErrorCode::InvalidEvidence,
                    "variable scope unavailable",
                )
            })?;
            let lexical = &self.file.scopes[index];
            if lexical.id == self.file.root_scope_id
                || matches!(
                    lexical.kind.as_str(),
                    "function" | "method" | "function_value" | "anonymous_callable"
                )
            {
                return Ok(current);
            }
            current = lexical.parent_id.clone().ok_or_else(|| {
                error(
                    EvidenceErrorCode::InvalidEvidence,
                    "variable scope parent unavailable",
                )
            })?;
        }
        Err(error(
            EvidenceErrorCode::BudgetExceeded,
            "variable scope depth limit",
        ))
    }
    fn binding(
        &mut self,
        node: Node<'_>,
        name: String,
        scope: &str,
        kind: BindingKind,
        decl: Option<String>,
        import: Option<String>,
    ) -> Result<(), EvidenceError> {
        limit(
            self.file.bindings.len() < self.budget.max_bindings,
            "binding limit",
        )?;
        self.skip.insert(node.id());
        let source = self.locator(node);
        let id = digest(&[
            "binding",
            &self.file.file_id,
            scope,
            &name,
            &format!("{}:{}", node.start_byte(), node.end_byte()),
            &format!("{kind:?}"),
        ]);
        let binding = BindingEvidence {
            id,
            name,
            kind,
            scope_id: scope.into(),
            source,
            declaration_id: decl,
            import_id: import,
        };
        self.account(&binding)?;
        self.file.bindings.push(binding);
        Ok(())
    }
    fn pattern(
        &mut self,
        node: Node<'_>,
        scope: &str,
        kind: BindingKind,
        depth: usize,
    ) -> Result<(), EvidenceError> {
        self.tick(depth)?;
        if is_identifier(node) && node.kind() != "type_identifier" {
            return self.binding(
                node,
                text(node, self.source).into(),
                scope,
                kind,
                None,
                None,
            );
        }
        if matches!(
            node.kind(),
            "attribute"
                | "member_expression"
                | "subscript"
                | "subscript_expression"
                | "scoped_identifier"
        ) {
            return Ok(());
        }
        if let Some(left) = node
            .child_by_field_name("pattern")
            .or_else(|| node.child_by_field_name("name"))
            .or_else(|| node.child_by_field_name("left"))
        {
            return self.pattern(left, scope, kind, depth + 1);
        }
        for child in children(node) {
            if !matches!(
                child.kind(),
                "type_annotation" | "type" | "default_value" | "decorator"
            ) {
                self.pattern(child, scope, kind.clone(), depth + 1)?;
            }
        }
        Ok(())
    }
    fn signature(&mut self, node: Node<'_>) -> Result<String, EvidenceError> {
        if let Some(value) = node
            .child_by_field_name("value")
            .or_else(|| node.child_by_field_name("right"))
            .filter(|n| {
                matches!(
                    n.kind(),
                    "arrow_function" | "function_expression" | "lambda"
                )
            })
        {
            let name = node
                .child_by_field_name("name")
                .or_else(|| node.child_by_field_name("left"))
                .map(|n| text(n, self.source))
                .unwrap_or("")
                .to_string();
            return Ok(format!("{} {}", name, self.signature(value)?));
        }
        let stop = node
            .child_by_field_name("body")
            .or_else(|| {
                node.child_by_field_name("value")
                    .filter(|n| matches!(n.kind(), "arrow_function" | "function_expression"))
            })
            .map(|n| n.start_byte())
            .unwrap_or(node.end_byte());
        let mut tokens = Vec::new();
        let mut stack = vec![(node, 0usize)];
        while let Some((n, d)) = stack.pop() {
            self.tick(d)?;
            if n.start_byte() >= stop || n.kind().contains("comment") {
                continue;
            }
            if n.child_count() == 0
                || matches!(
                    n.kind(),
                    "string" | "string_literal" | "raw_string_literal" | "template_string"
                )
            {
                tokens.push(text(n, self.source).to_string());
            } else {
                for i in (0..n.child_count()).rev() {
                    if let Some(c) = n.child(i as u32) {
                        stack.push((c, d + 1));
                    }
                }
            }
        }
        Ok(tokens.join(" "))
    }
    fn declaration_kind(&self, node: Node<'_>) -> Option<&'static str> {
        Some(match node.kind() {
            "function_item"
            | "function_signature_item"
            | "function_definition"
            | "function_declaration"
            | "function_signature"
            | "generator_function_declaration" => "function",
            "method_definition" | "method_signature" | "abstract_method_signature" => "method",
            "class_definition" | "class_declaration" | "abstract_class_declaration" => "class",
            "struct_item" => "struct",
            "enum_item" | "enum_declaration" => "enum",
            "trait_item" => "trait",
            "interface_declaration" => "interface",
            "type_item" | "type_alias_declaration" => "type_alias",
            "mod_item" | "internal_module" => "module",
            "const_item" => "constant",
            "static_item" => "static",
            "impl_item" => "implementation",
            "variable_declarator"
                if node.child_by_field_name("value").is_some_and(|n| {
                    matches!(n.kind(), "arrow_function" | "function_expression")
                }) =>
            {
                "function_value"
            }
            "assignment"
                if node
                    .child_by_field_name("right")
                    .is_some_and(|n| n.kind() == "lambda")
                    && node
                        .child_by_field_name("left")
                        .is_some_and(|n| n.kind() == "identifier") =>
            {
                "function_value"
            }
            _ => return None,
        })
    }
    fn imported(
        &mut self,
        node: Node<'_>,
        scope: &str,
        local: String,
        module: String,
        imported: Option<String>,
        alias: Option<String>,
        glob: bool,
        reexport: bool,
        type_only: bool,
    ) -> Result<(), EvidenceError> {
        limit(
            self.file.imports.len() < self.budget.max_imports,
            "import limit",
        )?;
        let id = digest(&[
            "import",
            &self.file.file_id,
            &self.file.source_hash,
            &node.start_byte().to_string(),
            &local,
            &module,
        ]);
        let import = ImportEvidence {
            id: id.clone(),
            scope_id: scope.into(),
            local_name: local.clone(),
            module_path: module,
            module_source: None,
            imported_name: imported,
            alias,
            reexport,
            type_only,
            is_glob: glob,
            source: self.locator(node),
            syntax_kind: node.kind().into(),
        };
        self.account(&import)?;
        self.file.imports.push(import);
        self.binding(node, local, scope, BindingKind::Import, None, Some(id))?;
        self.mark_skip(node);
        Ok(())
    }
    fn mark_skip(&mut self, node: Node<'_>) {
        let mut stack = vec![node];
        while let Some(n) = stack.pop() {
            self.skip.insert(n.id());
            stack.extend(children(n));
        }
    }
    fn rust_use(
        &mut self,
        node: Node<'_>,
        scope: &str,
        prefix: &str,
        reexport: bool,
        depth: usize,
    ) -> Result<(), EvidenceError> {
        self.tick(depth)?;
        if node.kind() == "scoped_use_list" {
            let path = node
                .child_by_field_name("path")
                .map(|n| text(n, self.source))
                .unwrap_or("");
            let joined = if prefix.is_empty() {
                path.into()
            } else {
                format!("{prefix}::{path}")
            };
            if let Some(list) = node.child_by_field_name("list") {
                for child in children(list) {
                    self.rust_use(child, scope, &joined, reexport, depth + 1)?;
                }
            }
            return Ok(());
        }
        if node.kind() == "use_list" {
            for child in children(node) {
                self.rust_use(child, scope, prefix, reexport, depth + 1)?;
            }
            return Ok(());
        }
        let (path, alias) = if node.kind() == "use_as_clause" {
            (
                node.child_by_field_name("path")
                    .map(|n| text(n, self.source))
                    .unwrap_or(""),
                node.child_by_field_name("alias")
                    .map(|n| text(n, self.source).to_string()),
            )
        } else {
            (text(node, self.source), None)
        };
        let full = if prefix.is_empty() {
            path.into()
        } else {
            format!("{prefix}::{path}")
        };
        let mut parts = full.rsplitn(2, "::");
        let last = parts.next().unwrap_or("");
        let module = parts.next().unwrap_or("").to_string();
        let glob = last == "*";
        let local = alias.clone().unwrap_or_else(|| last.to_string());
        self.imported(
            node,
            scope,
            local,
            module,
            (!glob).then(|| last.to_string()),
            alias,
            glob,
            reexport,
            false,
        )
    }
    fn imports(&mut self, node: Node<'_>, scope: &str) -> Result<bool, EvidenceError> {
        match node.kind() {
            "import_statement" if self.file.language == "python" => {
                for child in children(node) {
                    let (path, alias) = if child.kind() == "aliased_import" {
                        (
                            child.child_by_field_name("name").unwrap(),
                            child.child_by_field_name("alias"),
                        )
                    } else {
                        (child, None)
                    };
                    let module = text(path, self.source).to_string();
                    let local = alias
                        .map(|n| text(n, self.source).to_string())
                        .unwrap_or_else(|| module.split('.').next().unwrap_or("").into());
                    let binding_module = if alias.is_none() {
                        module.split('.').next().unwrap_or("").into()
                    } else {
                        module
                    };
                    self.imported(
                        child,
                        scope,
                        local,
                        binding_module,
                        None,
                        alias.map(|n| text(n, self.source).into()),
                        false,
                        false,
                        false,
                    )?;
                }
                self.mark_skip(node);
                Ok(true)
            }
            "import_from_statement" => {
                let module = node
                    .child_by_field_name("module_name")
                    .map(|n| text(n, self.source))
                    .unwrap_or("")
                    .to_string();
                for child in children(node) {
                    if Some(child) == node.child_by_field_name("module_name") {
                        continue;
                    }
                    let (name, alias) = if child.kind() == "aliased_import" {
                        (
                            child.child_by_field_name("name").unwrap(),
                            child.child_by_field_name("alias"),
                        )
                    } else {
                        (child, None)
                    };
                    let original = text(name, self.source).to_string();
                    let glob = child.kind() == "wildcard_import";
                    let local = alias
                        .map(|n| text(n, self.source).into())
                        .unwrap_or_else(|| original.clone());
                    self.imported(
                        child,
                        scope,
                        local,
                        module.clone(),
                        (!glob).then_some(original),
                        alias.map(|n| text(n, self.source).into()),
                        glob,
                        false,
                        false,
                    )?;
                }
                self.mark_skip(node);
                Ok(true)
            }
            "use_declaration" => {
                let reexport = children(node)
                    .iter()
                    .any(|n| n.kind() == "visibility_modifier");
                if let Some(arg) = node.child_by_field_name("argument") {
                    self.rust_use(arg, scope, "", reexport, 0)?;
                }
                self.mark_skip(node);
                Ok(true)
            }
            "variable_declarator" if matches!(self.file.language.as_str(),"javascript"|"typescript") => {
                let Some(call)=node.child_by_field_name("value").filter(|n|n.kind()=="call_expression") else{return Ok(false);};
                if call.child_by_field_name("function").is_none_or(|n|n.kind()!="identifier"||text(n,self.source)!="require"){return Ok(false);}
                let Some(args)=call.child_by_field_name("arguments") else{return Ok(false);};
                let args=children(args);if args.len()!=1||args[0].kind()!="string"{return Ok(false);}
                let Some(name)=node.child_by_field_name("name").filter(|n|n.kind()=="identifier") else{return Ok(false);};
                let module=text(args[0],self.source).trim_matches(['\'','"']).to_owned();
                if module.contains('\\'){return Ok(false);}
                self.imported(node,scope,text(name,self.source).into(),module,None,None,false,false,false)?;
                self.mark_skip(node);Ok(true)
            }
            "import_statement" | "export_statement" if matches!(self.file.language.as_str(), "typescript" | "javascript") => {
                let source = node.child_by_field_name("source");
                let Some(module_node) = source else {
                    return Ok(false);
                };
                let module = text(module_node, self.source)
                    .trim_matches(['\'', '"'])
                    .to_string();
                let reexport = node.kind() == "export_statement";
                let type_only = text(node, self.source)
                    .trim_start()
                    .starts_with("import type ")
                    || text(node, self.source)
                        .trim_start()
                        .starts_with("export type ");
                let first_import = self.file.imports.len();
                let mut stack = children(node);
                while let Some(n) = stack.pop() {
                    self.tick(0)?;
                    if matches!(n.kind(), "import_specifier" | "export_specifier") {
                        let Some(name) = n.child_by_field_name("name") else {
                            continue;
                        };
                        let alias = n.child_by_field_name("alias");
                        self.imported(
                            n,
                            scope,
                            alias
                                .map(|a| text(a, self.source))
                                .unwrap_or(text(name, self.source))
                                .into(),
                            module.clone(),
                            Some(text(name, self.source).into()),
                            alias.map(|a| text(a, self.source).into()),
                            false,
                            reexport,
                            type_only || text(n, self.source).starts_with("type "),
                        )?;
                    } else if n.kind() == "namespace_import" {
                        if let Some(name) = children(n).into_iter().find(|n| is_identifier(*n)) {
                            self.imported(
                                n,
                                scope,
                                text(name, self.source).into(),
                                module.clone(),
                                None,
                                None,
                                false,
                                reexport,
                                type_only,
                            )?;
                        }
                    } else if n.kind() == "identifier"
                        && n.parent().is_some_and(|p| p.kind() == "import_clause")
                    {
                        self.imported(
                            n,
                            scope,
                            text(n, self.source).into(),
                            module.clone(),
                            Some("default".into()),
                            None,
                            false,
                            reexport,
                            type_only,
                        )?;
                    } else {
                        stack.extend(children(n));
                    }
                }
                if !reexport {
                    let specifier = self.locator(module_node);
                    for import in &mut self.file.imports[first_import..] {
                        if import.alias.is_none() && import.imported_name.as_deref() == Some("default") && manifest::package_root(&import.module_path).is_some() { import.module_source = Some(specifier.clone()); }
                    }
                }
                self.mark_skip(node);
                Ok(true)
            }
            _ => Ok(false),
        }
    }
    fn walk(
        &mut self,
        node: Node<'_>,
        scope: &str,
        caller: Option<String>,
        parent: Option<usize>,
        conditional: bool,
        depth: usize,
    ) -> Result<(), EvidenceError> {
        self.tick(depth)?;
        self.contexts
            .insert(node.id(), (scope.into(), caller.clone()));
        let first_import = self.file.imports.len();
        if self.imports(node, scope)? {
            if conditional {
                for index in first_import..self.file.imports.len() {
                    let diagnostic = EvidenceDiagnostic {
                        code: "conditional_import".into(),
                        message: "import binding is conditional on control flow".into(),
                        source: Some(self.file.imports[index].source.clone()),
                    };
                    self.account(&diagnostic)?;
                    self.file.diagnostics.push(diagnostic);
                }
            }
            return Ok(());
        }
        let conditional = conditional
            || matches!(
                node.kind(),
                "if_statement"
                    | "if_expression"
                    | "match_arm"
                    | "try_statement"
                    | "except_clause"
                    | "while_statement"
                    | "for_statement"
            )
            || node.prev_named_sibling().is_some_and(|n| {
                n.kind() == "attribute_item" && text(n, self.source).contains("cfg")
            });
        if let Some(kind) = self.declaration_kind(node) {
            let name_node = node
                .child_by_field_name("name")
                .or_else(|| {
                    (kind == "implementation")
                        .then(|| node.child_by_field_name("type"))
                        .flatten()
                })
                .or_else(|| {
                    (kind == "function_value")
                        .then(|| node.child_by_field_name("left"))
                        .flatten()
                });
            if let Some(name_node) = name_node {
                limit(
                    self.file.declarations.len() < self.budget.max_declarations,
                    "declaration limit",
                )?;
                let name = text(name_node, self.source).to_string();
                let signature = self.signature(node)?;
                let parent_name = parent.map(|i| self.file.declarations[i].qualified_name.clone());
                let qualified_name = parent_name
                    .as_ref()
                    .map(|p| format!("{p}::{name}"))
                    .unwrap_or_else(|| name.clone());
                let parent_key = parent
                    .map(|i| self.file.declarations[i].identity_key.as_str())
                    .unwrap_or("");
                let identity_key = digest(&[
                    "declaration",
                    &self.file.checkout_id,
                    &self.file.rel_path,
                    parent_key,
                    kind,
                    &name,
                    &signature,
                ]);
                let provisional = digest(&[&identity_key, &node.start_byte().to_string()]);
                let index = self.file.declarations.len();
                let route=if self.file.language=="python"&&kind=="function"{fastapi_route(node,self.source,&self.file.imports)}else{None};
                let is_component=matches!(self.file.language.as_str(),"javascript"|"typescript")&&matches!(kind,"function"|"function_value")&&name.chars().next().is_some_and(char::is_uppercase)&&has_owned_jsx(node);
                let declared_kind=if route.is_some(){"route"}else if is_component{"component"}else if kind=="function"&&parent.is_some_and(|p|matches!(self.file.declarations[p].kind.as_str(),"class"|"implementation")){"method"}else{kind};
                let declaration = DeclarationEvidence {
                    id: provisional.clone(),
                    identity_key,
                    identity_state: IdentityState::Stable,
                    kind: declared_kind.into(),
                    name: name.clone(),
                    qualified_name,
                    lexical_parent_id: parent.map(|i| self.file.declarations[i].id.clone()),
                    scope_id: scope.into(),
                    source: self.locator(node),
                    name_span: span(name_node),
                    signature_hash: digest(&[&signature]),
                    signature,
                    content_hash: blake3::hash(text(node, self.source).as_bytes())
                        .to_hex()
                        .to_string(),
                    structural_shape: None,
                    function_flow: None,
                    explicit_export: children(node)
                        .iter()
                        .any(|n| n.kind() == "visibility_modifier")
                        || node
                            .parent()
                            .is_some_and(|n| n.kind() == "export_statement"),
                    conditional,
                    documentation: None,
                };
                self.account(&declaration)?;
                self.file.declarations.push(declaration);
                self.declaration_nodes.insert(node.id(), index);
                if matches!(declared_kind,"method")&&parent.is_some_and(|p|matches!(self.file.declarations[p].kind.as_str(),"class"|"interface")) {
                    let parent_id=&self.file.declarations[parent.unwrap()].id;
                    if self.file.occurrences.iter().any(|o|o.caller_id.as_ref()==Some(parent_id)&&o.syntax_kind=="inherits") {
                        self.occurrence(name_node,OccurrenceKind::TypeReference,name.clone(),scope.into(),Some(provisional.clone()))?;
                        self.file.occurrences.last_mut().unwrap().syntax_kind="overrides".into();
                    }
                }
                if let Some(decorator)=route {
                    self.occurrence(decorator,OccurrenceKind::ValueReference,name.clone(),scope.into(),None)?;
                    self.file.occurrences.last_mut().unwrap().syntax_kind="fastapi_route_registration".into();
                }
                // Base expressions bind in the enclosing scope; the class/impl
                // is the actual source declaration, not a fabricated caller.
                let mut bases=Vec::new();
                if let Some(superclasses)=node.child_by_field_name("superclasses") {
                    for base in children(superclasses){if matches!(base.kind(),"identifier"|"attribute"){bases.push((base,"inherits"));}}
                }
                for heritage in children(node).into_iter().filter(|n|n.kind()=="class_heritage"||n.kind()=="extends_type_clause") {
                    for clause in children(heritage) {
                        if matches!(clause.kind(),"identifier"|"type_identifier"|"member_expression"|"nested_type_identifier"){bases.push((clause,"inherits"));continue;}
                        let relation=if clause.kind()=="implements_clause"{"implements"}else{"inherits"};
                        for base in children(clause){if matches!(base.kind(),"identifier"|"type_identifier"|"member_expression"|"nested_type_identifier"){bases.push((base,relation));}else if base.kind()=="generic_type"{if let Some(name)=base.child_by_field_name("name"){bases.push((name,relation));}}}
                    }
                }
                if let Some(trait_node)=node.child_by_field_name("trait"){bases.push((trait_node,"implements"));}
                for (base,relation) in bases {
                    self.occurrence(base,OccurrenceKind::TypeReference,text(base,self.source).into(),scope.into(),Some(provisional.clone()))?;
                    self.file.occurrences.last_mut().unwrap().syntax_kind=relation.into();self.mark_skip(base);
                }
                if name_node.kind() == "computed_property_name" {
                    // A computed key executes in the surrounding source
                    // context, not in the method whose name it computes.
                    self.walk(
                        name_node,
                        scope,
                        caller.clone(),
                        parent,
                        conditional,
                        depth + 1,
                    )?;
                } else {
                    self.mark_skip(name_node);
                }
                // TS method names are object/class properties, not lexical
                // variables (including inside a field initializer or method).
                if kind != "implementation"
                    && !(matches!(self.file.language.as_str(), "typescript" | "javascript") && kind == "method")
                {
                    let binding_scope = self.variable_binding_scope(node, scope)?;
                    self.binding(
                        name_node,
                        name,
                        &binding_scope,
                        if kind == "function_value" {
                            BindingKind::Local
                        } else {
                            BindingKind::Declaration
                        },
                        Some(provisional.clone()),
                        None,
                    )?;
                }
                let nested = digest(&["scope", &provisional]);
                self.scope(LexicalScope {
                    id: nested.clone(),
                    kind: kind.into(),
                    parent_id: Some(scope.into()),
                    declaration_id: Some(provisional.clone()),
                    source: self.locator(node),
                })?;
                if let Some(previous) = node.prev_named_sibling().filter(|p| {
                    p.kind().contains("comment")
                        && node.start_position().row <= p.end_position().row + 1
                }) {
                    let raw = text(previous, self.source);
                    if raw.starts_with("///") || raw.starts_with("//!") || raw.starts_with("/**") {
                        let doc = DocumentationEvidence {
                            text: raw.into(),
                            source: self.locator(previous),
                        };
                        self.account(&doc)?;
                        self.file.declarations[index].documentation = Some(doc);
                    }
                }
                let callable = node
                    .child_by_field_name("value")
                    .or_else(|| node.child_by_field_name("right"))
                    .filter(|n| {
                        matches!(
                            n.kind(),
                            "arrow_function" | "function_expression" | "lambda"
                        )
                    })
                    .unwrap_or(node);
                let body_caller = if matches!(kind, "function" | "method" | "function_value") {
                    Some(provisional.clone())
                } else {
                    caller.clone()
                };
                if callable.kind() == "function_expression" {
                    if let Some(name) = callable.child_by_field_name("name") {
                        self.binding(
                            name,
                            text(name, self.source).into(),
                            &nested,
                            BindingKind::Local,
                            Some(provisional.clone()),
                            None,
                        )?;
                    }
                }
                if let Some(parameter) = callable.child_by_field_name("parameter") {
                    self.pattern(parameter, &nested, BindingKind::Parameter, depth + 1)?;
                }
                if let Some(params) = callable.child_by_field_name("parameters") {
                    for p in children(params) {
                        if p.kind() == "identifier" {
                            self.pattern(p, &nested, BindingKind::Parameter, depth + 1)?;
                        } else if let Some(n) = p
                            .child_by_field_name("name")
                            .or_else(|| p.child_by_field_name("pattern"))
                        {
                            self.pattern(n, &nested, BindingKind::Parameter, depth + 1)?;
                        } else if matches!(
                            p.kind(),
                            "list_splat_pattern"
                                | "dictionary_splat_pattern"
                                | "tuple_pattern"
                                | "self_parameter"
                        ) {
                            self.pattern(p, &nested, BindingKind::Parameter, depth + 1)?;
                        }
                    }
                }
                if let Some(body) = callable.child_by_field_name("body") {
                    if matches!(kind, "function" | "method" | "function_value") {
                        let shape = self.structural_shape(body, depth + 1)?;
                        self.account(&shape)?;
                        self.file.declarations[index].structural_shape = Some(shape);
                    }
                    if self.file.language == "python" {
                        if let Some(first) = children(body).first() {
                            if first.kind() == "expression_statement" {
                                if let Some(s) = children(*first).first() {
                                    if s.kind() == "string" {
                                        self.file.declarations[index].documentation =
                                            Some(DocumentationEvidence {
                                                text: text(*s, self.source).into(),
                                                source: self.locator(*s),
                                            });
                                    }
                                }
                            }
                        }
                    }
                    self.walk(
                        body,
                        &nested,
                        body_caller.clone(),
                        Some(index),
                        conditional,
                        depth + 1,
                    )?;
                    // Defaults, decorators and annotations are outside the Python function body.
                    for child in children(node) {
                        if child != body && child != name_node && child != callable {
                            self.walk(
                                child,
                                scope,
                                caller.clone(),
                                parent,
                                conditional,
                                depth + 1,
                            )?;
                        }
                    }
                    if callable != node {
                        for child in children(callable) {
                            if child != body {
                                self.walk(
                                    child,
                                    if self.file.language == "python" {
                                        scope
                                    } else {
                                        &nested
                                    },
                                    if self.file.language == "python" {
                                        caller.clone()
                                    } else {
                                        None
                                    },
                                    parent,
                                    conditional,
                                    depth + 1,
                                )?;
                            }
                        }
                    }
                } else {
                    for child in children(node) {
                        if child != name_node {
                            self.walk(
                                child,
                                &nested,
                                body_caller.clone(),
                                Some(index),
                                conditional,
                                depth + 1,
                            )?;
                        }
                    }
                }
                return Ok(());
            }
        }
        if matches!(
            node.kind(),
            "lambda"
                | "closure_expression"
                | "arrow_function"
                | "function_expression"
                | "generator_function"
        ) && !node
            .parent()
            .is_some_and(|p| self.declaration_nodes.contains_key(&p.id()))
        {
            let nested = digest(&[
                "anonymous_scope",
                &self.file.file_id,
                &self.file.source_hash,
                &node.start_byte().to_string(),
            ]);
            self.scope(LexicalScope {
                id: nested.clone(),
                kind: "anonymous_callable".into(),
                parent_id: Some(scope.into()),
                declaration_id: None,
                source: self.locator(node),
            })?;
            if matches!(node.kind(), "function_expression" | "generator_function") {
                if let Some(name) = node.child_by_field_name("name") {
                    self.binding(
                        name,
                        text(name, self.source).into(),
                        &nested,
                        BindingKind::Local,
                        None,
                        None,
                    )?;
                }
            }
            if let Some(parameter) = node.child_by_field_name("parameter") {
                self.pattern(parameter, &nested, BindingKind::Parameter, depth + 1)?;
            }
            if let Some(params) = node.child_by_field_name("parameters") {
                for p in children(params) {
                    self.pattern(p, &nested, BindingKind::Parameter, depth + 1)?;
                }
            }
            for child in children(node) {
                self.walk(child, &nested, None, parent, conditional, depth + 1)?;
            }
            return Ok(());
        }
        if matches!(self.file.language.as_str(), "typescript" | "javascript") && node.kind() == "class" {
            let nested = digest(&[
                "class_expression_scope",
                &self.file.file_id,
                &self.file.source_hash,
                &node.start_byte().to_string(),
            ]);
            self.scope(LexicalScope {
                id: nested.clone(),
                kind: "class_expression".into(),
                parent_id: Some(scope.into()),
                declaration_id: None,
                source: self.locator(node),
            })?;
            if let Some(name) = node.child_by_field_name("name") {
                self.binding(
                    name,
                    text(name, self.source).into(),
                    &nested,
                    BindingKind::Local,
                    None,
                    None,
                )?;
            }
            for child in children(node) {
                self.walk(
                    child,
                    &nested,
                    caller.clone(),
                    parent,
                    conditional,
                    depth + 1,
                )?;
            }
            return Ok(());
        }
        if matches!(self.file.language.as_str(), "typescript" | "javascript") && node.kind() == "object" {
            let nested = digest(&[
                "object_scope",
                &self.file.file_id,
                &self.file.source_hash,
                &node.start_byte().to_string(),
            ]);
            self.scope(LexicalScope {
                id: nested.clone(),
                kind: "object_literal".into(),
                parent_id: Some(scope.into()),
                declaration_id: None,
                source: self.locator(node),
            })?;
            for child in children(node) {
                self.walk(
                    child,
                    &nested,
                    caller.clone(),
                    parent,
                    conditional,
                    depth + 1,
                )?;
            }
            return Ok(());
        }
        if matches!(
            node.kind(),
            "list_comprehension"
                | "set_comprehension"
                | "dictionary_comprehension"
                | "generator_expression"
        ) {
            let nested = digest(&[
                "comprehension",
                &self.file.file_id,
                &self.file.source_hash,
                &node.start_byte().to_string(),
            ]);
            self.scope(LexicalScope {
                id: nested.clone(),
                kind: "comprehension".into(),
                parent_id: Some(scope.into()),
                declaration_id: None,
                source: self.locator(node),
            })?;
            for child in children(node) {
                self.walk(
                    child,
                    &nested,
                    caller.clone(),
                    parent,
                    conditional,
                    depth + 1,
                )?;
            }
            return Ok(());
        }
        match node.kind() {
            "assignment"
            | "augmented_assignment"
            | "named_expression"
            | "assignment_expression"
            | "augmented_assignment_expression" => {
                if let Some(left) = node
                    .child_by_field_name("left")
                    .or_else(|| node.child_by_field_name("name"))
                {
                    let binding_scope = if matches!(self.file.language.as_str(), "typescript" | "javascript") {
                        self.callable_binding_scope(scope)?
                    } else {
                        scope.into()
                    };
                    self.pattern(left, &binding_scope, BindingKind::Local, depth + 1)?;
                }
            }
            "let_declaration" => {
                if let Some(pattern) = node.child_by_field_name("pattern") {
                    self.pattern(pattern, scope, BindingKind::Local, depth + 1)?;
                }
            }
            "variable_declarator" => {
                if let Some(name) = node.child_by_field_name("name") {
                    let binding_scope = self.variable_binding_scope(node, scope)?;
                    self.pattern(name, &binding_scope, BindingKind::Local, depth + 1)?;
                }
            }
            "for_statement" | "for_in_statement" | "for_expression" | "for_in_clause" => {
                if let Some(left) = node
                    .child_by_field_name("left")
                    .or_else(|| node.child_by_field_name("pattern"))
                {
                    let binding_scope = self.variable_binding_scope(node, scope)?;
                    self.pattern(left, &binding_scope, BindingKind::Local, depth + 1)?;
                }
            }
            "as_pattern" => {
                if let Some(alias) = node.child_by_field_name("alias") {
                    self.pattern(alias, scope, BindingKind::Local, depth + 1)?;
                }
            }
            "catch_clause" => {
                if let Some(parameter) = node.child_by_field_name("parameter") {
                    self.pattern(parameter, scope, BindingKind::Local, depth + 1)?;
                }
            }
            "delete_statement" => {
                for child in children(node) {
                    self.pattern(child, scope, BindingKind::Local, depth + 1)?;
                }
            }
            "global_statement" | "nonlocal_statement" => {
                for n in children(node) {
                    if is_identifier(n) {
                        self.binding(
                            n,
                            text(n, self.source).into(),
                            scope,
                            BindingKind::Local,
                            None,
                            None,
                        )?;
                    }
                }
                self.file.diagnostics.push(EvidenceDiagnostic{code:"binding_directive_unsupported".into(),message:"global/nonlocal directive requires unsupported cross-scope assignment analysis".into(),source:Some(self.locator(node))});
            }
            _ => {}
        }
        let block =
            matches!(node.kind(), "block" | "statement_block") && self.file.language != "python";
        let block_scope = if block {
            let id = digest(&[
                "block",
                &self.file.file_id,
                &node.start_byte().to_string(),
                &self.file.source_hash,
            ]);
            self.scope(LexicalScope {
                id: id.clone(),
                kind: "block".into(),
                parent_id: Some(scope.into()),
                declaration_id: None,
                source: self.locator(node),
            })?;
            id
        } else {
            scope.into()
        };
        for child in children(node) {
            self.walk(
                child,
                &block_scope,
                caller.clone(),
                parent,
                conditional,
                depth + 1,
            )?;
        }
        Ok(())
    }
    fn occurrence(
        &mut self,
        node: Node<'_>,
        kind: OccurrenceKind,
        symbol: String,
        scope: String,
        caller: Option<String>,
    ) -> Result<(), EvidenceError> {
        limit(
            self.file.occurrences.len() < self.budget.max_occurrences,
            "occurrence limit",
        )?;
        let id = digest(&[
            "occurrence",
            &self.file.file_id,
            &self.file.source_hash,
            &format!("{kind:?}"),
            &node.start_byte().to_string(),
            &node.end_byte().to_string(),
            &symbol,
        ]);
        let syntax_kind=if matches!(self.file.language.as_str(),"javascript"|"typescript")&&symbol.chars().next().is_some_and(char::is_uppercase)&&node.parent().is_some_and(|p|matches!(p.kind(),"jsx_opening_element"|"jsx_self_closing_element")&&p.child_by_field_name("name")==Some(node)){"jsx_render"}else{node.kind()};
        let occurrence = OccurrenceEvidence {
            id,
            kind,
            symbol_text: symbol,
            source: self.locator(node),
            context_source: None,
            related_source: None,
            caller_id: caller,
            scope_id: scope,
            syntax_kind: syntax_kind.into(),
        };
        self.account(&occurrence)?;
        self.file.occurrences.push(occurrence);
        Ok(())
    }
    fn references(&mut self, node: Node<'_>, depth: usize) -> Result<(), EvidenceError> {
        self.tick(depth)?;
        let context = self.contexts.get(&node.id()).cloned();
        if let Some((scope, caller)) = context {
            if matches!(self.file.language.as_str(),"javascript"|"typescript") && node.kind()=="call_expression" {
                if let Some(function)=node.child_by_field_name("function") {
                    let named=function.kind()=="member_expression" && function.child_by_field_name("property").is_some_and(|p|text(p,self.source)=="addEventListener");
                    let computed=function.kind()=="subscript_expression" && text(function,self.source).contains("addEventListener");
                    if named || computed {
                        let mut recognized=false;
                        if named {
                            if let Some(args)=node.child_by_field_name("arguments") {
                                let entries:Vec<_>=children(args).into_iter().filter(|v|v.is_named()).collect();
                                let bridge=&self.source[function.end_byte()..args.start_byte()];
                                if entries.len()==2 && !text(function,self.source).contains("?.") && !bridge.contains("?.") {
                                    let event=text(entries[0],self.source).as_bytes();
                                    let literal=entries[0].kind()=="string" && event.len()>=2 && event.len()<=130 && matches!(event[0],b'\''|b'"') && event[event.len()-1]==event[0] && event.is_ascii() && !event.contains(&b'\\') && !event.contains(&b'\n') && !event.contains(&b'\r');
                                    let handler=entries[1];
                                    if literal && handler.kind()=="identifier" {
                                        let occurrence=OccurrenceEvidence{id:digest(&["callback_registration",&self.file.file_id,&handler.start_byte().to_string(),&handler.end_byte().to_string()]),kind:OccurrenceKind::ValueReference,symbol_text:text(handler,self.source).into(),source:self.locator(handler),context_source:Some(self.locator(node)),related_source:None,caller_id:caller.clone(),scope_id:scope.clone(),syntax_kind:"callback_registration".into()};
                                        self.account(&occurrence)?;limit(self.file.occurrences.len()<self.budget.max_occurrences,"callback occurrence limit")?;self.file.occurrences.push(occurrence);self.skip.insert(handler.id());recognized=true;
                                    }
                                }
                            }
                        }
                        if !recognized {let diagnostic=EvidenceDiagnostic{code:"unsupported_callback_registration".into(),message:"event callback syntax outside the supported static profile".into(),source:Some(self.locator(function))};self.account(&diagnostic)?;self.file.diagnostics.push(diagnostic);}
                    }
                }
            }
            if self.file.language == "python" && node.kind() == "call" {
                if let Some(function)=node.child_by_field_name("function") {
                    if function.kind()=="attribute" && function.child_by_field_name("attribute").is_some_and(|a|text(a,self.source)=="execute") {
                        let mut sql_recognized=false;
                        if let Some(arg)=node.child_by_field_name("arguments").and_then(|args|children(args).into_iter().find(|n|n.is_named())) {
                            let raw=text(arg,self.source); let b=raw.as_bytes();
                            let simple=arg.kind()=="string" && b.len()>=2 && matches!(b[0],b'\''|b'"') && b[b.len()-1]==b[0] && b.is_ascii() && !b.contains(&b'\\') && !b.contains(&b'\n') && !b.contains(&b'\r');
                            if simple { if let Some(name)=sql::select(&b[1..b.len()-1],self.budget) {
                                let start=arg.start_byte()+1+name.start; let end=arg.start_byte()+1+name.end;
                                let row=arg.start_position().row as u32+1; let col=arg.start_position().column;
                                let source=SourceLocator{file_id:self.file.file_id.clone(),rel_path:self.file.rel_path.clone(),source_hash:self.file.source_hash.clone(),span:SourceSpan{start_byte:start as u64,end_byte:end as u64,start_line:row,end_line:row,start_column:(col+1+name.start) as u32,end_column:(col+1+name.end) as u32}};
                                let occurrence=OccurrenceEvidence{id:digest(&["sql_table_reference",&self.file.file_id,&start.to_string(),&end.to_string()]),kind:OccurrenceKind::ValueReference,symbol_text:name.name,source,context_source:None,related_source:None,caller_id:caller.clone(),scope_id:scope.clone(),syntax_kind:"sql_table_reference".into()};
                                self.account(&occurrence)?;limit(self.file.occurrences.len()<self.budget.max_occurrences,"SQL occurrence limit")?;self.file.occurrences.push(occurrence);sql_recognized=true;
                            } }
                        }
                        if !sql_recognized {let diagnostic=EvidenceDiagnostic{code:"unsupported_sql_code".into(),message:"execute call is outside the supported static SQL literal profile".into(),source:Some(self.locator(function))};self.account(&diagnostic)?;self.file.diagnostics.push(diagnostic);}
                    }
                }
            }
            if matches!(
                node.kind(),
                "call" | "call_expression" | "macro_invocation" | "new_expression"
            ) {
                if let Some(function) = node
                    .child_by_field_name("function")
                    .or_else(|| node.child_by_field_name("macro"))
                    .or_else(|| node.child_by_field_name("constructor"))
                {
                    self.occurrence(
                        function,
                        OccurrenceKind::Call,
                        text(function, self.source).into(),
                        scope.clone(),
                        caller.clone(),
                    )?;
                    if matches!(node.kind(), "macro_invocation" | "new_expression") {
                        if let Some(last) = self.file.occurrences.last_mut() {
                            last.syntax_kind = node.kind().into();
                        }
                    }
                    self.skip.insert(function.id());
                }
            }
            if is_identifier(node) && !self.skip.contains(&node.id()) {
                let parent = node.parent();
                let member = parent.is_some_and(|p| {
                    matches!(
                        p.kind(),
                        "attribute"
                            | "member_expression"
                            | "field_expression"
                            | "scoped_identifier"
                            | "scoped_type_identifier"
                    ) && p
                        .child_by_field_name("object")
                        .or_else(|| p.child_by_field_name("value"))
                        != Some(node)
                });
                if !member {
                    let kind = if node.kind() == "type_identifier"
                        || parent.is_some_and(|p| {
                            matches!(p.kind(), "type_annotation" | "generic_type" | "type")
                        }) {
                        OccurrenceKind::TypeReference
                    } else {
                        OccurrenceKind::ValueReference
                    };
                    self.occurrence(node, kind, text(node, self.source).into(), scope, caller)?;
                }
            } else if matches!(
                node.kind(),
                "attribute"
                    | "member_expression"
                    | "field_expression"
                    | "scoped_identifier"
                    | "scoped_type_identifier"
            ) && (node.kind() != "attribute" || self.file.language == "python")
                && !self.skip.contains(&node.id())
            {
                let kind = if node.kind() == "scoped_type_identifier" {
                    OccurrenceKind::TypeReference
                } else {
                    OccurrenceKind::ValueReference
                };
                self.occurrence(node, kind, text(node, self.source).into(), scope, caller)?;
            }
        }
        for child in children(node) {
            self.references(child, depth + 1)?;
        }
        Ok(())
    }
    fn finish(mut self) -> Result<FileEvidence, EvidenceError> {
        let mut counts = BTreeMap::new();
        for d in &self.file.declarations {
            *counts.entry(d.identity_key.clone()).or_insert(0usize) += 1;
        }
        let mut remap = HashMap::new();
        let mut ambiguous_ids = HashSet::new();
        for i in 0..self.file.declarations.len() {
            let ambiguous = counts[&self.file.declarations[i].identity_key] > 1
                || self.file.declarations[i]
                    .lexical_parent_id
                    .as_ref()
                    .is_some_and(|p| ambiguous_ids.contains(p));
            let d = &mut self.file.declarations[i];
            let old = d.id.clone();
            d.id = if ambiguous {
                ambiguous_ids.insert(old.clone());
                d.identity_state = IdentityState::AmbiguousSnapshotLocal;
                if d.function_flow.is_some() {
                    d.function_flow = Some(function_flow::FunctionFlow::ambiguous());
                }
                digest(&[
                    "ambiguous",
                    &d.identity_key,
                    &self.file.source_hash,
                    &d.source.span.start_byte.to_string(),
                ])
            } else {
                digest(&["stable", &d.identity_key])
            };
            remap.insert(old, d.id.clone());
        }
        for d in &mut self.file.declarations {
            if let Some(p) = &mut d.lexical_parent_id {
                if let Some(id) = remap.get(p) {
                    *p = id.clone();
                }
            }
        }
        for s in &mut self.file.scopes {
            if let Some(d) = &mut s.declaration_id {
                if let Some(id) = remap.get(d) {
                    *d = id.clone();
                }
            }
        }
        for b in &mut self.file.bindings {
            if let Some(d) = &mut b.declaration_id {
                if let Some(id) = remap.get(d) {
                    *d = id.clone();
                }
            }
        }
        if let Some(profiles)=&mut self.file.error_boundaries {
            for profile in profiles {
                if let Some(id)=remap.get(&profile.function_id) {profile.function_id=id.clone();}
                if self.file.declarations.iter().any(|d|d.id==profile.function_id && d.identity_state!=IdentityState::Stable) {profile.status="unsupported".into();profile.reason=Some("ambiguous_binding".into());profile.regions.clear();}
            }
        }
        if let Some(profiles)=&mut self.file.local_value_slices {
            for profile in profiles {
                if let Some(id)=remap.get(&profile.function_id) {profile.function_id=id.clone();}
                if self.file.declarations.iter().any(|d|d.id==profile.function_id && d.identity_state!=IdentityState::Stable) {
                    profile.status="unsupported".into();profile.reason=Some("ambiguous_binding".into());profile.return_occurrence_id=None;profile.root_definition_id=None;profile.definitions.clear();
                }
            }
        }
        for o in &mut self.file.occurrences {
            if let Some(d) = &mut o.caller_id {
                if let Some(id) = remap.get(d) {
                    *d = id.clone();
                }
            }
        }
        for import in self.file.imports.clone() {
            self.occurrence_from_import(import)?;
        }
        self.file.occurrences.sort_by(|a, b| a.id.cmp(&b.id));
        self.file.public_surface_fingerprint = digest(&[
            &serde_json::to_string(
                &self
                    .file
                    .declarations
                    .iter()
                    .map(|d| {
                        (
                            &d.identity_key,
                            &d.identity_state,
                            d.explicit_export,
                            d.conditional,
                        )
                    })
                    .collect::<Vec<_>>(),
            )
            .unwrap(),
            &serde_json::to_string(&self.file.imports).unwrap(),
        ]);
        if self.file.diagnostics.iter().any(|d| d.code == "typescript_partial_syntax") {
            self.file.public_surface_fingerprint = digest(&["typescript_partial_syntax", &self.file.public_surface_fingerprint]);
        }
        if self.file.language == "package_manifest" {
            let aliases: Vec<_> = self.file.declarations.iter()
                .filter(|decl| decl.kind == "package_import_alias")
                .map(|decl| (&decl.identity_key, &decl.signature_hash))
                .collect();
            if !aliases.is_empty() {
                self.file.public_surface_fingerprint = digest(&[
                    &self.file.public_surface_fingerprint, &serde_json::to_string(&aliases).unwrap(),
                ]);
            }
        }
        validate_file_evidence(&self.file, self.budget)?;
        Ok(self.file)
    }
    fn occurrence_from_import(&mut self, import: ImportEvidence) -> Result<(), EvidenceError> {
        limit(
            self.file.occurrences.len() < self.budget.max_occurrences,
            "import occurrence limit",
        )?;
        let occurrence = OccurrenceEvidence {
            id: digest(&["import_occurrence", &import.id]),
            kind: OccurrenceKind::ImportReference,
            symbol_text: import.local_name,
            source: import.module_source.clone().unwrap_or(import.source),
            context_source: None,
            related_source: None,
            caller_id: None,
            scope_id: import.scope_id,
            syntax_kind: if import.module_source.is_some() { "manifest_dependency".into() } else { import.syntax_kind },
        };
        self.account(&occurrence)?;
        self.file.occurrences.push(occurrence);
        Ok(())
    }
}

fn profile_declaration(
    builder: &mut Builder<'_>,
    node: Node<'_>,
    name_node: Node<'_>,
    kind: &str,
    owner: Option<usize>,
    qualifier: &str,
    explicit_export: bool,
    language: &str,
) -> Result<usize, EvidenceError> {
    limit(
        builder.file.declarations.len() < builder.budget.max_declarations,
        "profile declaration limit",
    )?;
    let name = text(name_node, builder.source);
    let signature = builder.signature(node)?;
    let qualified_name = format!("{qualifier}::{name}");
    let identity_tag = match language {
        "go" => "go_declaration_v1",
        "javascript" => "javascript_bundle_declaration_v1",
        _ => "java_declaration_v1",
    };
    let identity_key = digest(&[
        identity_tag,
        &builder.file.checkout_id,
        &builder.file.rel_path,
        &qualified_name,
        kind,
    ]);
    let declaration = DeclarationEvidence {
        id: digest(&[&identity_key, &node.start_byte().to_string()]),
        identity_key,
        identity_state: IdentityState::Stable,
        kind: kind.into(),
        name: name.into(),
        qualified_name,
        lexical_parent_id: owner.map(|index| builder.file.declarations[index].id.clone()),
        scope_id: builder.file.root_scope_id.clone(),
        source: builder.locator(node),
        name_span: span(name_node),
        signature_hash: digest(&[&signature]),
        signature,
        content_hash: blake3::hash(text(node, builder.source).as_bytes())
            .to_hex()
            .to_string(),
        structural_shape: None,
        function_flow: None,
        explicit_export,
        conditional: false,
        documentation: None,
    };
    builder.account(&declaration)?;
    let index = builder.file.declarations.len();
    builder.file.declarations.push(declaration);
    Ok(index)
}

/// Keep syntactic wrapper declarations without entering a generated bundle's
/// initializer. A literal globalThis property is a write, not an ESM export.
fn generated_wrapper(builder: &mut Builder<'_>, root: Node<'_>) -> Result<(), EvidenceError> {
    let count = root.named_child_count();
    let mut cursor = root.walk();
    let last = (count > 256).then(|| root.named_child((count - 1) as u32)).flatten();
    for node in root.named_children(&mut cursor).take(256).chain(last) {
        builder.tick(1)?;
        if node.end_byte() - node.start_byte() > 4096 { continue; }
        if matches!(node.kind(), "variable_declaration" | "lexical_declaration") {
            for item in children(node).into_iter().filter(|n| n.kind() == "variable_declarator") {
                if let Some(name) = item.child_by_field_name("name").filter(|n| n.kind() == "identifier") {
                    profile_declaration(builder, item, name, "module_variable", None, "module", false, "javascript")?;
                }
            }
        } else if node.kind() == "import_statement" {
            let scope = builder.file.root_scope_id.clone();
            builder.imports(node, &scope)?;
        } else if node.kind() == "expression_statement" {
            let Some(assignment) = node.named_child(0).filter(|n| n.kind() == "assignment_expression") else {continue};
            let Some(left) = assignment.child_by_field_name("left").filter(|n| n.kind() == "subscript_expression") else {continue};
            if !left.child_by_field_name("object").is_some_and(|v| v.kind()=="identifier" && text(v,builder.source)=="globalThis") {continue;}
            let Some(key) = left.child_by_field_name("index").filter(|v| v.kind()=="string" && v.named_child_count()==1)
                .and_then(|v| v.named_child(0)).filter(|v| v.kind()=="string_fragment") else {continue};
            profile_declaration(builder, assignment, key, "global_property_assignment", None, "globalThis", false, "javascript")?;
        }
    }
    Ok(())
}

fn has_owned_jsx(node:Node<'_>)->bool {
    let owner=if node.kind()=="variable_declarator"{node.child_by_field_name("value").unwrap_or(node)}else{node};
    let mut stack=children(owner);
    while let Some(n)=stack.pop(){
        if matches!(n.kind(),"jsx_element"|"jsx_self_closing_element"){return true;}
        if matches!(n.kind(),"function_declaration"|"function_expression"|"arrow_function"|"class_declaration"|"method_definition"){continue;}
        stack.extend(children(n));
    }false
}
fn module_writes<'a>(root:Node<'a>,name:&str,source:&str)->Vec<Node<'a>> {
    let mut stack=children(root);let mut writes=Vec::new();
    while let Some(node)=stack.pop(){
        if matches!(node.kind(),"function_definition"|"class_definition") {
            if node.child_by_field_name("name").is_some_and(|n|text(n,source)==name){writes.push(node);}continue;
        }
        if matches!(node.kind(),"assignment"|"augmented_assignment"|"named_expression") {
            let mut targets=node.child_by_field_name("left").or_else(||node.child_by_field_name("name")).into_iter().collect::<Vec<_>>();let mut matches=false;
            while let Some(n)=targets.pop(){if n.kind()=="identifier"&&text(n,source)==name{matches=true;}targets.extend(children(n));}
            if matches{writes.push(node);}
        }
        stack.extend(children(node));
    }writes
}
fn fastapi_route<'a>(node:Node<'a>,source:&str,imports:&[ImportEvidence])->Option<Node<'a>> {
    let decorated=node.parent().filter(|p|p.kind()=="decorated_definition")?;
    let root=decorated.parent().filter(|p|p.kind()=="module")?;
    for decorator in children(decorated).into_iter().filter(|n|n.kind()=="decorator") {
        let Some(call)=children(decorator).into_iter().find(|n|n.kind()=="call") else{continue;};
        let Some(function)=call.child_by_field_name("function").filter(|n|n.kind()=="attribute") else{continue;};
        let Some(receiver)=function.child_by_field_name("object").filter(|n|n.kind()=="identifier") else{continue;};
        let Some(method)=function.child_by_field_name("attribute") else{continue;};
        if !matches!(text(method,source),"get"|"post"|"put"|"patch"|"delete"|"options"|"head"|"websocket"|"api_route"){continue;}
        let Some(args)=call.child_by_field_name("arguments") else{continue;};
        let Some(path)=children(args).into_iter().next() else{continue;};
        if path.kind()!="string"||children(path).iter().any(|n|n.kind()=="interpolation"){continue;}
        let assignments=module_writes(root,text(receiver,source),source);
        if assignments.iter().any(|n|n.kind()!="assignment"||n.parent().and_then(|p|p.parent()).is_none_or(|p|p!=root)){continue;}
        if assignments.len()!=1||assignments[0].start_byte()>=decorated.start_byte(){continue;}
        let Some(init)=assignments[0].child_by_field_name("right").filter(|n|n.kind()=="call").and_then(|n|n.child_by_field_name("function")) else{continue;};
        let constructor=text(init,source);
        for import in imports.iter().filter(|i|i.module_path=="fastapi") {
            let supported=import.imported_name.as_deref().is_some_and(|name|matches!(name,"FastAPI"|"APIRouter")&&import.local_name==constructor)||import.imported_name.is_none()&&matches!(constructor.strip_prefix(&format!("{}.",import.local_name)),Some("FastAPI"|"APIRouter"));
            if !supported||imports.iter().filter(|i|i.local_name==import.local_name).count()!=1{continue;}
            // No same-module rebinding of the imported constructor/module name.
            let rebound=!module_writes(root,&import.local_name,source).is_empty();
            if !rebound{return Some(decorator);}
        }
    }None
}
pub fn extract_evidence(
    checkout: &str,
    rel_path: &str,
    bytes: &[u8],
    budget: &ExtractionBudget,
) -> Result<FileEvidence, EvidenceError> {
    limit(bytes.len() <= budget.max_source_bytes, "source byte limit")?;
    if checkout.is_empty() || !valid_path(rel_path) {
        return Err(error(
            EvidenceErrorCode::InvalidSource,
            "invalid checkout/relative path",
        ));
    }
    let capability = language_capability(rel_path);
    if capability.language == "package_manifest" { return extract_package_manifest(checkout, rel_path, bytes, budget, capability); }
    if capability.language == "sql_static" { return extract_sql_static(checkout, rel_path, bytes, budget, capability); }
    if !capability.available {
        return Err(error(
            EvidenceErrorCode::UnsupportedLanguage,
            "no admitted grammar for this file",
        ));
    }
    let source = std::str::from_utf8(bytes)
        .map_err(|_| error(EvidenceErrorCode::InvalidSource, "source is not UTF-8"))?;
    if source.contains('\0') {
        return Err(error(
            EvidenceErrorCode::InvalidSource,
            "source contains NUL",
        ));
    }
    let grammar = match capability.dialect.as_str() {
        "rust" => tree_sitter_rust::LANGUAGE.into(),
        "go" => tree_sitter_go::LANGUAGE.into(),
        "java" => tree_sitter_java::LANGUAGE.into(),
        "python" => tree_sitter_python::LANGUAGE.into(),
        "tsx" => tree_sitter_typescript::LANGUAGE_TSX.into(),
        "javascript" => tree_sitter_javascript::LANGUAGE.into(),
        _ => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
    };
    // One bounded parser per grammar on each existing scheduler worker. No
    // retained trees/source buffers and no additional executor or CPU lease.
    thread_local! { static PARSERS: std::cell::RefCell<HashMap<String, tree_sitter::Parser>> = std::cell::RefCell::new(HashMap::new()); }
    let tree = PARSERS.with(|pool| {
        let mut pool = pool.borrow_mut();
        let parser = pool.entry(capability.dialect.clone()).or_insert_with(tree_sitter::Parser::new);
        parser.set_language(&grammar).map_err(|_| error(EvidenceErrorCode::UnsupportedLanguage, "grammar ABI unavailable"))?;
        parser.reset();
        parser.parse(source, None).ok_or_else(|| error(EvidenceErrorCode::SyntaxError, "parser did not return a tree"))
    })?;
    let recovery = if tree.root_node().has_error() {
        let mut node = tree.root_node();
        let mut depth = 0;
        let mut visited = 0;
        while !node.is_error() && !node.is_missing() && depth < budget.max_depth {
            let mut cursor = node.walk();
            let child = node.children(&mut cursor).take(budget.max_ast_nodes.saturating_sub(visited))
                .inspect(|_| visited += 1).find(|child| child.has_error());
            let Some(child) = child else { break; };
            node = child;
            depth += 1;
        }
        let message = format!("{} grammar recovery at {}:{} (bytes {}..{}, {})",
            capability.dialect, node.start_position().row + 1, node.start_position().column,
            node.start_byte(), node.end_byte(), node.kind());
        if capability.language != "typescript" {
            return Err(error(EvidenceErrorCode::SyntaxError, message));
        }
        Some((node, message))
    } else { None };
    let file_id = digest(&["file", checkout, rel_path]);
    let root_scope_id = digest(&["module_scope", &file_id]);
    let hash = blake3::hash(bytes).to_hex().to_string();
    let root_locator = SourceLocator {
        file_id: file_id.clone(),
        rel_path: rel_path.into(),
        source_hash: hash.clone(),
        span: span(tree.root_node()),
    };
    let file = FileEvidence {
        format_version: EVIDENCE_FORMAT_VERSION,
        checkout_id: checkout.into(),
        file_id,
        rel_path: rel_path.into(),
        source_hash: hash,
        source_len: bytes.len() as u64,
        language: capability.language.clone(),
        capability,
        producer_fingerprint: crate::code::producer_fingerprint(),
        root_scope_id: root_scope_id.clone(),
        scopes: vec![LexicalScope {
            id: root_scope_id.clone(),
            kind: "module".into(),
            parent_id: None,
            declaration_id: None,
            source: root_locator,
        }],
        bindings: vec![],
        declarations: vec![],
        occurrences: vec![],
        imports: vec![],
        local_value_slices: None,
        error_boundaries: None,
        public_surface_fingerprint: String::new(),
        diagnostics: vec![],
    };
    let mut builder = Builder {
        source,
        file,
        budget,
        contexts: HashMap::new(),
        skip: HashSet::new(),
        declaration_nodes: HashMap::new(),
        scope_indexes: HashMap::from([(root_scope_id.clone(), 0)]),
        work: 0,
        output_bytes: 0,
    };
    // A generated single-line bundle can exhaust the work cap before the
    // extractor reaches its first complete semantic unit. Keep the verified
    // file span and hash, and make the missing semantic detail explicit.
    if builder.file.language == "javascript" && rel_path.ends_with(".min.js")
        && bytes.len() >= 1024 * 1024
        && bytes.split(|&b| b == b'\n').any(|line| line.len() >= 65536) {
        generated_wrapper(&mut builder, tree.root_node())?;
        let diagnostic = EvidenceDiagnostic { code: "generated_bundle_wrapper_partial".into(),
            message: "Large minified JavaScript bundle; bounded top-level wrapper only, implementation and call references omitted".into(),
            source: Some(builder.locator(tree.root_node())) };
        builder.account(&diagnostic)?;
        builder.file.diagnostics.push(diagnostic);
        return builder.finish();
    }
    if let Some((recovered, message)) = recovery {
        // Each clean direct child has a real module-level boundary. Never walk
        // a repaired subtree or resolve calls through potentially missing
        // bindings. Large units remain outside this partial extraction profile.
        let root = tree.root_node();
        let mut cursor = root.walk();
        let mut skipped = 0usize;
        for child in root.named_children(&mut cursor) {
            builder.tick(0)?;
            if child.has_error() || child.end_byte() - child.start_byte() > 65536 {
                skipped += 1;
                continue;
            }
            builder.walk(child, &root_scope_id, None, None, false, 1)?;
        }
        if builder.file.declarations.is_empty() && builder.file.imports.is_empty() {
            return Err(error(EvidenceErrorCode::SyntaxError, message));
        }
        // finish() emits only syntax-backed import occurrences after this.
        builder.file.occurrences.clear();
        let diagnostic = EvidenceDiagnostic { code: "typescript_partial_syntax".into(),
            message: format!("{message}; {skipped} module units omitted; references limited to imports"),
            source: Some(builder.locator(recovered)) };
        builder.account(&diagnostic)?;
        builder.file.diagnostics.push(diagnostic);
        return builder.finish();
    }
    if builder.file.language == "go" {
        go::extract(&mut builder, tree.root_node())?;
        return builder.finish();
    }
    if builder.file.language == "java" {
        java::extract(&mut builder, tree.root_node())?;
        return builder.finish();
    }
    builder.walk(tree.root_node(), &root_scope_id, None, None, false, 0)?;
    builder.references(tree.root_node(), 0)?;
    if matches!(builder.file.language.as_str(), "javascript" | "typescript") {
        match js_events::extract(&mut builder, tree.root_node()) {
            Err(error) if error.code != EvidenceErrorCode::BudgetExceeded => return Err(error),
            _ => {}
        }
    }
    if builder.file.language=="python" {builder.file.local_value_slices=Some(local_slice::extract(&mut builder,tree.root_node())?);}
    if builder.file.language=="python" {
        builder.file.error_boundaries=Some(error_boundaries::extract(&mut builder,tree.root_node())?);
        function_flow::extract(&mut builder, tree.root_node());
    }
    builder.finish()
}

fn extract_package_manifest(checkout:&str, rel_path:&str, bytes:&[u8], budget:&ExtractionBudget, capability:LanguageCapability)->Result<FileEvidence,EvidenceError> {
    let source=std::str::from_utf8(bytes).map_err(|_|error(EvidenceErrorCode::InvalidSource,"manifest is not UTF-8"))?;
    let (keys,aliases,unsupported)=manifest::extract(bytes,budget)?;
    let file_id=digest(&["file",checkout,rel_path]);let root_scope_id=digest(&["module_scope",&file_id]);let hash=blake3::hash(bytes).to_hex().to_string();
    let mut lines=vec![0usize];for (i,b) in bytes.iter().enumerate(){if *b==b'\n'{lines.push(i+1);}}
    let locate=|start:usize,end:usize| {
        let a=lines.partition_point(|v|*v<=start).saturating_sub(1);let b=lines.partition_point(|v|*v<=end).saturating_sub(1);
        SourceSpan{start_byte:start as u64,end_byte:end as u64,start_line:a as u32+1,start_column:(start-lines[a]) as u32,end_line:b as u32+1,end_column:(end-lines[b]) as u32}
    };
    let source_locator=|span:SourceSpan|SourceLocator{file_id:file_id.clone(),rel_path:rel_path.into(),source_hash:hash.clone(),span};
    let root=source_locator(locate(0,bytes.len()));
    let file=FileEvidence{format_version:EVIDENCE_FORMAT_VERSION,checkout_id:checkout.into(),file_id:file_id.clone(),rel_path:rel_path.into(),source_hash:hash.clone(),source_len:bytes.len() as u64,language:capability.language.clone(),capability,producer_fingerprint:crate::code::producer_fingerprint(),root_scope_id:root_scope_id.clone(),scopes:vec![LexicalScope{id:root_scope_id.clone(),kind:"module".into(),parent_id:None,declaration_id:None,source:root}],bindings:vec![],declarations:vec![],occurrences:vec![],imports:vec![],local_value_slices:None,error_boundaries:None,public_surface_fingerprint:String::new(),diagnostics:vec![]};
    let mut builder=Builder{source,file,budget,contexts:HashMap::new(),skip:HashSet::new(),declaration_nodes:HashMap::new(),scope_indexes:HashMap::from([(root_scope_id.clone(),0)]),work:0,output_bytes:0};
    for key in keys {
        let loc=source_locator(locate(key.start,key.end));let identity_key=digest(&["manifest_dependency",checkout,rel_path,&key.section,&key.name]);
        let declaration=DeclarationEvidence{id:digest(&[&identity_key,&key.start.to_string()]),identity_key,identity_state:IdentityState::Stable,kind:"package_dependency".into(),name:key.name.clone(),qualified_name:format!("{}::{}",key.section,key.name),lexical_parent_id:None,scope_id:root_scope_id.clone(),source:loc.clone(),name_span:loc.span.clone(),signature:key.section.clone(),signature_hash:digest(&[&key.section]),content_hash:blake3::hash(&bytes[key.start..key.end]).to_hex().to_string(),structural_shape:None,function_flow:None,explicit_export:true,conditional:false,documentation:None};
        builder.account(&declaration)?;builder.file.declarations.push(declaration);
    }
    for alias in aliases {
        let source = source_locator(locate(alias.key_start, alias.value_end));
        let name_span = locate(alias.key_start, alias.key_end);
        let identity_key = digest(&["package_import_alias", checkout, rel_path, &alias.name]);
        let declaration = DeclarationEvidence {
            id: digest(&[&identity_key, &alias.key_start.to_string()]), identity_key,
            identity_state: IdentityState::Stable, kind: "package_import_alias".into(),
            qualified_name: format!("imports::{}", alias.name), name: alias.name,
            lexical_parent_id: None, scope_id: root_scope_id.clone(), source, name_span,
            signature_hash: digest(&[&alias.path]), signature: alias.path,
            content_hash: blake3::hash(&bytes[alias.key_start..alias.value_end]).to_hex().to_string(),
            structural_shape: None, function_flow: None, explicit_export: true,
            conditional: true, documentation: None,
        };
        builder.account(&declaration)?;
        builder.file.declarations.push(declaration);
    }
    if unsupported>0 {let diagnostic=EvidenceDiagnostic{code:"unsupported_manifest_dependency".into(),message:format!("{unsupported} dependency names are outside the supported literal package profile"),source:None};builder.account(&diagnostic)?;builder.file.diagnostics.push(diagnostic);}
    builder.finish()
}

fn extract_sql_static(checkout:&str, rel_path:&str, bytes:&[u8], budget:&ExtractionBudget, capability:LanguageCapability)->Result<FileEvidence,EvidenceError> {
    let source=std::str::from_utf8(bytes).map_err(|_|error(EvidenceErrorCode::InvalidSource,"SQL is not UTF-8"))?;
    let (keys,views,unsupported)=sql::declarations(bytes,budget)?;
    let file_id=digest(&["file",checkout,rel_path]);let root_scope_id=digest(&["module_scope",&file_id]);let hash=blake3::hash(bytes).to_hex().to_string();
    let mut lines=vec![0usize];for (i,b) in bytes.iter().enumerate(){if *b==b'\n'{lines.push(i+1);}}
    let locate=|start:usize,end:usize| {
        let a=lines.partition_point(|v|*v<=start).saturating_sub(1);let b=lines.partition_point(|v|*v<=end).saturating_sub(1);
        SourceSpan{start_byte:start as u64,end_byte:end as u64,start_line:a as u32+1,start_column:(start-lines[a]) as u32,end_line:b as u32+1,end_column:(end-lines[b]) as u32}
    };
    let source_locator=|span:SourceSpan|SourceLocator{file_id:file_id.clone(),rel_path:rel_path.into(),source_hash:hash.clone(),span};
    let root=source_locator(locate(0,bytes.len()));
    let file=FileEvidence{format_version:EVIDENCE_FORMAT_VERSION,checkout_id:checkout.into(),file_id:file_id.clone(),rel_path:rel_path.into(),source_hash:hash.clone(),source_len:bytes.len() as u64,language:capability.language.clone(),capability,producer_fingerprint:crate::code::producer_fingerprint(),root_scope_id:root_scope_id.clone(),scopes:vec![LexicalScope{id:root_scope_id.clone(),kind:"module".into(),parent_id:None,declaration_id:None,source:root}],bindings:vec![],declarations:vec![],occurrences:vec![],imports:vec![],local_value_slices:None,error_boundaries:None,public_surface_fingerprint:String::new(),diagnostics:vec![]};
    let mut builder=Builder{source,file,budget,contexts:HashMap::new(),skip:HashSet::new(),declaration_nodes:HashMap::new(),scope_indexes:HashMap::from([(root_scope_id.clone(),0)]),work:0,output_bytes:0};
    for key in keys {
        let loc=source_locator(locate(key.start,key.end));let identity_key=digest(&["sql_table",checkout,rel_path,"table",&key.name]);
        let declaration=DeclarationEvidence{id:digest(&[&identity_key,&key.start.to_string()]),identity_key,identity_state:IdentityState::Stable,kind:"sql_table".into(),name:key.name.clone(),qualified_name:key.name.clone(),lexical_parent_id:None,scope_id:root_scope_id.clone(),source:loc.clone(),name_span:loc.span.clone(),signature:"create_table".into(),signature_hash:digest(&["create_table"]),content_hash:blake3::hash(&bytes[key.start..key.end]).to_hex().to_string(),structural_shape:None,function_flow:None,explicit_export:true,conditional:false,documentation:None};
        builder.account(&declaration)?;builder.file.declarations.push(declaration);
    }
    for view in views {
        let identity_key = digest(&["sql_view", checkout, rel_path, &view.declaration.name]);
        let id = digest(&[&identity_key, &view.declaration.start.to_string()]);
        let source = source_locator(locate(view.start, view.end));
        let declaration = DeclarationEvidence {
            id: id.clone(), identity_key, identity_state: IdentityState::Stable,
            kind: "sql_view".into(), name: view.declaration.name.clone(),
            qualified_name: view.declaration.name, lexical_parent_id: None,
            scope_id: root_scope_id.clone(), source: source.clone(),
            name_span: locate(view.declaration.start, view.declaration.end),
            signature: "create_view".into(), signature_hash: digest(&["create_view"]),
            content_hash: blake3::hash(&bytes[view.start..view.end]).to_hex().to_string(),
            structural_shape: None, function_flow: None, explicit_export: true,
            conditional: false, documentation: None,
        };
        builder.account(&declaration)?;
        builder.file.declarations.push(declaration);
        limit(builder.file.occurrences.len() < budget.max_occurrences, "SQL view reference limit")?;
        let occurrence = OccurrenceEvidence {
            id: digest(&["sql_view_table_reference", &file_id, &hash, &view.table.start.to_string()]),
            kind: OccurrenceKind::TypeReference, symbol_text: view.table.name,
            source: source_locator(locate(view.table.start, view.table.end)),
            context_source: Some(source), related_source: None, caller_id: Some(id),
            scope_id: root_scope_id.clone(), syntax_kind: "sql_table_reference".into(),
        };
        builder.account(&occurrence)?;
        builder.file.occurrences.push(occurrence);
    }
    if unsupported>0 {let diagnostic=EvidenceDiagnostic{code:"unsupported_sql_statement".into(),message:format!("{unsupported} SQL statements are outside the supported static profile"),source:None};builder.account(&diagnostic)?;builder.file.diagnostics.push(diagnostic);}
    builder.finish()
}

fn locator_valid(file: &FileEvidence, locator: &SourceLocator) -> bool {
    locator.file_id == file.file_id
        && locator.rel_path == file.rel_path
        && locator.source_hash == file.source_hash
        && locator.span.start_byte <= locator.span.end_byte
        && locator.span.end_byte <= file.source_len
        && locator.span.start_line > 0
        && locator.span.end_line >= locator.span.start_line
}
pub fn validate_file_evidence(
    file: &FileEvidence,
    budget: &ExtractionBudget,
) -> Result<(), EvidenceError> {
    limit(
        usize::try_from(file.source_len)
            .ok()
            .is_some_and(|n| n <= budget.max_source_bytes),
        "file source size limit",
    )?;
    limit(
        file.declarations.len() <= budget.max_declarations
            && file.bindings.len() <= budget.max_bindings
            && file.imports.len() <= budget.max_imports
            && file.occurrences.len() <= budget.max_occurrences
            && file.scopes.len() <= budget.max_ast_nodes,
        "file record limit",
    )?;
    bounded_json(file, budget.max_serialized_bytes)?;
    let bad = |section: &str| {
        error(
            EvidenceErrorCode::InvalidEvidence,
            &format!("malformed evidence {section} identity, scope or provenance"),
        )
    };
    if file.format_version != EVIDENCE_FORMAT_VERSION
        || !valid_path(&file.rel_path)
        || file.checkout_id.is_empty()
        || file.file_id != digest(&["file", &file.checkout_id, &file.rel_path])
        || file.producer_fingerprint != crate::code::producer_fingerprint()
        || file.source_hash.len() != 64
        || !file.source_hash.bytes().all(|b| b.is_ascii_hexdigit())
        || file.capability != language_capability(&file.rel_path)
        || file.language != file.capability.language
        || !file.capability.available
    {
        return Err(bad("file"));
    }
    let scopes: HashSet<_> = file.scopes.iter().map(|s| s.id.as_str()).collect();
    let declarations: HashSet<_> = file.declarations.iter().map(|d| d.id.as_str()).collect();
    let imports: HashSet<_> = file.imports.iter().map(|i| i.id.as_str()).collect();
    if scopes.len() != file.scopes.len()
        || declarations.len() != file.declarations.len()
        || imports.len() != file.imports.len()
        || !scopes.contains(file.root_scope_id.as_str())
    {
        return Err(bad("duplicate/root"));
    }
    let scope_map: HashMap<_, _> = file.scopes.iter().map(|s| (&s.id, s)).collect();
    for s in &file.scopes {
        if !locator_valid(file, &s.source)
            || s.parent_id
                .as_ref()
                .is_some_and(|p| !scopes.contains(p.as_str()))
            || s.declaration_id
                .as_ref()
                .is_some_and(|d| !declarations.contains(d.as_str()))
        {
            return Err(bad("scope"));
        }
        let mut current = s.parent_id.as_ref();
        let mut visited = HashSet::new();
        while let Some(p) = current {
            if !visited.insert(p) || visited.len() > budget.max_depth {
                return Err(bad("scope cycle"));
            }
            current = scope_map.get(p).and_then(|s| s.parent_id.as_ref());
        }
    }
    for d in &file.declarations {
        if d.function_flow.as_ref().is_some_and(|profile| {
            file.language != "python" || !function_flow::is_valid(d, profile)
        }) {
            return Err(bad("function flow"));
        }
        if d.structural_shape.as_ref().is_some_and(|shape| {
            use sha2::Digest;
            let valid_body = shape.body.as_ref().is_some_and(|body| body.start_byte >= d.source.span.start_byte && body.end_byte <= d.source.span.end_byte && body.start_byte <= body.end_byte);
            let valid_nodes = shape.nodes.len() <= 4096 && shape.nodes.iter().all(|node| node.len() <= 256) && shape.nodes.iter().map(String::len).sum::<usize>() <= 65536;
            let hash = serde_json::to_vec(&shape.nodes).ok().map(|data| format!("{:x}", sha2::Sha256::digest(data)));
            shape.version != 1 || !valid_body || !valid_nodes || match shape.status.as_str() {
                "complete" => shape.nodes.len() < 12 || shape.sha256 != hash,
                "skipped_budget" | "skipped_trivial" => !shape.nodes.is_empty() || shape.sha256.is_some(),
                _ => true,
            }
        }) || !locator_valid(file, &d.source)
            || !scopes.contains(d.scope_id.as_str())
            || d.id.contains("::")
            || d.name_span.start_byte < d.source.span.start_byte
            || d.name_span.end_byte > d.source.span.end_byte
            || d.name_span.start_byte > d.name_span.end_byte
            || d.signature_hash != digest(&[&d.signature])
            || d.lexical_parent_id
                .as_ref()
                .is_some_and(|p| !declarations.contains(p.as_str()))
        {
            return Err(bad("declaration"));
        }
    }
    let mut ids = HashSet::new();
    for o in &file.occurrences {
        if !ids.insert(&o.id) {
            return Err(error(
                EvidenceErrorCode::InvalidEvidence,
                &format!(
                    "duplicate occurrence {:?} {} at {}..{}",
                    o.kind, o.syntax_kind, o.source.span.start_byte, o.source.span.end_byte
                ),
            ));
        }
        if o.context_source.as_ref().is_some_and(|context| !locator_valid(file,context) || context.span.start_byte>o.source.span.start_byte || context.span.end_byte<o.source.span.end_byte) || (matches!(o.syntax_kind.as_str(), "callback_registration" | "go_interface_assertion" | "java_event_publish" | "java_event_consume") && o.context_source.is_none()) || !locator_valid(file, &o.source) {
            return Err(bad("occurrence locator"));
        }
        if o.syntax_kind == "js_event_dispatch" {
            if !matches!(file.language.as_str(), "javascript" | "typescript")
                || o.context_source.is_none()
                || o.related_source.as_ref().is_none_or(|source| !locator_valid(file, source))
            {
                return Err(bad("event dispatch witness"));
            }
        } else if o.related_source.is_some() {
            return Err(bad("unexpected related occurrence locator"));
        }
        if !scopes.contains(o.scope_id.as_str()) {
            return Err(bad("occurrence scope"));
        }
        if o.caller_id
            .as_ref()
            .is_some_and(|c| !declarations.contains(c.as_str()))
        {
            return Err(bad("occurrence"));
        }
    }
    let mut ids = HashSet::new();
    for b in &file.bindings {
        if !ids.insert(&b.id)
            || !locator_valid(file, &b.source)
            || !scopes.contains(b.scope_id.as_str())
            || b.declaration_id
                .as_ref()
                .is_some_and(|d| !declarations.contains(d.as_str()))
            || b.import_id
                .as_ref()
                .is_some_and(|i| !imports.contains(i.as_str()))
        {
            return Err(bad("binding"));
        }
    }
    for i in &file.imports {
        if !locator_valid(file, &i.source) || i.module_source.as_ref().is_some_and(|loc|!locator_valid(file,loc)) || !scopes.contains(i.scope_id.as_str()) {
            return Err(bad("import"));
        }
    }
    local_slice::validate(file,budget)?;
    error_boundaries::validate(file,budget)?;
    Ok(())
}

fn dependency(file: &FileEvidence) -> FileDependency {
    FileDependency {
        rel_path: file.rel_path.clone(),
        source_hash: file.source_hash.clone(),
        public_surface_fingerprint: file.public_surface_fingerprint.clone(),
    }
}

const RESOLUTION_WORK_LIMIT: &str = "resolution work limit";
const RESOLUTION_INSPECT_LIMIT: &str = "resolution inspected-record limit";
const RESOLUTION_LOOKUP_OMISSION: &str = "source occurrence left unresolved at the bounded candidate or witness lookup limit";
const RESOLUTION_OUTPUT_OMISSION: &str = "source occurrence left unresolved after the file's resolution output budget";
const RESOLUTION_BUDGET_OMISSION: &str = "source occurrence left unresolved after the file's resolution work budget";

fn unresolved_occurrence(occurrence: &OccurrenceEvidence, reason: &str) -> ResolutionEvidence {
    ResolutionEvidence {
        occurrence_id: occurrence.id.clone(),
        source: occurrence.source.clone(),
        caller_id: occurrence.caller_id.clone(),
        target_id: None,
        candidate_ids: Vec::new(),
        state: ResolutionState::Unresolved,
        reason: reason.into(),
        witnesses: Vec::new(),
        dependencies: Vec::new(),
    }
}

struct Resolver<'a> {
    budget: ResolutionBudget,
    work: usize,
    growth: usize,
    indexes: HashMap<&'a str, FileIndex<'a>>,
    paths: HashMap<&'a str, &'a FileEvidence>,
    rust_roots: Vec<&'a FileEvidence>,
    base_indexes: Option<&'a HashMap<&'a str, FileIndex<'a>>>,
    base_paths: Option<&'a HashMap<&'a str, &'a FileEvidence>>,
    base_rust_roots: Option<&'a [&'a FileEvidence]>,
    unavailable: &'a [UnavailableFile],
    unavailable_paths: Option<&'a HashSet<&'a str>>,
    // Present only for an immutable partition build. None also signals a
    // saturated trace, which cannot be used for a successor's partial plan.
    observed_paths: Option<(usize,BTreeSet<String>)>,
}
/// Borrowed indices contain no copied source/evidence. Their construction is
/// charged once against the same finite resolver work budget. Every visited
/// lookup and candidate is charged; absence no longer scans an entire file.
#[derive(Default)]
struct FileIndex<'a> {
    bindings: HashMap<&'a str, HashMap<&'a str, Vec<&'a BindingEvidence>>>,
    declarations: HashMap<&'a str, &'a DeclarationEvidence>,
    scoped_declarations: HashMap<&'a str, HashMap<&'a str, Vec<&'a DeclarationEvidence>>>,
    scopes: HashMap<&'a str, &'a LexicalScope>,
    imports: HashMap<&'a str, &'a ImportEvidence>,
    scoped_imports: HashMap<&'a str, HashMap<&'a str, Vec<&'a ImportEvidence>>>,
}
impl<'a> Resolver<'a> {
    fn new(budget: &ResolutionBudget) -> Self {
        Self { budget: budget.clone(), work: 0, growth: 0,
            indexes: HashMap::new(), paths: HashMap::new(), rust_roots: Vec::new(),
            base_indexes: None, base_paths: None, base_rust_roots: None, unavailable: &[], unavailable_paths: None, observed_paths:None }
    }
    fn add_file(&mut self, file: &'a FileEvidence) -> Result<(), EvidenceError> {
        self.tick()?;
        self.paths.insert(file.rel_path.as_str(), file);
        if file.language == "rust" && matches!(file.rel_path.rsplit('/').next(), Some("lib.rs" | "main.rs")) {
            self.rust_roots.push(file);
        }
        let mut index = FileIndex::default();
        for row in &file.bindings {
            self.tick()?;
            index.bindings.entry(&row.scope_id).or_default().entry(&row.name).or_default().push(row);
        }
        for row in &file.declarations {
            self.inspect(2)?;
            index.declarations.insert(&row.id, row);
            index.scoped_declarations.entry(&row.scope_id).or_default().entry(&row.name).or_default().push(row);
        }
        for row in &file.scopes {
            self.tick()?;
            index.scopes.insert(&row.id, row);
        }
        for row in &file.imports {
            self.inspect(2)?;
            index.imports.insert(&row.id, row);
            index.scoped_imports.entry(&row.scope_id).or_default().entry(&row.local_name).or_default().push(row);
        }
        self.indexes.insert(&file.file_id, index);
        Ok(())
    }
    fn observe(&mut self,path:&str){
        if let Some((bytes,paths))=&mut self.observed_paths {
            if paths.contains(path){return;}
            *bytes=bytes.saturating_add(path.len());
            if paths.len()>=1024||*bytes>128*1024{self.observed_paths=None;}
            else{paths.insert(path.to_owned());}
        }
    }
    fn lookup_path(&mut self,path:&str)->Option<&'a FileEvidence>{
        let found=self.paths.get(path).or_else(||self.base_paths.and_then(|base|base.get(path))).copied();
        if found.is_some(){self.observe(path);}
        found
    }
    fn index(&self, file: &FileEvidence) -> &FileIndex<'a> {
        self.indexes.get(file.file_id.as_str())
            .or_else(|| self.base_indexes.and_then(|base| base.get(file.file_id.as_str())))
            .expect("validated resolver file is indexed")
    }
    fn bindings(
        &mut self,
        file: &'a FileEvidence,
        scope: &str,
        name: &str,
    ) -> Result<Vec<&'a BindingEvidence>, EvidenceError> {
        let rows = self.index(file)
            .bindings
            .get(scope)
            .and_then(|names| names.get(name));
        let size = rows.map_or(0, Vec::len);
        self.inspect(size + 1)?;
        limit(
            size <= self.budget.max_candidates,
            "binding candidate limit",
        )?;
        Ok(self.index(file)
            .bindings
            .get(scope)
            .and_then(|names| names.get(name))
            .cloned()
            .unwrap_or_default())
    }
    fn declaration(
        &mut self,
        file: &'a FileEvidence,
        id: &str,
    ) -> Result<Option<&'a DeclarationEvidence>, EvidenceError> {
        self.tick()?;
        Ok(self.index(file)
            .declarations
            .get(id)
            .copied())
    }
    fn scope(
        &mut self,
        file: &'a FileEvidence,
        id: &str,
    ) -> Result<Option<&'a LexicalScope>, EvidenceError> {
        self.tick()?;
        Ok(self.index(file).scopes.get(id).copied())
    }
    fn import(
        &mut self,
        file: &'a FileEvidence,
        id: &str,
    ) -> Result<Option<&'a ImportEvidence>, EvidenceError> {
        self.tick()?;
        Ok(self.index(file).imports.get(id).copied())
    }
    fn members(
        &mut self,
        file: &'a FileEvidence,
        scope: &str,
        name: &str,
    ) -> Result<Vec<&'a DeclarationEvidence>, EvidenceError> {
        let size = self.index(file)
            .scoped_declarations
            .get(scope)
            .and_then(|names| names.get(name))
            .map_or(0, Vec::len);
        self.inspect(size + 1)?;
        limit(
            size <= self.budget.max_candidates,
            "declaration candidate limit",
        )?;
        Ok(self.index(file)
            .scoped_declarations
            .get(scope)
            .and_then(|names| names.get(name))
            .cloned()
            .unwrap_or_default())
    }
    fn imports(
        &mut self,
        file: &'a FileEvidence,
        scope: &str,
        name: &str,
    ) -> Result<Vec<&'a ImportEvidence>, EvidenceError> {
        let size = self.index(file)
            .scoped_imports
            .get(scope)
            .and_then(|names| names.get(name))
            .map_or(0, Vec::len);
        self.inspect(size + 1)?;
        limit(size <= self.budget.max_candidates, "import candidate limit")?;
        Ok(self.index(file)
            .scoped_imports
            .get(scope)
            .and_then(|names| names.get(name))
            .cloned()
            .unwrap_or_default())
    }
    fn growth<T: Serialize>(&mut self, value: &T) -> Result<(), EvidenceError> {
        self.growth = self
            .growth
            .checked_add(bounded_json(value, self.budget.max_output_bytes)?)
            .ok_or_else(|| {
                error(
                    EvidenceErrorCode::BudgetExceeded,
                    "resolver growth overflow",
                )
            })?;
        limit(
            self.growth <= self.budget.max_output_bytes,
            "resolver witness/dependency growth limit",
        )
    }
    fn witness(
        &mut self,
        witnesses: &mut Vec<ResolutionWitness>,
        witness: ResolutionWitness,
    ) -> Result<(), EvidenceError> {
        self.growth(&witness)?;
        witnesses.push(witness);
        Ok(())
    }
    fn tick(&mut self) -> Result<(), EvidenceError> {
        self.work += 1;
        limit(self.work <= self.budget.max_work, RESOLUTION_WORK_LIMIT)
    }
    fn inspect(&mut self, count: usize) -> Result<(), EvidenceError> {
        self.work = self
            .work
            .checked_add(count)
            .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "work overflow"))?;
        limit(
            self.work <= self.budget.max_work,
            RESOLUTION_INSPECT_LIMIT,
        )
    }
    fn unavailable_at(&mut self, path: &str) -> Result<bool, EvidenceError> {
        if let Some(paths) = self.unavailable_paths {
            self.tick()?;
            return Ok(paths.contains(path));
        }
        self.inspect(self.unavailable.len())?;
        Ok(self.unavailable.iter().any(|row| row.rel_path == path))
    }
    fn module_candidates(
        &mut self,
        from: &FileEvidence,
        module: &str,
    ) -> Result<Vec<&'a FileEvidence>, EvidenceError> {
        let mut keys = BTreeSet::new();
        if from.language == "python" {
            let dots = module.chars().take_while(|c| *c == '.').count();
            let path = module[dots..].replace('.', "/");
            if dots > 0 {
                let mut base: Vec<_> = from.rel_path.split('/').collect();
                base.pop();
                for _ in 1..dots {
                    base.pop();
                }
                if !path.is_empty() {
                    base.push(&path);
                }
                keys.insert(base.join("/"));
            } else {
                keys.insert(path);
            }
        } else if matches!(from.language.as_str(), "typescript" | "javascript") {
            if !module.starts_with('.') {
                return Ok(vec![]);
            }
            let mut base: Vec<&str> = from.rel_path.split('/').collect();
            base.pop();
            for part in module.split('/') {
                match part {
                    "." | "" => {}
                    ".." => {
                        if base.pop().is_none() {
                            return Ok(vec![]);
                        }
                    }
                    _ => base.push(part),
                }
            }
            keys.insert(base.join("/"));
        } else {
            let normalized = module.replace("::", "/");
            if normalized == "crate" {
                keys.insert("lib".into());
                keys.insert("main".into());
            } else if let Some(rest) = normalized.strip_prefix("crate/") {
                // Candidate roots are parsed source modules, not an inferred Git/Cargo identity.
                let base_roots = self.base_rust_roots.unwrap_or(&[]);
                for index in 0..self.rust_roots.len() + base_roots.len() {
                    let file = if index < self.rust_roots.len() { self.rust_roots[index] }
                        else { base_roots[index - self.rust_roots.len()] };
                    if index >= self.rust_roots.len() && self.paths.contains_key(file.rel_path.as_str()) { continue; }
                    self.tick()?;
                    if file.language == "rust"
                        && matches!(file.rel_path.rsplit('/').next(), Some("lib.rs" | "main.rs"))
                    {
                        let base = file
                            .rel_path
                            .rsplit_once('/')
                            .map(|p| format!("{}/", p.0))
                            .unwrap_or_default();
                        keys.insert(format!("{base}{rest}"));
                    }
                }
            } else {
                let mut base: Vec<_> = from.rel_path.split('/').collect();
                base.pop();
                let rest = normalized.strip_prefix("self/").unwrap_or(&normalized);
                base.push(rest);
                keys.insert(base.join("/"));
            }
        }
        let mut result = Vec::new();
        let mut seen = HashSet::new();
        for key in keys {
            let candidates: Vec<String> = match from.language.as_str() {
                "python" => vec![format!("{key}.py"), format!("{key}/__init__.py")],
                "typescript" | "javascript" => std::iter::once(key.clone())
                    .chain(["ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs"].into_iter().flat_map(|ext| {
                        [format!("{key}.{ext}"), format!("{key}/index.{ext}")]
                    }))
                    .collect(),
                "rust" => vec![format!("{key}.rs"), format!("{key}/mod.rs")],
                _ => vec![],
            };
            for path in candidates {
                self.tick()?;
                if let Some(file) = self.lookup_path(&path)
                    .filter(|file| file.language == from.language || matches!((file.language.as_str(), from.language.as_str()), ("typescript", "javascript") | ("javascript", "typescript")))
                {
                    if seen.insert(file.rel_path.as_str()) {
                        result.push(file);
                        limit(result.len() <= self.budget.max_candidates, "module candidate limit")?;
                    }
                }
            }
        }
        result.sort_by(|a, b| a.rel_path.cmp(&b.rel_path));
        Ok(result)
    }
    fn package_alias_modules(
        &mut self,
        from: &FileEvidence,
        import: &ImportEvidence,
        witnesses: &mut Vec<ResolutionWitness>,
        deps: &mut BTreeMap<String, FileDependency>,
    ) -> Result<Vec<&'a FileEvidence>, EvidenceError> {
        if !manifest::import_alias_name(&import.module_path) {
            return Ok(vec![]);
        }
        let mut parent = from.rel_path.rsplit_once('/').map_or("", |(dir, _)| dir);
        loop {
            self.tick()?;
            let prefix = if parent.is_empty() { String::new() } else { format!("{parent}/") };
            let config = format!("{prefix}package.json");
            if self.unavailable_at(&config)? {
                break;
            }
            if let Some(file) = self.lookup_path(&config) {
                let dependency = dependency(file);
                if !deps.contains_key(&file.rel_path) { self.growth(&dependency)?; }
                deps.insert(file.rel_path.clone(), dependency);
                let aliases = self.members(file, &file.root_scope_id, &import.module_path)?;
                if let [alias] = aliases.as_slice() {
                    if alias.kind == "package_import_alias" && alias.identity_state == IdentityState::Stable
                        && manifest::local_import_path(&alias.signature)
                    {
                        self.witness(witnesses, ResolutionWitness {
                            kind: "package_import_alias".into(),
                            source: alias.source.clone(),
                            description: "Nearest admitted package manifest declares an exact local alias".into(),
                        })?;
                        let path = format!("{prefix}{}", &alias.signature[2..]);
                        return Ok(self.lookup_path(&path)
                            .filter(|target| matches!(target.language.as_str(), "javascript" | "typescript"))
                            .into_iter().collect());
                    }
                }
                break;
            }
            if parent.is_empty() { break; }
            parent = parent.rsplit_once('/').map_or("", |(next, _)| next);
        }
        self.witness(witnesses, ResolutionWitness {
            kind: "package_import_alias_unavailable".into(),
            source: import.source.clone(),
            description: "No admitted exact local alias in the nearest package manifest".into(),
        })?;
        Ok(vec![])
    }

    fn import_targets(
        &mut self,
        from: &FileEvidence,
        import: &ImportEvidence,
        name: Option<&str>,
        seen: &mut HashSet<String>,
        hops: usize,
        witnesses: &mut Vec<ResolutionWitness>,
        deps: &mut BTreeMap<String, FileDependency>,
    ) -> Result<Vec<&'a DeclarationEvidence>, EvidenceError> {
        self.tick()?;
        limit(hops <= self.budget.max_alias_hops, "alias traversal limit")?;
        if !seen.insert(import.id.clone()) {
            self.witness(
                witnesses,
                ResolutionWitness {
                    kind: "alias_cycle".into(),
                    source: import.source.clone(),
                    description: "cyclic alias/reexport chain is unresolved".into(),
                },
            )?;
            return Ok(vec![]);
        }
        self.witness(
            witnesses,
            ResolutionWitness {
                kind: "import_binding".into(),
                source: import.source.clone(),
                description: format!(
                    "static import {} from {}",
                    import.local_name, import.module_path
                ),
            },
        )?;
        self.inspect(from.diagnostics.len())?;
        if from
            .diagnostics
            .iter()
            .any(|d| d.code == "conditional_import" && d.source.as_ref() == Some(&import.source))
        {
            self.witness(
                witnesses,
                ResolutionWitness {
                    kind: "conditional_import".into(),
                    source: import.source.clone(),
                    description: "parsed conditional import cannot establish unconditional binding"
                        .into(),
                },
            )?;
        }
        let wanted = name.or(import.imported_name.as_deref());
        let Some(wanted) = wanted else {
            return Ok(vec![]);
        };
        if wanted.contains('.') || wanted.contains("::") {
            return Ok(vec![]);
        }
        let config_alias = import.module_path.starts_with('#')
            && matches!(from.language.as_str(), "javascript" | "typescript");
        let modules = if config_alias {
            self.package_alias_modules(from, import, witnesses, deps)?
        } else {
            self.module_candidates(from, &import.module_path)?
        };
        if modules.len() > 1 {
            self.witness(
                witnesses,
                ResolutionWitness {
                    kind: "module_loader_ambiguity".into(),
                    source: import.source.clone(),
                    description: "multiple admitted module files require loader policy".into(),
                },
            )?;
        }
        let mut result = Vec::new();
        for module in modules {
            let dependency = dependency(module);
            if !deps.contains_key(&module.rel_path) {
                self.growth(&dependency)?;
            }
            deps.insert(module.rel_path.clone(), dependency);
            // Another omitted unit might shadow or reexport this name. A
            // partial module is navigable but cannot prove an imported target.
            if module.diagnostics.iter().any(|d| matches!(d.code.as_str(),"typescript_partial_syntax" | "generated_bundle_wrapper_partial")) {
                return Ok(Vec::new());
            }

            if self
                .bindings(module, &module.root_scope_id, wanted)?
                .iter()
                .any(|b| matches!(b.kind, BindingKind::Local | BindingKind::Parameter))
            {
                continue;
            }
            for declaration in self.members(module, &module.root_scope_id, wanted)? {
                self.tick()?;
                if declaration.scope_id == module.root_scope_id && declaration.name == wanted
                    && (!config_alias || declaration.explicit_export) {
                    result.push(declaration);
                    limit(
                        result.len() <= self.budget.max_candidates,
                        "import candidate limit",
                    )?;
                }
            }
            for next in self.imports(module, &module.root_scope_id, wanted)? {
                self.tick()?;
                if next.scope_id == module.root_scope_id
                    && next.local_name == wanted
                    && (module.language == "python" || next.reexport)
                {
                    result.extend(self.import_targets(
                        module,
                        next,
                        None,
                        seen,
                        hops + 1,
                        witnesses,
                        deps,
                    )?);
                    limit(
                        result.len() <= self.budget.max_candidates,
                        "reexport candidate limit",
                    )?;
                }
            }
        }
        Ok(result)
    }
    fn occurrence(
        &mut self,
        file: &'a FileEvidence,
        o: &OccurrenceEvidence,
    ) -> Result<ResolutionEvidence, EvidenceError> {
        let mut r = unresolved_occurrence(o, "no admitted static lexical binding");
        if o.syntax_kind == "manifest_dependency" {
            self.inspect(file.imports.len())?;
            let Some(import) = file.imports.iter().find(|i| digest(&["import_occurrence", &i.id]) == o.id) else {
                return Err(error(EvidenceErrorCode::InvalidCorpus, "manifest occurrence without import"));
            };
            let Some(root) = manifest::package_root(&import.module_path) else {
                r.state = ResolutionState::Unsupported; r.reason = "unsupported package specifier".into(); return Ok(r);
            };
            let mut dir = file.rel_path.rsplit_once('/').map(|x| x.0).unwrap_or("");
            loop {
                self.tick()?;
                let path = if dir.is_empty() { "package.json".to_owned() } else { format!("{dir}/package.json") };
                if self.unavailable_at(&path)? {
                    r.state = ResolutionState::Unsupported;
                    r.reason = "nearer manifest was excluded or unavailable; association cannot be established".into();
                    r.dependencies = vec![dependency(file)]; return Ok(r);
                }
                let candidate = self.lookup_path(&path);
                if let Some(manifest_file) = candidate {
                    self.inspect(manifest_file.declarations.len())?;
                    if manifest_file.diagnostics.iter().any(|d| d.code == "unsupported_manifest_dependency") {
                        r.state = ResolutionState::Unsupported;
                        r.reason = "manifest has unsupported dependency entries; association is unavailable".into();
                        r.dependencies = vec![dependency(file), dependency(manifest_file)];
                        return Ok(r);
                    }
                    let matches: Vec<_> = manifest_file.declarations.iter().filter(|d| d.kind == "package_dependency" && d.name == root).collect();
                    r.dependencies = vec![dependency(file), dependency(manifest_file)];
                    r.candidate_ids = matches.iter().map(|d| d.id.clone()).collect();
                    if matches.len() == 1 {
                        r.state = ResolutionState::Inferred; r.target_id = Some(matches[0].id.clone());
                        r.reason = "closest admitted manifest declaration; not runtime resolution and unseen exclusions remain possible".into();
                        r.witnesses.push(ResolutionWitness { kind: "manifest_dependency_key".into(), source: matches[0].source.clone(), description: matches[0].signature.clone() });
                    } else if matches.len() > 1 {
                        r.state = ResolutionState::Ambiguous; r.reason = "package declared in multiple dependency sections".into();
                    } else { r.reason = "closest admitted manifest has no supported matching dependency key".into(); }
                    return Ok(r);
                }
                if dir.is_empty() { break; }
                dir = dir.rsplit_once('/').map(|x| x.0).unwrap_or("");
            }
            r.state = ResolutionState::Unsupported; r.reason = "no admitted ancestor manifest; unseen excluded manifests remain possible".into();
            r.dependencies = vec![dependency(file)]; return Ok(r);
        }
        if o.syntax_kind == "sql_table_reference" {
            self.inspect(self.unavailable.len()+self.paths.len()+self.base_paths.map_or(0,|v|v.len()))?;
            if self.unavailable.iter().any(|u|u.rel_path.ends_with(".sql")) { r.state=ResolutionState::Unsupported;r.reason="SQL source unavailable; candidate set incomplete".into();return Ok(r); }
            let mut files:Vec<&FileEvidence>=self.paths.values().copied().chain(self.base_paths.into_iter().flat_map(|p|p.values().copied())).filter(|f|f.language=="sql_static").collect();
            files.sort_by(|a,b|a.rel_path.cmp(&b.rel_path));files.dedup_by(|a,b|a.rel_path==b.rel_path);
            let mut matches=Vec::new();let mut deps=vec![dependency(file)];
            for f in files {self.observe(&f.rel_path);self.inspect(f.declarations.len()+f.diagnostics.len())?;deps.push(dependency(f));if f.diagnostics.iter().any(|d|d.code=="unsupported_sql_statement"){r.state=ResolutionState::Unsupported;r.reason="unsupported SQL statements make candidate set incomplete".into();r.dependencies=deps;return Ok(r);}for d in &f.declarations {if d.kind=="sql_table"&&d.name==o.symbol_text {matches.push(d);}}}
            r.dependencies=deps;r.candidate_ids=matches.iter().map(|d|d.id.clone()).collect();
            if matches.len()==1 {r.state=ResolutionState::Inferred;r.target_id=Some(matches[0].id.clone());r.reason="unique admitted syntactic table name; no runtime database or migration identity".into();r.witnesses.push(ResolutionWitness{kind:"sql_table_declaration".into(),source:matches[0].source.clone(),description:"CREATE TABLE identifier".into()});}
            else if matches.len()>1 {r.state=ResolutionState::Ambiguous;r.reason="multiple admitted CREATE TABLE declarations".into();}
            else {r.state=ResolutionState::Unsupported;r.reason="no admitted syntactic table match; unseen SQL sources remain possible".into();}return Ok(r);
        }
        if o.syntax_kind == "go_interface_assertion" {
            self.inspect(file.declarations.len())?;
            r.dependencies = vec![dependency(file)];
            let candidates: Vec<_> = file.declarations.iter().filter(|declaration| {
                declaration.kind == "interface" && declaration.name == o.symbol_text
            }).collect();
            limit(candidates.len() <= self.budget.max_candidates, "Go interface candidate limit")?;
            r.candidate_ids = candidates.iter().map(|d| d.id.clone()).collect();
            if let Some(context) = &o.context_source {
                self.witness(&mut r.witnesses, ResolutionWitness {
                    kind: "go_pointer_conformance_assertion".into(),
                    source: context.clone(),
                    description: "Explicit pointer assignment to a named interface".into(),
                })?;
            }
            match candidates.as_slice() {
                [target] if target.identity_state == IdentityState::Stable
                    && file.declarations.iter().any(|d| Some(&d.id) == o.caller_id.as_ref() && d.identity_state == IdentityState::Stable) => {
                    r.state = ResolutionState::Inferred;
                    r.target_id = Some(target.id.clone());
                    r.reason = "Explicit same-file Go pointer assertion; compiler satisfaction is unverified".into();
                    self.witness(&mut r.witnesses, ResolutionWitness {
                        kind: "target_declaration".into(), source: target.source.clone(),
                        description: target.qualified_name.clone(),
                    })?;
                }
                [] => {
                    r.state = ResolutionState::Unsupported;
                    r.reason = "No same-file interface declaration in this Go profile".into();
                }
                _ => {
                    r.state = ResolutionState::Ambiguous;
                    r.reason = "Go interface or concrete declaration has ambiguous identity".into();
                }
            }
            return Ok(r);
        }
        if o.syntax_kind == "js_event_dispatch" {
            self.inspect(file.declarations.len().saturating_add(file.bindings.len()))?;
            let candidates: Vec<_> = file.declarations.iter().filter(|d| {
                d.scope_id == file.root_scope_id && d.name == o.symbol_text
                    && matches!(d.kind.as_str(), "function" | "function_value" | "component")
            }).collect();
            limit(candidates.len() <= self.budget.max_candidates, "JavaScript event candidate limit")?;
            r.candidate_ids = candidates.iter().map(|d| d.id.clone()).collect();
            r.dependencies = vec![dependency(file)];
            for (kind, witness) in [
                ("js_event_dispatch", &o.context_source),
                ("js_event_registration", &o.related_source),
            ] {
                if let Some(source) = witness {
                    self.witness(&mut r.witnesses, ResolutionWitness {
                        kind: kind.into(), source: source.clone(),
                        description: "Same-file EventTarget call with a matching literal name".into(),
                    })?;
                }
            }
            let bindings = file.bindings.iter().filter(|binding|
                binding.scope_id == file.root_scope_id && binding.name == o.symbol_text
            ).count();
            match candidates.as_slice() {
                [target] if bindings == 1 && !target.conditional && target.identity_state == IdentityState::Stable => {
                    r.state = ResolutionState::Inferred;
                    r.target_id = Some(target.id.clone());
                    r.reason = "Named handler and matching same-file EventTarget literal; runtime delivery is unverified".into();
                    self.witness(&mut r.witnesses, ResolutionWitness {
                        kind: "target_declaration".into(), source: target.source.clone(),
                        description: target.qualified_name.clone(),
                    })?;
                }
                [] => {
                    r.state = ResolutionState::Unsupported;
                    r.reason = "No same-file function binding for the registered handler".into();
                }
                _ => {
                    r.state = ResolutionState::Ambiguous;
                    r.reason = "Registered handler binding is ambiguous or conditional".into();
                }
            }
            return Ok(r);
        }
        if matches!(o.syntax_kind.as_str(), "java_event_publish" | "java_event_consume") {
            self.inspect(file.declarations.len().saturating_mul(2))?;
            r.dependencies = vec![dependency(file)];
            let is_publish = o.syntax_kind == "java_event_publish";
            let candidates: Vec<_> = file.declarations.iter().filter(|declaration| {
                declaration.qualified_name == o.symbol_text && if is_publish {
                    matches!(declaration.kind.as_str(), "class" | "enum")
                } else {
                    declaration.kind == "method"
                }
            }).collect();
            limit(candidates.len() <= self.budget.max_candidates, "Java event candidate limit")?;
            r.candidate_ids = candidates.iter().map(|d| d.id.clone()).collect();
            if let Some(context) = &o.context_source {
                self.witness(&mut r.witnesses, ResolutionWitness {
                    kind: o.syntax_kind.clone(),
                    source: context.clone(),
                    description: "Qualified Spring event syntax in the admitted source".into(),
                })?;
            }
            match candidates.as_slice() {
                [target] if target.identity_state == IdentityState::Stable
                    && file.declarations.iter().any(|d| Some(&d.id) == o.caller_id.as_ref() && d.identity_state == IdentityState::Stable) => {
                    r.state = ResolutionState::Inferred;
                    r.target_id = Some(target.id.clone());
                    r.reason = "Qualified same-file Spring event syntax; runtime delivery is unverified".into();
                    self.witness(&mut r.witnesses, ResolutionWitness {
                        kind: "target_declaration".into(), source: target.source.clone(),
                        description: target.qualified_name.clone(),
                    })?;
                }
                [] => {
                    r.state = ResolutionState::Unsupported;
                    r.reason = "No same-file event endpoint in this Java profile".into();
                }
                _ => {
                    r.state = ResolutionState::Ambiguous;
                    r.reason = "Java event endpoint has ambiguous identity".into();
                }
            }
            return Ok(r);
        }
        if o.syntax_kind=="overrides" {
            self.inspect(file.declarations.len()+file.occurrences.len())?;
            let method=o.caller_id.as_ref().and_then(|id|file.declarations.iter().find(|d|&d.id==id));
            let owner=method.and_then(|m|m.lexical_parent_id.as_ref()).and_then(|id|file.declarations.iter().find(|d|&d.id==id));
            let bases=owner.map(|owner|file.occurrences.iter().filter(|base|base.syntax_kind=="inherits"&&base.caller_id.as_ref()==Some(&owner.id)).collect::<Vec<_>>()).unwrap_or_default();
            r.dependencies=vec![dependency(file)];
            if bases.len()!=1 {r.state=ResolutionState::Unsupported;r.reason="override rule requires exactly one direct base".into();return Ok(r);}
            let base=self.occurrence(file,bases[0])?;
            let Some(base_id)=base.target_id else{r.state=ResolutionState::Unsupported;r.reason="direct base is not statically resolved".into();return Ok(r);};
            if !file.declarations.iter().any(|d|d.id==base_id&&matches!(d.kind.as_str(),"class"|"interface")){r.state=ResolutionState::Unsupported;r.reason="override rule requires a same-file direct class/interface base".into();return Ok(r);}
            let targets=file.declarations.iter().filter(|d|d.lexical_parent_id.as_deref()==Some(&base_id)&&d.name==o.symbol_text&&d.kind=="method").collect::<Vec<_>>();
            r.candidate_ids=targets.iter().map(|d|d.id.clone()).collect();
            if targets.len()==1&&targets[0].identity_state==IdentityState::Stable&&method.is_some_and(|m|m.identity_state==IdentityState::Stable) {
                r.target_id=Some(targets[0].id.clone());r.state=ResolutionState::Inferred;r.reason="single direct same-file base has matching method; no MRO/typechecker/runtime dispatch claim".into();
                r.witnesses=base.witnesses;r.witnesses.push(ResolutionWitness{kind:"direct_base_syntax".into(),source:bases[0].source.clone(),description:"single declared direct base".into()});r.witnesses.push(ResolutionWitness{kind:"static_override_target".into(),source:targets[0].source.clone(),description:targets[0].qualified_name.clone()});
            }else if targets.len()>1{r.state=ResolutionState::Ambiguous;r.reason="multiple same-named direct-base methods".into();}
            return Ok(r);
        }
        let mut deps = BTreeMap::from([(file.rel_path.clone(), dependency(file))]);
        let raw = &o.symbol_text;
        let (name, member) = if let Some((head, tail)) = raw.split_once('.') {
            (head, Some(tail))
        } else if let Some((head, tail)) = raw.split_once("::") {
            (head, Some(tail))
        } else {
            (raw.as_str(), None)
        };
        if o.syntax_kind == "macro_invocation"
            || raw.contains('(')
            || raw.contains('[')
            || raw.contains('!')
            || raw.contains('?')
        {
            r.state = ResolutionState::Unsupported;
            r.reason = "dynamic/macro callee requires unavailable semantics".into();
            r.dependencies = deps.into_values().collect();
            return Ok(r);
        }
        let mut scope = Some(o.scope_id.as_str());
        let mut candidates = Vec::new();
        let mut blocked = false;
        let mut via_import = false;
        while let Some(id) = scope {
            let bindings = self.bindings(file, id, name)?;
            if !bindings.is_empty() {
                for b in bindings {
                    self.tick()?;
                    self.witness(
                        &mut r.witnesses,
                        ResolutionWitness {
                            kind: "lexical_binding".into(),
                            source: b.source.clone(),
                            description: format!("{:?} binding {}", b.kind, b.name),
                        },
                    )?;
                    match b.kind {
                        BindingKind::Parameter | BindingKind::Local => blocked = true,
                        BindingKind::Declaration => {
                            if let Some(d) = &b.declaration_id {
                                if let Some(decl) = self.declaration(file, d)? {
                                    if let Some(member) = member {
                                        if decl.kind == "module" && file.language == "rust" {
                                            // Child declarations are scoped to the module's
                                            // parsed body scope, not its enclosing scope.
                                            let children: Vec<_> = file
                                                .declarations
                                                .iter()
                                                .filter(|n| {
                                                    n.lexical_parent_id.as_deref()
                                                        == Some(decl.id.as_str())
                                                        && n.name == member
                                                })
                                                .collect();
                                            self.inspect(file.declarations.len())?;
                                            candidates.extend(children);
                                        } else {
                                            blocked = true;
                                        }
                                    } else {
                                        candidates.push(decl);
                                    }
                                }
                            }
                        }
                        BindingKind::Import => {
                            via_import = true;
                            if let Some(i) = match b.import_id.as_ref() {
                                Some(id) => self.import(file, id)?,
                                None => None,
                            } {
                                if i.type_only && o.kind == OccurrenceKind::Call {
                                    blocked = true;
                                } else {
                                    candidates.extend(self.import_targets(
                                        file,
                                        i,
                                        member,
                                        &mut HashSet::new(),
                                        0,
                                        &mut r.witnesses,
                                        &mut deps,
                                    )?);
                                }
                            }
                        }
                    }
                    limit(
                        candidates.len() <= self.budget.max_candidates,
                        "binding candidate limit",
                    )?;
                }
                break;
            }
            scope = self.scope(file, id)?.and_then(|s| s.parent_id.as_deref());
            // A method body does not implicitly import its class/impl members.
            loop {
                let current = match scope {
                    Some(id) => self.scope(file, id)?,
                    None => None,
                };
                if current.is_some_and(|s| {
                    matches!(
                        s.kind.as_str(),
                        "class" | "implementation" | "trait" | "object_literal"
                    )
                }) {
                    scope = current.and_then(|s| s.parent_id.as_deref());
                } else {
                    break;
                }
            }
        }
        candidates.sort_by(|a, b| a.id.cmp(&b.id));
        candidates.dedup_by(|a, b| a.id == b.id);
        if blocked {
            r.state = ResolutionState::Unsupported;
            r.reason =
                "local/parameter/dynamic receiver binding does not prove callable target".into();
        } else if candidates.len() > 1
            || candidates
                .iter()
                .any(|d| d.identity_state == IdentityState::AmbiguousSnapshotLocal)
        {
            r.state = ResolutionState::Ambiguous;
            r.reason = "multiple or snapshot-local static declaration candidates".into();
        } else if let Some(target) = candidates.first() {
            r.target_id = Some(target.id.clone());
            r.state = if target.conditional
                || (via_import && file.language != "python")
                || file.imports.iter().any(|i| i.is_glob)
                || r.witnesses.iter().any(|w| {
                    matches!(
                        w.kind.as_str(),
                        "module_loader_ambiguity" | "conditional_import"
                    )
                }) {
                ResolutionState::Inferred
            } else {
                ResolutionState::Exact
            };
            r.reason = if r.state == ResolutionState::Exact {
                "unique static lexical binding, not runtime dispatch"
            } else {
                "conditional, wildcard or loader-dependent static candidate"
            }
            .into();
            self.witness(
                &mut r.witnesses,
                ResolutionWitness {
                    kind: "target_declaration".into(),
                    source: target.source.clone(),
                    description: target.qualified_name.clone(),
                },
            )?;
        } else if member.is_some() || r.witnesses.iter().any(|w| matches!(w.kind.as_str(), "alias_cycle" | "package_import_alias_unavailable")) {
            r.state = ResolutionState::Unsupported;
            r.reason =
                "receiver/module/alias-cycle target unavailable; no bare-name fallback".into();
        }
        r.candidate_ids = candidates.iter().map(|d| d.id.clone()).collect();
        if o.syntax_kind=="callback_registration" {
            if via_import || candidates.first().is_some_and(|d| !matches!(d.kind.as_str(),"function"|"method")) {
                r.target_id=None;r.state=ResolutionState::Unsupported;r.reason="callback requires an unaliased unique lexical function declaration".into();
            } else if r.target_id.is_some() {
                r.state=ResolutionState::Inferred;r.reason="syntactic event registration only; receiver, registration and invocation are not proved".into();
                if let Some(context)=&o.context_source {self.witness(&mut r.witnesses,ResolutionWitness{kind:"callback_registration_call".into(),source:context.clone(),description:"syntactic event registration call".into()})?;}
            }
        }
        if matches!(o.syntax_kind.as_str(),"fastapi_route_registration"|"jsx_render")&&r.state==ResolutionState::Exact{r.state=ResolutionState::Inferred;r.reason="named static framework syntax; runtime registration/render is not asserted".into();}
        r.dependencies = deps.into_values().collect();
        Ok(r)
    }
}

pub fn resolve_evidence(
    input: &CorpusInput,
    budget: &ResolutionBudget,
) -> Result<CorpusEvidence, EvidenceError> {
    let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Resolve);
    resolve_window(input, budget, None)
}

/// Validated immutable interfaces and borrowed lookup indexes. This object
/// never reads source or admits an index. Construct it once per generation;
/// each active partition overlays only its own full evidence/index records.
pub struct PartitionResolver<'a> {
    input: &'a CorpusInput,
    indexes: HashMap<&'a str, FileIndex<'a>>,
    paths: HashMap<&'a str, &'a FileEvidence>,
    rust_roots: Vec<&'a FileEvidence>,
    records: usize,
    dependencies: Vec<FileDependency>,
    unavailable_files: Vec<UnavailableFile>,
    unavailable_paths: HashSet<&'a str>,
}
fn evidence_records(file: &FileEvidence) -> Result<usize, EvidenceError> {
    [file.declarations.len(), file.bindings.len(), file.imports.len(), file.scopes.len(), file.occurrences.len()]
        .into_iter().try_fold(0usize, |sum, count| sum.checked_add(count)
            .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "record overflow")))
}
impl<'a> PartitionResolver<'a> {
    pub fn new(input: &'a CorpusInput) -> Result<Self, EvidenceError> {
        let budget = ResolutionBudget {
            max_files: crate::code_index_limits::MAX_INDEXED_FILES,
            ..ResolutionBudget::default()
        };
        limit(input.files.len().saturating_add(input.unavailable_files.len()) <= budget.max_files, "partition file limit")?;
        bounded_json(input, MAX_MODULE_INTERFACE_BYTES)?;
        let extraction = ExtractionBudget { max_serialized_bytes: storage::MAX_DECODED, ..ExtractionBudget::default() };
        let mut paths = HashSet::new();
        let mut records = 0usize;
        let mut resolver = Resolver::new(&budget);
        for file in &input.files {
            if !file.occurrences.is_empty() || file.checkout_id != input.checkout_id || !paths.insert(file.rel_path.as_str()) {
                return Err(error(EvidenceErrorCode::InvalidCorpus, "invalid/mixed/duplicate module interface"));
            }
            validate_file_evidence(file, &extraction)?;
            if storage::module_interface(file) != *file {
                return Err(error(EvidenceErrorCode::InvalidCorpus, "noncanonical module interface"));
            }
            records = records.checked_add(evidence_records(file)?)
                .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "record overflow"))?;
            limit(records <= budget.max_records, "corpus record limit")?;
            resolver.add_file(file)?;
        }
        for file in &input.unavailable_files {
            if !valid_path(&file.rel_path) || !paths.insert(file.rel_path.as_str()) {
                return Err(error(EvidenceErrorCode::InvalidCorpus, "duplicate/invalid unavailable path"));
            }
        }
        let mut dependencies: Vec<_> = input.files.iter().map(dependency).collect();
        dependencies.sort_by(|a,b|a.rel_path.cmp(&b.rel_path));
        let mut unavailable_files = input.unavailable_files.clone();
        unavailable_files.sort_by(|a,b|a.rel_path.cmp(&b.rel_path));
        let unavailable_paths = input.unavailable_files.iter().map(|row|row.rel_path.as_str()).collect();
        Ok(Self { input, indexes: resolver.indexes, paths: resolver.paths, rust_roots: resolver.rust_roots,
            records, dependencies, unavailable_files, unavailable_paths })
    }

    pub fn resolve(&self, active: &FileEvidence) -> Result<CorpusEvidence, EvidenceError> {
        let (resolutions, _) = self.resolve_rows_with_trace(active, false)?;
        let result = CorpusEvidence {
            format_version: EVIDENCE_FORMAT_VERSION,
            checkout_id: self.input.checkout_id.clone(),
            producer_fingerprint: crate::code::producer_fingerprint(),
            complete: self.unavailable_files.is_empty() &&
                resolutions.iter().all(|row|row.reason!=RESOLUTION_BUDGET_OMISSION && row.reason!=RESOLUTION_OUTPUT_OMISSION && row.reason!=RESOLUTION_LOOKUP_OMISSION),
            resolutions,
            dependencies: self.dependencies.clone(),
            unavailable_files: self.unavailable_files.clone(),
        };
        bounded_json(&result, ResolutionBudget::default().max_output_bytes)?;
        Ok(result)
    }

    pub(crate) fn resolve_rows_with_trace(&self,active:&FileEvidence,trace:bool)
        ->Result<(Vec<ResolutionEvidence>,Option<BTreeSet<String>>),EvidenceError>{
        self.resolve_rows(active, trace, &ResolutionBudget::default())
    }

    fn resolve_rows(&self,active:&FileEvidence,trace:bool,budget:&ResolutionBudget)
        ->Result<(Vec<ResolutionEvidence>,Option<BTreeSet<String>>),EvidenceError>{
        let extraction = ExtractionBudget { max_serialized_bytes: storage::MAX_PARTITION_JSON, ..ExtractionBudget::default() };
        validate_file_evidence(active, &extraction)?;
        let interface = self.paths.get(active.rel_path.as_str())
            .ok_or_else(|| error(EvidenceErrorCode::InvalidCorpus, "active partition is missing"))?;
        if storage::module_interface(active) != **interface {
            return Err(error(EvidenceErrorCode::InvalidCorpus, "active partition differs from immutable interface"));
        }
        let records = self.records.checked_sub(evidence_records(interface)?)
            .and_then(|n| n.checked_add(evidence_records(active).ok()?))
            .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "record overflow"))?;
        limit(records <= budget.max_records, "corpus record limit")?;
        let mut resolver = Resolver::new(budget);
        resolver.base_indexes = Some(&self.indexes);
        resolver.base_paths = Some(&self.paths);
        resolver.base_rust_roots = Some(&self.rust_roots);
        resolver.unavailable = &self.input.unavailable_files;
        resolver.unavailable_paths = Some(&self.unavailable_paths);
        if trace{resolver.observed_paths=Some((0,BTreeSet::new()));}
        resolver.add_file(active)?;
        let mut resolutions = Vec::new();
        // Reserve one bounded row per occurrence, including the JSON array framing.
        // A large witness list can consume spare capacity only after this reservation.
        let mut reserved = active.occurrences.len().saturating_sub(1) + 2;
        let mut minimum_sizes = Vec::with_capacity(active.occurrences.len());
        for occurrence in &active.occurrences {
            let minimal = unresolved_occurrence(occurrence, RESOLUTION_OUTPUT_OMISSION);
            let size = bounded_json(&minimal, budget.max_output_bytes)?;
            reserved = reserved.checked_add(size)
                .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "resolution output overflow"))?;
            minimum_sizes.push(size);
        }
        limit(reserved <= budget.max_output_bytes, "resolution output limit")?;
        let mut spare = budget.max_output_bytes - reserved;
        let mut work_exhausted = false;
        for (occurrence, minimum) in active.occurrences.iter().zip(minimum_sizes) {
            let resolution = if work_exhausted {
                unresolved_occurrence(occurrence, RESOLUTION_BUDGET_OMISSION)
            } else {
                match resolver.occurrence(active, occurrence) {
                    Ok(resolution) => resolution,
                    Err(error) if error.code == EvidenceErrorCode::BudgetExceeded
                        && matches!(error.message.as_str(), RESOLUTION_WORK_LIMIT | RESOLUTION_INSPECT_LIMIT) => {
                        // The store can publish known edges and unresolved
                        // occurrences. An incomplete trace cannot be reused
                        // for partial re-resolution in a successor.
                        work_exhausted = true;
                        resolver.observed_paths = None;
                        unresolved_occurrence(occurrence, RESOLUTION_BUDGET_OMISSION)
                    }
                    Err(error) if error.code == EvidenceErrorCode::BudgetExceeded => {
                        // Candidate and alias limits apply to this lookup. Partial candidate
                        // sets cannot prove a link. Keep the occurrence and try the next one.
                        resolver.observed_paths = None;
                        unresolved_occurrence(occurrence, RESOLUTION_LOOKUP_OMISSION)
                    }
                    Err(error) => return Err(error),
                }
            };
            let available = spare + minimum;
            match bounded_json(&resolution, available) {
                Ok(size) => {
                    spare = available - size;
                    resolutions.push(resolution);
                }
                Err(_) => {
                    resolver.observed_paths = None;
                    resolutions.push(unresolved_occurrence(occurrence, RESOLUTION_OUTPUT_OMISSION));
                }
            }
        }
        resolutions.sort_by(|a,b|a.occurrence_id.cmp(&b.occurrence_id));
        let paths=resolver.observed_paths.map(|(_,mut paths)|{paths.remove(&active.rel_path);paths});
        Ok((resolutions,paths))
    }
}

/// A bounded active source plus previously validated, root-export interfaces.
/// Interfaces carry no occurrences. No source/body read is performed here.
pub fn resolve_partition(input: &CorpusInput, active: &str) -> Result<CorpusEvidence, EvidenceError> {
    let mut budget = ResolutionBudget::default();
    // Two independently bounded windows: module interfaces and active file.
    budget.max_files = crate::code_index_limits::MAX_INDEXED_FILES;
    budget.max_corpus_bytes = storage::MAX_DECODED + MAX_MODULE_INTERFACE_BYTES;
    validate_partition_window(input, active, storage::MAX_DECODED, MAX_MODULE_INTERFACE_BYTES)?;
    resolve_window(input, &budget, Some(active))
}

fn validate_partition_window(
    input: &CorpusInput,
    active: &str,
    maximum_bytes: usize,
    maximum_interface_bytes: usize,
) -> Result<(), EvidenceError> {
    limit(
        input.files.len().saturating_add(input.unavailable_files.len()) <= crate::code_index_limits::MAX_INDEXED_FILES,
        "partition file limit",
    )?;
    let mut active_file = None;
    let mut interfaces = Vec::new();
    for file in &input.files {
        if file.rel_path == active {
            if active_file.replace(file).is_some() {
                return Err(error(EvidenceErrorCode::InvalidCorpus, "duplicate active partition"));
            }
        } else {
            if !file.occurrences.is_empty() {
                return Err(error(EvidenceErrorCode::InvalidCorpus, "module interface contains occurrences"));
            }
            interfaces.push(file);
        }
    }
    let active_file = active_file.ok_or_else(|| error(EvidenceErrorCode::InvalidCorpus, "active partition is missing"))?;
    bounded_json(active_file, maximum_bytes)?;
    // Unavailable coverage and checkout metadata share the interface window;
    // neither can borrow the active partition's unused byte allowance.
    bounded_json(&(input.checkout_id.as_str(), interfaces, &input.unavailable_files), maximum_interface_bytes)?;
    Ok(())
}

fn resolve_window(
    input: &CorpusInput,
    budget: &ResolutionBudget,
    active: Option<&str>,
) -> Result<CorpusEvidence, EvidenceError> {
    limit(
        input.files.len() + input.unavailable_files.len() <= budget.max_files,
        "corpus file limit",
    )?;
    bounded_json(input, budget.max_corpus_bytes)?;
    let mut paths = HashSet::new();
    let mut records = 0usize;
    for file in &input.files {
        let mut extraction = ExtractionBudget::default();
        if active.is_some() {
            extraction.max_serialized_bytes = storage::MAX_DECODED;
        }
        validate_file_evidence(file, &extraction)?;
        if file.checkout_id != input.checkout_id || !paths.insert(&file.rel_path) {
            return Err(error(
                EvidenceErrorCode::InvalidCorpus,
                "mixed checkout or duplicate corpus path",
            ));
        }
        records = records
            .checked_add(
                file.declarations.len()
                    + file.occurrences.len()
                    + file.bindings.len()
                    + file.imports.len()
                    + file.scopes.len(),
            )
            .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "record overflow"))?;
        limit(records <= budget.max_records, "corpus record limit")?;
    }
    for unavailable in &input.unavailable_files {
        if !valid_path(&unavailable.rel_path) || !paths.insert(&unavailable.rel_path) {
            return Err(error(
                EvidenceErrorCode::InvalidCorpus,
                "duplicate/invalid unavailable path",
            ));
        }
    }
    let mut resolver = Resolver::new(budget);
    resolver.unavailable = &input.unavailable_files;
    for file in &input.files {
        resolver.add_file(file)?;
    }
    let mut resolutions = Vec::new();
    let mut output_bytes = 0usize;
    for file in &input.files {
        if active.is_some_and(|path| path != file.rel_path) {
            continue;
        }
        for occurrence in &file.occurrences {
            let r = resolver.occurrence(file, occurrence)?;
            output_bytes = output_bytes
                .checked_add(bounded_json(&r, budget.max_output_bytes)?)
                .ok_or_else(|| {
                    error(
                        EvidenceErrorCode::BudgetExceeded,
                        "resolution output overflow",
                    )
                })?;
            limit(
                output_bytes <= budget.max_output_bytes,
                "resolution output limit",
            )?;
            resolutions.push(r);
        }
    }
    resolutions.sort_by(|a, b| a.occurrence_id.cmp(&b.occurrence_id));
    let mut dependencies: Vec<_> = input.files.iter().map(dependency).collect();
    dependencies.sort_by(|a, b| a.rel_path.cmp(&b.rel_path));
    let mut unavailable_files = input.unavailable_files.clone();
    unavailable_files.sort_by(|a, b| a.rel_path.cmp(&b.rel_path));
    let result = CorpusEvidence {
        format_version: EVIDENCE_FORMAT_VERSION,
        checkout_id: input.checkout_id.clone(),
        producer_fingerprint: crate::code::producer_fingerprint(),
        complete: unavailable_files.is_empty(),
        resolutions,
        dependencies,
        unavailable_files,
    };
    bounded_json(&result, budget.max_output_bytes)?;
    Ok(result)
}
pub fn affected_files(
    before: &CorpusInput,
    after: &CorpusInput,
    changed: &[String],
) -> Result<Vec<String>, EvidenceError> {
    let budget = ResolutionBudget::default();
    bounded_json(before, budget.max_corpus_bytes)?;
    bounded_json(after, budget.max_corpus_bytes)?;
    limit(
        before.files.len() + before.unavailable_files.len() <= budget.max_files
            && after.files.len() + after.unavailable_files.len() <= budget.max_files,
        "affected corpus file limit",
    )?;
    if before.checkout_id != after.checkout_id {
        return Err(error(EvidenceErrorCode::InvalidCorpus, "checkout differs"));
    }
    validate_continuity_input(before, &budget)?;
    validate_continuity_input(after, &budget)?;
    if changed.is_empty() && before == after {
        return Ok(vec![]);
    }
    // Deliberately conservative whole-corpus dependency closure; no incremental optimization claim.
    Ok(before
        .files
        .iter()
        .chain(&after.files)
        .map(|f| f.rel_path.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect())
}
fn validation(state: ValidationState, reason: &str) -> EvidenceValidation {
    EvidenceValidation {
        state,
        reason: reason.into(),
        manifest_comparison: None,
    }
}
fn checked_slice<'a>(bytes: &'a [u8], span: &SourceSpan) -> Option<&'a str> {
    let source = std::str::from_utf8(bytes).ok()?;
    let start = usize::try_from(span.start_byte).ok()?;
    let end = usize::try_from(span.end_byte).ok()?;
    let value = source.get(start..end)?;
    let point = |offset: usize| {
        let prefix = &bytes[..offset];
        let row = prefix.iter().filter(|b| **b == b'\n').count() + 1;
        let col = prefix
            .iter()
            .rposition(|b| *b == b'\n')
            .map(|i| offset - i - 1)
            .unwrap_or(offset);
        (u32::try_from(row).ok(), u32::try_from(col).ok())
    };
    if point(start) != (Some(span.start_line), Some(span.start_column))
        || point(end) != (Some(span.end_line), Some(span.end_column))
    {
        return None;
    }
    Some(value)
}

/// A bounded current on-disk package observation; never published to the index.
/// Only a validated app binding and a literal package name can select paths.
pub fn observe_on_disk_library(binding:&crate::code::WorkspaceBinding,package:&str)->Result<serde_json::Value,String>{
    use sha2::{Digest,Sha256};
    use std::path::{Component,Path};
    let denied=||"library source unavailable".to_string();
    fn component(v:&str)->bool { !v.is_empty() && v.len()<=214 && (v.as_bytes()[0].is_ascii_lowercase() || v.as_bytes()[0].is_ascii_digit()) && v.bytes().all(|b|b.is_ascii_lowercase()||b.is_ascii_digit()||matches!(b,b'-'|b'_'|b'.')) && !v.contains("..") }
    if !matches!(binding.origin.as_str(),"app"|"agent") || package.len()>214 || !package.is_ascii(){return Err(denied());}
    let pieces:Vec<_>=package.split('/').collect();
    if !(pieces.len()==1 && component(pieces[0]) || pieces.len()==2 && pieces[0].strip_prefix('@').is_some_and(component) && component(pieces[1])) {return Err(denied());}
    let private=[".git",".clanker",".clankers",".references",".ssh",".aws",".gnupg"];
    if !binding.root.is_absolute() || binding.root.components().any(|c|matches!(c,Component::Normal(n) if n.to_str().is_none_or(|x|private.iter().any(|entry|entry.eq_ignore_ascii_case(x))))) {return Err(denied());}
    binding.validate().map_err(|_|denied())?;
    let identity=crate::code::directory_identity(&binding.root).map_err(|_|denied())?;
    let package_rel=Path::new("node_modules").join(package);
    let manifest_rel=package_rel.join("package.json");
    let manifest_path=binding.root.join(&manifest_rel);
    let first=crate::code::acquire_library_source(&binding.root,&manifest_path,&identity).map_err(|_|denied())?;
    let budget=ExtractionBudget {max_source_bytes:131072,max_ast_nodes:60000,max_depth:128,..ExtractionBudget::default()};
    let fields=manifest::library_fields(&first.bytes,&budget).map_err(|_|denied())?;
    if fields.name.as_deref()!=Some(package) {return Err(denied());}
    let raw=fields.types.ok_or_else(denied)?;
    let raw=raw.strip_prefix("./").unwrap_or(&raw);
    if raw.len()>512 || !raw.is_ascii() || !raw.ends_with(".d.ts") {return Err(denied());}
    let parts:Vec<_>=raw.split('/').collect();
    if parts.len()>32 || parts.iter().any(|v|v.is_empty() || v.starts_with('.') || v.eq_ignore_ascii_case("node_modules") || !v.bytes().all(|b|b.is_ascii_alphanumeric()||matches!(b,b'-'|b'_'|b'.'))) {return Err(denied());}
    let decl_rel=package_rel.join(raw); let decl_path=binding.root.join(&decl_rel);
    let declaration=crate::code::acquire_library_source(&binding.root,&decl_path,&identity).map_err(|_|denied())?;
    let rel=decl_rel.to_str().ok_or_else(denied)?;
    let evidence=extract_evidence("ephemeral-library",rel,&declaration.bytes,&budget).map_err(|_|denied())?;
    let mut items=Vec::new(); let mut omitted=0usize; let mut projected_bytes=0usize;
    for item in &evidence.declarations {
        if items.len()>=128 || item.name.len()>256 || item.signature.len()>1024 || item.name.chars().any(|c|c.is_control()) || item.signature.chars().any(|c|c.is_control() && c!='\t') {omitted+=1;continue;}
        let projected=serde_json::json!({"kind":item.kind,"name":item.name,"signature":item.signature,"span":item.source.span});
        let size=serde_json::to_vec(&projected).map_err(|_|denied())?.len();
        if projected_bytes.saturating_add(size)>12000 {omitted+=1;continue;}
        projected_bytes+=size;items.push(projected);
    }
    let again=crate::code::acquire_library_source(&binding.root,&manifest_path,&identity).map_err(|_|denied())?;
    let decl_again=crate::code::acquire_library_source(&binding.root,&decl_path,&identity).map_err(|_|denied())?;
    if again.bytes!=first.bytes || decl_again.bytes!=declaration.bytes || crate::code::directory_identity(&binding.root).map_err(|_|denied())?!=identity {return Err(denied());}
    binding.validate().map_err(|_|denied())?;
    Ok(serde_json::json!({"schema":"open-clank.library-observation/v1","state":"current","workspace_id":binding.workspace_id,"package":package,
      "manifest":{"rel_path":manifest_rel.to_str().ok_or_else(denied)?,"sha256":format!("{:x}",Sha256::digest(&first.bytes)),"size":first.bytes.len(),"name":package,"version":fields.version},
      "declaration":{"rel_path":rel,"sha256":format!("{:x}",Sha256::digest(&declaration.bytes)),"size":declaration.bytes.len(),"items":items,"omitted":omitted},
      "partial":true,"reasons":["manifest_declared_types_only","exports_and_runtime_unproved"]}))
}

pub fn validate_source_evidence(
    file: &FileEvidence,
    current_bytes: &[u8],
    claim: &SourceClaim,
    budget: &ExtractionBudget,
) -> Result<EvidenceValidation, EvidenceError> {
    limit(
        current_bytes.len() <= budget.max_source_bytes,
        "source validation byte limit",
    )?;
    if claim.source.file_id != file.file_id || claim.source.rel_path != file.rel_path {
        return Ok(validation(
            ValidationState::Invalid,
            "source locator identifies another file",
        ));
    }
    if blake3::hash(current_bytes).to_hex().as_str() != claim.source.source_hash
        || claim.source.source_hash != file.source_hash
    {
        return Ok(validation(ValidationState::Stale, "source hash changed"));
    }
    let Some(current) = checked_slice(current_bytes, &claim.source.span) else {
        return Ok(validation(
            ValidationState::Invalid,
            "invalid UTF-8 byte/line range",
        ));
    };
    match &claim.predicate {
        SourcePredicate::File => {
            return Ok(validation(
                if claim.source.span.start_byte == 0
                    && claim.source.span.end_byte == current_bytes.len() as u64
                {
                    ValidationState::Current
                } else {
                    ValidationState::Invalid
                },
                "whole-file source locator",
            ))
        }
        SourcePredicate::ManifestFileDependency { expected_spec_sha256 } => {
            if !file.rel_path.rsplit('/').next().is_some_and(|part| part == "package.json")
                || expected_spec_sha256.len() != 64
                || !expected_spec_sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
                return Ok(validation(ValidationState::Invalid, "invalid manifest comparison selector"));
            }
            let mut expected=[0u8;32];
            for (index, byte) in expected.iter_mut().enumerate() {
                *byte=u8::from_str_radix(&expected_spec_sha256[index*2..index*2+2],16)
                    .map_err(|_| error(EvidenceErrorCode::InvalidSource,"invalid manifest comparison selector"))?;
            }
            let compared=manifest::compare_file_dependency(current_bytes,budget,expected)?;
            let comparison=match compared {
                manifest::ManifestComparison::Match { key, value } => ManifestValidation::Match {
                    key_start:key.0 as u64,key_end:key.1 as u64,value_start:value.0 as u64,value_end:value.1 as u64,
                },
                manifest::ManifestComparison::NoMatch => ManifestValidation::NoMatch,
                manifest::ManifestComparison::Unsupported => ManifestValidation::Unsupported,
            };
            return Ok(EvidenceValidation { state:ValidationState::Current,
                reason:"bounded manifest comparison against current source".into(),manifest_comparison:Some(comparison) });
        }
        SourcePredicate::ExactText { text } => {
            return Ok(validation(
                if current == text {
                    ValidationState::Current
                } else {
                    ValidationState::Invalid
                },
                "exact source bytes compared",
            ))
        }
        SourcePredicate::Unsupported { .. } => {
            return Ok(validation(
                ValidationState::Unsupported,
                "predicate is not admitted",
            ))
        }
        _ => {}
    }
    if !language_capability(&file.rel_path).available {
        return Ok(validation(
            ValidationState::Unsupported,
            "semantic predicate requires admitted grammar",
        ));
    }
    let extracted = extract_evidence(&file.checkout_id, &file.rel_path, current_bytes, budget)?;
    let matches = match &claim.predicate {
        SourcePredicate::Declaration {
            declaration_id,
            declaration_kind,
            qualified_name,
            signature_hash,
        } => extracted.declarations.iter().any(|d| {
            &d.id == declaration_id
                && &d.kind == declaration_kind
                && &d.qualified_name == qualified_name
                && &d.signature_hash == signature_hash
                && d.source == claim.source
        }),
        SourcePredicate::Occurrence {
            occurrence_id,
            occurrence_kind,
            symbol_text,
        } => extracted.occurrences.iter().any(|o| {
            &o.id == occurrence_id
                && &o.kind == occurrence_kind
                && &o.symbol_text == symbol_text
                && o.source == claim.source
        }),
        SourcePredicate::ImportBinding {
            import_id,
            local_name,
            module_path,
            imported_name,
        } => extracted.imports.iter().any(|i| {
            &i.id == import_id
                && &i.local_name == local_name
                && &i.module_path == module_path
                && &i.imported_name == imported_name
                && i.source == claim.source
        }),
        _ => false,
    };
    Ok(validation(
        if matches {
            ValidationState::Current
        } else {
            ValidationState::Invalid
        },
        "predicate reparsed against current bytes",
    ))
}
pub fn validate_resolution_evidence(
    input: &CorpusInput,
    claim: &ResolutionEvidence,
    budget: &ResolutionBudget,
) -> Result<EvidenceValidation, EvidenceError> {
    let current = resolve_evidence(input, budget)?;
    Ok(validation(
        if current.resolutions.iter().any(|r| r == claim) {
            ValidationState::Current
        } else {
            ValidationState::Invalid
        },
        "resolution, endpoints, witnesses and dependency receipts compared",
    ))
}
pub fn validate_path_evidence(
    corpus: &CorpusEvidence,
    occurrence_ids: &[String],
    budget: &ResolutionBudget,
) -> Result<EvidenceValidation, EvidenceError> {
    limit(
        occurrence_ids.len() <= budget.max_path_hops,
        "path hop limit",
    )?;
    bounded_json(corpus, budget.max_output_bytes)?;
    if occurrence_ids.is_empty()
        || corpus.format_version != EVIDENCE_FORMAT_VERSION
        || corpus.producer_fingerprint != crate::code::producer_fingerprint()
    {
        return Ok(validation(
            ValidationState::Invalid,
            "empty path or producer mismatch",
        ));
    }
    let mut work = corpus.resolutions.len() + corpus.dependencies.len();
    limit(work <= budget.max_work, "path index work limit")?;
    let resolutions: HashMap<_, _> = corpus
        .resolutions
        .iter()
        .map(|r| (&r.occurrence_id, r))
        .collect();
    let dependencies: HashMap<_, _> = corpus
        .dependencies
        .iter()
        .map(|d| (&d.rel_path, d))
        .collect();
    if resolutions.len() != corpus.resolutions.len()
        || dependencies.len() != corpus.dependencies.len()
    {
        return Ok(validation(
            ValidationState::Invalid,
            "duplicate path evidence identity",
        ));
    }
    let mut previous: Option<&str> = None;
    for id in occurrence_ids {
        let Some(r) = resolutions.get(id) else {
            return Ok(validation(
                ValidationState::Invalid,
                "missing path occurrence",
            ));
        };
        work = work
            .checked_add(r.witnesses.len() + r.dependencies.len() + 1)
            .ok_or_else(|| error(EvidenceErrorCode::BudgetExceeded, "path work overflow"))?;
        limit(work <= budget.max_work, "path dependency work limit")?;
        let local: HashMap<_, _> = r.dependencies.iter().map(|d| (&d.rel_path, d)).collect();
        if r.state != ResolutionState::Exact
            || r.target_id.is_none()
            || r.candidate_ids.len() != 1
            || r.candidate_ids.first() != r.target_id.as_ref()
            || !local
                .get(&r.source.rel_path)
                .is_some_and(|d| d.source_hash == r.source.source_hash)
            || !r.witnesses.iter().any(|w| w.kind == "target_declaration")
            || r.witnesses.iter().any(|w| {
                !local
                    .get(&w.source.rel_path)
                    .is_some_and(|d| d.source_hash == w.source.source_hash)
            })
            || !r.dependencies.iter().all(|d| {
                dependencies
                    .get(&d.rel_path)
                    .is_some_and(|known| **known == *d)
            })
        {
            return Ok(validation(
                ValidationState::Invalid,
                "path lacks exact witnessed dependency receipts",
            ));
        }
        if previous.is_some() && previous != r.caller_id.as_deref() {
            return Ok(validation(
                ValidationState::Invalid,
                "path endpoints are not contiguous",
            ));
        }
        previous = r.target_id.as_deref();
    }
    Ok(validation(
        ValidationState::Current,
        "contiguous exact static path; current bytes must also be validated by caller",
    ))
}
pub fn match_renames(
    before: &CorpusInput,
    after: &CorpusInput,
) -> Result<Vec<ContinuityCandidate>, EvidenceError> {
    if before.checkout_id != after.checkout_id {
        return Err(error(EvidenceErrorCode::InvalidCorpus, "checkout differs"));
    }
    let budget = ResolutionBudget::default();
    validate_continuity_input(before, &budget)?;
    validate_continuity_input(after, &budget)?;
    bounded_json(before, budget.max_corpus_bytes)?;
    bounded_json(after, budget.max_corpus_bytes)?;
    let old: Vec<_> = before.files.iter().flat_map(|f| &f.declarations).collect();
    let new: Vec<_> = after.files.iter().flat_map(|f| &f.declarations).collect();
    limit(
        old.len() + new.len() <= budget.max_records,
        "continuity record limit",
    )?;
    let new_ids: HashSet<_> = new.iter().map(|d| &d.id).collect();
    let old_ids: HashSet<_> = old.iter().map(|d| &d.id).collect();
    let key = |d: &DeclarationEvidence| {
        let normalized = if let Some(rest) = d.signature.strip_prefix(&format!("{} ", d.name)) {
            format!("<name> {rest}")
        } else {
            d.signature
                .replacen(&format!(" {} ", d.name), " <name> ", 1)
        };
        (d.kind.clone(), normalized)
    };
    let mut old_matches: BTreeMap<_, usize> = BTreeMap::new();
    for d in &old {
        *old_matches.entry(key(d)).or_default() += 1;
    }
    let mut new_matches: BTreeMap<_, Vec<_>> = BTreeMap::new();
    for d in &new {
        new_matches.entry(key(d)).or_default().push(d);
    }
    let mut result = Vec::new();
    let mut work = old.len() + new.len();
    limit(work <= budget.max_work, "continuity index work limit")?;
    for a in &old {
        if new_ids.contains(&a.id) {
            continue;
        }
        let candidates = new_matches.get(&key(a)).cloned().unwrap_or_default();
        limit(
            candidates.len() <= budget.max_candidates,
            "continuity candidate limit",
        )?;
        for b in &candidates {
            work += 1;
            limit(work <= budget.max_work, "continuity work limit")?;
            let candidate = ContinuityCandidate {
                before_id: a.id.clone(),
                after_id: b.id.clone(),
                state: if candidates.len() == 1 && old_matches[&key(a)] == 1 && !old_ids.contains(&b.id) {
                    ResolutionState::Inferred
                } else {
                    ResolutionState::Ambiguous
                },
                reason: if a.content_hash==b.content_hash{"signature/content continuity candidate; movement/rename not proven"}else{"signature-shape-only continuity candidate; content differs and rename is not proven"}.into(),
            };
            bounded_json(&candidate, budget.max_output_bytes)?;
            result.push(candidate);
            limit(
                result.len() <= budget.max_records,
                "continuity output record limit",
            )?;
        }
    }
    result.sort_by(|a, b| (&a.before_id, &a.after_id).cmp(&(&b.before_id, &b.after_id)));
    bounded_json(&result, budget.max_output_bytes)?;
    Ok(result)
}

fn validate_continuity_input(
    input: &CorpusInput,
    budget: &ResolutionBudget,
) -> Result<(), EvidenceError> {
    limit(
        input.files.len() + input.unavailable_files.len() <= budget.max_files,
        "continuity corpus file limit",
    )?;
    bounded_json(input, budget.max_corpus_bytes)?;
    let mut paths = HashSet::new();
    let mut records = 0usize;
    for file in &input.files {
        validate_file_evidence(file, &ExtractionBudget::default())?;
        if file.checkout_id != input.checkout_id || !paths.insert(&file.rel_path) {
            return Err(error(
                EvidenceErrorCode::InvalidCorpus,
                "mixed checkout/duplicate continuity path",
            ));
        }
        records = records
            .checked_add(
                file.declarations.len()
                    + file.occurrences.len()
                    + file.scopes.len()
                    + file.bindings.len()
                    + file.imports.len(),
            )
            .ok_or_else(|| {
                error(
                    EvidenceErrorCode::BudgetExceeded,
                    "continuity record overflow",
                )
            })?;
        limit(
            records <= budget.max_records,
            "continuity corpus record limit",
        )?;
    }
    for file in &input.unavailable_files {
        if !valid_path(&file.rel_path) || !paths.insert(&file.rel_path) {
            return Err(error(
                EvidenceErrorCode::InvalidCorpus,
                "invalid unavailable continuity path",
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn file(path: &str, source: &str) -> FileEvidence {
        extract_evidence(
            "checkout-test",
            path,
            source.as_bytes(),
            &ExtractionBudget::default(),
        )
        .unwrap()
    }
    fn corpus(files: Vec<FileEvidence>) -> CorpusInput {
        CorpusInput {
            checkout_id: "checkout-test".into(),
            files,
            unavailable_files: vec![],
        }
    }
    fn call<'a>(e: &'a CorpusEvidence, f: &FileEvidence, name: &str) -> &'a ResolutionEvidence {
        let o = f
            .occurrences
            .iter()
            .find(|o| o.kind == OccurrenceKind::Call && o.symbol_text == name)
            .unwrap();
        e.resolutions
            .iter()
            .find(|r| r.occurrence_id == o.id)
            .unwrap()
    }
    #[test]
    fn recursion_and_every_reference_are_witnessed() {
        let f = file(
            "a.py",
            "def recurse(n):\n    return recurse(n) + recurse(n)\n",
        );
        let e = resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
        let calls: Vec<_> = f
            .occurrences
            .iter()
            .filter(|o| o.kind == OccurrenceKind::Call)
            .collect();
        assert_eq!(calls.len(), 2);
        assert_ne!(calls[0].id, calls[1].id);
        for o in calls {
            let r = e
                .resolutions
                .iter()
                .find(|r| r.occurrence_id == o.id)
                .unwrap();
            assert_eq!(r.state, ResolutionState::Exact);
            assert_eq!(r.target_id, r.caller_id);
        }
        assert!(f
            .occurrences
            .iter()
            .any(|o| o.symbol_text == "n" && o.kind == OccurrenceKind::ValueReference));
    }
    #[test]
    fn python_alias_reexport_namespace_and_unavailable() {
        let dep = file("dep.py", "def target():\n    return 1\n");
        let bridge = file("bridge.py", "from dep import target as exported\n");
        let caller=file("caller.py","from bridge import exported as action\nimport dep as module\ndef caller():\n    action()\n    module.target()\n");
        let input = corpus(vec![caller.clone(), bridge, dep.clone()]);
        let result = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
        for name in ["action", "module.target"] {
            let r = call(&result, &caller, name);
            assert_eq!(r.state, ResolutionState::Exact);
            assert_eq!(
                r.target_id.as_deref(),
                Some(dep.declarations[0].id.as_str())
            );
            assert!(r.dependencies.iter().any(|d| d.rel_path == "dep.py"));
            assert_eq!(
                validate_resolution_evidence(&input, r, &ResolutionBudget::default())
                    .unwrap()
                    .state,
                ValidationState::Current
            );
        }
        let mut failed = input;
        failed.files.retain(|f| f.rel_path != "dep.py");
        failed.unavailable_files.push(UnavailableFile {
            rel_path: "dep.py".into(),
            source_hash: Some(dep.source_hash),
            reason: "acquisition failed".into(),
        });
        let e = resolve_evidence(&failed, &ResolutionBudget::default()).unwrap();
        assert!(!e.complete);
        assert_ne!(call(&e, &caller, "action").state, ResolutionState::Exact);
    }
    #[test]
    fn relative_python_import_uses_package_not_basename() {
        let dep = file("pkg/dep.py", "def target():\n    pass\n");
        let wrong = file("dep.py", "def target():\n    pass\n");
        let caller = file("pkg/main.py", "from .dep import target\ntarget()\n");
        let e = resolve_evidence(
            &corpus(vec![caller.clone(), dep.clone(), wrong]),
            &ResolutionBudget::default(),
        )
        .unwrap();
        assert_eq!(
            call(&e, &caller, "target").target_id.as_deref(),
            Some(dep.declarations[0].id.as_str())
        );
    }
    #[test]
    fn aliases_cycle_and_hop_budget_never_claim_exact() {
        let a = file("a.py", "from b import target\ntarget()\n");
        let b = file("b.py", "from a import target\n");
        let input = corpus(vec![a.clone(), b]);
        let e = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
        let r = call(&e, &a, "target");
        assert_eq!(r.state, ResolutionState::Unsupported);
        assert!(r.witnesses.iter().any(|w| w.kind == "alias_cycle"));
        let mut budget = ResolutionBudget::default();
        budget.max_alias_hops = 0;
        assert_eq!(
            resolve_evidence(&input, &budget).unwrap_err().code,
            EvidenceErrorCode::BudgetExceeded
        );
    }
    #[test]
    fn rust_scopes_traits_generics_and_inline_module() {
        let f=file("src/lib.rs","/// Identity docs\npub fn identity<T>(value: T) -> T { value }\nmod nested { pub fn target() {} }\nfn caller() { nested::target(); identity(1); }\ntrait Thing { fn method(&self); }\nimpl Thing for usize { fn method(&self) {} }\n");
        assert!(f.declarations.iter().any(|d| d.name == "identity"
            && d.signature.contains("< T >")
            && d.documentation.is_some()));
        let methods: Vec<_> = f
            .declarations
            .iter()
            .filter(|d| d.name == "method")
            .collect();
        assert_eq!(methods.len(), 2);
        assert_ne!(methods[0].lexical_parent_id, methods[1].lexical_parent_id);
        let e = resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
        assert_eq!(call(&e, &f, "nested::target").state, ResolutionState::Exact);
    }
    #[test]
    fn typescript_alias_reexport_type_only_and_arrow_signature() {
        let dep = file(
            "dep.ts",
            "export function target(value: string): string { return value; }\n",
        );
        let bridge = file("bridge.ts", "export {target as exported} from './dep';\n");
        let caller=file("main.ts","import {exported as action} from './bridge';\nimport type {target as typed} from './dep';\nconst arrow = (value: string): string => value;\naction('x'); typed('x'); arrow('x');\n");
        assert!(caller
            .declarations
            .iter()
            .any(|d| d.name == "arrow" && d.signature.contains("string")));
        let e = resolve_evidence(
            &corpus(vec![dep, bridge, caller.clone()]),
            &ResolutionBudget::default(),
        )
        .unwrap();
        assert_eq!(call(&e, &caller, "action").state, ResolutionState::Inferred);
        assert_eq!(
            call(&e, &caller, "typed").state,
            ResolutionState::Unsupported
        );
        assert_ne!(call(&e, &caller, "arrow").state, ResolutionState::Exact);
        let tsx = file(
            "view.tsx",
            "export function View(){ return <div>Hello</div>; }\n",
        );
        assert!(tsx.capability.available);
    }
    #[test]
    fn shadows_comprehension_except_and_anonymous_callable() {
        for source in [
            "def target():\n    pass\nvalues = [target() for target in values]\n",
            "def target():\n    pass\ntry:\n    pass\nexcept Exception as target:\n    target()\n",
            "def target():\n    pass\nvalue = lambda target: target()\n",
        ] {
            let f = file("a.py", source);
            let e =
                resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
            assert_ne!(
                call(&e, &f, "target").state,
                ResolutionState::Exact,
                "{source}"
            );
        }
    }
    #[test]
    fn ambiguous_parent_propagates_to_nested_children() {
        let f=file("a.py","def outer():\n    def child():\n        pass\ndef outer():\n    def child():\n        pass\n");
        assert_eq!(f.declarations.len(), 4);
        assert!(f
            .declarations
            .iter()
            .all(|d| d.identity_state == IdentityState::AmbiguousSnapshotLocal));
    }
    #[test]
    fn serde_roundtrip_and_structural_tamper_rejection() {
        let f = file("a.py", "def target():\n    pass\ntarget()\n");
        let encoded = serde_json::to_vec(&f).unwrap();
        let decoded: FileEvidence = serde_json::from_slice(&encoded).unwrap();
        assert_eq!(f, decoded);
        let mut bad = f.clone();
        bad.scopes[0].parent_id = Some(bad.root_scope_id.clone());
        assert!(validate_file_evidence(&bad, &ExtractionBudget::default()).is_err());
        bad = f;
        bad.occurrences[0].source.span.end_byte = u64::MAX;
        assert!(validate_file_evidence(&bad, &ExtractionBudget::default()).is_err());
    }
    #[test]
    fn budgets_fail_closed_during_growth_and_work() {
        let f = file("a.py", "def target():\n    pass\ntarget()\n");
        let mut extraction = ExtractionBudget::default();
        extraction.max_serialized_bytes = 100;
        assert_eq!(
            extract_evidence(
                "checkout-test",
                "a.py",
                b"def target():\n    pass\n",
                &extraction
            )
            .unwrap_err()
            .code,
            EvidenceErrorCode::BudgetExceeded
        );
        let mut budget = ResolutionBudget::default();
        budget.max_work = 1;
        assert_eq!(
            resolve_evidence(&corpus(vec![f.clone()]), &budget)
                .unwrap_err()
                .code,
            EvidenceErrorCode::BudgetExceeded
        );
        budget = ResolutionBudget::default();
        budget.max_output_bytes = 10;
        assert!(resolve_evidence(&corpus(vec![f]), &budget).is_err());
    }
    #[test]
    fn dependency_changes_invalidate_callers_and_predicates() {
        let source = "def first():\n    return second()\ndef second():\n    return first()\n";
        let f = file("a.py", source);
        let input = corpus(vec![f.clone()]);
        let e = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
        let a = call(&e, &f, "second");
        let b = call(&e, &f, "first");
        let path = vec![a.occurrence_id.clone(), b.occurrence_id.clone()];
        assert_eq!(
            validate_path_evidence(&e, &path, &ResolutionBudget::default())
                .unwrap()
                .state,
            ValidationState::Current
        );
        let wrong = vec![a.occurrence_id.clone(), a.occurrence_id.clone()];
        assert_eq!(
            validate_path_evidence(&e, &wrong, &ResolutionBudget::default())
                .unwrap()
                .state,
            ValidationState::Invalid
        );
        let changed = corpus(vec![file("a.py", "def first():\n    return 0\n")]);
        assert_eq!(
            validate_resolution_evidence(&changed, a, &ResolutionBudget::default())
                .unwrap()
                .state,
            ValidationState::Invalid
        );
        assert_eq!(
            affected_files(&input, &changed, &["a.py".into()]).unwrap(),
            vec!["a.py"]
        );
        let mut claim = a.clone();
        claim.witnesses.clear();
        assert_eq!(
            validate_resolution_evidence(&input, &claim, &ResolutionBudget::default())
                .unwrap()
                .state,
            ValidationState::Invalid
        );
    }
    #[test]
    fn moves_are_inferred_continuity_not_same_identity() {
        let before = corpus(vec![file("old.py", "def target():\n    pass\n")]);
        let after = corpus(vec![file("new.py", "def target():\n    pass\n")]);
        assert_ne!(
            before.files[0].declarations[0].id,
            after.files[0].declarations[0].id
        );
        let continuity = match_renames(&before, &after).unwrap();
        assert_eq!(continuity.len(), 1);
        assert_eq!(continuity[0].state, ResolutionState::Inferred);
    }
    #[test]
    fn many_to_one_continuity_is_ambiguous_and_unchanged_uses_identity_index() {
        let source = "def target():\n    pass\n";
        let before = corpus(vec![file("one.py", source), file("two.py", source)]);
        let after = corpus(vec![file("three.py", source)]);
        let candidates = match_renames(&before, &after).unwrap();
        assert_eq!(candidates.len(), 2);
        assert!(candidates
            .iter()
            .all(|c| c.state == ResolutionState::Ambiguous));
        let many = corpus((0..200).map(|i| file(&format!("{i}.py"), source)).collect());
        assert!(match_renames(&many, &many).unwrap().is_empty());
    }
    #[test]
    fn defaults_wildcards_and_cfg_do_not_create_false_exact() {
        let f=file("a.py","from unknown import *\ndef target():\n    pass\nvalue = lambda arg=target(): arg\ntarget()\n");
        assert_eq!(
            f.occurrences
                .iter()
                .filter(|o| o.kind == OccurrenceKind::Call && o.symbol_text == "target")
                .count(),
            2
        );
        let e = resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
        for o in f
            .occurrences
            .iter()
            .filter(|o| o.kind == OccurrenceKind::Call)
        {
            assert_ne!(
                e.resolutions
                    .iter()
                    .find(|r| r.occurrence_id == o.id)
                    .unwrap()
                    .state,
                ResolutionState::Exact
            );
        }
        let rust = file(
            "lib.rs",
            "#[cfg(feature=\"x\")]\nfn target() {}\nfn caller(){ target(); }\n",
        );
        let e =
            resolve_evidence(&corpus(vec![rust.clone()]), &ResolutionBudget::default()).unwrap();
        assert_eq!(call(&e, &rust, "target").state, ResolutionState::Inferred);
    }
    #[test]
    fn path_work_budget_is_enforced() {
        let f = file("a.py", "def target():\n    return target()\n");
        let e = resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
        let mut budget = ResolutionBudget::default();
        budget.max_work = 0;
        assert_eq!(
            validate_path_evidence(&e, &[call(&e, &f, "target").occurrence_id.clone()], &budget)
                .unwrap_err()
                .code,
            EvidenceErrorCode::BudgetExceeded
        );
    }
    #[test]
    fn manifest_comparison_requires_exact_current_whole_file() {
        use sha2::{Digest, Sha256};
        let source = r#"{"dependencies":{"lib":"file:../lib"}}"#;
        let f = file("package.json", source);
        let expected = Sha256::digest(b"file:../lib").iter().map(|b|format!("{b:02x}")).collect();
        let claim = SourceClaim { source:f.scopes[0].source.clone(), predicate:SourcePredicate::ManifestFileDependency { expected_spec_sha256:expected } };
        let result=validate_source_evidence(&f,source.as_bytes(),&claim,&ExtractionBudget::default()).unwrap();
        assert_eq!(result.state,ValidationState::Current);
        assert!(matches!(result.manifest_comparison,Some(ManifestValidation::Match { .. })));
        let stale=validate_source_evidence(&f,b"{}",&claim,&ExtractionBudget::default()).unwrap();
        assert_eq!(stale.state,ValidationState::Stale);
        assert!(stale.manifest_comparison.is_none());
    }
    #[test]
    fn exact_file_and_literal_predicates_validate_byte_and_line_ranges() {
        let source = "# 雪\ndef target():\n    pass\n";
        let f = file("a.py", source);
        let claim = SourceClaim {
            source: f.scopes[0].source.clone(),
            predicate: SourcePredicate::File,
        };
        assert_eq!(
            validate_source_evidence(&f, source.as_bytes(), &claim, &ExtractionBudget::default())
                .unwrap()
                .state,
            ValidationState::Current
        );
        let mut bad = claim;
        bad.source.span.start_byte = 2;
        bad.source.span.start_column = 2;
        assert_ne!(
            validate_source_evidence(&f, source.as_bytes(), &bad, &ExtractionBudget::default())
                .unwrap()
                .state,
            ValidationState::Current
        );
    }
    #[test]
    fn direct_arrow_parameters_reassignment_and_catch_are_not_exact() {
        for source in [
            "function target() {}\nconst arrow = target => target();\n",
            "function target() {}\ntarget = (() => {});\ntarget();\n",
            "function target() {}\ntry {} catch(target) { target(); }\n",
        ] {
            let f = file("a.ts", source);
            let e =
                resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
            assert_ne!(
                call(&e, &f, "target").state,
                ResolutionState::Exact,
                "{source}"
            );
        }
    }
    #[test]
    fn conditional_imports_and_transitive_reexports_are_not_unconditional_exact() {
        let dep = file("dep.py", "def target():\n    pass\n");
        for source in [
            "if enabled:\n    from dep import target\ntarget()\n",
            "try:\n    from dep import target\nexcept ImportError:\n    pass\ntarget()\n",
        ] {
            let f = file("main.py", source);
            let e = resolve_evidence(
                &corpus(vec![f.clone(), dep.clone()]),
                &ResolutionBudget::default(),
            )
            .unwrap();
            let r = call(&e, &f, "target");
            assert_eq!(r.state, ResolutionState::Inferred);
            assert!(r.witnesses.iter().any(|w| w.kind == "conditional_import"));
        }
        let bridge = file("bridge.py", "if enabled:\n    from dep import target\n");
        let caller = file("main.py", "from bridge import target\ntarget()\n");
        let e = resolve_evidence(
            &corpus(vec![caller.clone(), bridge, dep]),
            &ResolutionBudget::default(),
        )
        .unwrap();
        assert_eq!(call(&e, &caller, "target").state, ResolutionState::Inferred);
    }
    #[test]
    fn object_methods_are_properties_not_outer_or_implicit_lexical_bindings() {
        for source in [
            "const object = {target(){}}; target();",
            "const object = {target(){return target();}};",
            "class Box {target(){} field = target();}",
            "const X = class {target(){}}; target();",
            "const X = class Inner {target(){}}; target();",
        ] {
            let f = file("a.ts", source);
            let e =
                resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
            let r = call(&e, &f, "target");
            assert_ne!(r.state, ResolutionState::Exact, "{source}");
            assert!(r.target_id.is_none(), "{source}");
        }
        let f = file(
            "a.ts",
            "function target(){} const object = {target(){return target();}};",
        );
        let outer = f
            .declarations
            .iter()
            .find(|d| d.kind == "function" && d.name == "target")
            .unwrap();
        let e = resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
        assert_eq!(
            call(&e, &f, "target").target_id.as_deref(),
            Some(outer.id.as_str())
        );
    }
    #[test]
    fn explicit_new_constructor_calls_keep_every_source_occurrence() {
        let f = file(
            "a.ts",
            "class Box {}\nfunction caller(){ new Box(); new Box(); }",
        );
        let calls: Vec<_> = f
            .occurrences
            .iter()
            .filter(|o| o.kind == OccurrenceKind::Call && o.symbol_text == "Box")
            .collect();
        assert_eq!(calls.len(), 2);
        assert_ne!(calls[0].id, calls[1].id);
        assert!(calls.iter().all(|o| o.syntax_kind == "new_expression"));
        let e = resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
        for o in calls {
            assert_eq!(
                e.resolutions
                    .iter()
                    .find(|r| r.occurrence_id == o.id)
                    .unwrap()
                    .state,
                ResolutionState::Exact,
                "unique static class binding only, not initializer dispatch"
            );
        }
    }
    #[test]
    fn ts_named_function_expression_shadows_outer_in_named_and_anonymous_paths() {
        for source in [
            "function target(){} const action = function target(){ target(); };",
            "function target(){} consume(function target(){ target(); });",
            "function target(){} const action = function* target(){ target(); };",
        ] {
            let f = file("a.ts", source);
            let outer = f
                .declarations
                .iter()
                .find(|d| d.kind == "function" && d.name == "target")
                .unwrap();
            let e =
                resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
            let r = call(&e, &f, "target");
            assert_ne!(r.target_id.as_deref(), Some(outer.id.as_str()), "{source}");
            assert_ne!(r.state, ResolutionState::Exact);
        }
    }
    #[test]
    fn named_class_expression_self_binding_does_not_select_outer_class() {
        let f = file(
            "a.ts",
            "class Inner {} const X = class Inner {method(){new Inner();}};",
        );
        let outer = f
            .declarations
            .iter()
            .find(|d| d.kind == "class" && d.name == "Inner")
            .unwrap();
        let e = resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
        let r = call(&e, &f, "Inner");
        assert_ne!(r.state, ResolutionState::Exact);
        assert_ne!(r.target_id.as_deref(), Some(outer.id.as_str()));
    }
    #[test]
    fn computed_method_keys_keep_each_call_in_surrounding_scope_not_method_body() {
        let f=file("a.ts","function key(){return 'property';} function surrounding(){ const object = {[key()](){},[key()](){}}; return class {[key()](){}}; } class Global {[key()](){}} const object = {[key()](){}};");
        let surrounding = f
            .declarations
            .iter()
            .find(|d| d.name == "surrounding")
            .unwrap();
        let calls: Vec<_> = f
            .occurrences
            .iter()
            .filter(|o| o.kind == OccurrenceKind::Call && o.symbol_text == "key")
            .collect();
        assert_eq!(calls.len(), 5);
        assert_eq!(calls.iter().map(|o| &o.id).collect::<HashSet<_>>().len(), 5);
        assert_eq!(
            calls
                .iter()
                .filter(|o| o.caller_id.as_deref() == Some(surrounding.id.as_str()))
                .count(),
            3
        );
        assert_eq!(calls.iter().filter(|o| o.caller_id.is_none()).count(), 2);
        assert!(calls.iter().all(|o| !f
            .declarations
            .iter()
            .any(|d| d.kind == "method" && o.caller_id.as_ref() == Some(&d.id))));
    }
    #[test]
    fn ts_var_hoists_shadow_but_let_const_keep_block_scope() {
        for statement in [
            "var target = () => 1;",
            "var target;",
            "var target = 1;",
            "for(var target in {}){}",
            "for(var target of []){}",
            "target = () => 1;",
        ] {
            let source = format!(
                "function target(){{}} function f(){{ if(true){{ {statement} }} target(); }}"
            );
            let f = file("a.ts", &source);
            let outer = f
                .declarations
                .iter()
                .find(|d| d.kind == "function" && d.name == "target")
                .unwrap();
            let e =
                resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
            let r = call(&e, &f, "target");
            assert_ne!(r.target_id.as_deref(), Some(outer.id.as_str()), "{source}");
            assert_ne!(r.state, ResolutionState::Exact);
        }
        for keyword in ["let", "const"] {
            let source=format!("function target(){{}} function f(){{ if(true){{ {keyword} target = () => 1; }} target(); }}");
            let f = file("a.ts", &source);
            let outer = f
                .declarations
                .iter()
                .find(|d| d.kind == "function" && d.name == "target")
                .unwrap();
            let e =
                resolve_evidence(&corpus(vec![f.clone()]), &ResolutionBudget::default()).unwrap();
            assert_eq!(
                call(&e, &f, "target").target_id.as_deref(),
                Some(outer.id.as_str())
            );
        }
    }
    #[test]
    fn occurrence_source_is_the_path_witness_and_tampering_is_rejected() {
        let f = file(
            "source.py",
            "def target(): pass\ndef caller():\n    target()\n    target()\n",
        );
        let input = corpus(vec![f.clone()]);
        let budget = ResolutionBudget::default();
        let current = resolve_evidence(&input, &budget).unwrap();
        let r = call(&current, &f, "target");
        let id = r.occurrence_id.clone();
        assert!(r.witnesses.iter().all(|w| w.kind != "occurrence"));
        assert_eq!(
            validate_path_evidence(&current, &[id.clone()], &budget)
                .unwrap()
                .state,
            ValidationState::Current
        );
        for variant in ["hash", "path", "dependency", "target"] {
            let mut changed = current.clone();
            let claim = changed
                .resolutions
                .iter_mut()
                .find(|r| r.occurrence_id == id)
                .unwrap();
            match variant {
                "hash" => claim.source.source_hash = "0".repeat(64),
                "path" => claim.source.rel_path = "absent.py".into(),
                "dependency" => claim
                    .dependencies
                    .retain(|d| d.rel_path != claim.source.rel_path),
                _ => claim.witnesses.retain(|w| w.kind != "target_declaration"),
            }
            let claimed = claim.clone();
            assert_eq!(
                validate_path_evidence(&changed, &[id.clone()], &budget)
                    .unwrap()
                    .state,
                ValidationState::Invalid,
                "{variant}"
            );
            assert_eq!(
                validate_resolution_evidence(&input, &claimed, &budget)
                    .unwrap()
                    .state,
                ValidationState::Invalid,
                "{variant}"
            );
        }
        // The source-recomputing predicate also rejects a forged target span
        // even when that forged locator retains a valid dependency hash.
        let mut forged = r.clone();
        let witness = forged
            .witnesses
            .iter_mut()
            .find(|w| w.kind == "target_declaration")
            .unwrap();
        witness.source.span.start_byte += 1;
        assert_eq!(
            validate_resolution_evidence(&input, &forged, &budget)
                .unwrap()
                .state,
            ValidationState::Invalid
        );
    }
    #[test]
    fn rust_attribute_wrapper_is_not_a_second_source_occurrence() {
        let f = file(
            "src/grpc.rs",
            "#[tonic::async_trait]\nimpl Transport for Service { fn health() {} }\n",
        );
        let occurrences: Vec<_> = f
            .occurrences
            .iter()
            .filter(|o| o.symbol_text == "tonic::async_trait")
            .collect();
        assert_eq!(occurrences.len(), 1);
        assert_eq!(occurrences[0].syntax_kind, "scoped_identifier");
        let ids: HashSet<_> = f.occurrences.iter().map(|o| &o.id).collect();
        assert_eq!(ids.len(), f.occurrences.len());
    }
    #[test]
    fn bounded_scope_lookup_preserves_repeated_call_semantics_and_tiny_budget_refuses() {
        let mut source = String::new();
        for i in 0..300 {
            source.push_str(&format!("def unrelated_{i}(): pass\n"));
        }
        source.push_str("def target(): pass\ndef caller():\n");
        for _ in 0..300 {
            source.push_str("    target()\n");
        }
        let f = file("real_shape.py", &source);
        let input = corpus(vec![f.clone()]);
        let mut budget = ResolutionBudget::default();
        budget.max_work = 20_000;
        let result = resolve_evidence(&input, &budget).unwrap();
        let target = f.declarations.iter().find(|d| d.name == "target").unwrap();
        let calls: Vec<_> = f
            .occurrences
            .iter()
            .filter(|o| o.kind == OccurrenceKind::Call && o.symbol_text == "target")
            .collect();
        assert_eq!(calls.len(), 300);
        for occurrence in calls {
            let r = result
                .resolutions
                .iter()
                .find(|r| r.occurrence_id == occurrence.id)
                .unwrap();
            assert_eq!(r.state, ResolutionState::Exact);
            assert_eq!(r.target_id.as_ref(), Some(&target.id));
            // A simple full scan is an independent lookup oracle, not the index.
            let binding = f
                .bindings
                .iter()
                .find(|b| b.scope_id == f.root_scope_id && b.name == "target")
                .unwrap();
            assert_eq!(binding.declaration_id.as_ref(), r.target_id.as_ref());
        }
        budget.max_work = 1;
        assert_eq!(
            resolve_evidence(&input, &budget).unwrap_err().code,
            EvidenceErrorCode::BudgetExceeded
        );
    }
    #[test]
    fn rename_candidates_remain_inferred_and_rust_aliases_are_not_loader_exact() {
        let before = corpus(vec![file("a.py", "def old():\n    pass\n")]);
        let after = corpus(vec![file("a.py", "def renamed():\n    pass\n")]);
        let candidates = match_renames(&before, &after).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].state, ResolutionState::Inferred);
        let root = file(
            "src/lib.rs",
            "mod dep;\nuse crate::dep::target as action;\nfn caller(){action();}\n",
        );
        let dep = file("src/dep.rs", "pub fn target() {}\n");
        let e = resolve_evidence(
            &corpus(vec![root.clone(), dep.clone()]),
            &ResolutionBudget::default(),
        )
        .unwrap();
        let r = call(&e, &root, "action");
        assert_eq!(r.state, ResolutionState::Inferred);
        assert_eq!(
            r.target_id.as_deref(),
            Some(dep.declarations[0].id.as_str())
        );
    }
    #[test]
    fn s01_supported_unavailable_sources_retain_the_finite_file_limit() {
        let mut input = CorpusInput { checkout_id: "checkout-test".into(), files: vec![], unavailable_files: (0..10_000).map(|i| UnavailableFile { rel_path: format!("source{i}.py"), source_hash: None, reason: "permission_denied".into() }).collect() };
        let accepted = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
        assert!(!accepted.complete);
        assert_eq!(accepted.unavailable_files.len(), 10_000);
        input.unavailable_files.push(UnavailableFile { rel_path: "one-too-many.py".into(), source_hash: None, reason: "oversize".into() });
        assert!(resolve_evidence(&input, &ResolutionBudget::default()).is_err());
    }

    #[test]
    fn partition_resolution_matches_full_corpus_for_every_active_file() {
        let cases = vec![
            vec![
                file("dep.py", "def target():\n    return 1\n"),
                file("bridge.py", "from dep import target as exported\n"),
                file("caller.py", "from bridge import exported as action\nimport dep as module\ndef caller():\n    action()\n    module.target()\ndef shadow(action):\n    action()\n"),
            ],
            vec![
                file("dep.py", "def target():\n    pass\n"),
                file("bridge.py", "if enabled:\n    from dep import target as action\n"),
                file("caller.py", "from bridge import action\naction()\n"),
            ],
            vec![
                file("dep.py", "def target():\n    pass\n"),
                file("dep/__init__.py", "def target():\n    pass\n"),
                file("caller.py", "from dep import target\ntarget()\n"),
            ],
            vec![
                file("dep.py", "def target():\n    pass\ntarget = external\n"),
                file("caller.py", "from dep import target\ntarget()\n"),
            ],
            vec![
                file("a.py", "from b import target\ntarget()\n"),
                file("b.py", "from a import target\n"),
            ],
            vec![
                file("src/lib.rs", "mod dep;\nuse crate::dep::target as action;\nfn caller(){ action(); }\nmod nested { pub fn target(){} }\nfn local(){nested::target();}\n"),
                file("src/dep.rs", "pub fn target(){}\nimpl Thing { fn method(&self) {} }\n"),
            ],
            vec![
                file("dep.ts", "export function target(value: string): string { return value; }\n"),
                file("bridge.ts", "export { target as action } from './dep';\n"),
                file("main.ts", "import { action } from './bridge';\nimport type {target as typed} from './dep';\naction('x'); typed('x');\n"),
            ],
        ];
        for files in cases {
            let mut input = corpus(files);
            input.unavailable_files.push(UnavailableFile {
                rel_path: "unavailable.py".into(), source_hash: None, reason: "syntax_error".into(),
            });
            let full = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
            let interfaces = CorpusInput {
                checkout_id: input.checkout_id.clone(),
                files: input.files.iter().map(storage::module_interface).collect(),
                unavailable_files: input.unavailable_files.clone(),
            };
            let prepared = PartitionResolver::new(&interfaces).unwrap();
            for active in &input.files {
                let partition = CorpusInput {
                    checkout_id: input.checkout_id.clone(),
                    files: input.files.iter().map(|candidate| {
                        if candidate.rel_path == active.rel_path { candidate.clone() }
                        else { storage::module_interface(candidate) }
                    }).collect(),
                    unavailable_files: input.unavailable_files.clone(),
                };
                let result = resolve_partition(&partition, &active.rel_path).unwrap();
                assert_eq!(prepared.resolve(active).unwrap(), result, "prepared active {}", active.rel_path);
                let expected: Vec<_> = full.resolutions.iter()
                    .filter(|r| r.source.file_id == active.file_id).cloned().collect();
                assert_eq!(result.resolutions, expected, "active {}", active.rel_path);
                assert_eq!(result.dependencies, full.dependencies);
                assert_eq!(result.unavailable_files, full.unavailable_files);
                assert!(!result.complete, "missing source coverage cannot become complete");
            }
        }
    }

    #[test]
    fn partition_rejects_missing_duplicate_foreign_and_rich_interfaces() {
        let active = file("caller.py", "def caller():\n    caller()\n");
        let other = file("other.py", "def other():\n    other()\n");
        let mut input = corpus(vec![active.clone(), storage::module_interface(&other)]);
        assert_eq!(resolve_partition(&input, "missing.py").unwrap_err().code, EvidenceErrorCode::InvalidCorpus);
        input.files.push(active.clone());
        assert_eq!(resolve_partition(&input, "caller.py").unwrap_err().code, EvidenceErrorCode::InvalidCorpus);
        input.files.pop();
        input.files[1] = other.clone();
        assert_eq!(resolve_partition(&input, "caller.py").unwrap_err().code, EvidenceErrorCode::InvalidCorpus);
        input.files[1] = storage::module_interface(&other);
        input.files[1].checkout_id = "foreign".into();
        assert!(resolve_partition(&input, "caller.py").is_err());
        input.files[1] = storage::module_interface(&other);
        input.files[1].declarations[0].source.source_hash = "0".repeat(64);
        assert_eq!(resolve_partition(&input, "caller.py").unwrap_err().code, EvidenceErrorCode::InvalidEvidence);
    }

    #[test]
    fn prepared_rejects_corrupt_rich_foreign_duplicate_and_mismatched_inputs() {
        let active = file("caller.py", "def caller():\n    caller()\n");
        let other = file("other.py", "def other():\n    other()\n");
        let mut input = corpus(vec![storage::module_interface(&active), storage::module_interface(&other)]);
        let prepared = PartitionResolver::new(&input).unwrap();
        assert!(prepared.resolve(&file("missing.py", "pass\n")).is_err());
        assert!(prepared.resolve(&file("caller.py", "def replacement():\n    replacement()\n")).is_err());
        let mut corrupt = active.clone();
        corrupt.declarations[0].signature.push_str("corrupt");
        assert_eq!(prepared.resolve(&corrupt).unwrap_err().code, EvidenceErrorCode::InvalidEvidence);
        // Repeated active calls reuse the same immutable base. Input/source
        // bodies cannot be retained by a previous active-file overlay.
        assert_eq!(prepared.resolve(&active).unwrap(), prepared.resolve(&active).unwrap());
        assert!(prepared.indexes.values().all(|index| index.bindings.values().all(|names|
            names.values().flatten().all(|binding| binding.scope_id == input.files.iter()
                .find(|f| f.file_id == binding.source.file_id).unwrap().root_scope_id))));
        drop(prepared);
        input.files.push(input.files[0].clone());
        assert!(PartitionResolver::new(&input).is_err());
        input.files.pop();
        input.files[0] = active.clone();
        assert!(PartitionResolver::new(&input).is_err());
        input.files[0] = storage::module_interface(&active);
        input.files[0].declarations[0].source.source_hash = "0".repeat(64);
        assert!(PartitionResolver::new(&input).is_err());
        input.files[0] = storage::module_interface(&active);
        input.files[0].checkout_id = "foreign".into();
        assert!(PartitionResolver::new(&input).is_err());
        input.files[0] = storage::module_interface(&active);
        input.unavailable_files.push(UnavailableFile { rel_path:"caller.py".into(),source_hash:None,reason:"syntax_error".into() });
        assert!(PartitionResolver::new(&input).is_err());
    }

    #[test]
    fn partition_work_exhaustion_preserves_witnesses_and_other_files() {
        let active = file("caller.py", "def caller():\n    caller()\n    caller()\n    caller()\n");
        let other = file("other.py", "def other():\n    other()\n");
        let input = corpus(vec![storage::module_interface(&active), storage::module_interface(&other)]);
        let prepared = PartitionResolver::new(&input).unwrap();
        assert!(active.occurrences.len() > 1);

        let mut first = Resolver::new(&ResolutionBudget::default());
        first.add_file(&active).unwrap();
        let witness = first.occurrence(&active, &active.occurrences[0]).unwrap();
        assert!(witness.target_id.is_some());
        let budget = ResolutionBudget { max_work: first.work, ..ResolutionBudget::default() };

        let (rows, trace) = prepared.resolve_rows(&active, true, &budget).unwrap();
        assert_eq!(rows.len(), active.occurrences.len());
        assert_eq!(rows[0], witness);
        assert!(trace.is_none());
        assert!(rows[1..].iter().all(|row| row.state == ResolutionState::Unresolved &&
            row.reason == RESOLUTION_BUDGET_OMISSION && row.target_id.is_none() &&
            row.witnesses.is_empty()));
        let (other_rows, trace) = prepared.resolve_rows_with_trace(&other, true).unwrap();
        assert!(other_rows.iter().any(|row|row.target_id.is_some()));
        assert!(trace.is_some());
    }

    #[test]
    fn partition_candidate_limit_does_not_discard_the_file() {
        let active = file("caller.py", "def x(): pass\ndef x(): pass\ndef y(): pass\nx()\ny()\n");
        let input = corpus(vec![storage::module_interface(&active)]);
        let resolver = PartitionResolver::new(&input).unwrap();
        let budget = ResolutionBudget { max_candidates: 1, ..ResolutionBudget::default() };
        let (rows, trace) = resolver.resolve_rows(&active, true, &budget).unwrap();
        assert!(trace.is_none());
        assert_eq!(rows.len(), active.occurrences.len());
        assert!(rows.iter().any(|r| r.reason == RESOLUTION_LOOKUP_OMISSION && r.target_id.is_none()));
        assert!(rows.iter().any(|r| r.target_id.is_some()));
    }

    #[test]
    fn partition_output_budget_keeps_each_occurrence_and_bounded_witnesses() {
        let active = file("caller.py", "def caller():\n    caller()\n    caller()\n    caller()\n");
        let input = corpus(vec![storage::module_interface(&active)]);
        let resolver = PartitionResolver::new(&input).unwrap();
        let minimum: Vec<_> = active.occurrences.iter()
            .map(|o| unresolved_occurrence(o, RESOLUTION_OUTPUT_OMISSION)).collect();
        let full = resolver.resolve_rows_with_trace(&active, true).unwrap().0;
        let bytes = serde_json::to_vec(&minimum).unwrap().len()
            + serde_json::to_vec(&full[0]).unwrap().len()
            - serde_json::to_vec(&minimum[0]).unwrap().len();
        let budget = ResolutionBudget { max_output_bytes: bytes, ..ResolutionBudget::default() };
        let (rows, trace) = resolver.resolve_rows(&active, true, &budget).unwrap();
        assert!(trace.is_none());
        assert_eq!(rows.len(), minimum.len());
        assert!(serde_json::to_vec(&rows).unwrap().len() <= bytes);
        assert!(rows.iter().any(|row| row.target_id.is_some()));
        assert!(rows.iter().any(|row| row.reason == RESOLUTION_OUTPUT_OMISSION && row.target_id.is_none()));
    }

    #[test]
    fn partition_windows_are_independently_bounded_and_legacy_limit_is_unchanged() {
        let active = file("a.py", "pass\n");
        let others: Vec<_> = (0..12).map(|i| storage::module_interface(&file(&format!("dep{i}.py"), "def target():\n    pass\n"))).collect();
        let maximum = others.iter().chain(std::iter::once(&active))
            .map(|f| bounded_json(f, usize::MAX).unwrap()).max().unwrap() + 128;
        let mut input = corpus(vec![active]);
        input.files.extend(others);
        assert_eq!(validate_partition_window(&input, "a.py", maximum, maximum).unwrap_err().code, EvidenceErrorCode::BudgetExceeded);
        let mut big = file("big.py", "pass\n");
        big.diagnostics.push(EvidenceDiagnostic { code:"fixture".into(), message:"x".repeat(9 * 1024 * 1024), source:None });
        let input = corpus(vec![big]);
        assert_eq!(resolve_evidence(&input, &ResolutionBudget::default()).unwrap_err().code, EvidenceErrorCode::BudgetExceeded);
        assert!(resolve_partition(&input, "big.py").is_ok());
        assert_eq!(validate_partition_window(&input, "big.py", 8 * 1024 * 1024, MAX_MODULE_INTERFACE_BYTES).unwrap_err().code, EvidenceErrorCode::BudgetExceeded);
        let mut budget = ResolutionBudget::default();
        budget.max_work = 0;
        let small = corpus(vec![file("a.py", "pass\n")]);
        assert_eq!(resolve_window(&small, &budget, Some("a.py")).unwrap_err().code, EvidenceErrorCode::BudgetExceeded);
    }

    #[test]
    fn aggregate_interface_window_is_measured_and_independent_of_file_and_legacy_limits() {
        assert_eq!(storage::MAX_DECODED,64*1024*1024);
        assert_eq!(MAX_MODULE_INTERFACE_BYTES,512*1024*1024);
        assert_eq!(ResolutionBudget::default().max_corpus_bytes,64*1024*1024);
        // Unavailable metadata belongs to the aggregate window, not an active
        // file. This isolates the real byte boundary without fabricating or
        // retaining a huge source corpus, parser tree or occurrence payload.
        let mut input=corpus(vec![storage::module_interface(&file("a.py","pass\n"))]);
        input.unavailable_files.push(UnavailableFile{rel_path:"failed.py".into(),source_hash:None,reason:"x".repeat(storage::MAX_DECODED+1024)});
        assert!(bounded_json(&input,storage::MAX_DECODED).is_err());
        let resolver=PartitionResolver::new(&input).unwrap();drop(resolver);
        assert!(validate_partition_window(&input,"a.py",storage::MAX_DECODED,MAX_MODULE_INTERFACE_BYTES).is_ok());
        assert_eq!(validate_partition_window(&input,"a.py",storage::MAX_DECODED,storage::MAX_DECODED).unwrap_err().code,EvidenceErrorCode::BudgetExceeded);
    }
    #[test]
    fn indexed_module_paths_preserve_scan_candidates_without_scanning_unrelated_files() {
        let paths = ["pkg/main.py", "pkg/dep.py", "pkg/dep/__init__.py", "dep.py", "pkg/main.ts", "pkg/dep.ts", "pkg/dep/index.ts", "pkg/dep.tsx", "lib.rs", "main.rs", "src/lib.rs", "src/main.rs", "src/dep.rs", "src/dep/mod.rs", "other/lib.rs", "other/dep.rs"];
        let mut files: Vec<_> = paths.iter().map(|path| file(path, "")).collect();
        for i in 0..500 {
            files.push(file(&format!("unrelated/u{i}.py"), "pass\n"));
        }
        let input = corpus(files);
        let budget = ResolutionBudget { max_work: 100, ..ResolutionBudget::default() };
        let mut resolver = Resolver {
            budget: budget.clone(), work:0, growth:0, indexes:HashMap::new(),
            paths:input.files.iter().map(|f| (f.rel_path.as_str(), f)).collect(),
            rust_roots:input.files.iter().filter(|f| f.language == "rust" && matches!(f.rel_path.rsplit('/').next(), Some("lib.rs" | "main.rs"))).collect(),
            base_indexes:None, base_paths:None, base_rust_roots:None, unavailable:&[], unavailable_paths:None, observed_paths:None,
        };
        // Literal path sets are a scan-based independent oracle for all
        // supported module path forms; unrelated files cannot consume work.
        let cases: [(&str, &str, &[&str]); 8] = [
            ("pkg/main.py", ".dep", &["pkg/dep.py", "pkg/dep/__init__.py"]),
            ("pkg/main.py", "dep", &["dep.py"]),
            ("pkg/main.py", "..dep", &["dep.py"]),
            ("pkg/main.ts", "./dep", &["pkg/dep.ts", "pkg/dep.tsx", "pkg/dep/index.ts"]),
            ("pkg/main.ts", "./dep.ts", &["pkg/dep.ts"]),
            ("pkg/main.ts", "external", &[]),
            ("src/lib.rs", "crate::dep", &["other/dep.rs", "src/dep.rs", "src/dep/mod.rs"]),
            ("src/lib.rs", "crate", &["lib.rs", "main.rs"]),
        ];
        for (from, module, expected) in cases {
            let from = resolver.paths[from];
            let result = resolver.module_candidates(from, module).unwrap();
            let expected: Vec<_> = input.files.iter().filter(|file| expected.contains(&file.rel_path.as_str()))
                .map(|file| file.rel_path.as_str()).collect::<BTreeSet<_>>().into_iter().collect();
            assert_eq!(result.iter().map(|file| file.rel_path.as_str()).collect::<Vec<_>>(), expected, "{module}");
        }
        assert!(resolver.work <= 100);
        let tiny = ResolutionBudget { max_candidates:1, ..ResolutionBudget::default() };
        resolver.budget = tiny;
        let from = resolver.paths["pkg/main.py"];
        assert_eq!(resolver.module_candidates(from, ".dep").unwrap_err().code, EvidenceErrorCode::BudgetExceeded);
    }
}
