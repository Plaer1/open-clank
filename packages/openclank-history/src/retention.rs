//! Finite, reservation-first history retention policy decisions.

use crate::usage::HistoryUsage;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const DEFAULT_TOTAL_BYTES: u64 = 1_073_741_824;

/// The stable scope classes used by budget attribution.  Directory scopes are
/// intentionally rooted in the application's existing allowlist registry; this
/// module does not create a second directory registry.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub enum ScopeKind {
    Global,
    Workspace,
    Directory,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ScopePolicy {
    pub scope_id: String,
    pub kind: ScopeKind,
    /// Immutable account/principal owner. Workspace ids are meaningful only
    /// inside this owner namespace and are never substituted for it.
    #[serde(default)]
    pub owner_account_id: Option<String>,
    pub workspace_id: Option<String>,
    pub root: Option<PathBuf>,
    pub limit_bytes: Option<u64>,
    pub revision: u64,
    pub enabled: bool,
}

impl ScopePolicy {
    pub fn global(scope_id: impl Into<String>, limit_bytes: u64) -> Self {
        Self {
            scope_id: scope_id.into(),
            kind: ScopeKind::Global,
            owner_account_id: None,
            workspace_id: None,
            root: None,
            limit_bytes: Some(limit_bytes),
            revision: 1,
            enabled: true,
        }
    }

    fn matches(&self, owner_account_id: Option<&str>, workspace_id: &str, path: Option<&Path>) -> bool {
        if !self.enabled {
            return false;
        }
        match self.kind {
            ScopeKind::Global => true,
            ScopeKind::Workspace => {
                self.owner_account_id.as_deref() == owner_account_id
                    && self.workspace_id.as_deref() == Some(workspace_id)
            }
            ScopeKind::Directory => {
                self.owner_account_id.as_deref() == owner_account_id
                    && self.workspace_id.as_deref().is_none_or(|id| id == workspace_id)
                    && self.root.as_deref().is_some_and(|root| {
                        path.is_some_and(|path| path == root || path.starts_with(root))
                    })
            }
        }
    }

    fn specificity(&self) -> (u8, usize) {
        let class = match self.kind {
            ScopeKind::Global => 0,
            ScopeKind::Workspace => 1,
            ScopeKind::Directory => 2,
        };
        (class, self.root.as_ref().map_or(0, |root| root.as_os_str().len()))
    }
}

/// Preferences for newly captured history. Older catalogs inherit these defaults.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct CapturePolicy {
    pub enabled: bool,
    pub files: bool,
    pub images: bool,
    pub agent_files: bool,
    pub memery_records: bool,
    pub memery_evidence: bool,
    pub memery_artifacts: bool,
    pub frequency: String,
    pub interval_seconds: u64,
    pub versions_per_item: u64,
    pub max_age_days: Option<u64>,
}

impl Default for CapturePolicy {
    fn default() -> Self {
        Self {
            enabled: true, files: true, images: true, agent_files: true,
            memery_records: true, memery_evidence: true, memery_artifacts: true,
            frequency: "interval".into(), interval_seconds: 300, versions_per_item: 10,
            max_age_days: None,
        }
    }
}

/// Persisted policy collection.  The global target remains the frozen 1 GiB
/// default while workspace and directory limits are optional overrides.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PolicySet {
    pub revision: u64,
    pub global: HistoryPolicy,
    pub scopes: Vec<ScopePolicy>,
    #[serde(default)]
    pub capture_defaults: CapturePolicy,
    #[serde(default)]
    pub accounts: std::collections::BTreeMap<String, CapturePolicy>,
}

