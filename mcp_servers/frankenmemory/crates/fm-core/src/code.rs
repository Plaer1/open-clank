//! Opt-in code graph (G3, design §3). Tree-sitter extraction of symbols,
//! imports and name-matched calls for Rust / Python / TypeScript, stored as
//! ordinary graph nodes/edges so graph_walk, RWR and groom work unchanged.
//!
//! NOTHING here runs automatically: a codebase enters the graph only through
//! the `code_index` tool. Call edges are name-matched and therefore
//! confidence-marked (trust 0) — dynamic dispatch will produce false/missed
//! edges by design; see .clankers/robonotes/audits/codebase-memory-mcp.md.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
#[cfg(test)]
use std::process::Command;

use serde::Serialize;
use tree_sitter::{Parser, Query, QueryCursor, StreamingIterator};

use crate::graph::{GraphCueInput, GraphEdgeInput, GraphNodeInput, GraphUpsertInput, NodeRef};

/// Noise defaults, overridable by explicit repository ignore negations.
/// Private/security exclusions below remain non-overridable.
const SKIP_DIRS: &[&str] = &[
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".cache",
    "vendor",
];

const MAX_FILE_BYTES: u64 = 512 * 1024;

/// Legacy config/path-hash identity cannot authorize a native registration.
/// Callers must use the opaque admitted checkout identity instead.
pub fn repository_id(root: &Path) -> Result<String, String> {
    let _=root;
    Err("legacy_identity_unavailable: use an admitted opaque registration".into())
}

/// A bounded declared Git-lineage hint, separate from checkout identity.
/// External pointer targets are never opened or treated as read authority.
pub fn git_lineage(root:&Path)->Option<String> {
    git_lineage_pinned(root,&directory_identity(root).ok()?)
}
#[cfg(not(any(unix,windows)))]
pub fn git_lineage_pinned(_root:&Path,_expected:&str)->Option<String>{None}
#[cfg(any(unix,windows))]
pub fn git_lineage_pinned(root:&Path,expected:&str)->Option<String> {
    let marker=root.join(".git");
    let opened=open_pinned_path(root,&marker,expected).ok()?;
    let metadata=opened.metadata().ok()?;
    let declared=if metadata.is_dir(){root.canonicalize().ok()?.join(".git")}else{
        let bytes=acquire_source_pinned(root,&marker,expected).ok()?.bytes;
        let text=std::str::from_utf8(&bytes).ok()?.trim().strip_prefix("gitdir:")?.trim();
        let target=PathBuf::from(text);
        if !target.is_absolute() || target.components().any(|c|matches!(c,std::path::Component::ParentDir)){return None;}
        let worktrees=target.parent()?;
        if worktrees.file_name()?.to_str()!=Some("worktrees"){return None;}
        let common=worktrees.parent()?;
        if common.file_name()?.to_str()!=Some(".git"){return None;}
        common.to_path_buf()
    };
    // This is a declared common-metadata locator hint, never registration
    // authority. Do not open a pointer target outside the admitted root.
    Some(format!("lineage_declared_{}",blake3::hash(declared.as_os_str().as_encoded_bytes()).to_hex()))
}

/// The immutable source facts captured at the start of an index run.  The
/// checkout path is intentionally absent: it is a request boundary detail,
/// not a durable identity.  A missing Git checkout is represented explicitly
/// rather than pretending that a path hash is a commit.
#[derive(Debug, Clone, Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct CodeSourceSnapshot {
    #[serde(default,skip_serializing_if="is_zero")]
    pub evidence_store_version:u32,
    /// Opaque sealed-cache metadata identity; a best-effort corruption guard,
    /// not source authority or a workspace-rule activation requirement.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub evidence_cache_identity: String,
    #[serde(default)]
    pub selection_policy_digest: String,
    pub repository_id: String,
    pub git_commit: Option<String>,
    pub git_ref: Option<String>,
    pub git_dirty: bool,
    #[serde(default)]
    pub git_dirty_known: bool,
    #[serde(default)]
    pub git_metadata_state: String,
    pub file_manifest_digest: String,
}
fn is_zero(value:&u32)->bool{*value==0}


/// Capture a path-free, content-addressed source snapshot for a selected scan
/// surface.  The file manifest is computed from the bytes that will be
/// indexed, so same-size/same-mtime edits cannot reuse an old run identity.
/// Per the code_index per-file failure policy, an unreadable file does not
/// abort the snapshot: it is folded in as an error marker so the digest still
/// moves when the failure set changes, and the run loop reports the file in
/// its errors/coverage instead.
pub fn source_snapshot(root: &Path, files: &[PathBuf]) -> Result<CodeSourceSnapshot, String> {
    source_snapshot_pinned(root,files,&directory_identity(root)?)
}

pub fn source_snapshot_pinned(root:&Path,files:&[PathBuf],expected:&str)->Result<CodeSourceSnapshot,String> {
    let repository_id = "unregistered".to_string();
    let head=acquire_source_pinned(root,&root.join(".git/HEAD"),expected).ok().and_then(|r|String::from_utf8(r.bytes).ok());
    let git_ref=head.as_deref().and_then(|s|s.trim().strip_prefix("ref: refs/heads/")).map(str::to_string);
    let commit=head.as_deref().and_then(|s| {
        if let Some(reference)=s.trim().strip_prefix("ref: ") {
            if reference.split('/').any(|c|c==".." || c.is_empty()){return None;}
            acquire_source_pinned(root,&root.join(".git").join(reference),expected).ok().and_then(|r|String::from_utf8(r.bytes).ok())
        }else{Some(s.trim().into())}
    });
    let git_commit=commit.map(|v|v.trim().to_string()).filter(|v|matches!(v.len(),40|64)&&v.bytes().all(|b|b.is_ascii_hexdigit()));

    let mut entries = Vec::with_capacity(files.len());
    for path in files {
        let rel = path
            .strip_prefix(root)
            .map_err(|_| format!("selected file escaped source root: {}", path.display()))?
            .to_str().ok_or("unsupported non-UTF8 manifest path")?.to_string();
        let digest = match acquire_source_pinned(root,path,expected) {
            Ok(receipt) => blake3::hash(&receipt.bytes).to_hex().to_string(),
            Err(error) => blake3::hash(format!("unreadable: {error}").as_bytes())
                .to_hex()
                .to_string(),
        };
        entries.push((rel, digest));
    }
    entries.sort();
    let mut hasher = blake3::Hasher::new();
    for (rel, digest) in entries {
        hasher.update(rel.as_bytes());
        hasher.update(&[0]);
        hasher.update(digest.as_bytes());
        hasher.update(&[0]);
    }
    Ok(CodeSourceSnapshot {
        evidence_store_version:0,
        evidence_cache_identity: String::new(),
        repository_id,
        git_commit,
        git_ref,
        git_dirty:false,
        git_dirty_known:false,
        git_metadata_state:"bounded_head_only; worktree_status_unavailable".into(),
        file_manifest_digest: hasher.finalize().to_hex().to_string(),
        selection_policy_digest: String::new(),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    Rust,
    Python,
    TypeScript,
    Tsx,
    JavaScript,
}

impl Lang {
    pub fn from_path(path: &Path) -> Option<Self> {
        match path.extension()?.to_str()? {
            "rs" => Some(Self::Rust),
            "py" => Some(Self::Python),
            "ts" | "mts" | "cts" => Some(Self::TypeScript),
            "tsx" => Some(Self::Tsx),
            "js" | "jsx" | "mjs" | "cjs" => Some(Self::JavaScript),
            _ => None,
        }
    }

    fn grammar(&self) -> tree_sitter::Language {
        match self {
            Self::Rust => tree_sitter_rust::LANGUAGE.into(),
            Self::Python => tree_sitter_python::LANGUAGE.into(),
            Self::TypeScript => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
            Self::Tsx => tree_sitter_typescript::LANGUAGE_TSX.into(),
            Self::JavaScript => tree_sitter_javascript::LANGUAGE.into(),
        }
    }

    /// Captures: @def (symbol definitions), @import (module references),
    /// @call (callee identifiers). Deliberately small queries — the goal is
    /// a navigable map, not a compiler.
    fn query_source(&self) -> &'static str {
        match self {
            Self::Rust => {
                r#"
                (function_item name: (identifier) @def)
                (struct_item name: (type_identifier) @def)
                (enum_item name: (type_identifier) @def)
                (trait_item name: (type_identifier) @def)
                (use_declaration argument: (_) @import)
                (call_expression function: (identifier) @call)
                (call_expression function: (field_expression field: (field_identifier) @call))
                (call_expression function: (scoped_identifier name: (identifier) @call))
            "#
            }
            Self::Python => {
                r#"
                (function_definition name: (identifier) @def)
                (class_definition name: (identifier) @def)
                (import_statement name: (dotted_name) @import)
                (import_from_statement module_name: (dotted_name) @import)
                (call function: (identifier) @call)
                (call function: (attribute attribute: (identifier) @call))
            "#
            }
            Self::JavaScript => { r#"
                (function_declaration name: (identifier) @def)
                (class_declaration name: (identifier) @def)
                (method_definition name: (property_identifier) @def)
                (import_statement source: (string (string_fragment) @import))
                (call_expression function: (identifier) @call)
                (call_expression function: (member_expression property: (property_identifier) @call))
            "# },
            Self::TypeScript | Self::Tsx => {
                r#"
                (function_declaration name: (identifier) @def)
                (class_declaration name: (type_identifier) @def)
                (method_definition name: (property_identifier) @def)
                (import_statement source: (string (string_fragment) @import))
                (call_expression function: (identifier) @call)
                (call_expression function: (member_expression property: (property_identifier) @call))
            "#
            }
        }
    }
}

#[derive(Debug, Default)]
pub struct FileExtraction {
    pub defs: Vec<String>,
    pub imports: Vec<String>,
    pub calls: Vec<String>,
}

fn parse_compatibility(lang:Lang,source:&str)->Result<tree_sitter::Tree,String>{
    thread_local!{static PARSERS:std::cell::RefCell<std::collections::HashMap<u8,Parser>>=std::cell::RefCell::new(std::collections::HashMap::new());}
    PARSERS.with(|pool|{let mut pool=pool.borrow_mut();let parser=pool.entry(lang as u8).or_insert_with(Parser::new);parser.set_language(&lang.grammar()).map_err(|e|format!("grammar load failed: {e}"))?;parser.reset();parser.parse(source,None).ok_or_else(||"parse produced no tree".into())})
}
pub fn extract_file(lang: Lang, source: &str) -> Result<FileExtraction, String> {
    let tree=parse_compatibility(lang,source)?;
    if tree.root_node().has_error() {
        return Err("syntax_error: parser recovered ERROR or missing nodes".into());
    }
    let query = Query::new(&lang.grammar(), lang.query_source())
        .map_err(|e| format!("query compile failed: {e}"))?;

    let mut out = FileExtraction::default();
    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source.as_bytes());
    while let Some(m) = matches.next() {
        for cap in m.captures {
            let name = &query.capture_names()[cap.index as usize];
            let text = cap
                .node
                .utf8_text(source.as_bytes())
                .unwrap_or_default()
                .trim()
                .to_string();
            if text.is_empty() {
                continue;
            }
            match *name {
                "def" => out.defs.push(text),
                "import" => out.imports.push(text),
                "call" => out.calls.push(text),
                _ => {}
            }
        }
    }
    out.defs.dedup();
    out.imports.dedup();
    out.calls.dedup();
    Ok(out)
}

#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub(crate) enum CompatibilityReference {Import,Call}
// Same query, trim, per-kind adjacent dedup and imports-before-calls order as
// extract_file + its existing consumer. Borrow source slices; never retain an
// unbounded compatibility Vec<String> ahead of the bounded graph builder.
pub(crate) fn stream_compatibility_references(lang:Lang,source:&str,mut accept:impl FnMut(CompatibilityReference,&str)->Result<(),String>)->Result<(),String>{
    let tree=parse_compatibility(lang,source)?;
    if tree.root_node().has_error(){return Err("syntax_error: parser recovered ERROR or missing nodes".into());}
    let query=Query::new(&lang.grammar(),lang.query_source()).map_err(|e|format!("query compile failed: {e}"))?;
    for (wanted,kind) in [("import",CompatibilityReference::Import),("call",CompatibilityReference::Call)]{
        let mut cursor=QueryCursor::new();let mut matches=cursor.matches(&query,tree.root_node(),source.as_bytes());let mut previous=None;
        while let Some(m)=matches.next(){for cap in m.captures{
            if query.capture_names()[cap.index as usize]!=wanted{continue;}
            let text=cap.node.utf8_text(source.as_bytes()).unwrap_or_default().trim();
            if text.is_empty()||previous==Some(text){continue;}accept(kind,text)?;previous=Some(text);
        }}
    }Ok(())
}

struct GraphOutputBudget{used:usize,limit:usize}
impl GraphOutputBudget{
    fn new(limit:usize)->Result<Self,String>{let value=Self{used:64,limit};if limit<64{return Err("budget_exceeded: code graph output".into());}Ok(value)}
    fn account<T:serde::Serialize>(&mut self,value:&T)->Result<(),String>{
        struct Count{bytes:usize,limit:usize}
        impl std::io::Write for Count{fn write(&mut self,b:&[u8])->std::io::Result<usize>{let next=self.bytes.checked_add(b.len()).filter(|n|*n<=self.limit).ok_or_else(||std::io::Error::other("graph output budget"))?;self.bytes=next;Ok(b.len())}fn flush(&mut self)->std::io::Result<()>{Ok(())}}
        let remaining=self.limit.checked_sub(self.used).ok_or("budget_exceeded: code graph output")?;let mut count=Count{bytes:0,limit:remaining};
        serde_json::to_writer(&mut count,value).map_err(|_|"budget_exceeded: code graph output")?;
        self.used=self.used.checked_add(count.bytes).and_then(|n|n.checked_add(1)).filter(|n|*n<=self.limit).ok_or("budget_exceeded: code graph output")?;Ok(())
    }
}

/// Split an identifier into lowercase search cues:
/// "parseModelSelection" → ["parse", "model", "selection"], snake/kebab too.
pub fn identifier_cues(identifier: &str) -> Vec<String> {
    let mut words: Vec<String> = Vec::new();
    let mut current = String::new();
    for c in identifier.chars() {
        if c == '_' || c == '-' || c == ':' || c == '.' || c == '/' {
            if !current.is_empty() {
                words.push(std::mem::take(&mut current));
            }
        } else if c.is_uppercase()
            && !current.is_empty()
            && current.chars().last().is_some_and(|p| p.is_lowercase())
        {
            words.push(std::mem::take(&mut current));
            current.push(c.to_ascii_lowercase());
        } else {
            current.push(c.to_ascii_lowercase());
        }
    }
    if !current.is_empty() {
        words.push(current);
    }
    words.retain(|w| w.len() > 2);
    words.dedup();
    words
}

pub struct IndexedFile {
    pub evidence: crate::code_evidence::FileEvidence,
    pub rel_path: String,
    pub blake3: String,
    pub mtime_ns: i64,
    pub size: i64,
    pub upsert: GraphUpsertInput,
    pub symbol_count: usize,
}

#[derive(Debug, Clone, Default, Serialize, serde::Deserialize)]
pub struct ScanCoverage {
    pub rel_path: String,
    pub status: String,
    pub detail: Option<String>,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub source_hash:Option<String>,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub source_generation:Option<String>,
}

#[derive(Debug, Clone)]
pub struct ScanPlan {
    pub files: Vec<PathBuf>,
    pub coverage: Vec<ScanCoverage>,
    pub policy: SelectionPolicy,
    /// Control bytes acquired during initial discovery and final validation.
    /// This internal numeric accounting is not source body I/O or a wire field.
    pub policy_bytes_read:usize,
}

// Only repository-local .gitignore bytes are interpreted. No walker, Git
// subprocess, global excludes, or index/tracked-file exception is consulted.
const MAX_IGNORE_FILE_BYTES: usize = 64 * 1024;
const MAX_IGNORE_TOTAL_BYTES: usize = 1024 * 1024;
const MAX_IGNORE_LINES: usize = 20_000;
const MAX_POLICY_DIRECTORIES: usize = 100_000;
const PRIVATE_DIRS: &[&str] = &[".git", ".clanker", ".clankers", ".references", ".ssh", ".aws", ".gnupg"];

#[derive(Debug, Clone, Default)]
pub struct SelectionPolicy {
    // Includes absent files: creating a new nested policy invalidates the run.
    inputs: Vec<(PathBuf, Option<String>)>,
}

#[cfg(any(unix, windows))]
fn ignore_bytes(root: &Path, dir: &Path, expected: &str) -> Result<Option<Vec<u8>>, String> {
    let pinned = open_pinned_path(root, dir, expected)?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe { libc::fstatat(pinned.as_raw_fd(), c".gitignore".as_ptr(), stat.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) } != 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ENOENT) { return Ok(None); }
            return Err(format!("ignore_policy_unavailable: {}: {error}", relative_path(root, dir)));
        }
    }
    #[cfg(windows)]
    if !pinned_directory_names(&pinned)?.iter().any(|name| name == ".gitignore") {
        return Ok(None);
    }
    let source = acquire_bounded_pinned(root, &dir.join(".gitignore"), expected, MAX_IGNORE_FILE_BYTES as u64)
        .map_err(|e| format!("ignore_policy_unavailable: {}: {e}", relative_path(root, dir)))?;
    if source.bytes.len() > MAX_IGNORE_FILE_BYTES {
        return Err("ignore_policy_budget_exceeded: individual .gitignore bytes".into());
    }
    Ok(Some(source.bytes))
}

