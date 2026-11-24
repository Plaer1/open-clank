use crate::MutationCaptureHook;
use crate::engine::{OrdinaryWriteCapture, OrdinaryCreateCapture, SearchDetails};
use crate::{
    ActiveAgentFolder, AgentScope, AppScope, Capability, ClientRequest, EngineError, FileEngine,
    Fingerprint, Operation, PolicyGeneration, ProtocolError, ProtocolErrorCode, RequestContext,
    RequestEnvelope, SearchOptions, Target, ThumbnailBroker, ThumbnailCancellationToken,
    ThumbnailError, ThumbnailPng, ThumbnailRequest, TrashEntry, TrustLane,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
#[cfg(not(test))]
use std::sync::OnceLock;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

const MAX_OPEN_HANDLES: usize = 1024;
const MAX_THUMBNAIL_CACHE_ENTRIES: usize = 512;
const MAX_THUMBNAIL_CACHE_BYTES: usize = 128 * 1024 * 1024;
const THUMBNAIL_CACHE_TTL: Duration = Duration::from_secs(5 * 60);
const MAX_STAGE_CHUNK_BYTES: usize = 512 * 1024;
const MAX_ACTIVE_STAGES: usize = 32;
const MAX_ACTIVE_STAGE_BYTES: u64 = 64 * 1024 * 1024;
// Kept unavailable until native History/registry recovery and Host consumer
// receive joint source review and focused qualification.
const MANAGED_FILE_MOVE_READY: bool = cfg!(target_os = "linux");
// The bounded folder coordinator is available only in the reviewed Linux App lane.
const MANAGED_FOLDER_MOVE_READY: bool = cfg!(target_os = "linux");
const STAGE_TTL: Duration = Duration::from_secs(15 * 60);
const STAGE_ROOT_NAME: &str = ".openclank-files-staging-v1";
const MAX_STAGE_REAP_ENTRIES: usize = 256;
const MAX_STAGE_MARKER_BYTES: usize = 4096;
static STAGE_SEQUENCE: AtomicU64 = AtomicU64::new(1);

// A path authorized as a regular file may be substituted before either open.
// Nonblocking open prevents a substituted FIFO from waiting for a peer; the
// descriptor check rejects it, and NOFOLLOW rejects a substituted final link.
fn open_identity_file(path: &Path) -> std::io::Result<File> {
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    {
        let file = OpenOptions::new().read(true).custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW).open(path)?;
        if !file.metadata()?.is_file() {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "not a regular file"));
        }
        Ok(file)
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    { File::open(path) }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct StageMarker {
    version: u8,
    stage_id: String,
    data_name: String,
    owner_id: String,
    principal_id: String,
    lane: TrustLane,
    session_id: String,
    created_unix_ms: u64,
}

#[cfg(target_os="linux")]
struct WorktreeSlot { binding:crate::engine::worktree_stage::Binding, target:PathBuf, state:Mutex<WorktreeSlotState> }
#[cfg(target_os="linux")]
enum WorktreeSlotState { Starting, Active{writer:crate::engine::worktree_stage::WorktreeStageWriter,sealed_digest:Option<String>,private:Option<crate::engine::worktree_stage::PrivateWorktreeDirectory>}, Inverse{writer:crate::engine::worktree_stage::WorktreeInverse,unknown:bool}, Redo{writer:crate::engine::worktree_stage::WorktreeRedo,unknown:bool}, Unknown }

struct StagedUpload {
    owner_id: String,
    principal_id: String,
    lane: TrustLane,
    session_id: String,
    path: PathBuf,
    marker_path: Option<PathBuf>,
    file: File,
    length: u64,
    digest: Sha256,
    created: Instant,
    #[cfg(target_os="linux")]
    worktree: Option<Arc<WorktreeSlot>>,
}
impl StagedUpload {
    fn is_worktree(&self)->bool {
        #[cfg(target_os="linux")] {return self.worktree.is_some();}
        #[cfg(not(target_os="linux"))] {false}
    }
}

#[derive(Clone)]
struct StageStore(Arc<Mutex<HashMap<String, StagedUpload>>>);

#[cfg(not(test))]
static GLOBAL_STAGE_STORE: OnceLock<StageStore> = OnceLock::new();

impl Drop for StageStore {
    fn drop(&mut self) {
        if Arc::strong_count(&self.0) != 1 {
            return;
        }
        if let Ok(mut stages) = self.0.lock() {
            for (_, stage) in stages.drain() {
                if stage.is_worktree(){continue;}
                let _ = fs::remove_file(stage.path);
                if let Some(marker) = stage.marker_path {
                    let _ = fs::remove_file(marker);
                }
            }
        }
    }
}

impl StageStore {
    fn root() -> PathBuf {
        std::env::temp_dir().join(STAGE_ROOT_NAME)
    }

