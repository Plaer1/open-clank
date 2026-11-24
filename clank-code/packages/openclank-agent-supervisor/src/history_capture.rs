//! Supervisor-owned declared-root baselines for subprocess lifecycle receipts.
//!
//! A baseline is deliberately reported as observation coverage. It does not
//! claim to identify every concurrent write and is never used to authorize a
//! root. Root authorization remains in the supervisor's existing binding.

use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct DeclaredRootBaseline {
    pub roots: Vec<PathBuf>,
    pub file_count: usize,
    pub truncated: bool,
    pub manifest_digest: String,
    pub coverage: &'static str,
}

pub fn declared_root_baseline<I, P>(roots: I, max_files: usize) -> DeclaredRootBaseline
where
    I: IntoIterator<Item = P>,
    P: AsRef<Path>,
{
    let mut normalized = Vec::new();
    let mut entries = Vec::new();
    let mut truncated = false;
    for raw in roots {
        let Ok(root) = fs::canonicalize(raw.as_ref()) else {
            continue;
        };
        if !root.is_dir() || normalized.iter().any(|item| item == &root) {
            continue;
        }
        normalized.push(root.clone());
        let mut stack = vec![root.clone()];
        while let Some(current) = stack.pop() {
            let Ok(read) = fs::read_dir(&current) else {
                continue;
            };
            for item in read.flatten() {
                let path = item.path();
                if path.file_name().is_some_and(|name| {
                    matches!(name.to_str(), Some(".git" | "target" | "node_modules"))
                }) {
                    continue;
                }
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                if entries.len() >= max_files {
                    truncated = true;
                    break;
                }
                let Ok(bytes) = fs::read(&path) else { continue };
                let mut digest = Sha256::new();
                digest.update(&bytes);
                entries.push((
                    path.strip_prefix(&root).unwrap_or(&path).to_path_buf(),
                    format!("sha256:{:x}:{}", digest.finalize(), bytes.len()),
                ));
            }
            if truncated {
                break;
            }
        }
        if truncated {
            break;
        }
    }
    entries.sort();
    let encoded = serde_json::to_vec(&entries).unwrap_or_default();
    let mut digest = Sha256::new();
    digest.update(&encoded);
    DeclaredRootBaseline {
        roots: normalized,
        file_count: entries.len(),
        truncated,
        manifest_digest: format!("sha256:{:x}:{}", digest.finalize(), encoded.len()),
        coverage: "DeclaredRootsBaseline",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn baseline_is_bounded_and_changes_when_file_changes() {
        let root = tempdir().unwrap();
        let file = root.path().join("state.txt");
        fs::write(&file, "one").unwrap();
        let first = declared_root_baseline([root.path()], 10);
        fs::write(&file, "two").unwrap();
        let second = declared_root_baseline([root.path()], 10);
        assert_eq!(first.file_count, 1);
        assert_ne!(first.manifest_digest, second.manifest_digest);
        assert!(declared_root_baseline([root.path()], 0).truncated);
    }
}
