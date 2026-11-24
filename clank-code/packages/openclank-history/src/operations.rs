//! Internal typed operation DTOs and Lore reference construction.

use crate::catalog::{
    ActionRecord, ActionState, AfterResource, CaptureManifest, Catalog, CatalogConflict, CatalogResult,
    LiveReceipt, LiveStatus, Locator, MutationBatch, MutationResource, PrepareReceipt,
    ManagedToolOriginV1, ResourceExistence, ResourceKey, ResourceMetadata, ResourceOutcome, ResourceType, Revision,
    VersionContent, VersionReceipt,
};
use crate::retention::{BudgetError, ExpiryRecord, ExpiryState, PersistedReservation, PolicySet};
use crate::usage::{
    add_external_storage, measure_root_with_reservations, HistoryUsage, RetainedVersionUsage,
};
use crate::{Context, HistoryStore, Partition};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(feature = "test_faults")]
use std::sync::atomic::AtomicU8;

static VERSION_SEQUENCE: AtomicU64 = AtomicU64::new(0);

pub const MAX_RESOURCE_VERSION_PAGE_SIZE: u16 = 100;
pub const MAX_RESOURCE_VERSION_CHUNK_BYTES: u32 = 256 * 1024;
const FILE_RESOURCE_PROVIDERS: [&str; 2] = ["filesystem", "odysseus-files"];

/// Bounded metadata returned for one exact Files resource version. The opaque
/// `id` binds an action and receipt version; callers still supply the resource
/// id and the service rechecks that binding before every read.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VersionDescriptor {
    pub id: String,
    pub source_provider: String,
    pub action_id: String,
    pub version_id: String,
    pub phase: String,
    pub operation: String,
    pub timestamp_millis: u64,
    pub display_name: Option<String>,
    pub resource_type: String,
    pub size_bytes: Option<u64>,
    pub fingerprint: String,
    pub action_state: String,
    /// `available`, `absent`, `expiring`, or `expired`.
    pub availability: String,
    pub can_read: bool,
    pub can_restore: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceVersionPage {
    pub items: Vec<VersionDescriptor>,
    pub next_cursor: Option<String>,
}

/// Display-only attribution captured on the same authorized workflow action.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkflowWriterSource {
    pub session_id: String, pub actor_id: String, pub execution_revision: u64,
    pub root_operation_id: String, pub inner_turn_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkflowWriterIdentity {
    pub version: u8, pub session_id: String, pub actor_id: String, pub execution_revision: u64,
    pub root_operation_id: String, pub inner_turn_id: String, pub tool_call_id: String,
    pub source: WorkflowWriterSource,
}

fn workflow_writer_identity(record: &ActionRecord, key: &ResourceKey) -> Option<WorkflowWriterIdentity> {
    if record.actor_account_id != key.account_id || record.actor_kind != "agent" ||
        !record.actor_id.starts_with("agent:") || record.actor_id.len() <= 6 || record.actor_id.len() > 256 ||
        record.tool_id.as_deref() != Some("workflow_artifact") { return None; }
    let raw=record.coverage.as_ref()?.metadata.as_ref()?.get("writerIdentity")?.clone();
    let value: WorkflowWriterIdentity=serde_json::from_value(raw).ok()?;
    let text=|s: &str| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control);
    if value.version != 1 || value.execution_revision > 9_007_199_254_740_991 || value.source.execution_revision > 9_007_199_254_740_991 ||
        ![&value.session_id,&value.actor_id,&value.root_operation_id,&value.inner_turn_id,&value.tool_call_id,
          &value.source.session_id,&value.source.actor_id,&value.source.root_operation_id,&value.source.inner_turn_id].iter().all(|v|text(v)) ||
        value.root_operation_id != value.source.root_operation_id { return None; }
    Some(value)
}

/// One recorded action affecting an authorized resource, not a whole turn.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceChange {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub writer_identity: Option<WorkflowWriterIdentity>,
    pub action_id: String,
    pub actor_id: String,
    pub actor_kind: String,
    pub session_id: Option<String>,
    pub run_id: Option<String>,
    pub task_id: Option<String>,
    pub tool_id: Option<String>,
    pub operation: String,
    pub state: String,
    pub timestamp_millis: Option<u64>,
    pub coverage: String,
    pub rename: String,
    pub binary: String,
    pub before: Option<VersionDescriptor>,
    pub after: Option<VersionDescriptor>,
    /// Multiple receipts for a phase cannot be silently presented as one.
    pub ambiguous_phase: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceChangePage {
    pub version: u8,
    pub coverage: String,
    pub order: String,
    pub revision: u64,
    pub items: Vec<ResourceChange>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManagedChangeMember {
    pub change: ResourceChange,
    pub origin: Option<crate::catalog::ManagedToolOriginV1>,
    pub locator: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManagedChangeFragment {
    pub group_id: Option<String>,
    pub actor_id: Option<String>,
    pub actions: Vec<ManagedChangeMember>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManagedChangePage {
    pub version: u8,
    pub scope: String,
    pub coverage: String,
    pub order: String,
    pub revision: u64,
    pub groups: Vec<ManagedChangeFragment>,
    pub next_cursor: Option<String>,
}

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
pub struct OrderedManagedPage {pub version:u8,pub scope:String,pub coverage:String,pub order:String,pub revision:u64,pub items:Vec<ManagedChangeMember>,pub next_cursor:Option<String>}

#[derive(Serialize)]
#[serde(rename_all="camelCase")]
struct ManagedActionLocator<'a> {
    version:u8, #[serde(rename="actionID")] action_id:&'a str, #[serde(rename="resourceID")] resource_id:&'a str,
    #[serde(rename="beforeVersionID")] before_version_id:&'a str,
    #[serde(rename="afterVersionID")] after_version_id:&'a str, origin:&'a crate::catalog::ManagedToolOriginV1,
}

pub fn cross_origin_action_locator(record:&ActionRecord,key:&ResourceKey)->CatalogResult<Option<String>> {
    Ok(managed_action_locator(record,key).map(|value|serde_json::to_vec(&value).map(|bytes|format!("oc-history-action:v1:{}",URL_SAFE_NO_PAD.encode(bytes)))).transpose()?.filter(|value|value.len()<=4096))
}

/// Mint a selector for an eligible complete action already found by the
/// scoped workspace activity cursor. Current Files access is rechecked by
/// the app before any of this member metadata is returned.
pub fn workspace_activity_witness(record:&ActionRecord,key:&ResourceKey) -> CatalogResult<Option<(String,crate::protocol::WorkspaceActivityWitness)>> {
    let Some(origin)=record.managed_tool_origin_v1.as_ref() else {return Ok(None)};
    let locator=if let Some(locator)=cross_origin_action_locator(record,key)? {
        locator
    } else {
        let Some(complete)=record.complete_request_digest.as_deref() else {return Ok(None)};
        let Ok(members)=crate::catalog::managed_batch_members(record,&key.account_id,&record.request_digest,complete,origin) else {return Ok(None)};
        if !members.iter().any(|(member,_,_)| member==key) {return Ok(None)}
        #[derive(Serialize)]
        #[serde(rename_all="camelCase")]
        struct BatchLocator<'a> {
            version:u8,
            #[serde(rename="actionID")] action_id:&'a str,
            request_digest:&'a str,
            complete_digest:&'a str,
            origin:&'a crate::catalog::ManagedToolOriginV1,
        }
        let value=BatchLocator{version:2,action_id:&record.action_id,request_digest:&record.request_digest,complete_digest:complete,origin};
        format!("oc-history-action:v2:{}",URL_SAFE_NO_PAD.encode(serde_json::to_vec(&value)?))
    };
    if locator.len()>4096 {return Ok(None)}
    let matched=record.after_resources.iter().find(|entry|entry.resource_key.account_id==key.account_id
        && entry.resource_key.workspace_id==key.workspace_id && entry.resource_key.resource_id==key.resource_id
        && matches!(entry.resource_key.provider.as_str(),"mimo-filesystem"|"odysseus-files"));
    Ok(matched.map(|entry|(locator,crate::protocol::WorkspaceActivityWitness{
        after_version_id:entry.after.version_id.clone(),after_fingerprint:entry.after.fingerprint.clone(),
    })))
}

fn managed_action_locator<'a>(record:&'a ActionRecord,key:&ResourceKey)->Option<ManagedActionLocator<'a>> {
    let origin=record.managed_tool_origin_v1.as_ref()?;
    // The registered Files key authorizes the account/workspace/resource; the
    // catalog action itself supplies its actual capture provider. Never relabel it.
    let same=|candidate:&ResourceKey| candidate.account_id==key.account_id && candidate.workspace_id==key.workspace_id && candidate.resource_id==key.resource_id
        && matches!(candidate.provider.as_str(), "mimo-filesystem" | "odysseus-files");
    if !matches!(key.provider.as_str(), "filesystem" | "mimo-filesystem" | "odysseus-files")
        || !origin.is_valid() || record.state!=crate::catalog::ActionState::Complete || record.actor_account_id!=key.account_id
        || record.before_resources.len()!=1 || record.after_resources.len()!=1 || !same(&record.before_resources[0].resource_key)
        || !same(&record.after_resources[0].resource_key)
        || record.before_resources[0].resource_key.provider!=record.after_resources[0].resource_key.provider {return None;}
    // A Files create has a real captured tombstone, not an inferred preimage.
    if record.before_resources[0].resource_key.provider == "odysseus-files" && record.operation == "create" {
        let pre=&record.before_resources[0]; let post=&record.after_resources[0];
        if record.tool_id.as_deref()!=Some("write_file") || pre.existence!=ResourceExistence::Absent
            || !pre.before.as_ref().is_some_and(|v|v.content==VersionContent::Tombstone)
            || post.existence!=ResourceExistence::Present || post.after.content==VersionContent::Tombstone {return None;}
    }
    let before=record.before_resources[0].before.as_ref()?.version_id.as_str();
    let after=record.after_resources[0].after.version_id.as_str();
    if [record.action_id.as_str(),key.resource_id.as_str(),before,after].iter().any(|v|v.is_empty() || v.len()>256 || v.chars().any(char::is_control)) {return None;}
    Some(ManagedActionLocator{version:1,action_id:&record.action_id,resource_id:&record.before_resources[0].resource_key.resource_id,before_version_id:before,after_version_id:after,origin})
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ChangeCursor {
    version: u8,
    policy: String,
    resource_digest: String,
    revision: u64,
    after: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VersionLookupError {
    InvalidCursor,
    InvalidLimit,
    InvalidVersionRef,
    InvalidOffset,
    AmbiguousVersionRef,
    VersionNotFound,
    VersionNotRestorable,
    VersionExpiring,
    VersionExpired,
    Storage(String),
}

impl VersionLookupError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::InvalidCursor => "invalid_cursor",
            Self::InvalidLimit => "invalid_limit",
            Self::InvalidVersionRef => "invalid_version_ref",
            Self::InvalidOffset => "invalid_offset",
            Self::AmbiguousVersionRef => "ambiguous_version_ref",
            Self::VersionNotFound => "version_not_found",
            Self::VersionNotRestorable => "version_not_restorable",
            Self::VersionExpiring => "version_expiring",
            Self::VersionExpired => "version_expired",
            Self::Storage(_) => "history_read_failed",
        }
    }

    pub fn message(&self) -> String {
        match self {
            Self::InvalidCursor => "history page cursor is invalid".into(),
            Self::InvalidLimit => "history page size must be between 1 and 100".into(),
            Self::InvalidVersionRef => "history version identity is invalid".into(),
            Self::InvalidOffset => "history read offset is outside the selected version".into(),
            Self::AmbiguousVersionRef => "the selected history version is ambiguous across source providers; reload the version list".into(),
            Self::VersionNotFound => "the selected history version is missing or no longer belongs to this resource".into(),
            Self::VersionNotRestorable => "the selected history version is not currently eligible for restore".into(),
            Self::VersionExpiring => "the selected history version is being pruned".into(),
            Self::VersionExpired => "the selected history version has been pruned".into(),
            Self::Storage(error) => format!("history version read failed: {error}"),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct VersionIdentity {
    action_id: String,
    version_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    source_provider: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct VersionPageCursor {
    timestamp_millis: u64,
    action_id: String,
    version_id: String,
    #[serde(default = "legacy_filesystem_provider")]
    source_provider: String,
}

fn legacy_filesystem_provider() -> String {
    "filesystem".into()
}

#[derive(Debug, Clone)]
struct VersionCandidate {
    phase: &'static str,
    receipt: VersionReceipt,
    metadata: Option<ResourceMetadata>,
    resource_type: Option<ResourceType>,
    locator: Option<Locator>,
    byte_len: Option<u64>,
}

fn version_ref(action_id: &str, version_id: &str, source_provider: &str) -> String {
    URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&VersionIdentity {
            action_id: action_id.to_owned(),
            version_id: version_id.to_owned(),
            source_provider: Some(source_provider.to_owned()),
        })
        .expect("history version identity is serializable"),
    )
}

pub fn checkpoint_version_ref(action_id: &str, version_id: &str) -> String {
    version_ref(action_id, version_id, "odysseus-files")
}

fn decode_version_ref(value: &str) -> Result<VersionIdentity, VersionLookupError> {
    if value.is_empty() || value.len() > 1024 {
        return Err(VersionLookupError::InvalidVersionRef);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| VersionLookupError::InvalidVersionRef)?;
    let identity: VersionIdentity =
        serde_json::from_slice(&bytes).map_err(|_| VersionLookupError::InvalidVersionRef)?;
    if identity.action_id.is_empty()
        || identity.version_id.is_empty()
        || identity
            .source_provider
            .as_deref()
            .is_some_and(|provider| provider.is_empty() || provider.len() > 128)
    {
        return Err(VersionLookupError::InvalidVersionRef);
    }
    Ok(identity)
}

fn encode_page_cursor(cursor: &VersionPageCursor) -> String {
    URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(cursor).expect("history page cursor is serializable"),
    )
}

fn decode_page_cursor(value: &str) -> Result<VersionPageCursor, VersionLookupError> {
    if value.is_empty() || value.len() > 2048 {
        return Err(VersionLookupError::InvalidCursor);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| VersionLookupError::InvalidCursor)?;
    let cursor: VersionPageCursor =
        serde_json::from_slice(&bytes).map_err(|_| VersionLookupError::InvalidCursor)?;
    if cursor.action_id.is_empty()
        || cursor.version_id.is_empty()
        || cursor.source_provider.is_empty()
        || cursor.source_provider.len() > 128
    {
        return Err(VersionLookupError::InvalidCursor);
    }
    Ok(cursor)
}

fn compare_page_cursor(left: &VersionPageCursor, right: &VersionPageCursor) -> std::cmp::Ordering {
    left.timestamp_millis
        .cmp(&right.timestamp_millis)
        .then_with(|| left.action_id.cmp(&right.action_id))
        .then_with(|| left.version_id.cmp(&right.version_id))
        .then_with(|| left.source_provider.cmp(&right.source_provider))
}

fn resource_version_providers(key: &ResourceKey) -> Vec<&str> {
    if FILE_RESOURCE_PROVIDERS.contains(&key.provider.as_str()) {
        FILE_RESOURCE_PROVIDERS.to_vec()
    } else {
        vec![key.provider.as_str()]
    }
}

/// Accept plain POSIX permissions or st_mode for a regular file only.
pub fn regular_file_permissions_v1(mode:u32)->Option<u32> {
    if mode<=0o777 || (mode & !0o777) == 0o100000 {Some(mode & 0o777)} else {None}
}

fn resource_key_for_provider(key: &ResourceKey, provider: &str) -> ResourceKey {
    ResourceKey {
        account_id: key.account_id.clone(),
        workspace_id: key.workspace_id.clone(),
        provider: provider.to_owned(),
        resource_id: key.resource_id.clone(),
    }
}

fn checkpoint_finished_millis(record: &ActionRecord) -> Option<u64> {
    let metadata=record.coverage.as_ref()?.metadata.as_ref()?;
    let started=metadata.get("started_millis")?.as_u64()?;
    let finished=metadata.get("finished_millis")?.as_u64()?;
    (started>0 && finished>=started && finished-started<=30_000 && finished<=9_007_199_254_740_991).then_some(finished)
}

fn version_candidates(record: &ActionRecord, key: &ResourceKey) -> Vec<VersionCandidate> {
    if record.recovery_only { return Vec::new(); }
    if record.operation == "selected_checkpoint_v1"
        && (record.state != ActionState::CheckpointComplete || checkpoint_finished_millis(record).is_none()) {
        return Vec::new();
    }
    let mut candidates = BTreeMap::<String, VersionCandidate>::new();
    let mut insert = |candidate: VersionCandidate| {
        candidates
            .entry(candidate.receipt.version_id.clone())
            .or_insert(candidate);
    };

    for resource in &record.before_resources {
        if &resource.resource_key == key {
            if let Some(receipt) = &resource.before {
                insert(VersionCandidate {
                    phase: if record.operation == "selected_checkpoint_v1" { "checkpoint" } else { "before" },
                    receipt: receipt.clone(),
                    metadata: Some(resource.metadata.clone()),
                    resource_type: Some(resource.resource_type.clone()),
                    locator: resource.old_locator.clone(),
                    byte_len: resource.coverage.byte_len,
                });
            }
        }
    }
    if let Some(batch) = &record.mutation_batch {
        for resource in &batch.resources {
            if &resource.resource_key == key {
                if let Some(receipt) = &resource.before {
                    insert(VersionCandidate {
                        phase: if record.operation == "selected_checkpoint_v1" { "checkpoint" } else { "before" },
                        receipt: receipt.clone(),
                        metadata: Some(resource.metadata.clone()),
                        resource_type: Some(resource.resource_type.clone()),
                        locator: resource.old_locator.clone(),
                        byte_len: resource.coverage.byte_len,
                    });
                }
            }
        }
    }
    for resource in &record.after_resources {
        if &resource.resource_key == key {
            insert(VersionCandidate {
                phase: "after",
                receipt: resource.after.clone(),
                metadata: Some(resource.metadata.clone()),
                resource_type: Some(resource.resource_type.clone()),
                locator: resource.locator.clone(),
                byte_len: resource.coverage.byte_len,
            });
        }
    }

    if &record.resource_key == key {
        if let Some(receipt) = &record.before {
            insert(VersionCandidate {
                phase: "before",
                receipt: receipt.clone(),
                metadata: None,
                resource_type: None,
                locator: record.original_locator.clone(),
                byte_len: record.coverage.as_ref().and_then(|value| value.byte_len),
            });
        }
        if let Some(receipt) = &record.after {
            insert(VersionCandidate {
                phase: "after",
                receipt: receipt.clone(),
                metadata: None,
                resource_type: None,
                locator: record
                    .destination_locator
                    .clone()
                    .or_else(|| record.original_locator.clone()),
                byte_len: record.coverage.as_ref().and_then(|value| value.byte_len),
            });
        }
    }

    candidates.into_values().collect()
}

