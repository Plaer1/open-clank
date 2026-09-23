//! Capture-facing adapter shared by user and agent mutation owners.
//!
//! This layer keeps wrapper traversal idempotent: callers carry one envelope and one action id
//! into the checked S17 coordinator. It does not observe filesystem writes or infer a write set
//! from shell text; those providers must call `prepare` at their mutation owner.

use crate::catalog::{
    ActionRecord, ActionState, CaptureManifest, CatalogResult, LiveReceipt, LiveStatus, Locator,
    ResourceKey, ResourceOutcome, Revision,
};
use crate::operations::{
    batch_capture_digest, begin, ActionRequest, BatchCaptureInput, HistoryCoordinator,
};
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum CoverageKind {
    KnownMutationHooks,
    DeclaredRootsBaseline,
    ObservedAfterOnly,
    Uncovered,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CoverageReceipt {
    pub kind: CoverageKind,
    pub captured_at_millis: u64,
    pub roots: Vec<Locator>,
    pub exclusions: Vec<String>,
}

impl CoverageReceipt {
    pub fn manifest(&self) -> CaptureManifest {
        CaptureManifest {
            metadata: Some(json!({
                "coverage_kind": self.kind,
                "captured_at_millis": self.captured_at_millis,
                "roots": self.roots,
                "exclusions": self.exclusions,
            })),
            ..CaptureManifest::default()
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CaptureEnvelope {
    pub action_id: String,
    pub actor_id: String,
    pub actor_account_id: String,
    pub actor_kind: String,
    pub session_id: Option<String>,
    pub run_id: Option<String>,
    pub task_id: Option<String>,
    pub tool_id: Option<String>,
    pub resource_key: ResourceKey,
    pub guard_resource_ids: Vec<ResourceKey>,
    pub modified_resource_ids: Vec<ResourceKey>,
    pub operation: String,
    pub expected_revision: Option<Revision>,
    pub before_revision: Option<Revision>,
    pub expected_after_revision: Option<Revision>,
    pub original_locator: Option<Locator>,
    pub destination_locator: Option<Locator>,
    pub per_resource_outcomes: Option<Vec<ResourceOutcome>>,
    pub coverage: CoverageReceipt,
}

impl CaptureEnvelope {
    pub fn request(&self) -> ActionRequest {
        ActionRequest {
            schema_version: 1,
            action_id: self.action_id.clone(),
            actor_account_id: self.actor_account_id.clone(),
            resource_key: self.resource_key.clone(),
            physical_lease_keys: Vec::new(),
            guard_resource_ids: self.guard_resource_ids.clone(),
            modified_resource_ids: self.modified_resource_ids.clone(),
            operation: self.operation.clone(),
            expected_revision: self.expected_revision.clone(),
            actor_id: self.actor_id.clone(),
            actor_kind: self.actor_kind.clone(),
            session_id: self.session_id.clone(),
            run_id: self.run_id.clone(),
            task_id: self.task_id.clone(),
            tool_id: self.tool_id.clone(),
            before_revision: self.before_revision.clone(),
            expected_after_revision: self.expected_after_revision.clone(),
            original_locator: self.original_locator.clone(),
            destination_locator: self.destination_locator.clone(),
            timestamp_millis: Some(self.coverage.captured_at_millis),
            coverage: Some(self.coverage.manifest()),
            per_resource_outcomes: self.per_resource_outcomes.clone(),
        }
    }
}

pub struct CaptureAdapter<'a> {
    coordinator: &'a HistoryCoordinator,
}

impl<'a> CaptureAdapter<'a> {
    pub fn new(coordinator: &'a HistoryCoordinator) -> Self {
        Self { coordinator }
    }

    /// Begin and durably capture the preimage. Repeated wrapper calls with the same action id
    /// return the existing record and never create a second action.
    pub async fn prepare(
        &self,
        envelope: CaptureEnvelope,
        before: Option<Bytes>,
        fingerprint: impl Into<String>,
    ) -> CatalogResult<ActionRecord> {
        let request = envelope.request();
        match begin(self.coordinator.catalog(), request)? {
            crate::catalog::BeginResult::Existing(record) => Ok(record),
            crate::catalog::BeginResult::New(record) => {
                debug_assert_eq!(record.state, ActionState::Intent);
                self.coordinator
                    .capture_before(&record.action_id, before, fingerprint)
                    .await
            }
        }
    }

    /// Prepare an exact parent mutation batch. Every supplied entry is
    /// durable before this method returns; callers that depend on recovery
    /// must not proceed when it returns an error.
    pub async fn prepare_batch(
        &self,
        envelope: CaptureEnvelope,
        mut entries: Vec<BatchCaptureInput>,
    ) -> CatalogResult<ActionRecord> {
        if entries.is_empty() {
            return Err("batch prepare requires at least one resource".into());
        }
        let mut request = envelope.request();
        for entry in &entries {
            if !request.modified_resource_ids.contains(&entry.resource_key) {
                request
                    .modified_resource_ids
                    .push(entry.resource_key.clone());
            }
        }
        let digest = batch_capture_digest(&entries);
        match begin(self.coordinator.catalog(), request)? {
            crate::catalog::BeginResult::Existing(record)
                if matches!(
                    record.state,
                    ActionState::Intent | ActionState::CaptureFailed
                ) =>
            {
                self.coordinator
                    .capture_batch_before(&record.action_id, entries)
                    .await
            }
            crate::catalog::BeginResult::Existing(record)
                if record.before_capture_digest.as_deref() == Some(digest.as_str()) =>
            {
                Ok(record)
            }
            crate::catalog::BeginResult::Existing(_) => {
                Err("history_prepare_conflict: existing batch digest differs".into())
            }
            crate::catalog::BeginResult::New(record) => {
                debug_assert_eq!(record.state, ActionState::Intent);
                self.coordinator
                    .capture_batch_before(&record.action_id, std::mem::take(&mut entries))
                    .await
            }
        }
    }

    /// Record the authoritative live result and, for a committed mutation, capture its postimage.
    /// A failed postimage remains an applied live action with S17's `AfterCaptureFailed` state.
    pub async fn finish(
        &self,
        action_id: &str,
        live: LiveReceipt,
        after: Option<Bytes>,
        fingerprint: impl Into<String>,
    ) -> CatalogResult<ActionRecord> {
        self.coordinator.begin_apply(action_id)?;
        let record = self.coordinator.record_live(action_id, live.clone())?;
        if !matches!(live.status, LiveStatus::Committed) {
            return Ok(record);
        }
        self.coordinator
            .capture_after(action_id, after, fingerprint)
            .await?;
        self.coordinator.complete(action_id)
    }
}
