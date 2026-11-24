//! Private, path-bearing protocol between `odysseus-files` and its macOS
//! Quick Look sidecar. This module is deliberately not exported by the library.

use std::fmt;
use std::io::{self, Read, Write};

pub const SESSION_TOKEN_BYTES: usize = 32;
pub const MAX_PATH_BYTES: usize = 16 * 1024;
pub const MAX_COMMAND_FRAME_BYTES: usize = MAX_PATH_BYTES + 128;
pub const MAX_HELPER_OUTPUT_BYTES: usize = 4 * 1024 * 1024;
pub const RESPONSE_OVERHEAD_BYTES: usize = 32;

const MAGIC: &[u8; 4] = b"OQL1";
const COMMAND_RENDER: u8 = 1;
const COMMAND_CANCEL: u8 = 2;
const COMMAND_SHUTDOWN: u8 = 3;
const RESPONSE_READY: u8 = 16;
const RESPONSE_PNG: u8 = 17;
const RESPONSE_ERROR: u8 = 18;

pub struct RenderCommand {
    pub session_token: [u8; SESSION_TOKEN_BYTES],
    pub job_id: u64,
    pub width: u32,
    pub height: u32,
    pub scale: f64,
    pub max_output_bytes: u32,
    pub path: Vec<u8>,
}

impl fmt::Debug for RenderCommand {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RenderCommand")
            .field("job_id", &self.job_id)
            .field("width", &self.width)
            .field("height", &self.height)
            .field("scale", &self.scale)
            .field("max_output_bytes", &self.max_output_bytes)
            .field("path", &"<redacted>")
            .finish()
    }
}

pub enum Command {
    Render(RenderCommand),
    Cancel {
        session_token: [u8; SESSION_TOKEN_BYTES],
        job_id: u64,
    },
    Shutdown {
        session_token: [u8; SESSION_TOKEN_BYTES],
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum HelperErrorCode {
    Malformed = 1,
    UnauthorizedChannel = 2,
    InvalidRequest = 3,
    NotRegularFile = 4,
    NativeUnavailable = 5,
    IconRepresentationRejected = 6,
    EncodeFailed = 7,
    OutputTooLarge = 8,
    Cancelled = 9,
    Busy = 10,
}

impl HelperErrorCode {
    fn from_byte(value: u8) -> Result<Self, WireError> {
        match value {
            1 => Ok(Self::Malformed),
            2 => Ok(Self::UnauthorizedChannel),
            3 => Ok(Self::InvalidRequest),
            4 => Ok(Self::NotRegularFile),
            5 => Ok(Self::NativeUnavailable),
            6 => Ok(Self::IconRepresentationRejected),
            7 => Ok(Self::EncodeFailed),
            8 => Ok(Self::OutputTooLarge),
            9 => Ok(Self::Cancelled),
            10 => Ok(Self::Busy),
            _ => Err(WireError::Malformed),
        }
    }
}

pub enum Response {
    Ready,
    Png { job_id: u64, bytes: Vec<u8> },
    Error { job_id: u64, code: HelperErrorCode },
}

#[derive(Debug)]
pub enum WireError {
    Io(io::Error),
    Malformed,
    TooLarge,
}

impl fmt::Display for WireError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "private helper transport failed: {error}"),
            Self::Malformed => formatter.write_str("private helper frame is malformed"),
            Self::TooLarge => formatter.write_str("private helper frame exceeds its limit"),
        }
    }
}

impl std::error::Error for WireError {}

impl From<io::Error> for WireError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

pub fn encode_command(command: &Command) -> Result<Vec<u8>, WireError> {
    let mut bytes = Vec::new();
    bytes.extend_from_slice(MAGIC);
    match command {
        Command::Render(command) => {
            if command.path.is_empty()
                || command.path.len() > MAX_PATH_BYTES
                || command.max_output_bytes as usize > MAX_HELPER_OUTPUT_BYTES
            {
                return Err(WireError::TooLarge);
            }
            bytes.push(COMMAND_RENDER);
            bytes.extend_from_slice(&command.session_token);
            bytes.extend_from_slice(&command.job_id.to_be_bytes());
            bytes.extend_from_slice(&command.width.to_be_bytes());
            bytes.extend_from_slice(&command.height.to_be_bytes());
            bytes.extend_from_slice(&command.scale.to_bits().to_be_bytes());
            bytes.extend_from_slice(&command.max_output_bytes.to_be_bytes());
            bytes.extend_from_slice(&(command.path.len() as u32).to_be_bytes());
            bytes.extend_from_slice(&command.path);
        }
        Command::Cancel {
            session_token,
            job_id,
        } => {
            bytes.push(COMMAND_CANCEL);
            bytes.extend_from_slice(session_token);
            bytes.extend_from_slice(&job_id.to_be_bytes());
        }
        Command::Shutdown { session_token } => {
            bytes.push(COMMAND_SHUTDOWN);
            bytes.extend_from_slice(session_token);
        }
    }
    Ok(bytes)
}

