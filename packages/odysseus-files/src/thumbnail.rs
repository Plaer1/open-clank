//! Bounded admission path for macOS Quick Look content thumbnails.
//!
//! The browser-facing protocol does not call this module directly. Callers must
//! first enter through one of the `render_*` methods, which authorize and stat
//! through `FileEngine`, capture a stable identity, and re-authorize/revalidate
//! after the isolated helper completes.

use crate::{AgentScope, AppScope, EngineError, FileEngine, FileKind};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use thiserror::Error;

const PNG_SIGNATURE: &[u8; 8] = b"\x89PNG\r\n\x1a\n";
const MAX_QUEUE_CAPACITY: usize = 128;

#[derive(Clone, Debug)]
pub struct ThumbnailLimits {
    pub queue_capacity: usize,
    pub max_width: u32,
    pub max_height: u32,
    pub max_scale: f64,
    pub max_output_bytes: usize,
    pub default_deadline: Duration,
    pub cancel_grace: Duration,
    pub helper_start_deadline: Duration,
    pub max_restarts_per_job: u32,
    pub max_consecutive_crashes: u32,
    pub circuit_breaker_cooldown: Duration,
}

impl Default for ThumbnailLimits {
    fn default() -> Self {
        Self {
            queue_capacity: 8,
            max_width: 1024,
            max_height: 1024,
            max_scale: 3.0,
            max_output_bytes: 4 * 1024 * 1024,
            default_deadline: Duration::from_secs(2),
            cancel_grace: Duration::from_millis(100),
            helper_start_deadline: Duration::from_secs(2),
            max_restarts_per_job: 1,
            max_consecutive_crashes: 3,
            circuit_breaker_cooldown: Duration::from_secs(5),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ThumbnailBrokerConfig {
    pub helper_path: PathBuf,
    pub shell_icons: bool,
    pub limits: ThumbnailLimits,
}

impl ThumbnailBrokerConfig {
    pub fn new(helper_path: impl Into<PathBuf>) -> Self {
        Self {
            helper_path: helper_path.into(),
            shell_icons: false,
            limits: ThumbnailLimits::default(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ThumbnailRequest {
    pub width: u32,
    pub height: u32,
    pub scale: f64,
    pub deadline: Option<Duration>,
}

impl ThumbnailRequest {
    pub fn new(width: u32, height: u32, scale: f64) -> Self {
        Self {
            width,
            height,
            scale,
            deadline: None,
        }
    }

    pub fn with_deadline(mut self, deadline: Duration) -> Self {
        self.deadline = Some(deadline);
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ThumbnailPng {
    pub bytes: Vec<u8>,
    pub requested_width: u32,
    pub requested_height: u32,
    pub scale_milli: u32,
}

#[derive(Clone, Default)]
pub struct CancellationToken(Arc<AtomicBool>);

impl CancellationToken {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}

#[derive(Clone, Default)]
pub struct PolicyGeneration(Arc<AtomicU64>);

impl PolicyGeneration {
    pub fn new(generation: u64) -> Self {
        Self(Arc::new(AtomicU64::new(generation)))
    }
    pub fn current(&self) -> u64 {
        self.0.load(Ordering::Acquire)
    }
    pub fn replace(&self, generation: u64) {
        self.0.store(generation, Ordering::Release);
    }
    pub fn invalidate(&self) -> u64 {
        self.0.fetch_add(1, Ordering::AcqRel) + 1
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ThumbnailStats {
    pub submitted: u64,
    pub completed: u64,
    pub queue_rejections: u64,
    pub helper_starts: u64,
    pub helper_restarts: u64,
    pub cancelled: u64,
    pub deadline_expirations: u64,
    pub output_rejections: u64,
    pub stale_completions: u64,
}

#[derive(Default)]
struct StatsInner {
    submitted: AtomicU64,
    completed: AtomicU64,
    queue_rejections: AtomicU64,
    helper_starts: AtomicU64,
    helper_restarts: AtomicU64,
    cancelled: AtomicU64,
    deadline_expirations: AtomicU64,
    output_rejections: AtomicU64,
    stale_completions: AtomicU64,
}

impl StatsInner {
    fn snapshot(&self) -> ThumbnailStats {
        ThumbnailStats {
            submitted: self.submitted.load(Ordering::Relaxed),
            completed: self.completed.load(Ordering::Relaxed),
            queue_rejections: self.queue_rejections.load(Ordering::Relaxed),
            helper_starts: self.helper_starts.load(Ordering::Relaxed),
            helper_restarts: self.helper_restarts.load(Ordering::Relaxed),
            cancelled: self.cancelled.load(Ordering::Relaxed),
            deadline_expirations: self.deadline_expirations.load(Ordering::Relaxed),
            output_rejections: self.output_rejections.load(Ordering::Relaxed),
            stale_completions: self.stale_completions.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum ThumbnailError {
    #[error("thumbnail request was denied")]
    Denied,
    #[error("resource is not eligible for a content thumbnail")]
    NotEligible,
    #[error("thumbnail request dimensions are invalid")]
    InvalidRequest,
    #[error("native thumbnails are unavailable on this host")]
    UnsupportedPlatform,
    #[error("native thumbnail helper is unavailable")]
    HelperUnavailable,
    #[error("native thumbnail generation is unavailable for this resource")]
    NativeUnavailable,
    #[error("native helper attempted to return an icon representation")]
    IconRepresentationRejected,
    #[error("thumbnail queue is full")]
    QueueFull,
    #[error("thumbnail request was cancelled")]
    Cancelled,
    #[error("thumbnail deadline elapsed")]
    DeadlineExceeded,
    #[error("thumbnail output exceeded its byte limit")]
    OutputTooLarge,
    #[error("resource or policy changed while the thumbnail was generated")]
    Stale,
}

pub struct ThumbnailBroker {
    platform: platform::PlatformBroker,
    limits: ThumbnailLimits,
    stats: Arc<StatsInner>,
}

impl ThumbnailBroker {
    pub fn new(config: ThumbnailBrokerConfig) -> Result<Self, ThumbnailError> {
        validate_limits(&config.limits)?;
        let stats = Arc::new(StatsInner::default());
        let platform = platform::PlatformBroker::new(&config, Arc::clone(&stats))?;
        Ok(Self {
            platform,
            limits: config.limits,
            stats,
        })
    }

    pub const fn supported_on_this_host() -> bool {
        cfg!(any(target_os = "macos", windows))
    }

    pub fn stats(&self) -> ThumbnailStats {
        self.stats.snapshot()
    }

    pub fn render_app(
        &self,
        engine: &FileEngine,
        scope: &AppScope,
        path: &Path,
        policy_generation: &PolicyGeneration,
        request: ThumbnailRequest,
        cancellation: CancellationToken,
    ) -> Result<ThumbnailPng, ThumbnailError> {
        let expected_generation = policy_generation.current();
        if !scope.host && scope.generation != expected_generation {
            return Err(ThumbnailError::Stale);
        }
        let stat = engine
            .stat_scoped(scope, path, false)
            .map_err(map_engine_error)?;
        let identity = FileIdentity::from_authorized_stat(stat.path, stat.kind)?;
        let result = self.render_identity(&identity, request, cancellation)?;
        if policy_generation.current() != expected_generation {
            self.stats.stale_completions.fetch_add(1, Ordering::Relaxed);
            return Err(ThumbnailError::Stale);
        }
        let current = engine
            .stat_scoped(scope, path, false)
            .map_err(|_| ThumbnailError::Stale)?;
        let current = FileIdentity::from_authorized_stat(current.path, current.kind)
            .map_err(|_| ThumbnailError::Stale)?;
        if identity != current {
            self.stats.stale_completions.fetch_add(1, Ordering::Relaxed);
            return Err(ThumbnailError::Stale);
        }
        Ok(result)
    }

    pub fn render_agent(
        &self,
        engine: &FileEngine,
        owner_id: &str,
        scope: &AgentScope,
        path: &Path,
        policy_generation: &PolicyGeneration,
        request: ThumbnailRequest,
        cancellation: CancellationToken,
    ) -> Result<ThumbnailPng, ThumbnailError> {
        let expected_generation = policy_generation.current();
        let stat = engine
            .stat(owner_id, scope, path, false)
            .map_err(map_engine_error)?;
        let identity = FileIdentity::from_authorized_stat(stat.path, stat.kind)?;
        let result = self.render_identity(&identity, request, cancellation)?;
        if policy_generation.current() != expected_generation {
            self.stats.stale_completions.fetch_add(1, Ordering::Relaxed);
            return Err(ThumbnailError::Stale);
        }
        let current = engine
            .stat(owner_id, scope, path, false)
            .map_err(|_| ThumbnailError::Stale)?;
        let current = FileIdentity::from_authorized_stat(current.path, current.kind)
            .map_err(|_| ThumbnailError::Stale)?;
        if identity != current {
            self.stats.stale_completions.fetch_add(1, Ordering::Relaxed);
            return Err(ThumbnailError::Stale);
        }
        Ok(result)
    }

    fn render_identity(
        &self,
        identity: &FileIdentity,
        request: ThumbnailRequest,
        cancellation: CancellationToken,
    ) -> Result<ThumbnailPng, ThumbnailError> {
        validate_request(&request, &self.limits)?;
        if cancellation.is_cancelled() {
            return Err(ThumbnailError::Cancelled);
        }
        let requested_width = request.width;
        let requested_height = request.height;
        let scale_milli = (request.scale * 1000.0).round() as u32;
        let bytes = self
            .platform
            .render(identity.path.clone(), request, cancellation)?;
        if bytes.len() > self.limits.max_output_bytes {
            self.stats.output_rejections.fetch_add(1, Ordering::Relaxed);
            return Err(ThumbnailError::OutputTooLarge);
        }
        if !bytes.starts_with(PNG_SIGNATURE) {
            return Err(ThumbnailError::NativeUnavailable);
        }
        Ok(ThumbnailPng {
            bytes,
            requested_width,
            requested_height,
            scale_milli,
        })
    }
}

fn validate_limits(limits: &ThumbnailLimits) -> Result<(), ThumbnailError> {
    if limits.queue_capacity == 0
        || limits.queue_capacity > MAX_QUEUE_CAPACITY
        || limits.max_width == 0
        || limits.max_height == 0
        || !limits.max_scale.is_finite()
        || limits.max_scale < 1.0
        || limits.max_output_bytes == 0
        || limits.max_output_bytes > crate::quicklook_protocol::MAX_HELPER_OUTPUT_BYTES
        || limits.max_consecutive_crashes == 0
    {
        return Err(ThumbnailError::InvalidRequest);
    }
    Ok(())
}

fn validate_request(
    request: &ThumbnailRequest,
    limits: &ThumbnailLimits,
) -> Result<(), ThumbnailError> {
    if request.width == 0
        || request.height == 0
        || request.width > limits.max_width
        || request.height > limits.max_height
        || !request.scale.is_finite()
        || request.scale < 1.0
        || request.scale > limits.max_scale
    {
        return Err(ThumbnailError::InvalidRequest);
    }
    Ok(())
}

fn map_engine_error(error: EngineError) -> ThumbnailError {
    match error {
        EngineError::Registry(_) => ThumbnailError::Denied,
        EngineError::NotAFile | EngineError::NotADirectory => ThumbnailError::NotEligible,
        _ => ThumbnailError::NativeUnavailable,
    }
}

#[derive(Debug, Eq, PartialEq)]
struct FileIdentity {
    #[cfg(windows)]
    native: crate::windows_fs::OpenedIdentity,
    path: PathBuf,
    length: u64,
    modified_ns: i128,
    created_or_changed_ns: i128,
    device: u64,
    inode: u64,
}

impl FileIdentity {
    fn from_authorized_stat(path: PathBuf, kind: FileKind) -> Result<Self, ThumbnailError> {
        if kind != FileKind::File {
            return Err(ThumbnailError::NotEligible);
        }
        let canonical = fs::canonicalize(&path).map_err(|_| ThumbnailError::NativeUnavailable)?;
        if canonical != path {
            return Err(ThumbnailError::Stale);
        }
        let metadata =
            fs::symlink_metadata(&canonical).map_err(|_| ThumbnailError::NativeUnavailable)?;
        if !metadata.file_type().is_file() {
            return Err(ThumbnailError::NotEligible);
        }
        if platform_is_package(&canonical)? {
            return Err(ThumbnailError::NotEligible);
        }
        let (modified_ns, created_or_changed_ns, device, inode) = platform_identity(&metadata);
        Ok(Self {
            #[cfg(windows)]
            native: {
                let file = fs::File::open(&canonical).map_err(|_| ThumbnailError::NativeUnavailable)?;
                crate::windows_fs::validate_opened_path(&file, &canonical).map_err(|_| ThumbnailError::Stale)?;
                crate::windows_fs::opened_identity(&file).map_err(|_| ThumbnailError::NativeUnavailable)?
            },
            path: canonical,
            length: metadata.len(),
            modified_ns,
            created_or_changed_ns,
            device,
            inode,
        })
    }
}

#[cfg(target_os = "macos")]
fn platform_is_package(path: &Path) -> Result<bool, ThumbnailError> {
    use objc2::rc::autoreleasepool;
    use objc2_foundation::{NSNumber, NSURLIsPackageKey, NSURL};

    autoreleasepool(|_| {
        let url = NSURL::from_file_path(path).ok_or(ThumbnailError::NativeUnavailable)?;
        let mut value = None;
        unsafe { url.getResourceValue_forKey_error(&mut value, NSURLIsPackageKey) }
            .map_err(|_| ThumbnailError::NativeUnavailable)?;
        let value = value.ok_or(ThumbnailError::NativeUnavailable)?;
        let number = value
            .downcast_ref::<NSNumber>()
            .ok_or(ThumbnailError::NativeUnavailable)?;
        Ok(number.boolValue())
    })
}

#[cfg(not(target_os = "macos"))]
fn platform_is_package(_: &Path) -> Result<bool, ThumbnailError> {
    Ok(false)
}

#[cfg(unix)]
fn platform_identity(metadata: &fs::Metadata) -> (i128, i128, u64, u64) {
    use std::os::unix::fs::MetadataExt;
    let modified = metadata.mtime() as i128 * 1_000_000_000 + metadata.mtime_nsec() as i128;
    let changed = metadata.ctime() as i128 * 1_000_000_000 + metadata.ctime_nsec() as i128;
    (modified, changed, metadata.dev(), metadata.ino())
}

#[cfg(not(unix))]
fn platform_identity(metadata: &fs::Metadata) -> (i128, i128, u64, u64) {
    use std::time::UNIX_EPOCH;
    let modified = metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_nanos() as i128)
        .unwrap_or_default();
    let created = metadata
        .created()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_nanos() as i128)
        .unwrap_or_default();
    (modified, created, 0, 0)
}

#[cfg(any(target_os = "macos", windows))]
mod platform {
    use super::*;
    use crate::quicklook_protocol::{
        self as wire, Command as WireCommand, HelperErrorCode, RenderCommand,
        Response as WireResponse, WireError,
    };
    use std::fs::File;
    use std::io::Read;
    #[cfg(unix)]
    use std::os::unix::ffi::OsStrExt;
    use std::process::{Child, ChildStdin, Command, Stdio};
    use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TrySendError};
    use std::thread::{self, JoinHandle};
    use std::time::Instant;

    const SESSION_ENV: &str = "ODYSSEUS_QUICKLOOK_SESSION_TOKEN";
    const POLL_INTERVAL: Duration = Duration::from_millis(10);

    pub struct PlatformBroker {
        sender: SyncSender<WorkerCommand>,
        worker: Option<JoinHandle<()>>,
        stats: Arc<StatsInner>,
    }

    enum WorkerCommand {
        Render(Work),
        Shutdown,
    }

    struct Work {
        job_id: u64,
        path: PathBuf,
        request: ThumbnailRequest,
        cancellation: CancellationToken,
        submitted: Instant,
        response: mpsc::Sender<Result<Vec<u8>, ThumbnailError>>,
    }

    impl PlatformBroker {
        pub fn new(
            config: &ThumbnailBrokerConfig,
            stats: Arc<StatsInner>,
        ) -> Result<Self, ThumbnailError> {
            let (sender, receiver) = mpsc::sync_channel(config.limits.queue_capacity);
            let helper_path = config.helper_path.clone();
            let shell_icons = config.shell_icons;
            let limits = config.limits.clone();
            let worker_stats = Arc::clone(&stats);
            let worker = thread::Builder::new()
                .name("odysseus-quicklook-supervisor".into())
                .spawn(move || Worker::new(helper_path, shell_icons, limits, worker_stats).run(receiver))
                .map_err(|_| ThumbnailError::HelperUnavailable)?;
            Ok(Self {
                sender,
                worker: Some(worker),
                stats,
            })
        }

        pub fn render(
            &self,
            path: PathBuf,
            request: ThumbnailRequest,
            cancellation: CancellationToken,
        ) -> Result<Vec<u8>, ThumbnailError> {
            static NEXT_JOB_ID: AtomicU64 = AtomicU64::new(1);
            let (response, receiver) = mpsc::channel();
            let work = Work {
                job_id: NEXT_JOB_ID.fetch_add(1, Ordering::Relaxed),
                path,
                request,
                cancellation,
                submitted: Instant::now(),
                response,
            };
            self.stats.submitted.fetch_add(1, Ordering::Relaxed);
            match self.sender.try_send(WorkerCommand::Render(work)) {
                Ok(()) => receiver
                    .recv()
                    .unwrap_or(Err(ThumbnailError::HelperUnavailable)),
                Err(TrySendError::Full(_)) => {
                    self.stats.queue_rejections.fetch_add(1, Ordering::Relaxed);
                    Err(ThumbnailError::QueueFull)
                }
                Err(TrySendError::Disconnected(_)) => Err(ThumbnailError::HelperUnavailable),
            }
        }
    }

    impl Drop for PlatformBroker {
        fn drop(&mut self) {
            let _ = self.sender.send(WorkerCommand::Shutdown);
            if let Some(worker) = self.worker.take() {
                let _ = worker.join();
            }
        }
    }

    struct Worker {
        helper_path: PathBuf,
        shell_icons: bool,
        limits: ThumbnailLimits,
        stats: Arc<StatsInner>,
        child: Option<ChildSession>,
        crash_streak: u32,
        circuit_open_until: Option<Instant>,
    }

    impl Worker {
        fn new(helper_path: PathBuf, shell_icons: bool, limits: ThumbnailLimits, stats: Arc<StatsInner>) -> Self {
            Self {
                helper_path,
                shell_icons,
                limits,
                stats,
                child: None,
                crash_streak: 0,
                circuit_open_until: None,
            }
        }

        fn run(mut self, receiver: Receiver<WorkerCommand>) {
            while let Ok(command) = receiver.recv() {
                match command {
                    WorkerCommand::Render(work) => {
                        let response = self.perform(&work);
                        let _ = work.response.send(response);
                    }
                    WorkerCommand::Shutdown => break,
                }
            }
            if let Some(mut child) = self.child.take() {
                child.shutdown();
            }
        }

        fn perform(&mut self, work: &Work) -> Result<Vec<u8>, ThumbnailError> {
            if work.cancellation.is_cancelled() {
                self.stats.cancelled.fetch_add(1, Ordering::Relaxed);
                return Err(ThumbnailError::Cancelled);
            }
            if self
                .circuit_open_until
                .is_some_and(|until| Instant::now() < until)
            {
                return Err(ThumbnailError::HelperUnavailable);
            }
            self.circuit_open_until = None;
            let deadline = work.submitted
                + work
                    .request
                    .deadline
                    .unwrap_or(self.limits.default_deadline);
            let mut attempt = 0;
            loop {
                if work.cancellation.is_cancelled() {
                    self.stats.cancelled.fetch_add(1, Ordering::Relaxed);
                    return Err(ThumbnailError::Cancelled);
                }
                if Instant::now() >= deadline {
                    self.stats
                        .deadline_expirations
                        .fetch_add(1, Ordering::Relaxed);
                    return Err(ThumbnailError::DeadlineExceeded);
                }
                if self.child.is_none() {
                    match ChildSession::spawn(
                        &self.helper_path,
                        self.shell_icons,
                        &self.limits,
                        Arc::clone(&self.stats),
                        deadline,
                        &work.cancellation,
                    ) {
                        Ok(child) => self.child = Some(child),
                        Err(ThumbnailError::Cancelled) => {
                            self.stats.cancelled.fetch_add(1, Ordering::Relaxed);
                            return Err(ThumbnailError::Cancelled);
                        }
                        Err(ThumbnailError::DeadlineExceeded) => {
                            self.stats
                                .deadline_expirations
                                .fetch_add(1, Ordering::Relaxed);
                            return Err(ThumbnailError::DeadlineExceeded);
                        }
                        Err(_) => {
                            self.record_crash();
                            if attempt >= self.limits.max_restarts_per_job {
                                return Err(ThumbnailError::HelperUnavailable);
                            }
                            attempt += 1;
                            self.stats.helper_restarts.fetch_add(1, Ordering::Relaxed);
                            continue;
                        }
                    }
                }
                let send_result = self
                    .child
                    .as_mut()
                    .expect("child present")
                    .send_render(work, self.limits.max_output_bytes);
                if send_result.is_err() {
                    self.force_restart(false);
                    self.record_crash();
                    if attempt >= self.limits.max_restarts_per_job {
                        return Err(ThumbnailError::HelperUnavailable);
                    }
                    attempt += 1;
                    self.stats.helper_restarts.fetch_add(1, Ordering::Relaxed);
                    continue;
                }

                match self.wait_for_job(work, deadline) {
                    WaitOutcome::Complete(result) => {
                        if result.is_ok() {
                            self.crash_streak = 0;
                            self.stats.completed.fetch_add(1, Ordering::Relaxed);
                        }
                        return result;
                    }
                    WaitOutcome::Crashed => {
                        self.force_restart(false);
                        self.record_crash();
                        if attempt >= self.limits.max_restarts_per_job
                            || self.circuit_open_until.is_some()
                        {
                            return Err(ThumbnailError::HelperUnavailable);
                        }
                        attempt += 1;
                        self.stats.helper_restarts.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        }

        fn wait_for_job(&mut self, work: &Work, deadline: Instant) -> WaitOutcome {
            loop {
                if work.cancellation.is_cancelled() {
                    self.stats.cancelled.fetch_add(1, Ordering::Relaxed);
                    return WaitOutcome::Complete(self.cancel_running(work.job_id, false));
                }
                if Instant::now() >= deadline {
                    self.stats
                        .deadline_expirations
                        .fetch_add(1, Ordering::Relaxed);
                    return WaitOutcome::Complete(self.cancel_running(work.job_id, true));
                }
                let response = self
                    .child
                    .as_ref()
                    .expect("child present")
                    .responses
                    .recv_timeout(
                        POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now())),
                    );
                if Instant::now() >= deadline {
                    self.stats
                        .deadline_expirations
                        .fetch_add(1, Ordering::Relaxed);
                    return WaitOutcome::Complete(self.cancel_running(work.job_id, true));
                }
                match response {
                    Ok(Ok(WireResponse::Png { job_id, bytes })) if job_id == work.job_id => {
                        if bytes.len() > self.limits.max_output_bytes {
                            self.stats.output_rejections.fetch_add(1, Ordering::Relaxed);
                            return WaitOutcome::Complete(Err(ThumbnailError::OutputTooLarge));
                        }
                        return WaitOutcome::Complete(Ok(bytes));
                    }
                    Ok(Ok(WireResponse::Error { job_id, code })) if job_id == work.job_id => {
                        if code == HelperErrorCode::OutputTooLarge {
                            self.stats.output_rejections.fetch_add(1, Ordering::Relaxed);
                        }
                        return WaitOutcome::Complete(Err(map_helper_error(code)));
                    }
                    Ok(Ok(WireResponse::Ready)) => continue,
                    Ok(Ok(_)) => continue,
                    Ok(Err(WireError::TooLarge)) => {
                        self.stats.output_rejections.fetch_add(1, Ordering::Relaxed);
                        self.force_restart(false);
                        return WaitOutcome::Complete(Err(ThumbnailError::OutputTooLarge));
                    }
                    Ok(Err(_)) | Err(RecvTimeoutError::Disconnected) => {
                        return WaitOutcome::Crashed
                    }
                    Err(RecvTimeoutError::Timeout) => continue,
                }
            }
        }

        fn cancel_running(
            &mut self,
            job_id: u64,
            deadline: bool,
        ) -> Result<Vec<u8>, ThumbnailError> {
            let Some(child) = self.child.as_mut() else {
                return Err(if deadline {
                    ThumbnailError::DeadlineExceeded
                } else {
                    ThumbnailError::Cancelled
                });
            };
            let _ = child.send_cancel(job_id);
            let until = Instant::now() + self.limits.cancel_grace;
            while Instant::now() < until {
                match child.responses.recv_timeout(POLL_INTERVAL) {
                    Ok(Ok(WireResponse::Error {
                        job_id: response_id,
                        code: HelperErrorCode::Cancelled,
                    })) if response_id == job_id => {
                        return Err(if deadline {
                            ThumbnailError::DeadlineExceeded
                        } else {
                            ThumbnailError::Cancelled
                        });
                    }
                    Ok(Ok(WireResponse::Png {
                        job_id: response_id,
                        ..
                    })) if response_id == job_id => {
                        return Err(if deadline {
                            ThumbnailError::DeadlineExceeded
                        } else {
                            ThumbnailError::Cancelled
                        });
                    }
                    Ok(Err(_)) | Err(RecvTimeoutError::Disconnected) => break,
                    _ => {}
                }
            }
            self.force_restart(true);
            Err(if deadline {
                ThumbnailError::DeadlineExceeded
            } else {
                ThumbnailError::Cancelled
            })
        }

        fn force_restart(&mut self, count_restart: bool) {
            if let Some(mut child) = self.child.take() {
                child.kill();
                if count_restart {
                    self.stats.helper_restarts.fetch_add(1, Ordering::Relaxed);
                }
            }
        }

        fn record_crash(&mut self) {
            self.crash_streak = self.crash_streak.saturating_add(1);
            if self.crash_streak >= self.limits.max_consecutive_crashes {
                self.circuit_open_until =
                    Some(Instant::now() + self.limits.circuit_breaker_cooldown);
            }
        }
    }

    enum WaitOutcome {
        Complete(Result<Vec<u8>, ThumbnailError>),
        Crashed,
    }

    struct ChildSession {
        child: Child,
        input: ChildStdin,
        responses: Receiver<Result<WireResponse, WireError>>,
        reader: Option<JoinHandle<()>>,
        token: [u8; wire::SESSION_TOKEN_BYTES],
        max_frame_bytes: usize,
    }

    impl ChildSession {
        fn spawn(
            helper_path: &Path,
            shell_icons: bool,
            limits: &ThumbnailLimits,
            stats: Arc<StatsInner>,
            job_deadline: Instant,
            cancellation: &CancellationToken,
        ) -> Result<Self, ThumbnailError> {
            let token = random_token().map_err(|_| ThumbnailError::HelperUnavailable)?;
            stats.helper_starts.fetch_add(1, Ordering::Relaxed);
            let mut child = Command::new(helper_path)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .env(SESSION_ENV, encode_token(&token))
                .env("ODYSSEUS_SHELL_IMAGE_KIND", if shell_icons { "icon" } else { "thumbnail" })
                .spawn()
                .map_err(|_| ThumbnailError::HelperUnavailable)?;
            let input = child
                .stdin
                .take()
                .ok_or(ThumbnailError::HelperUnavailable)?;
            let mut output = child
                .stdout
                .take()
                .ok_or(ThumbnailError::HelperUnavailable)?;
            let max_output = limits.max_output_bytes;
            let max_frame_bytes = max_output + wire::RESPONSE_OVERHEAD_BYTES;
            let (sender, responses) = mpsc::sync_channel(2);
            let reader = thread::Builder::new()
                .name("odysseus-quicklook-reader".into())
                .spawn(move || loop {
                    let result = wire::read_frame(&mut output, max_frame_bytes)
                        .and_then(|payload| wire::decode_response(&payload, max_output));
                    let stop = result.is_err();
                    if sender.send(result).is_err() || stop {
                        break;
                    }
                })
                .map_err(|_| ThumbnailError::HelperUnavailable)?;
            let mut session = Self {
                child,
                input,
                responses,
                reader: Some(reader),
                token,
                max_frame_bytes: wire::MAX_COMMAND_FRAME_BYTES,
            };
            let start_deadline = Instant::now() + limits.helper_start_deadline;
            loop {
                if cancellation.is_cancelled() {
                    session.kill();
                    return Err(ThumbnailError::Cancelled);
                }
                let now = Instant::now();
                if now >= job_deadline {
                    session.kill();
                    return Err(ThumbnailError::DeadlineExceeded);
                }
                if now >= start_deadline {
                    session.kill();
                    return Err(ThumbnailError::HelperUnavailable);
                }
                let wait = POLL_INTERVAL
                    .min(job_deadline.saturating_duration_since(now))
                    .min(start_deadline.saturating_duration_since(now));
                match session.responses.recv_timeout(wait) {
                    Ok(Ok(WireResponse::Ready)) => return Ok(session),
                    Ok(_) | Err(RecvTimeoutError::Disconnected) => {
                        session.kill();
                        return Err(ThumbnailError::HelperUnavailable);
                    }
                    Err(RecvTimeoutError::Timeout) => {}
                }
            }
        }

        fn send_render(&mut self, work: &Work, max_output_bytes: usize) -> Result<(), WireError> {
            let payload = wire::encode_command(&WireCommand::Render(RenderCommand {
                session_token: self.token,
                job_id: work.job_id,
                width: work.request.width,
                height: work.request.height,
                scale: work.request.scale,
                max_output_bytes: max_output_bytes as u32,
                #[cfg(unix)]
                path: work.path.as_os_str().as_bytes().to_vec(),
                #[cfg(windows)]
                path: work.path.to_str().ok_or(WireError::Malformed)?.as_bytes().to_vec(),
            }))?;
            wire::write_frame(&mut self.input, &payload, self.max_frame_bytes)
        }

        fn send_cancel(&mut self, job_id: u64) -> Result<(), WireError> {
            let payload = wire::encode_command(&WireCommand::Cancel {
                session_token: self.token,
                job_id,
            })?;
            wire::write_frame(&mut self.input, &payload, self.max_frame_bytes)
        }

        fn shutdown(&mut self) {
            if let Ok(payload) = wire::encode_command(&WireCommand::Shutdown {
                session_token: self.token,
            }) {
                let _ = wire::write_frame(&mut self.input, &payload, self.max_frame_bytes);
            }
            let until = Instant::now() + Duration::from_millis(250);
            while Instant::now() < until {
                match self.child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) => thread::sleep(Duration::from_millis(10)),
                    Err(_) => break,
                }
            }
            if self.child.try_wait().ok().flatten().is_none() {
                let _ = self.child.kill();
                let _ = self.child.wait();
            }
            if let Some(reader) = self.reader.take() {
                let _ = reader.join();
            }
        }

        fn kill(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
            if let Some(reader) = self.reader.take() {
                let _ = reader.join();
            }
        }
    }

    impl Drop for ChildSession {
        fn drop(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }

    fn map_helper_error(code: HelperErrorCode) -> ThumbnailError {
        match code {
            HelperErrorCode::NotRegularFile => ThumbnailError::NotEligible,
            HelperErrorCode::IconRepresentationRejected => {
                ThumbnailError::IconRepresentationRejected
            }
            HelperErrorCode::OutputTooLarge => ThumbnailError::OutputTooLarge,
            HelperErrorCode::Cancelled => ThumbnailError::Cancelled,
            _ => ThumbnailError::NativeUnavailable,
        }
    }

    fn random_token() -> std::io::Result<[u8; wire::SESSION_TOKEN_BYTES]> {
        let mut token = [0_u8; wire::SESSION_TOKEN_BYTES];
        #[cfg(windows)]
        getrandom::fill(&mut token)
            .map_err(|_| std::io::Error::other("secure helper session token unavailable"))?;
        #[cfg(not(windows))]
        File::open("/dev/urandom")?.read_exact(&mut token)?;
        Ok(token)
    }

    fn encode_token(token: &[u8; wire::SESSION_TOKEN_BYTES]) -> String {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut encoded = String::with_capacity(token.len() * 2);
        for byte in token {
            encoded.push(HEX[(byte >> 4) as usize] as char);
            encoded.push(HEX[(byte & 0x0f) as usize] as char);
        }
        encoded
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod platform {
    use super::*;

    pub struct PlatformBroker;

    impl PlatformBroker {
        pub fn new(_: &ThumbnailBrokerConfig, _: Arc<StatsInner>) -> Result<Self, ThumbnailError> {
            Ok(Self)
        }

        pub fn render(
            &self,
            _: PathBuf,
            _: ThumbnailRequest,
            _: CancellationToken,
        ) -> Result<Vec<u8>, ThumbnailError> {
            Err(ThumbnailError::UnsupportedPlatform)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_reject_invalid_dimensions_and_unbounded_output() {
        let limits = ThumbnailLimits::default();
        assert_eq!(
            validate_request(&ThumbnailRequest::new(0, 32, 1.0), &limits),
            Err(ThumbnailError::InvalidRequest)
        );
        assert_eq!(
            validate_request(&ThumbnailRequest::new(32, 32, 4.0), &limits),
            Err(ThumbnailError::InvalidRequest)
        );
        let mut invalid_limits = limits;
        invalid_limits.max_output_bytes = crate::quicklook_protocol::MAX_HELPER_OUTPUT_BYTES + 1;
        assert_eq!(
            validate_limits(&invalid_limits),
            Err(ThumbnailError::InvalidRequest)
        );
        invalid_limits.max_output_bytes = 1024;
        invalid_limits.queue_capacity = MAX_QUEUE_CAPACITY + 1;
        assert_eq!(
            validate_limits(&invalid_limits),
            Err(ThumbnailError::InvalidRequest)
        );
    }

    #[test]
    fn public_errors_are_path_blind() {
        for error in [
            ThumbnailError::Denied,
            ThumbnailError::NotEligible,
            ThumbnailError::NativeUnavailable,
            ThumbnailError::Stale,
        ] {
            let display = error.to_string();
            assert!(!display.contains('/'));
            assert!(!display.contains("secret"));
        }
    }
}