fn selected_version_candidate(
    record: &ActionRecord,
    key: &ResourceKey,
    identity: &VersionIdentity,
) -> Result<(String, VersionCandidate), VersionLookupError> {
    let providers = resource_version_providers(key);
    if let Some(selected_provider) = identity.source_provider.as_deref() {
        let managed_mimo = selected_provider=="mimo-filesystem" && FILE_RESOURCE_PROVIDERS.contains(&key.provider.as_str())
            && record.actor_account_id==key.account_id && record.managed_tool_origin_v1.as_ref().is_some_and(|origin| {
                origin.is_valid() && ((record.before_resources.len()==1 && record.after_resources.len()==1)
                    || record.complete_request_digest.as_deref().is_some_and(|complete| {
                        crate::catalog::managed_batch_members(record,&key.account_id,&record.request_digest,complete,origin)
                            .is_ok_and(|members| members.iter().any(|(source,before,after)| source.account_id==key.account_id
                                && source.workspace_id==key.workspace_id && source.resource_id==key.resource_id
                                && (before==&identity.version_id || after==&identity.version_id)))
                    }))
            });
        if !providers.contains(&selected_provider) && !managed_mimo {
            return Err(VersionLookupError::InvalidVersionRef);
        }
        let selected_key = resource_key_for_provider(key, selected_provider);
        return version_candidates(record, &selected_key)
            .into_iter()
            .find(|candidate| candidate.receipt.version_id == identity.version_id)
            .map(|candidate| (selected_provider.to_owned(), candidate))
            .ok_or(VersionLookupError::VersionNotFound);
    }

    let mut matches = providers.into_iter().filter_map(|provider| {
        let provider_key = resource_key_for_provider(key, provider);
        version_candidates(record, &provider_key)
            .into_iter()
            .find(|candidate| candidate.receipt.version_id == identity.version_id)
            .map(|candidate| (provider.to_owned(), candidate))
    });
    let Some(first) = matches.next() else {
        return Err(VersionLookupError::VersionNotFound);
    };
    if matches.next().is_some() {
        return Err(VersionLookupError::AmbiguousVersionRef);
    }
    Ok(first)
}

fn version_descriptor(
    record: &ActionRecord,
    candidate: &VersionCandidate,
    expiry: Option<&ExpiryRecord>,
    source_provider: &str,
) -> VersionDescriptor {
    let availability = match expiry.map(|value| &value.state) {
        Some(ExpiryState::Expiring) => "expiring",
        Some(ExpiryState::Expired) => "expired",
        None if candidate.receipt.content == VersionContent::Tombstone => "absent",
        None => "available",
    }
    .to_owned();
    let can_read = availability == "available";
    let completed_checkpoint = record.operation == "selected_checkpoint_v1"
        && record.state == ActionState::CheckpointComplete
        && checkpoint_finished_millis(record).is_some()
        && candidate.phase == "checkpoint"
        && source_provider == "odysseus-files"
        && record.live.is_none()
        && !record.modified_resource_ids.is_empty()
        && record.modified_resource_ids.len() <= 16
        && record.modified_resource_ids.iter().collect::<BTreeSet<_>>().len() == record.modified_resource_ids.len()
        && record.before_resources.len() == record.modified_resource_ids.len()
        && record.coverage.as_ref().and_then(|c| c.metadata.as_ref())
            .and_then(|m| m.get("items")).and_then(|v| v.as_array())
            .is_some_and(|items| items.len() == record.before_resources.len()
                && record.before_resources.iter().zip(&record.modified_resource_ids).zip(items).all(|((resource,key),item)| {
                    &resource.resource_key == key && key.account_id == record.actor_account_id
                        && key.provider == "odysseus-files"
                        && resource.resource_type == ResourceType::File && resource.existence == ResourceExistence::Present
                        && item.get("resource_id").and_then(|v| v.as_str()) == Some(key.resource_id.as_str())
                        && item.get("expected_fingerprint").and_then(|v| v.as_str()) == Some(resource.fingerprint.as_str())
                        && resource.before.as_ref().is_some_and(|receipt| receipt.fingerprint == resource.fingerprint)
                }))
        && record.before_resources.iter().filter(|resource| resource.before.as_ref() == Some(&candidate.receipt)).count() == 1;
    let can_restore = availability == "available"
        && ((record.operation != "selected_checkpoint_v1" && matches!(&record.state, ActionState::Complete | ActionState::AfterCaptureFailed))
            || completed_checkpoint);
    let resource_type = candidate
        .resource_type
        .as_ref()
        .map(|value| format!("{value:?}").to_lowercase())
        .unwrap_or_else(|| "file".into());
    let fallback_locator = if candidate.phase == "after" {
        record
            .destination_locator
            .as_ref()
            .or(record.original_locator.as_ref())
    } else {
        record.original_locator.as_ref()
    };
    VersionDescriptor {
        id: version_ref(
            &record.action_id,
            &candidate.receipt.version_id,
            source_provider,
        ),
        source_provider: source_provider.to_owned(),
        action_id: record.action_id.clone(),
        version_id: candidate.receipt.version_id.clone(),
        phase: candidate.phase.into(),
        operation: record.operation.clone(),
        timestamp_millis: if record.operation == "selected_checkpoint_v1" && record.state == ActionState::CheckpointComplete {
            checkpoint_finished_millis(record).unwrap_or(0)
        } else { record.timestamp_millis.unwrap_or(0) },
        display_name: candidate
            .locator
            .as_ref()
            .map(|value| value.display_name.clone())
            .or_else(|| fallback_locator.map(|value| value.display_name.clone())),
        resource_type,
        size_bytes: candidate
            .metadata
            .as_ref()
            .and_then(|value| value.size)
            .or(candidate.byte_len),
        fingerprint: candidate.receipt.fingerprint.clone(),
        action_state: format!("{:?}", record.state).to_lowercase(),
        availability,
        can_read,
        can_restore,
    }
}

pub(crate) fn project_resource_change(record: ActionRecord, expiry: Option<ExpiryRecord>, key: &ResourceKey) -> ResourceChange {
    let mut before = None;
    let mut after = None;
    let mut ambiguous_phase = false;
    for provider in resource_version_providers(key) {
        for candidate in version_candidates(&record, &resource_key_for_provider(key, provider)) {
            if candidate.phase == "checkpoint" { continue; }
            let target = if candidate.phase == "before" { &mut before } else { &mut after };
            if target.is_some() { ambiguous_phase = true; }
            else { *target = Some(version_descriptor(&record, &candidate, expiry.as_ref(), provider)); }
        }
    }
    if ambiguous_phase { before = None; after = None; }
    let writer_identity = workflow_writer_identity(&record, key);
    ResourceChange {
        writer_identity,
        action_id: record.action_id, actor_id: record.actor_id, actor_kind: record.actor_kind,
        session_id: record.session_id, run_id: record.run_id, task_id: record.task_id,
        tool_id: record.tool_id, operation: record.operation, state: format!("{:?}", record.state).to_lowercase(),
        timestamp_millis: record.timestamp_millis,
        coverage: if record.coverage.is_some() { "recorded" } else { "unknown" }.into(),
        rename: "unknown".into(), binary: "unknown".into(), before, after, ambiguous_phase,
    }
}

#[cfg(feature = "test_faults")]
static CAPTURE_FAULT: AtomicU8 = AtomicU8::new(0);

#[cfg(feature = "test_faults")]
#[derive(Debug, Clone, Copy)]
pub enum CaptureFault {
    AfterMarker = 1,
    AfterFlush = 2,
    AfterLiveMarker = 3,
}

#[cfg(feature = "test_faults")]
pub fn inject_capture_fault(fault: CaptureFault) {
    CAPTURE_FAULT.store(fault as u8, Ordering::SeqCst);
}

#[cfg(feature = "test_faults")]
fn take_capture_fault(fault: CaptureFault) -> bool {
    CAPTURE_FAULT
        .compare_exchange(fault as u8, 0, Ordering::SeqCst, Ordering::SeqCst)
        .is_ok()
}