pub fn decode_command(bytes: &[u8]) -> Result<Command, WireError> {
    if bytes.len() < MAGIC.len() + 1 || &bytes[..MAGIC.len()] != MAGIC {
        return Err(WireError::Malformed);
    }
    let mut cursor = MAGIC.len() + 1;
    match bytes[MAGIC.len()] {
        COMMAND_RENDER => {
            let token = take_array::<SESSION_TOKEN_BYTES>(bytes, &mut cursor)?;
            let job_id = u64::from_be_bytes(take_array(bytes, &mut cursor)?);
            let width = u32::from_be_bytes(take_array(bytes, &mut cursor)?);
            let height = u32::from_be_bytes(take_array(bytes, &mut cursor)?);
            let scale = f64::from_bits(u64::from_be_bytes(take_array(bytes, &mut cursor)?));
            let max_output_bytes = u32::from_be_bytes(take_array(bytes, &mut cursor)?);
            let path_len = u32::from_be_bytes(take_array(bytes, &mut cursor)?) as usize;
            if path_len == 0
                || path_len > MAX_PATH_BYTES
                || max_output_bytes as usize > MAX_HELPER_OUTPUT_BYTES
                || bytes.len().checked_sub(cursor) != Some(path_len)
            {
                return Err(WireError::Malformed);
            }
            Ok(Command::Render(RenderCommand {
                session_token: token,
                job_id,
                width,
                height,
                scale,
                max_output_bytes,
                path: bytes[cursor..].to_vec(),
            }))
        }
        COMMAND_CANCEL => {
            let session_token = take_array::<SESSION_TOKEN_BYTES>(bytes, &mut cursor)?;
            let job_id = u64::from_be_bytes(take_array(bytes, &mut cursor)?);
            if cursor != bytes.len() {
                return Err(WireError::Malformed);
            }
            Ok(Command::Cancel {
                session_token,
                job_id,
            })
        }
        COMMAND_SHUTDOWN => {
            let session_token = take_array::<SESSION_TOKEN_BYTES>(bytes, &mut cursor)?;
            if cursor != bytes.len() {
                return Err(WireError::Malformed);
            }
            Ok(Command::Shutdown { session_token })
        }
        _ => Err(WireError::Malformed),
    }
}

pub fn encode_response(response: &Response) -> Result<Vec<u8>, WireError> {
    let mut bytes = Vec::new();
    bytes.extend_from_slice(MAGIC);
    match response {
        Response::Ready => bytes.push(RESPONSE_READY),
        Response::Png { job_id, bytes: png } => {
            if png.len() > MAX_HELPER_OUTPUT_BYTES {
                return Err(WireError::TooLarge);
            }
            bytes.push(RESPONSE_PNG);
            bytes.extend_from_slice(&job_id.to_be_bytes());
            bytes.extend_from_slice(&(png.len() as u32).to_be_bytes());
            bytes.extend_from_slice(png);
        }
        Response::Error { job_id, code } => {
            bytes.push(RESPONSE_ERROR);
            bytes.extend_from_slice(&job_id.to_be_bytes());
            bytes.push(*code as u8);
        }
    }
    Ok(bytes)
}

