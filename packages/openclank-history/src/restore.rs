//! Exact-version restore planning. Providers apply a prepared plan through their own mutation
//! owner; this module never writes around a provider or guesses a destination revision.

use crate::catalog::{CatalogResult, ResourceKey, VersionReceipt};
use crate::operations::{ActionRequest, HistoryCoordinator, begin};
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestorePlan {
    pub source_action_id: String,
    pub source_version_id: String,
    pub destination: ResourceKey,
    pub expected_destination_fingerprint: Option<String>,
    pub observed_destination_fingerprint: Option<String>,
    pub content: Option<Vec<u8>>,
    #[serde(default)]
    pub source_host_metadata: Option<HostMetadata>,
    pub requires_current_capture: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CurrentStateReceipt {
    pub version_id: String,
    pub fingerprint: String,
    pub content: Vec<u8>,
    pub durable: bool,
    #[serde(default)]
    pub host_metadata: HostMetadata,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum RestoreOutcome {
    Complete,
    Conflict,
    Partial,
    Missing,
    Corrupt,
    Expired,
    MetadataExpired,
    UnknownVersion,
    Unauthorized,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestoreReceipt {
    pub source_action_id: String,
    pub source_version_id: String,
    pub outcome: RestoreOutcome,
    /// The provider outcome for each target in a multi-resource restore. A
    /// single-resource receipt contains one entry as well, so callers never
    /// have to infer whether a partial mutation occurred.
    #[serde(default)]
    pub resources: Vec<RestoreResourceOutcome>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestoreResourceOutcome {
    pub resource_id: String,
    pub outcome: RestoreOutcome,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestoreBatchReceipt {
    pub outcome: RestoreOutcome,
    pub resources: Vec<RestoreResourceOutcome>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManagedEnvelope {
    pub body: Vec<u8>,
    pub properties: serde_json::Value,
    pub relations: serde_json::Value,
    pub extensions: serde_json::Value,
    pub attachments: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct HostMetadata {
    pub mode: Option<u32>,
    pub modified_unix_millis: Option<u64>,
    pub native_locator: Option<String>,
    pub symlink_target: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FidelityManifest {
    pub managed_envelope: bool,
    pub host_metadata: HostMetadata,
    pub unsupported: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestoreRequest {
    pub restore_id: String,
    pub account_id: String,
    pub source_action_id: String,
    pub source_version_id: String,
    pub destination: ResourceKey,
    pub expected_destination_fingerprint: Option<String>,
    pub require_current_capture: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum RestoreState {
    Prepared,
    CurrentCaptured,
    Applying,
    Complete,
    Conflict,
    Partial,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestoreJournal {
    pub restore_id: String,
    pub source_action_id: String,
    pub source_version_id: String,
    pub destination: ResourceKey,
    pub expected_destination_fingerprint: Option<String>,
    pub state: RestoreState,
    pub current_capture: Option<CurrentStateReceipt>,
    pub staged_digest: Option<String>,
    pub outcome: Option<RestoreOutcome>,
    pub lease_ids: Vec<String>,
}

/// Provider boundary for restore. Implementations perform their own atomic
/// compare-and-swap and canonical envelope write; the history package never
/// mutates a provider's backing database or host file directly.
pub trait RestoreProvider {
    fn current_fingerprint(&self) -> CatalogResult<Option<String>>;
    fn capture_current(&mut self) -> CatalogResult<CurrentStateReceipt>;
    fn apply_if_revision(
        &mut self,
        expected_fingerprint: Option<&str>,
        content: &[u8],
    ) -> CatalogResult<()>;

    /// Provider receipt lookup closes the crash window between a successful
    /// CAS and durable restore-journal completion. Providers should return
    /// true only after matching their idempotency token and payload digest.
    fn reconcile_apply(&self, _restore_id: &str, _content_digest: &str) -> CatalogResult<bool> {
        Ok(false)
    }

    /// Providers may atomically pass the restore idempotency token through to
    /// their canonical mutation boundary. The default preserves old adapters.
    fn apply_if_revision_idempotent(
        &mut self,
        _restore_id: &str,
        expected_fingerprint: Option<&str>,
        content: &[u8],
    ) -> CatalogResult<()> {
        self.apply_if_revision(expected_fingerprint, content)
    }

    fn apply_if_revision_idempotent_with_metadata(
        &mut self,
        restore_id: &str,
        expected_fingerprint: Option<&str>,
        content: &[u8],
        _host_metadata: Option<&HostMetadata>,
    ) -> CatalogResult<()> {
        self.apply_if_revision_idempotent(restore_id, expected_fingerprint, content)
    }
}

/// Provider adapter for a host file. It performs compare-and-swap over bytes plus mode,
/// publishes through a same-directory atomic rename, and keeps an idempotency receipt beside the
/// file so a fresh worker can reconcile a commit that happened before its journal write.
pub struct FilesystemRestoreProvider {
    path: PathBuf,
    receipt_path: PathBuf,
}

impl FilesystemRestoreProvider {
    pub fn new(path: impl AsRef<Path>) -> Self {
        let path = path.as_ref().to_path_buf();
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        Self::new_with_receipt_root(&path, parent.join(".openclank-restore-receipts"))
    }

    /// Construct a host provider with a service-owned receipt directory. The
    /// directory must be inaccessible to ordinary destination writers; this
    /// prevents a destination file from forging a post-CAS reconciliation.
    pub fn new_with_receipt_root(path: impl AsRef<Path>, receipt_root: impl AsRef<Path>) -> Self {
        let path = path.as_ref().to_path_buf();
        let key = blake3::hash(path.to_string_lossy().as_bytes()).to_hex().to_string();
        let receipt_path = receipt_root.as_ref().join(format!("{key}.receipt"));
        Self { path, receipt_path }
    }

    fn fingerprint_for(bytes: &[u8], mode: Option<u32>) -> String {
        let mut input = Vec::with_capacity(bytes.len() + 8);
        input.extend_from_slice(bytes);
        input.extend_from_slice(&mode.unwrap_or(0).to_le_bytes());
        digest_content(&input)
    }

    fn mode(&self) -> CatalogResult<Option<u32>> {
        if !self.path.exists() {
            return Ok(None);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            return Ok(Some(std::fs::metadata(&self.path)?.permissions().mode()));
        }
        #[cfg(not(unix))]
        { Ok(None) }
    }

    fn read_current(&self) -> CatalogResult<(Vec<u8>, Option<u32>)> {
        if !self.path.exists() {
            return Ok((Vec::new(), None));
        }
        Ok((std::fs::read(&self.path)?, self.mode()?))
    }
}

impl RestoreProvider for FilesystemRestoreProvider {
    fn current_fingerprint(&self) -> CatalogResult<Option<String>> {
        if !self.path.exists() {
            return Ok(None);
        }
        let (bytes, mode) = self.read_current()?;
        Ok(Some(Self::fingerprint_for(&bytes, mode)))
    }

    fn capture_current(&mut self) -> CatalogResult<CurrentStateReceipt> {
        if !self.path.exists() {
            return Ok(CurrentStateReceipt {
                version_id: "host:missing".into(),
                fingerprint: "missing".into(),
                content: Vec::new(),
                durable: true,
                host_metadata: HostMetadata {
                    native_locator: Some(self.path.to_string_lossy().into_owned()),
                    ..HostMetadata::default()
                },
            });
        }
        let (content, mode) = self.read_current()?;
        let fingerprint = Self::fingerprint_for(&content, mode);
        Ok(CurrentStateReceipt {
            version_id: format!("host:{fingerprint}"),
            fingerprint: fingerprint.clone(),
            content,
            durable: true,
            host_metadata: HostMetadata {
                mode,
                native_locator: Some(self.path.to_string_lossy().into_owned()),
                ..HostMetadata::default()
            },
        })
    }

    fn apply_if_revision(
        &mut self,
        expected_fingerprint: Option<&str>,
        content: &[u8],
    ) -> CatalogResult<()> {
        self.apply_if_revision_idempotent("legacy", expected_fingerprint, content)
    }

    fn reconcile_apply(&self, restore_id: &str, content_digest: &str) -> CatalogResult<bool> {
        let destination_key = blake3::hash(self.path.to_string_lossy().as_bytes()).to_hex();
        let expected = format!("v1\n{destination_key}\n{restore_id}\n{content_digest}");
        Ok(std::fs::read_to_string(&self.receipt_path)
            .ok()
            .is_some_and(|receipt| receipt == expected))
    }

    fn apply_if_revision_idempotent(
        &mut self,
        restore_id: &str,
        expected_fingerprint: Option<&str>,
        content: &[u8],
    ) -> CatalogResult<()> {
        self.apply_if_revision_idempotent_with_metadata(
            restore_id,
            expected_fingerprint,
            content,
            None,
        )
    }

    fn apply_if_revision_idempotent_with_metadata(
        &mut self,
        restore_id: &str,
        expected_fingerprint: Option<&str>,
        content: &[u8],
        host_metadata: Option<&HostMetadata>,
    ) -> CatalogResult<()> {
        let digest = digest_content(content);
        if self.reconcile_apply(restore_id, &digest)? {
            return Ok(());
        }
        if self.current_fingerprint()?.as_deref() != expected_fingerprint {
            return Err("host file compare-and-swap conflict".into());
        }
        let parent = self.path.parent().ok_or("host file has no parent")?;
        std::fs::create_dir_all(parent)?;
        let mode = host_metadata.and_then(|metadata| metadata.mode).or(self.mode()?);
        let restore_key = blake3::hash(restore_id.as_bytes()).to_hex().to_string();
        let stem = format!(".openclank-restore-{}-{}-{}", std::process::id(), restore_key, digest);
        let mut temporary = parent.join(&stem);
        let mut created = false;
        for suffix in 0..32u32 {
            if suffix > 0 {
                temporary = parent.join(format!("{stem}-{suffix}"));
            }
            match std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)
            {
                Ok(mut file) => {
                    use std::io::Write;
                    file.write_all(content)?;
                    file.sync_all()?;
                    created = true;
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error.into()),
            }
        }
        if !created {
            return Err("unable to allocate a unique restore staging file".into());
        }
        #[cfg(unix)]
        if let Some(mode) = mode {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(mode))?;
        }
        std::fs::rename(&temporary, &self.path)?;
        std::fs::File::open(parent)?.sync_all()?;
        let receipt_parent = self.receipt_path.parent().ok_or("receipt has no parent")?;
        std::fs::create_dir_all(receipt_parent)?;
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let receipt_temp = receipt_parent.join(format!(".{}.tmp-{}-{nonce}", self.receipt_path.file_name().unwrap().to_string_lossy(), std::process::id()));
        {
            use std::io::Write;
            let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&receipt_temp)?;
            let destination_key = blake3::hash(self.path.to_string_lossy().as_bytes()).to_hex();
            file.write_all(format!("v1\n{destination_key}\n{restore_id}\n{digest}").as_bytes())?;
            file.sync_all()?;
        }
        std::fs::rename(&receipt_temp, &self.receipt_path)?;
        std::fs::File::open(receipt_parent)?.sync_all()?;
        Ok(())
    }
}

fn restore_lease_ids(request: &RestoreRequest) -> Vec<String> {
    let mut leases = vec![
        format!("restore:{}", request.restore_id),
        request.destination.scoped_lease_id(),
        format!("history-action:{}", request.source_action_id),
    ];
    leases.sort();
    leases.dedup();
    leases
}

fn digest_content(content: &[u8]) -> String {
    blake3::hash(content).to_hex().to_string()
}

#[cfg(feature = "test_faults")]
fn qualification_restore_abort(stage: &str) {
    if std::env::var("OPENCLANK_HISTORY_RESTORE_ABORT_STAGE").ok().as_deref() == Some(stage) {
        std::process::abort();
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct CurrentCaptureProof {
    action_id: String,
    receipt: VersionReceipt,
}

fn pre_restore_action(request: &RestoreRequest) -> ActionRequest {
    ActionRequest {
        schema_version: 1,
        action_id: format!("{}:pre-restore", request.restore_id),
        actor_account_id: request.account_id.clone(),
        resource_key: request.destination.clone(),
        physical_lease_keys: Vec::new(),
        guard_resource_ids: Vec::new(),
        modified_resource_ids: Vec::new(),
        operation: "restore-preimage".into(),
        expected_revision: None,
        actor_id: format!("restore:{}", request.restore_id),
        actor_kind: "restore".into(),
        session_id: None,
        run_id: None,
        task_id: None,
        tool_id: Some("history-restore".into()),
        before_revision: None,
        expected_after_revision: None,
        original_locator: None,
        destination_locator: None,
        timestamp_millis: None,
        coverage: None,
        per_resource_outcomes: None,
    }
}

pub async fn prepare_restore(
    coordinator: &HistoryCoordinator,
    source_action_id: &str,
    source: &VersionReceipt,
    destination: ResourceKey,
    expected_destination_fingerprint: Option<&str>,
    observed_destination_fingerprint: Option<&str>,
    require_current_capture: bool,
) -> CatalogResult<RestorePlan> {
    prepare_restore_with_metadata(
        coordinator,
        source_action_id,
        source,
        destination,
        expected_destination_fingerprint,
        observed_destination_fingerprint,
        require_current_capture,
        None,
    )
    .await
}

/// Variant used by provider adapters that have captured source host metadata
/// alongside the Lore version. Keeping it on the typed plan ensures the
/// metadata reaches the provider's canonical apply boundary.
pub async fn prepare_restore_with_metadata(
    coordinator: &HistoryCoordinator,
    source_action_id: &str,
    source: &VersionReceipt,
    destination: ResourceKey,
    expected_destination_fingerprint: Option<&str>,
    observed_destination_fingerprint: Option<&str>,
    require_current_capture: bool,
    source_host_metadata: Option<HostMetadata>,
) -> CatalogResult<RestorePlan> {
    if let (Some(expected), Some(observed)) = (
        expected_destination_fingerprint,
        observed_destination_fingerprint,
    ) {
        if expected != observed {
            return Err("destination revision conflict".into());
        }
    }
    let content = coordinator
        .read_version(source_action_id, source)
        .await?
        .map(|bytes| bytes.to_vec());
    Ok(RestorePlan {
        source_action_id: source_action_id.into(),
        source_version_id: source.version_id.clone(),
        destination,
        expected_destination_fingerprint: expected_destination_fingerprint.map(str::to_owned),
        observed_destination_fingerprint: observed_destination_fingerprint.map(str::to_owned),
        content,
        source_host_metadata,
        requires_current_capture: require_current_capture,
    })
}

/// Authorized, durable preparation for a restore. The source action's owner
/// and the destination owner must agree, and the exact version id must be
/// present in that action. A journal row and destination root lease are
/// created before the caller can apply anything.
pub async fn prepare_restore_authorized(
    coordinator: &HistoryCoordinator,
    request: &RestoreRequest,
    source: &VersionReceipt,
    observed_destination_fingerprint: Option<&str>,
) -> Result<RestorePlan, RestoreOutcome> {
    let action = coordinator
        .catalog()
        .get_action(&request.source_action_id)
        .map_err(|_| RestoreOutcome::UnknownVersion)?
        .ok_or(RestoreOutcome::UnknownVersion)?;
    if action.resource_key.account_id != request.account_id
        || request.destination.account_id != request.account_id
        || ![action.before.as_ref(), action.after.as_ref()]
            .into_iter()
            .flatten()
            .any(|receipt| receipt.version_id == request.source_version_id && receipt == source)
    {
        return Err(RestoreOutcome::Unauthorized);
    }
    if let Some(expiry) = coordinator
        .catalog()
        .expiry(&request.source_action_id)
        .map_err(|_| RestoreOutcome::UnknownVersion)?
        && expiry.state == crate::retention::ExpiryState::Expired
    {
        return Err(if expiry.metadata_expired {
            RestoreOutcome::MetadataExpired
        } else {
            RestoreOutcome::Expired
        });
    }
    let plan = prepare_restore(
        coordinator,
        &request.source_action_id,
        source,
        request.destination.clone(),
        request.expected_destination_fingerprint.as_deref(),
        observed_destination_fingerprint,
        request.require_current_capture,
    )
    .await
    .map_err(|error| {
        if error.to_string().contains("expired") {
            RestoreOutcome::Expired
        } else {
            RestoreOutcome::Corrupt
        }
    })?;
    let leases = restore_lease_ids(request);
    coordinator
        .catalog()
        .acquire_leases(&request.restore_id, &leases)
        .map_err(|_| RestoreOutcome::Conflict)?;
    let journal = RestoreJournal {
        restore_id: request.restore_id.clone(),
        source_action_id: request.source_action_id.clone(),
        source_version_id: request.source_version_id.clone(),
        destination: request.destination.clone(),
        expected_destination_fingerprint: request.expected_destination_fingerprint.clone(),
        state: RestoreState::Prepared,
        current_capture: None,
        staged_digest: plan.content.as_deref().map(digest_content),
        outcome: None,
        lease_ids: leases,
    };
    coordinator
        .catalog()
        .put_restore(&request.restore_id, &journal)
        .map_err(|_| RestoreOutcome::Corrupt)?;
    #[cfg(feature = "test_faults")]
    qualification_restore_abort("prepared");
    Ok(plan)
}

/// Apply a prepared restore with provider CAS and a durable pre-restore proof.
/// Repeating the same restore id after a completed response is idempotent; a
/// fresh worker can resume after a process interruption from its journal row.
#[cfg(test)]
fn apply_restore_authorized<P: RestoreProvider>(
    coordinator: &HistoryCoordinator,
    request: &RestoreRequest,
    plan: &RestorePlan,
    provider: &mut P,
) -> Result<RestoreReceipt, RestoreOutcome> {
    let mut journal = coordinator
        .catalog()
        .get_restore::<RestoreJournal>(&request.restore_id)
        .map_err(|_| RestoreOutcome::Corrupt)?
        .ok_or(RestoreOutcome::UnknownVersion)?;
    if journal.state == RestoreState::Complete {
        let outcome = journal.outcome.clone().unwrap_or(RestoreOutcome::Complete);
        return Ok(RestoreReceipt {
            source_action_id: journal.source_action_id,
            source_version_id: journal.source_version_id,
            outcome: outcome.clone(),
            resources: vec![RestoreResourceOutcome {
                resource_id: journal.destination.resource_id,
                outcome,
            }],
        });
    }
    let leases = if journal.lease_ids.is_empty() {
        restore_lease_ids(request)
    } else {
        journal.lease_ids.clone()
    };
    coordinator
        .catalog()
        .acquire_leases(&request.restore_id, &leases)
        .map_err(|_| RestoreOutcome::Conflict)?;
    let result = (|| {
        if let Some(expected) = plan.expected_destination_fingerprint.as_deref() {
            if provider.current_fingerprint().map_err(|_| RestoreOutcome::Corrupt)?.as_deref()
                != Some(expected)
            {
                return Err(RestoreOutcome::Conflict);
            }
        }
        if plan.requires_current_capture && journal.current_capture.is_none() {
            let proof = provider
                .capture_current()
                .map_err(|_| RestoreOutcome::Corrupt)?;
            if !proof.durable {
                return Err(RestoreOutcome::Conflict);
            }
            journal.current_capture = Some(proof);
            journal.state = RestoreState::CurrentCaptured;
            coordinator
                .catalog()
                .put_restore(&request.restore_id, &journal)
                .map_err(|_| RestoreOutcome::Corrupt)?;
            #[cfg(feature = "test_faults")]
            qualification_restore_abort("current_captured");
        }
        let Some(content) = plan.content.as_ref() else {
            return Err(RestoreOutcome::Missing);
        };
        journal.state = RestoreState::Applying;
        coordinator
            .catalog()
            .put_restore(&request.restore_id, &journal)
            .map_err(|_| RestoreOutcome::Corrupt)?;
        #[cfg(feature = "test_faults")]
        qualification_restore_abort("applying");
        provider
            .apply_if_revision(plan.expected_destination_fingerprint.as_deref(), content)
            .map_err(|_| RestoreOutcome::Conflict)?;
        #[cfg(feature = "test_faults")]
        qualification_restore_abort("after_provider_commit");
        journal.state = RestoreState::Complete;
        journal.outcome = Some(RestoreOutcome::Complete);
        coordinator
            .catalog()
            .put_restore(&request.restore_id, &journal)
            .map_err(|_| RestoreOutcome::Corrupt)?;
        Ok(RestoreReceipt {
            source_action_id: request.source_action_id.clone(),
            source_version_id: request.source_version_id.clone(),
            outcome: RestoreOutcome::Complete,
            resources: vec![RestoreResourceOutcome {
                resource_id: request.destination.resource_id.clone(),
                outcome: RestoreOutcome::Complete,
            }],
        })
    })();
    let _ = coordinator
        .catalog()
        .release_leases(&request.restore_id, &leases);
    result
}

/// Canonical restore entry point for providers that can await the checked
/// Lore capture. It links the current destination preimage to a durable
/// action/version before CAS and stores a proof row that a fresh worker can
/// reuse after interruption.
pub async fn apply_restore_authorized_durable<P: RestoreProvider + ?Sized>(
    coordinator: &HistoryCoordinator,
    request: &RestoreRequest,
    plan: &RestorePlan,
    provider: &mut P,
) -> Result<RestoreReceipt, RestoreOutcome> {
    let mut journal = coordinator
        .catalog()
        .get_restore::<RestoreJournal>(&request.restore_id)
        .map_err(|_| RestoreOutcome::Corrupt)?
        .ok_or(RestoreOutcome::UnknownVersion)?;
    if journal.state == RestoreState::Complete {
        let outcome = journal.outcome.clone().unwrap_or(RestoreOutcome::Complete);
        return Ok(RestoreReceipt {
            source_action_id: journal.source_action_id,
            source_version_id: journal.source_version_id,
            outcome: outcome.clone(),
            resources: vec![RestoreResourceOutcome {
                resource_id: journal.destination.resource_id,
                outcome,
            }],
        });
    }
    let leases = if journal.lease_ids.is_empty() {
        restore_lease_ids(request)
    } else {
        journal.lease_ids.clone()
    };
    coordinator
        .catalog()
        .acquire_leases(&request.restore_id, &leases)
        .map_err(|_| RestoreOutcome::Conflict)?;
    let result = async {
        let Some(content) = plan.content.as_ref() else {
            return Err(RestoreOutcome::Missing);
        };
        let digest = digest_content(content);
        // Applying is journaled before the provider call. On reopen, reconcile the
        // provider receipt before checking the old destination fingerprint because a
        // committed CAS necessarily changed that fingerprint.
        if journal.state == RestoreState::Applying
            && journal.staged_digest.as_deref() == Some(digest.as_str())
            && provider
                .reconcile_apply(&request.restore_id, &digest)
                .map_err(|_| RestoreOutcome::Corrupt)?
        {
            journal.state = RestoreState::Complete;
            journal.outcome = Some(RestoreOutcome::Complete);
            coordinator
                .catalog()
                .put_restore(&request.restore_id, &journal)
                .map_err(|_| RestoreOutcome::Corrupt)?;
            return Ok(RestoreReceipt {
                source_action_id: request.source_action_id.clone(),
                source_version_id: request.source_version_id.clone(),
                outcome: RestoreOutcome::Complete,
                resources: vec![RestoreResourceOutcome {
                    resource_id: request.destination.resource_id.clone(),
                    outcome: RestoreOutcome::Complete,
                }],
            });
        }
        if let Some(expected) = plan.expected_destination_fingerprint.as_deref() {
            if provider.current_fingerprint().map_err(|_| RestoreOutcome::Corrupt)?.as_deref()
                != Some(expected)
            {
                return Err(RestoreOutcome::Conflict);
            }
        }
        let proof = if let Some(proof) = coordinator
            .catalog()
            .get_restore::<CurrentCaptureProof>(&format!("{}:proof", request.restore_id))
            .map_err(|_| RestoreOutcome::Corrupt)?
        {
            proof
        } else {
            // The restore root lease protects the provider apply. Release the
            // in-memory alias briefly while the preimage action acquires the
            // same destination resource, then reacquire it before staging.
            let _ = coordinator
                .catalog()
                .release_leases(&request.restore_id, &leases);
            let current = provider
                .capture_current()
                .map_err(|_| RestoreOutcome::Corrupt)?;
            if !current.durable {
                return Err(RestoreOutcome::Conflict);
            }
            let action = pre_restore_action(request);
            let begun = begin(coordinator.catalog(), action).map_err(|_| RestoreOutcome::Corrupt)?;
            let proof_action_id = match begun {
                crate::catalog::BeginResult::New(_) | crate::catalog::BeginResult::Existing(_) => {
                    format!("{}:pre-restore", request.restore_id)
                }
            };
            let recorded = coordinator
                .catalog()
                .get_action(&proof_action_id)
                .map_err(|_| RestoreOutcome::Corrupt)?
                .and_then(|record| record.before)
                .map(|receipt| async { Ok::<_, RestoreOutcome>(receipt) });
            let receipt = if let Some(recorded) = recorded {
                recorded.await?
            } else {
                let record = coordinator
                    .capture_before(
                        &proof_action_id,
                        Some(Bytes::from(current.content.clone())),
                        current.fingerprint.clone(),
                    )
                    .await
                    .map_err(|_| RestoreOutcome::Corrupt)?;
                let receipt = record.before.ok_or(RestoreOutcome::Corrupt)?;
                let _ = coordinator.abort(&proof_action_id);
                receipt
            };
            let proof = CurrentCaptureProof {
                action_id: proof_action_id,
                receipt,
            };
            coordinator
                .catalog()
                .put_restore(&format!("{}:proof", request.restore_id), &proof)
                .map_err(|_| RestoreOutcome::Corrupt)?;
            coordinator
                .catalog()
                .acquire_leases(&request.restore_id, &leases)
                .map_err(|_| RestoreOutcome::Conflict)?;
            proof
        };
        // Validate the linked Lore version is still readable before replacing
        // the destination; this is the durable proof, not a provider boolean.
        coordinator
            .read_version(&proof.action_id, &proof.receipt)
            .await
            .map_err(|_| RestoreOutcome::Corrupt)?
            .ok_or(RestoreOutcome::Corrupt)?;
        journal.current_capture = Some(CurrentStateReceipt {
            version_id: proof.receipt.version_id.clone(),
            fingerprint: proof.receipt.fingerprint.clone(),
            content: Vec::new(),
            durable: true,
            host_metadata: HostMetadata::default(),
        });
        journal.state = RestoreState::CurrentCaptured;
        coordinator
            .catalog()
            .put_restore(&request.restore_id, &journal)
            .map_err(|_| RestoreOutcome::Corrupt)?;
        #[cfg(feature = "test_faults")]
        qualification_restore_abort("current_captured");
        journal.state = RestoreState::Applying;
        coordinator
            .catalog()
            .put_restore(&request.restore_id, &journal)
            .map_err(|_| RestoreOutcome::Corrupt)?;
        #[cfg(feature = "test_faults")]
        qualification_restore_abort("applying");
        provider
            .apply_if_revision_idempotent_with_metadata(
                &request.restore_id,
                plan.expected_destination_fingerprint.as_deref(),
                content,
                plan.source_host_metadata.as_ref(),
            )
            .map_err(|_| RestoreOutcome::Conflict)?;
        #[cfg(feature = "test_faults")]
        qualification_restore_abort("after_provider_commit");
        journal.state = RestoreState::Complete;
        journal.outcome = Some(RestoreOutcome::Complete);
        coordinator
            .catalog()
            .put_restore(&request.restore_id, &journal)
            .map_err(|_| RestoreOutcome::Corrupt)?;
        Ok(RestoreReceipt {
            source_action_id: request.source_action_id.clone(),
            source_version_id: request.source_version_id.clone(),
            outcome: RestoreOutcome::Complete,
            resources: vec![RestoreResourceOutcome {
                resource_id: request.destination.resource_id.clone(),
                outcome: RestoreOutcome::Complete,
            }],
        })
    }
    .await;
    let _ = coordinator
        .catalog()
        .release_leases(&request.restore_id, &leases);
    result
}

/// Apply independently prepared resources while preserving every result. A
/// failed target does not prevent other targets from being attempted; the
/// aggregate is Partial when at least one target committed and another did
/// not. Each target still has its own journal, CAS, lease and reconciliation
/// token, so retrying the batch is idempotent.
pub async fn apply_restore_batch_authorized(
    coordinator: &HistoryCoordinator,
    entries: &mut [(&RestoreRequest, &RestorePlan, &mut dyn RestoreProvider)],
) -> RestoreBatchReceipt {
    let mut resources = Vec::with_capacity(entries.len());
    for (request, plan, provider) in entries.iter_mut() {
        let outcome = match apply_restore_authorized_durable(coordinator, request, plan, *provider).await {
            Ok(receipt) => receipt.outcome,
            Err(outcome) => outcome,
        };
        resources.push(RestoreResourceOutcome {
            resource_id: request.destination.resource_id.clone(),
            outcome,
        });
    }
    let committed = resources.iter().any(|entry| entry.outcome == RestoreOutcome::Complete);
    let failed = resources.iter().any(|entry| entry.outcome != RestoreOutcome::Complete);
    let outcome = if resources.is_empty() {
        RestoreOutcome::Missing
    } else if committed && failed {
        RestoreOutcome::Partial
    } else if failed {
        resources
            .first()
            .map(|entry| entry.outcome.clone())
            .unwrap_or(RestoreOutcome::Missing)
    } else {
        RestoreOutcome::Complete
    };
    RestoreBatchReceipt { outcome, resources }
}

pub fn apply_restore<F>(
    plan: &RestorePlan,
    current_capture: Option<&CurrentStateReceipt>,
    mut write: F,
) -> CatalogResult<RestoreReceipt>
where
    F: FnMut(&[u8]) -> CatalogResult<()>,
{
    if plan.requires_current_capture && !current_capture.is_some_and(|receipt| receipt.durable) {
        return Err("protected current-state receipt required".into());
    }
    let Some(content) = plan.content.as_ref() else {
        return Err("source version has no bytes".into());
    };
    write(content)?;
    Ok(RestoreReceipt {
        source_action_id: plan.source_action_id.clone(),
        source_version_id: plan.source_version_id.clone(),
        outcome: RestoreOutcome::Complete,
        resources: vec![RestoreResourceOutcome {
            resource_id: plan.destination.resource_id.clone(),
            outcome: RestoreOutcome::Complete,
        }],
    })
}
