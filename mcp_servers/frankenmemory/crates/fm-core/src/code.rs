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
use std::process::Command;

use serde::Serialize;
use tree_sitter::{Parser, Query, QueryCursor, StreamingIterator};

use crate::graph::{GraphCueInput, GraphEdgeInput, GraphNodeInput, GraphUpsertInput, NodeRef};

/// Directories never worth indexing. v1 deny-list; .gitignore awareness can
/// come later if noise shows up in practice.
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

/// Return an opaque repository identity instead of embedding the absolute
/// checkout path in every node name. A Git worktree's stable metadata is used
/// when present; path hashing is a compatibility fallback for non-VCS source
/// directories and is deliberately marked as such by the caller's status.
///
/// `.git/HEAD` is deliberately excluded: it changes on every branch switch,
/// which would mint a new identity and orphan the old index namespace. The
/// branch is already captured separately as `git_ref` in the source snapshot.
pub fn repository_id(root: &Path) -> Result<String, String> {
    let canonical = root
        .canonicalize()
        .map_err(|e| format!("canonicalize repository root {root:?}: {e}"))?;
    let marker = canonical.join(".git");
    let mut identity = Vec::new();
    if let Ok(meta) = std::fs::symlink_metadata(&marker) {
        if meta.is_dir() {
            hash_git_dir(&mut identity, &marker);
        } else if meta.is_file() {
            // Git worktree: `.git` is a pointer file ("gitdir: <path>"). The
            // pointer targets a worktree-specific admin dir, so hashing it
            // would give each worktree its own identity. Follow the pointer
            // to the worktree gitdir, then its `commondir` to the shared git
            // dir, and hash that shared metadata instead.
            if let Ok(pointer) = std::fs::read(&marker) {
                if let Some(gitdir) = parse_worktree_gitdir(&canonical, &pointer) {
                    hash_git_dir(&mut identity, &shared_git_dir(&gitdir));
                }
            }
        }
    }
    let kind = if identity.is_empty() {
        identity.extend_from_slice(b"path-fallback\0");
        identity.extend_from_slice(canonical.as_os_str().to_string_lossy().as_bytes());
        "path-fallback"
    } else {
        "git"
    };
    Ok(format!(
        "repo_{}_{}",
        kind,
        blake3::hash(&identity).to_hex().as_str()
    ))
}

/// Fold a git dir's branch-independent metadata into the identity. `HEAD` is
/// never read: the checked-out branch is volatile and must not move the
/// repository identity.
fn hash_git_dir(identity: &mut Vec<u8>, git_dir: &Path) {
    for name in ["config", "commondir"] {
        let file = git_dir.join(name);
        if let Ok(bytes) = std::fs::read(file) {
            identity.extend_from_slice(name.as_bytes());
            identity.push(0);
            identity.extend_from_slice(&bytes);
        }
    }
}

/// Resolve a worktree `.git` pointer file ("gitdir: <path>") to the
/// worktree's admin gitdir. Relative targets are resolved against the
/// worktree root.
fn parse_worktree_gitdir(root: &Path, pointer: &[u8]) -> Option<PathBuf> {
    let text = std::str::from_utf8(pointer).ok()?;
    let target = text.trim().strip_prefix("gitdir:")?.trim();
    let path = PathBuf::from(target);
    let resolved = if path.is_absolute() {
        path
    } else {
        root.join(path)
    };
    resolved.canonicalize().ok()
}

/// A worktree gitdir (`<main>/.git/worktrees/<name>`) carries a `commondir`
/// file pointing at the repository's shared git dir. Follow it so every
/// worktree of one repository shares the same identity.
fn shared_git_dir(gitdir: &Path) -> PathBuf {
    if let Ok(commondir) = std::fs::read(gitdir.join("commondir")) {
        let text = String::from_utf8_lossy(&commondir);
        if let Ok(resolved) = gitdir.join(text.trim()).canonicalize() {
            return resolved;
        }
    }
    gitdir.to_path_buf()
}

