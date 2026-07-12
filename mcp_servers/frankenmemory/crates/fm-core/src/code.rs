//! Opt-in code graph (G3, design §3). Tree-sitter extraction of symbols,
//! imports and name-matched calls for Rust / Python / TypeScript, stored as
//! ordinary graph nodes/edges so graph_walk, RWR and groom work unchanged.
//!
//! NOTHING here runs automatically: a codebase enters the graph only through
//! the `code_index` tool. Call edges are name-matched and therefore
//! confidence-marked (trust 0) — dynamic dispatch will produce false/missed
//! edges by design; see robonotes/audits/codebase-memory-mcp.md.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use tree_sitter::{Parser, Query, QueryCursor, StreamingIterator};

use crate::graph::{GraphCueInput, GraphEdgeInput, GraphNodeInput, GraphUpsertInput, NodeRef};

/// Directories never worth indexing. v1 deny-list; .gitignore awareness can
/// come later if noise shows up in practice.
const SKIP_DIRS: &[&str] = &[
    ".git", "node_modules", "target", "dist", "build", ".venv", "venv",
    "__pycache__", ".next", ".cache", "vendor",
];

const MAX_FILE_BYTES: u64 = 512 * 1024;

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
            Self::Rust => r#"
                (function_item name: (identifier) @def)
                (struct_item name: (type_identifier) @def)
                (enum_item name: (type_identifier) @def)
                (trait_item name: (type_identifier) @def)
                (use_declaration argument: (_) @import)
                (call_expression function: (identifier) @call)
                (call_expression function: (field_expression field: (field_identifier) @call))
                (call_expression function: (scoped_identifier name: (identifier) @call))
            "#,
            Self::Python => r#"
                (function_definition name: (identifier) @def)
                (class_definition name: (identifier) @def)
                (import_statement name: (dotted_name) @import)
                (import_from_statement module_name: (dotted_name) @import)
                (call function: (identifier) @call)
                (call function: (attribute attribute: (identifier) @call))
            "#,
            Self::TypeScript => r#"
                (function_declaration name: (identifier) @def)
                (class_declaration name: (type_identifier) @def)
                (method_definition name: (property_identifier) @def)
                (import_statement source: (string (string_fragment) @import))
                (call_expression function: (identifier) @call)
                (call_expression function: (member_expression property: (property_identifier) @call))
            "#,
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
        } else if c.is_uppercase() && !current.is_empty()
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

/// Walk a codebase root and produce per-file graph payloads. Pure planning —
/// the store layer decides what actually changed (incremental).
pub fn scan_codebase(root: &Path) -> Result<Vec<PathBuf>, String> {
    let mut files = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = std::fs::read_dir(&dir).map_err(|e| format!("read_dir {dir:?}: {e}"))?;
        for entry in entries.flatten() {
            let path = entry.path();
            let Ok(meta) = entry.metadata() else { continue };
            if meta.is_dir() {
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if !SKIP_DIRS.contains(&name) && !name.starts_with('.') {
                    stack.push(path);
                }
            } else if meta.is_file()
                && meta.len() <= MAX_FILE_BYTES
                && Lang::from_path(&path).is_some()
            {
                files.push(path);
            }
        }
    }
    files.sort();
    Ok(files)
}

/// Build the graph payload for one source file. `codebase` is the opt-in
/// namespace (the root path as given to code_index); FQNs are
/// "codebase::rel_path::symbol" so removal and lookup stay scoped.
pub fn index_file(codebase: &str, root: &Path, path: &Path) -> Result<IndexedFile, String> {
    let lang = Lang::from_path(path).ok_or("unsupported language")?;
    let source = std::fs::read_to_string(path).map_err(|e| format!("read {path:?}: {e}"))?;
    let meta = std::fs::metadata(path).map_err(|e| format!("stat {path:?}: {e}"))?;
    let rel = path
        .strip_prefix(root)
        .map_err(|_| "path outside root".to_string())?
        .to_string_lossy()
        .to_string();

    let extraction = extract_file(lang, &source)?;
    let file_fqn = format!("{codebase}::{rel}");
    let file_ref = NodeRef { kind: "file".into(), name: file_fqn.clone() };

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
        let sym_ref = NodeRef { kind: "code_symbol".into(), name: sym_fqn.clone() };
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
            dst: NodeRef { kind: "module".into(), name: format!("{codebase}::{import}") },
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
            dst: NodeRef { kind: "callable".into(), name: format!("{codebase}::{call}") },
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
        assert_eq!(identifier_cues("parseModelSelection"), vec!["parse", "model", "selection"]);
        assert_eq!(identifier_cues("graph_edge_decay"), vec!["graph", "edge", "decay"]);
        assert!(identifier_cues("ab").is_empty(), "short fragments dropped");
    }
}
