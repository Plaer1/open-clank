//! Opt-in production Tonic transport for the canonical filesystem service.
//!
//! macOS retains the private Unix endpoint and peer UID check. Windows uses a
//! supervisor-owned, already-bound authenticated loopback endpoint. Stable
//! opened-object handles bind streams and native previews to authorized files.
//! The framed stdio lane remains available for unary operations.

use crate::{
    ClientRequest, FileService, Operation, ProtocolError, ProtocolErrorCode,
    ThumbnailCancellationToken, DEFAULT_MAX_FRAME_BYTES, PROTOCOL_VERSION,
};
use notify::{EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde_json::{json, Value};
use std::env;
#[cfg(target_os = "macos")]
use std::fs;
use std::io;
#[cfg(target_os = "macos")]
use std::os::fd::AsRawFd;
#[cfg(target_os = "macos")]
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
#[cfg(target_os = "macos")]
use tokio::net::{UnixListener, UnixStream};
#[cfg(windows)]
use tokio::net::TcpListener;
use tokio::sync::{mpsc, Semaphore};
use tokio_stream::wrappers::ReceiverStream;
#[cfg(target_os = "macos")]
use tokio_stream::wrappers::UnixListenerStream;
#[cfg(windows)]
use tokio_stream::wrappers::TcpListenerStream;
#[cfg(target_os = "macos")]
use tokio_stream::StreamExt;
use tonic::metadata::MetadataValue;
use tonic::transport::Server;
use tonic::{Request, Response, Status};
use tower::limit::ConcurrencyLimitLayer;

pub mod wire {
    tonic::include_proto!("openclank.files.transport.v1");
}

use wire::files_transport_server::{FilesTransport, FilesTransportServer};
use wire::{
    DispatchRequest, DispatchResponse, HealthRequest, HealthResponse, NativeThumbnailRequest,
    NativeThumbnailResponse, ReadChunk, ReadRequest, WatchEvent, WatchRequest,
};

pub const SESSION_HEADER: &str = "x-open-clank-session";
pub const GRPC_SOCKET_ENV: &str = "ODYSSEUS_FILES_GRPC_SOCKET";
pub const GRPC_SESSION_ENV: &str = "ODYSSEUS_FILES_GRPC_SESSION";
pub const MIN_CHUNK_BYTES: u32 = 64 * 1024;
pub const MAX_CHUNK_BYTES: u32 = 256 * 1024;
pub const MAX_STREAM_BYTES: u64 = 250 * 1024 * 1024;
pub const PRODUCER_QUEUE_CHUNKS: usize = 4;
const STREAM_CONCURRENCY: usize = 2;
const THUMBNAIL_CONCURRENCY: usize = 4;
const DISPATCH_CONCURRENCY: usize = 32;
const WATCH_CONCURRENCY: usize = 16;
const WATCH_CALLBACK_QUEUE: usize = 128;
const WATCH_OUTPUT_QUEUE: usize = 32;
const MAX_ENCODED_UNARY_BYTES: usize = DEFAULT_MAX_FRAME_BYTES + 4096;

#[derive(Clone)]
struct ProductionFilesTransport {
    service: Arc<FileService>,
    dispatch_slots: Arc<Semaphore>,
    stream_slots: Arc<Semaphore>,
    thumbnail_slots: Arc<Semaphore>,
    watch_slots: Arc<Semaphore>,
}

impl ProductionFilesTransport {
    fn new(service: Arc<FileService>) -> Self {
        Self {
            service,
            dispatch_slots: Arc::new(Semaphore::new(DISPATCH_CONCURRENCY)),
            stream_slots: Arc::new(Semaphore::new(STREAM_CONCURRENCY)),
            thumbnail_slots: Arc::new(Semaphore::new(THUMBNAIL_CONCURRENCY)),
            watch_slots: Arc::new(Semaphore::new(WATCH_CONCURRENCY)),
        }
    }
}

#[tonic::async_trait]
impl FilesTransport for ProductionFilesTransport {
    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        Ok(Response::new(HealthResponse {
            ready: true,
            transport: if cfg!(windows) { "authenticated-loopback-tcp" } else { "private-unix-domain-socket" }.into(),
            protocol_major: PROTOCOL_VERSION.major.into(),
            protocol_minor: PROTOCOL_VERSION.minor.into(),
            max_unary_bytes: DEFAULT_MAX_FRAME_BYTES as u32,
            max_chunk_bytes: MAX_CHUNK_BYTES,
            producer_queue_chunks: PRODUCER_QUEUE_CHUNKS as u32,
            // Regular-file read/stat/thumbnail handles are descriptor-bound.
            // Directory and mutation handles remain a later protocol seam.
            stable_object_handles: true,
            supports_watch: true,
            supports_watch_resume: false,
        }))
    }

    async fn dispatch(
        &self,
        request: Request<DispatchRequest>,
    ) -> Result<Response<DispatchResponse>, Status> {
        let envelope = request.into_inner().envelope_json;
        if envelope.len() > DEFAULT_MAX_FRAME_BYTES {
            return Err(Status::resource_exhausted(
                "filesystem request exceeds the unary limit",
            ));
        }
        let _permit = self
            .dispatch_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("filesystem dispatch queue is full"))?;
        let service = Arc::clone(&self.service);
        let response = tokio::task::spawn_blocking(move || dispatch_json(&service, &envelope))
            .await
            .map_err(|_| Status::unavailable("filesystem dispatch worker stopped"))?;
        if response.len() > DEFAULT_MAX_FRAME_BYTES {
            return Err(Status::resource_exhausted(
                "filesystem response exceeds the unary limit",
            ));
        }
        Ok(Response::new(DispatchResponse {
            envelope_json: response,
        }))
    }

    type ReadStream = ReceiverStream<Result<ReadChunk, Status>>;

    async fn read(
        &self,
        request: Request<ReadRequest>,
    ) -> Result<Response<Self::ReadStream>, Status> {
        let request = request.into_inner();
        validate_read_request(&request)?;
        let base_request: ClientRequest = serde_json::from_slice(&request.envelope_json)
            .map_err(|_| Status::invalid_argument("read envelope is invalid"))?;
        if base_request.operation != Operation::ReadRange {
            return Err(Status::invalid_argument(
                "streaming read requires a read_range envelope",
            ));
        }
        let permit = self
            .stream_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("read stream concurrency is exhausted"))?;
        let service = Arc::clone(&self.service);
        let (sender, receiver) = mpsc::channel(PRODUCER_QUEUE_CHUNKS);
        tokio::spawn(async move {
            let _permit = permit;
            let mut offset = request.offset;
            let end = request.offset.saturating_add(request.length);
            let mut sequence = 0_u64;
            loop {
                let remaining = end.saturating_sub(offset);
                let length = remaining.min(u64::from(request.chunk_bytes)) as usize;
                if length == 0 {
                    return;
                }
                let mut page_request = base_request.clone();
                page_request.payload = json!({
                    "offset": offset,
                    "length": length,
                    "include_fingerprint": false,
                });
                let page_service = Arc::clone(&service);
                let result =
                    tokio::task::spawn_blocking(move || read_page(&page_service, page_request))
                        .await;
                let page = match result {
                    Ok(Ok(page)) => page,
                    Ok(Err(status)) => {
                        let _ = sender.send(Err(status)).await;
                        return;
                    }
                    Err(_) => {
                        let _ = sender
                            .send(Err(Status::unavailable("filesystem read worker stopped")))
                            .await;
                        return;
                    }
                };
                let read = page.data.len() as u64;
                let next_offset = offset.saturating_add(read);
                let final_chunk = page.eof || next_offset >= end || read == 0;
                let chunk = ReadChunk {
                    request_id: base_request.request_id.clone(),
                    sequence,
                    offset,
                    data: page.data,
                    final_chunk,
                    eof: page.eof,
                };
                // A dropped grpc.aio call closes the receiver; this await then
                // releases the stream task and its permit without filling an
                // unbounded buffer.
                if sender.send(Ok(chunk)).await.is_err() || final_chunk {
                    return;
                }
                offset = next_offset;
                sequence += 1;
            }
        });
        Ok(Response::new(ReceiverStream::new(receiver)))
    }

    async fn thumbnail(
        &self,
        request: Request<NativeThumbnailRequest>,
    ) -> Result<Response<NativeThumbnailResponse>, Status> {
        let envelope = request.into_inner().envelope_json;
        if envelope.len() > DEFAULT_MAX_FRAME_BYTES {
            return Err(Status::resource_exhausted(
                "thumbnail request exceeds the unary limit",
            ));
        }
        let request: ClientRequest = serde_json::from_slice(&envelope)
            .map_err(|_| Status::invalid_argument("thumbnail envelope is invalid"))?;
        if request.operation != Operation::Thumbnail {
            return Err(Status::invalid_argument(
                "thumbnail RPC requires a thumbnail operation",
            ));
        }
        let _permit = self
            .thumbnail_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("thumbnail concurrency is exhausted"))?;
        let cancellation = ThumbnailCancellationToken::new();
        let mut cancel_on_drop = CancelOnDrop::new(cancellation.clone());
        let service = Arc::clone(&self.service);
        let result =
            tokio::task::spawn_blocking(move || service.render_thumbnail(request, cancellation))
                .await
                .map_err(|_| Status::unavailable("thumbnail worker stopped"))?
                .map_err(protocol_status)?;
        cancel_on_drop.disarm();
        Ok(Response::new(NativeThumbnailResponse {
            png: result.bytes,
            requested_width: result.requested_width,
            requested_height: result.requested_height,
            scale_milli: result.scale_milli,
        }))
    }

    type WatchStream = ReceiverStream<Result<WatchEvent, Status>>;

    async fn watch(
        &self,
        request: Request<WatchRequest>,
    ) -> Result<Response<Self::WatchStream>, Status> {
        let envelope = request.into_inner().envelope_json;
        if envelope.is_empty() || envelope.len() > DEFAULT_MAX_FRAME_BYTES {
            return Err(Status::invalid_argument(
                "watch envelope must fit the unary request limit",
            ));
        }
        let request: ClientRequest = serde_json::from_slice(&envelope)
            .map_err(|_| Status::invalid_argument("watch envelope is invalid"))?;
        let authorized = self
            .service
            .authorize_watch(request)
            .map_err(protocol_status)?;
        let permit = self
            .watch_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("watch concurrency is exhausted"))?;

        let (callback_sender, mut callback_receiver) = mpsc::channel(WATCH_CALLBACK_QUEUE);
        let overflowed = Arc::new(AtomicBool::new(false));
        let callback_overflow = Arc::clone(&overflowed);
        let mut watcher = RecommendedWatcher::new(
            move |event| {
                if callback_sender.try_send(event).is_err() {
                    callback_overflow.store(true, Ordering::Release);
                }
            },
            notify::Config::default(),
        )
        .map_err(|_| Status::unavailable("native filesystem watcher is unavailable"))?;
        #[cfg(windows)]
        let watch_parent_pins = crate::windows_fs::pin_parent(&authorized.path).map_err(|_| Status::permission_denied("watch directory authority changed"))?;
        #[cfg(windows)]
        let watch_directory_pin = crate::windows_fs::pin_directory(&authorized.path).map_err(|_| Status::permission_denied("watch directory authority changed"))?;
        watcher
            .watch(&authorized.path, RecursiveMode::NonRecursive)
            .map_err(|_| Status::unavailable("authorized directory cannot be watched"))?;

        #[cfg(windows)]
        drop((watch_parent_pins, watch_directory_pin));
        let (sender, receiver) = mpsc::channel(WATCH_OUTPUT_QUEUE);
        tokio::spawn(async move {
            let _permit = permit;
            // The watcher is deliberately owned by this task. Dropping the
            // browser/grpc stream closes the output receiver; the next send
            // exits and drops the native watcher without a detached watch.
            let _watcher = watcher;
            let mut sequence = 0_u64;
            loop {
                let event = tokio::select! {
                    _ = sender.closed() => return,
                    event = callback_receiver.recv() => match event { Some(event) => event, None => return },
                };
                let overflow = overflowed.swap(false, Ordering::AcqRel);
                let mapped = if overflow {
                    Some(("rescan_required", true))
                } else {
                    match event {
                        Ok(event) if event.need_rescan() => Some(("rescan_required", true)),
                        Ok(event) => event_kind(&event.kind),
                        Err(_) => Some(("rescan_required", true)),
                    }
                };
                let Some((kind, rescan_required)) = mapped else {
                    continue;
                };
                let output = WatchEvent {
                    request_id: authorized.request_id.clone(),
                    sequence,
                    kind: kind.into(),
                    rescan_required,
                    observed_unix_ms: observed_unix_ms(),
                    policy_generation: authorized.generation,
                };
                sequence = sequence.saturating_add(1);
                if sender.send(Ok(output)).await.is_err() {
                    return;
                }
            }
        });
        Ok(Response::new(ReceiverStream::new(receiver)))
    }
}

