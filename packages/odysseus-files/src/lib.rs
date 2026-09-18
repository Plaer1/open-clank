//! The stable boundary for the Odysseus filesystem service.
//!
//! This crate intentionally contains contracts and authorization decisions only.
//! The filesystem engine, transport, persistence, and UI remain adapters around
//! these types until their respective slices are admitted.

use serde::{Deserialize, Serialize};
use serde_json::Value;
mod engine;
pub mod history_capture;
pub use history_capture::HistoryServiceHook;
#[cfg(all(feature = "tonic-transport", target_os = "macos"))]
pub mod grpc_transport;
#[cfg(unix)]
mod local_ipc;
#[allow(dead_code)]
mod quicklook_protocol;
mod service;
pub mod thumbnail;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::{Component, Path, PathBuf};
use thiserror::Error;

pub use engine::{
    CaptureAfter, CaptureStatus, CaptureTarget, CopyOutcome, DirectoryEntry, DirectoryPage, DirectorySort,
    EditOutcome, EngineConfig, EngineError, FileEngine, FileKind, FileReadPage, Fingerprint,
    MutationCaptureHook, MutationCaptureTicket, ReplaceOutcome, SearchOptions, SearchResult,
    TextEncoding, TextPreview, TextSnapshot, TrashEntry,
};
#[cfg(unix)]
pub use local_ipc::{FramedUnixStream, LocalIpcServer};
pub use service::{AuthorizedWatch, FileService, ServiceIdentity};
pub use thumbnail::{
    CancellationToken as ThumbnailCancellationToken, PolicyGeneration, ThumbnailBroker,
    ThumbnailBrokerConfig, ThumbnailError, ThumbnailLimits, ThumbnailPng, ThumbnailRequest,
    ThumbnailStats,
};

pub const PROTOCOL_NAME: &str = "open-clank.files";
pub const PROTOCOL_VERSION: ProtocolVersion = ProtocolVersion { major: 1, minor: 0 };
pub const LEGACY_RESULT_CONTRACT: &str = "open-clank.file-result/v1";

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct ProtocolVersion {
    pub major: u16,
    pub minor: u16,
}

impl ProtocolVersion {
    pub const fn new(major: u16, minor: u16) -> Self {
        Self { major, minor }
    }