/// The immutable source facts captured at the start of an index run.  The
/// checkout path is intentionally absent: it is a request boundary detail,
/// not a durable identity.  A missing Git checkout is represented explicitly
/// rather than pretending that a path hash is a commit.
#[derive(Debug, Clone, Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct CodeSourceSnapshot {
    pub repository_id: String,
    pub git_commit: Option<String>,
    pub git_ref: Option<String>,
    pub git_dirty: bool,
    pub file_manifest_digest: String,
}

fn git_value(root: &Path, args: &[&str]) -> Option<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let value = String::from_utf8(output.stdout).ok()?.trim().to_string();
    (!value.is_empty()).then_some(value)
}

/// Capture a path-free, content-addressed source snapshot for a selected scan
/// surface.  The file manifest is computed from the bytes that will be
/// indexed, so same-size/same-mtime edits cannot reuse an old run identity.
/// Per the code_index per-file failure policy, an unreadable file does not
/// abort the snapshot: it is folded in as an error marker so the digest still
/// moves when the failure set changes, and the run loop reports the file in
/// its errors/coverage instead.
pub fn source_snapshot(root: &Path, files: &[PathBuf]) -> Result<CodeSourceSnapshot, String> {
    let repository_id = repository_id(root)?;
    let git_commit = git_value(root, &["rev-parse", "HEAD"])
        .filter(|v| v.len() == 40 && v.as_bytes().iter().all(|byte| byte.is_ascii_hexdigit()));
    let git_ref = git_value(root, &["symbolic-ref", "--short", "HEAD"]);
    let git_dirty = git_value(root, &["status", "--porcelain=v1", "--untracked-files=all"])
        .is_some_and(|value| !value.is_empty());

    let mut entries = Vec::with_capacity(files.len());
    for path in files {
        let rel = path
            .strip_prefix(root)
            .map_err(|_| format!("selected file escaped source root: {}", path.display()))?
            .to_string_lossy()
            .replace('\\', "/");
        let digest = match std::fs::read(path) {
            Ok(bytes) => blake3::hash(&bytes).to_hex().to_string(),
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
        repository_id,
        git_commit,
        git_ref,
        git_dirty,
        file_manifest_digest: hasher.finalize().to_hex().to_string(),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    Rust,
    Python,
    TypeScript,
}

impl Lang {
    pub fn from_path(path: &Path) -> Option<Self> {
        match path.extension()?.to_str()? {
            "rs" => Some(Self::Rust),
            "py" => Some(Self::Python),
            "ts" | "tsx" | "mts" | "cts" => Some(Self::TypeScript),
            _ => None,
        }
    }

    fn grammar(&self) -> tree_sitter::Language {
        match self {
            Self::Rust => tree_sitter_rust::LANGUAGE.into(),
            Self::Python => tree_sitter_python::LANGUAGE.into(),
            Self::TypeScript => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
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
            Self::TypeScript => {
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

pub fn extract_file(lang: Lang, source: &str) -> Result<FileExtraction, String> {
    let mut parser = Parser::new();
    parser
        .set_language(&lang.grammar())
        .map_err(|e| format!("grammar load failed: {e}"))?;
    let tree = parser
        .parse(source, None)
        .ok_or_else(|| "parse produced no tree".to_string())?;
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
    pub rel_path: String,
    pub blake3: String,
    pub mtime_ns: i64,
    pub size: i64,
    pub upsert: GraphUpsertInput,
    pub symbol_count: usize,
}

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct ScanCoverage {
    pub rel_path: String,
    pub status: String,
    pub detail: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ScanPlan {
    pub files: Vec<PathBuf>,
    pub coverage: Vec<ScanCoverage>,
}

fn relative_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .map(|value| value.to_string_lossy().replace('\\', "/"))
        .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"))
}

fn looks_binary(path: &Path) -> bool {
    let Ok(bytes) = std::fs::read(path) else {
        return false;
    };
    bytes.iter().take(8192).any(|byte| *byte == 0)
}

/// Walk a codebase and retain a closed explanation for every encountered
/// file/directory that was not selected. The graph store turns selected rows
/// into indexed/unchanged/parse_failed outcomes after extraction.
pub fn scan_codebase_detailed(root: &Path) -> Result<ScanPlan, String> {
    let root_meta =
        std::fs::symlink_metadata(root).map_err(|e| format!("stat root {root:?}: {e}"))?;
    if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
        return Err("codebase root must be a real directory".into());
    }
    let mut files = Vec::new();
    let mut coverage = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = std::fs::read_dir(&dir).map_err(|e| format!("read_dir {dir:?}: {e}"))?;
        for entry in entries {
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) => {
                    coverage.push(ScanCoverage {
                        rel_path: relative_path(root, &dir),
                        status: "permission_denied".into(),
                        detail: Some(error.to_string()),
                    });
                    continue;
                }
            };
            let path = entry.path();
            let rel = relative_path(root, &path);
            let Ok(meta) = std::fs::symlink_metadata(&path) else {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "permission_denied".into(),
                    detail: Some("metadata unavailable".into()),
                });
                continue;
            };
            if meta.file_type().is_symlink() {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "outside_root".into(),
                    detail: Some("symlink is never followed".into()),
                });
                continue;
            }
            if meta.is_dir() {
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if SKIP_DIRS.contains(&name) || name.starts_with('.') {
                    coverage.push(ScanCoverage {
                        rel_path: rel,
                        status: "ignored".into(),
                        detail: Some("hard-coded directory exclusion".into()),
                    });
                } else {
                    stack.push(path);
                }
                continue;
            }
            if !meta.is_file() {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "unsupported".into(),
                    detail: Some("not a regular file".into()),
                });
                continue;
            }
            if meta.len() > MAX_FILE_BYTES {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "oversize".into(),
                    detail: Some(format!("{} bytes exceeds {}", meta.len(), MAX_FILE_BYTES)),
                });
                continue;
            }
            if Lang::from_path(&path).is_none() {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "unsupported".into(),
                    detail: Some("language is not enabled".into()),
                });
                continue;
            }
            if looks_binary(&path) {
                coverage.push(ScanCoverage {
                    rel_path: rel,
                    status: "binary".into(),
                    detail: Some("NUL byte in bounded probe".into()),
                });
                continue;
            }
            files.push(path);
            coverage.push(ScanCoverage {
                rel_path: rel,
                status: "selected".into(),
                detail: None,
            });
        }
    }
    files.sort();
    coverage.sort_by(|left, right| left.rel_path.cmp(&right.rel_path));
    Ok(ScanPlan { files, coverage })
}