fn event_kind(kind: &EventKind) -> Option<(&'static str, bool)> {
    match kind {
        EventKind::Create(_) => Some(("created", false)),
        EventKind::Modify(_) => Some(("modified", false)),
        EventKind::Remove(_) => Some(("deleted", false)),
        // Merely reading/previewing an item must not trigger a refresh loop.
        EventKind::Access(_) => None,
        EventKind::Any | EventKind::Other => Some(("rescan_required", true)),
    }
}

fn observed_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or(0)
}

struct CancelOnDrop {
    token: ThumbnailCancellationToken,
    armed: bool,
}

impl CancelOnDrop {
    fn new(token: ThumbnailCancellationToken) -> Self {
        Self { token, armed: true }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        if self.armed {
            self.token.cancel();
        }
    }
}

struct ReadPage {
    data: Vec<u8>,
    eof: bool,
}

fn read_page(service: &FileService, request: ClientRequest) -> Result<ReadPage, Status> {
    let response = service.dispatch(request).map_err(protocol_status)?;
    let data = response
        .pointer("/data/bytes")
        .and_then(Value::as_array)
        .ok_or_else(|| Status::internal("filesystem read response has no byte payload"))?;
    let mut bytes = Vec::with_capacity(data.len());
    for value in data {
        let byte = value
            .as_u64()
            .and_then(|value| u8::try_from(value).ok())
            .ok_or_else(|| Status::internal("filesystem read response contains invalid bytes"))?;
        bytes.push(byte);
    }
    let eof = response
        .pointer("/data/eof")
        .and_then(Value::as_bool)
        .ok_or_else(|| Status::internal("filesystem read response has no eof marker"))?;
    Ok(ReadPage { data: bytes, eof })
}

