use crate::terminal_ring::{TerminalEpoch, MAX_TERMINAL_FRAME_BYTES};
use thiserror::Error;

pub const TERMINAL_FRAME_HEADER_BYTES: usize = 48;
pub const TERMINAL_PROTOCOL_MAJOR: u8 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TerminalFramePacket {
    pub stream_id: u32,
    pub epoch: TerminalEpoch,
    pub seq: u64,
    pub byte_offset: u64,
    pub eof: bool,
    pub payload: Vec<u8>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum TerminalFrameError {
    #[error("terminal frame is shorter than its fixed header")]
    ShortHeader,
    #[error("terminal frame magic or protocol major is invalid")]
    HeaderIdentity,
    #[error("terminal frame flags or header length is invalid")]
    HeaderShape,
    #[error("terminal frame payload exceeds the 256 KiB bound")]
    PayloadTooLarge,
    #[error("terminal frame payload length does not match the buffer")]
    LengthMismatch,
}

pub fn encode(packet: &TerminalFramePacket) -> Result<Vec<u8>, TerminalFrameError> {
    if packet.payload.len() > MAX_TERMINAL_FRAME_BYTES {
        return Err(TerminalFrameError::PayloadTooLarge);
    }
    let payload_len =
        u32::try_from(packet.payload.len()).map_err(|_| TerminalFrameError::PayloadTooLarge)?;
    let mut output = Vec::with_capacity(TERMINAL_FRAME_HEADER_BYTES + packet.payload.len());
    output.extend_from_slice(b"OCT1");
    output.push(TERMINAL_PROTOCOL_MAJOR);
    output.push(u8::from(packet.eof));
    output.extend_from_slice(&(TERMINAL_FRAME_HEADER_BYTES as u16).to_be_bytes());
    output.extend_from_slice(&packet.stream_id.to_be_bytes());
    output.extend_from_slice(&packet.epoch.0);
    output.extend_from_slice(&packet.seq.to_be_bytes());
    output.extend_from_slice(&packet.byte_offset.to_be_bytes());
    output.extend_from_slice(&payload_len.to_be_bytes());
    output.extend_from_slice(&packet.payload);
    Ok(output)
}

pub fn decode(frame: &[u8]) -> Result<TerminalFramePacket, TerminalFrameError> {
    if frame.len() < TERMINAL_FRAME_HEADER_BYTES {
        return Err(TerminalFrameError::ShortHeader);
    }
    if &frame[0..4] != b"OCT1" || frame[4] != TERMINAL_PROTOCOL_MAJOR {
        return Err(TerminalFrameError::HeaderIdentity);
    }
    if frame[5] & !1 != 0
        || u16::from_be_bytes([frame[6], frame[7]]) as usize != TERMINAL_FRAME_HEADER_BYTES
    {
        return Err(TerminalFrameError::HeaderShape);
    }
    let stream_id = u32::from_be_bytes(frame[8..12].try_into().expect("fixed header"));
    let mut epoch = [0; 16];
    epoch.copy_from_slice(&frame[12..28]);
    let seq = u64::from_be_bytes(frame[28..36].try_into().expect("fixed header"));
    let byte_offset = u64::from_be_bytes(frame[36..44].try_into().expect("fixed header"));
    let payload_len = u32::from_be_bytes(frame[44..48].try_into().expect("fixed header")) as usize;
    if payload_len > MAX_TERMINAL_FRAME_BYTES {
        return Err(TerminalFrameError::PayloadTooLarge);
    }
    if frame.len() != TERMINAL_FRAME_HEADER_BYTES + payload_len {
        return Err(TerminalFrameError::LengthMismatch);
    }
    Ok(TerminalFramePacket {
        stream_id,
        epoch: TerminalEpoch(epoch),
        seq,
        byte_offset,
        eof: frame[5] & 1 != 0,
        payload: frame[48..].to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_preserves_raw_bytes_and_header_fields() {
        let packet = TerminalFramePacket {
            stream_id: 7,
            epoch: TerminalEpoch([0xab; 16]),
            seq: 9,
            byte_offset: 17,
            eof: true,
            payload: vec![0xff, 0x00, 0xc3],
        };
        let encoded = encode(&packet).expect("encode");
        assert_eq!(encoded.len(), TERMINAL_FRAME_HEADER_BYTES + 3);
        assert_eq!(decode(&encoded).expect("decode"), packet);
    }

    #[test]
    fn malformed_headers_and_lengths_fail_closed() {
        let packet = TerminalFramePacket {
            stream_id: 1,
            epoch: TerminalEpoch([1; 16]),
            seq: 1,
            byte_offset: 0,
            eof: false,
            payload: vec![1, 2],
        };
        let mut encoded = encode(&packet).expect("encode");
        encoded[0] = b'X';
        assert_eq!(decode(&encoded), Err(TerminalFrameError::HeaderIdentity));
        let mut encoded = encode(&packet).expect("encode");
        encoded[5] = 2;
        assert_eq!(decode(&encoded), Err(TerminalFrameError::HeaderShape));
        let mut encoded = encode(&packet).expect("encode");
        encoded[47] = 3;
        assert_eq!(decode(&encoded), Err(TerminalFrameError::LengthMismatch));
    }
}