#[cfg(not(any(unix, windows)))]
fn ignore_bytes(_root: &Path, _dir: &Path, _expected: &str) -> Result<Option<Vec<u8>>, String> {
    Err("pinned_ignore_policy_unavailable_on_platform".into())
}

impl SelectionPolicy {
    pub(crate) fn inputs(&self) -> &[(PathBuf, Option<String>)] { &self.inputs }
    pub fn digest(&self) -> String {
        let mut hasher = blake3::Hasher::new();
        hasher.update(b"repository-ignore-selection-v1\0");
        for (dir, digest) in &self.inputs {
            hasher.update(dir.as_os_str().as_encoded_bytes());
            hasher.update(&[0]);
            hasher.update(digest.as_deref().unwrap_or("absent").as_bytes());
            hasher.update(&[0]);
        }
        hasher.finalize().to_hex().to_string()
    }

    pub fn validate(&self, root: &Path, expected: &str) -> Result<(), String> {
        let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Policy);
        for (relative, digest) in &self.inputs {
            let dir = root.join(relative);
            let current = ignore_bytes(root, &dir, expected)?
                .map(|bytes| blake3::hash(&bytes).to_hex().to_string());
            if &current != digest {
                return Err(format!("ignore_policy_changed: {}: retry with stable repository ignore rules", relative.display()));
            }
        }
        Ok(())
    }

    pub fn validate_source(&self, root: &Path, expected: &str, source: &Path) -> Result<(), String> {
        let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Policy);
        let mut parent = source.parent().ok_or("source has no parent")?;
        loop {
            let relative = parent.strip_prefix(root).map_err(|_| "source escaped policy root")?;
            let index = self.inputs.binary_search_by(|(dir, _)| dir.as_path().cmp(relative))
                .map_err(|_| "source scope was not admitted by selection policy")?;
            let current = ignore_bytes(root, parent, expected)?.map(|bytes| blake3::hash(&bytes).to_hex().to_string());
            if current != self.inputs[index].1 { return Err(format!("ignore_policy_changed: {}: retry with stable repository ignore rules", relative.display())); }
            if parent == root { return Ok(()); }
            parent = parent.parent().ok_or("source escaped policy root")?;
        }
    }
}

/// Coverage is broader than the supported source corpus. In particular an
/// unsupported asset (even a large one) cannot consume a resolver-file slot.
pub fn eligible_unavailable(entry: &ScanCoverage) -> bool {
    Lang::from_path(Path::new(&entry.rel_path)).is_some()
        && matches!(entry.status.as_str(), "parse_failed" | "budget_exceeded" | "permission_denied" | "unsupported" | "binary" | "outside_root" | "oversize" | "oversized")
}

fn relative_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_else(|_| path.to_string_lossy().into_owned())
}

fn unsupported_path_coverage(relative:&Path)->Option<ScanCoverage>{
    relative.to_str().is_none().then(||ScanCoverage{rel_path:format!("unsupported_non_utf8_{}",blake3::hash(relative.as_os_str().as_encoded_bytes()).to_hex()),status:"unsupported".into(),detail:Some("non-UTF8 filename excluded before source identity publication".into()),..Default::default()})
}

/// Every enabled producer depends on these grammar/query/discovery versions.
pub fn producer_fingerprint() -> String {
    static FINGERPRINT:std::sync::OnceLock<String>=std::sync::OnceLock::new();
    FINGERPRINT.get_or_init(||{
    let mut hash=blake3::Hasher::new();
    hash.update(include_str!("code.rs").as_bytes());
    hash.update(include_str!("code_index_limits.rs").as_bytes());
    hash.update(include_str!("graph.rs").as_bytes());
    hash.update(include_str!("store/graph.rs").as_bytes());
    hash.update(include_str!("code_evidence.rs").as_bytes());
    hash.update(include_str!("store/code_evidence.rs").as_bytes());
    hash.update(include_str!("code_storage.rs").as_bytes());
    hash.update(include_str!("store/code_partitioned.rs").as_bytes());
    hash.update(include_str!("store/code_partition_validation.rs").as_bytes());
    hash.update(include_str!("code_query.rs").as_bytes());
    hash.update(include_str!("store/code_query.rs").as_bytes());
    hash.update(include_str!("../Cargo.toml").as_bytes());
    hash.update(&crate::code_evidence::EVIDENCE_FORMAT_VERSION.to_le_bytes());
    hash.update(crate::code_evidence::EXTRACTOR_VERSION.as_bytes());
    hash.update(include_str!("../../../Cargo.lock").as_bytes());
    hash.update(MAX_FILE_BYTES.to_string().as_bytes());
    for excluded in SKIP_DIRS {hash.update(excluded.as_bytes());hash.update(&[0]);}
    hash.finalize().to_hex().to_string()
    });
    let limits=crate::code_index_limits::current();let mut effective=blake3::Hasher::new();
    effective.update(FINGERPRINT.get().unwrap().as_bytes());
    // Storage allowance does not change extracted semantics or invalidate a
    // readable prior head when lowered. Keep the original3 admission fields.
    effective.update(&serde_json::to_vec(&(&limits.max_files,&limits.max_source_bytes,&limits.max_file_bytes)).expect("bounded index limits"));
    effective.finalize().to_hex().to_string()
}

#[derive(Debug,Clone,PartialEq,Eq)]
pub struct CodeWorkTicket {
    pub checkout_id:String,
    pub admission_epoch:i64,
    pub config_fingerprint:String,
    pub policy_generation:i64,
    pub workspace_revision:i64,
}

pub fn directory_identity(root: &Path) -> Result<String,String> {
    #[cfg(windows)] {return windows_pinned::directory_identity(root);}
    #[cfg(not(windows))] {
    let metadata=std::fs::symlink_metadata(root).map_err(|e|e.to_string())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() { return Err("root is not a real directory".into()); }
    #[cfg(unix)] {
        use std::os::unix::fs::MetadataExt;
        Ok(format!("{}:{}",metadata.dev(),metadata.ino()))
    }
    #[cfg(not(unix))] { Err("directory_identity_unavailable".into()) }
    }
}

#[derive(Debug,Clone,serde::Deserialize,Serialize)]
pub struct WorkspaceBinding {
    pub owner: String,
    pub owner_subject_id: String,
    pub workspace_id: String,
    pub location_id: String,
    pub workspace_revision: i64,
    pub policy_generation: i64,
    pub root: PathBuf,
    pub origin: String,
    pub subscriber_id: String,
    #[serde(default)]
    pub authority_expires_unix_ms: Option<i64>,
}