fn dispatch_json(service: &FileService, bytes: &[u8]) -> Vec<u8> {
    let response = match serde_json::from_slice::<ClientRequest>(bytes) {
        Ok(request) => match service.dispatch(request) {
            Ok(value) => value,
            Err(error) => serde_json::to_value(error).unwrap_or_else(
                |_| json!({"code": "root_unavailable", "message": "service error"}),
            ),
        },
        Err(error) => json!({
            "code": "malformed_request",
            "message": "request envelope is invalid",
            "details": {"parse": error.to_string()},
        }),
    };
    serde_json::to_vec(&response).unwrap_or_else(|_| {
        b"{\"code\":\"root_unavailable\",\"message\":\"serialization failure\"}".to_vec()
    })
}

fn validate_read_request(request: &ReadRequest) -> Result<(), Status> {
    if request.envelope_json.is_empty() || request.envelope_json.len() > DEFAULT_MAX_FRAME_BYTES {
        return Err(Status::invalid_argument(
            "read envelope must fit the unary request limit",
        ));
    }
    if request.length == 0 || request.length > MAX_STREAM_BYTES {
        return Err(Status::invalid_argument(
            "read length must be in 1..=250 MiB",
        ));
    }
    if request.offset.checked_add(request.length).is_none() {
        return Err(Status::invalid_argument("read range overflows u64"));
    }
    if !(MIN_CHUNK_BYTES..=MAX_CHUNK_BYTES).contains(&request.chunk_bytes) {
        return Err(Status::invalid_argument(
            "chunk_bytes must be in 64..=256 KiB",
        ));
    }
    Ok(())
}