#[cfg(feature = "test_faults")]
fn qualification_abort(stage: &str) {
    if std::env::var("OPENCLANK_HISTORY_ABORT_STAGE")
        .ok()
        .as_deref()
        == Some(stage)
    {
        std::process::abort();
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ActionRequest {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    pub action_id: String,
    pub actor_account_id: String,
    pub resource_key: ResourceKey,
    /// Populated only by a trusted server-side physical-resource resolver. The protocol skips
    /// this field so client JSON cannot forge an alias used by the lease/read path.
    #[serde(skip_serializing, skip_deserializing)]
    pub physical_lease_keys: Vec<String>,
    pub guard_resource_ids: Vec<ResourceKey>,
    pub modified_resource_ids: Vec<ResourceKey>,
    pub operation: String,
    pub expected_revision: Option<Revision>,
    pub actor_id: String,
    #[serde(default)]
    pub actor_kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub managed_tool_origin_v1: Option<ManagedToolOriginV1>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub run_id: Option<String>,
    #[serde(default)]
    pub task_id: Option<String>,
    #[serde(default)]
    pub tool_id: Option<String>,
    #[serde(default)]
    pub before_revision: Option<Revision>,
    #[serde(default)]
    pub expected_after_revision: Option<Revision>,
    #[serde(default)]
    pub original_locator: Option<Locator>,
    #[serde(default)]
    pub destination_locator: Option<Locator>,
    #[serde(default)]
    pub timestamp_millis: Option<u64>,
    #[serde(default)]
    pub coverage: Option<CaptureManifest>,
    #[serde(default)]
    pub per_resource_outcomes: Option<Vec<ResourceOutcome>>,
}

/// One exact before-state supplied by a mutation owner. `None` content is
/// meaningful only together with `ResourceExistence::Absent` (or a provider
/// type such as a directory whose bytes are not representable); it is never
/// inferred to mean an unobserved secondary target.
#[derive(Debug, Clone)]
pub struct BatchCaptureInput {
    pub resource_key: ResourceKey,
    pub old_locator: Option<Locator>,
    pub new_locator: Option<Locator>,
    pub expected_revision: Option<Revision>,
    pub existence: ResourceExistence,
    pub resource_type: ResourceType,
    pub metadata: ResourceMetadata,
    pub content: Option<Bytes>,
    pub fingerprint: String,
    pub coverage: CaptureManifest,
}

/// One exact after-state supplied once a live owner has committed a batch.
#[derive(Debug, Clone)]
pub struct BatchAfterInput {
    pub resource_key: ResourceKey,
    pub locator: Option<Locator>,
    pub existence: ResourceExistence,
    pub resource_type: ResourceType,
    pub metadata: ResourceMetadata,
    pub content: Option<Bytes>,
    pub fingerprint: String,
    pub coverage: CaptureManifest,
    pub outcome: ResourceOutcome,
}

fn default_schema_version() -> u32 {
    1
}

pub fn new_action_record(request: ActionRequest) -> ActionRecord {
    let request_digest = normalized_digest(&request);
    ActionRecord {
        schema_version: request.schema_version,
        capture_policy_revision: 0,
        recovery_only: false,
        recovery_settled: false,
        captured_at_millis: 0,
        action_id: request.action_id,
        request_digest,
        actor_account_id: request.actor_account_id,
        resource_key: request.resource_key,
        physical_lease_keys: request.physical_lease_keys,
        guard_resource_ids: request.guard_resource_ids,
        modified_resource_ids: request.modified_resource_ids,
        operation: request.operation,
        expected_revision: request.expected_revision,
        actor_id: request.actor_id,
        actor_kind: request.actor_kind,
        managed_tool_origin_v1: request.managed_tool_origin_v1,
        session_id: request.session_id,
        run_id: request.run_id,
        task_id: request.task_id,
        tool_id: request.tool_id,
        before_revision: request.before_revision,
        expected_after_revision: request.expected_after_revision,
        original_locator: request.original_locator,
        destination_locator: request.destination_locator,
        timestamp_millis: request.timestamp_millis,
        coverage: request.coverage,
        per_resource_outcomes: request.per_resource_outcomes,
        state: ActionState::Intent,
        before: None,
        before_resources: Vec::new(),
        mutation_batch: None,
        after: None,
        after_resources: Vec::new(),
        complete_request_digest: None,
        live: None,
        before_capture_digest: None,
        after_capture_digest: None,
        before_scope_id: None,
        after_scope_id: None,
        before_logical_bytes: 0,
        after_logical_bytes: 0,
        pinned: false,
    }
}

pub fn unique_version_id(action_id: &str, phase: &str) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let sequence = VERSION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("{action_id}:{phase}:{now:x}:{sequence:x}")
}

pub fn unique_context(action_id: &str, version_id: &str) -> [u8; 16] {
    let mut input = String::with_capacity(action_id.len() + version_id.len() + 1);
    input.push_str(action_id);
    input.push(':');
    input.push_str(version_id);
    let digest = blake3::hash(input.as_bytes());
    let mut context = [0u8; 16];
    context.copy_from_slice(&digest.as_bytes()[..16]);
    if context == [0; 16] {
        context[0] = 1;
    }
    context
}

pub fn tombstone_version(action_id: &str, fingerprint: impl Into<String>) -> VersionReceipt {
    let version_id = unique_version_id(action_id, "tombstone");
    VersionReceipt {
        version_id,
        content: VersionContent::Tombstone,
        fingerprint: fingerprint.into(),
    }
}

pub fn lore_version(
    action_id: &str,
    hash_hex: impl Into<String>,
    fingerprint: impl Into<String>,
) -> VersionReceipt {
    lore_version_with_context(
        action_id,
        hash_hex,
        fingerprint,
        unique_context(action_id, "payload"),
    )
}

fn lore_version_with_context(
    action_id: &str,
    hash_hex: impl Into<String>,
    fingerprint: impl Into<String>,
    context: [u8; 16],
) -> VersionReceipt {
    let version_id = unique_version_id(action_id, "bytes");
    VersionReceipt {
        version_id,
        content: VersionContent::Bytes(crate::catalog::LoreRef {
            context,
            hash_hex: hash_hex.into(),
        }),
        fingerprint: fingerprint.into(),
    }
}

pub fn begin(
    catalog: &Catalog,
    request: ActionRequest,
) -> CatalogResult<crate::catalog::BeginResult> {
    if request.operation == "selected_checkpoint_v1" { return Err("reserved checkpoint operation".into()); }
    catalog.begin_action(new_action_record(request))
}

#[allow(dead_code)]
pub(crate) fn apply_live_receipt(
    catalog: &Catalog,
    action_id: &str,
    receipt: LiveReceipt,
) -> CatalogResult<ActionRecord> {
    catalog.record_live(action_id, receipt)
}

/// Internal coordinator that makes a checked Lore flush precede catalog publication.
pub struct HistoryCoordinator {
    pub(crate) catalog: Arc<Catalog>,
    pub(crate) store: HistoryStore,
    held_leases: Mutex<BTreeMap<String, Vec<String>>>,
}

fn digest_descriptor_allowed(value:&VersionDescriptor) -> bool {
    value.phase=="after" && value.availability=="available" && value.can_read && value.resource_type=="file"
        && value.size_bytes.is_some_and(|n|n<=64*1024*1024)
}

#[cfg(test)]
mod wb12_digest_admission_tests {
    use super::*;
    #[test]
    fn exact_size_and_expiry_refuse() {
        let mut value=VersionDescriptor{id:"v".into(),source_provider:"odysseus-files".into(),action_id:"a".into(),version_id:"v".into(),phase:"after".into(),operation:"write".into(),timestamp_millis:1,display_name:None,resource_type:"file".into(),size_bytes:Some(0),fingerprint:String::new(),action_state:"Complete".into(),availability:"available".into(),can_read:true,can_restore:false};
        assert!(digest_descriptor_allowed(&value));
        value.size_bytes=Some(64*1024*1024); assert!(digest_descriptor_allowed(&value));
        value.size_bytes=Some(64*1024*1024+1); assert!(!digest_descriptor_allowed(&value));
        value.size_bytes=Some(1); value.availability="expired".into(); assert!(!digest_descriptor_allowed(&value));
        value.availability="expiring".into(); assert!(!digest_descriptor_allowed(&value));
    }
    #[tokio::test]
    async fn retained_lease_blocks_then_expiry_refuses() {
        let dir=tempfile::tempdir().unwrap();
        let c=HistoryCoordinator::open(dir.path().join("catalog"),dir.path().join("lore"),"account").await.unwrap();
        let key=ResourceKey{account_id:"account".into(),workspace_id:"workspace".into(),provider:"files".into(),resource_id:"resource".into()};
        let req=ActionRequest{schema_version:1,action_id:"expiry-case".into(),actor_account_id:"account".into(),resource_key:key.clone(),physical_lease_keys:vec![],guard_resource_ids:vec![],modified_resource_ids:vec![],operation:"replace".into(),expected_revision:None,actor_id:"actor".into(),actor_kind:"user".into(),managed_tool_origin_v1:None,session_id:None,run_id:None,task_id:None,tool_id:None,before_revision:None,expected_after_revision:None,original_locator:None,destination_locator:None,timestamp_millis:None,coverage:Some(CaptureManifest{content_digest:None,metadata_digest:None,byte_len:Some(3),metadata:None}),per_resource_outcomes:None};
        begin(c.catalog(),req).unwrap();
        c.capture_before("expiry-case",Some(Bytes::from_static(b"old")),format!("sha256:{:x}:3",Sha256::digest(b"old"))).await.unwrap();
        c.begin_apply("expiry-case").unwrap();
        let fp=format!("sha256:{:x}:3",Sha256::digest(b"new"));
        c.record_live("expiry-case",LiveReceipt{action_id:"expiry-case".into(),status:LiveStatus::Committed,fingerprint:Some(fp.clone()),committed_resources:vec![]}).unwrap();
        c.capture_after("expiry-case",Some(Bytes::from_static(b"new")),fp).await.unwrap();
        c.complete("expiry-case").unwrap();
        let version=c.list_resource_versions(&key,None,100).unwrap().items.into_iter().find(|v|v.phase=="after").unwrap();
        let plan=c.prepare_resource_digest(&key,&version.id).unwrap();
        let (_,hash,size)=plan.digest().await.unwrap();
        assert_eq!((hash,size),(format!("{:x}",Sha256::digest(b"new")),3));
        assert!(c.reclaim_checked("expiry-case",1).await.is_err());
        drop(plan);
        c.reclaim_checked("expiry-case",1).await.unwrap();
        assert!(c.prepare_resource_digest(&key,&version.id).is_err());
    }

}

/// A scoped existing Catalog read lease, retained until the streaming task
/// has fully drained, including when the caller stops waiting.
pub struct ResourceDigestPlan {
    catalog: Arc<Catalog>, store: HistoryStore, action: ActionRecord,
    descriptor: VersionDescriptor, receipt: VersionReceipt, owner: String, leases: Vec<String>, partition: Partition,
}
impl Drop for ResourceDigestPlan {
    fn drop(&mut self) { let _ = self.catalog.release_leases(&self.owner, &self.leases); }
}
impl ResourceDigestPlan {
    pub async fn digest(&self) -> CatalogResult<(VersionDescriptor,String,u64)> {
        let size=self.descriptor.size_bytes.ok_or("digest size unavailable")?;
        let actual=match &self.receipt.content {
            VersionContent::Empty if size==0 => format!("{:x}",Sha256::digest([])),
            VersionContent::Bytes(reference) => {
                let address=lore_storage::Address{context:Context::from(reference.context),hash:reference.hash_hex.parse()?};
                self.store.digest_bounded(self.partition,address,size).await?
            },
            _ => return Err("digest content unavailable".into()),
        };
        if self.descriptor.fingerprint != format!("sha256:{actual}:{size}") { return Err("digest fingerprint changed".into()); }
        if self.catalog.expiry(&self.action.action_id)?.is_some() || self.catalog.get_action(&self.action.action_id)?.as_ref()!=Some(&self.action) { return Err("digest version changed".into()); }
        Ok((self.descriptor.clone(),actual,size))
    }
}

impl HistoryCoordinator {
    /// Reserve the selected bytes before the Files owner opens or stages them.
    /// Existing claims are always status-only and cannot be reissued.
    pub fn claim_selected_checkpoint(&self, request: ActionRequest, logical_bytes: u64) -> CatalogResult<(ActionRecord, bool)> {
        if request.operation != "selected_checkpoint_v1" || request.modified_resource_ids.is_empty()
            || request.modified_resource_ids.len() > 16 || logical_bytes > 64 * 1024 * 1024 {
            return Err("invalid selected checkpoint claim".into());
        }
        let action = match self.catalog.begin_checkpoint(new_action_record(request))? {
            crate::catalog::BeginResult::Existing(record) => return Ok((record, false)),
            crate::catalog::BeginResult::New(record) => record,
        };
        let (reservation, scope) = match self.reserve_for_capture(&action, "before", logical_bytes) {
            Ok(value) => value,
            Err(error) => { let _ = self.catalog.fail_checkpoint(&action.action_id); return Err(error); }
        };
        match self.catalog.checkpoint_reservation(&action.action_id, scope, logical_bytes) {
            Ok(record) => Ok((record, true)),
            Err(error) => { let _ = self.release_capture_reservation(&reservation); let _ = self.catalog.fail_checkpoint(&action.action_id); Err(error) }
        }
    }

    pub fn fail_selected_checkpoint(&self, action_id: &str, original_digest: &str) -> CatalogResult<ActionRecord> {
        let existing = self.catalog.get_action(action_id)?.ok_or("checkpoint unavailable")?;
        if existing.request_digest != original_digest || existing.state != ActionState::CheckpointPending {
            return Err("checkpoint unavailable".into());
        }
        self.catalog.fail_checkpoint_reserved(action_id, original_digest)
    }

    pub fn start_selected_checkpoint(&self, action_id: &str, digest: &str, logical_bytes: u64) -> CatalogResult<ActionRecord> {
        let existing=self.catalog.get_action(action_id)?.ok_or("checkpoint unavailable")?;
        if existing.operation!="selected_checkpoint_v1" || existing.request_digest!=digest
            || existing.state!=ActionState::CheckpointPending || existing.before_scope_id.is_none()
            || existing.before_logical_bytes!=logical_bytes {return Err("checkpoint unavailable".into());}
        self.catalog.start_checkpoint_capture(action_id)
    }

    pub fn finish_selected_checkpoint_cleanup(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        self.catalog.finish_checkpoint_cleanup(action_id)
    }

    /// Consume the one original claim. Once this transition starts, neither
    /// an error nor a lost reply authorizes another capture attempt.
    pub async fn finalize_selected_checkpoint(&self, action_id: &str, entries: Vec<BatchCaptureInput>, started_millis: u64, finished_millis: u64) -> CatalogResult<ActionRecord> {
        let action = self.catalog.get_action(action_id)?.ok_or("checkpoint unavailable")?;
        if action.operation != "selected_checkpoint_v1" || action.state != ActionState::CheckpointCapturing
            || action.before_scope_id.is_none() || entries.is_empty() || entries.len() != action.modified_resource_ids.len() {
            return Err("checkpoint is not finalizable".into());
        }
        self.catalog.check_capture(&action, &self.policy_set()?)?;
        let expected = action.coverage.as_ref().and_then(|v| v.metadata.as_ref()).and_then(|v| v.get("items"))
            .and_then(|v| v.as_array()).ok_or("checkpoint selection unavailable")?;
        let mut total = 0u64;
        if expected.len() != entries.len() { return Err("checkpoint selection changed".into()); }
        for ((entry, key), item) in entries.iter().zip(&action.modified_resource_ids).zip(expected) {
            let bytes = entry.content.as_ref().ok_or("checkpoint content unavailable")?;
            total = total.checked_add(bytes.len() as u64).ok_or("checkpoint size overflow")?;
            let fingerprint = format!("sha256:{:x}:{}", Sha256::digest(bytes), bytes.len());
            if &entry.resource_key != key || entry.fingerprint != fingerprint || bytes.len() > 16 * 1024 * 1024
                || item.get("resource_id").and_then(|v| v.as_str()) != Some(key.resource_id.as_str())
                || item.get("expected_fingerprint").and_then(|v| v.as_str()) != Some(fingerprint.as_str()) {
                return Err("checkpoint bytes differ from claim".into());
            }
        }
        if total != action.before_logical_bytes || total > 64 * 1024 * 1024 || started_millis == 0
            || finished_millis < started_millis || finished_millis - started_millis > 30_000 { return Err("checkpoint size or interval changed".into()); }
        let digest = batch_capture_digest(&entries);
        let mut resources = Vec::with_capacity(entries.len());
        for (index, entry) in entries.into_iter().enumerate() {
            let receipt = self.capture(action_id, &action.actor_account_id, entry.content, entry.fingerprint.clone(),
                &format!("checkpoint-{index}-{}", entry.resource_key.resource_id)).await?;
            resources.push(MutationResource { resource_key: entry.resource_key, old_locator: entry.old_locator,
                new_locator: None, expected_revision: entry.expected_revision, existence: entry.existence,
                resource_type: entry.resource_type, metadata: entry.metadata, before: Some(receipt),
                fingerprint: entry.fingerprint, coverage: entry.coverage });
        }
        let result = self.catalog.complete_checkpoint(action_id, resources, digest,
            action.before_scope_id.ok_or("checkpoint reservation unavailable")?, total, started_millis, finished_millis)?;
        if !self.release_capture_reservation(&format!("{action_id}:before"))? {return Err("checkpoint reservation unavailable".into());}
        Ok(result)
    }
    pub fn list_ordered_managed_changes(&self,key:&ResourceKey,selector:Option<&str>,cursor:Option<&str>,limit:u16)->CatalogResult<OrderedManagedPage> {
        if selector.is_some_and(|s|s.is_empty()||s.len()>256||s.chars().any(char::is_control)){return Err("origin selector unavailable".into());}
        let source=resource_key_for_provider(key,"mimo-filesystem");let policy=if selector.is_some(){"exact_origin_v1"}else{"recorded_time_v1"};
        let digest=format!("{:x}",Sha256::digest(serde_json::to_vec(&(&source,selector))?));
        let parsed=if let Some(cursor)=cursor {if cursor.len()>16384{return Err("invalid ordered cursor".into());}let v:ChangeCursor=serde_json::from_slice(&URL_SAFE_NO_PAD.decode(cursor)?)?;if v.version!=1||v.policy!=policy||v.resource_digest!=digest||v.after.is_empty()||v.after.len()>8192{return Err("invalid ordered cursor".into());}Some(v)}else{None};
        let page=self.catalog.ordered_changes(&source.account_id,&source,selector,parsed.as_ref().map(|v|v.revision),parsed.as_ref().map(|v|v.after.as_str()),usize::from(limit))?;
        let mut items=Vec::new();for (record,expiry) in page.items {let origin=record.managed_tool_origin_v1.clone().filter(|v|v.is_valid()&&record.actor_account_id==source.account_id);let locator=managed_action_locator(&record,&source).map(|v|serde_json::to_vec(&v).map(|b|format!("oc-history-action:v1:{}",URL_SAFE_NO_PAD.encode(b)))).transpose()?.filter(|v|v.len()<=4096);items.push(ManagedChangeMember{change:project_resource_change(record,expiry,&source),origin,locator});}
        let next_cursor=page.next.map(|after|serde_json::to_vec(&ChangeCursor{version:1,policy:policy.into(),resource_digest:digest,revision:page.revision,after}).map(|v|URL_SAFE_NO_PAD.encode(v))).transpose()?;
        if next_cursor.as_ref().is_some_and(|v|v.len()>16384){return Err("ordered cursor exceeds limit".into());}
        Ok(OrderedManagedPage{version:1,scope:"one_authorized_resource".into(),coverage:if selector.is_some(){"recorded_origin_only"}else{"recorded_timestamps_only"}.into(),order:if selector.is_some(){"action_id"}else{"recorded_timestamp_action_id"}.into(),revision:page.revision,items,next_cursor})
    }
    /// Exact catalog-owned locator after the service authorizes the registered resource.
    pub fn get_managed_action_locator(&self, key:&ResourceKey, action_id:&str, origin:&crate::catalog::ManagedToolOriginV1) -> CatalogResult<String> {
        if !origin.is_valid() || action_id.is_empty() || action_id.len()>256 || action_id.chars().any(char::is_control) {return Err("managed action unavailable".into());}
        let record=self.catalog.get_action(action_id)?.ok_or("managed action unavailable")?;
        if record.action_id != action_id {return Err("managed action unavailable".into());}
        let exact=managed_action_locator(&record,key).ok_or("managed action unavailable")?;
        if exact.origin!=origin {return Err("managed action unavailable".into());}
        cross_origin_action_locator(&record,key)?.ok_or_else(||"managed action unavailable".into())
    }
    /// Exact human-readable action projection after the service authorizes the resource.
    pub fn get_managed_resource_change(&self, key: &ResourceKey, action_id: &str, before_id: &str, after_id: &str, origin: &crate::catalog::ManagedToolOriginV1) -> CatalogResult<ResourceChange> {
        if !origin.is_valid() || [action_id,before_id,after_id].iter().any(|v| v.is_empty() || v.len()>256 || v.chars().any(char::is_control)) { return Err("managed action unavailable".into()); }
        let record=self.catalog.get_action(action_id)?.ok_or("managed action unavailable")?;
        if record.action_id != action_id {return Err("managed action unavailable".into());}
        if record.before_resources.len() == 1 {
            let exact=managed_action_locator(&record,key).ok_or("managed action unavailable")?;
            if exact.origin!=origin || exact.before_version_id!=before_id || exact.after_version_id!=after_id {return Err("managed action unavailable".into());}
        } else {
            let complete=record.complete_request_digest.as_deref().ok_or("managed action unavailable")?;
            let members=crate::catalog::managed_batch_members(&record,&key.account_id,&record.request_digest,complete,origin)?;
            if !members.iter().any(|(resource,before,after)| resource.account_id==key.account_id && resource.workspace_id==key.workspace_id
                && resource.resource_id==key.resource_id && resource.provider=="mimo-filesystem" && before==before_id && after==after_id) {
                return Err("managed action unavailable".into());
            }
        }
        let expiry=self.catalog.expiry(action_id)?;
        let source = if record.before_resources.len() == 1 {
            record.before_resources[0].resource_key.clone()
        } else {
            resource_key_for_provider(key,"mimo-filesystem")
        };
        Ok(project_resource_change(record,expiry,&source))
    }
    pub fn list_managed_change_fragments(&self,key:&ResourceKey,cursor:Option<&str>,limit:u16)->CatalogResult<ManagedChangePage> {
        const POLICY:&str="managed_resource_fragments_v1";
        let source=resource_key_for_provider(key,"mimo-filesystem");
        let resource_digest=format!("{:x}",Sha256::digest(serde_json::to_vec(&source)?));
        let parsed=if let Some(value)=cursor {
            if value.len()>4096{return Err("invalid change cursor".into());}
            let value:ChangeCursor=serde_json::from_slice(&URL_SAFE_NO_PAD.decode(value)?)?;
            if value.version!=1 || value.policy!=POLICY || value.resource_digest!=resource_digest || value.after.is_empty() || value.after.len()>512{return Err("invalid change cursor".into());}
            Some(value)
        } else {None};
        let page=self.catalog.indexed_changes(&source.account_id,&source,parsed.as_ref().map(|v|v.revision),parsed.as_ref().map(|v|v.after.as_str()),usize::from(limit))?;
        let mut groups:Vec<ManagedChangeFragment>=Vec::new();
        let mut group_positions=std::collections::HashMap::<String,usize>::new();
        for (record,expiry) in page.items {
            let origin=record.managed_tool_origin_v1.clone().filter(|v|v.is_valid() && record.actor_account_id==source.account_id);
            let group_id=origin.as_ref().map(|v|serde_json::to_vec(&("managed.change-group.v1",&source.account_id,&source.workspace_id,&v.host_chat_id,&v.engine_session_id,&v.root_operation_id,&v.actor_id,v.execution_revision,&v.inner_turn_id)).map(|b|format!("{:x}",Sha256::digest(b)))).transpose()?;
            let locator=managed_action_locator(&record,&source).map(|v|serde_json::to_vec(&v).map(|b|format!("oc-history-action:v1:{}",URL_SAFE_NO_PAD.encode(b)))).transpose()?.filter(|v|v.len()<=4096);
            let member=ManagedChangeMember{change:project_resource_change(record,expiry,&source),origin:origin.clone(),locator};
            if let Some(id)=group_id {
                if let Some(&position)=group_positions.get(&id) {groups[position].actions.push(member);}
                else {group_positions.insert(id.clone(),groups.len());groups.push(ManagedChangeFragment{group_id:Some(id),actor_id:origin.as_ref().map(|v|v.actor_id.clone()),actions:vec![member]});}
            } else {groups.push(ManagedChangeFragment{group_id:None,actor_id:None,actions:vec![member]});}
        }
        let next_cursor=page.next_action_id.map(|after|serde_json::to_vec(&ChangeCursor{version:1,policy:POLICY.into(),resource_digest,revision:page.revision,after}).map(|bytes|URL_SAFE_NO_PAD.encode(bytes))).transpose()?;
        Ok(ManagedChangePage{version:1,scope:"one_authorized_resource".into(),coverage:"page_fragment".into(),order:"action_id".into(),revision:page.revision,groups,next_cursor})
    }
    /// Caller must first resolve and authorize this exact registered resource.
    /// Availability is a point-in-time observation, rechecked on version read.
    pub fn list_resource_changes(&self, key: &ResourceKey, cursor: Option<&str>, limit: u16) -> CatalogResult<ResourceChangePage> {
        const POLICY: &str = "resource_actions_v1";
        let canonical = if FILE_RESOURCE_PROVIDERS.contains(&key.provider.as_str()) { resource_key_for_provider(key, "odysseus-files") } else { key.clone() };
        let resource_digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&canonical)?));
        let parsed = if let Some(value) = cursor {
            if value.len() > 4096 { return Err("invalid change cursor".into()); }
            let decoded = URL_SAFE_NO_PAD.decode(value)?;
            let cursor: ChangeCursor = serde_json::from_slice(&decoded)?;
            if cursor.version != 1 || cursor.policy != POLICY || cursor.resource_digest != resource_digest || cursor.after.is_empty() || cursor.after.len() > 512 {
                return Err("invalid change cursor".into());
            }
            Some(cursor)
        } else { None };
        let page = self.catalog.indexed_changes(&key.account_id, key, parsed.as_ref().map(|v| v.revision), parsed.as_ref().map(|v| v.after.as_str()), usize::from(limit))?;
        let mut items = Vec::with_capacity(page.items.len());
        for (record, expiry) in page.items {
            items.push(project_resource_change(record, expiry, key));
        }
        let next_cursor = page.next_action_id.map(|after| {
            serde_json::to_vec(&ChangeCursor { version: 1, policy: POLICY.into(), resource_digest, revision: page.revision, after })
                .map(|bytes| URL_SAFE_NO_PAD.encode(bytes))
        }).transpose()?;
        Ok(ResourceChangePage { version: 1, coverage: "recorded_receipts_only".into(), order: "action_id".into(), revision: page.revision, items, next_cursor })
    }
    pub async fn open(
        catalog_path: impl AsRef<std::path::Path>,
        lore_root: impl AsRef<std::path::Path>,
        account_id: &str,
    ) -> CatalogResult<Self> {
        HistoryStore::initialize_root(lore_root.as_ref())?;
        let catalog = Catalog::open(catalog_path, account_id)?;
        catalog.reconcile_reservations()?;
        let coordinator = Self {
            catalog: Arc::new(catalog),
            store: HistoryStore::open(lore_root).await?,
            held_leases: Mutex::new(BTreeMap::new()),
        };
        for action_id in coordinator.catalog.expiring_actions()? {
            let _ = coordinator
                .reclaim_checked(
                    &action_id,
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_millis() as u64,
                )
                .await;
        }
        Ok(coordinator)
    }

    /// Explicit offline retained-reference inspection must not reclaim expired
    /// payloads or reconcile pending writes while deciding coverage.
    pub async fn open_for_inventory(
        catalog_path: impl AsRef<std::path::Path>,
        lore_root: impl AsRef<std::path::Path>,
        account_id: &str,
    ) -> CatalogResult<Self> {
        Ok(Self {
            catalog: Arc::new(Catalog::open(catalog_path, account_id)?),
            store: HistoryStore::open(lore_root).await?,
            held_leases: Mutex::new(BTreeMap::new()),
        })
    }

    pub fn catalog(&self) -> &Catalog {
        &self.catalog
    }

    /// Return one bounded page of versions for an exact account/workspace/
    /// resource identity. Files resources span the two capture producer
    /// namespaces; each descriptor and cursor retains its source provider.
    pub fn list_resource_versions(
        &self,
        key: &ResourceKey,
        cursor: Option<&str>,
        limit: u16,
    ) -> Result<ResourceVersionPage, VersionLookupError> {
        if !(1..=MAX_RESOURCE_VERSION_PAGE_SIZE).contains(&limit) {
            return Err(VersionLookupError::InvalidLimit);
        }
        let providers = resource_version_providers(key);
        let after = cursor.map(decode_page_cursor).transpose()?;
        if after
            .as_ref()
            .is_some_and(|value| !providers.contains(&value.source_provider.as_str()))
        {
            return Err(VersionLookupError::InvalidCursor);
        }
        let actions = self
            .catalog
            .all_actions()
            .map_err(|error| VersionLookupError::Storage(error.to_string()))?;
        let mut versions = Vec::<(VersionPageCursor, VersionDescriptor)>::new();
        for action in actions {
            let expiry = self
                .catalog
                .expiry(&action.action_id)
                .map_err(|error| VersionLookupError::Storage(error.to_string()))?;
            for provider in providers.iter().copied() {
                let provider_key = resource_key_for_provider(key, provider);
                for candidate in version_candidates(&action, &provider_key) {
                    let descriptor =
                        version_descriptor(&action, &candidate, expiry.as_ref(), provider);
                    versions.push((
                        VersionPageCursor {
                            timestamp_millis: descriptor.timestamp_millis,
                            action_id: descriptor.action_id.clone(),
                            version_id: descriptor.version_id.clone(),
                            source_provider: provider.to_owned(),
                        },
                        descriptor,
                    ));
                }
            }
        }
        versions.sort_by(|left, right| compare_page_cursor(&right.0, &left.0));
        let mut eligible = versions.into_iter().filter(|(position, _)| {
            after
                .as_ref()
                .is_none_or(|cursor| compare_page_cursor(position, cursor).is_lt())
        });
        let page_size = usize::from(limit);
        let mut page = Vec::with_capacity(page_size + 1);
        for item in eligible.by_ref().take(page_size + 1) {
            page.push(item);
        }
        let has_more = page.len() > page_size;
        if has_more {
            page.pop();
        }
        let next_cursor = if has_more {
            page.last().map(|(position, _)| encode_page_cursor(position))
        } else {
            None
        };
        Ok(ResourceVersionPage {
            items: page.into_iter().map(|(_, descriptor)| descriptor).collect(),
            next_cursor,
        })
    }

    /// Resolve a browser selection to the catalog-verified receipt consumed
    /// by RestoreHost. The receipt stays inside the trusted service boundary.
    pub fn resolve_resource_version(
        &self,
        key: &ResourceKey,
        version_ref: &str,
    ) -> Result<(VersionDescriptor, VersionReceipt), VersionLookupError> {
        let identity = decode_version_ref(version_ref)?;
        let action = self
            .catalog
            .get_action(&identity.action_id)
            .map_err(|error| VersionLookupError::Storage(error.to_string()))?
            .ok_or(VersionLookupError::VersionNotFound)?;
        let (source_provider, candidate) =
            selected_version_candidate(&action, key, &identity)?;
        let expiry = self
            .catalog
            .expiry(&action.action_id)
            .map_err(|error| VersionLookupError::Storage(error.to_string()))?;
        let descriptor = version_descriptor(
            &action,
            &candidate,
            expiry.as_ref(),
            &source_provider,
        );
        match descriptor.availability.as_str() {
            "expiring" => return Err(VersionLookupError::VersionExpiring),
            "expired" => return Err(VersionLookupError::VersionExpired),
            _ => {}
        }
        if !descriptor.can_restore {
            return Err(VersionLookupError::VersionNotRestorable);
        }
        Ok((descriptor, candidate.receipt))
    }

    /// Private typed profile: the selected receipt and its mode come from the
    /// same unique recorded resource, never from the caller's metadata.
    pub fn resolve_regular_state_version(
        &self, key:&ResourceKey, version_ref:&str,
    ) -> Result<(VersionDescriptor,VersionReceipt,Option<u32>),VersionLookupError> {
        let identity=decode_version_ref(version_ref)?;
        let action=self.catalog.get_action(&identity.action_id).map_err(|e|VersionLookupError::Storage(e.to_string()))?.ok_or(VersionLookupError::VersionNotFound)?;
        let (provider,candidate)=selected_version_candidate(&action,key,&identity)?;
        let expiry=self.catalog.expiry(&action.action_id).map_err(|e|VersionLookupError::Storage(e.to_string()))?;
        let descriptor=version_descriptor(&action,&candidate,expiry.as_ref(),&provider);
        if descriptor.availability=="expiring" {return Err(VersionLookupError::VersionExpiring)}
        if descriptor.availability=="expired" {return Err(VersionLookupError::VersionExpired)}
        if !matches!(&action.state,ActionState::Complete|ActionState::AfterCaptureFailed|ActionState::CheckpointComplete) || candidate.resource_type!=Some(ResourceType::File) {return Err(VersionLookupError::VersionNotRestorable)}
        if candidate.receipt.content!=VersionContent::Tombstone && !descriptor.can_restore {return Err(VersionLookupError::VersionNotRestorable)}
        if candidate.receipt.content==VersionContent::Tombstone && action.state==ActionState::CheckpointComplete {return Err(VersionLookupError::VersionNotRestorable)}
        let selected=resource_key_for_provider(key,&provider);
        let before=action.before_resources.iter().filter(|r|r.resource_key==selected && r.before.as_ref()==Some(&candidate.receipt));
        let after=action.after_resources.iter().filter(|r|r.resource_key==selected && r.after==candidate.receipt);
        let mut matches=before.map(|r|(&r.existence,&r.resource_type,&r.metadata)).chain(after.map(|r|(&r.existence,&r.resource_type,&r.metadata)));
        let (existence,kind,metadata)=matches.next().ok_or(VersionLookupError::VersionNotRestorable)?;
        if matches.next().is_some() || *kind!=ResourceType::File {return Err(VersionLookupError::VersionNotRestorable)}
        let mode=match (&candidate.receipt.content,existence) {
            (VersionContent::Tombstone,ResourceExistence::Absent)=>None,
            (VersionContent::Tombstone,_)|(_,ResourceExistence::Absent)=>return Err(VersionLookupError::VersionNotRestorable),
            (_,ResourceExistence::Present)=>Some(metadata.mode.and_then(regular_file_permissions_v1).ok_or(VersionLookupError::VersionNotRestorable)?),
        };
        Ok((descriptor,candidate.receipt,mode))
    }

    /// Prepare a bounded after-version digest under the existing resource lease.
    pub fn prepare_resource_digest(&self, key:&ResourceKey, version_ref:&str) -> CatalogResult<ResourceDigestPlan> {
        let identity=decode_version_ref(version_ref).map_err(|_| "digest selection unavailable")?;
        let action=self.catalog.get_action(&identity.action_id)?.ok_or("digest action unavailable")?;
        let (provider,candidate)=selected_version_candidate(&action,key,&identity).map_err(|_| "digest selection unavailable")?;
        let expiry=self.catalog.expiry(&action.action_id)?;
        let descriptor=version_descriptor(&action,&candidate,expiry.as_ref(),&provider);
        if !digest_descriptor_allowed(&descriptor) {return Err("digest version unavailable".into());}
        let partition=Partition::from(self.catalog.owner_partition(&action.resource_key.account_id)?);
        let leases=lease_ids(&action); let owner=format!("digest-read-{}",unique_version_id(&action.action_id,"read"));
        self.catalog.acquire_leases(&owner,&leases)?;
        let plan=ResourceDigestPlan{catalog:self.catalog.clone(),store:self.store.clone(),action,descriptor,receipt:candidate.receipt,owner,leases,partition};
        if plan.catalog.expiry(&plan.action.action_id)?.is_some() || plan.catalog.get_action(&plan.action.action_id)?.as_ref()!=Some(&plan.action) {return Err("digest version changed".into());}
        Ok(plan)
    }

    /// Read one bounded chunk after resolving the opaque selection again
    /// against the exact resource key. No host path or Lore address crosses
    /// this API boundary.
    pub async fn read_resource_version_chunk(
        &self,
        key: &ResourceKey,
        version_ref: &str,
        offset: u64,
        length: u32,
    ) -> Result<(VersionDescriptor, Option<Vec<u8>>, bool), VersionLookupError> {
        if length == 0 || length > MAX_RESOURCE_VERSION_CHUNK_BYTES {
            return Err(VersionLookupError::InvalidLimit);
        }
        let identity = decode_version_ref(version_ref)?;
        let action = self
            .catalog
            .get_action(&identity.action_id)
            .map_err(|error| VersionLookupError::Storage(error.to_string()))?
            .ok_or(VersionLookupError::VersionNotFound)?;
        let (source_provider, candidate) =
            selected_version_candidate(&action, key, &identity)?;
        let expiry = self
            .catalog
            .expiry(&action.action_id)
            .map_err(|error| VersionLookupError::Storage(error.to_string()))?;
        let descriptor = version_descriptor(
            &action,
            &candidate,
            expiry.as_ref(),
            &source_provider,
        );
        match descriptor.availability.as_str() {
            "expiring" => return Err(VersionLookupError::VersionExpiring),
            "expired" => return Err(VersionLookupError::VersionExpired),
            _ => {}
        }
        let Some(bytes) = self
            .read_version(&action.action_id, &candidate.receipt)
            .await
            .map_err(|error| VersionLookupError::Storage(error.to_string()))?
        else {
            return Ok((descriptor, None, true));
        };
        let start = usize::try_from(offset).map_err(|_| VersionLookupError::InvalidOffset)?;
        if start > bytes.len() {
            return Err(VersionLookupError::InvalidOffset);
        }
        let end = start
            .saturating_add(length as usize)
            .min(bytes.len());
        Ok((
            descriptor,
            Some(bytes.slice(start..end).to_vec()),
            end == bytes.len(),
        ))
    }

    pub fn policy_set(&self) -> CatalogResult<PolicySet> {
        self.catalog.policy_set()
    }

    pub fn set_policy(
        &self,
        expected_revision: u64,
        policy: PolicySet,
    ) -> CatalogResult<PolicySet> {
        self.catalog.set_policy(expected_revision, policy)
    }

    pub fn reserve_capture(
        &self,
        reservation: PersistedReservation,
        usage: &HistoryUsage,
    ) -> CatalogResult<()> {
        self.catalog.reserve_capture_scoped(
            reservation.clone(),
            usage.physical_allocated_bytes,
            usage
                .scopes
                .iter()
                .find(|scope| scope.scope_id == reservation.scope_id)
                .map_or(0, |scope| scope.logical_retained_bytes),
        )
    }

    pub fn release_capture_reservation(&self, reservation_id: &str) -> CatalogResult<bool> {
        self.catalog.release_capture_reservation(reservation_id)
    }

    pub fn reserved_inflight_bytes(&self) -> CatalogResult<u64> {
        self.catalog.reserved_inflight_bytes()
    }

    /// Revoke the in-memory mutation leases held by an account that has been
    /// removed from the supervisor credential snapshot. Durable action rows
    /// remain available for audit/recovery, while a recreated account cannot
    /// inherit a stale live worker lease from the deleted credential.
    pub fn release_account_leases(&self, account_id: &str) -> CatalogResult<u64> {
        let actions = self.catalog.all_actions()?;
        let mut released = 0u64;
        for action in actions {
            if action.actor_account_id != account_id {
                continue;
            }
            let leases = lease_ids(&action);
            self.catalog.release_leases(&action.action_id, &leases)?;
            self.held_leases
                .lock()
                .map_err(|_| "lease lock poisoned")?
                .remove(&action.action_id);
            released = released.saturating_add(leases.len() as u64);
        }
        Ok(released)
    }

    /// Measure the same store and durable reservation rows used by admission.  Settings and
    /// live owners therefore observe one authoritative budget rather than a Python projection.
    pub fn usage(&self) -> CatalogResult<HistoryUsage> {
        let reserved = self.catalog.reserved_inflight_bytes()?;
        let measured_at_millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "system clock before unix epoch")?
            .as_millis() as u64;
        let actions = self.catalog.all_actions()?;
        let mut versions = Vec::new();
        for action in actions {
            if self
                .catalog
                .expiry(&action.action_id)?
                .is_some_and(|record| record.state == ExpiryState::Expired)
            {
                continue;
            }
            let locator = action
                .destination_locator
                .as_ref()
                .or(action.original_locator.as_ref())
                .map(|value| std::path::Path::new(value.location_label.as_str()));
            for (receipt, scope_id, logical_bytes) in [
                (
                    action.before.as_ref(),
                    action.before_scope_id.as_ref(),
                    action.before_logical_bytes,
                ),
                (
                    action.after.as_ref(),
                    action.after_scope_id.as_ref(),
                    action.after_logical_bytes,
                ),
            ] {
                let (Some(receipt), Some(scope_id)) = (receipt, scope_id) else {
                    continue;
                };
                versions.push(RetainedVersionUsage {
                    version_id: receipt.version_id.clone(),
                    scope_id: scope_id.clone(),
                    workspace_id: action.resource_key.workspace_id.clone(),
                    locator: locator.map(|value| value.to_string_lossy().into_owned()),
                    logical_bytes,
                    physical_bytes: 0,
                    created_millis: action.timestamp_millis.unwrap_or(0),
                });
            }
        }
        let mut usage = measure_root_with_reservations(
            self.store.root(),
            measured_at_millis,
            reserved,
            0,
            &versions,
        )?;
        add_external_storage(&mut usage, self.catalog.catalog_path())?;
        Ok(usage)
    }

    /// Return a truthful admission state consumed by Settings and IPC owners.
    pub fn status(&self) -> CatalogResult<(String, Option<String>, bool)> {
        let policy = self.catalog.policy_set()?;
        let usage = self.usage()?;
        if !policy.global.enabled {
            return Ok((
                "history_paused_disabled".into(),
                Some("history policy is disabled".into()),
                true,
            ));
        }
        let used = usage
            .physical_allocated_bytes
            .saturating_add(usage.reserved_inflight_bytes);
        if used >= policy.global.total_bytes {
            return Ok((
                "history_paused_budget".into(),
                Some("history budget is full; ordinary saves continue".into()),
                true,
            ));
        }
        Ok(("ready".into(), None, false))
    }

    fn reserve_for_capture(
        &self,
        action: &ActionRecord,
        phase: &str,
        content_bytes: u64,
    ) -> CatalogResult<(String, String)> {
        let policy = self.catalog.policy_set()?;
        self.catalog.check_capture(action, &policy)?;
        let locator = action
            .destination_locator
            .as_ref()
            .or(action.original_locator.as_ref())
            .map(|value| std::path::Path::new(value.location_label.as_str()));
        let effective = policy.effective_for_owner(
            Some(action.actor_account_id.as_str()),
            &action.resource_key.workspace_id,
            locator,
        );
        let reservation_id = format!("{}:{phase}", action.action_id);
        let metadata_bytes = serde_json::to_vec(action)
            .map(|bytes| bytes.len() as u64)
            .unwrap_or(0)
            .saturating_add(4096);
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "system clock before unix epoch")?
            .as_millis() as u64;
        let usage = self.usage()?;
        self.reserve_capture(
            PersistedReservation {
                reservation_id: reservation_id.clone(),
                scope_id: effective.scope_id.clone(),
                bytes: content_bytes,
                metadata_bytes,
                policy_revision: policy.revision,
                created_millis: now,
            },
            &usage,
        )?;
        Ok((reservation_id, effective.scope_id))
    }

    async fn reserve_with_reclaim(
        &self,
        action: &ActionRecord,
        phase: &str,
        content_bytes: u64,
    ) -> CatalogResult<(String, String)> {
        match self.reserve_for_capture(action, phase, content_bytes) {
            Ok(reservation) => Ok(reservation),
            Err(first_error) => {
                let global_failure = first_error
                    .downcast_ref::<BudgetError>()
                    .is_some_and(|error| matches!(error, BudgetError::ExceedsGlobal { .. }));
                let needed_bytes = first_error
                    .downcast_ref::<BudgetError>()
                    .map(|error| match error {
                        BudgetError::ExceedsGlobal {
                            requested,
                            available,
                        }
                        | BudgetError::ExceedsScope {
                            requested,
                            available,
                        }
                        | BudgetError::ExceedsTarget {
                            requested,
                            available,
                        } => requested.saturating_sub(*available),
                        _ => 0,
                    })
                    .unwrap_or(0);
                let reclaimable_failure =
                    first_error
                        .downcast_ref::<BudgetError>()
                        .is_some_and(|error| {
                            matches!(
                                error,
                                BudgetError::ExceedsGlobal { .. }
                                    | BudgetError::ExceedsScope { .. }
                                    | BudgetError::ExceedsTarget { .. }
                            )
                        });
                if !reclaimable_failure {
                    return Err(first_error);
                }
                let policy = self.catalog.policy_set()?;
                let locator = action
                    .destination_locator
                    .as_ref()
                    .or(action.original_locator.as_ref())
                    .map(|value| std::path::Path::new(value.location_label.as_str()));
                let target_scope = policy
                    .effective_for_owner(
                        Some(action.actor_account_id.as_str()),
                        &action.resource_key.workspace_id,
                        locator,
                    )
                    .scope_id;
                let mut candidates = self
                    .catalog
                    .all_actions()?
                    .into_iter()
                    .filter(|candidate| {
                        candidate.action_id != action.action_id && !candidate.recovery_only
                            && matches!(
                                candidate.state,
                                ActionState::Complete
                                    | ActionState::CheckpointComplete
                                    | ActionState::Failed
                                    | ActionState::Conflict
                                    | ActionState::Partial
                            )
                            && !candidate.pinned
                            && (global_failure
                                || target_scope == "global"
                                || candidate.before_scope_id.as_deref()
                                    == Some(target_scope.as_str())
                                || candidate.after_scope_id.as_deref()
                                    == Some(target_scope.as_str()))
                    })
                    .collect::<Vec<_>>();
                candidates.sort_by_key(|candidate| candidate.timestamp_millis.unwrap_or(0));
                let mut reclaimed_bytes = 0u64;
                for candidate in candidates {
                    let candidate_bytes = candidate
                        .before_logical_bytes
                        .saturating_add(candidate.after_logical_bytes)
                        .max(
                            candidate
                                .coverage
                                .as_ref()
                                .and_then(|coverage| coverage.byte_len)
                                .unwrap_or(0),
                        );
                    if self
                        .reclaim_checked(
                            &candidate.action_id,
                            SystemTime::now()
                                .duration_since(UNIX_EPOCH)
                                .unwrap_or_default()
                                .as_millis() as u64,
                        )
                        .await
                        .is_ok()
                    {
                        reclaimed_bytes = reclaimed_bytes.saturating_add(candidate_bytes);
                    }
                    if let Ok(reservation) = self.reserve_for_capture(action, phase, content_bytes)
                    {
                        return Ok(reservation);
                    }
                    if !global_failure && reclaimed_bytes >= needed_bytes {
                        break;
                    }
                }
                Err(first_error)
            }
        }
    }

    /// The producer calls after verifying its authoritative terminal result.
    pub async fn settle_recovery(&self, action: ActionRecord) -> CatalogResult<ActionRecord> {
        if action.recovery_settled { return Ok(action); }
        let now = chrono::Utc::now().timestamp_millis().max(0) as u64;
        if action.recovery_only {
            if !matches!(action.state, ActionState::Complete | ActionState::Failed | ActionState::Conflict |
                ActionState::Aborted | ActionState::AfterCaptureFailed) {
                return Err("operation recovery has not reached a settled state".into());
            }
            self.reclaim_checked(&action.action_id, now).await?;
            return self.catalog.mark_recovery_settled(&action.action_id);
        }
        Ok(action)
    }

    /// Expire terminal payloads without disturbing active leases or pins. A
    /// dropped settlement reply can be retried. After a day, a terminal private
    /// receipt is sufficient for the producer's restart reconciliation.
    pub async fn maintain_capture_retention(&self) -> CatalogResult<()> {
        let now = chrono::Utc::now().timestamp_millis().max(0) as u64;
        let policies = self.policy_set()?;
        let mut candidates = self.catalog.all_actions()?;
        candidates.sort_by_key(|row| std::cmp::Reverse((row.captured_at_millis, row.action_id.clone())));
        let mut retained_counts = BTreeMap::new();
        let mut reclaimed = 0;
        for row in candidates {
            if row.captured_at_millis == 0 || row.pinned || row.recovery_settled { continue; }
            if row.recovery_only {
                if now.saturating_sub(row.captured_at_millis) >= 86_400_000 && matches!(row.state,
                    ActionState::Complete | ActionState::Failed | ActionState::Conflict | ActionState::Aborted | ActionState::AfterCaptureFailed) {
                    if self.settle_recovery(row).await.is_ok() { reclaimed += 1; }
                }
            } else if matches!(row.state, ActionState::Complete | ActionState::CheckpointComplete) {
                let expiry = self.catalog.expiry(&row.action_id)?;
                if expiry.is_some_and(|state| state.state == ExpiryState::Expired) { continue; }
                let capture = policies.capture_for(&row.actor_account_id);
                let old = capture.max_age_days.is_some_and(|days|
                    now.saturating_sub(row.captured_at_millis) >= days.saturating_mul(86_400_000));
                let mut keep = false;
                for key in crate::catalog::action_resources(&row) {
                    let count = retained_counts.entry(key).or_insert(0u64);
                    *count += 1;
                    keep |= *count <= capture.versions_per_item;
                }
                if (old || !keep) && self.reclaim_checked(&row.action_id, now).await.is_ok() { reclaimed += 1; }
            }
            if reclaimed >= 16 { break; }
        }
        Ok(())
    }

    /// Expire all payload roots belonging to a completed action through the
    /// checked Lore maintenance path. The durable `Expiring` row is written
    /// before the first root is touched, so a restart can safely resume this
    /// same action. Active capture/restore actions cannot acquire its leases.
    pub async fn reclaim_checked(
        &self,
        action_id: &str,
        now_millis: u64,
    ) -> CatalogResult<ExpiryRecord> {
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        if !(action.recovery_only && matches!(action.state, ActionState::Aborted | ActionState::AfterCaptureFailed))
            && !matches!(
            action.state,
            ActionState::Complete
                | ActionState::CheckpointComplete
                | ActionState::Failed
                | ActionState::Conflict
                | ActionState::Partial
        ) {
            return Err("active history action is protected".into());
        }
        if action.pinned {
            return Err("pinned history action is protected".into());
        }
        let leases = lease_ids(&action);
        self.catalog.acquire_leases(action_id, &leases)?;
        let result = async {
            let existing = self.catalog.begin_expiry(ExpiryRecord {
                action_id: action_id.into(),
                state: ExpiryState::Expiring,
                bytes: action
                    .coverage
                    .as_ref()
                    .and_then(|manifest| manifest.byte_len)
                    .unwrap_or(0),
                started_millis: now_millis,
                finished_millis: None,
                metadata_expired: false,
            })?;
            if existing.state == ExpiryState::Expired {
                return Ok(existing);
            }
            let partition = self
                .catalog
                .owner_partition(&action.resource_key.account_id)?;
            let mut expired_roots = BTreeSet::new();
            let batch_before = action
                .before_resources
                .iter()
                .filter_map(|resource| resource.before.as_ref());
            for receipt in action
                .before
                .iter()
                .chain(batch_before)
                .chain(action.after.iter())
                .chain(action.after_resources.iter().map(|resource| &resource.after))
            {
                let VersionContent::Bytes(reference) = &receipt.content else {
                    continue;
                };
                let root_key = format!("{:?}:{}", reference.context, reference.hash_hex);
                if !expired_roots.insert(root_key) {
                    continue;
                }
                let hash = reference.hash_hex.parse()?;
                self.store
                    .expire(
                        Partition::from(partition),
                        lore_storage::Address {
                            context: Context::from(reference.context),
                            hash,
                        },
                    )
                    .await?;
            }
            self.store.flush_checked().await?;
            self.store.compact_checked().await?;
            self.catalog.complete_expiry(action_id, now_millis, false)
        }
        .await;
        self.catalog.release_leases(action_id, &leases)?;
        result
    }

    pub fn record_live(
        &self,
        action_id: &str,
        receipt: LiveReceipt,
    ) -> CatalogResult<ActionRecord> {
        if !self
            .held_leases
            .lock()
            .map_err(|_| "lease lock poisoned")?
            .contains_key(action_id)
        {
            return Err(CatalogConflict::LeaseConflict.into());
        }
        let release = !matches!(receipt.status, LiveStatus::Committed);
        let result = self.catalog.record_live(action_id, receipt);
        if release {
            if let Some(lease) = self
                .held_leases
                .lock()
                .map_err(|_| "lease lock poisoned")?
                .remove(action_id)
            {
                self.catalog.release_leases(action_id, &lease)?;
            }
        }
        result
    }

    pub fn begin_apply(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        let lease = lease_ids(&action);
        let already_held = self
            .held_leases
            .lock()
            .map_err(|_| "lease lock poisoned")?
            .contains_key(action_id);
        self.catalog.acquire_leases(action_id, &lease)?;
        match self
            .catalog
            .transition(action_id, ActionState::BeforeDurable, ActionState::Applying)
        {
            Ok(record) => {
                if !already_held {
                    self.held_leases
                        .lock()
                        .map_err(|_| "lease lock poisoned")?
                        .insert(action_id.to_owned(), lease);
                }
                Ok(record)
            }
            Err(error) => {
                if !already_held {
                    self.catalog.release_leases(action_id, &lease)?;
                }
                Err(error)
            }
        }
    }

    /// Atomically move an action's held lease from a provisional create key
    /// to the provider's real document identity.  The new lease is acquired
    /// before the catalog row changes; failure therefore leaves the original
    /// action and lease intact.
    pub fn rebind_resource(
        &self,
        action_id: &str,
        resource_key: ResourceKey,
    ) -> CatalogResult<ActionRecord> {
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        if action.state != ActionState::BeforeDurable {
            return Err("resource identity can only be rebound before live commit".into());
        }
        if action.resource_key.account_id != resource_key.account_id
            || action.resource_key.workspace_id != resource_key.workspace_id
            || action.resource_key.provider != resource_key.provider
        {
            return Err("resource rebind changed an immutable scope".into());
        }
        let old_leases = lease_ids(&action);
        let mut rebound = action.clone();
        rebound.resource_key = resource_key;
        let new_leases = lease_ids(&rebound);
        let added = new_leases
            .iter()
            .filter(|lease| !old_leases.contains(lease))
            .cloned()
            .collect::<Vec<_>>();
        self.catalog.acquire_leases(action_id, &added)?;
        let updated = match self
            .catalog
            .rebind_resource_key(action_id, rebound.resource_key)
        {
            Ok(record) => record,
            Err(error) => {
                let _ = self.catalog.release_leases(action_id, &added);
                return Err(error);
            }
        };
        let removed = old_leases
            .iter()
            .filter(|lease| !new_leases.contains(lease))
            .cloned()
            .collect::<Vec<_>>();
        self.catalog.release_leases(action_id, &removed)?;
        self.held_leases
            .lock()
            .map_err(|_| "lease lock poisoned")?
            .insert(action_id.to_owned(), new_leases);
        Ok(updated)
    }

    pub fn reconcile<F>(&self, resolve: F) -> CatalogResult<Vec<ActionRecord>>
    where
        F: FnMut(&ActionRecord) -> CatalogResult<LiveReceipt>,
    {
        let changed = self.catalog.reconcile(resolve)?;
        let mut held = self.held_leases.lock().map_err(|_| "lease lock poisoned")?;
        for action in &changed {
            held.remove(&action.action_id);
        }
        Ok(changed)
    }

    pub fn reconcile_one(
        &self,
        action_id: &str,
        receipt: LiveReceipt,
    ) -> CatalogResult<ActionRecord> {
        let result = self.catalog.reconcile_one(action_id, receipt);
        if result.is_ok() {
            self.held_leases
                .lock()
                .map_err(|_| "lease lock poisoned")?
                .remove(action_id);
        }
        result
    }

    pub async fn capture_before(
        &self,
        action_id: &str,
        content: Option<Bytes>,
        fingerprint: impl Into<String>,
    ) -> CatalogResult<ActionRecord> {
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        let start_state = if action.state == ActionState::CaptureFailed {
            ActionState::CaptureFailed
        } else {
            ActionState::Intent
        };
        let lease = lease_ids(&action);
        self.catalog.acquire_leases(action_id, &lease)?;
        if let Err(error) =
            self.catalog
                .transition(action_id, start_state, ActionState::CapturingBefore)
        {
            self.catalog.release_leases(action_id, &lease)?;
            return Err(error);
        }
        #[cfg(feature = "test_faults")]
        if take_capture_fault(CaptureFault::AfterMarker) {
            let _ = self.catalog.transition(
                action_id,
                ActionState::CapturingBefore,
                ActionState::CaptureFailed,
            );
            self.catalog.release_leases(action_id, &lease)?;
            return Err("injected failure after CapturingBefore marker".into());
        }
        #[cfg(feature = "test_faults")]
        qualification_abort("before_lore");
        let fingerprint = fingerprint.into();
        let capture_digest =
            capture_input_digest(content.as_ref().map(|value| value.as_ref()), &fingerprint);
        let logical_bytes = content.as_ref().map_or(0, |value| value.len() as u64);
        let (reservation_id, scope_id) = match self
            .reserve_with_reclaim(&action, "before", logical_bytes)
            .await
        {
            Ok(id) => id,
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingBefore,
                    ActionState::CaptureFailed,
                );
                self.catalog.release_leases(action_id, &lease)?;
                return Err(format!("history_paused_budget: {error}").into());
            }
        };
        let receipt = match self
            .capture(
                action_id,
                &action.resource_key.account_id,
                content,
                fingerprint,
                "single-before",
            )
            .await
        {
            Ok(receipt) => receipt,
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingBefore,
                    ActionState::CaptureFailed,
                );
                let _ = self.release_capture_reservation(&reservation_id);
                self.catalog.release_leases(action_id, &lease)?;
                return Err(error);
            }
        };
        match self.catalog.record_before_durable(
            action_id,
            receipt,
            capture_digest,
            scope_id,
            logical_bytes,
        ) {
            Ok(record) => {
                self.release_capture_reservation(&reservation_id)?;
                self.held_leases
                    .lock()
                    .map_err(|_| "lease lock poisoned")?
                    .insert(action_id.to_owned(), lease);
                Ok(record)
            }
            Err(error) => {
                let _ = self.release_capture_reservation(&reservation_id);
                self.catalog.release_leases(action_id, &lease)?;
                Err(error)
            }
        }
    }

    /// Prepare every resource in a parent mutation before the live owner is
    /// admitted. Each content payload uses the same Lore writer and durable
    /// flush as the single-resource path; the catalog acknowledgement is
    /// published only after all entries have receipts.
    pub async fn capture_batch_before(
        &self,
        action_id: &str,
        entries: Vec<BatchCaptureInput>,
    ) -> CatalogResult<ActionRecord> {
        if entries.is_empty() {
            return Err("batch prepare requires at least one resource".into());
        }
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        let mut declared = Vec::new();
        for resource in action
            .guard_resource_ids
            .iter()
            .chain(action.modified_resource_ids.iter())
            .chain(std::iter::once(&action.resource_key))
        {
            if !declared.contains(resource) {
                declared.push(resource.clone());
            }
        }
        let mut seen_resources = Vec::with_capacity(entries.len());
        for entry in &entries {
            if seen_resources
                .iter()
                .any(|resource| *resource == entry.resource_key)
            {
                return Err(
                    "history_prepare_duplicate_resource: batch entries must be unique".into(),
                );
            }
            seen_resources.push(entry.resource_key.clone());
            if entry.resource_key.account_id != action.resource_key.account_id
                || entry.resource_key.workspace_id != action.resource_key.workspace_id
                || entry.resource_key.provider != action.resource_key.provider
            {
                return Err(format!(
                    "history_prepare_scope_conflict: {} is outside the parent resource scope",
                    entry.resource_key.resource_id
                )
                .into());
            }
            if !declared
                .iter()
                .any(|resource| resource == &entry.resource_key)
            {
                return Err(format!(
                    "history_prepare_undeclared_resource: {} is absent from guard/modified resources",
                    entry.resource_key.resource_id
                )
                .into());
            }
            if entry.existence == ResourceExistence::Present
                && !matches!(entry.resource_type, ResourceType::File)
                && entry
                    .coverage
                    .metadata
                    .as_ref()
                    .and_then(|metadata| metadata.get("exact_preimage"))
                    != Some(&serde_json::Value::Bool(true))
            {
                return Err(format!(
                    "history_prepare_inexact_preimage: {} requires exact_preimage coverage",
                    entry.resource_key.resource_id
                )
                .into());
            }
            if entry.existence == ResourceExistence::Absent && entry.content.is_some() {
                return Err(
                    "history_prepare_invalid_preimage: absent resources cannot carry content"
                        .into(),
                );
            }
            if entry.existence == ResourceExistence::Present && entry.content.is_none() {
                return Err(
                    "history_prepare_missing_preimage: present resources require content".into(),
                );
            }
            if entry.existence == ResourceExistence::Present
                && entry.resource_type == ResourceType::Directory
            {
                let content = entry
                    .content
                    .as_ref()
                    .expect("present content checked above");
                let content_digest = blake3::hash(content).to_hex().to_string();
                let sha256_digest = format!("sha256:{:x}", Sha256::digest(content));
                let digest_bound =
                    entry.coverage.content_digest.as_deref() == Some(content_digest.as_str())
                        || entry.coverage.content_digest.as_deref()
                            == Some(sha256_digest.as_str());
                let opaque_manifest = entry
                    .metadata
                    .opaque
                    .as_ref()
                    .and_then(serde_json::Value::as_object)
                    .and_then(|value| value.get("manifest_digest"))
                    .and_then(serde_json::Value::as_str)
                    .is_some_and(|digest| {
                        digest == content_digest || digest == sha256_digest
                    });
                if !digest_bound && !opaque_manifest {
                    return Err(format!(
                        "history_prepare_inexact_preimage: {} directory requires a content-bound manifest or opaque payload",
                        entry.resource_key.resource_id
                    )
                    .into());
                }
            }
        }
        if seen_resources.len() != declared.len() {
            return Err(
                "history_prepare_missing_resource: every declared resource requires exactly one batch entry"
                    .into(),
            );
        }
        let logical_bytes = entries.iter().try_fold(0u64, |sum, entry| {
            sum.checked_add(entry.content.as_ref().map_or(0, |bytes| bytes.len() as u64))
                .ok_or("batch preimage size overflow")
        })?;
        let start_state = if action.state == ActionState::CaptureFailed {
            ActionState::CaptureFailed
        } else {
            ActionState::Intent
        };
        let mut lease = lease_ids(&action);
        lease.sort();
        lease.dedup();
        self.catalog.acquire_leases(action_id, &lease)?;
        if let Err(error) =
            self.catalog
                .transition(action_id, start_state, ActionState::CapturingBefore)
        {
            self.catalog.release_leases(action_id, &lease)?;
            return Err(error);
        }
        let (reservation_id, scope_id) = match self
            .reserve_with_reclaim(&action, "before", logical_bytes)
            .await
        {
            Ok(ids) => ids,
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingBefore,
                    ActionState::CaptureFailed,
                );
                self.catalog.release_leases(action_id, &lease)?;
                return Err(format!("history_paused_budget: {error}").into());
            }
        };
        let mut resources = Vec::with_capacity(entries.len());
        let digest = batch_capture_digest(&entries);
        for (index, entry) in entries.into_iter().enumerate() {
            let receipt = match self
                .capture(
                    action_id,
                    &action.resource_key.account_id,
                    entry.content,
                    entry.fingerprint.clone(),
                    &format!("batch-{index}-{}", entry.resource_key.resource_id),
                )
                .await
            {
                Ok(receipt) => receipt,
                Err(error) => {
                    let _ = self.catalog.transition(
                        action_id,
                        ActionState::CapturingBefore,
                        ActionState::CaptureFailed,
                    );
                    let _ = self.release_capture_reservation(&reservation_id);
                    self.catalog.release_leases(action_id, &lease)?;
                    return Err(format!("history_prepare_failed: {error}").into());
                }
            };
            resources.push(MutationResource {
                resource_key: entry.resource_key,
                old_locator: entry.old_locator,
                new_locator: entry.new_locator,
                expected_revision: entry.expected_revision,
                existence: entry.existence,
                resource_type: entry.resource_type,
                metadata: entry.metadata,
                before: Some(receipt),
                fingerprint: entry.fingerprint,
                coverage: entry.coverage,
            });
        }
        let batch = MutationBatch {
            schema_version: 1,
            parent_action_id: action_id.to_owned(),
            prepare: PrepareReceipt {
                batch_version: 1,
                parent_action_id: action_id.to_owned(),
                resource_count: resources.len() as u32,
                logical_bytes,
                acknowledged: true,
            },
            resources,
        };
        let result = self.catalog.record_batch_before_durable(
            action_id,
            batch,
            digest,
            scope_id,
            logical_bytes,
        );
        let _ = self.release_capture_reservation(&reservation_id);
        match result {
            Ok(record) => {
                self.held_leases
                    .lock()
                    .map_err(|_| "lease lock poisoned")?
                    .insert(action_id.to_owned(), lease);
                Ok(record)
            }
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingBefore,
                    ActionState::CaptureFailed,
                );
                self.catalog.release_leases(action_id, &lease)?;
                Err(error)
            }
        }
    }

    fn release_after_failure(&self, action_id: &str, lease: &[String]) {
        if let Ok(mut held) = self.held_leases.lock() {
            held.remove(action_id);
        }
        let _ = self.catalog.release_leases(action_id, lease);
    }

    /// Persist a capture gap without changing the authoritative committed live
    /// receipt. This retries only recovery state, never the provider mutation.
    pub fn after_unavailable(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        let action = self.catalog.get_action(action_id)?.ok_or("unknown action")?;
        if action.live.as_ref().map(|live| &live.status) != Some(&LiveStatus::Committed) {
            return Err("after-capture failure requires a committed live receipt".into());
        }
        if action.state == ActionState::AfterCaptureFailed {
            return Ok(action);
        }
        if !matches!(action.state, ActionState::Applied | ActionState::CapturingAfter) {
            return Err("after-capture failure has an invalid action state".into());
        }
        if action.state == ActionState::Applied {
            self.catalog.transition(action_id, ActionState::Applied, ActionState::CapturingAfter)?;
        }
        let record = self.catalog.transition(action_id, ActionState::CapturingAfter, ActionState::AfterCaptureFailed)?;
        self.release_after_failure(action_id, &lease_ids(&record));
        Ok(record)
    }

    pub async fn capture_after(
        &self,
        action_id: &str,
        content: Option<Bytes>,
        fingerprint: impl Into<String>,
    ) -> CatalogResult<ActionRecord> {
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        if crate::tree_container::action_marks_tree(&action) {return Err("worktree tree capture requires Source owner".into());}
        if !matches!(
            action.live.as_ref().map(|receipt| &receipt.status),
            Some(LiveStatus::Committed)
        ) {
            return Err(CatalogConflict::InvalidTransition.into());
        }
        let start_state = if action.state == ActionState::AfterCaptureFailed {
            ActionState::AfterCaptureFailed
        } else {
            ActionState::Applied
        };
        let lease = lease_ids(&action);
        self.catalog.acquire_leases(action_id, &lease)?;
        if let Err(error) =
            self.catalog
                .transition(action_id, start_state, ActionState::CapturingAfter)
        {
            self.catalog.release_leases(action_id, &lease)?;
            return Err(error);
        }
        self.held_leases
            .lock()
            .map_err(|_| "lease lock poisoned")?
            .insert(action_id.to_owned(), lease.clone());
        #[cfg(feature = "test_faults")]
        if take_capture_fault(CaptureFault::AfterLiveMarker) {
            let _ = self.catalog.transition(
                action_id,
                ActionState::CapturingAfter,
                ActionState::AfterCaptureFailed,
            );
            self.release_after_failure(action_id, &lease);
            return Err("injected failure after CapturingAfter marker".into());
        }
        #[cfg(feature = "test_faults")]
        qualification_abort("committed_before_after");
        let fingerprint = fingerprint.into();
        let capture_digest =
            capture_input_digest(content.as_ref().map(|value| value.as_ref()), &fingerprint);
        let logical_bytes = content.as_ref().map_or(0, |value| value.len() as u64);
        let (reservation_id, scope_id) = match self
            .reserve_with_reclaim(&action, "after", logical_bytes)
            .await
        {
            Ok(id) => id,
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingAfter,
                    ActionState::AfterCaptureFailed,
                );
                self.release_after_failure(action_id, &lease);
                return Err(format!("history_paused_budget: {error}").into());
            }
        };
        let receipt = match self
            .capture(
                action_id,
                &action.resource_key.account_id,
                content,
                fingerprint,
                "single-after",
            )
            .await
        {
            Ok(receipt) => receipt,
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingAfter,
                    ActionState::AfterCaptureFailed,
                );
                self.release_after_failure(action_id, &lease);
                let _ = self.release_capture_reservation(&reservation_id);
                return Err(error);
            }
        };
        let result =
            self.catalog
                .record_after(action_id, receipt, capture_digest, scope_id, logical_bytes);
        let _ = self.release_capture_reservation(&reservation_id);
        if result.is_err() {
            self.release_after_failure(action_id, &lease);
        }
        result
    }

    /// Capture one journal-verified Source worktree tree as one retained Lore root.
    /// No production constructor for `VerifiedWorktreeStage` exists until the
    /// Native Files/Git owner proves original journal custody and committed state.
    #[cfg(target_os = "linux")]
    pub(crate) async fn capture_after_worktree_tree(&self, action_id: &str,
        stage: &crate::tree_container::VerifiedWorktreeStage) -> CatalogResult<ActionRecord> {
        use crate::tree_container::{ActionCommitment, JournalAfterCommitment};
        let action=self.catalog.get_action(action_id)?.ok_or("unknown action")?;
        let redo=action.operation=="source_worktree_redo_v2";
        if (!redo && action.operation!="source_worktree_create_v2") || action.actor_account_id!=action.resource_key.account_id {
            return Err("worktree action unavailable".into());
        }
        let marker=action.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.get("source_worktree_tree_v2"))
            .cloned().ok_or("worktree provenance unavailable")?;
        let provenance:ActionCommitment=serde_json::from_value(marker)?;
        if provenance.version!=2 || provenance.allowed_after_format!="indexed_tree_v2"
            || provenance.journal_operation_id!=stage.journal_operation_id()
            || !(16..=80).contains(&provenance.journal_operation_id.len())
            || !provenance.journal_operation_id.bytes().all(|c|c.is_ascii_alphanumeric()||c==b'-'||c==b'_') {
            return Err("worktree provenance unavailable".into());
        }
        let (manifest,summary)=crate::tree_container::validate_pinned(stage.file())?;
        if manifest.source.commit_oid!=provenance.commit_oid || manifest.source.tree_oid!=provenance.tree_oid
            || manifest.source.target_manifest_sha256!=provenance.target_manifest_sha256 {
            return Err("worktree target changed".into());
        }
        let fingerprint=format!("sha256:{}:{}",summary.container_sha256,summary.bytes);
        let live=action.live.as_ref().filter(|v|v.status==LiveStatus::Committed && v.action_id==action_id
            && v.fingerprint.as_deref()==Some(fingerprint.as_str()) && v.committed_resources.len()==1)
            .ok_or("worktree committed proof unavailable")?;
        if action.before_resources.len()!=1 {return Err("worktree preimage unavailable".into());}
        let before=&action.before_resources[0];let proof=&live.committed_resources[0];
        if before.resource_key!=action.resource_key || before.existence!=ResourceExistence::Absent
            || before.resource_type!=ResourceType::Directory
            || !before.before.as_ref().is_some_and(|v|v.content==VersionContent::Tombstone)
            || proof.resource_key!=action.resource_key || proof.existence!=ResourceExistence::Present
            || proof.resource_type!=ResourceType::Directory || proof.metadata.size.is_some()
            || proof.fingerprint!=fingerprint || proof.coverage.byte_len!=Some(summary.bytes)
            || proof.coverage.content_digest.as_deref()!=Some(fingerprint.as_str()) {
            return Err("worktree committed proof changed".into());
        }
        let after:JournalAfterCommitment=serde_json::from_value(proof.coverage.metadata.as_ref()
            .and_then(|v|v.get("source_worktree_tree_v2_after")).cloned().ok_or("worktree journal proof unavailable")?)?;
        if after.journal_operation_id!=provenance.journal_operation_id || after.container_sha256!=summary.container_sha256
            || after.container_length!=summary.bytes || after.entry_count!=summary.entries {return Err("worktree journal proof changed".into());}
        let outcome=stage.outcome();
        if outcome.resource_id!=action.resource_key.resource_id || outcome.status!="Committed"
            || !matches!(&outcome.revision,Some(Revision::Opaque{kind,value}) if kind=="fingerprint" && value==&fingerprint) {
            return Err("worktree outcome unavailable".into());
        }
        if matches!(action.state,ActionState::AfterDurable|ActionState::Complete) {
            if action.after_resources.len()!=1 || action.per_resource_outcomes.as_deref()!=Some(std::slice::from_ref(outcome))
                || action.after_resources[0].resource_key!=proof.resource_key || action.after_resources[0].locator!=proof.locator
                || action.after_resources[0].existence!=proof.existence || action.after_resources[0].resource_type!=proof.resource_type
                || action.after_resources[0].metadata!=proof.metadata || action.after_resources[0].after.fingerprint!=fingerprint
                || action.after.as_ref()!=Some(&action.after_resources[0].after)
                || action.after_resources[0].fingerprint!=fingerprint || action.after_resources[0].coverage!=proof.coverage
                || action.after_capture_digest.as_deref()!=Some(batch_after_capture_digest(&action.after_resources,std::slice::from_ref(outcome)).as_str()) {
                return Err("worktree after retry changed".into());
            }
            return Ok(action);
        }
        let start=if action.state==ActionState::AfterCaptureFailed {ActionState::AfterCaptureFailed} else {ActionState::Applied};
        let lease=if redo {
            let marker=action.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.get("source_worktree_redo_v2")).ok_or("worktree redo unavailable")?;
            let inverse_id=marker.get("inverse_action_id").and_then(serde_json::Value::as_str).ok_or("worktree redo unavailable")?;
            let version_id=marker.get("inverse_version_id").and_then(serde_json::Value::as_str).ok_or("worktree redo unavailable")?;
            let inverse=self.catalog.get_action(inverse_id)?.ok_or("worktree redo unavailable")?;
            let held=self.held_leases.lock().map_err(|_|"lease lock poisoned")?.get(action_id).cloned().ok_or("worktree redo custody unavailable")?;
            if lease_ids(&action).iter().chain(lease_ids(&inverse).iter()).any(|key|!held.contains(key)) {return Err("worktree redo custody unavailable".into())}
            let policy=self.catalog.policy_set()?;
            let locator=action.destination_locator.as_ref().or(action.original_locator.as_ref()).map(|v|std::path::Path::new(v.location_label.as_str()));
            let effective=policy.effective_for_owner(Some(action.actor_account_id.as_str()),&action.resource_key.workspace_id,locator);
            self.catalog.checked_worktree_redo_reservation(action_id,inverse_id,version_id,summary.bytes,&effective.scope_id,policy.revision)?;
            held
        } else {
            let lease=lease_ids(&action);self.catalog.acquire_leases(action_id,&lease)?;lease
        };
        if action.state!=ActionState::CapturingAfter {
            if let Err(error)=self.catalog.transition(action_id,start,ActionState::CapturingAfter) {
                if !redo {self.catalog.release_leases(action_id,&lease)?;}return Err(error);
            }
        }
        if !redo {self.held_leases.lock().map_err(|_|"lease lock poisoned")?.insert(action_id.to_owned(),lease.clone());}
        let (reservation,scope)=if redo {
            let policy=self.catalog.policy_set()?;
            let locator=action.destination_locator.as_ref().or(action.original_locator.as_ref()).map(|v|std::path::Path::new(v.location_label.as_str()));
            let effective=policy.effective_for_owner(Some(action.actor_account_id.as_str()),&action.resource_key.workspace_id,locator);
            (format!("{action_id}:after"),effective.scope_id)
        } else {match self.reserve_with_reclaim(&action,"after",summary.bytes).await {
            Ok(value)=>value,Err(error)=>{
                let _=self.catalog.transition(action_id,ActionState::CapturingAfter,ActionState::AfterCaptureFailed);
                self.release_after_failure(action_id,&lease);return Err(format!("history_paused_budget: {error}").into());
            }
        }};
        let captured=async {
            let partition=self.catalog.ensure_owner_partition(&action.resource_key.account_id)?;
            let version_id=unique_version_id(action_id,&format!("bytes:batch-after-0-{}",action.resource_key.resource_id));
            let context=unique_context(action_id,&version_id);
            let (address,actual)=self.store.write_tree_pinned(Partition::from(partition),Context::from(context),stage.file()).await?;
            if actual!=summary {return Err("worktree staged tree changed".into());}
            let receipt=VersionReceipt{version_id,content:VersionContent::Bytes(crate::catalog::LoreRef{context,hash_hex:address.hash.to_string()}),fingerprint:fingerprint.clone()};
            let after_resource=AfterResource{resource_key:proof.resource_key.clone(),locator:proof.locator.clone(),existence:proof.existence.clone(),
                resource_type:proof.resource_type.clone(),metadata:proof.metadata.clone(),after:receipt,fingerprint:fingerprint.clone(),coverage:proof.coverage.clone()};
            let outcomes=vec![outcome.clone()];let resources=vec![after_resource];
            let digest=batch_after_capture_digest(&resources,&outcomes);
            self.catalog.record_after_batch(action_id,resources,outcomes,digest,scope,summary.bytes)
        }.await;
        if captured.is_ok() || !redo {let _=self.release_capture_reservation(&reservation);}
        if captured.is_err() && !redo {
            let _=self.catalog.transition(action_id,ActionState::CapturingAfter,ActionState::AfterCaptureFailed);
            self.release_after_failure(action_id,&lease);
        }
        captured
    }

    /// Persist a one-shot original-action claim after the Files protocol has
    /// authenticated its root-bound role and checked the registered resource.
    #[cfg(target_os="linux")]
    #[doc(hidden)]
    pub fn claim_files_worktree_after(&self, action_id:&str) -> CatalogResult<ActionRecord> {
        let action=self.catalog.get_action(action_id)?.ok_or("unknown action")?;
        if !matches!(action.operation.as_str(),"source_worktree_create_v2"|"source_worktree_redo_v2") || !crate::tree_container::action_marks_tree(&action)
            || action.state!=ActionState::Applied || !action.live.as_ref().is_some_and(|r|r.action_id==action_id && r.status==LiveStatus::Committed) {return Err("worktree claim unavailable".into());}
        self.catalog.transition(action_id,ActionState::Applied,ActionState::CapturingAfter)
    }

    /// Trusted Files service entry point. Its caller must hold the authenticated
    /// root-purpose slot and the uploaded file's exclusive registry lock. No
    /// path, journal ID, result or fingerprint is accepted from the caller.
    #[cfg(target_os="linux")]
    #[doc(hidden)]
    pub async fn complete_files_worktree_stage(&self, action_id:&str, file:std::fs::File) -> CatalogResult<ActionRecord> {
        use crate::tree_container::{ActionCommitment, VerifiedWorktreeStage};
        let action=self.catalog.get_action(action_id)?.ok_or("unknown action")?;
        if !matches!(action.operation.as_str(),"source_worktree_create_v2"|"source_worktree_redo_v2") || action.state!=ActionState::CapturingAfter {return Err("worktree claim unavailable".into());}
        let provenance:ActionCommitment=serde_json::from_value(action.coverage.as_ref().and_then(|c|c.metadata.as_ref())
            .and_then(|m|m.get("source_worktree_tree_v2")).cloned().ok_or("worktree provenance unavailable")?)?;
        let live=action.live.as_ref().filter(|r|r.action_id==action_id && r.status==LiveStatus::Committed && r.committed_resources.len()==1).ok_or("worktree live unavailable")?;
        let fingerprint=live.fingerprint.as_ref().ok_or("worktree fingerprint unavailable")?.clone();
        let outcome=ResourceOutcome{resource_id:action.resource_key.resource_id.clone(),status:"Committed".into(),revision:Some(Revision::Opaque{kind:"fingerprint".into(),value:fingerprint})};
        let stage=VerifiedWorktreeStage::from_files_stage(file,provenance.journal_operation_id,outcome);
        self.capture_after_worktree_tree(action_id,&stage).await
    }

    /// Private Source/Files terminal path for the exact committed inverse.
    /// The caller must be the retained authenticated Files owner after its
    /// journal records both the quarantine and Source Git metadata ACKs.
    #[cfg(target_os="linux")]
    #[doc(hidden)]
    pub async fn capture_inverse_absence(&self,action_id:&str,journal_operation_id:&str)->CatalogResult<ActionRecord>{
        let action=self.catalog.get_action(action_id)?.ok_or("worktree unavailable")?;
        let provenance=action.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.get("source_worktree_tree_v2")).ok_or("worktree unavailable")?;
        if action.operation!="source_worktree_inverse_v2" || !crate::tree_container::action_marks_tree(&action)
            || action.state!=ActionState::Applied || provenance.get("journal_operation_id").and_then(serde_json::Value::as_str)!=Some(journal_operation_id)
            || action.before_resources.len()!=1 || action.before_resources[0].resource_key!=action.resource_key {return Err("worktree unavailable".into())}
        let live=action.live.as_ref().filter(|v|v.action_id==action_id && v.status==LiveStatus::Committed && v.fingerprint.as_deref()==Some("missing") && v.committed_resources.len()==1).ok_or("worktree outcome unavailable")?;
        let proof=&live.committed_resources[0];
        if proof.resource_key!=action.resource_key || proof.existence!=ResourceExistence::Absent || proof.resource_type!=ResourceType::Directory
            || proof.fingerprint!="missing" || proof.metadata.size.is_some()
            || proof.coverage.metadata.as_ref().and_then(|v|v.get("source_worktree_inverse_v2")).and_then(|v|v.get("journal_operation_id")).and_then(serde_json::Value::as_str)!=Some(journal_operation_id) {return Err("worktree outcome unavailable".into())}
        let lease=lease_ids(&action);self.catalog.acquire_leases(action_id,&lease)?;
        if let Err(error)=self.catalog.transition(action_id,ActionState::Applied,ActionState::CapturingAfter){return Err(error)}
        self.held_leases.lock().map_err(|_|"lease lock poisoned")?.insert(action_id.to_owned(),lease.clone());
        let (reservation,scope)=match self.reserve_with_reclaim(&action,"after",0).await {Ok(v)=>v,Err(error)=>{
            let _=self.catalog.transition(action_id,ActionState::CapturingAfter,ActionState::AfterCaptureFailed);
            self.release_after_failure(action_id,&lease);return Err(format!("history_paused_budget: {error}").into());}};
        let result=async {
            let receipt=self.capture(action_id,&action.resource_key.account_id,None,"missing".into(),"worktree-inverse-after").await?;
            let outcome=ResourceOutcome{resource_id:action.resource_key.resource_id.clone(),status:"Committed".into(),revision:Some(Revision::Opaque{kind:"fingerprint".into(),value:"missing".into()})};
            let resource=AfterResource{resource_key:proof.resource_key.clone(),locator:proof.locator.clone(),existence:ResourceExistence::Absent,
                resource_type:ResourceType::Directory,metadata:proof.metadata.clone(),after:receipt,fingerprint:"missing".into(),coverage:proof.coverage.clone()};
            let resources=vec![resource];let outcomes=vec![outcome];let digest=batch_after_capture_digest(&resources,&outcomes);
            self.catalog.record_after_batch(action_id,resources,outcomes,digest,scope,0)
        }.await;
        let _=self.release_capture_reservation(&reservation);
        if result.is_err(){let _=self.catalog.transition(action_id,ActionState::CapturingAfter,ActionState::AfterCaptureFailed);self.release_after_failure(action_id,&lease);}
        result
    }

    /// Only an authenticated Files service owner may call this with its own
    /// fresh pinned stage; generic read/restore APIs remain closed to v2 trees.
    #[cfg(target_os="linux")]
    #[doc(hidden)]
    pub async fn read_files_worktree_stage(&self, action_id:&str, account_id:&str, version_id:&str, file:std::fs::File) -> CatalogResult<()> {
        let authority=crate::tree_container::VerifiedWorktreeRead::from_files_stage(action_id.into(),account_id.into(),version_id.into(),file);
        self.read_worktree_tree_to_stage(&authority).await
    }

    /// Persist every resource's after-state after a committed multi-resource
    /// mutation. Each content receipt is written before the catalog advances
    /// to `AfterDurable`; a retry therefore reuses the durable phase instead
    /// of replaying the live owner.
    pub async fn capture_after_batch(
        &self,
        action_id: &str,
        entries: Vec<BatchAfterInput>,
    ) -> CatalogResult<ActionRecord> {
        if entries.is_empty() {
            return Err("after-state batch cannot be empty".into());
        }
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        if crate::tree_container::action_marks_tree(&action) {return Err("worktree tree capture requires Source owner".into());}
        if !matches!(
            action.live.as_ref().map(|receipt| &receipt.status),
            Some(LiveStatus::Committed)
        ) {
            return Err(CatalogConflict::InvalidTransition.into());
        }
        let declared = action
            .mutation_batch
            .as_ref()
            .map(|batch| {
                batch
                    .resources
                    .iter()
                    .map(|resource| resource.resource_key.clone())
                    .collect::<BTreeSet<_>>()
            })
            .unwrap_or_else(|| {
                action
                    .modified_resource_ids
                    .iter()
                    .cloned()
                    .collect::<BTreeSet<_>>()
            });
        let supplied = entries
            .iter()
            .map(|entry| entry.resource_key.clone())
            .collect::<BTreeSet<_>>();
        if supplied != declared || supplied.len() != entries.len() {
            return Err("after-state batch does not match prepared resources".into());
        }
        if let Some(live_fingerprint) = action.live.as_ref().and_then(|live| live.fingerprint.as_deref()) {
            let primary = entries
                .iter()
                .find(|entry| entry.resource_key == action.resource_key)
                .ok_or("after-state batch is missing the primary resource")?;
            if primary.fingerprint != live_fingerprint {
                return Err("primary live fingerprint does not match submitted after-state".into());
            }
        }
        let prepared = action
            .mutation_batch
            .as_ref()
            .map(|batch| {
                batch
                    .resources
                    .iter()
                    .map(|resource| (&resource.resource_key, resource))
                    .collect::<BTreeMap<_, _>>()
            });
        for entry in &entries {
            if entry.outcome.resource_id != entry.resource_key.resource_id
                || entry.outcome.status != "Committed"
                || entry.outcome.revision.is_none()
            {
                return Err(format!(
                    "after-state outcome is not an authoritative committed result for {}",
                    entry.resource_key.resource_id
                )
                .into());
            }
            if entry.existence == ResourceExistence::Present && entry.content.is_none() {
                return Err(format!(
                    "after-state present resource {} has no exact content",
                    entry.resource_key.resource_id
                )
                .into());
            }
            let expected_fingerprint = entry
                .content
                .as_ref()
                .map(|content| format!("sha256:{:x}:{}", Sha256::digest(content), content.len()))
                .unwrap_or_else(|| "missing".into());
            if entry.fingerprint != expected_fingerprint {
                return Err(format!(
                    "after-state fingerprint does not match exact payload for {}",
                    entry.resource_key.resource_id
                )
                .into());
            }
            let outcome_revision = entry.outcome.revision.as_ref().ok_or_else(|| {
                format!("after-state outcome is missing a revision for {}", entry.resource_key.resource_id)
            })?;
            let revision_matches = match outcome_revision {
                Revision::Opaque { value, .. } if value == &entry.fingerprint => true,
                Revision::Opaque { value, .. } => prepared
                    .as_ref()
                    .and_then(|resources| resources.get(&entry.resource_key))
                    .and_then(|resource| resource.expected_revision.as_ref())
                    .is_some_and(|expected| matches!(expected, Revision::Opaque { value: expected_value, .. } if expected_value == value)),
            };
            if !revision_matches {
                return Err(format!(
                    "after-state outcome revision is not bound to the payload or expected revision for {}",
                    entry.resource_key.resource_id
                )
                .into());
            }
            if entry.existence == ResourceExistence::Present {
                match entry.resource_type {
                    ResourceType::File | ResourceType::Symlink => {
                        if entry.metadata.size != entry.content.as_ref().map(|content| content.len() as u64) {
                            return Err(format!(
                                "after-state file metadata size is invalid for {}",
                                entry.resource_key.resource_id
                            ).into());
                        }
                    }
                    ResourceType::Directory => {
                        if entry.metadata.size.is_some() {
                            return Err(format!(
                                "after-state directory metadata size is not a file size for {}",
                                entry.resource_key.resource_id
                            ).into());
                        }
                    }
                    ResourceType::Other => {
                        return Err(format!(
                            "after-state resource type is unsupported for {}",
                            entry.resource_key.resource_id
                        ).into());
                    }
                }
            }
            if let Some(content_digest) = entry.coverage.content_digest.as_deref() {
                let content = entry.content.as_ref().ok_or_else(|| {
                    format!(
                        "after-state content digest has no content for {}",
                        entry.resource_key.resource_id
                    )
                })?;
                let blake3_digest = blake3::hash(content).to_hex().to_string();
                let sha256_digest = format!("sha256:{:x}", Sha256::digest(content));
                let sized_sha256_digest = format!("{sha256_digest}:{}", content.len());
                if content_digest != blake3_digest
                    && content_digest != sha256_digest
                    && content_digest != sized_sha256_digest
                {
                    return Err(format!(
                        "after-state content digest mismatch for {}",
                        entry.resource_key.resource_id
                    )
                    .into());
                }
            }
            if let Some(size) = entry.metadata.size {
                if entry.existence == ResourceExistence::Present
                    && matches!(entry.resource_type, ResourceType::File | ResourceType::Symlink)
                    && entry.content.as_ref().map(|content| content.len() as u64) != Some(size)
                {
                    return Err(format!(
                        "after-state metadata size mismatch for {}",
                        entry.resource_key.resource_id
                    )
                    .into());
                }
            }
            let exact_after = entry
                .coverage
                .metadata
                .as_ref()
                .and_then(|metadata| metadata.get("exact_after"))
                == Some(&serde_json::Value::Bool(true));
            if !exact_after {
                return Err(format!(
                    "after-state coverage is not exact for {}",
                    entry.resource_key.resource_id
                )
                .into());
            }
            if let Some(prepared) = prepared.as_ref().and_then(|resources| resources.get(&entry.resource_key)) {
                if prepared.existence == ResourceExistence::Present
                    && entry.existence == ResourceExistence::Present
                    && prepared.resource_type != entry.resource_type
                {
                    return Err(format!(
                        "after-state type changed unexpectedly for {}",
                        entry.resource_key.resource_id
                    )
                    .into());
                }
                if prepared.new_locator.is_some()
                    && entry.locator.as_ref() != prepared.new_locator.as_ref()
                {
                    return Err(format!(
                        "after-state locator does not match prepared destination for {}",
                        entry.resource_key.resource_id
                    )
                    .into());
                }
                if entry.existence == ResourceExistence::Present
                    && prepared.metadata.mode.is_some()
                    && entry.metadata.mode != prepared.metadata.mode
                {
                    return Err(format!(
                        "after-state mode changed unexpectedly for {}",
                        entry.resource_key.resource_id
                    )
                    .into());
                }
            }
        }
        if action.state == ActionState::AfterDurable {
            let stored_outcomes = action.per_resource_outcomes.as_ref().ok_or(
                "durable after-state is missing per-resource outcomes",
            )?;
            if action.after_resources.len() != entries.len() || stored_outcomes.len() != entries.len() {
                return Err("durable after-state coverage does not match retry resources".into());
            }
            for ((entry, stored), outcome) in entries
                .iter()
                .zip(action.after_resources.iter())
                .zip(stored_outcomes.iter())
            {
                if entry.resource_key != stored.resource_key
                    || entry.locator != stored.locator
                    || entry.existence != stored.existence
                    || entry.resource_type != stored.resource_type
                    || entry.metadata != stored.metadata
                    || entry.fingerprint != stored.fingerprint
                    || entry.coverage != stored.coverage
                    || &entry.outcome != outcome
                {
                    return Err("durable after-state retry payload mismatch".into());
                }
            }
            return Ok(action);
        }
        let start_state = if action.state == ActionState::AfterCaptureFailed {
            ActionState::AfterCaptureFailed
        } else {
            ActionState::Applied
        };
        let lease = lease_ids(&action);
        self.catalog.acquire_leases(action_id, &lease)?;
        if let Err(error) = self
            .catalog
            .transition(action_id, start_state, ActionState::CapturingAfter)
        {
            self.catalog.release_leases(action_id, &lease)?;
            return Err(error);
        }
        self.held_leases
            .lock()
            .map_err(|_| "lease lock poisoned")?
            .insert(action_id.to_owned(), lease.clone());
        let logical_bytes = match entries.iter().try_fold(0u64, |sum, entry| {
            sum.checked_add(entry.content.as_ref().map_or(0, |bytes| bytes.len() as u64))
                .ok_or("after-state byte size overflow")
        }) {
            Ok(bytes) => bytes,
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingAfter,
                    ActionState::AfterCaptureFailed,
                );
                self.release_after_failure(action_id, &lease);
                return Err(error.into());
            }
        };
        let (reservation_id, scope_id) = match self
            .reserve_with_reclaim(&action, "after", logical_bytes)
            .await
        {
            Ok(value) => value,
            Err(error) => {
                let _ = self.catalog.transition(
                    action_id,
                    ActionState::CapturingAfter,
                    ActionState::AfterCaptureFailed,
                );
                self.release_after_failure(action_id, &lease);
                return Err(format!("history_paused_budget: {error}").into());
            }
        };
        let mut after_resources = Vec::with_capacity(entries.len());
        let mut outcomes = Vec::with_capacity(entries.len());
        for (index, entry) in entries.into_iter().enumerate() {
            let receipt = match self
                .capture(
                    action_id,
                    &action.resource_key.account_id,
                    entry.content,
                    entry.fingerprint.clone(),
                    &format!("batch-after-{index}-{}", entry.resource_key.resource_id),
                )
                .await
            {
                Ok(receipt) => receipt,
                Err(error) => {
                    let _ = self.catalog.transition(
                        action_id,
                        ActionState::CapturingAfter,
                        ActionState::AfterCaptureFailed,
                    );
                    self.release_after_failure(action_id, &lease);
                    let _ = self.release_capture_reservation(&reservation_id);
                    return Err(format!("history_after_failed: {error}").into());
                }
            };
            outcomes.push(entry.outcome);
            after_resources.push(AfterResource {
                resource_key: entry.resource_key,
                locator: entry.locator,
                existence: entry.existence,
                resource_type: entry.resource_type,
                metadata: entry.metadata,
                after: receipt,
                fingerprint: entry.fingerprint,
                coverage: entry.coverage,
            });
        }
        let digest = batch_after_capture_digest(&after_resources, &outcomes);
        let result = self.catalog.record_after_batch(
            action_id,
            after_resources,
            outcomes,
            digest,
            scope_id,
            logical_bytes,
        );
        let _ = self.release_capture_reservation(&reservation_id);
        if result.is_err() {
            self.release_after_failure(action_id, &lease);
        }
        result
    }

    pub fn complete(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        let result = self.catalog.complete(action_id);
        if result.is_ok() {
            if let Some(lease) = self
                .held_leases
                .lock()
                .map_err(|_| "lease lock poisoned")?
                .remove(action_id)
            {
                self.catalog.release_leases(action_id, &lease)?;
            }
        }
        result
    }

    pub fn abort(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        let result = self.catalog.abort(action_id);
        if result.is_ok() {
            if let Some(lease) = self
                .held_leases
                .lock()
                .map_err(|_| "lease lock poisoned")?
                .remove(action_id)
            {
                self.catalog.release_leases(action_id, &lease)?;
            }
        }
        result
    }

    pub async fn shutdown_checked(&self) -> CatalogResult<()> {
        if !self.catalog.active_actions()?.is_empty() {
            return Err("active actions prevent checked shutdown".into());
        }
        self.store.flush_checked().await?;
        Ok(())
    }

    /// The Files service invokes this only after authenticating its root-bound
    /// role and resolving both immutable actions to the same registered resource.
    /// The passed file must be the service-owned pinned stage, never a request path.
    #[cfg(target_os="linux")]
    #[doc(hidden)]
    pub async fn prepare_files_worktree_inverse(
        &self, original_id: &str, version_id: &str, inverse_id: &str, file: std::fs::File,
    ) -> CatalogResult<ActionRecord> {
        self.prepare_worktree_inverse_before(original_id, version_id, inverse_id, &file).await
    }

    /// Retain the exact original tree in a separately owned inverse version.
    /// This is private to the authenticated Files service: `stage` must be its
    /// newly created, pinned operation-owned file, never a caller path.
    #[cfg(target_os="linux")]
    pub(crate) async fn prepare_worktree_inverse_before(
        &self, original_id: &str, version_id: &str, inverse_id: &str, stage: &std::fs::File,
    ) -> CatalogResult<ActionRecord> {
        use crate::tree_container::{JournalAfterCommitment, Summary};
        let original = self.catalog.get_action(original_id)?.ok_or("worktree unavailable")?;
        let inverse = self.catalog.get_action(inverse_id)?.ok_or("worktree unavailable")?;
        let provenance = inverse.coverage.as_ref().and_then(|v| v.metadata.as_ref())
            .and_then(|v| v.get("source_worktree_tree_v2")).ok_or("worktree provenance unavailable")?;
        if original_id == inverse_id || original.operation != "source_worktree_create_v2"
            || inverse.operation != "source_worktree_inverse_v2" || original.state != ActionState::Complete
            || inverse.state != ActionState::Intent || original.resource_key != inverse.resource_key
            || original.actor_account_id != inverse.actor_account_id || inverse.actor_kind != "human"
            || !crate::tree_container::action_marks_tree(&original) || !crate::tree_container::action_marks_tree(&inverse)
            || !provenance.get("journal_operation_id").and_then(serde_json::Value::as_str).is_some_and(|v|(16..=80).contains(&v.len()) && v.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'-' || b==b'_'))
            || provenance.get("original_action_id").and_then(serde_json::Value::as_str) != Some(original_id)
            || provenance.get("original_version_id").and_then(serde_json::Value::as_str) != Some(version_id)
            || original.after_resources.len() != 1 { return Err("worktree unavailable".into()); }
        let after = &original.after_resources[0];
        if after.resource_key != original.resource_key || after.after.version_id != version_id
            || after.existence != ResourceExistence::Present || after.resource_type != ResourceType::Directory
            || original.after.as_ref() != Some(&after.after) { return Err("worktree version unavailable".into()); }
        let original_provenance: crate::tree_container::ActionCommitment = serde_json::from_value(original.coverage.as_ref()
            .and_then(|v| v.metadata.as_ref()).and_then(|v| v.get("source_worktree_tree_v2"))
            .cloned().ok_or("worktree provenance unavailable")?)?;
        let proof: JournalAfterCommitment = serde_json::from_value(after.coverage.metadata.as_ref()
            .and_then(|m| m.get("source_worktree_tree_v2_after")).cloned().ok_or("worktree proof unavailable")?)?;
        if proof.journal_operation_id != original_provenance.journal_operation_id
            || provenance.get("original_journal_operation_id").and_then(serde_json::Value::as_str) != Some(proof.journal_operation_id.as_str())
            || provenance.get("original_container_sha256").and_then(serde_json::Value::as_str) != Some(proof.container_sha256.as_str())
            || provenance.get("original_container_length").and_then(serde_json::Value::as_u64) != Some(proof.container_length) {
            return Err("worktree provenance changed".into());
        }
        let expected = Summary { entries: proof.entry_count, bytes: proof.container_length, container_sha256: proof.container_sha256 };
        let fingerprint = format!("sha256:{}:{}", expected.container_sha256, expected.bytes);
        if after.fingerprint != fingerprint || after.after.fingerprint != fingerprint
            || after.coverage.byte_len != Some(expected.bytes) || after.coverage.content_digest.as_deref() != Some(fingerprint.as_str()) {
            return Err("worktree proof changed".into());
        }
        let VersionContent::Bytes(reference) = &after.after.content else { return Err("worktree payload unavailable".into()) };
        let address = lore_storage::Address { context: Context::from(reference.context), hash: reference.hash_hex.parse()? };
        let inverse_leases = lease_ids(&inverse);
        let mut all_leases = inverse_leases.clone(); all_leases.extend(lease_ids(&original)); all_leases.sort(); all_leases.dedup();
        self.catalog.acquire_leases(inverse_id, &all_leases)?;
        let mut transitioned = false;
        let mut reservation = None;
        let result: CatalogResult<ActionRecord> = async {
            if self.catalog.expiry(original_id)?.is_some() || self.catalog.get_action(original_id)?.as_ref() != Some(&original)
                || self.catalog.get_action(inverse_id)?.as_ref() != Some(&inverse) { return Err("worktree action changed".into()); }
            self.catalog.transition(inverse_id, ActionState::Intent, ActionState::CapturingBefore)?;
            transitioned = true;
            let (reservation_id, scope) = self.reserve_with_reclaim(&inverse, "before", expected.bytes).await
                .map_err(|e| format!("history_paused_budget: {e}"))?;
            reservation = Some(reservation_id);
            let partition = Partition::from(self.catalog.owner_partition(&inverse.resource_key.account_id)?);
            self.store.read_tree_to_pinned_stage(partition, address, &expected, stage).await?;
            // Use the same framed batch digest as ordinary History capture,
            // but hash content from the pinned stage in bounded chunks.
            use std::os::unix::fs::FileExt;
            let mut content_hash = blake3::Hasher::new();
            let mut offset = 0u64; let mut bytes = vec![0u8; 128 * 1024];
            while offset < expected.bytes {
                let wanted = ((expected.bytes - offset).min(bytes.len() as u64)) as usize;
                let count = stage.read_at(&mut bytes[..wanted], offset)?;
                if count == 0 { return Err("worktree stage truncated".into()); }
                content_hash.update(&bytes[..count]); offset += count as u64;
            }
            let resource = MutationResource { resource_key: inverse.resource_key.clone(), old_locator: after.locator.clone(),
                new_locator: inverse.destination_locator.clone(), expected_revision: inverse.expected_revision.clone(),
                existence: ResourceExistence::Present, resource_type: ResourceType::Directory, metadata: after.metadata.clone(),
                before: None, fingerprint: fingerprint.clone(), coverage: after.coverage.clone() };
            let metadata = serde_json::to_vec(&(0usize, &resource.resource_key, &resource.old_locator, &resource.new_locator,
                &resource.expected_revision, &resource.existence, &resource.resource_type, &resource.metadata, &resource.coverage, &resource.fingerprint))?;
            let mut framed = b"openclank-mutation-batch-digest-v1".to_vec();
            append_digest_frame(&mut framed, b"entry", &metadata);
            let mut content_frame = Vec::with_capacity(40); content_frame.extend_from_slice(&expected.bytes.to_be_bytes());
            content_frame.extend_from_slice(content_hash.finalize().as_bytes()); append_digest_frame(&mut framed, b"content", &content_frame);
            let capture_digest = blake3::hash(&framed).to_hex().to_string();
            let version = unique_version_id(inverse_id, "source-worktree-inverse-before");
            let context = unique_context(inverse_id, &version);
            let (written, actual) = self.store.write_tree_pinned(partition, Context::from(context), stage).await?;
            if actual != expected || self.catalog.expiry(original_id)?.is_some()
                || self.catalog.get_action(original_id)?.as_ref() != Some(&original) { return Err("worktree source changed".into()); }
            let receipt = VersionReceipt { version_id: version, content: VersionContent::Bytes(crate::catalog::LoreRef {
                context, hash_hex: written.hash.to_string() }), fingerprint: fingerprint.clone() };
            let mut resource = resource; resource.before = Some(receipt);
            let batch = MutationBatch { schema_version: 1, parent_action_id: inverse_id.to_owned(),
                prepare: PrepareReceipt { batch_version: 1, parent_action_id: inverse_id.to_owned(), resource_count: 1,
                    logical_bytes: expected.bytes, acknowledged: true }, resources: vec![resource] };
            self.catalog.record_batch_before_durable(inverse_id, batch, capture_digest, scope, expected.bytes)
        }.await;
        if let Some(id) = reservation { let _ = self.release_capture_reservation(&id); }
        match result {
            Ok(record) => {
                let original_only: Vec<_> = all_leases.iter().filter(|id| !inverse_leases.contains(id)).cloned().collect();
                self.catalog.release_leases(inverse_id, &original_only)?;
                self.held_leases.lock().map_err(|_| "lease lock poisoned")?.insert(inverse_id.to_owned(), inverse_leases);
                Ok(record)
            }
            Err(error) => {
                if transitioned { let _ = self.catalog.transition(inverse_id, ActionState::CapturingBefore, ActionState::CaptureFailed); }
                self.catalog.release_leases(inverse_id, &all_leases)?;
                Err(error)
            }
        }
    }

    /// Retain the exact inverse version and reserve this Redo action's after
    /// capture before Source or Files can publish either guarded rename.
    /// The caller owns the pinned service stage and current Files root role.
    #[cfg(target_os="linux")]
    #[doc(hidden)]
    pub async fn prepare_files_worktree_redo_after(&self, redo_id:&str, inverse_id:&str,
        version_id:&str, stage:std::fs::File) -> CatalogResult<(String,String)> {
        self.prepare_worktree_redo_after(redo_id,inverse_id,version_id,&stage).await
    }

    #[cfg(target_os="linux")]
    pub(crate) async fn prepare_worktree_redo_after(&self, redo_id:&str, inverse_id:&str,
        version_id:&str, stage:&std::fs::File) -> CatalogResult<(String,String)> {
        use crate::tree_container::{JournalAfterCommitment,Summary};
        let redo=self.catalog.get_action(redo_id)?.ok_or("worktree redo unavailable")?;
        let inverse=self.catalog.get_action(inverse_id)?.ok_or("worktree inverse unavailable")?;
        let marker=redo.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.get("source_worktree_redo_v2")).ok_or("worktree redo unavailable")?;
        if redo_id==inverse_id || redo.operation!="source_worktree_redo_v2" || redo.state!=ActionState::BeforeDurable
            || redo.actor_kind!="human" || redo.actor_account_id!=redo.resource_key.account_id
            || inverse.operation!="source_worktree_inverse_v2" || inverse.state!=ActionState::Complete
            || inverse.resource_key!=redo.resource_key || inverse.actor_account_id!=redo.actor_account_id
            || inverse.before_resources.len()!=1 || redo.before_resources.len()!=1
            || redo.before_resources[0].existence!=ResourceExistence::Absent || redo.before_resources[0].resource_type!=ResourceType::Directory
            || !redo.before_resources[0].before.as_ref().is_some_and(|v|v.content==VersionContent::Tombstone)
            || marker.get("inverse_action_id").and_then(serde_json::Value::as_str)!=Some(inverse_id)
            || marker.get("inverse_version_id").and_then(serde_json::Value::as_str)!=Some(version_id) {
            return Err("worktree redo unavailable".into());
        }
        let before=&inverse.before_resources[0];let receipt=before.before.as_ref().ok_or("worktree inverse unavailable")?;
        if before.resource_key!=inverse.resource_key || inverse.before.as_ref()!=Some(receipt) || receipt.version_id!=version_id {
            return Err("worktree inverse changed".into());
        }
        let proof:JournalAfterCommitment=serde_json::from_value(before.coverage.metadata.as_ref()
            .and_then(|m|m.get("source_worktree_tree_v2_after")).cloned().ok_or("worktree inverse proof unavailable")?)?;
        let summary=Summary{entries:proof.entry_count,bytes:proof.container_length,container_sha256:proof.container_sha256};
        let fingerprint=format!("sha256:{}:{}",summary.container_sha256,summary.bytes);
        if receipt.fingerprint!=fingerprint || before.fingerprint!=fingerprint || before.coverage.byte_len!=Some(summary.bytes)
            || before.coverage.content_digest.as_deref()!=Some(fingerprint.as_str())
            || marker.get("container_length").and_then(serde_json::Value::as_u64)!=Some(summary.bytes)
            || marker.get("container_sha256").and_then(serde_json::Value::as_str)!=Some(summary.container_sha256.as_str()) {
            return Err("worktree inverse proof changed".into());
        }
        let VersionContent::Bytes(reference)=&receipt.content else {return Err("worktree inverse unavailable".into())};
        let address=lore_storage::Address{context:Context::from(reference.context),hash:reference.hash_hex.parse()?};
        let redo_leases=lease_ids(&redo);let mut all=redo_leases.clone();all.extend(lease_ids(&inverse));all.sort();all.dedup();
        self.catalog.acquire_leases(redo_id,&all)?;
        let inverse_only:Vec<_>=all.iter().filter(|key|!redo_leases.contains(key)).cloned().collect();
        let mut reserved=None;
        let result:CatalogResult<(String,String)>=async {
            if self.catalog.expiry(inverse_id)?.is_some() || self.catalog.get_action(inverse_id)?.as_ref()!=Some(&inverse)
                || self.catalog.get_action(redo_id)?.as_ref()!=Some(&redo) {return Err("worktree redo changed".into())}
            let (id,scope)=self.reserve_with_reclaim(&redo,"after",summary.bytes).await.map_err(|e|format!("history_paused_budget: {e}"))?;
            reserved=Some(id.clone());let revision=self.catalog.policy_set()?.revision;
            let partition=Partition::from(self.catalog.owner_partition(&redo.resource_key.account_id)?);
            self.store.read_tree_to_pinned_stage(partition,address,&summary,stage).await?;
            let (manifest,actual)=crate::tree_container::validate_pinned(stage)?;
            let tree:crate::tree_container::ActionCommitment=serde_json::from_value(redo.coverage.as_ref().and_then(|v|v.metadata.as_ref())
                .and_then(|v|v.get("source_worktree_tree_v2")).cloned().ok_or("worktree redo provenance unavailable")?)?;
            if actual!=summary || tree.version!=2 || tree.allowed_after_format!="indexed_tree_v2"
                || marker.get("journal_operation_id").and_then(serde_json::Value::as_str)!=Some(tree.journal_operation_id.as_str())
                || manifest.source.commit_oid!=tree.commit_oid || manifest.source.tree_oid!=tree.tree_oid
                || manifest.source.target_manifest_sha256!=tree.target_manifest_sha256 {return Err("worktree redo provenance changed".into())}
            self.catalog.checked_worktree_redo_reservation(redo_id,inverse_id,version_id,summary.bytes,&scope,revision)?;
            if self.catalog.get_action(inverse_id)?.as_ref()!=Some(&inverse) || self.catalog.get_action(redo_id)?.as_ref()!=Some(&redo) {
                return Err("worktree redo changed".into());
            }
            Ok((id,scope))
        }.await;
        match result {
            Ok(value)=>{self.held_leases.lock().map_err(|_|"lease lock poisoned")?.insert(redo_id.to_owned(),all);Ok(value)}
            Err(error)=>{if let Some(id)=reserved {let _=self.release_capture_reservation(&id);}self.catalog.release_leases(redo_id,&inverse_only)?;Err(error)}
        }
    }

    /// Recheck the retained Redo reservation and both action leases before
    /// each chunk of the original service-owned pinned stage.
    #[cfg(target_os="linux")]
    #[doc(hidden)]
    pub fn validate_files_worktree_redo_read(&self,action_id:&str,version:&str,length:u64,fingerprint:&str)->CatalogResult<()> {
        let action=self.catalog.get_action(action_id)?.ok_or("worktree redo unavailable")?;
        let marker=action.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.get("source_worktree_redo_v2")).ok_or("worktree redo unavailable")?;
        let inverse_id=marker.get("inverse_action_id").and_then(serde_json::Value::as_str).ok_or("worktree redo unavailable")?;
        let sha=marker.get("container_sha256").and_then(serde_json::Value::as_str).ok_or("worktree redo unavailable")?;
        if fingerprint!=format!("sha256:{}:{}",sha,length) {return Err("worktree redo unavailable".into())}
        let inverse=self.catalog.get_action(inverse_id)?.ok_or("worktree redo unavailable")?;
        let held=self.held_leases.lock().map_err(|_|"lease lock poisoned")?.get(action_id).cloned().ok_or("worktree redo unavailable")?;
        if lease_ids(&action).iter().chain(lease_ids(&inverse).iter()).any(|key|!held.contains(key)) {return Err("worktree redo unavailable".into())}
        let policy=self.catalog.policy_set()?;let locator=action.destination_locator.as_ref().or(action.original_locator.as_ref()).map(|v|std::path::Path::new(v.location_label.as_str()));
        let effective=policy.effective_for_owner(Some(action.actor_account_id.as_str()),&action.resource_key.workspace_id,locator);
        self.catalog.checked_worktree_redo_reservation(action_id,inverse_id,version,length,&effective.scope_id,policy.revision)?;Ok(())
    }

    /// Read only through the retained Source worktree authority; never expose a
    /// raw Git marker through the generic Files restore/read interface.
    #[cfg(target_os="linux")]
    pub(crate) async fn read_worktree_tree_to_stage(&self, authority: &crate::tree_container::VerifiedWorktreeRead) -> CatalogResult<()> {
        use crate::tree_container::{JournalAfterCommitment, Summary};
        let action=self.catalog.get_action(authority.action_id())?.ok_or("unknown action")?;
        if !crate::tree_container::action_marks_tree(&action) || action.actor_account_id!=authority.account_id()
            || action.resource_key.account_id!=authority.account_id() {return Err("worktree read unavailable".into());}
        let inverse=action.operation=="source_worktree_inverse_v2";
        let (receipt,coverage,fingerprint)=if inverse {
            if !matches!(&action.state,ActionState::BeforeDurable|ActionState::Applying|ActionState::Applied|ActionState::CapturingAfter|ActionState::AfterDurable|ActionState::Complete)
                || action.before_resources.len()!=1 {return Err("worktree read unavailable".into());}
            let before=&action.before_resources[0];let receipt=before.before.as_ref().ok_or("worktree version unavailable")?;
            if before.resource_key!=action.resource_key || action.before.as_ref()!=Some(receipt) {return Err("worktree version unavailable".into());}
            (receipt,&before.coverage,&before.fingerprint)
        } else {
            if action.operation!="source_worktree_create_v2" || action.state!=ActionState::Complete || action.after_resources.len()!=1 {return Err("worktree read unavailable".into());}
            let after=&action.after_resources[0];
            if after.resource_key!=action.resource_key || action.after.as_ref()!=Some(&after.after) {return Err("worktree version unavailable".into());}
            (&after.after,&after.coverage,&after.fingerprint)
        };
        if receipt.version_id!=authority.version_id() {return Err("worktree version unavailable".into());}
        let proof:JournalAfterCommitment=serde_json::from_value(coverage.metadata.as_ref()
            .and_then(|m|m.get("source_worktree_tree_v2_after")).cloned().ok_or("worktree proof unavailable")?)?;
        let expected=Summary{entries:proof.entry_count,bytes:proof.container_length,container_sha256:proof.container_sha256};
        let expected_fingerprint=format!("sha256:{}:{}",expected.container_sha256,expected.bytes);
        if fingerprint!=&expected_fingerprint || receipt.fingerprint!=expected_fingerprint
            || coverage.byte_len!=Some(expected.bytes) || coverage.content_digest.as_deref()!=Some(expected_fingerprint.as_str()) {
            return Err("worktree proof changed".into());
        }
        let VersionContent::Bytes(reference)=&receipt.content else {return Err("worktree payload unavailable".into())};
        let address=lore_storage::Address{context:Context::from(reference.context),hash:reference.hash_hex.parse()?};
        let lease=lease_ids(&action);
        // The Files service holds its coordinator mutex through this complete
        // copy. An active inverse already owns these keys until completion; do
        // not drop them or try to acquire them under a competing read owner.
        let active_inverse=inverse && action.state!=ActionState::Complete;
        let lease_owner=if active_inverse {
            if !self.held_leases.lock().map_err(|_|"lease lock poisoned")?.contains_key(&action.action_id) {return Err("worktree lease unavailable".into());}
            None
        } else {let owner=format!("worktree-read-{}",unique_version_id(authority.action_id(),"read"));self.catalog.acquire_leases(&owner,&lease)?;Some(owner)};
        let result=async {
            if self.catalog.expiry(authority.action_id())?.is_some() {return Err("history payload expired or expiring".into());}
            if self.catalog.get_action(authority.action_id())?.as_ref()!=Some(&action) {return Err("worktree action changed".into());}
            let partition=Partition::from(self.catalog.owner_partition(authority.account_id())?);
            self.store.read_tree_to_pinned_stage(partition,address,&expected,authority.file()).await?;
            Ok(())
        }.await;
        if let Some(owner)=lease_owner {self.catalog.release_leases(&owner,&lease)?;}
        result
    }

    pub async fn read_version(
        &self,
        action_id: &str,
        receipt: &VersionReceipt,
    ) -> CatalogResult<Option<Bytes>> {
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        if action.recovery_settled || self.catalog.expiry(action_id)?.is_some() {
            return Err("version expired".into());
        }
        if !action
            .before
            .as_ref()
            .is_some_and(|stored| stored == receipt)
            && !action
                .before_resources
                .iter()
                .any(|resource| resource.before.as_ref() == Some(receipt))
            && !action
                .mutation_batch
                .as_ref()
                .is_some_and(|batch| {
                    batch
                        .resources
                        .iter()
                        .any(|resource| resource.before.as_ref() == Some(receipt))
                })
            && !action
                .after
                .as_ref()
                .is_some_and(|stored| stored == receipt)
            && !action
                .after_resources
                .iter()
                .any(|resource| resource.after == *receipt)
        {
            return Err("version receipt is not recorded for action".into());
        }
        if crate::tree_container::action_marks_tree(&action) {
            return Err("worktree tree read requires Source owner".into());
        }
        if self.catalog.expiry(action_id)?.is_some() {
            return Err("history payload expired or expiring".into());
        }
        let VersionContent::Bytes(reference) = &receipt.content else {
            return Ok((receipt.content == VersionContent::Empty).then(Bytes::new));
        };
        let hash = reference.hash_hex.parse()?;
        let address = lore_storage::Address {
            context: Context::from(reference.context),
            hash,
        };
        Ok(Some(
            self.store
                .read(
                    Partition::from(
                        self.catalog
                            .owner_partition(&action.resource_key.account_id)?,
                    ),
                    address,
                )
                .await?,
        ))
    }

    async fn capture(
        &self,
        action_id: &str,
        account_id: &str,
        content: Option<Bytes>,
        fingerprint: String,
        identity: &str,
    ) -> CatalogResult<VersionReceipt> {
        let partition = self.catalog.ensure_owner_partition(account_id)?;
        let phase = if content.is_some() {
            format!("bytes:{identity}")
        } else {
            format!("tombstone:{identity}")
        };
        let version_id = unique_version_id(action_id, &phase);
        let context = unique_context(action_id, &version_id);
        let result = async {
            let Some(content) = content else {
                return Ok(VersionReceipt {
                    version_id,
                    content: VersionContent::Tombstone,
                    fingerprint,
                });
            };
            if content.is_empty() {
                return Ok(VersionReceipt {
                    version_id,
                    content: VersionContent::Empty,
                    fingerprint,
                });
            }
            let address = self
                .store
                .write(Partition::from(partition), Context::from(context), content)
                .await?;
            self.store.flush_checked().await?;
            #[cfg(feature = "test_faults")]
            if take_capture_fault(CaptureFault::AfterFlush) {
                return Err("injected failure after Lore flush before catalog publication".into());
            }
            #[cfg(feature = "test_faults")]
            qualification_abort("after_lore_flush");
            Ok(VersionReceipt {
                version_id,
                content: VersionContent::Bytes(crate::catalog::LoreRef {
                    context,
                    hash_hex: address.hash.to_string(),
                }),
                fingerprint,
            })
        }
        .await;
        result
    }
}

