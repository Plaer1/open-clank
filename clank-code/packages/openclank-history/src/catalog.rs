//! Durable, application-owned operation catalog for the checked Lore adapter.
//!
//! Lore stores bytes and associations; this journal stores provenance, idempotency and phase
//! receipts. It never performs a live mutation and never replays one during reconciliation.

use crate::retention::{BudgetError, ExpiryRecord, ExpiryState, PersistedReservation, PolicySet};
use redb::{Database, ReadableTable, ReadableTableMetadata, TableDefinition};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::{Arc, Mutex};

const LEGACY_SCHEMA_VERSION: u32 = 1;
const SCHEMA_VERSION: u32 = 2;
const ORDERED_SCHEMA_VERSION: u32 = 3;
const CHECKPOINT_SCHEMA_VERSION: u32 = 4;
const ORIGIN_SCHEMA_VERSION: u32 = 5;
const ORIGIN_INCARNATION: &str = "history_origin_catalog_incarnation_v1";
const ORIGIN_REVISION_PREFIX: &str = "history_origin_revision_v1:";
const META: TableDefinition<&str, &[u8]> = TableDefinition::new("history_meta");
const MEMERY: TableDefinition<&str, &[u8]> = TableDefinition::new("history_memery_manifest_v1");
const ACTIONS: TableDefinition<&str, &[u8]> = TableDefinition::new("history_actions");
const POLICIES: TableDefinition<&str, &[u8]> = TableDefinition::new("history_policies");
const RESERVATIONS: TableDefinition<&str, &[u8]> = TableDefinition::new("history_reservations");
const EXPIRY: TableDefinition<&str, &[u8]> = TableDefinition::new("history_expiry");
const RESTORES: TableDefinition<&str, &[u8]> = TableDefinition::new("history_restores");
const CHANGE_INDEX: TableDefinition<&str, &[u8]> = TableDefinition::new("history_change_index_v1");
const CHANGE_REVISIONS: TableDefinition<&str, &[u8]> = TableDefinition::new("history_change_revisions_v1");
const CHANGE_INDEX_COMPLETE: &str = "change_index_v1_complete";
const CHANGE_INDEX_POLICY: &str = "change_index_v1_policy";
const CHANGE_POLICY_VALUE: &[u8] = b"resource_actions_v1";
const CHANGE_PREPARING: &str = "change_index_v1_preparing";
const TIME_INDEX: TableDefinition<&str, &[u8]> = TableDefinition::new("history_recorded_time_v1");
const ORIGIN_INDEX: TableDefinition<&str, &[u8]> = TableDefinition::new("history_origin_resource_v1");
const ORIGIN_TIME_INDEX: TableDefinition<&str, &[u8]> = TableDefinition::new("history_origin_recorded_time_v1");
const ORDERED_POLICY: &str = "history_ordered_policy_v1";
const ORDERED_POLICY_VALUE: &[u8] = b"recorded_time_origin_resource_v1";

pub type CatalogResult<T> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct ResourceKey {
    pub account_id: String,
    pub workspace_id: String,
    pub provider: String,
    pub resource_id: String,
}

/// Provenance asserted by the trusted managed tool dispatcher, not a grant.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ManagedToolOriginV1 {
    pub version: u8,
    pub host_chat_id: String,
    pub engine_session_id: String,
    pub root_operation_id: String,
    pub actor_id: String,
    pub execution_revision: u64,
    pub inner_turn_id: String,
    pub tool_call_id: String,
    pub assistant_message_id: String,
}

impl ManagedToolOriginV1 {
    pub fn is_valid(&self) -> bool {
        self.version == 1 && self.execution_revision <= 9_007_199_254_740_991 &&
            [&self.host_chat_id, &self.engine_session_id, &self.root_operation_id, &self.actor_id,
             &self.inner_turn_id, &self.tool_call_id, &self.assistant_message_id]
                .iter().all(|value| !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control))
    }
}

impl ResourceKey {
    /// Returns a collision-free lease identity for this scoped resource.
    ///
    /// The JSON representation is length-delimited by its own syntax, so a resource id
    /// containing punctuation cannot alias another account/workspace/provider tuple.
    pub fn scoped_lease_id(&self) -> String {
        format!(
            "resource:{}",
            serde_json::to_string(self).expect("resource key is serializable")
        )
    }

