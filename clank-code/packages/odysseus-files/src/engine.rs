#[cfg(target_os = "linux")]
pub(crate) mod checkout;
#[cfg(target_os = "linux")]
pub(crate) mod worktree_stage;
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
use std::time::{Instant, SystemTime, UNIX_EPOCH};

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

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckpointSelection {
    pub resource_id: String,
    pub expected_fingerprint: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckpointBinding {
    pub resource_id: String,
    pub root_id: String,
    pub workspace_id: String,
    pub registry_generation: u64,
    pub relative_path: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub staged_upload_id: String,
    #[serde(skip)]
    pub canonical_root: PathBuf,
}

pub trait CheckpointTicket: Send {
    fn bindings(&self) -> &[CheckpointBinding];
    fn stage(&mut self, index: usize, bytes: &[u8], deadline: std::time::Instant) -> Result<(), String>;
    fn finalize(self: Box<Self>, started_millis: u64, finished_millis: u64, deadline: std::time::Instant) -> Result<serde_json::Value, String>;
    fn fail(self: Box<Self>);
}

pub trait MutationCaptureTicket: Send {
    fn action_id(&self) -> &str;
    fn complete(self: Box<Self>, after: Vec<CaptureAfter>) -> Result<(), String>;
    fn abort(self: Box<Self>);
    /// The live write committed, but its recovery copy could not be read.
    fn after_unavailable(self: Box<Self>, _error: String) {}
}

/// Server-owned identity for one original regular UTF-8 replacement.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OrdinaryWriteCapture {
    pub operation_id: String,
    pub action_id: String,
    pub request_digest: String,
    pub timestamp_millis: u64,
    pub expected_fingerprint: String,
    pub desired_sha256: String,
    pub desired_size: u64,
}

/// Exact original create identity. Parent revision is checked before any
/// owned mutation; the native owner retains the physical parent descriptor.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OrdinaryCreateCapture {
    pub operation_id: String,
    pub action_id: String,
    pub request_digest: String,
    pub timestamp_millis: u64,
    pub parent_revision: String,
    pub desired_sha256: String,
    pub desired_size: u64,
}

/// Immutable Source plan captured from the original managed journal.
#[derive(Clone,Debug)]
pub struct WorktreeCaptureBinding {pub operation_id:String,pub root_id:String,pub commit_oid:String,pub tree_oid:String,pub target_manifest_sha256:String}

/// Immutable values derived only from a retained worktree inverse journal.
#[cfg(target_os="linux")]
#[doc(hidden)]
pub struct WorktreeInverseHistoryBinding {
    pub(crate) action_id: String, pub(crate) actor_id: String, pub(crate) account_id: String,
    pub(crate) root_id: String, pub(crate) operation_id: String, pub(crate) original_action_id: String, pub(crate) original_operation_id: String,
    pub(crate) original_version_id: String, pub(crate) container_sha256: String, pub(crate) container_length: u64,
    pub(crate) resource_key: serde_json::Value, pub(crate) timestamp_millis: u64,
}

#[cfg(target_os="linux")]
#[doc(hidden)]
pub struct WorktreeRedoHistoryBinding {
    pub(crate) action_id:String,pub(crate) actor_id:String,pub(crate) account_id:String,pub(crate) root_id:String,pub(crate) operation_id:String,
    pub(crate) inverse_action_id:String,pub(crate) inverse_version_id:String,pub(crate) container_sha256:String,pub(crate) container_length:u64,
    pub(crate) commit_oid:String,pub(crate) tree_oid:String,pub(crate) target_manifest_sha256:String,pub(crate) resource_key:serde_json::Value,pub(crate) timestamp_millis:u64,
}
#[cfg(target_os="linux")]
#[doc(hidden)]
pub struct WorktreeRedoReadStage<'a> {_journal:&'a ManagedJournal,file:&'a File,binding:&'a WorktreeRedoHistoryBinding}
#[cfg(target_os="linux")]
impl WorktreeRedoReadStage<'_> {
    pub(crate) fn file(&self)->&File{self.file}
    pub(crate) fn binding(&self)->&WorktreeRedoHistoryBinding{self.binding}
}

pub trait MutationCaptureHook: Send + Sync {
    fn legacy_prepare_hook(&self) -> Option<Box<dyn MutationCaptureHook>> { None }
    fn settle_recovery(&self, _action_id: &str) -> Result<(), String> { Ok(()) }
    fn checkpoint_ready(&self) -> bool { false }
    fn ordinary_write_ready(&self) -> bool { false }
    fn graph_capture_active(&self) -> bool { false }
    fn validate_graph_stage(&self, _path: &Path, _digest: &str, _length: u64) -> Result<(), String> { Ok(()) }
    /// Explicit History metadata registration; the caller owns the pinned
    /// Files descriptor and has checked this exact selected root.
    fn checkpoint_root(&self, _root_id: &str) -> Result<PathBuf, String> { Err("selected registration unavailable".into()) }
    fn register_checkpoint_file(&self, _path: &Path, _root_id: &str) -> Result<serde_json::Value, String> {
        Err("selected registration unavailable".into())
    }
    fn claim_checkpoint(&self, _request_id: &str, _root_id: &str, _items: &[CheckpointSelection], _deadline: std::time::Instant) -> Result<Box<dyn CheckpointTicket>, String> {
        Err("selected checkpoints unavailable".into())
    }
    fn prepare(
        &self,
        operation: &str,
        targets: &[CaptureTarget],
    ) -> Result<Box<dyn MutationCaptureTicket>, String>;

    /// The original Host owner persisted this identity before any IPC issue.
    fn prepare_ordinary_write(
        &self, _target: &CaptureTarget, _binding: &OrdinaryWriteCapture, _desired: &[u8],
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        Err("ordinary recoverable capture is unavailable".into())
    }

    fn prepare_ordinary_create(
        &self, _target: &CaptureTarget, _binding: &OrdinaryCreateCapture, _desired: &[u8],
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        Err("ordinary create capture is unavailable".into())
    }

    fn ordinary_create_status(
        &self, _path: &Path, _binding: &OrdinaryCreateCapture,
    ) -> Result<serde_json::Value, String> {
        Err("ordinary create status is unavailable".into())
    }

    /// A private factual lookup; an absent or incomplete action is unknown.
    fn ordinary_write_status(
        &self, _path: &Path, _binding: &OrdinaryWriteCapture,
    ) -> Result<serde_json::Value, String> {
        Err("ordinary recoverable status is unavailable".into())
    }

    fn managed_account_id(&self) -> Result<String, String> { Err("managed account unavailable".into()) }
    fn managed_actor_id(&self) -> Result<String, String> { Err("managed actor unavailable".into()) }
    fn managed_worktree_capture_ready(&self,_root_id:&str,_deadline:std::time::Instant)->Result<bool,String>{Ok(false)}
    #[cfg(target_os="linux")]
    fn lookup_worktree_history_version(&self,_root_id:&str,_action_id:&str,_operation_id:&str,_deadline:std::time::Instant)->Result<String,String>{Err("worktree version unavailable".into())}
    #[cfg(target_os="linux")]
    fn prepare_worktree_redo_history(&self,_stage:&WorktreeRedoReadStage<'_>,_deadline:std::time::Instant)->Result<serde_json::Value,String>{Err("worktree redo capture unavailable".into())}
    #[cfg(target_os="linux")]
    fn prepare_worktree_inverse_history(&self,_binding:&WorktreeInverseHistoryBinding,_deadline:std::time::Instant)->Result<serde_json::Value,String>{Err("worktree inverse capture unavailable".into())}
    #[cfg(target_os="linux")]
    fn read_worktree_inverse_history(&self,_stage:&WorktreeInverseReadStage<'_>,_deadline:std::time::Instant)->Result<(),String>{Err("worktree inverse read unavailable".into())}
    #[cfg(target_os="linux")]
    fn finish_worktree_inverse_history(&self,_stage:&WorktreeInverseFinishStage<'_>,_deadline:std::time::Instant)->Result<(),String>{Err("worktree inverse completion unavailable".into())}
    #[cfg(target_os="linux")]
    fn finish_worktree_history(&self,_stage:&WorktreeHistoryStage<'_>,_live:&serde_json::Value,_deadline:std::time::Instant)->Result<(),String>{Err("worktree history completion unavailable".into())}

    fn managed_authorize_root(&self, _path: &Path) -> Result<(String, PathBuf), String> {
        Err("managed History root is unavailable".into())
    }

    /// Resolve History identities under the already journaled operation; each
    /// stable registration must be checkpointed before the next one begins.
    fn register_recoverable(
        &self, _targets: &[CaptureTarget],
        _checkpoint: &mut dyn FnMut(serde_json::Value) -> Result<(), String>,
    ) -> Result<Vec<serde_json::Value>, String> {
        Err("recoverable batch capture is unavailable".into())
    }

    /// Internal durable coordinator seam. The caller must persist the initial
    /// action plan and check History's remote state before invoking a retry.
    /// Implementations checkpoint identities and the exact request without
    /// storing authentication credentials. Legacy hooks deny by default.
    fn prepare_recoverable(
        &self,
        _operation: &str,
        _targets: &[CaptureTarget],
        _action_id: &str,
        _timestamp_millis: u64,
        _registry_plan_digest: &str,
        _checkpoint: &mut dyn FnMut(serde_json::Value) -> Result<(), String>,
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        Err("recoverable batch capture is unavailable".into())
    }

    fn prepare_worktree_action(&self,_target:&CaptureTarget,_binding:&WorktreeCaptureBinding,_registrations:&[serde_json::Value],_registry_digest:&str,_timestamp_millis:u64,_checkpoint:&mut dyn FnMut(serde_json::Value)->Result<(),String>)->Result<Box<dyn MutationCaptureTicket>,String>{Err("worktree capture unavailable".into())}

    fn register_checkout_targets(&self,_targets:&[CaptureTarget],_root_id:&str,_operation_id:&str,_directories:&[PathBuf],_checkpoint:&mut dyn FnMut(serde_json::Value)->Result<(),String>)->Result<Vec<serde_json::Value>,String>{Err("checkout registration unavailable".into())}
    fn prepare_checkout_targets(&self,_targets:&[CaptureTarget],_directories:&[PathBuf],_registrations:&[serde_json::Value],_action_id:&str,_timestamp_millis:u64,_registry_digest:&str,_checkpoint:&mut dyn FnMut(serde_json::Value)->Result<(),String>)->Result<Box<dyn MutationCaptureTicket>,String>{Err("checkout capture unavailable".into())}

    /// Trusted folder coordinator supplies the two directory targets; this
    /// is never populated from an unauthenticated type hint.
    fn prepare_recoverable_directories(
        &self, _operation: &str, _targets: &[CaptureTarget], _directories: &[PathBuf],
        _registrations: &[serde_json::Value], _action_id: &str, _timestamp_millis: u64, _registry_plan_digest: &str,
        _checkpoint: &mut dyn FnMut(serde_json::Value) -> Result<(), String>,
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        Err("recoverable directory capture is unavailable".into())
    }

    fn register_recoverable_folder(
        &self, _targets: &[CaptureTarget], _source: &Path, _destination: &Path, _operation_id: &str,
        _asset_proofs: &[serde_json::Value],
        _checkpoint: &mut dyn FnMut(serde_json::Value) -> Result<(), String>,
    ) -> Result<Vec<serde_json::Value>, String> {
        Err("recoverable folder registration is unavailable".into())
    }

    fn list_managed_folder_registrations(&self, _source: &Path, _destination: &Path) -> Result<Vec<serde_json::Value>, String> {
        Err("managed folder registrations are unavailable".into())
    }

    fn managed_action_id(&self, _operation_id: &str) -> Result<String, String> {
        Err("recoverable batch capture is unavailable".into())
    }

    /// Read the durable History action; an error is unknown, never evidence
    /// that a prepare or completion did not happen.
    fn recoverable_action(&self, _action_id: &str, _operation_id: &str) -> Result<serde_json::Value, String> {
        Err("recoverable batch capture is unavailable".into())
    }

    fn plan_recoverable_completion(
        &self, _action_id: &str, _prepared: &serde_json::Value, _after: &[CaptureAfter],
    ) -> Result<(serde_json::Value, Vec<(String, Vec<u8>, String)>), String> {
        Err("recoverable batch capture is unavailable".into())
    }

    /// Replay only a previously persisted exact completion. The caller must
    /// hold the operation lock and inspect remote action state first.
    fn complete_recoverable(
        &self, _action_id: &str, _completion: &serde_json::Value,
        _stages: &[(String, Vec<u8>, String)], _already_complete: bool,
    ) -> Result<serde_json::Value, String> {
        Err("recoverable batch capture is unavailable".into())
    }

    fn commit_recoverable_registry(&self, _action_id: &str, _plan: &serde_json::Value) -> Result<(), String> {
        Err("recoverable registry batch is unavailable".into())
    }

    fn validate_recoverable_subtree(&self, _action_id: &str, _plan: &serde_json::Value) -> Result<(), String> {
        Err("recoverable subtree validation is unavailable".into())
    }

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
    /// Filename queries remain substring searches unless explicitly glob.
    pub glob: bool,
    pub file_glob: Option<String>,
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
    pub eligible_files: usize,
    pub partial_reasons: BTreeSet<&'static str>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub locations: Vec<SearchMatch>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SearchMatch {
    pub path: PathBuf,
    pub line: usize,
    pub start_byte: usize,
    pub end_byte: usize,
    pub revision: Fingerprint,
}

#[derive(Default)]
pub(crate) struct SearchDetails {
    pub excludes: Vec<String>,
    pub deadline: Option<Instant>,
    pub locations: bool,
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
    /// Linux directory identity and metadata revision, not a content digest.
    pub directory_revision: Option<String>,
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
    #[error("managed move crosses filesystems")]
    ManagedCrossFilesystem,
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
    #[error("search pattern is invalid or unsupported")]
    InvalidSearchPattern,
    #[error("target is not a directory")]
    NotADirectory,
    #[error("filesystem mutation committed but history reconciliation is pending: {0}")]
    HistoryCapture(String),
    /// A managed undo that cannot be offered; the code is a stable, typed reason the Host can show.
    #[error("managed undo is unavailable: {0}")]
    ManagedUndoUnavailable(&'static str),
}

pub struct FileEngine {
    config: EngineConfig,
    registry: RootRegistry,
    // Sorted pages are a presentation snapshot, never an authority cache.
    // The key includes the canonical directory identity, observed generation,
    // and complete sort signature; mutations invalidate the bounded cache.
    sorted_snapshot_cache: Mutex<SortedSnapshotCache>,
    capture_hook: Option<Arc<dyn MutationCaptureHook>>,
    capture_required: bool,
    managed_journal_root: Option<PathBuf>,
}

/// Private, same-operation durability primitive. This is deliberately not a
/// protocol operation: the managed move remains unavailable until participant
/// authorization, History capture and recovery are all connected.
struct ManagedJournal {
    capture: Option<Arc<dyn MutationCaptureHook>>,
    directory: PathBuf,
    operation_id: String,
    _lock: File,
    exclusive: bool,
}

/// Bound to the original exclusive journal lock; no path-based constructor is
/// available to the History sender. Only a durable worktree completion record
/// may create this handle.
#[cfg(target_os="linux")]
#[doc(hidden)]
pub struct WorktreeHistoryStage<'a> {
    _journal: &'a ManagedJournal, file: File, action_id: String, actor_id: String, account_id: String, root_id: String,
    fingerprint: String, length: u64, identity: serde_json::Value,
}
#[cfg(target_os="linux")]
impl WorktreeHistoryStage<'_> {
    pub(crate) fn file(&self) -> &File { &self.file }
    pub(crate) fn action_id(&self) -> &str { &self.action_id }
    pub(crate) fn actor_id(&self) -> &str { &self.actor_id }
    pub(crate) fn account_id(&self) -> &str { &self.account_id }
    pub(crate) fn root_id(&self) -> &str { &self.root_id }
    pub(crate) fn fingerprint(&self) -> &str { &self.fingerprint }
    pub(crate) fn length(&self) -> u64 { self.length }
    pub(crate) fn verify_until(&self,deadline:std::time::Instant) -> io::Result<()> {
        use std::os::unix::fs::{FileExt, MetadataExt};
        let before=self.file.metadata()?;
        if !before.is_file() || before.nlink()!=1 || before.len()!=self.length || managed_object_identity(&self.file)?!=self.identity {return Err(io::Error::other("worktree stage identity changed"));}
        let mut hash=Sha256::new();let mut offset=0u64;let mut chunk=vec![0u8;128*1024];
        while offset<self.length {
            if std::time::Instant::now()>=deadline{return Err(io::Error::other("worktree stage deadline"));}
            let want=usize::try_from((self.length-offset).min(chunk.len() as u64)).map_err(io::Error::other)?;
            let n=self.file.read_at(&mut chunk[..want],offset)?;if n==0{return Err(io::Error::other("worktree stage truncated"));}
            hash.update(&chunk[..n]);offset+=n as u64;
        }
        let after=self.file.metadata()?;
        if before.dev()!=after.dev() || before.ino()!=after.ino() || before.len()!=after.len() || before.mtime()!=after.mtime() || before.mtime_nsec()!=after.mtime_nsec() || before.ctime()!=after.ctime() || before.ctime_nsec()!=after.ctime_nsec()
            || format!("sha256:{:x}:{}",hash.finalize(),self.length)!=self.fingerprint {return Err(io::Error::other("worktree stage changed"));}
        Ok(())
    }
}

/// Pinned output of the original inverse journal; never constructed from a
/// service request path and retained for the lifetime of the private owner.
#[cfg(target_os="linux")]
#[doc(hidden)]
pub struct WorktreeInverseReadStage<'a> {
    _journal: &'a ManagedJournal, file: &'a File, action_id: String, actor_id: String, account_id: String,
    root_id: String, version_id: String, fingerprint: String, length: u64,
}
#[cfg(target_os="linux")]
impl WorktreeInverseReadStage<'_> {
    pub(crate) fn file(&self)->&File{self.file}
    pub(crate) fn action_id(&self)->&str{&self.action_id}
    pub(crate) fn actor_id(&self)->&str{&self.actor_id}
    pub(crate) fn account_id(&self)->&str{&self.account_id}
    pub(crate) fn root_id(&self)->&str{&self.root_id}
    pub(crate) fn version_id(&self)->&str{&self.version_id}
    pub(crate) fn fingerprint(&self)->&str{&self.fingerprint}
    pub(crate) fn length(&self)->u64{self.length}
}

/// A terminal inverse receipt bound to the original locked Native journal.
#[cfg(target_os="linux")]
#[doc(hidden)]
pub struct WorktreeInverseFinishStage<'a> {
    journal:&'a ManagedJournal, action_id:String, actor_id:String, account_id:String, root_id:String,
    operation_id:String, live:serde_json::Value,
}
#[cfg(target_os="linux")]
impl WorktreeInverseFinishStage<'_> {
    pub(crate) fn action_id(&self)->&str{&self.action_id}
    pub(crate) fn actor_id(&self)->&str{&self.actor_id}
    pub(crate) fn account_id(&self)->&str{&self.account_id}
    pub(crate) fn root_id(&self)->&str{&self.root_id}
    pub(crate) fn operation_id(&self)->&str{&self.operation_id}
    pub(crate) fn live(&self)->&serde_json::Value{&self.live}
    pub(crate) fn verify(&self)->io::Result<()> {
        let record=self.journal.read()?.ok_or_else(||io::Error::other("worktree inverse journal unavailable"))?;
        if !self.journal.exclusive || record.phase!=ManagedJournalPhase::HistoryPending || record.operation_id!=self.operation_id
            || record.plan["history_action_id"]!=self.action_id || record.plan["history_actor_id"]!=self.actor_id
            || record.plan["history_account_id"]!=self.account_id || record.plan["root_id"]!=self.root_id
            || record.history_completion.as_ref().and_then(|v|v.get("live"))!=Some(&self.live)
            || !record.commit_progress.iter().any(|v|v["kind"]=="worktree_inverse_quarantine_done")
            || !record.commit_progress.iter().any(|v|v["kind"]=="worktree_inverse_admin_done")
            || !record.commit_progress.last().is_some_and(|v|v["kind"]=="worktree_inverse_source_confirmed") {return Err(io::Error::other("worktree inverse journal unavailable"))}Ok(())
    }
}

const MANAGED_MOVE_MAX_PARTICIPANTS: usize = 128;
const MANAGED_MOVE_MAX_BLOB_BYTES: u64 = 10 * 1024 * 1024;
const MANAGED_MOVE_MAX_TOTAL_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ManagedParticipantKind { Document, Asset, Reference }

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedParticipant {
    pub(crate) kind: ManagedParticipantKind,
    pub(crate) source: PathBuf,
    pub(crate) destination: Option<PathBuf>,
    pub(crate) expected_fingerprint: String,
    pub(crate) replacement: Option<Vec<u8>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedGuard {
    pub(crate) source: PathBuf,
    pub(crate) expected_fingerprint: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedDirectoryGuard {
    pub(crate) path: PathBuf,
    pub(crate) revision: String,
}

#[cfg(target_os = "linux")]
fn directory_metadata_revision(metadata: &fs::Metadata) -> String {
    use std::os::unix::fs::MetadataExt;
    let identity = format!("odysseus-directory-v1:{}:{}:{}:{}:{}:{}:{}",
        metadata.dev(), metadata.ino(), metadata.mode(), metadata.mtime(), metadata.mtime_nsec(),
        metadata.ctime(), metadata.ctime_nsec());
    format!("sha256:{:x}", Sha256::digest(identity.as_bytes()))
}

#[cfg(target_os = "linux")]
fn open_selected_directory(root: &Path, path: &Path) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Component;
    let relative = path.strip_prefix(root).map_err(|_| io::Error::other("selected directory outside root"))?;
    let mut options = OpenOptions::new();
    options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let mut directory = options.open(root)?;
    for component in relative.components() {
        let Component::Normal(name) = component else { return Err(io::Error::other("invalid selected directory")); };
        let name = CString::new(name.as_bytes()).map_err(io::Error::other)?;
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        directory = unsafe { File::from_raw_fd(fd) };
    }
    Ok(directory)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedFolderProof { digest: String, count: u64, bytes: u64 }

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedFolderReference { path: PathBuf, fingerprint: String, replacement: Option<Vec<u8>> }

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedFolderFrame {
    origin: String, canonical_root: PathBuf, layout_version: u8,
    workspace_id: Option<String>, workspace_relative_folder: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedFolderDestinationRoot {
    root_id: String, root_path: PathBuf,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    directory_guard: Option<ManagedDirectoryGuard>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedFolderAsset {
    path: PathBuf, destination: PathBuf, fingerprint: String,
    document: PathBuf, document_destination: PathBuf, workspace_root: PathBuf,
    origin: String, layout_version: u8,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    source_frame: Option<ManagedFolderFrame>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    destination_frame: Option<ManagedFolderFrame>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedFolderRequest {
    pub(crate) operation_id: String,
    pub(crate) source: PathBuf,
    destination: PathBuf,
    expected_snapshot: ManagedFolderProof,
    references: Vec<ManagedFolderReference>,
    assets: Vec<ManagedFolderAsset>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    directory_guard: Option<ManagedDirectoryGuard>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    destination_root: Option<ManagedFolderDestinationRoot>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    inverse_of: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    inverse_prune: Option<Vec<serde_json::Value>>,
}

struct ManagedPreparedGuard {
    guard: ManagedGuard,
    identity: serde_json::Value,
    byte_len: u64,
}

/// The folder move freezes this bounded, no-follow view before publication.
/// `history` uses the existing History directory-manifest format; `identities`
/// are private journal evidence, not client-supplied authority.
#[cfg(target_os = "linux")]
struct ManagedFolderSnapshot {
    root_identity: serde_json::Value,
    identities: Vec<serde_json::Value>,
    history: Vec<u8>,
    total_bytes: u64,
}

#[cfg(target_os = "linux")]
fn managed_folder_digest(root_identity: &serde_json::Value, snapshot: &ManagedFolderSnapshot) -> io::Result<String> {
    let evidence = serde_json::json!({"root_identity":root_identity,"folder_identity":snapshot.root_identity,
        "identities":snapshot.identities,"history_digest":format!("{:x}", Sha256::digest(&snapshot.history))});
    Ok(format!("sha256:{:x}", Sha256::digest(serde_json::to_vec(&evidence).map_err(io::Error::other)?)))
}

/// Freeze exact subtree evidence before a History registration or publication.
#[cfg(target_os = "linux")]
fn stage_managed_folder_intent(
    journal: &ManagedJournal, owner_id: &str, request_digest: &str, root_id: &str, root_path: &Path,
    policy_generation: u64, history_action_id: &str, history_timestamp_millis: u64,
    source: &Path, destination: &Path, source_parent: &File, destination_parent: &File,
    root_identity: &serde_json::Value, snapshot: &ManagedFolderSnapshot,
    expected_snapshot: &str, history_snapshot: &[serde_json::Value], participants: &[ManagedPreparedParticipant],
    directories: &[PathBuf], guards: &[ManagedPreparedGuard], assets: &[ManagedFolderAsset], directory_guard: Option<&serde_json::Value>,
    destination_root: Option<&(String, PathBuf, serde_json::Value)>, destination_guard: Option<&serde_json::Value>, inverse_request: Option<&ManagedFolderRequest>,
) -> io::Result<ManagedJournalRecord> {
    let destination_path=destination_root.map(|root|root.1.as_path()).unwrap_or(root_path);
    if destination_root.is_some_and(|root| root.0.is_empty() || !root.1.is_absolute() || root.0==root_id
        || root.1.starts_with(root_path) || root_path.starts_with(&root.1)) {return Err(io::Error::other("managed destination root is invalid"));}
    if owner_id.is_empty() || root_id.is_empty() || !root_path.is_absolute() || !source.starts_with(root_path)
        || !destination.starts_with(destination_path) || source == root_path || destination == destination_path
        || source.starts_with(destination) || destination.starts_with(source)
        || lexical_normalize(source) != source || lexical_normalize(destination) != destination
        || history_action_id.is_empty() || history_action_id.len() > 128
        || !history_action_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
        || managed_folder_digest(root_identity, snapshot)? != expected_snapshot
        || participants.is_empty() || managed_capture_targets(participants).len() > MANAGED_MOVE_MAX_PARTICIPANTS
        || participants[0].participant.source != source || participants[0].participant.destination.as_deref() != Some(destination)
        || participants[0].source_identity != snapshot.root_identity || participants[0].before != snapshot.history
        || history_snapshot.len() > MANAGED_MOVE_MAX_PARTICIPANTS || directories.len() < 2
        || directories[0] != source || directories[1] != destination
        || participants.len().checked_add(guards.len()).is_none_or(|count| count > MANAGED_MOVE_MAX_PARTICIPANTS) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "managed folder intent is invalid"));
    }
    let source_relative = source.strip_prefix(root_path).map_err(io::Error::other)?;
    let mut history_ids = BTreeSet::new(); let mut history_paths = BTreeSet::new();
    for entry in history_snapshot {
        let object = entry.as_object().filter(|object| object.len() == 3).ok_or_else(|| io::Error::other("managed folder History snapshot is invalid"))?;
        let id = object.get("resource_id").and_then(serde_json::Value::as_str).filter(|value| !value.is_empty()).ok_or_else(|| io::Error::other("managed folder History identity is invalid"))?;
        let path = object.get("relative_path").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("managed folder History path is invalid"))?;
        if object.get("generation").and_then(serde_json::Value::as_u64).is_none_or(|value| value == 0)
            || path.is_absolute() || lexical_normalize(path) != path || !path.starts_with(source_relative)
            || !history_ids.insert(id.to_owned()) || !history_paths.insert(path.to_path_buf()) {
            return Err(io::Error::other("managed folder History snapshot is invalid"));
        }
    }
    let mut total = 0u64;
    let mut participant_plan = Vec::with_capacity(participants.len());
    for (index, item) in participants.iter().enumerate() {
        if item.before.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES || fingerprint_bytes(&item.before).value != item.participant.expected_fingerprint
            || item.participant.replacement.as_ref().is_some_and(|bytes| bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "managed folder participant is invalid"));
        }
        total = total.checked_add(item.before.len() as u64).and_then(|size| size.checked_add(item.participant.replacement.as_ref().map_or(0, |bytes| bytes.len() as u64)))
            .ok_or_else(|| io::Error::other("managed folder participant bound exceeded"))?;
        if total > MANAGED_MOVE_MAX_TOTAL_BYTES { return Err(io::Error::other("managed folder participant bound exceeded")); }
        let mut participant = serde_json::json!({"kind":item.participant.kind,"source":item.participant.source,
            "destination":item.participant.destination,"expected_fingerprint":item.participant.expected_fingerprint,
            "mode":item.mode,"source_identity":item.source_identity,
            "source_hold":item.participant.source.parent().map(|parent| parent.join(format!(".openclank-managed-{}-{index}.hold", journal.operation_id))),
            "before_stage":format!("before-{index}"),"before_digest":format!("{:x}", Sha256::digest(&item.before)),
            "replacement_stage":item.participant.replacement.as_ref().map(|_| format!("replacement-{index}")),
            "replacement_digest":item.participant.replacement.as_ref().map(|bytes| format!("{:x}", Sha256::digest(bytes)))});
        if destination_root.is_some() && item.participant.source.strip_prefix(source).ok().is_some_and(|suffix|
            !suffix.as_os_str().is_empty() && item.participant.destination.as_ref().is_some_and(|target| *target != destination.join(suffix))) {
            if !assets.iter().any(|asset|asset.path==item.participant.source && Some(&asset.destination)==item.participant.destination.as_ref() && asset.destination_frame.is_some()) {
                return Err(io::Error::other("folder asset relocation is unproven"));
            }
            participant["internal_relocation"]=serde_json::json!(true);
        }
        participant_plan.push(participant);
    }
    let manifest_digest = format!("{:x}", Sha256::digest(&snapshot.history));
    let mut candidate = ManagedJournalRecord { version:1, operation_id:journal.operation_id.clone(), owner_id:owner_id.to_owned(),
        request_digest:request_digest.to_owned(), phase:ManagedJournalPhase::Prepared,
        plan:serde_json::json!({"kind":"folder", "root_id":root_id,"root_path":root_path,"root_identity":root_identity,
            "policy_generation":policy_generation,"history_action_id":history_action_id,"history_timestamp_millis":history_timestamp_millis,
            "folder":{"source":source,"destination":destination,"source_identity":snapshot.root_identity,
                "source_parent_identity":managed_object_identity(source_parent)?,"destination_parent_identity":managed_object_identity(destination_parent)?,
                "snapshot_digest":expected_snapshot,"manifest_stage":"folder-before","manifest_digest":manifest_digest,
                "identities":snapshot.identities,"registered_before":history_snapshot,"directories":directories,"total_bytes":snapshot.total_bytes,
                "asset_proofs":assets},"participants":participant_plan,
            "guards":guards.iter().map(|guard| serde_json::json!({"source":guard.guard.source,"expected_fingerprint":guard.guard.expected_fingerprint,
                "identity":guard.identity,"byte_len":guard.byte_len})).collect::<Vec<_>>()}),
        file_stages:vec![],directory_events:vec![],commit_progress:vec![],cleanup_progress:vec![],preparation:vec![],registry_plan:None,
        history_prepare:None,history_completion:None,review_outcome:None };
    if let Some(guard) = directory_guard { candidate.plan["directory_guard"] = guard.clone(); }
    if let Some((id,path,identity)) = destination_root {
        candidate.plan["destination_root_id"] = serde_json::json!(id);
        candidate.plan["destination_root_path"] = serde_json::json!(path);
        candidate.plan["destination_root_identity"] = identity.clone();
    }
    if let Some(guard) = destination_guard {candidate.plan["destination_directory_guard"] = guard.clone();}
    let inverse_bytes=if let Some(request)=inverse_request {
        let forward=request.inverse_of.as_deref().ok_or_else(||io::Error::other("managed folder inverse identity missing"))?;
        if forward==journal.operation_id || forward.len()<16 || forward.len()>80 || !forward.bytes().all(|c|c.is_ascii_alphanumeric() || c==b'-' || c==b'_') {return Err(io::Error::other("managed folder inverse identity invalid"));}
        let bytes=serde_json::to_vec(request).map_err(io::Error::other)?;
        if bytes.len()>10*1024*1024 {return Err(io::Error::other("managed folder inverse exceeds its bound"));}
        candidate.plan["inverse_of"]=serde_json::json!(forward);
        candidate.plan["inverse_prune"]=serde_json::json!(request.inverse_prune.as_ref().ok_or_else(||io::Error::other("managed inverse directories unavailable"))?);
        candidate.plan["inverse_request_digest"]=serde_json::json!(format!("{:x}",Sha256::digest(&bytes)));
        Some(bytes)
    } else {None};
    let record = match journal.read()? {
        Some(existing) if existing.owner_id == candidate.owner_id && existing.request_digest == candidate.request_digest && existing.plan == candidate.plan => existing,
        Some(_) => return Err(io::Error::other("managed folder intent changed")),
        None => { journal.write(&candidate)?; candidate },
    };
    if record.phase != ManagedJournalPhase::Prepared { return Err(io::Error::other("managed folder operation already advanced")); }
    if let Some(bytes)=inverse_bytes {journal.stage("inverse-request",&bytes)?;}
    if journal.stage("folder-before", &snapshot.history)? != manifest_digest {
        return Err(io::Error::other("managed folder manifest changed"));
    }
    for (index, item) in participants.iter().enumerate() {
        journal.stage(&format!("before-{index}"), &item.before)?;
        if let Some(bytes) = &item.participant.replacement { journal.stage(&format!("replacement-{index}"), bytes)?; }
    }
    Ok(record)
}

#[cfg(target_os = "linux")]
fn snapshot_managed_folder<F>(root: &Path, directory: &File, mut authorize: F) -> Result<ManagedFolderSnapshot, EngineError>
where F: FnMut(&Path) -> Result<(), EngineError> {
    snapshot_managed_folder_excluding(root, directory, &mut authorize, &HashMap::new())
}

#[cfg(target_os = "linux")]
fn snapshot_managed_folder_excluding<F>(root: &Path, directory: &File, mut authorize: F,
    exclusions: &HashMap<PathBuf, serde_json::Value>) -> Result<ManagedFolderSnapshot, EngineError>
where F: FnMut(&Path) -> Result<(), EngineError> {
    use base64::Engine as _;
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;

    fn signature(metadata: &fs::Metadata) -> (u64, u64, u32, u64, i64, i64, i64, i64) {
        (metadata.dev(), metadata.ino(), metadata.mode(), metadata.len(), metadata.mtime(), metadata.mtime_nsec(), metadata.ctime(), metadata.ctime_nsec())
    }
    fn walk<F>(root: &Path, relative: &Path, directory: &File, authorize: &mut F,
               entries: &mut Vec<serde_json::Value>, identities: &mut Vec<serde_json::Value>, total: &mut u64,
               observed: &mut Vec<(File, fs::Metadata)>, exclusions: &HashMap<PathBuf, serde_json::Value>,
               excluded: &mut BTreeSet<PathBuf>) -> Result<(), EngineError>
    where F: FnMut(&Path) -> Result<(), EngineError> {
        let before = directory.metadata()?;
        if !before.is_dir() { return Err(EngineError::NotADirectory); }
        let fd_path = PathBuf::from(format!("/proc/self/fd/{}", directory.as_raw_fd()));
        let mut children = fs::read_dir(fd_path)?.collect::<Result<Vec<_>, _>>()?;
        children.sort_by_key(|entry| entry.file_name());
        for child in children {
            let name = child.file_name();
            let text = name.to_str().filter(|value| !value.is_empty() && *value != "." && *value != ".." && !value.contains('\\'))
                .ok_or_else(|| io::Error::new(io::ErrorKind::Unsupported, "folder entry name is unsupported"))?;
            let name_c = CString::new(name.as_bytes()).map_err(io::Error::other)?;
            let mut raw: libc::stat = unsafe { std::mem::zeroed() };
            // SAFETY: the pinned directory descriptor, name and output remain valid.
            if unsafe { libc::fstatat(directory.as_raw_fd(), name_c.as_ptr(), &mut raw, libc::AT_SYMLINK_NOFOLLOW) } != 0 {
                return Err(io::Error::last_os_error().into());
            }
            let kind = raw.st_mode & libc::S_IFMT;
            if kind != libc::S_IFDIR && kind != libc::S_IFREG {
                return Err(EngineError::Io(io::Error::new(io::ErrorKind::Unsupported, "managed folder contains a symlink or special file")));
            }
            let child_relative = relative.join(text);
            authorize(&root.join(&child_relative))?;
            let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK
                | if kind == libc::S_IFDIR { libc::O_DIRECTORY } else { 0 };
            // SAFETY: openat returns a fresh descriptor or an error; no symlink is followed.
            let fd = unsafe { libc::openat(directory.as_raw_fd(), name_c.as_ptr(), flags) };
            if fd < 0 { return Err(io::Error::last_os_error().into()); }
            let file = unsafe { File::from_raw_fd(fd) };
            let metadata = file.metadata()?;
            if metadata.dev() != raw.st_dev || metadata.ino() != raw.st_ino || metadata.mode() != raw.st_mode {
                return Err(EngineError::Conflict);
            }
            if let Some(expected) = exclusions.get(&child_relative) {
                if !metadata.is_file() || managed_object_identity(&file)? != *expected || !excluded.insert(child_relative) {
                    return Err(EngineError::Conflict);
                }
                observed.push((file, metadata));
                continue;
            }
            if entries.len() + 1 >= MANAGED_MOVE_MAX_PARTICIPANTS { return Err(EngineError::LimitExceeded); }
            let path = child_relative.to_str().ok_or_else(|| io::Error::other("folder path is unsupported"))?.replace('\\', "/");
            let modified = metadata.modified().ok().and_then(|value| value.duration_since(UNIX_EPOCH).ok()).map(|value| value.as_millis() as u64);
            let mut record = serde_json::json!({"path":path, "mode":metadata.mode(), "mtime_millis":modified});
            identities.push(serde_json::json!({"path":path,"identity":managed_object_identity(&file)?}));
            if kind == libc::S_IFDIR {
                record["type"] = serde_json::json!("directory");
                entries.push(record);
                walk(root, &child_relative, &file, authorize, entries, identities, total, observed, exclusions, excluded)?;
            } else {
                if metadata.len() > MANAGED_MOVE_MAX_BLOB_BYTES { return Err(EngineError::LimitExceeded); }
                *total = total.checked_add(metadata.len()).ok_or(EngineError::LimitExceeded)?;
                if *total > MANAGED_MOVE_MAX_TOTAL_BYTES { return Err(EngineError::LimitExceeded); }
                let mut bytes = Vec::new(); (&file).take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut bytes)?;
                if bytes.len() as u64 != metadata.len() || signature(&file.metadata()?) != signature(&metadata) { return Err(EngineError::Conflict); }
                record["type"] = serde_json::json!("file");
                record["size"] = serde_json::json!(bytes.len());
                record["sha256"] = serde_json::json!(format!("sha256:{:x}", Sha256::digest(&bytes)));
                record["content"] = serde_json::json!(base64::engine::general_purpose::STANDARD.encode(bytes));
                entries.push(record);
            }
            observed.push((file, metadata));
        }
        if signature(&directory.metadata()?) != signature(&before) { return Err(EngineError::Conflict); }
        Ok(())
    }
    let root_identity = managed_object_identity(directory)?;
    let mut entries = Vec::new(); let mut identities = Vec::new(); let mut total = 0;
    let mut observed = vec![(directory.try_clone()?, directory.metadata()?)];
    let mut excluded = BTreeSet::new();
    walk(root, Path::new(""), directory, &mut authorize, &mut entries, &mut identities, &mut total, &mut observed, exclusions, &mut excluded)?;
    if excluded.len() != exclusions.len() { return Err(EngineError::Conflict); }
    // Earlier siblings can change while later siblings are read. Keep their
    // handles until the complete walk and reject any such observed drift.
    for (file, before) in &observed {
        if signature(&file.metadata()?) != signature(before) { return Err(EngineError::Conflict); }
    }
    let history = serde_json::to_vec(&serde_json::json!({"version":1,"root_type":"directory","entries":entries})).map_err(io::Error::other)?;
    if history.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES { return Err(EngineError::LimitExceeded); }
    Ok(ManagedFolderSnapshot { root_identity, identities, history, total_bytes: total })
}

#[cfg(target_os = "linux")]
fn open_folder_from_root(root: &File, root_path: &Path, path: &Path) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Component;
    if !path.is_absolute() || lexical_normalize(path) != path { return Err(io::Error::other("folder path is invalid")); }
    let relative = path.strip_prefix(root_path).map_err(io::Error::other)?;
    let mut current = root.try_clone()?;
    for part in relative.components() {
        let Component::Normal(name) = part else { return Err(io::Error::other("folder path is invalid")); };
        let name = CString::new(name.as_bytes()).map_err(io::Error::other)?;
        // SAFETY: the current parent descriptor and name remain live.
        let fd = unsafe { libc::openat(current.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        current = unsafe { File::from_raw_fd(fd) };
    }
    Ok(current)
}

#[cfg(target_os = "linux")]
fn folder_manifest_entry_bytes(entries: &[serde_json::Value], relative: &Path) -> io::Result<(Vec<u8>, bool, Option<u32>)> {
    use base64::Engine as _;
    let entry = entries.iter().find(|entry| entry.get("path").and_then(serde_json::Value::as_str).map(Path::new) == Some(relative))
        .ok_or_else(|| io::Error::other("registered folder descendant is unavailable"))?;
    let mode = entry.get("mode").and_then(serde_json::Value::as_u64).and_then(|value| u32::try_from(value).ok());
    match entry.get("type").and_then(serde_json::Value::as_str) {
        Some("file") => {
            let content = entry.get("content").and_then(serde_json::Value::as_str).ok_or_else(|| io::Error::other("folder file content is unavailable"))?;
            let bytes = base64::engine::general_purpose::STANDARD.decode(content).map_err(io::Error::other)?;
            let digest = format!("sha256:{:x}", Sha256::digest(&bytes));
            if entry.get("sha256").and_then(serde_json::Value::as_str) != Some(digest.as_str())
                || entry.get("size").and_then(serde_json::Value::as_u64) != Some(bytes.len() as u64) {
                return Err(io::Error::other("folder file content changed"));
            }
            Ok((bytes, false, mode))
        }
        Some("directory") => {
            let mut children = Vec::new();
            for item in entries {
                let path = item.get("path").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("folder manifest path is invalid"))?;
                if path == relative || !path.starts_with(relative) { continue; }
                let suffix = path.strip_prefix(relative).map_err(io::Error::other)?.to_str().ok_or_else(|| io::Error::other("folder descendant is unsupported"))?;
                let mut child = item.clone(); child["path"] = serde_json::json!(suffix); children.push(child);
            }
            let bytes = serde_json::to_vec(&serde_json::json!({"version":1,"root_type":"directory","entries":children})).map_err(io::Error::other)?;
            if bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES { return Err(io::Error::other("folder directory capture exceeds its bound")); }
            Ok((bytes, true, mode))
        }
        _ => Err(io::Error::other("folder manifest type is unsupported")),
    }
}

/// Build the History targets from the private no-follow tree and the atomic
/// registry snapshot. Ordinary unregistered descendants stay in the single
/// directory manifest; they are not given artificial resource identities.
#[cfg(target_os = "linux")]
fn assemble_managed_folder_participants(
    root: &Path, request: &ManagedFolderRequest, snapshot: &ManagedFolderSnapshot, registered: &[serde_json::Value],
) -> io::Result<(Vec<ManagedPreparedParticipant>, Vec<PathBuf>, Vec<ManagedGuard>)> {
    let manifest: serde_json::Value = serde_json::from_slice(&snapshot.history).map_err(io::Error::other)?;
    let entries = manifest.get("entries").and_then(serde_json::Value::as_array).ok_or_else(|| io::Error::other("folder manifest is invalid"))?;
    let source_relative = request.source.strip_prefix(root).map_err(io::Error::other)?;
    let mut by_path = HashMap::new();
    for entry in entries {
        let path = entry.get("path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or_else(|| io::Error::other("folder manifest path is invalid"))?;
        if path.is_absolute() || lexical_normalize(&path) != path || path.as_os_str().is_empty() || by_path.insert(path, entry).is_some() {
            return Err(io::Error::other("folder manifest path is invalid"));
        }
    }
    let mut identities = HashMap::new();
    for entry in &snapshot.identities {
        let path = entry.get("path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or_else(|| io::Error::other("folder identity path is invalid"))?;
        let identity = entry.get("identity").cloned().ok_or_else(|| io::Error::other("folder identity is unavailable"))?;
        if identities.insert(path, identity).is_some() { return Err(io::Error::other("folder identity is duplicated")); }
    }
    let bytes_for = |relative: &Path| folder_manifest_entry_bytes(entries, relative);
    let root_participant = ManagedPreparedParticipant { participant:ManagedParticipant { kind:ManagedParticipantKind::Document,
        source:request.source.clone(), destination:Some(request.destination.clone()),
        expected_fingerprint:fingerprint_bytes(&snapshot.history).value, replacement:None },
        before:snapshot.history.clone(), mode:None, source_identity:snapshot.root_identity.clone() };
    let mut participants = vec![root_participant];
    let mut directories = vec![request.source.clone(), request.destination.clone()];
    let mut seen = BTreeSet::new();
    for item in registered {
        let relative = item.get("relative_path").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("registered folder path is invalid"))?;
        let suffix = relative.strip_prefix(source_relative).map_err(io::Error::other)?;
        if !seen.insert(relative.to_path_buf()) { return Err(io::Error::other("registered folder path is duplicated")); }
        if suffix.as_os_str().is_empty() { continue; }
        let (before, directory, mode) = bytes_for(suffix)?;
        let identity = identities.get(suffix).cloned().ok_or_else(|| io::Error::other("registered folder identity is unavailable"))?;
        let old = request.source.join(suffix); let structural = request.destination.join(suffix);
        let new = request.assets.iter().find(|asset| asset.path == old && asset.destination_frame.is_some())
            .map(|asset|asset.destination.clone()).unwrap_or(structural);
        if directory { if request.assets.iter().any(|asset|asset.path == old) {return Err(io::Error::other("folder asset is not a file"));} directories.push(old.clone()); directories.push(new.clone()); }
        participants.push(ManagedPreparedParticipant { participant:ManagedParticipant { kind:ManagedParticipantKind::Asset,
            source:old, destination:Some(new), expected_fingerprint:fingerprint_bytes(&before).value, replacement:None },
            before, mode, source_identity:identity });
    }
    let mut edited_paths = BTreeSet::new();
    for item in &request.references {
        let Some(replacement) = &item.replacement else { continue; };
        let Ok(suffix) = item.path.strip_prefix(&request.source) else { continue; };
        if suffix.as_os_str().is_empty() || !edited_paths.insert(item.path.clone())
            || request.assets.iter().any(|asset| asset.path == item.path) || replacement.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES {
            return Err(io::Error::other("folder replacement is invalid"));
        }
        let (before, directory, mode) = bytes_for(suffix)?;
        if directory || fingerprint_bytes(&before).value != item.fingerprint { return Err(io::Error::other("folder replacement proof changed")); }
        if let Some(existing) = participants.iter_mut().find(|value| value.participant.source == item.path) {
            if existing.before != before || existing.participant.replacement.is_some() { return Err(io::Error::other("folder replacement conflicts")); }
            existing.participant.replacement = Some(replacement.clone());
        } else {
            let identity = identities.get(suffix).cloned().ok_or_else(|| io::Error::other("folder replacement identity is unavailable"))?;
            participants.push(ManagedPreparedParticipant { participant:ManagedParticipant { kind:ManagedParticipantKind::Asset,
                source:item.path.clone(), destination:Some(request.destination.join(suffix)), expected_fingerprint:item.fingerprint.clone(), replacement:Some(replacement.clone()) },
                before, mode, source_identity:identity });
        }
    }
    // Proven assets may not yet have a History registry identity; retain an
    // exact participant whenever their new layout moves them out of the tree.
    for asset in &request.assets {
        let Ok(suffix)=asset.path.strip_prefix(&request.source) else {continue;};
        if asset.destination_frame.is_none() || asset.destination == request.destination.join(suffix) {continue;}
        let (before,directory,mode)=bytes_for(suffix)?;
        if directory || fingerprint_bytes(&before).value != asset.fingerprint {return Err(io::Error::other("folder asset proof changed"));}
        if let Some(existing)=participants.iter().find(|item|item.participant.source==asset.path) {
            if existing.participant.destination.as_ref()!=Some(&asset.destination) || existing.before!=before {return Err(io::Error::other("folder asset mapping changed"));}
        } else {
            let identity=identities.get(suffix).cloned().ok_or_else(||io::Error::other("folder asset identity unavailable"))?;
            participants.push(ManagedPreparedParticipant {participant:ManagedParticipant {kind:ManagedParticipantKind::Asset,
                source:asset.path.clone(),destination:Some(asset.destination.clone()),expected_fingerprint:asset.fingerprint.clone(),replacement:None},before,mode,source_identity:identity});
        }
    }
    let mut external: HashMap<PathBuf, String> = HashMap::new();
    let mut verify = |path: &Path, fingerprint: &str| -> io::Result<()> {
        if let Ok(suffix) = path.strip_prefix(&request.source) {
            let (before, directory, _) = bytes_for(suffix)?;
            if directory || fingerprint_bytes(&before).value != fingerprint { return Err(io::Error::other("folder participant proof changed")); }
        } else if external.insert(path.to_path_buf(), fingerprint.to_owned()).is_some_and(|old| old != fingerprint) {
            return Err(io::Error::other("folder external proofs disagree"));
        }
        Ok(())
    };
    for item in &request.references {
        if item.replacement.is_some() && !item.path.starts_with(&request.source) { continue; }
        verify(&item.path, &item.fingerprint)?;
    }
    for item in &request.assets {
        let expected = item.path.strip_prefix(&request.source).ok().map(|suffix| request.destination.join(suffix)).unwrap_or_else(|| item.path.clone());
        if item.destination != expected {
            if item.path.starts_with(&request.source) {
                if item.destination_frame.is_none() {return Err(io::Error::other("folder asset destination changed"));}
                verify(&item.path,&item.fingerprint)?;
            }
            continue;
        }
        verify(&item.path, &item.fingerprint)?;
    }
    let mut guards = external.into_iter().map(|(source, expected_fingerprint)| ManagedGuard { source, expected_fingerprint }).collect::<Vec<_>>();
    guards.sort_by(|left, right| left.source.cmp(&right.source));
    if managed_capture_targets(&participants).len() > MANAGED_MOVE_MAX_PARTICIPANTS
        || participants.len().checked_add(guards.len()).is_none_or(|count| count > MANAGED_MOVE_MAX_PARTICIPANTS) {
        return Err(io::Error::other("folder history participant bound exceeded"));
    }
    Ok((participants, directories, guards))
}

#[cfg(target_os = "linux")]
fn preflight_managed_folder_external<F>(request: &ManagedFolderRequest, root: &(String, PathBuf), destination_root: &(String, PathBuf), mut authorize: F)
    -> Result<Vec<ManagedPreparedParticipant>, EngineError>
where F: FnMut(&Path, bool) -> Result<(String, PathBuf), EngineError> {
    let mut candidates = Vec::new();
    let mut asset_destinations=BTreeSet::new();
    for asset in &request.assets {if !asset_destinations.insert(asset.destination.clone()) {return Err(EngineError::Conflict);}}
    for reference in &request.references {
        if reference.replacement.is_some() && !reference.path.starts_with(&request.source) {
            candidates.push(ManagedParticipant { kind:ManagedParticipantKind::Reference, source:reference.path.clone(), destination:None,
                expected_fingerprint:reference.fingerprint.clone(), replacement:reference.replacement.clone() });
        }
    }
    for asset in &request.assets {
        let suffix = asset.document.strip_prefix(&request.source).map_err(|_| EngineError::Conflict)?;
        if destination_root != root {
            if !asset.workspace_root.starts_with(&root.1) || !folder_cross_asset_destination_proven(&destination_root.1,&request.source,&request.destination,asset) {return Err(EngineError::Conflict);}
            if let Ok(relative)=asset.path.strip_prefix(&request.source) {
                if asset.destination != request.destination.join(relative) {
                    if authorize(&asset.destination,true)? != *destination_root {return Err(EngineError::Conflict);}
                    let parent=managed_media_ancestor_inner(&destination_root.1,&asset.destination,true)?;
                    if metadata_device(&fs::symlink_metadata(&parent)?) != metadata_device(&fs::symlink_metadata(&asset.path)?) {return Err(EngineError::Conflict);}
                    let probe=asset.destination.strip_prefix(&request.destination).ok().map(|suffix|request.source.join(suffix)).unwrap_or_else(||asset.destination.clone());
                    match fs::symlink_metadata(probe) {
                        Ok(_) => return Err(EngineError::DestinationExists),
                        Err(error) if error.kind()==io::ErrorKind::NotFound => {}, Err(error)=>return Err(error.into()),
                    }
                }
                continue;
            }
            candidates.push(ManagedParticipant {kind:ManagedParticipantKind::Asset,source:asset.path.clone(),destination:Some(asset.destination.clone()),expected_fingerprint:asset.fingerprint.clone(),replacement:None});
            continue;
        }
        if suffix.as_os_str().is_empty() || asset.document_destination != request.destination.join(suffix)
            || !asset.workspace_root.is_absolute() || lexical_normalize(&asset.workspace_root) != asset.workspace_root
            || !asset.workspace_root.starts_with(&root.1) || !asset.document.starts_with(&asset.workspace_root)
            || !matches!(asset.layout_version, 1 | 2) || !matches!(asset.origin.as_str(), "workspace" | "loose" | "selected_root")
            || (asset.origin == "selected_root" && (asset.layout_version != 2 || request.directory_guard.as_ref().map(|guard| &guard.path) != Some(&asset.workspace_root) || !asset.document_destination.starts_with(&asset.workspace_root)))
            || (asset.origin == "workspace" && !asset.document_destination.starts_with(&asset.workspace_root))
            || (asset.origin == "loose" && asset.document.parent() != Some(asset.workspace_root.as_path()))
            || asset.path.file_name().is_none() || asset.path.file_name() != asset.destination.file_name() {
            return Err(EngineError::Conflict);
        }
        if let Ok(relative) = asset.path.strip_prefix(&request.source) {
            if asset.destination != request.destination.join(relative) { return Err(EngineError::Conflict); }
            continue;
        }
        if asset.path == asset.destination { continue; }
        if asset.origin != "workspace" || !asset.document_destination.starts_with(&asset.workspace_root)
            || asset.destination.starts_with(&request.destination) || asset.destination.starts_with(&request.source) {
            return Err(EngineError::Io(io::Error::new(io::ErrorKind::Unsupported, "external folder asset destination is unsupported")));
        }
        let relative = asset.document_destination.strip_prefix(&asset.workspace_root).map_err(|_| EngineError::Conflict)?;
        let name = asset.path.file_name().ok_or(EngineError::Conflict)?;
        let expected = if asset.layout_version == 2 {
            let stem = relative.file_stem().filter(|name| !name.is_empty()).ok_or(EngineError::Conflict)?;
            asset.workspace_root.join(".media").join(relative.parent().ok_or(EngineError::Conflict)?).join(stem).join(name)
        } else {
            let mut stem = relative.to_path_buf(); stem.set_extension("");
            asset.workspace_root.join("media").join(stem).join(name)
        };
        if asset.destination != expected { return Err(EngineError::Conflict); }
        candidates.push(ManagedParticipant { kind:ManagedParticipantKind::Asset, source:asset.path.clone(), destination:Some(asset.destination.clone()),
            expected_fingerprint:asset.fingerprint.clone(), replacement:None });
    }
    let mut sources = BTreeSet::new(); let mut destinations = BTreeSet::new(); let mut prepared = Vec::new();
    for item in candidates {
        if !sources.insert(item.source.clone()) || item.source.starts_with(&request.source) || item.source.starts_with(&request.destination) {return Err(EngineError::Conflict);}
        let source_root=authorize(&item.source,false)?;
        if source_root != *root && (source_root != *destination_root || !matches!(item.kind,ManagedParticipantKind::Reference)) {return Err(EngineError::Conflict);}
        managed_no_symlink(&source_root.1, &item.source, false)?;
        let metadata = fs::symlink_metadata(&item.source)?;
        if !metadata.is_file() || metadata.len() > MANAGED_MOVE_MAX_BLOB_BYTES
            || item.replacement.as_ref().is_some_and(|bytes| bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES) { return Err(EngineError::LimitExceeded); }
        if let Some(destination) = &item.destination {
            if !destinations.insert(destination.clone()) || authorize(destination, true)? != *destination_root { return Err(EngineError::Conflict); }
            let proven = request.assets.iter().any(|asset| asset.destination == *destination
                && asset.path == item.source && folder_asset_destination_proven(&destination_root.1, &request.source, &request.destination, asset));
            let parent=managed_media_ancestor_inner(&destination_root.1, destination, proven)?;
            if metadata_device(&fs::symlink_metadata(&parent)?) != metadata_device(&metadata) {return Err(EngineError::Conflict);}
            let probe=destination.strip_prefix(&request.destination).ok().map(|suffix|request.source.join(suffix)).unwrap_or_else(||destination.clone());
            match fs::symlink_metadata(probe) {
                Ok(_) => return Err(EngineError::DestinationExists),
                Err(error) if error.kind() == io::ErrorKind::NotFound => {},
                Err(error) => return Err(error.into()),
            }
        }
        let mut options = OpenOptions::new(); options.read(true);
        #[cfg(unix)] options.custom_flags(libc::O_NOFOLLOW);
        let file = options.open(&item.source)?; let identity = managed_object_identity(&file)?;
        let mut before = Vec::new(); (&file).take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut before)?;
        if before.len() as u64 != metadata.len() || fingerprint_bytes(&before).value != item.expected_fingerprint
            || fingerprint_file(&item.source)?.value != item.expected_fingerprint
            || managed_object_identity(&options.open(&item.source)?)? != identity { return Err(EngineError::Conflict); }
        prepared.push(ManagedPreparedParticipant { mode:file_mode(&item.source), participant:item, before, source_identity:identity });
    }
    if sources.iter().any(|source| destinations.contains(source)) { return Err(EngineError::Conflict); }
    Ok(prepared)
}

fn preflight_managed_guards<F>(guards: &[ManagedGuard], root: &(String, PathBuf), mut authorize: F) -> Result<Vec<ManagedPreparedGuard>, EngineError>
where F: FnMut(&Path) -> Result<(String, PathBuf), EngineError> {
    let mut result = Vec::with_capacity(guards.len());
    let mut seen = BTreeSet::new();
    for guard in guards {
        if !seen.insert(guard.source.clone()) || authorize(&guard.source)? != *root {
            return Err(EngineError::Conflict);
        }
        managed_no_symlink(&root.1, &guard.source, false)?;
        let mut options = OpenOptions::new(); options.read(true);
        #[cfg(unix)]
        options.custom_flags(libc::O_NOFOLLOW);
        let file = options.open(&guard.source)?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.len() > MANAGED_MOVE_MAX_BLOB_BYTES { return Err(EngineError::LimitExceeded); }
        let identity = managed_object_identity(&file)?;
        let mut bytes = Vec::new(); (&file).take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 != metadata.len() || fingerprint_bytes(&bytes).value != guard.expected_fingerprint
            || managed_object_identity(&options.open(&guard.source)?)? != identity { return Err(EngineError::Conflict); }
        result.push(ManagedPreparedGuard { guard:guard.clone(), identity, byte_len:metadata.len() });
    }
    Ok(result)
}

fn preflight_folder_guards<F>(guards:&[ManagedGuard], source:&(String,PathBuf), destination:&(String,PathBuf), mut authorize:F) -> Result<Vec<ManagedPreparedGuard>,EngineError>
where F:FnMut(&Path)->Result<(String,PathBuf),EngineError> {
    let mut seen=BTreeSet::new();let mut result=Vec::with_capacity(guards.len());
    for guard in guards {
        if !seen.insert(guard.source.clone()){return Err(EngineError::Conflict);}
        let root=authorize(&guard.source)?;
        if root != *source && root != *destination {return Err(EngineError::Conflict);}
        result.extend(preflight_managed_guards(std::slice::from_ref(guard),&root,&mut authorize)?);
    }
    Ok(result)
}

fn validate_managed_guard_budget(participants: &[ManagedPreparedParticipant], guards: &[ManagedPreparedGuard]) -> Result<(), EngineError> {
    if participants.len().checked_add(guards.len()).ok_or(EngineError::LimitExceeded)? > MANAGED_MOVE_MAX_PARTICIPANTS {
        return Err(EngineError::LimitExceeded);
    }
    let mut paths = BTreeSet::new();
    for item in participants {
        paths.insert(item.participant.source.clone());
        if let Some(destination) = &item.participant.destination { paths.insert(destination.clone()); }
    }
    let mut total = 0u64;
    for item in participants {
        let before = item.before.len() as u64;
        let after = item.participant.replacement.as_ref().map_or(before, |value| value.len() as u64);
        total = total.checked_add(before).and_then(|value| value.checked_add(after))
            .and_then(|value| value.checked_add(after))
            .and_then(|value| value.checked_add(if item.participant.replacement.is_some() { after } else { 0 }))
            .ok_or(EngineError::LimitExceeded)?;
    }
    for guard in guards {
        if !paths.insert(guard.guard.source.clone()) { return Err(EngineError::Conflict); }
        total = total.checked_add(guard.byte_len).ok_or(EngineError::LimitExceeded)?;
    }
    if total > MANAGED_MOVE_MAX_TOTAL_BYTES { return Err(EngineError::LimitExceeded); }
    Ok(())
}

fn verify_managed_guards<F>(record: &ManagedJournalRecord, root: &(String, PathBuf), authorize: &mut F) -> Result<bool, EngineError>
where F: FnMut(&Path) -> Result<(String, PathBuf), EngineError> {
    let guards = record.plan.get("guards").and_then(serde_json::Value::as_array).ok_or(EngineError::Conflict)?;
    for guard in guards {
        let path = guard.get("source").and_then(serde_json::Value::as_str).map(Path::new).ok_or(EngineError::Conflict)?;
        let current=authorize(path)?;
        if current != *root && managed_destination_root(record)?.as_ref() != Some(&current) { return Err(EngineError::Conflict); }
        if verify_managed_guard(record, guard).is_err() { return Ok(false); }
    }
    Ok(true)
}

#[cfg(target_os = "linux")]
fn verify_managed_guard(record: &ManagedJournalRecord, guard: &serde_json::Value) -> io::Result<()> {
    let path = guard.get("source").and_then(serde_json::Value::as_str).map(Path::new)
        .ok_or_else(|| io::Error::other("managed guard source is unavailable"))?;
    let expected = guard.get("expected_fingerprint").and_then(serde_json::Value::as_str)
        .ok_or_else(|| io::Error::other("managed guard fingerprint is unavailable"))?;
    let file = open_managed_file(record, path)?;
    if !file.metadata()?.is_file() || guard.get("identity") != Some(&managed_object_identity(&file)?) {
        return Err(io::Error::other("managed guard identity changed"));
    }
    let mut bytes = Vec::new(); file.take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES || fingerprint_bytes(&bytes).value != expected {
        return Err(io::Error::other("managed guard fingerprint changed"));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn verify_managed_guard(_record: &ManagedJournalRecord, _guard: &serde_json::Value) -> io::Result<()> {
    Err(io::Error::new(io::ErrorKind::Unsupported, "managed guards are unavailable on this platform"))
}

struct ManagedPreparedParticipant {
    participant: ManagedParticipant,
    before: Vec<u8>,
    mode: Option<u32>,
    source_identity: serde_json::Value,
}

fn restore_managed_participants(journal: &ManagedJournal, record: &ManagedJournalRecord) -> io::Result<Vec<ManagedPreparedParticipant>> {
    let rows = record.plan.get("participants").and_then(serde_json::Value::as_array)
        .filter(|rows| !rows.is_empty() && rows.len() <= MANAGED_MOVE_MAX_PARTICIPANTS)
        .ok_or_else(|| io::Error::other("managed participant plan is invalid"))?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let field = |key: &str| row.get(key).and_then(serde_json::Value::as_str).map(str::to_owned)
            .ok_or_else(|| io::Error::other("managed participant identity is invalid"));
        let before = journal.read_stage(&field("before_stage")?, &field("before_digest")?)?;
        let replacement = match (row.get("replacement_stage"), row.get("replacement_digest")) {
            (Some(serde_json::Value::Null), Some(serde_json::Value::Null)) => None,
            (Some(stage), Some(digest)) => Some(journal.read_stage(stage.as_str().ok_or_else(|| io::Error::other("managed replacement stage is invalid"))?, digest.as_str().ok_or_else(|| io::Error::other("managed replacement digest is invalid"))?)?),
            _ => return Err(io::Error::other("managed replacement plan is invalid")),
        };
        let kind = serde_json::from_value(row.get("kind").cloned().ok_or_else(|| io::Error::other("managed participant kind is invalid"))?)
            .map_err(|_| io::Error::other("managed participant kind is invalid"))?;
        let destination = match row.get("destination") {
            Some(serde_json::Value::Null) => None,
            Some(value) => Some(PathBuf::from(value.as_str().ok_or_else(|| io::Error::other("managed destination is invalid"))?)),
            None => return Err(io::Error::other("managed destination is invalid")),
        };
        let participant = ManagedParticipant { kind, source:PathBuf::from(field("source")?), destination,
            expected_fingerprint:field("expected_fingerprint")?, replacement };
        if fingerprint_bytes(&before).value != participant.expected_fingerprint { return Err(io::Error::other("managed preimage fingerprint changed")); }
        let mode = row.get("mode").and_then(serde_json::Value::as_u64).map(|value| value as u32);
        let source_identity = row.get("source_identity").cloned().ok_or_else(|| io::Error::other("managed source identity is unavailable"))?;
        result.push(ManagedPreparedParticipant { participant, before, mode, source_identity });
    }
    Ok(result)
}

fn managed_capture_targets(participants: &[ManagedPreparedParticipant]) -> Vec<CaptureTarget> {
    let mut targets = Vec::new();
    for item in participants {
        targets.push(CaptureTarget { resource_id:item.participant.source.clone(), before:Some(item.before.clone()) });
        if let Some(destination) = &item.participant.destination {
            targets.push(CaptureTarget { resource_id:destination.clone(), before:None });
        }
    }
    targets
}

/// Construct the exact ordered History-registry batch from the service's own
/// checkpointed registrations. A reference-only edit retains its identity.
fn managed_registry_plan(
    participants: &[ManagedPreparedParticipant], registrations: &[serde_json::Value],
    cross_roots: Option<(&(String, PathBuf), &(String, PathBuf))>,
) -> io::Result<serde_json::Value> {
    let mut offset = 0usize; let mut moves = Vec::new();
    for participant in participants {
        let source = registrations.get(offset).and_then(serde_json::Value::as_object)
            .ok_or_else(|| io::Error::other("managed source registration is unavailable"))?;
        offset += 1;
        if participant.participant.destination.is_none() { continue; }
        let target = registrations.get(offset).and_then(serde_json::Value::as_object)
            .ok_or_else(|| io::Error::other("managed destination registration is unavailable"))?;
        offset += 1;
        let text = |value: &serde_json::Map<String, serde_json::Value>, key: &str| -> io::Result<String> {
            value.get(key).and_then(serde_json::Value::as_str).filter(|text| !text.is_empty())
                .map(str::to_owned).ok_or_else(|| io::Error::other("managed registration identity is invalid"))
        };
        let number = |value: &serde_json::Map<String, serde_json::Value>| -> io::Result<u64> {
            value.get("generation").and_then(serde_json::Value::as_u64).filter(|value| *value > 0)
                .ok_or_else(|| io::Error::other("managed registration generation is invalid"))
        };
        let source_id = text(source, "resource_id")?; let target_id = text(target, "resource_id")?;
        let source_root = text(source, "root_id")?; let target_root = text(target, "root_id")?;
        let (source_path, target_path) = (text(source, "root_path")?, text(target, "root_path")?);
        // Only a journal that recorded a distinct destination root may move an
        // identity between roots, and only from the recorded source root to it.
        let roots_match = match cross_roots {
            None => source_root == target_root && source_path == target_path,
            Some((from, to)) => source_root == from.0 && Path::new(&source_path) == from.1
                && target_root == to.0 && Path::new(&target_path) == to.1,
        };
        if source_id == target_id || !roots_match {
            return Err(io::Error::other("managed registrations cross root or identity"));
        }
        moves.push(serde_json::json!({"resource_id":source_id, "expected_generation":number(source)?,
            "expected_root_id":source_root, "expected_relative_path":text(source,"relative_path")?,
            "destination_root_id":target_root, "destination_root_path":text(target,"root_path")?,
            "destination_relative_path":text(target,"relative_path")?, "replaced_resource_id":target_id,
            "replaced_generation":number(target)?}));
    }
    if offset != registrations.len() || moves.is_empty() { return Err(io::Error::other("managed registration count is invalid")); }
    Ok(serde_json::Value::Array(moves))
}

/// Persist the immutable intent before staging any exact pre/postimage bytes.
/// This is still private to the unfinished coordinator and performs no source
/// mutation or History RPC.
fn stage_managed_preimages(
    journal: &ManagedJournal, owner_id: &str, request_digest: &str,
    root_id: &str, root_path: &Path, policy_generation: u64,
    history_action_id: &str, history_timestamp_millis: u64,
    participants: &[ManagedPreparedParticipant],
    guards: &[ManagedPreparedGuard], directory_guard: Option<&serde_json::Value>,
    destination_root: Option<&(String, PathBuf)>,
) -> io::Result<ManagedJournalRecord> {
    stage_managed_preimages_for(journal, owner_id, request_digest, root_id, root_path, policy_generation, history_action_id, history_timestamp_millis,
        participants, guards, directory_guard, destination_root, None)
}

/// `inverse_of` names the forward operation an undo reverses; it is part of the immutable plan so a replay must name the same one.
fn stage_managed_preimages_for(
    journal: &ManagedJournal, owner_id: &str, request_digest: &str,
    root_id: &str, root_path: &Path, policy_generation: u64,
    history_action_id: &str, history_timestamp_millis: u64,
    participants: &[ManagedPreparedParticipant],
    guards: &[ManagedPreparedGuard], directory_guard: Option<&serde_json::Value>,
    destination_root: Option<&(String, PathBuf)>, inverse_of: Option<&str>,
) -> io::Result<ManagedJournalRecord> {
    if root_id.is_empty() || !root_path.is_absolute() || history_action_id.is_empty() || history_action_id.len() > 128
        || !history_action_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "managed capture identity is invalid"));
    }
    let plan: Vec<serde_json::Value> = participants.iter().enumerate().map(|(index, item)| {
        serde_json::json!({"kind":item.participant.kind, "source":item.participant.source,
            "destination":item.participant.destination, "expected_fingerprint":item.participant.expected_fingerprint,
            "mode":item.mode, "source_identity":item.source_identity,
            "source_hold":item.participant.source.parent().map(|parent| parent.join(format!(".openclank-managed-{}-{index}.hold", journal.operation_id))),
            "before_stage":format!("before-{index}"), "before_digest":format!("{:x}", Sha256::digest(&item.before)),
            "replacement_stage":item.participant.replacement.as_ref().map(|_| format!("replacement-{index}")),
            "replacement_digest":item.participant.replacement.as_ref().map(|bytes| format!("{:x}", Sha256::digest(bytes)))})
    }).collect();
    let mut root_options = OpenOptions::new(); root_options.read(true);
    #[cfg(unix)]
    root_options.custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let root_file = root_options.open(root_path)?;
    if !root_file.metadata()?.is_dir() { return Err(io::Error::other("managed root is unavailable")); }
    let root_identity = managed_object_identity(&root_file)?;
    let mut candidate = ManagedJournalRecord { version:1, operation_id:journal.operation_id.clone(), owner_id:owner_id.to_owned(),
        request_digest:request_digest.to_owned(), phase:ManagedJournalPhase::Prepared,
        plan:serde_json::json!({"root_id":root_id, "root_path":root_path, "root_identity":root_identity, "policy_generation":policy_generation,
            "history_action_id":history_action_id, "history_timestamp_millis":history_timestamp_millis, "participants":plan,
            "guards":guards.iter().map(|guard| serde_json::json!({"source":guard.guard.source, "expected_fingerprint":guard.guard.expected_fingerprint, "identity":guard.identity, "byte_len":guard.byte_len})).collect::<Vec<_>>()}),
        file_stages:vec![], directory_events:vec![], commit_progress:vec![], cleanup_progress:vec![], preparation:vec![], registry_plan:None, history_prepare:None, history_completion:None, review_outcome:None };
    if let Some(guard) = directory_guard { candidate.plan["directory_guard"] = guard.clone(); }
    if let Some(forward) = inverse_of { candidate.plan["inverse_of"] = serde_json::json!(forward); }
    if let Some((destination_id, destination_path)) = destination_root {
        // Cross-root only. The two roots are distinct and never nested, so a
        // recorded path always belongs to exactly one of them.
        if destination_id.is_empty() || destination_id == root_id || !destination_path.is_absolute()
            || destination_path.starts_with(root_path) || root_path.starts_with(destination_path) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "managed destination root is invalid"));
        }
        let mut destination_options = OpenOptions::new(); destination_options.read(true);
        #[cfg(unix)]
        destination_options.custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let destination_file = destination_options.open(destination_path)?;
        if !destination_file.metadata()?.is_dir() { return Err(io::Error::other("managed destination root is unavailable")); }
        candidate.plan["destination_root_id"] = serde_json::json!(destination_id);
        candidate.plan["destination_root_path"] = serde_json::json!(destination_path);
        candidate.plan["destination_root_identity"] = managed_object_identity(&destination_file)?;
    }
    let record = match journal.read()? {
        Some(existing) => {
            if existing.owner_id != candidate.owner_id || existing.request_digest != candidate.request_digest || existing.plan != candidate.plan {
                return Err(io::Error::other("managed operation conflicts with its recorded plan"));
            }
            existing
        }
        None => { journal.write(&candidate)?; candidate }
    };
    if record.phase != ManagedJournalPhase::Prepared { return Err(io::Error::other("managed operation has already advanced")); }
    for (index, item) in participants.iter().enumerate() {
        journal.stage(&format!("before-{index}"), &item.before)?;
        if let Some(bytes) = &item.participant.replacement { journal.stage(&format!("replacement-{index}"), bytes)?; }
    }
    Ok(record)
}

/// Source-only preflight. The coordinator supplies a closure backed by the
/// authenticated service scope for every path; a request cannot supply it.
/// Directory moves and commit remain unavailable until their own exact
/// participant enumeration and pinned-path mutation are connected.
fn preflight_managed_files<F>(participants: &[ManagedParticipant], mut authorize: F) -> Result<Vec<ManagedPreparedParticipant>, EngineError>
where F: FnMut(&Path, bool) -> Result<(String, PathBuf), EngineError> {
    if participants.is_empty() || participants.len() > MANAGED_MOVE_MAX_PARTICIPANTS {
        return Err(EngineError::LimitExceeded);
    }
    let mut documents = 0usize; let mut total = 0u64;
    let mut sources = BTreeSet::new(); let mut destinations = BTreeSet::new();
    // Sources share one root. All destinations share one root, which may be a
    // different, non-nested root only when every touched object is on one
    // filesystem; a move therefore never spans more than two roots.
    let mut source_root: Option<(String, PathBuf)> = None;
    let mut destination_root: Option<(String, PathBuf)> = None;
    let mut devices = BTreeSet::new();
    let mut reference_roots: Vec<(String, PathBuf)> = Vec::new();
    let mut result = Vec::with_capacity(participants.len());
    let document_destination = participants.iter().find(|item| matches!(item.kind, ManagedParticipantKind::Document))
        .and_then(|item| item.destination.as_ref());
    for item in participants {
        match item.kind {
            ManagedParticipantKind::Document if item.destination.is_some() => documents += 1,
            ManagedParticipantKind::Asset if item.destination.is_some() && item.replacement.is_none() => {},
            ManagedParticipantKind::Reference if item.destination.is_none() && item.replacement.is_some() => {},
            _ => return Err(EngineError::Io(io::Error::new(io::ErrorKind::InvalidInput, "invalid managed participant"))),
        }
        if !sources.insert(item.source.clone()) { return Err(EngineError::Conflict); }
        let root = authorize(&item.source, false)?;
        // A reference edit is in place; it may live in the source root or, for an undo, in the root the documents return to.
        if matches!(item.kind, ManagedParticipantKind::Reference) { reference_roots.push(root.clone()); } else {
            if source_root.as_ref().is_some_and(|expected| expected != &root) { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            source_root.get_or_insert_with(|| root.clone());
        }
        managed_no_symlink(&root.1, &item.source, false)?;
        let meta = fs::symlink_metadata(&item.source)?;
        if !meta.is_file() || meta.len() > MANAGED_MOVE_MAX_BLOB_BYTES { return Err(EngineError::LimitExceeded); }
        devices.insert(metadata_device(&meta));
        if let Some(destination) = &item.destination {
            if matches!(item.kind, ManagedParticipantKind::Asset) {
                // The destination root's layout rule applies; a path outside
                // the source root is accepted only if its own root authorizes it.
                let layout_root = if destination.starts_with(&root.1) { root.1.clone() } else {
                    authorize(destination, true).map_err(|_| EngineError::Registry(RegistryError::OutsideRoot))?.1
                };
                let relative = destination.strip_prefix(&layout_root).map_err(|_| EngineError::Registry(RegistryError::OutsideRoot))?;
                let is_v2 = relative.components().any(|part| matches!(part, std::path::Component::Normal(name) if name == ".media"));
                if !is_v2 {
                    let document = document_destination.ok_or(EngineError::Conflict)?;
                    let stem = document.file_stem().filter(|name| !name.is_empty()).ok_or(EngineError::Conflict)?;
                    let filename = destination.file_name().ok_or(EngineError::Conflict)?;
                    let expected = document.parent().ok_or(EngineError::Conflict)?.join("media").join(stem).join(filename);
                    if *destination != expected { return Err(EngineError::Conflict); }
                }
            }
            if !destinations.insert(destination.clone()) { return Err(EngineError::Conflict); }
            let destination_pair = authorize(destination, true)?;
            if destination_root.as_ref().is_some_and(|expected| expected != &destination_pair) { return Err(EngineError::Conflict); }
            destination_root.get_or_insert_with(|| destination_pair.clone());
            let cross_root = destination_root.as_ref() != Some(&root);
            let destination_directory = if matches!(item.kind, ManagedParticipantKind::Asset) {
                Some(managed_media_ancestor(&destination_pair.1, destination)?)
            } else {
                managed_no_symlink(&destination_pair.1, destination, true)?;
                destination.parent().map(Path::to_path_buf)
            };
            if cross_root {
                let directory = destination_directory.ok_or(EngineError::Conflict)?;
                devices.insert(metadata_device(&fs::symlink_metadata(&directory)?));
            }
            match fs::symlink_metadata(destination) {
                Ok(_) => return Err(EngineError::DestinationExists),
                Err(error) if error.kind() == io::ErrorKind::NotFound => {},
                Err(error) => return Err(error.into()),
            }
        }
        let replacement_len = item.replacement.as_ref().map_or(0, |bytes| bytes.len() as u64);
        if replacement_len > MANAGED_MOVE_MAX_BLOB_BYTES { return Err(EngineError::LimitExceeded); }
        let postimage_len = if item.replacement.is_some() { replacement_len } else { meta.len() };
        // Count the private preimage, same-root postimage, durable History
        // postimage and, when different, private replacement bytes.
        total = total.checked_add(meta.len()).and_then(|sum| sum.checked_add(postimage_len))
            .and_then(|sum| sum.checked_add(postimage_len)).and_then(|sum| sum.checked_add(replacement_len))
            .ok_or(EngineError::LimitExceeded)?;
        if total > MANAGED_MOVE_MAX_TOTAL_BYTES { return Err(EngineError::LimitExceeded); }
        let mut options = OpenOptions::new(); options.read(true);
        #[cfg(unix)]
        options.custom_flags(libc::O_NOFOLLOW);
        let source_file = options.open(&item.source)?;
        let identity = managed_object_identity(&source_file)?;
        let mut before = Vec::new();
        (&source_file).take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut before)?;
        if before.len() as u64 != meta.len() || fingerprint_bytes(&before).value != item.expected_fingerprint
            || fingerprint_file(&item.source)?.value != item.expected_fingerprint
            || managed_object_identity(&options.open(&item.source)?)? != identity { return Err(EngineError::Conflict); }
        result.push(ManagedPreparedParticipant { participant:item.clone(), before, mode:file_mode(&item.source), source_identity:identity });
    }
    if documents != 1 || sources.iter().any(|source| destinations.contains(source)) { return Err(EngineError::Conflict); }
    if reference_roots.iter().any(|root| Some(root) != source_root.as_ref() && Some(root) != destination_root.as_ref()) { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
    if let (Some(from), Some(to)) = (&source_root, &destination_root) {
        if from != to {
            // Nested roots would make a path's owning root ambiguous on resume.
            if from.1.starts_with(&to.1) || to.1.starts_with(&from.1) { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            // No copy-and-delete fallback: every source and used destination
            // directory must already be on one filesystem.
            if devices.len() > 1 { return Err(EngineError::ManagedCrossFilesystem); }
        }
    }
    Ok(result)
}

#[cfg(unix)]
fn metadata_device(metadata: &fs::Metadata) -> u64 {
    use std::os::unix::fs::MetadataExt;
    metadata.dev()
}

#[cfg(not(unix))]
fn metadata_device(_metadata: &fs::Metadata) -> u64 { 0 }

fn managed_no_symlink(root: &Path, path: &Path, allow_missing_final: bool) -> Result<(), EngineError> {
    if !root.is_absolute() || !path.is_absolute() || lexical_normalize(path).as_path() != path || !component_contains(root, path) {
        return Err(EngineError::Registry(RegistryError::OutsideRoot));
    }
    let relative = path.strip_prefix(root).map_err(|_| EngineError::Registry(RegistryError::OutsideRoot))?;
    let count = relative.components().count(); let mut cursor = root.to_path_buf();
    if fs::symlink_metadata(&cursor)?.file_type().is_symlink() { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
    for (index, component) in relative.components().enumerate() {
        cursor.push(component.as_os_str());
        match fs::symlink_metadata(&cursor) {
            Ok(meta) if meta.file_type().is_symlink() => return Err(EngineError::Registry(RegistryError::OutsideRoot)),
            Ok(_) => {},
            Err(error) if allow_missing_final && index + 1 == count && error.kind() == io::ErrorKind::NotFound => {},
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

/// Validate an exact workspace-media path and find its nearest existing
/// directory without following a symlink. This does not create anything.
fn managed_media_ancestor(root: &Path, path: &Path) -> Result<PathBuf, EngineError> {
    managed_media_ancestor_inner(root, path, false)
}

// The relaxed path shape is only for an exact asset proven by the frozen
// folder operation. It does not authorize arbitrary paths under `media`.
fn managed_media_ancestor_inner(root: &Path, path: &Path, proven_folder_asset: bool) -> Result<PathBuf, EngineError> {
    use std::path::Component;
    if !root.is_absolute() || !path.is_absolute() || lexical_normalize(path).as_path() != path || !component_contains(root, path) {
        return Err(EngineError::Registry(RegistryError::OutsideRoot));
    }
    let relative = path.strip_prefix(root).map_err(|_| EngineError::Registry(RegistryError::OutsideRoot))?;
    let parts: Vec<_> = relative.components().collect();
    let v2 = parts.iter().any(|part| matches!(part, Component::Normal(name) if *name == ".media"));
    let v1 = parts.len() >= 3 && matches!(parts[parts.len() - 3], Component::Normal(name) if name == "media");
    if parts.len() < 3 || !(v1 || v2 || proven_folder_asset) {
        return Err(EngineError::Registry(RegistryError::OutsideRoot));
    }
    if fs::symlink_metadata(root)?.file_type().is_symlink() { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
    let mut cursor = root.to_path_buf();
    for part in &parts[..parts.len() - 1] {
        let Component::Normal(name) = part else { return Err(EngineError::Registry(RegistryError::OutsideRoot)); };
        let next = cursor.join(name);
        match fs::symlink_metadata(&next) {
            Ok(meta) if meta.file_type().is_symlink() || !meta.is_dir() => return Err(EngineError::Registry(RegistryError::OutsideRoot)),
            Ok(_) => cursor = next,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(cursor),
            Err(error) => return Err(error.into()),
        }
    }
    Ok(cursor)
}

fn folder_cross_asset_destination_proven(destination_root:&Path, source:&Path, destination:&Path, asset:&ManagedFolderAsset) -> bool {
    let (Some(source_frame),Some(frame))=(asset.source_frame.as_ref(),asset.destination_frame.as_ref()) else{return false;};
    if source_frame.origin != asset.origin || source_frame.canonical_root != asset.workspace_root || source_frame.layout_version != asset.layout_version
        || !source_frame.workspace_relative_folder.is_empty() || (source_frame.origin == "workspace") != source_frame.workspace_id.is_some() {return false;}
    let Ok(suffix)=asset.document.strip_prefix(source) else{return false;};
    if suffix.as_os_str().is_empty() || asset.document_destination != destination.join(suffix)
        || !asset.workspace_root.is_absolute() || lexical_normalize(&asset.workspace_root)!=asset.workspace_root
        || !frame.canonical_root.is_absolute() || lexical_normalize(&frame.canonical_root)!=frame.canonical_root
        || !frame.canonical_root.starts_with(destination_root) || !frame.workspace_relative_folder.is_empty()
        || (frame.origin == "workspace") != frame.workspace_id.is_some() || asset.fingerprint.is_empty()
        || asset.path.file_name().is_none() || asset.path.file_name()!=asset.destination.file_name()
        || [&asset.path,&asset.destination,&asset.document,&asset.document_destination].iter().any(|path|!path.is_absolute() || lexical_normalize(path)!=**path) {return false;}
    let Some(name)=asset.path.file_name() else{return false;};
    let layout=|origin:&str,version:u8,base:&Path,document:&Path| -> Option<PathBuf> {
        let relative=document.strip_prefix(base).ok()?;let parent=relative.parent()?;
        let stem=relative.file_stem().filter(|v|!v.is_empty())?;
        match (origin,version) {
            ("workspace",1) => Some(base.join("media").join(parent).join(stem).join(name)),
            ("workspace",2) | ("selected_root",2) => Some(base.join(".media").join(parent).join(stem).join(name)),
            ("loose",1) if parent.as_os_str().is_empty() => Some(base.join("media").join(stem).join(name)),
            _ => None,
        }
    };
    layout(&asset.origin,asset.layout_version,&asset.workspace_root,&asset.document)==Some(asset.path.clone())
        && layout(&frame.origin,frame.layout_version,&frame.canonical_root,&asset.document_destination)==Some(asset.destination.clone())
        && asset.destination.starts_with(destination_root)
}

fn folder_asset_destination_proven(root: &Path, source: &Path, destination: &Path, asset: &ManagedFolderAsset) -> bool {
    if asset.destination_frame.is_some() {return folder_cross_asset_destination_proven(root,source,destination,asset);}
    let Ok(suffix) = asset.document.strip_prefix(source) else { return false; };
    if suffix.as_os_str().is_empty() || asset.document_destination != destination.join(suffix)
        || asset.origin != "workspace" || !asset.workspace_root.starts_with(root)
        || !asset.workspace_root.is_absolute() || lexical_normalize(&asset.workspace_root) != asset.workspace_root
        || [&asset.path, &asset.destination, &asset.document, &asset.document_destination].iter()
            .any(|path| !path.is_absolute() || lexical_normalize(path.as_path()) != path.to_path_buf())
        || !asset.document.starts_with(&asset.workspace_root) || asset.path.file_name() != asset.destination.file_name()
        || asset.path.file_name().is_none() || asset.fingerprint.is_empty() { return false; }
    let Ok(relative) = asset.document_destination.strip_prefix(&asset.workspace_root) else { return false; };
    let Some(name) = asset.path.file_name() else { return false; };
    let expected = match asset.layout_version {
        1 => { let mut stem = relative.to_path_buf(); stem.set_extension(""); asset.workspace_root.join("media").join(stem).join(name) },
        2 => {
            let (Some(parent), Some(stem)) = (relative.parent(), relative.file_stem().filter(|stem| !stem.is_empty())) else { return false; };
            asset.workspace_root.join(".media").join(parent).join(stem).join(name)
        },
        _ => return false,
    };
    expected == asset.destination && lexical_normalize(&expected) == expected && expected.starts_with(root)
}

fn folder_journal_asset_proven(record: &ManagedJournalRecord, root: &Path, path: &Path) -> bool {
    let Some(folder) = record.plan.get("folder") else { return false; };
    let (Some(source), Some(destination), Some(assets)) = (folder.get("source").and_then(serde_json::Value::as_str),
        folder.get("destination").and_then(serde_json::Value::as_str), folder.get("asset_proofs").and_then(serde_json::Value::as_array)) else { return false; };
    assets.iter().filter_map(|value| serde_json::from_value::<ManagedFolderAsset>(value.clone()).ok())
        .any(|asset| asset.destination == path && folder_asset_destination_proven(root, Path::new(source), Path::new(destination), &asset))
}

#[cfg(unix)]
fn managed_object_identity(file: &File) -> io::Result<serde_json::Value> {
    use std::os::unix::fs::MetadataExt;
    let metadata = file.metadata()?;
    Ok(serde_json::json!({"kind":"unix", "device":metadata.dev(), "inode":metadata.ino()}))
}

#[cfg(windows)]
fn managed_object_identity(file: &File) -> io::Result<serde_json::Value> {
    let identity = crate::windows_fs::opened_identity(file)?;
    Ok(serde_json::json!({"kind":"windows", "volume":identity.volume, "file_id":identity.file_id}))
}

#[cfg(not(any(unix, windows)))]
fn managed_object_identity(_file: &File) -> io::Result<serde_json::Value> {
    Err(io::Error::new(io::ErrorKind::Unsupported, "managed object identity is unavailable"))
}

/// Root that owns `path` in a journaled file move, with its recorded identity.
/// A cross-root record carries one extra, distinct destination root that is
/// never nested in the primary root; a record without it (every record
/// written before cross-root moves) always resolves to its single root.
fn managed_record_root<'a>(record: &'a ManagedJournalRecord, path: &Path) -> Option<(&'a Path, Option<&'a serde_json::Value>)> {
    let primary = record.plan.get("root_path").and_then(serde_json::Value::as_str).map(Path::new)?;
    if !path.starts_with(primary) {
        if let Some(second) = record.plan.get("destination_root_path").and_then(serde_json::Value::as_str).map(Path::new) {
            if path.starts_with(second) { return Some((second, record.plan.get("destination_root_identity"))); }
        }
    }
    Some((primary, record.plan.get("root_identity")))
}

/// The recorded primary (source) root of a file or folder journal.
fn managed_source_root(record: &ManagedJournalRecord) -> Result<(String, PathBuf), EngineError> {
    let id = record.plan.get("root_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
    let path = record.plan.get("root_path").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
    Ok((id.to_owned(), PathBuf::from(path)))
}

/// The recorded distinct destination root of a cross-root file move, if any.
/// A half-written pair fails closed instead of degrading to a single root.
fn managed_destination_root(record: &ManagedJournalRecord) -> Result<Option<(String, PathBuf)>, EngineError> {
    let id = record.plan.get("destination_root_id").and_then(serde_json::Value::as_str);
    let path = record.plan.get("destination_root_path").and_then(serde_json::Value::as_str);
    match (id, path) {
        (Some(id), Some(path)) => Ok(Some((id.to_owned(), PathBuf::from(path)))),
        (None, None) if record.plan.get("destination_root_identity").is_none() => Ok(None),
        _ => Err(EngineError::Conflict),
    }
}

/// Open an existing directory by walking from the recorded root descriptor;
/// no intermediate path component is followed through a symlink.
#[cfg(target_os = "linux")]
fn open_managed_directory(record: &ManagedJournalRecord, directory: &Path) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Component;
    let (root, expected_identity) = managed_record_root(record, directory)
        .ok_or_else(|| io::Error::other("managed root is unavailable"))?;
    let relative = directory.strip_prefix(root).map_err(|_| io::Error::other("managed directory is outside root"))?;
    let mut options = OpenOptions::new();
    options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let mut pinned = options.open(root)?;
    if expected_identity != Some(&managed_object_identity(&pinned)?) {
        return Err(io::Error::other("managed root identity changed"));
    }
    let mut current = root.to_path_buf();
    let selected = [record.plan.get("directory_guard"),record.plan.get("destination_directory_guard")];
    for guard in selected.into_iter().flatten().filter(|value| !value.is_null()) {
        if guard.get("path").and_then(serde_json::Value::as_str) == current.to_str()
            && guard.get("identity") != Some(&managed_object_identity(&pinned)?) { return Err(io::Error::other("selected directory changed")); }
    }
    for part in relative.components() {
        let Component::Normal(name) = part else { return Err(io::Error::other("managed directory is invalid")); };
        let name = CString::new(name.as_bytes()).map_err(io::Error::other)?;
        // SAFETY: the parent descriptor and NUL-terminated component remain live.
        let fd = unsafe { libc::openat(pinned.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        // SAFETY: openat returned a fresh owned descriptor.
        pinned = unsafe { File::from_raw_fd(fd) };
        current.push(part.as_os_str());
        if record.plan.get("kind").and_then(serde_json::Value::as_str)==Some("checkout") {
            if let Some((index,_))=record.plan.get("participants").and_then(serde_json::Value::as_array).into_iter().flatten().enumerate().find(|(_,item)|item.get("directory").and_then(serde_json::Value::as_bool)==Some(true) && item.get("destination").and_then(serde_json::Value::as_str)==current.to_str()) {
                let event=record.commit_progress.iter().find(|v|v.get("kind").and_then(serde_json::Value::as_str)==Some("checkout_done") && v.get("index").and_then(serde_json::Value::as_u64)==Some(index as u64)).ok_or_else(||io::Error::other("checkout directory is not yet owned"))?;
                if event.get("identity")!=Some(&managed_object_identity(&pinned)?) || record.directory_events.iter().any(|v|v.get("kind").and_then(serde_json::Value::as_str)==Some("checkout_rollback_done")&&v.get("index").and_then(serde_json::Value::as_u64)==Some(index as u64)){return Err(io::Error::other("checkout directory identity changed"))}
            }
        }
        for guard in selected.into_iter().flatten().filter(|value| !value.is_null()) {
            if guard.get("path").and_then(serde_json::Value::as_str) == current.to_str()
                && guard.get("identity") != Some(&managed_object_identity(&pinned)?) { return Err(io::Error::other("selected directory changed")); }
        }
    }
    Ok(pinned)
}

#[cfg(target_os = "linux")]
fn open_managed_file(record: &ManagedJournalRecord, path: &Path) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    let parent = path.parent().ok_or_else(|| io::Error::other("managed file has no parent"))?;
    let directory = open_managed_directory(record, parent)?;
    let name = CString::new(path.file_name().ok_or_else(|| io::Error::other("managed file has no name"))?.as_bytes()).map_err(io::Error::other)?;
    // SAFETY: the pinned directory and NUL-terminated name remain live.
    let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    // SAFETY: openat returned a fresh owned descriptor.
    Ok(unsafe { File::from_raw_fd(fd) })
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ManagedJournalRecord {
    version: u8,
    operation_id: String,
    owner_id: String,
    request_digest: String,
    phase: ManagedJournalPhase,
    // The coordinator must populate this with the exact durable participant
    // and History wire plan before it issues any mutating RPC.
    plan: serde_json::Value,
    // Same-root staging objects are recorded by stable OS identity, not by
    // their names or matching bytes alone.
    #[serde(default)]
    file_stages: Vec<serde_json::Value>,
    // Append-only ownership evidence for operation-created media containers.
    #[serde(default)]
    directory_events: Vec<serde_json::Value>,
    #[serde(default)]
    commit_progress: Vec<serde_json::Value>,
    #[serde(default)]
    cleanup_progress: Vec<serde_json::Value>,
    // Registration and stage results are append-only, so an interrupted
    // prepare cannot replace an already observed opaque identity.
    #[serde(default)]
    preparation: Vec<serde_json::Value>,
    #[serde(default)]
    registry_plan: Option<serde_json::Value>,
    #[serde(default)]
    history_prepare: Option<serde_json::Value>,
    // Once computed from committed bytes, this exact completion request is
    // persisted before RecordLive/CompleteBatch and cannot be replaced.
    #[serde(default)]
    history_completion: Option<serde_json::Value>,
    #[serde(default)]
    review_outcome: Option<String>,
}

// A crashed prepare may already have frozen the previous release's exact
// wire digest. Reissue it with that original representation.
impl ManagedJournalRecord {
    fn legacy_prepare_hook(&self, hook: &dyn MutationCaptureHook) -> Option<Box<dyn MutationCaptureHook>> {
        let request = self.history_prepare.as_ref()?.get("request")?;
        if request.pointer("/coverage/metadata/required_recovery").is_none() {
            return hook.legacy_prepare_hook();
        }
        None
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum ManagedJournalPhase {
    Prepared,
    HistoryPrepared,
    Committing,
    LiveCommitted,
    HistoryPending,
    Complete,
    NeedsReview,
}

#[cfg(target_os = "linux")]
fn folder_internal_replacement(record: &ManagedJournalRecord, participant: &serde_json::Value) -> io::Result<Option<(PathBuf, PathBuf)>> {
    if record.plan.get("kind").and_then(serde_json::Value::as_str) != Some("folder")
        || !record.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("folder_published"))
        || (participant.get("replacement_digest").and_then(serde_json::Value::as_str).is_none()
            && participant.get("internal_relocation").and_then(serde_json::Value::as_bool) != Some(true)) { return Ok(None); }
    let folder = record.plan.get("folder").ok_or_else(|| io::Error::other("managed folder plan is unavailable"))?;
    let source_root = folder.get("source").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("managed folder source is unavailable"))?;
    let destination_root = folder.get("destination").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("managed folder destination is unavailable"))?;
    let source = participant.get("source").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("managed participant source is unavailable"))?;
    let Ok(suffix) = source.strip_prefix(source_root) else { return Ok(None); };
    if suffix.as_os_str().is_empty() { return Err(io::Error::other("folder root cannot be replaced")); }
    let destination = participant.get("destination").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("managed participant destination is unavailable"))?;
    let structural=destination_root.join(suffix);
    if destination != structural && participant.get("internal_relocation").and_then(serde_json::Value::as_bool) != Some(true) {return Err(io::Error::other("managed folder destination changed"));}
    let hold = participant.get("source_hold").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("managed participant hold is unavailable"))?;
    let hold_suffix = hold.strip_prefix(source_root).map_err(io::Error::other)?;
    let hold_path = if record.plan.get("inverse_of").is_some()
        && participant.get("internal_relocation").and_then(serde_json::Value::as_bool)==Some(true) {
        destination_root.parent().ok_or_else(||io::Error::other("inverse custody parent unavailable"))?
            .join(hold.file_name().ok_or_else(||io::Error::other("inverse custody name unavailable"))?)
    } else { destination_root.join(hold_suffix) };
    Ok(Some((structural, hold_path)))
}

#[cfg(target_os = "linux")]
fn folder_owned_exclusions(record: &ManagedJournalRecord, destination: &Path) -> io::Result<HashMap<PathBuf, serde_json::Value>> {
    let mut result = HashMap::new();
    let cleaned=|key:&str| record.cleanup_progress.iter().any(|event|event.get("kind").and_then(serde_json::Value::as_str)==Some("done") && event.get("key").and_then(serde_json::Value::as_str)==Some(key));
    for stage in &record.file_stages {
        let index=stage.get("index").and_then(serde_json::Value::as_u64).ok_or_else(||io::Error::other("folder stage index unavailable"))?;
        if cleaned(&format!("stage-{index}")) {continue;}
        let path = stage.get("path").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("folder stage path is unavailable"))?;
        let Ok(relative) = path.strip_prefix(destination) else { continue; };
        let identity = stage.get("identity").cloned().ok_or_else(|| io::Error::other("folder stage identity is unavailable"))?;
        if relative.as_os_str().is_empty() || result.insert(relative.to_path_buf(), identity).is_some() {
            return Err(io::Error::other("folder owned stage conflicts"));
        }
    }
    let participants = record.plan.get("participants").and_then(serde_json::Value::as_array).ok_or_else(|| io::Error::other("folder participants are unavailable"))?;
    for event in &record.commit_progress {
        if event.get("kind").and_then(serde_json::Value::as_str) != Some("source_held") { continue; }
        let index = event.get("index").and_then(serde_json::Value::as_u64).and_then(|value| usize::try_from(value).ok()).ok_or_else(|| io::Error::other("folder held index is invalid"))?;
        if cleaned(&format!("hold-{index}")) {continue;}
        let participant = participants.get(index).ok_or_else(|| io::Error::other("folder held participant is unavailable"))?;
        let Some((_source, hold)) = folder_internal_replacement(record, participant)? else { continue; };
        let Ok(relative) = hold.strip_prefix(destination) else { continue; };
        let identity = event.get("identity").cloned().ok_or_else(|| io::Error::other("folder held identity is unavailable"))?;
        if participant.get("source_identity") != Some(&identity) || relative.as_os_str().is_empty()
            || result.insert(relative.to_path_buf(), identity).is_some() {
            return Err(io::Error::other("folder owned hold conflicts"));
        }
    }
    Ok(result)
}

#[cfg(target_os = "linux")]
fn verify_folder_postimage(journal: &ManagedJournal, record: &ManagedJournalRecord, snapshot: &ManagedFolderSnapshot) -> io::Result<bool> {
    let folder = record.plan.get("folder").ok_or_else(|| io::Error::other("folder plan is unavailable"))?;
    if folder.get("source_identity") != Some(&snapshot.root_identity) { return Ok(false); }
    let source = folder.get("source").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("folder source is unavailable"))?;
    let destination=folder.get("destination").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(||io::Error::other("folder destination unavailable"))?;
    let digest = folder.get("manifest_digest").and_then(serde_json::Value::as_str).ok_or_else(|| io::Error::other("folder manifest is unavailable"))?;
    let before: serde_json::Value = serde_json::from_slice(&journal.read_stage("folder-before", digest)?).map_err(io::Error::other)?;
    let after: serde_json::Value = serde_json::from_slice(&snapshot.history).map_err(io::Error::other)?;
    let entries = |value: &serde_json::Value| -> io::Result<HashMap<PathBuf, serde_json::Value>> {
        let mut result = HashMap::new();
        for entry in value.get("entries").and_then(serde_json::Value::as_array).ok_or_else(|| io::Error::other("folder manifest is invalid"))? {
            let path = entry.get("path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or_else(|| io::Error::other("folder manifest path is invalid"))?;
            if result.insert(path, entry.clone()).is_some() { return Err(io::Error::other("folder manifest path is duplicated")); }
        }
        Ok(result)
    };
    let identities = |values: &[serde_json::Value]| -> io::Result<HashMap<PathBuf, serde_json::Value>> {
        let mut result = HashMap::new();
        for item in values {
            let path = item.get("path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or_else(|| io::Error::other("folder identity path is invalid"))?;
            let identity = item.get("identity").cloned().ok_or_else(|| io::Error::other("folder identity is unavailable"))?;
            if result.insert(path, identity).is_some() { return Err(io::Error::other("folder identity path is duplicated")); }
        }
        Ok(result)
    };
    let before_entries = entries(&before)?; let after_entries = entries(&after)?;
    let before_identities = identities(folder.get("identities").and_then(serde_json::Value::as_array).ok_or_else(|| io::Error::other("folder identities are unavailable"))?)?;
    let after_identities = identities(&snapshot.identities)?;
    let relocated: BTreeSet<PathBuf> = record.plan.get("participants").and_then(serde_json::Value::as_array).ok_or_else(||io::Error::other("folder participants unavailable"))?.iter()
        .filter(|item|item.get("internal_relocation").and_then(serde_json::Value::as_bool)==Some(true))
        .map(|item|item.get("source").and_then(serde_json::Value::as_str).map(Path::new).and_then(|path|path.strip_prefix(source).ok()).map(Path::to_path_buf).ok_or_else(||io::Error::other("folder relocation path unavailable")))
        .collect::<io::Result<_>>()?;
    let participants = record.plan.get("participants").and_then(serde_json::Value::as_array).ok_or_else(|| io::Error::other("folder participants are unavailable"))?;
    let mut added_files:HashMap<PathBuf,(String,u64,serde_json::Value)>=HashMap::new();
    for (index,item) in participants.iter().enumerate() {
        let Some(target)=item.get("destination").and_then(serde_json::Value::as_str).map(Path::new) else {continue;};
        let Ok(relative)=target.strip_prefix(destination) else {continue;};
        let original=item.get("source").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(||io::Error::other("folder source unavailable"))?;
        if original.starts_with(source) && item.get("internal_relocation").and_then(serde_json::Value::as_bool)!=Some(true) {continue;}
        if relative.as_os_str().is_empty() || before_entries.contains_key(relative) {return Ok(false);}
        let stage=record.file_stages.iter().find(|value|value.get("index").and_then(serde_json::Value::as_u64)==Some(index as u64)).ok_or_else(||io::Error::other("folder asset stage unavailable"))?;
        if !record.commit_progress.iter().any(|event|event.get("kind").and_then(serde_json::Value::as_str)==Some("published") && event.get("index").and_then(serde_json::Value::as_u64)==Some(index as u64)) {return Ok(false);}
        let digest=item.get("before_digest").and_then(serde_json::Value::as_str).ok_or_else(||io::Error::other("folder asset digest unavailable"))?;
        let label=item.get("before_stage").and_then(serde_json::Value::as_str).ok_or_else(||io::Error::other("folder asset stage unavailable"))?;
        let bytes=journal.read_stage(label,digest)?;let identity=stage.get("identity").cloned().ok_or_else(||io::Error::other("folder asset identity unavailable"))?;
        if added_files.insert(relative.to_path_buf(),(format!("sha256:{digest}"),bytes.len() as u64,identity)).is_some(){return Ok(false);}
    }
    let mut added_directories:HashMap<PathBuf,serde_json::Value>=HashMap::new();
    for event in &record.directory_events {
        if event.get("kind").and_then(serde_json::Value::as_str)!=Some("published") {continue;}
        let path=event.get("path").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(||io::Error::other("folder directory unavailable"))?;
        let Ok(relative)=path.strip_prefix(destination) else {continue;};
        if relative.as_os_str().is_empty() || before_entries.contains_key(relative) || added_files.contains_key(relative) {return Ok(false);}
        let identity=event.get("identity").cloned().ok_or_else(||io::Error::other("folder directory identity unavailable"))?;
        if added_directories.insert(relative.to_path_buf(),identity).is_some(){return Ok(false);}
    }
    let mut pruned=BTreeSet::new();
    for proof in record.plan.get("inverse_prune").and_then(serde_json::Value::as_array).into_iter().flatten() {
        let relative=proof.get("relative").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or_else(||io::Error::other("inverse directory proof unavailable"))?;
        let identity=proof.get("identity").ok_or_else(||io::Error::other("inverse directory proof unavailable"))?;
        let path=destination.join(&relative);
        if before_entries.get(&relative).and_then(|item|item.get("type")).and_then(serde_json::Value::as_str)!=Some("directory")
            || before_identities.get(&relative)!=Some(identity) || after_entries.contains_key(&relative)
            || !record.directory_events.iter().any(|event|event.get("kind").and_then(serde_json::Value::as_str)==Some("inverse_prune_done") && event.get("path").and_then(serde_json::Value::as_str).map(Path::new)==Some(path.as_path()) && event.get("identity")==Some(identity))
            || !pruned.insert(relative) {return Ok(false);}
    }
    let removals=relocated.len()+pruned.len();
    let additions=added_files.len()+added_directories.len();
    if before_entries.len().checked_sub(removals).and_then(|n|n.checked_add(additions)) != Some(after_entries.len())
        || before_identities.len().checked_sub(removals).and_then(|n|n.checked_add(additions)) != Some(after_identities.len()) {return Ok(false);}
    for (path,(digest,size,identity)) in &added_files {
        let Some(item)=after_entries.get(path) else{return Ok(false);};
        if item.get("type").and_then(serde_json::Value::as_str)!=Some("file") || item.get("sha256").and_then(serde_json::Value::as_str)!=Some(digest.as_str())
            || item.get("size").and_then(serde_json::Value::as_u64)!=Some(*size) || after_identities.get(path)!=Some(identity){return Ok(false);}
    }
    for (path,identity) in &added_directories {
        if after_entries.get(path).and_then(|item|item.get("type")).and_then(serde_json::Value::as_str)!=Some("directory")
            || after_identities.get(path)!=Some(identity){return Ok(false);}
    }
    let mut edits = HashMap::new();
    for (index, participant) in participants.iter().enumerate() {
        if participant.get("replacement_digest").and_then(serde_json::Value::as_str).is_none() { continue; }
        let path = participant.get("source").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(|| io::Error::other("folder participant source is unavailable"))?;
        let Ok(relative) = path.strip_prefix(source) else { continue; };
        if relative.as_os_str().is_empty() { return Ok(false); }
        let stage = record.file_stages.iter().find(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64))
            .ok_or_else(|| io::Error::other("folder replacement stage is unavailable"))?;
        if !record.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("published") && event.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64)) {
            return Ok(false);
        }
        let label = participant.get("replacement_stage").and_then(serde_json::Value::as_str).ok_or_else(|| io::Error::other("folder replacement is unavailable"))?;
        let digest = participant.get("replacement_digest").and_then(serde_json::Value::as_str).ok_or_else(|| io::Error::other("folder replacement is unavailable"))?;
        let bytes = journal.read_stage(label, digest)?;
        let identity = stage.get("identity").cloned().ok_or_else(|| io::Error::other("folder replacement identity is unavailable"))?;
        if edits.insert(relative.to_path_buf(), (format!("sha256:{digest}"), bytes.len() as u64, identity)).is_some() { return Ok(false); }
    }
    for (path, old) in &before_entries {
        if pruned.contains(path) {continue;}
        if relocated.contains(path) {
            if old.get("type").and_then(serde_json::Value::as_str)!=Some("file") || after_entries.contains_key(path) {return Ok(false);}
            continue;
        }
        let Some(new) = after_entries.get(path) else { return Ok(false); };
        if old.get("type") != new.get("type") || old.get("mode") != new.get("mode") { return Ok(false); }
        let expected_identity = if let Some((digest, size, identity)) = edits.get(path) {
            if old.get("type").and_then(serde_json::Value::as_str) != Some("file")
                || new.get("sha256").and_then(serde_json::Value::as_str) != Some(digest.as_str())
                || new.get("size").and_then(serde_json::Value::as_u64) != Some(*size) { return Ok(false); }
            identity
        } else {
            if old.get("type").and_then(serde_json::Value::as_str) == Some("file")
                && (old.get("sha256") != new.get("sha256") || old.get("size") != new.get("size")) { return Ok(false); }
            let Some(identity) = before_identities.get(path) else { return Ok(false); };
            identity
        };
        if after_identities.get(path) != Some(expected_identity) { return Ok(false); }
    }
    Ok(true)
}

impl ManagedJournal {
    fn with_capture(mut self, capture: Option<Arc<dyn MutationCaptureHook>>) -> Self {
        self.capture = capture;
        self
    }

    // A failed cleanup never changes a durable mutation outcome. Reopening
    // the same terminal operation retries cleanup under its original authority.
    fn settle_terminal_capture(&self, record: &ManagedJournalRecord) {
        if record.phase != ManagedJournalPhase::Complete { return; }
        let Some(hook) = &self.capture else { return; };
        let Some(prepare) = &record.history_prepare else { return; };
        let request = prepare.get("request").unwrap_or(prepare);
        if request.pointer("/coverage/metadata/required_recovery").and_then(serde_json::Value::as_bool) != Some(true) { return; }
        let Some(action) = record.plan["history_action_id"].as_str() else { return; };
        if request["action_id"].as_str() == Some(action)
            && hook.managed_action_id(&self.operation_id).as_deref() == Ok(action) {
            let _ = hook.settle_recovery(action);
        }
    }

    /// Open an already-owned journal without creating or changing any path.
    fn open_existing(root: &Path, operation_id: &str, exclusive: bool) -> io::Result<Option<Self>> {
        if !root.is_absolute() || operation_id.len() < 16 || operation_id.len() > 80
            || !operation_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid managed operation"));
        }
        for component in root.ancestors() {
            let metadata = fs::symlink_metadata(component)?;
            if metadata.file_type().is_symlink() { return Err(io::Error::other("managed journal path is symlinked")); }
        }
        let mut options = OpenOptions::new(); options.read(true);
        #[cfg(unix)]
        options.custom_flags(libc::O_NOFOLLOW);
        let lock = match options.open(root.join(format!("{operation_id}.lock"))) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        if !lock.metadata()?.is_file() { return Err(io::Error::other("managed journal lock is invalid")); }
        if exclusive { lock.lock()?; } else { lock.lock_shared()?; }
        Ok(Some(Self { capture:None, directory:root.to_path_buf(), operation_id:operation_id.to_owned(), _lock:lock, exclusive }))
    }
    /// Nonblocking shared custody for private worktree continuation. A held
    /// original owner is unavailable, never a reason to wait or adopt it.
    #[cfg(target_os="linux")]
    fn open_existing_nowait(root:&Path,operation_id:&str)->io::Result<Option<Self>> {
        use std::os::fd::AsRawFd;
        if !root.is_absolute() || !(16..=80).contains(&operation_id.len()) || !operation_id.bytes().all(|c|c.is_ascii_alphanumeric() || c==b'-' || c==b'_') {return Err(io::Error::other("managed operation unavailable"))}
        for component in root.ancestors(){if fs::symlink_metadata(component)?.file_type().is_symlink(){return Err(io::Error::other("managed journal unavailable"))}}
        let mut options=OpenOptions::new();options.read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC);
        let lock=match options.open(root.join(format!("{operation_id}.lock"))){Ok(v)=>v,Err(e) if e.kind()==io::ErrorKind::NotFound=>return Ok(None),Err(e)=>return Err(e)};
        if !lock.metadata()?.is_file() || unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_SH|libc::LOCK_NB)}!=0 {return Err(io::Error::other("managed journal held"))}
        Ok(Some(Self{capture:None,directory:root.to_path_buf(),operation_id:operation_id.to_owned(),_lock:lock,exclusive:false}))
    }

    /// Remove only forward-created, pinned directories made empty by this
    /// inverse. The durable intent makes an interrupted unlink replayable.
    #[cfg(target_os = "linux")]
    fn prune_inverse_directories(&self, record:&mut ManagedJournalRecord) -> io::Result<()> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd,FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        if record.phase != ManagedJournalPhase::Committing || record.history_prepare.is_none() || record.plan.get("inverse_of").is_none() {
            return Err(io::Error::other("inverse directory pruning is not admitted"));
        }
        let root=record.plan.pointer("/folder/destination").and_then(serde_json::Value::as_str).map(PathBuf::from)
            .ok_or_else(||io::Error::other("inverse folder unavailable"))?;
        let items=record.plan.get("inverse_prune").and_then(serde_json::Value::as_array).cloned()
            .ok_or_else(||io::Error::other("inverse directory proof unavailable"))?;
        if items.len()>MANAGED_MOVE_MAX_PARTICIPANTS {return Err(io::Error::other("inverse directory proof exceeds bound"));}
        for item in items {
            let relative=item.get("relative").and_then(serde_json::Value::as_str).map(Path::new).ok_or_else(||io::Error::other("inverse directory path unavailable"))?;
            let identity=item.get("identity").cloned().ok_or_else(||io::Error::other("inverse directory identity unavailable"))?;
            if relative.is_absolute() || relative.as_os_str().is_empty() || lexical_normalize(relative)!=relative {return Err(io::Error::other("inverse directory path invalid"));}
            let path=root.join(relative); let parent=path.parent().ok_or_else(||io::Error::other("inverse directory parent unavailable"))?;
            let done=serde_json::json!({"kind":"inverse_prune_done","path":path,"identity":identity});
            if record.directory_events.contains(&done) {continue;}
            let directory=open_managed_directory(record,parent)?;
            let parent_identity=managed_object_identity(&directory)?;
            let quarantine_name=format!(".openclank-prune-{}-{:x}",self.operation_id,Sha256::digest(relative.as_os_str().as_bytes()));
            let intent=serde_json::json!({"kind":"inverse_prune_intent","path":path,"identity":identity,"parent_identity":parent_identity,"quarantine":parent.join(&quarantine_name)});
            let ready=serde_json::json!({"kind":"inverse_prune_unlink","path":path,"identity":identity});
            let restore_intent=serde_json::json!({"kind":"inverse_prune_restore_intent","path":path,"identity":identity,"quarantine":parent.join(&quarantine_name)});
            let restore_done=serde_json::json!({"kind":"inverse_prune_restore_done","path":path,"identity":identity});
            let prior=record.directory_events.contains(&intent);
            let name=CString::new(path.file_name().ok_or_else(||io::Error::other("inverse directory name unavailable"))?.as_bytes()).map_err(io::Error::other)?;
            let quarantine=CString::new(quarantine_name).map_err(io::Error::other)?;
            let open_dir=|name:&CString|->io::Result<Option<File>> {
                let fd=unsafe {libc::openat(directory.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
                if fd>=0 {return Ok(Some(unsafe {File::from_raw_fd(fd)}));}
                let error=io::Error::last_os_error();if error.kind()==io::ErrorKind::NotFound {Ok(None)} else {Err(error)}
            };
            let exists_name=|name:&CString|->io::Result<bool> {
                let mut metadata:libc::stat=unsafe {std::mem::zeroed()};
                if unsafe {libc::fstatat(directory.as_raw_fd(),name.as_ptr(),&mut metadata,libc::AT_SYMLINK_NOFOLLOW)}==0 {return Ok(true);}
                let error=io::Error::last_os_error();if error.kind()==io::ErrorKind::NotFound {Ok(false)} else {Err(error)}
            };
            if record.directory_events.contains(&restore_intent) {
                let original=match open_dir(&name) {Ok(value)=>value,Err(_)=>{
                    let _=self.guard_needs_review(record,"partial_needs_review")?;
                    return Err(io::Error::other("inverse directory restoration requires review"));
                }};
                let saved=match open_dir(&quarantine) {Ok(value)=>value,Err(_)=>{
                    let _=self.guard_needs_review(record,"partial_needs_review")?;
                    return Err(io::Error::other("inverse directory restoration requires review"));
                }};
                match (original,saved) {
                    (None,Some(file)) if managed_object_identity(&file)?==identity => {
                        if unsafe {libc::renameat2(directory.as_raw_fd(),quarantine.as_ptr(),directory.as_raw_fd(),name.as_ptr(),libc::RENAME_NOREPLACE)}!=0 {
                            let _=self.guard_needs_review(record,"partial_needs_review")?;
                            return Err(io::Error::other("inverse directory restoration requires review"));
                        }
                        directory.sync_all()?;
                    },
                    (Some(file),None) if managed_object_identity(&file)?==identity => {},
                    _ => {let _=self.guard_needs_review(record,"partial_needs_review")?;
                        return Err(io::Error::other("inverse directory restoration requires review"));},
                }
                if !record.directory_events.contains(&restore_done) {let mut next=record.clone();next.directory_events.push(restore_done);self.write(&next)?;*record=next;}
                let _=self.guard_needs_review(record,"partial_needs_review")?;
                return Err(io::Error::other("inverse directory changed during pruning"));
            }
            if !prior {
                if open_dir(&quarantine)?.is_some() {return Err(io::Error::other("inverse directory quarantine occupied"));}
                let file=open_dir(&name)?.ok_or_else(||io::Error::other("inverse directory missing"))?;
                if managed_object_identity(&file)?!=identity {return Err(io::Error::other("inverse directory changed"));}
                if std::fs::read_dir(format!("/proc/self/fd/{}",file.as_raw_fd()))?.next().is_some() {
                    let _=self.guard_needs_review(record,"partial_needs_review")?;
                    return Err(io::Error::other("inverse directory is not empty"));
                }
                let mut next=record.clone();next.directory_events.push(intent);self.write(&next)?;*record=next;
            }
            if open_dir(&quarantine)?.is_none() && !record.directory_events.contains(&ready) {
                let file=open_dir(&name)?.ok_or_else(||io::Error::other("inverse directory missing"))?;
                if managed_object_identity(&file)?!=identity {return Err(io::Error::other("inverse directory changed"));}
                if unsafe {libc::renameat2(directory.as_raw_fd(),name.as_ptr(),directory.as_raw_fd(),quarantine.as_ptr(),libc::RENAME_NOREPLACE)}!=0 {return Err(io::Error::last_os_error());}
                directory.sync_all()?;
            }
            if let Some(file)=open_dir(&quarantine)? {
                if exists_name(&name)? || managed_object_identity(&file)?!=identity {
                    let _=self.guard_needs_review(record,"partial_needs_review")?;
                    return Err(io::Error::other("inverse directory restoration requires review"));
                }
                if std::fs::read_dir(format!("/proc/self/fd/{}",file.as_raw_fd()))?.next().is_some() {
                    let mut next=record.clone();next.directory_events.push(restore_intent.clone());self.write(&next)?;*record=next;
                    if managed_object_identity(&file)?!=identity || exists_name(&name)?
                        || unsafe {libc::renameat2(directory.as_raw_fd(),quarantine.as_ptr(),directory.as_raw_fd(),name.as_ptr(),libc::RENAME_NOREPLACE)}!=0 {
                        let _=self.guard_needs_review(record,"partial_needs_review")?;
                        return Err(io::Error::other("inverse directory restoration requires review"));
                    }
                    directory.sync_all()?;
                    let restored=open_dir(&name)?.ok_or_else(||io::Error::other("inverse directory restoration missing"))?;
                    if managed_object_identity(&restored)?!=identity || open_dir(&quarantine)?.is_some() {
                        let _=self.guard_needs_review(record,"partial_needs_review")?;
                        return Err(io::Error::other("inverse directory restoration requires review"));
                    }
                    let mut next=record.clone();next.directory_events.push(restore_done);self.write(&next)?;*record=next;
                    let _=self.guard_needs_review(record,"partial_needs_review")?;
                    return Err(io::Error::other("inverse directory changed during pruning"));
                }
                if !record.directory_events.contains(&ready) {let mut next=record.clone();next.directory_events.push(ready.clone());self.write(&next)?;*record=next;}
                // Reopen the quarantine just before unlinking; unlinkat only
                // removes a directory if it is still empty. Concurrent OS
                // actors can still race this final name-based operation.
                let current=open_dir(&quarantine)?.ok_or_else(||io::Error::other("inverse directory quarantine missing"))?;
                if managed_object_identity(&current)?!=identity || exists_name(&name)? {
                    let _=self.guard_needs_review(record,"partial_needs_review")?;
                    return Err(io::Error::other("inverse directory restoration requires review"));
                }
                if unsafe {libc::unlinkat(directory.as_raw_fd(),quarantine.as_ptr(),libc::AT_REMOVEDIR)}!=0 {
                    let mut next=record.clone();next.directory_events.push(restore_intent.clone());self.write(&next)?;*record=next;
                    let saved=open_dir(&quarantine)?.ok_or_else(||io::Error::other("inverse directory quarantine missing"))?;
                    if managed_object_identity(&saved)?!=identity || exists_name(&name)?
                        || unsafe {libc::renameat2(directory.as_raw_fd(),quarantine.as_ptr(),directory.as_raw_fd(),name.as_ptr(),libc::RENAME_NOREPLACE)}!=0 {
                        let _=self.guard_needs_review(record,"partial_needs_review")?;
                        return Err(io::Error::other("inverse directory restoration requires review"));
                    }
                    directory.sync_all()?;
                    let restored=open_dir(&name)?.ok_or_else(||io::Error::other("inverse directory restoration missing"))?;
                    if managed_object_identity(&restored)?!=identity || open_dir(&quarantine)?.is_some() {
                        let _=self.guard_needs_review(record,"partial_needs_review")?;
                        return Err(io::Error::other("inverse directory restoration requires review"));
                    }
                    let mut next=record.clone();next.directory_events.push(restore_done);self.write(&next)?;*record=next;
                    let _=self.guard_needs_review(record,"partial_needs_review")?;
                    return Err(io::Error::other("inverse directory changed during pruning"));
                }
                directory.sync_all()?;
            } else if !record.directory_events.contains(&ready) || exists_name(&name)? {
                let _=self.guard_needs_review(record,"partial_needs_review")?;
                return Err(io::Error::other("inverse directory requires review"));
            }
            let mut next=record.clone();next.directory_events.push(done);self.write(&next)?;*record=next;
        }
        Ok(())
    }

    /// Explicit post-settlement cleanup. Unknown names or inode changes remain
    /// in place and leave cleanup pending; this never removes directories.
    #[cfg(target_os = "linux")]
    fn cleanup_owned_objects(&self, record: &mut ManagedJournalRecord) -> io::Result<bool> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        if record.phase != ManagedJournalPhase::Complete { return Err(io::Error::other("managed operation is not settled")); }
        let participants = record.plan.get("participants").and_then(serde_json::Value::as_array)
            .ok_or_else(|| io::Error::other("managed participants unavailable"))?;
        let mut objects:Vec<(String, PathBuf, serde_json::Value, String, bool)> = Vec::new();
        for stage in &record.file_stages {
            let index = stage.get("index").and_then(serde_json::Value::as_u64).ok_or_else(|| io::Error::other("stage index unavailable"))?;
            let path = stage.get("path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or_else(|| io::Error::other("stage path unavailable"))?;
            let identity = stage.get("identity").cloned().ok_or_else(|| io::Error::other("stage identity unavailable"))?;
            let digest = stage.get("digest").and_then(serde_json::Value::as_str).ok_or_else(|| io::Error::other("stage digest unavailable"))?.to_owned();
            objects.push((format!("stage-{index}"), path, identity, digest, true));
        }
        for event in &record.commit_progress {
            if event.get("kind").and_then(serde_json::Value::as_str) != Some("source_held") { continue; }
            let index = event.get("index").and_then(serde_json::Value::as_u64).and_then(|n| usize::try_from(n).ok()).ok_or_else(|| io::Error::other("hold index unavailable"))?;
            let participant = participants.get(index).ok_or_else(|| io::Error::other("hold participant unavailable"))?;
            let identity = event.get("identity").cloned().filter(|id| participant.get("source_identity") == Some(id)).ok_or_else(|| io::Error::other("hold identity unavailable"))?;
            let path = match folder_internal_replacement(record, participant)? {
                Some((_, hold)) => hold,
                None => participant.get("source_hold").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or_else(|| io::Error::other("hold path unavailable"))?,
            };
            let digest = participant.get("expected_fingerprint").and_then(serde_json::Value::as_str).ok_or_else(|| io::Error::other("hold fingerprint unavailable"))?.to_owned();
            objects.push((format!("hold-{index}"), path, identity, digest, false));
        }
        let mut names = BTreeSet::new();
        if objects.iter().any(|(_, path, _, _, _)| !names.insert(path.clone())) { return Ok(false); }
        for (key, path, identity, digest, stage_digest) in objects {
            if record.cleanup_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("done") && event.get("key").and_then(serde_json::Value::as_str) == Some(key.as_str())) { continue; }
            let parent = path.parent().ok_or_else(|| io::Error::other("cleanup parent unavailable"))?;
            let directory = open_managed_directory(record, parent)?;
            let original = CString::new(path.file_name().ok_or_else(|| io::Error::other("cleanup name unavailable"))?.as_bytes()).map_err(io::Error::other)?;
            let quarantine_name = format!(".openclank-cleanup-{}-{key}", self.operation_id);
            let quarantine = CString::new(quarantine_name.as_bytes()).map_err(io::Error::other)?;
            let open = |name:&CString| -> io::Result<Option<File>> {
                let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
                if fd >= 0 { return Ok(Some(unsafe { File::from_raw_fd(fd) })); }
                let error = io::Error::last_os_error(); if error.kind() == io::ErrorKind::NotFound { Ok(None) } else { Err(error) }
            };
            let intent = serde_json::json!({"kind":"intent","key":key,"path":path,"quarantine":parent.join(&quarantine_name),"identity":identity,"digest":digest,"stage_digest":stage_digest});
            if let Some(saved) = record.cleanup_progress.iter().find(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("intent") && event.get("key") == intent.get("key")) {
                if saved != &intent { return Ok(false); }
            } else {
                if open(&quarantine)?.is_some() { return Ok(false); }
                let mut next = record.clone(); next.cleanup_progress.push(intent); self.write(&next)?; *record = next;
            }
            let verify = |file:&File| -> io::Result<bool> {
                if !file.metadata()?.is_file() || managed_object_identity(file)? != identity { return Ok(false); }
                let mut bytes = Vec::new(); file.try_clone()?.take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut bytes)?;
                if bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES { return Ok(false); }
                Ok(if stage_digest { format!("{:x}", Sha256::digest(&bytes)) == digest } else { fingerprint_bytes(&bytes).value == digest })
            };
            if open(&quarantine)?.is_none() {
                let Some(file) = open(&original)? else { return Ok(false); };
                if !verify(&file)? { return Ok(false); }
                // SAFETY: both names belong to the same pinned directory.
                if unsafe { libc::renameat2(directory.as_raw_fd(), original.as_ptr(), directory.as_raw_fd(), quarantine.as_ptr(), libc::RENAME_NOREPLACE) } != 0 { return Ok(false); }
                directory.sync_all()?;
            }
            let Some(file) = open(&quarantine)? else { return Ok(false); };
            if open(&original)?.is_some() || !verify(&file)? { return Ok(false); }
            // SAFETY: the quarantined name is in the pinned directory and was
            // verified against this journal's exact object identity.
            if unsafe { libc::unlinkat(directory.as_raw_fd(), quarantine.as_ptr(), 0) } != 0 { return Ok(false); }
            directory.sync_all()?;
            let mut next = record.clone(); next.cleanup_progress.push(serde_json::json!({"kind":"done","key":key})); self.write(&next)?; *record = next;
        }
        Ok(true)
    }
    /// Publish only the missing `.media` containers of an admitted asset after
    /// History is prepared. Every operation-created inode is journaled before
    /// its visible name is installed; unknown objects are never adopted.
    #[cfg(target_os = "linux")]
    fn ensure_media_parents(&self, record: &mut ManagedJournalRecord, destination: &Path) -> io::Result<()> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        use std::path::Component;
        if record.phase != ManagedJournalPhase::Committing || record.history_prepare.is_none() {
            return Err(io::Error::other("managed directory publication is not admitted"));
        }
        let (root, expected_identity) = managed_record_root(record, destination)
            .map(|(root, identity)| (root.to_path_buf(), identity.cloned()))
            .ok_or_else(|| io::Error::other("managed root is unavailable"))?;
        if managed_media_ancestor_inner(&root, destination, folder_journal_asset_proven(record, &root, destination)).is_err() {
            return self.directory_needs_review(record, "managed media path changed");
        }
        let parent = destination.parent().ok_or_else(|| io::Error::other("managed destination has no parent"))?;
        let relative = parent.strip_prefix(&root).map_err(|_| io::Error::other("managed parent is outside its root"))?;
        let mut options = OpenOptions::new();
        options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let mut pinned = options.open(&root)?;
        if expected_identity.as_ref() != Some(&managed_object_identity(&pinned)?) {
            return self.directory_needs_review(record, "managed root identity changed");
        }
        let mut path = root;
        let mut in_media = false;
        for component in relative.components() {
            let Component::Normal(name) = component else { return Err(io::Error::other("managed media parent is invalid")); };
            if name == ".media" || name == "media" { in_media = true; }
            let name_c = CString::new(name.as_bytes()).map_err(io::Error::other)?;
            path.push(name);
            let path_text = path.to_str().ok_or_else(|| io::Error::other("managed media path is invalid"))?.to_owned();
            let parent_identity = managed_object_identity(&pinned)?;
            let find = |kind: &str| record.directory_events.iter().find(|event|
                event.get("kind").and_then(serde_json::Value::as_str) == Some(kind)
                    && event.get("path").and_then(serde_json::Value::as_str) == Some(path_text.as_str())).cloned();
            let intent = find("intent");
            let existing = find("existing");
            let staged = find("staged");
            let published = find("published");
            let open_dir = |name: &CString| -> io::Result<Option<File>> {
                // SAFETY: the pinned parent and NUL-terminated name remain live.
                let fd = unsafe { libc::openat(pinned.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
                if fd < 0 {
                    let error = io::Error::last_os_error();
                    if error.kind() == io::ErrorKind::NotFound { return Ok(None); }
                    return Err(error);
                }
                // SAFETY: openat returned a fresh owned descriptor.
                Ok(Some(unsafe { File::from_raw_fd(fd) }))
            };
            if intent.is_none() {
                let current = match open_dir(&name_c) {
                    Ok(value) => value,
                    Err(_) => return self.directory_needs_review(record, "managed media parent is unavailable"),
                };
                if let Some(child) = current {
                    let identity = managed_object_identity(&child)?;
                    if let Some(saved) = existing {
                        if saved.get("identity") != Some(&identity) || saved.get("parent_identity") != Some(&parent_identity) {
                            return self.directory_needs_review(record, "managed existing media parent changed");
                        }
                    } else {
                        self.append_directory_event(record, serde_json::json!({"kind":"existing", "path":path_text,
                            "identity":identity, "parent_identity":parent_identity}))?;
                    }
                    pinned = child;
                    continue;
                }
                if existing.is_some() || !in_media { return self.directory_needs_review(record, "managed media parent disappeared"); }
                let digest = format!("{:x}", Sha256::digest(format!("{}\0{}", self.operation_id, path_text).as_bytes()));
                self.append_directory_event(record, serde_json::json!({"kind":"intent", "path":path_text,
                    "parent_identity":parent_identity, "hidden":format!(".openclank-dir-{}", &digest[..24])}))?;
            }
            let intent = record.directory_events.iter().find(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("intent")
                && event.get("path").and_then(serde_json::Value::as_str) == Some(path_text.as_str())).cloned()
                .ok_or_else(|| io::Error::other("managed directory intent is missing"))?;
            if intent.get("parent_identity") != Some(&parent_identity) {
                return self.directory_needs_review(record, "managed directory parent changed");
            }
            let hidden = CString::new(intent.get("hidden").and_then(serde_json::Value::as_str).ok_or_else(|| io::Error::other("managed directory stage is invalid"))?)
                .map_err(io::Error::other)?;
            let staged = if let Some(saved) = staged { saved } else {
                // SAFETY: the hidden name is operation-derived and the parent is pinned.
                let result = unsafe { libc::mkdirat(pinned.as_raw_fd(), hidden.as_ptr(), 0o755) };
                if result != 0 { return self.directory_needs_review(record, "managed directory stage outcome is unknown"); }
                let child = open_dir(&hidden)?.ok_or_else(|| io::Error::other("managed directory stage disappeared"))?;
                let identity = managed_object_identity(&child)?;
                pinned.sync_all()?;
                let event = serde_json::json!({"kind":"staged", "path":path_text, "identity":identity, "parent_identity":parent_identity});
                self.append_directory_event(record, event.clone())?;
                event
            };
            let identity = staged.get("identity").cloned().ok_or_else(|| io::Error::other("managed directory identity is missing"))?;
            if staged.get("parent_identity") != Some(&parent_identity) {
                return self.directory_needs_review(record, "managed directory stage parent changed");
            }
            let current = match open_dir(&name_c) {
                Ok(value) => value,
                Err(_) => return self.directory_needs_review(record, "managed directory destination is unavailable"),
            };
            let child = if let Some(child) = current {
                if managed_object_identity(&child)? != identity { return self.directory_needs_review(record, "managed directory destination changed"); }
                child
            } else {
                let stage = match open_dir(&hidden) {
                    Ok(Some(stage)) => stage,
                    _ => return self.directory_needs_review(record, "managed directory stage disappeared"),
                };
                if managed_object_identity(&stage)? != identity { return self.directory_needs_review(record, "managed directory stage changed"); }
                // SAFETY: both names are relative to the same pinned parent.
                let result = unsafe { libc::renameat2(pinned.as_raw_fd(), hidden.as_ptr(), pinned.as_raw_fd(), name_c.as_ptr(), libc::RENAME_NOREPLACE) };
                if result != 0 { return self.directory_needs_review(record, "managed directory publication requires review"); }
                let child = open_dir(&name_c)?.ok_or_else(|| io::Error::other("managed directory disappeared"))?;
                if managed_object_identity(&child)? != identity { return self.directory_needs_review(record, "managed directory publication changed"); }
                pinned.sync_all()?;
                child
            };
            if let Some(saved) = published {
                if saved.get("identity") != Some(&identity) || saved.get("parent_identity") != Some(&parent_identity) {
                    return self.directory_needs_review(record, "managed directory publication identity changed");
                }
            } else {
                self.append_directory_event(record, serde_json::json!({"kind":"published", "path":path_text,
                    "identity":identity, "parent_identity":parent_identity}))?;
            }
            pinned = child;
        }
        Ok(())
    }

    fn append_directory_event(&self, record: &mut ManagedJournalRecord, event: serde_json::Value) -> io::Result<()> {
        if record.directory_events.len() >= 4096 { return Err(io::Error::other("managed directory event bound exceeded")); }
        let mut next = record.clone(); next.directory_events.push(event);
        self.write(&next)?; *record = next;
        Ok(())
    }

    fn directory_needs_review<T>(&self, record: &mut ManagedJournalRecord, message: &str) -> io::Result<T> {
        let mut next = record.clone(); next.phase = ManagedJournalPhase::NeedsReview;
        self.write(&next)?; *record = next;
        Err(io::Error::other(message))
    }

    fn guard_needs_review(&self, record: &mut ManagedJournalRecord, outcome: &str) -> io::Result<serde_json::Value> {
        if !matches!(outcome, "rejected_needs_review" | "partial_needs_review" | "committed_needs_review") {
            return Err(io::Error::other("managed guard outcome is invalid"));
        }
        let mut next = record.clone();
        next.phase = ManagedJournalPhase::NeedsReview;
        next.review_outcome = Some(outcome.to_owned());
        self.write(&next)?; *record = next;
        Ok(serde_json::json!({"operation_id":record.operation_id, "status":outcome, "cleanup":"pending"}))
    }

    #[cfg(not(target_os = "linux"))]
    fn ensure_media_parents(&self, _record: &mut ManagedJournalRecord, _destination: &Path) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "managed media parents are unavailable on this platform"))
    }

    #[cfg(target_os = "linux")]
    fn verify_live_participant(&self, record: &ManagedJournalRecord, index: usize) -> io::Result<()> {
        self.verify_destination_stage(record, index)?;
        let participant = record.plan.get("participants").and_then(serde_json::Value::as_array)
            .and_then(|items| items.get(index)).ok_or_else(|| io::Error::other("managed participant is unavailable"))?;
        let reference = participant.get("kind").and_then(serde_json::Value::as_str) == Some("reference");
        let mapped=folder_internal_replacement(record,participant)?;
        let source_owned=mapped.as_ref().map(|value|value.0.clone()).or_else(||participant.get("source").and_then(serde_json::Value::as_str).map(PathBuf::from)).ok_or_else(||io::Error::other("managed source unavailable"))?;
        let hold_owned=mapped.as_ref().map(|value|value.1.clone()).or_else(||participant.get("source_hold").and_then(serde_json::Value::as_str).map(PathBuf::from)).ok_or_else(||io::Error::other("managed hold unavailable"))?;
        let source=source_owned.as_path(); let hold=hold_owned.as_path();
        let target = participant.get("destination").and_then(serde_json::Value::as_str).map(Path::new).or_else(|| reference.then_some(source))
            .ok_or_else(|| io::Error::other("managed target is unavailable"))?;
        let external_inverse_hold = record.plan.get("inverse_of").is_some()
            && participant.get("internal_relocation").and_then(serde_json::Value::as_bool)==Some(true)
            && record.plan.pointer("/folder/destination").and_then(serde_json::Value::as_str).map(Path::new).and_then(Path::parent)==hold.parent();
        if source.parent() != hold.parent() && !external_inverse_hold { return Err(io::Error::other("managed hold parent changed")); }
        let hold_root = managed_record_root(record, hold).map(|(root, _)| root).ok_or_else(|| io::Error::other("managed root is unavailable"))?;
        let target_root = managed_record_root(record, target).map(|(root, _)| root).ok_or_else(|| io::Error::other("managed root is unavailable"))?;
        managed_no_symlink(hold_root, hold, false).map_err(|_| io::Error::other("managed hold changed"))?;
        managed_no_symlink(target_root, target, false).map_err(|_| io::Error::other("managed target changed"))?;
        let held = open_managed_file(record, hold)?;
        if managed_object_identity(&held)? != participant["source_identity"] { return Err(io::Error::other("managed hold identity changed")); }
        let mut before = Vec::new(); held.take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut before)?;
        if before.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES || participant.get("expected_fingerprint").and_then(serde_json::Value::as_str) != Some(fingerprint_bytes(&before).value.as_str()) {
            return Err(io::Error::other("managed hold fingerprint changed"));
        }
        let stage = record.file_stages.iter().find(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64)).ok_or_else(|| io::Error::other("managed stage is unavailable"))?;
        let current = open_managed_file(record, target)?;
        if managed_object_identity(&current)? != stage["identity"] { return Err(io::Error::other("managed target identity changed")); }
        if !reference {
            use std::ffi::CString;
            use std::os::fd::AsRawFd;
            use std::os::unix::ffi::OsStrExt;
            let source_parent=source.parent().ok_or_else(|| io::Error::other("managed source parent is unavailable"))?;
            let parent = match open_managed_directory(record, source_parent) {
                Ok(parent)=>parent,
                Err(error) if external_inverse_hold && error.kind()==io::ErrorKind::NotFound
                    && record.directory_events.iter().any(|event|event.get("kind").and_then(serde_json::Value::as_str)==Some("inverse_prune_done")
                        && event.get("path").and_then(serde_json::Value::as_str).map(Path::new)==Some(source_parent)) => return Ok(()),
                Err(error)=>return Err(error),
            };
            let name = CString::new(source.file_name().ok_or_else(|| io::Error::other("managed source name is unavailable"))?.as_bytes()).map_err(io::Error::other)?;
            // SAFETY: the pinned parent, name and output buffer remain live.
            let mut metadata: libc::stat = unsafe { std::mem::zeroed() };
            let status = unsafe { libc::fstatat(parent.as_raw_fd(), name.as_ptr(), &mut metadata, libc::AT_SYMLINK_NOFOLLOW) };
            if status == 0 { return Err(io::Error::other("managed source path was recreated")); }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::NotFound { return Err(error); }
        }
        Ok(())
    }

    #[cfg(not(target_os = "linux"))]
    fn verify_live_participant(&self, _record: &ManagedJournalRecord, _index: usize) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "managed live verification is unavailable on this platform"))
    }

    fn freeze_registry_plan(&self, record: &mut ManagedJournalRecord, plan: serde_json::Value) -> io::Result<String> {
        let items = plan.as_array().or_else(|| plan.as_object().filter(|object| object.len() == 2
            && object.get("subtree_guard").is_some_and(serde_json::Value::is_object))
            .and_then(|object| object.get("moves").and_then(serde_json::Value::as_array)));
        if record.phase != ManagedJournalPhase::Prepared
            || !items.is_some_and(|items| !items.is_empty() && items.len() <= MANAGED_MOVE_MAX_PARTICIPANTS) {
            return Err(io::Error::other("managed registry plan is unavailable"));
        }
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&plan).map_err(io::Error::other)?));
        if let Some(saved) = &record.registry_plan {
            if saved != &plan { return Err(io::Error::other("managed registry plan changed")); }
        } else {
            if record.history_prepare.is_some() { return Err(io::Error::other("managed registry plan was not frozen before prepare")); }
            let mut next = record.clone(); next.registry_plan = Some(plan);
            self.write(&next)?; *record = next;
        }
        Ok(digest)
    }

    /// Publish one journaled directory inode. Intent is durable before the
    /// rename; an interrupted call can recognize only that exact inode.
    #[cfg(target_os = "linux")]
    fn publish_folder(&self, record: &mut ManagedJournalRecord) -> io::Result<()> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::MetadataExt;
        if record.phase != ManagedJournalPhase::Committing || record.history_prepare.is_none() {
            return Err(io::Error::other("managed folder publication is not admitted"));
        }
        let folder = record.plan.get("folder").and_then(serde_json::Value::as_object)
            .ok_or_else(|| io::Error::other("managed folder plan is unavailable"))?;
        let path = |key: &str| -> io::Result<PathBuf> {
            folder.get(key).and_then(serde_json::Value::as_str).map(PathBuf::from)
                .ok_or_else(|| io::Error::other("managed folder path is unavailable"))
        };
        let source = path("source")?; let destination = path("destination")?;
        if source == destination || destination.starts_with(&source) || source.starts_with(&destination) {
            return Err(io::Error::other("managed folder paths overlap"));
        }
        let source_parent = open_managed_directory(record, source.parent().ok_or_else(|| io::Error::other("managed folder source has no parent"))?)?;
        let destination_parent = open_managed_directory(record, destination.parent().ok_or_else(|| io::Error::other("managed folder destination has no parent"))?)?;
        let source_parent_identity = folder.get("source_parent_identity").cloned().ok_or_else(|| io::Error::other("managed folder source parent is unavailable"))?;
        let destination_parent_identity = folder.get("destination_parent_identity").cloned().ok_or_else(|| io::Error::other("managed folder destination parent is unavailable"))?;
        if source_parent_identity != managed_object_identity(&source_parent)?
            || destination_parent_identity != managed_object_identity(&destination_parent)?
            || source_parent.metadata()?.dev() != destination_parent.metadata()?.dev() {
            return Err(io::Error::other("managed folder parent changed or crosses a filesystem"));
        }
        let expected = folder.get("source_identity").cloned().ok_or_else(|| io::Error::other("managed folder identity is unavailable"))?;
        let name = |path: &Path| -> io::Result<CString> {
            CString::new(path.file_name().ok_or_else(|| io::Error::other("managed folder name is unavailable"))?.as_bytes()).map_err(io::Error::other)
        };
        let source_name = name(&source)?; let destination_name = name(&destination)?;
        let open = |parent: &File, name: &CString| -> io::Result<Option<File>> {
            // SAFETY: parent and NUL-terminated name remain live for openat.
            let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if fd >= 0 { return Ok(Some(unsafe { File::from_raw_fd(fd) })); }
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::NotFound { Ok(None) } else { Err(error) }
        };
        let intended = record.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("folder_publish_intent"));
        if !intended {
            let current = open(&source_parent, &source_name)?.ok_or_else(|| io::Error::other("managed folder source disappeared"))?;
            if !current.metadata()?.is_dir() || managed_object_identity(&current)? != expected || current.metadata()?.dev() != destination_parent.metadata()?.dev()
                || open(&destination_parent, &destination_name)?.is_some() {
                return Err(io::Error::other("managed folder source or destination changed"));
            }
            let mut next = record.clone(); next.commit_progress.push(serde_json::json!({"kind":"folder_publish_intent"}));
            self.write(&next)?; *record = next;
        }
        let source_now = open(&source_parent, &source_name)?;
        let destination_now = open(&destination_parent, &destination_name)?;
        if let (Some(current), None) = (&source_now, &destination_now) {
            if !current.metadata()?.is_dir() || managed_object_identity(current)? != expected {
                self.guard_needs_review(record, "partial_needs_review")?;
                return Err(io::Error::other("managed folder source changed; review required"));
            }
            // SAFETY: both names are relative to pinned authorized parent descriptors.
            if unsafe { libc::renameat2(source_parent.as_raw_fd(), source_name.as_ptr(), destination_parent.as_raw_fd(), destination_name.as_ptr(), libc::RENAME_NOREPLACE) } != 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::AlreadyExists { self.guard_needs_review(record, "partial_needs_review")?; }
                return Err(error);
            }
        }
        let published = open(&destination_parent, &destination_name)?;
        let source_parent_current = open_managed_directory(record, source.parent().unwrap()).and_then(|file| managed_object_identity(&file));
        let destination_parent_current = open_managed_directory(record, destination.parent().unwrap()).and_then(|file| managed_object_identity(&file));
        if source_parent_current.ok().as_ref() != Some(&source_parent_identity)
            || destination_parent_current.ok().as_ref() != Some(&destination_parent_identity)
            || open(&source_parent, &source_name)?.is_some() || !published.as_ref().is_some_and(|file| file.metadata().is_ok_and(|metadata| metadata.is_dir()))
            || published.as_ref().map(managed_object_identity).transpose()?.as_ref() != Some(&expected) {
            let mut next = record.clone(); next.phase = ManagedJournalPhase::NeedsReview;
            next.review_outcome = Some("partial_needs_review".into());
            self.write(&next)?; *record = next;
            return Err(io::Error::other("managed folder publication requires review"));
        }
        source_parent.sync_all()?; destination_parent.sync_all()?;
        if !record.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("folder_published")) {
            let mut next = record.clone(); next.commit_progress.push(serde_json::json!({"kind":"folder_published"}));
            self.write(&next)?; *record = next;
        }
        Ok(())
    }
    /// Preserve an exact source under its journaled same-parent name. Recovery
    /// recognizes only the original recorded inode, never content equivalence.
    #[cfg(target_os = "linux")]
    fn hold_source(&self, record: &mut ManagedJournalRecord, index: usize) -> io::Result<()> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        if record.phase != ManagedJournalPhase::Committing || record.history_prepare.is_none() {
            return Err(io::Error::other("managed source hold is not admitted"));
        }
        let participant = record.plan.get("participants").and_then(serde_json::Value::as_array)
            .and_then(|items| items.get(index)).ok_or_else(|| io::Error::other("managed participant is unavailable"))?;
        let mapped = folder_internal_replacement(record, participant)?;
        let source_owned = mapped.as_ref().map(|value| value.0.clone()).or_else(|| participant.get("source").and_then(serde_json::Value::as_str).map(PathBuf::from))
            .ok_or_else(|| io::Error::other("managed source is unavailable"))?;
        let hold_owned = mapped.as_ref().map(|value| value.1.clone()).or_else(|| participant.get("source_hold").and_then(serde_json::Value::as_str).map(PathBuf::from))
            .ok_or_else(|| io::Error::other("managed source hold is unavailable"))?;
        let source = source_owned.as_path(); let hold = hold_owned.as_path();
        let parent = source.parent().ok_or_else(|| io::Error::other("managed source has no parent"))?;
        let hold_parent = hold.parent().ok_or_else(|| io::Error::other("managed hold has no parent"))?;
        if parent != hold_parent && !(record.plan.get("inverse_of").is_some()
            && participant.get("internal_relocation").and_then(serde_json::Value::as_bool)==Some(true)
            && record.plan.pointer("/folder/destination").and_then(serde_json::Value::as_str).map(Path::new).and_then(Path::parent)==Some(hold_parent)) {
            return Err(io::Error::other("managed source hold changed"));
        }
        let root = managed_record_root(record, parent).map(|(root, _)| root)
            .ok_or_else(|| io::Error::other("managed root is unavailable"))?;
        let pruned_parent=parent != hold_parent && record.directory_events.iter().any(|event|
            matches!(event.get("kind").and_then(serde_json::Value::as_str),Some("inverse_prune_intent"|"inverse_prune_unlink"|"inverse_prune_done"))
            && event.get("path").and_then(serde_json::Value::as_str).map(Path::new)==Some(parent));
        if !pruned_parent {managed_no_symlink(root, parent, false).map_err(|_| io::Error::other("managed source parent changed"))?;}
        let identity = participant.get("source_identity").cloned().ok_or_else(|| io::Error::other("managed source identity is unavailable"))?;
        let fingerprint = participant.get("expected_fingerprint").and_then(serde_json::Value::as_str)
            .ok_or_else(|| io::Error::other("managed source fingerprint is unavailable"))?.to_owned();
        let parent_file = match open_managed_directory(record,parent) {
            Ok(_) if pruned_parent => return Err(io::Error::other("pruned source parent was recreated")),
            Err(error) if pruned_parent && error.kind()==io::ErrorKind::NotFound => None,
            Ok(file)=>Some(file), Err(error)=>return Err(error),
        };
        let hold_parent_file = open_managed_directory(record, hold_parent)?;
        let source_name = CString::new(source.file_name().ok_or_else(|| io::Error::other("managed source name is missing"))?.as_bytes()).map_err(io::Error::other)?;
        let hold_name = CString::new(hold.file_name().ok_or_else(|| io::Error::other("managed hold name is missing"))?.as_bytes()).map_err(io::Error::other)?;
        let open_relative = |directory: &File, name: &CString| -> io::Result<Option<File>> {
            let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if fd >= 0 { return Ok(Some(unsafe { File::from_raw_fd(fd) })); }
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::NotFound { Ok(None) } else { Err(error) }
        };
        let verify = |file: &File| -> io::Result<()> {
            if !file.metadata()?.is_file() || managed_object_identity(file)? != identity { return Err(io::Error::other("managed source identity changed")); }
            let mut bytes = Vec::new(); file.try_clone()?.take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut bytes)?;
            if bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES || fingerprint_bytes(&bytes).value != fingerprint {
                return Err(io::Error::other("managed source fingerprint changed"));
            }
            Ok(())
        };
        let already_held = record.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("source_held") && event.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64));
        if already_held {
            let held = open_relative(&hold_parent_file, &hold_name)?.ok_or_else(|| io::Error::other("managed source hold is missing"))?;
            verify(&held)?;
            if let Some(current) = match parent_file.as_ref() {Some(file)=>open_relative(file,&source_name)?,None=>None} {
                let reference = mapped.is_some() || participant.get("kind").and_then(serde_json::Value::as_str) == Some("reference");
                let stage_identity = record.file_stages.iter().find(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64))
                    .and_then(|stage| stage.get("identity"));
                if !reference || stage_identity != Some(&managed_object_identity(&current)?) {
                    return Err(io::Error::other("managed source path was recreated"));
                }
            }
            return Ok(());
        }
        let parent_file=parent_file.ok_or_else(||io::Error::other("managed source parent is unavailable"))?;
        if let Some(source_file) = open_relative(&parent_file, &source_name)? {
            verify(&source_file)?;
            // SAFETY: both names are relative to the still-open parent FD.
            let moved = unsafe { libc::renameat2(parent_file.as_raw_fd(), source_name.as_ptr(), hold_parent_file.as_raw_fd(), hold_name.as_ptr(), libc::RENAME_NOREPLACE) };
            if moved != 0 { return Err(io::Error::last_os_error()); }
        }
        let held = open_relative(&hold_parent_file, &hold_name)?.ok_or_else(|| io::Error::other("managed source hold is missing"))?;
        if let Err(error) = verify(&held) {
            // A path race may have moved an unexpected object into the hold.
            // Preserve it and fence replay until an owner reviews the journal.
            let mut next = record.clone();
            next.phase = ManagedJournalPhase::NeedsReview;
            self.write(&next)?;
            *record = next;
            return Err(error);
        }
        if open_relative(&parent_file, &source_name)?.is_some() { return Err(io::Error::other("managed source path was recreated")); }
        parent_file.sync_all()?;
        if parent != hold_parent { hold_parent_file.sync_all()?; }
        let event = serde_json::json!({"kind":"source_held", "index":index, "identity":identity});
        if !record.commit_progress.contains(&event) {
            let mut next = record.clone(); next.commit_progress.push(event);
            self.write(&next)?; *record = next;
        }
        Ok(())
    }

    #[cfg(not(target_os = "linux"))]
    fn hold_source(&self, _record: &mut ManagedJournalRecord, _index: usize) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "managed source hold is unavailable on this platform"))
    }

    /// Linux-only no-replace publication from a pinned stage FD into a pinned
    /// parent FD. An existing destination is replay only if it is the exact
    /// recorded object; matching content alone is never accepted.
    #[cfg(target_os = "linux")]
    fn publish_destination(&self, record: &mut ManagedJournalRecord, index: usize) -> io::Result<()> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        if record.phase != ManagedJournalPhase::Committing || record.history_prepare.is_none() {
            return Err(io::Error::other("managed publication is not admitted"));
        }
        self.verify_destination_stage(record, index)?;
        let participant = record.plan.get("participants").and_then(serde_json::Value::as_array)
            .and_then(|items| items.get(index)).ok_or_else(|| io::Error::other("managed participant is unavailable"))?;
        let reference = folder_internal_replacement(record, participant)?.is_some()
            || participant.get("kind").and_then(serde_json::Value::as_str) == Some("reference");
        if reference && !record.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("source_held") && event.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64)) {
            return Err(io::Error::other("managed reference preimage is not held"));
        }
        let target = participant.get("destination").and_then(serde_json::Value::as_str).map(Path::new)
            .or_else(|| reference.then(|| participant.get("source").and_then(serde_json::Value::as_str).map(Path::new)).flatten())
            .ok_or_else(|| io::Error::other("managed destination is unavailable"))?;
        let stage = record.file_stages.iter().find(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64))
            .ok_or_else(|| io::Error::other("managed stage is unavailable"))?;
        let stage_path = stage.get("path").and_then(serde_json::Value::as_str).map(Path::new)
            .ok_or_else(|| io::Error::other("managed stage is invalid"))?;
        let expected_identity = stage.get("identity").cloned().ok_or_else(|| io::Error::other("managed stage identity is unavailable"))?;
        let stage_parent = stage_path.parent().ok_or_else(|| io::Error::other("managed stage parent is unavailable"))?;
        let stage_directory = open_managed_directory(record, stage_parent)?;
        let stage_name = CString::new(stage_path.file_name().ok_or_else(|| io::Error::other("managed stage name is missing"))?.as_bytes()).map_err(io::Error::other)?;
        // SAFETY: the stage directory and NUL-terminated name are live.
        let stage_fd = unsafe { libc::openat(stage_directory.as_raw_fd(), stage_name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if stage_fd < 0 { return Err(io::Error::last_os_error()); }
        // SAFETY: openat returned a fresh owned descriptor.
        let stage_file = unsafe { File::from_raw_fd(stage_fd) };
        if managed_object_identity(&stage_file)? != expected_identity { return Err(io::Error::other("managed stage identity changed")); }
        let mut pinned_bytes = Vec::new();
        stage_file.try_clone()?.take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut pinned_bytes)?;
        if pinned_bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES
            || stage.get("digest").and_then(serde_json::Value::as_str) != Some(format!("{:x}", Sha256::digest(&pinned_bytes)).as_str()) {
            return Err(io::Error::other("managed stage fingerprint changed"));
        }
        let parent = target.parent().ok_or_else(|| io::Error::other("managed destination has no parent"))?;
        let root = managed_record_root(record, parent).map(|(root, _)| root)
            .ok_or_else(|| io::Error::other("managed root is unavailable"))?;
        managed_no_symlink(root, parent, false).map_err(|_| io::Error::other("managed destination parent changed"))?;
        let parent_file = open_managed_directory(record, parent)?;
        let proc_path = CString::new(format!("/proc/self/fd/{}", stage_file.as_raw_fd())).map_err(io::Error::other)?;
        let name = CString::new(target.file_name().ok_or_else(|| io::Error::other("managed destination name is missing"))?.as_bytes())
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "managed destination name is invalid"))?;
        // SAFETY: both C strings are NUL-terminated; parent_file and
        // stage_file remain open throughout the call.
        let result = unsafe { libc::linkat(libc::AT_FDCWD, proc_path.as_ptr(), parent_file.as_raw_fd(), name.as_ptr(), libc::AT_SYMLINK_FOLLOW) };
        if result != 0 {
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::AlreadyExists { return Err(error); }
        }
        // Open relative to the same pinned parent. Only the recorded inode is
        // evidence of our earlier publication after an interrupted response.
        let fd = unsafe { libc::openat(parent_file.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        // SAFETY: openat returned a fresh owned descriptor.
        let destination = unsafe { File::from_raw_fd(fd) };
        if managed_object_identity(&destination)? != expected_identity { return Err(io::Error::other("managed destination collision")); }
        parent_file.sync_all()?;
        let event = serde_json::json!({"kind":"published", "index":index, "identity":expected_identity});
        if !record.commit_progress.contains(&event) {
            let mut next = record.clone(); next.commit_progress.push(event);
            self.write(&next)?; *record = next;
        }
        Ok(())
    }

    #[cfg(not(target_os = "linux"))]
    fn publish_destination(&self, _record: &mut ManagedJournalRecord, _index: usize) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "managed publication is unavailable on this platform"))
    }

    #[cfg(target_os = "linux")]
    fn verify_destination_stage(&self, record: &ManagedJournalRecord, index: usize) -> io::Result<()> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        let participant = record.plan.get("participants").and_then(serde_json::Value::as_array)
            .and_then(|items| items.get(index)).ok_or_else(|| io::Error::other("managed participant is unavailable"))?;
        let target = participant.get("destination").and_then(serde_json::Value::as_str)
            .or_else(|| (participant.get("kind").and_then(serde_json::Value::as_str) == Some("reference"))
                .then(|| participant.get("source").and_then(serde_json::Value::as_str)).flatten())
            .map(Path::new).ok_or_else(|| io::Error::other("managed destination is unavailable"))?;
        let mut matches = record.file_stages.iter().filter(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64));
        let stage = matches.next().ok_or_else(|| io::Error::other("managed destination stage is unavailable"))?;
        if matches.next().is_some() { return Err(io::Error::other("managed destination stage is duplicated")); }
        let path = stage.get("path").and_then(serde_json::Value::as_str).map(Path::new)
            .ok_or_else(|| io::Error::other("managed destination stage is invalid"))?;
        let stage_parent = path.parent().ok_or_else(|| io::Error::other("managed stage has no parent"))?;
        let root = managed_record_root(record, stage_parent).map(|(root, _)| root)
            .ok_or_else(|| io::Error::other("managed root is unavailable"))?;
        if (!target.starts_with(stage_parent) || !stage_parent.starts_with(root))
            || (stage_parent != target.parent().ok_or_else(|| io::Error::other("managed target has no parent"))?
                && managed_media_ancestor_inner(root, target, folder_journal_asset_proven(record, root, target)).is_err())
            || !path.file_name().and_then(|name| name.to_str()).is_some_and(|name| name.starts_with(".openclank-managed-") && name.ends_with(".stage")) {
            return Err(io::Error::other("managed destination stage path changed"));
        }
        managed_no_symlink(root, stage_parent, false).map_err(|_| io::Error::other("managed stage parent changed"))?;
        managed_no_symlink(root, path, false).map_err(|_| io::Error::other("managed destination stage changed"))?;
        let directory = open_managed_directory(record, stage_parent)?;
        let name = CString::new(path.file_name().ok_or_else(|| io::Error::other("managed stage name is unavailable"))?.as_bytes()).map_err(io::Error::other)?;
        // SAFETY: the pinned directory and NUL-terminated name remain live.
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        // SAFETY: openat returned a fresh owned descriptor.
        let file = unsafe { File::from_raw_fd(fd) };
        if !file.metadata()?.is_file() || managed_object_identity(&file)? != stage["identity"] {
            return Err(io::Error::other("managed destination stage identity changed"));
        }
        let mut bytes = Vec::new(); file.take(MANAGED_MOVE_MAX_BLOB_BYTES + 1).read_to_end(&mut bytes)?;
        let planned = participant.get("replacement_digest").and_then(serde_json::Value::as_str)
            .or_else(|| participant.get("before_digest").and_then(serde_json::Value::as_str));
        let digest = format!("{:x}", Sha256::digest(&bytes));
        if bytes.len() as u64 > MANAGED_MOVE_MAX_BLOB_BYTES || stage.get("digest").and_then(serde_json::Value::as_str) != Some(digest.as_str()) || planned != Some(digest.as_str()) {
            return Err(io::Error::other("managed destination stage fingerprint changed"));
        }
        Ok(())
    }

    #[cfg(not(target_os = "linux"))]
    fn verify_destination_stage(&self, _record: &ManagedJournalRecord, _index: usize) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "managed stages are unavailable on this platform"))
    }

    /// Copy one staged postimage beside its authorized destination. The
    /// journal receives the OS object identity before any publication; an
    /// unrecorded file after interruption is never adopted by name or digest.
    #[cfg(target_os = "linux")]
    fn stage_destination(&self, record: &mut ManagedJournalRecord, index: usize, bytes: &[u8]) -> io::Result<()> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        let participant = record.plan.get("participants").and_then(serde_json::Value::as_array)
            .and_then(|items| items.get(index)).ok_or_else(|| io::Error::other("managed participant is unavailable"))?;
        let ordinary = record.phase == ManagedJournalPhase::Prepared && record.history_prepare.is_none();
        let checkout_after=record.phase==ManagedJournalPhase::Committing && record.history_prepare.is_some() && record.plan.get("kind").and_then(serde_json::Value::as_str)==Some("checkout") && participant.get("directory").and_then(serde_json::Value::as_bool)==Some(false);
        let folder_after = (|| {
            if record.phase != ManagedJournalPhase::Committing || record.history_prepare.is_none()
                || record.plan.get("kind").and_then(serde_json::Value::as_str) != Some("folder")
                || !record.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("folder_published"))
                || participant.get("replacement_digest").and_then(serde_json::Value::as_str).is_none() { return None; }
            let folder = record.plan.get("folder")?;
            let source_root = Path::new(folder.get("source")?.as_str()?);
            let destination_root = Path::new(folder.get("destination")?.as_str()?);
            let source = Path::new(participant.get("source")?.as_str()?);
            let destination = Path::new(participant.get("destination")?.as_str()?);
            let suffix = source.strip_prefix(source_root).ok()?;
            if suffix.as_os_str().is_empty() || destination != destination_root.join(suffix) { return None; }
            Some(())
        })().is_some();
        if (!ordinary && !folder_after && !checkout_after) || bytes.len() > MANAGED_MOVE_MAX_BLOB_BYTES as usize {
            return Err(io::Error::other("managed destination staging is unavailable"));
        }
        let destination = participant.get("destination").and_then(serde_json::Value::as_str)
            .or_else(|| (participant.get("kind").and_then(serde_json::Value::as_str) == Some("reference"))
                .then(|| participant.get("source").and_then(serde_json::Value::as_str)).flatten())
            .ok_or_else(|| io::Error::other("managed destination is unavailable"))?;
        let expected = participant.get("replacement_digest").and_then(serde_json::Value::as_str)
            .or_else(|| participant.get("before_digest").and_then(serde_json::Value::as_str))
            .ok_or_else(|| io::Error::other("managed postimage digest is unavailable"))?;
        if format!("{:x}", Sha256::digest(bytes)) != expected { return Err(io::Error::other("managed postimage changed")); }
        if record.file_stages.iter().any(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64)) {
            return Err(io::Error::other("managed destination already has a recorded stage"));
        }
        let destination = Path::new(destination);
        let parent = destination.parent().ok_or_else(|| io::Error::other("managed destination has no parent"))?;
        let root = managed_record_root(record, destination).map(|(root, _)| root)
            .ok_or_else(|| io::Error::other("managed root is unavailable"))?;
        let parent = if managed_no_symlink(root, parent, false).is_ok() { parent.to_path_buf() }
            else { managed_media_ancestor_inner(root, destination, folder_journal_asset_proven(record, root, destination))
                .map_err(|_| io::Error::other("managed destination parent changed"))? };
        let mut random = [0u8; 16]; getrandom::fill(&mut random).map_err(io::Error::other)?;
        let suffix: String = random.iter().map(|value| format!("{value:02x}")).collect();
        let path = parent.join(format!(".openclank-managed-{suffix}.stage"));
        let directory = open_managed_directory(record, &parent)?;
        let name = CString::new(path.file_name().ok_or_else(|| io::Error::other("managed stage name is unavailable"))?.as_bytes()).map_err(io::Error::other)?;
        // SAFETY: the pinned directory and NUL-terminated name remain live.
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), libc::O_CREAT | libc::O_EXCL | libc::O_RDWR | libc::O_NOFOLLOW | libc::O_CLOEXEC, 0o600) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        // SAFETY: openat returned a fresh owned descriptor.
        let mut file = unsafe { File::from_raw_fd(fd) };
        file.write_all(bytes)?;
        apply_file_mode(&file, participant.get("mode").and_then(serde_json::Value::as_u64).map(|value| value as u32))?;
        file.sync_all()?;
        let identity = managed_object_identity(&file)?;
        directory.sync_all()?;
        let mut next = record.clone();
        next.file_stages.push(serde_json::json!({"index":index, "path":path, "identity":identity, "digest":expected}));
        // Do not delete a file on an uncertain journal write: the caller does
        // not yet have durable proof that it owns the staged object.
        self.write(&next)?;
        *record = next;
        Ok(())
    }

    #[cfg(not(target_os = "linux"))]
    fn stage_destination(&self, _record: &mut ManagedJournalRecord, _index: usize, _bytes: &[u8]) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "managed stages are unavailable on this platform"))
    }

    /// Replays a preparation stream against the immutable recorded prefix;
    /// new identities are durably appended before the next History RPC.
    fn preparation_checkpoint<'a>(&'a self, record: &'a mut ManagedJournalRecord)
        -> impl FnMut(serde_json::Value) -> Result<(), String> + 'a {
        self.preparation_checkpoint_from(record,0)
    }
    fn preparation_checkpoint_from<'a>(&'a self, record: &'a mut ManagedJournalRecord, start:usize)
        -> impl FnMut(serde_json::Value) -> Result<(), String> + 'a {
        let mut cursor = start;
        move |event| {
            if record.phase != ManagedJournalPhase::Prepared {
                return Err("managed history preparation is no longer active".into());
            }
            if event.get("kind").and_then(serde_json::Value::as_str) == Some("prepare_batch") {
                if cursor != record.preparation.len() || record.registry_plan.is_none() { return Err("managed history preparation is incomplete".into()); }
                if let Some(saved) = &record.history_prepare {
                    if saved != &event { return Err("managed history prepare request changed".into()); }
                } else {
                    let mut next = record.clone(); next.history_prepare = Some(event);
                    self.write(&next).map_err(|error| error.to_string())?;
                    *record = next;
                }
                return Ok(());
            }
            if record.history_prepare.is_some() && cursor >= record.preparation.len() {
                return Err("managed history preparation is already frozen".into());
            }
            if let Some(saved) = record.preparation.get(cursor) {
                if saved != &event { return Err("managed history preparation identity changed".into()); }
            } else {
                let mut next = record.clone(); next.preparation.push(event);
                self.write(&next).map_err(|error| error.to_string())?;
                *record = next;
            }
            cursor += 1;
            Ok(())
        }
    }

    /// The private coordinator must persist this exact descriptor in the
    /// original journal before opening it; no wire caller supplies a path.
    #[cfg(target_os="linux")]
    fn worktree_history_stage<'a>(&'a self, owner_id:&str, action_id:&str, request_digest:&str,deadline:std::time::Instant) -> io::Result<WorktreeHistoryStage<'a>> {
        use std::os::unix::fs::MetadataExt;
        if !self.exclusive {return Err(io::Error::other("exclusive journal required"));}
        let record=self.read()?.ok_or_else(||io::Error::other("worktree journal unavailable"))?;
        if record.phase!=ManagedJournalPhase::HistoryPending || record.owner_id!=owner_id || record.request_digest!=request_digest
            || !matches!(record.plan.get("kind").and_then(serde_json::Value::as_str),Some("source_worktree_create_v2"|"source_worktree_redo_v2"))
            || record.plan.get("history_action_id").and_then(serde_json::Value::as_str)!=Some(action_id) {return Err(io::Error::other("worktree journal mismatch"));}
        let actor_id=record.plan.get("history_actor_id").and_then(serde_json::Value::as_str).filter(|s|!s.is_empty()).ok_or_else(||io::Error::other("history actor unavailable"))?;
        let account_id=record.plan.get("history_account_id").and_then(serde_json::Value::as_str).filter(|s|!s.is_empty()).ok_or_else(||io::Error::other("history account unavailable"))?;
        let root_id=record.plan.get("root_id").and_then(serde_json::Value::as_str).filter(|s|!s.is_empty()).ok_or_else(||io::Error::other("history root unavailable"))?;
        let descriptor=record.history_completion.as_ref().and_then(|v|v.get("worktree_stage")).ok_or_else(||io::Error::other("worktree completion unavailable"))?;
        let label=descriptor.get("label").and_then(serde_json::Value::as_str).ok_or_else(||io::Error::other("worktree stage unavailable"))?;
        let length=descriptor.get("length").and_then(serde_json::Value::as_u64).filter(|n|*n<=512*1024*1024+4*1024*1024+12).ok_or_else(||io::Error::other("worktree stage bound"))?;
        let digest=descriptor.get("sha256").and_then(serde_json::Value::as_str).filter(|s|s.len()==64 && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))).ok_or_else(||io::Error::other("worktree stage digest unavailable"))?;
        let identity=descriptor.get("identity").cloned().ok_or_else(||io::Error::other("worktree stage identity unavailable"))?;
        let mut options=OpenOptions::new();options.read(true).custom_flags(libc::O_NOFOLLOW);
        let file=options.open(self.staged_path(label)?)?;let metadata=file.metadata()?;
        if !metadata.is_file() || metadata.nlink()!=1 || managed_object_identity(&file)?!=identity {return Err(io::Error::other("worktree stage identity changed"));}
        let stage=WorktreeHistoryStage{_journal:self,file,action_id:action_id.into(),actor_id:actor_id.into(),account_id:account_id.into(),root_id:root_id.into(),fingerprint:format!("sha256:{digest}:{length}"),length,identity};
        stage.verify_until(deadline)?;Ok(stage)
    }

    fn staged_path(&self, label: &str) -> io::Result<PathBuf> {
        if label.is_empty() || label.len() > 80 || !label.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid managed stage label"));
        }
        Ok(self.directory.join(format!("{}.{}.blob", self.operation_id, label)))
    }

    fn read_stage(&self, label: &str, expected_digest: &str) -> io::Result<Vec<u8>> {
        let path = self.staged_path(label)?;
        let mut options = OpenOptions::new(); options.read(true);
        #[cfg(unix)]
        options.custom_flags(libc::O_NOFOLLOW);
        let mut file = options.open(path)?;
        if !file.metadata()?.is_file() || file.metadata()?.len() > 10 * 1024 * 1024 {
            return Err(io::Error::other("managed stage is invalid"));
        }
        let mut bytes = Vec::new(); file.take(10 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > 10 * 1024 * 1024 { return Err(io::Error::other("managed stage exceeds its bound")); }
        if format!("{:x}", Sha256::digest(&bytes)) != expected_digest {
            return Err(io::Error::other("managed stage fingerprint changed"));
        }
        Ok(bytes)
    }

    fn stage(&self, label: &str, bytes: &[u8]) -> io::Result<String> {
        if bytes.len() > 10 * 1024 * 1024 { return Err(io::Error::other("managed stage exceeds its bound")); }
        let destination = self.staged_path(label)?;
        let digest = format!("{:x}", Sha256::digest(bytes));
        if destination.exists() {
            self.read_stage(label, &digest)?;
            return Ok(digest);
        }
        let mut random = [0u8; 16]; getrandom::fill(&mut random).map_err(io::Error::other)?;
        let suffix: String = random.iter().map(|value| format!("{value:02x}")).collect();
        let temporary = self.directory.join(format!(".{}.{}.stage", self.operation_id, suffix));
        let mut options = OpenOptions::new(); options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        let result = (|| {
            let mut file = options.open(&temporary)?;
            file.write_all(bytes)?; file.sync_all()?;
            match fs::hard_link(&temporary, &destination) {
                Ok(()) => {},
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => { self.read_stage(label, &digest)?; },
                Err(error) => return Err(error),
            }
            fs::remove_file(&temporary)?;
            File::open(&self.directory)?.sync_all()?;
            Ok(digest)
        })();
        if result.is_err() { let _ = fs::remove_file(&temporary); }
        result
    }

    fn open(root: &Path, operation_id: &str) -> io::Result<Self> {
        #[cfg(windows)]
        { let _ = (root, operation_id); return Err(io::Error::new(io::ErrorKind::Unsupported, "managed journal requires Windows private-directory validation")); }
        if !root.is_absolute() || operation_id.len() < 16 || operation_id.len() > 80
            || !operation_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
        {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid managed operation"));
        }
        // The root is supervisor-owned, not a path supplied in a request.
        // Reject existing symlink components rather than adopting an alias.
        for component in root.ancestors() {
            match fs::symlink_metadata(component) {
                Ok(meta) if meta.file_type().is_symlink() => return Err(io::Error::other("managed journal path is symlinked")),
                Ok(_) => {},
                Err(error) if error.kind() == io::ErrorKind::NotFound => {},
                Err(error) => return Err(error),
            }
        }
        fs::create_dir_all(root)?;
        let metadata = fs::symlink_metadata(root)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(io::Error::other("managed journal root is unavailable"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(root, fs::Permissions::from_mode(0o700))?;
        }
        let lock_path = root.join(format!("{operation_id}.lock"));
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true);
        #[cfg(unix)]
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        let lock = options.open(lock_path)?;
        if !lock.metadata()?.is_file() { return Err(io::Error::other("managed journal lock is invalid")); }
        lock.lock()?;
        Ok(Self { capture:None, directory:root.to_path_buf(), operation_id:operation_id.to_owned(), _lock:lock, exclusive:true })
    }

    fn read(&self) -> io::Result<Option<ManagedJournalRecord>> {
        let path = self.directory.join(format!("{}.json", self.operation_id));
        let mut options = OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        options.custom_flags(libc::O_NOFOLLOW);
        let mut file = match options.open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        if !file.metadata()?.is_file() || file.metadata()?.len() > 16 * 1024 * 1024 {
            return Err(io::Error::other("managed journal record is invalid"));
        }
        let mut bytes = Vec::new(); file.take(16 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > 16 * 1024 * 1024 { return Err(io::Error::other("managed journal record exceeds its bound")); }
        let record: ManagedJournalRecord = serde_json::from_slice(&bytes).map_err(|_| io::Error::other("managed journal record is invalid"))?;
        if record.version != 1 || record.operation_id != self.operation_id || record.owner_id.is_empty() || record.preparation.len() > 2048 || record.file_stages.len() > MANAGED_MOVE_MAX_PARTICIPANTS || record.directory_events.len() > 4096 || record.commit_progress.len() > MANAGED_MOVE_MAX_PARTICIPANTS * 2 || record.cleanup_progress.len() > MANAGED_MOVE_MAX_PARTICIPANTS * 4
            || record.request_digest.len() != 64 || !record.request_digest.bytes().all(|c| c.is_ascii_hexdigit())
        { return Err(io::Error::other("managed journal identity is invalid")); }
        self.settle_terminal_capture(&record);
        Ok(Some(record))
    }

    fn write(&self, record: &ManagedJournalRecord) -> io::Result<()> {
        if record.version != 1 || record.operation_id != self.operation_id || record.owner_id.is_empty() || record.preparation.len() > 2048 || record.file_stages.len() > MANAGED_MOVE_MAX_PARTICIPANTS || record.directory_events.len() > 4096 || record.commit_progress.len() > MANAGED_MOVE_MAX_PARTICIPANTS * 2 || record.cleanup_progress.len() > MANAGED_MOVE_MAX_PARTICIPANTS * 4
            || record.request_digest.len() != 64 || !record.request_digest.bytes().all(|c| c.is_ascii_hexdigit())
        { return Err(io::Error::new(io::ErrorKind::InvalidInput, "managed journal identity is invalid")); }
        if let Some(previous) = self.read()? {
            let checkout_stage_allowed=previous.plan.get("kind").and_then(serde_json::Value::as_str)==Some("checkout") && previous.phase==ManagedJournalPhase::Committing && record.phase==ManagedJournalPhase::Committing && previous.history_prepare.is_some();
            let folder_stage_allowed = previous.plan.get("kind").and_then(serde_json::Value::as_str) == Some("folder")
                && previous.phase == ManagedJournalPhase::Committing && record.phase == ManagedJournalPhase::Committing
                && previous.commit_progress.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("folder_published"));
            // Only this inverse owner may append its one-shot History read
            // stage after preparation. Bind both events to the retained receipt.
            let inverse_stage_allowed = previous.plan.get("kind").and_then(serde_json::Value::as_str)==Some("source_worktree_inverse_v2")
                && previous.phase==ManagedJournalPhase::HistoryPrepared && record.phase==ManagedJournalPhase::HistoryPrepared
                && previous.history_prepare.is_some() && record.preparation.len()==previous.preparation.len()+1
                && record.preparation.starts_with(&previous.preparation) && record.preparation.last().is_some_and(|event| {
                    let version=previous.history_prepare.as_ref().and_then(|v|v.pointer("/before/version_id"));
                    let fingerprint=previous.history_prepare.as_ref().and_then(|v|v.pointer("/before/fingerprint"));
                    if version.and_then(serde_json::Value::as_str).filter(|v|!v.is_empty()).is_none()
                        || fingerprint.and_then(serde_json::Value::as_str).filter(|v|!v.is_empty()).is_none()
                        || event.get("label").and_then(serde_json::Value::as_str)!=Some("worktree-inverse-v2")
                        || event.get("version_id")!=version || event.get("fingerprint")!=fingerprint {return false;}
                    match event.get("kind").and_then(serde_json::Value::as_str) {
                        Some("worktree_inverse_stage_issued")=>event.as_object().is_some_and(|v|v.len()==4)
                            && !previous.preparation.iter().any(|v|matches!(v["kind"].as_str(),Some("worktree_inverse_stage_issued"|"worktree_inverse_stage_complete"))),
                        Some("worktree_inverse_stage_complete")=>event.as_object().is_some_and(|v|v.len()==5) && event.get("identity").is_some()
                            && previous.preparation.last().is_some_and(|v|v["kind"]=="worktree_inverse_stage_issued" && v["version_id"]==event["version_id"] && v["fingerprint"]==event["fingerprint"]),
                        _=>false,
                    }
                });
            let allowed = previous.phase == record.phase || matches!((previous.phase, record.phase),
                (ManagedJournalPhase::Prepared, ManagedJournalPhase::HistoryPrepared | ManagedJournalPhase::NeedsReview)
                | (ManagedJournalPhase::HistoryPrepared, ManagedJournalPhase::Committing | ManagedJournalPhase::NeedsReview)
                | (ManagedJournalPhase::Committing, ManagedJournalPhase::LiveCommitted | ManagedJournalPhase::NeedsReview)
                | (ManagedJournalPhase::LiveCommitted, ManagedJournalPhase::HistoryPending | ManagedJournalPhase::Complete | ManagedJournalPhase::NeedsReview)
                | (ManagedJournalPhase::HistoryPending, ManagedJournalPhase::Complete | ManagedJournalPhase::NeedsReview));
            if previous.owner_id != record.owner_id || previous.request_digest != record.request_digest
                || previous.plan != record.plan || !allowed
                || !record.file_stages.starts_with(&previous.file_stages)
                || !record.directory_events.starts_with(&previous.directory_events)
                || previous.phase != ManagedJournalPhase::Committing && previous.directory_events != record.directory_events
                || !record.commit_progress.starts_with(&previous.commit_progress)
                || previous.phase != ManagedJournalPhase::Committing && previous.commit_progress != record.commit_progress
                || !record.cleanup_progress.starts_with(&previous.cleanup_progress)
                || previous.phase != ManagedJournalPhase::Complete && previous.cleanup_progress != record.cleanup_progress
                || previous.phase != ManagedJournalPhase::Prepared && !folder_stage_allowed && !checkout_stage_allowed && previous.file_stages != record.file_stages
                || previous.history_prepare.is_some() && !folder_stage_allowed && !checkout_stage_allowed && previous.file_stages != record.file_stages
                || !record.preparation.starts_with(&previous.preparation)
                || previous.history_prepare.is_some() && previous.history_prepare != record.history_prepare
                || previous.history_prepare.is_some() && !inverse_stage_allowed && previous.preparation != record.preparation
                || previous.registry_plan.is_some() && previous.registry_plan != record.registry_plan
                || previous.registry_plan.is_none() && record.registry_plan.is_some() && record.phase != ManagedJournalPhase::Prepared
                || !matches!(record.phase, ManagedJournalPhase::Prepared | ManagedJournalPhase::NeedsReview) && record.history_prepare.is_none()
                || previous.history_completion.is_some() && previous.history_completion != record.history_completion
                || previous.review_outcome.is_some() && previous.review_outcome != record.review_outcome
                || previous.review_outcome.is_none() && record.review_outcome.is_some() && record.phase != ManagedJournalPhase::NeedsReview
                || previous.history_completion.is_none() && record.history_completion.is_some()
                    && !matches!(record.phase, ManagedJournalPhase::LiveCommitted | ManagedJournalPhase::HistoryPending) {
                return Err(io::Error::other("managed journal operation conflicts with its recorded plan"));
            }
        } else if record.phase != ManagedJournalPhase::Prepared || record.history_completion.is_some() || record.history_prepare.is_some() || record.registry_plan.is_some() || record.review_outcome.is_some() || !record.preparation.is_empty() || !record.file_stages.is_empty() || !record.directory_events.is_empty() || !record.commit_progress.is_empty() || !record.cleanup_progress.is_empty() {
            return Err(io::Error::other("managed journal must begin in prepared phase"));
        }
        let bytes = serde_json::to_vec(record).map_err(io::Error::other)?;
        if bytes.len() > 16 * 1024 * 1024 { return Err(io::Error::other("managed journal record exceeds its bound")); }
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).map_err(io::Error::other)?;
        let suffix: String = random.iter().map(|value| format!("{value:02x}")).collect();
        let temporary = self.directory.join(format!(".{}.{}.tmp", self.operation_id, suffix));
        let mut options = OpenOptions::new(); options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        let result = (|| {
            let mut file = options.open(&temporary)?;
            file.write_all(&bytes)?; file.sync_all()?;
            fs::rename(&temporary, self.directory.join(format!("{}.json", self.operation_id)))?;
            File::open(&self.directory)?.sync_all()
        })();
        if result.is_err() { let _ = fs::remove_file(&temporary); }
        result?;
        self.settle_terminal_capture(record);
        Ok(())
    }
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
    fn operation_journal(&self, root: &Path, operation: &str) -> io::Result<ManagedJournal> {
        Ok(ManagedJournal::open(root, operation)?.with_capture(self.capture_hook.clone()))
    }

    fn existing_journal(&self, root: &Path, operation: &str, exclusive: bool) -> io::Result<Option<ManagedJournal>> {
        Ok(ManagedJournal::open_existing(root, operation, exclusive)?.map(|j| j.with_capture(self.capture_hook.clone())))
    }

    #[cfg(target_os="linux")]
    fn existing_journal_nowait(&self, root: &Path, operation: &str) -> io::Result<Option<ManagedJournal>> {
        Ok(ManagedJournal::open_existing_nowait(root, operation)?.map(|j| j.with_capture(self.capture_hook.clone())))
    }

    pub fn new(config: EngineConfig, registry: RootRegistry) -> Self {
        Self {
            config,
            registry,
            sorted_snapshot_cache: Mutex::new(SortedSnapshotCache::default()),
            capture_hook: None,
            capture_required: false,
            managed_journal_root: None,
        }
    }

    pub fn require_capture(mut self, required: bool) -> Self {
        self.capture_required = required;
        self
    }

    /// Trusted supervisor configuration only; protocol callers cannot set it.
    pub fn with_managed_journal_root(mut self, root: Option<PathBuf>) -> Self {
        self.managed_journal_root = root.filter(|path| path.is_absolute());
        self
    }

    pub fn with_capture_hook(mut self, hook: Arc<dyn MutationCaptureHook>) -> Self {
        self.capture_hook = Some(hook);
        self
    }

    /// Prepare one already-authorized, fully staged file transaction. The
    /// public service cannot call this until its coordinator revalidates the
    /// same scope and every participant. History capture is always mandatory.
    fn prepare_managed_capture(
        &self, journal: &ManagedJournal, record: &mut ManagedJournalRecord,
        participants: &[ManagedPreparedParticipant],
    ) -> Result<Box<dyn MutationCaptureTicket>, EngineError> {
        if record.phase != ManagedJournalPhase::Prepared || self.managed_journal_root.as_ref() != Some(&journal.directory) {
            return Err(EngineError::HistoryCapture("managed journal is unavailable".into()));
        }
        let hook = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        let action = record.plan.get("history_action_id").and_then(serde_json::Value::as_str)
            .ok_or_else(|| EngineError::HistoryCapture("managed History identity is unavailable".into()))?.to_owned();
        let timestamp = record.plan.get("history_timestamp_millis").and_then(serde_json::Value::as_u64)
            .ok_or_else(|| EngineError::HistoryCapture("managed History timestamp is unavailable".into()))?;
        if hook.managed_action_id(&record.operation_id).map_err(EngineError::HistoryCapture)? != action {
            return Err(EngineError::HistoryCapture("managed History identity changed".into()));
        }
        if record.history_prepare.is_some() {
            let status = hook.recoverable_action(&action, &record.operation_id).map_err(EngineError::HistoryCapture)?;
            let absent = status.get("Unavailable").and_then(|value| value.get("code")).and_then(serde_json::Value::as_str) == Some("managed_action_not_found");
            let prepared = status.get("Action").and_then(|value| value.get("state")).and_then(serde_json::Value::as_str) == Some("BeforeDurable");
            if !absent && !prepared { return Err(EngineError::HistoryCapture("managed History outcome requires review".into())); }
        }
        for (index, participant) in participants.iter().enumerate() {
            if record.file_stages.iter().any(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64)) {
                journal.verify_destination_stage(record, index)?;
            } else {
                let postimage = participant.participant.replacement.as_deref().unwrap_or(&participant.before);
                journal.stage_destination(record, index, postimage)?;
            }
        }
        if record.file_stages.len() != participants.len() { return Err(EngineError::Conflict); }
        let targets = managed_capture_targets(participants);
        let registrations = {
            let mut checkpoint = journal.preparation_checkpoint(record);
            hook.register_recoverable(&targets, &mut checkpoint).map_err(EngineError::HistoryCapture)?
        };
        let source_root = managed_source_root(record)?;
        let destination_root = managed_destination_root(record)?;
        let registry_plan = managed_registry_plan(participants, &registrations, destination_root.as_ref().map(|to| (&source_root, to)))?;
        let digest = journal.freeze_registry_plan(record, registry_plan)?;
        let ticket = {
            let legacy = record.legacy_prepare_hook(hook.as_ref());
            let hook = legacy.as_deref().unwrap_or(hook.as_ref());
            let mut checkpoint = journal.preparation_checkpoint(record);
            hook.prepare_recoverable("managed_move", &targets, &action, timestamp, &digest, &mut checkpoint)
                .map_err(EngineError::HistoryCapture)?
        };
        let mut next = record.clone(); next.phase = ManagedJournalPhase::HistoryPrepared;
        journal.write(&next)?; *record = next;
        Ok(ticket)
    }

    /// Prepare the exact folder action only after its complete journal intent
    /// exists. Directory targets carry the staged manifest, never a re-read
    /// of the post-publication path.
    #[cfg(target_os = "linux")]
    fn prepare_managed_folder_capture(
        &self, journal: &ManagedJournal, record: &mut ManagedJournalRecord,
        participants: &[ManagedPreparedParticipant], directories: &[PathBuf],
    ) -> Result<(), EngineError> {
        if record.phase != ManagedJournalPhase::Prepared || self.managed_journal_root.as_ref() != Some(&journal.directory)
            || record.plan.get("kind").and_then(serde_json::Value::as_str) != Some("folder") {
            return Err(EngineError::HistoryCapture("managed folder journal is unavailable".into()));
        }
        let saved = restore_managed_participants(journal, record)?;
        if saved.len() != participants.len() || saved.iter().zip(participants).any(|(old, current)|
            serde_json::to_value(&old.participant).ok() != serde_json::to_value(&current.participant).ok()
                || old.before != current.before || old.mode != current.mode || old.source_identity != current.source_identity) {
            return Err(EngineError::Conflict);
        }
        let folder = record.plan.get("folder").and_then(serde_json::Value::as_object).ok_or(EngineError::Conflict)?;
        let source = folder.get("source").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
        let destination = folder.get("destination").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
        let manifest_digest = folder.get("manifest_digest").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
        let history_before = folder.get("registered_before").and_then(serde_json::Value::as_array).cloned().ok_or(EngineError::Conflict)?;
        let asset_proofs = folder.get("asset_proofs").and_then(serde_json::Value::as_array).cloned().ok_or(EngineError::Conflict)?;
        let manifest = journal.read_stage("folder-before", manifest_digest)?;
        let first = participants.first().ok_or(EngineError::Conflict)?;
        if first.participant.source != source || first.participant.destination.as_ref() != Some(&destination)
            || first.before != manifest || directories.len() < 2 || directories[0] != source || directories[1] != destination {
            return Err(EngineError::Conflict);
        }
        let targets = managed_capture_targets(participants);
        if targets.len() > MANAGED_MOVE_MAX_PARTICIPANTS { return Err(EngineError::LimitExceeded); }
        let root = record.plan.get("root_path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
        let root_id = record.plan.get("root_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?.to_owned();
        let action = record.plan.get("history_action_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?.to_owned();
        let timestamp = record.plan.get("history_timestamp_millis").and_then(serde_json::Value::as_u64).ok_or(EngineError::Conflict)?;
        let hook = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        if hook.managed_action_id(&record.operation_id).map_err(EngineError::HistoryCapture)? != action { return Err(EngineError::Conflict); }
        for (index, item) in participants.iter().enumerate() {
            let relocated=record.plan.pointer(&format!("/participants/{index}/internal_relocation")).and_then(serde_json::Value::as_bool)==Some(true);
            if item.participant.source.starts_with(&source) && !relocated { continue; }
            if record.file_stages.iter().any(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64)) {
                journal.verify_destination_stage(record, index)?;
            } else {
                let postimage = item.participant.replacement.as_deref().unwrap_or(&item.before);
                journal.stage_destination(record, index, postimage)?;
            }
        }
        if record.history_prepare.is_some() {
            let status = hook.recoverable_action(&action, &record.operation_id).map_err(EngineError::HistoryCapture)?;
            let absent = status.get("Unavailable").and_then(|value| value.get("code")).and_then(serde_json::Value::as_str) == Some("managed_action_not_found");
            let prepared = status.get("Action").and_then(|value| value.get("state")).and_then(serde_json::Value::as_str) == Some("BeforeDurable");
            if !absent && !prepared { return Err(EngineError::HistoryCapture("managed folder History outcome requires review".into())); }
        }
        let operation_id = record.operation_id.clone();
        let registrations = {
            let mut checkpoint = journal.preparation_checkpoint(record);
            hook.register_recoverable_folder(&targets, &source, &destination, &operation_id, &asset_proofs, &mut checkpoint).map_err(EngineError::HistoryCapture)?
        };
        for saved in &history_before {
            let relative = saved.get("relative_path").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
            let absolute = root.join(relative);
            let matching: Vec<_> = targets.iter().zip(&registrations).filter(|(target, _)| target.resource_id == absolute && target.before.is_some()).collect();
            if matching.len() != 1 || ["resource_id", "generation", "relative_path"].iter().any(|key| matching[0].1.get(*key) != saved.get(*key)) {
                return Err(EngineError::Conflict);
            }
        }
        let source_root=(root_id.clone(),root.clone());
        let destination_root=managed_destination_root(record)?;
        let cross=destination_root.as_ref().map(|destination| (&source_root,destination));
        let moves = managed_registry_plan(participants, &registrations, cross)?;
        let source_relative = source.strip_prefix(&root).map_err(|_| EngineError::Conflict)?;
        let destination_relative = destination.strip_prefix(destination_root.as_ref().map(|value| &value.1).unwrap_or(&root)).map_err(|_| EngineError::Conflict)?;
        let mut registry_plan = serde_json::json!({"moves":moves,"subtree_guard":{"root_id":root_id,"root_path":root,
            "source_relative_path":source_relative,"destination_relative_path":destination_relative}});
        if let Some((id,path))=destination_root {
            registry_plan["subtree_guard"]["destination_root_id"]=serde_json::json!(id);
            registry_plan["subtree_guard"]["destination_root_path"]=serde_json::json!(path);
            registry_plan["subtree_guard"]["source_root_identity"]=record.plan.get("root_identity").cloned().ok_or(EngineError::Conflict)?;
            registry_plan["subtree_guard"]["destination_root_identity"]=record.plan.get("destination_root_identity").cloned().ok_or(EngineError::Conflict)?;
        }
        let digest = journal.freeze_registry_plan(record, registry_plan.clone())?;
        {
            let legacy = record.legacy_prepare_hook(hook.as_ref());
            let hook = legacy.as_deref().unwrap_or(hook.as_ref());
            let mut checkpoint = journal.preparation_checkpoint(record);
            hook.prepare_recoverable_directories("managed_move", &targets, directories, &registrations, &action, timestamp, &digest, &mut checkpoint)
                .map_err(EngineError::HistoryCapture)?;
        }
        if let Err(error) = hook.validate_recoverable_subtree(&action, &registry_plan) {
            if error == "managed_registry_subtree_changed" { journal.guard_needs_review(record, "rejected_needs_review")?; }
            return Err(EngineError::HistoryCapture(error));
        }
        let mut next = record.clone(); next.phase = ManagedJournalPhase::HistoryPrepared;
        journal.write(&next)?; *record = next;
        Ok(())
    }

    /// Persist the exact auth-free History live/completion request and any
    /// staged payloads before issuing the first completion RPC.
    fn stage_managed_completion(
        &self, journal: &ManagedJournal, record: &mut ManagedJournalRecord,
        participants: &[ManagedPreparedParticipant],
    ) -> Result<(serde_json::Value, Vec<(String, Vec<u8>, String)>), EngineError> {
        if !matches!(record.phase, ManagedJournalPhase::LiveCommitted | ManagedJournalPhase::HistoryPending) {
            return Err(EngineError::HistoryCapture("managed live outcome is unavailable".into()));
        }
        let hook = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        if record.history_completion.is_none() {
            let mut after = Vec::new();
            for (index, item) in participants.iter().enumerate() {
                journal.verify_live_participant(record, index)?;
                let postimage = item.participant.replacement.as_ref().unwrap_or(&item.before).clone();
                if let Some(destination) = &item.participant.destination {
                    after.push(CaptureAfter { resource_id:item.participant.source.clone(), after:None });
                    after.push(CaptureAfter { resource_id:destination.clone(), after:Some(postimage) });
                } else {
                    after.push(CaptureAfter { resource_id:item.participant.source.clone(), after:Some(postimage) });
                }
            }
            let action = record.plan.get("history_action_id").and_then(serde_json::Value::as_str)
                .ok_or_else(|| EngineError::HistoryCapture("managed History identity is unavailable".into()))?;
            let prepared = record.history_prepare.as_ref().ok_or_else(|| EngineError::HistoryCapture("managed History preparation is unavailable".into()))?;
            let (request, stages) = hook.plan_recoverable_completion(action, prepared, &after).map_err(EngineError::HistoryCapture)?;
            let mut descriptors = Vec::with_capacity(stages.len());
            for (index, (id, bytes, fingerprint)) in stages.iter().enumerate() {
                let label = format!("history-after-{index}"); let digest = journal.stage(&label, bytes)?;
                descriptors.push(serde_json::json!({"id":id, "label":label, "digest":digest, "fingerprint":fingerprint}));
            }
            let mut next = record.clone(); next.history_completion = Some(serde_json::json!({"request":request, "stages":descriptors}));
            journal.write(&next)?; *record = next;
        }
        let saved = record.history_completion.as_ref().ok_or_else(|| EngineError::HistoryCapture("managed History completion is unavailable".into()))?;
        let request = saved.get("request").cloned().ok_or_else(|| EngineError::HistoryCapture("managed History request is unavailable".into()))?;
        let descriptors = saved.get("stages").and_then(serde_json::Value::as_array).ok_or_else(|| EngineError::HistoryCapture("managed History stages are unavailable".into()))?;
        let mut stages = Vec::with_capacity(descriptors.len());
        for item in descriptors {
            let field = |key: &str| item.get(key).and_then(serde_json::Value::as_str).map(str::to_owned)
                .ok_or_else(|| EngineError::HistoryCapture("managed History stage identity is invalid".into()));
            let id = field("id")?; let label = field("label")?; let digest = field("digest")?; let fingerprint = field("fingerprint")?;
            stages.push((id, journal.read_stage(&label, &digest)?, fingerprint));
        }
        Ok((request, stages))
    }

    /// Resume only the exact journaled file transaction. This remains
    /// unexposed until the service supplies a trusted scope closure and the
    /// History registry batch is enabled after joint review.
    fn continue_managed_files<F, G>(
        &self, journal: &ManagedJournal, record: &mut ManagedJournalRecord,
        owner_id: &str, policy_generation: u64, mut authorize: F, mut authorize_guard: G,
    ) -> Result<serde_json::Value, EngineError>
    where F: FnMut(&Path, bool) -> Result<(String, PathBuf), EngineError>,
          G: FnMut(&Path) -> Result<(String, PathBuf), EngineError> {
        if record.owner_id != owner_id || self.managed_journal_root.as_ref() != Some(&journal.directory)
            || record.plan.get("policy_generation").and_then(serde_json::Value::as_u64) != Some(policy_generation) {
            return Err(EngineError::Conflict);
        }
        let (root_id, root_path) = managed_source_root(record)?;
        let recorded_destination_root = managed_destination_root(record)?;
        let source_root = (root_id, root_path.clone());
        let destination_root = recorded_destination_root.clone().unwrap_or_else(|| source_root.clone());
        // A reference edit is in place, so it may sit in the destination root (an undo edits it back where it was).
        let source_ok = |root: &(String, PathBuf), destination: &Option<PathBuf>| *root == source_root || (destination.is_none() && *root == destination_root);
        let mut root_options = OpenOptions::new(); root_options.read(true);
        #[cfg(unix)]
        root_options.custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let root_file = root_options.open(&root_path)?;
        if record.plan.get("root_identity") != Some(&managed_object_identity(&root_file)?) {
            return Err(EngineError::Conflict);
        }
        if let Some((_, destination_path)) = &recorded_destination_root {
            let destination_file = root_options.open(destination_path)?;
            if record.plan.get("destination_root_identity") != Some(&managed_object_identity(&destination_file)?) {
                return Err(EngineError::Conflict);
            }
        }
        let participants = restore_managed_participants(journal, record)?;
        #[cfg(target_os = "linux")]
        if let Some(path) = record.plan.get("directory_guard").and_then(|guard| guard.get("path")).and_then(serde_json::Value::as_str) {
            open_managed_directory(record, Path::new(path))?;
        }
        for item in &participants {
            if !source_ok(&authorize(&item.participant.source, true)?, &item.participant.destination) { return Err(EngineError::Conflict); }
            if let Some(destination) = &item.participant.destination {
                if authorize(destination, true)? != destination_root { return Err(EngineError::Conflict); }
            }
        }
        if record.phase == ManagedJournalPhase::NeedsReview {
            return Ok(serde_json::json!({"operation_id":record.operation_id,
                "status":record.review_outcome.as_deref().unwrap_or("needs_review"), "cleanup":"pending"}));
        }
        if matches!(record.phase, ManagedJournalPhase::Prepared | ManagedJournalPhase::HistoryPrepared) {
            let requested: Vec<ManagedParticipant> = participants.iter().map(|item| item.participant.clone()).collect();
            let current = preflight_managed_files(&requested, &mut authorize)?;
            if current.len() != participants.len() || current.iter().zip(&participants).any(|(now, saved)|
                now.before != saved.before || now.source_identity != saved.source_identity || now.mode != saved.mode) {
                return Err(EngineError::Conflict);
            }
            for item in &participants {
                if !source_ok(&authorize(&item.participant.source, false)?, &item.participant.destination) { return Err(EngineError::Conflict); }
                if let Some(destination) = &item.participant.destination {
                    if authorize(destination, true)? != destination_root { return Err(EngineError::Conflict); }
                }
            }
        }
        if record.phase == ManagedJournalPhase::Prepared {
            let _ticket = self.prepare_managed_capture(journal, record, &participants)?;
        }
        if record.phase == ManagedJournalPhase::HistoryPrepared {
            #[cfg(target_os = "linux")]
            if let Some(path) = record.plan.get("directory_guard").and_then(|guard| guard.get("path")).and_then(serde_json::Value::as_str) {
                open_managed_directory(record, Path::new(path))?;
            }
            for index in 0..participants.len() { journal.verify_destination_stage(record, index)?; }
            if !verify_managed_guards(record, &source_root, &mut authorize_guard)? {
                return Ok(journal.guard_needs_review(record, "rejected_needs_review")?);
            }
            let mut next = record.clone(); next.phase = ManagedJournalPhase::Committing;
            journal.write(&next)?; *record = next;
        }
        if record.phase == ManagedJournalPhase::Committing {
            if !verify_managed_guards(record, &source_root, &mut authorize_guard)? {
                let visible = !record.commit_progress.is_empty() || record.directory_events.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("published"));
                return Ok(journal.guard_needs_review(record, if visible { "partial_needs_review" } else { "rejected_needs_review" })?);
            }
            for item in &participants {
                if matches!(item.participant.kind, ManagedParticipantKind::Asset) {
                    let destination = item.participant.destination.as_deref().ok_or(EngineError::Conflict)?;
                    if authorize(destination, true)? != destination_root { return Err(EngineError::Conflict); }
                    journal.ensure_media_parents(record, destination)?;
                }
            }
            if !verify_managed_guards(record, &source_root, &mut authorize_guard)? {
                let visible = record.directory_events.iter().any(|event| event.get("kind").and_then(serde_json::Value::as_str) == Some("published"));
                return Ok(journal.guard_needs_review(record, if visible { "partial_needs_review" } else { "rejected_needs_review" })?);
            }
            for (index, item) in participants.iter().enumerate() {
                if !source_ok(&authorize(&item.participant.source, true)?, &item.participant.destination) { return Err(EngineError::Conflict); }
                if let Some(destination) = &item.participant.destination {
                    if authorize(destination, true)? != destination_root { return Err(EngineError::Conflict); }
                    journal.publish_destination(record, index)?;
                    journal.hold_source(record, index)?;
                } else {
                    journal.hold_source(record, index)?;
                    journal.publish_destination(record, index)?;
                }
            }
            for index in 0..participants.len() { journal.verify_live_participant(record, index)?; }
            if !verify_managed_guards(record, &source_root, &mut authorize_guard)? {
                return Ok(journal.guard_needs_review(record, "committed_needs_review")?);
            }
            let mut next = record.clone(); next.phase = ManagedJournalPhase::LiveCommitted;
            journal.write(&next)?; *record = next;
        }
        if matches!(record.phase, ManagedJournalPhase::LiveCommitted | ManagedJournalPhase::HistoryPending) {
            if !verify_managed_guards(record, &source_root, &mut authorize_guard)? {
                return Ok(journal.guard_needs_review(record, "committed_needs_review")?);
            }
            for item in &participants {
                if !source_ok(&authorize(&item.participant.source, true)?, &item.participant.destination) { return Err(EngineError::Conflict); }
                if let Some(destination) = &item.participant.destination {
                    if authorize(destination, true)? != destination_root { return Err(EngineError::Conflict); }
                }
            }
            let (completion, stages) = self.stage_managed_completion(journal, record, &participants)?;
            if record.phase == ManagedJournalPhase::LiveCommitted {
                let mut next = record.clone(); next.phase = ManagedJournalPhase::HistoryPending;
                journal.write(&next)?; *record = next;
            }
            let hook = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
            let action = record.plan.get("history_action_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
            let status = hook.recoverable_action(action, &record.operation_id).map_err(EngineError::HistoryCapture)?;
            let state = status.get("Action").and_then(|value| value.get("state")).and_then(serde_json::Value::as_str);
            if !matches!(state, Some("BeforeDurable" | "Applied" | "Complete")) {
                return Err(EngineError::HistoryCapture("managed History outcome requires review".into()));
            }
            hook.complete_recoverable(action, &completion, &stages, state == Some("Complete")).map_err(EngineError::HistoryCapture)?;
            let registry = record.registry_plan.as_ref().ok_or_else(|| EngineError::HistoryCapture("managed registry plan is unavailable".into()))?;
            if let Err(error) = hook.commit_recoverable_registry(action, registry) {
                if error == "managed_registry_subtree_changed" {
                    return Ok(journal.guard_needs_review(record, "committed_needs_review")?);
                }
                return Err(EngineError::HistoryCapture(error));
            }
            let mut next = record.clone(); next.phase = ManagedJournalPhase::Complete;
            journal.write(&next)?; *record = next;
        }
        if record.phase == ManagedJournalPhase::Complete {
            return Ok(serde_json::json!({"operation_id":record.operation_id, "status":"committed", "cleanup":"pending"}));
        }
        Err(EngineError::HistoryCapture("managed operation requires review".into()))
    }

    /// Begin or resume by exact operation ID. The service adapter must bind
    /// owner, policy generation and authorization to its authenticated lane.
    fn run_managed_files<F, G>(
        &self, owner_id: &str, operation_id: &str, policy_generation: u64,
        participants: &[ManagedParticipant], guards: &[ManagedGuard], directory_guard: Option<&ManagedDirectoryGuard>, inverse_of: Option<&str>, mut authorize: F, mut authorize_guard: G,
    ) -> Result<serde_json::Value, EngineError>
    where F: FnMut(&Path, bool) -> Result<(String, PathBuf), EngineError>,
          G: FnMut(&Path) -> Result<(String, PathBuf), EngineError> {
        let root = self.managed_journal_root.as_ref().ok_or_else(|| EngineError::HistoryCapture("managed journal is unavailable".into()))?;
        let hook = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        if participants.len().checked_add(guards.len()).ok_or(EngineError::LimitExceeded)? > MANAGED_MOVE_MAX_PARTICIPANTS {
            return Err(EngineError::LimitExceeded);
        }
        let mut request = serde_json::json!({"owner_id":owner_id, "policy_generation":policy_generation, "participants":participants, "guards":guards});
        if let Some(guard) = directory_guard { request["directory_guard"] = serde_json::to_value(guard).map_err(io::Error::other)?; }
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&request).map_err(io::Error::other)?));
        let selected = if let Some(guard) = directory_guard {
            #[cfg(target_os = "linux")]
            {
                if !guard.path.is_absolute() || lexical_normalize(&guard.path) != guard.path
                    || participants.iter().flat_map(|part| std::iter::once(&part.source).chain(part.destination.iter())).chain(guards.iter().map(|part| &part.source)).any(|path| !path.starts_with(&guard.path)) { return Err(EngineError::Conflict); }
                let (_, authorized_root) = authorize(&guard.path, false)?;
                let file = open_selected_directory(&authorized_root, &guard.path)?;
                if directory_metadata_revision(&file.metadata()?) != guard.revision { return Err(EngineError::Conflict); }
                Some(serde_json::json!({"path":guard.path,"identity":managed_object_identity(&file)?}))
            }
            #[cfg(not(target_os = "linux"))]
            { let _ = guard; return Err(EngineError::Conflict); }
        } else { None };
        let journal = self.operation_journal(root, operation_id)?;
        let mut record = if let Some(existing) = journal.read()? {
            if existing.owner_id != owner_id || existing.request_digest != digest { return Err(EngineError::Conflict); }
            if existing.phase == ManagedJournalPhase::Prepared {
                let prepared = preflight_managed_files(participants, &mut authorize)?;
                let root_id = existing.plan.get("root_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
                let root_path = existing.plan.get("root_path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
                let prepared_guards = preflight_managed_guards(guards, &(root_id.to_owned(), root_path.clone()), &mut authorize_guard)?;
                validate_managed_guard_budget(&prepared, &prepared_guards)?;
                let action = existing.plan.get("history_action_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
                let timestamp = existing.plan.get("history_timestamp_millis").and_then(serde_json::Value::as_u64).ok_or(EngineError::Conflict)?;
                let destination_root = managed_destination_root(&existing)?;
                stage_managed_preimages_for(&journal, owner_id, &digest, root_id, &root_path, policy_generation, action, timestamp, &prepared, &prepared_guards, selected.as_ref(), destination_root.as_ref(), inverse_of)?
            } else { existing }
        } else {
            let prepared = preflight_managed_files(participants, &mut authorize)?;
            let first = prepared.first().ok_or(EngineError::Conflict)?;
            let (root_id, root_path) = authorize(&first.participant.source, false)?;
            let prepared_guards = preflight_managed_guards(guards, &(root_id.clone(), root_path.clone()), &mut authorize_guard)?;
            validate_managed_guard_budget(&prepared, &prepared_guards)?;
            let action = hook.managed_action_id(operation_id).map_err(EngineError::HistoryCapture)?;
            let timestamp = SystemTime::now().duration_since(UNIX_EPOCH).map_err(io::Error::other)?.as_millis();
            let timestamp = u64::try_from(timestamp).map_err(|_| EngineError::LimitExceeded)?;
            // A destination under another registered root makes this a cross-root
            // journal; the preflight above already proved the two roots distinct,
            // non-nested and on one filesystem.
            let document_destination = prepared.iter().find(|item| matches!(item.participant.kind, ManagedParticipantKind::Document))
                .and_then(|item| item.participant.destination.as_ref()).ok_or(EngineError::Conflict)?;
            let destination_root = authorize(document_destination, true)?;
            let destination_root = (destination_root != (root_id.clone(), root_path.clone())).then_some(destination_root);
            stage_managed_preimages_for(&journal, owner_id, &digest, &root_id, &root_path, policy_generation, &action, timestamp, &prepared, &prepared_guards, selected.as_ref(), destination_root.as_ref(), inverse_of)?
        };
        self.continue_managed_files(&journal, &mut record, owner_id, policy_generation, authorize, authorize_guard)
    }

    /// Internal structural folder lifecycle. The service gate stays closed
    /// until replacement and external-asset participants are connected.
    #[cfg(target_os = "linux")]
    fn run_managed_folder<F, G>(
        &self, owner_id: &str, policy_generation: u64, request: &ManagedFolderRequest,
        mut authorize: F, mut authorize_read: G,
    ) -> Result<serde_json::Value, EngineError>
    where F: FnMut(&Path, bool) -> Result<(String, PathBuf), EngineError>,
          G: FnMut(&Path) -> Result<(String, PathBuf), EngineError> {
        use std::os::unix::fs::MetadataExt;
        let journal_root = self.managed_journal_root.as_ref().ok_or_else(|| EngineError::HistoryCapture("managed journal is unavailable".into()))?;
        let hook = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        let digest_input = serde_json::json!({"owner_id":owner_id,"policy_generation":policy_generation,"request":request});
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&digest_input).map_err(io::Error::other)?));
        let journal = self.operation_journal(journal_root, &request.operation_id)?;
        let existing = journal.read()?;
        if existing.as_ref().is_some_and(|record| record.owner_id != owner_id || record.request_digest != digest) {
            return Err(EngineError::Conflict);
        }
        let root = authorize(&request.source, existing.is_some())?;
        let destination_root = authorize(&request.destination, true)?;
        let cross = destination_root != root;
        match (&request.destination_root,cross) {
            (None,false) => {},
            (Some(witness),true) if witness.root_id == destination_root.0 && witness.root_path == destination_root.1
                && !root.1.starts_with(&destination_root.1) && !destination_root.1.starts_with(&root.1) => {},
            _ => return Err(EngineError::Conflict),
        }
        let selected = if let Some(guard) = &request.directory_guard {
            if !guard.path.is_absolute() || lexical_normalize(&guard.path) != guard.path
                || request.source == guard.path || (!cross && request.destination == guard.path)
                || !request.source.starts_with(&guard.path) || (!cross && !request.destination.starts_with(&guard.path))
                || request.references.iter().any(|item| item.path.starts_with(&root.1) && !item.path.starts_with(&guard.path))
                || request.assets.iter().any(|item| !item.path.starts_with(&guard.path) || (!cross && !item.destination.starts_with(&guard.path)))
                || authorize(&guard.path, false)? != root { return Err(EngineError::Conflict); }
            let file = open_selected_directory(&root.1, &guard.path)?;
            if existing.is_none() && directory_metadata_revision(&file.metadata()?) != guard.revision { return Err(EngineError::Conflict); }
            Some(serde_json::json!({"path":guard.path,"identity":managed_object_identity(&file)?}))
        } else { None };
        let destination_selected = if let Some(guard) = request.destination_root.as_ref().and_then(|value| value.directory_guard.as_ref()) {
            if !guard.path.is_absolute() || lexical_normalize(&guard.path) != guard.path || request.destination == guard.path
                || !request.destination.starts_with(&guard.path) || authorize(&guard.path,false)? != destination_root
                || request.references.iter().any(|item| item.path.starts_with(&destination_root.1) && !item.path.starts_with(&guard.path))
                || request.assets.iter().any(|item| !item.destination.starts_with(&guard.path)) {return Err(EngineError::Conflict);}
            let file=open_selected_directory(&destination_root.1,&guard.path)?;
            if existing.is_none() && directory_metadata_revision(&file.metadata()?) != guard.revision {return Err(EngineError::Conflict);}
            Some(serde_json::json!({"path":guard.path,"identity":managed_object_identity(&file)?}))
        } else {None};
        let mut options = OpenOptions::new(); options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let root_file = options.open(&root.1)?;
        let root_identity = managed_object_identity(&root_file)?;
        let destination_file = if cross {Some(options.open(&destination_root.1)?)} else {None};
        let destination_identity = if let Some(file)=destination_file.as_ref() {Some(managed_object_identity(file)?)} else {None};
        let cross_identity=destination_identity.as_ref().map(|identity|(destination_root.0.clone(),destination_root.1.clone(),identity.clone()));
        let source_parent = open_folder_from_root(&root_file, &root.1, request.source.parent().ok_or(EngineError::Conflict)?)?;
        let target_file=destination_file.as_ref().unwrap_or(&root_file);
        let destination_parent = open_folder_from_root(target_file, &destination_root.1, request.destination.parent().ok_or(EngineError::Conflict)?)?;
        if source_parent.metadata()?.dev() != destination_parent.metadata()?.dev() { return Err(EngineError::Conflict); }
        if existing.as_ref().is_some_and(|record| record.owner_id != owner_id || record.request_digest != digest
            || record.plan.get("root_id").and_then(serde_json::Value::as_str) != Some(root.0.as_str())
            || record.plan.get("root_identity") != Some(&root_identity)
            || record.plan.get("policy_generation").and_then(serde_json::Value::as_u64) != Some(policy_generation)
            || selected.as_ref().is_some_and(|guard| record.plan.get("directory_guard") != Some(guard))
            || destination_selected.as_ref().is_some_and(|guard| record.plan.get("destination_directory_guard") != Some(guard))
            || (cross && (record.plan.get("destination_root_id").and_then(serde_json::Value::as_str) != Some(destination_root.0.as_str())
                || record.plan.get("destination_root_path").and_then(serde_json::Value::as_str) != destination_root.1.to_str()
                || record.plan.get("destination_root_identity") != destination_identity.as_ref()))) {
            return Err(EngineError::Conflict);
        }
        let capture = |path: &Path, read: &mut G| -> Result<ManagedFolderSnapshot, EngineError> {
            let (expected,file)=if cross && path.starts_with(&destination_root.1) {(&destination_root,target_file)} else {(&root,&root_file)};
            if read(path)? != *expected { return Err(EngineError::Conflict); }
            let folder = open_folder_from_root(file, &expected.1, path)?;
            snapshot_managed_folder(path, &folder, |child| {
                if read(child)? != *expected { return Err(EngineError::Conflict); }
                Ok(())
            })
        };
        let matches_proof = |snapshot: &ManagedFolderSnapshot| -> Result<bool, EngineError> {
            Ok(managed_folder_digest(&root_identity, snapshot)? == request.expected_snapshot.digest
                && snapshot.identities.len() as u64 == request.expected_snapshot.count
                && snapshot.total_bytes == request.expected_snapshot.bytes)
        };
        let mut record = match existing {
            Some(record) if record.phase != ManagedJournalPhase::Prepared => record,
            previous => {
                let snapshot = capture(&request.source, &mut authorize_read)?;
                if !matches_proof(&snapshot)? { return Err(EngineError::Conflict); }
                let registered = if let Some(record) = &previous {
                    record.plan.pointer("/folder/registered_before").and_then(serde_json::Value::as_array).cloned().ok_or(EngineError::Conflict)?
                } else { hook.list_managed_folder_registrations(&request.source, &request.destination).map_err(EngineError::HistoryCapture)? };
                let (mut participants, directories, guards) = assemble_managed_folder_participants(&root.1, request, &snapshot, &registered)?;
                participants.extend(preflight_managed_folder_external(request, &root, &destination_root, &mut authorize)?);
                let prepared_guards = preflight_folder_guards(&guards, &root, &destination_root, &mut authorize_read)?;
                validate_managed_guard_budget(&participants, &prepared_guards)?;
                let action = hook.managed_action_id(&request.operation_id).map_err(EngineError::HistoryCapture)?;
                let timestamp = if let Some(record) = &previous {
                    record.plan.get("history_timestamp_millis").and_then(serde_json::Value::as_u64).ok_or(EngineError::Conflict)?
                } else { u64::try_from(SystemTime::now().duration_since(UNIX_EPOCH).map_err(io::Error::other)?.as_millis()).map_err(|_| EngineError::LimitExceeded)? };
                stage_managed_folder_intent(&journal, owner_id, &digest, &root.0, &root.1, policy_generation, &action, timestamp,
                    &request.source, &request.destination, &source_parent, &destination_parent, &root_identity, &snapshot,
                    &request.expected_snapshot.digest, &registered, &participants, &directories, &prepared_guards, &request.assets, selected.as_ref(), cross_identity.as_ref(), destination_selected.as_ref(), request.inverse_of.as_ref().map(|_|request))?
            }
        };
        if record.phase == ManagedJournalPhase::NeedsReview {
            return Ok(serde_json::json!({"operation_id":record.operation_id,"status":record.review_outcome.as_deref().unwrap_or("needs_review"),"cleanup":"pending"}));
        }
        let participants = restore_managed_participants(&journal, &record)?;
        if let Some(guard) = selected.as_ref() {
            let path = guard.get("path").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
            open_managed_directory(&record, Path::new(path))?;
        }
        let directories: Vec<PathBuf> = serde_json::from_value(record.plan.pointer("/folder/directories").cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        if record.phase == ManagedJournalPhase::Prepared {
            self.prepare_managed_folder_capture(&journal, &mut record, &participants, &directories)?;
        }
        if record.phase == ManagedJournalPhase::HistoryPrepared {
            if let Some(guard) = selected.as_ref() {
                let path = guard.get("path").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
                open_managed_directory(&record, Path::new(path))?;
            }
            let snapshot = capture(&request.source, &mut authorize_read)?;
            if !matches_proof(&snapshot)? || !verify_managed_guards(&record, &root, &mut authorize_read)? {
                return Ok(journal.guard_needs_review(&mut record, "rejected_needs_review")?);
            }
            let current = preflight_managed_folder_external(request, &root, &destination_root, &mut authorize)?;
            let frozen: Vec<_> = participants.iter().enumerate().filter(|(_, item)| !item.participant.source.starts_with(&request.source)).collect();
            if current.len() != frozen.len() || current.iter().zip(&frozen).any(|(now, (_, saved))|
                serde_json::to_value(&now.participant).ok() != serde_json::to_value(&saved.participant).ok()
                    || now.before != saved.before || now.mode != saved.mode || now.source_identity != saved.source_identity) {
                return Ok(journal.guard_needs_review(&mut record, "rejected_needs_review")?);
            }
            for (index, _) in &frozen { journal.verify_destination_stage(&record, *index)?; }
            for (index, _) in participants.iter().enumerate().filter(|(index,_)|record.plan.pointer(&format!("/participants/{index}/internal_relocation")).and_then(serde_json::Value::as_bool)==Some(true)) {
                journal.verify_destination_stage(&record,index)?;
            }
            let action = record.plan.get("history_action_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
            let registry = record.registry_plan.as_ref().ok_or(EngineError::Conflict)?;
            if let Err(error) = hook.validate_recoverable_subtree(action, registry) {
                if error == "managed_registry_subtree_changed" { return Ok(journal.guard_needs_review(&mut record, "rejected_needs_review")?); }
                return Err(EngineError::HistoryCapture(error));
            }
            let mut next = record.clone(); next.phase = ManagedJournalPhase::Committing;
            journal.write(&next)?; record = next;
        }
        if record.phase == ManagedJournalPhase::Committing {
            match capture(&request.source, &mut authorize_read) {
                Ok(snapshot) if !matches_proof(&snapshot)? => return Ok(journal.guard_needs_review(&mut record, "partial_needs_review")?),
                Ok(_) => {},
                Err(EngineError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {},
                Err(error) => return Err(error),
            }
            if !verify_managed_guards(&record, &root, &mut authorize_read)? {
                return Ok(journal.guard_needs_review(&mut record, "partial_needs_review")?);
            }
            if let Err(error) = journal.publish_folder(&mut record) {
                if record.phase == ManagedJournalPhase::NeedsReview {
                    return Ok(serde_json::json!({"operation_id":record.operation_id,"status":record.review_outcome,"cleanup":"pending"}));
                }
                return Err(error.into());
            }
            for (index, item) in participants.iter().enumerate().skip(1) {
                if item.participant.source.starts_with(&request.source) {
                    let relocated=record.plan.pointer(&format!("/participants/{index}/internal_relocation")).and_then(serde_json::Value::as_bool)==Some(true);
                    if relocated {
                        let destination=item.participant.destination.as_deref().ok_or(EngineError::Conflict)?;
                        if authorize(destination,true)? != destination_root {return Err(EngineError::Conflict);}
                        journal.ensure_media_parents(&mut record,destination)?;
                        journal.hold_source(&mut record,index)?;
                        journal.publish_destination(&mut record,index)?;
                        continue;
                    }
                    let Some(replacement) = item.participant.replacement.as_deref() else { continue; };
                    let destination = item.participant.destination.as_deref().ok_or(EngineError::Conflict)?;
                    if authorize(destination, false)? != destination_root { return Err(EngineError::Conflict); }
                    if !record.file_stages.iter().any(|stage| stage.get("index").and_then(serde_json::Value::as_u64) == Some(index as u64)) {
                        journal.stage_destination(&mut record, index, replacement)?;
                    }
                    journal.hold_source(&mut record, index)?;
                    journal.publish_destination(&mut record, index)?;
                } else {
                    let expected=if cross && item.participant.source.starts_with(&destination_root.1) {&destination_root} else {&root};
                    if authorize(&item.participant.source, true)? != *expected { return Err(EngineError::Conflict); }
                    if let Some(destination) = item.participant.destination.as_deref() {
                        if authorize(destination, true)? != destination_root { return Err(EngineError::Conflict); }
                        journal.ensure_media_parents(&mut record, destination)?;
                        journal.publish_destination(&mut record, index)?;
                        journal.hold_source(&mut record, index)?;
                    } else {
                        journal.hold_source(&mut record, index)?;
                        journal.publish_destination(&mut record, index)?;
                    }
                }
            }
            if record.plan.get("inverse_of").is_some() {
                if let Err(error)=journal.prune_inverse_directories(&mut record) {
                    if record.phase==ManagedJournalPhase::NeedsReview {
                        return Ok(serde_json::json!({"operation_id":record.operation_id,"status":record.review_outcome,"cleanup":"pending"}));
                    }
                    return Err(error.into());
                }
            }
            let mut next = record.clone(); next.phase = ManagedJournalPhase::LiveCommitted;
            journal.write(&next)?; record = next;
        }
        if matches!(record.phase, ManagedJournalPhase::LiveCommitted | ManagedJournalPhase::HistoryPending) {
            if authorize_read(&request.destination)? != destination_root { return Err(EngineError::Conflict); }
            let folder = match open_folder_from_root(target_file, &destination_root.1, &request.destination) {
                Ok(folder) => folder,
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(journal.guard_needs_review(&mut record, "committed_needs_review")?),
                Err(error) => return Err(error.into()),
            };
            let exclusions = folder_owned_exclusions(&record, &request.destination)?;
            let snapshot = match snapshot_managed_folder_excluding(&request.destination, &folder, |child| {
                if authorize_read(child)? != destination_root { return Err(EngineError::Conflict); }
                Ok(())
            }, &exclusions) {
                Ok(snapshot) => snapshot,
                Err(EngineError::Conflict) => return Ok(journal.guard_needs_review(&mut record, "committed_needs_review")?),
                Err(error) => return Err(error),
            };
            if !verify_folder_postimage(&journal, &record, &snapshot)? || !verify_managed_guards(&record, &root, &mut authorize_read)? {
                return Ok(journal.guard_needs_review(&mut record, "committed_needs_review")?);
            }
            for (index, item) in participants.iter().enumerate() {
                if !item.participant.source.starts_with(&request.source) || record.plan.pointer(&format!("/participants/{index}/internal_relocation")).and_then(serde_json::Value::as_bool)==Some(true) { journal.verify_live_participant(&record, index)?; }
            }
            if record.history_completion.is_none() {
                let manifest: serde_json::Value = serde_json::from_slice(&snapshot.history).map_err(io::Error::other)?;
                let entries = manifest.get("entries").and_then(serde_json::Value::as_array).ok_or(EngineError::Conflict)?;
                let mut after = Vec::with_capacity(participants.len() * 2);
                for item in &participants {
                    if let Ok(suffix) = item.participant.source.strip_prefix(&request.source) {
                        let destination = item.participant.destination.clone().ok_or(EngineError::Conflict)?;
                        let postimage = if suffix.as_os_str().is_empty() { snapshot.history.clone() }
                            else if destination != request.destination.join(suffix) { item.before.clone() }
                            else { folder_manifest_entry_bytes(entries, suffix)?.0 };
                        after.push(CaptureAfter { resource_id:item.participant.source.clone(), after:None });
                        after.push(CaptureAfter { resource_id:destination, after:Some(postimage) });
                    } else if let Some(destination) = &item.participant.destination {
                        after.push(CaptureAfter { resource_id:item.participant.source.clone(), after:None });
                        after.push(CaptureAfter { resource_id:destination.clone(), after:Some(item.before.clone()) });
                    } else {
                        after.push(CaptureAfter { resource_id:item.participant.source.clone(), after:item.participant.replacement.clone() });
                    }
                }
                let action = record.plan.get("history_action_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
                let prepared = record.history_prepare.as_ref().ok_or(EngineError::Conflict)?;
                let (completion, stages) = hook.plan_recoverable_completion(action, prepared, &after).map_err(EngineError::HistoryCapture)?;
                let mut descriptors = Vec::with_capacity(stages.len());
                for (index, (id, bytes, fingerprint)) in stages.iter().enumerate() {
                    let label = format!("history-after-{index}"); let stage_digest = journal.stage(&label, bytes)?;
                    descriptors.push(serde_json::json!({"id":id,"label":label,"digest":stage_digest,"fingerprint":fingerprint}));
                }
                let mut next = record.clone(); next.history_completion = Some(serde_json::json!({"request":completion,"stages":descriptors}));
                journal.write(&next)?; record = next;
            }
            let saved = record.history_completion.as_ref().ok_or(EngineError::Conflict)?;
            let completion = saved.get("request").cloned().ok_or(EngineError::Conflict)?;
            let descriptors = saved.get("stages").and_then(serde_json::Value::as_array).ok_or(EngineError::Conflict)?;
            let mut stages = Vec::with_capacity(descriptors.len());
            for item in descriptors {
                let field = |key: &str| item.get(key).and_then(serde_json::Value::as_str).map(str::to_owned).ok_or(EngineError::Conflict);
                let id = field("id")?; let label = field("label")?; let stage_digest = field("digest")?; let fingerprint = field("fingerprint")?;
                stages.push((id, journal.read_stage(&label, &stage_digest)?, fingerprint));
            }
            if record.phase == ManagedJournalPhase::LiveCommitted {
                let mut next = record.clone(); next.phase = ManagedJournalPhase::HistoryPending;
                journal.write(&next)?; record = next;
            }
            let action = record.plan.get("history_action_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
            let status = hook.recoverable_action(action, &record.operation_id).map_err(EngineError::HistoryCapture)?;
            let state = status.get("Action").and_then(|value| value.get("state")).and_then(serde_json::Value::as_str);
            if !matches!(state, Some("BeforeDurable" | "Applied" | "Complete")) { return Err(EngineError::HistoryCapture("managed folder History outcome requires review".into())); }
            hook.complete_recoverable(action, &completion, &stages, state == Some("Complete")).map_err(EngineError::HistoryCapture)?;
            let registry = record.registry_plan.as_ref().ok_or(EngineError::Conflict)?;
            if let Err(error) = hook.commit_recoverable_registry(action, registry) {
                if error == "managed_registry_subtree_changed" { return Ok(journal.guard_needs_review(&mut record, "committed_needs_review")?); }
                return Err(EngineError::HistoryCapture(error));
            }
            let mut next = record.clone(); next.phase = ManagedJournalPhase::Complete;
            journal.write(&next)?; record = next;
        }
        if record.phase == ManagedJournalPhase::Complete {
            return Ok(serde_json::json!({"operation_id":record.operation_id,"status":"committed","cleanup":"pending"}));
        }
        Err(EngineError::HistoryCapture("managed folder outcome requires review".into()))
    }

    #[cfg(target_os = "linux")]
    pub(crate) fn inspect_managed_folder_app(&self, scope: &AppScope, path: &Path) -> Result<serde_json::Value, EngineError> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        use std::path::Component;
        let history = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        let root = history.managed_authorize_root(path).map_err(EngineError::HistoryCapture)?;
        let read = BTreeSet::from([Capability::Read]);
        if !scope.host {
            let allowed = self.authorize_app_checked(scope, path, &read, false)?;
            if allowed.id != root.0 || Path::new(&allowed.canonical_path) != root.1 { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
        }
        let relative = path.strip_prefix(&root.1).map_err(|_| EngineError::Registry(RegistryError::OutsideRoot))?;
        if relative.as_os_str().is_empty() || lexical_normalize(path) != path { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
        let mut options = OpenOptions::new();
        options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let mut pinned = options.open(&root.1)?;
        let root_identity = managed_object_identity(&pinned)?;
        for part in relative.components() {
            let Component::Normal(name) = part else { return Err(EngineError::Conflict); };
            let name = CString::new(name.as_bytes()).map_err(io::Error::other)?;
            // SAFETY: the pinned parent descriptor and name remain live.
            let fd = unsafe { libc::openat(pinned.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if fd < 0 { return Err(io::Error::last_os_error().into()); }
            pinned = unsafe { File::from_raw_fd(fd) };
        }
        let snapshot = snapshot_managed_folder(path, &pinned, |child| {
            if history.managed_authorize_root(child).map_err(EngineError::HistoryCapture)? != root { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            if !scope.host {
                let allowed = self.authorize_app_checked(scope, child, &read, false)?;
                if allowed.id != root.0 || Path::new(&allowed.canonical_path) != root.1 { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            }
            Ok(())
        })?;
        let mut current = options.open(&root.1)?;
        if managed_object_identity(&current)? != root_identity { return Err(EngineError::Conflict); }
        for part in relative.components() {
            let Component::Normal(name) = part else { return Err(EngineError::Conflict); };
            let name = CString::new(name.as_bytes()).map_err(io::Error::other)?;
            // SAFETY: the revalidated parent descriptor and name remain live.
            let fd = unsafe { libc::openat(current.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if fd < 0 { return Err(EngineError::Conflict); }
            current = unsafe { File::from_raw_fd(fd) };
        }
        if managed_object_identity(&current)? != snapshot.root_identity { return Err(EngineError::Conflict); }
        Ok(serde_json::json!({"digest":managed_folder_digest(&root_identity, &snapshot)?,
            "count":snapshot.identities.len(), "bytes":snapshot.total_bytes}))
    }

    #[cfg(not(target_os = "linux"))]
    pub(crate) fn inspect_managed_folder_app(&self, _scope: &AppScope, _path: &Path) -> Result<serde_json::Value, EngineError> {
        Err(EngineError::Io(io::Error::new(io::ErrorKind::Unsupported, "managed folder inspection is unavailable")))
    }

    pub(crate) fn managed_file_move_ready(&self) -> bool {
        cfg!(target_os = "linux") && self.managed_journal_root.is_some() && self.capture_hook.is_some()
    }

    #[cfg(target_os = "linux")]
    pub(crate) fn cleanup_managed_file_app(&self, scope: &AppScope, owner_id: &str, operation_id: &str, root: &Path,
        participants: &[ManagedParticipant], guards: &[ManagedGuard], directory_guard: Option<&ManagedDirectoryGuard>) -> Result<serde_json::Value, EngineError> {
        let journal_root = self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;
        let hook = self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        let journal = self.existing_journal(journal_root, operation_id, true)?.ok_or(EngineError::Conflict)?;
        let mut record = journal.read()?.ok_or(EngineError::Conflict)?;
        let mut request = serde_json::json!({"owner_id":owner_id,"policy_generation":scope.generation,"participants":participants,"guards":guards});
        if let Some(guard) = directory_guard { request["directory_guard"] = serde_json::to_value(guard).map_err(io::Error::other)?; }
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&request).map_err(io::Error::other)?));
        let root_id = record.plan.get("root_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
        if record.owner_id != owner_id || record.phase != ManagedJournalPhase::Complete || record.plan.get("kind").is_some()
            || record.request_digest != digest || record.plan.get("root_path").and_then(serde_json::Value::as_str).map(Path::new) != Some(root)
            || record.plan.get("policy_generation").and_then(serde_json::Value::as_u64) != Some(scope.generation)
            || hook.managed_authorize_root(root).map_err(EngineError::HistoryCapture)? != (root_id.to_owned(), root.to_path_buf()) { return Err(EngineError::Conflict); }
        if !scope.host {
            let allowed = self.authorize_app_checked(scope, root, &BTreeSet::from([Capability::Read, Capability::Write]), false)?;
            if allowed.id != root_id || Path::new(&allowed.canonical_path) != root { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
        }
        open_managed_directory(&record, root)?;
        // A cross-root move also owns objects (stages) under its destination root.
        if let Some((destination_id, destination_path)) = managed_destination_root(&record)? {
            if hook.managed_authorize_root(&destination_path).map_err(EngineError::HistoryCapture)? != (destination_id.clone(), destination_path.clone()) { return Err(EngineError::Conflict); }
            if !scope.host {
                let allowed = self.authorize_app_checked(scope, &destination_path, &BTreeSet::from([Capability::Read, Capability::Write]), false)?;
                if allowed.id != destination_id || Path::new(&allowed.canonical_path) != destination_path { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            }
            open_managed_directory(&record, &destination_path)?;
        }
        if let Some(guard) = record.plan.get("directory_guard").filter(|guard| !guard.is_null()) {
            let path = guard.get("path").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
            if guard.get("identity").is_none() { return Err(EngineError::Conflict); }
            open_managed_directory(&record, Path::new(path))?;
        }
        let complete = journal.cleanup_owned_objects(&mut record)?;
        Ok(serde_json::json!({"operation_id":operation_id,"status":"committed","cleanup":if complete {"complete"} else {"pending"}}))
    }

    #[cfg(not(target_os = "linux"))]
    pub(crate) fn cleanup_managed_file_app(&self, _scope: &AppScope, _owner_id: &str, _operation_id: &str, _root: &Path,
        _participants: &[ManagedParticipant], _guards: &[ManagedGuard], _directory_guard: Option<&ManagedDirectoryGuard>) -> Result<serde_json::Value, EngineError> {
        Err(EngineError::Io(io::Error::new(io::ErrorKind::Unsupported, "managed file cleanup is unavailable")))
    }

    #[cfg(target_os = "linux")]
    pub(crate) fn cleanup_managed_folder_app(&self, scope: &AppScope, owner_id: &str, operation_id: &str, root: &Path) -> Result<serde_json::Value, EngineError> {
        let journal_root = self.managed_journal_root.as_ref().ok_or_else(|| EngineError::HistoryCapture("managed journal is unavailable".into()))?;
        let hook = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("managed History is unavailable".into()))?;
        let journal = self.operation_journal(journal_root, operation_id)?;
        let mut record = journal.read()?.ok_or(EngineError::Conflict)?;
        let root_id = record.plan.get("root_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
        if record.owner_id != owner_id || record.phase != ManagedJournalPhase::Complete
            || record.plan.get("kind").and_then(serde_json::Value::as_str) != Some("folder")
            || record.plan.get("root_path").and_then(serde_json::Value::as_str).map(Path::new) != Some(root)
            || record.plan.get("policy_generation").and_then(serde_json::Value::as_u64) != Some(scope.generation)
            || hook.managed_authorize_root(root).map_err(EngineError::HistoryCapture)? != (root_id.to_owned(), root.to_path_buf()) {
            return Err(EngineError::Conflict);
        }
        if !scope.host {
            let allowed = self.authorize_app_checked(scope, root, &BTreeSet::from([Capability::Read, Capability::Write]), false)?;
            if allowed.id != root_id || Path::new(&allowed.canonical_path) != root { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
        }
        if let Some((destination_id,destination_path))=managed_destination_root(&record)? {
            if hook.managed_authorize_root(&destination_path).map_err(EngineError::HistoryCapture)? != (destination_id.clone(),destination_path.clone()) {return Err(EngineError::Conflict);}
            if !scope.host {
                let allowed=self.authorize_app_checked(scope,&destination_path,&BTreeSet::from([Capability::Read,Capability::Write]),false)?;
                if allowed.id != destination_id || Path::new(&allowed.canonical_path) != destination_path {return Err(EngineError::Registry(RegistryError::OutsideRoot));}
            }
            open_managed_directory(&record,&destination_path)?;
        }
        for key in ["directory_guard","destination_directory_guard"] {
            if let Some(guard)=record.plan.get(key).filter(|guard| !guard.is_null()) {
                let path=guard.get("path").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
                if guard.get("identity").is_none(){return Err(EngineError::Conflict);}
                open_managed_directory(&record,Path::new(path))?;
            }
        }
        let complete = journal.cleanup_owned_objects(&mut record)?;
        Ok(serde_json::json!({"operation_id":operation_id,"status":"committed","cleanup":if complete {"complete"} else {"pending"}}))
    }

    #[cfg(not(target_os = "linux"))]
    pub(crate) fn cleanup_managed_folder_app(&self, _scope: &AppScope, _owner_id: &str, _operation_id: &str, _root: &Path) -> Result<serde_json::Value, EngineError> {
        Err(EngineError::Io(io::Error::new(io::ErrorKind::Unsupported, "managed folder cleanup is unavailable")))
    }

    /// Rebuild an inverse only from the completed forward journal and its
    /// exact staged preimages. No browser-supplied participant is authority.
    #[cfg(target_os = "linux")]
    fn derive_managed_folder_inverse(&self, scope:&AppScope, owner_id:&str, request:&ManagedFolderRequest) -> Result<ManagedFolderRequest,EngineError> {
        use std::os::unix::fs::OpenOptionsExt;
        let forward=request.inverse_of.as_deref().ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        if !request.references.is_empty() || !request.assets.is_empty() || request.directory_guard.is_some() || request.destination_root.is_some() || request.inverse_prune.is_some() {
            return Err(EngineError::ManagedUndoUnavailable("managed_undo_unavailable"));
        }
        let journal_root=self.managed_journal_root.as_ref().ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        let journal=self.existing_journal(journal_root,forward,false).map_err(|_|EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?
            .ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        let record=journal.read().map_err(|_|EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable"))?
            .ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        if record.owner_id!=owner_id || record.phase!=ManagedJournalPhase::Complete || record.plan.get("kind").and_then(serde_json::Value::as_str)!=Some("folder") {
            return Err(EngineError::ManagedUndoUnavailable("managed_undo_move_not_complete"));
        }
        if record.plan.get("inverse_of").is_some() {return Err(EngineError::ManagedUndoUnavailable("managed_undo_of_undo_unsupported"));}
        if record.plan.get("directory_guard").is_some() || record.plan.get("destination_directory_guard").is_some() {
            return Err(EngineError::ManagedUndoUnavailable("managed_undo_unavailable"));
        }
        if record.plan.get("policy_generation").and_then(serde_json::Value::as_u64)!=Some(scope.generation) {return Err(EngineError::Conflict);}
        let source_root=managed_source_root(&record)?;
        let destination_root=managed_destination_root(&record)?.ok_or(EngineError::ManagedUndoUnavailable("managed_undo_requires_cross_root"))?;
        let hook=self.capture_hook.as_ref().ok_or(EngineError::ManagedUndoUnavailable("managed_undo_unavailable"))?;
        let history_action=record.plan.get("history_action_id").and_then(serde_json::Value::as_str).ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable"))?;
        let history=hook.recoverable_action(history_action,forward).map_err(EngineError::HistoryCapture)?;
        if record.history_prepare.is_none() || history.get("Action").and_then(|value|value.get("state")).and_then(serde_json::Value::as_str)!=Some("Complete") {
            return Err(EngineError::ManagedUndoUnavailable("managed_undo_unavailable"));
        }
        for root in [&source_root,&destination_root] {
            if hook.managed_authorize_root(&root.1).map_err(EngineError::HistoryCapture)? != *root {return Err(EngineError::Conflict);}
            if !scope.host {
                let allowed=self.authorize_app_checked(scope,&root.1,&BTreeSet::from([Capability::Read,Capability::Write]),false)?;
                if allowed.id!=root.0 || PathBuf::from(&allowed.canonical_path)!=root.1 {return Err(EngineError::Conflict);}
            }
            open_managed_directory(&record,&root.1)?;
        }
        let require_read=|path:&Path| -> Result<(),EngineError> {
            let expected=if path.starts_with(&source_root.1) {&source_root} else if path.starts_with(&destination_root.1) {&destination_root} else {return Err(EngineError::Conflict);};
            if hook.managed_authorize_root(path).map_err(EngineError::HistoryCapture)? != *expected {return Err(EngineError::Conflict);}
            if !scope.host {let allowed=self.authorize_app_checked(scope,path,&BTreeSet::from([Capability::Read]),false)?;
                if allowed.id!=expected.0 || PathBuf::from(&allowed.canonical_path)!=expected.1 {return Err(EngineError::Conflict);}}
            Ok(())
        };
        let folder=record.plan.get("folder").ok_or(EngineError::Conflict)?;
        let old_source=folder.get("source").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
        let old_destination=folder.get("destination").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
        if request.source!=old_destination || request.destination!=old_source || request.operation_id==forward {return Err(EngineError::Conflict);}
        let mut options=OpenOptions::new();options.read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC);
        let root_file=options.open(&destination_root.1)?;
        if record.plan.get("destination_root_identity")!=Some(&managed_object_identity(&root_file)?) {return Err(EngineError::Conflict);}
        let folder_file=open_folder_from_root(&root_file,&destination_root.1,&old_destination)?;
        let exclusions=folder_owned_exclusions(&record,&old_destination)?;
        let snapshot=snapshot_managed_folder_excluding(&old_destination,&folder_file,|path| {
            if hook.managed_authorize_root(path).map_err(EngineError::HistoryCapture)?!=destination_root {return Err(EngineError::Conflict);}
            if !scope.host {let allowed=self.authorize_app_checked(scope,path,&BTreeSet::from([Capability::Read]),false)?;
                if allowed.id!=destination_root.0 || PathBuf::from(&allowed.canonical_path)!=destination_root.1 {return Err(EngineError::Conflict);}}
            Ok(())
        },&exclusions)?;
        if !verify_folder_postimage(&journal,&record,&snapshot)? {return Err(EngineError::Conflict);}
        let participants=restore_managed_participants(&journal,&record).map_err(|_|EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable"))?;
        let plan=record.plan.get("participants").and_then(serde_json::Value::as_array).ok_or(EngineError::Conflict)?;
        let mut references=Vec::new();
        for (index,item) in participants.iter().enumerate().skip(1) {
            let row=plan.get(index).ok_or(EngineError::Conflict)?;
            let internal=item.participant.source.starts_with(&old_source);
            let relocated=row.get("internal_relocation").and_then(serde_json::Value::as_bool)==Some(true);
            if !internal || relocated {
                // A completed cleanup may remove the private stage/hold; the
                // live target still has its pinned stage inode and exact bytes.
                let target=item.participant.destination.as_ref().unwrap_or(&item.participant.source);
                let stage=record.file_stages.iter().find(|value|value.get("index").and_then(serde_json::Value::as_u64)==Some(index as u64)).ok_or(EngineError::Conflict)?;
                require_read(target)?;
                let file=open_managed_file(&record,target)?;
                if stage.get("identity")!=Some(&managed_object_identity(&file)?) {return Err(EngineError::Conflict);}
                let mut bytes=Vec::new();file.take(MANAGED_MOVE_MAX_BLOB_BYTES+1).read_to_end(&mut bytes)?;
                let expected=item.participant.replacement.as_ref().unwrap_or(&item.before);
                if &bytes!=expected {return Err(EngineError::Conflict);}
            }
            if let Some(after)=&item.participant.replacement {
                let path=if internal {item.participant.destination.clone().ok_or(EngineError::Conflict)?} else {item.participant.source.clone()};
                references.push(ManagedFolderReference {path,fingerprint:fingerprint_bytes(after).value,replacement:Some(item.before.clone())});
            }
        }
        for guard in record.plan.get("guards").and_then(serde_json::Value::as_array).ok_or(EngineError::Conflict)? {
            let path=guard.get("source").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
            require_read(&path)?;verify_managed_guard(&record,guard)?;
            references.push(ManagedFolderReference {path,
                fingerprint:guard.get("expected_fingerprint").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?.to_owned(),replacement:None});
        }
        let saved:Vec<ManagedFolderAsset>=serde_json::from_value(folder.get("asset_proofs").cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        let mut assets=Vec::with_capacity(saved.len());
        for item in saved {
            let from=item.source_frame.ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable"))?;
            let to=item.destination_frame.ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable"))?;
            assets.push(ManagedFolderAsset {path:item.destination,destination:item.path,fingerprint:item.fingerprint,
                document:item.document_destination,document_destination:item.document,workspace_root:to.canonical_root.clone(),origin:to.origin.clone(),layout_version:to.layout_version,
                source_frame:Some(to),destination_frame:Some(from)});
        }
        let mut prune=Vec::new();
        for event in &record.directory_events {
            if event.get("kind").and_then(serde_json::Value::as_str)!=Some("published") {continue;}
            let path=event.get("path").and_then(serde_json::Value::as_str).map(Path::new).ok_or(EngineError::Conflict)?;
            let Ok(relative)=path.strip_prefix(&old_destination) else {continue;};
            if relative.as_os_str().is_empty() || relative.is_absolute() || lexical_normalize(relative)!=relative {return Err(EngineError::Conflict);}
            let identity=event.get("identity").cloned().ok_or(EngineError::Conflict)?;
            if snapshot.identities.iter().find(|item|item.get("path").and_then(serde_json::Value::as_str).map(Path::new)==Some(relative))
                .and_then(|item|item.get("identity")) != Some(&identity) {return Err(EngineError::Conflict);}
            prune.push(serde_json::json!({"relative":relative,"identity":identity}));
        }
        prune.sort_by(|a,b| b["relative"].as_str().unwrap_or("").matches('/').count().cmp(&a["relative"].as_str().unwrap_or("").matches('/').count())
            .then_with(||a["relative"].as_str().cmp(&b["relative"].as_str())));
        if prune.len()>MANAGED_MOVE_MAX_PARTICIPANTS {return Err(EngineError::LimitExceeded);}
        let digest=managed_folder_digest(&managed_object_identity(&root_file)?,&snapshot)?;
        Ok(ManagedFolderRequest {operation_id:request.operation_id.clone(),source:old_destination,destination:old_source,
            expected_snapshot:ManagedFolderProof {digest,count:snapshot.identities.len() as u64,bytes:snapshot.total_bytes},
            references,assets,directory_guard:None,destination_root:Some(ManagedFolderDestinationRoot {root_id:source_root.0,root_path:source_root.1,directory_guard:None}),inverse_of:Some(forward.to_owned()),inverse_prune:Some(prune)})
    }

    #[cfg(target_os = "linux")]
    fn frozen_managed_folder_inverse(&self, owner_id:&str, request:&ManagedFolderRequest) -> Result<Option<ManagedFolderRequest>,EngineError> {
        let forward=request.inverse_of.as_deref().ok_or(EngineError::Conflict)?;
        if !request.references.is_empty() || !request.assets.is_empty() || request.directory_guard.is_some() || request.destination_root.is_some() || request.inverse_prune.is_some() {return Err(EngineError::Conflict);}
        let root=self.managed_journal_root.as_ref().ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        let original=self.existing_journal(root,forward,false).map_err(|_|EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?
            .ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        let original_record=original.read().map_err(|_|EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable"))?
            .ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        if original_record.owner_id!=owner_id || original_record.phase!=ManagedJournalPhase::Complete || original_record.plan.get("inverse_of").is_some()
            || original_record.plan.get("kind").and_then(serde_json::Value::as_str)!=Some("folder") {return Err(EngineError::ManagedUndoUnavailable("managed_undo_move_not_complete"));}
        drop(original);
        let Some(journal)=self.existing_journal(root,&request.operation_id,false)? else {return Ok(None);};
        let Some(record)=journal.read()? else {return Ok(None);};
        if record.owner_id!=owner_id || record.plan.get("inverse_of").and_then(serde_json::Value::as_str)!=Some(forward) {return Err(EngineError::Conflict);}
        let digest=record.plan.get("inverse_request_digest").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
        let bytes=match journal.read_stage("inverse-request",digest) {
            Ok(value)=>value,
            Err(_) if record.phase==ManagedJournalPhase::Prepared && record.preparation.is_empty()=>return Ok(None),
            Err(_)=>return Err(EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable")),
        };
        let saved:ManagedFolderRequest=serde_json::from_slice(&bytes).map_err(io::Error::other)?;
        if saved.operation_id!=request.operation_id || saved.inverse_of!=request.inverse_of || saved.source!=request.source || saved.destination!=request.destination {return Err(EngineError::Conflict);}
        Ok(Some(saved))
    }

    #[cfg(target_os = "linux")]
    pub(crate) fn run_managed_folder_app(&self, scope: &AppScope, owner_id: &str, request: &ManagedFolderRequest) -> Result<serde_json::Value, EngineError> {
        let history = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        let inverse_request=if let Some(forward)=request.inverse_of.as_deref() {
            Some(match self.frozen_managed_folder_inverse(owner_id,request)? {
                Some(saved)=>saved,
                None=> {
                    // Verify the exact forward postimage before touching its
                    // private custody, then use the existing journal-owned
                    // cleanup and derive a fresh inverse from the clean tree.
                    let verified=self.derive_managed_folder_inverse(scope,owner_id,request)?;
                    let root=verified.destination_root.as_ref().ok_or(EngineError::Conflict)?.root_path.clone();
                    let cleanup=self.cleanup_managed_folder_app(scope,owner_id,forward,&root)?;
                    if cleanup.get("cleanup").and_then(serde_json::Value::as_str)!=Some("complete") {
                        return Err(EngineError::HistoryCapture("managed folder cleanup requires reconciliation".into()));
                    }
                    self.derive_managed_folder_inverse(scope,owner_id,request)?
                }
            })
        } else {None};
        let request=inverse_request.as_ref().unwrap_or(request);
        self.run_managed_folder(owner_id, scope.generation, request, |path, missing| {
            let history_root = match history.managed_authorize_root(path) {
                Ok(root) => root,
                Err(error) if missing => {
                    let root = if let Some(destination)=request.destination_root.as_ref().filter(|root|path.starts_with(&root.root_path)) {
                        history.managed_authorize_root(&destination.root_path).map_err(EngineError::HistoryCapture)?
                    } else {history.managed_authorize_root(&request.source).map_err(EngineError::HistoryCapture)?};
                    if !request.assets.iter().any(|asset| asset.destination == path
                        && folder_asset_destination_proven(&root.1, &request.source, &request.destination, asset)) {
                        return Err(EngineError::HistoryCapture(error));
                    }
                    root
                }
                Err(error) => return Err(EngineError::HistoryCapture(error)),
            };
            if scope.host { return Ok(history_root); }
            let permissions = BTreeSet::from([Capability::Read, Capability::Write]);
            let root = match self.authorize_app_checked(scope, path, &permissions, missing) {
                Ok(root) => root,
                Err(_) if missing && request.assets.iter().any(|asset| asset.destination == path
                    && folder_asset_destination_proven(&history_root.1, &request.source, &request.destination, asset)) => {
                    let ancestor = managed_media_ancestor_inner(&history_root.1, path, true)?;
                    self.authorize_app_checked(scope, &ancestor, &permissions, false)?
                }
                Err(error) => return Err(error),
            };
            let files_root = (root.id.clone(), PathBuf::from(&root.canonical_path));
            if files_root != history_root { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            Ok(files_root)
        }, |path| {
            let history_root = history.managed_authorize_root(path).map_err(EngineError::HistoryCapture)?;
            if scope.host { return Ok(history_root); }
            let root = self.authorize_app_checked(scope, path, &BTreeSet::from([Capability::Read]), false)?;
            let files_root = (root.id.clone(), PathBuf::from(&root.canonical_path));
            if files_root != history_root { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            Ok(files_root)
        })
    }

    #[cfg(not(target_os = "linux"))]
    pub(crate) fn run_managed_folder_app(&self, _scope: &AppScope, _owner_id: &str, _request: &ManagedFolderRequest) -> Result<serde_json::Value, EngineError> {
        Err(EngineError::Io(io::Error::new(io::ErrorKind::Unsupported, "managed folder moves are unavailable")))
    }

    pub(crate) fn observe_managed_folder_app(&self, scope: &AppScope, owner_id: &str, request: &ManagedFolderRequest) -> Result<serde_json::Value, EngineError> {
        let journal_root = self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;
        let Some(journal) = self.existing_journal(journal_root, &request.operation_id, false)? else {
            return Ok(serde_json::json!({"operation_id":request.operation_id,"status":"unknown"}));
        };
        let Some(record) = journal.read()? else {
            return Ok(serde_json::json!({"operation_id":request.operation_id,"status":"unknown"}));
        };
        #[cfg(target_os = "linux")]
        let saved_inverse=if request.inverse_of.is_some() {self.frozen_managed_folder_inverse(owner_id,request)?} else {None};
        #[cfg(target_os = "linux")]
        if request.inverse_of.is_some() && saved_inverse.is_none() {return Ok(serde_json::json!({"operation_id":request.operation_id,"status":"unknown"}));}
        #[cfg(target_os = "linux")]
        let request=saved_inverse.as_ref().unwrap_or(request);
        #[cfg(not(target_os = "linux"))]
        if request.inverse_of.is_some() {return Err(EngineError::Conflict);}
        let input = serde_json::json!({"owner_id":owner_id,"policy_generation":scope.generation,"request":request});
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&input).map_err(io::Error::other)?));
        if record.owner_id != owner_id || record.request_digest != digest || record.plan.get("kind").and_then(serde_json::Value::as_str) != Some("folder") { return Err(EngineError::Conflict); }
        let root_id = record.plan.get("root_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
        let root_path = record.plan.get("root_path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
        let mut options = OpenOptions::new(); options.read(true);
        #[cfg(unix)] options.custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let root_file = options.open(&root_path)?;
        if record.plan.get("root_identity") != Some(&managed_object_identity(&root_file)?) { return Err(EngineError::Conflict); }
        let hook = self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        if hook.managed_authorize_root(&root_path).map_err(EngineError::HistoryCapture)? != (root_id.to_owned(), root_path.clone()) { return Err(EngineError::Conflict); }
        let destination_root=managed_destination_root(&record)?;
        match (&request.destination_root,&destination_root) {
            (None,None) => {},
            (Some(witness),Some((id,path))) if &witness.root_id == id && &witness.root_path == path => {
                let file=options.open(path)?;
                if record.plan.get("destination_root_identity") != Some(&managed_object_identity(&file)?)
                    || hook.managed_authorize_root(path).map_err(EngineError::HistoryCapture)? != (id.clone(),path.clone()) {return Err(EngineError::Conflict);}
                if !scope.host {
                    let allowed=self.authorize_app_checked(scope,path,&BTreeSet::from([Capability::Read,Capability::Write]),false)?;
                    if allowed.id != *id || PathBuf::from(&allowed.canonical_path) != *path {return Err(EngineError::Conflict);}
                }
            },
            _ => return Err(EngineError::Conflict),
        }
        for key in ["directory_guard","destination_directory_guard"] {
            if let Some(guard)=record.plan.get(key).filter(|value| !value.is_null()) {
                let path=guard.get("path").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
                #[cfg(target_os = "linux")]
                open_managed_directory(&record,Path::new(path))?;
                #[cfg(not(target_os = "linux"))]
                { let _ = path; return Err(EngineError::Conflict); }
            }
        }
        let paths = std::iter::once(&request.source).chain(std::iter::once(&request.destination))
            .chain(request.references.iter().map(|item| &item.path))
            .chain(request.assets.iter().flat_map(|item| [&item.path, &item.destination]));
        for path in paths {
            if !path.is_absolute() || lexical_normalize(path) != *path || (!component_contains(&root_path, path)
                && !destination_root.as_ref().is_some_and(|value|component_contains(&value.1,path))) { return Err(EngineError::Conflict); }
        }
        if !scope.host {
            let root = self.authorize_app_checked(scope, &root_path, &BTreeSet::from([Capability::Read, Capability::Write]), false)?;
            if root.id != root_id || PathBuf::from(&root.canonical_path) != root_path { return Err(EngineError::Conflict); }
        }
        let status=if record.phase==ManagedJournalPhase::Complete {"committed"}
            else if record.phase==ManagedJournalPhase::NeedsReview {record.review_outcome.as_deref().unwrap_or("unknown")}
            else {"unknown"};
        Ok(serde_json::json!({"operation_id":request.operation_id,"status":status}))
    }

    /// The reverse of a settled cross-root managed move, derived ONLY from its own journal record and the exact before/replacement bytes that
    /// journal staged: documents and assets go back to their old paths, a reference edit gets its old bytes back. Nothing here reads
    /// the live tree; the ordinary prepare then refuses any participant that changed since (expected fingerprint = the forward after-bytes).
    #[cfg(target_os = "linux")]
    pub(crate) fn managed_undo_participants(&self, owner_id: &str, inverse_of: &str) -> Result<Vec<ManagedParticipant>, EngineError> {
        let missing = EngineError::ManagedUndoUnavailable("managed_undo_journal_missing");
        let journal_root = self.managed_journal_root.as_ref().ok_or_else(|| EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        let journal = self.existing_journal(journal_root, inverse_of, false).map_err(|_| EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?.ok_or(missing)?;
        let record = journal.read().map_err(|_| EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?
            .ok_or(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing"))?;
        if record.owner_id != owner_id { return Err(EngineError::ManagedUndoUnavailable("managed_undo_journal_missing")); }
        if record.plan.get("inverse_of").is_some() { return Err(EngineError::ManagedUndoUnavailable("managed_undo_of_undo_unsupported")); }
        if record.phase != ManagedJournalPhase::Complete || record.plan.get("kind").is_some() { return Err(EngineError::ManagedUndoUnavailable("managed_undo_move_not_complete")); }
        if managed_destination_root(&record)?.is_none() { return Err(EngineError::ManagedUndoUnavailable("managed_undo_requires_cross_root")); }
        let prepared = restore_managed_participants(&journal, &record).map_err(|_| EngineError::ManagedUndoUnavailable("managed_undo_journal_unreadable"))?;
        let mut reversed = Vec::with_capacity(prepared.len());
        for item in prepared {
            let original = item.participant;
            let after = original.replacement.as_deref().unwrap_or(&item.before);
            let expected_fingerprint = fingerprint_bytes(after).value;
            let replacement = original.replacement.as_ref().map(|_| item.before.clone());
            reversed.push(match original.destination {
                Some(destination) => ManagedParticipant { kind: original.kind, source: destination, destination: Some(original.source), expected_fingerprint, replacement },
                None => ManagedParticipant { kind: original.kind, source: original.source, destination: None, expected_fingerprint, replacement },
            });
        }
        Ok(reversed)
    }

    #[cfg(not(target_os = "linux"))]
    pub(crate) fn managed_undo_participants(&self, _owner_id: &str, _inverse_of: &str) -> Result<Vec<ManagedParticipant>, EngineError> {
        Err(EngineError::ManagedUndoUnavailable("managed_undo_unavailable_platform"))
    }

    pub(crate) fn observe_managed_files_app(
        &self, scope: &AppScope, owner_id: &str, operation_id: &str,
        participants: &[ManagedParticipant], guards: &[ManagedGuard], directory_guard: Option<&ManagedDirectoryGuard>,
    ) -> Result<serde_json::Value, EngineError> {
        let journal_root = self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;
        let Some(journal) = self.existing_journal(journal_root, operation_id, false)? else {
            return Ok(serde_json::json!({"operation_id":operation_id,"status":"unknown"}));
        };
        let Some(record) = journal.read()? else {
            return Ok(serde_json::json!({"operation_id":operation_id,"status":"unknown"}));
        };
        let mut request = serde_json::json!({"owner_id":owner_id, "policy_generation":scope.generation, "participants":participants, "guards":guards});
        if let Some(guard) = directory_guard { request["directory_guard"] = serde_json::to_value(guard).map_err(io::Error::other)?; }
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&request).map_err(io::Error::other)?));
        if record.owner_id != owner_id || record.request_digest != digest { return Err(EngineError::Conflict); }
        let root_id = record.plan.get("root_id").and_then(serde_json::Value::as_str).ok_or(EngineError::Conflict)?;
        let root_path = record.plan.get("root_path").and_then(serde_json::Value::as_str).map(PathBuf::from).ok_or(EngineError::Conflict)?;
        let mut root_options = OpenOptions::new(); root_options.read(true);
        #[cfg(unix)]
        root_options.custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let root_file = root_options.open(&root_path)?;
        if record.plan.get("root_identity") != Some(&managed_object_identity(&root_file)?) { return Err(EngineError::Conflict); }
        let source_root = (root_id.to_owned(), root_path.clone());
        let destination_root = managed_destination_root(&record)?;
        if let Some((_, destination_path)) = &destination_root {
            let destination_file = root_options.open(destination_path)?;
            if record.plan.get("destination_root_identity") != Some(&managed_object_identity(&destination_file)?) { return Err(EngineError::Conflict); }
        }
        let hook = self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        for path in participants.iter().flat_map(|p| std::iter::once(&p.source).chain(p.destination.iter())).chain(guards.iter().map(|g| &g.source)) {
            let expected = destination_root.as_ref().filter(|(_, destination_path)| path.starts_with(destination_path)).unwrap_or(&source_root);
            if hook.managed_authorize_root(path).map_err(EngineError::HistoryCapture)? != *expected { return Err(EngineError::Conflict); }
            if !scope.host {
                let root = self.authorize_app_checked(scope, &expected.1, &BTreeSet::from([Capability::Read, Capability::Write]), false)?;
                if root.id != expected.0 || PathBuf::from(&root.canonical_path) != expected.1 { return Err(EngineError::Conflict); }
            }
        }
        Ok(serde_json::json!({"operation_id":operation_id,"status":if record.phase == ManagedJournalPhase::Complete { "committed" } else { "unknown" }}))
    }

    pub(crate) fn run_managed_files_app(
        &self, scope: &AppScope, owner_id: &str, operation_id: &str, participants: &[ManagedParticipant], guards: &[ManagedGuard], directory_guard: Option<&ManagedDirectoryGuard>, inverse_of: Option<&str>,
    ) -> Result<serde_json::Value, EngineError> {
        if !cfg!(target_os = "linux") { return Err(EngineError::HistoryCapture("managed moves are unavailable on this platform".into())); }
        let permissions = BTreeSet::from([Capability::Read, Capability::Write]);
        let history = self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("mandatory managed History capture is unavailable".into()))?;
        self.run_managed_files(owner_id, operation_id, scope.generation, participants, guards, directory_guard, inverse_of, |path, missing| {
            let history_root = history.managed_authorize_root(path).map_err(EngineError::HistoryCapture)?;
            if scope.host { return Ok(history_root); }
            let root = match self.authorize_app_checked(scope, path, &permissions, missing) {
                Ok(root) => root,
                Err(_) if missing => {
                    let ancestor = managed_media_ancestor(&history_root.1, path)?;
                    self.authorize_app_checked(scope, &ancestor, &permissions, false)?
                }
                Err(error) => return Err(error),
            };
            let files_root = (root.id.clone(), PathBuf::from(&root.canonical_path));
            if files_root != history_root { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            Ok(files_root)
        }, |path| {
            let history_root = history.managed_authorize_root(path).map_err(EngineError::HistoryCapture)?;
            if scope.host { return Ok(history_root); }
            let root = self.authorize_app_checked(scope, path, &BTreeSet::from([Capability::Read]), false)?;
            let files_root = (root.id.clone(), PathBuf::from(&root.canonical_path));
            if files_root != history_root { return Err(EngineError::Registry(RegistryError::OutsideRoot)); }
            Ok(files_root)
        })
    }

    pub fn set_capture_hook(&mut self, hook: Arc<dyn MutationCaptureHook>) {
        self.capture_hook = Some(hook);
    }

    pub fn claim_checkpoint(&self, request_id: &str, root_id: &str, items: &[CheckpointSelection], deadline: std::time::Instant) -> Result<Box<dyn CheckpointTicket>, EngineError> {
        self.capture_hook.as_ref().ok_or(EngineError::Conflict)?.claim_checkpoint(request_id, root_id, items, deadline).map_err(EngineError::HistoryCapture)
    }

    pub fn checkpoint_root(&self, root_id: &str) -> Result<PathBuf, EngineError> {
        self.capture_hook.as_ref().ok_or(EngineError::Conflict)?.checkpoint_root(root_id).map_err(EngineError::HistoryCapture)
    }

    pub fn register_checkpoint_file(&self, path: &Path, root_id: &str) -> Result<serde_json::Value, EngineError> {
        self.capture_hook.as_ref().ok_or(EngineError::Conflict)?.register_checkpoint_file(path, root_id).map_err(EngineError::HistoryCapture)
    }

    pub fn ordinary_write_ready(&self) -> bool {
        self.capture_hook.as_ref().is_some_and(|hook| hook.ordinary_write_ready())
    }

    pub fn checkpoint_ready(&self) -> bool {
        self.capture_hook.as_ref().is_some_and(|hook| hook.checkpoint_ready())
    }

    pub fn capture_status(&self, action_id: &str) -> Option<CaptureStatus> {
        self.capture_hook.as_ref()?.status(action_id)
    }

    pub fn last_capture_status(&self) -> Option<CaptureStatus> {
        let hook = self.capture_hook.as_ref()?;
        let action_id = hook.last_action_id()?;
        hook.status(&action_id)
    }

    pub(crate) fn graph_capture_active(&self) -> bool {
        self.capture_hook.as_ref().is_some_and(|hook| hook.graph_capture_active())
    }
    pub(crate) fn validate_graph_stage(&self, path: &Path, digest: &str, length: u64) -> Result<(), EngineError> {
        if let Some(hook) = self.capture_hook.as_ref() {hook.validate_graph_stage(path, digest, length).map_err(EngineError::HistoryCapture)?;}
        Ok(())
    }

    fn prepare_capture(
        &self,
        operation: &str,
        targets: Vec<CaptureTarget>,
    ) -> Result<Option<Box<dyn MutationCaptureTicket>>, EngineError> {
        let Some(hook) = self.capture_hook.as_ref() else {
            if self.capture_required {
                return Err(EngineError::HistoryCapture("required recovery preimage is unavailable; original preserved".into()));
            }
            return Ok(None);
        };
        match hook.prepare(operation, &targets) {
            Ok(ticket) => Ok(Some(ticket)),
            Err(error) => Err(EngineError::HistoryCapture(error)),
        }
    }

    fn finish_capture(
        ticket: Option<Box<dyn MutationCaptureTicket>>,
        after: Vec<CaptureAfter>,
    ) -> Result<(), EngineError> {
        if let Some(ticket) = ticket {
            // Capture failure belongs to the recovery status, not the already
            // committed live filesystem operation. Never invite a write retry.
            let _ = ticket.complete(after);
        }
        Ok(())
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
            RootKind::ExactFile => crate::platform_path_equal(&resolved, &root_path),
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
            RootKind::ExactFile => crate::platform_path_equal(&resolved, &root_path),
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
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
        #[cfg(windows)]
        let _directory_pin = crate::windows_fs::pin_directory(path)?;
        let metadata = fs::metadata(path)?;
        if !metadata.is_dir() {
            return Err(EngineError::NotADirectory);
        }
        let generation = directory_generation(path, &metadata)?;
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
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
        #[cfg(windows)]
        let _directory_pin = crate::windows_fs::pin_directory(path)?;
        let metadata = fs::metadata(path)?;
        if !metadata.is_dir() {
            return Err(EngineError::NotADirectory);
        }
        let generation = directory_generation(path, &metadata)?;
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
        #[cfg(target_os = "linux")]
        let directory_revision = if metadata.is_dir() { Some(directory_metadata_revision(&metadata)) } else { None };
        #[cfg(not(target_os = "linux"))]
        let directory_revision = None;
        Ok(FileStat {
            path: path.to_path_buf(),
            kind,
            size: metadata.len(),
            modified_unix_ms: system_time_ms(metadata.modified().ok()),
            mode: file_mode(path),
            fingerprint,
            directory_revision,
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
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
        if length > self.config.max_read_bytes {
            return Err(EngineError::LimitExceeded);
        }
        let mut file = File::open(path)?;
        #[cfg(windows)]
        crate::windows_fs::validate_opened_path(&file, path)?;
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
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
        let mut file = File::open(path)?;
        #[cfg(windows)]
        crate::windows_fs::validate_opened_path(&file, path)?;
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
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
        if max_bytes == 0 || max_bytes > self.config.max_read_bytes {
            return Err(EngineError::LimitExceeded);
        }
        let mut file = File::open(path)?;
        #[cfg(windows)]
        crate::windows_fs::validate_opened_path(&file, path)?;
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
        self.replace_if_fingerprint_resolved(&resolved, expected, bytes, None)
    }

    pub fn replace_if_fingerprint_app(
        &self,
        path: &Path,
        expected: Option<&Fingerprint>,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        let resolved = resolve_for_write(path)?;
        self.replace_if_fingerprint_resolved(&resolved, expected, bytes, None)
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
        self.replace_if_fingerprint_resolved(&resolved, expected, bytes, None)
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
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
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
        let ticket = self.prepare_capture(
            "create", vec![CaptureTarget { resource_id: path.to_path_buf(), before: None }],
        )?;
        let temporary = temporary_path(
            parent,
            path.file_name().and_then(|name| name.to_str()).unwrap_or("file"),
        );
        let result = (|| -> io::Result<(File, Fingerprint)> {
            staging.seek(SeekFrom::Start(0))?;
            let mut output_options = OpenOptions::new();
            output_options.read(true).write(true).create_new(true);
            #[cfg(unix)]
            output_options.mode(0o600);
            let mut output = output_options.open(&temporary)?;
            let copied = io::copy(staging, &mut output)?;
            if copied != expected_length || copied > self.config.max_write_bytes as u64 {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "staged file length changed"));
            }
            output.sync_all()?;
            output.seek(SeekFrom::Start(0))?;
            let mut hasher = Sha256::new();
            let mut buffer = [0_u8; 64 * 1024];
            let mut length = 0_u64;
            loop {
                let read = output.read(&mut buffer)?;
                if read == 0 { break; }
                length = length.checked_add(read as u64).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "staged output length overflow"))?;
                if length > expected_length { return Err(io::Error::new(io::ErrorKind::InvalidData, "staged output length changed")); }
                hasher.update(&buffer[..read]);
            }
            if length != expected_length {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "staged output length changed"));
            }
            let fingerprint = Fingerprint { algorithm: "sha256", value: format!("sha256:{:x}:{}", hasher.finalize(), length) };
            publish_file(&temporary, path, false)?;
            Ok((output, fingerprint))
        })();
        let (mut output, fingerprint) = match result {
            Ok(value) => value,
            Err(error) => {
                let _ = fs::remove_file(&temporary);
                if let Some(ticket) = ticket { ticket.abort(); }
                return Err(error.into());
            }
        };
        self.clear_sorted_snapshot_cache();
        if let Some(ticket) = ticket {
            let after = (|| -> io::Result<Vec<u8>> {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::MetadataExt;
                    let held = output.metadata()?;
                    let current = fs::symlink_metadata(path)?;
                    if !current.is_file() || held.dev() != current.dev() || held.ino() != current.ino() {
                        return Err(io::Error::new(io::ErrorKind::InvalidData, "published file identity changed"));
                    }
                }
                #[cfg(windows)]
                crate::windows_fs::validate_opened_path(&output, path)?;
                #[cfg(not(any(unix, windows)))]
                return Err(io::Error::new(io::ErrorKind::Unsupported, "published file identity unavailable"));
                output.seek(SeekFrom::Start(0))?;
                let mut bytes = Vec::new();
                (&mut output).take(self.config.max_write_bytes as u64 + 1).read_to_end(&mut bytes)?;
                if bytes.len() as u64 != expected_length || fingerprint_bytes(&bytes) != fingerprint {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "published file content changed"));
                }
                Ok(bytes)
            })();
            match after {
                Ok(bytes) => Self::finish_capture(Some(ticket), vec![CaptureAfter { resource_id: path.to_path_buf(), after: Some(bytes) }])?,
                Err(error) => ticket.after_unavailable(format!("after-state verification failed: {error}")),
            }
        }
        Ok(ReplaceOutcome::Created { fingerprint })
    }

    fn create_file_resolved(
        &self,
        path: &Path,
        bytes: &[u8],
    ) -> Result<ReplaceOutcome, EngineError> {
        if path.exists() {
            return Err(EngineError::DestinationExists);
        }
        self.replace_if_fingerprint_resolved(path, None, bytes, None)
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
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
        if path.exists() {
            return Err(EngineError::DestinationExists);
        }
        let ticket = self.prepare_capture(
            "mkdir",
            vec![CaptureTarget {
                resource_id: path.to_path_buf(),
                before: None,
            }],
        )?;
        fs::create_dir(path)?;
        self.clear_sorted_snapshot_cache();
        match (ticket, crate::history_capture::directory_manifest(path)) {
            (Some(ticket), Ok(after)) => Self::finish_capture(
                Some(ticket),
                vec![CaptureAfter {
                    resource_id: path.to_path_buf(),
                    after: Some(after),
                }],
            ),
            (Some(ticket), Err(error)) => {
                ticket.after_unavailable(format!("after-state read failed: {error}"));
                Ok(())
            }
            (None, _) => Ok(())
        }?;
        Ok(path.to_path_buf())
    }

    fn history_payload(path: &Path) -> io::Result<Vec<u8>> {
        if path.is_symlink() {
            Ok(fs::read_link(path)?.to_string_lossy().as_bytes().to_vec())
        } else if path.is_dir() {
            crate::history_capture::directory_manifest(path).map_err(io::Error::other)
        } else {
            fs::read(path)
        }
    }

    /// App-lane original-ID profile. Host owns the durable one-shot admission.
    /// Linux first profile: the revision is checked before History registration
    /// or staging. The selected directory descriptor remains held throughout.
    #[cfg(target_os="linux")]
    fn ordinary_create_resolved(&self,path:&Path,binding:&OrdinaryCreateCapture,bytes:&[u8])->Result<ReplaceOutcome,EngineError> {
        use std::os::fd::AsRawFd;
        if bytes.len()>256*1024 || std::str::from_utf8(bytes).is_err() || bytes.contains(&0) {return Err(EngineError::BinaryOrUnsupportedText);}
        let parent=path.parent().ok_or(EngineError::Conflict)?;
        if fs::canonicalize(parent)?!=parent {return Err(EngineError::Conflict);}
        let directory=open_selected_directory(Path::new("/"),parent)?;
        if directory_metadata_revision(&directory.metadata()?)!=binding.parent_revision {return Err(EngineError::Conflict);}
        let name=path.file_name().ok_or(EngineError::Conflict)?;
        let anchored=PathBuf::from(format!("/proc/self/fd/{}",directory.as_raw_fd())).join(name);
        match fs::symlink_metadata(&anchored) {
            Ok(_) => return Err(EngineError::DestinationExists),
            Err(error) if error.kind()==io::ErrorKind::NotFound => (),
            Err(error) => return Err(error.into()),
        }
        let target=CaptureTarget{resource_id:path.to_path_buf(),before:None};
        let ticket=self.capture_hook.as_ref().ok_or_else(||EngineError::HistoryCapture("ordinary create capture unavailable".into()))?.prepare_ordinary_create(&target,binding,bytes).map_err(EngineError::HistoryCapture)?;
        let temp=temporary_path(anchored.parent().ok_or(EngineError::Conflict)?,name.to_str().ok_or(EngineError::Conflict)?);
        let result=(||->io::Result<()> {let mut f=OpenOptions::new().write(true).create_new(true).open(&temp)?;f.write_all(bytes)?;f.sync_all()?;drop(f);publish_file(&temp,&anchored,false)})();
        if let Err(e)=result {let _=fs::remove_file(&temp);ticket.abort();return Err(e.into());}
        let fingerprint=fingerprint_file(&anchored)?;
        self.clear_sorted_snapshot_cache();
        match Self::history_payload(&anchored) {Ok(after)=>Self::finish_capture(Some(ticket),vec![CaptureAfter{resource_id:path.to_path_buf(),after:Some(after)}])?,Err(e)=>ticket.after_unavailable(format!("after-state read failed: {e}"))};
        Ok(ReplaceOutcome::Created{fingerprint})
    }
    #[cfg(target_os="linux")]
    pub fn ordinary_create_scoped(&self,scope:&AppScope,path:&Path,binding:&OrdinaryCreateCapture,bytes:&[u8])->Result<ReplaceOutcome,EngineError> {
        if !scope.host {self.authorize_app_checked(scope,path,&BTreeSet::from([Capability::Write]),true)?;}
        self.ordinary_create_resolved(path,binding,bytes)
    }
    #[cfg(target_os="linux")]
    pub fn ordinary_create_agent(&self,owner:&str,scope:&AgentScope,path:&Path,binding:&OrdinaryCreateCapture,bytes:&[u8])->Result<ReplaceOutcome,EngineError> {
        self.authorize_checked(owner,scope,path,&BTreeSet::from([Capability::Write]),true)?;
        self.ordinary_create_resolved(path,binding,bytes)
    }
    #[cfg(target_os="linux")]
    pub fn ordinary_create_status_scoped(&self,scope:&AppScope,path:&Path,binding:&OrdinaryCreateCapture)->Result<serde_json::Value,EngineError> {
        if !scope.host {self.authorize_app_checked(scope,path,&BTreeSet::from([Capability::Read]),true)?;}
        self.capture_hook.as_ref().ok_or_else(||EngineError::HistoryCapture("ordinary create status unavailable".into()))?.ordinary_create_status(path,binding).map_err(EngineError::HistoryCapture)
    }
    #[cfg(target_os="linux")]
    pub fn ordinary_create_status_agent(&self,owner:&str,scope:&AgentScope,path:&Path,binding:&OrdinaryCreateCapture)->Result<serde_json::Value,EngineError> {
        self.authorize_checked(owner,scope,path,&BTreeSet::from([Capability::Read]),true)?;
        self.capture_hook.as_ref().ok_or_else(||EngineError::HistoryCapture("ordinary create status unavailable".into()))?.ordinary_create_status(path,binding).map_err(EngineError::HistoryCapture)
    }

    pub fn ordinary_replace_scoped(&self, scope: &AppScope, path: &Path,
        binding: &OrdinaryWriteCapture, bytes: &[u8]) -> Result<ReplaceOutcome, EngineError> {
        if !scope.host {
            self.authorize_app_checked(scope, path, &BTreeSet::from([Capability::Write]), false)?;
        }
        self.ordinary_replace_resolved(path,binding,bytes)
    }

    pub fn ordinary_replace_agent(&self, owner:&str, scope:&AgentScope, path:&Path,
        binding:&OrdinaryWriteCapture, bytes:&[u8]) -> Result<ReplaceOutcome,EngineError> {
        self.authorize_checked(owner,scope,path,&BTreeSet::from([Capability::Write]),false)?;
        self.ordinary_replace_resolved(path,binding,bytes)
    }

    fn ordinary_replace_resolved(&self,path:&Path,binding:&OrdinaryWriteCapture,bytes:&[u8])->Result<ReplaceOutcome,EngineError> {
        if bytes.len() > 256 * 1024 || std::str::from_utf8(bytes).is_err() || bytes.contains(&0) {
            return Err(EngineError::BinaryOrUnsupportedText);
        }
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() || fs::canonicalize(path)? != path {
            return Err(EngineError::Conflict);
        }
        let expected = Fingerprint { algorithm: "sha256", value: binding.expected_fingerprint.clone() };
        self.replace_if_fingerprint_resolved(path, Some(&expected), bytes, Some(binding))
    }

    pub fn ordinary_replace_status_scoped(&self, scope: &AppScope, path: &Path,
        binding: &OrdinaryWriteCapture) -> Result<serde_json::Value, EngineError> {
        if !scope.host {
            self.authorize_app_checked(scope, path, &BTreeSet::from([Capability::Read]), true)?;
        }
        let resolved = resolve_for_write(path)?;
        if resolved != path { return Err(EngineError::Conflict); }
        self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("ordinary capture unavailable".into()))?
            .ordinary_write_status(path, binding).map_err(EngineError::HistoryCapture)
    }

    pub fn ordinary_replace_status_agent(&self, owner:&str, scope:&AgentScope, path:&Path,
        binding:&OrdinaryWriteCapture) -> Result<serde_json::Value,EngineError> {
        self.authorize_checked(owner,scope,path,&BTreeSet::from([Capability::Read]),true)?;
        let resolved=resolve_for_write(path)?;
        if resolved!=path {return Err(EngineError::Conflict);}
        self.capture_hook.as_ref().ok_or_else(||EngineError::HistoryCapture("ordinary capture unavailable".into()))?
            .ordinary_write_status(path,binding).map_err(EngineError::HistoryCapture)
    }

    fn replace_if_fingerprint_resolved(
        &self,
        path: &Path,
        expected: Option<&Fingerprint>,
        bytes: &[u8],
        ordinary: Option<&OrdinaryWriteCapture>,
    ) -> Result<ReplaceOutcome, EngineError> {
        #[cfg(windows)]
        let _parent_pins = crate::windows_fs::pin_parent(path)?;
        if bytes.len() > self.config.max_write_bytes {
            return Err(EngineError::LimitExceeded);
        }
        let current = path.exists().then(|| fingerprint_file(path)).transpose()?;
        if expected != current.as_ref() {
            if expected.is_some() || current.is_some() {
                return Err(EngineError::Conflict);
            }
        }
        let target = CaptureTarget {
            resource_id: path.to_path_buf(),
            before: current.as_ref().map(|_| Self::history_payload(path)).transpose()?,
        };
        let ticket = if let Some(binding) = ordinary {
            let before = target.before.as_ref().ok_or(EngineError::Conflict)?;
            if before.len() > 256 * 1024 || before.contains(&0) { return Err(EngineError::BinaryOrUnsupportedText); }
            Some(self.capture_hook.as_ref().ok_or_else(|| EngineError::HistoryCapture("ordinary capture unavailable".into()))?
                .prepare_ordinary_write(&target, binding, bytes).map_err(EngineError::HistoryCapture)?)
        } else {
            self.prepare_capture(if current.is_some() { "replace" } else { "create" }, vec![target])?
        };
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
            publish_file(&temp_path, path, current.is_some())?;
            Ok::<(), io::Error>(())
        })();
        if write_result.is_err() {
            #[cfg(windows)]
            let preserve_recovery = write_result.as_ref().err().is_some_and(|error| matches!(error.raw_os_error(), Some(1176 | 1177)));
            #[cfg(not(windows))]
            let preserve_recovery = false;
            if !preserve_recovery { let _ = fs::remove_file(&temp_path); }
        }
        if let Err(error) = write_result {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error.into());
        }
        let fingerprint = fingerprint_file(path)?;
        self.clear_sorted_snapshot_cache();
        match (ticket, Self::history_payload(path)) {
            (Some(ticket), Ok(after)) => Self::finish_capture(
                Some(ticket),
                vec![CaptureAfter {
                    resource_id: path.to_path_buf(),
                    after: Some(after),
                }],
            ),
            (Some(ticket), Err(error)) => {
                ticket.after_unavailable(format!("after-state read failed: {error}"));
                Ok(())
            }
            (None, _) => Ok(())
        }?;
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
        #[cfg(windows)]
        let _source_pins = crate::windows_fs::pin_parent(source)?;
        #[cfg(windows)]
        let _destination_pins = crate::windows_fs::pin_parent(destination)?;
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
                    before: Some(Self::history_payload(source)?),
                },
                CaptureTarget {
                    resource_id: destination.to_path_buf(),
                    before: None,
                },
            ],
        )?;
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
            publish_file(&temporary, destination, false)?;
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
            Self::history_payload(source).and_then(|source_after| {
                Self::history_payload(destination).map(|destination_after| (source_after, destination_after))
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
            (Some(ticket), Err(error)) => {
                ticket.after_unavailable(format!("after-state read failed: {error}"));
                Ok(())
            }
            (None, _) => Ok(())
        }?;
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
        #[cfg(windows)]
        let _source_pins = crate::windows_fs::pin_parent(source)?;
        #[cfg(windows)]
        let _destination_pins = crate::windows_fs::pin_parent(destination)?;
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
                    before: Some(Self::history_payload(source)?),
                },
                CaptureTarget {
                    resource_id: destination.to_path_buf(),
                    before: None,
                },
            ],
        )?;
        let result = move_without_replace(source, destination).map_err(|error| {
            if error.raw_os_error() == Some(if cfg!(windows) { 17 } else { 18 }) {
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
        match (ticket, Self::history_payload(destination)) {
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
            (Some(ticket), Err(error)) => {
                ticket.after_unavailable(format!("after-state read failed: {error}"));
                Ok(())
            }
            (None, _) => Ok(())
        }?;
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
                    before: Some(Self::history_payload(&entry.trashed_path)?),
                },
                CaptureTarget {
                    resource_id: entry.original_path.clone(),
                    before: None,
                },
            ],
        )?;
        if let Err(error) = fs::rename(&entry.trashed_path, &entry.original_path) {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error.into());
        }
        self.clear_sorted_snapshot_cache();
        match (ticket, Self::history_payload(&entry.original_path)) {
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
            (Some(ticket), Err(error)) => {
                ticket.after_unavailable(format!("after-state read failed: {error}"));
                Ok(())
            }
            (None, _) => Ok(())
        }?;
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
        if root.kind == RootKind::ExactFile || crate::platform_path_equal(&resolved, &root_path) {
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
                before: Some(Self::history_payload(resolved)?),
            }],
        )?;
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
        )?;
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
                    before: Some(Self::history_payload(&entry.trashed_path)?),
                },
                CaptureTarget {
                    resource_id: entry.original_path.clone(),
                    before: None,
                },
            ],
        )?;
        if let Err(error) = fs::rename(&entry.trashed_path, &entry.original_path) {
            if let Some(ticket) = ticket {
                ticket.abort();
            }
            return Err(error.into());
        }
        self.clear_sorted_snapshot_cache();
        match (ticket, Self::history_payload(&entry.original_path)) {
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
            (Some(ticket), Err(error)) => {
                ticket.after_unavailable(format!("after-state read failed: {error}"));
                Ok(())
            }
            (None, _) => Ok(())
        }?;
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
        search_walk(root, options, &SearchDetails::default(), cancelled, false)
    }

    pub fn filename_search_app(
        &self,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        let resolved = resolve_existing_path(root)?;
        search_walk(&resolved, options, &SearchDetails::default(), cancelled, false)
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
        search_walk(root, options, &SearchDetails::default(), cancelled, false)
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
        search_walk(root, options, &SearchDetails::default(), cancelled, true)
    }

    pub fn content_search_app(
        &self,
        root: &Path,
        options: &SearchOptions,
        cancelled: Option<&AtomicBool>,
    ) -> Result<SearchResult, EngineError> {
        let resolved = resolve_existing_path(root)?;
        search_walk(&resolved, options, &SearchDetails::default(), cancelled, true)
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
        search_walk(root, options, &SearchDetails::default(), cancelled, true)
    }
    pub(crate) fn agent_search_with_details(
        &self,
        owner_id: &str,
        scope: &AgentScope,
        root: &Path,
        options: &SearchOptions,
        details: &SearchDetails,
        content: bool,
    ) -> Result<SearchResult, EngineError> {
        self.authorize_checked(owner_id, scope, root, &BTreeSet::from([Capability::Read]), false)?;
        search_walk(root, options, details, None, content)
    }

    pub(crate) fn app_search_with_details(
        &self,
        scope: &AppScope,
        root: &Path,
        options: &SearchOptions,
        details: &SearchDetails,
        content: bool,
    ) -> Result<SearchResult, EngineError> {
        if scope.host {
            return search_walk(&resolve_existing_path(root)?, options, details, None, content);
        }
        self.authorize_app_checked(scope, root, &BTreeSet::from([Capability::Read]), false)?;
        search_walk(root, options, details, None, content)
    }
}

fn unavailable_search_descendant(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied
    ) || (cfg!(target_os = "macos") && error.raw_os_error() == Some(9))
}

fn search_walk(
    root: &Path,
    options: &SearchOptions,
    details: &SearchDetails,
    cancelled: Option<&AtomicBool>,
    content: bool,
) -> Result<SearchResult, EngineError> {
    validate_search_options(options, details)?;
    let mut pending = vec![(root.to_path_buf(), 0_usize)];
    let mut result = SearchResult {
        matches: Vec::new(),
        complete: true,
        work: WorkCost::default(),
        eligible_files: 0,
        partial_reasons: BTreeSet::new(),
        locations: Vec::new(),
    };
    let needle = if options.case_sensitive {
        options.query.clone()
    } else {
        options.query.to_lowercase()
    };
    let timed_out = || details.deadline.is_some_and(|deadline| Instant::now() >= deadline);
    'walk: while let Some((path, depth)) = pending.pop() {
        check_cancelled(cancelled)?;
        if timed_out() {
            result.partial_reasons.insert("timeout");
            break;
        }
        if depth > 0 && details.excludes.iter().any(|pattern|
            glob_path_matches(root, &path, pattern, options.case_sensitive))
        {
            continue;
        }
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if depth > 0 && unavailable_search_descendant(&error) => {
                result.partial_reasons.insert("descendant_unavailable");
                continue;
            }
            Err(error) => return Err(error.into()),
        };
        if metadata.is_file() {
            if options.file_glob.as_ref().is_some_and(|pattern|
                !glob_path_matches(root, &path, pattern, options.case_sensitive))
            {
                continue;
            }
            result.eligible_files += 1;
            let matched = if content {
                match search_content_file(&path, &needle, options, details, &mut result) {
                    Ok(matched) => matched,
                    Err(error) if depth > 0 && unavailable_search_descendant(&error) => {
                        result.partial_reasons.insert("descendant_unavailable");
                        continue;
                    }
                    Err(error) => return Err(error.into()),
                }
            } else if options.glob {
                glob_path_matches(root, &path, &options.query, options.case_sensitive)
            } else {
                file_name_matches(&path, &needle, options.case_sensitive)
            };
            if matched {
                result.matches.push(path);
                if result.matches.len() >= options.max_results {
                    result.partial_reasons.insert("result_limit");
                    break;
                }
            }
            continue;
        }
        if !metadata.is_dir() {
            continue;
        }
        if depth >= options.max_depth {
            result.partial_reasons.insert("depth_limit");
            continue;
        }
        if result.work.entries_visited >= options.max_entries {
            result.partial_reasons.insert("entry_limit");
            continue;
        }
        let directory = match fs::read_dir(&path) {
            Ok(directory) => directory,
            Err(error) if depth > 0 && unavailable_search_descendant(&error) => {
                result.partial_reasons.insert("descendant_unavailable");
                continue;
            }
            Err(error) => return Err(error.into()),
        };
        for entry in directory {
            check_cancelled(cancelled)?;
            if timed_out() {
                result.partial_reasons.insert("timeout");
                break 'walk;
            }
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) if unavailable_search_descendant(&error) => {
                    result.partial_reasons.insert("descendant_unavailable");
                    continue;
                }
                Err(error) => return Err(error.into()),
            };
            if result.work.entries_visited >= options.max_entries {
                result.partial_reasons.insert("entry_limit");
                break;
            }
            result.work.entries_visited += 1;
            if !options.include_hidden && entry.file_name().to_string_lossy().starts_with('.') {
                continue;
            }
            pending.push((entry.path(), depth + 1));
        }
    }
    result.complete = result.partial_reasons.is_empty();
    Ok(result)
}

fn search_content_file(
    path: &Path,
    needle: &str,
    options: &SearchOptions,
    details: &SearchDetails,
    result: &mut SearchResult,
) -> io::Result<bool> {
    let mut open = OpenOptions::new();
    open.read(true);
    #[cfg(unix)]
    open.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    let file = open.open(path)?;
    let before = file.metadata()?;
    if !before.is_file() {
        result.partial_reasons.insert("descendant_unavailable");
        return Ok(false);
    }
    let mut body = Vec::new();
    (&file).take(options.max_bytes_per_file as u64).read_to_end(&mut body)?;
    result.work.bytes_read += body.len() as u64;
    let after = file.metadata()?;
    let unchanged = before.len() == after.len() && before.modified()? == after.modified()?;
    if !unchanged {
        result.partial_reasons.insert("file_changed");
        return Ok(false);
    }
    let whole_file = body.len() as u64 == before.len();
    if !whole_file {
        result.partial_reasons.insert("file_byte_limit");
    }
    let folded;
    let haystack = if options.case_sensitive {
        body.as_slice()
    } else {
        folded = String::from_utf8_lossy(&body).to_lowercase();
        folded.as_bytes()
    };
    let Some(offset) = haystack.windows(needle.len()).position(|window| window == needle.as_bytes()) else {
        return Ok(false);
    };
    // Sparse evidence for the first known match per file. Non-ASCII case folding
    // can change offsets, so that lane keeps the existing path-only result.
    if details.locations && whole_file && (options.case_sensitive || body.is_ascii())
        && !body.contains(&0) && std::str::from_utf8(&body).is_ok()
    {
        result.locations.push(SearchMatch {
            path: path.to_path_buf(),
            line: 1 + body[..offset].iter().filter(|&&byte| byte == b'\n').count(),
            start_byte: offset,
            end_byte: offset + needle.len(),
            revision: fingerprint_bytes(&body),
        });
    }
    Ok(true)
}

fn validate_search_options(options: &SearchOptions, details: &SearchDetails) -> Result<(), EngineError> {
    if options.query.is_empty() || options.query.len() > 4096 || options.max_results == 0 {
        return Err(EngineError::InvalidSearchPattern);
    }
    if details.excludes.len() > 8 {
        return Err(EngineError::InvalidSearchPattern);
    }
    for pattern in options.file_glob.iter()
        .chain(options.glob.then_some(&options.query))
        .chain(&details.excludes)
    {
        // This bounded dialect deliberately supports only *, ** and ?.
        if pattern.is_empty() || pattern.chars().count() > 512
            || pattern.starts_with('/')
            || pattern.contains(['[', ']', '{', '}', '\\'])
            || pattern.split('/').any(|part| part.is_empty() || part == ".."
                || (part.contains("**") && part != "**")) {
            return Err(EngineError::InvalidSearchPattern);
        }
    }
    Ok(())
}

/// Polynomial dynamic programming, with Unicode scalar ? and segment-local *.
/// There is no regex engine or recursive wildcard backtracking.
fn glob_segment_matches(pattern: &str, name: &str) -> bool {
    let name: Vec<char> = name.chars().collect();
    let mut previous = vec![false; name.len() + 1];
    previous[0] = true;
    for token in pattern.chars() {
        let mut current = vec![false; name.len() + 1];
        if token == '*' {
            current[0] = previous[0];
        }
        for index in 1..=name.len() {
            current[index] = if token == '*' {
                previous[index] || current[index - 1]
            } else {
                previous[index - 1] && (token == '?' || token == name[index - 1])
            };
        }
        previous = current;
    }
    previous[name.len()]
}

fn glob_path_matches(root: &Path, path: &Path, pattern: &str, case_sensitive: bool) -> bool {
    let relative = if root.is_file() {
        path.file_name().map(Path::new).unwrap_or(path)
    } else {
        path.strip_prefix(root).unwrap_or(path)
    };
    // Join actual components; backslash is a legal Unix filename character.
    let name = relative.components().map(|part| part.as_os_str().to_string_lossy())
        .collect::<Vec<_>>().join("/");
    let (pattern, name) = if case_sensitive {
        (pattern.to_owned(), name)
    } else {
        (pattern.to_lowercase(), name.to_lowercase())
    };
    if !pattern.contains('/') {
        return glob_segment_matches(&pattern, name.rsplit('/').next().unwrap_or(&name));
    }
    let segments: Vec<&str> = name.split('/').collect();
    let mut previous = vec![false; segments.len() + 1];
    previous[0] = true;
    for part in pattern.split('/') {
        let mut current = vec![false; segments.len() + 1];
        if part == "**" { current[0] = previous[0]; }
        for index in 1..=segments.len() {
            current[index] = if part == "**" {
                previous[index] || current[index - 1]
            } else {
                previous[index - 1] && glob_segment_matches(part, segments[index - 1])
            };
        }
        previous = current;
    }
    previous[segments.len()]
}

fn directory_generation(_path: &Path, metadata: &fs::Metadata) -> io::Result<u64> {
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
    #[cfg(windows)]
    {
        let identity = crate::windows_fs::directory_identity(_path)?;
        generation ^= identity.volume.rotate_left(37);
        generation ^= (identity.change_time as u64).rotate_left(7);
        for chunk in identity.file_id.chunks_exact(8) {
            generation ^= u64::from_le_bytes(chunk.try_into().unwrap()).rotate_left(47);
        }
    }
    Ok(generation)
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
    #[cfg(windows)]
    crate::windows_fs::validate_opened_path(&file, path)?;
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

#[cfg(windows)]
fn move_without_replace(source: &Path, destination: &Path) -> io::Result<()> {
    crate::windows_fs::publish_file(source, destination, false)
}
#[cfg(target_os = "linux")]
fn move_without_replace(source: &Path, destination: &Path) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let source = CString::new(source.as_os_str().as_bytes()).map_err(io::Error::other)?;
    let destination = CString::new(destination.as_os_str().as_bytes()).map_err(io::Error::other)?;
    // RENAME_NOREPLACE is an atomic destination-existence check for files and directories.
    if unsafe { libc::renameat2(libc::AT_FDCWD, source.as_ptr(), libc::AT_FDCWD, destination.as_ptr(), libc::RENAME_NOREPLACE) } == 0 { Ok(()) } else { Err(io::Error::last_os_error()) }
}
#[cfg(target_os = "macos")]
fn move_without_replace(source: &Path, destination: &Path) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let source = CString::new(source.as_os_str().as_bytes()).map_err(io::Error::other)?;
    let destination = CString::new(destination.as_os_str().as_bytes()).map_err(io::Error::other)?;
    if unsafe { libc::renamex_np(source.as_ptr(), destination.as_ptr(), libc::RENAME_EXCL) } == 0 { Ok(()) } else { Err(io::Error::last_os_error()) }
}
#[cfg(all(not(windows), not(target_os = "linux"), not(target_os = "macos")))]
fn move_without_replace(_source: &Path, _destination: &Path) -> io::Result<()> {
    Err(io::Error::new(io::ErrorKind::Unsupported, "atomic no-replace move unavailable"))
}

#[cfg(windows)]
fn publish_file(temporary: &Path, destination: &Path, replace: bool) -> io::Result<()> {
    crate::windows_fs::publish_file(temporary, destination, replace)
}
#[cfg(not(windows))]
fn publish_file(temporary: &Path, destination: &Path, replace: bool) -> io::Result<()> {
    if replace {
        fs::rename(temporary, destination)
    } else {
        // Both names reside on the same filesystem. Linking atomically fails
        // if the destination exists, including a concurrent creator.
        fs::hard_link(temporary, destination)?;
        // Publication has succeeded; inability to remove the temporary name
        // must not turn a committed create into a reported no-effect error.
        let _ = fs::remove_file(temporary);
        Ok(())
    }
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

#[cfg(test)]
mod managed_journal_tests {
    use super::*;

    #[cfg(target_os = "linux")]
    #[test]
    fn worktree_stage_pins_journal_identity_and_bounds_frames() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixListener;
        let base=std::env::temp_dir().join(format!("worktree-stage-{}-{}",std::process::id(),SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
        fs::create_dir_all(&base).unwrap();let journal=ManagedJournal::open(&base.join("journal"),"worktree-operation-0001").unwrap();
        let bytes=vec![0xff;300_000];let digest=journal.stage("worktree",&bytes).unwrap();let file=File::open(journal.staged_path("worktree").unwrap()).unwrap();let identity=managed_object_identity(&file).unwrap();
        let request_digest="a".repeat(64);let action="history-worktree-action";
        let mut record=ManagedJournalRecord{version:1,operation_id:journal.operation_id.clone(),owner_id:"files-owner".into(),request_digest:request_digest.clone(),phase:ManagedJournalPhase::Prepared,
            plan:serde_json::json!({"kind":"source_worktree_create_v2","history_action_id":action,"history_actor_id":"actor","history_account_id":"account","root_id":"workspace-root"}),file_stages:vec![],directory_events:vec![],commit_progress:vec![],cleanup_progress:vec![],preparation:vec![],registry_plan:None,history_prepare:None,history_completion:None,review_outcome:None};
        journal.write(&record).unwrap();record.history_prepare=Some(serde_json::json!({"action_id":action}));record.phase=ManagedJournalPhase::HistoryPrepared;journal.write(&record).unwrap();record.phase=ManagedJournalPhase::Committing;journal.write(&record).unwrap();record.phase=ManagedJournalPhase::LiveCommitted;journal.write(&record).unwrap();
        record.history_completion=Some(serde_json::json!({"worktree_stage":{"label":"worktree","length":bytes.len(),"sha256":digest,"identity":identity}}));record.phase=ManagedJournalPhase::HistoryPending;journal.write(&record).unwrap();
        assert!(journal.worktree_history_stage("other",action,&request_digest,std::time::Instant::now()+std::time::Duration::from_secs(30)).is_err());
        let stage=journal.worktree_history_stage("files-owner",action,&request_digest,std::time::Instant::now()+std::time::Duration::from_secs(30)).unwrap();
        let socket=base.join("history.sock");let listener=UnixListener::bind(&socket).unwrap();
        let server=std::thread::spawn(move ||{let mut chunks=0;let mut largest=0usize;for _ in 0..8 {let (stream,_)=listener.accept().unwrap();let mut reader=BufReader::new(stream);let mut line=String::new();reader.read_line(&mut line).unwrap();largest=largest.max(line.len());let msg:serde_json::Value=serde_json::from_str(&line).unwrap();if msg.get("StageChunk").is_some(){chunks+=1;}let reply=if msg.get("ClaimFilesWorktree").is_some(){serde_json::json!({"FilesWorktreeSlot":{"upload_id":"odysseus-worktree-fixture","fingerprint":format!("sha256:{:x}:300000",Sha256::digest(vec![0xff;300000])),"content_length":300000}})}else if let Some(finish)=msg.get("StageFinish") {serde_json::json!({"Staged":{"upload_id":finish["upload_id"],"content_length":300000}})}else if msg.get("CompleteFilesWorktree").is_some(){serde_json::json!({"Action":{"action_id":"history-worktree-action","state":"AfterDurable"}})}else if msg.get("FinalizeFilesWorktree").is_some(){serde_json::json!({"Action":{"action_id":"history-worktree-action","state":"Complete"}})}else{serde_json::json!("Accepted")};reader.get_mut().write_all(format!("{}\n",reply).as_bytes()).unwrap();} (chunks,largest)});
        let mut hook=crate::HistoryServiceHook::new(&socket,"actor","account","workspace",&base,"fixture").unwrap();crate::HistoryServiceHook::set_worktree_test_token(&mut hook,"workspace-root","private-fixture");
        let upload=hook.stage_worktree_history(&stage,std::time::Instant::now()+std::time::Duration::from_secs(10)).unwrap();
        assert!(upload.starts_with("odysseus-worktree-"));let (chunks,largest)=server.join().unwrap();assert_eq!(chunks,3);assert!(largest<1024*1024);
        let stage_path=journal.staged_path("worktree").unwrap();fs::write(&stage_path,b"changed").unwrap();assert!(stage.verify_until(std::time::Instant::now()+std::time::Duration::from_secs(30)).is_err());
        drop(stage);drop(journal);fs::remove_dir_all(base).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn managed_guard_drift_preserves_committed_effect_and_external_bytes() {
        let unique = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let base = std::env::temp_dir().join(format!("openclank-guard-{}-{unique}", std::process::id()));
        let root = base.join("workspace"); let private = base.join("journal");
        fs::create_dir_all(&root).unwrap();
        let source = root.join("a.md"); let destination = root.join("b.md"); let guard_path = root.join("read-only.md");
        fs::write(&source, b"document").unwrap(); fs::write(&guard_path, b"original reference").unwrap();
        let participant = ManagedParticipant { kind:ManagedParticipantKind::Document, source:source.clone(),
            destination:Some(destination.clone()), expected_fingerprint:fingerprint_bytes(b"document").value, replacement:None };
        let guard = ManagedGuard { source:guard_path.clone(), expected_fingerprint:fingerprint_bytes(b"original reference").value };
        let prepared = preflight_managed_files(&[participant], |_, _| Ok(("root".into(), root.clone()))).unwrap();
        let guards = preflight_managed_guards(&[guard], &("root".into(), root.clone()), |_| Ok(("root".into(), root.clone()))).unwrap();
        validate_managed_guard_budget(&prepared, &guards).unwrap();
        let journal = ManagedJournal::open(&private, "operation-guard-test").unwrap();
        let mut record = stage_managed_preimages(&journal, "owner", &"c".repeat(64), "root", &root, 1, "history-action", 1, &prepared, &guards, None, None).unwrap();
        journal.stage_destination(&mut record, 0, &prepared[0].before).unwrap();
        journal.freeze_registry_plan(&mut record, serde_json::json!([{"resource_id":"stable"}])).unwrap();
        journal.preparation_checkpoint(&mut record)(serde_json::json!({"kind":"prepare_batch", "request":{"action_id":"history-action"}, "entries":[]})).unwrap();
        record.phase = ManagedJournalPhase::HistoryPrepared; journal.write(&record).unwrap();
        assert!(verify_managed_guards(&record, &("root".into(), root.clone()), &mut |_| Ok(("root".into(), root.clone()))).unwrap());
        record.phase = ManagedJournalPhase::Committing; journal.write(&record).unwrap();
        journal.publish_destination(&mut record, 0).unwrap(); journal.hold_source(&mut record, 0).unwrap();
        fs::write(&guard_path, b"external update").unwrap();
        assert!(!verify_managed_guards(&record, &("root".into(), root.clone()), &mut |_| Ok(("root".into(), root.clone()))).unwrap());
        let outcome = journal.guard_needs_review(&mut record, "committed_needs_review").unwrap();
        assert_eq!(outcome["status"], "committed_needs_review");
        assert_eq!(fs::read(&destination).unwrap(), b"document");
        assert_eq!(fs::read(&guard_path).unwrap(), b"external update");
        assert_eq!(journal.read().unwrap().unwrap().review_outcome.as_deref(), Some("committed_needs_review"));
        drop(journal); fs::remove_dir_all(base).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn managed_media_parent_waits_for_history_and_replays() {
        let unique = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let base = std::env::temp_dir().join(format!("openclank-media-{}-{unique}", std::process::id()));
        let root = base.join("workspace"); let private = base.join("journal");
        fs::create_dir_all(root.join(".media/a")).unwrap();
        fs::create_dir_all(root.join("b")).unwrap();
        let document = root.join("a.md"); let asset = root.join(".media/a/x.png");
        fs::write(&document, b"document").unwrap(); fs::write(&asset, b"asset").unwrap();
        let destination = root.join(".media/b/x.png");
        let items = vec![
            ManagedParticipant { kind:ManagedParticipantKind::Document, source:document, destination:Some(root.join("b/a.md")), expected_fingerprint:fingerprint_bytes(b"document").value, replacement:None },
            ManagedParticipant { kind:ManagedParticipantKind::Asset, source:asset, destination:Some(destination.clone()), expected_fingerprint:fingerprint_bytes(b"asset").value, replacement:None },
        ];
        let prepared = preflight_managed_files(&items, |_, _| Ok(("root".into(), root.clone()))).unwrap();
        let journal = ManagedJournal::open(&private, "operation-media-test").unwrap();
        let mut record = stage_managed_preimages(&journal, "owner", &"b".repeat(64), "root", &root, 1, "history-action", 1, &prepared, &[], None, None).unwrap();
        for (index, item) in prepared.iter().enumerate() { journal.stage_destination(&mut record, index, &item.before).unwrap(); }
        assert!(journal.ensure_media_parents(&mut record, &destination).is_err());
        assert!(!root.join(".media/b").exists());
        journal.freeze_registry_plan(&mut record, serde_json::json!([{"resource_id":"stable"}])).unwrap();
        journal.preparation_checkpoint(&mut record)(serde_json::json!({"kind":"prepare_batch", "request":{"action_id":"history-action"}, "entries":[]})).unwrap();
        record.phase = ManagedJournalPhase::HistoryPrepared; journal.write(&record).unwrap();
        record.phase = ManagedJournalPhase::Committing; journal.write(&record).unwrap();
        journal.ensure_media_parents(&mut record, &destination).unwrap();
        journal.ensure_media_parents(&mut record, &destination).unwrap();
        journal.verify_destination_stage(&record, 1).unwrap();
        assert!(root.join(".media/b").is_dir());
        assert!(!destination.exists());
        assert_eq!(record.directory_events.iter().filter(|event| event.get("kind").and_then(serde_json::Value::as_str)==Some("published")).count(), 1);
        drop(journal); fs::remove_dir_all(base).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn managed_cleanup_preserves_replaced_hold_and_resumes_exact_objects() {
        let unique = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let base = std::env::temp_dir().join(format!("openclank-cleanup-{}-{unique}", std::process::id()));
        let root = base.join("root"); fs::create_dir_all(&root).unwrap();
        let stage = root.join(".openclank-managed-test.stage"); let hold = root.join(".openclank-managed-test.hold");
        fs::write(&stage, b"stage").unwrap(); fs::write(&hold, b"original").unwrap();
        let stage_id = managed_object_identity(&File::open(&stage).unwrap()).unwrap();
        let hold_id = managed_object_identity(&File::open(&hold).unwrap()).unwrap();
        let root_id = managed_object_identity(&File::open(&root).unwrap()).unwrap();
        let journal = ManagedJournal::open(&base.join("journal"), "cleanup-test-012345").unwrap();
        let mut record = ManagedJournalRecord { version:1, operation_id:journal.operation_id.clone(), owner_id:"owner".into(),
            request_digest:"a".repeat(64), phase:ManagedJournalPhase::Prepared,
            plan:serde_json::json!({"root_path":root,"root_identity":root_id,"participants":[{"source_hold":hold,"source_identity":hold_id,"expected_fingerprint":fingerprint_bytes(b"original").value}]}),
            file_stages:vec![], directory_events:vec![], commit_progress:vec![], cleanup_progress:vec![], preparation:vec![],
            registry_plan:None, history_prepare:None, history_completion:None, review_outcome:None };
        journal.write(&record).unwrap();
        record.file_stages.push(serde_json::json!({"index":0,"path":stage,"identity":stage_id,"digest":format!("{:x}", Sha256::digest(b"stage"))})); journal.write(&record).unwrap();
        record.history_prepare = Some(serde_json::json!({})); record.phase = ManagedJournalPhase::HistoryPrepared; journal.write(&record).unwrap();
        record.phase = ManagedJournalPhase::Committing; journal.write(&record).unwrap();
        record.commit_progress.push(serde_json::json!({"kind":"source_held","index":0,"identity":hold_id})); journal.write(&record).unwrap();
        record.phase = ManagedJournalPhase::LiveCommitted; journal.write(&record).unwrap();
        record.phase = ManagedJournalPhase::Complete; journal.write(&record).unwrap();
        let saved = root.join("saved-original"); fs::rename(&hold, &saved).unwrap(); fs::write(&hold, b"unrelated").unwrap();
        assert!(!journal.cleanup_owned_objects(&mut record).unwrap());
        assert_eq!(fs::read(&hold).unwrap(), b"unrelated"); assert!(!stage.exists());
        fs::remove_file(&hold).unwrap(); fs::rename(&saved, &hold).unwrap();
        assert!(journal.cleanup_owned_objects(&mut record).unwrap());
        assert!(journal.cleanup_owned_objects(&mut record).unwrap()); assert!(!hold.exists());
        drop(journal); fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn terminal_journal_retries_recovery_settlement_after_a_lost_reply() {
        struct Settlement {
            journal_path: PathBuf,
            calls: Mutex<usize>,
        }
        impl MutationCaptureHook for Settlement {
            fn prepare(&self, _: &str, _: &[CaptureTarget]) -> Result<Box<dyn MutationCaptureTicket>, String> { unreachable!() }
            fn managed_action_id(&self, _: &str) -> Result<String, String> { Ok("action".into()) }
            fn settle_recovery(&self, action: &str) -> Result<(), String> {
                assert_eq!(action, "action");
                let persisted: ManagedJournalRecord = serde_json::from_slice(&fs::read(&self.journal_path).unwrap()).unwrap();
                assert_eq!(persisted.phase, ManagedJournalPhase::Complete);
                let mut calls = self.calls.lock().unwrap();
                *calls += 1;
                if *calls == 1 { Err("lost reply".into()) } else { Ok(()) }
            }
        }
        let root = tempfile::tempdir().unwrap();
        let id = "settlement-test-0001";
        let hook = Arc::new(Settlement { journal_path: root.path().join(format!("{id}.json")), calls: Mutex::new(0) });
        let journal = ManagedJournal::open(root.path(), id).unwrap().with_capture(Some(hook.clone()));
        let mut record = ManagedJournalRecord {
            version:1, operation_id:id.into(), owner_id:"owner".into(), request_digest:"a".repeat(64),
            phase:ManagedJournalPhase::Prepared, plan:serde_json::json!({"history_action_id":"action"}),
            file_stages:vec![], directory_events:vec![], commit_progress:vec![], cleanup_progress:vec![],
            preparation:vec![], registry_plan:None, history_prepare:None, history_completion:None, review_outcome:None,
        };
        journal.write(&record).unwrap();
        record.history_prepare = Some(serde_json::json!({"request":{"action_id":"action","coverage":{"metadata":{"required_recovery":true}}}}));
        for phase in [ManagedJournalPhase::HistoryPrepared, ManagedJournalPhase::Committing, ManagedJournalPhase::LiveCommitted] {
            record.phase = phase;
            journal.write(&record).unwrap();
        }
        assert_eq!(*hook.calls.lock().unwrap(), 0);
        record.phase = ManagedJournalPhase::Complete;
        journal.write(&record).unwrap();
        assert_eq!(*hook.calls.lock().unwrap(), 1);
        drop(journal);
        let reopened = ManagedJournal::open_existing(root.path(), id, false).unwrap().unwrap().with_capture(Some(hook.clone()));
        assert_eq!(reopened.read().unwrap().unwrap().phase, ManagedJournalPhase::Complete);
        assert_eq!(*hook.calls.lock().unwrap(), 2);
    }

    #[test]
    fn managed_journal_reopens_exact_record() {
        let unique = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("openclank-managed-journal-{}-{unique}", std::process::id()));
        let id = "operation-0123456789";
        let record = ManagedJournalRecord { version:1, operation_id:id.into(), owner_id:"owner-a".into(),
            request_digest:"a".repeat(64), phase:ManagedJournalPhase::Prepared,
            plan:serde_json::json!({"action":"exact-action", "staged":["stable-stage"]}), file_stages:vec![], directory_events:vec![], commit_progress:vec![], cleanup_progress:vec![], preparation:vec![], registry_plan:None, history_prepare:None, history_completion:None, review_outcome:None };
        {
            let journal = ManagedJournal::open(&root, id).unwrap();
            assert!(journal.read().unwrap().is_none());
            let digest = journal.stage("before-0", b"exact-before").unwrap();
            assert_eq!(journal.stage("before-0", b"exact-before").unwrap(), digest);
            assert!(journal.stage("before-0", b"changed").is_err());
            journal.write(&record).unwrap();
            let mut working = record.clone();
            journal.freeze_registry_plan(&mut working, serde_json::json!([{"resource_id":"file:stable"}])).unwrap();
            {
                let mut checkpoint = journal.preparation_checkpoint(&mut working);
                checkpoint(serde_json::json!({"kind":"resource", "index":0, "resource_id":"file:stable"})).unwrap();
                checkpoint(serde_json::json!({"kind":"prepare_batch", "request":{"action_id":"exact-action"}, "entries":[]})).unwrap();
            }
            let mut resumed = journal.read().unwrap().unwrap();
            assert!(resumed.history_prepare.is_some());
            journal.freeze_registry_plan(&mut resumed, serde_json::json!([{"resource_id":"file:stable"}])).unwrap();
            assert!(journal.freeze_registry_plan(&mut resumed, serde_json::json!([{"resource_id":"file:changed"}])).is_err());
            working.phase = ManagedJournalPhase::HistoryPrepared;
            journal.write(&working).unwrap();
        }
        {
            let journal = ManagedJournal::open(&root, id).unwrap();
            let recovered = journal.read().unwrap().unwrap();
            assert_eq!(recovered.owner_id, "owner-a");
            assert_eq!(recovered.phase, ManagedJournalPhase::HistoryPrepared);
            assert_eq!(recovered.preparation.len(), 1);
            assert!(recovered.history_prepare.is_some());
            assert_eq!(recovered.plan, record.plan);
            let mut conflict = recovered.clone(); conflict.request_digest = "b".repeat(64);
            assert!(journal.write(&conflict).is_err());
            assert_eq!(journal.read_stage("before-0", &format!("{:x}", Sha256::digest(b"exact-before"))).unwrap(), b"exact-before");
            assert!(ManagedJournal::open(&root, "../invalid-operation").is_err());
        }
        let workspace = root.join("workspace"); fs::create_dir(&workspace).unwrap();
        let source = workspace.join("document.md"); fs::write(&source, b"original").unwrap();
        let item = ManagedParticipant { kind:ManagedParticipantKind::Document, source:source.clone(),
            destination:Some(workspace.join("moved.md")), expected_fingerprint:fingerprint_bytes(b"original").value,
            replacement:Some(b"rewritten".to_vec()) };
        let prepared = preflight_managed_files(&[item], |_, _| Ok(("root-a".into(), workspace.clone()))).unwrap();
        let journal = ManagedJournal::open(&root, "operation-9876543210").unwrap();
        let mut saved = stage_managed_preimages(&journal, "owner-a", &"b".repeat(64), "root-a", &workspace, 4,
            "history-action", 123, &prepared, &[], None, None).unwrap();
        assert_eq!(saved.phase, ManagedJournalPhase::Prepared);
        assert_eq!(journal.read_stage("before-0", &format!("{:x}", Sha256::digest(b"original"))).unwrap(), b"original");
        journal.stage_destination(&mut saved, 0, b"rewritten").unwrap();
        journal.verify_destination_stage(&saved, 0).unwrap();
        assert_eq!(fs::read(&source).unwrap(), b"original");
        #[cfg(target_os = "linux")]
        {
            journal.freeze_registry_plan(&mut saved, serde_json::json!([{"resource_id":"file:stable"}])).unwrap();
            {
                let mut checkpoint = journal.preparation_checkpoint(&mut saved);
                checkpoint(serde_json::json!({"kind":"prepare_batch", "request":{"action_id":"history-action"}, "entries":[]})).unwrap();
            }
            saved.phase = ManagedJournalPhase::HistoryPrepared; journal.write(&saved).unwrap();
            saved.phase = ManagedJournalPhase::Committing; journal.write(&saved).unwrap();
            journal.publish_destination(&mut saved, 0).unwrap();
            journal.publish_destination(&mut saved, 0).unwrap();
            assert_eq!(fs::read(workspace.join("moved.md")).unwrap(), b"rewritten");
            journal.hold_source(&mut saved, 0).unwrap();
            journal.hold_source(&mut saved, 0).unwrap();
            assert!(!source.exists());
            assert_eq!(saved.commit_progress.len(), 2);
        }
        drop(journal);
        fs::remove_dir_all(root).unwrap();
    }
}