fn lease_ids(action: &ActionRecord) -> Vec<String> {
    let mut ids = action
        .guard_resource_ids
        .iter()
        .chain(action.modified_resource_ids.iter())
        .map(ResourceKey::scoped_lease_id)
        .collect::<Vec<_>>();
    ids.push(action.resource_key.scoped_lease_id());
    ids.push(format!("history-action:{}", action.action_id));
    ids.extend(
        action
            .physical_lease_keys
            .iter()
            .map(|key| format!("physical:{key}")),
    );
    ids.sort();
    ids.dedup();
    ids
}

fn normalized_digest(request: &ActionRequest) -> String {
    let bytes = serde_json::to_vec(request).expect("action request is serializable");
    blake3::hash(&bytes).to_hex().to_string()
}

pub fn capture_input_digest(content: Option<&[u8]>, fingerprint: &str) -> String {
    let mut hasher = blake3::Hasher::new();
    hasher.update(fingerprint.as_bytes());
    hasher.update(&[0]);
    if let Some(bytes) = content {
        hasher.update(bytes);
    }
    hasher.finalize().to_hex().to_string()
}

pub fn batch_capture_digest(entries: &[BatchCaptureInput]) -> String {
    let mut input = Vec::new();
    input.extend_from_slice(b"openclank-mutation-batch-digest-v1");
    for (index, entry) in entries.iter().enumerate() {
        let metadata = serde_json::to_vec(&(
            index,
            &entry.resource_key,
            &entry.old_locator,
            &entry.new_locator,
            &entry.expected_revision,
            &entry.existence,
            &entry.resource_type,
            &entry.metadata,
            &entry.coverage,
            &entry.fingerprint,
        ))
        .expect("batch capture metadata is serializable");
        append_digest_frame(&mut input, b"entry", &metadata);
        if let Some(content) = entry.content.as_ref() {
            let mut content_frame = Vec::with_capacity(8 + 32);
            content_frame.extend_from_slice(&(content.len() as u64).to_be_bytes());
            content_frame.extend_from_slice(blake3::hash(content).as_bytes());
            append_digest_frame(&mut input, b"content", &content_frame);
        } else {
            append_digest_frame(&mut input, b"content-absent", &[]);
        }
    }
    blake3::hash(&input).to_hex().to_string()
}