    pub(crate) fn is_well_formed(&self) -> bool {
        !self.account_id.is_empty()
            && !self.workspace_id.is_empty()
            && !self.provider.is_empty()
            && !self.resource_id.is_empty()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum Revision {
    Opaque { kind: String, value: String },
}
impl From<&str> for Revision {
    fn from(value: &str) -> Self {
        Self::Opaque {
            kind: "opaque".into(),
            value: value.into(),
        }
    }
}
impl From<String> for Revision {
    fn from(value: String) -> Self {
        Self::Opaque {
            kind: "opaque".into(),
            value,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Locator {
    pub display_name: String,
    pub location_label: String,
    pub opaque_ref: Option<String>,
}
impl From<&str> for Locator {
    fn from(value: &str) -> Self {
        Self {
            display_name: value.into(),
            location_label: value.into(),
            opaque_ref: None,
        }
    }
}
impl From<String> for Locator {
    fn from(value: String) -> Self {
        Self {
            display_name: value.clone(),
            location_label: value,
            opaque_ref: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct CaptureManifest {
    pub content_digest: Option<String>,
    pub metadata_digest: Option<String>,
    pub byte_len: Option<u64>,
    pub metadata: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct ResourceOutcome {
    pub resource_id: String,
    pub status: String,
    pub revision: Option<Revision>,
}

/// The state observed for one resource at preparation time.  `Absent` is an
/// explicit preimage: a later restore must remove a resource created by the
/// live mutation rather than treating a missing byte payload as unknown.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ResourceExistence {
    Absent,
    Present,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ResourceType {
    File,
    Directory,
    Symlink,
    Other,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct ResourceMetadata {
    pub mode: Option<u32>,
    pub size: Option<u64>,
    pub modified_millis: Option<u64>,
    pub opaque: Option<serde_json::Value>,
}

/// Durable acknowledgement that every required before-state was admitted to
/// the history worker before the live owner was allowed to mutate anything.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PrepareReceipt {
    pub batch_version: u32,
    pub parent_action_id: String,
    pub resource_count: u32,
    pub logical_bytes: u64,
    pub acknowledged: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MutationResource {
    pub resource_key: ResourceKey,
    pub old_locator: Option<Locator>,
    pub new_locator: Option<Locator>,
    pub expected_revision: Option<Revision>,
    pub existence: ResourceExistence,
    pub resource_type: ResourceType,
    pub metadata: ResourceMetadata,
    pub before: Option<VersionReceipt>,
    pub fingerprint: String,
    pub coverage: CaptureManifest,
}

/// Durable state observed after one resource owner reports its live commit.
/// This keeps the before locator and identity separate from the resulting state
/// of a move, overwrite, deletion, or creation.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AfterResource {
    pub resource_key: ResourceKey,
    pub locator: Option<Locator>,
    pub existence: ResourceExistence,
    pub resource_type: ResourceType,
    pub metadata: ResourceMetadata,
    pub after: VersionReceipt,
    pub fingerprint: String,
    pub coverage: CaptureManifest,
}

/// Versioned parent mutation batch.  Content is referenced by Lore receipts
/// so large preimages continue to use the existing chunk/content-addressed
/// path instead of growing the catalog row or IPC frame.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MutationBatch {
    pub schema_version: u32,
    pub parent_action_id: String,
    pub resources: Vec<MutationResource>,
    pub prepare: PrepareReceipt,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LoreRef {
    pub context: [u8; 16],
    pub hash_hex: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum VersionContent {
    Bytes(LoreRef),
    Tombstone,
    Empty,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VersionReceipt {
    pub version_id: String,
    pub content: VersionContent,
    pub fingerprint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ActionState {
    CheckpointPending,
    CheckpointCapturing,
    CheckpointFinalizing,
    CheckpointComplete,
    CheckpointFailed,
    Intent,
    CapturingBefore,
    BeforeDurable,
    Applying,
    Applied,
    CapturingAfter,
    AfterDurable,
    Complete,
    Aborted,
    CaptureFailed,
    Conflict,
    Failed,
    Partial,
    AfterCaptureFailed,
    NeedsReconciliation,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum LiveStatus {
    Committed,
    NotCommitted,
    Conflict,
    Partial,
    Unknown,
}

/// Payload-independent original after-state, supplied by the committed byte owner.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CommittedResource {
    pub resource_key: ResourceKey,
    pub locator: Option<Locator>,
    pub existence: ResourceExistence,
    pub resource_type: ResourceType,
    pub metadata: ResourceMetadata,
    pub fingerprint: String,
    pub coverage: CaptureManifest,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LiveReceipt {
    pub action_id: String,
    pub status: LiveStatus,
    pub fingerprint: Option<String>,
    #[serde(default)]
    pub committed_resources: Vec<CommittedResource>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ActionRecord {
    #[serde(default)]
    pub capture_policy_revision: u64,
    #[serde(default)]
    pub recovery_only: bool,
    #[serde(default)]
    pub recovery_settled: bool,
    #[serde(default)]
    pub captured_at_millis: u64,
    pub schema_version: u32,
    pub action_id: String,
    pub request_digest: String,
    pub actor_account_id: String,
    pub resource_key: ResourceKey,
    /// Physical aliases resolved by a trusted server-side registry.
    ///
    /// This field is persisted in the journal, but it is deliberately skipped by request
    /// deserialization/serialization. An IPC client can submit scoped resources only; it cannot
    /// supply an alias that would lock or read a different physical resource.
    #[serde(default)]
    pub physical_lease_keys: Vec<String>,
    #[serde(default)]
    pub guard_resource_ids: Vec<ResourceKey>,
    #[serde(default)]
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
    pub state: ActionState,
    pub before: Option<VersionReceipt>,
    /// Exact per-resource before receipts for a prepared mutation batch.
    /// The legacy `before` field remains the first-resource projection.
    #[serde(default)]
    pub before_resources: Vec<MutationResource>,
    #[serde(default)]
    pub mutation_batch: Option<MutationBatch>,
    pub after: Option<VersionReceipt>,
    /// Exact after-state receipts for every resource in a committed batch.
    #[serde(default)]
    pub after_resources: Vec<AfterResource>,
    pub live: Option<LiveReceipt>,
    #[serde(default)]
    pub before_capture_digest: Option<String>,
    #[serde(default)]
    pub after_capture_digest: Option<String>,
    #[serde(default)]
    pub complete_request_digest: Option<String>,
    #[serde(default)]
    pub before_scope_id: Option<String>,
    #[serde(default)]
    pub after_scope_id: Option<String>,
    #[serde(default)]
    pub before_logical_bytes: u64,
    #[serde(default)]
    pub after_logical_bytes: u64,
    #[serde(default)]
    pub pinned: bool,
}

/// Stable receipt projection used by protocol consumers.  `live_outcome` records what the
/// authoritative live writer reported, while `history_status` records what this journal has
/// durably captured; they intentionally remain separate during reconciliation.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ActionReceipt {
    pub action_id: String,
    pub phase: ActionState,
    pub history_status: ActionState,
    pub live_outcome: Option<LiveStatus>,
    pub capture_phase: ActionState,
    pub coverage: Option<CaptureManifest>,
    pub before_version_id: Option<String>,
    pub after_version_id: Option<String>,
    pub before: Option<VersionReceipt>,
    pub after: Option<VersionReceipt>,
    #[serde(default)]
    pub before_resources: Vec<MutationResource>,
    #[serde(default)]
    pub mutation_batch: Option<MutationBatch>,
    #[serde(default)]
    pub after_resources: Vec<AfterResource>,
}

/// Validate the immutable selector against one completed managed batch. This
/// returns identities only; the service must authorize every member before disclosure.
pub fn managed_batch_members(record: &ActionRecord, account: &str, request_digest: &str, complete_digest: &str, origin: &ManagedToolOriginV1) -> CatalogResult<Vec<(ResourceKey, String, String)>> {
    let digest_valid = |s: &str| s.len() == 64 && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c));
    let identity_valid = |s: &str| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control);
    if !origin.is_valid() || !identity_valid(&record.action_id) || record.state != ActionState::Complete
        || record.actor_account_id != account || record.resource_key.account_id != account
        || record.managed_tool_origin_v1.as_ref() != Some(origin)
        || !digest_valid(request_digest) || !digest_valid(complete_digest)
        || record.request_digest != request_digest || record.complete_request_digest.as_deref() != Some(complete_digest)
        || !(2..=64).contains(&record.before_resources.len()) || record.after_resources.len() != record.before_resources.len() {
        return Err("managed batch unavailable".into());
    }
    let mut before = BTreeMap::new();
    for item in &record.before_resources {
        let key = &item.resource_key;
        let version = item.before.as_ref().ok_or("managed batch unavailable")?.version_id.clone();
        if key.account_id != account || key.workspace_id != record.resource_key.workspace_id
            || key.provider != "mimo-filesystem" || !identity_valid(&key.resource_id) || !identity_valid(&version)
            || before.insert(key.clone(), version).is_some() { return Err("managed batch unavailable".into()); }
    }
    let mut result = BTreeMap::new();
    for item in &record.after_resources {
        let key = &item.resource_key;
        let prior = before.get(key).ok_or("managed batch unavailable")?;
        let after = &item.after.version_id;
        if !identity_valid(after) || result.insert(key.clone(), (prior.clone(), after.clone())).is_some() {
            return Err("managed batch unavailable".into());
        }
    }
    if result.len() != before.len() { return Err("managed batch unavailable".into()); }
    Ok(result.into_iter().map(|(key, (before, after))| (key, before, after)).collect())
}

impl ActionRecord {
    pub fn requires_recovery(&self) -> bool {
        self.coverage.as_ref().and_then(|coverage| coverage.metadata.as_ref())
            .and_then(|metadata| metadata.get("required_recovery"))
            .and_then(serde_json::Value::as_bool) == Some(true)
    }

    fn capture_clock_key(&self) -> CatalogResult<String> {
        Ok(format!("capture_clock:{}", change_scope(&self.resource_key)?))
    }

    pub fn receipt(&self) -> ActionReceipt {
        ActionReceipt {
            action_id: self.action_id.clone(),
            phase: self.state.clone(),
            history_status: if matches!(self.state, ActionState::AfterCaptureFailed) {
                ActionState::Applied
            } else {
                self.state.clone()
            },
            live_outcome: self.live.as_ref().map(|receipt| receipt.status.clone()),
            capture_phase: self.state.clone(),
            coverage: self.coverage.clone(),
            before_version_id: self
                .before
                .as_ref()
                .map(|receipt| receipt.version_id.clone()),
            after_version_id: self
                .after
                .as_ref()
                .map(|receipt| receipt.version_id.clone()),
            before: self.before.clone(),
            after: self.after.clone(),
            before_resources: self.before_resources.clone(),
            mutation_batch: self.mutation_batch.clone(),
            after_resources: self.after_resources.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BeginResult {
    New(ActionRecord),
    Existing(ActionRecord),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CatalogConflict {
    ActionDigestMismatch,
    ActorMismatch,
    InvalidTransition,
    LeaseConflict,
    LeaseOrder,
    PolicyRevisionMismatch,
}

impl std::fmt::Display for CatalogConflict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "catalog conflict: {self:?}")
    }
}
impl std::error::Error for CatalogConflict {}

impl std::fmt::Display for BudgetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Disabled => write!(f, "history capture is disabled"),
            Self::ExceedsTarget {
                requested,
                available,
            } => {
                write!(
                    f,
                    "history budget exceeded: requested {requested}, available {available}"
                )
            }
            Self::ExceedsGlobal {
                requested,
                available,
            } => {
                write!(
                    f,
                    "global history budget exceeded: requested {requested}, available {available}"
                )
            }
            Self::ExceedsScope {
                requested,
                available,
            } => {
                write!(
                    f,
                    "scope history budget exceeded: requested {requested}, available {available}"
                )
            }
            Self::Arithmetic => write!(f, "history budget arithmetic overflow"),
            Self::InvalidPolicy(reason) => write!(f, "invalid history policy: {reason}"),
            Self::ReservationConflict => write!(f, "history reservation conflict"),
        }
    }
}
impl std::error::Error for BudgetError {}

pub struct Catalog {
    pub(crate) db: Arc<Database>,
    path: std::path::PathBuf,
    leases: Mutex<BTreeMap<String, String>>,
}

/// Internal exact-resource page. Callers must separately authorize the resource
/// and project private action records before exposing anything to a client.
#[derive(Debug, Clone)]
pub struct IndexedChangePage {
    pub revision: u64,
    pub items: Vec<(ActionRecord, Option<ExpiryRecord>)>,
    pub next_action_id: Option<String>,
}

#[derive(Debug,Clone)]
pub struct OrderedChangePage {pub revision:u64,pub items:Vec<(ActionRecord,Option<ExpiryRecord>)>,pub next:Option<String>}
#[derive(Debug, Clone)]
pub struct CrossOriginPage {pub incarnation:String,pub revision:u64,pub items:Vec<(String,ResourceKey,ActionRecord,Option<ExpiryRecord>)>,pub next:Option<String>}


fn canonical_change_resource(key: &ResourceKey) -> ResourceKey {
    let mut key = key.clone();
    if matches!(key.provider.as_str(), "filesystem" | "odysseus-files") { key.provider = "odysseus-files".into(); }
    key
}

pub(crate) fn change_scope(key: &ResourceKey) -> CatalogResult<String> {
    if [&key.account_id, &key.workspace_id, &key.provider, &key.resource_id].iter().any(|value| value.is_empty() || value.len() > 512) {
        return Err("change resource identity exceeds limit".into());
    }
    let bytes = serde_json::to_vec(&canonical_change_resource(key))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

pub(crate) fn action_resources(record: &ActionRecord) -> BTreeSet<ResourceKey> {
    let mut keys = BTreeSet::new();
    if record.recovery_only { return keys; }
    keys.insert(record.resource_key.clone());
    keys.extend(record.modified_resource_ids.iter().cloned());
    keys.extend(record.before_resources.iter().map(|r| r.resource_key.clone()));
    keys.extend(record.after_resources.iter().map(|r| r.resource_key.clone()));
    if let Some(batch) = &record.mutation_batch {
        keys.extend(batch.resources.iter().map(|r| r.resource_key.clone()));
    }
    if let Some(live) = &record.live {
        keys.extend(live.committed_resources.iter().map(|r| r.resource_key.clone()));
    }
    keys.iter().map(canonical_change_resource).collect()
}

fn update_change_index(write: &redb::WriteTransaction, old: Option<&ActionRecord>, new: &ActionRecord) -> CatalogResult<()> {
    {
        let meta = write.open_table(META)?;
        let version: u32 = serde_json::from_slice(meta.get("schema")?.ok_or("missing catalog schema")?.value())?;
        if version == LEGACY_SCHEMA_VERSION { return Ok(()); }
        if !matches!(version, SCHEMA_VERSION | ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) || !meta.get(CHANGE_INDEX_COMPLETE)?.is_some_and(|v| v.value() == b"true") ||
            !meta.get(CHANGE_INDEX_POLICY)?.is_some_and(|v| v.value() == CHANGE_POLICY_VALUE) {
            return Err("change index policy unavailable".into());
        }
        if matches!(version, ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) {
            if !meta.get(ORDERED_POLICY)?.is_some_and(|v|v.value()==ORDERED_POLICY_VALUE) {return Err("ordered index policy unavailable".into());}
            drop(meta);
            write_ordered_indexes(write,old,new)?;
            if version==ORIGIN_SCHEMA_VERSION {write_origin_timeline(write,old,new)?;write_origin_revisions(write,old,new)?;}
        }
    }
    write_change_index(write, old, new)
}

fn origin_scope(record:&ActionRecord)->CatalogResult<Option<String>> {
    use sha2::{Digest,Sha256};
    if record.recovery_only { return Ok(None); }
    let Some(origin)=record.managed_tool_origin_v1.as_ref().filter(|o|o.is_valid()) else{return Ok(None)};
    let bytes=serde_json::to_vec(&(&record.actor_account_id,&record.resource_key.workspace_id,&origin.host_chat_id,
        &origin.engine_session_id,&origin.root_operation_id,&origin.actor_id,origin.execution_revision,&origin.inner_turn_id))?;
    Ok(Some(format!("{:x}",Sha256::digest(bytes))))
}

pub(crate) fn ordered_keys(record:&ActionRecord)->CatalogResult<(BTreeSet<String>,BTreeSet<String>)> {
    let mut times=BTreeSet::new();let mut origins=BTreeSet::new();let origin=origin_scope(record)?;
    for key in action_resources(record) {
        let scope=change_scope(&key)?;
        if let Some(timestamp)=record.timestamp_millis {times.insert(format!("{scope}:{timestamp:016x}:{}",record.action_id));}
        if let Some(ref origin)=origin {origins.insert(format!("{origin}:{scope}:{}",record.action_id));}
    }
    Ok((times,origins))
}

fn write_ordered_indexes(write:&redb::WriteTransaction,old:Option<&ActionRecord>,new:&ActionRecord)->CatalogResult<()> {
    let (old_time,old_origin)=old.map(ordered_keys).transpose()?.unwrap_or_default();
    let (time,origin)=ordered_keys(new)?;
    let mut ti=write.open_table(TIME_INDEX)?;let mut oi=write.open_table(ORIGIN_INDEX)?;
    for key in old_time.difference(&time) {ti.remove(key.as_str())?;}
    for key in old_origin.difference(&origin) {oi.remove(key.as_str())?;}
    for key in &time {ti.insert(key.as_str(),new.action_id.as_bytes())?;}
    for key in &origin {oi.insert(key.as_str(),new.action_id.as_bytes())?;}
    Ok(())
}

pub(crate) fn origin_timeline_keys(record:&ActionRecord)->CatalogResult<BTreeMap<String,ResourceKey>> {
    let Some(origin)=origin_scope(record)? else{return Ok(BTreeMap::new())};
    let stamp=match record.timestamp_millis {Some(value)=>format!("0:{value:016x}"),None=>"1:0000000000000000".to_owned()};
    let mut keys=BTreeMap::new();
    for resource in action_resources(record) {
        let key=format!("{origin}:{stamp}:{}:{}",record.action_id,change_scope(&resource)?);
        if keys.insert(key,resource).is_some(){return Err("ambiguous origin timeline".into());}
    }
    Ok(keys)
}
fn write_origin_timeline(write:&redb::WriteTransaction,old:Option<&ActionRecord>,new:&ActionRecord)->CatalogResult<()> {
    let old=old.map(origin_timeline_keys).transpose()?.unwrap_or_default();let next=origin_timeline_keys(new)?;
    let mut index=write.open_table(ORIGIN_TIME_INDEX)?;
    for key in old.keys().filter(|key|!next.contains_key(*key)){index.remove(key.as_str())?;}
    for key in next.keys(){index.insert(key.as_str(),new.action_id.as_bytes())?;}
    Ok(())
}

fn write_origin_revisions(write:&redb::WriteTransaction,old:Option<&ActionRecord>,new:&ActionRecord)->CatalogResult<()> {
    let mut origins=BTreeSet::new();
    if let Some(old)=old {if let Some(value)=origin_scope(old)? {origins.insert(value);}}
    if let Some(value)=origin_scope(new)? {origins.insert(value);}
    let mut meta=write.open_table(META)?;
    for origin in origins {let key=format!("{ORIGIN_REVISION_PREFIX}{origin}");
        let next=match meta.get(key.as_str())? {Some(value)=>serde_json::from_slice::<u64>(value.value())?.checked_add(1).filter(|v|*v<=9_007_199_254_740_991).ok_or("origin revision exhausted")?,None=>1};
        meta.insert(key.as_str(),serde_json::to_vec(&next)?.as_slice())?;
    }
    Ok(())
}

fn write_change_index(write: &redb::WriteTransaction, old: Option<&ActionRecord>, new: &ActionRecord) -> CatalogResult<()> {
    if new.action_id.is_empty() || new.action_id.len() > 512 { return Err("change action identity exceeds limit".into()); }
    let old_keys = old.map(action_resources).unwrap_or_default();
    let new_keys = action_resources(new);
    let mut index = write.open_table(CHANGE_INDEX)?;
    let mut revisions = write.open_table(CHANGE_REVISIONS)?;
    for key in old_keys.union(&new_keys) {
        let scope = change_scope(key)?;
        let entry = format!("{scope}:{}", new.action_id);
        if new_keys.contains(key) { index.insert(entry.as_str(), new.action_id.as_bytes())?; }
        else { index.remove(entry.as_str())?; }
        let next = match revisions.get(scope.as_str())? {
            Some(value) => serde_json::from_slice::<u64>(value.value())?.checked_add(1).filter(|v| *v <= 9_007_199_254_740_991).ok_or("change revision exhausted")?,
            None => 1,
        };
        revisions.insert(scope.as_str(), serde_json::to_vec(&next)?.as_slice())?;
    }
    Ok(())
}

fn touch_change_action(write: &redb::WriteTransaction, action_id: &str) -> CatalogResult<()> {
    let record = {
        let actions = write.open_table(ACTIONS)?;
        actions.get(action_id)?.map(|value| serde_json::from_slice::<ActionRecord>(value.value())).transpose()?
    };
    if let Some(record) = record { update_change_index(write, Some(&record), &record)?; }
    Ok(())
}

#[cfg(unix)]
#[derive(Serialize, Deserialize)]
struct CopyPreparation {
    version: u8,
    source_path: String,
    source_digest: String,
    copy_device: u64,
    copy_inode: u64,
    last_action: Option<String>,
}

/// Explicit offline operation. The source is opened read-only under the same
/// nonblocking flock used by redb; only newly created or marked destination
/// copies may be modified. Existing runtime catalogs are never converted here.
#[cfg(unix)]
pub fn prepare_change_index_copy(source: &Path, destination: &Path, batch: usize) -> CatalogResult<()> {
    prepare_index_copy(source,destination,batch,false,false,false)
}

#[cfg(unix)]
pub fn prepare_ordered_index_copy(source: &Path, destination: &Path, batch: usize) -> CatalogResult<()> {
    prepare_index_copy(source,destination,batch,true,false,false)
}

#[cfg(unix)]
pub fn prepare_checkpoint_index_copy(source: &Path, destination: &Path, batch: usize) -> CatalogResult<()> {
    prepare_index_copy(source,destination,batch,true,true,false)
}

#[cfg(unix)]
pub fn prepare_origin_index_copy(source: &Path, destination: &Path, batch: usize) -> CatalogResult<()> {
    prepare_index_copy(source,destination,batch,true,false,true)
}

#[cfg(unix)]
fn prepare_index_copy(source: &Path, destination: &Path, batch: usize, ordered:bool, checkpoint_profile:bool, origin_profile:bool) -> CatalogResult<()> {
    use std::io::{Read, Seek, SeekFrom, Write};
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use sha2::{Digest, Sha256};
    if !(1..=100).contains(&batch) { return Err("invalid preparation batch size".into()); }
    let source_path = source.canonicalize()?;
    let mut input = std::fs::File::open(&source_path)?;
    if !input.metadata()?.is_file() { return Err("source is not a regular catalog".into()); }
    if unsafe { libc::flock(input.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 { return Err("source catalog is not quiescent".into()); }
    let parent = destination.parent().ok_or("copy destination needs a parent")?.canonicalize()?;
    let name = destination.file_name().ok_or("copy destination needs a filename")?;
    let target = parent.join(name);
    if target == source_path { return Err("source and copy must differ".into()); }
    let initial = !target.exists();
    let target_file = if initial {
        Some(std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&target)?)
    } else { None };
    if let Ok(metadata) = std::fs::symlink_metadata(&target) {
        let original = input.metadata()?;
        if !metadata.file_type().is_file() || (metadata.dev() == original.dev() && metadata.ino() == original.ino()) {
            return Err("copy must be a distinct regular file".into());
        }
    }
    let unique = format!(".history-verify-{}-{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos());
    let verify_path = parent.join(unique);
    let mut verify_file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&verify_path)?;
    struct OwnedVerification { path: std::path::PathBuf, dev: u64, ino: u64 }
    impl Drop for OwnedVerification {
        fn drop(&mut self) {
            use std::os::unix::fs::MetadataExt;
            if std::fs::symlink_metadata(&self.path).is_ok_and(|m| m.file_type().is_file() && m.dev() == self.dev && m.ino() == self.ino) { let _ = std::fs::remove_file(&self.path); }
        }
    }
    let verify_metadata = verify_file.metadata()?;
    let _owned_verify = OwnedVerification { path: verify_path.clone(), dev: verify_metadata.dev(), ino: verify_metadata.ino() };
    let mut target_file = target_file;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = input.read(&mut buffer)?; if count == 0 { break; }
        digest.update(&buffer[..count]); verify_file.write_all(&buffer[..count])?;
        if let Some(file) = target_file.as_mut() { file.write_all(&buffer[..count])?; }
    }
    verify_file.sync_all()?;
    if let Some(file) = target_file.as_mut() { file.sync_all()?; }
    drop(verify_file); drop(target_file);
    let source_digest = format!("{:x}", digest.finalize());
    let identity = std::fs::symlink_metadata(&target)?;
    let source_text = source_path.to_str().ok_or("source path is not UTF-8")?.to_owned();
    // Never open an arbitrary existing destination through recovering redb.
    // Inspect a private snapshot while the destination is read-only locked.
    if !initial {
        let mut existing = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&target)?;
        let opened = existing.metadata()?;
        if opened.dev() != identity.dev() || opened.ino() != identity.ino() || unsafe { libc::flock(existing.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("copy is not quiescent or identity changed".into());
        }
        let inspect_path = parent.join(format!(".history-inspect-{}-{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos()));
        let mut inspect_file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&inspect_path)?;
        let inspect_metadata = inspect_file.metadata()?;
        let owned_inspect = OwnedVerification { path: inspect_path.clone(), dev: inspect_metadata.dev(), ino: inspect_metadata.ino() };
        std::io::copy(&mut existing, &mut inspect_file)?; inspect_file.sync_all()?; drop(inspect_file);
        let inspection = Database::open(&inspect_path)?;
        {
            let read = inspection.begin_read()?; let meta = read.open_table(META)?;
            let schema: u32 = serde_json::from_slice(meta.get("schema")?.ok_or("copy schema missing")?.value())?;
            let saved: CopyPreparation = serde_json::from_slice(meta.get(CHANGE_PREPARING)?.ok_or("existing destination is not an owned preparation copy")?.value())?;
            if !(schema==LEGACY_SCHEMA_VERSION || (ordered && schema==SCHEMA_VERSION) || (checkpoint_profile && schema==ORDERED_SCHEMA_VERSION) || (origin_profile && schema==CHECKPOINT_SCHEMA_VERSION)) || saved.version != if origin_profile {4} else if checkpoint_profile {3} else if ordered {2} else {1} || saved.source_path != source_text || saved.source_digest != source_digest || saved.copy_device != identity.dev() || saved.copy_inode != identity.ino() {
                return Err("preparation source or copy identity changed".into());
            }
        }
        drop(inspection); drop(owned_inspect); drop(existing);
    }
    let verification = Database::open(&verify_path)?;
    let copy = Database::open(&target)?;
    let after_open = std::fs::symlink_metadata(&target)?;
    if !after_open.file_type().is_file() || after_open.dev() != identity.dev() || after_open.ino() != identity.ino() { return Err("copy identity changed".into()); }
    let baseline = verification.begin_read()?;
    let baseline_meta = baseline.open_table(META)?;
    let baseline_schema: u32 = serde_json::from_slice(baseline_meta.get("schema")?.ok_or("source schema missing")?.value())?;
    if !(baseline_schema==LEGACY_SCHEMA_VERSION || (ordered && baseline_schema==SCHEMA_VERSION) || (checkpoint_profile && baseline_schema==ORDERED_SCHEMA_VERSION) || (origin_profile && baseline_schema==CHECKPOINT_SCHEMA_VERSION)) || baseline_meta.get(CHANGE_PREPARING)?.is_some() { return Err("source catalog profile unavailable".into()); }
    if checkpoint_profile && baseline_schema!=ORDERED_SCHEMA_VERSION {return Err("checkpoint preparation requires ordered profile".into());}
    if matches!(baseline_schema,SCHEMA_VERSION|ORDERED_SCHEMA_VERSION|CHECKPOINT_SCHEMA_VERSION) && (!baseline_meta.get(CHANGE_INDEX_COMPLETE)?.is_some_and(|v|v.value()==b"true") || !baseline_meta.get(CHANGE_INDEX_POLICY)?.is_some_and(|v|v.value()==CHANGE_POLICY_VALUE)) {return Err("source change index unavailable".into());}
    if matches!(baseline_schema,ORDERED_SCHEMA_VERSION|CHECKPOINT_SCHEMA_VERSION) && !baseline_meta.get(ORDERED_POLICY)?.is_some_and(|v|v.value()==ORDERED_POLICY_VALUE) {return Err("source ordered index unavailable".into());}
    drop(baseline_meta);
    if matches!(baseline_schema,SCHEMA_VERSION|ORDERED_SCHEMA_VERSION|CHECKPOINT_SCHEMA_VERSION) {
        let actions=baseline.open_table(ACTIONS)?;let index=baseline.open_table(CHANGE_INDEX)?;let revisions=baseline.open_table(CHANGE_REVISIONS)?;let mut count=0u64;
        for entry in actions.iter()? {let (key,value)=entry?;if value.value().len()>1_000_000{return Err("source action exceeds bounds".into());}let row:ActionRecord=serde_json::from_slice(value.value())?;if row.action_id!=key.value(){return Err("source action identity changed".into());}for resource in action_resources(&row){let scope=change_scope(&resource)?;let item=format!("{scope}:{}",row.action_id);if !index.get(item.as_str())?.is_some_and(|v|v.value()==row.action_id.as_bytes()) || revisions.get(scope.as_str())?.is_none(){return Err("source index inconsistent".into());}count=count.checked_add(1).ok_or("source index too large")?;}}
        if index.len()?!=count{return Err("source index inconsistent".into());}
        for entry in revisions.iter()? {let (_,value)=entry?;let revision:u64=serde_json::from_slice(value.value())?;if revision==0||revision>9_007_199_254_740_991{return Err("source revision invalid".into());}}
    }
    let mut checkpoint = {
        let read = copy.begin_read()?;
        let meta = read.open_table(META)?;
        let schema: u32 = serde_json::from_slice(meta.get("schema")?.ok_or("copy schema missing")?.value())?;
        if schema != baseline_schema { return Err("copy schema changed".into()); }
        let saved = meta.get(CHANGE_PREPARING)?.map(|v| serde_json::from_slice::<CopyPreparation>(v.value())).transpose()?;
        if initial && saved.is_some() { return Err("unexpected preparation marker".into()); }
        if !initial && saved.is_none() { return Err("existing destination is not an owned preparation copy".into()); }
        saved
    };
    if let Some(saved) = &checkpoint {
        if saved.version != if checkpoint_profile {3} else if ordered {2} else {1} || saved.source_path != source_text || saved.source_digest != source_digest || saved.copy_device != identity.dev() || saved.copy_inode != identity.ino() {
            return Err("preparation source or copy identity changed".into());
        }
    }
    // The locked source snapshot and copy must still contain identical legacy
    // records. This detects old-writer changes across interruption.
    {
        let candidate = copy.begin_read()?;
        for definition in [ACTIONS, EXPIRY, POLICIES, RESERVATIONS, RESTORES] {
            let source_table = baseline.open_table(definition)?;
            let copy_table = candidate.open_table(definition)?;
            let mut a = source_table.iter()?; let mut b = copy_table.iter()?;
            loop {
                match (a.next(), b.next()) {
                    (None, None) => break,
                    (Some(left), Some(right)) => {
                        let (lk, lv) = left?; let (rk, rv) = right?;
                        if lk.value() != rk.value() || lv.value() != rv.value() || lv.value().len() > 1_000_000 { return Err("copy history changed or exceeds preparation bounds".into()); }
                    }
                    _ => return Err("copy history changed".into()),
                }
            }
        }
        let source_meta = baseline.open_table(META)?; let copy_meta = candidate.open_table(META)?;
        let private_key = |key: &str| matches!(key, CHANGE_PREPARING | CHANGE_INDEX_COMPLETE | CHANGE_INDEX_POLICY | ORDERED_POLICY);
        let mut a = source_meta.iter()?.filter(|item| item.as_ref().map_or(true, |(key, _)| !private_key(key.value())));
        let mut b = copy_meta.iter()?.filter(|item| item.as_ref().map_or(true, |(key, _)| !private_key(key.value())));
        loop {
            match (a.next(), b.next()) {
                (None, None) => break,
                (Some(left), Some(right)) => { let (lk, lv) = left?; let (rk, rv) = right?;
                    if lk.value() != rk.value() || lv.value() != rv.value() || lv.value().len() > 1_000_000 { return Err("copy metadata changed or exceeds preparation bounds".into()); }
                }
                _ => return Err("copy metadata changed".into()),
            }
        }
    }
    if checkpoint.is_none() {
        let write = copy.begin_write()?;
        write.delete_table(CHANGE_INDEX)?; write.delete_table(CHANGE_REVISIONS)?;
        { let _index = write.open_table(CHANGE_INDEX)?; let _revisions = write.open_table(CHANGE_REVISIONS)?; }
        if ordered {write.delete_table(TIME_INDEX)?;write.delete_table(ORIGIN_INDEX)?;let _=write.open_table(TIME_INDEX)?;let _=write.open_table(ORIGIN_INDEX)?;}
        if origin_profile {write.delete_table(ORIGIN_TIME_INDEX)?;let _=write.open_table(ORIGIN_TIME_INDEX)?;}
        let saved = CopyPreparation { version:if origin_profile {4} else if checkpoint_profile {3} else if ordered {2} else {1}, source_path:source_text, source_digest:source_digest.clone(), copy_device:identity.dev(), copy_inode:identity.ino(), last_action:None };
        { let mut meta = write.open_table(META)?; meta.remove(CHANGE_INDEX_COMPLETE)?; meta.remove(CHANGE_INDEX_POLICY)?; meta.insert(CHANGE_PREPARING, serde_json::to_vec(&saved)?.as_slice())?; }
        write.commit()?; checkpoint = Some(saved);
    }
    let mut saved = checkpoint.ok_or("preparation marker missing")?;
    // Verify the entire indexed prefix and its revision counts before resuming.
    {
        let read = copy.begin_read()?;
        let actions = read.open_table(ACTIONS)?; let index = read.open_table(CHANGE_INDEX)?; let revisions = read.open_table(CHANGE_REVISIONS)?;
        let mut expected = 0u64;
        let mut expected_time=0u64;let mut expected_origin=0u64;let mut expected_origin_time=0u64;
        let origin_time=if origin_profile {Some(read.open_table(ORIGIN_TIME_INDEX)?)} else {None};
        let ordered_tables=if ordered {Some((read.open_table(TIME_INDEX)?,read.open_table(ORIGIN_INDEX)?))} else {None};
        for entry in actions.iter()? {
            let (key, value) = entry?;
            if saved.last_action.as_ref().is_none_or(|last| key.value() > last.as_str()) { break; }
            let record: ActionRecord = serde_json::from_slice(value.value())?;
            if record.action_id != key.value() { return Err("invalid action identity".into()); }
            if let Some(index)=&origin_time {let keys=origin_timeline_keys(&record)?;for key in keys.keys(){if !index.get(key.as_str())?.is_some_and(|v|v.value()==record.action_id.as_bytes()){return Err("prepared origin-time index changed".into());}}expected_origin_time+=keys.len() as u64;}
            if let Some((time,origin))=&ordered_tables {let (times,origins)=ordered_keys(&record)?;for entry in &times {if !time.get(entry.as_str())?.is_some_and(|v|v.value()==record.action_id.as_bytes()){return Err("prepared time index changed".into());}}for entry in &origins {if !origin.get(entry.as_str())?.is_some_and(|v|v.value()==record.action_id.as_bytes()){return Err("prepared origin index changed".into());}}expected_time+=times.len() as u64;expected_origin+=origins.len() as u64;}
            for resource in action_resources(&record) {
                let index_key = format!("{}:{}", change_scope(&resource)?, record.action_id);
                if !index.get(index_key.as_str())?.is_some_and(|v| v.value() == record.action_id.as_bytes()) { return Err("prepared index changed".into()); }
                expected = expected.checked_add(1).ok_or("prepared index too large")?;
            }
        }
        if origin_time.as_ref().is_some_and(|index|index.len().ok()!=Some(expected_origin_time)){return Err("prepared origin-time index changed".into());}
        if index.len()? != expected { return Err("prepared index changed".into()); }
        if let Some((time,origin))=&ordered_tables {if time.len()?!=expected_time || origin.len()?!=expected_origin {return Err("prepared ordered index changed".into());}}
        let mut current = String::new(); let mut count = 0u64; let mut scopes = 0u64;
        for entry in index.iter()? {
            let (key, _) = entry?; let scope = key.value().split_once(':').ok_or("invalid index key")?.0;
            if current != scope {
                if !current.is_empty() {
                    let stored: u64 = serde_json::from_slice(revisions.get(current.as_str())?.ok_or("missing revision")?.value())?;
                    if stored != count { return Err("prepared revision changed".into()); }
                }
                current = scope.to_owned(); count = 0; scopes += 1;
            }
            count += 1;
        }
        if !current.is_empty() {
            let stored: u64 = serde_json::from_slice(revisions.get(current.as_str())?.ok_or("missing revision")?.value())?;
            if stored != count { return Err("prepared revision changed".into()); }
        }
        if revisions.len()? != scopes { return Err("prepared revisions changed".into()); }
    }
    loop {
        let read = copy.begin_read()?; let actions = read.open_table(ACTIONS)?;
        let start = saved.last_action.as_deref().unwrap_or(""); let mut records = Vec::new(); let mut bytes = 0usize;
        for entry in actions.range(start..)? {
            let (key, value) = entry?; if saved.last_action.as_deref() == Some(key.value()) { continue; }
            bytes = bytes.checked_add(value.value().len()).ok_or("preparation batch too large")?;
            if value.value().len() > 1_000_000 || bytes > 4_000_000 { return Err("preparation batch exceeds bounds; retry with smaller batch".into()); }
            let record: ActionRecord = serde_json::from_slice(value.value())?;
            if record.action_id != key.value() { return Err("invalid action identity".into()); }
            records.push(record); if records.len() == batch { break; }
        }
        drop(actions); drop(read);
        if records.is_empty() { break; }
        let write = copy.begin_write()?;
        for record in &records { write_change_index(&write, None, record)?; if ordered {write_ordered_indexes(&write,None,record)?;} if origin_profile {write_origin_timeline(&write,None,record)?;} }
        saved.last_action = records.last().map(|r| r.action_id.clone());
        { let mut meta = write.open_table(META)?; meta.insert(CHANGE_PREPARING, serde_json::to_vec(&saved)?.as_slice())?; }
        write.commit()?;
    }
    // Recompute every derived key from the retained canonical actions before
    // marking this copy ready; neither checkpoint nor table length is proof.
    if ordered {
        let read=copy.begin_read()?;let actions=read.open_table(ACTIONS)?;let time=read.open_table(TIME_INDEX)?;let origin=read.open_table(ORIGIN_INDEX)?;
        let mut time_count=0u64;let mut origin_count=0u64;
        for entry in actions.iter()? {let (key,value)=entry?;if value.value().len()>1_000_000{return Err("action exceeds preparation bounds".into());}let action:ActionRecord=serde_json::from_slice(value.value())?;if action.action_id!=key.value(){return Err("invalid action identity".into());}let (times,origins)=ordered_keys(&action)?;for k in &times {if !time.get(k.as_str())?.is_some_and(|v|v.value()==action.action_id.as_bytes()){return Err("time index verification failed".into());}}for k in &origins {if !origin.get(k.as_str())?.is_some_and(|v|v.value()==action.action_id.as_bytes()){return Err("origin index verification failed".into());}}time_count=time_count.checked_add(times.len() as u64).ok_or("index too large")?;origin_count=origin_count.checked_add(origins.len() as u64).ok_or("index too large")?;}
        if time.len()?!=time_count || origin.len()?!=origin_count{return Err("ordered index verification failed".into());}
    }
    if origin_profile {
        let read=copy.begin_read()?;let actions=read.open_table(ACTIONS)?;let index=read.open_table(ORIGIN_TIME_INDEX)?;let mut count=0u64;
        for row in actions.iter()? {let (key,value)=row?;let action:ActionRecord=serde_json::from_slice(value.value())?;
            if action.action_id!=key.value(){return Err("origin-time identity unavailable".into());}
            let keys=origin_timeline_keys(&action)?;for entry in keys.keys(){if !index.get(entry.as_str())?.is_some_and(|v|v.value()==action.action_id.as_bytes()){return Err("origin-time index unavailable".into());}}
            count=count.checked_add(keys.len() as u64).ok_or("origin-time index too large")?;}
        if index.len()?!=count{return Err("origin-time index unavailable".into());}
    }
    input.seek(SeekFrom::Start(0))?; let mut final_digest = Sha256::new();
    loop { let count = input.read(&mut buffer)?; if count == 0 { break; } final_digest.update(&buffer[..count]); }
    if format!("{:x}", final_digest.finalize()) != source_digest { return Err("source changed during preparation".into()); }
    let incarnation=if origin_profile {
        let mut random=[0u8;32];std::fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
        Some(random.iter().map(|byte|format!("{byte:02x}")).collect::<String>())
    } else {None};
    let write = copy.begin_write()?;
    { let mut meta = write.open_table(META)?; if let Some(value)=incarnation {meta.insert(ORIGIN_INCARNATION,value.as_bytes())?;} meta.insert("schema", serde_json::to_vec(&if origin_profile {ORIGIN_SCHEMA_VERSION} else if checkpoint_profile {CHECKPOINT_SCHEMA_VERSION} else if ordered {ORDERED_SCHEMA_VERSION} else {SCHEMA_VERSION})?.as_slice())?; meta.insert(CHANGE_INDEX_POLICY, CHANGE_POLICY_VALUE)?; meta.insert(CHANGE_INDEX_COMPLETE, b"true".as_slice())?; if ordered {meta.insert(ORDERED_POLICY,ORDERED_POLICY_VALUE)?;} meta.remove(CHANGE_PREPARING)?; }
    write.commit()?;
    Ok(())
}

#[cfg(test)]
mod change_index_tests {
    use super::*;
    fn resource(id: &str) -> ResourceKey {
        ResourceKey { account_id: "owner".into(), workspace_id: "workspace".into(), provider: "filesystem".into(), resource_id: id.into() }
    }
    fn action(id: &str, key: &ResourceKey) -> ActionRecord {
        serde_json::from_value(serde_json::json!({
            "schema_version":1,"action_id":id,"request_digest":"digest","actor_account_id":"owner",
            "resource_key":key,"operation":"edit","expected_revision":null,"actor_id":"actor",
            "state":"Intent","before":null,"after":null,"live":null
        })).unwrap()
    }
    #[cfg(unix)]
    #[test]
    fn ordered_copy_resource_origin_and_staleness() {
        let temp=tempfile::tempdir().unwrap();let source=temp.path().join("source.redb");let target=temp.path().join("ordered.redb");let key=resource("file");
        let origin=ManagedToolOriginV1{version:1,host_chat_id:"chat".into(),engine_session_id:"session".into(),root_operation_id:"root".into(),actor_id:"main".into(),execution_revision:1,inner_turn_id:"turn".into(),tool_call_id:"tool".into(),assistant_message_id:"message".into()};
        {let c=Catalog::open(&source,"owner").unwrap();for (id,stamp) in [("a",Some(8)),("b",Some(8)),("c",None)] {let mut row=action(id,&key);row.timestamp_millis=stamp;row.managed_tool_origin_v1=Some(origin.clone());c.begin_action(row).unwrap();}assert!(c.ordered_changes("owner",&key,None,None,None,2).is_err());}
        let original=std::fs::read(&source).unwrap();prepare_ordered_index_copy(&source,&target,2).unwrap();assert_eq!(std::fs::read(&source).unwrap(),original);
        // Recreate the existing preparer's durable first-batch checkpoint in a
        // distinct copy, then require exact prefix validation before resume.
        {use sha2::{Digest,Sha256};use std::os::unix::fs::MetadataExt;let resume=temp.path().join("resume.redb");std::fs::copy(&source,&resume).unwrap();
            {let db=Database::open(&resume).unwrap();let identity=std::fs::metadata(&resume).unwrap();let read=db.begin_read().unwrap();let actions=read.open_table(ACTIONS).unwrap();let first:ActionRecord=serde_json::from_slice(actions.get("a").unwrap().unwrap().value()).unwrap();drop(actions);drop(read);
            let write=db.begin_write().unwrap();write.delete_table(CHANGE_INDEX).unwrap();write.delete_table(CHANGE_REVISIONS).unwrap();{let _=write.open_table(CHANGE_INDEX).unwrap();let _=write.open_table(CHANGE_REVISIONS).unwrap();let _=write.open_table(TIME_INDEX).unwrap();let _=write.open_table(ORIGIN_INDEX).unwrap();}write_change_index(&write,None,&first).unwrap();write_ordered_indexes(&write,None,&first).unwrap();
            let saved=CopyPreparation{version:2,source_path:source.canonicalize().unwrap().to_str().unwrap().into(),source_digest:format!("{:x}",Sha256::digest(&original)),copy_device:identity.dev(),copy_inode:identity.ino(),last_action:Some("a".into())};{let mut meta=write.open_table(META).unwrap();meta.remove(CHANGE_INDEX_COMPLETE).unwrap();meta.remove(CHANGE_INDEX_POLICY).unwrap();meta.insert(CHANGE_PREPARING,serde_json::to_vec(&saved).unwrap().as_slice()).unwrap();}write.commit().unwrap();}
            prepare_ordered_index_copy(&source,&resume,1).unwrap();let resumed=Catalog::open(&resume,"owner").unwrap();assert_eq!(resumed.ordered_changes("owner",&key,Some("a"),None,None,10).unwrap().items.len(),3);assert_eq!(std::fs::read(&source).unwrap(),original);
        }
        let c=Catalog::open(&target,"owner").unwrap();{let read=c.db.begin_read().unwrap();let meta=read.open_table(META).unwrap();let schema:u32=serde_json::from_slice(meta.get("schema").unwrap().unwrap().value()).unwrap();assert_eq!(schema,3);assert_ne!(schema,2,"format-2 opener refuses this profile");}let page=c.ordered_changes("owner",&key,None,None,None,1).unwrap();assert_eq!(page.items[0].0.action_id,"a");
        let next=c.ordered_changes("owner",&key,None,Some(page.revision),page.next.as_deref(),1).unwrap();assert_eq!(next.items[0].0.action_id,"b");assert!(next.next.is_none());
        let grouped=c.ordered_changes("owner",&key,Some("a"),None,None,10).unwrap();assert_eq!(grouped.items.iter().map(|v|v.0.action_id.as_str()).collect::<Vec<_>>(),vec!["a","b","c"]);
        assert!(c.ordered_changes("other",&key,None,None,None,1).is_err());assert!(c.ordered_changes("owner",&resource("foreign"),Some("a"),None,None,1).is_err());
        c.set_pinned("a",true).unwrap();assert!(c.ordered_changes("owner",&key,None,Some(page.revision),page.next.as_deref(),1).is_err());
        let revision=c.ordered_changes("owner",&key,None,None,None,1).unwrap().revision;c.begin_expiry(ExpiryRecord{action_id:"a".into(),state:ExpiryState::Expiring,bytes:0,started_millis:1,finished_millis:None,metadata_expired:false}).unwrap();assert!(c.ordered_changes("owner",&key,None,Some(revision),None,1).is_err());
        c.update("b",|row|{row.state=ActionState::BeforeDurable;Ok(())}).unwrap();let moved=resource("moved");c.rebind_resource_key("b",moved.clone()).unwrap();assert_eq!(c.ordered_changes("owner",&moved,Some("b"),None,None,10).unwrap().items.len(),1);assert!(c.ordered_changes("owner",&key,Some("b"),None,None,10).is_err());
    }
    #[test]
    fn bounded_index_revision_and_legacy() {
        let temp = tempfile::tempdir().unwrap();
        let catalog = Catalog::open(temp.path().join("catalog.redb"), "owner").unwrap();
        let first = resource("first");
        let second = resource("second");
        let mut a = action("a", &first);
        a.modified_resource_ids.push(second.clone());
        a.before = Some(VersionReceipt { version_id:"before".into(), content:VersionContent::Tombstone, fingerprint:"missing".into() });
        catalog.begin_action(a).unwrap();
        catalog.begin_action(action("b", &first)).unwrap();
        let page = catalog.indexed_changes("owner", &first, None, None, 1).unwrap();
        assert_eq!(page.items[0].0.action_id, "a");
        let projected = crate::operations::project_resource_change(page.items[0].0.clone(), page.items[0].1.clone(), &first);
        assert_eq!(projected.before.unwrap().availability, "absent");
        assert!(projected.after.is_none());
        assert_eq!(projected.binary, "unknown");
        assert_eq!(page.next_action_id.as_deref(), Some("a"));
        assert_eq!(catalog.indexed_changes("owner", &second, None, None, 10).unwrap().items.len(), 1);
        let mut alias = first.clone(); alias.provider = "odysseus-files".into();
        assert_eq!(catalog.indexed_changes("owner", &alias, None, None, 10).unwrap().items.len(), 2);
        assert!(catalog.indexed_changes("owner", &resource("unrelated"), None, None, 10).unwrap().items.is_empty());
        assert!(catalog.indexed_changes("other", &first, None, None, 1).is_err());
        catalog.set_pinned("a", true).unwrap();
        assert!(catalog.indexed_changes("owner", &first, Some(page.revision), Some("a"), 1).is_err());
        let revision = catalog.indexed_changes("owner", &first, None, None, 1).unwrap().revision;
        catalog.begin_expiry(ExpiryRecord { action_id:"a".into(), state:ExpiryState::Expiring, bytes:0, started_millis:1, finished_millis:None, metadata_expired:false }).unwrap();
        assert!(catalog.indexed_changes("owner", &first, Some(revision), None, 1).is_err());
        let read = catalog.db.begin_read().unwrap();
        let meta = read.open_table(META).unwrap();
        let schema: u32 = serde_json::from_slice(meta.get("schema").unwrap().unwrap().value()).unwrap();
        assert_ne!(schema, 1, "legacy writer must refuse format 2");
        drop(meta); drop(read);
        let write = catalog.db.begin_write().unwrap();
        { let mut meta = write.open_table(META).unwrap(); meta.insert("schema", b"1".as_slice()).unwrap(); meta.remove(CHANGE_INDEX_COMPLETE).unwrap(); }
        write.commit().unwrap();
        assert!(catalog.indexed_changes("owner", &first, None, None, 1).is_err());
        catalog.begin_action(action("legacy", &first)).unwrap();
        assert!(catalog.get_action("a").unwrap().is_some());
    }

    #[cfg(unix)]
    #[test]
    fn copy_preparation_legacy_and_resume() {
        use sha2::{Digest, Sha256};
        use std::os::unix::fs::MetadataExt;
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("legacy.redb");
        let target = temp.path().join("prepared.redb");
        let key = resource("file");
        {
            let catalog = Catalog::open(&source, "owner").unwrap();
            let write = catalog.db.begin_write().unwrap();
            { let mut meta = write.open_table(META).unwrap(); meta.insert("schema", b"1".as_slice()).unwrap(); meta.remove(CHANGE_INDEX_COMPLETE).unwrap(); meta.remove(CHANGE_INDEX_POLICY).unwrap(); }
            write.commit().unwrap();
            catalog.begin_action(action("a", &key)).unwrap();
            catalog.begin_action(action("b", &key)).unwrap();
            assert!(catalog.indexed_changes("owner", &key, None, None, 10).is_err());
        }
        let original = std::fs::read(&source).unwrap();
        prepare_change_index_copy(&source, &target, 1).unwrap();
        assert_eq!(std::fs::read(&source).unwrap(), original);
        {
            let prepared = Catalog::open(&target, "owner").unwrap();
            assert_eq!(prepared.indexed_changes("owner", &key, None, None, 10).unwrap().items.len(), 2);
            let read = prepared.db.begin_read().unwrap(); let meta = read.open_table(META).unwrap();
            let schema: u32 = serde_json::from_slice(meta.get("schema").unwrap().unwrap().value()).unwrap();
            assert_eq!(schema, 2); assert_ne!(schema, 1, "legacy writer rejects this format");
        }
        let resume = temp.path().join("resume.redb");
        std::fs::copy(&source, &resume).unwrap();
        {
            let db = Database::open(&resume).unwrap(); let identity = std::fs::metadata(&resume).unwrap();
            let write = db.begin_write().unwrap();
            write.delete_table(CHANGE_INDEX).unwrap(); write.delete_table(CHANGE_REVISIONS).unwrap();
            { let _index = write.open_table(CHANGE_INDEX).unwrap(); let _revisions = write.open_table(CHANGE_REVISIONS).unwrap(); }
            write_change_index(&write, None, &action("a", &key)).unwrap();
            let saved = CopyPreparation {version:1,source_path:source.canonicalize().unwrap().to_str().unwrap().into(),source_digest:format!("{:x}",Sha256::digest(&original)),copy_device:identity.dev(),copy_inode:identity.ino(),last_action:Some("a".into())};
            { let mut meta=write.open_table(META).unwrap();meta.insert(CHANGE_PREPARING,serde_json::to_vec(&saved).unwrap().as_slice()).unwrap(); }
            write.commit().unwrap();
        }
        prepare_change_index_copy(&source, &resume, 1).unwrap();
        assert_eq!(std::fs::read(&source).unwrap(), original);
        let prepared = Catalog::open(&resume, "owner").unwrap();
        assert_eq!(prepared.indexed_changes("owner", &key, None, None, 10).unwrap().items.len(), 2);
    }
}

impl Catalog {
    /// One bounded workspace namespace, merged from existing resource indexes.
    pub fn workspace_recorded_activity(&self, account:&str, workspace:&str, expected_incarnation:Option<&str>, expected_scopes:Option<&str>, after:Option<&str>, fence_only:bool) -> CatalogResult<(String,String,Vec<(String,ResourceKey,ActionRecord,Option<ExpiryRecord>)>,Option<String>)> {
        use base64::{engine::general_purpose::URL_SAFE_NO_PAD,Engine};
        use sha2::{Digest,Sha256};
        if account.is_empty() || workspace.is_empty() || account.len()>512 || workspace.len()>512 || after.is_some() != (expected_incarnation.is_some() && expected_scopes.is_some()) {return Err("workspace cursor unavailable".into());}
        let last:Option<(u64,String,String)> = after.map(|v| -> CatalogResult<_> {if v.is_empty() || v.len()>16384{return Err("workspace cursor unavailable".into())} let decoded=URL_SAFE_NO_PAD.decode(v)?;let value:(u64,String,String)=serde_json::from_slice(&decoded)?;if URL_SAFE_NO_PAD.encode(serde_json::to_vec(&value)?)!=v || value.1.is_empty() || value.1.len()>512 || value.2.is_empty() || value.2.len()>8192 || !value.2.bytes().all(|b|b.is_ascii_hexdigit()) {return Err("workspace cursor unavailable".into())} Ok(value)}).transpose()?;
        let template=ResourceKey{account_id:account.into(),workspace_id:workspace.into(),provider:"mimo-filesystem".into(),resource_id:String::new()};
        let serialized=serde_json::to_string(&template)?;
        let prefix_json=serialized.strip_suffix("\"resource_id\":\"\"}").ok_or("workspace codec unavailable")?.to_owned()+"\"resource_id\":\"";
        let prefix:String=prefix_json.as_bytes().iter().map(|b|format!("{b:02x}")).collect();
        let read=self.db.begin_read()?;let meta=read.open_table(META)?;
        let schema:u32=serde_json::from_slice(meta.get("schema")?.ok_or("missing schema")?.value())?;
        if schema!=ORIGIN_SCHEMA_VERSION || meta.get(CHANGE_PREPARING)?.is_some() || !meta.get(CHANGE_INDEX_COMPLETE)?.is_some_and(|v|v.value()==b"true") || !meta.get(CHANGE_INDEX_POLICY)?.is_some_and(|v|v.value()==CHANGE_POLICY_VALUE) || !meta.get(ORDERED_POLICY)?.is_some_and(|v|v.value()==ORDERED_POLICY_VALUE) {return Err("workspace profile unavailable".into());}
        let incarnation=std::str::from_utf8(meta.get(ORIGIN_INCARNATION)?.ok_or("workspace incarnation unavailable")?.value())?.to_owned();
        if incarnation.len()!=64 || !incarnation.bytes().all(|v|v.is_ascii_hexdigit()) {return Err("workspace incarnation unavailable".into())}
        let index=read.open_table(CHANGE_INDEX)?;let revisions=read.open_table(CHANGE_REVISIONS)?;
        let mut scopes=Vec::new();let mut seek=prefix.clone();
        loop {let mut range=index.range(seek.as_str()..)?;let Some(next)=range.next() else{break};let (key,_)=next?;let key=key.value();if !key.starts_with(&prefix){break}
            let scope=key.split_once(':').ok_or("workspace scope unavailable")?.0.to_owned();
            if scope.len()%2!=0 || scope.len()>8192 || !scope.bytes().all(|b|b.is_ascii_hexdigit()) {return Err("workspace scope unavailable".into())}
            let bytes=(0..scope.len()).step_by(2).map(|i|u8::from_str_radix(&scope[i..i+2],16)).collect::<Result<Vec<_>,_>>()?;
            let resource:ResourceKey=serde_json::from_slice(&bytes)?;
            if resource.account_id!=account || resource.workspace_id!=workspace || resource.provider!="mimo-filesystem" || change_scope(&resource)?!=scope {return Err("workspace scope unavailable".into())}
            let revision=serde_json::from_slice::<u64>(revisions.get(scope.as_str())?.ok_or("workspace revision unavailable")?.value())?;
            scopes.push((scope.clone(),resource,revision));if scopes.len()>32{return Err("workspace scope overflow".into())}seek=format!("{scope};");
        }
        let scope_digest=format!("{:x}",Sha256::digest(serde_json::to_vec(&scopes.iter().map(|(s,_,r)|(s,r)).collect::<Vec<_>>())?));
        if last.as_ref().is_some_and(|(_,id,scope)|id.is_empty() || id.len()>512 || !scopes.iter().any(|(known,_,_)|known==scope)) {return Err("workspace cursor unavailable".into())}
        if expected_incarnation.is_some_and(|v|v!=incarnation) || expected_scopes.is_some_and(|v|v!=scope_digest) {return Err("stale workspace cursor".into())}
        if fence_only {return Ok((incarnation,scope_digest,Vec::new(),None))}
        let time=read.open_table(TIME_INDEX)?;let mut entries=Vec::new();
        for (scope,resource,_) in &scopes {let p=format!("{scope}:");let start=last.as_ref().map(|(stamp,id,_)|format!("{p}{stamp:016x}:{id}")).unwrap_or_else(||p.clone());let mut count=0;
            for item in time.range(start.as_str()..)? {let (key,value)=item?;let key=key.value();if !key.starts_with(&p){break}let rest=&key[p.len()..];let (stamp,id)=rest.split_once(':').ok_or("workspace index unavailable")?;if stamp.len()!=16 || !stamp.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) || id.is_empty() || id.len()>512 {return Err("workspace index unavailable".into())}let stamp=u64::from_str_radix(stamp,16)?;let tuple=(stamp,id.to_owned(),scope.clone());if last.as_ref().is_some_and(|v|&tuple<=v){continue}if std::str::from_utf8(value.value())?!=id{return Err("workspace index unavailable".into())}entries.push((tuple,resource.clone(),key.to_owned()));count+=1;if count>100{break}}
        }
        entries.sort_by(|a,b|a.0.cmp(&b.0));let more=entries.len()>100;entries.truncate(100);
        let actions=read.open_table(ACTIONS)?;let expiry=read.open_table(EXPIRY)?;let mut items=Vec::new();let mut total=0usize;
        for (position,resource,key) in entries {let raw=actions.get(position.1.as_str())?.ok_or("workspace action unavailable")?;total=total.checked_add(raw.value().len()).ok_or("workspace page unavailable")?;if raw.value().len()>1_000_000 || total>4_000_000{return Err("workspace page unavailable".into())}let action:ActionRecord=serde_json::from_slice(raw.value())?;if action.action_id!=position.1 || action.actor_account_id!=account || action.timestamp_millis!=Some(position.0) || !ordered_keys(&action)?.0.contains(&key) || !action_resources(&action).contains(&resource){return Err("workspace index inconsistent".into())}let state=expiry.get(action.action_id.as_str())?.map(|v|serde_json::from_slice::<ExpiryRecord>(v.value())).transpose()?;let encoded=URL_SAFE_NO_PAD.encode(serde_json::to_vec(&position)?);items.push((encoded,resource,action,state));}
        let next=if more{items.last().map(|item|item.0.clone())}else{None};Ok((incarnation,scope_digest,items,next))
    }
    pub fn checkpoint_schema(&self) -> CatalogResult<Option<u32>> {
        let read = self.db.begin_read()?;
        let meta = read.open_table(META)?;
        let schema: u32 = serde_json::from_slice(meta.get("schema")?.ok_or("missing catalog schema")?.value())?;
        Ok(matches!(schema,CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION).then_some(schema))
    }
    pub fn checkpoint_profile(&self) -> CatalogResult<bool> { Ok(self.checkpoint_schema()?.is_some()) }
    pub fn cross_origin_fence(&self,account:&str,resource:&ResourceKey,action_id:&str)->CatalogResult<(String,u64)> {
        if account!=resource.account_id || action_id.is_empty() || action_id.len()>512 {return Err("origin unavailable".into());}
        let read=self.db.begin_read()?;let meta=read.open_table(META)?;
        let schema:u32=serde_json::from_slice(meta.get("schema")?.ok_or("missing schema")?.value())?;
        if schema!=ORIGIN_SCHEMA_VERSION || meta.get(CHANGE_PREPARING)?.is_some()
            || !meta.get(CHANGE_INDEX_COMPLETE)?.is_some_and(|v|v.value()==b"true")
            || !meta.get(CHANGE_INDEX_POLICY)?.is_some_and(|v|v.value()==CHANGE_POLICY_VALUE)
            || !meta.get(ORDERED_POLICY)?.is_some_and(|v|v.value()==ORDERED_POLICY_VALUE)
            || !meta.get(ORIGIN_INCARNATION)?.is_some_and(|v|v.value().len()==64 && v.value().iter().all(u8::is_ascii_hexdigit))
            {return Err("origin profile unavailable".into());}
        for table in [ACTIONS,POLICIES,RESERVATIONS,EXPIRY,RESTORES,CHANGE_INDEX,CHANGE_REVISIONS,TIME_INDEX,ORIGIN_INDEX,ORIGIN_TIME_INDEX] {
            read.open_table(table).map_err(|_|"origin profile table unavailable")?;
        }
        let incarnation=std::str::from_utf8(meta.get(ORIGIN_INCARNATION)?.ok_or("origin unavailable")?.value())?.to_owned();
        let actions=read.open_table(ACTIONS)?;let raw=actions.get(action_id)?.ok_or("origin unavailable")?;
        if raw.value().len()>1_000_000{return Err("origin unavailable".into());}
        let action:ActionRecord=serde_json::from_slice(raw.value())?;
        if action.action_id!=action_id || action.actor_account_id!=account || !action_resources(&action).contains(&canonical_change_resource(resource)){return Err("origin unavailable".into());}
        let origin=origin_scope(&action)?.ok_or("origin unavailable")?;
        let revision=meta.get(format!("{ORIGIN_REVISION_PREFIX}{origin}").as_str())?.map(|v|serde_json::from_slice::<u64>(v.value())).transpose()?.unwrap_or(0);
        Ok((incarnation,revision))
    }

    /// Private profile-5 origin-prefix scan. Resource keys never become public
    /// until the caller independently authorizes each one.
    pub fn cross_origin_changes(&self,account:&str,resource:&ResourceKey,action_id:&str,expected_incarnation:Option<&str>,expected_revision:Option<u64>,after:Option<&str>)->CatalogResult<CrossOriginPage> {
        if account!=resource.account_id || action_id.is_empty() || action_id.len()>512 || after.is_some_and(|x|x.len()>8192 || x.is_empty())
            || after.is_some()!=(expected_incarnation.is_some() && expected_revision.is_some()) {return Err("origin query unavailable".into());}
        let read=self.db.begin_read()?;let meta=read.open_table(META)?;
        let schema:u32=serde_json::from_slice(meta.get("schema")?.ok_or("missing schema")?.value())?;
        if schema!=ORIGIN_SCHEMA_VERSION || meta.get(CHANGE_PREPARING)?.is_some()
            || !meta.get(CHANGE_INDEX_COMPLETE)?.is_some_and(|v|v.value()==b"true")
            || !meta.get(CHANGE_INDEX_POLICY)?.is_some_and(|v|v.value()==CHANGE_POLICY_VALUE)
            || !meta.get(ORDERED_POLICY)?.is_some_and(|v|v.value()==ORDERED_POLICY_VALUE)
            || !meta.get(ORIGIN_INCARNATION)?.is_some_and(|v|v.value().len()==64 && v.value().iter().all(u8::is_ascii_hexdigit))
            {return Err("origin profile unavailable".into());}
        for table in [ACTIONS,POLICIES,RESERVATIONS,EXPIRY,RESTORES,CHANGE_INDEX,CHANGE_REVISIONS,TIME_INDEX,ORIGIN_INDEX,ORIGIN_TIME_INDEX] {
            read.open_table(table).map_err(|_|"origin profile table unavailable")?;
        }
        let incarnation=std::str::from_utf8(meta.get(ORIGIN_INCARNATION)?.ok_or("origin incarnation unavailable")?.value())?.to_owned();
        let actions=read.open_table(ACTIONS)?;let source=actions.get(action_id)?.ok_or("origin selector unavailable")?;
        if source.value().len()>1_000_000{return Err("origin selector unavailable".into());}
        let selected:ActionRecord=serde_json::from_slice(source.value())?;
        if selected.action_id!=action_id || selected.actor_account_id!=account || !action_resources(&selected).contains(&canonical_change_resource(resource)) {return Err("origin selector unavailable".into());}
        let origin=origin_scope(&selected)?.ok_or("origin selector unavailable")?;let prefix=format!("{origin}:");
        let revision=meta.get(format!("{ORIGIN_REVISION_PREFIX}{origin}").as_str())?.map(|v|serde_json::from_slice::<u64>(v.value())).transpose()?.unwrap_or(0);
        if expected_incarnation.is_some_and(|v|v!=incarnation) || expected_revision.is_some_and(|v|v!=revision) || after.is_some_and(|v|!v.starts_with(&prefix)) {return Err("stale origin cursor".into());}
        let index=read.open_table(ORIGIN_TIME_INDEX)?;let expiry=read.open_table(EXPIRY)?;let mut items=Vec::new();let mut last=None;let mut total=0usize;
        for row in index.range(after.unwrap_or(&prefix)..)? {let (key,value)=row?;let key=key.value();if !key.starts_with(&prefix){break;}if after==Some(key){continue;}
            let id=std::str::from_utf8(value.value())?;let raw=actions.get(id)?.ok_or("origin index unavailable")?;
            total=total.checked_add(raw.value().len()).ok_or("origin page unavailable")?;
            if raw.value().len()>1_000_000 || total>4_000_000{return Err("origin page unavailable".into());}
            let action:ActionRecord=serde_json::from_slice(raw.value())?;
            if action.action_id!=id || action.actor_account_id!=selected.actor_account_id || action.resource_key.workspace_id!=selected.resource_key.workspace_id
                || action.managed_tool_origin_v1.as_ref().map(|v|(&v.host_chat_id,&v.engine_session_id,&v.root_operation_id,&v.actor_id,v.execution_revision,&v.inner_turn_id))
                    !=selected.managed_tool_origin_v1.as_ref().map(|v|(&v.host_chat_id,&v.engine_session_id,&v.root_operation_id,&v.actor_id,v.execution_revision,&v.inner_turn_id)) {return Err("origin index unavailable".into());}
            let keys=origin_timeline_keys(&action)?;
            let selected_resource=keys.get(key).cloned().ok_or("origin resource unavailable")?;
            let expired=expiry.get(id)?.map(|v|serde_json::from_slice::<ExpiryRecord>(v.value())).transpose()?;
            items.push((key.to_owned(),selected_resource,action,expired));last=Some(key.to_owned());if items.len()==100{return Ok(CrossOriginPage{incarnation,revision,items,next:last});}
        }
        Ok(CrossOriginPage{incarnation,revision,items,next:None})
    }

    pub fn ordered_changes(&self,account:&str,resource:&ResourceKey,selector:Option<&str>,expected:Option<u64>,after:Option<&str>,limit:usize)->CatalogResult<OrderedChangePage> {
        if account!=resource.account_id || !(1..=100).contains(&limit) || after.is_some_and(|v|v.len()>8192 || v.is_empty()) || after.is_some()&&expected.is_none() {return Err("invalid ordered query".into());}
        let scope=change_scope(resource)?;let read=self.db.begin_read()?;let meta=read.open_table(META)?;
        let schema:u32=serde_json::from_slice(meta.get("schema")?.ok_or("missing schema")?.value())?;
        if !matches!(schema, ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) || !meta.get(ORDERED_POLICY)?.is_some_and(|v|v.value()==ORDERED_POLICY_VALUE) {return Err("ordered index unavailable".into());}
        let revisions=read.open_table(CHANGE_REVISIONS)?;let revision=revisions.get(scope.as_str())?.map(|v|serde_json::from_slice::<u64>(v.value())).transpose()?.unwrap_or(0);
        if expected.is_some_and(|v|v!=revision) {return Err("stale change cursor".into());}
        let actions=read.open_table(ACTIONS)?;let expiry=read.open_table(EXPIRY)?;
        let selected=if let Some(id)=selector {let data=actions.get(id)?.ok_or("origin selector unavailable")?;let row:ActionRecord=serde_json::from_slice(data.value())?;if row.action_id!=id || row.actor_account_id!=account || !action_resources(&row).contains(&canonical_change_resource(resource)) || origin_scope(&row)?.is_none(){return Err("origin selector unavailable".into());}Some(row)}else{None};
        let prefix=if let Some(row)=&selected {format!("{}:{scope}:",origin_scope(row)?.ok_or("origin unavailable")?)} else {format!("{scope}:")};
        if after.is_some_and(|v|!v.starts_with(&prefix)){return Err("invalid ordered cursor".into());}
        let start=after.unwrap_or(&prefix);let index=if selected.is_some(){read.open_table(ORIGIN_INDEX)?}else{read.open_table(TIME_INDEX)?};
        let mut items=Vec::new();let mut last=None;let mut total=0usize;
        for entry in index.range(start..)? {let (key,value)=entry?;let key=key.value();if !key.starts_with(&prefix){break;}if after==Some(key){continue;}
            let id=std::str::from_utf8(value.value())?;let data=actions.get(id)?.ok_or("inconsistent ordered index")?;
            total=total.checked_add(data.value().len()).ok_or("ordered page exceeds limit")?;if data.value().len()>1_000_000||total>4_000_000{return Err("ordered page exceeds limit".into());}
            let row:ActionRecord=serde_json::from_slice(data.value())?;let (times,origins)=ordered_keys(&row)?;
            if row.action_id!=id || !action_resources(&row).contains(&canonical_change_resource(resource)) || if selected.is_some(){!origins.contains(key)}else{!times.contains(key)} {return Err("inconsistent ordered index".into());}
            if let Some(origin)=&selected {if row.actor_account_id!=origin.actor_account_id || row.resource_key.workspace_id!=origin.resource_key.workspace_id || row.managed_tool_origin_v1.as_ref().map(|v|(&v.host_chat_id,&v.engine_session_id,&v.root_operation_id,&v.actor_id,v.execution_revision,&v.inner_turn_id))!=origin.managed_tool_origin_v1.as_ref().map(|v|(&v.host_chat_id,&v.engine_session_id,&v.root_operation_id,&v.actor_id,v.execution_revision,&v.inner_turn_id)){return Err("origin collision".into());}}
            if items.len()==limit {return Ok(OrderedChangePage{revision,items,next:last});}
            let state=expiry.get(id)?.map(|v|serde_json::from_slice::<ExpiryRecord>(v.value())).transpose()?;items.push((row,state));last=Some(key.to_owned());
        }
        Ok(OrderedChangePage{revision,items,next:None})
    }
    /// Read a bounded action page from one redb snapshot. Cursors must carry
    /// the same resource and revision; a changed catalog invalidates the page.
    pub fn indexed_changes(&self, account_id: &str, resource: &ResourceKey, expected_revision: Option<u64>, after: Option<&str>, limit: usize) -> CatalogResult<IndexedChangePage> {
        if account_id != resource.account_id || !(1..=100).contains(&limit) || after.is_some_and(|v| v.is_empty() || v.len() > 512) ||
            [&resource.account_id, &resource.workspace_id, &resource.provider, &resource.resource_id].iter().any(|v| v.is_empty() || v.len() > 512) {
            return Err("invalid change query scope or cursor".into());
        }
        let scope = change_scope(resource)?;
        let prefix = format!("{scope}:");
        let start = format!("{prefix}{}", after.unwrap_or(""));
        let read = self.db.begin_read()?;
        let meta = read.open_table(META)?;
        let schema: u32 = serde_json::from_slice(meta.get("schema")?.ok_or("missing catalog schema")?.value())?;
        if !matches!(schema, SCHEMA_VERSION | ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) || !meta.get(CHANGE_INDEX_COMPLETE)?.is_some_and(|value| value.value() == b"true") ||
            !meta.get(CHANGE_INDEX_POLICY)?.is_some_and(|value| value.value() == CHANGE_POLICY_VALUE) { return Err("change index incomplete".into()); }
        let revisions = read.open_table(CHANGE_REVISIONS)?;
        let revision = revisions.get(scope.as_str())?.map(|v| serde_json::from_slice::<u64>(v.value())).transpose()?.unwrap_or(0);
        if expected_revision.is_some_and(|value| value != revision) { return Err("stale change cursor".into()); }
        if after.is_some() && expected_revision.is_none() { return Err("change cursor requires revision".into()); }
        let index = read.open_table(CHANGE_INDEX)?;
        let actions = read.open_table(ACTIONS)?;
        let expiry = read.open_table(EXPIRY)?;
        let mut items = Vec::with_capacity(limit + 1);
        let mut total_bytes = 0usize;
        for entry in index.range(start.as_str()..)? {
            let (key, value) = entry?;
            if !key.value().starts_with(&prefix) { break; }
            let id = std::str::from_utf8(value.value())?;
            if id.is_empty() || id.len() > 512 { return Err("change action identity exceeds limit".into()); }
            if after == Some(id) { continue; }
            let stored = actions.get(id)?.ok_or("inconsistent change index")?;
            total_bytes = total_bytes.checked_add(stored.value().len()).ok_or("change page exceeds limit")?;
            if stored.value().len() > 1_000_000 || total_bytes > 4_000_000 { return Err("change page exceeds limit".into()); }
            let action: ActionRecord = serde_json::from_slice(stored.value())?;
            if !action_resources(&action).contains(&canonical_change_resource(resource)) { return Err("inconsistent change resource".into()); }
            let state = expiry.get(id)?.map(|v| serde_json::from_slice::<ExpiryRecord>(v.value())).transpose()?;
            items.push((action, state));
            if items.len() > limit { break; }
        }
        let next_action_id = if items.len() > limit { items.pop(); items.last().map(|item| item.0.action_id.clone()) } else { None };
        Ok(IndexedChangePage { revision, items, next_action_id })
    }

    pub fn open(path: impl AsRef<Path>, account_id: &str) -> CatalogResult<Self> {
        let path = path.as_ref().to_path_buf();
        let existing = path.exists();
        let db = if existing { Database::open(&path)? } else { Database::create(&path)? };
        if existing {
            let read = db.begin_read()?;
            let meta = read.open_table(META).map_err(|_| "missing history catalog metadata; use .clanker/tools/native/history catalog conversion")?;
            let schema = meta.get("schema")?.ok_or("unversioned history catalog; use .clanker/tools/native/history catalog conversion")?;
            let stored: u32 = serde_json::from_slice(schema.value())?;
            if !matches!(stored, SCHEMA_VERSION | LEGACY_SCHEMA_VERSION | ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) { return Err(format!("unsupported history catalog schema {stored}").into()); }
            if meta.get(CHANGE_PREPARING)?.is_some() { return Err("catalog copy preparation incomplete".into()); }
            if stored==ORIGIN_SCHEMA_VERSION && !meta.get(ORIGIN_INCARNATION)?.is_some_and(|v|v.value().len()==64 && v.value().iter().all(u8::is_ascii_hexdigit)) {return Err("origin catalog incarnation unavailable".into());}
            if stored==ORIGIN_SCHEMA_VERSION {read.open_table(ORIGIN_TIME_INDEX).map_err(|_|"origin time index unavailable")?;}
            if matches!(stored, SCHEMA_VERSION | ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) && (!meta.get(CHANGE_INDEX_COMPLETE)?.is_some_and(|v| v.value() == b"true") ||
                !meta.get(CHANGE_INDEX_POLICY)?.is_some_and(|v| v.value() == CHANGE_POLICY_VALUE)) { return Err("change index policy unavailable".into()); }
            for table in [ACTIONS, POLICIES, RESERVATIONS, EXPIRY, RESTORES] {
                read.open_table(table).map_err(|_| "missing current history catalog table; use .clanker/tools/native/history catalog conversion")?;
            }
            if matches!(stored, SCHEMA_VERSION | ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) {
                read.open_table(CHANGE_INDEX).map_err(|_| "missing change index")?;
                read.open_table(CHANGE_REVISIONS).map_err(|_| "missing change revisions")?;
            }
            if matches!(stored, ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) {
                if !meta.get(ORDERED_POLICY)?.is_some_and(|v|v.value()==ORDERED_POLICY_VALUE) {return Err("ordered index policy unavailable".into());}
                read.open_table(TIME_INDEX).map_err(|_|"missing recorded time index")?;
                read.open_table(ORIGIN_INDEX).map_err(|_|"missing origin index")?;
            }
        }
        let catalog = Self {
            db: Arc::new(db),
            path,
            leases: Mutex::new(BTreeMap::new()),
        };
        let partition_key = format!("partition:{account_id}");
        let write = catalog.db.begin_write()?;
        {
            let mut meta = write.open_table(META)?;
            let _memery = write.open_table(MEMERY)?;
            crate::memery_components::initialize(&write)?;
            let _actions = write.open_table(ACTIONS)?;
            let _policies = write.open_table(POLICIES)?;
            let _reservations = write.open_table(RESERVATIONS)?;
            let _expiry = write.open_table(EXPIRY)?;
            let _restores = write.open_table(RESTORES)?;
            if let Some(schema) = meta.get("schema")? {
                let stored: u32 = serde_json::from_slice(schema.value())?;
                if !matches!(stored, SCHEMA_VERSION | LEGACY_SCHEMA_VERSION | ORDERED_SCHEMA_VERSION | CHECKPOINT_SCHEMA_VERSION | ORIGIN_SCHEMA_VERSION) {
                    return Err(format!("unsupported history catalog schema {stored}").into());
                }
            } else {
                if meta.len()? > 0 || _actions.len()? > 0 || _policies.len()? > 0
                    || _reservations.len()? > 0 || _expiry.len()? > 0 || _restores.len()? > 0 {
                    return Err("unversioned populated history catalog; use explicit .clanker/tools conversion".into());
                }
                meta.insert("schema", serde_json::to_vec(&SCHEMA_VERSION)?.as_slice())?;
            }
            if !existing {
                let _change_index = write.open_table(CHANGE_INDEX)?;
                let _change_revisions = write.open_table(CHANGE_REVISIONS)?;
                meta.insert(CHANGE_INDEX_COMPLETE, b"true".as_slice())?;
                meta.insert(CHANGE_INDEX_POLICY, CHANGE_POLICY_VALUE)?;
            }
            let mut wanted = owner_partition(account_id);
            let occupied = meta
                .iter()?
                .filter_map(|entry| {
                    let (key, value) = entry.ok()?;
                    (key.value() != partition_key && key.value().starts_with("partition:"))
                        .then(|| value.value().to_vec())
                })
                .collect::<BTreeSet<_>>();
            let mut attempt = 0u64;
            while occupied.contains(wanted.as_slice()) {
                attempt = attempt
                    .checked_add(1)
                    .ok_or("owner partition allocation exhausted")?;
                wanted = owner_partition_seed(account_id, attempt);
            }
            if meta.get(partition_key.as_str())?.is_none() {
                meta.insert(partition_key.as_str(), wanted.as_slice())?;
            }
        }
        write.commit()?;
        Ok(catalog)
    }

    /// Read the durable policy collection. Missing settings use the frozen
    /// default and are materialized on first update, so a restart cannot lose
    /// a user's selected target.
    pub fn policy_set(&self) -> CatalogResult<PolicySet> {
        let read = self.db.begin_read()?;
        let table = read.open_table(POLICIES)?;
        Ok(table
            .get("policy")?
            .map(|value| serde_json::from_slice(value.value()))
            .transpose()?
            .unwrap_or_default())
    }

    pub fn set_policy(
        &self,
        expected_revision: u64,
        policy: PolicySet,
    ) -> CatalogResult<PolicySet> {
        policy.validate()?;
        let write = self.db.begin_write()?;
        let mut table = write.open_table(POLICIES)?;
        let current = table
            .get("policy")?
            .map(|value| serde_json::from_slice::<PolicySet>(value.value()))
            .transpose()?
            .unwrap_or_default();
        if current.revision != expected_revision {
            return Err(CatalogConflict::PolicyRevisionMismatch.into());
        }
        let mut next = policy;
        next.revision = current.revision.saturating_add(1);
        next.global.revision = next.revision;
        // A pending capture cannot reappear after exclusion and re-enabling,
        // even across restarts. Fence only owners whose capture choices changed.
        let changed_at = chrono::Utc::now().timestamp_millis().to_be_bytes();
        if current.global.enabled != next.global.enabled || current.capture_defaults != next.capture_defaults {
            table.insert("memery_policy_changed_at", changed_at.as_slice())?;
            table.insert("capture_epoch", next.revision.to_le_bytes().as_slice())?;
        }
        for owner in current.accounts.keys().chain(next.accounts.keys()).collect::<BTreeSet<_>>() {
            if current.accounts.get(owner) != next.accounts.get(owner) {
                table.insert(format!("memery_policy_changed_at:{owner}").as_str(), changed_at.as_slice())?;
                table.insert(format!("capture_epoch:{owner}").as_str(), next.revision.to_le_bytes().as_slice())?;
            }
        }
        // An in-flight mutation still needs its original preimage. If the
        // owner's choices change, finish it privately and retire on settlement.
        if current.global.enabled != next.global.enabled || current.capture_defaults != next.capture_defaults || current.accounts != next.accounts {
            let mut actions = write.open_table(ACTIONS)?;
            let mut changes = Vec::new();
            for entry in actions.iter()? {
                let (_, raw) = entry?;
                let row: ActionRecord = serde_json::from_slice(raw.value())?;
                if row.recovery_only || !row.requires_recovery() || matches!(row.state, ActionState::Complete |
                    ActionState::Aborted | ActionState::Conflict | ActionState::Failed | ActionState::Partial) { continue; }
                if current.capture_for(&row.actor_account_id) != next.capture_for(&row.actor_account_id) {
                    changes.push(row);
                }
            }
            for old in changes {
                let mut row = old.clone();
                row.recovery_only = true;
                actions.insert(row.action_id.as_str(), serde_json::to_vec(&row)?.as_slice())?;
                update_change_index(&write, Some(&old), &row)?;
            }
        }
        table.insert("policy", serde_json::to_vec(&next)?.as_slice())?;
        drop(table);
        write.commit()?;
        Ok(next)
    }

    pub fn last_capture_status(&self, owner: &str) -> CatalogResult<Option<String>> {
        let read = self.db.begin_read()?;
        let table = read.open_table(POLICIES)?;
        Ok(table.get(format!("last_capture:{owner}").as_str())?
            .map(|value| serde_json::from_slice(value.value())).transpose()?)
    }

    pub fn check_capture(&self, action: &ActionRecord, policy: &PolicySet) -> CatalogResult<()> {
        if action.recovery_only && action.requires_recovery() { return Ok(()); }
        if !policy.permits_action(action) { return Err(BudgetError::Disabled.into()); }
        let read = self.db.begin_read()?;
        let table = read.open_table(POLICIES)?;
        for key in ["capture_epoch".to_owned(), format!("capture_epoch:{}", action.actor_account_id)] {
            if let Some(value) = table.get(key.as_str())? {
                let epoch = u64::from_le_bytes(value.value().try_into()?);
                if action.capture_policy_revision < epoch { return Err(BudgetError::Disabled.into()); }
            }
        }
        Ok(())
    }

    /// Atomically reserve content plus metadata/staging headroom. The
    /// reservation row is the concurrency boundary: two writers cannot both
    /// spend the same remaining target after a restart.
    pub fn reserve_capture(
        &self,
        reservation: PersistedReservation,
        physical_allocated_bytes: u64,
    ) -> CatalogResult<()> {
        self.reserve_capture_scoped(
            reservation,
            physical_allocated_bytes,
            physical_allocated_bytes,
        )
    }

    pub fn reserve_capture_scoped(
        &self,
        reservation: PersistedReservation,
        global_physical_bytes: u64,
        scope_logical_bytes: u64,
    ) -> CatalogResult<()> {
        let policy = self.policy_set()?;
        let effective = policy
            .scopes
            .iter()
            .find(|scope| scope.scope_id == reservation.scope_id)
            .map(|scope| scope.limit_bytes.unwrap_or(policy.global.total_bytes))
            .unwrap_or(policy.global.total_bytes);
        if reservation.policy_revision != policy.revision {
            return Err(CatalogConflict::PolicyRevisionMismatch.into());
        }
        if !policy.global.enabled {
            let action = reservation.reservation_id.rsplit_once(':').map(|(id, _)| id);
            if !action.map(|id| self.get_action(id)).transpose()?.flatten()
                .is_some_and(|record| record.recovery_only && record.requires_recovery()) {
                return Err(BudgetError::Disabled.into());
            }
        }
        let write = self.db.begin_write()?;
        let mut table = write.open_table(RESERVATIONS)?;
        let existing_bytes = table
            .get(reservation.reservation_id.as_str())?
            .map(|value| value.value().to_vec());
        if let Some(existing_bytes) = existing_bytes {
            let existing: PersistedReservation = serde_json::from_slice(&existing_bytes)?;
            if existing == reservation {
                drop(table);
                write.commit()?;
                return Ok(());
            }
            return Err(BudgetError::ReservationConflict.into());
        }
        let global_scope = reservation.scope_id == "global";
        let reservations = table
            .iter()?
            .filter_map(|item| item.ok())
            .filter_map(|(_, value)| {
                serde_json::from_slice::<PersistedReservation>(value.value()).ok()
            })
            .collect::<Vec<_>>();
        let all_reserved = reservations
            .iter()
            .try_fold(0u64, |sum, item| sum.checked_add(item.total_bytes()))
            .ok_or(BudgetError::Arithmetic)?;
        let scoped_reserved = reservations
            .iter()
            .filter(|item| global_scope || item.scope_id == reservation.scope_id)
            .try_fold(0u64, |sum, item| sum.checked_add(item.total_bytes()))
            .ok_or(BudgetError::Arithmetic)?;
        let global_used = global_physical_bytes
            .checked_add(all_reserved)
            .and_then(|value| value.checked_add(reservation.total_bytes()))
            .ok_or(BudgetError::Arithmetic)?;
        let scoped_used = scope_logical_bytes
            .checked_add(scoped_reserved)
            .and_then(|value| value.checked_add(reservation.total_bytes()))
            .ok_or(BudgetError::Arithmetic)?;
        if global_used > policy.global.total_bytes {
            return Err(BudgetError::ExceedsGlobal {
                requested: reservation.total_bytes(),
                available: policy
                    .global
                    .total_bytes
                    .saturating_sub(global_physical_bytes.saturating_add(all_reserved)),
            }
            .into());
        }
        if !global_scope && scoped_used > effective {
            return Err(BudgetError::ExceedsScope {
                requested: reservation.total_bytes(),
                available: effective
                    .saturating_sub(scope_logical_bytes.saturating_add(scoped_reserved)),
            }
            .into());
        }
        table.insert(
            reservation.reservation_id.as_str(),
            serde_json::to_vec(&reservation)?.as_slice(),
        )?;
        drop(table);
        write.commit()?;
        Ok(())
    }

    pub fn release_capture_reservation(&self, reservation_id: &str) -> CatalogResult<bool> {
        let write = self.db.begin_write()?;
        let mut table = write.open_table(RESERVATIONS)?;
        let removed = table.remove(reservation_id)?.is_some();
        drop(table);
        write.commit()?;
        Ok(removed)
    }

    /// Check the persisted after-capture reservation against one exact
    /// Source-owned Redo action. This does not release or recreate it.
    pub(crate) fn checked_worktree_redo_reservation(&self, action_id:&str, inverse_id:&str,
        inverse_version:&str, bytes:u64, scope:&str, policy_revision:u64) -> CatalogResult<PersistedReservation> {
        let read=self.db.begin_read()?;
        let action:ActionRecord={let table=read.open_table(ACTIONS)?;let value=table.get(action_id)?.ok_or("worktree redo unavailable")?;serde_json::from_slice(value.value())?};
        let marker=action.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.get("source_worktree_redo_v2")).ok_or("worktree redo unavailable")?;
        if action.operation!="source_worktree_redo_v2" || action.actor_kind!="human"
            || action.actor_account_id!=action.resource_key.account_id
            || !matches!(action.state,ActionState::BeforeDurable|ActionState::Applying|ActionState::Applied|ActionState::CapturingAfter)
            || marker.get("inverse_action_id").and_then(serde_json::Value::as_str)!=Some(inverse_id)
            || marker.get("inverse_version_id").and_then(serde_json::Value::as_str)!=Some(inverse_version)
            || marker.get("container_length").and_then(serde_json::Value::as_u64)!=Some(bytes) {
            return Err("worktree redo unavailable".into());
        }
        let inverse:ActionRecord={let table=read.open_table(ACTIONS)?;let value=table.get(inverse_id)?.ok_or("worktree inverse unavailable")?;serde_json::from_slice(value.value())?};
        if inverse.action_id==action.action_id || inverse.operation!="source_worktree_inverse_v2" || inverse.state!=ActionState::Complete
            || inverse.resource_key!=action.resource_key || inverse.actor_account_id!=action.actor_account_id || inverse.before_resources.len()!=1
            || inverse.before_resources[0].resource_key!=inverse.resource_key
            || inverse.before_resources[0].before.as_ref()!=inverse.before.as_ref()
            || !inverse.before.as_ref().is_some_and(|v|v.version_id==inverse_version && matches!(&v.content,VersionContent::Bytes(_))) {
            return Err("worktree inverse changed".into());
        }
        let original_proof=inverse.before_resources[0].coverage.metadata.as_ref().and_then(|v|v.get("source_worktree_tree_v2_after")).ok_or("worktree inverse proof unavailable")?;
        if original_proof.get("container_length").and_then(serde_json::Value::as_u64)!=Some(bytes)
            || original_proof.get("container_sha256")!=marker.get("container_sha256")
            || marker.get("container_sha256").and_then(serde_json::Value::as_str).is_none_or(|v|v.len()!=64 || !v.bytes().all(|c|c.is_ascii_digit() || (b'a'..=b'f').contains(&c))) {
            return Err("worktree inverse proof changed".into());
        }
        {let expiry=read.open_table(EXPIRY)?;if expiry.get(inverse_id)?.is_some(){return Err("worktree inverse expired".into());}}
        let table=read.open_table(RESERVATIONS)?;
        let key=format!("{action_id}:after");let value=table.get(key.as_str())?.ok_or("worktree redo reservation unavailable")?;
        let reservation:PersistedReservation=serde_json::from_slice(value.value())?;
        if reservation.reservation_id!=key || reservation.bytes!=bytes || reservation.scope_id!=scope
            || reservation.policy_revision!=policy_revision {return Err("worktree redo reservation changed".into());}
        Ok(reservation)
    }

    pub fn reserved_inflight_bytes(&self) -> CatalogResult<u64> {
        let read = self.db.begin_read()?;
        let table = read.open_table(RESERVATIONS)?;
        let mut total = 0u64;
        for item in table.iter()? {
            let (_, value) = item?;
            let reservation: PersistedReservation = serde_json::from_slice(value.value())?;
            total = total
                .checked_add(reservation.total_bytes())
                .ok_or("reservation total overflow")?;
        }
        Ok(total)
    }

    /// Drop reservations whose owning action reached a terminal state before the process died.
    /// Capturing states remain reserved so a reopened worker can resume them safely.
    pub fn reconcile_reservations(&self) -> CatalogResult<u64> {
        let actions = self.list_actions()?;
        let known = actions
            .iter()
            .map(|action| action.action_id.clone())
            .collect::<BTreeSet<_>>();
        let terminal = actions
            .into_iter()
            .filter(|action| {
                matches!(
                    action.state,
                    ActionState::Complete
                        | ActionState::CheckpointComplete | ActionState::CheckpointFailed
                        | ActionState::Aborted
                        | ActionState::Failed
                        | ActionState::Conflict
                        | ActionState::Partial
                )
            })
            .map(|action| action.action_id)
            .collect::<BTreeSet<_>>();
        let write = self.db.begin_write()?;
        let mut table = write.open_table(RESERVATIONS)?;
        let mut stale = Vec::new();
        for item in table.iter()? {
            let (key, _) = item?;
            let action_id = key
                .value()
                .strip_suffix(":before")
                .or_else(|| key.value().strip_suffix(":after"))
                .unwrap_or(key.value());
            if terminal.contains(action_id) || !known.contains(action_id) {
                stale.push(key.value().to_owned());
            }
        }
        let count = stale.len() as u64;
        for key in stale {
            table.remove(key.as_str())?;
        }
        drop(table);
        write.commit()?;
        Ok(count)
    }

    pub fn begin_expiry(&self, record: ExpiryRecord) -> CatalogResult<ExpiryRecord> {
        let write = self.db.begin_write()?;
        let mut table = write.open_table(EXPIRY)?;
        let existing_bytes = table
            .get(record.action_id.as_str())?
            .map(|value| value.value().to_vec());
        if let Some(existing_bytes) = existing_bytes {
            let existing: ExpiryRecord = serde_json::from_slice(&existing_bytes)?;
            drop(table);
            write.commit()?;
            return Ok(existing);
        }
        table.insert(
            record.action_id.as_str(),
            serde_json::to_vec(&record)?.as_slice(),
        )?;
        drop(table);
        touch_change_action(&write, &record.action_id)?;
        write.commit()?;
        Ok(record)
    }

    pub fn complete_expiry(
        &self,
        action_id: &str,
        finished_millis: u64,
        metadata_expired: bool,
    ) -> CatalogResult<ExpiryRecord> {
        let write = self.db.begin_write()?;
        let mut table = write.open_table(EXPIRY)?;
        let value = table
            .get(action_id)?
            .ok_or("unknown expiry record")?
            .value()
            .to_vec();
        let mut record: ExpiryRecord = serde_json::from_slice(&value)?;
        record.state = ExpiryState::Expired;
        record.finished_millis = Some(finished_millis);
        record.metadata_expired = metadata_expired;
        table.insert(action_id, serde_json::to_vec(&record)?.as_slice())?;
        drop(table);
        touch_change_action(&write, action_id)?;
        write.commit()?;
        Ok(record)
    }

    pub fn expiry(&self, action_id: &str) -> CatalogResult<Option<ExpiryRecord>> {
        let read = self.db.begin_read()?;
        let table = read.open_table(EXPIRY)?;
        table
            .get(action_id)?
            .map(|value| serde_json::from_slice(value.value()).map_err(Into::into))
            .transpose()
    }

    pub fn expiring_actions(&self) -> CatalogResult<Vec<String>> {
        let read = self.db.begin_read()?;
        let table = read.open_table(EXPIRY)?;
        Ok(table
            .iter()?
            .filter_map(|item| item.ok())
            .filter_map(|(key, value)| {
                let record = serde_json::from_slice::<ExpiryRecord>(value.value()).ok()?;
                (record.state == ExpiryState::Expiring).then(|| key.value().to_owned())
            })
            .collect::<Vec<_>>())
    }

    /// Store a restore journal entry in the same redb transaction domain as
    /// actions and leases. Generic serde keeps the public recovery DTO owned
    /// by `restore.rs` while making the state durable across worker restarts.
    pub fn put_restore<T: Serialize>(&self, restore_id: &str, value: &T) -> CatalogResult<()> {
        let write = self.db.begin_write()?;
        let mut table = write.open_table(RESTORES)?;
        table.insert(restore_id, serde_json::to_vec(value)?.as_slice())?;
        drop(table);
        write.commit()?;
        Ok(())
    }

    /// Atomic insert-if-absent or exact-value replacement for the private
    /// typed restore journal. Legacy put_restore retains its old behavior.
    pub fn compare_restore_exact<T: Serialize>(&self, restore_id:&str, expected:Option<&T>, value:&T) -> CatalogResult<bool> {
        let expected=expected.map(serde_json::to_vec).transpose()?;
        let value=serde_json::to_vec(value)?;
        let write=self.db.begin_write()?;
        let changed={
            let mut table=write.open_table(RESTORES)?;
            let matches=table.get(restore_id)?.as_ref().map(|v|v.value().to_vec())==expected;
            if matches {table.insert(restore_id,value.as_slice())?;}
            matches
        };
        write.commit()?;Ok(changed)
    }

    pub fn get_restore<T: DeserializeOwned>(&self, restore_id: &str) -> CatalogResult<Option<T>> {
        let read = self.db.begin_read()?;
        let table = read.open_table(RESTORES)?;
        table
            .get(restore_id)?
            .map(|value| serde_json::from_slice(value.value()).map_err(Into::into))
            .transpose()
    }

    pub fn owner_partition(&self, account_id: &str) -> CatalogResult<[u8; 16]> {
        let key = format!("partition:{account_id}");
        let read = self.db.begin_read()?;
        let table = read.open_table(META)?;
        let value = table.get(key.as_str())?.ok_or("missing owner partition")?;
        let bytes = value.value();
        bytes
            .try_into()
            .map_err(|_| "invalid owner partition".into())
    }

    /// Register an immutable resource-owner partition before a shared edit captures bytes.
    pub fn ensure_owner_partition(&self, account_id: &str) -> CatalogResult<[u8; 16]> {
        let key = format!("partition:{account_id}");
        let write = self.db.begin_write()?;
        let selected = {
            let mut meta = write.open_table(META)?;
            if let Some(value) = meta.get(key.as_str())? {
                value
                    .value()
                    .try_into()
                    .map_err(|_| "invalid owner partition")?
            } else {
                let occupied = meta
                    .iter()?
                    .filter_map(|entry| {
                        let (name, value) = entry.ok()?;
                        (name.value().starts_with("partition:")).then(|| value.value().to_vec())
                    })
                    .collect::<BTreeSet<_>>();
                let mut attempt = 0u64;
                let selected = loop {
                    let candidate = owner_partition_seed(account_id, attempt);
                    if !occupied.contains(candidate.as_slice()) {
                        break candidate;
                    }
                    attempt = attempt
                        .checked_add(1)
                        .ok_or("owner partition allocation exhausted")?;
                };
                meta.insert(key.as_str(), selected.as_slice())?;
                selected
            }
        };
        write.commit()?;
        Ok(selected)
    }

    pub fn begin_action(&self, mut record: ActionRecord) -> CatalogResult<BeginResult> {
        let write = self.db.begin_write()?;
        let existing = {
            let table = write.open_table(ACTIONS)?;
            table
                .get(record.action_id.as_str())?
                .map(|v| serde_json::from_slice(v.value()))
        };
        if let Some(existing) = existing {
            let existing: ActionRecord = existing?;
            write.commit()?;
            if existing.actor_id != record.actor_id {
                return Err(CatalogConflict::ActorMismatch.into());
            }
            if existing.request_digest != record.request_digest {
                return Err(CatalogConflict::ActionDigestMismatch.into());
            }
            if existing.actor_account_id != record.actor_account_id
                || existing.resource_key != record.resource_key
                || existing.operation != record.operation
                || existing.physical_lease_keys != record.physical_lease_keys
                || existing.guard_resource_ids != record.guard_resource_ids
                || existing.modified_resource_ids != record.modified_resource_ids
                || existing.expected_revision != record.expected_revision
            {
                return Err(CatalogConflict::ActorMismatch.into());
            }
            return Ok(BeginResult::Existing(existing));
        }
        let policy = write.open_table(POLICIES)?.get("policy")?
            .map(|bytes| serde_json::from_slice::<PolicySet>(bytes.value())).transpose()?.unwrap_or_default();
        let now = chrono::Utc::now().timestamp_millis().max(0) as u64;
        let capture = policy.capture_for(&record.actor_account_id);
        let checkpoint = matches!(record.operation.as_str(), "selected_checkpoint_v1" | "checkpoint");
        let last = write.open_table(POLICIES)?.get(record.capture_clock_key()?.as_str())?
            .map(|v| <[u8; 8]>::try_from(v.value()).map(u64::from_le_bytes)).transpose()?.unwrap_or(0);
        let in_interval = capture.frequency == "interval" && last > 0 && now.saturating_sub(last) < capture.interval_seconds.saturating_mul(1000);
        let publish = policy.permits_action(&record) && (checkpoint || (capture.frequency != "manual" && !in_interval));
        if !publish {
            if record.requires_recovery() {
                record.recovery_only = true;
            } else {
                let summary = format!("Skipped by capture settings at {}", chrono::Utc::now().to_rfc3339());
                write.open_table(POLICIES)?.insert(format!("last_capture:{}", record.actor_account_id).as_str(),
                    serde_json::to_vec(&summary)?.as_slice())?;
                write.commit()?;
                return Err(BudgetError::Disabled.into());
            }
        }
        record.captured_at_millis = now;
        record.capture_policy_revision = policy.revision;
        let encoded = serde_json::to_vec(&record)?;
        {
            let mut table = write.open_table(ACTIONS)?;
            table.insert(record.action_id.as_str(), encoded.as_slice())?;
        }
        update_change_index(&write, None, &record)?;
        write.commit()?;
        Ok(BeginResult::New(record))
    }

    pub(crate) fn begin_checkpoint(&self, mut record: ActionRecord) -> CatalogResult<BeginResult> {
        if !self.checkpoint_profile()? || record.operation != "selected_checkpoint_v1"
            || record.state != ActionState::Intent || record.live.is_some() || !record.before_resources.is_empty() {
            return Err("checkpoint profile or initial state unavailable".into());
        }
        record.state = ActionState::CheckpointPending;
        self.begin_action(record)
    }

    pub(crate) fn complete_checkpoint(&self, action_id: &str, resources: Vec<MutationResource>,
        capture_digest: String, scope_id: String, logical_bytes: u64, started_millis: u64, finished_millis: u64) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.operation != "selected_checkpoint_v1" || record.state != ActionState::CheckpointCapturing
                || record.live.is_some() || record.after.is_some() || !record.before_resources.is_empty()
                || started_millis == 0 || finished_millis < started_millis || resources.is_empty() || resources.len() != record.modified_resource_ids.len()
                || resources.iter().zip(&record.modified_resource_ids).any(|(entry,key)| &entry.resource_key != key || entry.before.is_none()) {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            record.before = resources.first().and_then(|item| item.before.clone());
            record.before_resources = resources;
            record.before_capture_digest = Some(capture_digest);
            record.before_scope_id = Some(scope_id);
            record.before_logical_bytes = logical_bytes;
            let metadata = record.coverage.as_mut().and_then(|c| c.metadata.as_mut()).and_then(|m| m.as_object_mut()).ok_or("checkpoint metadata unavailable")?;
            metadata.insert("started_millis".into(), serde_json::json!(started_millis));
            metadata.insert("finished_millis".into(), serde_json::json!(finished_millis));
            record.state = ActionState::CheckpointFinalizing;
            Ok(())
        })
    }

    pub(crate) fn finish_checkpoint_cleanup(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.operation != "selected_checkpoint_v1" || record.state != ActionState::CheckpointFinalizing
                || record.live.is_some() || record.before_resources.is_empty() {return Err(CatalogConflict::InvalidTransition.into());}
            record.state=ActionState::CheckpointComplete;
            Ok(())
        })
    }

    pub(crate) fn start_checkpoint_capture(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.operation != "selected_checkpoint_v1" || record.state != ActionState::CheckpointPending
                || record.before_scope_id.is_none() { return Err(CatalogConflict::InvalidTransition.into()); }
            record.state = ActionState::CheckpointCapturing;
            Ok(())
        })
    }

    pub(crate) fn checkpoint_reservation(&self, action_id: &str, scope_id: String, logical_bytes: u64) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.operation != "selected_checkpoint_v1" || record.state != ActionState::CheckpointPending
                || record.before_scope_id.is_some() { return Err(CatalogConflict::InvalidTransition.into()); }
            record.before_scope_id = Some(scope_id);
            record.before_logical_bytes = logical_bytes;
            Ok(())
        })
    }

    pub(crate) fn fail_checkpoint(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.operation != "selected_checkpoint_v1" || record.state != ActionState::CheckpointPending
                || !record.before_resources.is_empty() { return Err(CatalogConflict::InvalidTransition.into()); }
            record.state = ActionState::CheckpointFailed;
            Ok(())
        })
    }

    /// Publish known pre-stage failure and release its exact reservation in
    /// one catalog transaction, so observers cannot see a premature terminal.
    pub(crate) fn fail_checkpoint_reserved(&self, action_id: &str, digest: &str) -> CatalogResult<ActionRecord> {
        let write=self.db.begin_write()?;
        let mut record:ActionRecord={let table=write.open_table(ACTIONS)?;let value=table.get(action_id)?.ok_or("checkpoint unavailable")?;serde_json::from_slice(value.value())?};
        if record.operation!="selected_checkpoint_v1" || record.state!=ActionState::CheckpointPending
            || record.request_digest!=digest || record.before_scope_id.is_none() || !record.before_resources.is_empty() {
            return Err(CatalogConflict::InvalidTransition.into());
        }
        let old=record.clone();
        {let mut reservations=write.open_table(RESERVATIONS)?;
            if reservations.remove(format!("{action_id}:before").as_str())?.is_none() {return Err("checkpoint reservation unavailable".into());}}
        record.state=ActionState::CheckpointFailed;
        {let mut actions=write.open_table(ACTIONS)?;actions.insert(action_id,serde_json::to_vec(&record)?.as_slice())?;}
        update_change_index(&write,Some(&old),&record)?;
        write.commit()?;Ok(record)
    }

    pub fn get_action(&self, action_id: &str) -> CatalogResult<Option<ActionRecord>> {
        let read = self.db.begin_read()?;
        let table = read.open_table(ACTIONS)?;
        table
            .get(action_id)?
            .map(|v| serde_json::from_slice(v.value()).map_err(Into::into))
            .transpose()
    }

    /// Rebind the provider resource allocated after a create operation.  The
    /// account, workspace, and provider partition are immutable; only the
    /// opaque resource identity may be filled in while the action is still
    /// before its live receipt.
    pub fn rebind_resource_key(
        &self,
        action_id: &str,
        resource_key: ResourceKey,
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.state != ActionState::BeforeDurable {
                return Err("resource identity can only be rebound before live commit".into());
            }
            if record.resource_key.account_id != resource_key.account_id
                || record.resource_key.workspace_id != resource_key.workspace_id
                || record.resource_key.provider != resource_key.provider
                || resource_key.resource_id.trim().is_empty()
            {
                return Err("resource rebind changed an immutable scope".into());
            }
            record.resource_key = resource_key;
            Ok(())
        })
    }

    pub fn active_actions(&self) -> CatalogResult<Vec<ActionRecord>> {
        Ok(self
            .list_actions()?
            .into_iter()
            .filter(|record| {
                !matches!(
                    record.state,
                    ActionState::Complete
                        | ActionState::CheckpointComplete | ActionState::CheckpointFailed
                        | ActionState::Aborted
                        | ActionState::Failed
                        | ActionState::Conflict
                        | ActionState::Partial
                )
            })
            .collect())
    }

    pub fn all_actions(&self) -> CatalogResult<Vec<ActionRecord>> {
        self.list_actions()
    }

    pub fn mark_recovery_settled(&self, id: &str) -> CatalogResult<ActionRecord> {
        self.update(id, |record| {
            record.recovery_settled = true;
            Ok(())
        })
    }

    pub fn set_pinned(&self, action_id: &str, pinned: bool) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            record.pinned = pinned;
            Ok(())
        })
    }

    pub fn catalog_path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn transition(
        &self,
        action_id: &str,
        expected: ActionState,
        next: ActionState,
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.state != expected || !legal_transition(&expected, &next) {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            record.state = next;
            Ok(())
        })
    }

    pub(crate) fn record_before_durable(
        &self,
        action_id: &str,
        receipt: VersionReceipt,
        capture_digest: String,
        scope_id: String,
        logical_bytes: u64,
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.state != ActionState::CapturingBefore {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            record.before = Some(receipt);
            record.before_capture_digest = Some(capture_digest);
            record.before_scope_id = Some(scope_id);
            record.before_logical_bytes = logical_bytes;
            record.state = ActionState::BeforeDurable;
            Ok(())
        })
    }

    pub(crate) fn record_batch_before_durable(
        &self,
        action_id: &str,
        batch: MutationBatch,
        capture_digest: String,
        scope_id: String,
        logical_bytes: u64,
    ) -> CatalogResult<ActionRecord> {
        if batch.schema_version != 1
            || batch.prepare.batch_version != 1
            || !batch.prepare.acknowledged
            || batch.parent_action_id != action_id
            || batch.resources.is_empty()
            || batch.prepare.resource_count as usize != batch.resources.len()
            || batch.prepare.logical_bytes != logical_bytes
        {
            return Err("batch prepare acknowledgement is incomplete".into());
        }
        self.update(action_id, |record| {
            if record.state != ActionState::CapturingBefore {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            record.before = batch
                .resources
                .first()
                .and_then(|resource| resource.before.clone());
            record.before_resources = batch.resources.clone();
            record.mutation_batch = Some(batch);
            record.before_capture_digest = Some(capture_digest);
            record.before_scope_id = Some(scope_id);
            record.before_logical_bytes = logical_bytes;
            record.state = ActionState::BeforeDurable;
            Ok(())
        })
    }

    pub(crate) fn add_physical_lease_keys(
        &self,
        action_id: &str,
        keys: &[String],
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            for key in keys {
                if !record.physical_lease_keys.contains(key) {
                    record.physical_lease_keys.push(key.clone());
                }
            }
            record.physical_lease_keys.sort();
            Ok(())
        })
    }

    pub(crate) fn record_live(
        &self,
        action_id: &str,
        receipt: LiveReceipt,
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.state != ActionState::Applying || receipt.action_id != record.action_id {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            if !receipt.committed_resources.is_empty() {
                if receipt.status != LiveStatus::Committed
                    || receipt.committed_resources.len() != record.before_resources.len() {
                    return Err("committed proof must cover the exact prepared batch".into());
                }
                let mut seen = std::collections::HashSet::new();
                for proof in &receipt.committed_resources {
                    if !seen.insert(proof.resource_key.resource_id.clone())
                        || !record.before_resources.iter().any(|before| before.resource_key == proof.resource_key)
                        || proof.fingerprint.is_empty()
                        || (proof.existence == ResourceExistence::Present
                            && (proof.coverage.content_digest.is_none() || proof.coverage.byte_len.is_none())) {
                        return Err("invalid committed resource proof".into());
                    }
                }
            }
            record.state = match receipt.status {
                LiveStatus::Committed => ActionState::Applied,
                LiveStatus::NotCommitted => ActionState::Failed,
                LiveStatus::Conflict => ActionState::Conflict,
                LiveStatus::Partial => ActionState::Partial,
                LiveStatus::Unknown => ActionState::NeedsReconciliation,
            };
            record.live = Some(receipt);
            Ok(())
        })
    }

    pub(crate) fn record_after(
        &self,
        action_id: &str,
        receipt: VersionReceipt,
        capture_digest: String,
        scope_id: String,
        logical_bytes: u64,
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.state != ActionState::CapturingAfter
                || !matches!(
                    record.live.as_ref().map(|r| &r.status),
                    Some(LiveStatus::Committed)
                )
            {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            record.after = Some(receipt);
            record.after_capture_digest = Some(capture_digest);
            record.after_scope_id = Some(scope_id);
            record.after_logical_bytes = logical_bytes;
            record.state = ActionState::AfterDurable;
            Ok(())
        })
    }

    pub(crate) fn record_after_batch(
        &self,
        action_id: &str,
        after_resources: Vec<AfterResource>,
        outcomes: Vec<ResourceOutcome>,
        capture_digest: String,
        scope_id: String,
        logical_bytes: u64,
    ) -> CatalogResult<ActionRecord> {
        if after_resources.is_empty() || after_resources.len() != outcomes.len() {
            return Err("after-state batch must contain one outcome per resource".into());
        }
        self.update(action_id, |record| {
            if record.state != ActionState::CapturingAfter
                || !matches!(
                    record.live.as_ref().map(|r| &r.status),
                    Some(LiveStatus::Committed)
                )
            {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            if let Some(live) = &record.live {
                if !live.committed_resources.is_empty() {
                    if live.committed_resources.len() != after_resources.len() {
                        return Err("after capture does not cover committed proof".into());
                    }
                    for proof in &live.committed_resources {
                        if !after_resources.iter().any(|after| after.resource_key == proof.resource_key
                            && after.locator == proof.locator && after.existence == proof.existence
                            && after.resource_type == proof.resource_type && after.metadata == proof.metadata
                            && after.fingerprint == proof.fingerprint && after.coverage == proof.coverage) {
                            return Err("after capture differs from original committed proof".into());
                        }
                    }
                }
            }
            record.after = after_resources.first().map(|resource| resource.after.clone());
            record.after_resources = after_resources;
            record.per_resource_outcomes = Some(outcomes);
            record.after_capture_digest = Some(capture_digest);
            record.after_scope_id = Some(scope_id);
            record.after_logical_bytes = logical_bytes;
            record.state = ActionState::AfterDurable;
            Ok(())
        })
    }

    pub fn bind_complete_request(
        &self,
        action_id: &str,
        request_digest: &str,
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if let Some(existing) = record.complete_request_digest.as_deref() {
                if existing != request_digest {
                    return Err("complete batch retry payload mismatch".into());
                }
            } else {
                record.complete_request_digest = Some(request_digest.to_owned());
            }
            Ok(())
        })
    }

    pub(crate) fn complete(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        self.transition(action_id, ActionState::AfterDurable, ActionState::Complete)
    }

    pub(crate) fn abort(&self, action_id: &str) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if !matches!(
                record.state,
                ActionState::Intent
                    | ActionState::CapturingBefore
                    | ActionState::CaptureFailed
                    | ActionState::BeforeDurable
            ) {
                return Err(CatalogConflict::InvalidTransition.into());
            }
            record.state = ActionState::Aborted;
            Ok(())
        })
    }

    pub(crate) fn reconcile_one(
        &self,
        action_id: &str,
        receipt: LiveReceipt,
    ) -> CatalogResult<ActionRecord> {
        let record = self.get_action(action_id)?.ok_or("unknown action")?;
        if record.operation == "selected_checkpoint_v1" { return Err("checkpoint is not a mutation".into()); }
        let lease = lease_ids(&record);
        self.acquire_leases(action_id, &lease)?;
        let result = self.update(action_id, |current| {
            if receipt.action_id != current.action_id {
                return Err(CatalogConflict::ActorMismatch.into());
            }
            current.live = Some(receipt.clone());
            current.state = match receipt.status {
                LiveStatus::NotCommitted => ActionState::Aborted,
                LiveStatus::Committed
                | LiveStatus::Conflict
                | LiveStatus::Partial
                | LiveStatus::Unknown => ActionState::NeedsReconciliation,
            };
            Ok(())
        });
        self.release_leases(action_id, &lease)?;
        result
    }

    pub(crate) fn reconcile<F>(&self, mut resolve: F) -> CatalogResult<Vec<ActionRecord>>
    where
        F: FnMut(&ActionRecord) -> CatalogResult<LiveReceipt>,
    {
        let mut changed = Vec::new();
        for record in self.list_actions()? {
            if record.operation == "selected_checkpoint_v1" { continue; }
            if matches!(
                record.state,
                ActionState::Complete | ActionState::Aborted | ActionState::Failed
            ) {
                continue;
            }
            let lease = lease_ids(&record);
            if self.acquire_leases(&record.action_id, &lease).is_err() {
                continue;
            }
            let receipt = resolve(&record).unwrap_or(LiveReceipt {
                action_id: record.action_id.clone(),
                status: LiveStatus::Unknown,
                fingerprint: None,
                committed_resources: Vec::new(),
            });
            let update_result = self.update(&record.action_id, |current| {
                if receipt.action_id != current.action_id {
                    return Err(CatalogConflict::ActorMismatch.into());
                }
                current.live = Some(receipt.clone());
                current.state = match receipt.status {
                    LiveStatus::NotCommitted => ActionState::Aborted,
                    LiveStatus::Committed
                    | LiveStatus::Conflict
                    | LiveStatus::Partial
                    | LiveStatus::Unknown => ActionState::NeedsReconciliation,
                };
                Ok(())
            });
            self.release_leases(&record.action_id, &lease)?;
            let updated = update_result?;
            changed.push(updated);
        }
        Ok(changed)
    }

    pub fn acquire_leases(&self, action_id: &str, physical_ids: &[String]) -> CatalogResult<()> {
        if physical_ids.windows(2).any(|pair| pair[0] >= pair[1])
            || physical_ids.iter().collect::<BTreeSet<_>>().len() != physical_ids.len()
        {
            return Err(CatalogConflict::LeaseOrder.into());
        }
        let mut leases = self.leases.lock().map_err(|_| "lease lock poisoned")?;
        if physical_ids
            .iter()
            .any(|id| leases.get(id).is_some_and(|owner| owner != action_id))
        {
            return Err(CatalogConflict::LeaseConflict.into());
        }
        for id in physical_ids {
            leases.insert(id.clone(), action_id.to_owned());
        }
        Ok(())
    }

    pub fn release_leases(&self, action_id: &str, physical_ids: &[String]) -> CatalogResult<()> {
        let mut leases = self.leases.lock().map_err(|_| "lease lock poisoned")?;
        for id in physical_ids {
            if leases.get(id).is_some_and(|owner| owner == action_id) {
                leases.remove(id);
            }
        }
        Ok(())
    }

    fn list_actions(&self) -> CatalogResult<Vec<ActionRecord>> {
        let read = self.db.begin_read()?;
        let table = read.open_table(ACTIONS)?;
        table
            .iter()?
            .map(|item| {
                let (_, value) = item?;
                Ok(serde_json::from_slice(value.value())?)
            })
            .collect()
    }

    fn update<F>(&self, action_id: &str, mutate: F) -> CatalogResult<ActionRecord>
    where
        F: FnOnce(&mut ActionRecord) -> CatalogResult<()>,
    {
        let write = self.db.begin_write()?;
        let mut record: ActionRecord = {
            let table = write.open_table(ACTIONS)?;
            let value = table.get(action_id)?.ok_or("unknown action")?;
            serde_json::from_slice(value.value())?
        };
        let old = record.clone();
        mutate(&mut record)?;
        let encoded = serde_json::to_vec(&record)?;
        {
            let mut table = write.open_table(ACTIONS)?;
            table.insert(action_id, encoded.as_slice())?;
        }
        if old.state != record.state && !record.recovery_only && matches!(record.state, ActionState::Complete | ActionState::CheckpointComplete) {
            write.open_table(POLICIES)?.insert(record.capture_clock_key()?.as_str(), record.captured_at_millis.to_le_bytes().as_slice())?;
        }
        if old.state != record.state {
            let outcome = match &record.state {
                ActionState::Complete if record.recovery_only => Some("Optional backup skipped by capture settings"),
                ActionState::Complete | ActionState::CheckpointComplete => Some("Captured"),
                ActionState::CaptureFailed | ActionState::AfterCaptureFailed |
                    ActionState::CheckpointFailed => Some("Capture failed"),
                _ => None,
            };
            if let Some(outcome) = outcome {
                let summary = format!("{outcome} at {}", chrono::Utc::now().to_rfc3339());
                write.open_table(POLICIES)?.insert(format!("last_capture:{}", record.actor_account_id).as_str(),
                    serde_json::to_vec(&summary)?.as_slice())?;
            }
        }
        update_change_index(&write, Some(&old), &record)?;
        write.commit()?;
        Ok(record)
    }
}