impl WorkspaceBinding {
    pub fn validate_cleanup(&self)->Result<(),String> {
        if self.origin!="app" && self.origin!="agent"{return Err("denied: invalid cleanup purpose".into());}
        let authority=std::env::var("OPEN_CLANK_AUTHORITY_DB_PATH").map_err(|_|"authority_unavailable")?;
        let conn=rusqlite::Connection::open_with_flags(authority,rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(|e|e.to_string())?;
        let count:i64=conn.query_row("SELECT count(*) FROM file_policy_subject_aliases a JOIN file_policy_workspaces w ON w.id=?3 WHERE a.username=?1 AND a.subject_id=?2 AND w.owner_subject_id=a.subject_id AND w.location_id=?4",rusqlite::params![self.owner,self.owner_subject_id,self.workspace_id,self.location_id],|r|r.get(0)).map_err(|e|e.to_string())?;
        if count!=1{return Err("denied: cleanup ownership differs".into());}
        Ok(())
    }
    pub fn publication_guard(&self)->Result<rusqlite::Connection,String> {
        let authority=std::env::var("OPEN_CLANK_AUTHORITY_DB_PATH").map_err(|_|"authority_unavailable")?;
        self.publication_guard_at(Path::new(&authority))
    }
    fn publication_guard_at(&self,authority:&Path)->Result<rusqlite::Connection,String> {
        let conn=rusqlite::Connection::open_with_flags(authority,rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE).map_err(|e|e.to_string())?;
        conn.busy_timeout(std::time::Duration::from_millis(250)).map_err(|e|e.to_string())?;
        conn.execute_batch("BEGIN IMMEDIATE").map_err(|e|format!("authority_busy_retry: {e}"))?;
        // Dropping a connection always rolls back this lock-only transaction.
        self.validate_authority(&conn)?;
        Ok(conn)
    }
    pub fn validate(&self) -> Result<(),String> {
        let authority=std::env::var("OPEN_CLANK_AUTHORITY_DB_PATH").map_err(|_|"authority_unavailable: explicit authority DB required")?;
        let conn=rusqlite::Connection::open_with_flags(authority,rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(|e|format!("authority_unavailable: {e}"))?;
        self.validate_authority(&conn)
    }

    pub fn validate_authority(&self,conn:&rusqlite::Connection)->Result<(),String> {
        let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Admission);
        use rusqlite::params;
        if self.origin != "app" && self.origin != "agent" { return Err("denied: invalid read purpose".into()); }
        let record: (String,String,String,String,i64,i64,i64,String,String,i64)=conn.query_row(
            "SELECT a.subject_id,w.owner_subject_id,w.location_id,l.canonical_path,w.archived,w.revision,m.generation,w.relative_folder,l.capabilities_json,l.enabled FROM file_policy_subject_aliases a JOIN file_policy_workspaces w ON w.id=?2 JOIN file_policy_locations l ON l.id=w.location_id JOIN file_policy_meta m ON m.singleton=1 WHERE a.username=?1 AND l.availability='available'",
            params![self.owner,self.workspace_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?))).map_err(|e|if matches!(e,rusqlite::Error::QueryReturnedNoRows){"denied: authority binding unavailable".into()}else{format!("authority_unavailable: {e}")})?;
        let (subject,workspace_owner,location,canonical,archived,revision,generation,relative,capabilities,enabled)=record;
        if subject!=self.owner_subject_id || workspace_owner!=subject || location!=self.location_id || archived!=0 || revision!=self.workspace_revision || generation!=self.policy_generation || enabled!=1 { return Err("denied: authority changed".into()); }
        let capabilities:serde_json::Value=serde_json::from_str(&capabilities).map_err(|e|e.to_string())?;
        if !capabilities.as_array().is_some_and(|values|values.iter().any(|v|v.as_str()==Some("read"))) { return Err("denied: location is not readable".into()); }
        let admin:i64=conn.query_row("SELECT is_admin FROM file_policy_subject_aliases WHERE username=?1 AND subject_id=?2",params![self.owner,self.owner_subject_id],|r|r.get(0)).map_err(|e|e.to_string())?;
        for class in ["people","agent"] {
            if class=="people" && admin==1 {continue;}
            if class=="agent" && self.origin=="app" {continue;}
            let count:i64=conn.query_row("SELECT count(*) FROM file_policy_bindings b WHERE b.binding_class=?1 AND b.subject_kind='user' AND b.subject_id=?2 AND b.location_id=?3 AND b.status='active' AND (b.expires_unix_ms IS NULL OR b.expires_unix_ms>?5) AND (b.workspace_id IS NULL OR b.workspace_id=?4) AND b.chat_id IS NULL AND (b.lifetime!='once' OR b.remaining_uses>0) AND (b.lifetime!='always' OR b.workspace_id IS NULL) AND EXISTS(SELECT 1 FROM json_each(b.capabilities_json) WHERE value='read')",params![class,self.owner_subject_id,self.location_id,self.workspace_id,chrono::Utc::now().timestamp_millis()],|r|r.get(0)).map_err(|e|e.to_string())?;
            if count==0 { return Err(format!("denied: {class} read grant unavailable or expired")); }
        }
        let location=Path::new(&canonical).canonicalize().map_err(|e|e.to_string())?;
        let root=location.join(relative).canonicalize().map_err(|e|e.to_string())?;
        if !root.starts_with(&location){return Err("denied: workspace escaped authorized location".into());}
        #[cfg(windows)] let same=windows_pinned::authority_path(&self.root)==windows_pinned::authority_path(&root);
        #[cfg(not(windows))] let same=self.root==root;
        if !same { return Err("denied: root binding differs".into()); }
        directory_identity(&self.root)?;
        Ok(())
    }
}

pub struct AcquiredSource {
    pub bytes: Vec<u8>,
    pub metadata: std::fs::Metadata,
}

/// Open each real path component relative to a pinned directory descriptor.
/// O_NOFOLLOW prevents component replacement from escaping admission.
#[cfg(any(unix,windows))]
pub fn acquire_source(root: &Path, path: &Path) -> Result<AcquiredSource, String> {
    let expected=directory_identity(root)?;
    acquire_source_pinned(root,path,&expected)
}

#[cfg(unix)]
#[cfg(test)]
thread_local! { static ABSOLUTE_ROOT_SETUPS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) }; }

#[cfg(unix)]
fn open_pinned_path(root:&Path,path:&Path,expected:&str)->Result<std::fs::File,String> {
    #[cfg(test)] ABSOLUTE_ROOT_SETUPS.with(|count| count.set(count.get() + 1));
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::OpenOptionsExt;
    use libc::{openat,O_NOFOLLOW as NOFOLLOW,O_NONBLOCK,O_CLOEXEC};
    const EXTRA:i32=O_NONBLOCK|O_CLOEXEC;
    let canonical=root.canonicalize().map_err(|e|e.to_string())?;
    let mut directory = std::fs::OpenOptions::new().read(true).custom_flags(NOFOLLOW)
        .open("/").map_err(|e| format!("open filesystem root: {e}"))?;
    for component in canonical.components() {
        let std::path::Component::Normal(name)=component else {continue;};
        let name=std::ffi::CString::new(name.as_bytes()).map_err(|_|"NUL path")?;
        let fd=unsafe{openat(directory.as_raw_fd(),name.as_ptr(),NOFOLLOW|EXTRA)};
        if fd<0{return Err(format!("open root component: {}",std::io::Error::last_os_error()));}
        directory=unsafe{std::fs::File::from_raw_fd(fd)};
        if !directory.metadata().map_err(|e|e.to_string())?.is_dir(){return Err("root component is not directory".into());}
    }
    {
        use std::os::unix::fs::MetadataExt;
        let metadata=directory.metadata().map_err(|e|e.to_string())?;
        if format!("{}:{}",metadata.dev(),metadata.ino())!=expected{return Err("denied: admitted directory replaced".into());}
    }
    let relative = path.strip_prefix(root).map_err(|_| "source escaped root")?;
    let components: Vec<_> = relative.components().collect();
    if components.is_empty() { return Ok(directory); }
    for (i, component) in components.iter().enumerate() {
        let std::path::Component::Normal(name) = component else { return Err("invalid source component".into()); };
        let name = std::ffi::CString::new(name.as_bytes()).map_err(|_| "NUL path")?;
        let fd = unsafe { openat(directory.as_raw_fd(), name.as_ptr(), NOFOLLOW|EXTRA) };
        if fd < 0 { return Err(format!("open pinned component: {}", std::io::Error::last_os_error())); }
        let opened = unsafe { std::fs::File::from_raw_fd(fd) };
        let metadata = opened.metadata().map_err(|e| e.to_string())?;
        if i + 1 == components.len() {
            return Ok(opened);
        }
        if !metadata.is_dir() { return Err("source parent is not a real directory".into()); }
        directory = opened;
    }
    unreachable!()
}

#[cfg(any(unix,windows))]
pub fn acquire_source_pinned(root:&Path,path:&Path,expected:&str)->Result<AcquiredSource,String> {
    acquire_bounded_pinned(root,path,expected,crate::code_index_limits::current().max_file_bytes as u64)
}

#[cfg(any(test, all(target_os = "macos", target_arch = "aarch64")))]
fn scan_kernel_version_supported(bytes: &[u8]) -> bool {
    if bytes.len() > 64 { return false; }
    let Some(version) = bytes.strip_suffix(&[0]) else { return false; };
    let Ok(version) = std::str::from_utf8(version) else { return false; };
    let parts: Vec<_> = version.split('.').collect();
    if !(2..=3).contains(&parts.len()) { return false; }
    let mut numbers = [0u32; 3];
    for (index, part) in parts.iter().enumerate() {
        if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) { return false; }
        let Ok(number) = part.parse::<u32>() else { return false; };
        numbers[index] = number;
    }
    numbers >= [11, 0, 1]
}

#[cfg(any(test, all(target_os = "macos", target_arch = "aarch64")))]
fn scan_kernel_version_response(result: libc::c_int, length: usize, bytes: &[u8; 64]) -> bool {
    result == 0 && length <= bytes.len() && scan_kernel_version_supported(&bytes[..length])
}

#[cfg(test)]
thread_local! { static SCAN_FORCE_COMPONENT_WALKER: std::cell::Cell<bool> = const { std::cell::Cell::new(false) }; }

fn scan_kernel_relative_supported() -> bool {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        static SUPPORTED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
        *SUPPORTED.get_or_init(|| {
            // Read-only capability metadata, never source/authority state.
            // Unknown/older versions keep the existing component walker.
            let mut bytes = [0u8; 64];
            let mut length = bytes.len();
            let result = unsafe { libc::sysctlbyname(c"kern.osproductversion".as_ptr(),
                bytes.as_mut_ptr().cast(), &mut length, std::ptr::null_mut(), 0) };
            scan_kernel_version_response(result, length, &bytes)
        })
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    { false }
}

/// One synchronous inventory's root pin, never an authorization/cache lifetime.
/// Relative components and control bytes are always acquired afresh. Normal
/// aliases resolving to the same admitted inode retain the old canonicalize
/// semantics; a path now resolving elsewhere cannot use the retained pin.
#[cfg(unix)]
struct ScanRoot<'a> {
    root: &'a Path,
    expected: &'a str,
    pin: std::fs::File,
    kernel_relative: bool,
    #[cfg(test)]
    boundaries: std::cell::Cell<usize>,
    #[cfg(test)]
    opens: std::cell::Cell<usize>,
    #[cfg(test)]
    intermediate_stats: std::cell::Cell<usize>,
}

