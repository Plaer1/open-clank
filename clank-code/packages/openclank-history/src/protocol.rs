//! Versioned authenticated local protocol types for the history service.

use crate::catalog::{
    CaptureManifest, Locator, ResourceExistence, ResourceKey, ResourceMetadata, ResourceOutcome,
    ResourceType, Revision,
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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CrossOriginCandidate {pub position:String,pub resource:crate::catalog::ResourceKey,pub action_id:String,pub recorded_millis:Option<String>,pub state:String,pub availability:String,pub locator:Option<String>,pub registration:Option<crate::registry::ResourceRegistration>,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub graph_witness:Option<WorkspaceActivityWitness>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceActivityWitness {
    pub after_version_id:String,
    pub after_fingerprint:String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CrossOriginCandidatePage {pub incarnation:String,pub origin_revision:u64,pub registry_revision:u64,pub authority_generation:u64,pub items:Vec<CrossOriginCandidate>,pub next:Option<String>}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WorkspaceActivityPage {pub incarnation:String,pub scopes_digest:String,pub registry_revision:u64,pub authority_generation:u64,pub root_id:String,pub workspace_id:String,pub items:Vec<CrossOriginCandidate>,pub next:Option<String>}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub enum RegularFileSourceV1 { Selected {version_ref:String}, Inverse {restore_id:String} }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RegularFilePreviewV1 {pub version:u8,pub consistency:String,pub source_action_id:String,pub source_version_id:String,pub desired:crate::restore::RegularFileStateV1,pub expected:crate::restore::RegularFileObservationV1}

/// Public response for registry operations.  The service keeps the canonical
/// root and path private; callers receive only the opaque handle and its scope.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceHandle {
    pub resource_id: String,
    pub account_id: String,
    pub workspace_id: String,
    pub generation: u64,
}

/// Private account-scoped candidates returned only across the authenticated
/// local service boundary so the Files adapter can resolve its stable ID.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RegisteredResourcePage {
    pub items: Vec<crate::registry::ResourceRegistration>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestoreDestinationPreview {
    /// The exact provider fingerprint to echo when confirming, or `missing`.
    pub expected_fingerprint: String,
    pub exists: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RestoreEffect {
    Create,
    Replace,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceVersionRestorePreview {
    pub resource: ResourceHandle,
    pub source: crate::operations::VersionDescriptor,
    pub destination: RestoreDestinationPreview,
    pub effect: RestoreEffect,
    pub requires_confirmation: bool,
    pub captures_current_destination: bool,
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
pub struct BatchCompleteEntry {
    pub resource_key: ResourceKey,
    pub locator: Option<Locator>,
    pub existence: ResourceExistence,
    pub resource_type: ResourceType,
    pub metadata: ResourceMetadata,
    #[serde(with = "base64_content")]
    pub content: Option<Vec<u8>>,
    #[serde(default)]
    pub staged_upload_id: Option<String>,
    pub fingerprint: String,
    pub coverage: CaptureManifest,
    pub outcome: ResourceOutcome,
}

/// Exact registry pre/post identity for one participant in a captured batch.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RegistryBatchMove {
    pub resource_id: String,
    pub expected_generation: u64,
    pub expected_root_id: String,
    pub expected_relative_path: String,
    pub destination_root_id: String,
    pub destination_root_path: String,
    pub destination_relative_path: String,
    pub replaced_resource_id: Option<String>,
    pub replaced_generation: Option<u64>,
}

/// Folder moves bind the complete registered source and destination subtrees
/// to the same captured batch; this is private to the authenticated provider.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RegistrySubtreeGuard {
    pub root_id: String,
    pub root_path: String,
    pub source_relative_path: String,
    pub destination_relative_path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub destination_root_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub destination_root_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_root_identity: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub destination_root_identity: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RegistrySubtreeEntry {
    pub resource_id: String,
    pub generation: u64,
    pub relative_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CheckpointSelection {
    pub resource_id: String,
    pub expected_fingerprint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CheckpointBinding {
    pub resource_id: String,
    pub root_id: String,
    pub workspace_id: String,
    pub registry_generation: u64,
    pub relative_path: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub staged_upload_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CheckpointStaged {
    pub resource_id: String,
    pub staged_upload_id: String,
    pub fingerprint: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CheckpointVersion {
    pub resource_id: String,
    pub version_ref: String,
    pub fingerprint: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ServiceRequest {
    /// Drain prior frames and acknowledge the exact published authority.
    AuthorityBarrier { envelope: ControlEnvelope, expected_generation: u64, expected_process_id: u32 },
    AgentAuthorityBarrier { envelope: ControlEnvelope, root_id: String, phase: String, drain_session_id: Option<String>, expected_generation: u64, expected_process_id: u32 },
    FilesWorktreeCapabilities(ControlEnvelope),
    LookupFilesWorktreeVersion { envelope: ControlEnvelope, root_id: String, operation_id: String },
    ClaimFilesWorktree { envelope: ControlEnvelope, root_id: String },
    CompleteFilesWorktree { envelope: ControlEnvelope, root_id: String, upload_id: String },
    FinalizeFilesWorktree { envelope: ControlEnvelope, root_id: String },
    PrepareFilesWorktreeInverse { envelope: RequestEnvelope, root_id: String, original_action_id: String, original_version_id: String },
    PrepareFilesWorktreeRedo { envelope: RequestEnvelope, root_id: String, inverse_action_id: String, inverse_version_id: String },
    FinalizeFilesWorktreeInverse { envelope: ControlEnvelope, root_id: String, journal_operation_id: String },
    BeginFilesWorktreeRead { envelope: ControlEnvelope, root_id: String, version_id: String },
    FilesWorktreeReadChunk { envelope: ControlEnvelope, root_id: String, read_id: String, offset: u64 },
    ClaimSelectedCheckpoint { envelope: ControlEnvelope, request_id: String, workspace_id: String,
        root_id: String, items: Vec<CheckpointSelection> },
    FinalizeSelectedCheckpoint { envelope: ControlEnvelope, request_id: String, workspace_id: String,
        root_id: String, items: Vec<CheckpointSelection>, staged: Vec<CheckpointStaged>,
        started_millis: u64, finished_millis: u64 },
    FailSelectedCheckpoint { envelope: ControlEnvelope, request_id: String, workspace_id: String,
        root_id: String, items: Vec<CheckpointSelection> },
    GetSelectedCheckpoint { envelope: ControlEnvelope, request_id: String },
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
    PrepareBatchOnce {
        envelope: RequestEnvelope,
        #[serde(default = "default_batch_version")]
        batch_version: u32,
        entries: Vec<BatchPrepareEntry>,
    },
    RecordLive {
        envelope: ControlEnvelope,
        receipt: crate::catalog::LiveReceipt,
    },
    /// A known committed mutation has no available recovery postimage.
    AfterUnavailable(ControlEnvelope),
    /// Selected capture metadata for the exact actor/account that owns it.
    GetCaptureAction(ControlEnvelope),
    SettleRecovery(ControlEnvelope),
    /// Scoped lookup for a deterministic managed Files action identity.
    LookupManagedAction { envelope: ControlEnvelope, operation_id: String },
    Complete {
        envelope: ControlEnvelope,
        #[serde(with = "base64_content")]
        content: Option<Vec<u8>>,
        fingerprint: String,
    },
    CompleteBatch {
        envelope: ControlEnvelope,
        #[serde(default = "default_batch_version")]
        batch_version: u32,
        entries: Vec<BatchCompleteEntry>,
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
    /// List bounded metadata for one registered Files resource. The service
    /// resolves the opaque resource id through its private registry and binds
    /// the result to the authenticated account before consulting the catalog.
    ListResourceVersions {
        envelope: ControlEnvelope,
        resource_id: String,
        #[serde(default)]
        cursor: Option<String>,
        limit: u16,
    },
    ListResourceChanges {
        envelope: ControlEnvelope,
        resource_id: String,
        #[serde(default)]
        cursor: Option<String>,
        limit: u16,
    },
    /// Resolve an exact completed managed batch under current read authority.
    GetManagedActionBatch {
        envelope: ControlEnvelope,
        request_digest: String,
        complete_digest: String,
        origin: crate::catalog::ManagedToolOriginV1,
    },
    /// Return the catalog-generated locator for one exact authorized action/resource.
    GetManagedActionLocator {
        envelope: ControlEnvelope,
        resource_id: String,
        origin: crate::catalog::ManagedToolOriginV1,
    },
    GetResourceChange {
        envelope: ControlEnvelope,
        resource_id: String,
        before_version_id: String,
        after_version_id: String,
        origin: crate::catalog::ManagedToolOriginV1,
    },
    ListManagedChangeFragments {
        envelope: ControlEnvelope,
        resource_id: String,
        #[serde(default)] cursor: Option<String>,
        limit: u16,
    },
    ListOrderedManagedChanges {envelope:ControlEnvelope,resource_id:String,#[serde(default)] origin_action_id:Option<String>,#[serde(default)] cursor:Option<String>,limit:u16},
    ListCrossOriginChanges {envelope:ControlEnvelope,resource_id:String,origin_action_id:String,#[serde(default)] incarnation:Option<String>,#[serde(default)] origin_revision:Option<u64>,#[serde(default)] registry_revision:Option<u64>,#[serde(default)] authority_generation:Option<u64>,#[serde(default)] after:Option<String>},
    CrossOriginFence {envelope:ControlEnvelope,resource_id:String,origin_action_id:String},
    ListWorkspaceActivity {envelope:ControlEnvelope,resource_id:String,#[serde(default)] incarnation:Option<String>,#[serde(default)] scopes_digest:Option<String>,#[serde(default)] registry_revision:Option<u64>,#[serde(default)] authority_generation:Option<u64>,#[serde(default)] after:Option<String>,#[serde(default)] graph_activity:bool},
    WorkspaceActivityFence {envelope:ControlEnvelope,resource_id:String},
    /// List private registrations for the authenticated account. This is an
    /// internal identity bridge; HTTP callers receive only public Files IDs.
    ListRegisteredResources {
        envelope: ControlEnvelope,
        #[serde(default)]
        cursor: Option<String>,
        limit: u16,
    },
    /// Atomic private folder identity snapshot, with generic alias conflicts.
    RegisteredSubtreeSnapshot {
        envelope: ControlEnvelope,
        account_id: String,
        workspace_id: String,
        root_id: String,
        root_path: String,
        source_relative_path: String,
        destination_relative_path: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        destination_root_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        destination_root_path: Option<String>,
    },
    /// Resolve one listed version to the existing RestoreHost receipt inside
    /// the service. This keeps Lore addresses out of the browser contract.
    ResolveResourceVersion {
        envelope: ControlEnvelope,
        resource_id: String,
        version_ref: String,
    },
    /// Preview an exact version against the current registered Files resource.
    /// The worker resolves its private path and returns only a provider fingerprint.
    PreviewResourceVersionRestore {
        envelope: ControlEnvelope,
        resource_id: String,
        version_ref: String,
    },
    /// Confirm a previewed restore. `missing` is the explicit reviewed value
    /// for a destination that did not exist at preview time.
    RestoreResourceVersion {
        envelope: ControlEnvelope,
        restore_id: String,
        resource_id: String,
        version_ref: String,
        expected_destination_fingerprint: String,
    },
    /// Read only the exact original restore journal; missing never permits replay.
    GetRestoreStatus {
        envelope: ControlEnvelope,
        restore_id: String,
        resource_id: String,
        source_action_id: String,
        source_version_id: String,
        expected_destination_fingerprint: String,
    },
    PreviewRegularFileV1 {envelope:ControlEnvelope,resource_id:String,source:RegularFileSourceV1},
    ExecuteRegularFileV1 {envelope:ControlEnvelope,restore_id:String,resource_id:String,source:RegularFileSourceV1,expected:crate::restore::RegularFileObservationV1},
    GetRegularFileStatusV1 {envelope:ControlEnvelope,restore_id:String,resource_id:String,source:RegularFileSourceV1,expected:crate::restore::RegularFileObservationV1},
    /// Read one bounded chunk using the stable opaque id returned by the
    /// resource-version listing. The exact action/version pair is resolved
    /// inside the service and checked against this resource again.
    DigestResourceVersion { envelope: ControlEnvelope, resource_id: String, version_ref: String },
    ReadResourceVersionChunk {
        envelope: ControlEnvelope,
        resource_id: String,
        version_ref: String,
        offset: u64,
        length: u32,
    },
    Reconcile {
        envelope: ControlEnvelope,
        receipt: crate::catalog::LiveReceipt,
    },
    /// Authenticated settings calls use the same single-writer Catalog as capture admission.
    PutMemeryComponent { envelope: ControlEnvelope, change: String, digest: String, size: u64,
                         upload_id: String, policy_revision: u64, resource: String, relative_path: String },
    ReadMemeryComponent { envelope: ControlEnvelope, change: String, digest: String, resource: String, part: u16 },
    InspectMemeryComponent { envelope: ControlEnvelope, change: String, digest: String, resource: String },
    SweepMemeryComponents(ControlEnvelope),
    GetMemeryManifest { envelope: ControlEnvelope, assets: bool },
    SettleMemeryManifest {
        envelope: ControlEnvelope,
        assets: bool,
        checkpoint: bool,
        generation: u64,
        position: u64,
        witness: String,
        add: Vec<crate::catalog::MemeryReference>,
        remove: Vec<String>,
    },
    GetPolicy(ControlEnvelope),
    GetCapturePolicy(ControlEnvelope),
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
    /// Register a managed media destination whose validated parent suffix is
    /// not yet published. This records identity only; it creates no directory.
    RegisterManagedMediaDestination {
        envelope: ControlEnvelope,
        account_id: String,
        workspace_id: String,
        root_id: Option<String>,
        root_path: String,
        relative_path: String,
        resource_id: Option<String>,
    },
    /// Register one explicitly frozen absent checkout participant under an authorized root; no filesystem effects.
    RegisterCheckoutAbsent { envelope: ControlEnvelope, account_id:String, workspace_id:String, root_id:String, root_path:String, relative_path:String, operation_id:String, paths:Vec<String>, directories:Vec<String> },
    /// Internal folder coordinator destination, derived from one existing
    /// source identity and the exact frozen subtree mapping.
    RegisterManagedFolderDestination {
        envelope: ControlEnvelope,
        account_id: String,
        workspace_id: String,
        root_id: String,
        root_path: String,
        operation_id: String,
        source_resource_id: String,
        source_folder: String,
        destination_folder: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        destination_root_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        destination_root_path: Option<String>,
        #[serde(default)]
        asset_proof: Option<serde_json::Value>,
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
    /// Owner/action-bound all-participant registry commit; exact replay only.
    MoveResourcesBatch {
        envelope: ControlEnvelope,
        account_id: String,
        workspace_id: String,
        moves: Vec<RegistryBatchMove>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        subtree_guard: Option<RegistrySubtreeGuard>,
        #[serde(default)]
        validate_only: bool,
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
    CheckpointCapabilities(ControlEnvelope),
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
pub struct ManagedBatchMember {
    pub resource_id: String,
    pub before_version_id: String,
    pub after_version_id: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManagedActionBatch {
    pub version: u8,
    pub members: Vec<ManagedBatchMember>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ServiceResponse {
    AuthorityBarrier { authority_generation: u64, process_id: u32 },
    AgentAuthorityBarrier { authority_generation: u64, process_id: u32 },
    FilesWorktreeCapabilities { process_id: u32, authority_generation: u64, worktree_capture: bool },
    FilesWorktreeSlot { upload_id: String, fingerprint: String, content_length: u64 },
    FilesWorktreeRead { read_id: String, content_length: u64, fingerprint: String },
    FilesWorktreeVersion { version_id: String },
    FilesWorktreeRedoPrepared { action: crate::catalog::ActionRecord, read_id: String, content_length: u64, fingerprint: String },
    Accepted,
    Action(crate::catalog::ActionRecord),
    BatchOnce { created: bool, action: crate::catalog::ActionRecord },
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
    Versions(crate::operations::ResourceVersionPage),
    Changes(crate::operations::ResourceChangePage),
    ResourceChange(crate::operations::ResourceChange),
    ManagedActionLocator { locator: String },
    ManagedActionBatch(ManagedActionBatch),
    ManagedChangeFragments(crate::operations::ManagedChangePage),
    OrderedManagedChanges(crate::operations::OrderedManagedPage),
    CrossOriginChanges(CrossOriginCandidatePage),
    CrossOriginFence {incarnation:String,origin_revision:u64,registry_revision:u64,authority_generation:u64},
    WorkspaceActivity(WorkspaceActivityPage),
    WorkspaceActivityFence {incarnation:String,scopes_digest:String,registry_revision:u64,authority_generation:u64,root_id:String,workspace_id:String},
    RegisteredResources(RegisteredResourcePage),
    RegisteredSubtreeSnapshot { items: Vec<RegistrySubtreeEntry> },
    ResourceVersionDigest { version: crate::operations::VersionDescriptor, sha256: String, size: u64 },
    ResourceVersionChunk {
        version: crate::operations::VersionDescriptor,
        offset: u64,
        #[serde(with = "base64_content")]
        content: Option<Vec<u8>>,
        eof: bool,
    },
    ResourceVersionSelection {
        resource: ResourceHandle,
        version: crate::operations::VersionDescriptor,
        receipt: crate::catalog::VersionReceipt,
    },
    ResourceVersionRestorePreview(ResourceVersionRestorePreview),
    RestoreStatus { state: String, outcome: Option<crate::restore::RestoreOutcome>, verified: bool },
    RegularFilePreviewV1(RegularFilePreviewV1),
    RegularFileStatusV1 {state:String,verified:bool,current_matches:Option<bool>,consistency:String},
    /// A typed, non-transport absence such as a pruned version or a resource
    /// whose Files permission was revoked after the browser opened it.
    Unavailable {
        code: String,
        message: String,
    },
    Actions(Vec<crate::catalog::ActionRecord>),
    MemeryManifest(crate::catalog::MemeryManifest),
    MemeryComponentLocation(Option<String>),
    Policy(PolicySet),
    CapturePolicy { capture: crate::retention::CapturePolicy, revision: u64 },
    Usage(HistoryUsage),
    Status {
        state: String,
        reason: Option<String>,
        history_paused: bool,
        #[serde(default)]
        last_capture: Option<String>,
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
    CheckpointCapabilities {
        schema_version: u32,
        authority_generation: u64,
        process_id: u32,
        checkpoint_capture: bool,
    },
    CheckpointStatus {
        state: String,
        request_digest: Option<String>,
        locator: Option<String>,
        items: Vec<CheckpointVersion>,
        consistency: Option<String>,
        quiescence: Option<String>,
        started_millis: Option<u64>,
        finished_millis: Option<u64>,
    },
    CheckpointClaim {
        state: String,
        request_digest: String,
        bindings: Vec<CheckpointBinding>,
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
    InvalidManagedToolOrigin,
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
    if envelope.request.managed_tool_origin_v1.as_ref().is_some_and(|origin| {
        let request = &envelope.request;
        let mimo = request.tool_id.as_deref() == Some("mimo-filesystem")
            && request.resource_key.provider == "mimo-filesystem"
            && request.modified_resource_ids.iter().all(|resource| resource.provider == "mimo-filesystem");
        // Ordinary model files are a distinct, strictly single-resource Files
        // capture. The Files service supplies the origin; it grants no scope.
        let ordinary = ((matches!(request.tool_id.as_deref(), Some("write_file" | "edit_file")) && request.operation == "replace")
            || (request.tool_id.as_deref() == Some("write_file") && request.operation == "create"))
            && request.resource_key.provider == "odysseus-files"
            && request.modified_resource_ids.len() == 1
            && request.modified_resource_ids[0] == request.resource_key;
        !origin.is_valid() || request.actor_kind != "agent" || !(mimo || ordinary)
    }) {
        return Err(ProtocolError::InvalidManagedToolOrigin);
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
