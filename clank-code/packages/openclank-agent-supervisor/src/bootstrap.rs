use crate::PROTOCOL_MAJOR;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::io::{Read, Write};
use thiserror::Error;

pub const MAX_BOOTSTRAP_FRAME: usize = 64 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BootstrapHello {
    pub protocol_major: String,
    pub instance_id: String,
    pub credential: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BootstrapAck {
    pub protocol_major: String,
    pub instance_id: String,
    pub pid: u32,
    pub process_start_token: String,
    pub transport_kind: String,
    pub endpoint_identity_sha256: String,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum BootstrapError {
    #[error("bootstrap frame is empty or truncated")]
    Truncated,
    #[error("bootstrap frame exceeds the bounded maximum")]
    Oversized,
    #[error("bootstrap frame has trailing bytes")]
    TrailingBytes,
    #[error("bootstrap frame is not valid JSON")]
    InvalidJson,
    #[error("bootstrap protocol major is incompatible")]
    IncompatibleMajor,
    #[error("bootstrap instance identity is invalid")]
    WrongInstance,
    #[error("bootstrap credential is invalid")]
    WrongCredential,
    #[error("bootstrap hello was replayed")]
    Replayed,
    #[error("bootstrap channel I/O failed")]
    Io,
}

pub fn encode_frame<T: Serialize>(value: &T) -> Result<Vec<u8>, BootstrapError> {
    let payload = serde_json::to_vec(value).map_err(|_| BootstrapError::InvalidJson)?;
    if payload.is_empty() {
        return Err(BootstrapError::Truncated);
    }
    if payload.len() > MAX_BOOTSTRAP_FRAME {
        return Err(BootstrapError::Oversized);
    }
    let mut frame = Vec::with_capacity(payload.len() + 4);
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(&payload);
    Ok(frame)
}

pub fn decode_frame<T: DeserializeOwned>(frame: &[u8]) -> Result<T, BootstrapError> {
    if frame.len() < 4 {
        return Err(BootstrapError::Truncated);
    }
    let length = u32::from_be_bytes([frame[0], frame[1], frame[2], frame[3]]) as usize;
    if length == 0 {
        return Err(BootstrapError::Truncated);
    }
    if length > MAX_BOOTSTRAP_FRAME {
        return Err(BootstrapError::Oversized);
    }
    if frame.len() < length + 4 {
        return Err(BootstrapError::Truncated);
    }
    if frame.len() != length + 4 {
        return Err(BootstrapError::TrailingBytes);
    }
    serde_json::from_slice(&frame[4..]).map_err(|_| BootstrapError::InvalidJson)
}

pub fn write_frame<W: Write, T: Serialize>(
    writer: &mut W,
    value: &T,
) -> Result<(), BootstrapError> {
    let frame = encode_frame(value)?;
    writer.write_all(&frame).map_err(|_| BootstrapError::Io)
}

pub fn read_frame<R: Read>(reader: &mut R) -> Result<Vec<u8>, BootstrapError> {
    let mut length_bytes = [0; 4];
    reader
        .read_exact(&mut length_bytes)
        .map_err(|_| BootstrapError::Truncated)?;
    let length = u32::from_be_bytes(length_bytes) as usize;
    if length == 0 {
        return Err(BootstrapError::Truncated);
    }
    if length > MAX_BOOTSTRAP_FRAME {
        return Err(BootstrapError::Oversized);
    }
    let mut frame = Vec::with_capacity(length + 4);
    frame.extend_from_slice(&length_bytes);
    frame.resize(length + 4, 0);
    reader
        .read_exact(&mut frame[4..])
        .map_err(|_| BootstrapError::Truncated)?;
    Ok(frame)
}

#[derive(Debug, Default)]
pub struct BootstrapAcceptor {
    accepted: bool,
}

impl BootstrapAcceptor {
    pub fn accept(
        &mut self,
        frame: &[u8],
        expected_instance_id: &str,
        expected_credential: &str,
    ) -> Result<BootstrapHello, BootstrapError> {
        if self.accepted {
            return Err(BootstrapError::Replayed);
        }
        let hello: BootstrapHello = decode_frame(frame)?;
        if hello.protocol_major != PROTOCOL_MAJOR {
            return Err(BootstrapError::IncompatibleMajor);
        }
        if hello.instance_id != expected_instance_id {
            return Err(BootstrapError::WrongInstance);
        }
        if hello.credential != expected_credential {
            return Err(BootstrapError::WrongCredential);
        }
        self.accepted = true;
        Ok(hello)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hello() -> BootstrapHello {
        BootstrapHello {
            protocol_major: PROTOCOL_MAJOR.to_owned(),
            instance_id: "instance-1".to_owned(),
            credential: "credential-in-memory-only".to_owned(),
        }
    }

    #[test]
    fn bounded_round_trip_and_replay_fence() {
        let frame = encode_frame(&hello()).expect("encode");
        let mut acceptor = BootstrapAcceptor::default();
        assert_eq!(
            acceptor.accept(&frame, "instance-1", "credential-in-memory-only"),
            Ok(hello())
        );
        assert_eq!(
            acceptor.accept(&frame, "instance-1", "credential-in-memory-only"),
            Err(BootstrapError::Replayed)
        );
    }

    #[test]
    fn wrong_binding_and_major_fail_closed() {
        let mut acceptor = BootstrapAcceptor::default();
        let mut bad = hello();
        bad.instance_id = "other".to_owned();
        assert_eq!(
            acceptor.accept(
                &encode_frame(&bad).unwrap(),
                "instance-1",
                "credential-in-memory-only"
            ),
            Err(BootstrapError::WrongInstance)
        );
        bad = hello();
        bad.protocol_major = "99".to_owned();
        assert_eq!(
            acceptor.accept(
                &encode_frame(&bad).unwrap(),
                "instance-1",
                "credential-in-memory-only"
            ),
            Err(BootstrapError::IncompatibleMajor)
        );
    }

    #[test]
    fn oversized_and_trailing_frames_are_rejected() {
        let mut frame = vec![0xff, 0xff, 0xff, 0xff];
        frame.extend_from_slice(&[0; 4]);
        assert_eq!(
            decode_frame::<BootstrapHello>(&frame),
            Err(BootstrapError::Oversized)
        );
        let mut valid = encode_frame(&hello()).unwrap();
        valid.push(0);
        assert_eq!(
            decode_frame::<BootstrapHello>(&valid),
            Err(BootstrapError::TrailingBytes)
        );
    }

    #[test]
    fn channel_helpers_are_bounded_and_length_delimited() {
        let mut channel = Vec::new();
        write_frame(&mut channel, &hello()).expect("write");
        let frame = read_frame(&mut channel.as_slice()).expect("read");
        assert_eq!(
            decode_frame::<BootstrapHello>(&frame).expect("decode"),
            hello()
        );
        assert_eq!(
            read_frame(&mut &[0, 0, 0][..]),
            Err(BootstrapError::Truncated)
        );
    }
}