fn legal_transition(from: &ActionState, to: &ActionState) -> bool {
    matches!(
        (from, to),
        (ActionState::Intent, ActionState::CapturingBefore)
            | (ActionState::BeforeDurable, ActionState::Applying)
            | (ActionState::Applying, ActionState::CapturingAfter)
            | (ActionState::Applied, ActionState::CapturingAfter)
            | (ActionState::AfterDurable, ActionState::Complete)
            | (ActionState::Intent, ActionState::Aborted)
            | (
                ActionState::CapturingBefore,
                ActionState::NeedsReconciliation
            )
            | (ActionState::CapturingBefore, ActionState::CaptureFailed)
            | (ActionState::CaptureFailed, ActionState::CapturingBefore)
            | (ActionState::BeforeDurable, ActionState::NeedsReconciliation)
            | (ActionState::Applying, ActionState::NeedsReconciliation)
            | (
                ActionState::CapturingAfter,
                ActionState::NeedsReconciliation
            )
            | (ActionState::CapturingAfter, ActionState::AfterCaptureFailed)
            | (ActionState::AfterCaptureFailed, ActionState::CapturingAfter)
            | (ActionState::Applying, ActionState::Conflict)
            | (ActionState::Applying, ActionState::Partial)
            | (ActionState::Applying, ActionState::Failed)
            | (ActionState::AfterDurable, ActionState::NeedsReconciliation)
    )
}