fn protocol_status(error: ProtocolError) -> Status {
    let message = error.message;
    match error.code {
        ProtocolErrorCode::MalformedRequest | ProtocolErrorCode::ProtocolMismatch => {
            Status::invalid_argument(message)
        }
        ProtocolErrorCode::Unauthorized | ProtocolErrorCode::CrossOwner => {
            Status::unauthenticated(message)
        }
        ProtocolErrorCode::Denied => Status::permission_denied(message),
        ProtocolErrorCode::InvalidPath | ProtocolErrorCode::StaleHandle => {
            Status::failed_precondition(message)
        }
        ProtocolErrorCode::StaleCursor | ProtocolErrorCode::Conflict => Status::aborted(message),
        ProtocolErrorCode::DeadlineExceeded => Status::deadline_exceeded(message),
        ProtocolErrorCode::Cancelled => Status::cancelled(message),
        ProtocolErrorCode::Backpressure => Status::resource_exhausted(message),
        ProtocolErrorCode::Unsupported => Status::unimplemented(message),
        ProtocolErrorCode::RootUnavailable
        | ProtocolErrorCode::Crashed
        | ProtocolErrorCode::PartialStream => Status::unavailable(message),
    }
}

#[cfg(target_os = "macos")]
struct SocketGuard {
    path: PathBuf,
    device: u64,
    inode: u64,
}

