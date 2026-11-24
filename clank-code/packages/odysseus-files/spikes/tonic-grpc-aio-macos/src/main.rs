use std::env;
use std::fs;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{mpsc, OwnedSemaphorePermit, Semaphore};
use tokio_stream::wrappers::{ReceiverStream, UnixListenerStream};
use tokio_stream::StreamExt;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Server;
use tonic::{Request, Response, Status};
use tower::limit::ConcurrencyLimitLayer;

pub mod wire {
    tonic::include_proto!("openclank.files.admission.v1");
}

use wire::admission_server::{Admission, AdmissionServer};
use wire::{
    BrowseEntry, BrowseRequest, BrowseResponse, HealthRequest, HealthResponse, MetricsRequest,
    MetricsResponse, ShutdownRequest, ShutdownResponse, TransferChunk, TransferRequest,
};

const SESSION_HEADER: &str = "x-open-clank-session";
const MIN_CHUNK_BYTES: u32 = 64 * 1024;
const MAX_CHUNK_BYTES: u32 = 256 * 1024;
const MAX_TRANSFER_BYTES: u64 = 250 * 1024 * 1024;
const PRODUCER_QUEUE_CHUNKS: usize = 4;
const STREAM_CONCURRENCY: usize = 2;
const BROWSE_CONCURRENCY: usize = 32;
const MAX_ENCODED_CHUNK_BYTES: usize = MAX_CHUNK_BYTES as usize + 4096;

#[derive(Default)]
struct Counters {
    active_transfers: AtomicU64,
    completed_transfers: AtomicU64,
    cancelled_transfers: AtomicU64,
    emitted_bytes: AtomicU64,
    max_producer_queue_chunks: AtomicU64,
}

#[derive(Clone)]
struct AdmissionService {
    counters: Arc<Counters>,
    stream_slots: Arc<Semaphore>,
    browse_slots: Arc<Semaphore>,
    shutdown: CancellationToken,
}

impl AdmissionService {
    fn new(shutdown: CancellationToken) -> Self {
        Self {
            counters: Arc::new(Counters::default()),
            stream_slots: Arc::new(Semaphore::new(STREAM_CONCURRENCY)),
            browse_slots: Arc::new(Semaphore::new(BROWSE_CONCURRENCY)),
            shutdown,
        }
    }
}

struct TransferGuard {
    counters: Arc<Counters>,
    _permit: OwnedSemaphorePermit,
    complete: bool,
}

impl TransferGuard {
    fn new(counters: Arc<Counters>, permit: OwnedSemaphorePermit) -> Self {
        counters.active_transfers.fetch_add(1, Ordering::SeqCst);
        Self {
            counters,
            _permit: permit,
            complete: false,
        }
    }

    fn mark_complete(&mut self) {
        self.complete = true;
    }
}

impl Drop for TransferGuard {
    fn drop(&mut self) {
        self.counters
            .active_transfers
            .fetch_sub(1, Ordering::SeqCst);
        if self.complete {
            self.counters
                .completed_transfers
                .fetch_add(1, Ordering::SeqCst);
        } else {
            self.counters
                .cancelled_transfers
                .fetch_add(1, Ordering::SeqCst);
        }
    }
}