fn owner_partition(account_id: &str) -> [u8; 16] {
    owner_partition_seed(account_id, 0)
}

fn owner_partition_seed(account_id: &str, attempt: u64) -> [u8; 16] {
    let mut input = account_id.as_bytes().to_vec();
    input.extend_from_slice(&attempt.to_le_bytes());
    let digest = blake3::hash(&input);
    let mut partition = [0u8; 16];
    partition.copy_from_slice(&digest.as_bytes()[..16]);
    if partition == [0; 16] {
        partition[0] = 1;
    }
    partition
}

fn lease_ids(record: &ActionRecord) -> Vec<String> {
    let mut ids = record
        .guard_resource_ids
        .iter()
        .chain(record.modified_resource_ids.iter())
        .map(ResourceKey::scoped_lease_id)
        .collect::<Vec<_>>();
    ids.push(record.resource_key.scoped_lease_id());
    ids.push(format!("history-action:{}", record.action_id));
    ids.extend(
        record
            .physical_lease_keys
            .iter()
            .map(|key| format!("physical:{key}")),
    );
    ids.sort();
    ids.dedup();
    ids
}


/// The catalog retains bounded references to immutable, independently authorized
/// Memery revisions. A reference alone can neither reveal nor restore a value.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MemeryReference {
    pub block: String,
    pub revision: u64,
    pub change: String,
    pub digest: String,
    pub at: u64,
    pub evidence: bool,
}