fn batch_after_capture_digest(entries: &[AfterResource], outcomes: &[ResourceOutcome]) -> String {
    let mut input = Vec::new();
    input.extend_from_slice(b"openclank-mutation-after-batch-digest-v1");
    for (index, (entry, outcome)) in entries.iter().zip(outcomes.iter()).enumerate() {
        let metadata = serde_json::to_vec(&(
            index,
            &entry.resource_key,
            &entry.locator,
            &entry.existence,
            &entry.resource_type,
            &entry.metadata,
            &entry.coverage,
            &entry.fingerprint,
            &entry.after.version_id,
            outcome,
        ))
        .expect("batch after metadata is serializable");
        append_digest_frame(&mut input, b"entry", &metadata);
        match &entry.after.content {
            VersionContent::Bytes(reference) => {
                append_digest_frame(&mut input, b"content-ref", reference.hash_hex.as_bytes())
            }
            VersionContent::Empty => append_digest_frame(&mut input, b"empty", &[]),
            VersionContent::Tombstone => append_digest_frame(&mut input, b"tombstone", &[]),
        }
    }
    blake3::hash(&input).to_hex().to_string()
}

fn append_digest_frame(input: &mut Vec<u8>, domain: &[u8], payload: &[u8]) {
    input.extend_from_slice(&(domain.len() as u64).to_be_bytes());
    input.extend_from_slice(domain);
    input.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    input.extend_from_slice(payload);
}

