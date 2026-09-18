//! Durable, application-owned operation catalog for the checked Lore adapter.
//!
//! Lore stores bytes and associations; this journal stores provenance, idempotency and phase
//! receipts. It never performs a live mutation and never replays one during reconciliation.

use redb::{Database, ReadableTable, TableDefinition};
use crate::retention::{
    BudgetError, ExpiryRecord, ExpiryState, PersistedReservation, PolicySet,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::{Arc, Mutex};

const SCHEMA_VERSION: u32 = 1;
const META: TableDefinition<&str, &[u8]> = TableDefinition::new("history_meta");
const ACTIONS: TableDefinition<&str, &[u8]> = TableDefinition::new("history_actions");
const POLICIES: TableDefinition<&str, &[u8]> = TableDefinition::new("history_policies");
const RESERVATIONS: TableDefinition<&str, &[u8]> = TableDefinition::new("history_reservations");
const EXPIRY: TableDefinition<&str, &[u8]> = TableDefinition::new("history_expiry");
const RESTORES: TableDefinition<&str, &[u8]> = TableDefinition::new("history_restores");

pub type CatalogResult<T> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceKey {
    pub account_id: String,
    pub workspace_id: String,
    pub provider: String,
    pub resource_id: String,
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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LiveReceipt {
    pub action_id: String,
    pub status: LiveStatus,
    pub fingerprint: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ActionRecord {
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
    pub after: Option<VersionReceipt>,
    pub live: Option<LiveReceipt>,
    #[serde(default)]
    pub before_capture_digest: Option<String>,
    #[serde(default)]
    pub after_capture_digest: Option<String>,
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
}

impl ActionRecord {
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
            Self::ExceedsTarget { requested, available } => {
                write!(f, "history budget exceeded: requested {requested}, available {available}")
            }
            Self::ExceedsGlobal { requested, available } => {
                write!(f, "global history budget exceeded: requested {requested}, available {available}")
            }
            Self::ExceedsScope { requested, available } => {
                write!(f, "scope history budget exceeded: requested {requested}, available {available}")
            }
            Self::Arithmetic => write!(f, "history budget arithmetic overflow"),
            Self::InvalidPolicy(reason) => write!(f, "invalid history policy: {reason}"),
            Self::ReservationConflict => write!(f, "history reservation conflict"),
        }
    }
}
impl std::error::Error for BudgetError {}

pub struct Catalog {
    db: Arc<Database>,
    path: std::path::PathBuf,
    leases: Mutex<BTreeMap<String, String>>,
}

impl Catalog {
    pub fn open(path: impl AsRef<Path>, account_id: &str) -> CatalogResult<Self> {
        let path = path.as_ref().to_path_buf();
        let db = Database::create(&path)?;
        let catalog = Self {
            db: Arc::new(db),
            path,
            leases: Mutex::new(BTreeMap::new()),
        };
        let partition_key = format!("partition:{account_id}");
        let write = catalog.db.begin_write()?;
        {
            let mut meta = write.open_table(META)?;
            let _actions = write.open_table(ACTIONS)?;
            let _policies = write.open_table(POLICIES)?;
            let _reservations = write.open_table(RESERVATIONS)?;
            let _expiry = write.open_table(EXPIRY)?;
            let _restores = write.open_table(RESTORES)?;
            if let Some(schema) = meta.get("schema")? {
                let stored: u32 = serde_json::from_slice(schema.value())?;
                if stored != SCHEMA_VERSION {
                    return Err(format!("unsupported history catalog schema {stored}").into());
                }
            } else {
                meta.insert("schema", serde_json::to_vec(&SCHEMA_VERSION)?.as_slice())?;
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
        table.insert("policy", serde_json::to_vec(&next)?.as_slice())?;
        drop(table);
        write.commit()?;
        Ok(next)
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
            return Err(BudgetError::Disabled.into());
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
            .filter_map(|(_, value)| serde_json::from_slice::<PersistedReservation>(value.value()).ok())
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
                available: policy.global.total_bytes.saturating_sub(global_physical_bytes.saturating_add(all_reserved)),
            }
            .into());
        }
        if !global_scope && scoped_used > effective {
            return Err(BudgetError::ExceedsScope {
                requested: reservation.total_bytes(),
                available: effective.saturating_sub(scope_logical_bytes.saturating_add(scoped_reserved)),
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
        let known = actions.iter().map(|action| action.action_id.clone()).collect::<BTreeSet<_>>();
        let terminal = actions
            .into_iter()
            .filter(|action| {
                matches!(
                    action.state,
                    ActionState::Complete
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
        table.insert(record.action_id.as_str(), serde_json::to_vec(&record)?.as_slice())?;
        drop(table);
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

    pub fn get_restore<T: DeserializeOwned>(
        &self,
        restore_id: &str,
    ) -> CatalogResult<Option<T>> {
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

    pub fn begin_action(&self, record: ActionRecord) -> CatalogResult<BeginResult> {
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
        let encoded = serde_json::to_vec(&record)?;
        {
            let mut table = write.open_table(ACTIONS)?;
            table.insert(record.action_id.as_str(), encoded.as_slice())?;
        }
        write.commit()?;
        Ok(BeginResult::New(record))
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

    pub(crate) fn record_live(
        &self,
        action_id: &str,
        receipt: LiveReceipt,
    ) -> CatalogResult<ActionRecord> {
        self.update(action_id, |record| {
            if record.state != ActionState::Applying || receipt.action_id != record.action_id {
                return Err(CatalogConflict::InvalidTransition.into());
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
        mutate(&mut record)?;
        let encoded = serde_json::to_vec(&record)?;
        {
            let mut table = write.open_table(ACTIONS)?;
            table.insert(action_id, encoded.as_slice())?;
        }
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
