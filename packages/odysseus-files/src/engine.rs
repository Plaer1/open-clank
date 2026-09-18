use crate::{
    AgentScope, AppScope, Capability, Cursor, RegistryError, RootKind, RootRecord, RootRegistry,
    WorkCost, component_contains, lexical_normalize,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::cmp::Ordering;
use std::collections::{BTreeSet, HashMap, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use thiserror::Error;

#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub directory_page_size: usize,
    pub max_read_bytes: usize,
    pub max_write_bytes: usize,
}

/// Authoritative mutation-owner capture boundary. The provider implementation
/// may use the local history service, a durable outbox, or a journal. Capture
/// admission failures are deliberately reported to the hook while the live
/// filesystem operation remains authoritative.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CaptureTarget {
    pub resource_id: PathBuf,
    pub before: Option<Vec<u8>>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CaptureAfter {
    pub resource_id: PathBuf,
    pub after: Option<Vec<u8>>,
}

pub trait MutationCaptureTicket: Send {
    fn action_id(&self) -> &str;
    fn complete(self: Box<Self>, after: Vec<CaptureAfter>) -> Result<(), String>;
    fn abort(self: Box<Self>);
}

pub trait MutationCaptureHook: Send + Sync {
    fn prepare(
        &self,
        operation: &str,
        targets: &[CaptureTarget],
    ) -> Result<Box<dyn MutationCaptureTicket>, String>;

    fn status(&self, _action_id: &str) -> Option<CaptureStatus> {
        None
    }

    fn last_action_id(&self) -> Option<String> {
        None
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CaptureStatus {
    pub action_id: String,
    pub status: String,
    pub phase: String,
    pub error: Option<String>,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            directory_page_size: 200,
            max_read_bytes: 1024 * 1024,
            max_write_bytes: 10 * 1024 * 1024,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub enum FileKind {
    File,
    Directory,
    Symlink,
    Other,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub enum TextEncoding {
    Utf8,
    Utf16Le,
    Utf16Be,
    Utf32Le,
    Utf32Be,
}

impl TextEncoding {
    pub fn label(self) -> &'static str {
        match self {
            Self::Utf8 => "utf-8",
            Self::Utf16Le => "utf-16-le",
            Self::Utf16Be => "utf-16-be",
            Self::Utf32Le => "utf-32-le",
            Self::Utf32Be => "utf-32-be",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct TextSnapshot {
    pub text: String,
    pub fingerprint: Fingerprint,
    pub encoding: TextEncoding,
    pub bom_bytes: usize,
    pub newline: String,
    pub mode: Option<u32>,
    pub size: u64,
}

/// A display-only text sample whose filesystem work is bounded independently
/// of the file's total size. Large files include decoded text from both ends;
/// callers can distinguish that sample from a complete snapshot via
/// `truncated` and the explicit byte counters.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct TextPreview {
    pub text: String,
    pub encoding: TextEncoding,
    pub bom_bytes: usize,
    pub newline: String,
    pub size: u64,
    pub truncated: bool,
    pub head_bytes: u64,
    pub tail_bytes: u64,
    pub work: WorkCost,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct EditOutcome {
    pub old_fingerprint: Fingerprint,
    pub new_fingerprint: Fingerprint,
    pub replacements: usize,
    pub encoding: TextEncoding,
    pub newline: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CopyOutcome {
    pub source: PathBuf,
    pub destination: PathBuf,
    pub fingerprint: Fingerprint,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct TrashEntry {
    pub id: String,
    pub root_id: String,
    pub original_path: PathBuf,
    pub trashed_path: PathBuf,
    #[serde(default)]
    pub fingerprint: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct SearchOptions {
    pub query: String,
    pub max_results: usize,
    pub max_entries: u64,
    pub max_depth: usize,
    pub max_bytes_per_file: usize,
    pub include_hidden: bool,
    pub case_sensitive: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SearchResult {
    pub matches: Vec<PathBuf>,
    pub complete: bool,
    pub work: WorkCost,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DirectoryEntry {
    pub name: String,
    pub kind: FileKind,
    pub size: u64,
    pub modified_unix_ms: Option<u64>,
}

/// Versioned metadata ordering shared by Code and Files. This is presentation
/// ordering only; authorization still happens before the directory is opened.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct DirectorySort {
    #[serde(default)]
    pub key: String,
    #[serde(default)]
    pub direction: String,
    #[serde(default = "default_directories_first")]
    pub directories_first: bool,
    #[serde(default)]
    pub collation: String,
}

fn default_directories_first() -> bool {
    true
}

impl Default for DirectorySort {
    fn default() -> Self {
        Self {
            key: "name".into(),
            direction: "asc".into(),
            directories_first: true,
            collation: "open-clank-v1".into(),
        }
    }
}

impl DirectorySort {
    fn normalized(&self) -> Self {
        let key = match self.key.to_ascii_lowercase().as_str() {
            "kind" | "modified" | "size" => self.key.to_ascii_lowercase(),
            _ => "name".into(),
        };
        let direction = if self.direction.eq_ignore_ascii_case("desc") {
            "desc"
        } else {
            "asc"
        };
        let collation = if self.collation.is_empty() {
            "open-clank-v1"
        } else {
            self.collation.as_str()
        };
        Self {
            key,
            direction: direction.into(),
            directories_first: self.directories_first,
            collation: collation.into(),
        }
    }

    fn signature(&self) -> String {
        let normalized = self.normalized();
        format!(
            "{}:{}:{}:{}",
            normalized.key,
            normalized.direction,
            if normalized.directories_first { 1 } else { 0 },
            normalized.collation.replace(':', "_")
        )
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct FileStat {
    pub path: PathBuf,
    pub kind: FileKind,
    pub size: u64,
    pub modified_unix_ms: Option<u64>,
    pub mode: Option<u32>,
    pub fingerprint: Option<Fingerprint>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DirectoryPage {
    pub path: PathBuf,
    pub entries: Vec<DirectoryEntry>,
    pub next_cursor: Option<Cursor>,
    pub generation: u64,
    pub work: WorkCost,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct FileReadPage {
    pub path: PathBuf,
    pub offset: u64,
    pub bytes: Vec<u8>,
    pub next_offset: Option<u64>,
    pub eof: bool,
    pub fingerprint: Option<Fingerprint>,
    pub work: WorkCost,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct Fingerprint {
    pub algorithm: &'static str,
    pub value: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub enum ReplaceOutcome {
    Created { fingerprint: Fingerprint },
    Replaced { fingerprint: Fingerprint },
}

#[derive(Debug, Error)]
pub enum EngineError {
    #[error("authorization failed: {0}")]
    Registry(#[from] RegistryError),
    #[error("filesystem operation failed: {0}")]
    Io(#[from] io::Error),
    #[error("request exceeds the configured byte limit")]
    LimitExceeded,
    #[error("expected fingerprint does not match the current file")]
    Conflict,
    #[error("directory cursor is stale")]
    StaleCursor,
    #[error("destination already exists")]
    DestinationExists,
    #[error("move crosses filesystem volumes")]
    CrossVolume,
    #[error("cannot trash an approved root itself")]
    CannotTrashRoot,
    #[error("trash entry is invalid or no longer available")]
    InvalidTrashEntry,
    #[error("operation cancelled")]
    Cancelled,
    #[error("target is not a regular file")]
    NotAFile,
    #[error("target is not a supported text file")]
    BinaryOrUnsupportedText,
    #[error("target is not a directory")]
    NotADirectory,
}

pub struct FileEngine {
    config: EngineConfig,
    registry: RootRegistry,
    // Sorted pages are a presentation snapshot, never an authority cache.
    // The key includes the canonical directory identity, observed generation,
    // and complete sort signature; mutations invalidate the bounded cache.
    sorted_snapshot_cache: Mutex<SortedSnapshotCache>,
    capture_hook: Option<Arc<dyn MutationCaptureHook>>,
}

const SORTED_SNAPSHOT_CACHE_MAX_ENTRIES: usize = 250_000;
const SORTED_SNAPSHOT_CACHE_MAX_SNAPSHOTS: usize = 32;

#[derive(Default)]
struct SortedSnapshotCache {
    snapshots: HashMap<String, Arc<[DirectoryEntry]>>,
    order: VecDeque<String>,
    total_entries: usize,
}

impl SortedSnapshotCache {
    fn get(&mut self, key: &str) -> Option<Arc<[DirectoryEntry]>> {
        let value = self.snapshots.get(key)?.clone();
        self.order.retain(|candidate| candidate != key);
        self.order.push_back(key.to_string());
        Some(value)
    }

    fn insert(&mut self, key: String, entries: Arc<[DirectoryEntry]>) {
        // Very large directories still receive a correctly sorted first page,
        // but are not retained indefinitely in process memory. A future
        // external-sort cursor can replace this explicit bounded fallback.
        if entries.len() > SORTED_SNAPSHOT_CACHE_MAX_ENTRIES {
            return;
        }
        if let Some(previous) = self.snapshots.remove(&key) {
            self.total_entries = self.total_entries.saturating_sub(previous.len());
            self.order.retain(|candidate| candidate != &key);
        }
        while !self.snapshots.is_empty()
            && (self.snapshots.len() >= SORTED_SNAPSHOT_CACHE_MAX_SNAPSHOTS
                || self.total_entries + entries.len() > SORTED_SNAPSHOT_CACHE_MAX_ENTRIES)
        {
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            if let Some(removed) = self.snapshots.remove(&oldest) {
                self.total_entries = self.total_entries.saturating_sub(removed.len());
            }
        }
        self.total_entries += entries.len();
        self.order.push_back(key.clone());
        self.snapshots.insert(key, entries);
    }

    fn clear(&mut self) {
        self.snapshots.clear();
        self.order.clear();
        self.total_entries = 0;
    }
}

fn kind_label(kind: FileKind) -> &'static str {
    match kind {
        FileKind::Directory => "directory",
        FileKind::File => "file",
        FileKind::Symlink => "symlink",
        FileKind::Other => "other",
    }
}

fn compare_directory_entries(
    left: &DirectoryEntry,
    right: &DirectoryEntry,
    sort: &DirectorySort,
) -> Ordering {
    if sort.directories_first
        && (left.kind == FileKind::Directory) != (right.kind == FileKind::Directory)
    {
        return if left.kind == FileKind::Directory {
            Ordering::Less
        } else {
            Ordering::Greater
        };
    }
    let mut result = match sort.key.as_str() {
        "kind" => kind_label(left.kind).cmp(kind_label(right.kind)),
        "modified" => compare_optional_number(left.modified_unix_ms, right.modified_unix_ms),
        "size" => compare_optional_number(
            (left.kind != FileKind::Directory).then_some(left.size),
            (right.kind != FileKind::Directory).then_some(right.size),
        ),
        _ => left.name.to_lowercase().cmp(&right.name.to_lowercase()),
    };
    if result == Ordering::Equal {
        result = left.name.cmp(&right.name);
    }
    if result == Ordering::Equal {
        result = kind_label(left.kind).cmp(kind_label(right.kind));
    }
    if sort.direction == "desc" {
        result.reverse()
    } else {
        result
    }
}

fn compare_optional_number(left: Option<u64>, right: Option<u64>) -> Ordering {
    match (left, right) {
        (Some(a), Some(b)) => a.cmp(&b),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    }
}

impl FileEngine {
    pub fn new(config: EngineConfig, registry: RootRegistry) -> Self {
        Self {
            config,
            registry,
            sorted_snapshot_cache: Mutex::new(SortedSnapshotCache::default()),
            capture_hook: None,
        }
    }

    pub fn with_capture_hook(mut self, hook: Arc<dyn MutationCaptureHook>) -> Self {
        self.capture_hook = Some(hook);
        self
    }

    pub fn set_capture_hook(&mut self, hook: Arc<dyn MutationCaptureHook>) {
        self.capture_hook = Some(hook);
    }

    pub fn capture_status(&self, action_id: &str) -> Option<CaptureStatus> {
        self.capture_hook.as_ref()?.status(action_id)
    }

    pub fn last_capture_status(&self) -> Option<CaptureStatus> {
        let hook = self.capture_hook.as_ref()?;
        let action_id = hook.last_action_id()?;
        hook.status(&action_id)
    }

    fn prepare_capture(
        &self,
        operation: &str,
        targets: Vec<CaptureTarget>,
    ) -> Option<Box<dyn MutationCaptureTicket>> {
        let hook = self.capture_hook.as_ref()?;
        match hook.prepare(operation, &targets) {
            Ok(ticket) => Some(ticket),
            Err(_) => None,
        }
    }

    fn finish_capture(ticket: Option<Box<dyn MutationCaptureTicket>>, after: Vec<CaptureAfter>) {
        if let Some(ticket) = ticket {
            let _ = ticket.complete(after);
        }
    }

    pub fn config(&self) -> &EngineConfig {
        &self.config
    }

    pub fn registry(&self) -> &RootRegistry {
        &self.registry
    }

    fn clear_sorted_snapshot_cache(&self) {
        if let Ok(mut cache) = self.sorted_snapshot_cache.lock() {
            cache.clear();
        }
    }

    fn authorize_checked(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        requested: &BTreeSet<Capability>,
        allow_missing_final: bool,
    ) -> Result<&RootRecord, EngineError> {
        let mut missing_final = false;
        let resolved = match fs::canonicalize(path) {
            Ok(path) => path,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let parent = path.parent().ok_or(error)?;
                let parent = fs::canonicalize(parent)?;
                let name = path.file_name().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "missing final path component")
                })?;
                missing_final = true;
                parent.join(name)
            }
            Err(error) => return Err(EngineError::Io(error)),
        };
        let root = self
            .registry
            .authorize(owner_id, &resolved, requested, scope)?;
        if missing_final && !allow_missing_final {
            return Err(EngineError::Io(io::Error::new(
                io::ErrorKind::NotFound,
                "target does not exist",
            )));
        }
        let root_path = lexical_normalize(Path::new(&root.canonical_path));
        let resolved = lexical_normalize(&resolved);
        let contained = match root.kind {
            RootKind::ExactFile => resolved == root_path,
            RootKind::RecursiveDirectory => component_contains(&root_path, &resolved),
        };
        if !contained {
            return Err(EngineError::Registry(RegistryError::OutsideRoot));
        }
        Ok(root)
    }

    fn authorize_app_checked(
        &self,
        scope: &AppScope,
        path: &Path,
        requested: &BTreeSet<Capability>,
        allow_missing_final: bool,
    ) -> Result<&RootRecord, EngineError> {
        if scope.host {
            return Err(EngineError::Registry(RegistryError::OutsideRoot));
        }
        let mut missing_final = false;
        let resolved = match fs::canonicalize(path) {
            Ok(path) => path,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let parent = path.parent().ok_or(error)?;
                let parent = fs::canonicalize(parent)?;
                let name = path.file_name().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "missing final path component")
                })?;
                missing_final = true;
                parent.join(name)
            }
            Err(error) => return Err(EngineError::Io(error)),
        };
        let root = self.registry.authorize_app(scope, &resolved, requested)?;
        if missing_final && !allow_missing_final {
            return Err(EngineError::Io(io::Error::new(
                io::ErrorKind::NotFound,
                "target does not exist",
            )));
        }
        let root_path = lexical_normalize(Path::new(&root.canonical_path));
        let resolved = lexical_normalize(&resolved);
        let contained = match root.kind {
            RootKind::ExactFile => resolved == root_path,
            RootKind::RecursiveDirectory => component_contains(&root_path, &resolved),
        };
        if !contained {
            return Err(EngineError::Registry(RegistryError::OutsideRoot));
        }
        if let Some(folder) = &scope.active_folder {
            if folder.root_id != root.id
                || !component_contains(&root_path, Path::new(&folder.canonical_path))
                || !component_contains(Path::new(&folder.canonical_path), &resolved)
                || !requested.is_subset(&folder.capabilities)
            {
                return Err(EngineError::Registry(RegistryError::OutsideRoot));
            }
        }
        Ok(root)
    }

    pub fn list_directory(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        cursor: Option<&Cursor>,
    ) -> Result<DirectoryPage, EngineError> {
        self.list_directory_with_limit(
            owner_id,
            scope,
            path,
            cursor,
            self.config.directory_page_size,
        )
    }

    pub fn list_directory_with_limit(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        cursor: Option<&Cursor>,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        self.validate_directory_page_size(limit)?;
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.list_directory_resolved(&resolved, cursor, limit)
    }

    pub fn list_directory_sorted(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        cursor: Option<&Cursor>,
        sort: &DirectorySort,
    ) -> Result<DirectoryPage, EngineError> {
        self.list_directory_sorted_with_limit(
            owner_id,
            scope,
            path,
            cursor,
            sort,
            self.config.directory_page_size,
        )
    }

    pub fn list_directory_sorted_with_limit(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        cursor: Option<&Cursor>,
        sort: &DirectorySort,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        self.validate_directory_page_size(limit)?;
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.list_directory_resolved_sorted(&resolved, cursor, sort, limit)
    }

    /// App-principal browsing is intentionally not constrained by the agent
    /// registry. It still canonicalizes the target so a symlink swap cannot
    /// silently change which path is displayed between authorization and I/O.
    pub fn list_directory_app(
        &self,
        path: &Path,
        cursor: Option<&Cursor>,
    ) -> Result<DirectoryPage, EngineError> {
        self.list_directory_app_with_limit(path, cursor, self.config.directory_page_size)
    }

    pub fn list_directory_app_with_limit(
        &self,
        path: &Path,
        cursor: Option<&Cursor>,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        self.validate_directory_page_size(limit)?;
        let resolved = resolve_existing_path(path)?;
        self.list_directory_resolved(&resolved, cursor, limit)
    }

    pub fn list_directory_app_sorted(
        &self,
        path: &Path,
        cursor: Option<&Cursor>,
        sort: &DirectorySort,
    ) -> Result<DirectoryPage, EngineError> {
        self.list_directory_app_sorted_with_limit(
            path,
            cursor,
            sort,
            self.config.directory_page_size,
        )
    }

    pub fn list_directory_app_sorted_with_limit(
        &self,
        path: &Path,
        cursor: Option<&Cursor>,
        sort: &DirectorySort,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        self.validate_directory_page_size(limit)?;
        let resolved = resolve_existing_path(path)?;
        self.list_directory_resolved_sorted(&resolved, cursor, sort, limit)
    }

    pub fn list_directory_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        cursor: Option<&Cursor>,
    ) -> Result<DirectoryPage, EngineError> {
        self.list_directory_scoped_with_limit(scope, path, cursor, self.config.directory_page_size)
    }

    pub fn list_directory_scoped_with_limit(
        &self,
        scope: &AppScope,
        path: &Path,
        cursor: Option<&Cursor>,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        self.validate_directory_page_size(limit)?;
        if scope.host {
            return self.list_directory_app_with_limit(path, cursor, limit);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.list_directory_resolved(&resolved, cursor, limit)
    }

    pub fn list_directory_scoped_sorted(
        &self,
        scope: &AppScope,
        path: &Path,
        cursor: Option<&Cursor>,
        sort: &DirectorySort,
    ) -> Result<DirectoryPage, EngineError> {
        self.list_directory_scoped_sorted_with_limit(
            scope,
            path,
            cursor,
            sort,
            self.config.directory_page_size,
        )
    }

    pub fn list_directory_scoped_sorted_with_limit(
        &self,
        scope: &AppScope,
        path: &Path,
        cursor: Option<&Cursor>,
        sort: &DirectorySort,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        self.validate_directory_page_size(limit)?;
        if scope.host {
            return self.list_directory_app_sorted_with_limit(path, cursor, sort, limit);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.list_directory_resolved_sorted(&resolved, cursor, sort, limit)
    }

    fn validate_directory_page_size(&self, limit: usize) -> Result<(), EngineError> {
        if limit == 0 || limit > self.config.directory_page_size {
            return Err(EngineError::LimitExceeded);
        }
        Ok(())
    }

    fn list_directory_resolved(
        &self,
        path: &Path,
        cursor: Option<&Cursor>,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        let metadata = fs::metadata(path)?;
        if !metadata.is_dir() {
            return Err(EngineError::NotADirectory);
        }
        let generation = directory_generation(&metadata);
        let path_key = directory_path_key(path);
        let offset = if let Some(cursor) = cursor {
            let expected_prefix = format!("dir:{path_key}:{generation}:");
            if cursor.generation != generation || !cursor.token.starts_with(&expected_prefix) {
                return Err(EngineError::StaleCursor);
            }
            cursor.position as usize
        } else {
            0
        };
        let mut entries = Vec::with_capacity(limit);
        let mut visited = 0_u64;
        let mut iterator = fs::read_dir(path)?;
        while entries.len() < limit {
            let Some(entry) = iterator.next() else { break };
            let entry = entry?;
            if visited < offset as u64 {
                visited += 1;
                continue;
            }
            let file_type = entry.file_type()?;
            let kind = if file_type.is_file() {
                FileKind::File
            } else if file_type.is_dir() {
                FileKind::Directory
            } else if file_type.is_symlink() {
                FileKind::Symlink
            } else {
                FileKind::Other
            };
            let metadata = fs::symlink_metadata(entry.path())?;
            entries.push(DirectoryEntry {
                name: entry.file_name().to_string_lossy().into_owned(),
                kind,
                size: metadata.len(),
                modified_unix_ms: system_time_ms(metadata.modified().ok()),
            });
            visited += 1;
        }
        let has_more = if entries.len() == limit {
            let has_more = iterator.next().is_some();
            if has_more {
                visited += 1;
            }
            has_more
        } else {
            false
        };
        let next_position = offset + entries.len();
        let next_cursor = has_more.then(|| Cursor {
            token: format!("dir:{path_key}:{generation}:{next_position}"),
            generation,
            position: next_position as u64,
        });
        Ok(DirectoryPage {
            path: path.to_path_buf(),
            entries,
            next_cursor,
            generation,
            work: WorkCost {
                entries_visited: visited,
                ..WorkCost::default()
            },
        })
    }

    fn list_directory_resolved_sorted(
        &self,
        path: &Path,
        cursor: Option<&Cursor>,
        sort: &DirectorySort,
        limit: usize,
    ) -> Result<DirectoryPage, EngineError> {
        let metadata = fs::metadata(path)?;
        if !metadata.is_dir() {
            return Err(EngineError::NotADirectory);
        }
        let generation = directory_generation(&metadata);
        let path_key = directory_path_key(path);
        let sort = sort.normalized();
        let sort_signature = sort.signature();
        let expected_prefix = format!("dir:{path_key}:{generation}:{sort_signature}:");
        let (offset, requested_snapshot) = if let Some(cursor) = cursor {
            let position_suffix = format!(":{}", cursor.position);
            let snapshot = cursor
                .token
                .strip_prefix(&expected_prefix)
                .and_then(|value| value.strip_suffix(&position_suffix))
                .filter(|value| !value.is_empty() && !value.contains(':'));
            if cursor.generation != generation || snapshot.is_none() {
                return Err(EngineError::StaleCursor);
            }
            (cursor.position as usize, snapshot.map(str::to_string))
        } else {
            (0, None)
        };
        let requested_key = requested_snapshot
            .as_ref()
            .map(|snapshot| format!("{path_key}:{generation}:{sort_signature}:{snapshot}"));
        // A request without a cursor starts a fresh listing and therefore
        // must observe changed child size/mtime even when the directory inode
        // itself did not change. Continuation pages reuse the exact immutable
        // snapshot named by their cursor.
        let cached = requested_key.as_ref().and_then(|snapshot_key| {
            self.sorted_snapshot_cache
                .lock()
                .ok()
                .and_then(|mut cache| cache.get(snapshot_key))
        });
        let (all_entries, entries_visited, snapshot_id) = if let Some(entries) = cached {
            (
                entries,
                0_u64,
                requested_snapshot.expect("cached cursor snapshot"),
            )
        } else {
            let mut all_entries = Vec::new();
            let mut iterator = fs::read_dir(path)?;
            while let Some(entry) = iterator.next() {
                let entry = entry?;
                let file_type = entry.file_type()?;
                let kind = if file_type.is_file() {
                    FileKind::File
                } else if file_type.is_dir() {
                    FileKind::Directory
                } else if file_type.is_symlink() {
                    FileKind::Symlink
                } else {
                    FileKind::Other
                };
                let metadata = fs::symlink_metadata(entry.path())?;
                all_entries.push(DirectoryEntry {
                    name: entry.file_name().to_string_lossy().into_owned(),
                    kind,
                    size: metadata.len(),
                    modified_unix_ms: system_time_ms(metadata.modified().ok()),
                });
            }
            all_entries.sort_by(|left, right| compare_directory_entries(left, right, &sort));
            let visited = all_entries.len() as u64;
            let snapshot_id = directory_snapshot_identity(&all_entries);
            if requested_snapshot
                .as_deref()
                .is_some_and(|expected| expected != snapshot_id)
            {
                return Err(EngineError::StaleCursor);
            }
            let entries: Arc<[DirectoryEntry]> = all_entries.into();
            if let Ok(mut cache) = self.sorted_snapshot_cache.lock() {
                cache.insert(
                    format!("{path_key}:{generation}:{sort_signature}:{snapshot_id}"),
                    entries.clone(),
                );
            }
            (entries, visited, snapshot_id)
        };
        let end = offset.saturating_add(limit).min(all_entries.len());
        let entries = if offset < all_entries.len() {
            all_entries[offset..end].to_vec()
        } else {
            Vec::new()
        };
        let has_more = end < all_entries.len();
        let next_position = end;
        let next_cursor = has_more.then(|| Cursor {
            token: format!(
                "dir:{path_key}:{generation}:{sort_signature}:{snapshot_id}:{next_position}"
            ),
            generation,
            position: next_position as u64,
        });
        Ok(DirectoryPage {
            path: path.to_path_buf(),
            entries,
            next_cursor,
            generation,
            work: WorkCost {
                entries_visited,
                ..WorkCost::default()
            },
        })
    }

    pub fn stat(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        include_fingerprint: bool,
    ) -> Result<FileStat, EngineError> {
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.stat_resolved(&resolved, include_fingerprint)
    }

    pub fn stat_app(
        &self,
        path: &Path,
        include_fingerprint: bool,
    ) -> Result<FileStat, EngineError> {
        let resolved = resolve_existing_path(path)?;
        self.stat_resolved(&resolved, include_fingerprint)
    }

    pub fn stat_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        include_fingerprint: bool,
    ) -> Result<FileStat, EngineError> {
        if scope.host {
            return self.stat_app(path, include_fingerprint);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.stat_resolved(&resolved, include_fingerprint)
    }

    fn stat_resolved(
        &self,
        path: &Path,
        include_fingerprint: bool,
    ) -> Result<FileStat, EngineError> {
        let metadata = fs::metadata(path)?;
        let kind = if metadata.is_file() {
            FileKind::File
        } else if metadata.is_dir() {
            FileKind::Directory
        } else {
            FileKind::Other
        };
        let fingerprint = if include_fingerprint && metadata.is_file() {
            Some(fingerprint_file(path)?)
        } else {
            None
        };
        Ok(FileStat {
            path: path.to_path_buf(),
            kind,
            size: metadata.len(),
            modified_unix_ms: system_time_ms(metadata.modified().ok()),
            mode: file_mode(path),
            fingerprint,
        })
    }

    pub fn read_range(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        offset: u64,
        length: usize,
        include_fingerprint: bool,
    ) -> Result<FileReadPage, EngineError> {
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.read_range_resolved(&resolved, offset, length, include_fingerprint)
    }

    pub fn read_range_app(
        &self,
        path: &Path,
        offset: u64,
        length: usize,
        include_fingerprint: bool,
    ) -> Result<FileReadPage, EngineError> {
        let resolved = resolve_existing_path(path)?;
        self.read_range_resolved(&resolved, offset, length, include_fingerprint)
    }

    pub fn read_range_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        offset: u64,
        length: usize,
        include_fingerprint: bool,
    ) -> Result<FileReadPage, EngineError> {
        if scope.host {
            return self.read_range_app(path, offset, length, include_fingerprint);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.read_range_resolved(&resolved, offset, length, include_fingerprint)
    }

    fn read_range_resolved(
        &self,
        path: &Path,
        offset: u64,
        length: usize,
        include_fingerprint: bool,
    ) -> Result<FileReadPage, EngineError> {
        if length > self.config.max_read_bytes {
            return Err(EngineError::LimitExceeded);
        }
        let mut file = File::open(path)?;
        // Authorization resolves the path before this open.  Re-resolve after
        // opening so a symlink/reparse-point swap between those steps cannot
        // silently turn a scoped read into a different object.
        if fs::canonicalize(path)? != path {
            return Err(EngineError::Registry(RegistryError::OutsideRoot));
        }
        let metadata = file.metadata()?;
        if !metadata.is_file() {
            return Err(EngineError::NotAFile);
        }
        file.seek(SeekFrom::Start(offset))?;
        let mut bytes = vec![0; length];
        let read = file.read(&mut bytes)?;
        bytes.truncate(read);
        let end = offset.saturating_add(read as u64);
        let eof = end >= metadata.len();
        let fingerprint = include_fingerprint
            .then(|| fingerprint_file(path))
            .transpose()?;
        Ok(FileReadPage {
            path: path.to_path_buf(),
            offset,
            bytes,
            next_offset: (!eof).then_some(end),
            eof,
            fingerprint,
            work: WorkCost {
                bytes_read: read as u64,
                hashes_computed: u64::from(include_fingerprint),
                ..WorkCost::default()
            },
        })
    }

    pub fn read_text_snapshot(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
    ) -> Result<TextSnapshot, EngineError> {
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.read_text_snapshot_resolved(&resolved)
    }

    /// App-principal text reads retain the same decoding/binary protections as
    /// agent reads but deliberately skip the agent root registry.
    pub fn read_text_snapshot_app(&self, path: &Path) -> Result<TextSnapshot, EngineError> {
        let resolved = resolve_existing_path(path)?;
        self.read_text_snapshot_resolved(&resolved)
    }

    pub fn read_text_snapshot_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
    ) -> Result<TextSnapshot, EngineError> {
        if scope.host {
            return self.read_text_snapshot_app(path);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.read_text_snapshot_resolved(&resolved)
    }

    fn read_text_snapshot_resolved(&self, path: &Path) -> Result<TextSnapshot, EngineError> {
        let mut file = File::open(path)?;
        if fs::canonicalize(path)? != path {
            return Err(EngineError::Registry(RegistryError::OutsideRoot));
        }
        let metadata = file.metadata()?;
        if !metadata.is_file() {
            return Err(EngineError::NotAFile);
        }
        if metadata.len() > self.config.max_read_bytes as u64 {
            return Err(EngineError::LimitExceeded);
        }
        let mut raw = Vec::with_capacity(metadata.len() as usize);
        file.read_to_end(&mut raw)?;
        let fingerprint = fingerprint_bytes(&raw);
        let (encoding, bom_bytes, payload) = detect_encoding(&raw);
        let text = decode_text(encoding, payload).ok_or(EngineError::BinaryOrUnsupportedText)?;
        if text.contains('\0') {
            return Err(EngineError::BinaryOrUnsupportedText);
        }
        let newline = dominant_newline(&text);
        Ok(TextSnapshot {
            text,
            fingerprint,
            encoding,
            bom_bytes,
            newline,
            mode: file_mode(path),
            size: raw.len() as u64,
        })
    }

    pub fn read_text_preview(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        max_bytes: usize,
    ) -> Result<TextPreview, EngineError> {
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.read_text_preview_resolved(&resolved, max_bytes)
    }

    pub fn read_text_preview_app(
        &self,
        path: &Path,
        max_bytes: usize,
    ) -> Result<TextPreview, EngineError> {
        let resolved = resolve_existing_path(path)?;
        self.read_text_preview_resolved(&resolved, max_bytes)
    }

    pub fn read_text_preview_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        max_bytes: usize,
    ) -> Result<TextPreview, EngineError> {
        if scope.host {
            return self.read_text_preview_app(path, max_bytes);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        self.read_text_preview_resolved(&resolved, max_bytes)
    }

    fn read_text_preview_resolved(
        &self,
        path: &Path,
        max_bytes: usize,
    ) -> Result<TextPreview, EngineError> {
        if max_bytes == 0 || max_bytes > self.config.max_read_bytes {
            return Err(EngineError::LimitExceeded);
        }
        let mut file = File::open(path)?;
        if fs::canonicalize(path)? != path {
            return Err(EngineError::Registry(RegistryError::OutsideRoot));
        }
        let starting_metadata = file.metadata()?;
        if !starting_metadata.is_file() {
            return Err(EngineError::NotAFile);
        }
        let size = starting_metadata.len();
        let read_limit = usize::try_from(size.min(max_bytes as u64)).unwrap_or(max_bytes);
        let truncated = size > max_bytes as u64;

        let (mut head, mut tail) = if truncated {
            // Keep the presentation ratio aligned with the browser's existing
            // preview model while enforcing one aggregate raw-byte budget.
            let head_budget = ((max_bytes as u64 * 72) / 100).max(8) as usize;
            let head_budget = head_budget.min(max_bytes);
            let tail_budget = max_bytes - head_budget;
            let mut head = Vec::with_capacity(head_budget);
            Read::by_ref(&mut file)
                .take(head_budget as u64)
                .read_to_end(&mut head)?;
            let mut tail = Vec::with_capacity(tail_budget);
            if tail_budget > 0 {
                file.seek(SeekFrom::Start(size.saturating_sub(tail_budget as u64)))?;
                Read::by_ref(&mut file)
                    .take(tail_budget as u64)
                    .read_to_end(&mut tail)?;
            }
            (head, tail)
        } else {
            let mut head = Vec::with_capacity(read_limit);
            Read::by_ref(&mut file)
                .take(read_limit as u64)
                .read_to_end(&mut head)?;
            (head, Vec::new())
        };

        // Use metadata from the opened descriptor, rather than following the
        // path again, to catch truncation/growth during the bounded read.
        if file.metadata()?.len() != size {
            return Err(EngineError::Conflict);
        }

        let raw_bytes_read = (head.len() + tail.len()) as u64;
        let (encoding, bom_bytes, _) = detect_encoding(&head);
        let unit = encoding_code_unit_bytes(encoding);
        let head_payload_start = bom_bytes.min(head.len());
        let head_payload_len = align_down(head.len().saturating_sub(head_payload_start), unit);
        head.truncate(head_payload_start + head_payload_len);
        let head_text = decode_preview_chunk(
            encoding,
            &head[head_payload_start..],
            if truncated {
                PreviewChunkEdge::Head
            } else {
                PreviewChunkEdge::Whole
            },
        )
        .ok_or(EngineError::BinaryOrUnsupportedText)?;

        let (tail_text, tail_bytes) = if truncated && !tail.is_empty() {
            let raw_tail_start = size.saturating_sub(tail.len() as u64);
            let payload_origin = bom_bytes as u64;
            let aligned_tail_start =
                align_up_from_origin(raw_tail_start, payload_origin, unit as u64);
            let skip = usize::try_from(aligned_tail_start.saturating_sub(raw_tail_start))
                .unwrap_or(tail.len())
                .min(tail.len());
            tail.drain(..skip);
            let aligned_len = align_down(tail.len(), unit);
            tail.truncate(aligned_len);
            let decoded = decode_preview_chunk(encoding, &tail, PreviewChunkEdge::Tail)
                .ok_or(EngineError::BinaryOrUnsupportedText)?;
            (decoded, tail.len() as u64)
        } else {
            (String::new(), 0)
        };

        if sampled_text_is_binary(&head_text) || sampled_text_is_binary(&tail_text) {
            return Err(EngineError::BinaryOrUnsupportedText);
        }
        let sampled = if truncated {
            format!("{head_text}{tail_text}")
        } else {
            head_text.clone()
        };
        let newline = dominant_newline(&sampled);
        let text = if truncated {
            format!("{head_text}\n\n… [preview truncated; {size} bytes total] …\n\n{tail_text}")
        } else {
            head_text
        };
        let head_bytes = head.len().saturating_sub(head_payload_start) as u64;
        Ok(TextPreview {
            text,
            encoding,
            bom_bytes,
            newline,
            size,
            truncated,
            head_bytes,
            tail_bytes,
            work: WorkCost {
                bytes_read: raw_bytes_read,
                ..WorkCost::default()
            },
        })
    }

    pub fn edit_text(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        expected: Option<&Fingerprint>,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<EditOutcome, EngineError> {
        if old.is_empty() || old == new {
            return Err(EngineError::BinaryOrUnsupportedText);
        }
        let snapshot = self.read_text_snapshot(owner_id, scope, path)?;
        self.edit_text_snapshot(
            owner_id,
            scope,
            path,
            expected,
            old,
            new,
            replace_all,
            snapshot,
        )
    }

    /// App-principal editing has no agent registry check, but still uses the
    /// same optimistic fingerprint and atomic replacement implementation.
    pub fn edit_text_app(
        &self,
        path: &Path,
        expected: Option<&Fingerprint>,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<EditOutcome, EngineError> {
        if old.is_empty() || old == new {
            return Err(EngineError::BinaryOrUnsupportedText);
        }
        let resolved = resolve_existing_path(path)?;
        let snapshot = self.read_text_snapshot_resolved(&resolved)?;
        self.edit_text_snapshot_app(&resolved, expected, old, new, replace_all, snapshot)
    }

    pub fn edit_text_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        expected: Option<&Fingerprint>,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<EditOutcome, EngineError> {
        if scope.host {
            return self.edit_text_app(path, expected, old, new, replace_all);
        }
        if old.is_empty() || old == new {
            return Err(EngineError::BinaryOrUnsupportedText);
        }
        let snapshot = self.read_text_snapshot_scoped(scope, path)?;
        let bytes = self.edit_text_bytes(snapshot, expected, old, new, replace_all)?;
        let old_fingerprint = bytes.0.fingerprint.clone();
        let new_fingerprint =
            self.replace_if_fingerprint_scoped(scope, path, Some(&old_fingerprint), &bytes.1)?;
        let new_fingerprint = match new_fingerprint {
            ReplaceOutcome::Created { fingerprint } | ReplaceOutcome::Replaced { fingerprint } => {
                fingerprint
            }
        };
        Ok(EditOutcome {
            old_fingerprint,
            new_fingerprint,
            replacements: bytes.2,
            encoding: bytes.0.encoding,
            newline: bytes.0.newline,
        })
    }

    fn edit_text_snapshot(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        expected: Option<&Fingerprint>,
        old: &str,
        new: &str,
        replace_all: bool,
        snapshot: TextSnapshot,
    ) -> Result<EditOutcome, EngineError> {
        let bytes = self.edit_text_bytes(snapshot, expected, old, new, replace_all)?;
        let old_fingerprint = bytes.0.fingerprint.clone();
        let new_fingerprint =
            self.replace_if_fingerprint(owner_id, scope, path, Some(&old_fingerprint), &bytes.1)?;
        let new_fingerprint = match new_fingerprint {
            ReplaceOutcome::Created { fingerprint } | ReplaceOutcome::Replaced { fingerprint } => {
                fingerprint
            }
        };
        Ok(EditOutcome {
            old_fingerprint,
            new_fingerprint,
            replacements: bytes.2,
            encoding: bytes.0.encoding,
            newline: bytes.0.newline,
        })
    }

    fn edit_text_snapshot_app(
        &self,
        path: &Path,
        expected: Option<&Fingerprint>,
        old: &str,
        new: &str,
        replace_all: bool,
        snapshot: TextSnapshot,
    ) -> Result<EditOutcome, EngineError> {
        let bytes = self.edit_text_bytes(snapshot, expected, old, new, replace_all)?;
        let old_fingerprint = bytes.0.fingerprint.clone();
        let new_fingerprint =
            self.replace_if_fingerprint_app(path, Some(&old_fingerprint), &bytes.1)?;
        let new_fingerprint = match new_fingerprint {
            ReplaceOutcome::Created { fingerprint } | ReplaceOutcome::Replaced { fingerprint } => {
                fingerprint
            }
        };
        Ok(EditOutcome {
            old_fingerprint,
            new_fingerprint,
            replacements: bytes.2,
            encoding: bytes.0.encoding,
            newline: bytes.0.newline,
        })
    }

    fn edit_text_bytes(
        &self,
        snapshot: TextSnapshot,
        expected: Option<&Fingerprint>,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<(TextSnapshot, Vec<u8>, usize), EngineError> {
        if let Some(expected) = expected {
            if expected != &snapshot.fingerprint {
                return Err(EngineError::Conflict);
            }
        }
        let count = snapshot.text.matches(old).count();
        if count == 0 || (count > 1 && !replace_all) {
            return Err(EngineError::Conflict);
        }
        let updated = if replace_all {
            snapshot
                .text
                .replace(old, &preserve_newline_style(new, &snapshot.newline))
        } else {
            snapshot
                .text
                .replacen(old, &preserve_newline_style(new, &snapshot.newline), 1)
        };
        let bytes = encode_text(snapshot.encoding, snapshot.bom_bytes > 0, &updated)
            .ok_or(EngineError::BinaryOrUnsupportedText)?;
        Ok((snapshot, bytes, if replace_all { count } else { 1 }))
    }

    pub fn replace_if_fingerprint(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        expected: Option<&Fingerprint>,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_checked(owner_id, scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.replace_if_fingerprint_resolved(&resolved, expected, bytes)
    }

    pub fn replace_if_fingerprint_app(
        &self,
        path: &Path,
        expected: Option<&Fingerprint>,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        let resolved = resolve_for_write(path)?;
        self.replace_if_fingerprint_resolved(&resolved, expected, bytes)
    }

    pub fn replace_if_fingerprint_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        expected: Option<&Fingerprint>,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        if scope.host {
            return self.replace_if_fingerprint_app(path, expected, bytes);
        }
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_app_checked(scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.replace_if_fingerprint_resolved(&resolved, expected, bytes)
    }

    pub fn create_file(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_checked(owner_id, scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.create_file_resolved(&resolved, bytes)
    }

    pub fn create_file_app(
        &self,
        path: &Path,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        let resolved = resolve_for_write(path)?;
        self.create_file_resolved(&resolved, bytes)
    }

    pub fn create_file_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        if scope.host {
            return self.create_file_app(path, bytes);
        }
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_app_checked(scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.create_file_resolved(&resolved, bytes)
    }

    /// Atomically create a file from provider-owned staging without loading
    /// its contents into the service request JSON or an in-memory Vec.
    pub(crate) fn create_file_from_staging_app(
        &self,
        path: &Path,
        staging: &mut File,
        expected_length: u64,
    ) -> Result<ReplaceOutcome, EngineError> {
        let resolved = resolve_for_write(path)?;
        self.create_file_from_staging_resolved(&resolved, staging, expected_length)
    }

    pub(crate) fn create_file_from_staging(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        staging: &mut File,
        expected_length: u64,
    ) -> Result<ReplaceOutcome, EngineError> {
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_checked(owner_id, scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.create_file_from_staging_resolved(&resolved, staging, expected_length)
    }

    pub(crate) fn create_file_from_staging_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
        staging: &mut File,
        expected_length: u64,
    ) -> Result<ReplaceOutcome, EngineError> {
        if scope.host {
            return self.create_file_from_staging_app(path, staging, expected_length);
        }
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_app_checked(scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.create_file_from_staging_resolved(&resolved, staging, expected_length)
    }

    pub(crate) fn authorize_stage_directory_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
    ) -> Result<(), EngineError> {
        if scope.host {
            if !fs::metadata(path)?.is_dir() {
                return Err(EngineError::NotADirectory);
            }
            return Ok(());
        }
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        if !fs::metadata(resolved)?.is_dir() {
            return Err(EngineError::NotADirectory);
        }
        Ok(())
    }

    pub(crate) fn authorize_stage_directory_agent(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
    ) -> Result<(), EngineError> {
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_checked(owner_id, scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        if !fs::metadata(resolved)?.is_dir() {
            return Err(EngineError::NotADirectory);
        }
        Ok(())
    }

    fn create_file_from_staging_resolved(
        &self,
        path: &Path,
        staging: &mut File,
        expected_length: u64,
    ) -> Result<ReplaceOutcome, EngineError> {
        if path.exists() {
            return Err(EngineError::DestinationExists);
        }
        let metadata = staging.metadata()?;
        if !metadata.is_file() || metadata.len() != expected_length || metadata.len() > self.config.max_write_bytes as u64 {
            return Err(if metadata.len() > self.config.max_write_bytes as u64 {
                EngineError::LimitExceeded
            } else {
                EngineError::NotAFile
            });
        }
        let parent = path.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "missing destination parent")
        })?;
        let temporary = temporary_path(
            parent,
            path.file_name().and_then(|name| name.to_str()).unwrap_or("file"),
        );
        let result = (|| {
            staging.seek(SeekFrom::Start(0))?;
            let mut output_options = OpenOptions::new();
            output_options.write(true).create_new(true);
            #[cfg(unix)]
            output_options.mode(0o600);
            let mut output = output_options.open(&temporary)?;
            let copied = io::copy(staging, &mut output)?;
            if copied > self.config.max_write_bytes as u64 {
                return Err(io::Error::new(io::ErrorKind::FileTooLarge, "staged file exceeds write limit"));
            }
            output.sync_all()?;
            drop(output);
            fs::rename(&temporary, path)?;
            Ok::<(), io::Error>(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result.map_err(EngineError::from)?;
        self.clear_sorted_snapshot_cache();
        Ok(ReplaceOutcome::Created { fingerprint: fingerprint_file(path)? })
    }

    fn create_file_resolved(
        &self,
        path: &Path,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        if path.exists() {
            return Err(EngineError::DestinationExists);
        }
        self.replace_if_fingerprint_resolved(path, None, bytes)
    }

    pub fn make_directory(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
    ) -> Result<PathBuf, EngineError> {
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_checked(owner_id, scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.make_directory_resolved(&resolved)
    }

    pub fn make_directory_app(&self, path: &Path) -> Result<PathBuf, EngineError> {
        let resolved = resolve_for_write(path)?;
        self.make_directory_resolved(&resolved)
    }

    pub fn make_directory_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
    ) -> Result<PathBuf, EngineError> {
        if scope.host {
            return self.make_directory_app(path);
        }
        let requested = BTreeSet::from([Capability::Write]);
        self.authorize_app_checked(scope, path, &requested, true)?;
        let resolved = resolve_for_write(path)?;
        self.make_directory_resolved(&resolved)
    }

    fn make_directory_resolved(&self, path: &Path) -> Result<PathBuf, EngineError> {
        if path.exists() {
            return Err(EngineError::DestinationExists);
        }
        fs::create_dir(path)?;
        self.clear_sorted_snapshot_cache();
        Ok(path.to_path_buf())
    }

    fn replace_if_fingerprint_resolved(
        &self,
        path: &Path,
        expected: Option<&Fingerprint>,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        if bytes.len() > self.config.max_write_bytes {
            return Err(EngineError::LimitExceeded);
        }
        let current = path.exists().then(|| fingerprint_file(path)).transpose()?;
        if expected != current.as_ref() {
            if expected.is_some() || current.is_some() {
                return Err(EngineError::Conflict);
            }
        }
        let ticket = self.prepare_capture(
            if current.is_some() {
                "replace"
            } else {
                "create"
            },
            vec![CaptureTarget {
                resource_id: path.to_path_buf(),
                before: current.as_ref().map(|_| fs::read(path)).transpose()?,
            }],
        );
        let parent = path
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing parent"))?;
        let temp_path = temporary_path(
            parent,
            path.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("file"),
        );
        let existing_mode = file_mode(path);
        let mut temp = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp_path)?;
        let write_result = (|| {
            temp.write_all(bytes)?;
            apply_file_mode(&temp, existing_mode)?;
            temp.sync_all()?;
            drop(temp);
            fs::rename(&temp_path, path)?;
            Ok::<(), io::Error>(())
        })();
        if write_result.is_err() {
            let _ = fs::remove_file(&temp_path);
        }
        if let Err(error) = write_result {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error.into());
        }
        let fingerprint = fingerprint_file(path)?;
        self.clear_sorted_snapshot_cache();
        match (ticket, fs::read(path)) {
            (Some(ticket), Ok(after)) => Self::finish_capture(
                Some(ticket),
                vec![CaptureAfter {
                    resource_id: path.to_path_buf(),
                    after: Some(after),
                }],
            ),
            (Some(ticket), Err(_)) => ticket.abort(),
            (None, _) => {}
        }
        Ok(if current.is_some() {
            ReplaceOutcome::Replaced { fingerprint }
        } else {
            ReplaceOutcome::Created { fingerprint }
        })
    }

    pub fn copy_file(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        source: &Path,
        destination: &Path,
    ) -> Result<CopyOutcome, EngineError> {
        let read = BTreeSet::from([Capability::Read]);
        let write = BTreeSet::from([Capability::Write]);
        self.authorize_checked(owner_id, scope, source, &read, false)?;
        self.authorize_checked(owner_id, scope, destination, &write, true)?;
        let source = resolve_existing_path(source)?;
        let destination = resolve_for_write(destination)?;
        self.copy_file_resolved(&source, &destination)
    }

    pub fn copy_file_app(
        &self,
        source: &Path,
        destination: &Path,
    ) -> Result<CopyOutcome, EngineError> {
        let source = resolve_existing_path(source)?;
        let destination = resolve_for_write(destination)?;
        self.copy_file_resolved(&source, &destination)
    }

    pub fn copy_file_scoped(
        &self,
        scope: &AppScope,
        source: &Path,
        destination: &Path,
    ) -> Result<CopyOutcome, EngineError> {
        if scope.host {
            return self.copy_file_app(source, destination);
        }
        let read = BTreeSet::from([Capability::Read]);
        let write = BTreeSet::from([Capability::Write]);
        self.authorize_app_checked(scope, source, &read, false)?;
        self.authorize_app_checked(scope, destination, &write, true)?;
        let source = resolve_existing_path(source)?;
        let destination = resolve_for_write(destination)?;
        self.copy_file_resolved(&source, &destination)
    }

    fn copy_file_resolved(
        &self,
        source: &Path,
        destination: &Path,
    ) -> Result<CopyOutcome, EngineError> {
        if destination.exists() {
            return Err(EngineError::DestinationExists);
        }
        if !fs::metadata(source)?.is_file() {
            return Err(EngineError::NotAFile);
        }
        let ticket = self.prepare_capture(
            "copy",
            vec![
                CaptureTarget {
                    resource_id: source.to_path_buf(),
                    before: Some(fs::read(source)?),
                },
                CaptureTarget {
                    resource_id: destination.to_path_buf(),
                    before: None,
                },
            ],
        );
        let parent = destination.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "missing destination parent")
        })?;
        let temporary = temporary_path(
            parent,
            destination
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("file"),
        );
        let result = (|| {
            fs::copy(source, &temporary)?;
            let source_mode = file_mode(source);
            let temporary_file = OpenOptions::new().write(true).open(&temporary)?;
            apply_file_mode(&temporary_file, source_mode)?;
            temporary_file.sync_all()?;
            fs::rename(&temporary, destination)?;
            Ok::<(), io::Error>(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        if let Err(error) = result {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error.into());
        }
        self.clear_sorted_snapshot_cache();
        match (
            ticket,
            fs::read(source).and_then(|source_after| {
                fs::read(destination).map(|destination_after| (source_after, destination_after))
            }),
        ) {
            (Some(ticket), Ok((source_after, destination_after))) => Self::finish_capture(
                Some(ticket),
                vec![
                    CaptureAfter {
                        resource_id: source.to_path_buf(),
                        after: Some(source_after),
                    },
                    CaptureAfter {
                        resource_id: destination.to_path_buf(),
                        after: Some(destination_after),
                    },
                ],
            ),
            (Some(ticket), Err(_)) => ticket.abort(),
            (None, _) => {}
        }
        Ok(CopyOutcome {
            source: source.to_path_buf(),
            destination: destination.to_path_buf(),
            fingerprint: fingerprint_file(destination)?,
        })
    }

    pub fn move_file(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        source: &Path,
        destination: &Path,
    ) -> Result<(), EngineError> {
        self.move_file_if_fingerprint(owner_id, scope, source, destination, None)
    }

    pub fn move_file_if_fingerprint(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        source: &Path,
        destination: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        // Moving/renaming removes the source directory entry, so the source
        // requires mutation authority as well as the destination. Read-only
        // sources may be copied, never moved out of their assigned location.
        let write = BTreeSet::from([Capability::Write]);
        self.authorize_checked(owner_id, scope, source, &write, false)?;
        self.authorize_checked(owner_id, scope, destination, &write, true)?;
        let source = resolve_existing_path(source)?;
        let destination = resolve_for_write(destination)?;
        self.move_file_resolved(&source, &destination, expected)
    }

    pub fn move_file_app(&self, source: &Path, destination: &Path) -> Result<(), EngineError> {
        self.move_file_app_if_fingerprint(source, destination, None)
    }

    pub fn move_file_app_if_fingerprint(
        &self,
        source: &Path,
        destination: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        let source = resolve_existing_path(source)?;
        let destination = resolve_for_write(destination)?;
        self.move_file_resolved(&source, &destination, expected)
    }

    pub fn move_file_scoped(
        &self,
        scope: &AppScope,
        source: &Path,
        destination: &Path,
    ) -> Result<(), EngineError> {
        self.move_file_scoped_if_fingerprint(scope, source, destination, None)
    }

    pub fn move_file_scoped_if_fingerprint(
        &self,
        scope: &AppScope,
        source: &Path,
        destination: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        if scope.host {
            return self.move_file_app_if_fingerprint(source, destination, expected);
        }
        let write = BTreeSet::from([Capability::Write]);
        self.authorize_app_checked(scope, source, &write, false)?;
        self.authorize_app_checked(scope, destination, &write, true)?;
        let source = resolve_existing_path(source)?;
        let destination = resolve_for_write(destination)?;
        self.move_file_resolved(&source, &destination, expected)
    }

    /// Rename is the same atomic same-volume operation as move, but remains a
    /// distinct semantic entry point for clients and audit records.
    pub fn rename_file(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        source: &Path,
        destination: &Path,
    ) -> Result<(), EngineError> {
        self.move_file(owner_id, scope, source, destination)
    }

    pub fn rename_file_app(&self, source: &Path, destination: &Path) -> Result<(), EngineError> {
        self.move_file_app(source, destination)
    }

    pub fn rename_file_scoped(
        &self,
        scope: &AppScope,
        source: &Path,
        destination: &Path,
    ) -> Result<(), EngineError> {
        self.move_file_scoped(scope, source, destination)
    }

    fn move_file_resolved(
        &self,
        source: &Path,
        destination: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        if destination.exists() {
            return Err(EngineError::DestinationExists);
        }
        if let Some(expected) = expected {
            if &fingerprint_file(source)? != expected {
                return Err(EngineError::Conflict);
            }
        }
        let ticket = self.prepare_capture(
            "move",
            vec![
                CaptureTarget {
                    resource_id: source.to_path_buf(),
                    before: Some(fs::read(source)?),
                },
                CaptureTarget {
                    resource_id: destination.to_path_buf(),
                    before: None,
                },
            ],
        );
        let result = fs::rename(source, destination).map_err(|error| {
            if error.raw_os_error() == Some(18) {
                EngineError::CrossVolume
            } else {
                EngineError::Io(error)
            }
        });
        if let Err(error) = result {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error);
        }
        self.clear_sorted_snapshot_cache();
        match (ticket, fs::read(destination)) {
            (Some(ticket), Ok(destination_after)) => Self::finish_capture(
                Some(ticket),
                vec![
                    CaptureAfter {
                        resource_id: source.to_path_buf(),
                        after: None,
                    },
                    CaptureAfter {
                        resource_id: destination.to_path_buf(),
                        after: Some(destination_after),
                    },
                ],
            ),
            (Some(ticket), Err(_)) => ticket.abort(),
            (None, _) => {}
        }
        Ok(())
    }

    pub fn trash_file(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
    ) -> Result<TrashEntry, EngineError> {
        self.trash_file_if_fingerprint(owner_id, scope, path, None)
    }

    pub fn trash_file_if_fingerprint(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<TrashEntry, EngineError> {
        let write = BTreeSet::from([Capability::Write]);
        let root = self.authorize_checked(owner_id, scope, path, &write, false)?;
        let resolved = resolve_existing_path(path)?;
        let root_path = lexical_normalize(Path::new(&root.canonical_path));
        if root.kind == crate::RootKind::ExactFile || root_path == resolved {
            return Err(EngineError::CannotTrashRoot);
        }
        self.trash_file_resolved(&resolved, &root.id, &root_path, expected)
    }

    pub fn restore_file(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        entry: &TrashEntry,
    ) -> Result<(), EngineError> {
        self.restore_file_if_fingerprint(owner_id, scope, entry, None)
    }

    pub fn restore_file_if_fingerprint(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        entry: &TrashEntry,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        let write = BTreeSet::from([Capability::Write]);
        let root = self.authorize_checked(owner_id, scope, &entry.original_path, &write, true)?;
        if root.id != entry.root_id
            || !entry
                .trashed_path
                .starts_with(Path::new(&root.canonical_path).join(".odysseus-trash"))
            || !entry.trashed_path.exists()
        {
            return Err(EngineError::InvalidTrashEntry);
        }
        if entry.original_path.exists() {
            return Err(EngineError::DestinationExists);
        }
        if let Some(expected) = expected {
            if &fingerprint_file(&entry.trashed_path)? != expected {
                return Err(EngineError::Conflict);
            }
        }
        let ticket = self.prepare_capture(
            "restore",
            vec![
                CaptureTarget {
                    resource_id: entry.trashed_path.clone(),
                    before: Some(fs::read(&entry.trashed_path)?),
                },
                CaptureTarget {
                    resource_id: entry.original_path.clone(),
                    before: None,
                },
            ],
        );
        if let Err(error) = fs::rename(&entry.trashed_path, &entry.original_path) {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error.into());
        }
        self.clear_sorted_snapshot_cache();
        match (ticket, fs::read(&entry.original_path)) {
            (Some(ticket), Ok(after)) => Self::finish_capture(
                Some(ticket),
                vec![
                    CaptureAfter {
                        resource_id: entry.trashed_path.clone(),
                        after: None,
                    },
                    CaptureAfter {
                        resource_id: entry.original_path.clone(),
                        after: Some(after),
                    },
                ],
            ),
            (Some(ticket), Err(_)) => ticket.abort(),
            (None, _) => {}
        }
        Ok(())
    }

    pub fn trash_file_app(&self, path: &Path) -> Result<TrashEntry, EngineError> {
        self.trash_file_app_if_fingerprint(path, None)
    }

    pub fn trash_file_app_if_fingerprint(
        &self,
        path: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<TrashEntry, EngineError> {
        let resolved = resolve_existing_path(path)?;
        let parent = resolved
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing trash parent"))?;
        self.trash_file_resolved(&resolved, "app-host", parent, expected)
    }

    pub fn trash_file_scoped(
        &self,
        scope: &AppScope,
        path: &Path,
    ) -> Result<TrashEntry, EngineError> {
        self.trash_file_scoped_if_fingerprint(scope, path, None)
    }

    pub fn trash_file_scoped_if_fingerprint(
        &self,
        scope: &AppScope,
        path: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<TrashEntry, EngineError> {
        if scope.host {
            return self.trash_file_app_if_fingerprint(path, expected);
        }
        let requested = BTreeSet::from([Capability::Write]);
        let root = self.authorize_app_checked(scope, path, &requested, false)?;
        let resolved = resolve_existing_path(path)?;
        let root_path = lexical_normalize(Path::new(&root.canonical_path));
        if root.kind == RootKind::ExactFile || resolved == root_path {
            return Err(EngineError::CannotTrashRoot);
        }
        self.trash_file_resolved(&resolved, &root.id, &root_path, expected)
    }

    fn trash_file_resolved(
        &self,
        resolved: &Path,
        root_id: &str,
        trash_parent: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<TrashEntry, EngineError> {
        let fingerprint = fingerprint_file(resolved)?;
        if expected.is_some_and(|value| value != &fingerprint) {
            return Err(EngineError::Conflict);
        }
        let trash_dir = trash_parent.join(".odysseus-trash");
        fs::create_dir_all(&trash_dir)?;
        let id = format!("trash-{}", unique_nonce());
        let trashed_path = trash_dir.join(&id);
        let ticket = self.prepare_capture(
            "trash",
            vec![CaptureTarget {
                resource_id: resolved.to_path_buf(),
                before: Some(fs::read(resolved)?),
            }],
        );
        if let Err(error) = fs::rename(resolved, &trashed_path).map_err(EngineError::Io) {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error);
        }
        self.clear_sorted_snapshot_cache();
        Self::finish_capture(
            ticket,
            vec![CaptureAfter {
                resource_id: resolved.to_path_buf(),
                after: None,
            }],
        );
        Ok(TrashEntry {
            id,
            root_id: root_id.to_string(),
            original_path: resolved.to_path_buf(),
            trashed_path,
            fingerprint: Some(fingerprint.value),
        })
    }

    pub fn restore_file_app(&self, entry: &TrashEntry) -> Result<(), EngineError> {
        self.restore_file_app_if_fingerprint(entry, None)
    }

    pub fn restore_file_app_if_fingerprint(
        &self,
        entry: &TrashEntry,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        if entry.root_id != "app-host" {
            return Err(EngineError::InvalidTrashEntry);
        }
        let parent = entry
            .original_path
            .parent()
            .ok_or(EngineError::InvalidTrashEntry)?;
        self.restore_file_resolved(entry, parent, expected)
    }

    pub fn restore_file_scoped(
        &self,
        scope: &AppScope,
        entry: &TrashEntry,
    ) -> Result<(), EngineError> {
        self.restore_file_scoped_if_fingerprint(scope, entry, None)
    }

    pub fn restore_file_scoped_if_fingerprint(
        &self,
        scope: &AppScope,
        entry: &TrashEntry,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        if scope.host {
            return self.restore_file_app_if_fingerprint(entry, expected);
        }
        let requested = BTreeSet::from([Capability::Write]);
        let root = self.authorize_app_checked(scope, &entry.original_path, &requested, true)?;
        if root.id != entry.root_id {
            return Err(EngineError::InvalidTrashEntry);
        }
        self.restore_file_resolved(entry, Path::new(&root.canonical_path), expected)
    }

    fn restore_file_resolved(
        &self,
        entry: &TrashEntry,
        root_path: &Path,
        expected: Option<&Fingerprint>,
    ) -> Result<(), EngineError> {
        let trash_dir = root_path.join(".odysseus-trash");
        if !component_contains(&trash_dir, &entry.trashed_path) || !entry.trashed_path.exists() {
            return Err(EngineError::InvalidTrashEntry);
        }
        if entry.original_path.exists() {
            return Err(EngineError::DestinationExists);
        }
        if let Some(expected) = expected {
            if &fingerprint_file(&entry.trashed_path)? != expected {
                return Err(EngineError::Conflict);
            }
        }
        let parent = entry
            .original_path
            .parent()
            .ok_or(EngineError::InvalidTrashEntry)?;
        if !component_contains(root_path, parent) {
            return Err(EngineError::InvalidTrashEntry);
        }
        let ticket = self.prepare_capture(
            "restore",
            vec![
                CaptureTarget {
                    resource_id: entry.trashed_path.clone(),
                    before: Some(fs::read(&entry.trashed_path)?),
                },
                CaptureTarget {
                    resource_id: entry.original_path.clone(),
                    before: None,
                },
            ],
        );
        if let Err(error) = fs::rename(&entry.trashed_path, &entry.original_path) {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error.into());
        }
        self.clear_sorted_snapshot_cache();
        match (ticket, fs::read(&entry.original_path)) {
            (Some(ticket), Ok(after)) => Self::finish_capture(
                Some(ticket),
                vec![
                    CaptureAfter {
                        resource_id: entry.trashed_path.clone(),
                        after: None,
                    },
                    CaptureAfter {
                        resource_id: entry.original_path.clone(),
                        after: Some(after),
                    },
                ],
            ),
            (Some(ticket), Err(_)) => ticket.abort(),
            (None, _) => {}
        }
        Ok(())
    }

    pub fn filename_search(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, root, &requested, false)?;
        filename_search_walk(root, options, cancelled)
    }

    pub fn filename_search_app(
        &self,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        let resolved = resolve_existing_path(root)?;
        filename_search_walk(&resolved, options, cancelled)
    }

    pub fn filename_search_scoped(
        &self,
        scope: &AppScope,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        if scope.host {
            return self.filename_search_app(root, options, cancelled);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, root, &requested, false)?;
        filename_search_walk(root, options, cancelled)
    }

    pub fn content_search(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_checked(owner_id, scope, root, &requested, false)?;
        content_search_walk(root, options, cancelled)
    }

    pub fn content_search_app(
        &self,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        let resolved = resolve_existing_path(root)?;
        content_search_walk(&resolved, options, cancelled)
    }

    pub fn content_search_scoped(
        &self,
        scope: &AppScope,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        if scope.host {
            return self.content_search_app(root, options, cancelled);
        }
        let requested = BTreeSet::from([Capability::Read]);
        self.authorize_app_checked(scope, root, &requested, false)?;
        content_search_walk(root, options, cancelled)
    }
}

fn filename_search_walk(
    root: &Path,
    options: &SearchOptions,
    cancelled: Option<&AtomicBool>,
) -> Result<SearchResult, EngineError> {
    let mut pending = vec![(root.to_path_buf(), 0_usize)];
    let mut matches = Vec::new();
    let mut work = WorkCost::default();
    let needle = if options.case_sensitive {
        options.query.clone()
    } else {
        options.query.to_lowercase()
    };
    while let Some((path, depth)) = pending.pop() {
        check_cancelled(cancelled)?;
        if work.entries_visited >= options.max_entries {
            return Ok(SearchResult {
                matches,
                complete: false,
                work,
            });
        }
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.is_file() {
            work.entries_visited += 1;
            if file_name_matches(&path, &needle, options.case_sensitive) {
                matches.push(path);
                if matches.len() >= options.max_results {
                    return Ok(SearchResult {
                        matches,
                        complete: false,
                        work,
                    });
                }
            }
            continue;
        }
        if !metadata.is_dir() || depth >= options.max_depth {
            continue;
        }
        for entry in fs::read_dir(path)? {
            check_cancelled(cancelled)?;
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if !options.include_hidden && name.starts_with('.') {
                continue;
            }
            pending.push((entry.path(), depth + 1));
        }
    }
    Ok(SearchResult {
        matches,
        complete: true,
        work,
    })
}

fn content_search_walk(
    root: &Path,
    options: &SearchOptions,
    cancelled: Option<&AtomicBool>,
) -> Result<SearchResult, EngineError> {
    let mut pending = vec![(root.to_path_buf(), 0_usize)];
    let mut matches = Vec::new();
    let mut work = WorkCost::default();
    let needle = if options.case_sensitive {
        options.query.as_bytes().to_vec()
    } else {
        options.query.to_lowercase().into_bytes()
    };
    while let Some((path, depth)) = pending.pop() {
        check_cancelled(cancelled)?;
        if work.entries_visited >= options.max_entries {
            return Ok(SearchResult {
                matches,
                complete: false,
                work,
            });
        }
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.is_file() {
            work.entries_visited += 1;
            let file = File::open(&path)?;
            let mut body = Vec::new();
            file.take(options.max_bytes_per_file as u64)
                .read_to_end(&mut body)?;
            work.bytes_read += body.len() as u64;
            let haystack = if options.case_sensitive {
                body
            } else {
                String::from_utf8_lossy(&body).to_lowercase().into_bytes()
            };
            if !needle.is_empty()
                && haystack
                    .windows(needle.len())
                    .any(|window| window == needle)
            {
                matches.push(path);
                if matches.len() >= options.max_results {
                    return Ok(SearchResult {
                        matches,
                        complete: false,
                        work,
                    });
                }
            }
            continue;
        }
        if !metadata.is_dir() || depth >= options.max_depth {
            continue;
        }
        for entry in fs::read_dir(path)? {
            check_cancelled(cancelled)?;
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if !options.include_hidden && name.starts_with('.') {
                continue;
            }
            pending.push((entry.path(), depth + 1));
        }
    }
    Ok(SearchResult {
        matches,
        complete: true,
        work,
    })
}

fn directory_generation(metadata: &fs::Metadata) -> u64 {
    let modified = metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| {
            let nanos = duration.as_nanos();
            (nanos as u64) ^ ((nanos >> 64) as u64).rotate_left(23)
        })
        .unwrap_or_default();
    let mut generation = modified ^ metadata.len().rotate_left(17);
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        generation ^= (metadata.ctime() as u64).rotate_left(7);
        generation ^= (metadata.ctime_nsec() as u64).rotate_left(29);
        generation ^= metadata.dev().rotate_left(37);
        generation ^= metadata.ino().rotate_left(47);
    }
    generation
}

fn directory_path_key(path: &Path) -> String {
    let mut hasher = Sha256::new();
    hasher.update(path.to_string_lossy().as_bytes());
    hasher
        .finalize()
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn directory_snapshot_identity(entries: &[DirectoryEntry]) -> String {
    let mut hasher = Sha256::new();
    for entry in entries {
        hasher.update(entry.name.as_bytes());
        hasher.update([0]);
        hasher.update(kind_label(entry.kind).as_bytes());
        hasher.update([0]);
        hasher.update(entry.size.to_le_bytes());
        hasher.update(entry.modified_unix_ms.unwrap_or(u64::MAX).to_le_bytes());
    }
    hasher
        .finalize()
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn system_time_ms(value: Option<SystemTime>) -> Option<u64> {
    value
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_millis() as u64)
}

fn fingerprint_file(path: &Path) -> Result<Fingerprint, io::Error> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    let size = file.metadata()?.len();
    Ok(Fingerprint {
        algorithm: "sha256",
        value: format!("sha256:{:x}:{}", hasher.finalize(), size),
    })
}

fn fingerprint_bytes(bytes: &[u8]) -> Fingerprint {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    Fingerprint {
        algorithm: "sha256",
        value: format!("sha256:{:x}:{}", hasher.finalize(), bytes.len()),
    }
}

fn detect_encoding(raw: &[u8]) -> (TextEncoding, usize, &[u8]) {
    if raw.starts_with(&[0xef, 0xbb, 0xbf]) {
        (TextEncoding::Utf8, 3, &raw[3..])
    } else if raw.starts_with(&[0xff, 0xfe, 0x00, 0x00]) {
        (TextEncoding::Utf32Le, 4, &raw[4..])
    } else if raw.starts_with(&[0x00, 0x00, 0xfe, 0xff]) {
        (TextEncoding::Utf32Be, 4, &raw[4..])
    } else if raw.starts_with(&[0xff, 0xfe]) {
        (TextEncoding::Utf16Le, 2, &raw[2..])
    } else if raw.starts_with(&[0xfe, 0xff]) {
        (TextEncoding::Utf16Be, 2, &raw[2..])
    } else {
        (TextEncoding::Utf8, 0, raw)
    }
}

fn decode_text(encoding: TextEncoding, payload: &[u8]) -> Option<String> {
    match encoding {
        TextEncoding::Utf8 => String::from_utf8(payload.to_vec()).ok(),
        TextEncoding::Utf16Le | TextEncoding::Utf16Be => {
            if payload.len() % 2 != 0 {
                return None;
            }
            let words = payload
                .chunks_exact(2)
                .map(|chunk| match encoding {
                    TextEncoding::Utf16Le => u16::from_le_bytes([chunk[0], chunk[1]]),
                    TextEncoding::Utf16Be => u16::from_be_bytes([chunk[0], chunk[1]]),
                    _ => unreachable!(),
                })
                .collect::<Vec<_>>();
            String::from_utf16(&words).ok()
        }
        TextEncoding::Utf32Le | TextEncoding::Utf32Be => {
            if payload.len() % 4 != 0 {
                return None;
            }
            let mut text = String::new();
            for chunk in payload.chunks_exact(4) {
                let codepoint = match encoding {
                    TextEncoding::Utf32Le => {
                        u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]])
                    }
                    TextEncoding::Utf32Be => {
                        u32::from_be_bytes([chunk[0], chunk[1], chunk[2], chunk[3]])
                    }
                    _ => unreachable!(),
                };
                text.push(char::from_u32(codepoint)?);
            }
            Some(text)
        }
    }
}

#[derive(Clone, Copy)]
enum PreviewChunkEdge {
    Whole,
    Head,
    Tail,
}

fn encoding_code_unit_bytes(encoding: TextEncoding) -> usize {
    match encoding {
        TextEncoding::Utf8 => 1,
        TextEncoding::Utf16Le | TextEncoding::Utf16Be => 2,
        TextEncoding::Utf32Le | TextEncoding::Utf32Be => 4,
    }
}

fn align_down(value: usize, unit: usize) -> usize {
    value - (value % unit.max(1))
}

fn align_up_from_origin(value: u64, origin: u64, unit: u64) -> u64 {
    if value <= origin || unit <= 1 {
        return value.max(origin);
    }
    let relative = value - origin;
    let remainder = relative % unit;
    if remainder == 0 {
        value
    } else {
        value.saturating_add(unit - remainder)
    }
}

fn decode_preview_chunk(
    encoding: TextEncoding,
    payload: &[u8],
    edge: PreviewChunkEdge,
) -> Option<String> {
    match encoding {
        TextEncoding::Utf8 => {
            let mut bytes = payload;
            if matches!(edge, PreviewChunkEdge::Tail) {
                let leading_continuations = bytes
                    .iter()
                    .take(3)
                    .take_while(|byte| **byte & 0xc0 == 0x80)
                    .count();
                bytes = &bytes[leading_continuations..];
            }
            match std::str::from_utf8(bytes) {
                Ok(value) => Some(value.to_owned()),
                Err(error)
                    if matches!(edge, PreviewChunkEdge::Head) && error.error_len().is_none() =>
                {
                    std::str::from_utf8(&bytes[..error.valid_up_to()])
                        .ok()
                        .map(str::to_owned)
                }
                Err(_) => None,
            }
        }
        TextEncoding::Utf16Le | TextEncoding::Utf16Be => {
            if payload.len() % 2 != 0 {
                return None;
            }
            let mut words = payload
                .chunks_exact(2)
                .map(|chunk| match encoding {
                    TextEncoding::Utf16Le => u16::from_le_bytes([chunk[0], chunk[1]]),
                    TextEncoding::Utf16Be => u16::from_be_bytes([chunk[0], chunk[1]]),
                    _ => unreachable!(),
                })
                .collect::<Vec<_>>();
            if matches!(edge, PreviewChunkEdge::Tail)
                && words
                    .first()
                    .is_some_and(|word| (0xdc00..=0xdfff).contains(word))
            {
                words.remove(0);
            }
            if matches!(edge, PreviewChunkEdge::Head)
                && words
                    .last()
                    .is_some_and(|word| (0xd800..=0xdbff).contains(word))
            {
                words.pop();
            }
            String::from_utf16(&words).ok()
        }
        TextEncoding::Utf32Le | TextEncoding::Utf32Be => decode_text(encoding, payload),
    }
}

fn sampled_text_is_binary(text: &str) -> bool {
    text.chars().any(|character| {
        character == '\0'
            || (character.is_ascii_control()
                && !matches!(character, '\n' | '\r' | '\t' | '\u{000c}' | '\u{001b}'))
    })
}

fn encode_text(encoding: TextEncoding, bom: bool, text: &str) -> Option<Vec<u8>> {
    let mut bytes = Vec::new();
    if bom {
        bytes.extend_from_slice(match encoding {
            TextEncoding::Utf8 => &[0xef, 0xbb, 0xbf],
            TextEncoding::Utf16Le => &[0xff, 0xfe],
            TextEncoding::Utf16Be => &[0xfe, 0xff],
            TextEncoding::Utf32Le => &[0xff, 0xfe, 0x00, 0x00],
            TextEncoding::Utf32Be => &[0x00, 0x00, 0xfe, 0xff],
        });
    }
    match encoding {
        TextEncoding::Utf8 => bytes.extend_from_slice(text.as_bytes()),
        TextEncoding::Utf16Le | TextEncoding::Utf16Be => {
            for word in text.encode_utf16() {
                let encoded = if encoding == TextEncoding::Utf16Le {
                    word.to_le_bytes()
                } else {
                    word.to_be_bytes()
                };
                bytes.extend_from_slice(&encoded);
            }
        }
        TextEncoding::Utf32Le | TextEncoding::Utf32Be => {
            for character in text.chars() {
                let encoded = if encoding == TextEncoding::Utf32Le {
                    (character as u32).to_le_bytes()
                } else {
                    (character as u32).to_be_bytes()
                };
                bytes.extend_from_slice(&encoded);
            }
        }
    }
    Some(bytes)
}

fn dominant_newline(text: &str) -> String {
    let crlf = text.matches("\r\n").count();
    let bare_lf = text.matches('\n').count().saturating_sub(crlf);
    let bare_cr = text.matches('\r').count().saturating_sub(crlf);
    if crlf >= bare_lf && crlf >= bare_cr && crlf > 0 {
        "\r\n".into()
    } else if bare_cr > bare_lf {
        "\r".into()
    } else {
        "\n".into()
    }
}

fn preserve_newline_style(text: &str, newline: &str) -> String {
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    if newline == "\n" {
        normalized
    } else {
        normalized.replace('\n', newline)
    }
}

#[cfg(unix)]
fn file_mode(path: &Path) -> Option<u32> {
    use std::os::unix::fs::PermissionsExt;
    fs::symlink_metadata(path)
        .ok()
        .map(|metadata| metadata.permissions().mode())
}

#[cfg(not(unix))]
fn file_mode(_path: &Path) -> Option<u32> {
    None
}

#[cfg(unix)]
fn apply_file_mode(file: &File, mode: Option<u32>) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    if let Some(mode) = mode {
        file.set_permissions(fs::Permissions::from_mode(mode))?;
    }
    Ok(())
}

#[cfg(not(unix))]
fn apply_file_mode(_file: &File, _mode: Option<u32>) -> io::Result<()> {
    Ok(())
}

fn temporary_path(parent: &Path, base: &str) -> PathBuf {
    parent.join(format!(
        ".{base}.odysseus-tmp-{}-{}",
        std::process::id(),
        unique_nonce()
    ))
}

fn unique_nonce() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or_default()
}

fn resolve_existing_path(path: &Path) -> Result<PathBuf, EngineError> {
    fs::canonicalize(path).map_err(EngineError::Io)
}

fn resolve_for_write(path: &Path) -> Result<PathBuf, EngineError> {
    match fs::canonicalize(path) {
        Ok(resolved) => Ok(resolved),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let parent = path.parent().ok_or(error)?;
            let parent = fs::canonicalize(parent)?;
            let name = path.file_name().ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "missing final path component")
            })?;
            Ok(parent.join(name))
        }
        Err(error) => Err(EngineError::Io(error)),
    }
}

fn check_cancelled(cancelled: Option<&AtomicBool>) -> Result<(), EngineError> {
    if cancelled.is_some_and(|value| value.load(AtomicOrdering::Relaxed)) {
        Err(EngineError::Cancelled)
    } else {
        Ok(())
    }
}

fn file_name_matches(path: &Path, needle: &str, case_sensitive: bool) -> bool {
    let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
        return false;
    };
    if case_sensitive {
        name.contains(needle)
    } else {
        name.to_lowercase().contains(needle)
    }
}