pub fn decode_response(bytes: &[u8], max_output_bytes: usize) -> Result<Response, WireError> {
    if bytes.len() < MAGIC.len() + 1 || &bytes[..MAGIC.len()] != MAGIC {
        return Err(WireError::Malformed);
    }
    let mut cursor = MAGIC.len() + 1;
    match bytes[MAGIC.len()] {
        RESPONSE_READY if cursor == bytes.len() => Ok(Response::Ready),
        RESPONSE_PNG => {
            let job_id = u64::from_be_bytes(take_array(bytes, &mut cursor)?);
            let png_len = u32::from_be_bytes(take_array(bytes, &mut cursor)?) as usize;
            if png_len > max_output_bytes
                || png_len > MAX_HELPER_OUTPUT_BYTES
                || bytes.len().checked_sub(cursor) != Some(png_len)
            {
                return Err(WireError::TooLarge);
            }
            Ok(Response::Png {
                job_id,
                bytes: bytes[cursor..].to_vec(),
            })
        }
        RESPONSE_ERROR => {
            let job_id = u64::from_be_bytes(take_array(bytes, &mut cursor)?);
            let code = *bytes.get(cursor).ok_or(WireError::Malformed)?;
            cursor += 1;
            if cursor != bytes.len() {
                return Err(WireError::Malformed);
            }
            Ok(Response::Error {
                job_id,
                code: HelperErrorCode::from_byte(code)?,
            })
        }
        _ => Err(WireError::Malformed),
    }
}

pub fn read_frame(reader: &mut impl Read, max_frame_bytes: usize) -> Result<Vec<u8>, WireError> {
    let mut length = [0_u8; 4];
    reader.read_exact(&mut length)?;
    let length = u32::from_be_bytes(length) as usize;
    if length > max_frame_bytes {
        return Err(WireError::TooLarge);
    }
    let mut payload = vec![0_u8; length];
    reader.read_exact(&mut payload)?;
    Ok(payload)
}

pub fn write_frame(
    writer: &mut impl Write,
    payload: &[u8],
    max_frame_bytes: usize,
) -> Result<(), WireError> {
    if payload.len() > max_frame_bytes || payload.len() > u32::MAX as usize {
        return Err(WireError::TooLarge);
    }
    writer.write_all(&(payload.len() as u32).to_be_bytes())?;
    writer.write_all(payload)?;
    writer.flush()?;
    Ok(())
}

pub fn token_matches(
    expected: &[u8; SESSION_TOKEN_BYTES],
    actual: &[u8; SESSION_TOKEN_BYTES],
) -> bool {
    expected
        .iter()
        .zip(actual)
        .fold(0_u8, |difference, (left, right)| {
            difference | (left ^ right)
        })
        == 0
}

fn take_array<const N: usize>(bytes: &[u8], cursor: &mut usize) -> Result<[u8; N], WireError> {
    let end = cursor.checked_add(N).ok_or(WireError::Malformed)?;
    let value = bytes.get(*cursor..end).ok_or(WireError::Malformed)?;
    *cursor = end;
    value.try_into().map_err(|_| WireError::Malformed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_debug_and_errors_never_expose_the_path() {
        let command = RenderCommand {
            session_token: [7; SESSION_TOKEN_BYTES],
            job_id: 9,
            width: 128,
            height: 128,
            scale: 2.0,
            max_output_bytes: 1024,
            path: b"/private/secret-name.txt".to_vec(),
        };
        let debug = format!("{command:?}");
        assert!(!debug.contains("secret-name"));
        assert!(debug.contains("<redacted>"));
    }

    #[test]
    fn private_protocol_round_trips_and_caps_png_output() {
        let command = Command::Render(RenderCommand {
            session_token: [3; SESSION_TOKEN_BYTES],
            job_id: 42,
            width: 96,
            height: 64,
            scale: 2.0,
            max_output_bytes: 2048,
            path: b"/tmp/example.txt".to_vec(),
        });
        let decoded = decode_command(&encode_command(&command).unwrap()).unwrap();
        let Command::Render(decoded) = decoded else {
            panic!("expected render command")
        };
        assert_eq!(decoded.job_id, 42);
        assert_eq!(decoded.path, b"/tmp/example.txt");

        let response = encode_response(&Response::Png {
            job_id: 42,
            bytes: vec![1; 64],
        })
        .unwrap();
        assert!(matches!(
            decode_response(&response, 64),
            Ok(Response::Png { job_id: 42, .. })
        ));
        assert!(matches!(
            decode_response(&response, 63),
            Err(WireError::TooLarge)
        ));
    }

    #[test]
    fn framed_transport_rejects_declared_oversize_before_allocating_payload() {
        let mut frame = ((MAX_COMMAND_FRAME_BYTES + 1) as u32)
            .to_be_bytes()
            .to_vec();
        frame.extend_from_slice(b"ignored");
        assert!(matches!(
            read_frame(&mut frame.as_slice(), MAX_COMMAND_FRAME_BYTES),
            Err(WireError::TooLarge)
        ));
    }
}