#[cfg(unix)]
impl<'a> ScanRoot<'a> {
    fn new(root: &'a Path, expected: &'a str) -> Result<Self, String> {
        let kernel_relative = scan_kernel_relative_supported();
        #[cfg(test)] let kernel_relative = kernel_relative && !SCAN_FORCE_COMPONENT_WALKER.with(|force| force.get());
        Ok(Self { root, expected, pin: open_pinned_path(root, root, expected)?,
            kernel_relative,
            #[cfg(test)] boundaries: std::cell::Cell::new(0),
            #[cfg(test)] opens: std::cell::Cell::new(0),
            #[cfg(test)] intermediate_stats: std::cell::Cell::new(0) })
    }

    fn validate(&self) -> Result<(), String> {
        use std::os::unix::fs::MetadataExt;
        #[cfg(test)] self.boundaries.set(self.boundaries.get() + 1);
        for metadata in [std::fs::metadata(self.root).map_err(|e| e.to_string())?,
            self.pin.metadata().map_err(|e| e.to_string())?] {
            if !metadata.is_dir() || format!("{}:{}", metadata.dev(), metadata.ino()) != self.expected {
                return Err("denied: admitted directory replaced".into());
            }
        }
        Ok(())
    }

    fn open(&self, path: &Path) -> Result<std::fs::File, String> {
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        self.validate()?;
        let relative = path.strip_prefix(self.root).map_err(|_| "source escaped root")?;
        // A dup would share readdir position with the retained root descriptor.
        // Reopen dot for a fresh open-file description, not an absolute walk.
        #[cfg(test)] self.opens.set(self.opens.get() + 1);
        let fd = unsafe { libc::openat(self.pin.as_raw_fd(), c".".as_ptr(),
            libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC) };
        if fd < 0 { return Err(std::io::Error::last_os_error().to_string()); }
        let mut directory = unsafe { std::fs::File::from_raw_fd(fd) };
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        if self.kernel_relative && !relative.as_os_str().is_empty() {
            // Keep full-path preparation out of the unchanged fallback walker.
            // Validate every component before the whole-name kernel operation.
            for component in relative.components() {
                if !matches!(component, std::path::Component::Normal(_)) {
                    return Err("invalid source component".into());
                }
            }
            let relative_name = std::ffi::CString::new(relative.as_os_str().as_bytes()).map_err(|_| "NUL path")?;
            // macOS >=11.0.1 ARM64 has NAMEI_NOFOLLOW_ANY. Never combine
            // O_NOFOLLOW with ANY (EINVAL), nor retry a denial/symlink error.
            // Fresh dot above still enforces root readability. Intermediate
            // parents use normal OS search permission, not extra list opens.
            #[cfg(test)] self.opens.set(self.opens.get() + 1);
            let fd = unsafe { libc::openat(directory.as_raw_fd(), relative_name.as_ptr(),
                libc::O_NOFOLLOW_ANY | libc::O_NONBLOCK | libc::O_CLOEXEC) };
            if fd < 0 { return Err(format!("open pinned component: {}", std::io::Error::last_os_error())); }
            return Ok(unsafe { std::fs::File::from_raw_fd(fd) });
        }
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        let _ = self.kernel_relative;
        let mut components = relative.components().peekable();
        while let Some(component) = components.next() {
            let std::path::Component::Normal(name) = component else { return Err("invalid source component".into()); };
            let name = std::ffi::CString::new(name.as_bytes()).map_err(|_| "NUL path")?;
            #[cfg(test)] self.opens.set(self.opens.get() + 1);
            let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(),
                libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC) };
            if fd < 0 { return Err(format!("open pinned component: {}", std::io::Error::last_os_error())); }
            directory = unsafe { std::fs::File::from_raw_fd(fd) };
            if components.peek().is_some() {
                #[cfg(test)] self.intermediate_stats.set(self.intermediate_stats.get() + 1);
                if !directory.metadata().map_err(|e| e.to_string())?.is_dir() {
                    return Err("source parent is not a real directory".into());
                }
            }
        }
        Ok(directory)
    }

    fn ignore_bytes(&self, dir: &Path) -> Result<Option<Vec<u8>>, String> {
        use std::os::fd::AsRawFd;
        use std::io::Read;
        let pinned = self.open(dir)?;
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe { libc::fstatat(pinned.as_raw_fd(), c".gitignore".as_ptr(), stat.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) } != 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ENOENT) { return Ok(None); }
            return Err(format!("ignore_policy_unavailable: {}: {error}", relative_path(self.root, dir)));
        }
        let mut timing = crate::code_metrics::span(crate::code_metrics::Stage::Read);
        let mut opened = self.open(&dir.join(".gitignore"))
            .map_err(|e| format!("ignore_policy_unavailable: {}: {e}", relative_path(self.root, dir)))?;
        let metadata = opened.metadata().map_err(|e| e.to_string())?;
        if !metadata.is_file() || metadata.len() > MAX_IGNORE_FILE_BYTES as u64 {
            return Err("ignore_policy_unavailable: source is not an admitted bounded regular file".into());
        }
        let mut bytes = Vec::new();
        (&mut opened).take(MAX_IGNORE_FILE_BYTES as u64 + 1).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
        timing.add(1, bytes.len());
        if bytes.len() > MAX_IGNORE_FILE_BYTES { return Err("ignore_policy_unavailable: source grew beyond byte limit".into()); }
        Ok(Some(bytes))
    }

    fn validate_policy(&self, policy: &SelectionPolicy) -> Result<(), String> {
        let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Policy);
        for (relative, digest) in &policy.inputs {
            let current = self.ignore_bytes(&self.root.join(relative))?.map(|bytes| blake3::hash(&bytes).to_hex().to_string());
            if &current != digest { return Err(format!("ignore_policy_changed: {}: retry with stable repository ignore rules", relative.display())); }
        }
        self.validate()
    }
}

#[cfg(any(unix,windows))]
fn acquire_bounded_pinned(root:&Path,path:&Path,expected:&str,limit:u64)->Result<AcquiredSource,String> {
    let mut timing = crate::code_metrics::span(crate::code_metrics::Stage::Read);
    use std::io::Read;
    let mut opened=open_pinned_path(root,path,expected)?;
    let metadata=opened.metadata().map_err(|e|e.to_string())?;
    if !metadata.is_file() || metadata.len()>limit {return Err("source is not an admitted bounded regular file".into());}
    let mut bytes=Vec::new();
    (&mut opened).take(limit+1).read_to_end(&mut bytes).map_err(|e|e.to_string())?;
    timing.add(1, bytes.len());
    if bytes.len() as u64>limit {return Err("source grew beyond byte limit".into());}
    Ok(AcquiredSource{bytes,metadata})
}

#[cfg(not(any(unix,windows)))]
pub fn acquire_source_pinned(_root:&Path,_path:&Path,_expected:&str)->Result<AcquiredSource,String>{Err("pinned_source_acquisition_unavailable_on_platform".into())}

#[cfg(not(any(unix,windows)))]
pub fn acquire_source(_root: &Path, _path: &Path) -> Result<AcquiredSource, String> {
    Err("pinned_source_acquisition_unavailable_on_platform".into())
}

/// Fixed numeric diagnostic probe, not paths, source data or authorization.
#[derive(Debug,Default)]
pub(crate) struct PolicyDiscoveryStats {
    pub entries:usize,
    pub known_non_directories_skipped:usize,
    pub unknown_type_metadata:usize,
    pub directories:usize,
    pub full_scanner_fallback:bool,
}

/// Fresh policy discovery only. Deliberately not connected to query callers
/// until separate review/measurement; the indexing inventory below is intact.
pub(crate) fn scan_selection_policy_pinned(root:&Path,expected:&str)->Result<SelectionPolicy,String>{
    scan_selection_policy_with_stats(root,expected).map(|(policy,_)|policy)
}
pub(crate) fn scan_selection_policy_with_stats(root:&Path,expected:&str)->Result<(SelectionPolicy,PolicyDiscoveryStats),String>{
    #[cfg(any(target_os="macos",target_os="linux"))] {scan_selection_policy_unix(root,expected,false)}
    #[cfg(not(any(target_os="macos",target_os="linux")))] {scan_codebase_pinned(root,expected).map(|scan|(scan.policy,PolicyDiscoveryStats{full_scanner_fallback:true,..Default::default()}))}
}