#[cfg(target_os = "macos")]
impl SocketGuard {
    fn capture(path: PathBuf) -> io::Result<Self> {
        let metadata = fs::symlink_metadata(&path)?;
        Ok(Self {
            path,
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
}

#[cfg(target_os = "macos")]
impl Drop for SocketGuard {
    fn drop(&mut self) {
        let Ok(metadata) = fs::symlink_metadata(&self.path) else {
            return;
        };
        if metadata.file_type().is_socket()
            && metadata.dev() == self.device
            && metadata.ino() == self.inode
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}

#[cfg(target_os = "macos")]
pub fn validate_socket_path(path: &Path) -> io::Result<()> {
    if !path.is_absolute() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "gRPC socket path must be absolute",
        ));
    }
    if path.as_os_str().as_encoded_bytes().len() > 96 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "gRPC socket path is too long for the macOS endpoint",
        ));
    }
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "gRPC socket has no parent"))?;
    let metadata = fs::symlink_metadata(parent)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "gRPC socket parent must be a real directory",
        ));
    }
    if metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "gRPC socket parent must be owner-owned mode 0700",
        ));
    }
    if fs::symlink_metadata(path).is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "gRPC socket path already exists and is never replaced implicitly",
        ));
    }
    Ok(())
}

pub fn session_binding_from_env() -> io::Result<String> {
    let value = env::var(GRPC_SESSION_ENV).map_err(|_| {
        io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("{GRPC_SESSION_ENV} is required"),
        )
    })?;
    validate_session_binding(&value)?;
    Ok(value)
}

