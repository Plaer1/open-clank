//! Internal typed operation DTOs and Lore reference construction.

use crate::catalog::{
    ActionRecord, ActionState, CaptureManifest, Catalog, CatalogConflict, CatalogResult,
    LiveReceipt, LiveStatus, Locator, MutationBatch, MutationResource, PrepareReceipt,
    ResourceExistence, ResourceKey, ResourceMetadata, ResourceOutcome, ResourceType, Revision,
    VersionContent, VersionReceipt,
};
use crate::retention::{BudgetError, ExpiryRecord, ExpiryState, PersistedReservation, PolicySet};
use crate::usage::{
    add_external_storage, measure_root_with_reservations, HistoryUsage, RetainedVersionUsage,
};
use crate::{Context, HistoryStore, Partition};
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(feature = "test_faults")]
use std::sync::atomic::AtomicU8;

static VERSION_SEQUENCE: AtomicU64 = AtomicU64::new(0);

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

fn default_schema_version() -> u32 {
    1
}

pub fn new_action_record(request: ActionRequest) -> ActionRecord {
    let request_digest = normalized_digest(&request);
    ActionRecord {
        schema_version: request.schema_version,
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
    pub(crate) catalog: Catalog,
    store: HistoryStore,
    held_leases: Mutex<BTreeMap<String, Vec<String>>>,
}

impl HistoryCoordinator {
    pub async fn open(
        catalog_path: impl AsRef<std::path::Path>,
        lore_root: impl AsRef<std::path::Path>,
        account_id: &str,
    ) -> CatalogResult<Self> {
        let catalog = Catalog::open(catalog_path, account_id)?;
        catalog.reconcile_reservations()?;
        let coordinator = Self {
            catalog,
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

    pub fn catalog(&self) -> &Catalog {
        &self.catalog
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
                        candidate.action_id != action.action_id
                            && matches!(
                                candidate.state,
                                ActionState::Complete
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
        if !matches!(
            action.state,
            ActionState::Complete
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
                let digest_bound =
                    entry.coverage.content_digest.as_deref() == Some(content_digest.as_str());
                let opaque_manifest = entry
                    .metadata
                    .opaque
                    .as_ref()
                    .and_then(serde_json::Value::as_object)
                    .and_then(|value| value.get("manifest_digest"))
                    .and_then(serde_json::Value::as_str)
                    == Some(content_digest.as_str());
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
                let _ = self.release_capture_reservation(&reservation_id);
                return Err(error);
            }
        };
        let result =
            self.catalog
                .record_after(action_id, receipt, capture_digest, scope_id, logical_bytes);
        let _ = self.release_capture_reservation(&reservation_id);
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

    pub async fn read_version(
        &self,
        action_id: &str,
        receipt: &VersionReceipt,
    ) -> CatalogResult<Option<Bytes>> {
        let action = self
            .catalog
            .get_action(action_id)?
            .ok_or("unknown action")?;
        if !action
            .before
            .as_ref()
            .is_some_and(|stored| stored == receipt)
            && !action
                .before_resources
                .iter()
                .any(|resource| resource.before.as_ref() == Some(receipt))
            && !action
                .after
                .as_ref()
                .is_some_and(|stored| stored == receipt)
        {
            return Err("version receipt is not recorded for action".into());
        }
        if self
            .catalog
            .expiry(action_id)?
            .is_some_and(|expiry| expiry.state == ExpiryState::Expired)
        {
            return Err("history payload expired".into());
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
    let mut data = fingerprint.as_bytes().to_vec();
    data.push(0);
    if let Some(bytes) = content {
        data.extend_from_slice(bytes);
    }
    blake3::hash(&data).to_hex().to_string()
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

fn append_digest_frame(input: &mut Vec<u8>, domain: &[u8], payload: &[u8]) {
    input.extend_from_slice(&(domain.len() as u64).to_be_bytes());
    input.extend_from_slice(domain);
    input.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    input.extend_from_slice(payload);
}

pub fn request_digest(request: &ActionRequest) -> String {
    normalized_digest(request)
}