pub fn request_digest(request: &ActionRequest) -> String {
    normalized_digest(request)
}


#[cfg(test)]
mod workflow_writer_projection_test {
    use super::*;
    #[test]
    fn same_action_writer_is_closed_and_display_only() {
        let key=ResourceKey{account_id:"account".into(),workspace_id:"workspace".into(),provider:"odysseus-files".into(),resource_id:"resource".into()};
        let source=serde_json::json!({"session_id":"parent","actor_id":"main","execution_revision":1,"root_operation_id":"root","inner_turn_id":"turn"});
        let writer=serde_json::json!({"version":1,"session_id":"child","actor_id":"checkpoint-writer","execution_revision":2,"root_operation_id":"root","inner_turn_id":"child-turn","tool_call_id":"call","source":source});
        let raw=serde_json::json!({"schema_version":1,"action_id":"action","request_digest":"digest","actor_account_id":"account",
            "resource_key":key,"operation":"edit","expected_revision":null,"actor_id":"agent:alice","actor_kind":"agent","tool_id":"workflow_artifact",
            "coverage":{"content_digest":null,"metadata_digest":null,"byte_len":null,"metadata":{"writerIdentity":writer}},
            "state":"Complete","before":null,"after":null,"live":null});
        let record:ActionRecord=serde_json::from_value(raw).unwrap();
        let change=project_resource_change(record.clone(),None,&key);
        assert_eq!(change.actor_id,"agent:alice");
        assert_eq!(change.writer_identity.unwrap().actor_id,"checkpoint-writer");
        let mut wrong=record.clone();wrong.actor_kind="human".into();assert!(project_resource_change(wrong,None,&key).writer_identity.is_none());
        let mut malformed=record;malformed.coverage.as_mut().unwrap().metadata.as_mut().unwrap()["writerIdentity"]["source"]["root_operation_id"]=serde_json::json!("other");
        assert!(project_resource_change(malformed,None,&key).writer_identity.is_none());
    }
}