#[tonic::async_trait]
impl Admission for AdmissionService {
    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        Ok(Response::new(HealthResponse {
            ready: true,
            transport: "unix-domain-socket".into(),
            chunk_limit_bytes: MAX_CHUNK_BYTES,
            producer_queue_chunks: PRODUCER_QUEUE_CHUNKS as u32,
        }))
    }

    async fn browse(
        &self,
        request: Request<BrowseRequest>,
    ) -> Result<Response<BrowseResponse>, Status> {
        let _permit = self
            .browse_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("browse admission queue is full"))?;
        let page_size = request.into_inner().page_size;
        if page_size == 0 || page_size > 200 {
            return Err(Status::invalid_argument("page_size must be in 1..=200"));
        }
        let entries = (0..page_size)
            .map(|index| BrowseEntry {
                opaque_id: format!("admission:{index:04}"),
                display_name: format!("synthetic-{index:04}.bin"),
                size: (index as u64 + 1) * 4096,
            })
            .collect();
        Ok(Response::new(BrowseResponse {
            entries,
            next_cursor: "admission:next".into(),
        }))
    }

    type TransferStream = ReceiverStream<Result<TransferChunk, Status>>;

    async fn transfer(
        &self,
        request: Request<TransferRequest>,
    ) -> Result<Response<Self::TransferStream>, Status> {
        let request = request.into_inner();
        validate_transfer(&request)?;
        let permit = self
            .stream_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("transfer concurrency is exhausted"))?;
        let counters = Arc::clone(&self.counters);
        let (sender, receiver) = mpsc::channel(PRODUCER_QUEUE_CHUNKS);
        tokio::spawn(async move {
            let mut guard = TransferGuard::new(Arc::clone(&counters), permit);
            let mut offset = 0_u64;
            let mut sequence = 0_u64;
            while offset < request.total_bytes {
                if request.producer_delay_micros > 0 {
                    tokio::time::sleep(Duration::from_micros(request.producer_delay_micros.into()))
                        .await;
                }
                let remaining = request.total_bytes - offset;
                let length = remaining.min(request.chunk_bytes.into()) as usize;
                let fill = (sequence % 251) as u8;
                let chunk = TransferChunk {
                    transfer_id: request.transfer_id.clone(),
                    sequence,
                    offset,
                    data: vec![fill; length],
                    final_chunk: offset + length as u64 == request.total_bytes,
                };
                if sender.send(Ok(chunk)).await.is_err() {
                    return;
                }
                counters
                    .emitted_bytes
                    .fetch_add(length as u64, Ordering::Relaxed);
                let queued = PRODUCER_QUEUE_CHUNKS - sender.capacity();
                update_max(&counters.max_producer_queue_chunks, queued as u64);
                offset += length as u64;
                sequence += 1;
            }
            guard.mark_complete();
        });
        Ok(Response::new(ReceiverStream::new(receiver)))
    }

    async fn metrics(
        &self,
        _request: Request<MetricsRequest>,
    ) -> Result<Response<MetricsResponse>, Status> {
        Ok(Response::new(MetricsResponse {
            active_transfers: self.counters.active_transfers.load(Ordering::SeqCst),
            completed_transfers: self.counters.completed_transfers.load(Ordering::SeqCst),
            cancelled_transfers: self.counters.cancelled_transfers.load(Ordering::SeqCst),
            emitted_bytes: self.counters.emitted_bytes.load(Ordering::Relaxed),
            max_producer_queue_chunks: self
                .counters
                .max_producer_queue_chunks
                .load(Ordering::Relaxed),
            producer_queue_capacity: PRODUCER_QUEUE_CHUNKS as u32,
        }))
    }

    async fn shutdown(
        &self,
        _request: Request<ShutdownRequest>,
    ) -> Result<Response<ShutdownResponse>, Status> {
        let shutdown = self.shutdown.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(20)).await;
            shutdown.cancel();
        });
        Ok(Response::new(ShutdownResponse { accepted: true }))
    }
}

fn validate_transfer(request: &TransferRequest) -> Result<(), Status> {
    if request.transfer_id.is_empty() || request.transfer_id.len() > 128 {
        return Err(Status::invalid_argument(
            "transfer_id must be an opaque value of 1..=128 bytes",
        ));
    }
    if request.total_bytes == 0 || request.total_bytes > MAX_TRANSFER_BYTES {
        return Err(Status::invalid_argument(
            "total_bytes exceeds the 250 MiB admission ceiling",
        ));
    }
    if !(MIN_CHUNK_BYTES..=MAX_CHUNK_BYTES).contains(&request.chunk_bytes) {
        return Err(Status::invalid_argument(
            "chunk_bytes must be in the 64..=256 KiB admission range",
        ));
    }
    if request.producer_delay_micros > 100_000 {
        return Err(Status::invalid_argument(
            "producer delay exceeds the bounded admission range",
        ));
    }
    Ok(())
}

fn update_max(target: &AtomicU64, candidate: u64) {
    let mut current = target.load(Ordering::Relaxed);
    while candidate > current {
        match target.compare_exchange_weak(current, candidate, Ordering::Relaxed, Ordering::Relaxed)
        {
            Ok(_) => return,
            Err(actual) => current = actual,
        }
    }
}

struct SocketGuard {
    path: PathBuf,
    device: u64,
    inode: u64,
}

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

fn validate_socket_path(path: &Path) -> io::Result<()> {
    if !path.is_absolute() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "socket path must be absolute",
        ));
    }
    if path.as_os_str().as_encoded_bytes().len() > 96 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "socket path is too long for the bounded macOS endpoint",
        ));
    }
    let parent = path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "socket has no parent directory",
        )
    })?;
    let metadata = fs::symlink_metadata(parent)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "socket parent must be a real directory",
        ));
    }
    if metadata.uid() != unsafe { libc::geteuid() } {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "socket parent is not owned by the effective user",
        ));
    }
    if metadata.permissions().mode() & 0o777 != 0o700 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "socket parent must have mode 0700",
        ));
    }
    if fs::symlink_metadata(path).is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "socket path already exists; stale paths are never removed implicitly",
        ));
    }
    Ok(())
}

