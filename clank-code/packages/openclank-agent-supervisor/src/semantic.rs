use sha2::Digest;
use std::collections::{BTreeMap, VecDeque};
use thiserror::Error;

pub const MAX_SEMANTIC_PAYLOAD_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SemanticEpoch(pub [u8; 16]);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SemanticEvent {
    pub owner_subject_id: String,
    pub session_id: String,
    pub runtime_id: String,
    pub runtime_epoch: String,
    pub runtime_generation: u64,
    pub run_id: String,
    pub turn_id: String,
    pub semantic_epoch: String,
    pub seq: u64,
    pub emitted_unix_ms: i64,
    pub op: String,
    pub entity_kind: String,
    pub entity_id: String,
    pub payload: serde_json::Value,
    pub payload_sha256: String,
    pub append_offset_utf8_bytes: Option<u64>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum SemanticJournalError {
    #[error("semantic payload must be a JSON object")]
    PayloadNotObject,
    #[error("semantic payload exceeds the 256 KiB protocol bound")]
    PayloadTooLarge,
    #[error("semantic replay requested for a different epoch")]
    WrongEpoch,
    #[error("semantic replay cursor is ahead of the journal")]
    CursorAhead,
    #[error("semantic replay cursor fell outside the retained journal")]
    ReplayGap { oldest_seq: u64 },
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum SemanticApplyError {
    #[error("semantic sequence gap: expected {expected}, got {got}")]
    SequenceGap { expected: u64, got: u64 },
    #[error("semantic event was already applied")]
    Duplicate,
    #[error("text append has no cumulative UTF-8 byte offset")]
    MissingAppendOffset,
    #[error("text append offset mismatch: expected {expected}, got {got}")]
    AppendOffsetMismatch { expected: u64, got: u64 },
    #[error("text append payload has no string text")]
    InvalidAppendPayload,
}

#[derive(Debug, Clone, Default)]
pub struct SemanticState {
    last_seq: u64,
    entities: BTreeMap<(String, String), serde_json::Value>,
    append_offsets: BTreeMap<(String, String), u64>,
}

impl SemanticState {
    pub fn last_seq(&self) -> u64 {
        self.last_seq
    }

    pub fn entity(&self, kind: &str, id: &str) -> Option<&serde_json::Value> {
        self.entities.get(&(kind.to_owned(), id.to_owned()))
    }

    pub fn apply(&mut self, event: &SemanticEvent) -> Result<(), SemanticApplyError> {
        if event.seq <= self.last_seq {
            return Err(SemanticApplyError::Duplicate);
        }
        let expected = self.last_seq.saturating_add(1);
        if event.seq != expected {
            return Err(SemanticApplyError::SequenceGap {
                expected,
                got: event.seq,
            });
        }
        let key = (event.entity_kind.clone(), event.entity_id.clone());
        if event.op == "text_append" {
            let Some(offset) = event.append_offset_utf8_bytes else {
                return Err(SemanticApplyError::MissingAppendOffset);
            };
            let expected_offset = self.append_offsets.get(&key).copied().unwrap_or(0);
            if offset != expected_offset {
                return Err(SemanticApplyError::AppendOffsetMismatch {
                    expected: expected_offset,
                    got: offset,
                });
            }
            let Some(text) = event
                .payload
                .get("text")
                .and_then(serde_json::Value::as_str)
            else {
                return Err(SemanticApplyError::InvalidAppendPayload);
            };
            let current = self
                .entities
                .get(&key)
                .and_then(|value| value.get("text"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");
            let combined = format!("{current}{text}");
            self.entities
                .insert(key.clone(), serde_json::json!({"text": combined}));
            self.append_offsets
                .insert(key, offset.saturating_add(text.len() as u64));
        } else if event.op.ends_with("_remove") {
            self.entities.remove(&key);
            self.append_offsets.remove(&key);
        } else if event.op.ends_with("_merge") {
            let mut merged = self
                .entities
                .get(&key)
                .and_then(serde_json::Value::as_object)
                .cloned()
                .unwrap_or_default();
            if let Some(fields) = event.payload.as_object() {
                merged.extend(fields.clone());
            }
            self.entities.insert(key, serde_json::Value::Object(merged));
        } else if event.op.ends_with("_upsert") || event.op == "snapshot_reset" {
            self.entities.insert(key, event.payload.clone());
        }
        self.last_seq = event.seq;
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct SemanticJournal {
    epoch: SemanticEpoch,
    max_bytes: usize,
    max_events: usize,
    bytes: usize,
    next_seq: u64,
    events: VecDeque<SemanticEvent>,
}

impl SemanticJournal {
    pub fn new(epoch: SemanticEpoch, max_bytes: usize, max_events: usize) -> Self {
        Self {
            epoch,
            max_bytes: max_bytes.max(1),
            max_events: max_events.max(1),
            bytes: 0,
            next_seq: 1,
            events: VecDeque::new(),
        }
    }

    pub fn epoch(&self) -> SemanticEpoch {
        self.epoch.clone()
    }

    pub fn tail_seq(&self) -> u64 {
        self.next_seq.saturating_sub(1)
    }

    pub fn retained_events(&self) -> usize {
        self.events.len()
    }

    pub fn append(
        &mut self,
        mut event: SemanticEvent,
    ) -> Result<SemanticEvent, SemanticJournalError> {
        if !event.payload.is_object() {
            return Err(SemanticJournalError::PayloadNotObject);
        }
        let payload = serde_json::to_vec(&event.payload).expect("JSON values serialize");
        if payload.len() > MAX_SEMANTIC_PAYLOAD_BYTES || payload.len() > self.max_bytes {
            return Err(SemanticJournalError::PayloadTooLarge);
        }
        event.seq = self.next_seq;
        event.payload_sha256 = format!("{:x}", sha2::Sha256::digest(&payload));
        self.next_seq = self.next_seq.saturating_add(1);
        self.bytes = self.bytes.saturating_add(payload.len());
        self.events.push_back(event.clone());
        while self.events.len() > self.max_events || self.bytes > self.max_bytes {
            if let Some(evicted) = self.events.pop_front() {
                let bytes = serde_json::to_vec(&evicted.payload)
                    .map(|value| value.len())
                    .unwrap_or(0);
                self.bytes = self.bytes.saturating_sub(bytes);
            }
        }
        Ok(event)
    }

    pub fn replay(
        &self,
        epoch: &SemanticEpoch,
        since_seq: u64,
    ) -> Result<Vec<SemanticEvent>, SemanticJournalError> {
        if epoch != &self.epoch {
            return Err(SemanticJournalError::WrongEpoch);
        }
        let tail = self.tail_seq();
        if since_seq > tail {
            return Err(SemanticJournalError::CursorAhead);
        }
        let wanted = since_seq.saturating_add(1);
        if wanted > tail {
            return Ok(Vec::new());
        }
        let oldest = self.events.front().map(|event| event.seq).unwrap_or(wanted);
        if wanted < oldest {
            return Err(SemanticJournalError::ReplayGap { oldest_seq: oldest });
        }
        Ok(self
            .events
            .iter()
            .filter(|event| event.seq >= wanted)
            .cloned()
            .collect())
    }
}

impl Default for SemanticEvent {
    fn default() -> Self {
        Self {
            owner_subject_id: String::new(),
            session_id: String::new(),
            runtime_id: String::new(),
            runtime_epoch: String::new(),
            runtime_generation: 0,
            run_id: String::new(),
            turn_id: String::new(),
            semantic_epoch: String::new(),
            seq: 0,
            emitted_unix_ms: 0,
            op: String::new(),
            entity_kind: String::new(),
            entity_id: String::new(),
            payload: serde_json::Value::Object(Default::default()),
            payload_sha256: String::new(),
            append_offset_utf8_bytes: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(value: serde_json::Value) -> SemanticEvent {
        SemanticEvent {
            owner_subject_id: "subject-1".into(),
            session_id: "session-1".into(),
            runtime_id: "runtime-1".into(),
            runtime_epoch: "0123456789abcdef0123456789abcdef".into(),
            runtime_generation: 4,
            run_id: "run-1".into(),
            turn_id: "turn-1".into(),
            semantic_epoch: "fedcba9876543210fedcba9876543210".into(),
            seq: 0,
            emitted_unix_ms: 1,
            op: "message_upsert".into(),
            entity_kind: "message".into(),
            entity_id: "message-1".into(),
            payload: value,
            payload_sha256: String::new(),
            append_offset_utf8_bytes: None,
        }
    }

    #[test]
    fn append_assigns_sequence_and_payload_digest() {
        let mut journal = SemanticJournal::new(SemanticEpoch([1; 16]), 1024, 8);
        let stored = journal
            .append(event(serde_json::json!({"text":"界"})))
            .expect("append");
        assert_eq!(stored.seq, 1);
        assert_eq!(stored.payload_sha256.len(), 64);
        assert_eq!(
            journal.replay(&SemanticEpoch([1; 16]), 0).expect("replay"),
            vec![stored]
        );
    }

    #[test]
    fn bounded_replay_reports_gap_instead_of_silent_loss() {
        let mut journal = SemanticJournal::new(SemanticEpoch([2; 16]), 64, 2);
        journal
            .append(event(serde_json::json!({"n":1})))
            .expect("one");
        journal
            .append(event(serde_json::json!({"n":2})))
            .expect("two");
        journal
            .append(event(serde_json::json!({"n":3})))
            .expect("three");
        assert_eq!(
            journal.replay(&SemanticEpoch([2; 16]), 0),
            Err(SemanticJournalError::ReplayGap { oldest_seq: 2 })
        );
    }

    #[test]
    fn wrong_epoch_and_future_cursor_fail_closed() {
        let mut journal = SemanticJournal::new(SemanticEpoch([3; 16]), 64, 2);
        journal
            .append(event(serde_json::json!({"ok":true})))
            .expect("append");
        assert_eq!(
            journal.replay(&SemanticEpoch([4; 16]), 0),
            Err(SemanticJournalError::WrongEpoch)
        );
        assert_eq!(
            journal.replay(&SemanticEpoch([3; 16]), 9),
            Err(SemanticJournalError::CursorAhead)
        );
    }

    #[test]
    fn oversized_payload_is_rejected_before_retention() {
        let mut journal = SemanticJournal::new(SemanticEpoch([5; 16]), 1024 * 1024, 2);
        let oversized = serde_json::json!({"text": "x".repeat(MAX_SEMANTIC_PAYLOAD_BYTES)});
        assert_eq!(
            journal.append(event(oversized)),
            Err(SemanticJournalError::PayloadTooLarge)
        );
        assert_eq!(journal.retained_events(), 0);
    }

    #[test]
    fn non_object_payload_is_rejected_before_digesting() {
        let mut journal = SemanticJournal::new(SemanticEpoch([6; 16]), 1024, 2);
        assert_eq!(
            journal.append(event(serde_json::json!(["not", "an", "object"]))),
            Err(SemanticJournalError::PayloadNotObject)
        );
        assert_eq!(journal.retained_events(), 0);
    }

    #[test]
    fn reducer_requires_contiguous_sequences_and_replays_utf8_append_offsets() {
        let mut journal = SemanticJournal::new(SemanticEpoch([7; 16]), 4096, 8);
        let mut state = SemanticState::default();
        let mut first = event(serde_json::json!({"text":"界"}));
        first.op = "text_append".into();
        first.append_offset_utf8_bytes = Some(0);
        let first = journal.append(first).expect("first");
        state.apply(&first).expect("first apply");
        let mut second = event(serde_json::json!({"text":"!"}));
        second.op = "text_append".into();
        second.append_offset_utf8_bytes = Some("界".len() as u64);
        let second = journal.append(second).expect("second");
        state.apply(&second).expect("second apply");
        assert_eq!(state.last_seq(), 2);
        assert_eq!(state.entity("message", "message-1").unwrap()["text"], "界!");
        assert_eq!(state.apply(&second), Err(SemanticApplyError::Duplicate));
    }

    #[test]
    fn reducer_rejects_append_gaps_before_mutating_state() {
        let mut journal = SemanticJournal::new(SemanticEpoch([8; 16]), 4096, 8);
        let mut state = SemanticState::default();
        let mut first = event(serde_json::json!({"text":"x"}));
        first.op = "text_append".into();
        first.append_offset_utf8_bytes = Some(1);
        let first = journal.append(first).expect("first");
        assert_eq!(
            state.apply(&first),
            Err(SemanticApplyError::AppendOffsetMismatch {
                expected: 0,
                got: 1
            })
        );
        assert_eq!(state.last_seq(), 0);
    }
}