#[cfg(any(target_os="macos",target_os="linux"))]
#[derive(Debug,PartialEq,Eq)]
struct PolicyDirectoryStamp {dev:u64,ino:u64,mtime:i64,mtime_ns:i64,ctime:i64,ctime_ns:i64}
#[cfg(any(target_os="macos",target_os="linux"))]
#[derive(Debug,Default,PartialEq,Eq)]
struct PolicyDirectoryEntries {count:u64,xor:[u8;32]}
#[cfg(any(target_os="macos",target_os="linux"))]
impl PolicyDirectoryEntries {
    fn add(&mut self,name:&[u8])->Result<(),String>{
        self.count=self.count.checked_add(1).ok_or("incomplete_enumeration: directory entry count overflow")?;
        let mut hash=blake3::Hasher::new();hash.update(b"policy-directory-entry-v1\0");hash.update(name);
        for(a,b)in self.xor.iter_mut().zip(hash.finalize().as_bytes()){*a^=*b;}Ok(())
    }
}
#[cfg(any(target_os="macos",target_os="linux"))]
fn policy_directory_stamp(pin:&std::fs::File)->Result<PolicyDirectoryStamp,String>{
    use std::os::unix::fs::MetadataExt;
    let meta=pin.metadata().map_err(|e|e.to_string())?;
    if !meta.is_dir(){return Err("ignore_policy_changed: directory is no longer real".into());}
    Ok(PolicyDirectoryStamp{dev:meta.dev(),ino:meta.ino(),mtime:meta.mtime(),mtime_ns:meta.mtime_nsec(),ctime:meta.ctime(),ctime_ns:meta.ctime_nsec()})
}
#[cfg(any(target_os="macos",target_os="linux"))]
fn policy_entry_is_directory(pin:&std::fs::File,name:&std::ffi::OsStr,kind:u8,stats:&mut PolicyDiscoveryStats)->Result<bool,String>{
    if kind==libc::DT_DIR {return Ok(true);}
    if kind!=libc::DT_UNKNOWN {stats.known_non_directories_skipped=stats.known_non_directories_skipped.saturating_add(1);return Ok(false);}
    stats.unknown_type_metadata=stats.unknown_type_metadata.saturating_add(1);
    let meta=pinned_entry_metadata(pin,name).map_err(|e|format!("incomplete_enumeration: policy entry metadata unavailable: {e}"))?;
    Ok(meta.directory&&!meta.symlink)
}
#[cfg(any(target_os="macos",target_os="linux"))]
fn policy_directory_entries(pin:&std::fs::File,stats:&mut PolicyDiscoveryStats,force_unknown:bool,max_new_dirs:usize,mut include:impl FnMut(&std::ffi::OsStr)->bool)->Result<(Vec<std::ffi::OsString>,PolicyDirectoryEntries),String>{
    use std::os::fd::{AsRawFd,FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    #[cfg(target_os="macos")] unsafe extern "C" {fn __error()->*mut i32;}
    #[cfg(not(target_os="macos"))] unsafe extern "C" {fn __errno_location()->*mut i32;}
    // Independent open-file offset; no path fallback or shared readdir offset.
    let fd=unsafe{libc::openat(pin.as_raw_fd(),c".".as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
    if fd<0{return Err(std::io::Error::last_os_error().to_string());}
    let dir=unsafe{libc::fdopendir(fd)};
    if dir.is_null(){drop(unsafe{std::fs::File::from_raw_fd(fd)});return Err(std::io::Error::last_os_error().to_string());}
    struct Guard(*mut libc::DIR);impl Drop for Guard{fn drop(&mut self){unsafe{libc::closedir(self.0);}}}
    let guard=Guard(dir);let mut directories=Vec::new();let mut identity=PolicyDirectoryEntries::default();
    loop {
        #[cfg(target_os="macos")] let errno=unsafe{__error()};
        #[cfg(not(target_os="macos"))] let errno=unsafe{__errno_location()};
        unsafe{*errno=0;}let entry=unsafe{libc::readdir(guard.0)};
        if entry.is_null(){if unsafe{*errno}!=0{return Err(format!("incomplete_enumeration: {}",std::io::Error::last_os_error()));}break;}
        let bytes=unsafe{std::ffi::CStr::from_ptr((*entry).d_name.as_ptr())}.to_bytes();
        if bytes==b"."||bytes==b".."{continue;}
        stats.entries=stats.entries.saturating_add(1);
        // The old scanner excludes non-UTF8 identities before metadata too.
        if std::str::from_utf8(bytes).is_err(){continue;}
        let name=std::ffi::OsStr::from_bytes(bytes);
        let kind=if force_unknown{libc::DT_UNKNOWN}else{unsafe{(*entry).d_type}};
        if policy_entry_is_directory(pin,name,kind,stats)? {
            // Order-independent namespace probe, no retained names for skipped
            // private/ignored directories. Unique OS names + count prevent
            // duplicate cancellation; this is not a policy authority digest.
            identity.add(bytes)?;
            if include(name){
                if directories.len()>=max_new_dirs{return Err("ignore_policy_budget_exceeded: directory count".into());}
                directories.push(name.to_owned());
            }
        }
    }
    directories.sort();Ok((directories,identity))
}
#[cfg(test)]
thread_local!{static POLICY_DISCOVERY_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};}
#[cfg(any(target_os="macos",target_os="linux"))]
fn scan_selection_policy_unix(root:&Path,expected:&str,force_unknown:bool)->Result<(SelectionPolicy,PolicyDiscoveryStats),String>{
    let _timing=crate::code_metrics::span(crate::code_metrics::Stage::Inventory);
    let mut policy=SelectionPolicy::default();let mut stats=PolicyDiscoveryStats::default();
    let mut policy_bytes=0usize;let mut policy_lines=0usize;let mut stamps=Vec::new();
    let mut stack=vec![(root.to_path_buf(),Vec::<ignore::gitignore::Gitignore>::new())];
    while let Some((dir,mut matchers))=stack.pop(){
        let pinned=open_pinned_path(root,&dir,expected)?;let stamp=policy_directory_stamp(&pinned)?;
        let bytes=ignore_bytes(root,&dir,expected)?;
        if policy.inputs.len()>=MAX_POLICY_DIRECTORIES{return Err("ignore_policy_budget_exceeded: directory count".into());}
        let relative=dir.strip_prefix(root).map_err(|_|"policy escaped root")?.to_path_buf();
        policy.inputs.push((relative.clone(),bytes.as_ref().map(|b|blake3::hash(b).to_hex().to_string())));stats.directories+=1;
        if let Some(bytes)=bytes {
            policy_bytes+=bytes.len();if policy_bytes>MAX_IGNORE_TOTAL_BYTES{return Err("ignore_policy_budget_exceeded: total bytes".into());}
            let text=std::str::from_utf8(&bytes).map_err(|_|format!("ignore_policy_invalid: {}: expected UTF-8",relative_path(root,&dir)))?;
            let mut builder=ignore::gitignore::GitignoreBuilder::new(&dir);
            for line in text.lines(){policy_lines+=1;if policy_lines>MAX_IGNORE_LINES{return Err("ignore_policy_budget_exceeded: line count".into());}
                builder.add_line(Some(dir.join(".gitignore")),line).map_err(|e|format!("ignore_policy_invalid: {}: {e}",relative_path(root,&dir)))?;}
            matchers.push(builder.build().map_err(|e|format!("ignore_policy_invalid: {e}"))?);
        }
        // Apply exclusion before retaining names/counting queued directories;
        // many ignored/private children cannot consume the visited-dir budget.
        let (entries,entry_identity)=policy_directory_entries(&pinned,&mut stats,force_unknown,MAX_POLICY_DIRECTORIES.saturating_sub(policy.inputs.len()+stack.len()),|name|{
            let name_text=name.to_str().unwrap_or("");let path=dir.join(name);
            let matched=matchers.iter().rev().map(|m|m.matched(&path,true)).find(|m|!m.is_none());
            let ignored=matched.as_ref().is_some_and(|m|m.is_ignore());let excepted=matched.as_ref().is_some_and(|m|m.is_whitelist());
            !(PRIVATE_DIRS.contains(&name_text)||ignored||(!excepted&&(SKIP_DIRS.contains(&name_text)||name_text.starts_with('.'))))
        })?;
        stamps.push((relative,stamp,entry_identity));
        for name in entries {
            let path=dir.join(name);
            if policy.inputs.len()+stack.len()>=MAX_POLICY_DIRECTORIES{return Err("ignore_policy_budget_exceeded: directory count".into());}
            stack.push((path,matchers.clone()));
        }
    }
    #[cfg(test)] {let hook=POLICY_DISCOVERY_TEST_HOOK.with(|slot|slot.borrow_mut().take());if let Some(hook)=hook{hook();}}
    let mut check_directories=||->Result<(),String>{for(relative,stamp,entries)in &stamps{
        let pin=open_pinned_path(root,&root.join(relative),expected)?;
        if &policy_directory_stamp(&pin)?!=stamp{return Err("ignore_policy_changed: directory discovery changed".into());}
        let(_,current)=policy_directory_entries(&pin,&mut stats,force_unknown,0,|_|false)?;
        if &current!=entries||&policy_directory_stamp(&pin)?!=stamp{return Err("ignore_policy_changed: directory entries changed".into());}
    }Ok(())};
    // d_type is only a discovery hint. Detect entry type/rename/addition races
    // via identity/mtime/ctime plus fresh directory-entry identities (also on
    // coarse timestamp filesystems), then revalidate all control bytes.
    // Not an atomic filesystem snapshot; final request fences remain required.
    check_directories()?;policy.inputs.sort_by(|a,b|a.0.cmp(&b.0));policy.validate(root,expected)?;check_directories()?;
    Ok((policy,stats))
}
#[cfg(test)]
#[path="code_policy_discovery_tests.rs"]
mod policy_discovery_tests;

/// Walk a codebase and retain a closed explanation for every encountered
/// file/directory that was not selected. The graph store turns selected rows
/// into indexed/unchanged/parse_failed outcomes after extraction.
struct DiscoveryMetadata{symlink:bool,directory:bool,regular:bool,size:u64}
#[cfg(unix)]
fn pinned_entry_metadata(pinned:&std::fs::File,entry:&std::ffi::OsStr)->Result<DiscoveryMetadata,String>{
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    let name=std::ffi::CString::new(entry.as_bytes()).map_err(|_|"invalid directory entry")?;
    let mut stat=std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe{libc::fstatat(pinned.as_raw_fd(),name.as_ptr(),stat.as_mut_ptr(),libc::AT_SYMLINK_NOFOLLOW)}!=0{return Err(std::io::Error::last_os_error().to_string());}
    let stat=unsafe{stat.assume_init()};let kind=stat.st_mode & libc::S_IFMT;
    Ok(DiscoveryMetadata{symlink:kind==libc::S_IFLNK,directory:kind==libc::S_IFDIR,regular:kind==libc::S_IFREG,size:stat.st_size.max(0) as u64})
}

#[cfg(windows)]
mod windows_pinned {
    use super::*;
    use std::os::windows::{ffi::OsStringExt,fs::{MetadataExt,OpenOptionsExt},io::AsRawHandle};
    use windows_sys::Win32::Storage::FileSystem::*;
    pub struct Pinned{file:std::fs::File,pins:Vec<std::fs::File>,pub path:PathBuf}
    impl std::io::Read for Pinned{fn read(&mut self,buffer:&mut [u8])->std::io::Result<usize>{std::io::Read::read(&mut self.file,buffer)}}
    impl std::ops::Deref for Pinned{type Target=std::fs::File;fn deref(&self)->&Self::Target{&self.file}}
    impl std::ops::DerefMut for Pinned{fn deref_mut(&mut self)->&mut Self::Target{&mut self.file}}
    pub fn authority_path(path:&Path)->PathBuf{
        use std::path::{Component,Prefix};let mut result=PathBuf::new();
        for component in path.components(){match component{
            Component::Prefix(p)=>match p.kind(){
                Prefix::Disk(d)|Prefix::VerbatimDisk(d)=>result.push(format!("{}:",d.to_ascii_uppercase() as char)),
                Prefix::UNC(server,share)|Prefix::VerbatimUNC(server,share)=>{let mut p=std::ffi::OsString::from(r"\\");p.push(server);p.push(r"\");p.push(share);result.push(p);},
                _=>result.push(p.as_os_str())},
            Component::RootDir=>result.push(Path::new(r"\")),_=>result.push(component.as_os_str())}}
        result
    }
    fn identity(file:&std::fs::File)->Result<String,String>{
        let mut info=std::mem::MaybeUninit::<FILE_ID_INFO>::uninit();
        if unsafe{GetFileInformationByHandleEx(file.as_raw_handle(),FileIdInfo,info.as_mut_ptr().cast(),std::mem::size_of::<FILE_ID_INFO>() as u32)}==0{return Err(std::io::Error::last_os_error().to_string());}
        let info=unsafe{info.assume_init()};Ok(format!("{}:{:02x?}",info.VolumeSerialNumber,info.FileId.Identifier))
    }
    fn final_path(file:&std::fs::File)->Result<PathBuf,String>{
        let mut buffer=vec![0u16;32768];let n=unsafe{GetFinalPathNameByHandleW(file.as_raw_handle(),buffer.as_mut_ptr(),buffer.len() as u32,0)};
        if n==0 || n as usize>=buffer.len(){return Err("handle final path unavailable".into());}
        Ok(PathBuf::from(std::ffi::OsString::from_wide(&buffer[..n as usize])))
    }
    fn open(path:&Path)->Result<std::fs::File,String>{
        // Disallow write/delete sharing while all ancestors are held. This
        // prevents a junction/reparse mutation or rename after validation.
        let file=std::fs::OpenOptions::new().read(true).share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT|FILE_FLAG_BACKUP_SEMANTICS).open(path).map_err(|e|e.to_string())?;
        if file.metadata().map_err(|e|e.to_string())?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT!=0{return Err("denied: reparse component".into());}
        if authority_path(&final_path(&file)?)!=authority_path(path){return Err("denied: opened path differs".into());}
        Ok(file)
    }
    pub fn directory_identity(root:&Path)->Result<String,String>{
        let pinned=pin(root,root,None)?;if !pinned.metadata().map_err(|e|e.to_string())?.is_dir(){return Err("root is not directory".into());}identity(&pinned)
    }
    pub fn pin(root:&Path,path:&Path,expected:Option<&str>)->Result<Pinned,String>{
        if !root.is_absolute(){return Err("absolute root required".into());}
        let relative=path.strip_prefix(root).map_err(|_|"source escaped root")?;
        let mut current=PathBuf::new();let mut pins=Vec::new();
        for component in root.components(){
            if matches!(component,std::path::Component::ParentDir|std::path::Component::CurDir){return Err("invalid root component".into());}
            current.push(component.as_os_str());
            if matches!(component,std::path::Component::Prefix(_)){continue;}
            let file=open(&current)?;if !file.metadata().map_err(|e|e.to_string())?.is_dir(){return Err("root ancestor not directory".into());}pins.push(file);
        }
        let root_file=pins.last().ok_or("root handle unavailable")?;
        if let Some(expected)=expected{if identity(root_file)?!=expected{return Err("denied: admitted directory replaced".into());}}
        for component in relative.components(){
            let std::path::Component::Normal(name)=component else{return Err("invalid source component".into());};
            if !pins.last().ok_or("parent unavailable")?.metadata().map_err(|e|e.to_string())?.is_dir(){return Err("source parent not directory".into());}
            current.push(name);pins.push(open(&current)?);
        }
        let file=pins.pop().ok_or("opened handle unavailable")?;Ok(Pinned{file,pins,path:current})
    }
    pub fn names(pinned:&Pinned)->Result<Vec<std::ffi::OsString>,String>{
        let _held=&pinned.pins;
        let mut names=std::fs::read_dir(&pinned.path).map_err(|e|e.to_string())?.map(|e|e.map(|e|e.file_name())).collect::<Result<Vec<_>,_>>().map_err(|e|format!("incomplete_enumeration: {e}"))?;names.sort();Ok(names)
    }
    pub fn metadata(pinned:&Pinned,name:&std::ffi::OsStr)->Result<DiscoveryMetadata,String>{
        let m=std::fs::symlink_metadata(pinned.path.join(name)).map_err(|e|e.to_string())?;
        Ok(DiscoveryMetadata{symlink:m.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT!=0,directory:m.is_dir(),regular:m.is_file(),size:m.len()})
    }
}
#[cfg(windows)]
fn open_pinned_path(root:&Path,path:&Path,expected:&str)->Result<windows_pinned::Pinned,String>{windows_pinned::pin(root,path,Some(expected))}
#[cfg(windows)]
fn pinned_directory_names(pinned:&windows_pinned::Pinned)->Result<Vec<std::ffi::OsString>,String>{windows_pinned::names(pinned)}
#[cfg(windows)]
fn pinned_entry_metadata(pinned:&windows_pinned::Pinned,entry:&std::ffi::OsStr)->Result<DiscoveryMetadata,String>{windows_pinned::metadata(pinned,entry)}

#[cfg(unix)]
fn pinned_directory_names(pinned:&std::fs::File)->Result<Vec<std::ffi::OsString>,String> {
    pinned_directory_names_limited(pinned,usize::MAX,usize::MAX)
}
#[cfg(windows)]
fn pinned_directory_names_limited(_pinned:&windows_pinned::Pinned,_entries:usize,_bytes:usize)->Result<Vec<std::ffi::OsString>,String>{Err("unavailable: bounded visual discovery is not qualified on this platform".into())}
#[cfg(unix)]
fn pinned_directory_names_limited(pinned:&std::fs::File,max_entries:usize,max_bytes:usize)->Result<Vec<std::ffi::OsString>,String> {
    use std::os::fd::{IntoRawFd,FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use libc::{fdopendir,readdir,closedir};
    #[cfg(target_os="macos")]
    unsafe extern "C" {fn __error()->*mut i32;}
    #[cfg(not(target_os="macos"))]
    unsafe extern "C" {fn __errno_location()->*mut i32;}
    let fd=pinned.try_clone().map_err(|e|e.to_string())?.into_raw_fd();
    let dir=unsafe{fdopendir(fd)};
    if dir.is_null(){drop(unsafe{std::fs::File::from_raw_fd(fd)});return Err(std::io::Error::last_os_error().to_string());}
    struct Guard(*mut libc::DIR);
    impl Drop for Guard {fn drop(&mut self){unsafe{closedir(self.0);}}}
    let guard=Guard(dir);
    let mut names=Vec::new();let mut retained=0usize;
    loop {
        #[cfg(target_os="macos")] let errno=unsafe{__error()};
        #[cfg(not(target_os="macos"))] let errno=unsafe{__errno_location()};
        unsafe{*errno=0;}
        let entry=unsafe{readdir(guard.0)};
        if entry.is_null(){if unsafe{*errno}!=0{return Err(format!("incomplete_enumeration: {}",std::io::Error::last_os_error()));}break;}
        let bytes=unsafe{std::ffi::CStr::from_ptr((*entry).d_name.as_ptr())}.to_bytes();
        if bytes!=b"." && bytes!=b".." {
            if names.len()>=max_entries{return Err("budget_exceeded: visual discovery entries".into());}
            retained=retained.checked_add(bytes.len()+64).ok_or("budget_exceeded: visual discovery bytes")?;
            if retained>max_bytes{return Err("budget_exceeded: visual discovery bytes".into());}
            names.push(std::ffi::OsStr::from_bytes(bytes).to_owned());
        }
    }
    names.sort();
    Ok(names)
}

#[cfg(not(any(unix,windows)))]
pub fn scan_codebase_detailed(_root:&Path)->Result<ScanPlan,String>{Err("pinned_discovery_unavailable_on_platform".into())}

#[cfg(any(unix,windows))]
pub fn scan_codebase_detailed(root: &Path) -> Result<ScanPlan, String> {
    scan_codebase_pinned(root,&directory_identity(root)?)
}

#[cfg(not(any(unix,windows)))]
pub fn scan_codebase_pinned(_root:&Path,_expected:&str)->Result<ScanPlan,String>{Err("pinned_discovery_unavailable_on_platform".into())}

#[cfg(any(unix,windows))]
pub fn scan_codebase_pinned(root:&Path,expected:&str)->Result<ScanPlan,String>{
    scan_codebase_inventory(root,expected,None)
}

/// Metadata-only bounded manifest, never an index/admission or source read.
/// The caller must compare two fresh manifests and policy/root authority.
#[cfg(any(unix,windows))]
pub(crate) fn scan_visual_inventory(root:&Path,expected:&str,cancel:&std::sync::atomic::AtomicBool,max_entries:usize)->Result<ScanPlan,String>{
    scan_codebase_inventory(root,expected,Some((cancel,max_entries.min(100_000))))
}
#[cfg(not(any(unix,windows)))]
pub(crate) fn scan_visual_inventory(_root:&Path,_expected:&str,_cancel:&std::sync::atomic::AtomicBool,_max_entries:usize)->Result<ScanPlan,String>{Err("unavailable: bounded visual discovery unsupported".into())}

#[cfg(any(unix,windows))]
fn scan_codebase_inventory(root:&Path,expected:&str,visual:Option<(&std::sync::atomic::AtomicBool,usize)>)->Result<ScanPlan,String>{
    scan_codebase_inventory_mode(root,expected,visual,true)
}

#[cfg(any(unix,windows))]
fn scan_codebase_inventory_mode(root:&Path,expected:&str,visual:Option<(&std::sync::atomic::AtomicBool,usize)>,reuse_root:bool)->Result<ScanPlan,String>{
    let _timing = crate::code_metrics::span(crate::code_metrics::Stage::Inventory);
    #[cfg(unix)]
    let scan_root = if reuse_root { Some(ScanRoot::new(root,expected)?) } else { None };
    #[cfg(windows)] let _ = reuse_root;
    let mut files = Vec::new();
    let mut coverage = Vec::new();
    let mut policy = SelectionPolicy::default();
    let mut policy_bytes = 0usize;
    let mut policy_lines = 0usize;
    let mut stack = vec![(root.to_path_buf(), Vec::<ignore::gitignore::Gitignore>::new())];
    let mut visited=0usize;let mut materialized=0usize;
    while let Some((dir, mut matchers)) = stack.pop() {
        if let Some((cancel,_))=visual {
            if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err("cancelled: visual discovery".into());}
            if policy.inputs.len()>=10_000||dir.strip_prefix(root).map_err(|_|"visual root changed")?.components().count()>64{return Err("budget_exceeded: visual discovery directories/depth".into());}
        }
        #[cfg(unix)] let pinned=if let Some(pin)=&scan_root {pin.open(&dir)?} else {open_pinned_path(root,&dir,expected)?};
        #[cfg(windows)] let pinned=open_pinned_path(root,&dir,expected)?;
        let entries=if let Some((_,maximum))=visual{pinned_directory_names_limited(&pinned,maximum.saturating_sub(visited),16*1024*1024)?}else{pinned_directory_names(&pinned)?};
        visited=visited.checked_add(entries.len()).ok_or("budget_exceeded: discovery entries")?;
        #[cfg(unix)] let bytes=if let Some(pin)=&scan_root {pin.ignore_bytes(&dir)?} else {ignore_bytes(root,&dir,expected)?};
        #[cfg(windows)] let bytes = ignore_bytes(root, &dir, expected)?;
        if policy.inputs.len() >= MAX_POLICY_DIRECTORIES { return Err("ignore_policy_budget_exceeded: directory count".into()); }
        policy.inputs.push((dir.strip_prefix(root).map_err(|_| "policy escaped root")?.to_path_buf(), bytes.as_ref().map(|bytes| blake3::hash(bytes).to_hex().to_string())));
        if let Some(bytes) = bytes {
            policy_bytes += bytes.len();
            if policy_bytes > MAX_IGNORE_TOTAL_BYTES { return Err("ignore_policy_budget_exceeded: total bytes".into()); }
            let text = std::str::from_utf8(&bytes).map_err(|_| format!("ignore_policy_invalid: {}: expected UTF-8", relative_path(root, &dir)))?;
            let mut builder = ignore::gitignore::GitignoreBuilder::new(&dir);
            for line in text.lines() {
                policy_lines += 1;
                if policy_lines > MAX_IGNORE_LINES { return Err("ignore_policy_budget_exceeded: line count".into()); }
                builder.add_line(Some(dir.join(".gitignore")), line)
                    .map_err(|e| format!("ignore_policy_invalid: {}: {e}", relative_path(root, &dir)))?;
            }
            matchers.push(builder.build().map_err(|e| format!("ignore_policy_invalid: {e}"))?);
        }
        for entry in entries {
            if let Some((cancel,_))=visual {
                if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err("cancelled: visual discovery".into());}
                let bytes=dir.as_os_str().as_encoded_bytes().len().checked_add(entry.as_encoded_bytes().len()).ok_or("budget_exceeded: visual path")?;
                if bytes>4096{return Err("budget_exceeded: visual path".into());}
                materialized=materialized.checked_add(bytes.saturating_mul(2)+512).ok_or("budget_exceeded: visual metadata")?;
                if materialized>16*1024*1024||stack.len()+policy.inputs.len()>10_000{return Err("budget_exceeded: visual metadata".into());}
            }
            let path=dir.join(&entry);
            if let Some(entry)=unsupported_path_coverage(path.strip_prefix(root).map_err(|_|"discovery escaped root")?) {
                coverage.push(entry);
                continue;
            }
            let rel = relative_path(root, &path);
            let meta=pinned_entry_metadata(&pinned,&entry).map_err(|e|format!("incomplete_enumeration: metadata unavailable for {rel}: {e}"))?;
            let name = entry.to_str().unwrap_or("");
            let private = PRIVATE_DIRS.contains(&name);
            let matched = matchers.iter().rev().map(|matcher| matcher.matched(&path, meta.directory)).find(|matched| !matched.is_none());
            let ignored = matched.as_ref().is_some_and(|m| m.is_ignore());
            let excepted = matched.as_ref().is_some_and(|m| m.is_whitelist());
            if private || ignored || (meta.directory && !excepted && (SKIP_DIRS.contains(&name) || name.starts_with('.'))) {
                coverage.push(ScanCoverage { rel_path: rel, status: "ignored".into(), detail: Some(if private { "non-overridable private/security exclusion" } else if ignored { "repository .gitignore selection exclusion (including tracked files)" } else { "default noise directory exclusion" }.into()), ..Default::default() });
                continue;
            }
            if meta.symlink {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "outside_root".into(),
                    detail: Some("symlink is never followed".into()),
                    ..Default::default()
                });
                continue;
            }
            if meta.directory {
                stack.push((path, matchers.clone()));
                continue;
            }
            if !meta.regular {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "unsupported".into(),
                    detail: Some("not a regular file".into()),
                    ..Default::default()
                });
                continue;
            }
            if Lang::from_path(&path).is_none() {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "unsupported".into(),
                    detail: Some("language is not enabled".into()),
                    ..Default::default()
                });
                continue;
            }
            if meta.size > crate::code_index_limits::current().max_file_bytes as u64 {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "oversize".into(),
                    detail: Some(format!("{} bytes exceeds {}", meta.size, crate::code_index_limits::current().max_file_bytes)),
                    ..Default::default()
                });
                continue;
            }
            files.push(path);
            coverage.push(ScanCoverage {
                rel_path: rel,
                status: "selected".into(),
                detail: None,
                ..Default::default()
            });
        }
    }
    files.sort();
    coverage.sort_by(|left, right| left.rel_path.cmp(&right.rel_path));
    policy.inputs.sort_by(|left, right| left.0.cmp(&right.0));
    #[cfg(unix)] if let Some(pin)=&scan_root {pin.validate_policy(&policy)?;} else {policy.validate(root,expected)?;}
    #[cfg(windows)] policy.validate(root, expected)?;
    Ok(ScanPlan { files, coverage, policy, policy_bytes_read:policy_bytes*2 })
}