    fn ensure_root() -> std::io::Result<PathBuf> {
        let root = Self::root();
        #[cfg(windows)]
        { crate::windows_fs::ensure_private_directory(&root)?; return Ok(root); }
        #[cfg(not(windows))]
        {
        match fs::symlink_metadata(&root) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return Err(std::io::Error::new(std::io::ErrorKind::Other, "staging root is not a directory"));
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => fs::create_dir(&root)?,
            Err(error) => return Err(error),
        }
        #[cfg(unix)]
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
        Ok(root)
        }
    }

    fn valid_stage_id(value: &str) -> bool {
        value.len() == 73 && value.starts_with("fs-stage-") && value[9..].bytes().all(|byte| byte.is_ascii_hexdigit())
    }

    fn remove_stage_files(stage: &StagedUpload) {
        if stage.is_worktree(){return;}
        let _ = fs::remove_file(&stage.path);
        if let Some(marker) = &stage.marker_path {
            let _ = fs::remove_file(marker);
        }
    }

    fn reap_for_identity(&self, identity: &ServiceIdentity) {
        let root = match Self::ensure_root() {
            Ok(root) => root,
            Err(_) => return,
        };
        let active: BTreeSet<String> = self.0.lock().ok().map(|stages| stages.keys().cloned().collect()).unwrap_or_default();
        let now = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
            Ok(value) => value.as_millis() as u64,
            Err(_) => return,
        };
        let entries = match fs::read_dir(&root) {
            Ok(entries) => entries,
            Err(_) => return,
        };
        for entry in entries.take(MAX_STAGE_REAP_ENTRIES) {
            let entry = match entry { Ok(entry) => entry, Err(_) => continue };
            let marker_path = entry.path();
            let file_name = match entry.file_name().to_str() { Some(name) => name.to_string(), None => continue };
            let stage_id = match file_name.strip_suffix(".json") {
                Some(value) if Self::valid_stage_id(value) => value.to_string(),
                _ => continue,
            };
            if active.contains(&stage_id) { continue; }
            let metadata = match fs::symlink_metadata(&marker_path) {
                Ok(metadata) if metadata.file_type().is_file() => metadata,
                _ => continue,
            };
            if metadata.len() > MAX_STAGE_MARKER_BYTES as u64 { continue; }
            let marker: StageMarker = match fs::read(&marker_path).ok().and_then(|bytes| serde_json::from_slice(&bytes).ok()) {
                Some(marker) => marker,
                None => continue,
            };
            if marker.version != 1 || marker.stage_id != stage_id || marker.data_name != format!("{stage_id}.data")
                || marker.owner_id != identity.owner_id || marker.principal_id != identity.principal_id
                || marker.lane != identity.lane || marker.created_unix_ms > now
                || now.saturating_sub(marker.created_unix_ms) <= STAGE_TTL.as_millis() as u64 { continue; }
            let data_path = root.join(&marker.data_name);
            let data_metadata = match fs::symlink_metadata(&data_path) {
                Ok(metadata) if metadata.file_type().is_file() => metadata,
                _ => continue,
            };
            let _ = data_metadata;
            let _ = fs::remove_file(&data_path);
            let _ = fs::remove_file(&marker_path);
        }
    }

    #[cfg(not(test))]
    fn shared() -> Self {
        GLOBAL_STAGE_STORE
            .get_or_init(|| StageStore(Arc::new(Mutex::new(HashMap::new()))))
            .clone()
    }

    #[cfg(test)]
    fn shared() -> Self {
        StageStore(Arc::new(Mutex::new(HashMap::new())))
    }

    fn remove_session(&self, identity: &ServiceIdentity) {
        if let Ok(mut stages) = self.0.lock() {
            let ids: Vec<String> = stages
                .iter()
                .filter_map(|(id, stage)| {
                    (stage.owner_id == identity.owner_id
                        && stage.principal_id == identity.principal_id
                        && stage.lane == identity.lane
                        && stage.session_id == identity.session_id)
                    .then_some(id.clone())
                })
                .collect();
            for id in ids {
                if let Some(stage) = stages.remove(&id) {
                    Self::remove_stage_files(&stage);
                }
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ObjectIdentity {
    #[cfg(windows)]
    native: crate::windows_fs::OpenedIdentity,
    size: u64,
    modified_ns: Option<u128>,
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(unix)]
    ctime_seconds: i64,
    #[cfg(unix)]
    ctime_nanoseconds: i64,
}

impl ObjectIdentity {
    pub(crate) fn from_file(file: &File) -> std::io::Result<Self> {
        let metadata = file.metadata()?;
        Ok(Self {
            #[cfg(windows)]
            native: crate::windows_fs::opened_identity(file)?,
            size: metadata.len(),
            modified_ns: metadata
                .modified()
                .ok()
                .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|value| value.as_nanos()),
            #[cfg(unix)]
            device: metadata.dev(),
            #[cfg(unix)]
            inode: metadata.ino(),
            #[cfg(unix)]
            ctime_seconds: metadata.ctime(),
            #[cfg(unix)]
            ctime_nanoseconds: metadata.ctime_nsec(),
        })
    }

    fn from_path(path: &Path) -> std::io::Result<Self> {
        Self::from_file(&open_identity_file(path)?)
    }

    pub(crate) fn opaque_tag(&self) -> String {
        let mut digest = Sha256::new();
        digest.update(format!("odysseus-file-object-v1:{self:?}").as_bytes());
        format!("{:x}", digest.finalize())
    }
}

struct HandleEntry {
    handle: crate::ResourceHandle,
    owner_id: String,
    principal_id: String,
    lane: TrustLane,
    session_id: String,
    path: PathBuf,
    file: File,
    identity: ObjectIdentity,
}

#[derive(Default)]
struct HandleTable {
    entries: HashMap<String, HandleEntry>,
    order: VecDeque<String>,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct ThumbnailCacheKey {
    object_tag: String,
    icon: bool,
    policy_generation: u64,
    width: u32,
    height: u32,
    scale_milli: u32,
}

struct ThumbnailCacheEntry {
    png: ThumbnailPng,
    inserted: Instant,
}

#[derive(Default)]
struct ThumbnailCache {
    entries: HashMap<ThumbnailCacheKey, ThumbnailCacheEntry>,
    order: VecDeque<ThumbnailCacheKey>,
    bytes: usize,
}

impl ThumbnailCache {
    fn get(&mut self, key: &ThumbnailCacheKey) -> Option<ThumbnailPng> {
        let now = Instant::now();
        self.prune_expired(now);
        let png = self.entries.get(key)?.png.clone();
        self.order.retain(|candidate| candidate != key);
        self.order.push_back(key.clone());
        Some(png)
    }

    fn insert(&mut self, key: ThumbnailCacheKey, png: ThumbnailPng) {
        if png.bytes.len() > MAX_THUMBNAIL_CACHE_BYTES {
            return;
        }
        if let Some(previous) = self.entries.remove(&key) {
            self.bytes = self.bytes.saturating_sub(previous.png.bytes.len());
            self.order.retain(|candidate| candidate != &key);
        }
        self.bytes = self.bytes.saturating_add(png.bytes.len());
        self.entries.insert(
            key.clone(),
            ThumbnailCacheEntry {
                png,
                inserted: Instant::now(),
            },
        );
        self.order.push_back(key);
        while self.entries.len() > MAX_THUMBNAIL_CACHE_ENTRIES
            || self.bytes > MAX_THUMBNAIL_CACHE_BYTES
        {
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            if let Some(removed) = self.entries.remove(&oldest) {
                self.bytes = self.bytes.saturating_sub(removed.png.bytes.len());
            }
        }
    }

    fn prune_expired(&mut self, now: Instant) {
        let expired: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(key, entry)| {
                (now.duration_since(entry.inserted) >= THUMBNAIL_CACHE_TTL).then(|| key.clone())
            })
            .collect();
        for key in expired {
            if let Some(removed) = self.entries.remove(&key) {
                self.bytes = self.bytes.saturating_sub(removed.png.bytes.len());
            }
            self.order.retain(|candidate| candidate != &key);
        }
    }
}

#[derive(Clone, Debug)]
pub struct ServiceIdentity {
    owner_id: String,
    principal_id: String,
    lane: TrustLane,
    session_id: String,
    approved_root_ids: BTreeSet<String>,
    root_capabilities: BTreeMap<String, BTreeSet<Capability>>,
    capabilities: BTreeSet<Capability>,
    active_folder: Option<ActiveAgentFolder>,
    app_scope: AppScope,
}

impl ServiceIdentity {
    pub fn app(
        owner_id: impl Into<String>,
        principal_id: impl Into<String>,
        session_id: impl Into<String>,
    ) -> Self {
        Self {
            owner_id: owner_id.into(),
            principal_id: principal_id.into(),
            lane: TrustLane::App,
            session_id: session_id.into(),
            approved_root_ids: BTreeSet::new(),
            root_capabilities: BTreeMap::new(),
            capabilities: BTreeSet::new(),
            active_folder: None,
            app_scope: AppScope::host(),
        }
    }

    pub fn app_with_scope(
        owner_id: impl Into<String>,
        principal_id: impl Into<String>,
        session_id: impl Into<String>,
        app_scope: AppScope,
    ) -> Self {
        Self {
            owner_id: owner_id.into(),
            principal_id: principal_id.into(),
            lane: TrustLane::App,
            session_id: session_id.into(),
            approved_root_ids: BTreeSet::new(),
            root_capabilities: BTreeMap::new(),
            capabilities: BTreeSet::new(),
            active_folder: None,
            app_scope,
        }
    }

    pub fn agent(
        owner_id: impl Into<String>,
        principal_id: impl Into<String>,
        session_id: impl Into<String>,
        scope: AgentScope,
        capabilities: impl IntoIterator<Item = Capability>,
    ) -> Self {
        Self {
            owner_id: owner_id.into(),
            principal_id: principal_id.into(),
            lane: TrustLane::Agent,
            session_id: session_id.into(),
            approved_root_ids: scope.approved_root_ids,
            root_capabilities: scope.root_capabilities,
            capabilities: capabilities.into_iter().collect(),
            active_folder: scope.active_folder,
            app_scope: AppScope::host(),
        }
    }

    pub fn lane(&self) -> TrustLane {
        self.lane
    }
    pub fn owner_id(&self) -> &str {
        &self.owner_id
    }
    pub fn principal_id(&self) -> &str {
        &self.principal_id
    }
    pub fn session_id(&self) -> &str {
        &self.session_id
    }
    pub fn app_scope(&self) -> &AppScope {
        &self.app_scope
    }

    fn scope(&self) -> AgentScope {
        AgentScope {
            approved_root_ids: self.approved_root_ids.clone(),
            root_capabilities: self.root_capabilities.clone(),
            active_folder: self.active_folder.clone(),
        }
    }
}

pub struct FileService {
    engine: FileEngine,
    identity: ServiceIdentity,
    handles: Mutex<HandleTable>,
    thumbnail_broker: Option<ThumbnailBroker>,
    native_icon_broker: Option<ThumbnailBroker>,
    thumbnail_policy_generation: PolicyGeneration,
    thumbnail_cache: Mutex<ThumbnailCache>,
    staged_uploads: StageStore,
    mutation_receipt_lock: Mutex<()>,
    checkpoint_prepared: Mutex<Option<PreparedCheckpoint>>,
}

struct PreparedCheckpoint {
    selector: String,
    ticket: Box<dyn crate::CheckpointTicket>,
    files: Vec<CheckpointPinned>,
    deadline: Instant,
    started_millis: u64,
    finished_millis: u64,
}
struct CheckpointPinned { path: PathBuf, binding: crate::CheckpointBinding, file: File, identity: ObjectIdentity, root_identity: (u64,u64) }

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CheckpointRegistrationArgs { root_id:String, expected_fingerprint:String }
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CheckpointInput { path: String, resource_id: String, expected_fingerprint: String }
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CheckpointPrepareArgs { request_id: String, root_id: String, items: Vec<CheckpointInput> }
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CheckpointFinalizeArgs { selector: String }

/// Internal authorization result for the private watch transport. The
/// canonical path never crosses the Rust/Python trust boundary; browser-facing
/// watch events are path-free invalidation hints.
#[derive(Clone, Debug)]
pub struct AuthorizedWatch {
    pub path: PathBuf,
    pub request_id: String,
    pub generation: u64,
}

impl FileService {
    pub fn new(engine: FileEngine, identity: ServiceIdentity) -> Self {
        let policy_generation = match identity.lane {
            TrustLane::App => identity.app_scope.generation,
            TrustLane::Agent => 0,
        };
        let staged_uploads = StageStore::shared();
        staged_uploads.reap_for_identity(&identity);
        Self {
            engine,
            mutation_receipt_lock: Mutex::new(()),            identity,
            checkpoint_prepared: Mutex::new(None),
            handles: Mutex::new(HandleTable::default()),
            thumbnail_broker: None,
            native_icon_broker: None,
            thumbnail_policy_generation: PolicyGeneration::new(policy_generation),
            thumbnail_cache: Mutex::new(ThumbnailCache::default()),
            staged_uploads,
        }
    }

    /// Bind the authenticated history owner when constructing the provider.
    /// The hook is immutable after this point and all engine mutations share
    /// the same owner-bound service connection.
    pub fn with_history_capture(mut self, hook: std::sync::Arc<dyn MutationCaptureHook>) -> Self {
        self.engine.set_capture_hook(hook);
        self
    }

    pub fn last_capture_status(&self) -> Option<crate::CaptureStatus> {
        self.engine.last_capture_status()
    }

    pub fn with_thumbnail_broker(mut self, broker: ThumbnailBroker) -> Self {
        self.thumbnail_broker = Some(broker);
        self
    }

    pub fn with_native_icon_broker(mut self, broker: ThumbnailBroker) -> Self {
        self.native_icon_broker = Some(broker);
        self
    }

    pub fn native_thumbnails_available(&self) -> bool {
        self.thumbnail_broker.is_some()
    }

    pub fn engine(&self) -> &FileEngine {
        &self.engine
    }

    pub fn authorize_watch(
        &self,
        request: ClientRequest,
    ) -> Result<AuthorizedWatch, ProtocolError> {
        let (operation, envelope, audit_id) = self.bind_request(request)?;
        if operation != Operation::WatchSubscribe {
            return Err(ProtocolError::new(
                ProtocolErrorCode::MalformedRequest,
                "watch RPC requires a watch_subscribe operation",
                Some(audit_id),
            ));
        }
        let args: WatchSubscribeArgs = serde_json::from_value(envelope.payload).map_err(|_| {
            ProtocolError::new(
                ProtocolErrorCode::MalformedRequest,
                "watch_subscribe payload is invalid",
                Some(audit_id.clone()),
            )
        })?;
        if args.recursive {
            return Err(ProtocolError::new(
                ProtocolErrorCode::Denied,
                "recursive filesystem watches are not available",
                Some(audit_id),
            ));
        }
        let path = target_path(&envelope.target, &audit_id)?;
        let authorized = match self.identity.lane {
            TrustLane::App => self
                .engine
                .stat_scoped(&self.identity.app_scope, &path, false),
            TrustLane::Agent => self.engine.stat(
                self.identity.owner_id(),
                &self.identity.scope(),
                &path,
                false,
            ),
        }
        .map_err(|error| engine_protocol(error, &audit_id))?;
        if authorized.kind != crate::engine::FileKind::Directory {
            return Err(ProtocolError::new(
                ProtocolErrorCode::InvalidPath,
                "filesystem watches require an authorized directory",
                Some(audit_id),
            ));
        }
        Ok(AuthorizedWatch {
            path: authorized.path,
            request_id: envelope.context.request_id().to_string(),
            generation: self.scope_generation(),
        })
    }
    pub fn identity(&self) -> &ServiceIdentity {
        &self.identity
    }

    fn scope_generation(&self) -> u64 {
        match self.identity.lane {
            TrustLane::App => self.identity.app_scope.generation,
            // Agent service instances are themselves generation-scoped and are
            // torn down by the host on any policy mutation.
            TrustLane::Agent => 0,
        }
    }

    fn stale_handle(&self, audit_id: &str) -> ProtocolError {
        ProtocolError::new(
            ProtocolErrorCode::StaleHandle,
            "resource handle is stale or unavailable",
            Some(audit_id.into()),
        )
    }

    // One bounded, lossless directory observation; no cursor or retained handle.
    #[cfg(target_os = "linux")]
    fn strict_snapshot_directory(&self, path: &Path, limit: usize, audit_id: &str) -> Result<Value, ProtocolError> {
        use std::os::fd::AsRawFd;
        let stat = match self.identity.lane {
            TrustLane::App => self.engine.stat_scoped(&self.identity.app_scope, path, false),
            TrustLane::Agent => self.engine.stat(self.identity.owner_id(), &self.identity.scope(), path, false),
        }.map_err(|e| engine_protocol(e, audit_id))?;
        if stat.kind != crate::engine::FileKind::Directory || limit == 0 || limit > 1024 { return Err(self.stale_handle(audit_id)); }
        let canonical=fs::canonicalize(path).map_err(|_| self.stale_handle(audit_id))?;
        if canonical!=stat.path || fs::symlink_metadata(path).map_err(|_| self.stale_handle(audit_id))?.file_type().is_symlink() {return Err(self.stale_handle(audit_id));}
        let dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK).open(path).map_err(|_| self.stale_handle(audit_id))?;
        let before=ObjectIdentity::from_file(&dir).map_err(|_| self.stale_handle(audit_id))?;
        let pinned=PathBuf::from(format!("/proc/self/fd/{}",dir.as_raw_fd()));
        let mut entries=Vec::new();let mut reasons=BTreeSet::new();let mut omitted=0usize;
        let mut iter=fs::read_dir(&pinned).map_err(|_| self.stale_handle(audit_id))?;
        for index in 0..=limit {
            let Some(item)=iter.next() else {break};
            if index==limit {omitted+=1;reasons.insert("overflow");break;}
            let item=item.map_err(|_| self.stale_handle(audit_id))?;
            let name=item.file_name();let Some(name)=name.to_str() else {omitted+=1;reasons.insert("invalid_name");continue};
            if name.is_empty() || name=="." || name==".." || name.contains('\\') || name.chars().any(|c|c.is_control()) || name.len()>255 {omitted+=1;reasons.insert("invalid_name");continue;}
            let metadata=fs::symlink_metadata(item.path()).map_err(|_| self.stale_handle(audit_id))?;
            let kind=if metadata.file_type().is_symlink(){omitted+=1;reasons.insert("symlink");continue}else if metadata.is_dir(){"directory"}else if metadata.is_file(){"file"}else{omitted+=1;reasons.insert("special");continue};
            let child=path.join(name);
            let authorized=match self.identity.lane {TrustLane::App=>self.engine.stat_scoped(&self.identity.app_scope,&child,false),TrustLane::Agent=>self.engine.stat(self.identity.owner_id(),&self.identity.scope(),&child,false)};
            if authorized.is_err(){omitted+=1;reasons.insert("denied");continue;}
            entries.push(json!({"name":name,"kind":kind,"size":metadata.len()}));
        }
        entries.sort_by(|a,b|a["name"].as_str().unwrap_or("").as_bytes().cmp(b["name"].as_str().unwrap_or("").as_bytes()));
        let after=ObjectIdentity::from_file(&dir).map_err(|_| self.stale_handle(audit_id))?;
        let path_dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK).open(path).map_err(|_| self.stale_handle(audit_id))?;
        if before!=after || before!=ObjectIdentity::from_file(&path_dir).map_err(|_| self.stale_handle(audit_id))? {return Err(self.stale_handle(audit_id));}
        let metadata=dir.metadata().map_err(|_| self.stale_handle(audit_id))?;
        let directory_id=format!("{:x}",Sha256::digest(format!("odysseus-directory-identity-v1:{}:{}",metadata.dev(),metadata.ino()).as_bytes()));
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"path":canonical,"entries":entries,"object_tag":before.opaque_tag(),"directory_id":directory_id,"complete":reasons.is_empty(),"omitted_count":omitted,"reasons":reasons},"audit_id":audit_id}))
    }

    fn authorized_open_identity(&self, path: &Path, audit_id: &str) -> Result<(PathBuf, File, ObjectIdentity), ProtocolError> {
        let authorized = match self.identity.lane {
            TrustLane::App => self
                .engine
                .stat_scoped(&self.identity.app_scope, path, false),
            TrustLane::Agent => self.engine.stat(
                self.identity.owner_id(),
                &self.identity.scope(),
                path,
                false,
            ),
        };
        let authorized = authorized.map_err(|error| engine_protocol(error, audit_id))?;
        if authorized.kind != crate::engine::FileKind::File {
            return Err(ProtocolError::new(
                ProtocolErrorCode::InvalidPath,
                "only regular files can be opened as stable handles",
                Some(audit_id.into()),
            ));
        }
        let canonical = fs::canonicalize(path)
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        if canonical != authorized.path {
            return Err(self.stale_handle(audit_id));
        }
        let link = fs::symlink_metadata(&canonical)
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        if link.file_type().is_symlink() {
            return Err(self.stale_handle(audit_id));
        }
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        let open_path = path;
        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        let open_path = canonical.as_path();
        let file = open_identity_file(open_path)
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        let descriptor_metadata = file
            .metadata()
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        #[cfg(windows)]
        crate::windows_fs::validate_opened_path(&file, &canonical).map_err(|_| self.stale_handle(audit_id))?;
        let identity = ObjectIdentity::from_file(&file).map_err(|_| self.stale_handle(audit_id))?;
        if !descriptor_metadata.is_file()
            || identity != ObjectIdentity::from_path(&canonical).map_err(|_| self.stale_handle(audit_id))?
        { return Err(self.stale_handle(audit_id)); }
        Ok((canonical, file, identity))
    }

    fn open_handle(&self, path: &Path, audit_id: &str) -> Result<Value, ProtocolError> {
        let (canonical, file, identity) = self.authorized_open_identity(path, audit_id)?;
        let descriptor_metadata = file.metadata().map_err(|_| self.stale_handle(audit_id))?;
        let object_tag = identity.opaque_tag();
        let mut random = [0_u8; 32];
        getrandom::fill(&mut random).map_err(|_| {
            ProtocolError::new(
                ProtocolErrorCode::RootUnavailable,
                "secure resource handle generation failed",
                Some(audit_id.into()),
            )
        })?;
        let token: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let handle = crate::ResourceHandle {
            token: token.clone(),
            root_id: "service-object".into(),
            relative_components: Vec::new(),
            generation: self.scope_generation(),
        };
        let mut table = self.handles.lock().map_err(|_| {
            ProtocolError::new(
                ProtocolErrorCode::Crashed,
                "resource handle table is unavailable",
                Some(audit_id.into()),
            )
        })?;
        while table.entries.len() >= MAX_OPEN_HANDLES {
            if let Some(oldest) = table.order.pop_front() {
                table.entries.remove(&oldest);
            } else {
                break;
            }
        }
        table.order.push_back(token.clone());
        table.entries.insert(
            token,
            HandleEntry {
                handle: handle.clone(),
                owner_id: self.identity.owner_id.clone(),
                principal_id: self.identity.principal_id.clone(),
                lane: self.identity.lane,
                session_id: self.identity.session_id.clone(),
                path: canonical,
                file,
                identity,
            },
        );
        Ok(json!({
            "protocol": crate::PROTOCOL_VERSION,
            "data": {
                "handle": handle,
                "kind": crate::engine::FileKind::File,
                "size": descriptor_metadata.len(),
                "modified_unix_ms": metadata_modified_ms(&descriptor_metadata),
                "mode": metadata_mode(&descriptor_metadata),
                "object_tag": object_tag,
            },
            "audit_id": audit_id,
        }))
    }

    fn stage_matches(&self, stage: &StagedUpload) -> bool {
        !stage.is_worktree() && stage.owner_id == self.identity.owner_id
            && stage.principal_id == self.identity.principal_id
            && stage.lane == self.identity.lane
            && stage.session_id == self.identity.session_id
    }

    fn stage_error(message: &str, audit_id: &str) -> ProtocolError {
        ProtocolError::new(ProtocolErrorCode::MalformedRequest, message, Some(audit_id.into()))
    }

    fn prune_stages(&self) {
        let now = Instant::now();
        let mut expired = Vec::new();
        if let Ok(mut stages) = self.staged_uploads.0.lock() {
            let ids: Vec<String> = stages
                .iter()
                .filter_map(|(id, stage)| {
                    (!stage.is_worktree() && stage.owner_id == self.identity.owner_id
                        && stage.principal_id == self.identity.principal_id
                        && stage.lane == self.identity.lane
                        && stage.session_id == self.identity.session_id
                        && now.duration_since(stage.created) > STAGE_TTL)
                        .then_some(id.clone())
                })
                .collect();
            for id in ids {
                if let Some(stage) = stages.remove(&id) { expired.push(stage); }
            }
        }
        for stage in expired { StageStore::remove_stage_files(&stage); }
        self.staged_uploads.reap_for_identity(&self.identity);
    }

    fn discard_stage(&self, stage_id: &str) {
        if let Ok(mut stages) = self.staged_uploads.0.lock() {
            if let Some(stage) = stages.remove(stage_id) { StageStore::remove_stage_files(&stage); }
        }
    }

    #[cfg(target_os="linux")]
    fn worktree_stage_begin(&self, target:PathBuf, args:crate::engine::worktree_stage::Begin, audit_id:&str) -> Result<Value,ProtocolError> {
        use crate::engine::worktree_stage::Binding;
        if self.identity.lane!=TrustLane::App || args.root_path!=target {return Err(Self::stage_error("worktree stage unavailable",audit_id));}
        let binding=Binding{operation_id:args.operation_id.clone(),request_digest:args.request_digest.clone(),git_plan_digest:args.git_plan_digest.clone(),root_id:args.root_id.clone(),root_object_tag:args.root_object_tag.clone()};
        let slot=Arc::new(WorktreeSlot{binding,target:target.clone(),state:Mutex::new(WorktreeSlotState::Starting)});
        // Reserve the only worktree slot under the existing registry lock. Do
        // not hold that lock while validating, allocating or writing the file.
        let placeholder=File::open("/dev/null").map_err(|_|Self::stage_error("worktree stage unavailable",audit_id))?;
        let existing={let mut stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree stage unavailable",audit_id))?;
            if let Some(entry)=stages.values().find(|entry|entry.is_worktree()) {
                let same=entry.owner_id==self.identity.owner_id && entry.principal_id==self.identity.principal_id && entry.lane==self.identity.lane && entry.session_id==self.identity.session_id;
                Some((same,entry.worktree.clone()))
            } else {
                stages.insert("__worktree_stage__".into(),StagedUpload{owner_id:self.identity.owner_id.clone(),principal_id:self.identity.principal_id.clone(),lane:self.identity.lane,session_id:self.identity.session_id.clone(),path:PathBuf::new(),marker_path:None,file:placeholder,length:0,digest:Sha256::new(),created:Instant::now(),worktree:Some(slot.clone())});None
            }};
        if let Some((same,Some(original)))=existing {
            if same && original.binding==slot.binding && original.target==slot.target {if let Ok(mut state)=original.state.lock(){*state=WorktreeSlotState::Unknown;}}
            return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));
        }
        if existing.is_some(){return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));}
        let result=self.engine.worktree_stage_begin(&self.identity.app_scope,self.identity.owner_id(),&args);
        let mut state=slot.state.lock().map_err(|_|Self::stage_error("worktree stage unavailable",audit_id))?;
        let data=match result {Ok(writer) if matches!(*state,WorktreeSlotState::Starting)=>{*state=WorktreeSlotState::Active{writer,sealed_digest:None,private:None};json!({"state":"accepted","next_ordinal":0,"next_offset":0})},_=>{*state=WorktreeSlotState::Unknown;json!({"state":"unknown"})}};
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}))
    }

    #[cfg(target_os="linux")]
    fn worktree_inverse_begin(&self,target:PathBuf,args:crate::engine::worktree_stage::InverseBegin,audit_id:&str)->Result<Value,ProtocolError>{
        use crate::engine::worktree_stage::Binding;
        if self.identity.lane!=TrustLane::App || args.root_path!=target {return Err(Self::stage_error("worktree inverse unavailable",audit_id));}
        let binding=Binding{operation_id:args.operation_id.clone(),request_digest:args.request_digest.clone(),git_plan_digest:args.git_plan_digest.clone(),root_id:args.root_id.clone(),root_object_tag:args.root_object_tag.clone()};
        let slot=Arc::new(WorktreeSlot{binding,target:target.clone(),state:Mutex::new(WorktreeSlotState::Starting)});
        let placeholder=File::open("/dev/null").map_err(|_|Self::stage_error("worktree inverse unavailable",audit_id))?;
        let existing={let mut stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree inverse unavailable",audit_id))?;
            if let Some(entry)=stages.values().find(|entry|entry.is_worktree()) {
                let same=entry.owner_id==self.identity.owner_id && entry.principal_id==self.identity.principal_id && entry.lane==self.identity.lane && entry.session_id==self.identity.session_id;
                Some((same,entry.worktree.clone()))
            } else {stages.insert("__worktree_stage__".into(),StagedUpload{owner_id:self.identity.owner_id.clone(),principal_id:self.identity.principal_id.clone(),lane:self.identity.lane,session_id:self.identity.session_id.clone(),path:PathBuf::new(),marker_path:None,file:placeholder,length:0,digest:Sha256::new(),created:Instant::now(),worktree:Some(slot.clone())});None}};
        if let Some((same,Some(original)))=existing {
            if same && original.binding==slot.binding && original.target==slot.target {if let Ok(mut state)=original.state.lock(){if let WorktreeSlotState::Inverse{unknown,..}=&mut *state {*unknown=true;}}}
            return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));
        }
        if existing.is_some(){return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));}
        let result=self.engine.worktree_inverse_begin(&self.identity.app_scope,self.identity.owner_id(),&args);
        let mut state=slot.state.lock().map_err(|_|Self::stage_error("worktree inverse unavailable",audit_id))?;
        let data=match result {Ok(writer) if matches!(*state,WorktreeSlotState::Starting)=>match writer.original_evidence(){Ok(original)=>{*state=WorktreeSlotState::Inverse{writer,unknown:false};json!({"state":"accepted","original":original})},Err(_)=>{*state=WorktreeSlotState::Inverse{writer,unknown:true};json!({"state":"unknown"})}},_=>{*state=WorktreeSlotState::Unknown;json!({"state":"unknown"})}};
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}))
    }
    #[cfg(target_os="linux")]
    fn worktree_redo_begin(&self,target:PathBuf,args:crate::engine::worktree_stage::RedoBegin,audit_id:&str)->Result<Value,ProtocolError>{
        use crate::engine::worktree_stage::Binding;
        if self.identity.lane!=TrustLane::App || args.root_path!=target {return Err(Self::stage_error("worktree redo unavailable",audit_id));}
        let binding=Binding{operation_id:args.operation_id.clone(),request_digest:args.request_digest.clone(),git_plan_digest:args.git_plan_digest.clone(),root_id:args.root_id.clone(),root_object_tag:args.root_object_tag.clone()};
        let slot=Arc::new(WorktreeSlot{binding,target:target.clone(),state:Mutex::new(WorktreeSlotState::Starting)});
        let placeholder=File::open("/dev/null").map_err(|_|Self::stage_error("worktree redo unavailable",audit_id))?;
        let existing={let mut stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree redo unavailable",audit_id))?;
            if let Some(entry)=stages.values().find(|entry|entry.is_worktree()) {
                let same=entry.owner_id==self.identity.owner_id && entry.principal_id==self.identity.principal_id && entry.lane==self.identity.lane && entry.session_id==self.identity.session_id;
                Some((same,entry.worktree.clone()))
            } else {stages.insert("__worktree_stage__".into(),StagedUpload{owner_id:self.identity.owner_id.clone(),principal_id:self.identity.principal_id.clone(),lane:self.identity.lane,session_id:self.identity.session_id.clone(),path:PathBuf::new(),marker_path:None,file:placeholder,length:0,digest:Sha256::new(),created:Instant::now(),worktree:Some(slot.clone())});None}};
        if let Some((same,Some(original)))=existing {
            if same && original.binding==slot.binding && original.target==slot.target {if let Ok(mut state)=original.state.lock(){if let WorktreeSlotState::Redo{unknown,..}=&mut *state {*unknown=true;}}}
            return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));
        }
        if existing.is_some(){return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));}
        let result=self.engine.worktree_redo_begin(&self.identity.app_scope,self.identity.owner_id(),&args);
        let mut state=slot.state.lock().map_err(|_|Self::stage_error("worktree redo unavailable",audit_id))?;
        let data=match result {Ok(writer) if matches!(*state,WorktreeSlotState::Starting)=>match writer.source_evidence(){Ok(original)=>{*state=WorktreeSlotState::Redo{writer,unknown:false};json!({"state":"accepted","original":original})},Err(_)=>{*state=WorktreeSlotState::Redo{writer,unknown:true};json!({"state":"unknown"})}},_=>{*state=WorktreeSlotState::Unknown;json!({"state":"unknown"})}};
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}))
    }
    #[cfg(target_os="linux")]
    fn worktree_inverse_phase(&self,operation:Operation,target:PathBuf,payload:Value,audit_id:&str)->Result<Value,ProtocolError>{
        use crate::engine::worktree_stage::Binding;
        if self.identity.lane!=TrustLane::App{return Err(Self::stage_error("worktree inverse unavailable",audit_id));}
        let mut object=payload.as_object().cloned().ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?;
        let timeout=object.remove("timeout_ms").and_then(|v|v.as_u64()).filter(|v|*v>0 && *v<=300000).ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?;
        let witness=if matches!(operation,Operation::WorktreeInverseRelocateAdmin|Operation::WorktreeInverseFinish) {
            Some(serde_json::from_value::<crate::engine::worktree_stage::InverseGitAdminWitness>(object.remove("witness").ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?).map_err(|_|Self::stage_error("worktree inverse unavailable",audit_id))?)
        }else{None};
        let binding:Binding=serde_json::from_value(Value::Object(object)).map_err(|_|Self::stage_error("worktree inverse unavailable",audit_id))?;
        let slot={let stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree inverse unavailable",audit_id))?;
            let entry=stages.get("__worktree_stage__").ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?;
            if entry.owner_id!=self.identity.owner_id || entry.principal_id!=self.identity.principal_id || entry.lane!=self.identity.lane || entry.session_id!=self.identity.session_id{return Err(Self::stage_error("worktree inverse unavailable",audit_id));}
            entry.worktree.clone().ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?};
        if slot.binding!=binding || slot.target!=target {return Err(Self::stage_error("worktree inverse unavailable",audit_id));}
        let mut state=slot.state.lock().map_err(|_|Self::stage_error("worktree inverse unavailable",audit_id))?;
        let data=match &mut *state {
            WorktreeSlotState::Inverse{writer,unknown} if !*unknown=>{
                let expected=match operation {Operation::WorktreeInversePrepareHistory=>"pending",Operation::WorktreeInverseStageHistory|Operation::WorktreeInverseVerify|Operation::WorktreeInverseQuarantine=>"history_prepared",Operation::WorktreeInverseRelocateAdmin=>"quarantined",Operation::WorktreeInverseFinish=>"admin_relocated",_=>"unknown"};
                if writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_err() || writer.history_state()!=expected {*unknown=true;json!({"state":"unknown"})}
                else {let deadline=Instant::now().checked_add(Duration::from_millis(timeout)).ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?;
                    let outcome=match operation {Operation::WorktreeInversePrepareHistory=>writer.prepare_history(&self.engine,&self.identity.app_scope,deadline),Operation::WorktreeInverseStageHistory=>writer.stage_history(&self.engine,&self.identity.app_scope,deadline),Operation::WorktreeInverseVerify=>writer.verify_target(&self.engine,&self.identity.app_scope,deadline),Operation::WorktreeInverseQuarantine=>writer.quarantine_target(&self.engine,&self.identity.app_scope,deadline),Operation::WorktreeInverseRelocateAdmin=>writer.relocate_git_admin(witness.as_ref().ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?,deadline),Operation::WorktreeInverseFinish=>writer.finish_inverse(&self.engine,&self.identity.app_scope,witness.as_ref().ok_or_else(||Self::stage_error("worktree inverse unavailable",audit_id))?,deadline),_=>Err(EngineError::Conflict)};
                    if outcome.is_ok(){json!({"state":match operation {Operation::WorktreeInverseVerify=>"verified",Operation::WorktreeInverseQuarantine=>"quarantined",Operation::WorktreeInverseRelocateAdmin=>"admin_relocated",Operation::WorktreeInverseFinish=>"completed",_=>"history_prepared"}})}else{*unknown=true;json!({"state":"unknown"})}}
            }, _=>json!({"state":"unknown"})};
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}))
    }

    #[cfg(target_os="linux")]
    fn worktree_redo_phase(&self,operation:Operation,target:PathBuf,payload:Value,audit_id:&str)->Result<Value,ProtocolError>{
        use crate::engine::worktree_stage::Binding;
        if self.identity.lane!=TrustLane::App{return Err(Self::stage_error("worktree redo unavailable",audit_id));}
        let mut object=payload.as_object().cloned().ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?;
        let timeout=object.remove("timeout_ms").and_then(|v|v.as_u64()).filter(|v|*v>0 && *v<=300000).ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?;
        let witness=if matches!(operation,Operation::WorktreeRedoRestoreAdmin|Operation::WorktreeRedoRestoreTree|Operation::WorktreeRedoFinish) {
            Some(serde_json::from_value::<crate::engine::worktree_stage::InverseGitAdminWitness>(object.remove("witness").ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?).map_err(|_|Self::stage_error("worktree redo unavailable",audit_id))?)
        }else{None};
        let binding:Binding=serde_json::from_value(Value::Object(object)).map_err(|_|Self::stage_error("worktree redo unavailable",audit_id))?;
        let slot={let stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree redo unavailable",audit_id))?;
            let entry=stages.get("__worktree_stage__").ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?;
            if entry.owner_id!=self.identity.owner_id || entry.principal_id!=self.identity.principal_id || entry.lane!=self.identity.lane || entry.session_id!=self.identity.session_id{return Err(Self::stage_error("worktree redo unavailable",audit_id));}
            entry.worktree.clone().ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?};
        if slot.binding!=binding || slot.target!=target {return Err(Self::stage_error("worktree redo unavailable",audit_id));}
        let mut state=slot.state.lock().map_err(|_|Self::stage_error("worktree redo unavailable",audit_id))?;
        let data=match &mut *state {
            WorktreeSlotState::Redo{writer,unknown} if !*unknown=>{
                let expected=match operation {Operation::WorktreeRedoPrepareHistory=>"pending",Operation::WorktreeRedoRestoreAdmin=>"history_prepared",Operation::WorktreeRedoRestoreTree=>"admin_restored",Operation::WorktreeRedoFinish=>"tree_restored",_=>"unknown"};
                if writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_err() || writer.history_state()!=expected {*unknown=true;json!({"state":"unknown"})}
                else {let deadline=Instant::now().checked_add(Duration::from_millis(timeout)).ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?;
                    let outcome=match operation {Operation::WorktreeRedoPrepareHistory=>writer.prepare_history(&self.engine,&self.identity.app_scope,deadline),Operation::WorktreeRedoRestoreAdmin=>writer.restore_admin(&self.engine,&self.identity.app_scope,witness.as_ref().ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?,deadline),Operation::WorktreeRedoRestoreTree=>writer.restore_tree(&self.engine,&self.identity.app_scope,witness.as_ref().ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?,deadline),Operation::WorktreeRedoFinish=>writer.finish_redo(&self.engine,&self.identity.app_scope,witness.as_ref().ok_or_else(||Self::stage_error("worktree redo unavailable",audit_id))?,deadline),_=>Err(EngineError::Conflict)};
                    if outcome.is_ok(){json!({"state":match operation {Operation::WorktreeRedoRestoreAdmin=>"admin_restored",Operation::WorktreeRedoRestoreTree=>"tree_restored",Operation::WorktreeRedoFinish=>"completed",_=>"history_prepared"}})}else{*unknown=true;json!({"state":"unknown"})}}
            }, _=>json!({"state":"unknown"})};
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}))
    }

    #[cfg(target_os="linux")]
    fn worktree_release_completed(&self,target:PathBuf,payload:Value,audit_id:&str)->Result<Value,ProtocolError>{
        use crate::engine::worktree_stage::Binding;
        if self.identity.lane!=TrustLane::App{return Err(Self::stage_error("worktree release unavailable",audit_id))}
        let binding:Binding=serde_json::from_value(payload).map_err(|_|Self::stage_error("worktree release unavailable",audit_id))?;
        let slot={let stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree release unavailable",audit_id))?;
            let entry=stages.get("__worktree_stage__").ok_or_else(||Self::stage_error("worktree release unavailable",audit_id))?;
            if entry.owner_id!=self.identity.owner_id || entry.principal_id!=self.identity.principal_id || entry.lane!=self.identity.lane || entry.session_id!=self.identity.session_id {return Err(Self::stage_error("worktree release unavailable",audit_id))}
            entry.worktree.clone().ok_or_else(||Self::stage_error("worktree release unavailable",audit_id))?};
        if slot.binding!=binding || slot.target!=target{return Err(Self::stage_error("worktree release unavailable",audit_id))}
        let state=slot.state.lock().map_err(|_|Self::stage_error("worktree release unavailable",audit_id))?;
        let complete=match &*state {
            WorktreeSlotState::Active{writer,..}=>writer.is_complete() && writer.check_binding_after_publication(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_ok(),
            WorktreeSlotState::Inverse{writer,unknown}=>!*unknown && writer.history_state()=="completed" && writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_ok(),
            WorktreeSlotState::Redo{writer,unknown}=>!*unknown && writer.history_state()=="completed" && writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_ok(),_=>false};
        if !complete{return Err(Self::stage_error("worktree release unavailable",audit_id))}
        let mut stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree release unavailable",audit_id))?;
        if !stages.get("__worktree_stage__").and_then(|v|v.worktree.as_ref()).is_some_and(|current|Arc::ptr_eq(current,&slot)) {return Err(Self::stage_error("worktree release unavailable",audit_id))}
        stages.remove("__worktree_stage__");drop(stages);drop(state);
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"released"},"audit_id":audit_id}))
    }

    #[cfg(target_os="linux")]
    fn worktree_stage_phase(&self, operation:Operation, target:PathBuf, payload:Value, audit_id:&str) -> Result<Value,ProtocolError> {
        use base64::Engine as _;
        use crate::engine::worktree_stage::Binding;
        if self.identity.lane!=TrustLane::App {return Err(Self::stage_error("worktree stage unavailable",audit_id));}
        let object=payload.as_object().ok_or_else(||Self::stage_error("worktree stage unavailable",audit_id))?;
        let mut required=vec!["operation_id","request_digest","git_plan_digest","root_id","root_object_tag"];
        match operation {Operation::WorktreeStageChunk=>required.extend(["ordinal","offset","content_base64"]),Operation::WorktreeStageEnd=>required.extend(["ordinal","git_oid","sha256"]),Operation::WorktreeStageSeal|Operation::WorktreeStageStatus=>{},_=>return Err(Self::stage_error("worktree stage unavailable",audit_id))}
        if object.len()!=required.len() || required.iter().any(|k|!object.contains_key(*k)) {return Err(Self::stage_error("worktree stage unavailable",audit_id));}
        let get=|name:&str|object.get(name).and_then(Value::as_str).map(str::to_owned).ok_or_else(||Self::stage_error("worktree stage unavailable",audit_id));
        let binding=Binding{operation_id:get("operation_id")?,request_digest:get("request_digest")?,git_plan_digest:get("git_plan_digest")?,root_id:get("root_id")?,root_object_tag:get("root_object_tag")?};
        let slot={let stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree stage unavailable",audit_id))?;
            let Some(entry)=stages.get("__worktree_stage__") else{return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));};
            if entry.owner_id!=self.identity.owner_id || entry.principal_id!=self.identity.principal_id || entry.lane!=self.identity.lane || entry.session_id!=self.identity.session_id {return Err(Self::stage_error("worktree stage unavailable",audit_id));}
            entry.worktree.clone().ok_or_else(||Self::stage_error("worktree stage unavailable",audit_id))?};
        if slot.binding!=binding || slot.target!=target {return Err(Self::stage_error("worktree stage unavailable",audit_id));}
        let mut state=slot.state.lock().map_err(|_|Self::stage_error("worktree stage unavailable",audit_id))?;
        if operation==Operation::WorktreeStageStatus {
            let invalid=matches!(&*state,WorktreeSlotState::Starting) || matches!(&*state,WorktreeSlotState::Active{writer,..} if (if writer.has_published(){writer.check_binding_after_publication(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target)}else{writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target)}).is_err());
            if invalid {*state=WorktreeSlotState::Unknown;}
            if let WorktreeSlotState::Inverse{writer,unknown}=&mut *state {if writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_err(){*unknown=true;}}
            if let WorktreeSlotState::Redo{writer,unknown}=&mut *state {if writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_err(){*unknown=true;}}
            let data=match &*state {WorktreeSlotState::Starting=>json!({"state":"status","stage":"pending"}),WorktreeSlotState::Unknown=>json!({"state":"status","stage":"unknown"}),WorktreeSlotState::Inverse{writer,unknown}=>{
                let stage=if *unknown || writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_err(){"unknown"}else{writer.history_state()};json!({"state":"status","stage":stage})},WorktreeSlotState::Redo{writer,unknown}=>{
                let stage=if *unknown || writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_err(){"unknown"}else{writer.history_state()};json!({"state":"status","stage":stage})},WorktreeSlotState::Active{sealed_digest,..}=>{
                if let Some(digest)=sealed_digest {json!({"state":"status","stage":"sealed","target_manifest_sha256":digest})}else{json!({"state":"status","stage":"pending"})}}};
            return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}));
        }
        let data=(|| -> Result<Value,()> {
            let WorktreeSlotState::Active{writer,sealed_digest,..}=&mut *state else{return Err(())};
            if sealed_digest.is_some() || writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&target).is_err(){return Err(())}
            match operation {
                Operation::WorktreeStageChunk=>{let ordinal=object.get("ordinal").and_then(Value::as_u64).and_then(|n|usize::try_from(n).ok()).ok_or(())?;let offset=object.get("offset").and_then(Value::as_u64).ok_or(())?;let text=object.get("content_base64").and_then(Value::as_str).ok_or(())?;
                    if text.len()>174764{return Err(())}let bytes=base64::engine::general_purpose::STANDARD.decode(text).map_err(|_|())?;
                    if bytes.is_empty() || bytes.len()>128*1024 || base64::engine::general_purpose::STANDARD.encode(&bytes)!=text{return Err(())}
                    writer.write_chunk(ordinal,offset,&bytes).map_err(|_|())?;let (next_ordinal,next_offset,_)=writer.cursor();Ok(json!({"state":"accepted","next_ordinal":next_ordinal,"next_offset":next_offset}))},
                Operation::WorktreeStageEnd=>{let ordinal=object.get("ordinal").and_then(Value::as_u64).and_then(|n|usize::try_from(n).ok()).ok_or(())?;let oid=object.get("git_oid").and_then(Value::as_str).ok_or(())?;let sha=object.get("sha256").and_then(Value::as_str).ok_or(())?;
                    writer.finish_blob(ordinal,oid,sha).map_err(|_|())?;let (next_ordinal,next_offset,_)=writer.cursor();Ok(json!({"state":"accepted","next_ordinal":next_ordinal,"next_offset":next_offset}))},
                Operation::WorktreeStageSeal=>{let digest=writer.seal_target().map_err(|_|())?;*sealed_digest=Some(digest.clone());Ok(json!({"state":"sealed","target_manifest_sha256":digest}))},
                _=>Err(())
            }
        })();
        let data=match data {Ok(data)=>data,Err(())=>{*state=WorktreeSlotState::Unknown;json!({"state":"unknown"})}};
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}))
    }

    #[cfg(target_os="linux")]
    fn worktree_private_phase(&self,operation:Operation,target:PathBuf,payload:Value,audit_id:&str)->Result<Value,ProtocolError> {
        use crate::engine::worktree_stage::{Binding,RegistrationWitness,PublicationLockWitness,BacklinkCommittedWitness,PrivateFileWitness};
        if self.identity.lane!=TrustLane::App {return Err(Self::stage_error("worktree stage unavailable",audit_id));}
        let object=payload.as_object().ok_or_else(||Self::stage_error("worktree stage unavailable",audit_id))?;
        let mut required=vec!["operation_id","request_digest","git_plan_digest","root_id","root_object_tag"];
        match operation {Operation::WorktreePrivatePrepare=>required.extend(["destination_parent_object_tag","destination_name"]),Operation::WorktreePrivateAccept=>{required.push("phase");match object.get("phase").and_then(Value::as_str){Some("registered")=>required.push("witness"),Some("before_index")=>required.push("target_oid"),Some("index_ready")=>required.push("index"),_=>{}}},Operation::WorktreePrivateMaterialize|Operation::WorktreePrivatePrepareHistory|Operation::WorktreePrivateBeginPublication|Operation::WorktreePrivateCheckPublished=>required.push("timeout_ms"),Operation::WorktreePrivatePublish|Operation::WorktreePrivateFinishPublication=>required.extend(["timeout_ms","witness"]),_=>return Err(Self::stage_error("worktree stage unavailable",audit_id))}
        if object.len()!=required.len() || required.iter().any(|key|!object.contains_key(*key)){return Err(Self::stage_error("worktree stage unavailable",audit_id));}
        let get=|name:&str|object.get(name).and_then(Value::as_str).map(str::to_owned).ok_or_else(||Self::stage_error("worktree stage unavailable",audit_id));
        let binding=Binding{operation_id:get("operation_id")?,request_digest:get("request_digest")?,git_plan_digest:get("git_plan_digest")?,root_id:get("root_id")?,root_object_tag:get("root_object_tag")?};
        let slot={let stages=self.staged_uploads.0.lock().map_err(|_|Self::stage_error("worktree stage unavailable",audit_id))?;
            let Some(entry)=stages.get("__worktree_stage__") else{return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"unknown"},"audit_id":audit_id}));};
            if entry.owner_id!=self.identity.owner_id || entry.principal_id!=self.identity.principal_id || entry.lane!=self.identity.lane || entry.session_id!=self.identity.session_id{return Err(Self::stage_error("worktree stage unavailable",audit_id));}
            entry.worktree.clone().ok_or_else(||Self::stage_error("worktree stage unavailable",audit_id))?};
        if slot.binding!=binding || (matches!(operation,Operation::WorktreePrivateAccept|Operation::WorktreePrivateMaterialize|Operation::WorktreePrivatePrepareHistory|Operation::WorktreePrivateBeginPublication|Operation::WorktreePrivatePublish|Operation::WorktreePrivateCheckPublished|Operation::WorktreePrivateFinishPublication) && slot.target!=target){return Err(Self::stage_error("worktree stage unavailable",audit_id));}
        let mut state=slot.state.lock().map_err(|_|Self::stage_error("worktree stage unavailable",audit_id))?;
        let data=(||->Result<Value,()> {
            let WorktreeSlotState::Active{writer,sealed_digest,private}=&mut *state else{return Err(())};
            let bound=if matches!(operation,Operation::WorktreePrivateCheckPublished|Operation::WorktreePrivateFinishPublication){writer.check_binding_after_publication(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&slot.target)}else{writer.check_binding(&self.engine,&self.identity.app_scope,self.identity.owner_id(),&binding,&slot.target)};
            if sealed_digest.is_none() || bound.is_err(){return Err(())}
            match operation {
                Operation::WorktreePrivatePrepare=>{
                    if private.is_some(){return Err(())}
                    let parent=writer.private_destination_parent(&self.engine,&self.identity.app_scope,&target,object.get("destination_parent_object_tag").and_then(Value::as_str).ok_or(())?).map_err(|_|())?;
                    let directory=writer.create_private_directory(&parent,&target,object.get("destination_name").and_then(Value::as_str).ok_or(())?).map_err(|_|())?;
                    let meta=directory.file.metadata().map_err(|_|())?;let path=directory.path.to_str().ok_or(())?.to_owned();let journal=directory.path.parent().and_then(Path::to_str).ok_or(())?.to_owned();
                    if path.len()>4096 || journal.len()>4096{return Err(())}
                    let data=json!({"state":"ready","private_path":path,"journal_directory":journal,"private_identity":{"device":meta.dev().to_string(),"inode":meta.ino().to_string()}});*private=Some(directory);Ok(data)
                },
                Operation::WorktreePrivateBeginPublication=>{
                    let directory=private.as_ref().ok_or(())?;let timeout=object.get("timeout_ms").and_then(Value::as_u64).filter(|v|*v>0 && *v<=300000).ok_or(())?;
                    writer.begin_publication_lock(directory,&self.engine,&self.identity.app_scope,Instant::now()+Duration::from_millis(timeout)).map_err(|_|())?;Ok(json!({"state":"proceed"}))
                },
                Operation::WorktreePrivatePublish=>{
                    let directory=private.as_ref().ok_or(())?;let timeout=object.get("timeout_ms").and_then(Value::as_u64).filter(|v|*v>0 && *v<=300000).ok_or(())?;
                    let witness:PublicationLockWitness=serde_json::from_value(object.get("witness").cloned().ok_or(())?).map_err(|_|())?;
                    writer.publication_lock_ready(directory,&witness).map_err(|_|())?;writer.publish_private(directory,&self.engine,&self.identity.app_scope,Instant::now()+Duration::from_millis(timeout)).map_err(|_|())?;Ok(json!({"state":"published"}))
                },
                Operation::WorktreePrivateCheckPublished=>{
                    let directory=private.as_ref().ok_or(())?;let timeout=object.get("timeout_ms").and_then(Value::as_u64).filter(|v|*v>0 && *v<=300000).ok_or(())?;
                    writer.check_published(directory,&self.engine,&self.identity.app_scope,Instant::now()+Duration::from_millis(timeout)).map_err(|_|())?;Ok(json!({"state":"proceed"}))
                },
                Operation::WorktreePrivateFinishPublication=>{
                    let directory=private.as_ref().ok_or(())?;let timeout=object.get("timeout_ms").and_then(Value::as_u64).filter(|v|*v>0 && *v<=300000).ok_or(())?;
                    let witness:BacklinkCommittedWitness=serde_json::from_value(object.get("witness").cloned().ok_or(())?).map_err(|_|())?;
                    writer.finish_published_history(directory,&self.engine,&self.identity.app_scope,&witness,Instant::now()+Duration::from_millis(timeout)).map_err(|_|())?;Ok(json!({"state":"completed"}))
                },
                Operation::WorktreePrivatePrepareHistory=>{
                    let directory=private.as_ref().ok_or(())?;let timeout=object.get("timeout_ms").and_then(Value::as_u64).filter(|v|*v>0 && *v<=300000).ok_or(())?;
                    writer.prepare_history(directory,&self.engine,&self.identity.app_scope,Instant::now()+Duration::from_millis(timeout)).map_err(|_|())?;
                    Ok(json!({"state":"history_prepared"}))
                },
                Operation::WorktreePrivateMaterialize=>{
                    let directory=private.as_ref().ok_or(())?;let timeout=object.get("timeout_ms").and_then(Value::as_u64).filter(|v|*v>0 && *v<=300000).ok_or(())?;
                    let digest=writer.materialize_private(directory,Instant::now()+Duration::from_millis(timeout)).map_err(|_|())?;
                    if sealed_digest.as_ref()!=Some(&digest){return Err(())}Ok(json!({"state":"materialized","target_manifest_sha256":digest}))
                },
                Operation::WorktreePrivateAccept=>{
                    let directory=private.as_ref().ok_or(())?;
                    match object.get("phase").and_then(Value::as_str) {
                        Some("before_registration")=>{writer.registration_issued(directory).map_err(|_|())?;Ok(json!({"state":"proceed"}))},
                        Some("registered")=>{let witness:RegistrationWitness=serde_json::from_value(object.get("witness").cloned().ok_or(())?).map_err(|_|())?;writer.accept_registration(directory,&witness).map_err(|_|())?;Ok(json!({"state":"registered"}))},
                        Some("before_index")=>{writer.index_issued(directory,object.get("target_oid").and_then(Value::as_str).ok_or(())?).map_err(|_|())?;Ok(json!({"state":"proceed"}))},
                        Some("index_ready")=>{let index:PrivateFileWitness=serde_json::from_value(object.get("index").cloned().ok_or(())?).map_err(|_|())?;writer.accept_index(directory,&index).map_err(|_|())?;Ok(json!({"state":"registered"}))},
                        _=>Err(())
                    }
                },_=>Err(())
            }
        })();
        let data=match data {Ok(data)=>data,Err(())=>{*state=WorktreeSlotState::Unknown;json!({"state":"unknown"})}};
        Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}))
    }

    fn stage_begin(&self, target: PathBuf, audit_id: &str) -> Result<Value, ProtocolError> {
        let target_result = match self.identity.lane {
            TrustLane::App => self.engine.authorize_stage_directory_scoped(&self.identity.app_scope, &target),
            TrustLane::Agent => self.engine.authorize_stage_directory_agent(self.identity.owner_id(), &self.identity.scope(), &target),
        };
        if let Err(error) = target_result {
            return Err(engine_protocol(error, audit_id));
        }
        self.prune_stages();
        let mut stages = self.staged_uploads.0.lock().map_err(|_| Self::stage_error("staging is unavailable", audit_id))?;
        let active_bytes: u64 = stages.values().filter(|stage|!stage.is_worktree()).map(|stage| stage.length).sum();
        if stages.values().filter(|stage|!stage.is_worktree()).count() >= MAX_ACTIVE_STAGES || active_bytes >= MAX_ACTIVE_STAGE_BYTES {
            return Err(Self::stage_error("staging capacity is exhausted", audit_id));
        }
        let root = StageStore::ensure_root().map_err(|_| Self::stage_error("staging is unavailable", audit_id))?;
        let (token, path, marker_path, file) = (0..8).find_map(|_| {
            let sequence = STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let mut digest = Sha256::new();
            digest.update(self.identity.owner_id.as_bytes());
            digest.update(self.identity.session_id.as_bytes());
            digest.update(audit_id.as_bytes());
            digest.update(sequence.to_le_bytes());
            let now_ns = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|value| value.as_nanos()).unwrap_or_default();
            digest.update(now_ns.to_le_bytes());
            let token = format!("fs-stage-{:x}", digest.finalize());
            let path = root.join(format!("{token}.data"));
            let marker_path = root.join(format!("{token}.json"));
            let mut options = OpenOptions::new();
            options.read(true).write(true).create_new(true);
            #[cfg(unix)]
            options.mode(0o600);
            match options.open(&path) {
                Ok(file) => Some((token, path, marker_path, file)),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => None,
                Err(_) => None,
            }
        }).ok_or_else(|| Self::stage_error("staging is unavailable", audit_id))?;
        #[cfg(unix)]
        if fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).is_err() {
            let _ = fs::remove_file(&path);
            return Err(Self::stage_error("staging is unavailable", audit_id));
        }
        let marker = StageMarker {
            version: 1,
            stage_id: token.clone(),
            data_name: format!("{token}.data"),
            owner_id: self.identity.owner_id.clone(),
            principal_id: self.identity.principal_id.clone(),
            lane: self.identity.lane,
            session_id: self.identity.session_id.clone(),
            created_unix_ms: std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|value| value.as_millis() as u64).unwrap_or_default(),
        };
        let marker_bytes = serde_json::to_vec(&marker).map_err(|_| Self::stage_error("staging is unavailable", audit_id))?;
        let mut marker_options = OpenOptions::new();
        marker_options.write(true).create_new(true);
        #[cfg(unix)]
        marker_options.mode(0o600);
        let mut marker_file = marker_options.open(&marker_path)
            .map_err(|_| { let _ = fs::remove_file(&path); Self::stage_error("staging is unavailable", audit_id) })?;
        if marker_file.write_all(&marker_bytes).and_then(|_| marker_file.sync_all()).is_err() {
            let _ = fs::remove_file(&path);
            let _ = fs::remove_file(&marker_path);
            return Err(Self::stage_error("staging is unavailable", audit_id));
        }
        let stage = StagedUpload {
            #[cfg(target_os="linux")] worktree:None,
            owner_id: self.identity.owner_id.clone(),
            principal_id: self.identity.principal_id.clone(),
            lane: self.identity.lane,
            session_id: self.identity.session_id.clone(),
            path: path.clone(),
            marker_path: Some(marker_path),
            file,
            length: 0,
            digest: Sha256::new(),
            created: Instant::now(),
        };
        stages.insert(token.clone(), stage);
        let remaining_active_bytes = MAX_ACTIVE_STAGE_BYTES.saturating_sub(active_bytes);
        let max_total_bytes = (self.engine.config().max_write_bytes as u64).min(remaining_active_bytes);
        if max_total_bytes == 0 {
            if let Some(stage) = stages.remove(&token) { StageStore::remove_stage_files(&stage); }
            return Err(Self::stage_error("staging capacity is exhausted", audit_id));
        }
        Ok(json!({"protocol": crate::PROTOCOL_VERSION, "data": {"stage_id": token, "max_chunk_bytes": MAX_STAGE_CHUNK_BYTES, "max_total_bytes": max_total_bytes}, "audit_id": audit_id}))
    }

    fn stage_chunk(&self, args: StageChunkArgs, audit_id: &str) -> Result<Value, ProtocolError> {
        use base64::Engine as _;
        self.prune_stages();
        let mut stages = self.staged_uploads.0.lock().map_err(|_| Self::stage_error("staging is unavailable", audit_id))?;
        let active_bytes: u64 = stages.values().filter(|entry|!entry.is_worktree()).map(|entry| entry.length).sum();
        let stage = stages.get_mut(&args.stage_id).ok_or_else(|| Self::stage_error("staging handle is unavailable", audit_id))?;
        if !self.stage_matches(stage) {
            return Err(Self::stage_error("staging handle is invalid", audit_id));
        }
        if args.offset != stage.length {
            drop(stages); self.discard_stage(&args.stage_id);
            return Err(Self::stage_error("staging offset is invalid", audit_id));
        }
        let bytes = match base64::engine::general_purpose::STANDARD.decode(args.chunk.as_bytes()) {
            Ok(bytes) => bytes,
            Err(_) => { drop(stages); self.discard_stage(&args.stage_id); return Err(Self::stage_error("staging chunk is invalid", audit_id)); }
        };
        if bytes.len() > MAX_STAGE_CHUNK_BYTES {
            drop(stages); self.discard_stage(&args.stage_id);
            return Err(Self::stage_error("staging chunk exceeds the bound", audit_id));
        }
        if stage.length.saturating_add(bytes.len() as u64) > self.engine.config().max_write_bytes as u64 || active_bytes.saturating_add(bytes.len() as u64) > MAX_ACTIVE_STAGE_BYTES {
            drop(stages); self.discard_stage(&args.stage_id);
            return Err(engine_protocol(EngineError::LimitExceeded, audit_id));
        }
        if stage.file.write_all(&bytes).is_err() {
            drop(stages); self.discard_stage(&args.stage_id);
            return Err(Self::stage_error("staging write failed", audit_id));
        }
        stage.digest.update(&bytes);
        stage.length += bytes.len() as u64;
        Ok(json!({"protocol": crate::PROTOCOL_VERSION, "data": {"stage_id": args.stage_id, "offset": stage.length}, "audit_id": audit_id}))
    }

    fn stage_finish(&self, args: StageFinishArgs, audit_id: &str) -> Result<Value, ProtocolError> {
        self.prune_stages();
        let mut stages = self.staged_uploads.0.lock().map_err(|_| Self::stage_error("staging is unavailable", audit_id))?;
        let owned = stages.get(&args.stage_id).map(|stage| self.stage_matches(stage)).unwrap_or(false);
        if !owned {
            return Err(Self::stage_error("staging handle is invalid", audit_id));
        }
        let mut stage = stages.remove(&args.stage_id).expect("owned stage disappeared while locked");
        drop(stages);
        if stage.length != args.length
            || format!("sha256:{:x}", stage.digest.clone().finalize()) != args.digest
        {
            StageStore::remove_stage_files(&stage);
            return Err(Self::stage_error("staging digest or length is invalid", audit_id));
        }
        if let Err(error)=self.engine.validate_graph_stage(&PathBuf::from(&args.target), &args.digest, args.length) {
            StageStore::remove_stage_files(&stage);
            return serialize_engine::<crate::engine::ReplaceOutcome>(Err(error), audit_id);
        }
        if stage.file.sync_all().is_err() {
            StageStore::remove_stage_files(&stage);
            return Err(Self::stage_error("staging write failed", audit_id));
        }
        if let Some(wire) = args.expected_fingerprint {
            let result = (|| {
                if self.identity.lane != TrustLane::App || self.engine.graph_capture_active() {
                    return Err(Self::stage_error("staged replacement unavailable for this lane", audit_id));
                }
                let expected = wire.into_fingerprint().map_err(|_| Self::stage_error("invalid replacement fingerprint", audit_id))?;
                let mut bytes = Vec::with_capacity(stage.length as usize);
                stage.file.rewind().and_then(|_| (&mut stage.file).take(stage.length + 1).read_to_end(&mut bytes))
                    .map_err(|_| Self::stage_error("staged replacement unavailable", audit_id))?;
                if bytes.len() as u64 != stage.length || format!("sha256:{:x}", Sha256::digest(&bytes)) != args.digest {
                    return Err(Self::stage_error("staged replacement changed", audit_id));
                }
                serialize_engine(self.engine.replace_if_fingerprint_scoped(&self.identity.app_scope,
                    Path::new(&args.target), Some(&expected), &bytes), audit_id)
            })();
            StageStore::remove_stage_files(&stage);
            return result;
        }
        let result = match self.identity.lane {
            TrustLane::App => self.engine.create_file_from_staging_scoped(&self.identity.app_scope, &PathBuf::from(args.target), &mut stage.file, stage.length),
            TrustLane::Agent => self.engine.create_file_from_staging(self.identity.owner_id(), &self.identity.scope(), &PathBuf::from(args.target), &mut stage.file, stage.length),
        };
        if let Err(ref error) = result {
            if matches!(error, EngineError::DestinationExists) {
                if let Ok(mut stages) = self.staged_uploads.0.lock() {
                    stages.insert(args.stage_id, stage);
                }
            } else {
                StageStore::remove_stage_files(&stage);
            }
            return serialize_engine(result, audit_id);
        }
        StageStore::remove_stage_files(&stage);
        serialize_engine(result, audit_id)
    }

    fn stage_abort(&self, args: StageAbortArgs, audit_id: &str) -> Result<Value, ProtocolError> {
        self.prune_stages();
        let mut stages = self.staged_uploads.0.lock().map_err(|_| Self::stage_error("staging is unavailable", audit_id))?;
        let owned = stages.get(&args.stage_id).map(|stage| self.stage_matches(stage)).unwrap_or(false);
        if !owned && stages.contains_key(&args.stage_id) {
            return Err(Self::stage_error("staging handle is invalid", audit_id));
        }
        let stage = stages.remove(&args.stage_id);
        if let Some(stage) = stage {
            StageStore::remove_stage_files(&stage);
        }
        Ok(json!({"protocol": crate::PROTOCOL_VERSION, "data": {"aborted": true}, "audit_id": audit_id}))
    }

    fn resolve_file_handle(
        &self,
        handle: &crate::ResourceHandle,
        audit_id: &str,
    ) -> Result<(File, PathBuf, Metadata), ProtocolError> {
        let (file, path, expected) = {
            let table = self
                .handles
                .lock()
                .map_err(|_| self.stale_handle(audit_id))?;
            let entry = table
                .entries
                .get(&handle.token)
                .ok_or_else(|| self.stale_handle(audit_id))?;
            if &entry.handle != handle
                || entry.owner_id != self.identity.owner_id
                || entry.principal_id != self.identity.principal_id
                || entry.lane != self.identity.lane
                || entry.session_id != self.identity.session_id
                || handle.generation != self.scope_generation()
            {
                return Err(self.stale_handle(audit_id));
            }
            (
                entry
                    .file
                    .try_clone()
                    .map_err(|_| self.stale_handle(audit_id))?,
                entry.path.clone(),
                entry.identity.clone(),
            )
        };

        // Re-run policy authorization on every use, then require the pathname
        // and the already-open descriptor to still name the same object.
        let authorized = match self.identity.lane {
            TrustLane::App => self
                .engine
                .stat_scoped(&self.identity.app_scope, &path, false),
            TrustLane::Agent => self.engine.stat(
                self.identity.owner_id(),
                &self.identity.scope(),
                &path,
                false,
            ),
        };
        authorized.map_err(|_| self.stale_handle(audit_id))?;
        if fs::canonicalize(&path).map_err(|_| self.stale_handle(audit_id))? != path {
            return Err(self.stale_handle(audit_id));
        }
        #[cfg(windows)]
        crate::windows_fs::validate_opened_path(&file, &path).map_err(|_| self.stale_handle(audit_id))?;
        let descriptor_metadata = file.metadata().map_err(|_| self.stale_handle(audit_id))?;
        if ObjectIdentity::from_path(&path).map_err(|_| self.stale_handle(audit_id))? != expected
            || ObjectIdentity::from_file(&file).map_err(|_| self.stale_handle(audit_id))? != expected
        { return Err(self.stale_handle(audit_id)); }
        Ok((file, path, descriptor_metadata))
    }

    fn stat_handle(
        &self,
        handle: &crate::ResourceHandle,
        include_fingerprint: bool,
        audit_id: &str,
    ) -> Result<Value, ProtocolError> {
        let (mut file, path, metadata) = self.resolve_file_handle(handle, audit_id)?;
        let fingerprint = include_fingerprint
            .then(|| fingerprint_descriptor(&mut file))
            .transpose()
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        Ok(json!({
            "protocol": crate::PROTOCOL_VERSION,
            "data": {
                "path": path,
                "kind": crate::engine::FileKind::File,
                "size": metadata.len(),
                "modified_unix_ms": metadata_modified_ms(&metadata),
                "mode": metadata_mode(&metadata),
                "fingerprint": fingerprint,
            },
            "audit_id": audit_id,
        }))
    }

    fn read_handle(
        &self,
        handle: &crate::ResourceHandle,
        args: ReadRangeArgs,
        audit_id: &str,
    ) -> Result<Value, ProtocolError> {
        if args.length > self.engine.config().max_read_bytes {
            return Err(engine_protocol(EngineError::LimitExceeded, audit_id));
        }
        let (mut file, path, metadata) = self.resolve_file_handle(handle, audit_id)?;
        file.seek(SeekFrom::Start(args.offset))
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        let mut bytes = vec![0_u8; args.length];
        let read = file
            .read(&mut bytes)
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        bytes.truncate(read);
        let end = args.offset.saturating_add(read as u64);
        let eof = end >= metadata.len();
        let fingerprint = args
            .include_fingerprint
            .then(|| fingerprint_descriptor(&mut file))
            .transpose()
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        Ok(json!({
            "protocol": crate::PROTOCOL_VERSION,
            "data": {
                "path": path,
                "offset": args.offset,
                "bytes": bytes,
                "next_offset": if eof { Value::Null } else { json!(end) },
                "eof": eof,
                "fingerprint": fingerprint,
                "work": {
                    "entries_visited": 0,
                    "bytes_read": read,
                    "hashes_computed": if args.include_fingerprint { 1 } else { 0 },
                    "cache_hits": 0,
                },
            },
            "audit_id": audit_id,
        }))
    }

    fn bind_request(
        &self,
        request: ClientRequest,
    ) -> Result<(Operation, RequestEnvelope, String), ProtocolError> {
        let audit_id = format!("audit:{}", request.request_id);
        let operation = request.operation;
        let target = request.target.clone();
        let context = match self.identity.lane {
            TrustLane::App => RequestContext::app(
                self.identity.owner_id.clone(),
                self.identity.principal_id.clone(),
                request.request_id.clone(),
                request.session_id.clone(),
                request.root_operation_id.clone(),
                operation,
                target.clone(),
                audit_id.clone(),
            )
            .with_app_scope(self.identity.app_scope.clone()),
            TrustLane::Agent => RequestContext::agent(
                self.identity.owner_id.clone(),
                self.identity.principal_id.clone(),
                request.request_id.clone(),
                request.session_id.clone(),
                request.root_operation_id.clone(),
                operation,
                target,
                audit_id.clone(),
            )
            .with_scope(
                self.identity.approved_root_ids.iter().cloned(),
                self.identity.capabilities.iter().copied(),
                self.identity
                    .active_folder
                    .as_ref()
                    .map(|folder| folder.id.clone()),
            ),
        };
        let envelope = context.bind(request)?;
        Ok((operation, envelope, audit_id))
    }

    pub fn render_thumbnail(
        &self,
        request: ClientRequest,
        cancellation: ThumbnailCancellationToken,
    ) -> Result<ThumbnailPng, ProtocolError> {
        let (operation, envelope, audit_id) = self.bind_request(request)?;
        if operation != Operation::Thumbnail {
            return Err(ProtocolError::new(
                ProtocolErrorCode::MalformedRequest,
                "native thumbnail RPC requires a thumbnail operation",
                Some(audit_id),
            ));
        }
        let args: ThumbnailArgs = serde_json::from_value(envelope.payload).map_err(|_| {
            ProtocolError::new(
                ProtocolErrorCode::MalformedRequest,
                "thumbnail payload is invalid",
                Some(audit_id.clone()),
            )
        })?;
        if !(1..=2_000).contains(&args.deadline_ms) || !(1_000..=3_000).contains(&args.scale_milli)
        {
            return Err(ProtocolError::new(
                ProtocolErrorCode::MalformedRequest,
                "thumbnail scale or deadline is outside the bounded contract",
                Some(audit_id),
            ));
        }
        let handle = match &envelope.target {
            Target::Handle(handle) => handle,
            Target::Path(_) => {
                return Err(ProtocolError::new(
                    ProtocolErrorCode::MalformedRequest,
                    "native thumbnails require a stable resource handle",
                    Some(audit_id),
                ))
            }
        };
        let (file, path, _metadata) = self.resolve_file_handle(handle, &audit_id)?;
        let broker = (if args.icon { self.native_icon_broker.as_ref() } else { self.thumbnail_broker.as_ref() }).ok_or_else(|| {
            ProtocolError::new(
                ProtocolErrorCode::Unsupported,
                "native content thumbnails are unavailable on this host",
                Some(audit_id.clone()),
            )
        })?;
        let cache_key = ThumbnailCacheKey {
            object_tag: ObjectIdentity::from_file(&file).map_err(|_| self.stale_handle(&audit_id))?.opaque_tag(),
            icon: args.icon,
            policy_generation: self.thumbnail_policy_generation.current(),
            width: args.width,
            height: args.height,
            scale_milli: args.scale_milli,
        };
        if let Some(cached) = self
            .thumbnail_cache
            .lock()
            .map_err(|_| {
                ProtocolError::new(
                    ProtocolErrorCode::Crashed,
                    "native thumbnail cache is unavailable",
                    Some(audit_id.clone()),
                )
            })?
            .get(&cache_key)
        {
            return Ok(cached);
        }
        let request = ThumbnailRequest::new(
            args.width,
            args.height,
            f64::from(args.scale_milli) / 1_000.0,
        )
        .with_deadline(Duration::from_millis(args.deadline_ms));
        let result = match self.identity.lane {
            TrustLane::App => broker.render_app(
                &self.engine,
                &self.identity.app_scope,
                &path,
                &self.thumbnail_policy_generation,
                request,
                cancellation,
            ),
            TrustLane::Agent => broker.render_agent(
                &self.engine,
                self.identity.owner_id(),
                &self.identity.scope(),
                &path,
                &self.thumbnail_policy_generation,
                request,
                cancellation,
            ),
        };
        let png = result.map_err(|error| thumbnail_protocol(error, &audit_id))?;
        self.thumbnail_cache
            .lock()
            .map_err(|_| {
                ProtocolError::new(
                    ProtocolErrorCode::Crashed,
                    "native thumbnail cache is unavailable",
                    Some(audit_id.clone()),
                )
            })?
            .insert(cache_key, png.clone());
        Ok(png)
    }

    #[cfg(target_os = "linux")]
    fn checkpoint_open(&self, binding: &crate::CheckpointBinding, path: &Path, audit_id: &str) -> Result<(File,ObjectIdentity,(u64,u64)), ProtocolError> {
        use std::os::unix::fs::MetadataExt;
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        use std::path::Component;
        let relative=Path::new(&binding.relative_path);
        let parts:Vec<_>=relative.components().collect();
        if parts.is_empty() || parts.iter().any(|part|!matches!(part,Component::Normal(_)))
            || binding.canonical_root.join(relative)!=path {return Err(self.stale_handle(audit_id));}
        let (_,_,authorized)=self.authorized_open_identity(path,audit_id)?;
        let mut parent=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)
            .open(&binding.canonical_root).map_err(|_|self.stale_handle(audit_id))?;
        let root_metadata=parent.metadata().map_err(|_|self.stale_handle(audit_id))?;
        let root_identity=(root_metadata.dev(),root_metadata.ino());
        for (index,part) in parts.iter().enumerate() {
            let Component::Normal(name)=part else {return Err(self.stale_handle(audit_id));};
            let name=CString::new(name.as_bytes()).map_err(|_|self.stale_handle(audit_id))?;
            let flags=libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK
                | if index+1==parts.len(){0}else{libc::O_DIRECTORY};
            // SAFETY: parent and NUL-terminated name remain live during openat.
            let fd=unsafe {libc::openat(parent.as_raw_fd(),name.as_ptr(),flags)};
            if fd<0{return Err(self.stale_handle(audit_id));}
            // SAFETY: openat returned a fresh owned descriptor.
            let opened=unsafe {File::from_raw_fd(fd)};
            if index+1==parts.len() {
                let metadata=opened.metadata().map_err(|_|self.stale_handle(audit_id))?;
                let identity=ObjectIdentity::from_file(&opened).map_err(|_|self.stale_handle(audit_id))?;
                if !metadata.is_file() || identity!=authorized {return Err(self.stale_handle(audit_id));}
                return Ok((opened,identity,root_identity));
            }
            parent=opened;
        }
        Err(self.stale_handle(audit_id))
    }

    pub fn dispatch(&self, request: ClientRequest) -> Result<Value, ProtocolError> {
        let (operation, envelope, audit_id) = self.bind_request(request)?;
        let mutation = matches!(operation, Operation::Create | Operation::Replace | Operation::OrdinaryReplace | Operation::OrdinaryCreate | Operation::Patch
            | Operation::Copy | Operation::Move | Operation::ManagedFileMove | Operation::ManagedFileCleanup | Operation::ManagedFolderMove | Operation::ManagedFolderCleanup | Operation::Rename | Operation::Mkdir
            | Operation::Trash | Operation::Restore | Operation::StageFinish | Operation::CheckoutPrepare | Operation::CheckoutStep | Operation::CheckoutRollback | Operation::CheckoutFinalize);
        if self.engine.graph_capture_active() && mutation && !matches!(operation, Operation::StageFinish) {
            return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "graph capture requires staged create", Some(audit_id)));
        }
        // A hook's last action belongs to this operation only while mutations
        // are serialized. Read-only requests never attach stale recovery status.
        let _receipt_guard = if mutation {
            Some(self.mutation_receipt_lock.lock().map_err(|_| ProtocolError::new(
                ProtocolErrorCode::RootUnavailable, "mutation receipt lock unavailable", Some(audit_id.clone())))?)
        } else { None };
        let before_action = if mutation { self.last_capture_status().map(|s| s.action_id) } else { None };
        let result = (|| { match operation {
            Operation::Health => Ok(json!({
                "protocol": crate::PROTOCOL_VERSION,
                "lane": self.identity.lane,
                "owner_id": self.identity.owner_id,
                "service_session_id": self.identity.session_id,
                "ready": true,
            })),
            Operation::Capabilities => {
                let mut capabilities = json!({
                "protocol": crate::PROTOCOL_VERSION,
                "lane": self.identity.lane,
                "operations": ["health", "capabilities", "list_directory", "stat", "stat_object_identity", "open_handle", "read_range", "read_lines", "read_text_preview", "create", "stage_begin", "stage_chunk", "stage_finish", "stage_abort", "replace", "patch", "copy", "move", "rename", "mkdir", "trash", "restore", "filename_search", "content_search"],
                "native_thumbnails": self.native_thumbnails_available(),
                "native_icons": self.native_icon_broker.is_some(),
                "max_page_size": self.engine.config().directory_page_size,
                "max_read_bytes": self.engine.config().max_read_bytes,
                "search": {"hit_unit": "file", "content_modes": ["literal"],
                    "filename_modes": ["filename", "glob"], "glob_syntax": ["*", "**", "?"],
                    "resumable": false, "max_results": 200, "max_entries": 10000,
                    "max_depth": 32, "max_bytes_per_file": 1048576},
                });
                if cfg!(target_os = "linux") {
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("strict_snapshot_directory_v1"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("snapshot_file_hash"));
                }
                if MANAGED_FILE_MOVE_READY && self.engine.managed_file_move_ready() && self.identity.lane == TrustLane::App {
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("managed_file_move"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("managed_file_move_status"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("managed_file_cleanup"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("managed_folder_inspect"));
                }
                if MANAGED_FOLDER_MOVE_READY && self.engine.managed_file_move_ready() && self.identity.lane == TrustLane::App {
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("managed_folder_move"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("managed_folder_move_status"));
                }
                if ORDINARY_CREATE_READY && self.engine.ordinary_write_ready() {
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("ordinary_create"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("ordinary_create_status"));
                }
                if self.engine.ordinary_write_ready() {
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("ordinary_replace"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("ordinary_replace_status"));
                }
                if cfg!(target_os="linux") && self.engine.checkpoint_ready() {
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("register_checkpoint_file"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("checkpoint_prepare"));
                    capabilities["operations"].as_array_mut().expect("static operations array").push(json!("checkpoint_finalize"));
                }
                Ok(capabilities)
            },
            Operation::ListDirectory => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: ListDirectoryArgs = serde_json::from_value(envelope.payload.clone())
                    .map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "list_directory payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                let cursor = args.cursor;
                if args.strict_snapshot {
                    if cursor.is_some() || args.sort.is_some() {return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest,"strict snapshot has no cursor or sort",Some(audit_id)));}
                    #[cfg(target_os = "linux")]
                    {return self.strict_snapshot_directory(&path,args.limit.unwrap_or(1024),&audit_id);}
                    #[cfg(not(target_os = "linux"))]
                    {return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"strict snapshot listing unavailable",Some(audit_id)));}
                }
                let max_page_size = self.engine.config().directory_page_size;
                let limit = args.limit.unwrap_or(max_page_size);
                if limit == 0 || limit > max_page_size {
                    return Err(ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        format!("list_directory limit must be between 1 and {max_page_size}"),
                        Some(audit_id),
                    ));
                }
                let result = match self.identity.lane {
                    TrustLane::App => match args.sort.as_ref() {
                        Some(sort) => self.engine.list_directory_scoped_sorted_with_limit(
                            &self.identity.app_scope,
                            &path,
                            cursor.as_ref(),
                            sort,
                            limit,
                        ),
                        None => self.engine.list_directory_scoped_with_limit(
                            &self.identity.app_scope,
                            &path,
                            cursor.as_ref(),
                            limit,
                        ),
                    },
                    TrustLane::Agent => match args.sort.as_ref() {
                        Some(sort) => self.engine.list_directory_sorted_with_limit(
                            self.identity.owner_id(),
                            &self.identity.scope(),
                            &path,
                            cursor.as_ref(),
                            sort,
                            limit,
                        ),
                        None => self.engine.list_directory_with_limit(
                            self.identity.owner_id(),
                            &self.identity.scope(),
                            &path,
                            cursor.as_ref(),
                            limit,
                        ),
                    },
                };
                serialize_engine(result, &audit_id)
            }
            Operation::OpenHandle => {
                let path = target_path(&envelope.target, &audit_id)?;
                self.open_handle(&path, &audit_id)
            }
            Operation::RegisterCheckpointFile => {
                #[cfg(target_os="linux")]
                {
                    let args:CheckpointRegistrationArgs=serde_json::from_value(envelope.payload).map_err(|_|self.stale_handle(&audit_id))?;
                    let path=target_path(&envelope.target,&audit_id)?;
                    let root=self.engine.checkpoint_root(&args.root_id).map_err(|_|self.stale_handle(&audit_id))?;
                    let relative=path.strip_prefix(&root).map_err(|_|self.stale_handle(&audit_id))?;
                    let relative=relative.to_str().filter(|value|!value.is_empty()).ok_or_else(||self.stale_handle(&audit_id))?;
                    let binding=crate::CheckpointBinding {resource_id:String::new(),root_id:args.root_id.clone(),workspace_id:String::new(),registry_generation:0,relative_path:relative.into(),staged_upload_id:String::new(),canonical_root:root};
                    let (mut file,identity,root_identity)=self.checkpoint_open(&binding,&path,&audit_id)?;
                    if identity.size>16*1024*1024 {return Err(self.stale_handle(&audit_id));}
                    let deadline=Instant::now()+std::time::Duration::from_secs(30);
                    let mut digest=Sha256::new();let mut size=0u64;let mut buf=[0u8;65536];
                    loop {if Instant::now()>=deadline{return Err(self.stale_handle(&audit_id));}
                        let n=file.read(&mut buf).map_err(|_|self.stale_handle(&audit_id))?;if n==0{break;}
                        size=size.checked_add(n as u64).ok_or_else(||self.stale_handle(&audit_id))?;
                        if size>identity.size{return Err(self.stale_handle(&audit_id));}digest.update(&buf[..n]);}
                    if size!=identity.size || format!("sha256:{:x}:{}",digest.finalize(),size)!=args.expected_fingerprint
                        || ObjectIdentity::from_file(&file).map_err(|_|self.stale_handle(&audit_id))?!=identity {return Err(self.stale_handle(&audit_id));}
                    let (_,before,before_root)=self.checkpoint_open(&binding,&path,&audit_id)?;
                    if before!=identity || before_root!=root_identity {return Err(self.stale_handle(&audit_id));}
                    let result=self.engine.register_checkpoint_file(&path,&args.root_id).map_err(|_|self.stale_handle(&audit_id))?;
                    let (_,after,after_root)=self.checkpoint_open(&binding,&path,&audit_id)?;
                    if after!=identity || after_root!=root_identity || ObjectIdentity::from_file(&file).map_err(|_|self.stale_handle(&audit_id))?!=identity {return Err(self.stale_handle(&audit_id));}
                    return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":result,"audit_id":audit_id}));
                }
                #[cfg(not(target_os="linux"))]
                {return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"registration unavailable",Some(audit_id)));}
            }
            Operation::CheckpointPrepare => {
                #[cfg(target_os = "linux")]
                {
                    let args:CheckpointPrepareArgs=serde_json::from_value(envelope.payload).map_err(|_|self.stale_handle(&audit_id))?;
                    if args.items.is_empty() || args.items.len()>16 || args.request_id.len()<16 || args.request_id.len()>80
                        || !args.request_id.bytes().all(|c|c.is_ascii_alphanumeric() || c==b'-' || c==b'_') {return Err(self.stale_handle(&audit_id));}
                    let mut slot=self.checkpoint_prepared.lock().map_err(|_|self.stale_handle(&audit_id))?;
                    if slot.as_ref().is_some_and(|value|Instant::now()>=value.deadline) {if let Some(old)=slot.take(){old.ticket.fail();}}
                    if slot.is_some() {return Err(self.stale_handle(&audit_id));}
                    let deadline=Instant::now()+Duration::from_secs(30);
                    let selection:Vec<_>=args.items.iter().map(|item|crate::CheckpointSelection {resource_id:item.resource_id.clone(),expected_fingerprint:item.expected_fingerprint.clone()}).collect();
                    let mut ticket=self.engine.claim_checkpoint(&args.request_id,&args.root_id,&selection,deadline).map_err(|_|self.stale_handle(&audit_id))?;
                    let started=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|_|self.stale_handle(&audit_id))?.as_millis() as u64;
                    let build=(|| -> Result<(Vec<CheckpointPinned>,u64),ProtocolError> {
                        let bindings=ticket.bindings().to_vec();
                        if bindings.len()!=args.items.len(){return Err(self.stale_handle(&audit_id));}
                        let mut files=Vec::with_capacity(bindings.len());let mut total=0u64;
                        for (index,(input,binding)) in args.items.iter().zip(bindings).enumerate() {
                            if Instant::now()>=deadline || input.resource_id!=binding.resource_id || input.path.len()>4096 {return Err(self.stale_handle(&audit_id));}
                            let path=PathBuf::from(&input.path);let (mut file,identity,root_identity)=self.checkpoint_open(&binding,&path,&audit_id)?;
                            if files.first().is_some_and(|first: &CheckpointPinned|first.root_identity!=root_identity) {return Err(self.stale_handle(&audit_id));}
                            if identity.size>16*1024*1024 {return Err(self.stale_handle(&audit_id));}
                            total=total.checked_add(identity.size).ok_or_else(||self.stale_handle(&audit_id))?;
                            if total>64*1024*1024 {return Err(self.stale_handle(&audit_id));}
                            let mut bytes=Vec::with_capacity(identity.size as usize);let mut buffer=[0u8;65536];
                            loop {if Instant::now()>=deadline{return Err(self.stale_handle(&audit_id));}
                                let count=file.read(&mut buffer).map_err(|_|self.stale_handle(&audit_id))?;if count==0{break;}
                                if bytes.len().saturating_add(count)>identity.size as usize{return Err(self.stale_handle(&audit_id));}bytes.extend_from_slice(&buffer[..count]);}
                            let fingerprint=format!("sha256:{:x}:{}",Sha256::digest(&bytes),bytes.len());
                            if bytes.len() as u64!=identity.size || fingerprint!=input.expected_fingerprint
                                || ObjectIdentity::from_file(&file).map_err(|_|self.stale_handle(&audit_id))?!=identity {return Err(self.stale_handle(&audit_id));}
                            ticket.stage(index,&bytes,deadline).map_err(|_|self.stale_handle(&audit_id))?;
                            let (_,current,current_root)=self.checkpoint_open(&binding,&path,&audit_id)?;
                            if current!=identity || current_root!=root_identity{return Err(self.stale_handle(&audit_id));}
                            files.push(CheckpointPinned {path,binding,file,identity,root_identity});
                        }
                        for item in &files {let (_,current,current_root)=self.checkpoint_open(&item.binding,&item.path,&audit_id)?;
                            if current!=item.identity || current_root!=item.root_identity || ObjectIdentity::from_file(&item.file).map_err(|_|self.stale_handle(&audit_id))?!=item.identity {return Err(self.stale_handle(&audit_id));}}
                        let finished=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|_|self.stale_handle(&audit_id))?.as_millis() as u64;
                        if Instant::now()>=deadline{return Err(self.stale_handle(&audit_id));} Ok((files,finished))
                    })();
                    let (files,finished)=match build {Ok(value)=>value,Err(error)=>{ticket.fail();return Err(error);}};
                    let mut random=[0u8;32];if getrandom::fill(&mut random).is_err(){ticket.fail();return Err(self.stale_handle(&audit_id));}
                    let selector:String=random.iter().map(|byte|format!("{byte:02x}")).collect();
                    *slot=Some(PreparedCheckpoint {selector:selector.clone(),ticket,files,deadline,started_millis:started,finished_millis:finished});
                    return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"state":"prepared","selector":selector},"audit_id":audit_id}));
                }
                #[cfg(not(target_os = "linux"))]
                {return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"checkpoint unavailable",Some(audit_id)));}
            }
            Operation::CheckpointFinalize => {
                #[cfg(target_os = "linux")]
                {
                    let args:CheckpointFinalizeArgs=serde_json::from_value(envelope.payload).map_err(|_|self.stale_handle(&audit_id))?;
                    let mut slot=self.checkpoint_prepared.lock().map_err(|_|self.stale_handle(&audit_id))?;
                    if slot.as_ref().is_none_or(|value|value.selector!=args.selector) {return Err(self.stale_handle(&audit_id));}
                    let prepared=slot.take().ok_or_else(||self.stale_handle(&audit_id))?;
                    if Instant::now()>=prepared.deadline {prepared.ticket.fail();return Err(self.stale_handle(&audit_id));}
                    for item in &prepared.files {
                        let current=self.checkpoint_open(&item.binding,&item.path,&audit_id);
                        if current.as_ref().is_err() || current.ok().is_none_or(|(_,identity,root_identity)|identity!=item.identity || root_identity!=item.root_identity)
                            || ObjectIdentity::from_file(&item.file).map_or(true,|identity|identity!=item.identity) {
                            prepared.ticket.fail();return Err(self.stale_handle(&audit_id));
                        }
                    }
                    let result=prepared.ticket.finalize(prepared.started_millis,prepared.finished_millis,prepared.deadline).map_err(|_|self.stale_handle(&audit_id))?;
                    return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":result,"audit_id":audit_id}));
                }
                #[cfg(not(target_os = "linux"))]
                {return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"checkpoint unavailable",Some(audit_id)));}
            }
            Operation::SnapshotFileHash => {
                #[cfg(target_os = "linux")]
                {
                    let path=target_path(&envelope.target,&audit_id)?;
                    let args:SnapshotHashArgs=serde_json::from_value(envelope.payload).map_err(|_| ProtocolError::new(ProtocolErrorCode::MalformedRequest,"snapshot hash payload invalid",Some(audit_id.clone())))?;
                    if args.max_bytes==0 || args.max_bytes>1024*1024*1024 || args.max_ms==0 || args.max_ms>30000 {return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest,"snapshot hash bounds invalid",Some(audit_id)));}
                    let (canonical,mut file,before)=self.authorized_open_identity(&path,&audit_id)?;
                    if before.size>args.max_bytes {return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"snapshot file exceeds bound",Some(audit_id)));}
                    let deadline=Instant::now()+Duration::from_millis(args.max_ms);let mut total=0u64;let mut digest=Sha256::new();let mut buffer=[0u8;65536];
                    loop {if Instant::now()>=deadline{return Err(self.stale_handle(&audit_id));}let n=file.read(&mut buffer).map_err(|_| self.stale_handle(&audit_id))?;if n==0{break;}total=total.checked_add(n as u64).ok_or_else(||self.stale_handle(&audit_id))?;if total>before.size || total>args.max_bytes{return Err(self.stale_handle(&audit_id));}digest.update(&buffer[..n]);}
                    let (_,_,current)=self.authorized_open_identity(&path,&audit_id)?;
                    if Instant::now()>=deadline || total!=before.size || ObjectIdentity::from_file(&file).map_err(|_| self.stale_handle(&audit_id))?!=before || current!=before{return Err(self.stale_handle(&audit_id));}
                    return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":{"path":canonical,"size":total,"sha256":format!("{:x}",digest.finalize()),"object_tag":before.opaque_tag()},"audit_id":audit_id}));
                }
                #[cfg(not(target_os = "linux"))]
                {return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"snapshot hash unavailable",Some(audit_id)));}
            }
            Operation::Thumbnail => Err(ProtocolError::new(
                ProtocolErrorCode::Unsupported,
                "native thumbnails require the bounded Tonic thumbnail RPC",
                Some(audit_id),
            )),
            Operation::Stat => {
                let args: StatArgs = serde_json::from_value(envelope.payload).map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "stat payload is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                match &envelope.target {
                    Target::Handle(handle) => {
                        if args.object_identity { return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "identity stat requires a path", Some(audit_id))); }
                        self.stat_handle(handle, args.include_fingerprint, &audit_id)
                    }
                    Target::Path(path) if args.object_identity => {
                        if args.include_fingerprint {
                            return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "identity stat cannot request a fingerprint", Some(audit_id)));
                        }
                        let (canonical, file, identity) = self.authorized_open_identity(Path::new(path), &audit_id)?;
                        let metadata = file.metadata().map_err(|_| self.stale_handle(&audit_id))?;
                        Ok(json!({"protocol": crate::PROTOCOL_VERSION, "data": {"path":canonical, "kind":crate::engine::FileKind::File, "size":metadata.len(), "modified_unix_ms":metadata_modified_ms(&metadata), "object_tag":identity.opaque_tag()}, "audit_id":audit_id}))
                    }
                    Target::Path(path) => {
                        let path = PathBuf::from(path);
                        let result = match self.identity.lane {
                            TrustLane::App => self.engine.stat_scoped(
                                &self.identity.app_scope,
                                &path,
                                args.include_fingerprint,
                            ),
                            TrustLane::Agent => self.engine.stat(
                                self.identity.owner_id(),
                                &self.identity.scope(),
                                &path,
                                args.include_fingerprint,
                            ),
                        };
                        serialize_engine(result, &audit_id)
                    }
                }
            }
            Operation::ReadRange => {
                let args: ReadRangeArgs =
                    serde_json::from_value(envelope.payload).map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "read_range payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                match &envelope.target {
                    Target::Handle(handle) => self.read_handle(handle, args, &audit_id),
                    Target::Path(path) => {
                        let path = PathBuf::from(path);
                        let result = match self.identity.lane {
                            TrustLane::App => self.engine.read_range_scoped(
                                &self.identity.app_scope,
                                &path,
                                args.offset,
                                args.length,
                                args.include_fingerprint,
                            ),
                            TrustLane::Agent => self.engine.read_range(
                                self.identity.owner_id(),
                                &self.identity.scope(),
                                &path,
                                args.offset,
                                args.length,
                                args.include_fingerprint,
                            ),
                        };
                        serialize_engine(result, &audit_id)
                    }
                }
            }
            Operation::ReadLines => {
                let path = target_path(&envelope.target, &audit_id)?;
                let result = match self.identity.lane {
                    TrustLane::App => self
                        .engine
                        .read_text_snapshot_scoped(&self.identity.app_scope, &path),
                    TrustLane::Agent => self.engine.read_text_snapshot(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::ReadTextPreview => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: ReadTextPreviewArgs =
                    serde_json::from_value(envelope.payload).map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "read_text_preview payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.read_text_preview_scoped(
                        &self.identity.app_scope,
                        &path,
                        args.max_bytes,
                    ),
                    TrustLane::Agent => self.engine.read_text_preview(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        args.max_bytes,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::StageBegin => {
                let path = target_path(&envelope.target, &audit_id)?;
                self.stage_begin(path, &audit_id)
            }
            Operation::StageChunk => {
                let args: StageChunkArgs = serde_json::from_value(envelope.payload).map_err(|_| Self::stage_error("staging chunk is invalid", &audit_id))?;
                self.stage_chunk(args, &audit_id)
            }
            Operation::StageFinish => {
                let args: StageFinishArgs = serde_json::from_value(envelope.payload).map_err(|_| Self::stage_error("staging finish is invalid", &audit_id))?;
                self.stage_finish(args, &audit_id)
            }
            Operation::StageAbort => {
                let args: StageAbortArgs = serde_json::from_value(envelope.payload).map_err(|_| Self::stage_error("staging abort is invalid", &audit_id))?;
                self.stage_abort(args, &audit_id)
            }
            Operation::Create => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: CreateArgs = serde_json::from_value(envelope.payload).map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "create payload is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let bytes = args.bytes.or_else(|| args.text.map(|text| text.into_bytes())).unwrap_or_default();
                let result = match self.identity.lane {
                    TrustLane::App => {
                        self.engine
                            .create_file_scoped(&self.identity.app_scope, &path, &bytes)
                    }
                    TrustLane::Agent => self.engine.create_file(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        &bytes,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::Replace => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: ReplaceArgs = serde_json::from_value(envelope.payload).map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "replace payload is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let expected = args.expected().map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "replace fingerprint is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let bytes = args
                    .bytes
                    .or_else(|| args.text.map(|text| text.into_bytes()))
                    .ok_or_else(|| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "replace requires bytes or text",
                            Some(audit_id.clone()),
                        )
                    })?;
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.replace_if_fingerprint_scoped(
                        &self.identity.app_scope,
                        &path,
                        expected.as_ref(),
                        &bytes,
                    ),
                    TrustLane::Agent => self.engine.replace_if_fingerprint(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        expected.as_ref(),
                        &bytes,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::OrdinaryCreate | Operation::OrdinaryCreateStatus => {
                if !ORDINARY_CREATE_READY || !self.engine.ordinary_write_ready() { return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable,"ordinary create unavailable",Some(audit_id))); }
                #[cfg(not(target_os="linux"))]
                { return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable,"ordinary create unavailable",Some(audit_id))); }
                #[cfg(target_os="linux")]
                {
                    let path=target_path(&envelope.target,&audit_id)?;
                    let args:OrdinaryCreateArgs=serde_json::from_value(envelope.payload).map_err(|_|ProtocolError::new(ProtocolErrorCode::MalformedRequest,"ordinary create identity invalid",Some(audit_id.clone())))?;
                    if args.binding.operation_id!=envelope.context.root_operation_id() {return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest,"ordinary create identity changed",Some(audit_id)));}
                    let status=operation==Operation::OrdinaryCreateStatus;
                    if status && args.bytes.is_some() {return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest,"status must not include content",Some(audit_id)));}
                    if status {match self.identity.lane {
                        TrustLane::App=>serialize_engine(self.engine.ordinary_create_status_scoped(&self.identity.app_scope,&path,&args.binding),&audit_id),
                        TrustLane::Agent=>serialize_engine(self.engine.ordinary_create_status_agent(self.identity.owner_id(),&self.identity.scope(),&path,&args.binding),&audit_id),
                    }} else {
                        let bytes=args.bytes.ok_or_else(||ProtocolError::new(ProtocolErrorCode::MalformedRequest,"ordinary create content unavailable",Some(audit_id.clone())))?;
                        match self.identity.lane {
                            TrustLane::App=>serialize_engine(self.engine.ordinary_create_scoped(&self.identity.app_scope,&path,&args.binding,&bytes),&audit_id),
                            TrustLane::Agent=>serialize_engine(self.engine.ordinary_create_agent(self.identity.owner_id(),&self.identity.scope(),&path,&args.binding,&bytes),&audit_id),
                        }
                    }
                }
            }
            Operation::OrdinaryReplace | Operation::OrdinaryReplaceStatus => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: OrdinaryReplaceArgs = serde_json::from_value(envelope.payload).map_err(|_| ProtocolError::new(
                    ProtocolErrorCode::MalformedRequest, "ordinary write identity invalid", Some(audit_id.clone())))?;
                if args.binding.operation_id != envelope.context.root_operation_id() {
                    return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "ordinary write identity changed", Some(audit_id)));
                }
                if operation == Operation::OrdinaryReplaceStatus {
                    if args.bytes.is_some() { return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "status must not include content", Some(audit_id))); }
                    match self.identity.lane {
                        TrustLane::App=>serialize_engine(self.engine.ordinary_replace_status_scoped(&self.identity.app_scope,&path,&args.binding),&audit_id),
                        TrustLane::Agent=>serialize_engine(self.engine.ordinary_replace_status_agent(self.identity.owner_id(),&self.identity.scope(),&path,&args.binding),&audit_id),
                    }
                } else {
                    let bytes = args.bytes.ok_or_else(|| ProtocolError::new(ProtocolErrorCode::MalformedRequest, "ordinary write content unavailable", Some(audit_id.clone())))?;
                    match self.identity.lane {
                        TrustLane::App=>serialize_engine(self.engine.ordinary_replace_scoped(&self.identity.app_scope,&path,&args.binding,&bytes),&audit_id),
                        TrustLane::Agent=>serialize_engine(self.engine.ordinary_replace_agent(self.identity.owner_id(),&self.identity.scope(),&path,&args.binding,&bytes),&audit_id),
                    }
                }
            }
            Operation::Patch => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: PatchArgs = serde_json::from_value(envelope.payload).map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "patch payload is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let expected = args.expected().map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "patch fingerprint is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.edit_text_scoped(
                        &self.identity.app_scope,
                        &path,
                        expected.as_ref(),
                        &args.old,
                        &args.new,
                        args.replace_all,
                    ),
                    TrustLane::Agent => self.engine.edit_text(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        expected.as_ref(),
                        &args.old,
                        &args.new,
                        args.replace_all,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::Copy => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: DestinationArgs =
                    serde_json::from_value(envelope.payload).map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "copy payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                let destination = std::path::PathBuf::from(args.destination);
                let result = match self.identity.lane {
                    TrustLane::App => {
                        self.engine
                            .copy_file_scoped(&self.identity.app_scope, &path, &destination)
                    }
                    TrustLane::Agent => self.engine.copy_file(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        &destination,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::WorktreeLookup | Operation::WorktreeReleaseCompleted | Operation::WorktreeRedoBegin | Operation::WorktreeRedoPrepareHistory | Operation::WorktreeRedoRestoreAdmin | Operation::WorktreeRedoRestoreTree | Operation::WorktreeRedoFinish | Operation::WorktreeInverseBegin | Operation::WorktreeInversePrepareHistory | Operation::WorktreeInverseStageHistory | Operation::WorktreeInverseVerify | Operation::WorktreeInverseQuarantine | Operation::WorktreeInverseRelocateAdmin | Operation::WorktreeInverseFinish | Operation::WorktreeStageBegin | Operation::WorktreeStageChunk | Operation::WorktreeStageEnd | Operation::WorktreeStageSeal | Operation::WorktreeStageStatus | Operation::WorktreePrivatePrepare | Operation::WorktreePrivateAccept | Operation::WorktreePrivateMaterialize | Operation::WorktreePrivatePrepareHistory | Operation::WorktreePrivateBeginPublication | Operation::WorktreePrivatePublish | Operation::WorktreePrivateCheckPublished | Operation::WorktreePrivateFinishPublication => {
                #[cfg(target_os="linux")]
                {
                    let target=target_path(&envelope.target,&audit_id)?;
                    if operation==Operation::WorktreeLookup {
                        if self.identity.lane!=TrustLane::App{return Err(Self::stage_error("worktree lookup unavailable",&audit_id))}
                        let args:crate::engine::worktree_stage::WorktreeLookup=serde_json::from_value(envelope.payload).map_err(|_|Self::stage_error("worktree lookup unavailable",&audit_id))?;
                        if args.operation_id!=envelope.context.root_operation_id() || args.root_path!=target{return Err(Self::stage_error("worktree lookup unavailable",&audit_id))}
                        let data=self.engine.worktree_lookup(&self.identity.app_scope,self.identity.owner_id(),&args,Instant::now()+Duration::from_secs(10)).map_err(|_|Self::stage_error("worktree lookup unavailable",&audit_id))?;
                        return Ok(json!({"protocol":crate::PROTOCOL_VERSION,"data":data,"audit_id":audit_id}));
                    }
                    if operation==Operation::WorktreeReleaseCompleted {
                        if envelope.payload.get("operation_id").and_then(Value::as_str)!=Some(envelope.context.root_operation_id()){return Err(Self::stage_error("worktree release unavailable",&audit_id))}
                        return self.worktree_release_completed(target,envelope.payload,&audit_id);
                    }
                    if operation==Operation::WorktreeRedoBegin {
                        let args:crate::engine::worktree_stage::RedoBegin=serde_json::from_value(envelope.payload).map_err(|_|Self::stage_error("worktree redo unavailable",&audit_id))?;
                        if args.operation_id!=envelope.context.root_operation_id(){return Err(Self::stage_error("worktree redo unavailable",&audit_id));}
                        return self.worktree_redo_begin(target,args,&audit_id);
                    }
                    if matches!(operation,Operation::WorktreeRedoPrepareHistory|Operation::WorktreeRedoRestoreAdmin|Operation::WorktreeRedoRestoreTree|Operation::WorktreeRedoFinish) {
                        if envelope.payload.get("operation_id").and_then(Value::as_str)!=Some(envelope.context.root_operation_id()){return Err(Self::stage_error("worktree redo unavailable",&audit_id));}
                        return self.worktree_redo_phase(operation,target,envelope.payload,&audit_id);
                    }
                    if operation==Operation::WorktreeInverseBegin {
                        let args:crate::engine::worktree_stage::InverseBegin=serde_json::from_value(envelope.payload).map_err(|_|Self::stage_error("worktree inverse unavailable",&audit_id))?;
                        if args.operation_id!=envelope.context.root_operation_id(){return Err(Self::stage_error("worktree inverse unavailable",&audit_id));}
                        return self.worktree_inverse_begin(target,args,&audit_id);
                    }
                    if matches!(operation,Operation::WorktreeInversePrepareHistory|Operation::WorktreeInverseStageHistory|Operation::WorktreeInverseVerify|Operation::WorktreeInverseQuarantine|Operation::WorktreeInverseRelocateAdmin|Operation::WorktreeInverseFinish) {
                        if envelope.payload.get("operation_id").and_then(Value::as_str)!=Some(envelope.context.root_operation_id()){return Err(Self::stage_error("worktree inverse unavailable",&audit_id));}
                        return self.worktree_inverse_phase(operation,target,envelope.payload,&audit_id);
                    }
                    if operation==Operation::WorktreeStageBegin {
                        let args:crate::engine::worktree_stage::Begin=serde_json::from_value(envelope.payload).map_err(|_|Self::stage_error("worktree stage unavailable",&audit_id))?;
                        if args.operation_id!=envelope.context.root_operation_id(){return Err(Self::stage_error("worktree stage unavailable",&audit_id));}
                        return self.worktree_stage_begin(target,args,&audit_id);
                    }
                    if envelope.payload.get("operation_id").and_then(Value::as_str)!=Some(envelope.context.root_operation_id()){return Err(Self::stage_error("worktree stage unavailable",&audit_id));}
                    if matches!(operation,Operation::WorktreePrivatePrepare|Operation::WorktreePrivateAccept|Operation::WorktreePrivateMaterialize|Operation::WorktreePrivatePrepareHistory|Operation::WorktreePrivateBeginPublication|Operation::WorktreePrivatePublish|Operation::WorktreePrivateCheckPublished|Operation::WorktreePrivateFinishPublication) {self.worktree_private_phase(operation,target,envelope.payload,&audit_id)} else {self.worktree_stage_phase(operation,target,envelope.payload,&audit_id)}
                }
                #[cfg(not(target_os="linux"))] {Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"worktree stage unavailable",Some(audit_id)))}
            }
            Operation::CheckoutPrepare | Operation::CheckoutStep | Operation::CheckoutStatus | Operation::CheckoutRollback | Operation::CheckoutFinalize | Operation::CheckoutProbeChild => {
                if self.identity.lane != TrustLane::App || !self.engine.managed_file_move_ready() {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable,"checkout owner unavailable",Some(audit_id)));
                }
                #[cfg(target_os="linux")]
                {
                    let target=target_path(&envelope.target,&audit_id)?;
                    if operation==Operation::CheckoutProbeChild {
                        let args:crate::engine::checkout::Probe=serde_json::from_value(envelope.payload).map_err(|_|ProtocolError::new(ProtocolErrorCode::MalformedRequest,"checkout probe payload invalid",Some(audit_id.clone())))?;
                        return serialize_engine(self.engine.checkout_probe(&self.identity.app_scope,&target,&args),&audit_id);
                    }
                    if operation==Operation::CheckoutPrepare {
                        let args:crate::engine::checkout::Prepare=serde_json::from_value(envelope.payload).map_err(|_|ProtocolError::new(ProtocolErrorCode::MalformedRequest,"checkout payload invalid",Some(audit_id.clone())))?;
                        if args.operation_id!=envelope.context.root_operation_id() || args.root_path!=target {return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest,"checkout identity mismatch",Some(audit_id)))}
                        return serialize_engine(self.engine.checkout_prepare(&self.identity.app_scope,self.identity.owner_id(),&args),&audit_id);
                    }
                    let args:crate::engine::checkout::Phase=serde_json::from_value(envelope.payload).map_err(|_|ProtocolError::new(ProtocolErrorCode::MalformedRequest,"checkout phase invalid",Some(audit_id.clone())))?;
                    if args.operation_id!=envelope.context.root_operation_id(){return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest,"checkout identity mismatch",Some(audit_id)))}
                    self.engine.checkout_target_matches(&self.identity.app_scope,self.identity.owner_id(),&args,&target).map_err(|error|engine_protocol(error,&audit_id))?;
                    let result=match operation {
                        Operation::CheckoutStep=>self.engine.checkout_step(&self.identity.app_scope,self.identity.owner_id(),&args),
                        Operation::CheckoutStatus=>self.engine.checkout_status(&self.identity.app_scope,self.identity.owner_id(),&args),
                        Operation::CheckoutRollback=>self.engine.checkout_rollback(&self.identity.app_scope,self.identity.owner_id(),&args),
                        Operation::CheckoutFinalize=>self.engine.checkout_finalize(&self.identity.app_scope,self.identity.owner_id(),&args),
                        _=>unreachable!(),
                    };
                    serialize_engine(result,&audit_id)
                }
                #[cfg(not(target_os="linux"))]
                { Err(ProtocolError::new(ProtocolErrorCode::Unsupported,"checkout owner unavailable on this platform",Some(audit_id))) }
            }
            Operation::ManagedFolderInspect => {
                if !MANAGED_FILE_MOVE_READY || !self.engine.managed_file_move_ready() || self.identity.lane != TrustLane::App {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed folder inspection is unavailable", Some(audit_id)));
                }
                let path = target_path(&envelope.target, &audit_id)?;
                match self.engine.inspect_managed_folder_app(&self.identity.app_scope, &path) {
                    Err(EngineError::Io(error)) if error.kind() == std::io::ErrorKind::Unsupported => Err(ProtocolError::new(ProtocolErrorCode::Unsupported, "folder contains unsupported entries", Some(audit_id))),
                    Err(EngineError::LimitExceeded) => Err(ProtocolError::new(ProtocolErrorCode::Backpressure, "folder exceeds the supported move bound", Some(audit_id))),
                    result => serialize_engine(result, &audit_id),
                }
            }
            Operation::ManagedFolderMoveStatus => {
                if !MANAGED_FOLDER_MOVE_READY || !self.engine.managed_file_move_ready() || self.identity.lane != TrustLane::App {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed folder status is unavailable", Some(audit_id)));
                }
                let args: crate::engine::ManagedFolderRequest = serde_json::from_value(envelope.payload).map_err(|_| ProtocolError::new(
                    ProtocolErrorCode::MalformedRequest, "managed folder payload is invalid", Some(audit_id.clone())))?;
                let path = target_path(&envelope.target, &audit_id)?;
                if args.operation_id != envelope.context.root_operation_id() || args.source != path {
                    return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "managed folder identity does not match the authenticated request", Some(audit_id)));
                }
                serialize_engine(self.engine.observe_managed_folder_app(&self.identity.app_scope, self.identity.owner_id(), &args), &audit_id)
            }
            Operation::ManagedFolderMove => {
                if !MANAGED_FOLDER_MOVE_READY || !self.engine.managed_file_move_ready() || self.identity.lane != TrustLane::App {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed folder moves are unavailable", Some(audit_id)));
                }
                let args: crate::engine::ManagedFolderRequest = serde_json::from_value(envelope.payload).map_err(|_| ProtocolError::new(
                    ProtocolErrorCode::MalformedRequest, "managed folder payload is invalid", Some(audit_id.clone())))?;
                let path = target_path(&envelope.target, &audit_id)?;
                if args.operation_id != envelope.context.root_operation_id() || args.source != path {
                    return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "managed folder identity does not match the authenticated request", Some(audit_id)));
                }
                match self.engine.run_managed_folder_app(&self.identity.app_scope, self.identity.owner_id(), &args) {
                    Err(EngineError::HistoryCapture(_)) => Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable,
                        "managed folder outcome requires reconciliation", Some(audit_id))),
                    result => serialize_engine(result, &audit_id),
                }
            }
            Operation::ManagedFileCleanup => {
                if !MANAGED_FILE_MOVE_READY || !self.engine.managed_file_move_ready() || self.identity.lane != TrustLane::App {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed file cleanup is unavailable", Some(audit_id)));
                }
                let mut args: ManagedFileMoveArgs = serde_json::from_value(envelope.payload).map_err(|_| ProtocolError::new(
                    ProtocolErrorCode::MalformedRequest, "managed file cleanup payload is invalid", Some(audit_id.clone())))?;
                resolve_managed_undo(&self.engine, self.identity.owner_id(), &mut args, &audit_id)?;
                let root = target_path(&envelope.target, &audit_id)?;
                if args.operation_id != envelope.context.root_operation_id() || args.participants.iter().filter(|item| matches!(item.kind, crate::engine::ManagedParticipantKind::Document)).count() != 1 {
                    return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "managed cleanup identity is invalid", Some(audit_id)));
                }
                match self.engine.cleanup_managed_file_app(&self.identity.app_scope, self.identity.owner_id(), &args.operation_id, &root, &args.participants, &args.guards, args.directory_guard.as_ref()) {
                    Err(_) => Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed file cleanup requires reconciliation", Some(audit_id))),
                    result => serialize_engine(result, &audit_id),
                }
            }
            Operation::ManagedFolderCleanup => {
                if !MANAGED_FOLDER_MOVE_READY || !self.engine.managed_file_move_ready() || self.identity.lane != TrustLane::App {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed folder cleanup is unavailable", Some(audit_id)));
                }
                if !envelope.payload.as_object().is_some_and(|payload| payload.is_empty()) {
                    return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "managed folder cleanup payload is invalid", Some(audit_id)));
                }
                let root = target_path(&envelope.target, &audit_id)?;
                match self.engine.cleanup_managed_folder_app(&self.identity.app_scope, self.identity.owner_id(), envelope.context.root_operation_id(), &root) {
                    Err(_) => Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed folder cleanup requires reconciliation", Some(audit_id))),
                    result => serialize_engine(result, &audit_id),
                }
            }
            Operation::ManagedFileMoveStatus => {
                if !MANAGED_FILE_MOVE_READY || !self.engine.managed_file_move_ready() || self.identity.lane != TrustLane::App {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed move status is unavailable", Some(audit_id)));
                }
                let mut args: ManagedFileMoveArgs = serde_json::from_value(envelope.payload).map_err(|_| ProtocolError::new(
                    ProtocolErrorCode::MalformedRequest, "managed move payload is invalid", Some(audit_id.clone())))?;
                resolve_managed_undo(&self.engine, self.identity.owner_id(), &mut args, &audit_id)?;
                let path = target_path(&envelope.target, &audit_id)?;
                let documents: Vec<_> = args.participants.iter().filter(|item| matches!(item.kind, crate::engine::ManagedParticipantKind::Document)).collect();
                if args.operation_id != envelope.context.root_operation_id() || documents.len() != 1 || documents[0].source != path {
                    return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "managed move identity does not match the authenticated request", Some(audit_id)));
                }
                serialize_engine(self.engine.observe_managed_files_app(&self.identity.app_scope, self.identity.owner_id(), &args.operation_id, &args.participants, &args.guards, args.directory_guard.as_ref()), &audit_id)
            }
            Operation::ManagedFileMove => {
                if !MANAGED_FILE_MOVE_READY || !self.engine.managed_file_move_ready() || self.identity.lane != TrustLane::App {
                    return Err(ProtocolError::new(ProtocolErrorCode::RootUnavailable, "managed file moves are unavailable", Some(audit_id)));
                }
                let mut args: ManagedFileMoveArgs = serde_json::from_value(envelope.payload).map_err(|_| ProtocolError::new(
                    ProtocolErrorCode::MalformedRequest, "managed move payload is invalid", Some(audit_id.clone())))?;
                resolve_managed_undo(&self.engine, self.identity.owner_id(), &mut args, &audit_id)?;
                let path = target_path(&envelope.target, &audit_id)?;
                let documents: Vec<_> = args.participants.iter().filter(|item| matches!(item.kind, crate::engine::ManagedParticipantKind::Document)).collect();
                if args.operation_id != envelope.context.root_operation_id() || documents.len() != 1 || documents[0].source != path {
                    return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "managed move identity does not match the authenticated request", Some(audit_id)));
                }
                serialize_engine(self.engine.run_managed_files_app(&self.identity.app_scope, self.identity.owner_id(), &args.operation_id, &args.participants, &args.guards, args.directory_guard.as_ref(), args.inverse_of.as_deref()), &audit_id)
            }
            Operation::Move => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: DestinationArgs =
                    serde_json::from_value(envelope.payload).map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "move payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                let expected = args.expected().map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "move fingerprint is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let destination = std::path::PathBuf::from(args.destination);
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.move_file_scoped_if_fingerprint(
                        &self.identity.app_scope,
                        &path,
                        &destination,
                        expected.as_ref(),
                    ),
                    TrustLane::Agent => self.engine.move_file_if_fingerprint(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        &destination,
                        expected.as_ref(),
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::Rename => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: DestinationArgs =
                    serde_json::from_value(envelope.payload).map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "rename payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                let destination = std::path::PathBuf::from(args.destination);
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.rename_file_scoped(
                        &self.identity.app_scope,
                        &path,
                        &destination,
                    ),
                    TrustLane::Agent => self.engine.rename_file(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        &destination,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::Mkdir => {
                let path = target_path(&envelope.target, &audit_id)?;
                let result = match self.identity.lane {
                    TrustLane::App => self
                        .engine
                        .make_directory_scoped(&self.identity.app_scope, &path),
                    TrustLane::Agent => self.engine.make_directory(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::Trash => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: MutationFingerprintArgs = serde_json::from_value(envelope.payload)
                    .map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "trash payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                let expected = args.expected().map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "trash fingerprint is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.trash_file_scoped_if_fingerprint(
                        &self.identity.app_scope,
                        &path,
                        expected.as_ref(),
                    ),
                    TrustLane::Agent => self.engine.trash_file_if_fingerprint(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        expected.as_ref(),
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::Restore => {
                let args: RestoreArgs = serde_json::from_value(envelope.payload).map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "restore payload is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let expected = args.expected().map_err(|_| {
                    ProtocolError::new(
                        ProtocolErrorCode::MalformedRequest,
                        "restore fingerprint is invalid",
                        Some(audit_id.clone()),
                    )
                })?;
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.restore_file_scoped_if_fingerprint(
                        &self.identity.app_scope,
                        &args.entry,
                        expected.as_ref(),
                    ),
                    TrustLane::Agent => self.engine.restore_file_if_fingerprint(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &args.entry,
                        expected.as_ref(),
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            Operation::FilenameSearch | Operation::ContentSearch => {
                let path = target_path(&envelope.target, &audit_id)?;
                let args: SearchOptionsWire =
                    serde_json::from_value(envelope.payload).map_err(|_| {
                        ProtocolError::new(
                            ProtocolErrorCode::MalformedRequest,
                            "search payload is invalid",
                            Some(audit_id.clone()),
                        )
                    })?;
                if args.cursor.as_ref().is_some_and(|value| !value.is_null() && value.as_u64() != Some(0)) {
                    return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,
                        "search is bounded and nonresumable; nonzero cursors are unsupported", Some(audit_id.clone())));
                }
                if !matches!(args.mode.as_str(), "literal" | "filename" | "glob")
                    || (operation == Operation::ContentSearch && args.mode != "literal") {
                    return Err(ProtocolError::new(ProtocolErrorCode::Unsupported,
                        "search mode is unsupported", Some(audit_id.clone())));
                }
                let (options, details) = args.into_options();
                let content = operation == Operation::ContentSearch;
                let result = match self.identity.lane {
                    TrustLane::App => self.engine.app_search_with_details(
                        &self.identity.app_scope, &path, &options, &details, content,
                    ),
                    TrustLane::Agent => self.engine.agent_search_with_details(
                        self.identity.owner_id(), &self.identity.scope(), &path, &options, &details, content,
                    ),
                };
                serialize_engine(result, &audit_id)
            }
            _ => Err(ProtocolError::new(
                ProtocolErrorCode::Unsupported,
                "operation is not implemented by this service build",
                Some(audit_id),
            )),
        }
        })();
        result.map(|mut response| {
            #[cfg(any(target_os = "linux", windows))]
            { response["native_thumbnails"] = json!(self.native_thumbnails_available()); response["native_icons"] = json!(self.native_icon_broker.is_some()); }
            if mutation {
                if let Some(status) = self.last_capture_status() {
                    if before_action.as_deref() != Some(status.action_id.as_str()) {
                        response["live_status"] = json!("Committed");
                        response["history"] = json!({"action_id": status.action_id,
                            "status": status.status, "phase": status.phase, "error": status.error,
                            "durable": status.status == "complete",
                            "message": match status.status.as_str() { "complete" => "Saved", "skipped" => "Saved. Backup skipped by History settings.", _ => "Saved; recovery copy unavailable" }});
                    }
                }
            }
            response
        })
    }
}

