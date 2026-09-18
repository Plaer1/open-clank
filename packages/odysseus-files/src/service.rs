use crate::MutationCaptureHook;
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
const STAGE_TTL: Duration = Duration::from_secs(15 * 60);
const STAGE_ROOT_NAME: &str = ".openclank-files-staging-v1";
const MAX_STAGE_REAP_ENTRIES: usize = 256;
const MAX_STAGE_MARKER_BYTES: usize = 4096;
static STAGE_SEQUENCE: AtomicU64 = AtomicU64::new(1);

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

    fn valid_stage_id(value: &str) -> bool {
        value.len() == 73 && value.starts_with("fs-stage-") && value[9..].bytes().all(|byte| byte.is_ascii_hexdigit())
    }

    fn remove_stage_files(stage: &StagedUpload) {
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
struct ObjectIdentity {
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
    fn from_metadata(metadata: &Metadata) -> Self {
        Self {
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
        }
    }

    fn opaque_tag(&self) -> String {
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
    thumbnail_policy_generation: PolicyGeneration,
    thumbnail_cache: Mutex<ThumbnailCache>,
    staged_uploads: StageStore,
}

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
            identity,
            handles: Mutex::new(HandleTable::default()),
            thumbnail_broker: None,
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

    fn open_handle(&self, path: &Path, audit_id: &str) -> Result<Value, ProtocolError> {
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
        let link = fs::symlink_metadata(&canonical)
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        if link.file_type().is_symlink() {
            return Err(self.stale_handle(audit_id));
        }
        let file = File::open(&canonical)
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        let descriptor_metadata = file
            .metadata()
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        let path_metadata = fs::metadata(&canonical)
            .map_err(|error| engine_protocol(EngineError::Io(error), audit_id))?;
        if !descriptor_metadata.is_file()
            || ObjectIdentity::from_metadata(&descriptor_metadata)
                != ObjectIdentity::from_metadata(&path_metadata)
        {
            return Err(self.stale_handle(audit_id));
        }
        let identity = ObjectIdentity::from_metadata(&descriptor_metadata);
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
                "kind": authorized.kind,
                "size": authorized.size,
                "modified_unix_ms": authorized.modified_unix_ms,
                "mode": authorized.mode,
                "object_tag": object_tag,
            },
            "audit_id": audit_id,
        }))
    }

    fn stage_matches(&self, stage: &StagedUpload) -> bool {
        stage.owner_id == self.identity.owner_id
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
                    (stage.owner_id == self.identity.owner_id
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
        let active_bytes: u64 = stages.values().map(|stage| stage.length).sum();
        if stages.len() >= MAX_ACTIVE_STAGES || active_bytes >= MAX_ACTIVE_STAGE_BYTES {
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
        let active_bytes: u64 = stages.values().map(|entry| entry.length).sum();
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
        if stage.file.sync_all().is_err() {
            StageStore::remove_stage_files(&stage);
            return Err(Self::stage_error("staging write failed", audit_id));
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
        let path_metadata = fs::metadata(&path).map_err(|_| self.stale_handle(audit_id))?;
        let descriptor_metadata = file.metadata().map_err(|_| self.stale_handle(audit_id))?;
        if ObjectIdentity::from_metadata(&path_metadata) != expected
            || ObjectIdentity::from_metadata(&descriptor_metadata) != expected
        {
            return Err(self.stale_handle(audit_id));
        }
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
        let (_file, path, metadata) = self.resolve_file_handle(handle, &audit_id)?;
        let broker = self.thumbnail_broker.as_ref().ok_or_else(|| {
            ProtocolError::new(
                ProtocolErrorCode::Unsupported,
                "native content thumbnails are unavailable on this host",
                Some(audit_id.clone()),
            )
        })?;
        let cache_key = ThumbnailCacheKey {
            object_tag: ObjectIdentity::from_metadata(&metadata).opaque_tag(),
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

    pub fn dispatch(&self, request: ClientRequest) -> Result<Value, ProtocolError> {
        let (operation, envelope, audit_id) = self.bind_request(request)?;
        match operation {
            Operation::Health => Ok(json!({
                "protocol": crate::PROTOCOL_VERSION,
                "lane": self.identity.lane,
                "owner_id": self.identity.owner_id,
                "service_session_id": self.identity.session_id,
                "ready": true,
            })),
            Operation::Capabilities => Ok(json!({
                "protocol": crate::PROTOCOL_VERSION,
                "lane": self.identity.lane,
                "operations": ["health", "capabilities", "list_directory", "stat", "open_handle", "read_range", "read_lines", "read_text_preview", "create", "stage_begin", "stage_chunk", "stage_finish", "stage_abort", "replace", "patch", "copy", "move", "rename", "mkdir", "trash", "restore", "filename_search", "content_search"],
                "native_thumbnails": self.native_thumbnails_available(),
                "max_page_size": self.engine.config().directory_page_size,
                "max_read_bytes": self.engine.config().max_read_bytes,
            })),
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
                        self.stat_handle(handle, args.include_fingerprint, &audit_id)
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
                let options = args.into_options();
                let cancelled = AtomicBool::new(false);
                let result = match (self.identity.lane, operation) {
                    (TrustLane::App, Operation::FilenameSearch) => {
                        self.engine.filename_search_scoped(
                            &self.identity.app_scope,
                            &path,
                            &options,
                            Some(&cancelled),
                        )
                    }
                    (TrustLane::Agent, Operation::FilenameSearch) => self.engine.filename_search(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        &options,
                        Some(&cancelled),
                    ),
                    (TrustLane::Agent, Operation::ContentSearch) => self.engine.content_search(
                        self.identity.owner_id(),
                        &self.identity.scope(),
                        &path,
                        &options,
                        Some(&cancelled),
                    ),
                    (TrustLane::App, Operation::ContentSearch) => {
                        self.engine.content_search_scoped(
                            &self.identity.app_scope,
                            &path,
                            &options,
                            Some(&cancelled),
                        )
                    }
                    _ => unreachable!(),
                };
                serialize_engine(result, &audit_id)
            }
            _ => Err(ProtocolError::new(
                ProtocolErrorCode::Unsupported,
                "operation is not implemented by this service build",
                Some(audit_id),
            )),
        }
    }
}

impl Drop for FileService {
    fn drop(&mut self) {
        self.staged_uploads.remove_session(&self.identity);
    }
}

#[derive(Debug, Default, Deserialize)]
struct ListDirectoryArgs {
    #[serde(default)]
    cursor: Option<crate::Cursor>,
    #[serde(default)]
    sort: Option<crate::DirectorySort>,
    #[serde(default)]
    limit: Option<usize>,
}

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
}

#[derive(Debug, Deserialize)]
struct ThumbnailArgs {
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
struct StageFinishArgs { stage_id: String, target: String, length: u64, digest: String }

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
    fn into_options(self) -> SearchOptions {
        SearchOptions {
            query: self.query,
            max_results: self.max_results,
            max_entries: self.max_entries,
            max_depth: self.max_depth,
            max_bytes_per_file: self.max_bytes_per_file,
            include_hidden: self.include_hidden,
            case_sensitive: self.case_sensitive,
        }
    }
}

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

fn serialize_engine<T: serde::Serialize>(
    result: Result<T, EngineError>,
    audit_id: &str,
) -> Result<Value, ProtocolError> {
    result
        .map(|value| json!({"protocol": crate::PROTOCOL_VERSION, "data": value, "audit_id": audit_id}))
        .map_err(|error| engine_protocol(error, audit_id))
}

fn engine_protocol(error: EngineError, audit_id: &str) -> ProtocolError {
    let code = match &error {
        EngineError::Conflict
        | EngineError::DestinationExists
        | EngineError::CrossVolume
        | EngineError::InvalidTrashEntry => ProtocolErrorCode::Conflict,
        EngineError::Cancelled => ProtocolErrorCode::Cancelled,
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
        let nonce = STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let old_id = format!("fs-stage-{nonce:064x}");
        let active_id = format!("fs-stage-{:064x}", nonce + 1);
        let foreign_id = format!("fs-stage-{:064x}", nonce + 2);
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
        write_marker(&old_id, "owner", now.saturating_sub(STAGE_TTL.as_millis() as u64 + 1));
        write_marker(&active_id, "owner", now);
        write_marker(&foreign_id, "other-owner", now.saturating_sub(STAGE_TTL.as_millis() as u64 + 1));
        let foreign_data = root.join(format!("foreign-{nonce}.data"));
        fs::write(&foreign_data, b"unmarked").unwrap();

        let _restarted = staging_service("owner", "principal", "new-session");
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
        let finished = service.stage_finish(StageFinishArgs { stage_id: id, target: root.join("finished").display().to_string(), length: 5, digest: format!("sha256:{:x}", Sha256::digest(b"hello")) }, "audit-2");
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
            owner_id: "owner".into(), principal_id: "principal".into(), lane: TrustLane::App, session_id: "session".into(), path: path.clone(), marker_path: None, file, length: MAX_ACTIVE_STAGE_BYTES - 1, digest: Sha256::new(), created: Instant::now(),
        });
        let root = std::env::temp_dir();
        let begin = service.stage_begin(root, "remaining").unwrap();
        assert_eq!(begin["data"]["max_total_bytes"], 1);
        service.stage_abort(StageAbortArgs { stage_id: stage_id(&begin) }, "remaining").unwrap();
        let expired_path = std::env::temp_dir().join(format!(".openclank-files-service-test-{}", STAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed)));
        let expired_file = File::create(&expired_path).unwrap();
        service.staged_uploads.0.lock().unwrap().insert("expired".into(), StagedUpload {
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
        assert!(owner.stage_finish(StageFinishArgs { stage_id: id.clone(), target: "/tmp/never-used".into(), length: 1, digest: "sha256:bad".into() }, "audit").is_err());
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
