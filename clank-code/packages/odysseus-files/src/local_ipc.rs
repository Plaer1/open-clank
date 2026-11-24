use crate::{encode_frame, FrameDecoder, FrameError, DEFAULT_MAX_FRAME_BYTES};
use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::fs::FileTypeExt;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};

/// Long-lived local IPC endpoint candidate. The socket is deliberately created
/// with owner-only permissions and never binds a TCP/public listener.
pub struct LocalIpcServer {
    listener: UnixListener,
    path: PathBuf,
    max_frame_bytes: usize,
}

impl LocalIpcServer {
    pub fn bind(path: impl Into<PathBuf>, max_frame_bytes: usize) -> io::Result<Self> {
        let path = path.into();
        if path.exists() {
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.file_type().is_socket() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "IPC path is not a socket",
                ));
            }
            fs::remove_file(&path)?;
        }
        let listener = UnixListener::bind(&path)?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
        Ok(Self {
            listener,
            path,
            max_frame_bytes,
        })
    }

    pub fn accept(&self) -> io::Result<FramedUnixStream> {
        let (stream, _) = self.listener.accept()?;
        Ok(FramedUnixStream::new(stream, self.max_frame_bytes))
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for LocalIpcServer {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

pub struct FramedUnixStream {
    stream: UnixStream,
    decoder: FrameDecoder,
}

impl FramedUnixStream {
    pub fn connect(path: impl AsRef<Path>, max_frame_bytes: usize) -> io::Result<Self> {
        Ok(Self::new(UnixStream::connect(path)?, max_frame_bytes))
    }

    fn new(stream: UnixStream, max_frame_bytes: usize) -> Self {
        Self {
            stream,
            decoder: FrameDecoder::new(max_frame_bytes),
        }
    }

    pub fn read_frame(&mut self) -> io::Result<Vec<u8>> {
        loop {
            let mut chunk = [0_u8; 16 * 1024];
            let read = self.stream.read(&mut chunk)?;
            if read == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "IPC stream closed",
                ));
            }
            match self.decoder.feed(&chunk[..read]) {
                Ok(mut frames) if !frames.is_empty() => return Ok(frames.remove(0)),
                Ok(_) => {}
                Err(FrameError::TooLarge) => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "IPC frame exceeds limit",
                    ))
                }
                Err(FrameError::Malformed) => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "IPC frame malformed",
                    ))
                }
            }
        }
    }

    pub fn write_frame(&mut self, payload: &[u8], max_frame_bytes: usize) -> io::Result<()> {
        let frame = encode_frame(payload, max_frame_bytes)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        self.stream.write_all(&frame)
    }
}

impl Default for FramedUnixStream {
    fn default() -> Self {
        let (left, _) = UnixStream::pair().expect("UnixStream pair available on Unix");
        Self::new(left, DEFAULT_MAX_FRAME_BYTES)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;
    use tempfile::tempdir;

    #[test]
    fn socket_is_owner_only_and_round_trips_a_frame() {
        let directory = tempdir().unwrap();
        let socket_path = directory.path().join("files.sock");
        let server = LocalIpcServer::bind(&socket_path, DEFAULT_MAX_FRAME_BYTES).unwrap();
        let permissions = fs::metadata(&socket_path).unwrap().permissions().mode() & 0o777;
        assert_eq!(permissions, 0o600);
        let path = socket_path.clone();
        let worker = thread::spawn(move || {
            let mut connection = server.accept().unwrap();
            let payload = connection.read_frame().unwrap();
            connection
                .write_frame(&payload, DEFAULT_MAX_FRAME_BYTES)
                .unwrap();
        });
        let mut client = FramedUnixStream::connect(path, DEFAULT_MAX_FRAME_BYTES).unwrap();
        client
            .write_frame(b"health", DEFAULT_MAX_FRAME_BYTES)
            .unwrap();
        assert_eq!(client.read_frame().unwrap(), b"health");
        worker.join().unwrap();
    }
}