impl Drop for FileService {
    fn drop(&mut self) {
        if let Ok(slot)=self.checkpoint_prepared.get_mut() {
            if let Some(prepared)=slot.take() { prepared.ticket.fail(); }
        }
        self.staged_uploads.remove_session(&self.identity);
    }
}

#[derive(Debug, Default, Deserialize)]
struct ListDirectoryArgs {
    #[serde(default)]
    strict_snapshot: bool,
    #[serde(default)]
    cursor: Option<crate::Cursor>,
    #[serde(default)]
    sort: Option<crate::DirectorySort>,
    #[serde(default)]
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SnapshotHashArgs { max_bytes:u64, max_ms:u64 }

#[derive(Debug, Deserialize)]
struct ReadRangeArgs {
    #[serde(default)]
    offset: u64,
    length: usize,
    #[serde(default)]
    include_fingerprint: bool,
}

#[derive(Debug, Deserialize)]
struct ReadTextPreviewArgs {
    max_bytes: usize,
}

#[derive(Debug, Deserialize)]
struct StatArgs {
    #[serde(default)]
    include_fingerprint: bool,
    #[serde(default)]
    object_identity: bool,
}

#[derive(Debug, Deserialize)]
struct ThumbnailArgs {
    #[serde(default)]
    icon: bool,
    width: u32,
    height: u32,
    scale_milli: u32,
    #[serde(default = "default_thumbnail_deadline_ms")]
    deadline_ms: u64,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct WatchSubscribeArgs {
    #[serde(default)]
    recursive: bool,
}

fn default_thumbnail_deadline_ms() -> u64 {
    2_000
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedFileMoveArgs {
    operation_id: String,
    #[serde(default)]
    participants: Vec<crate::engine::ManagedParticipant>,
    #[serde(default)]
    guards: Vec<crate::engine::ManagedGuard>,
    #[serde(default)]
    directory_guard: Option<crate::engine::ManagedDirectoryGuard>,
    /// The forward operation this one undoes. The participants are then derived from that operation's journal and must not be sent.
    #[serde(default)]
    inverse_of: Option<String>,
}

#[derive(Debug, Deserialize)]
struct DestinationArgs {
    destination: String,
    #[serde(default)]
    expected_fingerprint: Option<FingerprintWire>,
}

#[derive(Debug, Deserialize)]
struct CreateArgs {
    #[serde(default)]
    bytes: Option<Vec<u8>>,
    #[serde(default)]
    text: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StageChunkArgs { stage_id: String, chunk: String, offset: u64 }

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StageFinishArgs { stage_id: String, target: String, length: u64, digest: String, expected_fingerprint: Option<FingerprintWire> }

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StageAbortArgs { stage_id: String }

#[derive(Debug, Deserialize)]
struct RestoreArgs {
    entry: TrashEntry,
    #[serde(default)]
    expected_fingerprint: Option<FingerprintWire>,
}

#[derive(Debug, Default, Deserialize)]
struct MutationFingerprintArgs {
    #[serde(default)]
    expected_fingerprint: Option<FingerprintWire>,
}

#[derive(Debug, Deserialize)]
struct FingerprintWire {
    algorithm: String,
    value: String,
}

impl FingerprintWire {
    fn into_fingerprint(self) -> Result<Fingerprint, ()> {
        if self.algorithm != "sha256" || self.value.is_empty() {
            return Err(());
        }
        Ok(Fingerprint {
            algorithm: "sha256",
            value: self.value,
        })
    }
}

fn expected_fingerprint(value: &Option<FingerprintWire>) -> Result<Option<Fingerprint>, ()> {
    value
        .as_ref()
        .map(|value| {
            FingerprintWire {
                algorithm: value.algorithm.clone(),
                value: value.value.clone(),
            }
            .into_fingerprint()
        })
        .transpose()
}

impl DestinationArgs {
    fn expected(&self) -> Result<Option<Fingerprint>, ()> {
        expected_fingerprint(&self.expected_fingerprint)
    }
}

impl RestoreArgs {
    fn expected(&self) -> Result<Option<Fingerprint>, ()> {
        expected_fingerprint(&self.expected_fingerprint)
    }
}

impl MutationFingerprintArgs {
    fn expected(&self) -> Result<Option<Fingerprint>, ()> {
        expected_fingerprint(&self.expected_fingerprint)
    }
}

const ORDINARY_CREATE_READY: bool = cfg!(target_os="linux");

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OrdinaryCreateArgs { binding: OrdinaryCreateCapture, #[serde(default)] bytes: Option<Vec<u8>> }

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OrdinaryReplaceArgs {
    binding: OrdinaryWriteCapture,
    #[serde(default)]
    bytes: Option<Vec<u8>>,
}

#[derive(Debug, Deserialize)]
struct ReplaceArgs {
    #[serde(default)]
    expected_fingerprint: Option<FingerprintWire>,
    #[serde(default)]
    bytes: Option<Vec<u8>>,
    #[serde(default)]
    text: Option<String>,
}

impl ReplaceArgs {
    fn expected(&self) -> Result<Option<Fingerprint>, ()> {
        expected_fingerprint(&self.expected_fingerprint)
    }
}

#[derive(Debug, Deserialize)]
struct PatchArgs {
    #[serde(default)]
    expected_fingerprint: Option<FingerprintWire>,
    old: String,
    new: String,
    #[serde(default)]
    replace_all: bool,
}

impl PatchArgs {
    fn expected(&self) -> Result<Option<Fingerprint>, ()> {
        expected_fingerprint(&self.expected_fingerprint)
    }
}

#[derive(Debug, Deserialize)]
struct SearchOptionsWire {
    query: String,
    #[serde(default = "default_search_mode")]
    mode: String,
    #[serde(default)]
    file_glob: Option<String>,
    #[serde(default)]
    exclude: Vec<String>,
    #[serde(default)]
    deadline_ms: Option<u64>,
    #[serde(default)]
    locations: bool,
    #[serde(default)]
    cursor: Option<Value>,
    #[serde(default = "default_max_results")]
    max_results: usize,
    #[serde(default = "default_max_entries")]
    max_entries: u64,
    #[serde(default = "default_max_depth")]
    max_depth: usize,
    #[serde(default = "default_max_bytes")]
    max_bytes_per_file: usize,
    #[serde(default)]
    include_hidden: bool,
    #[serde(default)]
    case_sensitive: bool,
}

impl SearchOptionsWire {
    fn into_options(self) -> (SearchOptions, SearchDetails) {
        let options = SearchOptions {
            query: self.query,
            glob: self.mode == "glob",
            file_glob: self.file_glob,
            max_results: self.max_results.min(200),
            max_entries: self.max_entries.min(10000),
            max_depth: self.max_depth.min(32),
            max_bytes_per_file: self.max_bytes_per_file.min(1024 * 1024),
            include_hidden: self.include_hidden,
            case_sensitive: self.case_sensitive,
        };
        let details = SearchDetails {
            excludes: self.exclude,
            deadline: self.deadline_ms.map(|ms| Instant::now() + Duration::from_millis(ms.clamp(1, 5000))),
            locations: self.locations,
        };
        (options, details)
    }
}

fn default_search_mode() -> String { "literal".into() }

fn default_max_results() -> usize {
    100
}
fn default_max_entries() -> u64 {
    10_000
}
fn default_max_depth() -> usize {
    32
}
fn default_max_bytes() -> usize {
    1024 * 1024
}

fn fingerprint_descriptor(file: &mut File) -> std::io::Result<crate::engine::Fingerprint> {
    file.seek(SeekFrom::Start(0))?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 128 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(crate::engine::Fingerprint {
        algorithm: "sha256",
        value: format!("{:x}", digest.finalize()),
    })
}

fn metadata_modified_ms(metadata: &Metadata) -> Option<u64> {
    metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|value| value.as_millis() as u64)
}

#[cfg(unix)]
fn metadata_mode(metadata: &Metadata) -> Option<u32> {
    Some(metadata.permissions().mode())
}

#[cfg(not(unix))]
fn metadata_mode(_metadata: &Metadata) -> Option<u32> {
    None
}

fn target_path(target: &Target, audit_id: &str) -> Result<std::path::PathBuf, ProtocolError> {
    match target {
        Target::Path(path) => Ok(std::path::PathBuf::from(path)),
        Target::Handle(_) => Err(ProtocolError::new(
            ProtocolErrorCode::Unsupported,
            "resource handles are not implemented by this service build",
            Some(audit_id.into()),
        )),
    }
}

/// An undo names only the forward operation; its participants come from that operation's own journal (never from the request).
fn resolve_managed_undo(engine: &FileEngine, owner_id: &str, args: &mut ManagedFileMoveArgs, audit_id: &str) -> Result<(), ProtocolError> {
    let Some(forward) = args.inverse_of.clone() else { return Ok(()) };
    if !args.participants.is_empty() || !args.guards.is_empty() || args.directory_guard.is_some() || forward == args.operation_id {
        return Err(ProtocolError::new(ProtocolErrorCode::MalformedRequest, "managed undo payload is invalid", Some(audit_id.into())));
    }
    args.participants = engine.managed_undo_participants(owner_id, &forward).map_err(|error| engine_protocol(error, audit_id))?;
    Ok(())
}

fn serialize_engine<T: serde::Serialize>(
    result: Result<T, EngineError>,
    audit_id: &str,
) -> Result<Value, ProtocolError> {
    result
        .map(|value| json!({"protocol": crate::PROTOCOL_VERSION, "data": value, "audit_id": audit_id}))
        .map_err(|error| engine_protocol(error, audit_id))
}

fn engine_protocol(error: EngineError, audit_id: &str) -> ProtocolError {
    if matches!(&error, EngineError::HistoryCapture(_)) {
        return ProtocolError::new(ProtocolErrorCode::RootUnavailable, "required recovery preimage unavailable; original preserved", Some(audit_id.into()));
    }
    if let EngineError::ManagedUndoUnavailable(code) = &error {
        let mut refusal = ProtocolError::new(ProtocolErrorCode::Unsupported, *code, Some(audit_id.into()));
        refusal.details.insert("code".into(), (*code).into());
        return refusal;
    }
    if matches!(&error, EngineError::ManagedCrossFilesystem) {
        // Distinct from Conflict so the Host can say why the move was refused.
        let mut refusal = ProtocolError::new(ProtocolErrorCode::Unsupported, "managed_cross_filesystem_unsupported", Some(audit_id.into()));
        refusal.details.insert("code".into(), "managed_cross_filesystem_unsupported".into());
        return refusal;
    }
    let code = match &error {
        EngineError::Conflict
        | EngineError::DestinationExists
        | EngineError::CrossVolume
        | EngineError::InvalidTrashEntry => ProtocolErrorCode::Conflict,
        EngineError::Cancelled => ProtocolErrorCode::Cancelled,
        EngineError::InvalidSearchPattern => ProtocolErrorCode::MalformedRequest,
        EngineError::LimitExceeded => ProtocolErrorCode::Backpressure,
        EngineError::Registry(RegistryError::OutsideRoot) => ProtocolErrorCode::Denied,
        EngineError::StaleCursor => ProtocolErrorCode::StaleCursor,
        EngineError::NotAFile
        | EngineError::NotADirectory
        | EngineError::BinaryOrUnsupportedText => ProtocolErrorCode::InvalidPath,
        EngineError::CannotTrashRoot => ProtocolErrorCode::Denied,
        EngineError::Io(io_error) if io_error.kind() == std::io::ErrorKind::NotFound => {
            ProtocolErrorCode::InvalidPath
        }
        _ => ProtocolErrorCode::RootUnavailable,
    };
    ProtocolError::new(code, "filesystem operation failed", Some(audit_id.into()))
}

fn thumbnail_protocol(error: ThumbnailError, audit_id: &str) -> ProtocolError {
    let code = match error {
        ThumbnailError::Denied => ProtocolErrorCode::Denied,
        ThumbnailError::NotEligible | ThumbnailError::NativeUnavailable => {
            ProtocolErrorCode::InvalidPath
        }
        ThumbnailError::InvalidRequest => ProtocolErrorCode::MalformedRequest,
        ThumbnailError::UnsupportedPlatform => ProtocolErrorCode::Unsupported,
        ThumbnailError::QueueFull | ThumbnailError::OutputTooLarge => {
            ProtocolErrorCode::Backpressure
        }
        ThumbnailError::Cancelled => ProtocolErrorCode::Cancelled,
        ThumbnailError::DeadlineExceeded => ProtocolErrorCode::DeadlineExceeded,
        ThumbnailError::Stale => ProtocolErrorCode::StaleHandle,
        ThumbnailError::HelperUnavailable | ThumbnailError::IconRepresentationRejected => {
            ProtocolErrorCode::RootUnavailable
        }
    };
    ProtocolError::new(
        code,
        "native content thumbnail is unavailable",
        Some(audit_id.into()),
    )
}

use crate::RegistryError;

#[cfg(all(test, any(target_os = "linux", target_os = "macos")))]
mod identity_open_tests {
    use super::*;
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::symlink;

    #[test]
    fn regular_open_refuses_fifo_and_link_substitutions() {
        let temp = tempfile::tempdir().unwrap();
        let target = temp.path().join("target");
        let other = temp.path().join("other");
        fs::write(&target, b"source").unwrap();
        fs::write(&other, b"other").unwrap();
        assert!(fs::metadata(&target).unwrap().is_file());
        let opened = open_identity_file(&target).unwrap();
        assert!(opened.metadata().unwrap().is_file());
        assert_eq!(ObjectIdentity::from_file(&opened).unwrap(), ObjectIdentity::from_path(&target).unwrap());

        // Simulate replacement after the preceding regular-file authorization.
        fs::remove_file(&target).unwrap();
        let c_path = CString::new(target.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) }, 0);
        let started = Instant::now();
        assert!(open_identity_file(&target).is_err());
        assert!(ObjectIdentity::from_path(&target).is_err());
        assert!(started.elapsed() < Duration::from_secs(1));

        fs::remove_file(&target).unwrap();
        symlink(&other, &target).unwrap();
        assert!(open_identity_file(&target).is_err());
        assert!(ObjectIdentity::from_path(&target).is_err());
    }
}

#[cfg(test)]
mod thumbnail_cache_tests {
    use super::*;