/// Walk a codebase root and produce per-file graph payloads. Pure planning —
/// the store layer decides what actually changed (incremental). Symlinks are
/// intentionally excluded: a repository file must never make the index walk
/// outside the host-authorized root between enumeration and open.
pub fn scan_codebase(root: &Path) -> Result<Vec<PathBuf>, String> {
    Ok(scan_codebase_detailed(root)?.files)
}

/// Build the graph payload for one source file. `codebase` is the opaque
/// repository namespace returned by [`repository_id`]; FQNs are
/// "repository_id::rel_path::symbol" so relocation does not leak the root.
pub fn index_file(codebase: &str, root: &Path, path: &Path) -> Result<IndexedFile, String> {
    let lang = Lang::from_path(path).ok_or("unsupported language")?;
    // Enumeration and opening are separate filesystem operations. Re-check
    // the canonical path immediately before reading so a symlink swap cannot
    // make a file outside the authorized root look like an in-root source.
    let canonical_root = root
        .canonicalize()
        .map_err(|e| format!("canonicalize root {root:?}: {e}"))?;
    let file_meta =
        std::fs::symlink_metadata(path).map_err(|e| format!("stat source {path:?}: {e}"))?;
    if file_meta.file_type().is_symlink() || !file_meta.is_file() {
        return Err("source path changed or is not a regular file".into());
    }
    let canonical_path = path
        .canonicalize()
        .map_err(|e| format!("canonicalize source {path:?}: {e}"))?;
    if !canonical_path.starts_with(&canonical_root) {
        return Err("source path escaped authorized root".into());
    }
    let source = std::fs::read_to_string(&canonical_path)
        .map_err(|e| format!("read {canonical_path:?}: {e}"))?;
    let meta =
        std::fs::metadata(&canonical_path).map_err(|e| format!("stat {canonical_path:?}: {e}"))?;
    let rel = path
        .strip_prefix(root)
        .map_err(|_| "path outside root".to_string())?
        .to_string_lossy()
        .to_string();

    let extraction = extract_file(lang, &source)?;
    let file_fqn = format!("{codebase}::{rel}");
    let file_ref = NodeRef {
        kind: "file".into(),
        name: file_fqn.clone(),
    };

    let mut nodes = vec![GraphNodeInput {
        kind: "file".into(),
        name: file_fqn.clone(),
        label: Some(format!("{lang:?}").to_lowercase()),
        layer: Some("semantic".into()),
        trust: Some(3),
    }];
    let mut edges = Vec::new();
    let mut cues = Vec::new();
    let mut seen_cues: HashSet<String> = HashSet::new();

    for def in &extraction.defs {
        let sym_fqn = format!("{file_fqn}::{def}");
        let sym_ref = NodeRef {
            kind: "code_symbol".into(),
            name: sym_fqn.clone(),
        };
        nodes.push(GraphNodeInput {
            kind: "code_symbol".into(),
            name: sym_fqn.clone(),
            label: None,
            layer: Some("semantic".into()),
            trust: Some(3),
        });
        edges.push(GraphEdgeInput {
            src: file_ref.clone(),
            tag: "defines".into(),
            dst: sym_ref.clone(),
            fact: None,
            trust: Some(3),
        });
        for cue in identifier_cues(def).into_iter().chain([def.to_lowercase()]) {
            if seen_cues.insert(format!("{cue}->{sym_fqn}")) {
                cues.push(GraphCueInput {
                    cue,
                    node: sym_ref.clone(),
                    source: Some("code_index".into()),
                });
            }
        }
    }

    for import in &extraction.imports {
        edges.push(GraphEdgeInput {
            src: file_ref.clone(),
            tag: "imports".into(),
            dst: NodeRef {
                kind: "module".into(),
                name: format!("{codebase}::{import}"),
            },
            fact: None,
            trust: Some(3),
        });
    }

    // Name-matched call edges: file --calls--> bare callee name node. The
    // callee node is namespace-global to the codebase (not per-file) so
    // definitions and call sites of the same name converge; trust 0 marks
    // the low confidence of name matching.
    for call in &extraction.calls {
        edges.push(GraphEdgeInput {
            src: file_ref.clone(),
            tag: "calls".into(),
            dst: NodeRef {
                kind: "callable".into(),
                name: format!("{codebase}::{call}"),
            },
            fact: None,
            trust: Some(0),
        });
    }

    let symbol_count = extraction.defs.len();
    Ok(IndexedFile {
        rel_path: rel,
        blake3: blake3::hash(source.as_bytes()).to_hex().to_string(),
        mtime_ns: meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_nanos() as i64)
            .unwrap_or(0),
        size: meta.len() as i64,
        upsert: GraphUpsertInput { nodes, edges, cues },
        symbol_count,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

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
    fn git_repository_identity_is_opaque_and_move_stable() {
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
        let a = repository_id(&first).unwrap();
        let b = repository_id(&second).unwrap();
        assert!(a.starts_with("repo_git_"));
        assert_eq!(a, b);
        assert!(!a.contains(first.to_string_lossy().as_ref()));
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
        let on_main = repository_id(&root).unwrap();

        // A branch switch rewrites HEAD; the repository identity must not
        // move (the branch is carried separately as git_ref).
        std::fs::write(root.join(".git/HEAD"), b"ref: refs/heads/feature\n").unwrap();
        let on_feature = repository_id(&root).unwrap();
        assert_eq!(on_main, on_feature);

        // A detached HEAD must not move the identity either.
        std::fs::write(
            root.join(".git/HEAD"),
            b"0123456789abcdef0123456789abcdef01234567\n",
        )
        .unwrap();
        assert_eq!(on_main, repository_id(&root).unwrap());
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
        let gitdir = main.join(".git/worktrees/linked");
        std::fs::write(
            worktree.join(".git"),
            format!("gitdir: {}\n", gitdir.display()),
        )
        .unwrap();

        let shared = repository_id(&main).unwrap();
        let linked = repository_id(&worktree).unwrap();
        assert!(linked.starts_with("repo_git_"));
        assert_eq!(shared, linked, "worktree shares the repository identity");
        let _ = std::fs::remove_dir_all(main);
        let _ = std::fs::remove_dir_all(worktree);
    }
}
