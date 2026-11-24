use std::collections::VecDeque;
use thiserror::Error;

pub const MAX_TERMINAL_FRAME_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TerminalEpoch(pub [u8; 16]);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TerminalFrame {
    pub seq: u64,
    pub byte_offset: u64,
    pub bytes: Vec<u8>,
    pub eof: bool,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum TerminalRingError {
    #[error("terminal frame exceeds the 256 KiB protocol bound")]
    FrameTooLarge,
    #[error("terminal replay requested for a different epoch")]
    WrongEpoch,
    #[error("terminal replay cursor is ahead of the stream")]
    CursorAhead,
    #[error("terminal replay cursor fell outside the retained ring")]
    ReplayGap { oldest_seq: u64 },
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum SubscriberError {
    #[error(transparent)]
    Ring(#[from] TerminalRingError),
    #[error("terminal subscriber in-flight window is exhausted")]
    InFlightExceeded,
    #[error("terminal subscriber ACK is ahead of delivered output")]
    AckAhead,
    #[error("terminal subscriber ACK moved backwards")]
    AckRegressed,
}

#[derive(Debug, Clone)]
pub struct SubscriberCursor {
    ack_seq: u64,
    max_in_flight_bytes: usize,
    in_flight_bytes: usize,
    delivered: VecDeque<(u64, usize)>,
}

impl SubscriberCursor {
    pub fn new(max_in_flight_bytes: usize) -> Self {
        Self {
            ack_seq: 0,
            max_in_flight_bytes: max_in_flight_bytes.max(1),
            in_flight_bytes: 0,
            delivered: VecDeque::new(),
        }
    }

    pub fn ack_seq(&self) -> u64 {
        self.ack_seq
    }

    pub fn in_flight_bytes(&self) -> usize {
        self.in_flight_bytes
    }

    pub fn poll(&mut self, ring: &TerminalRing) -> Result<Vec<TerminalFrame>, SubscriberError> {
        let frames = ring.replay(ring.epoch(), self.ack_seq)?;
        let bytes = frames.iter().map(|frame| frame.bytes.len()).sum::<usize>();
        if self.in_flight_bytes.saturating_add(bytes) > self.max_in_flight_bytes {
            return Err(SubscriberError::InFlightExceeded);
        }
        for frame in &frames {
            self.delivered.push_back((frame.seq, frame.bytes.len()));
        }
        self.in_flight_bytes = self.in_flight_bytes.saturating_add(bytes);
        Ok(frames)
    }

    pub fn ack(&mut self, seq: u64) -> Result<(), SubscriberError> {
        if seq < self.ack_seq {
            return Err(SubscriberError::AckRegressed);
        }
        let highest_delivered = self
            .delivered
            .back()
            .map(|entry| entry.0)
            .unwrap_or(self.ack_seq);
        if seq > highest_delivered {
            return Err(SubscriberError::AckAhead);
        }
        while let Some((delivered_seq, bytes)) = self.delivered.front().copied() {
            if delivered_seq > seq {
                break;
            }
            self.delivered.pop_front();
            self.in_flight_bytes = self.in_flight_bytes.saturating_sub(bytes);
        }
        self.ack_seq = seq;
        Ok(())
    }
}

/// A byte- and frame-bounded replay ring for one terminal epoch.
///
/// The ring stores raw PTY bytes without decoding them. `since_seq` is the
/// highest contiguous sequence already parsed by a subscriber; replay returns
/// only the exact suffix after that cursor or an explicit gap.
#[derive(Debug, Clone)]
pub struct TerminalRing {
    epoch: TerminalEpoch,
    max_bytes: usize,
    max_frames: usize,
    bytes: usize,
    next_seq: u64,
    next_offset: u64,
    frames: VecDeque<TerminalFrame>,
}

impl TerminalRing {
    pub fn new(epoch: TerminalEpoch, max_bytes: usize, max_frames: usize) -> Self {
        Self {
            epoch,
            max_bytes: max_bytes.max(1),
            max_frames: max_frames.max(1),
            bytes: 0,
            next_seq: 1,
            next_offset: 0,
            frames: VecDeque::new(),
        }
    }

    pub fn epoch(&self) -> TerminalEpoch {
        self.epoch
    }

    pub fn tail_seq(&self) -> u64 {
        self.next_seq.saturating_sub(1)
    }

    pub fn next_offset(&self) -> u64 {
        self.next_offset
    }

    pub fn retained_bytes(&self) -> usize {
        self.bytes
    }

    pub fn retained_frames(&self) -> usize {
        self.frames.len()
    }

    pub fn push(&mut self, bytes: Vec<u8>, eof: bool) -> Result<TerminalFrame, TerminalRingError> {
        if bytes.len() > MAX_TERMINAL_FRAME_BYTES || bytes.len() > self.max_bytes {
            return Err(TerminalRingError::FrameTooLarge);
        }
        let frame = TerminalFrame {
            seq: self.next_seq,
            byte_offset: self.next_offset,
            bytes,
            eof,
        };
        self.next_seq = self.next_seq.saturating_add(1);
        self.next_offset = self.next_offset.saturating_add(frame.bytes.len() as u64);
        self.bytes = self.bytes.saturating_add(frame.bytes.len());
        self.frames.push_back(frame.clone());
        while self.frames.len() > self.max_frames || self.bytes > self.max_bytes {
            if let Some(evicted) = self.frames.pop_front() {
                self.bytes = self.bytes.saturating_sub(evicted.bytes.len());
            }
        }
        Ok(frame)
    }

    pub fn replay(
        &self,
        epoch: TerminalEpoch,
        since_seq: u64,
    ) -> Result<Vec<TerminalFrame>, TerminalRingError> {
        if epoch != self.epoch {
            return Err(TerminalRingError::WrongEpoch);
        }
        let tail = self.tail_seq();
        if since_seq > tail {
            return Err(TerminalRingError::CursorAhead);
        }
        let wanted = since_seq.saturating_add(1);
        if wanted > tail {
            return Ok(Vec::new());
        }
        let oldest = self.frames.front().map(|frame| frame.seq).unwrap_or(wanted);
        if wanted < oldest {
            return Err(TerminalRingError::ReplayGap { oldest_seq: oldest });
        }
        Ok(self
            .frames
            .iter()
            .filter(|frame| frame.seq >= wanted)
            .cloned()
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn epoch(byte: u8) -> TerminalEpoch {
        TerminalEpoch([byte; 16])
    }

    #[test]
    fn sequence_offsets_and_exact_replay_are_raw_byte_safe() {
        let mut ring = TerminalRing::new(epoch(1), 32, 8);
        let first = ring.push(vec![0xff, 0x00], false).expect("first");
        let second = ring.push("界".as_bytes().to_vec(), false).expect("second");
        assert_eq!(first.seq, 1);
        assert_eq!(first.byte_offset, 0);
        assert_eq!(second.seq, 2);
        assert_eq!(second.byte_offset, 2);
        assert_eq!(ring.next_offset(), 5);
        assert_eq!(
            ring.replay(epoch(1), 0).expect("replay"),
            vec![first, second]
        );
        assert!(ring.replay(epoch(1), 2).expect("tail").is_empty());
    }

    #[test]
    fn eviction_is_an_explicit_gap_and_never_a_silent_suffix() {
        let mut ring = TerminalRing::new(epoch(2), 4, 2);
        ring.push(vec![1, 2], false).expect("one");
        ring.push(vec![3, 4], false).expect("two");
        ring.push(vec![5, 6], false).expect("three");
        assert_eq!(ring.retained_frames(), 2);
        assert_eq!(ring.retained_bytes(), 4);
        assert_eq!(
            ring.replay(epoch(2), 0),
            Err(TerminalRingError::ReplayGap { oldest_seq: 2 })
        );
        assert_eq!(ring.replay(epoch(2), 1).expect("suffix").len(), 2);
    }

    #[test]
    fn epoch_and_cursor_mismatches_fail_closed() {
        let mut ring = TerminalRing::new(epoch(3), 8, 4);
        ring.push(vec![1], false).expect("frame");
        assert_eq!(ring.replay(epoch(4), 0), Err(TerminalRingError::WrongEpoch));
        assert_eq!(
            ring.replay(epoch(3), 99),
            Err(TerminalRingError::CursorAhead)
        );
    }

    #[test]
    fn frame_size_is_bounded_and_eof_is_preserved() {
        let mut ring = TerminalRing::new(epoch(5), 4, 4);
        assert_eq!(
            ring.push(vec![0; MAX_TERMINAL_FRAME_BYTES + 1], false),
            Err(TerminalRingError::FrameTooLarge)
        );
        let eof = ring.push(Vec::new(), true).expect("eof");
        assert!(eof.eof);
        assert!(ring.replay(epoch(5), 0).expect("eof replay")[0].eof);
    }

    #[test]
    fn subscriber_window_requires_parse_ack_before_more_output() {
        let mut ring = TerminalRing::new(epoch(9), 64, 8);
        ring.push(vec![1, 2, 3], false).expect("frame");
        ring.push(vec![4, 5, 6], false).expect("frame");
        let mut subscriber = SubscriberCursor::new(6);
        assert_eq!(subscriber.poll(&ring).expect("first poll").len(), 2);
        assert_eq!(subscriber.in_flight_bytes(), 6);
        ring.push(vec![7, 8, 9], false).expect("frame");
        assert_eq!(
            subscriber.poll(&ring),
            Err(SubscriberError::InFlightExceeded)
        );
        subscriber.ack(1).expect("ack first");
        assert_eq!(subscriber.in_flight_bytes(), 3);
        assert_eq!(
            subscriber.poll(&ring),
            Err(SubscriberError::InFlightExceeded)
        );
        subscriber.ack(2).expect("ack second");
        assert_eq!(subscriber.poll(&ring).expect("third poll").len(), 1);
        assert_eq!(subscriber.ack(0), Err(SubscriberError::AckRegressed));
        assert_eq!(subscriber.ack(9), Err(SubscriberError::AckAhead));
    }
}