impl Default for PolicySet {
    fn default() -> Self {
        Self {
            revision: 1,
            global: HistoryPolicy::default(),
            scopes: Vec::new(),
            accounts: Default::default(),
            capture_defaults: Default::default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EffectivePolicy {
    pub scope_id: String,
    pub limit_bytes: u64,
    pub revision: u64,
}

impl PolicySet {
    pub fn capture_for(&self, owner: &str) -> CapturePolicy {
        let mut capture = self.accounts.get(owner).unwrap_or(&self.capture_defaults).clone();
        capture.enabled &= self.global.enabled;
        capture
    }

    pub fn permits_action(&self, action: &crate::catalog::ActionRecord) -> bool {
        let capture = self.capture_for(&action.actor_account_id);
        let category_enabled = match action.resource_key.provider.as_str() {
            "imps-project" => capture.images,
            "memery-record" => capture.memery_records,
            "memery-evidence" => capture.memery_evidence,
            "memery-artifact" => capture.memery_artifacts,
            _ if action.actor_kind == "agent" || action.managed_tool_origin_v1.is_some() => capture.agent_files,
            _ => capture.files,
        };
        capture.enabled && category_enabled
    }

    pub fn effective_for(
        &self,
        workspace_id: &str,
        path: Option<&Path>,
    ) -> EffectivePolicy {
        self.effective_for_owner(None, workspace_id, path)
    }

    pub fn effective_for_owner(
        &self,
        owner_account_id: Option<&str>,
        workspace_id: &str,
        path: Option<&Path>,
    ) -> EffectivePolicy {
        let fallback = EffectivePolicy {
            scope_id: "global".into(),
            limit_bytes: self.global.total_bytes,
            revision: self.global.revision,
        };
        self.scopes
            .iter()
            .filter(|scope| scope.matches(owner_account_id, workspace_id, path))
            .max_by(|left, right| {
                left.specificity()
                    .cmp(&right.specificity())
                    .then_with(|| right.scope_id.cmp(&left.scope_id))
            })
            .and_then(|scope| scope.limit_bytes.map(|limit| EffectivePolicy {
                scope_id: scope.scope_id.clone(),
                limit_bytes: limit,
                revision: scope.revision,
            }))
            .unwrap_or(fallback)
    }

    pub fn validate(&self) -> Result<(), BudgetError> {
        for capture in self.accounts.values().chain(std::iter::once(&self.capture_defaults)) {
            if !matches!(capture.frequency.as_str(), "every_change" | "interval" | "manual")
                || !(1..=31_536_000).contains(&capture.interval_seconds)
                || !(1..=100_000).contains(&capture.versions_per_item)
                || capture.max_age_days.is_some_and(|days| !(1..=36_500).contains(&days)) {
                return Err(BudgetError::InvalidPolicy("invalid capture frequency or retention".into()));
            }
        }
        if self.global.total_bytes == 0 {
            return Err(BudgetError::InvalidPolicy("global target must be positive".into()));
        }
        let mut ids = std::collections::BTreeSet::new();
        for scope in &self.scopes {
            if scope.scope_id.trim().is_empty() || !ids.insert(scope.scope_id.clone()) {
                return Err(BudgetError::InvalidPolicy("scope ids must be unique".into()));
            }
            if scope.kind == ScopeKind::Directory && scope.root.is_none() {
                return Err(BudgetError::InvalidPolicy("directory scope requires a root".into()));
            }
            if scope.kind != ScopeKind::Global && scope.owner_account_id.as_deref().is_none_or(str::is_empty) {
                return Err(BudgetError::InvalidPolicy("non-global scope requires an owner account".into()));
            }
            if scope.limit_bytes == Some(0) {
                return Err(BudgetError::InvalidPolicy("scope target must be positive".into()));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HistoryPolicy {
    pub revision: u64,
    pub total_bytes: u64,
    pub workspace_bytes: Option<u64>,
    pub directory_bytes: Option<u64>,
    pub enabled: bool,
}
impl Default for HistoryPolicy {
    fn default() -> Self {
        Self {
            revision: 1,
            total_bytes: DEFAULT_TOTAL_BYTES,
            workspace_bytes: None,
            directory_bytes: None,
            enabled: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BudgetReservation {
    pub bytes: u64,
    pub metadata_bytes: u64,
    pub policy_revision: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExpiryCandidate {
    pub action_id: String,
    pub bytes: u64,
    pub created_millis: u64,
    pub pinned: bool,
    pub active: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BudgetError {
    Disabled,
    ExceedsTarget { requested: u64, available: u64 },
    ExceedsGlobal { requested: u64, available: u64 },
    ExceedsScope { requested: u64, available: u64 },
    Arithmetic,
    InvalidPolicy(String),
    ReservationConflict,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PersistedReservation {
    pub reservation_id: String,
    pub scope_id: String,
    pub bytes: u64,
    pub metadata_bytes: u64,
    pub policy_revision: u64,
    pub created_millis: u64,
}

impl PersistedReservation {
    pub fn total_bytes(&self) -> u64 {
        self.bytes.saturating_add(self.metadata_bytes)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ExpiryState {
    Expiring,
    Expired,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExpiryRecord {
    pub action_id: String,
    pub state: ExpiryState,
    pub bytes: u64,
    pub started_millis: u64,
    pub finished_millis: Option<u64>,
    pub metadata_expired: bool,
}

pub fn reserve_capture(
    policy: &HistoryPolicy,
    usage: &HistoryUsage,
    content_bytes: u64,
    metadata_bytes: u64,
) -> Result<BudgetReservation, BudgetError> {
    if !policy.enabled {
        return Err(BudgetError::Disabled);
    }
    let requested = content_bytes
        .checked_add(metadata_bytes)
        .ok_or(BudgetError::Arithmetic)?;
    let used = usage
        .physical_allocated_bytes
        .checked_add(usage.reserved_inflight_bytes)
        .ok_or(BudgetError::Arithmetic)?;
    let available = policy.total_bytes.saturating_sub(used);
    if requested > available {
        return Err(BudgetError::ExceedsTarget {
            requested,
            available,
        });
    }
    Ok(BudgetReservation {
        bytes: content_bytes,
        metadata_bytes,
        policy_revision: policy.revision,
    })
}

pub fn select_expiry(
    mut candidates: Vec<ExpiryCandidate>,
    usage: &HistoryUsage,
    policy: &HistoryPolicy,
    incoming_bytes: u64,
) -> Vec<String> {
    let used = usage
        .physical_allocated_bytes
        .saturating_add(usage.reserved_inflight_bytes);
    let needed = used
        .saturating_add(incoming_bytes)
        .saturating_sub(policy.total_bytes);
    if needed == 0 {
        return Vec::new();
    }
    candidates.retain(|candidate| !candidate.pinned && !candidate.active);
    candidates.sort_by_key(|candidate| candidate.created_millis);
    let mut reclaimed = 0u64;
    let mut selected = Vec::new();
    for candidate in candidates {
        if reclaimed >= needed {
            break;
        }
        reclaimed = reclaimed.saturating_add(candidate.bytes);
        selected.push(candidate.action_id);
    }
    selected
}