fn validate_session_binding(value: &str) -> io::Result<()> {
    if !(32..=128).contains(&value.len())
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "gRPC session binding must be 32..=128 safe ASCII bytes",
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn verify_peer(stream: &UnixStream) -> io::Result<()> {
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    let result = unsafe { libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    if uid != unsafe { libc::geteuid() } {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "gRPC peer UID does not match the service owner",
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
pub async fn serve(
    service: Arc<FileService>,
    socket_path: PathBuf,
    expected_session: String,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    validate_socket_path(&socket_path)?;
    let listener = UnixListener::bind(&socket_path)?;
    fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))?;
    let socket_metadata = fs::symlink_metadata(&socket_path)?;
    if !socket_metadata.file_type().is_socket()
        || socket_metadata.uid() != unsafe { libc::geteuid() }
        || socket_metadata.permissions().mode() & 0o777 != 0o600
    {
        return Err("created gRPC endpoint did not retain owner-only permissions".into());
    }
    let _socket_guard = SocketGuard::capture(socket_path)?;
    let session_value: MetadataValue<_> = expected_session.parse()?;
    let transport = FilesTransportServer::new(ProductionFilesTransport::new(service))
        .max_decoding_message_size(MAX_ENCODED_UNARY_BYTES)
        .max_encoding_message_size(MAX_ENCODED_UNARY_BYTES);
    let transport = tonic::service::interceptor::InterceptedService::new(
        transport,
        move |request: Request<()>| {
            if request.metadata().get(SESSION_HEADER) != Some(&session_value) {
                return Err(Status::unauthenticated("invalid local session binding"));
            }
            Ok(request)
        },
    );
    let incoming = UnixListenerStream::new(listener).filter_map(|result| match result {
        Ok(stream) => match verify_peer(&stream) {
            Ok(()) => Some(Ok(stream)),
            Err(error) => {
                eprintln!("rejected gRPC Unix peer: {error}");
                None
            }
        },
        Err(error) => Some(Err(error)),
    });
    eprintln!(
        "odysseus-files-service ready: transport=grpc-uds socket_mode=0600 parent_mode=0700 stable_handles=true"
    );
    Server::builder()
        .layer(ConcurrencyLimitLayer::new(64))
        .add_service(transport)
        .serve_with_incoming_shutdown(incoming, shutdown_signal())
        .await?;
    Ok(())
}

#[cfg(target_os = "macos")]
async fn shutdown_signal() {
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {},
        _ = terminate.recv() => {},
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_bounds_are_explicit() {
        let mut request = ReadRequest {
            envelope_json: br#"{"protocol":{"major":1,"minor":0}}"#.to_vec(),
            offset: 0,
            length: MAX_STREAM_BYTES,
            chunk_bytes: MAX_CHUNK_BYTES,
        };
        assert!(validate_read_request(&request).is_ok());
        request.length += 1;
        assert!(validate_read_request(&request).is_err());
        request.length = 1;
        request.chunk_bytes += 1;
        assert!(validate_read_request(&request).is_err());
    }

    #[test]
    fn socket_validation_never_replaces_an_existing_entry() {
        let directory = tempfile::tempdir().unwrap();
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = directory.path().join("files.sock");
        fs::write(&path, b"keep").unwrap();
        assert_eq!(
            validate_socket_path(&path).unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        assert_eq!(fs::read(path).unwrap(), b"keep");
    }

    #[test]
    fn socket_validation_requires_a_private_parent() {
        let directory = tempfile::tempdir().unwrap();
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(
            validate_socket_path(&directory.path().join("files.sock"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn session_binding_format_is_strict() {
        assert!(validate_session_binding(&"a".repeat(32)).is_ok());
        assert!(validate_session_binding("short").is_err());
        assert!(validate_session_binding(&format!("{}!", "a".repeat(31))).is_err());
    }

    #[test]
    fn encoded_unary_ceiling_covers_the_chunk_payload() {
        assert!(MAX_ENCODED_UNARY_BYTES > MAX_CHUNK_BYTES as usize);
    }

    #[test]
    fn dropped_thumbnail_rpc_propagates_cancellation() {
        let token = ThumbnailCancellationToken::new();
        {
            let _guard = CancelOnDrop::new(token.clone());
        }
        assert!(token.is_cancelled());

        let token = ThumbnailCancellationToken::new();
        {
            let mut guard = CancelOnDrop::new(token.clone());
            guard.disarm();
        }
        assert!(!token.is_cancelled());
    }

    #[test]
    fn watcher_ignores_access_and_escalates_unknown_native_events() {
        assert_eq!(
            event_kind(&EventKind::Access(notify::event::AccessKind::Any)),
            None
        );
        assert_eq!(event_kind(&EventKind::Any), Some(("rescan_required", true)));
        assert_eq!(
            event_kind(&EventKind::Modify(notify::event::ModifyKind::Any)),
            Some(("modified", false))
        );
    }
}

#[cfg(windows)]
pub async fn serve(service: Arc<FileService>, endpoint: PathBuf, expected_session: String)
    -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    if endpoint != Path::new("loopback") { return Err("Windows Files endpoint must be supervisor-selected loopback".into()); }
    validate_session_binding(&expected_session)?;
    // Bind port zero once: stdout returns the already-bound endpoint to the owning supervisor.
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
    let endpoint = listener.local_addr()?;
    let session_value: MetadataValue<_> = expected_session.parse()?;
    let transport = FilesTransportServer::new(ProductionFilesTransport::new(service))
        .max_decoding_message_size(MAX_ENCODED_UNARY_BYTES)
        .max_encoding_message_size(MAX_ENCODED_UNARY_BYTES);
    let transport = tonic::service::interceptor::InterceptedService::new(transport, move |request: Request<()>| {
        if request.metadata().get(SESSION_HEADER) != Some(&session_value) {
            return Err(Status::unauthenticated("invalid local session binding"));
        }
        Ok(request)
    });
    use std::io::Write;
    writeln!(std::io::stdout(), "{endpoint}")?;
    std::io::stdout().flush()?;
    Server::builder().layer(ConcurrencyLimitLayer::new(64)).add_service(transport)
        .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async { let _ = tokio::signal::ctrl_c().await; }).await?;
    Ok(())
}