/// Preserve existing on-disk keys and wire values (false = records, true = assets).
/// Guidance needs its own cursor because Hex revisions have their own SQLite rowids.
#[derive(Debug, Copy, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum MemeryCollection {
    #[serde(rename = "guidance")]
    Guidance,
    #[serde(untagged)]
    RecordsOrAssets(bool),
}

#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MemeryManifest {
    pub generation: u64,
    pub position: u64,
    pub witness: String,
    pub entries: Vec<MemeryReference>,
}

impl Catalog {
    pub fn memery_manifest(&self, account: &str, collection: MemeryCollection) -> CatalogResult<MemeryManifest> {
        let read = self.db.begin_read()?;
        let table = read.open_table(MEMERY)?;
        let key = serde_json::to_string(&(account, collection))?;
        let raw = table.get(key.as_str())?;
        Ok(raw.map(|v| serde_json::from_slice(v.value())).transpose()?.unwrap_or_default())
    }

    pub fn settle_memery(&self, account: &str, collection: MemeryCollection, checkpoint: bool, generation: u64, position: u64, witness: String,
        add: Vec<MemeryReference>, remove: Vec<String>) -> CatalogResult<MemeryManifest> {
        let digest = |s: &str| s.len() == 64 && s.bytes().all(|v| v.is_ascii_hexdigit());
        if add.len() > 32 || remove.len() > 256 || (position != 0 && !digest(&witness))
            || (position == 0 && !witness.is_empty()) {
            return Err("invalid Memery batch".into());
        }
        let write = self.db.begin_write()?;
        let policies = write.open_table(POLICIES)?;
        let policy: PolicySet = policies.get("policy")?.map(|v| serde_json::from_slice(v.value()))
            .transpose()?.unwrap_or_default();
        let policy = policy.capture_for(account);
        let mut epoch = 0;
        for key in ["memery_policy_changed_at".to_owned(), format!("memery_policy_changed_at:{account}")] {
            if let Some(v) = policies.get(key.as_str())? {
                epoch = epoch.max(u64::from_be_bytes(v.value().try_into()?));
            }
        }
        let now = chrono::Utc::now().timestamp_millis() as u64;
        let earliest = now.saturating_sub(policy.max_age_days.unwrap_or(u64::MAX).saturating_mul(86_400_000));
        let mut table = write.open_table(MEMERY)?;
        let key = serde_json::to_string(&(account, collection))?;
        let mut state: MemeryManifest = table.get(key.as_str())?.map(|v| serde_json::from_slice(v.value()))
            .transpose()?.unwrap_or_default();
        let original = state.clone();
        if state.generation != generation { return Err("Memery generation changed".into()); }
        if checkpoint && (position != state.position || witness != state.witness || add.len() != 1 || !remove.is_empty()) {
            return Err("invalid Memery checkpoint".into());
        }
        state.entries.retain(|v| !remove.contains(&v.change) && v.at >= earliest);
        for item in add {
            if item.block.is_empty() || item.block.len() > 256 || item.block.chars().any(char::is_control)
                || !digest(&item.change) || !digest(&item.digest) || item.revision == 0 || item.at > now {
                return Err("invalid Memery revision".into());
            }
            let enabled = if collection == MemeryCollection::Guidance { policy.memery_records }
                else if collection == MemeryCollection::RecordsOrAssets(true) { policy.memery_artifacts }
                else if item.evidence { policy.memery_evidence } else { policy.memery_records };
            if !policy.enabled || !enabled || item.at < earliest
                || (!checkpoint && (policy.frequency == "manual" || item.at <= epoch)) { continue; }
            if state.entries.iter().any(|v| v.change == item.change) { continue; }
            if !checkpoint && policy.frequency == "interval" {
                let interval = policy.interval_seconds.saturating_mul(1000);
                state.entries.retain(|v| v.block != item.block || v.at.abs_diff(item.at) >= interval);
            }
            state.entries.push(item);
        }
        state.entries.sort_by_key(|v| std::cmp::Reverse((v.at, v.revision)));
        let mut counts = BTreeMap::new();
        state.entries.retain(|v| {
            let count = counts.entry(v.block.clone()).or_insert(0u64);
            *count += 1;
            *count <= policy.versions_per_item
        });
        state.entries.truncate(256);
        state.position = position;
        state.witness = witness;
        if state == original { return Ok(original); }
        state.generation = generation.checked_add(1).ok_or("Memery generation exhausted")?;
        table.insert(key.as_str(), serde_json::to_vec(&state)?.as_slice())?;
        drop(table);
        drop(policies);
        write.commit()?;
        Ok(state)
    }
}