    fn staging_service(owner: &str, principal: &str, session: &str) -> FileService {
        FileService::new(
            FileEngine::new(
                crate::EngineConfig { directory_page_size: 32, max_read_bytes: 1024 * 1024, max_write_bytes: 1024 * 1024 },
                crate::RootRegistry::default(),
            ),
            ServiceIdentity::app(owner, principal, session),
        )
    }

    fn stage_id(value: &Value) -> String {
        value["data"]["stage_id"].as_str().unwrap().to_string()
    }

    fn key(index: u32) -> ThumbnailCacheKey {
        ThumbnailCacheKey {
            object_tag: format!("object-{index}"),
            icon: false,
            policy_generation: 7,
            width: 160,
            height: 160,
            scale_milli: 2_000,
        }
    }

    fn png(index: u32) -> ThumbnailPng {
        ThumbnailPng {
            bytes: [b"\x89PNG\r\n\x1a\n".as_slice(), &index.to_be_bytes()].concat(),
            requested_width: 160,
            requested_height: 160,
            scale_milli: 2_000,
        }
    }

    #[test]
    fn staging_reaps_expired_owned_markers_across_restart_and_preserves_active_or_foreign_files() {
        let root = StageStore::ensure_root().unwrap();
        // Other tests construct services/reap in parallel against the same
        // process temp root. Give this fixture independent IDs AND identities.
        let mut random = [0_u8; 24];
        getrandom::fill(&mut random).unwrap();
        let nonce = random.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
        let owner = format!("reap-owner-{nonce}");
        let foreign_owner = format!("reap-foreign-{nonce}");
        let old_id = format!("fs-stage-{nonce}0000000000000000");
        let active_id = format!("fs-stage-{nonce}0000000000000001");
        let foreign_id = format!("fs-stage-{nonce}0000000000000002");
        let write_marker = |stage_id: &str, owner: &str, created_unix_ms: u64| {
            let marker = StageMarker {
                version: 1,
                stage_id: stage_id.to_string(),
                data_name: format!("{stage_id}.data"),
                owner_id: owner.to_string(),
                principal_id: "principal".into(),
                lane: TrustLane::App,
                session_id: "old-session".into(),
                created_unix_ms,
            };
            fs::write(root.join(format!("{stage_id}.data")), b"staged").unwrap();
            fs::write(root.join(format!("{stage_id}.json")), serde_json::to_vec(&marker).unwrap()).unwrap();
        };
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64;
        write_marker(&old_id, &owner, now.saturating_sub(STAGE_TTL.as_millis() as u64 + 1));
        write_marker(&active_id, &owner, now);
        write_marker(&foreign_id, &foreign_owner, now.saturating_sub(STAGE_TTL.as_millis() as u64 + 1));
        let foreign_data = root.join(format!("foreign-{nonce}.data"));
        fs::write(&foreign_data, b"unmarked").unwrap();

        let _restarted = staging_service(&owner, "principal", "new-session");
        assert!(!root.join(format!("{old_id}.data")).exists());
        assert!(!root.join(format!("{old_id}.json")).exists());
        assert!(root.join(format!("{active_id}.data")).exists());
        assert!(root.join(format!("{active_id}.json")).exists());
        assert!(root.join(format!("{foreign_id}.data")).exists());
        assert!(root.join(format!("{foreign_id}.json")).exists());
        assert!(foreign_data.exists());
        for id in [&active_id, &foreign_id] {
            let _ = fs::remove_file(root.join(format!("{id}.data")));
            let _ = fs::remove_file(root.join(format!("{id}.json")));
        }
        let _ = fs::remove_file(foreign_data);
    }