fn verify_peer(stream: &UnixStream) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        let mut uid: libc::uid_t = 0;
        let mut gid: libc::gid_t = 0;
        let result = unsafe { libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) };
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        if uid != unsafe { libc::geteuid() } {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Unix peer UID does not match the service owner",
            ));
        }
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = stream;
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "this admission spike requires macOS getpeereid",
        ))
    }
}

fn parse_socket_argument() -> io::Result<PathBuf> {
    let mut arguments = env::args_os().skip(1);
    let flag = arguments.next();
    let value = arguments.next();
    if flag.as_deref() != Some(std::ffi::OsStr::new("--socket"))
        || value.is_none()
        || arguments.next().is_some()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "usage: openclank-tonic-admission --socket /absolute/private/path.sock",
        ));
    }
    Ok(PathBuf::from(value.expect("checked above")))
}

fn session_binding() -> io::Result<String> {
    let value = env::var("OPENCLANK_ADMISSION_SESSION").map_err(|_| {
        io::Error::new(
            io::ErrorKind::PermissionDenied,
            "OPENCLANK_ADMISSION_SESSION is required",
        )
    })?;
    if !(32..=128).contains(&value.len())
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "session binding must be 32..=128 safe ASCII bytes",
        ));
    }
    Ok(value)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(not(target_os = "macos"))]
    return Err("this bounded admission executable is macOS-only".into());

    #[cfg(target_os = "macos")]
    {
        let socket_path = parse_socket_argument()?;
        validate_socket_path(&socket_path)?;
        let expected_session = session_binding()?;
        let listener = UnixListener::bind(&socket_path)?;
        fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))?;
        let socket_metadata = fs::symlink_metadata(&socket_path)?;
        if !socket_metadata.file_type().is_socket()
            || socket_metadata.uid() != unsafe { libc::geteuid() }
            || socket_metadata.permissions().mode() & 0o777 != 0o600
        {
            return Err("created endpoint did not retain its owner-only contract".into());
        }
        let _socket_guard = SocketGuard::capture(socket_path.clone())?;
        let shutdown = CancellationToken::new();
        let service = AdmissionService::new(shutdown.clone());
        let session_value: MetadataValue<_> = expected_session.parse()?;
        let admission = AdmissionServer::new(service)
            .max_decoding_message_size(MAX_ENCODED_CHUNK_BYTES)
            .max_encoding_message_size(MAX_ENCODED_CHUNK_BYTES);
        let admission = tonic::service::interceptor::InterceptedService::new(
            admission,
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
                    eprintln!("rejected Unix peer: {error}");
                    None
                }
            },
            Err(error) => Some(Err(error)),
        });
        eprintln!(
            "openclank-tonic-admission ready transport=unix socket_mode=0600 parent_mode=0700"
        );
        Server::builder()
            .layer(ConcurrencyLimitLayer::new(64))
            .add_service(admission)
            .serve_with_incoming_shutdown(incoming, shutdown.cancelled_owned())
            .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transfer_bounds_are_explicit() {
        let valid = TransferRequest {
            transfer_id: "opaque-transfer".into(),
            total_bytes: MAX_TRANSFER_BYTES,
            chunk_bytes: MAX_CHUNK_BYTES,
            producer_delay_micros: 0,
        };
        assert!(validate_transfer(&valid).is_ok());
        let mut oversized = valid.clone();
        oversized.total_bytes += 1;
        assert!(validate_transfer(&oversized).is_err());
        let mut oversized_chunk = valid;
        oversized_chunk.chunk_bytes += 1;
        assert!(validate_transfer(&oversized_chunk).is_err());
    }

    #[test]
    fn socket_path_fails_closed_on_existing_entry() {
        let directory = tempfile::tempdir().expect("private temporary directory");
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = directory.path().join("admission.sock");
        fs::write(&path, b"do not replace").unwrap();
        let error = validate_socket_path(&path).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(fs::read(path).unwrap(), b"do not replace");
    }

    #[test]
    fn socket_path_requires_private_parent() {
        let directory = tempfile::tempdir().expect("temporary directory");
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o755)).unwrap();
        let error = validate_socket_path(&directory.path().join("admission.sock")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
    }

    #[test]
    fn relative_socket_path_is_rejected() {
        let error = validate_socket_path(Path::new("admission.sock")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }
}
