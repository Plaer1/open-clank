//! Versioned authenticated local protocol types for the history service.

use crate::catalog::{
    CaptureManifest, Locator, ResourceExistence, ResourceKey, ResourceMetadata, ResourceType,
    Revision,
};
use crate::operations::{request_digest, ActionRequest};
use crate::restore::{HostMetadata, RestoreRequest};
use crate::retention::PolicySet;
use crate::usage::HistoryUsage;
use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u16 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuthContext {
    pub actor_id: String,
    pub account_id: String,
    pub token: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RequestEnvelope {
    pub protocol_version: u16,
    pub auth: AuthContext,
    pub claimed_digest: String,
    pub request: ActionRequest,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ControlEnvelope {
    pub protocol_version: u16,
    pub auth: AuthContext,
    pub action_id: String,
}

/// Public response for registry operations.  The service keeps the canonical
/// root and path private; callers receive only the opaque handle and its scope.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceHandle {
    pub resource_id: String,
    pub account_id: String,
    pub workspace_id: String,
    pub generation: u64,
}

/// Wire representation of one exact before-state in a parent mutation.
/// Content remains bounded by the existing inline/staged payload limits.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BatchPrepareEntry {
    pub resource_key: ResourceKey,
    pub old_locator: Option<Locator>,
    pub new_locator: Option<Locator>,
    pub expected_revision: Option<Revision>,
    pub existence: ResourceExistence,
    pub resource_type: ResourceType,
    pub metadata: ResourceMetadata,
    #[serde(with = "base64_content")]
    pub content: Option<Vec<u8>>,
    /// A service-owned staged payload. Exactly one of `content` and this id
    /// may be present; staging keeps each entry under the IPC frame limit.
    #[serde(default)]
    pub staged_upload_id: Option<String>,
    pub fingerprint: String,
    pub coverage: CaptureManifest,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ServiceRequest {
    Prepare {
        envelope: RequestEnvelope,
        #[serde(with = "base64_content")]
        content: Option<Vec<u8>>,
        fingerprint: String,
    },
    PrepareBatch {
        envelope: RequestEnvelope,
        #[serde(default = "default_batch_version")]
        batch_version: u32,
        entries: Vec<BatchPrepareEntry>,
    },
    RecordLive {
        envelope: ControlEnvelope,
        receipt: crate::catalog::LiveReceipt,
    },
    Complete {
        envelope: ControlEnvelope,
        #[serde(with = "base64_content")]
        content: Option<Vec<u8>>,
        fingerprint: String,
    },
    /// Bind a create action to the provider's real document identity after
    /// the provider has allocated it, before the live receipt is recorded.
    /// Account/workspace ownership stays immutable across this transition.
    RebindResource {
        envelope: ControlEnvelope,
        resource_id: String,
    },
    /// Open a bounded chunk upload for a content payload larger than one IPC
    /// frame. The staged bytes live in a service-owned private directory.
    StageBegin {
        envelope: ControlEnvelope,
        upload_id: String,
        content_length: u64,
        fingerprint: String,
    },
    StageChunk {
        envelope: ControlEnvelope,
        upload_id: String,
        offset: u64,
        #[serde(with = "base64_content")]
        content: Option<Vec<u8>>,
    },
    StageFinish {
        envelope: ControlEnvelope,
        upload_id: String,
    },
    StageAbort {
        envelope: ControlEnvelope,
        upload_id: String,
    },
    PrepareStaged {
        envelope: RequestEnvelope,
        upload_id: String,
        fingerprint: String,
    },
    CompleteStaged {
        envelope: ControlEnvelope,
        upload_id: String,
        fingerprint: String,
    },
    Abort {
        envelope: ControlEnvelope,
    },
    ReadVersion {
        envelope: ControlEnvelope,
        receipt: crate::catalog::VersionReceipt,
    },
    /// Return the authenticated byte length before a bounded readback.
    /// `None` represents a tombstone; an empty byte payload has length zero.
    ReadVersionInfo {
        envelope: ControlEnvelope,
        receipt: crate::catalog::VersionReceipt,
    },
    /// Read one bounded base64 chunk of an authenticated version payload.
    ReadVersionChunk {
        envelope: ControlEnvelope,
        receipt: crate::catalog::VersionReceipt,
        offset: u64,
        length: u32,
    },
    Reconcile {
        envelope: ControlEnvelope,
        receipt: crate::catalog::LiveReceipt,
    },
    /// Authenticated settings calls use the same single-writer Catalog as capture admission.
    GetPolicy(ControlEnvelope),
    SetPolicy {
        envelope: ControlEnvelope,
        expected_revision: u64,
        policy: PolicySet,
    },
    GetUsage(ControlEnvelope),
    GetStatus(ControlEnvelope),
    /// Restore a host resource through the service-owned provider boundary.
    RestoreHost {
        envelope: ControlEnvelope,
        request: RestoreRequest,
        source: crate::catalog::VersionReceipt,
        destination_path: String,
        #[serde(default)]
        source_host_metadata: Option<HostMetadata>,
    },
    /// Register a provider-resolved resource. The service owns the durable
    /// mapping; a client may suggest an id only when it is continuing an
    /// already trusted provider identity.
    RegisterResource {
        envelope: ControlEnvelope,
        account_id: String,
        workspace_id: String,
        /// Opaque Files root identity selected by the trusted provider.
        /// Older clients may omit it while the service is upgraded.
        #[serde(default)]
        root_id: Option<String>,
        root_path: String,
        relative_path: String,
        #[serde(default)]
        resource_id: Option<String>,
    },
    /// Move an existing opaque resource to a new provider path while retaining
    /// its identity through atomic rename/move.
    UpdateResource {
        envelope: ControlEnvelope,
        resource_id: String,
        account_id: String,
        workspace_id: String,
        #[serde(default)]
        root_id: Option<String>,
        root_path: String,
        relative_path: String,
    },
    /// Commit a provider rename/move in one registry transaction.  The source
    /// keeps its opaque identity while an already-active destination, when
    /// supplied, is tombstoned before the source path is updated.
    MoveResource {
        envelope: ControlEnvelope,
        resource_id: String,
        #[serde(default)]
        replaced_resource_id: Option<String>,
        account_id: String,
        workspace_id: String,
        #[serde(default)]
        root_id: Option<String>,
        root_path: String,
        relative_path: String,
    },
    /// Retain a tombstone for a deleted resource so a later create at the same
    /// path receives a new identity.
    RevokeResource {
        envelope: ControlEnvelope,
        resource_id: String,
        #[serde(default)]
        reason: Option<String>,
    },
    /// Resolve an opaque id for an authorized provider read/restore operation.
    ResolveResource {
        envelope: ControlEnvelope,
        resource_id: String,
    },
    Health(ControlEnvelope),
    Shutdown(ControlEnvelope),
}

fn default_batch_version() -> u32 {
    1
}

/// Capture bytes travel as one base64 string on the JSON wire.  The deserializer also accepts
/// the historical JSON array form so old local clients can be upgraded without losing access to
/// their pending actions; all new serialization uses the bounded string form.
mod base64_content {
    use super::*;
    use serde::de::Error;
    use serde_json::Value;

    pub fn serialize<S>(value: &Option<Vec<u8>>, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        match value {
            Some(bytes) => serializer.serialize_str(&encode_base64(bytes)),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Option<Vec<u8>>, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        match Value::deserialize(deserializer)? {
            Value::Null => Ok(None),
            Value::String(encoded) => decode_base64(&encoded).map(Some).map_err(D::Error::custom),
            Value::Array(values) => values
                .into_iter()
                .map(|value| {
                    value
                        .as_u64()
                        .filter(|byte| *byte <= u8::MAX as u64)
                        .map(|byte| byte as u8)
                        .ok_or_else(|| D::Error::custom("capture array contains a non-byte"))
                })
                .collect::<Result<Vec<_>, _>>()
                .map(Some),
            _ => Err(D::Error::custom(
                "capture content must be base64 or a byte array",
            )),
        }
    }

    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    fn encode_base64(bytes: &[u8]) -> String {
        let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
        for chunk in bytes.chunks(3) {
            let first = chunk[0] as u32;
            let second = chunk.get(1).copied().unwrap_or(0) as u32;
            let third = chunk.get(2).copied().unwrap_or(0) as u32;
            output.push(TABLE[((first >> 2) & 0x3f) as usize] as char);
            output.push(TABLE[(((first << 4) | (second >> 4)) & 0x3f) as usize] as char);
            output.push(if chunk.len() > 1 {
                TABLE[(((second << 2) | (third >> 6)) & 0x3f) as usize] as char
            } else {
                '='
            });
            output.push(if chunk.len() > 2 {
                TABLE[(third & 0x3f) as usize] as char
            } else {
                '='
            });
        }
        output
    }

    fn decode_base64(encoded: &str) -> Result<Vec<u8>, String> {
        if encoded.len() % 4 != 0 {
            return Err("base64 capture has an invalid length".into());
        }
        let mut output = Vec::with_capacity(encoded.len() / 4 * 3);
        for chunk in encoded.as_bytes().chunks(4) {
            let values = chunk
                .iter()
                .copied()
                .map(|byte| match byte {
                    b'A'..=b'Z' => Ok(byte - b'A'),
                    b'a'..=b'z' => Ok(byte - b'a' + 26),
                    b'0'..=b'9' => Ok(byte - b'0' + 52),
                    b'+' => Ok(62),
                    b'/' => Ok(63),
                    b'=' => Ok(64),
                    _ => Err("base64 capture contains an invalid character".to_owned()),
                })
                .collect::<Result<Vec<_>, _>>()?;
            if values[0] >= 64 || values[1] >= 64 || (values[2] == 64 && values[3] != 64) {
                return Err("base64 capture has invalid padding".into());
            }
            output.push((values[0] << 2) | (values[1] >> 4));
            if values[2] < 64 {
                output.push((values[1] << 4) | (values[2] >> 2));
            }
            if values[3] < 64 {
                output.push((values[2] << 6) | values[3]);
            }
        }
        Ok(output)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ServiceResponse {
    Accepted,
    Action(crate::catalog::ActionRecord),
    #[serde(with = "base64_content")]
    Bytes(Option<Vec<u8>>),
    VersionInfo {
        content_length: Option<u64>,
    },
    Chunk {
        offset: u64,
        #[serde(with = "base64_content")]
        content: Option<Vec<u8>>,
        eof: bool,
    },
    Actions(Vec<crate::catalog::ActionRecord>),
    Policy(PolicySet),
    Usage(HistoryUsage),
    Status {
        state: String,
        reason: Option<String>,
        history_paused: bool,
        #[serde(default)]
        staged_bytes: u64,
    },
    Restore(crate::restore::RestoreReceipt),
    Resource {
        handle: ResourceHandle,
        created: bool,
    },
    Staged {
        upload_id: String,
        content_length: u64,
    },
    Health {
        protocol_version: u16,
        account_id: String,
    },
    Error {
        code: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    UnsupportedVersion,
    InvalidToken,
    ActorMismatch,
    AccountMismatch,
    DigestMismatch,
    InvalidResourceKey,
    UntrustedPhysicalLeaseIdentity,
}

pub fn validate(
    envelope: &RequestEnvelope,
    _expected_account: &str,
    expected_token: &str,
) -> Result<(), ProtocolError> {
    if envelope.protocol_version != PROTOCOL_VERSION {
        return Err(ProtocolError::UnsupportedVersion);
    }
    if envelope.auth.token != expected_token {
        return Err(ProtocolError::InvalidToken);
    }
    if envelope.auth.account_id.is_empty()
        || envelope.request.actor_account_id != envelope.auth.account_id
    {
        return Err(ProtocolError::AccountMismatch);
    }
    if !envelope.request.resource_key.is_well_formed()
        || envelope
            .request
            .guard_resource_ids
            .iter()
            .chain(envelope.request.modified_resource_ids.iter())
            .any(|key| !key.is_well_formed())
    {
        return Err(ProtocolError::InvalidResourceKey);
    }
    if !envelope.request.physical_lease_keys.is_empty() {
        return Err(ProtocolError::UntrustedPhysicalLeaseIdentity);
    }
    if envelope.auth.actor_id != envelope.request.actor_id {
        return Err(ProtocolError::ActorMismatch);
    }
    // The local service is the authority for the request digest.  A client may still send a
    // digest as an integrity hint, but an empty hint is valid so language clients do not need to
    // reproduce Rust/serde_json byte-for-byte before the service can bind the action identity.
    if !envelope.claimed_digest.is_empty()
        && envelope.claimed_digest != request_digest(&envelope.request)
    {
        return Err(ProtocolError::DigestMismatch);
    }
    Ok(())
}

pub fn validate_control(
    envelope: &ControlEnvelope,
    _expected_account: &str,
    expected_token: &str,
) -> Result<(), ProtocolError> {
    if envelope.protocol_version != PROTOCOL_VERSION {
        return Err(ProtocolError::UnsupportedVersion);
    }
    if envelope.auth.token != expected_token {
        return Err(ProtocolError::InvalidToken);
    }
    if envelope.auth.account_id.is_empty() {
        return Err(ProtocolError::AccountMismatch);
    }
    Ok(())
}