    #[test]
    fn thumbnail_cache_is_bounded_and_keyed_by_object_policy_and_size() {
        let mut cache = ThumbnailCache::default();
        for index in 0..=(MAX_THUMBNAIL_CACHE_ENTRIES as u32) {
            cache.insert(key(index), png(index));
        }
        assert_eq!(cache.entries.len(), MAX_THUMBNAIL_CACHE_ENTRIES);
        assert!(cache.get(&key(0)).is_none());
        assert_eq!(cache.get(&key(1)).unwrap(), png(1));

        let mut different_generation = key(1);
        different_generation.policy_generation += 1;
        assert!(cache.get(&different_generation).is_none());
        let mut different_size = key(1);
        different_size.width += 1;
        assert!(cache.get(&different_size).is_none());
    }

    #[test]
    fn dropped_stage_store_removes_only_owned_stage_files() {
        let path = std::env::temp_dir().join(format!(".openclank-files-service-test-{}", STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed)));
        let file = File::create(&path).unwrap();
        let store = StageStore(Arc::new(Mutex::new(HashMap::from([(String::from("stage"), StagedUpload {
            #[cfg(target_os="linux")] worktree:None,
            owner_id: String::from("owner"), principal_id: String::from("principal"), lane: TrustLane::Agent,
            session_id: String::from("session"), path: path.clone(), marker_path: None, file, length: 0, digest: Sha256::new(), created: Instant::now(),
        })]))));
        drop(store);
        assert!(!path.exists());
    }