/// Walk a codebase root and produce per-file graph payloads. Pure planning —
/// the store layer decides what actually changed (incremental). Symlinks are
/// intentionally excluded: a repository file must never make the index walk
/// outside the host-authorized root between enumeration and open.
pub fn scan_codebase(root: &Path) -> Result<Vec<PathBuf>, String> {
    Ok(scan_codebase_detailed(root)?.files)
}

/// Build the graph payload for one source file. `codebase` is the opaque
/// checkout namespace returned by admitted registration; FQNs are
/// "checkout_id::rel_path::symbol" so relocation does not leak the root.
pub fn index_file(codebase: &str, root: &Path, path: &Path) -> Result<IndexedFile, String> {
    let acquired = acquire_source(root, path)?;
    index_acquired(codebase, root, path, &acquired)
}

pub fn index_acquired(codebase: &str, root: &Path, path: &Path, acquired: &AcquiredSource) -> Result<IndexedFile, String> {
    index_acquired_budget(codebase,root,path,acquired,&crate::code_evidence::ExtractionBudget::default())
}
pub(crate) fn index_acquired_budget(codebase:&str,root:&Path,path:&Path,acquired:&AcquiredSource,budget:&crate::code_evidence::ExtractionBudget)->Result<IndexedFile,String>{
    index_acquired_graph_budget(codebase,root,path,acquired,budget,crate::code_evidence::storage::MAX_DECODED)
}
pub(crate) fn index_acquired_graph_budget(codebase:&str,root:&Path,path:&Path,acquired:&AcquiredSource,budget:&crate::code_evidence::ExtractionBudget,graph_limit:usize)->Result<IndexedFile,String>{
    if graph_limit>crate::code_evidence::storage::MAX_DECODED{return Err("budget_exceeded: code graph output".into());}
    let mut timing = crate::code_metrics::span(crate::code_metrics::Stage::Extract);
    timing.add(1, acquired.bytes.len());
    let lang = Lang::from_path(path).ok_or("unsupported language")?;
    let source = std::str::from_utf8(&acquired.bytes).map_err(|_| "source is not UTF-8")?;
    if acquired.bytes.contains(&0) { return Err("binary source".into()); }
    let meta = &acquired.metadata;
    let rel = path
        .strip_prefix(root)
        .map_err(|_| "path outside root".to_string())?
        .to_str().ok_or("unsupported non-UTF8 source path")?
        .to_string();

    let evidence = crate::code_evidence::extract_evidence(codebase, &rel, &acquired.bytes, budget).map_err(|error| {
        if error.code == crate::code_evidence::EvidenceErrorCode::BudgetExceeded {format!("budget_exceeded: {error}")} else {error.to_string()}
    })?;
    let mut graph_budget=GraphOutputBudget::new(graph_limit)?;
    let file_fqn = format!("{codebase}::{rel}");
    let file_ref = NodeRef {
        kind: "file".into(),
        name: file_fqn.clone(),
    };

    let root_node=GraphNodeInput {
        kind: "file".into(),
        name: file_fqn.clone(),
        label: Some(format!("{lang:?}").to_lowercase()),
        layer: Some("semantic".into()),
        trust: Some(3),
    };graph_budget.account(&root_node)?;let mut nodes=vec![root_node];
    let mut edges = Vec::new();
    let mut cues = Vec::new();
    let mut seen_cues: HashSet<String> = HashSet::new();

    for declaration in &evidence.declarations {
        let def = &declaration.name;
        let sym_fqn = format!("{file_fqn}::{}", declaration.id);
        let sym_ref = NodeRef {
            kind: "code_symbol".into(),
            name: sym_fqn.clone(),
        };
        let node=GraphNodeInput {
            kind: "code_symbol".into(),
            name: sym_fqn.clone(),
            label: Some(declaration.qualified_name.clone()),
            layer: Some("semantic".into()),
            trust: Some(3),
        };graph_budget.account(&node)?;nodes.push(node);
        let edge=GraphEdgeInput {
            src: file_ref.clone(),
            tag: "defines".into(),
            dst: sym_ref.clone(),
            fact: None,
            trust: Some(3),
        };graph_budget.account(&edge)?;edges.push(edge);
        for cue in identifier_cues(def).into_iter().chain([def.to_lowercase()]) {
            if seen_cues.insert(format!("{cue}->{sym_fqn}")) {
                let cue=GraphCueInput {
                    cue,
                    node: sym_ref.clone(),
                    source: Some("code_index".into()),
                };graph_budget.account(&cue)?;cues.push(cue);
            }
        }
    }

    // Name-matched call edges: file --calls--> bare callee name node. The
    // callee node is namespace-global to the codebase (not per-file) so
    // definitions and call sites of the same name converge; trust 0 marks
    // the low confidence of name matching.
    stream_compatibility_references(lang,source,|kind,text|{
        let import=kind==CompatibilityReference::Import;
        let edge=GraphEdgeInput {
            src: file_ref.clone(),
            tag: if import{"imports"}else{"calls"}.into(),
            dst: NodeRef {
                kind: if import{"module"}else{"callable"}.into(),
                name: format!("{codebase}::{text}"),
            },
            fact: None,
            trust: Some(if import{3}else{0}),
        };graph_budget.account(&edge)?;edges.push(edge);Ok(())
    })?;

    let symbol_count = evidence.declarations.len();
    Ok(IndexedFile {
        evidence,
        rel_path: rel,
        blake3: blake3::hash(source.as_bytes()).to_hex().to_string(),
        mtime_ns: meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_nanos() as i64)
            .unwrap_or(0),
        size: source.len() as i64,
        upsert: GraphUpsertInput { nodes, edges, cues },
        symbol_count,
    })
}