#[cfg(test)]
mod guidance_history_tests {
    use super::*;

    #[test]
    fn guidance_has_an_independent_cursor_and_obeys_record_capture_policy() {
        let temp = tempfile::tempdir().unwrap();
        let catalog = Catalog::open(temp.path().join("catalog.redb"), "account").unwrap();
        let stream = MemeryCollection::Guidance;
        for (wire, kind) in [("false", MemeryCollection::RecordsOrAssets(false)),
                            ("true", MemeryCollection::RecordsOrAssets(true)), ("\"guidance\"", stream)] {
            assert_eq!(serde_json::from_str::<MemeryCollection>(wire).unwrap(), kind);
            assert_eq!(serde_json::to_string(&kind).unwrap(), wire);
        }

        let mut policy = catalog.policy_set().unwrap();
        policy.capture_defaults.frequency = "manual".into();
        let policy = catalog.set_policy(policy.revision, policy).unwrap();
        let revision = MemeryReference { block: "profile-rule".into(), revision: 1,
            change: "a".repeat(64), digest: "b".repeat(64), at: 100, evidence: false };
        let settle = |generation, checkpoint| catalog.settle_memery("account", stream,
            checkpoint, generation, 1, "a".repeat(64), vec![revision.clone()], vec![]).unwrap();
        let scanned = settle(0, false);
        assert!(scanned.entries.is_empty());
        let records = catalog.memery_manifest("account", MemeryCollection::RecordsOrAssets(false)).unwrap();
        assert_eq!(records.position, 0);
        let saved = settle(scanned.generation, true);
        assert_eq!(saved.entries, vec![revision.clone()]);
        assert_eq!(settle(saved.generation, true), saved);

        let mut policy = policy;
        policy.capture_defaults.memery_records = false;
        catalog.set_policy(policy.revision, policy).unwrap();
        let other = MemeryReference { revision: 2, change: "c".repeat(64), ..revision };
        let excluded = catalog.settle_memery("account", stream, true, saved.generation, saved.position,
            saved.witness, vec![other], vec![]).unwrap();
        assert_eq!(excluded.entries, saved.entries);
        assert!(catalog.memery_manifest("other", stream).unwrap().entries.is_empty());
    }
}