    #[test]
    fn staging_binds_identity_offsets_digest_and_finish() {
        use base64::Engine as _;
        let service = staging_service("owner", "principal", "session");
        let root = std::env::temp_dir().join(format!("openclank-stage-root-{}-{}-{}", std::process::id(), STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed), Instant::now().elapsed().as_nanos()));
        fs::create_dir(&root).unwrap();
        let begin = service.stage_begin(root.clone(), "audit").unwrap();
        assert_eq!(begin["data"]["max_chunk_bytes"], MAX_STAGE_CHUNK_BYTES);
        assert_eq!(begin["data"]["max_total_bytes"], 1024 * 1024);
        let id = stage_id(&begin);
        #[cfg(unix)]
        {
            let stages = service.staged_uploads.0.lock().unwrap();
            let mode = fs::metadata(&stages[&id].path).unwrap().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
        let chunk = base64::engine::general_purpose::STANDARD.encode(b"hello");
        service.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk, offset: 0 }, "audit").unwrap();
        assert!(service.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk: String::new(), offset: 0 }, "audit").is_err());
        let begin = service.stage_begin(root.clone(), "audit-2").unwrap();
        let id = stage_id(&begin);
        let chunk = base64::engine::general_purpose::STANDARD.encode(b"hello");
        service.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk, offset: 0 }, "audit-2").unwrap();
        #[cfg(unix)]
        let decoy = {
            let stages = service.staged_uploads.0.lock().unwrap();
            let stage_path = stages[&id].path.clone();
            drop(stages);
            let decoy = root.join("decoy");
            fs::write(&decoy, b"attacker-bytes").unwrap();
            fs::remove_file(&stage_path).unwrap();
            std::os::unix::fs::symlink(&decoy, &stage_path).unwrap();
            decoy
        };
        let finished = service.stage_finish(StageFinishArgs { expected_fingerprint: None, stage_id: id, target: root.join("finished").display().to_string(), length: 5, digest: format!("sha256:{:x}", Sha256::digest(b"hello")) }, "audit-2");
        assert!(finished.is_ok(), "{finished:?}");
        assert_eq!(fs::read(root.join("finished")).unwrap(), b"hello");
        #[cfg(unix)]
        {
            assert_eq!(fs::read(decoy).unwrap(), b"attacker-bytes");
            assert_eq!(fs::metadata(root.join("finished")).unwrap().permissions().mode() & 0o777, 0o600);
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn staging_advertises_remaining_shared_capacity_and_prunes_expired_stage() {
        let service = staging_service("owner", "principal", "session");
        assert!(service.stage_begin(PathBuf::from("/definitely/missing/openclank-stage"), "denied").is_err());
        assert!(service.staged_uploads.0.lock().unwrap().is_empty());
        let path = std::env::temp_dir().join(format!(".openclank-files-service-test-{}", STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed)));
        let file = File::create(&path).unwrap();
        service.staged_uploads.0.lock().unwrap().insert("old".into(), StagedUpload {
            #[cfg(target_os="linux")] worktree:None,
            owner_id: "owner".into(), principal_id: "principal".into(), lane: TrustLane::App, session_id: "session".into(), path: path.clone(), marker_path: None, file, length: MAX_ACTIVE_STAGE_BYTES - 1, digest: Sha256::new(), created: Instant::now(),
        });
        let root = std::env::temp_dir();
        let begin = service.stage_begin(root, "remaining").unwrap();
        assert_eq!(begin["data"]["max_total_bytes"], 1);
        service.stage_abort(StageAbortArgs { stage_id: stage_id(&begin) }, "remaining").unwrap();
        let expired_path = std::env::temp_dir().join(format!(".openclank-files-service-test-{}", STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed)));
        let expired_file = File::create(&expired_path).unwrap();
        service.staged_uploads.0.lock().unwrap().insert("expired".into(), StagedUpload {
            #[cfg(target_os="linux")] worktree:None,
            owner_id: "owner".into(), principal_id: "principal".into(), lane: TrustLane::App, session_id: "session".into(), path: expired_path.clone(), marker_path: None, file: expired_file, length: 0, digest: Sha256::new(), created: Instant::now() - STAGE_TTL - Duration::from_secs(1),
        });
        let _ = service.stage_begin(std::env::temp_dir(), "prune").unwrap();
        assert!(path.exists());
        assert!(!expired_path.exists());
    }

    #[test]
    fn staging_rejects_other_identity_and_cleans_digest_failure_and_abort_is_idempotent() {
        use base64::Engine as _;
        let owner = staging_service("owner", "principal", "session");
        let mut other = staging_service("owner", "other", "session");
        other.staged_uploads = owner.staged_uploads.clone();
        let mut other_session = staging_service("owner", "principal", "other-session");
        other_session.staged_uploads = owner.staged_uploads.clone();
        let mut other_owner = staging_service("other-owner", "principal", "session");
        other_owner.staged_uploads = owner.staged_uploads.clone();
        let mut other_lane = staging_service("owner", "principal", "session");
        other_lane.identity.lane = TrustLane::Agent;
        other_lane.staged_uploads = owner.staged_uploads.clone();
        let root = std::env::temp_dir().join(format!("openclank-stage-root-{}-{}-{}", std::process::id(), STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed), Instant::now().elapsed().as_nanos()));
        fs::create_dir(&root).unwrap();
        let begin = owner.stage_begin(root.clone(), "audit").unwrap();
        let id = stage_id(&begin);
        let chunk = base64::engine::general_purpose::STANDARD.encode(b"x");
        assert!(other.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk: chunk.clone(), offset: 0 }, "audit").is_err());
        assert!(other_session.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk: chunk.clone(), offset: 0 }, "audit").is_err());
        assert!(other_owner.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk: chunk.clone(), offset: 0 }, "audit").is_err());
        assert!(other_lane.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk: base64::engine::general_purpose::STANDARD.encode(b"x"), offset: 0 }, "audit").is_err());
        owner.stage_chunk(StageChunkArgs { stage_id: id.clone(), chunk, offset: 0 }, "audit").unwrap();
        assert!(owner.stage_finish(StageFinishArgs { expected_fingerprint: None, stage_id: id.clone(), target: "/tmp/never-used".into(), length: 1, digest: "sha256:bad".into() }, "audit").is_err());
        assert!(owner.stage_abort(StageAbortArgs { stage_id: id }, "audit").is_ok());
        assert!(owner.stage_abort(StageAbortArgs { stage_id: "missing".into() }, "audit").is_ok());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn shared_stage_store_pruning_preserves_an_expired_foreign_identity() {
        let shared = StageStore(Arc::new(Mutex::new(HashMap::new())));
        let owner = staging_service("owner-a", "principal-a", "session-a");
        let mut other = staging_service("owner-b", "principal-b", "session-b");
        let mut owner = owner;
        owner.staged_uploads = shared.clone();
        other.staged_uploads = shared.clone();

        let foreign_path = std::env::temp_dir().join(format!(".openclank-foreign-stage-{}", STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed)));
        let foreign_file = File::create(&foreign_path).unwrap();
        shared.0.lock().unwrap().insert("foreign-expired".into(), StagedUpload {
            #[cfg(target_os="linux")] worktree:None,
            owner_id: "owner-a".into(),
            principal_id: "principal-a".into(),
            lane: TrustLane::App,
            session_id: "session-a".into(),
            path: foreign_path.clone(),
            marker_path: None,
            file: foreign_file,
            length: 0,
            digest: Sha256::new(),
            created: Instant::now() - STAGE_TTL - Duration::from_secs(1),
        });

        let root = std::env::temp_dir();
        let begin = other.stage_begin(root, "other-stage").unwrap();
        other.stage_abort(StageAbortArgs { stage_id: stage_id(&begin) }, "other-stage").unwrap();
        assert!(foreign_path.exists());
        assert!(shared.0.lock().unwrap().contains_key("foreign-expired"));
        drop(owner);
        assert!(!foreign_path.exists());
    }
}