    pub fn is_compatible_with(self, server: Self) -> bool {
        self.major == server.major && self.minor <= server.minor
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TrustLane {
    App,
    Agent,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Capability {
    Read,
    Write,
    Execute,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct RequestContext {
    owner_id: String,
    principal_id: String,
    lane: TrustLane,
    request_id: String,
    session_id: String,
    root_operation_id: String,
    deadline_unix_ms: Option<u64>,
    approved_root_ids: BTreeSet<String>,
    effective_capabilities: BTreeSet<Capability>,
    active_agent_folder_id: Option<String>,
    app_scope: AppScope,
    operation: Operation,
    target: Target,
    audit_id: String,
}

impl RequestContext {
    fn new(
        owner_id: impl Into<String>,
        principal_id: impl Into<String>,
        lane: TrustLane,
        request_id: impl Into<String>,
        session_id: impl Into<String>,
        root_operation_id: impl Into<String>,
        operation: Operation,
        target: Target,
        audit_id: impl Into<String>,
    ) -> Self {
        Self {
            owner_id: owner_id.into(),
            principal_id: principal_id.into(),
            lane,
            request_id: request_id.into(),
            session_id: session_id.into(),
            root_operation_id: root_operation_id.into(),
            deadline_unix_ms: None,
            approved_root_ids: BTreeSet::new(),
            effective_capabilities: BTreeSet::new(),
            active_agent_folder_id: None,
            app_scope: AppScope::host(),
            operation,
            target,
            audit_id: audit_id.into(),
        }
    }

    /// The app lane is assigned by the authenticated server boundary, never by
    /// a client request body.
    pub fn app(
        owner_id: impl Into<String>,
        principal_id: impl Into<String>,
        request_id: impl Into<String>,
        session_id: impl Into<String>,
        root_operation_id: impl Into<String>,
        operation: Operation,
        target: Target,
        audit_id: impl Into<String>,
    ) -> Self {
        Self::new(
            owner_id,
            principal_id,
            TrustLane::App,
            request_id,
            session_id,
            root_operation_id,
            operation,
            target,
            audit_id,
        )
    }

    /// The agent lane is assigned by the agent-session authenticator. It is
    /// still narrowed by approved roots and the active workspace folder.
    pub fn agent(
        owner_id: impl Into<String>,
        principal_id: impl Into<String>,
        request_id: impl Into<String>,
        session_id: impl Into<String>,
        root_operation_id: impl Into<String>,
        operation: Operation,
        target: Target,
        audit_id: impl Into<String>,
    ) -> Self {
        Self::new(
            owner_id,
            principal_id,
            TrustLane::Agent,
            request_id,
            session_id,
            root_operation_id,
            operation,
            target,
            audit_id,
        )
    }

    pub fn with_scope(
        mut self,
        approved_root_ids: impl IntoIterator<Item = String>,
        capabilities: impl IntoIterator<Item = Capability>,
        active_agent_folder_id: Option<String>,
    ) -> Self {
        self.approved_root_ids = approved_root_ids.into_iter().collect();
        self.effective_capabilities = capabilities.into_iter().collect();
        self.active_agent_folder_id = active_agent_folder_id;
        self
    }

    /// Attach the server-derived application visibility scope. This is never
    /// accepted from the client request body.
    pub fn with_app_scope(mut self, app_scope: AppScope) -> Self {
        self.app_scope = app_scope;
        self
    }

    pub fn with_deadline(mut self, deadline_unix_ms: Option<u64>) -> Self {
        self.deadline_unix_ms = deadline_unix_ms;
        self
    }

    pub fn owner_id(&self) -> &str {
        &self.owner_id
    }
    pub fn principal_id(&self) -> &str {
        &self.principal_id
    }
    pub fn lane(&self) -> TrustLane {
        self.lane
    }
    pub fn request_id(&self) -> &str {
        &self.request_id
    }
    pub fn session_id(&self) -> &str {
        &self.session_id
    }
    pub fn root_operation_id(&self) -> &str {
        &self.root_operation_id
    }
    pub fn deadline_unix_ms(&self) -> Option<u64> {
        self.deadline_unix_ms
    }
    pub fn approved_root_ids(&self) -> &BTreeSet<String> {
        &self.approved_root_ids
    }
    pub fn effective_capabilities(&self) -> &BTreeSet<Capability> {
        &self.effective_capabilities
    }
    pub fn active_agent_folder_id(&self) -> Option<&str> {
        self.active_agent_folder_id.as_deref()
    }
    pub fn app_scope(&self) -> &AppScope {
        &self.app_scope
    }
    pub fn operation(&self) -> Operation {
        self.operation
    }
    pub fn target(&self) -> &Target {
        &self.target
    }
    pub fn audit_id(&self) -> &str {
        &self.audit_id
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ClientRequest {
    pub protocol: ProtocolVersion,
    pub request_id: String,
    pub session_id: String,
    pub root_operation_id: String,
    pub operation: Operation,
    pub target: Target,
    pub payload: Value,
}

impl ClientRequest {
    pub fn new(
        operation: Operation,
        target: Target,
        request_id: impl Into<String>,
        session_id: impl Into<String>,
        root_operation_id: impl Into<String>,
        payload: Value,
    ) -> Self {
        Self {
            protocol: PROTOCOL_VERSION,
            request_id: request_id.into(),
            session_id: session_id.into(),
            root_operation_id: root_operation_id.into(),
            operation,
            target,
            payload,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct RequestEnvelope {
    pub protocol: ProtocolVersion,
    pub context: RequestContext,
    pub operation: Operation,
    pub target: Target,
    pub payload: Value,
}

impl RequestContext {
    pub fn bind(self, request: ClientRequest) -> Result<RequestEnvelope, ProtocolError> {
        if !request.protocol.is_compatible_with(PROTOCOL_VERSION) {
            return Err(ProtocolError::new(
                ProtocolErrorCode::ProtocolMismatch,
                "client protocol is not compatible with the server",
                Some(self.audit_id.clone()),
            ));
        }
        if self.request_id != request.request_id
            || self.session_id != request.session_id
            || self.root_operation_id != request.root_operation_id
            || self.operation != request.operation
            || self.target != request.target
        {
            return Err(ProtocolError::new(
                ProtocolErrorCode::MalformedRequest,
                "authenticated context does not match the request envelope",
                Some(self.audit_id.clone()),
            ));
        }
        Ok(RequestEnvelope {
            protocol: PROTOCOL_VERSION,
            context: self,
            operation: request.operation,
            target: request.target,
            payload: request.payload,
        })
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Health,
    Capabilities,
    ValidateRoot,
    ListVolumes,
    ListDirectory,
    Stat,
    Lstat,
    Canonicalize,
    Probe,
    ReadRange,
    ReadLines,
    ReadTextPreview,
    OpenHandle,
    Thumbnail,
    Create,
    StageBegin,
    StageChunk,
    StageFinish,
    StageAbort,
    Replace,
    Patch,
    Append,
    Rename,
    Move,
    Copy,
    Mkdir,
    Trash,
    Restore,
    FilenameSearch,
    ContentSearch,
    WatchSubscribe,
    WatchUnsubscribe,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case", tag = "kind", content = "value")]
pub enum Target {
    Path(String),
    Handle(ResourceHandle),
}

impl Target {
    pub fn path(path: impl Into<String>) -> Self {
        Self::Path(path.into())
    }
    pub fn handle(handle: ResourceHandle) -> Self {
        Self::Handle(handle)
    }
}

#[derive(Clone, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
pub struct ResourceHandle {
    /// Unguessable service-local capability. The descriptive fields are bound
    /// into the table entry and cannot authorize anything without this token.
    pub token: String,
    pub root_id: String,
    pub relative_components: Vec<String>,
    pub generation: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TransportKind {
    AuthenticatedLocalIpc,
    FramedStdio,
    SameOriginStreamingHttp,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct TransportSelection {
    pub primary: TransportKind,
    pub fallback: Option<TransportKind>,
    pub rationale: String,
    pub measured: bool,
}

impl TransportSelection {
    pub fn pending_local_ipc() -> Self {
        Self {
            primary: TransportKind::AuthenticatedLocalIpc,
            fallback: Some(TransportKind::FramedStdio),
            rationale:
                "local authenticated IPC is the candidate; admission requires the S01 bakeoff"
                    .into(),
            measured: false,
        }
    }
}

pub const DEFAULT_MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum FrameError {
    #[error("frame exceeds the configured maximum")]
    TooLarge,
    #[error("frame length is malformed")]
    Malformed,
}

/// Incremental length-prefixed framing used by both local transport candidates.
/// The decoder retains only an explicitly bounded frame buffer and can be fed
/// arbitrary partial socket reads.
#[derive(Debug)]
pub struct FrameDecoder {
    max_frame_bytes: usize,
    buffer: Vec<u8>,
}

impl FrameDecoder {
    pub fn new(max_frame_bytes: usize) -> Self {
        Self {
            max_frame_bytes,
            buffer: Vec::new(),
        }
    }

    pub fn feed(&mut self, bytes: &[u8]) -> Result<Vec<Vec<u8>>, FrameError> {
        self.buffer.extend_from_slice(bytes);
        let mut frames = Vec::new();
        loop {
            if self.buffer.len() < 4 {
                break;
            }
            let length = u32::from_be_bytes(
                self.buffer[..4]
                    .try_into()
                    .map_err(|_| FrameError::Malformed)?,
            ) as usize;
            if length > self.max_frame_bytes {
                self.buffer.clear();
                return Err(FrameError::TooLarge);
            }
            if self.buffer.len() < 4 + length {
                break;
            }
            frames.push(self.buffer[4..4 + length].to_vec());
            self.buffer.drain(..4 + length);
        }
        Ok(frames)
    }

    pub fn buffered_bytes(&self) -> usize {
        self.buffer.len()
    }
}

pub fn encode_frame(payload: &[u8], max_frame_bytes: usize) -> Result<Vec<u8>, FrameError> {
    if payload.len() > max_frame_bytes || payload.len() > u32::MAX as usize {
        return Err(FrameError::TooLarge);
    }
    let mut frame = Vec::with_capacity(4 + payload.len());
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(payload);
    Ok(frame)
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct CapabilityAdvertisement {
    pub protocol: ProtocolVersion,
    pub operations: BTreeSet<Operation>,
    pub transports: BTreeSet<TransportKind>,
    pub max_page_size: u32,
    pub supports_cancel: bool,
    pub supports_backpressure: bool,
    pub supports_watch_resume: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CancellationState {
    Queued,
    Running,
    Completed,
    Cancelled,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct StreamFrame<T> {
    pub request_id: String,
    pub sequence: u64,
    pub done: bool,
    pub state: CancellationState,
    pub payload: Option<T>,
    pub work: WorkCost,
    pub error: Option<ProtocolError>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FileEventKind {
    Created,
    Modified,
    Deleted,
    Renamed,
    Overflow,
    RootUnavailable,
    Gap,
    RescanRequired,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct FileEvent {
    pub root_id: String,
    pub generation: u64,
    pub sequence: u64,
    pub kind: FileEventKind,
    pub path: Option<String>,
    pub old_path: Option<String>,
    pub fingerprint: Option<String>,
    pub observed_unix_ms: u64,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SymlinkPolicy {
    Deny,
    AllowWithinRoot,
    AllowWithRevalidation,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PathFlavor {
    Posix,
    Windows,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PathObjectKind {
    RegularFile,
    Directory,
    Symlink,
    Junction,
    ReparsePoint,
    Hardlink,
    SparseFile,
    Device,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CrossVolumeDisposition {
    AtomicRename,
    CopyThenVerify,
    Deny,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct PathProbe {
    pub input: String,
    pub flavor: PathFlavor,
    pub kind: PathObjectKind,
    pub identity: Option<PlatformIdentity>,
    pub symlink_hops: u32,
    pub mode: Option<u32>,
    pub size: Option<u64>,
    pub cross_volume: Option<CrossVolumeDisposition>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct PathResolution {
    pub input: String,
    pub canonical: Option<String>,
    pub symlink_hops: u32,
    pub escaped_root: bool,
    pub policy: SymlinkPolicy,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProtocolErrorCode {
    MalformedRequest,
    ProtocolMismatch,
    Unauthorized,
    Denied,
    CrossOwner,
    InvalidPath,
    RootUnavailable,
    StaleCursor,
    StaleHandle,
    Conflict,
    DeadlineExceeded,
    Cancelled,
    Backpressure,
    Crashed,
    Unsupported,
    PartialStream,
}

#[derive(Clone, Debug, Deserialize, Eq, Error, PartialEq, Serialize)]
#[error("{code:?}: {message}")]
pub struct ProtocolError {
    pub code: ProtocolErrorCode,
    pub message: String,
    pub audit_id: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub details: BTreeMap<String, String>,
}

impl ProtocolError {
    pub fn new(
        code: ProtocolErrorCode,
        message: impl Into<String>,
        audit_id: Option<String>,
    ) -> Self {
        Self {
            code,
            message: message.into(),
            audit_id,
            details: BTreeMap::new(),
        }
    }

    /// Denials deliberately carry no target path, root path, or directory
    /// listing. The audit id is enough to correlate a safe server-side record.
    pub fn denied(audit_id: impl Into<String>) -> Self {
        Self::new(
            ProtocolErrorCode::Denied,
            "operation denied",
            Some(audit_id.into()),
        )
    }
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct WorkCost {
    pub entries_visited: u64,
    pub bytes_read: u64,
    pub hashes_computed: u64,
    pub cache_hits: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Cursor {
    pub token: String,
    pub generation: u64,
    pub position: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct PageInfo {
    pub cursor: Option<Cursor>,
    pub next_cursor: Option<Cursor>,
    pub returned: u64,
    pub total: Option<u64>,
    pub has_more: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Truncation {
    pub truncated: bool,
    pub reason: Option<String>,
    pub limit: Option<u64>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct FileResult {
    pub protocol: ProtocolVersion,
    pub operation: Operation,
    pub path: String,
    pub kind: String,
    pub range: Option<RangeSpec>,
    pub page: Option<PageInfo>,
    pub bytes_considered: Option<u64>,
    pub lines_considered: Option<u64>,
    pub truncation: Option<Truncation>,
    pub encoding: Option<String>,
    pub newline: Option<String>,
    pub media_type: Option<String>,
    pub fingerprint: Option<String>,
    pub search_mode: Option<String>,
    pub items: Vec<Value>,
    pub diagnostics: Vec<String>,
    pub work: WorkCost,
    pub audit_id: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct RangeSpec {
    pub unit: String,
    pub start: u64,
    pub end: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct LegacyFileResult {
    pub contract: String,
    pub operation: Operation,
    pub path: String,
    pub kind: String,
    pub range: Option<RangeSpec>,
    pub page: Option<PageInfo>,
    pub bytes_considered: Option<u64>,
    pub lines_considered: Option<u64>,
    pub truncation_reason: Option<String>,
    pub encoding: Option<String>,
    pub newline: Option<String>,
    pub media_type: Option<String>,
    pub fingerprint: Option<String>,
    pub search_mode: Option<String>,
    pub items: Vec<Value>,
    pub diagnostics: Vec<String>,
}

impl FileResult {
    /// Compatibility projection for current Copal/agent consumers. The new
    /// Rust contract stays authoritative; this is a read-only shape adapter.
    pub fn to_legacy(&self) -> LegacyFileResult {
        LegacyFileResult {
            contract: LEGACY_RESULT_CONTRACT.into(),
            operation: self.operation,
            path: self.path.clone(),
            kind: self.kind.clone(),
            range: self.range.clone(),
            page: self.page.clone(),
            bytes_considered: self.bytes_considered,
            lines_considered: self.lines_considered,
            truncation_reason: self
                .truncation
                .as_ref()
                .and_then(|value| value.reason.clone()),
            encoding: self.encoding.clone(),
            newline: self.newline.clone(),
            media_type: self.media_type.clone(),
            fingerprint: self.fingerprint.clone(),
            search_mode: self.search_mode.clone(),
            items: self.items.clone(),
            diagnostics: self.diagnostics.clone(),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RootKind {
    ExactFile,
    RecursiveDirectory,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RootAvailability {
    Available,
    Missing,
    PermissionDenied,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct PlatformIdentity {
    pub volume_id: Option<String>,
    pub file_id: Option<String>,
    pub device: Option<u64>,
    pub inode: Option<u64>,
    pub case_sensitive: Option<bool>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct RootRecord {
    pub id: String,
    pub owner_id: String,
    pub kind: RootKind,
    pub canonical_path: String,
    pub display_path: String,
    pub enabled: bool,
    pub capabilities: BTreeSet<Capability>,
    pub platform_identity: PlatformIdentity,
    pub last_validated_unix_ms: Option<u64>,
    pub availability: RootAvailability,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ActiveAgentFolder {
    pub id: String,
    pub root_id: String,
    pub canonical_path: String,
    pub capabilities: BTreeSet<Capability>,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct AgentScope {
    pub approved_root_ids: BTreeSet<String>,
    /// Per-root capability ceilings minted by the server. An empty map is
    /// retained for legacy in-process callers and means the physical root
    /// capabilities apply unchanged; a non-empty map is authoritative and a
    /// missing root entry is denied.
    #[serde(default)]
    pub root_capabilities: BTreeMap<String, BTreeSet<Capability>>,
    pub active_folder: Option<ActiveAgentFolder>,
}

impl AgentScope {
    pub fn new(approved_root_ids: impl IntoIterator<Item = String>) -> Self {
        Self {
            approved_root_ids: approved_root_ids.into_iter().collect(),
            root_capabilities: BTreeMap::new(),
            active_folder: None,
        }
    }

    pub fn with_root_capabilities(
        mut self,
        root_capabilities: BTreeMap<String, BTreeSet<Capability>>,
    ) -> Self {
        self.root_capabilities = root_capabilities;
        self
    }

    pub fn with_active_folder(mut self, folder: ActiveAgentFolder) -> Self {
        self.active_folder = Some(folder);
        self
    }
}

/// Server-derived visibility ceiling for user-initiated app operations.
///
/// `host = true` is reserved for an authenticated administrator app context.
/// A non-admin context must set `host = false` and provide the exact root IDs
/// assigned by an administrator. Empty IDs are an empty scope, never a fallback.
#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct AppScope {
    #[serde(default)]
    pub host: bool,
    #[serde(default)]
    pub visible_root_ids: BTreeSet<String>,
    #[serde(default)]
    pub capabilities: BTreeSet<Capability>,
    /// Capability ceiling for each administrator-assigned visible root.
    ///
    /// The aggregate `capabilities` field remains as a cheap admission hint and
    /// for wire compatibility, but it cannot authorize a root by itself. This
    /// map prevents a write grant on one root from widening a read-only root.
    #[serde(default)]
    pub root_capabilities: BTreeMap<String, BTreeSet<Capability>>,
    #[serde(default)]
    pub generation: u64,
    #[serde(default)]
    pub active_folder: Option<ActiveAgentFolder>,
}

impl AppScope {
    pub fn host() -> Self {
        Self {
            host: true,
            ..Self::default()
        }
    }

    pub fn assigned(
        visible_root_ids: impl IntoIterator<Item = String>,
        capabilities: impl IntoIterator<Item = Capability>,
        generation: u64,
    ) -> Self {
        let visible_root_ids: BTreeSet<String> = visible_root_ids.into_iter().collect();
        let capabilities: BTreeSet<Capability> = capabilities.into_iter().collect();
        let root_capabilities = visible_root_ids
            .iter()
            .map(|root_id| (root_id.clone(), capabilities.clone()))
            .collect();
        Self {
            host: false,
            visible_root_ids,
            capabilities,
            root_capabilities,
            generation,
            active_folder: None,
        }
    }

    pub fn with_active_folder(mut self, folder: ActiveAgentFolder) -> Self {
        self.active_folder = Some(folder);
        self
    }
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct RootRegistry {
    roots: BTreeMap<String, RootRecord>,
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum RegistryError {
    #[error("root id is already registered")]
    DuplicateId,
    #[error("root id is not registered")]
    UnknownRoot,
    #[error("root path must be absolute and canonical")]
    NonCanonicalPath,
    #[error("a recursive root may not be the filesystem root")]
    FilesystemRoot,
    #[error("root is disabled or unavailable")]
    Unavailable,
    #[error("root belongs to another owner")]
    CrossOwner,
    #[error("target is outside the approved root")]
    OutsideRoot,
    #[error("requested capability is not granted")]
    CapabilityDenied,
    #[error("active folder is not a narrowing of the approved root")]
    InvalidActiveFolder,
}

impl RootRegistry {
    pub fn register(&mut self, record: RootRecord) -> Result<(), RegistryError> {
        validate_root_record(&record)?;
        if self.roots.contains_key(&record.id) {
            return Err(RegistryError::DuplicateId);
        }
        self.roots.insert(record.id.clone(), record);
        Ok(())
    }

    pub fn get(&self, root_id: &str) -> Option<&RootRecord> {
        self.roots.get(root_id)
    }

    pub fn disable(&mut self, root_id: &str) -> Result<(), RegistryError> {
        let root = self
            .roots
            .get_mut(root_id)
            .ok_or(RegistryError::UnknownRoot)?;
        root.enabled = false;
        Ok(())
    }

    pub fn authorize(
        &self,
        owner_id: &str,
        target_path: &Path,
        requested: &BTreeSet<Capability>,
        scope: &AgentScope,
    ) -> Result<&RootRecord, RegistryError> {
        if scope.approved_root_ids.is_empty() {
            return Err(RegistryError::OutsideRoot);
        }
        for root_id in &scope.approved_root_ids {
            let Some(root) = self.roots.get(root_id) else {
                continue;
            };
            if root.owner_id != owner_id {
                continue;
            }
            if !root.enabled || root.availability != RootAvailability::Available {
                continue;
            }
            if !requested.is_subset(&root.capabilities) {
                continue;
            }
            if !scope.root_capabilities.is_empty()
                && !scope
                    .root_capabilities
                    .get(root_id)
                    .map(|capabilities| requested.is_subset(capabilities))
                    .unwrap_or(false)
            {
                continue;
            }
            let root_path = lexical_normalize(Path::new(&root.canonical_path));
            let target = lexical_normalize(target_path);
            let inside = match root.kind {
                RootKind::ExactFile => target == root_path,
                RootKind::RecursiveDirectory => component_contains(&root_path, &target),
            };
            if !inside {
                continue;
            }
            if let Some(folder) = &scope.active_folder {
                if folder.root_id != root.id
                    || !component_contains(&root_path, Path::new(&folder.canonical_path))
                    || !component_contains(Path::new(&folder.canonical_path), &target)
                    || !requested.is_subset(&folder.capabilities)
                {
                    continue;
                }
            }
            return Ok(root);
        }
        Err(RegistryError::OutsideRoot)
    }

    /// Authorize a non-admin app scope using server-minted visibility IDs.
    /// Unlike the agent registry lookup, these roots may be administered by a
    /// different owner; possession of the IDs is the already-authenticated
    /// assignment projection. The caller's capability ceiling is checked before
    /// the physical root's capabilities.
    pub fn authorize_app(
        &self,
        scope: &AppScope,
        target_path: &Path,
        requested: &BTreeSet<Capability>,
    ) -> Result<&RootRecord, RegistryError> {
        if scope.visible_root_ids.is_empty() || !requested.is_subset(&scope.capabilities) {
            return Err(RegistryError::OutsideRoot);
        }
        let target = lexical_normalize(target_path);
        for root_id in &scope.visible_root_ids {
            let Some(assigned_capabilities) = scope.root_capabilities.get(root_id) else {
                continue;
            };
            if !requested.is_subset(assigned_capabilities) {
                continue;
            }
            let Some(root) = self.roots.get(root_id) else {
                continue;
            };
            if !root.enabled || root.availability != RootAvailability::Available {
                continue;
            }
            if !requested.is_subset(&root.capabilities) {
                continue;
            }
            let root_path = lexical_normalize(Path::new(&root.canonical_path));
            let inside = match root.kind {
                RootKind::ExactFile => target == root_path,
                RootKind::RecursiveDirectory => component_contains(&root_path, &target),
            };
            if inside {
                return Ok(root);
            }
        }
        Err(RegistryError::OutsideRoot)
    }

    pub fn authorize_pair(
        &self,
        owner_id: &str,
        source: &Path,
        destination: &Path,
        requested: &BTreeSet<Capability>,
        scope: &AgentScope,
    ) -> Result<(&RootRecord, &RootRecord), RegistryError> {
        let source_root = self.authorize(owner_id, source, requested, scope)?;
        let destination_root = self.authorize(owner_id, destination, requested, scope)?;
        Ok((source_root, destination_root))
    }

    pub fn validate_active_folder(
        &self,
        owner_id: &str,
        folder: &ActiveAgentFolder,
        scope: &AgentScope,
    ) -> Result<(), RegistryError> {
        if !scope.approved_root_ids.contains(&folder.root_id) {
            return Err(RegistryError::InvalidActiveFolder);
        }
        let root = self
            .roots
            .get(&folder.root_id)
            .ok_or(RegistryError::UnknownRoot)?;
        if root.owner_id != owner_id
            || !root.enabled
            || root.availability != RootAvailability::Available
        {
            return Err(RegistryError::InvalidActiveFolder);
        }
        if root.kind != RootKind::RecursiveDirectory
            || !component_contains(
                Path::new(&root.canonical_path),
                Path::new(&folder.canonical_path),
            )
            || !folder.capabilities.is_subset(&root.capabilities)
        {
            return Err(RegistryError::InvalidActiveFolder);
        }
        if !scope.root_capabilities.is_empty()
            && !scope
                .root_capabilities
                .get(&folder.root_id)
                .map(|capabilities| folder.capabilities.is_subset(capabilities))
                .unwrap_or(false)
        {
            return Err(RegistryError::InvalidActiveFolder);
        }
        Ok(())
    }
}

fn validate_root_record(record: &RootRecord) -> Result<(), RegistryError> {
    let path = Path::new(&record.canonical_path);
    if !path.is_absolute() || lexical_normalize(path) != path {
        return Err(RegistryError::NonCanonicalPath);
    }
    Ok(())
}

/// Lexical normalization is intentionally component based. It prevents
/// `/approved` from authorizing `/approved-sibling`; the engine must still
/// perform OS-level canonicalization and revalidation before I/O.
pub fn lexical_normalize(path: &Path) -> PathBuf {
    let mut output = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => output.push(prefix.as_os_str()),
            Component::RootDir => output.push(Path::new(std::path::MAIN_SEPARATOR_STR)),
            Component::CurDir => {}
            Component::ParentDir => {
                output.pop();
            }
            Component::Normal(value) => output.push(value),
        }
    }
    output
}

pub fn component_contains(root: &Path, target: &Path) -> bool {
    lexical_normalize(target).starts_with(lexical_normalize(root))
}

/// Component containment fixture logic for paths whose syntax differs from the
/// host running the contract tests. The live engine must use native OS APIs and
/// revalidate the resulting identity before every mutation.
pub fn portable_component_contains(
    root: &str,
    target: &str,
    flavor: PathFlavor,
    case_sensitive: bool,
) -> bool {
    let Some(root) = portable_components(root, flavor) else {
        return false;
    };
    let Some(target) = portable_components(target, flavor) else {
        return false;
    };
    if root.len() > target.len() {
        return false;
    }
    root.iter().zip(&target).all(|(left, right)| {
        if case_sensitive {
            left == right
        } else {
            left.eq_ignore_ascii_case(right)
        }
    })
}

fn portable_components(value: &str, flavor: PathFlavor) -> Option<Vec<String>> {
    let separators = match flavor {
        PathFlavor::Posix => "/",
        PathFlavor::Windows => r"/\",
    };
    let mut parts: Vec<String> = value
        .split(|character| separators.contains(character))
        .filter(|part| !part.is_empty() && *part != ".")
        .map(str::to_owned)
        .collect();
    let prefix = match flavor {
        PathFlavor::Posix if value.starts_with('/') => vec!["/".to_string()],
        PathFlavor::Posix => return None,
        PathFlavor::Windows => {
            if value.starts_with(r"\\") {
                if parts.len() < 2 {
                    return None;
                }
                let server = parts.remove(0);
                let share = parts.remove(0);
                vec![format!(r"\\{}\{}", server, share)]
            } else if value.len() >= 3
                && value.as_bytes()[1] == b':'
                && value.as_bytes()[2] == b'\\'
            {
                vec![value[..2].to_string()]
            } else {
                return None;
            }
        }
    };
    let mut output = prefix;
    for part in parts {
        if part == ".." {
            if output.len() == 1 {
                return None;
            }
            output.pop();
        } else {
            output.push(part);
        }
    }
    Some(output)
}

impl fmt::Display for TrustLane {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TrustLane::App => f.write_str("app"),
            TrustLane::Agent => f.write_str("agent"),
        }
    }
}