#[cfg(test)]
#[path="code_s04_tests.rs"]mod s04_tests;
#[cfg(all(test,unix))]
#[path="code_scan_root_tests.rs"]mod scan_root_tests;
#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn selection_fixture(name: &str, entries: &[(&str, &str)]) -> PathBuf {
        let root = temp_fixture(name);
        for (path, body) in entries {
            let path = root.join(path);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, body).unwrap();
        }
        root
    }

    #[test]
    #[ignore = "explicit metadata-only inventory; no candidate source bodies are opened"]
    fn s01_metadata_only_selection_inventory() {
        let root = PathBuf::from(std::env::var_os("OPEN_CLANK_S01_METADATA_ROOT").expect("explicit metadata root required")).canonicalize().unwrap();
        let identity = directory_identity(&root).unwrap();
        let scan = scan_codebase_pinned(&root, &identity).unwrap();
        let mut statuses = std::collections::BTreeMap::<String, usize>::new();
        for entry in &scan.coverage { *statuses.entry(entry.status.clone()).or_default() += 1; }
        let bytes: u64 = scan.files.iter().map(|path| std::fs::symlink_metadata(path).unwrap().len()).sum();
        // Reproduce only the former metadata-selection predicate for comparison.
        let mut old_selected = 0usize; let mut old_bytes = 0u64; let mut old_unsupported = 0usize;
        let mut stack = vec![root.clone()];
        while let Some(dir) = stack.pop() {
            let pinned = open_pinned_path(&root, &dir, &identity).unwrap();
            for name in pinned_directory_names(&pinned).unwrap() {
                let path = dir.join(&name); let meta = pinned_entry_metadata(&pinned, &name).unwrap();
                if meta.symlink { continue; }
                if meta.directory { let name = name.to_str().unwrap_or(""); if !SKIP_DIRS.contains(&name) && !name.starts_with('.') { stack.push(path); } continue; }
                if meta.regular && meta.size <= MAX_FILE_BYTES { if Lang::from_path(&path).is_some() { old_selected += 1; old_bytes += meta.size; } else { old_unsupported += 1; } }
            }
        }
        eprintln!("S01_METADATA_ONLY old_selected={old_selected} old_bytes={old_bytes} old_unsupported={old_unsupported} new_selected={} new_bytes={bytes} statuses={statuses:?} policy_digest={}", scan.files.len(), scan.policy.digest());
    }

    #[test]
    fn repository_ignore_order_nested_roots_negation_and_defaults() {
        let root = selection_fixture("ignore-semantics", &[
            (".gitignore", "# comment\n/data/\n*.py\n!keep.py\n!pkg/\n!static/\n!static/js/\n!static/js/editor/\n!static/js/editor/build/\n!.clanker/\n"),
            ("data/private.py", "canary"), ("keep.py", "pass"), ("drop.py", "canary"),
            ("pkg/.gitignore", "!nested.py\n/secret.py\n"), ("pkg/nested.py", "pass"), ("pkg/secret.py", "canary"),
            ("services/hwfit/data/keep.py", "pass"), ("static/js/editor/build/keep.py", "pass"),
            (".clanker/keep.py", "canary"), (".references/keep.py", "canary"), (".git/keep.py", "canary"),
            ("target/keep.py", "canary"),
        ]);
        let scan = scan_codebase_detailed(&root).unwrap();
        let selected: Vec<_> = scan.files.iter().map(|p| relative_path(&root, p)).collect();
        assert_eq!(selected, ["keep.py", "pkg/nested.py", "services/hwfit/data/keep.py", "static/js/editor/build/keep.py"]);
        assert!(scan.coverage.iter().any(|e| e.rel_path=="data" && e.status=="ignored"));
        assert!(!scan.coverage.iter().any(|e| e.rel_path=="data/private.py"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn ignore_policy_mutation_creation_and_removal_rejects_snapshot() {
        let root = selection_fixture("ignore-races", &[("main.py", "pass"), ("pkg/other.py", "pass")]);
        let identity = directory_identity(&root).unwrap();
        let scan = scan_codebase_detailed(&root).unwrap();
        std::fs::write(root.join("pkg/.gitignore"), "other.py\n").unwrap();
        assert!(scan.policy.validate_source(&root, &identity, &root.join("pkg/other.py")).unwrap_err().contains("ignore_policy_changed"));
        let next = scan_codebase_detailed(&root).unwrap();
        assert_ne!(scan.policy.digest(), next.policy.digest());
        std::fs::remove_file(root.join("pkg/.gitignore")).unwrap();
        assert!(next.policy.validate(&root, &identity).is_err());
        std::fs::write(root.join(".gitignore"), "main.py\n").unwrap();
        assert!(scan.policy.validate(&root, &identity).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn invalid_and_oversized_ignore_policy_fails_closed() {
        for (name, bytes) in [("bad-glob", b"[z-a]\n".to_vec()), ("bad-utf8", vec![0xff]), ("large-policy", vec![b'x';MAX_IGNORE_FILE_BYTES+1]), ("many-rules", b"#\n".repeat(MAX_IGNORE_LINES+1))] {
            let root = selection_fixture(name, &[("private.py", "canary")]);
            std::fs::write(root.join(".gitignore"), bytes).unwrap();
            assert!(scan_codebase_detailed(&root).unwrap_err().contains("ignore_policy"), "{name}");
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn unsafe_and_unreadable_ignore_policy_never_follows_external_path() {
        use std::os::unix::{fs::symlink, fs::PermissionsExt};
        let root = selection_fixture("unsafe-policy", &[("main.py", "pass")]);
        let outside = selection_fixture("external-policy", &[("rules", "!main.py\n")]);
        symlink(outside.join("rules"), root.join(".gitignore")).unwrap();
        assert!(scan_codebase_detailed(&root).unwrap_err().contains("ignore_policy_unavailable"));
        std::fs::remove_file(root.join(".gitignore")).unwrap();
        std::fs::create_dir(root.join(".gitignore")).unwrap();
        assert!(scan_codebase_detailed(&root).is_err());
        std::fs::remove_dir(root.join(".gitignore")).unwrap();
        std::fs::write(root.join(".gitignore"), "main.py\n").unwrap();
        std::fs::set_permissions(root.join(".gitignore"), std::fs::Permissions::from_mode(0)).unwrap();
        // The normal unprivileged test lane must reject unreadable policy.
        if unsafe { libc::geteuid() } != 0 { assert!(scan_codebase_detailed(&root).is_err()); }
        std::fs::set_permissions(root.join(".gitignore"), std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::remove_dir_all(root).unwrap(); std::fs::remove_dir_all(outside).unwrap();
    }

    #[test]
    fn language_eligibility_precedes_asset_size_and_unavailability() {
        let root = selection_fixture("asset-eligibility", &[("main.py", "pass"), ("large.pack", ""), ("large.py", "")]);
        for name in ["large.pack", "large.py"] { std::fs::OpenOptions::new().write(true).open(root.join(name)).unwrap().set_len(MAX_FILE_BYTES+1).unwrap(); }
        let scan = scan_codebase_detailed(&root).unwrap();
        let asset = scan.coverage.iter().find(|e| e.rel_path=="large.pack").unwrap();
        let source = scan.coverage.iter().find(|e| e.rel_path=="large.py").unwrap();
        assert_eq!(asset.status, "unsupported"); assert!(!eligible_unavailable(asset));
        assert_eq!(source.status, "oversize"); assert!(eligible_unavailable(source));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn invalid_utf8_names_are_explicitly_excluded_before_identity() {
        use std::os::unix::ffi::OsStringExt;
        let paths=[PathBuf::from(std::ffi::OsString::from_vec(vec![b'x',0xfe,b'.',b'p',b'y'])),PathBuf::from(std::ffi::OsString::from_vec(vec![b'x',0xff,b'.',b'p',b'y']))];
        let entries:Vec<_>=paths.iter().map(|path|unsupported_path_coverage(path).unwrap()).collect();assert_ne!(entries[0].rel_path,entries[1].rel_path);assert!(entries.iter().all(|e|e.status=="unsupported"));
        assert_ne!(unsupported_path_coverage(&Path::new("a").join(&paths[0])).unwrap().rel_path,unsupported_path_coverage(&Path::new("b").join(&paths[0])).unwrap().rel_path);
        #[cfg(not(target_os="macos"))]{let root=temp_fixture("nonutf8");for path in paths{std::fs::write(root.join(path),"def secret(): pass\n").unwrap();}let scan=scan_codebase_detailed(&root).unwrap();assert!(scan.files.is_empty());assert_eq!(scan.coverage.len(),2);std::fs::remove_dir_all(root).unwrap();}
    }

    #[test]
    fn hostile_git_config_never_executes_or_reads_external_metadata(){
        let root=temp_fixture("hostilegit");let outside=temp_fixture("externalgit");
        std::fs::create_dir_all(root.join(".git")).unwrap();
        let canary=root.join("should-not-run");
        std::fs::write(root.join(".git/config"),format!("[core]\n fsmonitor = !touch {}\n",canary.display())).unwrap();
        std::fs::write(root.join(".git/HEAD"),"0123456789abcdef0123456789abcdef01234567\n").unwrap();
        let snapshot=source_snapshot(&root,&[]).unwrap();assert!(snapshot.git_commit.is_some());assert!(!snapshot.git_dirty_known);assert!(!canary.exists());
        std::fs::remove_dir_all(root.join(".git")).unwrap();
        std::fs::write(root.join(".git"),format!("gitdir: {}\n",outside.display())).unwrap();
        std::fs::write(outside.join("config"),"[core]\n fsmonitor = !false\n").unwrap();
        assert!(git_lineage(&root).is_none());assert!(source_snapshot(&root,&[]).unwrap().git_commit.is_none());assert!(!canary.exists());
        std::fs::remove_dir_all(root).unwrap();std::fs::remove_dir_all(outside).unwrap();
    }

    #[test]
    fn canonical_grant_expiry_and_publication_revocation_lock_are_authoritative(){
        let root=temp_fixture("authority-lock").canonicalize().unwrap();let db=root.join("authority.sqlite");
        let conn=rusqlite::Connection::open(&db).unwrap();
        conn.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER);CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);INSERT INTO file_policy_subject_aliases VALUES('alice','subject-a',0);INSERT INTO file_policy_workspaces VALUES('ws','subject-a','loc','',0,1);INSERT INTO file_policy_meta VALUES(1,7);INSERT INTO file_policy_bindings VALUES('people','user','subject-a','loc','active',1,NULL,NULL,'always',NULL,'[\"read\"]');INSERT INTO file_policy_bindings VALUES('people','user','subject-a','loc','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');").unwrap();
        conn.execute("INSERT INTO file_policy_locations VALUES('loc',?1,'[\"read\"]',1,'available')",[root.to_str().unwrap()]).unwrap();
        let binding=WorkspaceBinding{owner:"alice".into(),owner_subject_id:"subject-a".into(),workspace_id:"ws".into(),location_id:"loc".into(),workspace_revision:1,policy_generation:7,root:root.clone(),origin:"app".into(),subscriber_id:"view:test".into(),authority_expires_unix_ms:Some(1)};
        binding.validate_authority(&conn).unwrap(); // alternate durable grant wins, advisory hint cannot deny.
        let guard=binding.publication_guard_at(&db).unwrap();conn.busy_timeout(std::time::Duration::ZERO).unwrap();
        assert!(conn.execute("UPDATE file_policy_meta SET generation=8",[]).is_err());
        drop(guard);conn.execute("UPDATE file_policy_meta SET generation=8",[]).unwrap();assert!(binding.validate_authority(&conn).is_err());
        conn.execute("UPDATE file_policy_meta SET generation=7;",[]).unwrap();conn.execute("UPDATE file_policy_bindings SET expires_unix_ms=1",[]).unwrap();assert!(binding.validate_authority(&conn).unwrap_err().contains("expired"));
        drop(conn);std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn acquired_bytes_survive_edit_and_preserve_backslash_component() {
        let root=temp_fixture("acquired");
        let path=root.join("real\\name.py");
        std::fs::write(&path,"def before(): pass\n").unwrap();
        let receipt=acquire_source(&root,&path).unwrap();
        std::fs::write(&path,"def after_(): pass\n").unwrap();
        let indexed=index_acquired("checkout_fixture",&root,&path,&receipt).unwrap();
        assert_eq!(indexed.rel_path,"real\\name.py");
        assert_eq!(indexed.blake3,blake3::hash(&receipt.bytes).to_hex().to_string());
        let declaration=indexed.evidence.declarations.iter().find(|d|d.name=="before").unwrap();
        assert!(indexed.upsert.nodes.iter().any(|n|n.name.ends_with(&format!("::{}",declaration.id))&&n.label.as_deref()==Some("before")));
        assert!(!indexed.evidence.declarations.iter().any(|d|d.name=="after_"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn parser_loss_is_failure_and_tsx_has_its_actual_grammar() {
        assert!(extract_file(Lang::Python,"def broken(:\n").unwrap_err().contains("syntax_error"));
        assert!(extract_file(Lang::Tsx,"function View(){return <div>Hello</div>}\n").is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn pinned_acquisition_rejects_parent_root_swap_and_fifo_without_waiting() {
        use std::os::unix::fs::symlink;
        let root=temp_fixture("swaps");let outside=temp_fixture("outside");
        std::fs::create_dir(root.join("src")).unwrap();
        std::fs::write(outside.join("secret.py"),"def outside(): pass\n").unwrap();
        let identity=directory_identity(&root).unwrap();
        std::fs::remove_dir(root.join("src")).unwrap();
        symlink(&outside,root.join("src")).unwrap();
        assert!(acquire_source_pinned(&root,&root.join("src/secret.py"),&identity).is_err());
        assert!(scan_codebase_detailed(&root).unwrap().files.is_empty());
        std::fs::remove_file(root.join("src")).unwrap();
        assert!(Command::new("mkfifo").arg(root.join("fifo.py")).status().unwrap().success());
        let start=std::time::Instant::now();
        assert!(acquire_source_pinned(&root,&root.join("fifo.py"),&identity).is_err());
        assert!(start.elapsed()<std::time::Duration::from_secs(2));
        let moved=root.with_extension("moved");std::fs::rename(&root,&moved).unwrap();
        symlink(&outside,&root).unwrap();
        assert!(acquire_source_pinned(&root,&root.join("secret.py"),&identity).is_err());
        std::fs::remove_file(root).unwrap();std::fs::remove_dir_all(moved).unwrap();std::fs::remove_dir_all(outside).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn discovery_names_and_metadata_remain_on_pinned_parent_after_swap(){
        use std::os::unix::fs::symlink;
        let root=temp_fixture("discovery-parent");let outside=temp_fixture("discovery-outside");std::fs::create_dir(root.join("src")).unwrap();std::fs::write(root.join("src/inside.py"),"def inside(): pass\n").unwrap();std::fs::write(outside.join("outside.py"),"def private(): pass\n").unwrap();
        let identity=directory_identity(&root).unwrap();let pinned=open_pinned_path(&root,&root.join("src"),&identity).unwrap();std::fs::rename(root.join("src"),root.join("original")).unwrap();symlink(&outside,root.join("src")).unwrap();
        let names=pinned_directory_names(&pinned).unwrap();assert_eq!(names,vec![std::ffi::OsString::from("inside.py")]);let metadata=pinned_entry_metadata(&pinned,&names[0]).unwrap();assert!(metadata.regular);assert!(!metadata.symlink);assert!(pinned_entry_metadata(&pinned,std::ffi::OsStr::new("outside.py")).is_err());
        std::fs::remove_dir_all(root).unwrap();std::fs::remove_dir_all(outside).unwrap();
    }

    #[test]
    fn rust_extraction_finds_defs_imports_calls() {
        let src = r#"
            use crate::store::sqlite::SqliteStore;
            pub struct GraphWalker { steps: usize }
            pub fn walk_graph(store: &SqliteStore) { expand_node(store); }
            fn expand_node(_s: &SqliteStore) {}
        "#;
        let out = extract_file(Lang::Rust, src).unwrap();
        assert!(out.defs.contains(&"GraphWalker".to_string()));
        assert!(out.defs.contains(&"walk_graph".to_string()));
        assert!(out.imports.iter().any(|i| i.contains("SqliteStore")));
        assert!(out.calls.contains(&"expand_node".to_string()));
    }

    #[test]
    fn python_and_typescript_extract() {
        let py = "import os\nfrom pathlib import Path\nclass Loader:\n    def load_config(self):\n        return parse_file()\n";
        let out = extract_file(Lang::Python, py).unwrap();
        assert!(out.defs.contains(&"Loader".to_string()));
        assert!(out.defs.contains(&"load_config".to_string()));
        assert!(out.calls.contains(&"parse_file".to_string()));

        let ts = "import { thing } from \"./thing\"\nexport function renderPage() { return buildTree() }\n";
        let out = extract_file(Lang::TypeScript, ts).unwrap();
        assert!(out.defs.contains(&"renderPage".to_string()));
        assert!(out.imports.contains(&"./thing".to_string()));
        assert!(out.calls.contains(&"buildTree".to_string()));
    }

    #[test]
    fn identifier_cues_split_cases() {
        assert_eq!(
            identifier_cues("parseModelSelection"),
            vec!["parse", "model", "selection"]
        );
        assert_eq!(
            identifier_cues("graph_edge_decay"),
            vec!["graph", "edge", "decay"]
        );
        assert!(identifier_cues("ab").is_empty(), "short fragments dropped");
    }

    #[test]
    fn legacy_configuration_identity_is_explicitly_unavailable() {
        let first = std::env::temp_dir().join(format!("fm-repo-id-a-{}", std::process::id()));
        let second = std::env::temp_dir().join(format!("fm-repo-id-b-{}", std::process::id()));
        for root in [&first, &second] {
            std::fs::create_dir_all(root.join(".git")).unwrap();
            let mut head = std::fs::File::create(root.join(".git/HEAD")).unwrap();
            head.write_all(b"ref: refs/heads/main\n").unwrap();
            let mut config = std::fs::File::create(root.join(".git/config")).unwrap();
            config
                .write_all(b"[remote \"origin\"]\n\turl=https://example.invalid/repo.git\n")
                .unwrap();
        }
        assert!(repository_id(&first).unwrap_err().contains("legacy_identity_unavailable"));
        assert!(repository_id(&second).unwrap_err().contains("legacy_identity_unavailable"));
        let _ = std::fs::remove_dir_all(first);
        let _ = std::fs::remove_dir_all(second);
    }

    fn temp_fixture(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("fm-repo-id-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn git_repository_identity_is_stable_across_branch_switches() {
        let root = temp_fixture("branch");
        std::fs::create_dir_all(root.join(".git")).unwrap();
        std::fs::write(
            root.join(".git/config"),
            b"[remote \"origin\"]\n\turl=https://example.invalid/repo.git\n",
        )
        .unwrap();
        std::fs::write(root.join(".git/HEAD"), b"ref: refs/heads/main\n").unwrap();
        let on_main = git_lineage(&root).unwrap();

        // A branch switch rewrites HEAD; the repository identity must not
        // move (the branch is carried separately as git_ref).
        std::fs::write(root.join(".git/HEAD"), b"ref: refs/heads/feature\n").unwrap();
        let on_feature = git_lineage(&root).unwrap();
        assert_eq!(on_main, on_feature);

        // A detached HEAD must not move the identity either.
        std::fs::write(
            root.join(".git/HEAD"),
            b"0123456789abcdef0123456789abcdef01234567\n",
        )
        .unwrap();
        assert_eq!(on_main, git_lineage(&root).unwrap());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn git_worktree_identity_matches_the_shared_repository() {
        let main = temp_fixture("worktree-main");
        let worktree = temp_fixture("worktree-linked");
        std::fs::create_dir_all(main.join(".git/worktrees/linked")).unwrap();
        std::fs::write(
            main.join(".git/config"),
            b"[remote \"origin\"]\n\turl=https://example.invalid/repo.git\n",
        )
        .unwrap();
        std::fs::write(main.join(".git/HEAD"), b"ref: refs/heads/main\n").unwrap();
        std::fs::write(main.join(".git/worktrees/linked/commondir"), b"../..\n").unwrap();
        std::fs::write(
            main.join(".git/worktrees/linked/HEAD"),
            b"ref: refs/heads/feature\n",
        )
        .unwrap();
        let gitdir = main.canonicalize().unwrap().join(".git/worktrees/linked");
        std::fs::write(
            worktree.join(".git"),
            format!("gitdir: {}\n", gitdir.display()),
        )
        .unwrap();

        let shared = git_lineage(&main).unwrap();
        let linked = git_lineage(&worktree).unwrap();
        assert!(linked.starts_with("lineage_declared_"));
        assert_eq!(shared, linked, "worktree shares the repository identity");
        let _ = std::fs::remove_dir_all(main);
        let _ = std::fs::remove_dir_all(worktree);
    }
}
